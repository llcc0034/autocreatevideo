from __future__ import annotations

from datetime import timedelta
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import urlparse
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from studio.models import (
    Asset,
    AssetGenerationJob,
    AssetGenerationJobResolution,
    GenerationTask,
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    Style,
    WorkflowSmokeAttempt,
    WorkflowTemplateVersion,
    WorkerLease,
)
from studio.media_signing import classify_task_output
from studio.services.async_media_contract import (
    ASYNC_MEDIA_OUTPUT_KIND,
    async_operation_contract,
    deployment_target_sha256,
    fresh_smoke_snapshot,
    validate_async_model_profile,
    validate_standard_payload,
)
from studio.services.configuration import resolve_configuration
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.media_request_limits import media_prompt_absolute_max_chars
from studio.services.model_profile_contracts import CHAT_REQUEST_OPTIONS_KEY
from studio.services.provenance import ProvenanceError
from studio.services.provider_registry import (
    ProviderDriverError,
    driver_for,
    restricted_async_rest_runtime_budget,
    restricted_comfyui_runtime_budget,
)
from studio.services.production_pipeline import can_execute_template
from studio.services.remote_artifacts import download_remote_artifact
from studio.services.script_generation_contract import (
    SCRIPT_GENERATE_OPERATION,
    validate_script_generation_model_profile,
)
from studio.services.security_boundaries import boundary_int
from studio.services.content_contracts import normalize_audit_note
from studio.services.media_processing_profiles import resolve_reference_media_profile
from studio.services.workflow_smoke_fixtures import (
    FIXTURE_SCHEMA_VERSION,
    normalize_workflow_execution_inputs,
    workflow_execution_conditioning_contract,
    workflow_execution_input_schema,
)
from studio.services.workflow_versions import (
    declared_workflow_operation,
    materialize_workflow_execution_graph,
    resolve_workflow_canvas_export_deployment,
    verify_published_workflow_capability,
    workflow_execution_target_sha256,
)
from studio.services.workflow_smoke_resolution import (
    unresolved_workflow_smoke_attempts,
)
from studio.services.task_queue import ComfyUIError, prepare_workflow


MEDIA_OPERATION = {
    AssetGenerationJob.MediaType.IMAGE: "image.text_to_image",
    AssetGenerationJob.MediaType.VIDEO: "video.text_to_video",
    AssetGenerationJob.MediaType.AUDIO: "audio.text_to_audio",
}


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _text_provider_request_max_bytes() -> int:
    return boundary_int("security.snapshot.text_provider_request_max_bytes")


def _workflow_graph_max_bytes() -> int:
    return boundary_int("security.workflow.max_graph_bytes")
IMAGE_CATEGORIES = {
    Asset.Kind.CHARACTER,
    Asset.Kind.SCENE,
    Asset.Kind.PROP,
    Asset.Kind.REFERENCE,
}
TEXT_JOB_STATUSES = (
    AssetGenerationJob.Status.QUEUED,
    AssetGenerationJob.Status.CANCEL_REQUESTED,
)
MEDIA_JOB_STATUSES = (
    AssetGenerationJob.Status.QUEUED,
    AssetGenerationJob.Status.SUBMITTED,
    AssetGenerationJob.Status.RUNNING,
    AssetGenerationJob.Status.CANCEL_REQUESTED,
)


def _workflow_user_parameters(contract: dict) -> tuple[dict, dict]:
    definitions = contract.get("parameters") if isinstance(contract, dict) else None
    definitions = definitions if isinstance(definitions, dict) else {}
    smoke_inputs = contract.get("smoke_inputs") if isinstance(contract, dict) else None
    smoke_inputs = smoke_inputs if isinstance(smoke_inputs, dict) else {}
    editable: dict[str, dict] = {}
    defaults: dict[str, object] = {}
    for key, raw_definition in definitions.items():
        key = str(key or "")
        if (
            not key
            or key.startswith("prompt.")
            or key.startswith("model.")
            or key.startswith("asset.")
            or key == "output.prefix"
            or key == "generation.batch_size"
        ):
            continue
        definition = raw_definition if isinstance(raw_definition, dict) else {}
        value_type = str(definition.get("type") or "")
        if value_type not in {"string", "integer", "number", "boolean", "enum"}:
            continue
        editable[key] = {
            field: definition[field]
            for field in (
                "type", "required", "default", "min", "max", "multiple_of",
                "min_length", "max_length", "options",
            )
            if field in definition
        }
        if key in smoke_inputs:
            value = smoke_inputs[key]
        elif "default" in definition:
            value = definition["default"]
        else:
            continue
        # A canvas-exported value can be a valid-looking default yet violate a
        # node's declared step/alignment constraint. The same normalization is applied to
        # smoke fixtures; apply it here as well so the form's untouched values
        # are executable through the asset-generation route.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = definition.get("min")
            maximum = definition.get("max")
            if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
                value = max(value, minimum)
            if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
                value = min(value, maximum)
            multiple = definition.get("multiple_of")
            if isinstance(multiple, int) and multiple > 1:
                value = max(multiple, int(round(float(value) / multiple)) * multiple)
                if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
                    value = min(value, int(maximum))
            if value_type == "integer":
                value = int(value)
        defaults[key] = value
    return editable, defaults


def _eligible_workflow_route(
    version: WorkflowTemplateVersion,
    *,
    user,
    media_type: str,
) -> tuple[WorkflowTemplateVersion, ModelDeployment, WorkflowSmokeAttempt, str]:
    if media_type not in MEDIA_OPERATION:
        raise ValidationError("该素材类型不支持 ComfyUI 工作流")
    version, deployment, _source = resolve_workflow_canvas_export_deployment(version)
    if version.template.output_kind != media_type:
        raise ValidationError("工作流必须是当前已发布、已烟测且输出类型匹配的版本")
    primary = (
        (version.input_contract.get("outputs") or {}).get("primary")
        if isinstance(version.input_contract, dict) else None
    )
    primary_node = (
        version.graph.get(str(primary.get("node_id") or ""))
        if isinstance(primary, dict) and isinstance(version.graph, dict) else None
    )
    primary_class = str(primary_node.get("class_type") or "") if isinstance(primary_node, dict) else ""
    durable_output_nodes = {
        AssetGenerationJob.MediaType.IMAGE: {"SaveImage", "SaveImageAdvanced"},
        AssetGenerationJob.MediaType.VIDEO: {"SaveVideo", "VHS_VideoCombine"},
        AssetGenerationJob.MediaType.AUDIO: {"SaveAudioAdvanced"},
    }
    if primary_class not in durable_output_nodes.get(media_type, set()):
        raise ValidationError("素材库工作流主输出必须是受支持的持久 Save 节点")
    if not can_execute_template(version.template, user):
        raise ValidationError("当前用户无权执行该工作流")
    operation = declared_workflow_operation(version)
    smoke, target_sha256 = verify_published_workflow_capability(
        version,
        deployment,
        operation=operation,
    )
    return version, deployment, smoke, target_sha256


def _combined_config(deployment: ModelDeployment) -> dict:
    return {
        **(deployment.profile_version.provider.driver_config or {}),
        **(deployment.runtime_config or {}),
    }


def _is_external(deployment: ModelDeployment) -> bool:
    hostname = (
        urlparse(str(_combined_config(deployment).get("base_url") or "")).hostname
        or ""
    ).casefold()
    if not hostname:
        raise ValidationError("模型部署缺少有效 base_url")
    return hostname not in {"localhost", "127.0.0.1", "::1"}


def _eligible_deployment(
    deployment: ModelDeployment,
    *,
    job_type: str,
    media_type: str,
    ) -> tuple[str, dict | None, object, dict | None]:
    if (
        deployment.status != ModelDeployment.Status.ACTIVE
        or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
        or not deployment.provider.is_active
    ):
        raise ValidationError("模型部署未处于已发布、启用状态")
    if job_type == AssetGenerationJob.JobType.PROMPT or media_type == AssetGenerationJob.MediaType.SCRIPT:
        policy = validate_script_generation_model_profile(deployment.profile_version)
        if policy is None:
            raise ValidationError("文本部署未声明 script.generate 受控能力")
        operation = SCRIPT_GENERATE_OPERATION
        contract = None
        text_policy = policy
    else:
        operation = MEDIA_OPERATION.get(media_type)
        if operation is None:
            raise ValidationError("素材类型没有对应的受控生成能力")
        if deployment.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
            raise ValidationError("媒体素材生成只接受 restricted async REST deployment")
        if operation not in validate_async_model_profile(deployment.profile_version):
            raise ValidationError(f"模型部署未声明 {operation}")
        contract = async_operation_contract(deployment.profile_version, operation)
        text_policy = None
    smoke = fresh_smoke_snapshot(deployment, operation)
    if smoke is None:
        raise ValidationError(f"模型部署缺少 {operation} 的新鲜真实烟测证据")
    return operation, contract, smoke, text_policy


def asset_generation_options(*, user, job_type: str, media_type: str) -> list[dict]:
    rows: list[dict] = []
    deployments = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).filter(
        status=ModelDeployment.Status.ACTIVE,
        profile_version__status=ModelProfileVersion.Status.PUBLISHED,
        profile_version__provider__is_active=True,
    )
    for deployment in deployments:
        try:
            operation, contract, smoke, text_policy = _eligible_deployment(
                deployment,
                job_type=job_type,
                media_type=media_type,
            )
            external = _is_external(deployment)
        except (ValidationError, ProviderDriverError, ValueError, TypeError):
            continue
        rows.append({
            "target_kind": AssetGenerationJob.ExecutionKind.DEPLOYMENT,
            "target_id": str(deployment.id),
            "key": deployment.key,
            "name": deployment.name,
            "model_deployment_id": str(deployment.id),
            "model_deployment_name": deployment.name,
            "model_identifier": deployment.profile_version.model_identifier,
            "operation": operation,
            "is_external": external,
            "smoke_evidence_id": str(smoke.id),
            "smoke_expires_at": smoke.expires_at,
            "request_schema": contract["request_schema"] if contract else None,
            "parameter_defaults": {
                key: value for key, value in (
                    contract["smoke_inputs"] if contract else {
                        "max_tokens": int((text_policy or {}).get("max_output_tokens") or 1),
                    }
                ).items()
                if key not in {"prompt", "negative_prompt", "model"}
            },
            "workflow_input_schema": None,
            "accepts_negative_prompt": bool(
                contract
                and "negative_prompt" in contract["request_schema"]["properties"]
            ),
            "workflow_version_id": None,
            "workflow_name": "",
            "workflow_version": None,
        })
    if (
        job_type == AssetGenerationJob.JobType.ASSET
        and media_type in MEDIA_OPERATION
    ):
        versions = WorkflowTemplateVersion.objects.select_related(
            "template", "provider", "model_profile_version",
            "validated_deployment__profile_version__provider",
        ).prefetch_related(
            "model_requirements__artifact", "semantic_bindings", "smoke_attempts",
        ).filter(
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            is_current=True,
            template__output_kind=media_type,
        )
        for candidate in versions:
            try:
                version, deployment, smoke, _target_sha = _eligible_workflow_route(
                    candidate,
                    user=user,
                    media_type=media_type,
                )
                external = _is_external(deployment)
                input_schema = workflow_execution_input_schema(version)
            except (ValidationError, ProviderDriverError, ValueError, TypeError):
                continue
            contract_parameters = (
                version.input_contract.get("parameters")
                if isinstance(version.input_contract, dict) else {}
            )
            rows.append({
                "target_kind": AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
                "target_id": str(version.id),
                "key": f"workflow:{version.template.slug}:v{version.version}",
                "name": f"{version.template.name} v{version.version}",
                "model_deployment_id": str(deployment.id),
                "model_deployment_name": deployment.name,
                "model_identifier": deployment.profile_version.model_identifier,
                "operation": declared_workflow_operation(version),
                "is_external": external,
                "smoke_evidence_id": str(smoke.id),
                "smoke_expires_at": None,
                "request_schema": None,
                "parameter_defaults": {},
                "workflow_input_schema": input_schema,
                "accepts_negative_prompt": bool(
                    isinstance(contract_parameters, dict)
                    and "prompt.negative" in contract_parameters
                ),
                "workflow_version_id": str(version.id),
                "workflow_name": version.template.name,
                "workflow_version": version.version,
            })
    return rows


def _text_payload(
    *,
    job_type: str,
    media_type: str,
    prompt: str,
    style: Style | None,
    parameters: dict,
    policy: dict,
) -> dict:
    allowed = {"temperature", "max_tokens", "seed"}
    unknown = sorted(set(parameters) - allowed)
    if unknown:
        raise ValidationError({"parameters": f"文本模型参数不受支持: {', '.join(unknown)}"})
    normalized = dict(parameters)
    temperature = normalized.pop("temperature", None)
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValidationError({"parameters": "temperature 必须是非负有限数值"})
        if not math.isfinite(float(temperature)) or float(temperature) < 0:
            raise ValidationError({"parameters": "temperature 必须是非负有限数值"})
    seed = normalized.get("seed")
    if seed is not None and (type(seed) is not int or not -(2**63) <= seed < 2**63):
        raise ValidationError({"parameters": "seed 必须是 64 位有符号整数"})
    max_allowed = int(policy["max_output_tokens"])
    max_tokens = normalized.get("max_tokens", max_allowed)
    if type(max_tokens) is not int or not 1 <= max_tokens <= max_allowed:
        raise ValidationError({
            "parameters": f"max_tokens 必须是 1 到已发布策略上限 {max_allowed} 的整数",
        })
    normalized["max_tokens"] = max_tokens
    media_label = {
        AssetGenerationJob.MediaType.IMAGE: "图片",
        AssetGenerationJob.MediaType.VIDEO: "视频",
        AssetGenerationJob.MediaType.AUDIO: "音频",
        AssetGenerationJob.MediaType.SCRIPT: "剧本",
    }[media_type]
    if job_type == AssetGenerationJob.JobType.PROMPT:
        system = (
            f"你是短剧素材提示词编辑。根据需求生成一份可直接编辑和用于{media_label}模型的中文提示词。"
            "只输出最终提示词正文，不要输出解释、标题或代码围栏。"
        )
    else:
        system = (
            "你是短剧剧本作者。根据创作需求生成完整、可人工审阅的 Markdown 剧本。"
            "只输出剧本正文，不要输出解释或代码围栏。"
        )
    style_context = ""
    if style is not None:
        style_context = (
            f"\n风格：{style.name}\n风格说明：{style.description}"
            f"\n视觉提示：{style.visual_prompt}\n负面提示：{style.negative_prompt}"
        )
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"创作需求：{prompt}{style_context}"},
        ],
        "timeout_seconds": float(policy["timeout_seconds"]),
        CHAT_REQUEST_OPTIONS_KEY: (
            {"temperature": float(temperature)}
            if temperature is not None else {}
        ),
        **normalized,
    }
    json_contract(payload, max_bytes=_text_provider_request_max_bytes())
    return payload


def _media_payload(
    *,
    deployment: ModelDeployment,
    contract: dict,
    prompt: str,
    negative_prompt: str,
    parameters: dict,
    idempotency_key: str,
) -> dict:
    protected = {"model", "prompt", "negative_prompt", "schema_version", "idempotency_key"}
    overlap = sorted(set(parameters) & protected)
    if overlap:
        raise ValidationError({"parameters": f"这些字段由服务端冻结: {', '.join(overlap)}"})
    payload = {
        **parameters,
        "model": deployment.profile_version.model_identifier,
        "prompt": prompt,
        "schema_version": 1,
        "idempotency_key": idempotency_key,
    }
    if "negative_prompt" in contract["request_schema"]["properties"]:
        payload["negative_prompt"] = negative_prompt
    return validate_standard_payload(contract, payload)


def _decimal_amount(value, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "必须是非负有限金额"}) from exc
    if not amount.is_finite() or amount < 0:
        raise ValidationError({field: "必须是非负有限金额"})
    return amount


@transaction.atomic
def create_asset_generation_job(
    *,
    owner,
    job_type: str,
    media_type: str,
    prompt: str,
    execution_kind: str = AssetGenerationJob.ExecutionKind.DEPLOYMENT,
    deployment: ModelDeployment | None = None,
    workflow_version: WorkflowTemplateVersion | None = None,
    negative_prompt: str = "",
    parameters: dict | None = None,
    workflow_inputs: dict | None = None,
    style: Style | None = None,
    image_category: str = "",
    name: str = "",
    description: str = "",
    allow_external_calls: bool = False,
    budget_limit=None,
    client_idempotency_key: str = "",
) -> tuple[AssetGenerationJob, bool]:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValidationError({"prompt": "创作需求或最终提示词不能为空"})
    prompt_limit = media_prompt_absolute_max_chars()
    if (
        len(prompt) > prompt_limit
        or len(str(negative_prompt or "")) > prompt_limit
    ):
        raise ValidationError({
            "prompt": f"提示词不能超过当前安全边界 {prompt_limit} 字符",
        })
    parameters = parameters if isinstance(parameters, dict) else {}
    workflow_inputs = workflow_inputs if isinstance(workflow_inputs, dict) else {}
    if style is not None and style.owner_id not in (None, owner.id) and not owner.is_staff:
        raise ValidationError({"style_id": "不能使用其他用户的风格"})
    if job_type not in AssetGenerationJob.JobType.values:
        raise ValidationError({"job_type": "任务类型无效"})
    if media_type not in AssetGenerationJob.MediaType.values:
        raise ValidationError({"media_type": "素材类型无效"})
    if execution_kind not in AssetGenerationJob.ExecutionKind.values:
        raise ValidationError({"execution_kind": "执行方案类型无效"})
    if execution_kind == AssetGenerationJob.ExecutionKind.DEPLOYMENT:
        if deployment is None or workflow_version is not None:
            raise ValidationError("模型部署方案必须且只能指定 model deployment")
        if workflow_inputs:
            raise ValidationError({"workflow_inputs": "模型部署方案不接受工作流输入"})
        target_id = str(deployment.pk)
    else:
        if workflow_version is None or deployment is not None:
            raise ValidationError("ComfyUI 工作流方案必须且只能指定 workflow version")
        if job_type != AssetGenerationJob.JobType.ASSET:
            raise ValidationError("提示词候选不能使用 ComfyUI 工作流方案")
        if parameters:
            raise ValidationError({
                "parameters": "ComfyUI 工作流参数统一放入 workflow_inputs.values",
            })
        target_id = str(workflow_version.pk)
    if job_type == AssetGenerationJob.JobType.ASSET:
        if not str(name or "").strip():
            raise ValidationError({"name": "生成素材必须填写名称"})
        if media_type == AssetGenerationJob.MediaType.IMAGE:
            image_category = str(image_category or Asset.Kind.REFERENCE)
            if image_category not in IMAGE_CATEGORIES:
                raise ValidationError({"image_category": "图片类别只支持人物、场景、道具或通用"})
            if image_category != Asset.Kind.REFERENCE and not str(description or "").strip():
                raise ValidationError({"description": "分类图片素材必须填写基础描述"})
        elif image_category:
            raise ValidationError({"image_category": "只有图片素材可以设置图片类别"})

    budget = _decimal_amount(budget_limit, field="budget_limit")
    if client_idempotency_key:
        raw_key = str(client_idempotency_key).strip()
        if not raw_key or len(raw_key) > 191:
            raise ValidationError("Idempotency-Key 长度必须在 1 到 191 之间")
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        idempotency_key = f"asset-generation:{owner.id}:{key_hash}"
    else:
        idempotency_key = f"asset-generation:{uuid.uuid4()}"

    # This fingerprint represents only the user's stable intent.  Live smoke,
    # pricing and deployment evidence are frozen separately below and must not
    # make an HTTP replay create a second billable request.
    client_intent = json_contract({
        "schema_version": 3,
        "job_type": job_type,
        "media_type": media_type,
        "execution_kind": execution_kind,
        "generation_target_id": target_id,
        "style_id": str(style.id) if style else None,
        "image_category": image_category,
        "name": str(name or "")[:160],
        "description": str(description or ""),
        "prompt": prompt,
        "negative_prompt": str(negative_prompt or ""),
        "parameters": parameters,
        "workflow_inputs": workflow_inputs,
        "allow_external_calls": bool(allow_external_calls),
        "budget_limit": str(budget) if budget is not None else None,
    }, max_bytes=_stage_snapshot_max_bytes())
    existing = AssetGenerationJob.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        existing_fingerprint = str(
            (existing.request_snapshot or {}).get("client_request_sha256") or ""
        )
        if existing.owner_id != owner.id or existing_fingerprint != client_intent.sha256:
            raise ValidationError("Idempotency-Key 已用于不同的素材生成请求")
        return existing, False

    workflow_smoke = None
    workflow_target_sha = ""
    if execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
        workflow_version = WorkflowTemplateVersion.objects.select_for_update().get(
            pk=workflow_version.pk,
        )
        workflow_version, deployment, workflow_smoke, workflow_target_sha = (
            _eligible_workflow_route(
                workflow_version,
                user=owner,
                media_type=media_type,
            )
        )
        operation = declared_workflow_operation(workflow_version)
        contract = None
        smoke = None
        text_policy = None
    else:
        deployment = ModelDeployment.objects.select_for_update().select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        operation, contract, smoke, text_policy = _eligible_deployment(
            deployment,
            job_type=job_type,
            media_type=media_type,
        )
    external = _is_external(deployment)
    if (
        execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
        and external
    ):
        raise ValidationError(
            "远程 ComfyUI 尚无受控产物下载合同；当前只允许共享本地输出目录的 loopback 部署",
        )
    if external and not allow_external_calls:
        raise ValidationError({"allow_external_calls": "外部模型调用必须显式授权"})
    if external and budget is None:
        raise ValidationError({"budget_limit": "外部模型调用必须设置预算上限"})

    job_id = uuid.uuid4()
    compiled = None
    frozen_workflow_inputs = None
    conditioning_contract = None
    conditioning_mode_contract = None
    reference_media_profile = None
    server_managed_values: dict[str, object] = {}
    if execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
        frozen_workflow_inputs = normalize_workflow_execution_inputs(
            workflow_version,
            workflow_inputs,
            owner=owner,
        )
        normalized_parameters = dict(frozen_workflow_inputs.values)
        values = {
            **normalized_parameters,
            "prompt.positive": prompt,
            "prompt.negative": str(negative_prompt or ""),
        }
        if workflow_version.semantic_bindings.filter(
            semantic_key="generation.batch_size",
        ).exists():
            values["generation.batch_size"] = 1
            server_managed_values["generation.batch_size"] = 1
        output_prefix = f"autoprompt/assets/{owner.id}/{job_id}"
        compiled = materialize_workflow_execution_graph(
            workflow_version,
            values=values,
            reference_bindings=list(frozen_workflow_inputs.reference_bindings),
            asset_facts=list(frozen_workflow_inputs.asset_facts),
            output_prefix=output_prefix,
            owner_id=owner.id,
        )
        conditioning_contract, conditioning_mode_contract = (
            workflow_execution_conditioning_contract(
                workflow_version,
                normalized_parameters,
            )
        )
        if conditioning_mode_contract is not None:
            reference_media_profile = resolve_reference_media_profile()
        primary = compiled.output_contract.get("primary")
        if (
            not isinstance(primary, dict)
            or primary.get("kind") != media_type
            or not str(primary.get("node_id") or "")
        ):
            raise ValidationError("工作流主输出合同与素材类型不一致")
        payload = {
            "workflow": compiled.graph,
            "client_id": f"autoprompt-asset-{job_id}",
            "submission_token": str(job_id),
        }
        parameters = normalized_parameters
        output_kind = media_type
    elif job_type == AssetGenerationJob.JobType.PROMPT or media_type == AssetGenerationJob.MediaType.SCRIPT:
        payload = _text_payload(
            job_type=job_type,
            media_type=media_type,
            prompt=prompt,
            style=style,
            parameters=parameters,
            policy=text_policy or {},
        )
        output_kind = "text"
    else:
        payload = _media_payload(
            deployment=deployment,
            contract=contract,
            prompt=prompt,
            negative_prompt=str(negative_prompt or ""),
            parameters=parameters,
            idempotency_key=idempotency_key,
        )
        output_kind = ASYNC_MEDIA_OUTPUT_KIND[operation]

    estimate = driver_for(deployment.provider).estimate(deployment, operation, payload)
    if not isinstance(estimate, dict) or estimate.get("amount") is None:
        raise ValidationError("素材生成前必须能确定模型请求费用")
    estimated = _decimal_amount(estimate.get("amount"), field="estimated_cost")
    currency = str(estimate.get("currency") or "").strip().upper()[:12]
    if estimated and not currency:
        raise ValidationError("模型费用缺少币种")
    if budget is not None and estimated is not None and estimated > budget:
        raise ValidationError({"budget_limit": "预算上限低于本次预估费用"})

    target_sha = (
        workflow_target_sha
        if execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
        else deployment_target_sha256(deployment)
    )
    execution_config = _combined_config(deployment)
    async_runtime_budget = (
        restricted_async_rest_runtime_budget(execution_config)
        if contract is not None
        else None
    )
    comfy_runtime_budget = (
        restricted_comfyui_runtime_budget(execution_config)
        if execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
        else None
    )
    snapshot_payload = {
        "schema_version": (
            3
            if execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
            else 1
        ),
        "client_request_sha256": client_intent.sha256,
        "job_type": job_type,
        "media_type": media_type,
        "execution_kind": execution_kind,
        "operation": operation,
        "output_kind": output_kind,
        "deployment_id": str(deployment.id),
        "payload": payload,
        "style_id": str(style.id) if style else None,
        "image_category": image_category,
        "name": str(name or "")[:160],
        "description": str(description or ""),
        "prompt": prompt,
        "negative_prompt": str(negative_prompt or ""),
        "is_external": external,
        "estimated_cost": str(estimated) if estimated is not None else None,
        "cost_currency": currency,
        "max_poll_seconds": (
            int(async_runtime_budget["max_poll_seconds"])
            if async_runtime_budget is not None
            else int(comfy_runtime_budget["max_poll_seconds"])
            if comfy_runtime_budget is not None
            else None
        ),
        "text_timeout_seconds": (
            float((text_policy or {}).get("timeout_seconds") or 0)
            if text_policy is not None else None
        ),
    }
    if execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
        reference_bindings = list(frozen_workflow_inputs.reference_bindings)
        asset_facts = list(frozen_workflow_inputs.asset_facts)
        asset_bindings = list(compiled.asset_bindings)
        reference_bindings_contract = json_contract(
            reference_bindings,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        asset_facts_contract = json_contract(
            asset_facts,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        asset_bindings_contract = json_contract(
            asset_bindings,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        snapshot_payload.update({
            "workflow_version_id": str(workflow_version.id),
            "workflow_version": workflow_version.version,
            "workflow_graph_sha256": workflow_version.graph_sha256,
            "workflow_execution_contract_sha256": (
                workflow_version.execution_contract_sha256
            ),
            "workflow_validation_evidence_sha256": (
                workflow_version.validation_evidence_sha256
            ),
            "workflow_smoke_attempt_id": str(workflow_smoke.id),
            "workflow_target_sha256": target_sha,
            "compiled_graph_sha256": compiled.graph_sha256,
            "workflow_hash": compiled.graph_sha256,
            "workflow_contract_sha256": workflow_version.execution_contract_sha256,
            "workflow_compiler_id": workflow_version.compiler_id,
            "workflow_compiler_version": workflow_version.compiler_version,
            "compiled_output_contract": compiled.output_contract,
            "output_prefix": output_prefix,
            "parameter_values": parameters,
            "workflow_input_request": frozen_workflow_inputs.request,
            "server_managed_values": server_managed_values,
            "reference_bindings": reference_bindings,
            "reference_bindings_sha256": reference_bindings_contract.sha256,
            "asset_facts": asset_facts,
            "asset_facts_sha256": asset_facts_contract.sha256,
            "asset_bindings": asset_bindings,
            "asset_bindings_sha256": asset_bindings_contract.sha256,
        })
        if conditioning_contract is not None and conditioning_mode_contract is not None:
            snapshot_payload.update({
                "conditioning_contract": conditioning_contract,
                "conditioning_contract_sha256": json_contract(
                    conditioning_contract,
                    max_bytes=_stage_snapshot_max_bytes(),
                ).sha256,
                "conditioning_mode_contract": conditioning_mode_contract,
                "conditioning_mode_contract_sha256": json_contract(
                    conditioning_mode_contract,
                    max_bytes=_stage_snapshot_max_bytes(),
                ).sha256,
                "effective_conditioning_mode": str(
                    conditioning_mode_contract.get("key") or ""
                ),
                "reference_media_profile": reference_media_profile.value,
                "reference_media_profile_sha256": reference_media_profile.sha256,
                "reference_media_profile_configuration_sha256": (
                    reference_media_profile.configuration_sha256
                ),
            })
    else:
        snapshot_payload.update({
            "deployment_target_sha256": target_sha,
            "smoke_snapshot_id": str(smoke.id),
            "smoke_expires_at": smoke.expires_at.isoformat(),
        })
    snapshot = json_contract(
        snapshot_payload,
        schema_version=int(snapshot_payload["schema_version"]),
        max_bytes=_stage_snapshot_max_bytes(),
    )
    try:
        # Keep the unique-key race inside its own savepoint.  Without the
        # nested atomic block, PostgreSQL marks this outer transaction as
        # broken after IntegrityError and the idempotent lookup below cannot
        # run.
        with transaction.atomic():
            job = AssetGenerationJob.objects.create(
                id=job_id,
                owner=owner,
                style=style,
                model_deployment=deployment,
                workflow_template_version=workflow_version,
                job_type=job_type,
                media_type=media_type,
                execution_kind=execution_kind,
                image_category=image_category,
                name=str(name or "")[:160],
                description=str(description or ""),
                prompt=prompt,
                negative_prompt=str(negative_prompt or ""),
                parameters=parameters,
                status=AssetGenerationJob.Status.QUEUED,
                operation=operation,
                is_external=external,
                allow_external_calls=bool(allow_external_calls),
                budget_limit=budget,
                estimated_cost=estimated,
                cost_currency=currency,
                request_snapshot=snapshot.value,
                request_snapshot_schema_version=snapshot.schema_version,
                request_snapshot_size_bytes=snapshot.size_bytes,
                request_snapshot_sha256=snapshot.sha256,
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        existing = AssetGenerationJob.objects.get(idempotency_key=idempotency_key)
        existing_fingerprint = str(
            (existing.request_snapshot or {}).get("client_request_sha256") or ""
        )
        if existing.owner_id != owner.id or existing_fingerprint != client_intent.sha256:
            raise ValidationError("Idempotency-Key 已用于不同的素材生成请求")
        return existing, False
    return job, True


def _snapshot(job: AssetGenerationJob) -> dict:
    snapshot = verify_json_contract(
        job.request_snapshot,
        schema_version=job.request_snapshot_schema_version,
        size_bytes=job.request_snapshot_size_bytes,
        sha256=job.request_snapshot_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    ).value
    schema_version = snapshot.get("schema_version") if isinstance(snapshot, dict) else None
    valid = (
        isinstance(snapshot, dict)
        and schema_version in {1, 2, 3}
        and snapshot.get("operation") == job.operation
        and str(snapshot.get("deployment_id")) == str(job.model_deployment_id)
        and isinstance(snapshot.get("payload"), dict)
    )
    if schema_version in {2, 3}:
        valid = bool(
            valid
            and job.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
            and job.workflow_template_version_id is not None
            and str(snapshot.get("workflow_version_id"))
            == str(job.workflow_template_version_id)
            and snapshot.get("execution_kind")
            == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
        )
    elif schema_version == 1:
        valid = bool(
            valid
            and job.execution_kind == AssetGenerationJob.ExecutionKind.DEPLOYMENT
            and job.workflow_template_version_id is None
        )
    if not valid:
        raise ValidationError("素材生成任务快照无效")
    return snapshot


def _frozen_max_poll_seconds(snapshot: dict) -> int:
    value = snapshot.get("max_poll_seconds")
    boundary_key = (
        "security.runtime.max_timeout_seconds"
        if snapshot.get("execution_kind") == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
        else "security.provider.async_rest_max_poll_seconds"
    )
    maximum = boundary_int(boundary_key, minimum=1)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValidationError(
            "媒体生成请求快照缺少有限的 max_poll_seconds 运行预算",
        )
    return value


def _verify_live_target(
    job: AssetGenerationJob,
    snapshot: dict,
    *,
    require_executable_route: bool = True,
) -> ModelDeployment:
    if job.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
        if job.workflow_template_version_id is None:
            raise ValidationError("ComfyUI 素材任务缺少工作流版本")
        version = WorkflowTemplateVersion.objects.select_related(
            "template", "validated_deployment__profile_version__provider",
        ).prefetch_related("model_requirements__artifact").get(
            pk=job.workflow_template_version_id,
        )
        if require_executable_route:
            version, deployment, smoke, target_sha = _eligible_workflow_route(
                version,
                user=job.owner,
                media_type=job.media_type,
            )
            smoke_matches = str(smoke.id) == str(
                snapshot.get("workflow_smoke_attempt_id"),
            )
        else:
            deployment = version.validated_deployment
            if deployment is None:
                raise ValidationError("ComfyUI 冻结任务缺少 validated deployment")
            if (
                deployment.status != ModelDeployment.Status.ACTIVE
                or not deployment.provider.is_active
                or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
                or deployment.provider.driver_type != ProviderProfile.DriverType.COMFYUI
            ):
                raise ValidationError("ComfyUI 冻结部署当前不可用于远端状态对账")
            target_sha = workflow_execution_target_sha256(version, deployment)
            smoke_matches = True
        if (
            deployment.id != job.model_deployment_id
            or not smoke_matches
            or target_sha != snapshot.get("workflow_target_sha256")
            or version.graph_sha256 != snapshot.get("workflow_graph_sha256")
            or version.execution_contract_sha256
            != snapshot.get("workflow_execution_contract_sha256")
            or version.validation_evidence_sha256
            != snapshot.get("workflow_validation_evidence_sha256")
        ):
            raise ValidationError("ComfyUI 工作流、Smoke 或部署证据已改变；冻结任务拒绝执行")
        return deployment
    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=job.model_deployment_id)
    if (
        deployment.status != ModelDeployment.Status.ACTIVE
        or not deployment.provider.is_active
        or deployment_target_sha256(deployment) != snapshot.get("deployment_target_sha256")
    ):
        raise ValidationError("模型部署或凭据已改变；冻结任务拒绝执行")
    return deployment


@transaction.atomic
def _claim(worker_id: str, *, text: bool) -> AssetGenerationJob | None:
    now = timezone.now()
    queryset = AssetGenerationJob.objects.select_for_update(skip_locked=True).select_related(
        "model_deployment__profile_version__provider", "workflow_template_version",
        "owner", "style",
    ).filter(claim_token__isnull=True)
    if text:
        queryset = queryset.filter(
            execution_kind=AssetGenerationJob.ExecutionKind.DEPLOYMENT,
            operation=SCRIPT_GENERATE_OPERATION,
            status__in=TEXT_JOB_STATUSES,
        )
    else:
        queryset = queryset.filter(
            execution_kind=AssetGenerationJob.ExecutionKind.DEPLOYMENT,
            operation__in=tuple(MEDIA_OPERATION.values()),
            status__in=MEDIA_JOB_STATUSES,
        ).filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
    job = queryset.order_by("created_at").first()
    if job is None:
        return None
    job.claim_token = uuid.uuid4()
    job.worker_id = str(worker_id)[:160]
    job.claimed_at = now
    job.heartbeat_at = now
    job.save(update_fields=(
        "claim_token", "worker_id", "claimed_at", "heartbeat_at", "updated_at",
    ))
    return job


def claim_next_text_asset_job(worker_id: str) -> AssetGenerationJob | None:
    return _claim(worker_id, text=True)


def claim_next_media_asset_job(worker_id: str) -> AssetGenerationJob | None:
    return _claim(worker_id, text=False)


@transaction.atomic
def claim_next_comfy_asset_job(worker_id: str) -> AssetGenerationJob | None:
    WorkerLease.objects.get_or_create(key="generation-claim")
    WorkerLease.objects.select_for_update().get(key="generation-claim")
    if unresolved_workflow_smoke_attempts().exists():
        return None
    if WorkflowSmokeAttempt.objects.filter(
        status__in=(
            WorkflowSmokeAttempt.Status.QUEUED,
            WorkflowSmokeAttempt.Status.SUBMITTING,
            WorkflowSmokeAttempt.Status.RUNNING,
            WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
        ),
    ).exists():
        return None
    nonterminal_task_token = GenerationTask.objects.exclude(
        submission_token="",
    ).exclude(
        submission_state__in=(
            GenerationTask.SubmissionState.TERMINAL_FAILED,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        ),
    ).filter(
        status__in=(
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        ),
    ).exists()
    if nonterminal_task_token or GenerationTask.objects.filter(
        status=GenerationTask.Status.RUNNING,
    ).exists():
        return None
    now = timezone.now()
    base = AssetGenerationJob.objects.select_for_update(skip_locked=True).select_related(
        "model_deployment__profile_version__provider", "workflow_template_version",
        "owner", "style",
    ).filter(
        claim_token__isnull=True,
        execution_kind=AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
    )
    job = base.filter(
        status__in=(
            AssetGenerationJob.Status.SUBMITTED,
            AssetGenerationJob.Status.RUNNING,
            AssetGenerationJob.Status.CANCEL_REQUESTED,
        ),
    ).filter(
        Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now),
    ).order_by("created_at").first()
    active_remote = AssetGenerationJob.objects.filter(
        execution_kind=AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
    ).filter(
        Q(status__in=(
            AssetGenerationJob.Status.SUBMITTING,
            AssetGenerationJob.Status.SUBMITTED,
            AssetGenerationJob.Status.RUNNING,
            AssetGenerationJob.Status.CANCEL_REQUESTED,
            AssetGenerationJob.Status.UNCERTAIN,
        ))
        | Q(status=AssetGenerationJob.Status.QUEUED, claim_token__isnull=False),
    ).exists()
    if job is None and active_remote:
        return None
    if job is None:
        job = base.filter(status=AssetGenerationJob.Status.QUEUED).order_by(
            "created_at",
        ).first()
    if job is None:
        return None
    job.claim_token = uuid.uuid4()
    job.worker_id = str(worker_id)[:160]
    job.claimed_at = now
    job.heartbeat_at = now
    job.save(update_fields=(
        "claim_token", "worker_id", "claimed_at", "heartbeat_at", "updated_at",
    ))
    return job


def _clear_claim(job: AssetGenerationJob) -> None:
    job.claim_token = None
    job.worker_id = ""
    job.claimed_at = None


def _heartbeat_claim(job: AssetGenerationJob) -> None:
    if job.claim_token is None:
        raise ValidationError("素材生成任务 claim 已失效")
    updated = AssetGenerationJob.objects.filter(
        pk=job.pk,
        claim_token=job.claim_token,
    ).update(heartbeat_at=timezone.now(), updated_at=timezone.now())
    if updated != 1:
        raise ValidationError("素材生成任务 claim 已被回收，停止继续物化")


def _fail(
    job: AssetGenerationJob,
    *,
    code: str,
    message: str,
    uncertain: bool = False,
    remote_status: str | None = None,
    actual_cost=None,
    currency: str = "",
):
    with transaction.atomic():
        locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
        if job.claim_token is not None and locked.claim_token != job.claim_token:
            return locked
        cancelled_before_submission = (
            not uncertain
            and locked.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            and locked.submitted_at is None
            and not locked.provider_request_id
            and not locked.provider_request_id_sha256
        )
        locked.status = (
            AssetGenerationJob.Status.CANCELLED
            if cancelled_before_submission
            else AssetGenerationJob.Status.UNCERTAIN
            if uncertain
            else AssetGenerationJob.Status.FAILED
        )
        locked.error_code = (
            "" if cancelled_before_submission else str(code or "asset_generation_failed")[:100]
        )
        locked.error_message = (
            "" if cancelled_before_submission else str(message or "素材生成失败")[:2000]
        )
        if remote_status is not None:
            locked.remote_status = str(remote_status or "")[:120]
        if actual_cost is not None:
            locked.actual_cost = _decimal_amount(actual_cost, field="actual_cost")
        if currency:
            locked.cost_currency = str(currency).strip().upper()[:12]
        locked.finished_at = timezone.now()
        locked.next_poll_at = None
        _clear_claim(locked)
        locked.heartbeat_at = locked.finished_at
        locked.save(update_fields=(
            "status", "remote_status", "actual_cost", "cost_currency", "error_code",
            "error_message", "finished_at", "next_poll_at",
            "claim_token", "worker_id", "claimed_at", "heartbeat_at", "updated_at",
        ))
        return locked


def _finish_cancelled(
    job: AssetGenerationJob,
    *,
    code: str = "",
    message: str = "",
    remote_status: str | None = None,
) -> AssetGenerationJob:
    with transaction.atomic():
        locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
        if job.claim_token is not None and locked.claim_token != job.claim_token:
            return locked
        locked.status = AssetGenerationJob.Status.CANCELLED
        locked.error_code = str(code or "")[:100]
        locked.error_message = str(message or "")[:2000]
        if remote_status is not None:
            locked.remote_status = str(remote_status or "")[:120]
        locked.finished_at = timezone.now()
        locked.next_poll_at = None
        locked.heartbeat_at = locked.finished_at
        _clear_claim(locked)
        locked.save(update_fields=(
            "status", "remote_status", "error_code", "error_message", "finished_at", "next_poll_at",
            "heartbeat_at", "claim_token", "worker_id", "claimed_at", "updated_at",
        ))
        return locked


def _confirmed_remote_submission(job: AssetGenerationJob) -> bool:
    current = AssetGenerationJob.objects.only(
        "submitted_at", "provider_request_id", "provider_request_id_sha256",
    ).get(pk=job.pk)
    return bool(
        current.submitted_at is not None
        or current.provider_request_id
        or current.provider_request_id_sha256
    )


def _tenant_quota_available(owner_id, incoming_size: int) -> None:
    # Match the upload path's tenant lock so concurrent uploads/generations
    # cannot each observe the same pre-write quota usage.
    get_user_model().objects.select_for_update().get(pk=owner_id)
    limit = int(resolve_configuration(
        required_keys=("artifact.tenant_quota_bytes",),
    ).values["artifact.tenant_quota_bytes"])
    used = 0
    # Inactive assets still occupy managed storage and generated assets retain
    # an audit relationship, so deactivation must never free quota.
    for asset in Asset.objects.filter(owner_id=owner_id).exclude(file="").only("file"):
        try:
            used += int(asset.file.size)
        except (OSError, ValueError, FileNotFoundError):
            continue
    if used + int(incoming_size) > limit:
        raise ValidationError("租户素材配额不足，生成结果不能入库")


def _asset_kind(job: AssetGenerationJob) -> str:
    if job.media_type == AssetGenerationJob.MediaType.IMAGE:
        return job.image_category or Asset.Kind.REFERENCE
    if job.media_type == AssetGenerationJob.MediaType.VIDEO:
        return Asset.Kind.REFERENCE_VIDEO
    if job.media_type == AssetGenerationJob.MediaType.AUDIO:
        return Asset.Kind.REFERENCE_AUDIO
    return Asset.Kind.SCRIPT


def _extension(mime_type: str, media_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/x-matroska": ".mkv",
        "audio/wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "audio/mp4": ".m4a",
        "text/markdown": ".md",
    }.get(mime_type, f".{media_type}")


def _asset_metadata(job: AssetGenerationJob, facts: dict) -> dict:
    observed = facts.get("observed_media") if isinstance(facts.get("observed_media"), dict) else {}
    metadata = {
        "manifest_schema_version": 1,
        "source": "model_generation",
        "generation_job_id": str(job.id),
        "model_deployment_id": str(job.model_deployment_id),
        "execution_kind": job.execution_kind,
        "operation": job.operation,
        "request_snapshot_sha256": job.request_snapshot_sha256,
        "size_bytes": facts["size_bytes"],
        "generated_sha256": facts["sha256"],
        **observed,
    }
    if job.workflow_template_version_id:
        metadata["workflow_template_version_id"] = str(job.workflow_template_version_id)
        metadata["compiled_graph_sha256"] = str(
            (job.request_snapshot or {}).get("compiled_graph_sha256") or "",
        )
    if job.media_type == AssetGenerationJob.MediaType.VIDEO:
        duration_ms = int(metadata.get("duration_ms") or 0)
        if duration_ms <= 0:
            raise ValidationError("生成视频缺少可验证的正数时长")
        fps = metadata.get("fps")
        frame_rate = str(metadata.get("frame_rate") or "")
        if not fps or not frame_rate:
            raise ValidationError("生成视频缺少可验证 FPS，不能作为短剧参考素材")
        metadata.update({
            "source_fps": fps,
            "source_fps_fraction": frame_rate,
            "source_avg_frame_rate": frame_rate,
            "source_r_frame_rate": frame_rate,
            "verified_media_type": "video",
            "verified_by": "ffprobe",
        })
    elif job.media_type == AssetGenerationJob.MediaType.AUDIO:
        duration_ms = int(metadata.get("duration_ms") or 0)
        if duration_ms <= 0:
            raise ValidationError("生成音频缺少可验证的正数时长")
        metadata.update({
            "verified_media_type": "audio",
            "verified_by": "ffprobe",
            "has_audio": True,
        })
    return metadata


def _asset_source_info(job: AssetGenerationJob) -> str:
    route = (
        "ComfyUI 工作流"
        if job.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
        else "模型部署"
    )
    return f"{route}生成 · 素材生成任务 {job.id}"


def _finish_success(
    job: AssetGenerationJob,
    *,
    asset: Asset | None,
    result_text: str = "",
    actual_cost=None,
    currency: str = "",
):
    with transaction.atomic():
        locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
        if job.claim_token is not None and locked.claim_token != job.claim_token:
            if asset is not None:
                stored_file = asset.file
                asset.delete()
                if stored_file:
                    stored_file.delete(save=False)
            return locked
        if (
            locked.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            or locked.ignore_late_result
        ):
            locked.status = AssetGenerationJob.Status.CANCELLED
            locked.error_code = "late_result_ignored"
            locked.error_message = "取消后的模型结果已收到，但没有进入素材库。"
            if asset is not None:
                stored_file = asset.file
                asset.delete()
                if stored_file:
                    stored_file.delete(save=False)
            asset = None
            result_text = ""
        else:
            locked.status = AssetGenerationJob.Status.SUCCEEDED
            locked.error_code = ""
            locked.error_message = ""
        locked.output_asset = asset
        locked.result_text = result_text
        encoded = result_text.encode("utf-8")
        locked.result_text_size_bytes = len(encoded)
        locked.result_text_sha256 = hashlib.sha256(encoded).hexdigest() if encoded else ""
        locked.actual_cost = _decimal_amount(actual_cost, field="actual_cost")
        if currency:
            locked.cost_currency = str(currency).strip().upper()[:12]
        if (
            locked.status == AssetGenerationJob.Status.SUCCEEDED
            and locked.budget_limit is not None
            and locked.actual_cost is not None
            and locked.actual_cost > locked.budget_limit
        ):
            locked.error_code = "actual_budget_exceeded"
            locked.error_message = "实际费用超过预算；真实素材已保留，请人工核对费用。"
        locked.finished_at = timezone.now()
        locked.next_poll_at = None
        _clear_claim(locked)
        locked.heartbeat_at = locked.finished_at
        locked.save(update_fields=(
            "status", "output_asset", "result_text", "result_text_size_bytes",
            "result_text_sha256", "actual_cost", "cost_currency", "error_code",
            "error_message", "finished_at", "next_poll_at", "claim_token",
            "worker_id", "claimed_at", "heartbeat_at", "updated_at",
        ))
        return locked


def process_text_asset_job(job: AssetGenerationJob) -> AssetGenerationJob:
    if job.claim_token is None:
        raise ValidationError("素材文本任务没有 worker claim")
    try:
        snapshot = _snapshot(job)
        current = AssetGenerationJob.objects.only(
            "status", "provider_request_id",
        ).get(pk=job.pk)
        if (
            current.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            and not current.provider_request_id
        ):
            return _finish_cancelled(job)
        deployment = _verify_live_target(job, snapshot)
        with transaction.atomic():
            locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
            if locked.claim_token != job.claim_token:
                return locked
            if locked.status == AssetGenerationJob.Status.CANCEL_REQUESTED:
                locked.status = AssetGenerationJob.Status.CANCELLED
                locked.finished_at = timezone.now()
                _clear_claim(locked)
                locked.save(update_fields=(
                    "status", "finished_at", "claim_token", "worker_id", "claimed_at", "updated_at",
                ))
                return locked
            locked.status = AssetGenerationJob.Status.SUBMITTING
            locked.submission_started_at = timezone.now()
            locked.started_at = locked.started_at or locked.submission_started_at
            locked.heartbeat_at = locked.submission_started_at
            locked.save(update_fields=(
                "status", "submission_started_at", "started_at", "heartbeat_at", "updated_at",
            ))
        try:
            response = driver_for(deployment.provider).invoke(
                deployment, "chat_completion", snapshot["payload"],
            )
        except ProviderDriverError as exc:
            return _fail(
                job,
                code="uncertain_remote_response" if exc.submission_uncertain else exc.code,
                message=(
                    "文本模型提交响应不确定；禁止自动重试，需人工核验远端。"
                    if exc.submission_uncertain else str(exc)
                ),
                uncertain=bool(exc.submission_uncertain),
            )
        text = str(response.get("text") or "") if isinstance(response, dict) else ""
        provider_request_id = (
            str(response.get("request_id") or "") if isinstance(response, dict) else ""
        )
        provider_request_id_sha256 = (
            hashlib.sha256(provider_request_id.encode("utf-8")).hexdigest()
            if provider_request_id else ""
        )
        AssetGenerationJob.objects.filter(pk=job.pk, claim_token=job.claim_token).update(
            provider_request_id=(
                provider_request_id if len(provider_request_id) <= 191 else ""
            ),
            provider_request_id_sha256=provider_request_id_sha256,
            submitted_at=timezone.now(),
            heartbeat_at=timezone.now(),
            updated_at=timezone.now(),
        )
        if len(provider_request_id) > 191:
            return _fail(
                job,
                code="provider_request_id_too_long",
                message=(
                    "文本模型返回的 request id 超过受控合同长度；已保存摘要，"
                    "禁止截断身份事实或自动重试。"
                ),
                uncertain=True,
            )
        max_bytes = int(resolve_configuration(
            required_keys=("script.max_upload_bytes",),
        ).values["script.max_upload_bytes"])
        encoded = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        if not text.strip() or len(encoded) > max_bytes or b"\x00" in encoded:
            return _fail(
                job,
                code="text_result_invalid",
                message="文本模型已返回但结果未通过安全合同；禁止直接重复付费请求，需人工核验。",
                uncertain=True,
            )
        if AssetGenerationJob.objects.filter(
            pk=job.pk,
            status=AssetGenerationJob.Status.CANCEL_REQUESTED,
        ).exists():
            return _finish_success(
                job,
                asset=None,
                actual_cost=job.estimated_cost,
                currency=job.cost_currency,
            )
        with transaction.atomic():
            asset = None
            if job.job_type == AssetGenerationJob.JobType.ASSET:
                _tenant_quota_available(job.owner_id, len(encoded))
                asset = Asset(
                    owner=job.owner,
                    style=job.style,
                    kind=Asset.Kind.SCRIPT,
                    name=job.name,
                    description=job.description,
                    prompt_hint=job.prompt,
                    source_info=_asset_source_info(job),
                    mime_type="text/markdown",
                    checksum=hashlib.sha256(encoded).hexdigest(),
                    metadata=_asset_metadata(job, {
                        "mime_type": "text/markdown",
                        "size_bytes": len(encoded),
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                    }),
                )
                try:
                    asset.file.save(f"{job.name or 'script'}.md", ContentFile(encoded), save=False)
                    asset.save()
                except Exception:
                    if asset.file:
                        asset.file.delete(save=False)
                    raise
            return _finish_success(
                job,
                asset=asset,
                result_text=text if job.job_type == AssetGenerationJob.JobType.PROMPT else "",
                actual_cost=job.estimated_cost,
                currency=job.cost_currency,
            )
    except (ValidationError, ProviderDriverError) as exc:
        crossed = _confirmed_remote_submission(job)
        return _fail(
            job,
            code=getattr(exc, "code", "asset_text_job_invalid"),
            message=(
                f"文本模型已受理后本地处理失败，禁止直接重试：{exc}"
                if crossed else str(exc)
            ),
            uncertain=crossed,
        )


def _schedule(
    job: AssetGenerationJob,
    *,
    status: str,
    remote_status: str,
    error_code: str = "",
    error_message: str = "",
):
    with transaction.atomic():
        locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked.claim_token != job.claim_token:
            return locked
        config = _combined_config(locked.model_deployment)
        interval = (
            float(restricted_async_rest_runtime_budget(config)["poll_interval_seconds"])
            if locked.execution_kind == AssetGenerationJob.ExecutionKind.DEPLOYMENT
            else float(restricted_comfyui_runtime_budget(config)["poll_interval_seconds"])
        )
        locked.status = (
            AssetGenerationJob.Status.CANCEL_REQUESTED
            if locked.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            else status
        )
        locked.remote_status = str(remote_status or "")[:120]
        locked.error_code = str(error_code or "")[:100]
        locked.error_message = str(error_message or "")[:2000]
        locked.last_polled_at = timezone.now()
        locked.poll_count += 1
        locked.next_poll_at = locked.last_polled_at + timedelta(seconds=interval)
        locked.heartbeat_at = locked.last_polled_at
        _clear_claim(locked)
        locked.save(update_fields=(
            "status", "remote_status", "error_code", "error_message",
            "last_polled_at", "poll_count", "next_poll_at",
            "heartbeat_at", "claim_token", "worker_id", "claimed_at", "updated_at",
        ))
        return locked


def _request_remote_deadline_cancel(job: AssetGenerationJob) -> AssetGenerationJob:
    with transaction.atomic():
        locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked.claim_token != job.claim_token:
            return locked
        now = timezone.now()
        locked.status = AssetGenerationJob.Status.CANCEL_REQUESTED
        locked.cancel_requested_at = locked.cancel_requested_at or now
        locked.ignore_late_result = True
        locked.next_poll_at = now
        locked.error_code = "remote_deadline_exceeded"
        locked.error_message = "远端素材生成超过冻结的最长轮询时间，系统正在请求取消。"
        locked.heartbeat_at = now
        _clear_claim(locked)
        locked.save(update_fields=(
            "status", "cancel_requested_at", "ignore_late_result", "next_poll_at", "error_code",
            "error_message", "heartbeat_at", "claim_token", "worker_id",
            "claimed_at", "updated_at",
        ))
        return locked


def _materialize_downloaded_asset(job: AssetGenerationJob, path: Path, facts: dict) -> Asset:
    max_bytes = int(resolve_configuration(
        required_keys=("artifact.max_bytes",),
    ).values["artifact.max_bytes"])
    staged_path: Path | None = None
    try:
        # Copy one opened descriptor into a private bounded staging file, then
        # probe exactly those bytes. Otherwise a local Comfy output can be
        # replaced between validation and managed-storage save (TOCTOU).
        with path.open("rb") as source, tempfile.NamedTemporaryFile(
            prefix="autoprompt-asset-",
            suffix=path.suffix,
            delete=False,
        ) as staged:
            staged_path = Path(staged.name)
            copied = 0
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise ValidationError("生成产物超过受控大小上限")
                staged.write(chunk)
        staged_facts = verified_local_media_facts(
            staged_path,
            expected_kind=job.media_type,
            max_bytes=max_bytes,
            include_observed_media=job.media_type in {"video", "audio"},
        )
        if any(
            staged_facts.get(field) != facts.get(field)
            for field in ("sha256", "size_bytes", "mime_type")
        ):
            raise ValidationError("生成产物在校验与物化之间发生变化")
        _tenant_quota_available(job.owner_id, staged_facts["size_bytes"])
        metadata = _asset_metadata(job, staged_facts)
        asset = Asset(
            owner=job.owner,
            style=job.style,
            kind=_asset_kind(job),
            name=job.name,
            description=job.description,
            prompt_hint=job.prompt,
            source_info=_asset_source_info(job),
            mime_type=staged_facts["mime_type"],
            checksum=staged_facts["sha256"],
            metadata=metadata,
        )
        extension = _extension(staged_facts["mime_type"], job.media_type)
        with staged_path.open("rb") as handle:
            asset.file.save(
                f"{job.name or job.media_type}{extension}",
                File(handle),
                save=False,
            )
            asset.save()
    except Exception:
        if "asset" in locals() and asset.file:
            asset.file.delete(save=False)
        raise
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
    return asset


def process_media_asset_job(job: AssetGenerationJob) -> AssetGenerationJob:
    if job.claim_token is None:
        raise ValidationError("素材媒体任务没有 worker claim")
    try:
        snapshot = _snapshot(job)
        current = AssetGenerationJob.objects.only(
            "status", "provider_request_id",
        ).get(pk=job.pk)
        if (
            current.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            and not current.provider_request_id
        ):
            return _finish_cancelled(job)
        deployment = _verify_live_target(job, snapshot)
        contract = async_operation_contract(deployment.profile_version, job.operation)
        validate_standard_payload(contract, snapshot["payload"])
        driver = driver_for(deployment.provider)
        if job.status == AssetGenerationJob.Status.QUEUED:
            with transaction.atomic():
                locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
                if locked.claim_token != job.claim_token:
                    return locked
                if locked.status == AssetGenerationJob.Status.CANCEL_REQUESTED:
                    locked.status = AssetGenerationJob.Status.CANCELLED
                    locked.finished_at = timezone.now()
                    _clear_claim(locked)
                    locked.save(update_fields=(
                        "status", "finished_at", "claim_token", "worker_id", "claimed_at", "updated_at",
                    ))
                    return locked
                locked.status = AssetGenerationJob.Status.SUBMITTING
                locked.submission_started_at = timezone.now()
                locked.started_at = locked.started_at or locked.submission_started_at
                locked.heartbeat_at = locked.submission_started_at
                locked.save(update_fields=(
                    "status", "submission_started_at", "started_at", "heartbeat_at", "updated_at",
                ))
            try:
                response = driver.invoke(deployment, "submit", snapshot["payload"])
            except ProviderDriverError as exc:
                return _fail(
                    job,
                    code="uncertain_remote_response" if exc.submission_uncertain else exc.code,
                    message=(
                        "媒体模型提交响应不确定；禁止自动重复 POST，需人工核验远端。"
                        if exc.submission_uncertain else str(exc)
                    ),
                    uncertain=bool(exc.submission_uncertain),
                )
            request_id = str(response.get("request_id") or "") if isinstance(response, dict) else ""
            if not request_id:
                return _fail(
                    job,
                    code="uncertain_remote_response",
                    message="远端提交缺少 request_id；禁止自动重复 POST。",
                    uncertain=True,
                )
            request_id_sha256 = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
            if len(request_id) > 191:
                AssetGenerationJob.objects.filter(
                    pk=job.pk,
                    claim_token=job.claim_token,
                ).update(
                    provider_request_id="",
                    provider_request_id_sha256=request_id_sha256,
                    remote_status=str(response.get("status") or "submitted")[:120],
                    submitted_at=timezone.now(),
                    heartbeat_at=timezone.now(),
                    updated_at=timezone.now(),
                )
                return _fail(
                    job,
                    code="provider_request_id_too_long",
                    message=(
                        "媒体模型返回的 request id 超过受控合同长度；已保存摘要，"
                        "禁止使用截断 id 轮询或取消。"
                    ),
                    uncertain=True,
                )
            with transaction.atomic():
                locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
                if locked.claim_token != job.claim_token:
                    return locked
                locked.provider_request_id = request_id
                locked.provider_request_id_sha256 = request_id_sha256
                locked.remote_status = str(response.get("status") or "submitted")[:120]
                locked.submitted_at = timezone.now()
                locked.status = (
                    AssetGenerationJob.Status.CANCEL_REQUESTED
                    if locked.status == AssetGenerationJob.Status.CANCEL_REQUESTED
                    else AssetGenerationJob.Status.SUBMITTED
                )
                locked.next_poll_at = locked.submitted_at
                locked.heartbeat_at = locked.submitted_at
                _clear_claim(locked)
                locked.save(update_fields=(
                    "provider_request_id", "provider_request_id_sha256", "remote_status",
                    "submitted_at", "status",
                    "next_poll_at", "heartbeat_at", "claim_token", "worker_id",
                    "claimed_at", "updated_at",
                ))
                return locked

        terminal_success = set(_combined_config(deployment).get("terminal_success") or [])
        if (
            job.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            and job.remote_status not in terminal_success
        ):
            max_poll_seconds = _frozen_max_poll_seconds(snapshot)
            if (
                job.cancel_requested_at is not None
                and timezone.now() >= job.cancel_requested_at + timedelta(seconds=max_poll_seconds)
            ):
                return _fail(
                    job,
                    code="remote_cancel_unconfirmed",
                    message="远端在冻结的取消等待上限内未确认终态；禁止重试，需人工核验。",
                    uncertain=True,
                )
            if not job.provider_request_id:
                return _finish_cancelled(job)
            protocol = "cancel" if job.cancel_submitted_at is None else "poll"
            response = driver.invoke(deployment, protocol, {"request_id": job.provider_request_id})
            if protocol == "cancel":
                AssetGenerationJob.objects.filter(pk=job.pk).update(
                    cancel_submitted_at=timezone.now(), updated_at=timezone.now(),
                )
            response_status = str(response.get("status") or job.remote_status)
            if response.get("terminal_cancelled"):
                return _finish_cancelled(
                    job,
                    code="remote_cancelled",
                    message="远端已确认取消。",
                    remote_status=response_status,
                )
            if response.get("terminal_failure"):
                return _fail(
                    job,
                    code="remote_generation_failed_during_cancel",
                    message="取消请求处理期间，远端明确报告生成失败。",
                    remote_status=response_status,
                )
            if response.get("terminal_success"):
                return _schedule(
                    job,
                    remote_status=response_status,
                    status=AssetGenerationJob.Status.CANCEL_REQUESTED,
                    error_code="late_result_pending_cost_reconciliation",
                    error_message="取消后远端已成功；正在读取费用事实，产物不会进入素材库。",
                )
            return _schedule(
                job,
                status=AssetGenerationJob.Status.CANCEL_REQUESTED,
                remote_status=str(response.get("status") or job.remote_status),
            )

        if job.remote_status not in terminal_success:
            max_poll_seconds = _frozen_max_poll_seconds(snapshot)
            if (
                job.submitted_at is not None
                and timezone.now() >= job.submitted_at + timedelta(seconds=max_poll_seconds)
            ):
                return _request_remote_deadline_cancel(job)
            response = driver.invoke(deployment, "poll", {"request_id": job.provider_request_id})
            if response.get("terminal_cancelled"):
                return _finish_cancelled(
                    job,
                    code="remote_cancelled",
                    message="远端任务已取消",
                    remote_status=str(response.get("status") or "cancelled"),
                )
            if response.get("terminal_failure"):
                return _fail(
                    job,
                    code="remote_generation_failed",
                    message="远端任务明确失败",
                    remote_status=str(response.get("status") or "failed"),
                )
            return _schedule(
                job,
                status=AssetGenerationJob.Status.RUNNING,
                remote_status=str(response.get("status") or ""),
            )

        _heartbeat_claim(job)
        collected = driver.invoke(deployment, "collect", {"request_id": job.provider_request_id})
        _heartbeat_claim(job)
        artifacts = collected.get("artifacts") if isinstance(collected, dict) else None
        expected_kind = str(snapshot["output_kind"])
        attested_cost, _attested_currency = _latest_attested_resolution_cost(job)
        collected_actual = collected.get("actual_cost")
        if collected_actual is None:
            actual = attested_cost if attested_cost is not None else job.estimated_cost
        else:
            actual = _decimal_amount(collected_actual, field="actual_cost")
            if attested_cost is not None and actual != attested_cost:
                raise ProviderDriverError(
                    "远端 collect 费用与管理员消歧证据冲突，需重新人工对账",
                    code="actual_cost_reconciliation_required",
                )
        actual = _decimal_amount(actual, field="actual_cost")
        actual_currency = str(collected.get("currency") or job.cost_currency).strip().upper()[:12]
        if actual and not actual_currency:
            raise ValidationError("素材生成实际费用缺少币种")
        if job.cost_currency and actual_currency and job.cost_currency != actual_currency:
            raise ProviderDriverError(
                "素材生成实际费用币种与冻结的预估币种冲突，需人工对账",
                code="actual_cost_currency_conflict",
            )
        if job.ignore_late_result:
            return _finish_success(
                job,
                asset=None,
                actual_cost=actual,
                currency=actual_currency,
            )
        if (
            not isinstance(artifacts, list)
            or len(artifacts) != 1
            or artifacts[0].get("kind") != expected_kind
        ):
            raise ProviderDriverError(
                "素材生成必须且只能返回一个类型匹配的产物",
                code="artifact_manifest_invalid",
            )
        root = Path(settings.MEDIA_ROOT).resolve()
        target = (root / ".asset-generation" / str(job.owner_id) / str(job.id) / "artifact.media").resolve()
        if not target.is_relative_to(root):
            raise ProviderDriverError("素材生成暂存目录越界", code="artifact_archive_failed")
        max_bytes = int(resolve_configuration(
            required_keys=("artifact.max_bytes",),
        ).values["artifact.max_bytes"])
        try:
            download_remote_artifact(
                url=str(artifacts[0].get("url") or ""),
                provider_config=_combined_config(deployment),
                target=target,
                expected_kind=expected_kind,
                expected_sha256=str(artifacts[0].get("sha256") or ""),
                max_bytes=max_bytes,
                heartbeat=lambda: _heartbeat_claim(job),
            )
            facts = verified_local_media_facts(
                target,
                expected_kind=expected_kind,
                max_bytes=max_bytes,
                include_observed_media=expected_kind in {"video", "audio"},
            )
            with transaction.atomic():
                if AssetGenerationJob.objects.filter(
                    pk=job.pk,
                    status=AssetGenerationJob.Status.CANCEL_REQUESTED,
                ).exists():
                    asset = None
                else:
                    asset = _materialize_downloaded_asset(job, target, facts)
                result = _finish_success(
                    job,
                    asset=asset,
                    actual_cost=actual,
                    currency=actual_currency,
                )
        finally:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()
                target.parent.parent.rmdir()
            except OSError:
                pass
        return result
    except ProviderDriverError as exc:
        if exc.retryable and not exc.submission_uncertain and job.provider_request_id:
            current = AssetGenerationJob.objects.get(pk=job.pk)
            if current.claim_token == job.claim_token and current.status in {
                AssetGenerationJob.Status.SUBMITTED,
                AssetGenerationJob.Status.RUNNING,
                AssetGenerationJob.Status.CANCEL_REQUESTED,
            }:
                return _schedule(
                    job,
                    status=current.status,
                    remote_status=current.remote_status,
                    error_code=exc.code,
                    error_message="远端暂时不可用，任务将按冻结请求继续轮询，不会重复提交。",
                )
        crossed = _confirmed_remote_submission(job)
        return _fail(
            job,
            code=(
                "uncertain_remote_response"
                if exc.submission_uncertain else exc.code
            ),
            message=(
                f"远端请求已受理后处理失败，禁止直接重试：{exc}"
                if crossed else str(exc)
            ),
            uncertain=bool(exc.submission_uncertain or crossed),
        )
    except ValidationError as exc:
        crossed = _confirmed_remote_submission(job)
        return _fail(
            job,
            code="asset_media_job_invalid",
            message=(
                f"远端请求已受理后本地校验失败，禁止直接重试：{exc}"
                if crossed else str(exc)
            ),
            uncertain=crossed,
        )


def _comfy_output_path(item: dict, *, expected_prefix: str) -> Path:
    output_root_raw = str(getattr(settings, "COMFYUI_OUTPUT_DIR", "") or "").strip()
    if not output_root_raw:
        raise ProviderDriverError(
            "ComfyUI 素材生成需要可访问的受控输出目录",
            code="comfyui_output_unavailable",
        )
    filename_raw = str(item.get("filename") or "").replace("\\", "/")
    subfolder_raw = str(item.get("subfolder") or "").replace("\\", "/")
    filename = PurePosixPath(filename_raw).name
    subfolder = PurePosixPath(subfolder_raw)
    if (
        not filename
        or filename != filename_raw
        or subfolder.is_absolute()
        or ".." in subfolder.parts
        or str(item.get("type") or "") != "output"
    ):
        raise ProviderDriverError(
            "ComfyUI 主输出路径或存储类型不受信",
            code="comfyui_output_path_invalid",
        )
    expected_prefix = str(expected_prefix or "").replace("\\", "/").strip("/")
    expected_path = PurePosixPath(expected_prefix)
    expected_parent = PurePosixPath(*expected_path.parts[:-1])
    expected_name = expected_path.name
    invalid_windows_name = any(
        ord(character) < 32
        or ord(character) == 127
        or character in '<>:"|?*'
        for character in filename
    )
    if (
        not expected_prefix
        or expected_path.is_absolute()
        or ".." in expected_path.parts
        or subfolder != expected_parent
        or not expected_name
        or not filename.startswith(f"{expected_name}_")
        or invalid_windows_name
    ):
        raise ProviderDriverError(
            "ComfyUI 主输出未绑定本任务的冻结输出前缀",
            code="comfyui_output_path_invalid",
        )
    root = Path(output_root_raw).resolve()
    candidate = (root / Path(*subfolder.parts) / filename).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ProviderDriverError(
            "ComfyUI 主输出文件不存在或越过受控目录",
            code="comfyui_output_path_invalid",
        )
    return candidate


def _frozen_output_prefix(snapshot: dict) -> str:
    """Read the explicit task-owned output prefix from the frozen snapshot."""
    explicit = str(snapshot.get("output_prefix") or "").strip()
    if not explicit:
        raise ProviderDriverError(
            "ComfyUI 素材任务缺少冻结的输出前缀",
            code="comfyui_output_prefix_missing",
        )
    return explicit


class _AssetWorkflowStagingExecution:
    """Small adapter exposing the shared GenerationTask staging contract."""

    def __init__(self, job: AssetGenerationJob, snapshot: dict, graph: dict):
        self._job = job
        self.owner = job.owner
        self.owner_id = job.owner_id
        self.id = job.id
        self.staged_inputs = deepcopy(job.staged_inputs or [])
        self.capability_snapshot = snapshot
        self.generated_workflow = type("FrozenAssetWorkflow", (), {
            "workflow_json": graph,
            "workflow_template_version_id": job.workflow_template_version_id,
            "compiler_id": str(snapshot.get("workflow_compiler_id") or ""),
            "content_hash": str(snapshot.get("compiled_graph_sha256") or ""),
            "workflow_contract_sha256": str(
                snapshot.get("workflow_contract_sha256") or ""
            ),
            "injected_values": {
                "asset_bindings": deepcopy(snapshot.get("asset_bindings") or []),
            },
            "asset_bindings_sha256": str(
                snapshot.get("asset_bindings_sha256") or ""
            ),
        })()

    def save(self, *, update_fields=()):
        if "staged_inputs" not in update_fields:
            raise ValidationError("素材任务 staging adapter 只允许持久化 staged_inputs")
        self._job.staged_inputs = deepcopy(self.staged_inputs)
        self._job.save(update_fields=("staged_inputs", "updated_at"))


class _AssetWorkflowUploadClient:
    def __init__(self, driver, deployment: ModelDeployment):
        self.driver = driver
        self.deployment = deployment

    def upload_input_file(self, source_path, *, subfolder: str, filename: str):
        return self.driver.invoke(self.deployment, "upload", {
            "source_path": str(source_path),
            "subfolder": subfolder,
            "filename": filename,
        })


def _prepared_comfy_asset_payload(
    job: AssetGenerationJob,
    snapshot: dict,
    deployment: ModelDeployment,
    driver,
    payload: dict,
) -> dict:
    if snapshot.get("schema_version") == 2:
        # Historical token-free jobs created before workflow input schema v3.
        return deepcopy(payload)
    asset_bindings = snapshot.get("asset_bindings")
    reference_bindings = snapshot.get("reference_bindings")
    asset_facts = snapshot.get("asset_facts")
    if not all(isinstance(value, list) for value in (
        asset_bindings, reference_bindings, asset_facts,
    )):
        raise ValidationError("ComfyUI 素材任务缺少冻结的工作流输入 manifest")
    for value, sha_key in (
        (asset_bindings, "asset_bindings_sha256"),
        (reference_bindings, "reference_bindings_sha256"),
        (asset_facts, "asset_facts_sha256"),
    ):
        if json_contract(
            value,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256 != str(snapshot.get(sha_key) or ""):
            raise ValidationError(f"ComfyUI 素材任务冻结的 {sha_key} 已漂移")
    runtime = deepcopy(payload)
    execution = _AssetWorkflowStagingExecution(
        job,
        snapshot,
        runtime["workflow"],
    )
    # Reuse the exact short-drama staging path: authorization, checksum,
    # frozen preprocessing, durable upload cache and token replacement.
    runtime["workflow"] = prepare_workflow(
        execution,
        _AssetWorkflowUploadClient(driver, deployment),
    )
    job.staged_inputs = deepcopy(execution.staged_inputs)
    return runtime


def process_comfy_asset_job(job: AssetGenerationJob) -> AssetGenerationJob:
    """Execute one frozen ComfyUI workflow route without any submit fallback."""

    if job.claim_token is None:
        raise ValidationError("ComfyUI 素材任务没有 worker claim")
    try:
        snapshot = _snapshot(job)
        if job.execution_kind != AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
            raise ValidationError("任务不是 ComfyUI 工作流执行方案")
        current = AssetGenerationJob.objects.only("status", "provider_request_id").get(pk=job.pk)
        if (
            current.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            and not current.provider_request_id
        ):
            return _finish_cancelled(job)
        deployment = _verify_live_target(
            job,
            snapshot,
            require_executable_route=current.status == AssetGenerationJob.Status.QUEUED,
        )
        payload = snapshot.get("payload")
        graph = payload.get("workflow") if isinstance(payload, dict) else None
        if (
            not isinstance(graph, dict)
            or json_contract(
                graph,
                max_bytes=_workflow_graph_max_bytes(),
            ).sha256 != snapshot.get("compiled_graph_sha256")
            or str(payload.get("submission_token") or "") != str(job.id)
        ):
            raise ValidationError("ComfyUI 冻结提交图或 submission token 无效")
        driver = driver_for(deployment.provider)
        runtime_payload = deepcopy(payload)

        if job.status == AssetGenerationJob.Status.QUEUED:
            runtime_payload = _prepared_comfy_asset_payload(
                job,
                snapshot,
                deployment,
                driver,
                payload,
            )
            with transaction.atomic():
                locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
                if locked.claim_token != job.claim_token:
                    return locked
                if locked.status == AssetGenerationJob.Status.CANCEL_REQUESTED:
                    return _finish_cancelled(job)
                locked.status = AssetGenerationJob.Status.SUBMITTING
                locked.submission_started_at = timezone.now()
                locked.started_at = locked.started_at or locked.submission_started_at
                locked.heartbeat_at = locked.submission_started_at
                locked.save(update_fields=(
                    "status", "submission_started_at", "started_at", "heartbeat_at", "updated_at",
                ))
            try:
                response = driver.invoke(deployment, "submit", runtime_payload)
            except ProviderDriverError as exc:
                if (
                    exc.code.startswith("comfyui_resource_release_")
                    and not exc.submission_uncertain
                ):
                    return _fail(
                        job,
                        code=exc.code,
                        message=str(exc),
                        uncertain=False,
                    )
                return _fail(
                    job,
                    code="uncertain_remote_response",
                    message=(
                        "ComfyUI 提交响应不确定；冻结 submission token，禁止自动重复 POST："
                        f"{exc}"
                    ),
                    uncertain=True,
                )
            request_id = str(response.get("request_id") or "") if isinstance(response, dict) else ""
            if request_id != str(job.id):
                request_hash = (
                    hashlib.sha256(request_id.encode("utf-8")).hexdigest()
                    if request_id else ""
                )
                AssetGenerationJob.objects.filter(
                    pk=job.pk,
                    claim_token=job.claim_token,
                ).update(
                    provider_request_id_sha256=request_hash,
                    submitted_at=timezone.now(),
                    updated_at=timezone.now(),
                )
                return _fail(
                    job,
                    code="comfyui_submission_token_mismatch",
                    message="ComfyUI 返回的 request id 与冻结 submission token 不一致。",
                    uncertain=True,
                )
            with transaction.atomic():
                locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
                if locked.claim_token != job.claim_token:
                    return locked
                locked.provider_request_id = request_id
                locked.provider_request_id_sha256 = hashlib.sha256(
                    request_id.encode("utf-8"),
                ).hexdigest()
                locked.remote_status = "submitted"
                locked.submitted_at = timezone.now()
                locked.status = (
                    AssetGenerationJob.Status.CANCEL_REQUESTED
                    if locked.status == AssetGenerationJob.Status.CANCEL_REQUESTED
                    else AssetGenerationJob.Status.SUBMITTED
                )
                locked.next_poll_at = locked.submitted_at
                locked.heartbeat_at = locked.submitted_at
                _clear_claim(locked)
                locked.save(update_fields=(
                    "provider_request_id", "provider_request_id_sha256", "remote_status",
                    "submitted_at", "status", "next_poll_at", "heartbeat_at",
                    "claim_token", "worker_id", "claimed_at", "updated_at",
                ))
                return locked

        max_poll_seconds = _frozen_max_poll_seconds(snapshot)
        if job.status == AssetGenerationJob.Status.CANCEL_REQUESTED:
            if (
                job.cancel_requested_at is not None
                and timezone.now() >= job.cancel_requested_at + timedelta(seconds=max_poll_seconds)
            ):
                return _fail(
                    job,
                    code="remote_cancel_unconfirmed",
                    message="ComfyUI 未在冻结上限内确认取消；需人工核验。",
                    uncertain=True,
                )
            if not job.provider_request_id:
                return _finish_cancelled(job)
            protocol = "cancel" if job.cancel_submitted_at is None else "poll"
            response = driver.invoke(
                deployment,
                protocol,
                {"request_id": job.provider_request_id},
            )
            if protocol == "cancel":
                AssetGenerationJob.objects.filter(pk=job.pk).update(
                    cancel_submitted_at=timezone.now(),
                    updated_at=timezone.now(),
                )
                return _schedule(
                    job,
                    status=AssetGenerationJob.Status.CANCEL_REQUESTED,
                    remote_status=str(response.get("status") or "cancel_requested"),
                )
            remote_status = str(response.get("status") or job.remote_status)
            if remote_status in {"cancelled", "missing"}:
                return _finish_cancelled(
                    job,
                    code="remote_cancelled",
                    message="ComfyUI 已确认任务不再运行。",
                    remote_status=remote_status,
                )
            if remote_status == "failed":
                return _fail(
                    job,
                    code="remote_generation_failed_during_cancel",
                    message="取消期间 ComfyUI 明确报告生成失败。",
                    remote_status=remote_status,
                )
            if remote_status == "succeeded":
                return _finish_success(
                    job,
                    asset=None,
                    actual_cost=job.estimated_cost,
                    currency=job.cost_currency,
                )
            return _schedule(
                job,
                status=AssetGenerationJob.Status.CANCEL_REQUESTED,
                remote_status=remote_status,
            )

        if job.remote_status != "succeeded":
            if (
                job.submitted_at is not None
                and timezone.now() >= job.submitted_at + timedelta(seconds=max_poll_seconds)
            ):
                return _request_remote_deadline_cancel(job)
            response = driver.invoke(
                deployment,
                "poll",
                {"request_id": job.provider_request_id},
            )
            remote_status = str(response.get("status") or "")
            if remote_status == "failed":
                return _fail(
                    job,
                    code="remote_generation_failed",
                    message="ComfyUI 工作流明确失败。",
                    remote_status=remote_status,
                )
            if remote_status == "missing":
                return _fail(
                    job,
                    code="comfyui_request_missing",
                    message="已提交的 ComfyUI request 不在队列或历史中；需人工核验。",
                    uncertain=True,
                    remote_status=remote_status,
                )
            return _schedule(
                job,
                status=AssetGenerationJob.Status.RUNNING,
                remote_status=remote_status or "running",
            )

        _heartbeat_claim(job)
        collected = driver.invoke(
            deployment,
            "collect",
            {"request_id": job.provider_request_id},
        )
        _heartbeat_claim(job)
        outputs = collected.get("outputs") if isinstance(collected, dict) else None
        primary = snapshot.get("compiled_output_contract", {}).get("primary")
        expected_kind = str(snapshot.get("output_kind") or "")
        expected_node_id = str(primary.get("node_id") or "") if isinstance(primary, dict) else ""
        matching = [
            item for item in (outputs or [])
            if isinstance(item, dict)
            and str(item.get("node_id") or "") == expected_node_id
            and classify_task_output(item) == expected_kind
        ]
        if len(matching) != 1:
            raise ProviderDriverError(
                "ComfyUI 冻结主输出节点必须且只能产生一个类型匹配的文件",
                code="comfyui_primary_output_invalid",
            )
        source = _comfy_output_path(
            matching[0],
            expected_prefix=_frozen_output_prefix(snapshot),
        )
        max_bytes = int(resolve_configuration(
            required_keys=("artifact.max_bytes",),
        ).values["artifact.max_bytes"])
        facts = verified_local_media_facts(
            source,
            expected_kind=expected_kind,
            max_bytes=max_bytes,
            include_observed_media=expected_kind in {"video", "audio"},
        )
        from studio.services.workflow_resolution import validate_resolved_image_output
        validate_resolved_image_output(source, snapshot.get("compiled_output_contract") or {})
        with transaction.atomic():
            if AssetGenerationJob.objects.filter(
                pk=job.pk,
                status=AssetGenerationJob.Status.CANCEL_REQUESTED,
            ).exists():
                asset = None
            else:
                asset = _materialize_downloaded_asset(job, source, facts)
            return _finish_success(
                job,
                asset=asset,
                actual_cost=job.estimated_cost,
                currency=job.cost_currency,
            )
    except ProviderDriverError as exc:
        current = AssetGenerationJob.objects.get(pk=job.pk)
        if (
            current.provider_request_id
            and current.status in {
                AssetGenerationJob.Status.SUBMITTED,
                AssetGenerationJob.Status.RUNNING,
                AssetGenerationJob.Status.CANCEL_REQUESTED,
            }
            and exc.code in {"comfyui_operation_failed", "provider_collect_not_ready"}
        ):
            return _schedule(
                job,
                status=current.status,
                remote_status=current.remote_status,
                error_code=exc.code,
                error_message="ComfyUI 暂时不可用；将继续同一 request，不会重复提交。",
            )
        crossed = _confirmed_remote_submission(job)
        remote_succeeded = current.remote_status == "succeeded"
        return _fail(
            job,
            code=exc.code,
            message=(
                f"ComfyUI 远端已成功，但本地产物校验或物化失败：{exc}"
                if remote_succeeded
                else f"ComfyUI 已受理后处理失败，禁止重复提交：{exc}"
                if crossed else str(exc)
            ),
            uncertain=bool(
                not remote_succeeded and (crossed or exc.submission_uncertain)
            ),
            remote_status="succeeded" if remote_succeeded else None,
            actual_cost=job.estimated_cost if remote_succeeded else None,
            currency=job.cost_currency if remote_succeeded else "",
        )
    except (ValidationError, ProvenanceError, ComfyUIError) as exc:
        current = AssetGenerationJob.objects.get(pk=job.pk)
        crossed = _confirmed_remote_submission(job)
        remote_succeeded = current.remote_status == "succeeded"
        return _fail(
            job,
            code="comfyui_asset_job_invalid",
            message=(
                f"ComfyUI 远端已成功，但本地产物校验失败：{exc}"
                if remote_succeeded
                else f"ComfyUI 已受理后本地校验失败，禁止重复提交：{exc}"
                if crossed else str(exc)
            ),
            uncertain=bool(crossed and not remote_succeeded),
            remote_status="succeeded" if remote_succeeded else None,
            actual_cost=job.estimated_cost if remote_succeeded else None,
            currency=job.cost_currency if remote_succeeded else "",
        )
    except OSError as exc:
        current = AssetGenerationJob.objects.get(pk=job.pk)
        if current.provider_request_id and current.remote_status == "succeeded":
            return _schedule(
                job,
                status=AssetGenerationJob.Status.RUNNING,
                remote_status="succeeded",
                error_code="asset_storage_temporarily_unavailable",
                error_message=str(exc)[:2000],
            )
        raise


@transaction.atomic
def request_asset_generation_cancel(job: AssetGenerationJob) -> AssetGenerationJob:
    job = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
    if job.status == AssetGenerationJob.Status.CANCELLED:
        return job
    if job.status in {
        AssetGenerationJob.Status.SUCCEEDED,
        AssetGenerationJob.Status.FAILED,
        AssetGenerationJob.Status.UNCERTAIN,
    }:
        raise ValidationError("当前任务状态不能取消")
    if job.status == AssetGenerationJob.Status.CANCEL_REQUESTED:
        if not job.ignore_late_result:
            job.ignore_late_result = True
            job.save(update_fields=("ignore_late_result", "updated_at"))
        return job
    job.ignore_late_result = True
    if job.status == AssetGenerationJob.Status.QUEUED and job.claim_token is None:
        job.status = AssetGenerationJob.Status.CANCELLED
        job.finished_at = timezone.now()
    else:
        job.status = AssetGenerationJob.Status.CANCEL_REQUESTED
        job.cancel_requested_at = job.cancel_requested_at or timezone.now()
        job.next_poll_at = timezone.now()
    job.save(update_fields=(
        "status", "finished_at", "cancel_requested_at", "ignore_late_result",
        "next_poll_at", "updated_at",
    ))
    return job


def retry_asset_generation_job(
    job: AssetGenerationJob,
    *,
    client_idempotency_key: str = "",
) -> tuple[AssetGenerationJob, bool]:
    if job.status not in {AssetGenerationJob.Status.FAILED, AssetGenerationJob.Status.CANCELLED}:
        raise ValidationError("只有明确失败或已取消的任务可以重试；不确定任务必须先人工核验")
    workflow_input_request: dict = {}
    if job.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
        if job.request_snapshot_schema_version != 3:
            raise ValidationError(
                "历史 ComfyUI 素材任务缺少 v3 workflow_inputs 冻结意图，不能自动重试；请重新创建任务",
            )
        snapshot = _snapshot(job)
        workflow_input_request = snapshot.get("workflow_input_request")
        if (
            not isinstance(workflow_input_request, dict)
            or workflow_input_request.get("schema_version") != FIXTURE_SCHEMA_VERSION
        ):
            raise ValidationError(
                "ComfyUI 素材任务缺少完整的 workflow_input_request 冻结意图，不能自动重试；请重新创建任务",
            )
    return create_asset_generation_job(
        owner=job.owner,
        job_type=job.job_type,
        media_type=job.media_type,
        execution_kind=job.execution_kind,
        deployment=(
            job.model_deployment
            if job.execution_kind == AssetGenerationJob.ExecutionKind.DEPLOYMENT
            else None
        ),
        workflow_version=(
            job.workflow_template_version
            if job.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
            else None
        ),
        prompt=job.prompt,
        negative_prompt=job.negative_prompt,
        parameters=(
            job.parameters
            if job.execution_kind == AssetGenerationJob.ExecutionKind.DEPLOYMENT
            else {}
        ),
        workflow_inputs=(
            workflow_input_request
            if job.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
            else {}
        ),
        style=job.style,
        image_category=job.image_category,
        name=job.name,
        description=job.description,
        allow_external_calls=job.allow_external_calls,
        budget_limit=job.budget_limit,
        client_idempotency_key=(
            str(client_idempotency_key or "").strip() or f"retry:{job.id}"
        ),
    )


@transaction.atomic
def recover_stale_asset_generation_jobs(
    minutes: float,
    *,
    text: bool | None = None,
    execution_kind: str | None = None,
    text_submission_grace_seconds: int,
) -> dict[str, int]:
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, (int, float))
        or not math.isfinite(float(minutes))
        or float(minutes) <= 0
    ):
        raise ValidationError("asset generation 陈旧任务回收阈值必须是正有限分钟数")
    if isinstance(text_submission_grace_seconds, bool) or not isinstance(
        text_submission_grace_seconds, int,
    ) or text_submission_grace_seconds < 1:
        raise ValidationError("asset text submission grace seconds 必须是正整数")
    cutoff = timezone.now() - timedelta(minutes=float(minutes))
    released = 0
    uncertain = 0
    queryset = AssetGenerationJob.objects.select_for_update().filter(
        claim_token__isnull=False,
        heartbeat_at__lt=cutoff,
    )
    if execution_kind is not None:
        queryset = queryset.filter(execution_kind=execution_kind)
    if text is True:
        queryset = queryset.filter(operation=SCRIPT_GENERATE_OPERATION)
    elif (
        text is False
        and execution_kind != AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
    ):
        queryset = queryset.filter(operation__in=tuple(MEDIA_OPERATION.values()))
    now = timezone.now()
    for job in queryset:
        if (
            job.operation == SCRIPT_GENERATE_OPERATION
            and job.status == AssetGenerationJob.Status.SUBMITTING
            and job.submission_started_at is not None
        ):
            timeout_seconds = float(
                (job.request_snapshot or {}).get("text_timeout_seconds") or 0
            )
            if now < job.submission_started_at + timedelta(
                seconds=timeout_seconds + text_submission_grace_seconds,
            ):
                continue
        if (
            job.status == AssetGenerationJob.Status.SUBMITTING
            or (
                job.status == AssetGenerationJob.Status.CANCEL_REQUESTED
                and job.submission_started_at is not None
                and job.submitted_at is None
            )
        ):
            job.status = AssetGenerationJob.Status.UNCERTAIN
            job.error_code = "uncertain_remote_response"
            job.error_message = "worker 在模型提交边界失联；禁止自动重复提交。"
            job.finished_at = timezone.now()
            uncertain += 1
        else:
            released += 1
        _clear_claim(job)
        job.save(update_fields=(
            "status", "error_code", "error_message", "finished_at", "claim_token",
            "worker_id", "claimed_at", "updated_at",
        ))
    return {"released": released, "uncertain": uncertain}


def reconcile_uncertain_comfy_asset_jobs(*, limit: int) -> dict[str, int]:
    """Reattach stable Comfy prompt IDs without ever repeating ``/prompt``."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValidationError("asset uncertain reconcile batch size 必须是正整数")

    resumed = 0
    terminal = 0
    unresolved = 0
    job_ids = list(
        AssetGenerationJob.objects.filter(
            execution_kind=AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
            status=AssetGenerationJob.Status.UNCERTAIN,
        ).order_by("created_at").values_list("id", flat=True)[:limit]
    )
    for job_id in job_ids:
        job = AssetGenerationJob.objects.select_related(
            "owner", "style", "model_deployment__profile_version__provider",
            "workflow_template_version__template",
        ).get(pk=job_id)
        try:
            snapshot = _snapshot(job)
            payload = snapshot.get("payload")
            token = str(payload.get("submission_token") or "") if isinstance(payload, dict) else ""
            if token != str(job.id):
                raise ValidationError("ComfyUI uncertain 任务的冻结 submission token 无效")
            deployment = _verify_live_target(
                job,
                snapshot,
                require_executable_route=False,
            )
            response = driver_for(deployment.provider).invoke(
                deployment,
                "poll",
                {"request_id": token},
            )
        except (ValidationError, ProviderDriverError, ValueError, TypeError):
            unresolved += 1
            continue
        remote_status = str(response.get("status") or "") if isinstance(response, dict) else ""
        if remote_status == "missing" or not remote_status:
            # Absence from both queue and history is not proof that a timed-out
            # POST was never accepted. Staff evidence is still required.
            unresolved += 1
            continue
        if remote_status == "failed":
            _fail(
                job,
                code="remote_generation_failed",
                message="稳定 ComfyUI submission token 已确认远端失败。",
                remote_status="failed",
            )
            terminal += 1
            continue
        if remote_status not in {"running", "succeeded"}:
            unresolved += 1
            continue
        with transaction.atomic():
            locked = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
            if locked.status != AssetGenerationJob.Status.UNCERTAIN:
                continue
            now = timezone.now()
            locked.provider_request_id = token
            locked.provider_request_id_sha256 = hashlib.sha256(
                token.encode("utf-8"),
            ).hexdigest()
            locked.submitted_at = locked.submitted_at or now
            locked.status = (
                AssetGenerationJob.Status.CANCEL_REQUESTED
                if locked.ignore_late_result else AssetGenerationJob.Status.RUNNING
            )
            locked.remote_status = remote_status
            locked.next_poll_at = now
            locked.finished_at = None
            locked.error_code = ""
            locked.error_message = ""
            locked.heartbeat_at = now
            _clear_claim(locked)
            locked.save(update_fields=(
                "provider_request_id", "provider_request_id_sha256", "submitted_at",
                "status", "remote_status", "next_poll_at", "finished_at",
                "error_code", "error_message", "heartbeat_at", "claim_token",
                "worker_id", "claimed_at", "updated_at",
            ))
        resumed += 1
    return {"resumed": resumed, "terminal": terminal, "unresolved": unresolved}


def _latest_attested_resolution_cost(
    job: AssetGenerationJob,
) -> tuple[Decimal | None, str]:
    for resolution in job.resolutions.order_by("-revision"):
        evidence = resolution.evidence if isinstance(resolution.evidence, dict) else {}
        raw_cost = evidence.get("actual_cost")
        source = str(evidence.get("actual_cost_source") or "operator_attested")
        if raw_cost is not None and source != "estimated_fallback":
            return (
                _decimal_amount(raw_cost, field="actual_cost"),
                str(evidence.get("cost_currency") or "").strip().upper()[:12],
            )
    return None, ""


@transaction.atomic
def resolve_uncertain_asset_generation_job(
    job: AssetGenerationJob,
    *,
    outcome: str,
    note: str,
    user,
    provider_request_id: str = "",
    actual_cost=None,
    cost_currency: str = "",
    client_idempotency_key: str = "",
) -> tuple[AssetGenerationJobResolution, bool]:
    """Append staff evidence and deterministically close or resume uncertainty."""

    if not getattr(user, "is_staff", False):
        raise ValidationError("只有 staff 可以消歧素材生成任务")
    normalized_note = normalize_audit_note(note)
    if outcome not in set(AssetGenerationJobResolution.Outcome.values):
        raise ValidationError({"outcome": "素材生成消歧结果无效"})
    supplied_request_id = str(provider_request_id or "").strip()
    if len(supplied_request_id) > 191:
        raise ValidationError({"provider_request_id": "provider request id 过长"})
    parsed_cost = _decimal_amount(actual_cost, field="actual_cost")
    supplied_currency = str(cost_currency or "").strip().upper()
    if len(supplied_currency) > 12:
        raise ValidationError({"cost_currency": "费用币种过长"})
    if parsed_cost is not None and not supplied_currency:
        raise ValidationError({"cost_currency": "提供实际成本时必须提供币种"})
    request_intent = json_contract({
        "schema_version": 1,
        "outcome": outcome,
        "note": normalized_note,
        "provider_request_id_sha256": (
            hashlib.sha256(supplied_request_id.encode("utf-8")).hexdigest()
            if supplied_request_id else ""
        ),
        "actual_cost": str(parsed_cost) if parsed_cost is not None else None,
        "cost_currency": supplied_currency,
    }, max_bytes=_stage_snapshot_max_bytes())
    raw_idempotency_key = str(client_idempotency_key or "").strip()
    if len(raw_idempotency_key) > 191:
        raise ValidationError({"idempotency_key": "消歧幂等键过长"})
    idempotency_key = raw_idempotency_key or f"auto:{request_intent.sha256}"

    locked = AssetGenerationJob.objects.select_for_update().select_related(
        "model_deployment__profile_version__provider",
    ).get(pk=job.pk)
    existing_resolution = locked.resolutions.filter(
        idempotency_key=idempotency_key,
    ).first()
    if existing_resolution is not None:
        if existing_resolution.request_sha256 != request_intent.sha256:
            raise ValidationError("同一消歧幂等键不能用于不同请求")
        return existing_resolution, False
    if locked.status != AssetGenerationJob.Status.UNCERTAIN:
        raise ValidationError("只有 uncertain 素材生成任务可以人工消歧")

    if locked.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
        frozen_comfy_token = str(locked.id)
        if supplied_request_id and supplied_request_id != frozen_comfy_token:
            raise ValidationError({
                "provider_request_id": "ComfyUI 消歧只能使用任务冻结的 submission token",
            })
        if outcome != AssetGenerationJobResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
            supplied_request_id = supplied_request_id or frozen_comfy_token

    supplied_request_hash = (
        hashlib.sha256(supplied_request_id.encode("utf-8")).hexdigest()
        if supplied_request_id else ""
    )
    frozen_request_hash = (
        locked.provider_request_id_sha256
        or (
            hashlib.sha256(locked.provider_request_id.encode("utf-8")).hexdigest()
            if locked.provider_request_id else ""
        )
    )
    if supplied_request_id and locked.provider_request_id:
        if supplied_request_id != locked.provider_request_id:
            raise ValidationError({
                "provider_request_id": "输入的 provider request id 与任务已冻结事实冲突",
            })
    if supplied_request_hash and frozen_request_hash:
        if supplied_request_hash != frozen_request_hash:
            raise ValidationError({
                "provider_request_id": "输入的 provider request id 摘要与任务事实冲突",
            })
    request_id = supplied_request_id or locked.provider_request_id
    request_id_hash = supplied_request_hash or frozen_request_hash
    if outcome == AssetGenerationJobResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
        if request_id_hash or locked.submitted_at is not None:
            raise ValidationError({
                "provider_request_id": "任务已有远端 request id 或 submitted 时间，不能确认未提交",
            })
        if parsed_cost not in (None, Decimal("0")):
            raise ValidationError({"actual_cost": "确认未提交时实际成本必须为空或 0"})
    elif locked.operation != SCRIPT_GENERATE_OPERATION and not request_id_hash:
        raise ValidationError({
            "provider_request_id": "媒体远端结论必须关联 provider request id 摘要",
        })

    prior_cost, prior_currency = _latest_attested_resolution_cost(locked)
    effective_cost = parsed_cost
    actual_cost_source = "operator_attested" if parsed_cost is not None else "unknown"
    if effective_cost is None and prior_cost is not None:
        effective_cost = prior_cost
        actual_cost_source = "carried_forward"
    if (
        effective_cost is None
        and outcome == AssetGenerationJobResolution.Outcome.CONFIRMED_REMOTE_SUCCEEDED
        and (
            locked.operation == SCRIPT_GENERATE_OPERATION
            or not request_id
            or locked.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
        )
    ):
        effective_cost = locked.estimated_cost
        actual_cost_source = "estimated_fallback" if effective_cost is not None else "unknown"
    effective_currency = supplied_currency or prior_currency or locked.cost_currency
    if locked.cost_currency and effective_currency and locked.cost_currency != effective_currency:
        raise ValidationError({"cost_currency": "实际费用币种与冻结币种冲突"})
    if effective_cost not in (None, Decimal("0")) and not effective_currency:
        raise ValidationError({"cost_currency": "已知费用必须关联币种"})

    now = timezone.now()
    evidence_contract = json_contract({
        "schema_version": 1,
        "job_id": str(locked.id),
        "job_type": locked.job_type,
        "media_type": locked.media_type,
        "operation": locked.operation,
        "request_snapshot_sha256": locked.request_snapshot_sha256,
        "previous_status": locked.status,
        "previous_error_code": locked.error_code,
        "cancel_intent": locked.ignore_late_result,
        "cancel_requested_at": (
            locked.cancel_requested_at.isoformat() if locked.cancel_requested_at else None
        ),
        "outcome": outcome,
        "provider_request_id_sha256": request_id_hash,
        "actual_cost": str(effective_cost) if effective_cost is not None else None,
        "actual_cost_source": actual_cost_source,
        "cost_currency": effective_currency,
        "resolved_at": now.isoformat(),
    }, max_bytes=_stage_snapshot_max_bytes())
    revision = (locked.resolutions.aggregate(value=Max("revision"))["value"] or 0) + 1
    resolution = AssetGenerationJobResolution.objects.create(
        job=locked,
        revision=revision,
        outcome=outcome,
        idempotency_key=idempotency_key,
        request_sha256=request_intent.sha256,
        provider_request_id_sha256=request_id_hash,
        evidence=evidence_contract.value,
        evidence_schema_version=evidence_contract.schema_version,
        evidence_size_bytes=evidence_contract.size_bytes,
        evidence_sha256=evidence_contract.sha256,
        note=normalized_note,
        decided_by=user,
    )

    if effective_cost is not None:
        locked.actual_cost = effective_cost
    if effective_currency:
        locked.cost_currency = effective_currency
    if request_id:
        locked.provider_request_id = request_id
    if request_id_hash:
        locked.provider_request_id_sha256 = request_id_hash
    locked.next_poll_at = None
    locked.finished_at = now
    locked.heartbeat_at = now
    _clear_claim(locked)
    if outcome == AssetGenerationJobResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
        locked.status = (
            AssetGenerationJob.Status.CANCELLED
            if locked.ignore_late_result else AssetGenerationJob.Status.FAILED
        )
        locked.remote_status = "operator_confirmed_not_submitted"
        locked.error_code = "operator_confirmed_not_submitted"
        locked.error_message = "管理员确认远端未提交。"
    elif outcome == AssetGenerationJobResolution.Outcome.CONFIRMED_REMOTE_FAILED:
        locked.status = AssetGenerationJob.Status.FAILED
        locked.remote_status = "operator_confirmed_remote_failed"
        locked.error_code = "operator_confirmed_remote_failed"
        locked.error_message = "管理员确认远端已明确失败；可创建新的显式重试任务。"
    elif outcome == AssetGenerationJobResolution.Outcome.CONFIRMED_REMOTE_CANCELLED:
        locked.status = AssetGenerationJob.Status.CANCELLED
        locked.remote_status = "operator_confirmed_remote_cancelled"
        locked.error_code = "operator_confirmed_remote_cancelled"
        locked.error_message = "管理员确认远端已取消。"
    elif locked.operation == SCRIPT_GENERATE_OPERATION:
        locked.status = (
            AssetGenerationJob.Status.CANCELLED
            if locked.ignore_late_result else AssetGenerationJob.Status.FAILED
        )
        locked.remote_status = "operator_confirmed_remote_succeeded"
        locked.error_code = (
            "late_result_ignored"
            if locked.ignore_late_result
            else "operator_confirmed_text_result_unavailable"
        )
        locked.error_message = (
            "管理员确认远端成功；因用户已取消，文本结果不会进入素材库。"
            if locked.ignore_late_result
            else "管理员确认远端成功，但文本响应无法安全恢复；可显式新建重试。"
        )
    elif locked.ignore_late_result and effective_cost is not None:
        locked.status = AssetGenerationJob.Status.CANCELLED
        locked.remote_status = "operator_confirmed_remote_succeeded"
        locked.error_code = "late_result_ignored"
        locked.error_message = "管理员确认远端成功及费用；取消意图生效，产物不会进入素材库。"
    elif not request_id:
        locked.status = (
            AssetGenerationJob.Status.CANCELLED
            if locked.ignore_late_result else AssetGenerationJob.Status.FAILED
        )
        locked.remote_status = "operator_confirmed_remote_succeeded"
        locked.error_code = (
            "late_result_ignored"
            if locked.ignore_late_result
            else "operator_confirmed_result_unrecoverable"
        )
        locked.error_message = (
            "管理员确认远端成功，但 request id 仅有摘要，无法安全 collect；结果未入库。"
        )
    elif locked.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
        locked.status = (
            AssetGenerationJob.Status.CANCEL_REQUESTED
            if locked.ignore_late_result else AssetGenerationJob.Status.RUNNING
        )
        locked.remote_status = "succeeded"
        locked.submitted_at = locked.submitted_at or now
        locked.next_poll_at = now
        locked.finished_at = None
        locked.error_code = (
            "late_result_pending_cost_reconciliation"
            if locked.ignore_late_result else ""
        )
        locked.error_message = (
            "取消后的 ComfyUI 成功结果只恢复费用对账，产物不会进入素材库。"
            if locked.ignore_late_result else ""
        )
    else:
        terminal_success = list(
            _combined_config(locked.model_deployment).get("terminal_success") or []
        )
        if not terminal_success:
            raise ValidationError("媒体 deployment 缺少冻结终态成功值，不能恢复 collect")
        locked.status = (
            AssetGenerationJob.Status.CANCEL_REQUESTED
            if locked.ignore_late_result else AssetGenerationJob.Status.SUBMITTED
        )
        locked.remote_status = str(terminal_success[0])[:120]
        locked.submitted_at = locked.submitted_at or now
        locked.next_poll_at = now
        locked.finished_at = None
        locked.error_code = (
            "late_result_pending_cost_reconciliation"
            if locked.ignore_late_result else ""
        )
        locked.error_message = (
            "取消后的远端成功结果只恢复费用 collect，产物不会进入素材库。"
            if locked.ignore_late_result else ""
        )
    locked.save(update_fields=(
        "status", "provider_request_id", "provider_request_id_sha256",
        "remote_status", "submitted_at", "actual_cost", "cost_currency",
        "next_poll_at", "finished_at", "heartbeat_at", "error_code",
        "error_message", "claim_token", "worker_id", "claimed_at", "updated_at",
    ))
    return resolution, True


def fail_unhandled_asset_generation_job(job: AssetGenerationJob) -> AssetGenerationJob:
    current = AssetGenerationJob.objects.get(pk=job.pk)
    crossed_submission_boundary = (
        current.submission_started_at is not None
        and current.status not in {
            AssetGenerationJob.Status.SUCCEEDED,
            AssetGenerationJob.Status.FAILED,
            AssetGenerationJob.Status.CANCELLED,
            AssetGenerationJob.Status.UNCERTAIN,
        }
    )
    return _fail(
        job,
        code=(
            "uncertain_remote_response"
            if crossed_submission_boundary else "asset_generation_worker_internal_error"
        ),
        message=(
            "素材生成 worker 在远端提交边界后异常退出；禁止自动重试，需人工核验远端。"
            if crossed_submission_boundary
            else "素材生成 worker 出现未处理错误；详细信息仅保留在受控日志。"
        ),
        uncertain=crossed_submission_boundary,
    )
