from __future__ import annotations

from datetime import timedelta
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from pathlib import Path
import tempfile
from urllib.parse import urlparse
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from studio.models import (
    Asset,
    AssetGenerationJob,
    AssetGenerationJobResolution,
    GenerationTask,
    ModelDeployment,
    ProviderProfile,
    Style,
    WorkflowSmokeAttempt,
    WorkerLease,
)
from studio.services.async_media_contract import (
    validate_standard_payload,
)
from studio.services.configuration import resolve_configuration
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.execution_asset_dispatch import begin_asset_execution_submission, check_asset_execution_staging
from studio.services.execution_asset_requests import read_asset_prompt_hint
from studio.services.json_contracts import assign_json_contract, json_contract, verify_json_contract
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.provenance import ProvenanceError
from studio.services.provider_registry import (
    ProviderDriverError,
    driver_for,
    registered_driver_capabilities,
    restricted_async_rest_runtime_budget,
    restricted_comfyui_runtime_budget,
)
from studio.services.production_pipeline import can_execute_template
from studio.services.remote_artifacts import download_remote_artifact
from studio.services.security_boundaries import boundary_int
from studio.services.content_contracts import normalize_audit_note
from studio.services.workflow_smoke_resolution import (
    unresolved_workflow_smoke_attempts,
)
from studio.services.task_queue import ComfyUIError


def _text_driver_types():
    return tuple(key for key, contract in registered_driver_capabilities().items()
        if "chat_completion" in contract.get("operations", []))


def _is_text_job(job):
    return job.model_deployment.provider.driver_type in _text_driver_types()


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




def _asset_execution_output_kind(job_type, media_type):
    """Business script files consume text output; catalogs and writes agree."""
    return "text" if job_type == "prompt" or media_type == "script" else media_type


def asset_generation_options(*, user, job_type: str, media_type: str):
    """Return a pageable exact-version catalog, without reading Smoke examples.

    Configuration choices are not executable permits. Input-contract detail and
    root admission perform their own full checks; no catalog call invokes a model.
    """
    from studio.services.execution_instances import selectable_execution_versions
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
    if job_type not in {"asset", "prompt"} or (media_type not in {"image", "video", "audio", "script"}
            and (job_type, media_type) != ("prompt", "text")):
        raise ValidationError("请选择正式任务与素材类型")
    kind = _asset_execution_output_kind(job_type, media_type)
    return selectable_execution_versions(user).exclude(operation__in=FIELD_OPERATION_MODES).filter(
        Q(contract__definition__interface__output__kind=kind)
        | Q(contract__definition__interface__output__contract__primary__kind=kind)
    ).select_related("instance", "contract").only(
        "id", "instance_id", "instance__id", "instance__key", "instance__name", "version", "operation", "mode", "prompt_mode",
        "contract__id", "contract__version_id", "contract__sha256",
    ).order_by("instance__key", "-version", "id")


def _decimal_amount(value, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValidationError({field: "必须是非负有限金额"})
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "必须是非负有限金额"}) from exc
    if not amount.is_finite() or amount < 0 or amount >= Decimal("100000000"):
        raise ValidationError({field: "必须是非负有限金额"})
    return amount


def _actual_cost_currency(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", value):
        raise ValidationError({"cost_currency": "实际费用必须附带明确币种，不能从估算推断或截断"})
    return value


@registry_reference_writer
def create_asset_generation_job(
    *, owner, execution_instance_version_id, expected_definition_sha256,
    job_type: str, media_type: str, inputs: dict, parameters: dict,
    manual_channels: dict, subject_facts: list, source_language: str, target_language: str,
    field_values: list, style: Style | None = None, image_category: str = "",
    name: str = "", description: str = "", allow_external_calls: bool = False,
    budget_limit=None, client_idempotency_key: str, appearance_target=None,
) -> tuple[AssetGenerationJob, bool]:
    """Create one real root plus its preparation; never queue a provider here.

    The published instance is the only target selector. Missing editable fields
    are a persisted wait, not a fake prompt or an implicit auxiliary approval.
    """
    from studio.services.execution_definitions import (
        execution_definition_version, stored_execution_contract, verify_current_execution_definition,
    )
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.execution_request_storage import execution_request_max_bytes
    from studio.services.execution_preparation_fields import create_manual_execution_field
    from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
    from studio.services.execution_preparations import create_execution_instance_preparation, read_execution_preparation_detail
    from studio.services.execution_request_preparation import resolve_execution_request_facts
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
    from studio.services.media_generation_appearances import normalize_media_appearance_request

    appearance_target = normalize_media_appearance_request(appearance_target)
    if appearance_target is not None and (job_type != "asset" or media_type != "image"):
        raise ValidationError("主图/变体声明仅用于图片素材生成")

    if not owner or not owner.is_authenticated or not owner.is_active:
        raise PermissionDenied("需要有效登录身份")
    if type(allow_external_calls) is not bool:
        raise ValidationError({"allow_external_calls": "必须明确授权或不授权外部调用"})
    if job_type not in {AssetGenerationJob.JobType.ASSET, AssetGenerationJob.JobType.PROMPT}:
        raise ValidationError({"job_type": "此入口只能创建素材或提示词候选，不能伪造辅助任务"})
    if media_type not in {AssetGenerationJob.MediaType.IMAGE, AssetGenerationJob.MediaType.VIDEO,
            AssetGenerationJob.MediaType.AUDIO, AssetGenerationJob.MediaType.SCRIPT} and not (job_type == "prompt" and media_type == "text"):
        raise ValidationError({"media_type": "请选择正式素材类型"})
    if style is not None and style.owner_id not in (None, owner.id) and not owner.is_staff:
        raise PermissionDenied("不能使用其他用户的风格")
    if not isinstance(name, str) or len(name) > 160 or not isinstance(description, str):
        raise ValidationError("素材名称或描述格式无效")
    if job_type == AssetGenerationJob.JobType.ASSET:
        if not name.strip():
            raise ValidationError({"name": "生成素材必须填写名称"})
        if media_type == AssetGenerationJob.MediaType.IMAGE:
            if image_category not in IMAGE_CATEGORIES:
                raise ValidationError({"image_category": "图片类别需明确选择人物、场景、道具或通用"})
            if image_category != Asset.Kind.REFERENCE and not description.strip():
                raise ValidationError({"description": "分类图片素材必须填写基础描述"})
        elif image_category:
            raise ValidationError({"image_category": "只有图片素材可以设置图片类别"})
    elif image_category:
        raise ValidationError({"image_category": "提示词候选不设置图片素材类别"})
    if not isinstance(field_values, list):
        raise ValidationError({"field_values": "字段内容必须是明确列表"})
    field_keys = set()
    for row in field_values:
        if (not isinstance(row, dict) or set(row) != {"unit_key", "field_key", "text"}
                or any(not isinstance(row[key], str) for key in row)
                or (row["unit_key"], row["field_key"]) in field_keys):
            raise ValidationError({"field_values": "字段需明确单元、字段名和正文，不能重复或包含批准信息"})
        field_keys.add((row["unit_key"], row["field_key"]))
    if not isinstance(client_idempotency_key, str) or not client_idempotency_key.strip() or len(client_idempotency_key) > 191:
        raise ValidationError({"Idempotency-Key": "创建需要1至191字符的稳定幂等键，重放请使用原键"})
    if not isinstance(expected_definition_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_definition_sha256):
        raise ValidationError({"expected_definition_sha256": "需要明确的实例定义摘要"})
    if budget_limit is not None:
        raise ValidationError("资源提醒由系统配置统一管理，不再接受单次预算")
    budget = None
    requested = {"inputs": inputs, "parameters": parameters, "manual_channels": manual_channels,
        "subject_facts": subject_facts, "source_language": source_language, "target_language": target_language}
    client_intent = json_contract({"schema_version": 1,
        "execution_instance_version_id": str(execution_instance_version_id), "definition_sha256": expected_definition_sha256,
        "job_type": job_type, "media_type": media_type, "style_id": str(style.pk) if style else None,
        "image_category": image_category, "name": name, "description": description,
        "request": requested, "field_values": field_values, "allow_external_calls": allow_external_calls,
        **({"appearance_target": appearance_target} if appearance_target is not None else {}),
        "budget_limit": str(budget) if budget is not None else None}, max_bytes=execution_request_max_bytes())
    key_hash = hashlib.sha256(client_idempotency_key.encode("utf-8")).hexdigest()
    idempotency_key = f"asset-generation:{owner.pk}:{key_hash}"
    # Serialize same-owner allocation as in the original tenant-scoped queue;
    # a replay happens before current target/evidence checks or seed drawing.
    get_user_model().objects.select_for_update().get(pk=owner.pk)
    existing = AssetGenerationJob.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        header = _asset_request_header(existing)
        if existing.owner_id != owner.pk or header["client_request_sha256"] != client_intent.sha256:
            raise ValidationError("Idempotency-Key已用于不同的素材生成请求", code="execution_preparation_conflict")
        preparation = existing.execution_preparations.get()
        read_execution_preparation_detail(preparation)
        return existing, False
    version = execution_definition_version(execution_instance_version_id)
    if version.status != "published" or not version.instance.is_active:
        raise ValidationError("请选择启用执行实例的已发布精确版本")
    if version.workflow_version_id and not can_execute_template(version.workflow_version.template, owner):
        raise PermissionDenied("当前用户无权执行该工作流")
    definition = verify_current_execution_definition(stored_execution_contract(version.pk))
    if definition.sha256 != expected_definition_sha256:
        raise ValidationError("执行定义与本次确认不一致，请重新读取", code="execution_preparation_conflict")
    if version.operation in FIELD_OPERATION_MODES:
        raise ValidationError("字段辅助叶子不能从素材生成入口直接调用")
    output = definition.value["interface"]["output"]
    output_kind = output["contract"]["primary"]["kind"] if version.workflow_version_id else output["kind"]
    expected_kind = _asset_execution_output_kind(job_type, media_type)
    if output_kind != expected_kind:
        raise ValidationError({"media_type": "所选执行实例的实际输出合同与请求类型不一致"})
    if field_values and version.prompt_mode != "profile":
        raise ValidationError("完整人工提示词与策略字段不能同时提交")
    deployment = version.deployment
    external = _is_external(deployment)
    if version.workflow_version_id and external:
        raise ValidationError("远程ComfyUI尚无受控产物下载合同，不能创建可运行请求")
    header = _execution_asset_header(definition, deployment, client_intent.sha256, output_kind)
    job = AssetGenerationJob(owner=owner, style=style, execution_instance_version=version,
        model_deployment=deployment, workflow_template_version=version.workflow_version,
        job_type=job_type, media_type=media_type, operation=version.operation,
        execution_kind="comfy_workflow" if version.workflow_version_id else "deployment",
        image_category=image_category, name=name, description=description, prompt="",
        status=AssetGenerationJob.Status.PREPARING, is_external=external,
        allow_external_calls=allow_external_calls, budget_limit=budget)
    job.idempotency_key = idempotency_key
    assign_json_contract(job, "request_snapshot", header.value, max_bytes=execution_configuration_max_bytes())
    # clean checks cross-table target identities, not an admission permission.
    job.clean()
    job.save()
    def resolve(definition, root):
        # This root owns a request, not a ProductionRun unit map. Explicit
        # request-scoped media can feed its supplied units; real Shot scopes
        # must not silently become global references.
        from studio.services.media_generation_targets import MEDIA_KINDS, resolve_media_generation_target
        resolver = resolve_media_generation_target if output_kind in MEDIA_KINDS else resolve_execution_request_facts
        target_policy = {"candidate_count": 1, "candidate_evaluator": None,
            "appearance_target": appearance_target} if output_kind in MEDIA_KINDS else {}
        return resolver(definition, requested, **target_policy, owner=root.owner, unit_key="asset",
            max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}, max_bytes=execution_configuration_max_bytes()),
            parameter_policies=[], resolve_parameter_source=_reject_asset_parameter_source, scope_units={})
    preparation = create_execution_instance_preparation(owner_ref=("asset_job_id", job.pk), version_id=version.pk,
        expected_definition_sha256=definition.sha256, unit_key="asset", request_key=client_idempotency_key,
        intent=json_contract({**requested, **({"appearance_target": appearance_target} if appearance_target is not None else {})},
            max_bytes=execution_request_max_bytes()), resolve_facts=resolve, actor=owner)
    for row in field_values:
        create_manual_execution_field(preparation_id=preparation.pk, **row, revision=1,
            expected_preparation_revision=preparation.revision, actor=owner)
        preparation.refresh_from_db()
    preparation = evaluate_preparation_fields(preparation_id=preparation.pk, expected_preparation_revision=preparation.revision, actor=owner)
    from studio.services.execution_advancement import schedule_authorized_asset_fields
    schedule_authorized_asset_fields(preparation)
    return job, True


def _reject_asset_parameter_source(_source):
    raise ValidationError("素材请求没有生产步骤参数来源，必须按实例合同明确传入")


def _execution_asset_header(definition, deployment, client_request_sha256, output_kind):
    from studio.services.execution_instances import execution_configuration_max_bytes
    config = _combined_config(deployment)
    if definition.value["target"]["workflow"] is not None:
        budget = restricted_comfyui_runtime_budget(config)
    elif deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
        budget = restricted_async_rest_runtime_budget(config)
    else:
        budget = {"timeout_seconds": config["timeout_seconds"]}
    return json_contract({"schema_version": 1, "client_request_sha256": client_request_sha256,
        "execution_instance_version_id": definition.value["version_id"], "definition_sha256": definition.sha256,
        "output_kind": output_kind, "runtime_budget": budget}, max_bytes=execution_configuration_max_bytes())


def _asset_request_header(job):
    from studio.services.execution_instances import execution_configuration_max_bytes
    header = verify_json_contract(job.request_snapshot, schema_version=job.request_snapshot_schema_version,
        size_bytes=job.request_snapshot_size_bytes, sha256=job.request_snapshot_sha256,
        max_bytes=execution_configuration_max_bytes()).value
    if (not isinstance(header, dict) or set(header) != {"schema_version", "client_request_sha256",
            "execution_instance_version_id", "definition_sha256", "output_kind", "runtime_budget"}
            or type(header["schema_version"]) is not int or header["schema_version"] != 1
            or job.request_snapshot_schema_version != header["schema_version"]
            or header["execution_instance_version_id"] != str(job.execution_instance_version_id)
            or any(not isinstance(header[key], str) or not re.fullmatch(r"[a-f0-9]{64}", header[key])
                   for key in ("client_request_sha256", "definition_sha256"))
            or not isinstance(header["runtime_budget"], dict)):
        raise ValidationError("素材任务的当前请求头或执行实例关联不完整，不能恢复旧请求")
    return header


def _snapshot(job: AssetGenerationJob):
    from studio.services.execution_asset_requests import read_prepared_asset_execution
    return read_prepared_asset_execution(job, _asset_request_header(job))


def _frozen_max_poll_seconds(snapshot: dict) -> int:
    value = snapshot.runtime_budget.get("max_poll_seconds")
    boundary_key = (
        "security.runtime.max_timeout_seconds"
        if snapshot.transport.value["driver_type"] == ProviderProfile.DriverType.COMFYUI
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


def _verify_live_target(job: AssetGenerationJob, snapshot, *, require_executable_route: bool = True) -> ModelDeployment:
    from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
    from studio.services.execution_identity import execution_target_contract
    from studio.services.execution_instances import selectable_execution_versions
    if require_executable_route:
        if not selectable_execution_versions(job.owner).filter(pk=job.execution_instance_version_id).exists():
            raise ValidationError("执行实例已停用、退役或无执行权限，不能开始新的调用")
        definition = verify_current_execution_definition(stored_execution_contract(job.execution_instance_version_id))
        if definition.sha256 != snapshot.definition.sha256:
            raise ValidationError("执行定义与原请求不一致")
    else:
        # Poll/cancel must still address the original peer after retirement.
        # Resource identity drift is not permission to contact a new endpoint.
        target = execution_target_contract(deployment_id=job.model_deployment_id,
            workflow_version_id=job.workflow_template_version_id)
        if target.sha256 != snapshot.definition.value["target_fingerprint"]:
            raise ValidationError("执行资源已变化，不能到另一目标核对原任务")
    return ModelDeployment.objects.select_related("profile_version__provider").get(pk=job.model_deployment_id)


@transaction.atomic
def _claim(worker_id: str, *, text: bool) -> AssetGenerationJob | None:
    now = timezone.now()
    queryset = AssetGenerationJob.objects.select_for_update(skip_locked=True).select_related(
        "model_deployment__profile_version__provider", "workflow_template_version",
        "owner", "style",
    ).filter(claim_token__isnull=True, execution_preparations__request_frozen_at__isnull=False).exclude(
        status="queued", error_code="execution_resource_confirmation_required").filter(
        Q(status=AssetGenerationJob.Status.CANCEL_REQUESTED) | Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
    if text:
        queryset = queryset.filter(
            execution_kind=AssetGenerationJob.ExecutionKind.DEPLOYMENT,
            model_deployment__profile_version__provider__driver_type__in=_text_driver_types(),
            status__in=TEXT_JOB_STATUSES,
        )
    else:
        queryset = queryset.filter(
            execution_kind=AssetGenerationJob.ExecutionKind.DEPLOYMENT,
            model_deployment__profile_version__provider__driver_type=ProviderProfile.DriverType.ASYNC_REST,
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
    from studio.services.execution_production_attempts import superseded_uncertain_task_ids
    WorkerLease.objects.get_or_create(key="generation-claim")
    WorkerLease.objects.select_for_update().get(key="generation-claim")
    if unresolved_workflow_smoke_attempts().exists():
        return None
    if WorkflowSmokeAttempt.objects.filter(
        status__in=(
            WorkflowSmokeAttempt.Status.SUBMITTING,
            WorkflowSmokeAttempt.Status.RUNNING,
            WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
        ),
    ).exists():
        return None
    nonterminal_task_token = GenerationTask.objects.exclude(pk__in=superseded_uncertain_task_ids()).exclude(
        submission_token="",
    ).exclude(
        submission_state__in=(
            GenerationTask.SubmissionState.TERMINAL_SUCCEEDED,
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
        execution_preparations__request_frozen_at__isnull=False,
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
    if job is None and WorkflowSmokeAttempt.objects.filter(status=WorkflowSmokeAttempt.Status.QUEUED).exists():
        # Pending Smoke wins over new generation, never over polling an
        # already submitted asset. The Smoke worker waits for that asset.
        return None
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
        job = base.filter(status=AssetGenerationJob.Status.QUEUED).exclude(error_code="execution_resource_confirmation_required").filter(
            Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now)).order_by(
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
    if job.parent_preparation_id and job.status in {"succeeded", "failed", "cancelled", "uncertain"}:
        from studio.services.execution_asset_auxiliaries import schedule_asset_parent_preparation
        transaction.on_commit(lambda job_id=job.pk: schedule_asset_parent_preparation(job_id), robust=True)


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
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    with locked_preparation_owner_rows([("asset_job_id", job.pk)]) as owners:
        locked = owners[("asset_job_id", str(job.pk))]
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
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    with locked_preparation_owner_rows([("asset_job_id", job.pk)]) as owners:
        locked = owners[("asset_job_id", str(job.pk))]
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


def _asset_metadata(job: AssetGenerationJob, facts: dict) -> dict:
    request = _snapshot(job)
    observed = facts.get("observed_media") if isinstance(facts.get("observed_media"), dict) else {}
    metadata = {
        "manifest_schema_version": 1,
        "source": "model_generation",
        "generation_job_id": str(job.id),
        "execution_instance_version_id": str(job.execution_instance_version_id),
        "preparation_id": str(request.preparation.pk),
        "prepared_request_sha256": request.preparation.request_sha256,
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
        metadata["compiled_graph_sha256"] = request.transport.value["payload"]["compiled_workflow"]["graph_sha256"]
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


def read_generated_image_asset(job: AssetGenerationJob) -> Asset:
    """Verify the original output and manifest; never materialize on a read."""
    from studio.services.execution_media_sources import resolve_execution_asset_media
    if (job.status != AssetGenerationJob.Status.SUCCEEDED or job.media_type != "image"
            or not job.output_asset_id):
        raise ValidationError("独立图片任务尚无已完成的原Asset")
    actual = resolve_execution_asset_media(owner=job.owner, asset_id=job.output_asset_id,
        expected_kind="image", max_media_bytes=None)
    asset, facts = actual.asset, actual.media_facts
    if (asset.owner_id != job.owner_id or asset.mime_type != facts["mime_type"]
            or asset.metadata != _asset_metadata(job, {key: facts[key] for key in ("sha256", "size_bytes")})):
        raise ValidationError("已登记图片Asset与原独立生成来源不一致")
    return asset


@registry_reference_writer
def _finish_success(
    job: AssetGenerationJob,
    *,
    asset: Asset | None,
    result_text: str = "",
    actual_cost=None,
    currency: str = "",
):
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    with locked_preparation_owner_rows([("asset_job_id", job.pk)]) as owners:
        locked = owners[("asset_job_id", str(job.pk))]
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
        settled_cost = _decimal_amount(actual_cost, field="actual_cost")
        if settled_cost is not None:
            locked.cost_currency = _actual_cost_currency(currency)
            locked.actual_cost = settled_cost
        if locked.status == AssetGenerationJob.Status.SUCCEEDED and locked.actual_cost is not None:
            from studio.services.execution_resource_policy import actual_cost_review_message
            review_message = actual_cost_review_message(locked.actual_cost, locked.cost_currency)
            if review_message:
                locked.error_code, locked.error_message = "actual_budget_exceeded", review_message
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
        if locked.status == AssetGenerationJob.Status.SUCCEEDED and asset is not None and locked.media_type == "image":
            from rest_framework.exceptions import ValidationError as APIValidationError
            from studio.services.asset_references import register_generated_material
            try:
                register_generated_material(job=locked)
            except APIValidationError as exc:
                raise ValidationError(exc.detail) from exc
        from studio.models import ExecutionTextResponseReview
        if (locked.status == AssetGenerationJob.Status.SUCCEEDED
                and not ExecutionTextResponseReview.objects.filter(asset_job=locked, confirmed_at__isnull=False).exists()):
            from studio.services.execution_capacity_observations import capacity_observation_request, record_completed_capacity_observation
            record_completed_capacity_observation(locked, capacity_observation_request(locked))
        return locked


def _finish_text_asset_result(job: AssetGenerationJob, text: str, *, created_files=None):
    """Persist a protocol-verified inline result, independent of transport."""
    max_bytes = int(resolve_configuration(
        required_keys=("script.max_upload_bytes",),
    ).values["script.max_upload_bytes"])
    if not isinstance(text, str):
        raise ValidationError("文本结果必须为字符串")
    encoded = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    if not text.strip() or len(encoded) > max_bytes or b"\x00" in encoded:
        raise ValidationError("文本模型已返回但结果未通过安全合同", code="text_result_invalid")
    if AssetGenerationJob.objects.filter(pk=job.pk, status=AssetGenerationJob.Status.CANCEL_REQUESTED).exists():
        return _finish_success(job, asset=None)
    from studio.services.execution_text_response_review import asset_text_correction
    correction = asset_text_correction(job, text)
    with transaction.atomic():
        asset = None
        if job.job_type == AssetGenerationJob.JobType.ASSET:
            _tenant_quota_available(job.owner_id, len(encoded))
            asset = Asset(
                owner=job.owner, style=job.style, kind=Asset.Kind.SCRIPT,
                name=job.name, description=job.description, prompt_hint=read_asset_prompt_hint(job),
                source_info=_asset_source_info(job), mime_type="text/markdown",
                checksum=hashlib.sha256(encoded).hexdigest(),
                metadata=_asset_metadata(job, {"mime_type": "text/markdown",
                    "size_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}),
            )
            if correction is not None:
                asset.source_info = f"人工补正 · 素材任务 {job.pk} · 修订 {correction['review_id']}"
                asset.metadata.update(source="human_correction", text_response_correction=correction,
                    corrected_file_sha256=asset.metadata.pop("generated_sha256"))
            try:
                asset.file.save(f"{job.name or 'script'}.md", ContentFile(encoded), save=False)
                if created_files is not None:
                    created_files.append(asset.file.name)
                asset.save()
            except Exception:
                if asset.file:
                    asset.file.delete(save=False)
                raise
        return _finish_success(job, asset=asset,
            result_text=text if job.job_type in {AssetGenerationJob.JobType.PROMPT, AssetGenerationJob.JobType.FIELD} else "")


def _retain_text_asset_output(job, snapshot, *, text, diagnostics, collection_request_id=None, collection_body_kind="text"):
    """Return a stopped job, or None after retaining this original response."""
    from studio.services.execution_text_response_review import retain_asset_text_response
    from studio.services.execution_response_storage import ExecutionResponseStorageError
    try:
        retained = retain_asset_text_response(job=job, preparation_id=snapshot.preparation.pk,
            request_sha256=snapshot.preparation.request_sha256, text=text, diagnostics=diagnostics,
            collection_request_id=collection_request_id, collection_body_kind=collection_body_kind)
    except ExecutionResponseStorageError:
        return _fail(job, code="execution_response_storage_failed",
            message="模型已经返回，但原始正文未能保存；没有可读取的原文或候选，不会自动重试。",
            remote_status=job.remote_status if collection_request_id is not None else "response_storage_failed")
    if not retained:
        # Preserve this worker's claim: refreshing first could adopt a newer claim.
        if AssetGenerationJob.objects.filter(pk=job.pk, claim_token=job.claim_token,
                status=AssetGenerationJob.Status.CANCEL_REQUESTED).exists():
            return _finish_cancelled(job)
        job.refresh_from_db()
        return job
    return None


def _read_media_collect_receipt(job):
    from studio.models import ExecutionTextResponseReview
    from studio.services.execution_text_response_review import read_collect_receipt, read_retained_text_response
    retained = ExecutionTextResponseReview.objects.filter(asset_job=job).first()
    if retained is None:
        return None
    original, _, _, _ = read_retained_text_response(retained)
    return read_collect_receipt(original, request_id=job.provider_request_id)


def _retain_media_collect_receipt(job, snapshot, collected):
    from studio.services.execution_text_response_review import COLLECT_RECEIPT_KIND
    if not isinstance(collected, dict) or collected.get("request_id") != job.provider_request_id:
        raise ValidationError("媒体收集回执与原冻结请求不一致")
    try:
        receipt_text = json.dumps(collected, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("驱动收集回执不是可保存的 JSON，不会截断或伪造正文") from exc
    return _retain_text_asset_output(job, snapshot, text=receipt_text,
        diagnostics={"schema_version": 1, "contract_error_code": "media_collect_receipt_retained",
            "summary": "已保留驱动实际收集回执；输出校验及物化结果以原任务状态为准，不代表素材已生成。"},
        collection_request_id=collected["request_id"], collection_body_kind=COLLECT_RECEIPT_KIND)


def process_text_asset_job(job: AssetGenerationJob) -> AssetGenerationJob:
    from studio.services.execution_text_contracts import parse_execution_text_response
    from studio.services.execution_text_response_usage import record_asset_text_response_usage
    from studio.services.provider_registry import safe_chat_failure_details, safe_chat_failure_message
    if job.claim_token is None:
        raise ValidationError("素材文本任务没有 worker claim")
    try:
        snapshot = _snapshot(job)
        if snapshot.transport.value["driver_type"] not in _text_driver_types():
            raise ValidationError("文本worker只处理已冻结的聊天传输")
        current = AssetGenerationJob.objects.only(
            "status", "provider_request_id",
        ).get(pk=job.pk)
        if (
            current.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            and not current.provider_request_id
        ):
            return _finish_cancelled(job)
        # Original target only here; current ownership/resource/Smoke/budget
        # admission is shared with dispatch immediately before submission.
        deployment = _verify_live_target(job, snapshot, require_executable_route=False)
        permitted, job = begin_asset_execution_submission(job)
        if not permitted:
            return job
        try:
            from studio.services.chat_http_evidence import observe_chat_http_body, record_asset_chat_body
            with observe_chat_http_body(lambda facts: record_asset_chat_body(
                    job, snapshot.preparation.pk, snapshot.preparation.request_sha256, facts)):
                response = driver_for(deployment.provider).invoke(
                    deployment, "chat_completion", snapshot.payload,
                )
        except ProviderDriverError as exc:
            failure = safe_chat_failure_details(exc)
            usage = failure.get("usage")
            if usage and not exc.submission_uncertain:
                if not record_asset_text_response_usage(job, snapshot.preparation.pk, snapshot.preparation.request_sha256, usage):
                    job.refresh_from_db()
                    return job
            if (not exc.submission_uncertain and failure.get("contract_error_code") == "response_output_budget_exhausted"
                    and isinstance(getattr(exc, "partial_text", None), str)):
                stopped = _retain_text_asset_output(job, snapshot, text=exc.partial_text,
                    diagnostics={"schema_version": 1, **failure,
                        "summary": "服务返回因输出额度耗尽而中断；已保留实际收到的部分正文，不是可用候选，不会自动重试。"})
                if stopped is not None:
                    return stopped
            return _fail(
                job,
                code="uncertain_remote_response" if exc.submission_uncertain else exc.code,
                message=safe_chat_failure_message(exc),
                uncertain=bool(exc.submission_uncertain),
            )
        if not record_asset_text_response_usage(job, snapshot.preparation.pk, snapshot.preparation.request_sha256,
                response.get("usage") if isinstance(response, dict) else None):
            job.refresh_from_db()
            return job
        # Parse once, but retain the actual returned text even when it is not
        # a valid candidate. Do not put rejected output in result_text/Asset.
        validation_error = None
        try:
            parsed = parse_execution_text_response(job.operation, response, field_request=snapshot.field_request,
                operation_plan=snapshot.transport.value.get("operation_plan"), request_payload=snapshot.transport.value["payload"],
                expected_output=snapshot.definition.value["interface"].get("business_output"))
        except ValidationError as exc:
            validation_error = exc
        if isinstance(response, dict) and isinstance(response.get("text"), str):
            from studio.services.execution_text_response_review import response_validation_diagnostic
            diagnostics = response_validation_diagnostic(validation_error, response) if validation_error else {
                "schema_version": 1, "contract_error_code": "response_contract_valid",
                "summary": "已保存独立素材调用的原始返回；候选、素材与人工采用仍是独立事实。",
                "warnings": parsed.get("warnings", [])}
            stopped = _retain_text_asset_output(job, snapshot, text=response["text"], diagnostics=diagnostics)
            if stopped is not None:
                return stopped
        provider_request_id = response.get("request_id", "") if isinstance(response, dict) else ""
        if not isinstance(provider_request_id, str):
            return _fail(job, code="invalid_provider_request_id",
                message="文本模型返回的请求标识不是文本；无法核对原调用，禁止自动重试。", uncertain=True)
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
        if validation_error is not None:
            # The synchronous chat call returned. A rejected response is a known
            # result, not an unknown submission. Storage/finalization failures
            # below still follow the original uncertainty handling.
            return _fail(job, code="asset_text_response_invalid",
                message="模型已返回，但结果未通过本次合同校验，没有生成可用候选；有文本正文时可在任务详情查看原文与诊断。",
                remote_status="response_rejected")
        from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
        result_text = parsed["text"] if job.operation in NATIVE_TEXT_OPERATIONS else response["text"]
        return _finish_text_asset_result(job, result_text)
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
        config = _asset_request_header(locked)["runtime_budget"]
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
            prompt_hint=read_asset_prompt_hint(job),
            source_info=_asset_source_info(job),
            mime_type=staged_facts["mime_type"],
            checksum=staged_facts["sha256"],
            metadata=metadata,
        )
        from studio.services.media_extensions import verified_media_extension
        extension = verified_media_extension(staged_facts["mime_type"], job.media_type)
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


def _finish_media_asset_result(job, path, facts, *, actual_cost=None, currency=""):
    """Both transports share one atomic Asset/version completion and file guard."""
    asset = None
    try:
        with transaction.atomic():
            if not AssetGenerationJob.objects.filter(pk=job.pk, status=AssetGenerationJob.Status.CANCEL_REQUESTED).exists():
                asset = _materialize_downloaded_asset(job, path, facts)
            return _finish_success(job, asset=asset, actual_cost=actual_cost, currency=currency)
    except Exception:
        # This is only the newly materialized output, never input/shared media.
        # The database rollback alone cannot remove a file saved before failure.
        if asset is not None and asset.file:
            asset.file.delete(save=False)
        raise


def _asset_cancel_protocol(job):
    """Persist a single cancel attempt before I/O; recovery only polls it."""
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    with locked_preparation_owner_rows([("asset_job_id", job.pk)]) as owners:
        locked = owners[("asset_job_id", str(job.pk))]
        if locked.claim_token != job.claim_token or locked.status != AssetGenerationJob.Status.CANCEL_REQUESTED:
            return None, locked
        if locked.cancel_submitted_at is not None:
            return "poll", locked
        locked.cancel_submitted_at = timezone.now()
        locked.save(update_fields=("cancel_submitted_at", "updated_at"))
        return "cancel", locked


def process_media_asset_job(job: AssetGenerationJob) -> AssetGenerationJob:
    if job.claim_token is None:
        raise ValidationError("素材媒体任务没有 worker claim")
    collecting_known_success = False
    try:
        snapshot = _snapshot(job)
        current = AssetGenerationJob.objects.only(
            "status", "provider_request_id", "claim_token",
        ).get(pk=job.pk)
        if current.claim_token != job.claim_token or current.status not in {
                AssetGenerationJob.Status.QUEUED, AssetGenerationJob.Status.SUBMITTED,
                AssetGenerationJob.Status.RUNNING, AssetGenerationJob.Status.CANCEL_REQUESTED}:
            return current
        if (
            current.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            and not current.provider_request_id
        ):
            return _finish_cancelled(job)
        deployment = _verify_live_target(job, snapshot, require_executable_route=False)
        if snapshot.transport.value["driver_type"] != ProviderProfile.DriverType.ASYNC_REST:
            raise ValidationError("媒体worker只处理已冻结的受限异步传输")
        contract = {"request_schema": snapshot.definition.value["interface"]["request_schema"]}
        validate_standard_payload(contract, snapshot.payload)
        driver = driver_for(deployment.provider)
        if job.status == AssetGenerationJob.Status.QUEUED:
            permitted, job = begin_asset_execution_submission(job)
            if not permitted:
                return job
            try:
                response = driver.invoke(deployment, "submit", snapshot.payload)
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
            request_id = response.get("request_id") if isinstance(response, dict) else None
            if not isinstance(request_id, str) or not request_id.strip():
                return _fail(
                    job,
                    code="uncertain_remote_response",
                    message="远端提交缺少有效的文本 request_id；禁止自动重复 POST。",
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
            protocol, job = _asset_cancel_protocol(job)
            if protocol is None:
                return job
            response = driver.invoke(deployment, protocol, {"request_id": job.provider_request_id})
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

        collecting_known_success = True
        if (not job.provider_request_id or job.submitted_at is None or job.submission_started_at is None
                or job.provider_request_id_sha256 != hashlib.sha256(job.provider_request_id.encode()).hexdigest()):
            raise ValidationError("REST 收集缺少本次冻结请求的确切提交事实")
        _heartbeat_claim(job)
        collected = _read_media_collect_receipt(job)
        if collected is None:
            from studio.services.provider_registry import ProviderCollectResponseError
            collect_error = None
            try:
                collected = driver.invoke(deployment, "collect", {"request_id": job.provider_request_id})
            except ProviderCollectResponseError as exc:
                collected, collect_error = exc.collect_receipt, exc
            _heartbeat_claim(job)
            if not job.ignore_late_result:
                stopped = _retain_media_collect_receipt(job, snapshot, collected)
                if stopped is not None:
                    return stopped
            if collect_error is not None:
                raise collect_error
        _heartbeat_claim(job)
        artifacts = collected.get("artifacts") if isinstance(collected, dict) else None
        expected_kind = str(snapshot.output_kind)
        actual, actual_currency = _collected_actual_cost(job, collected)
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
            or not isinstance(artifacts[0], dict)
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
            result = _finish_media_asset_result(job, target, facts, actual_cost=actual, currency=actual_currency)
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
                f"远端已确认完成，但收集或本地处理失败，不会自动重试：{exc}"
                if collecting_known_success else (
                    f"远端请求已受理后处理失败，禁止直接重试：{exc}" if crossed else str(exc))
            ),
            uncertain=bool(exc.submission_uncertain or (crossed and not collecting_known_success)
                or exc.code in {"actual_cost_reconciliation_required", "actual_cost_currency_conflict"}),
        )
    except (ValidationError, ProvenanceError) as exc:
        crossed = _confirmed_remote_submission(job)
        return _fail(
            job,
            code="asset_media_job_invalid",
            message=(
                f"远端已确认完成，但本地校验失败，不会自动重试：{exc}"
                if collecting_known_success else (
                    f"远端请求已受理后本地校验失败，禁止直接重试：{exc}" if crossed else str(exc))
            ),
            uncertain=crossed and not collecting_known_success,
        )




def _prepared_comfy_asset_payload(job, snapshot, deployment, driver, payload):
    from studio.services.execution_workflow_staging import stage_execution_workflow_media, ExecutionWorkflowUploadClient
    if payload != snapshot.payload:
        raise ValidationError("素材提交参数必须来自本次冻结请求")
    def check_active():
        current = AssetGenerationJob.objects.only("status", "claim_token").get(pk=job.pk)
        if job.claim_token is None or current.claim_token != job.claim_token or current.status != AssetGenerationJob.Status.QUEUED:
            raise ValidationError("素材任务已取消、租约已变化或已经提交，不能继续上传")
        _heartbeat_claim(job)
    def persist(records):
        with transaction.atomic():
            current = AssetGenerationJob.objects.select_for_update().get(pk=job.pk)
            if job.claim_token is None or current.claim_token != job.claim_token or current.status != AssetGenerationJob.Status.QUEUED:
                raise ValidationError("上传期间素材任务已取消或租约已变化，不能写入回执")
            current.staged_inputs = records
            current.save(update_fields=("staged_inputs", "updated_at"))
        job.staged_inputs = deepcopy(records)
    result = stage_execution_workflow_media(definition=snapshot.definition, resolved_inputs=snapshot.resolved_inputs,
        transport=snapshot.transport, owner=job.owner, root_id=job.pk,
        client=ExecutionWorkflowUploadClient(driver, deployment), staged_inputs=job.staged_inputs,
        persist_staged_inputs=persist, check_active=check_active,
        max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes", minimum=1))
    return {**deepcopy(payload), "workflow": result.graph}


def process_comfy_asset_job(job: AssetGenerationJob) -> AssetGenerationJob:
    """Execute one frozen ComfyUI workflow route without any submit fallback."""

    if job.claim_token is None:
        raise ValidationError("ComfyUI 素材任务没有 worker claim")
    try:
        snapshot = _snapshot(job)
        if job.execution_kind != AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
            raise ValidationError("任务不是 ComfyUI 工作流执行方案")
        current = AssetGenerationJob.objects.only("status", "provider_request_id", "claim_token").get(pk=job.pk)
        if current.claim_token != job.claim_token or current.status not in {
                AssetGenerationJob.Status.QUEUED, AssetGenerationJob.Status.SUBMITTED,
                AssetGenerationJob.Status.RUNNING, AssetGenerationJob.Status.CANCEL_REQUESTED}:
            return current
        if (
            current.status == AssetGenerationJob.Status.CANCEL_REQUESTED
            and not current.provider_request_id
        ):
            return _finish_cancelled(job)
        deployment = _verify_live_target(
            job,
            snapshot,
            require_executable_route=False,
        )
        payload = snapshot.payload
        graph = payload.get("workflow") if isinstance(payload, dict) else None
        if (
            not isinstance(graph, dict)
            or json_contract(
                graph,
                max_bytes=_workflow_graph_max_bytes(),
            ).sha256 != snapshot.transport.value["payload"]["compiled_workflow"]["graph_sha256"]
            or str(payload.get("submission_token") or "") != str(snapshot.preparation.pk)
        ):
            raise ValidationError("ComfyUI 冻结提交图或 submission token 无效")
        driver = driver_for(deployment.provider)
        runtime_payload = deepcopy(payload)

        if job.status == AssetGenerationJob.Status.QUEUED:
            permitted, job = check_asset_execution_staging(job)
            if not permitted:
                return job
            runtime_payload = _prepared_comfy_asset_payload(
                job,
                snapshot,
                deployment,
                driver,
                payload,
            )
            permitted, job = begin_asset_execution_submission(job)
            if not permitted:
                return job
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
            if request_id != str(snapshot.preparation.pk):
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
            protocol, job = _asset_cancel_protocol(job)
            if protocol is None:
                return job
            response = driver.invoke(
                deployment,
                protocol,
                {"request_id": job.provider_request_id},
            )
            if protocol == "cancel":
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

        if (
                job.provider_request_id != str(snapshot.preparation.pk)
                or job.provider_request_id_sha256 != hashlib.sha256(str(snapshot.preparation.pk).encode()).hexdigest()
                or job.submitted_at is None or job.submission_started_at is None):
            raise ValidationError("工作流收集缺少本次冻结请求的确切提交事实")
        _heartbeat_claim(job)
        retained = _read_media_collect_receipt(job) if snapshot.output_kind != "text" else None
        if retained is None:
            collected = driver.invoke(deployment, "collect", {"request_id": job.provider_request_id})
        else:
            collected = retained
        _heartbeat_claim(job)
        if snapshot.output_kind != "text" and retained is None:
            stopped = _retain_media_collect_receipt(job, snapshot, collected)
            if stopped is not None:
                return stopped
        from studio.services.execution_workflow_outputs import verify_comfy_primary_output
        max_bytes = int(resolve_configuration(
            required_keys=("artifact.max_bytes",),
        ).values["artifact.max_bytes"])
        if snapshot.output_kind == "text":
            from studio.services.execution_workflow_outputs import select_comfy_primary_output
            from studio.services.execution_text_contracts import parse_execution_text_response
            from studio.services.execution_text_response_review import response_validation_diagnostic
            if (not isinstance(collected, dict) or collected.get("request_id") != str(snapshot.preparation.pk)
                    or job.provider_request_id != str(snapshot.preparation.pk)):
                raise ValidationError("工作流文本收集回执与原冻结请求不一致")
            item = select_comfy_primary_output(collected, output_contract=snapshot.output_contract, expected_kind="text")
            validation_error = None
            try:
                verify_comfy_primary_output(collected, output_contract=snapshot.output_contract,
                    expected_kind="text", expected_prefix=snapshot.output_prefix,
                    resolved_semantics=snapshot.transport.value["payload"]["compiled_workflow"]["resolved_semantics"], max_bytes=max_bytes)
            except ValidationError as exc:
                validation_error = exc
                diagnostics = {"schema_version": 1, "contract_error_code": "comfyui_text_output_invalid",
                    "summary": "已收到冻结节点的文本，但正文为空、大小或摘要未通过输出校验；只读留存，不作为成功候选。"}
            if validation_error is None:
                try:
                    parsed = parse_execution_text_response(job.operation, {"text": item["text"]}, field_request=snapshot.field_request,
                        operation_plan=snapshot.transport.value.get("operation_plan"), request_payload=snapshot.transport.value["payload"],
                        expected_output=snapshot.definition.value["interface"].get("business_output"))
                    diagnostics = {"schema_version": 1, "contract_error_code": "response_contract_valid",
                        "summary": "已保存工作流实际文本返回；候选与人工采用仍是独立事实。", "warnings": parsed.get("warnings", [])}
                except ValidationError as exc:
                    validation_error = exc
                    diagnostics = response_validation_diagnostic(exc, {})
            if isinstance(item.get("text"), str):
                stopped = _retain_text_asset_output(job, snapshot, text=item["text"], diagnostics=diagnostics,
                    collection_request_id=collected["request_id"])
                if stopped is not None:
                    return stopped
            if validation_error is not None:
                return _fail(job, code="comfyui_asset_text_response_invalid",
                    message="ComfyUI 远端已完成，但文本输出未通过本次合同校验；有明确节点正文时已保留，没有可用候选，不会自动重试。",
                    remote_status="succeeded")
            return _finish_text_asset_result(job, item["text"])
        source, facts = verify_comfy_primary_output(collected, output_contract=snapshot.output_contract,
            expected_kind=snapshot.output_kind, expected_prefix=snapshot.output_prefix,
            resolved_semantics=snapshot.transport.value["payload"]["compiled_workflow"]["resolved_semantics"], max_bytes=max_bytes)
        return _finish_media_asset_result(job, source, facts)
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


_TERMINAL_ASSET_STATUSES = (AssetGenerationJob.Status.SUCCEEDED, AssetGenerationJob.Status.FAILED, AssetGenerationJob.Status.CANCELLED)


@transaction.atomic
def _mark_asset_generation_cancel(job_id, *, child=False):
    from studio.services.execution_preparations import read_execution_preparation_detail
    job = AssetGenerationJob.objects.select_for_update().get(pk=job_id)
    if job.status == AssetGenerationJob.Status.CANCELLED:
        return job
    if job.status in _TERMINAL_ASSET_STATUSES or job.status == AssetGenerationJob.Status.UNCERTAIN:
        if not child:
            raise ValidationError("当前任务状态不能取消；远端不确定任务须先消歧")
        # Never rewrite an uncertain child's actual remote outcome.
        if job.status == AssetGenerationJob.Status.UNCERTAIN and not job.ignore_late_result:
            job.ignore_late_result = True
            job.cancel_requested_at = job.cancel_requested_at or timezone.now()
            job.save(update_fields=("ignore_late_result", "cancel_requested_at", "updated_at"))
        return job
    preparation = job.execution_preparations.select_for_update().get()
    read_execution_preparation_detail(preparation)
    if preparation.status != "cancelled":
        preparation.status, preparation.reason_code, preparation.reason = "cancelled", "user_cancelled", "用户已取消本次请求"
        preparation.revision += 1
        preparation.next_check_at = None
        preparation.save(update_fields=("status", "revision", "reason_code", "reason", "next_check_at", "updated_at"))
    pending_children = preparation.auxiliary_asset_jobs.exclude(status__in=_TERMINAL_ASSET_STATUSES).exists()
    job.ignore_late_result = True
    job.cancel_requested_at = job.cancel_requested_at or timezone.now()
    if job.submission_started_at is None and job.claim_token is None and not pending_children:
        job.status, job.finished_at, job.next_poll_at = AssetGenerationJob.Status.CANCELLED, timezone.now(), None
        job.error_code, job.error_message = "user_cancelled", "用户取消了本次请求，未提交主调用"
    else:
        job.status, job.next_poll_at = AssetGenerationJob.Status.CANCEL_REQUESTED, timezone.now()
    if pending_children and job.submission_started_at is None:
        job.error_code, job.error_message = "awaiting_auxiliary_cancellation", "正在等待辅助任务取消或远端状态消歧；不会再提交主请求"
    job.save(update_fields=("status", "finished_at", "cancel_requested_at", "ignore_late_result",
        "next_poll_at", "error_code", "error_message", "updated_at"))
    return job


def request_asset_generation_cancel(job: AssetGenerationJob) -> AssetGenerationJob:
    # Stop the parent first, then lock each child in a separate transaction.
    # No parent-root lock may be held while taking child-root locks.
    job = _mark_asset_generation_cancel(job.pk)
    if job.status == AssetGenerationJob.Status.CANCELLED:
        return job
    preparation = job.execution_preparations.get()
    child_ids = list(preparation.auxiliary_asset_jobs.exclude(status__in=_TERMINAL_ASSET_STATUSES)
        .order_by("id").values_list("id", flat=True))
    for child_id in child_ids:
        _mark_asset_generation_cancel(child_id, child=True)
    # In-flight children still need the original remote cancellation/reconcile.
    return _mark_asset_generation_cancel(job.pk)


def retry_asset_generation_job(job: AssetGenerationJob, *, client_idempotency_key: str = "") -> tuple[AssetGenerationJob, bool]:
    from studio.services.execution_asset_retries import retry_prepared_asset_job
    return retry_prepared_asset_job(job, client_idempotency_key=client_idempotency_key)


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
        queryset = queryset.filter(model_deployment__profile_version__provider__driver_type__in=_text_driver_types())
    elif (
        text is False
        and execution_kind != AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW
    ):
        queryset = queryset.filter(model_deployment__profile_version__provider__driver_type=ProviderProfile.DriverType.ASYNC_REST)
    now = timezone.now()
    for job in queryset:
        if (
            _is_text_job(job)
            and job.status == AssetGenerationJob.Status.SUBMITTING
            and job.submission_started_at is not None
        ):
            timeout_seconds = float(
                _asset_request_header(job)["runtime_budget"]["timeout_seconds"]
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
            payload = snapshot.payload
            token = str(payload.get("submission_token") or "") if isinstance(payload, dict) else ""
            if token != str(snapshot.preparation.pk):
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
    resolution = job.resolutions.filter(
        outcome__in=AssetGenerationJobResolution.OPERATOR_OUTCOMES).order_by("-revision").first()
    if resolution is None:
        return None, ""
    evidence = verify_json_contract(resolution.evidence,
        schema_version=resolution.evidence_schema_version, size_bytes=resolution.evidence_size_bytes,
        sha256=resolution.evidence_sha256, max_bytes=_stage_snapshot_max_bytes()).value
    if not isinstance(evidence, dict) or evidence.get("job_id") != str(job.pk):
        raise ValidationError("费用消歧证据与当前素材任务不一致")
    source = evidence.get("actual_cost_source")
    amount = _decimal_amount(evidence.get("actual_cost"), field="actual_cost")
    if source == "unknown" and amount is None:
        return None, ""
    if source not in {"operator_attested", "carried_forward", "operator_confirmed_not_submitted"} or amount is None:
        raise ValidationError("费用消歧证据没有有效的实际费用来源")
    currency = evidence.get("cost_currency")
    # Confirmed non-submission is an explicit zero, not a monetary receipt.
    if source == "operator_confirmed_not_submitted" and amount == 0 and currency == "":
        return amount, ""
    return amount, _actual_cost_currency(currency)


def _collected_actual_cost(job: AssetGenerationJob, collected: dict) -> tuple[Decimal | None, str]:
    """Use only provider billing or verified operator evidence, never prices."""
    if not isinstance(collected, dict):
        raise ValidationError("远端 collect 响应无效")
    attested_cost, attested_currency = _latest_attested_resolution_cost(job)
    raw = collected.get("actual_cost")
    if raw is None:
        actual, currency = attested_cost, attested_currency
    else:
        actual = _decimal_amount(raw, field="actual_cost")
        if actual is None:
            raise ValidationError("远端实际费用为空字符串，不能视为有效费用")
        currency = _actual_cost_currency(collected.get("currency"))
        if attested_cost is not None and (actual != attested_cost or currency != attested_currency):
            raise ProviderDriverError(
                "远端 collect 费用与管理员消歧证据冲突，需重新人工对账",
                code="actual_cost_reconciliation_required",
            )
    if actual is not None and job.cost_currency and currency != job.cost_currency:
        raise ProviderDriverError(
            "素材生成实际费用币种与冻结的预估币种冲突，需人工对账",
            code="actual_cost_currency_conflict",
        )
    return actual, currency


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
    if outcome not in AssetGenerationJobResolution.OPERATOR_OUTCOMES:
        raise ValidationError({"outcome": "素材生成消歧结果无效"})
    supplied_request_id = str(provider_request_id or "").strip()
    if len(supplied_request_id) > 191:
        raise ValidationError({"provider_request_id": "provider request id 过长"})
    parsed_cost = _decimal_amount(actual_cost, field="actual_cost")
    supplied_currency = str(cost_currency or "").strip().upper()
    if supplied_currency or parsed_cost is not None:
        supplied_currency = _actual_cost_currency(supplied_currency)
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

    from studio.services.execution_preparation_owners import locked_preparation_owners
    # The enclosing transaction.atomic retains these root locks after this
    # nested guard, including the parent's budget lock for an auxiliary.
    with locked_preparation_owners([("asset_job_id", job.pk)], user) as owners:
        locked = owners[("asset_job_id", str(job.pk))]
    existing_resolution = locked.resolutions.filter(
        idempotency_key=idempotency_key,
    ).first()
    if existing_resolution is not None:
        if existing_resolution.outcome not in AssetGenerationJobResolution.OPERATOR_OUTCOMES:
            raise ValidationError("系统事件幂等键不能用于人工消歧")
        if existing_resolution.request_sha256 != request_intent.sha256:
            raise ValidationError("同一消歧幂等键不能用于不同请求")
        return existing_resolution, False
    if locked.status != AssetGenerationJob.Status.UNCERTAIN:
        raise ValidationError("只有 uncertain 素材生成任务可以人工消歧")

    prepared = _snapshot(locked)
    if locked.execution_kind == AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW:
        frozen_comfy_token = str(prepared.preparation.pk)
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
    elif not _is_text_job(locked) and not request_id_hash:
        raise ValidationError({
            "provider_request_id": "媒体远端结论必须关联 provider request id 摘要",
        })

    prior_cost, prior_currency = _latest_attested_resolution_cost(locked)
    effective_cost = parsed_cost
    actual_cost_source = "operator_attested" if parsed_cost is not None else "unknown"
    if effective_cost is None and prior_cost is not None:
        effective_cost = prior_cost
        actual_cost_source = "carried_forward"
    if outcome == AssetGenerationJobResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
        # This zero is an explicit administrator fact, not an unknown-price
        # fallback; release the possibly-spent reservation exactly once.
        effective_cost = Decimal("0")
        actual_cost_source = "operator_confirmed_not_submitted"
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
        "execution_instance_version_id": str(locked.execution_instance_version_id),
        "preparation_id": str(prepared.preparation.pk),
        "prepared_request_sha256": prepared.preparation.request_sha256,
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
    elif _is_text_job(locked):
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
