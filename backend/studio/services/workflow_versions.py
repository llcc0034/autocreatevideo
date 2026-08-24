from __future__ import annotations

import hashlib
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    Asset,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    WorkflowModelRequirement,
    WorkflowSemanticBinding,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.comfyui_contract import ComfyUIContractError, validate_comfyui_api_contract
from studio.services.configuration import (
    resolve_configuration,
    validate_version_configuration_defaults,
)
from studio.services.json_contracts import canonical_json_bytes, json_contract, verify_json_contract
from studio.services.provider_registry import (
    ProviderDriverError,
    credential_binding_sha256,
    driver_for,
)
from studio.services.runtime_platform import verified_artifact_availability
from studio.services.provenance import classify_task_output, sha256_file
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.workflow import WorkflowBuildError, validate_comfy_graph
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    compile_frozen_workflow,
    compiler_contract_from_input,
    workflow_execution_contract,
)
from studio.services.workflow_semantics import (
    VALUE_TYPES,
    WorkflowSemanticError,
    canonical_binding_rows,
    canonical_requirement_rows,
    expected_value_type,
    validate_semantic_key,
)


_MODEL_FOLDER_BY_ROLE = {
    ModelArtifact.Role.CHECKPOINT: "checkpoints",
    ModelArtifact.Role.DIFFUSION_MODEL: "diffusion_models",
    ModelArtifact.Role.TEXT_ENCODER: "text_encoders",
    ModelArtifact.Role.VAE: "vae",
    ModelArtifact.Role.LORA: "loras",
}


def _evidence_digest(value: dict[str, Any]) -> str:
    return json_contract(value).sha256


def _execution_target_evidence(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
) -> dict[str, Any]:
    provider = deployment.profile_version.provider
    artifacts = []
    for requirement in version.model_requirements.select_related("artifact").order_by("role", "alias", "id"):
        artifact = requirement.artifact
        artifacts.append({
            "requirement_id": str(requirement.id),
            "required": requirement.is_required,
            "expected_filename": requirement.expected_filename,
            "expected_sha256": requirement.expected_sha256,
            "artifact_id": str(artifact.id) if artifact else None,
            "artifact_status": artifact.status if artifact else None,
            "artifact_filename": artifact.filename if artifact else None,
            "artifact_sha256": artifact.sha256 if artifact else None,
        })
    return {
        "schema_version": 1,
        "deployment_id": str(deployment.id),
        "deployment_status": deployment.status,
        "deployment_runtime_config_sha256": deployment.runtime_config_sha256,
        "credential_binding_sha256": credential_binding_sha256(deployment),
        "provider_id": str(provider.id),
        "provider_active": provider.is_active,
        "provider_driver_type": provider.driver_type,
        "provider_driver_config_sha256": provider.driver_config_sha256,
        "model_profile_version_id": str(deployment.profile_version_id),
        "model_profile_status": deployment.profile_version.status,
        "model_identifier": deployment.profile_version.model_identifier,
        "template_output_kind": version.template.output_kind,
        "required_artifacts": artifacts,
    }


def _execution_target_sha256(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
) -> str:
    return _evidence_digest(_execution_target_evidence(version, deployment))


def _validate_model_inventory(version, deployment, driver) -> dict[str, Any]:
    if version.model_profile_version_id is None:
        raise ValidationError("workflow version 未绑定 model profile version")
    if deployment.profile_version_id != version.model_profile_version_id:
        raise ValidationError("deployment model profile 与 workflow 精确绑定不一致")
    checked = []
    folder_cache: dict[str, set[str]] = {}
    host_snapshot = None
    for requirement in version.model_requirements.select_related("artifact").all():
        artifact = requirement.artifact
        if artifact is None:
            if requirement.is_required:
                raise ValidationError({"model_requirements": f"{requirement.alias} needs_review: 未绑定 artifact"})
            continue
        if artifact.profile_version_id != version.model_profile_version_id:
            raise ValidationError({"model_requirements": f"{requirement.alias} artifact 不属于绑定模型版本"})
        if artifact.filename != requirement.expected_filename:
            raise ValidationError({"model_requirements": f"{requirement.alias} 文件名与 artifact 不一致"})
        if requirement.is_required:
            expected_sha = str(requirement.expected_sha256 or "").strip()
            artifact_sha = str(artifact.sha256 or "").strip()
            if artifact.status != ModelArtifact.Status.AVAILABLE:
                raise ValidationError({"model_requirements": f"{requirement.alias} needs_review: artifact 未验证可用"})
            if not expected_sha or not artifact_sha or expected_sha != artifact_sha:
                raise ValidationError({"model_requirements": f"{requirement.alias} needs_review: 缺少一致的受信 hash"})
            if deployment.runtime_host_id is None:
                raise ValidationError({
                    "model_requirements": f"{requirement.alias} needs_review: deployment 未绑定受管运行主机",
                })
            if host_snapshot is None:
                host_snapshot = HostCapabilitySnapshot.objects.filter(
                    host_id=deployment.runtime_host_id,
                ).order_by("-revision").first()
            if host_snapshot is None:
                raise ValidationError({
                    "model_requirements": f"{requirement.alias} needs_review: 缺少主机 artifact 观测快照",
                })
            try:
                verified_artifact_availability(
                    snapshot=host_snapshot,
                    deployment=deployment,
                    artifact=artifact,
                )
            except ValidationError as exc:
                raise ValidationError({
                    "model_requirements": (
                        f"{requirement.alias} needs_review: 缺少一致的新鲜 size/hash 主机证据"
                    ),
                }) from exc
        folder = _MODEL_FOLDER_BY_ROLE.get(requirement.role)
        if folder is None:
            if requirement.is_required:
                raise ValidationError({"model_requirements": f"{requirement.alias} role 无受控库存探测器"})
            continue
        if folder not in folder_cache:
            response = driver.invoke(deployment, "models", {"folder": folder})
            items = response.get("items") if isinstance(response, dict) else None
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise ValidationError({"model_requirements": f"目标运行端没有返回 {folder} 库存"})
            folder_cache[folder] = set(items)
        if requirement.expected_filename not in folder_cache[folder]:
            raise ValidationError({"model_requirements": f"目标运行端缺少 {requirement.expected_filename}"})
        checked.append({
            "requirement_id": str(requirement.id),
            "artifact_id": str(artifact.id),
            "filename": requirement.expected_filename,
            "sha256": artifact.sha256,
            "folder": folder,
        })
    return {"schema_version": 1, "checked_requirements": checked}


def _binding_rows(version: WorkflowTemplateVersion) -> list[dict[str, Any]]:
    return canonical_binding_rows([{
        "semantic_key": row.semantic_key,
        "node_id": row.node_id,
        "input_name": row.input_name,
        "value_type": row.value_type,
        "is_required": row.is_required,
    } for row in version.semantic_bindings.all()])


def _requirement_rows(version: WorkflowTemplateVersion) -> list[dict[str, Any]]:
    return canonical_requirement_rows([{
        "role": row.role,
        "alias": row.alias,
        "artifact_id": str(row.artifact_id) if row.artifact_id else None,
        "expected_filename": row.expected_filename,
        "expected_sha256": row.expected_sha256,
        "is_required": row.is_required,
    } for row in version.model_requirements.all()])


def workflow_version_execution_snapshot(version: WorkflowTemplateVersion) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "workflow_version_id": str(version.id),
        "workflow_version": version.version,
        "template": {
            "id": str(version.template_id),
            "slug": version.template.slug,
            "name": version.template.name,
            "output_kind": version.template.output_kind,
        },
        "model_identifier": (
            version.model_profile_version.model_identifier
            if version.model_profile_version_id else ""
        ),
        "graph": version.graph,
        "graph_schema_version": version.graph_schema_version,
        "graph_size_bytes": version.graph_size_bytes,
        "graph_sha256": version.graph_sha256,
        "input_contract": version.input_contract,
        "input_contract_schema_version": version.input_contract_schema_version,
        "input_contract_size_bytes": version.input_contract_size_bytes,
        "input_contract_sha256": version.input_contract_sha256,
        "compiler_id": version.compiler_id,
        "compiler_version": version.compiler_version,
        "semantic_bindings": _binding_rows(version),
        "semantic_bindings_sha256": version.semantic_bindings_sha256,
        "model_requirements": _requirement_rows(version),
        "model_requirements_sha256": version.model_requirements_sha256,
        "execution_contract_sha256": version.execution_contract_sha256,
    }


def _smoke_h3_mode(contract: dict[str, Any]) -> str:
    values = contract.get("smoke_inputs") if isinstance(contract, dict) else None
    explicit = str((values or {}).get("H3_MODE") or "").strip()
    modes = contract.get("h3_modes") if isinstance(contract, dict) else None
    declared = [str(item).strip() for item in modes] if isinstance(modes, list) else []
    if explicit:
        if declared and explicit not in declared:
            raise ValidationError({
                "input_contract": "smoke_inputs.H3_MODE 不在 input_contract.h3_modes 中",
            })
        return explicit
    if len(declared) == 1:
        return declared[0]
    return "T2VA"


def _materialize_smoke_graph(version: WorkflowTemplateVersion):
    contract = version.input_contract if isinstance(version.input_contract, dict) else {}
    values = contract.get("smoke_inputs")
    if not isinstance(values, dict) or not values:
        raise ValidationError("input_contract 缺少受控 smoke_inputs；只能保持 experimental")
    smoke_h3_mode = _smoke_h3_mode(contract)
    prompt = SimpleNamespace(
        id=version.id,
        prompt_text=str(values.get("prompt.positive") or values.get("PROMPT") or "smoke test"),
        negative_prompt=str(values.get("prompt.negative") or values.get("NEGATIVE_PROMPT") or ""),
        total_duration_ms=int(values.get("video.duration_ms") or values.get("DURATION_MS") or 5000),
        include_audio=bool(values.get("audio.enabled", values.get("AUDIO_ENABLED", False))),
        prompt_type=version.template.output_kind,
        h3_mode=smoke_h3_mode,
        model=(
            version.model_profile_version.model_identifier
            if version.model_profile_version_id else ""
        ),
        structured_payload={
            "reference_bindings": contract.get("smoke_asset_bindings") or [],
            "prompt_template_key": values.get("PROMPT_TEMPLATE_KEY", ""),
            "effective_conditioning_mode": smoke_h3_mode,
        },
        project=SimpleNamespace(owner_id=None),
    )
    project = SimpleNamespace(
        id=version.id,
        owner_id=None,
        width=int(values.get("image.width") or values.get("WIDTH") or 256),
        height=int(values.get("image.height") or values.get("HEIGHT") or 256),
        fps=int(values.get("video.fps") or values.get("FPS") or 24),
        continuity_seed=int(values.get("sampling.seed") or values.get("SEED") or 1),
        aspect_ratio="16:9",
    )
    try:
        parameters = contract.get("parameters") if isinstance(contract, dict) else {}
        generic_overrides = {
            key: value
            for key, value in values.items()
            if (
                isinstance(parameters, dict)
                and key in parameters
                and not key.startswith("asset.")
                and not key.startswith("model.")
            )
        }
        generic_overrides["output.prefix"] = f"autoprompt/smoke/{version.id}"
        compiled = compile_frozen_workflow(
            workflow_snapshot=workflow_version_execution_snapshot(version),
            prompt=prompt,
            project=project,
            overrides=generic_overrides
            if version.compiler_id == "generic.comfy"
            else {**values, "OUTPUT_PREFIX": f"autoprompt/smoke/{version.id}"},
        )
    except (WorkflowCompilerError, WorkflowSemanticError) as exc:
        raise ValidationError({"input_contract": str(exc)}) from exc
    return compiled


def _validate_smoke_contract_preflight(version: WorkflowTemplateVersion) -> None:
    """Validate managed Ref2VA smoke inputs before any provider network I/O."""

    contract = version.input_contract if isinstance(version.input_contract, dict) else {}
    if _smoke_h3_mode(contract) != "Ref2VA":
        return
    bindings = contract.get("smoke_asset_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise ValidationError({
            "input_contract": "Ref2VA live validate 需要非空 smoke_asset_bindings",
        })
    asset_root = Path(settings.ASSET_LIBRARY_ROOT).resolve()
    expected_ids: list[str] = []
    for index, binding in enumerate(bindings, start=1):
        if not isinstance(binding, dict):
            raise ValidationError({
                "input_contract": f"Ref2VA smoke_asset_bindings[{index}] 必须是对象",
            })
        asset_id = str(binding.get("asset_id") or binding.get("id") or "").strip()
        category = str(binding.get("category") or "").strip()
        checksum = str(binding.get("checksum") or "").casefold()
        if (
            not asset_id
            or not category
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)
        ):
            raise ValidationError({
                "input_contract": (
                    f"Ref2VA smoke_asset_bindings[{index}] 缺少合法 asset/category/checksum"
                ),
            })
        try:
            asset = Asset.objects.get(pk=asset_id, owner__isnull=True, is_active=True)
        except (Asset.DoesNotExist, ValueError) as exc:
            raise ValidationError({
                "input_contract": f"Ref2VA smoke asset {asset_id} 不是启用的全局受管资产",
            }) from exc
        if asset.file or not asset.local_path:
            raise ValidationError({
                "input_contract": "Ref2VA smoke asset 必须来自只读素材库 local_path",
            })
        source = Path(asset.local_path).resolve()
        if (
            not source.is_relative_to(asset_root)
            or not source.is_file()
            or str(asset.checksum or "").casefold() != checksum
            or sha256_file(source) != checksum
            or not str(asset.mime_type or "").casefold().startswith(
                f"{'video' if category == 'video' else 'audio' if category == 'audio' else 'image'}/"
            )
        ):
            raise ValidationError({
                "input_contract": "Ref2VA smoke asset 路径、类型或 checksum 无效",
            })
        expected_ids.append(str(asset.id))
    compiled = _materialize_smoke_graph(version)
    compiled_ids = [str(item.get("asset_id") or "") for item in compiled.asset_bindings]
    if not compiled_ids or compiled_ids != expected_ids:
        raise ValidationError({
            "input_contract": "Ref2VA smoke reference bindings 未进入冻结执行合同",
        })


def _stage_smoke_assets(*, compiled, version, deployment, driver) -> tuple[dict[str, Any], set[str]]:
    graph = compiled.graph
    if not compiled.asset_bindings:
        return graph, set()
    operations = set((driver.capabilities() or {}).get("operations") or [])
    if "upload" not in operations:
        raise ValidationError("i2v/media smoke-test 需要 provider upload 契约")
    asset_root = Path(settings.ASSET_LIBRARY_ROOT).resolve()
    replacements: dict[str, str] = {}
    uploaded_paths: set[str] = set()
    for binding in compiled.asset_bindings:
        try:
            asset = Asset.objects.get(pk=binding["asset_id"], is_active=True, owner__isnull=True)
        except (Asset.DoesNotExist, ValueError) as exc:
            raise ValidationError("smoke asset 必须是启用的全局受管资产") from exc
        if not asset.local_path or asset.file:
            raise ValidationError("smoke asset 必须来自只读素材库 local_path")
        source = Path(asset.local_path).resolve()
        if not source.is_relative_to(asset_root) or not source.is_file():
            raise ValidationError("smoke asset 路径越过受控素材库或文件不存在")
        digest = sha256_file(source)
        if digest != str(binding.get("checksum") or "").casefold() or digest != str(asset.checksum or "").casefold():
            raise ValidationError("smoke asset checksum 与冻结 manifest 不一致")
        uploaded = driver.invoke(deployment, "upload", {
            "source_path": str(source),
            "subfolder": f"autoprompt/smoke/{version.id}",
            "filename": f"{asset.id}{source.suffix.lower() or '.bin'}",
        })
        name = str(uploaded.get("name") or "") if isinstance(uploaded, dict) else ""
        subfolder = str(uploaded.get("subfolder") or "") if isinstance(uploaded, dict) else ""
        relative_path = "/".join(filter(None, (subfolder, name)))
        if not name or not relative_path:
            raise ValidationError("provider upload 未返回可执行相对路径")
        replacements[str(binding["token"])] = relative_path
        uploaded_paths.add(relative_path)

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str) and value.startswith("__AUTOPROMPT_ASSET__:"):
            if value not in replacements:
                raise ValidationError("compiled smoke graph 含未授权 asset token")
            return replacements[value]
        return value

    return replace(graph), uploaded_paths


def _validate_semantic_bindings(
    graph: dict,
    input_contract: dict,
    bindings: list[dict[str, Any]],
    *,
    strict: bool,
) -> None:
    seen: set[tuple[str, str]] = set()
    bound_keys: set[str] = set()
    parameters = input_contract.get("parameters") if isinstance(input_contract, dict) else None
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValidationError({"semantic_bindings": f"第 {index + 1} 项必须是对象"})
        semantic_key = str(binding.get("semantic_key") or "").strip()
        node_id = str(binding.get("node_id") or "").strip()
        input_name = str(binding.get("input_name") or "").strip()
        target = (node_id, input_name)
        if not semantic_key or target in seen:
            raise ValidationError({"semantic_bindings": "semantic_key 必须非空，node/input 目标必须唯一"})
        node = graph.get(node_id)
        if not isinstance(node, dict):
            raise ValidationError({"semantic_bindings": f"节点 {node_id} 不存在"})
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or input_name not in inputs:
            raise ValidationError({"semantic_bindings": f"节点 {node_id} 不包含输入 {input_name}"})
        current = inputs[input_name]
        if (
            isinstance(current, list) and len(current) == 2
            and isinstance(current[0], str) and isinstance(current[1], int)
        ):
            raise ValidationError({"semantic_bindings": "generic binding 禁止覆盖节点连接"})
        if strict:
            try:
                validate_semantic_key(semantic_key)
            except WorkflowSemanticError as exc:
                raise ValidationError({"semantic_bindings": str(exc)}) from exc
            value_type = str(binding.get("value_type") or "string")
            if value_type not in VALUE_TYPES:
                raise ValidationError({"semantic_bindings": f"{semantic_key} value_type 无效"})
            definition = parameters.get(semantic_key) if isinstance(parameters, dict) else None
            if not isinstance(definition, dict) or definition.get("type") != value_type:
                raise ValidationError({"semantic_bindings": f"{semantic_key} 缺少一致的参数 Schema"})
            expected = expected_value_type(semantic_key)
            if expected and value_type != expected and not (
                semantic_key == "video.fps" and value_type == "integer"
            ):
                raise ValidationError({"semantic_bindings": f"{semantic_key} 类型必须为 {expected}"})
        seen.add(target)
        bound_keys.add(semantic_key)
    if strict and isinstance(parameters, dict):
        missing_required = sorted(
            key for key, definition in parameters.items()
            if isinstance(definition, dict)
            and bool(definition.get("required", True))
            and key not in bound_keys
        )
        if missing_required:
            raise ValidationError({
                "semantic_bindings": (
                    "required parameter 缺少实际 node/input binding: "
                    + ", ".join(missing_required)
                ),
            })
    if strict:
        resolution_keys = {"image.width", "image.height"}
        missing_resolution = sorted(resolution_keys - bound_keys)
        if missing_resolution:
            raise ValidationError({
                "semantic_bindings": (
                    "generic.comfy 必须同时绑定 image.width 与 image.height；缺少: "
                    + ", ".join(missing_resolution)
                ),
            })


def _validate_requirements(requirements: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    roles = set(ModelArtifact.Role.values)
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            raise ValidationError({"model_requirements": f"第 {index + 1} 项必须是对象"})
        role = str(requirement.get("role") or "")
        alias = str(requirement.get("alias") or "").strip()
        filename = str(requirement.get("expected_filename") or "").strip()
        if role not in roles or not alias or not filename:
            raise ValidationError({"model_requirements": "role/alias/expected_filename 无效"})
        if (role, alias) in seen:
            raise ValidationError({"model_requirements": "role+alias 必须唯一"})
        seen.add((role, alias))


def _validate_model_binding_coverage(
    input_contract: dict[str, Any],
    bindings: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> None:
    required_aliases = {
        str(row["alias"])
        for row in requirements
        if bool(row.get("is_required", True))
    }
    parameters = input_contract.get("parameters") or {}
    bound_aliases = set()
    for row in bindings:
        key = str(row.get("semantic_key") or "")
        if not key.startswith("model.") or not key.endswith(".filename"):
            continue
        definition = parameters.get(key) if isinstance(parameters, dict) else None
        fallback_alias = key[len("model."):-len(".filename")]
        bound_aliases.add(str((definition or {}).get("requirement_alias") or fallback_alias))
    missing = sorted(required_aliases - bound_aliases)
    if missing:
        raise ValidationError({
            "model_requirements": "required requirement 缺少 model semantic binding: " + ", ".join(missing),
        })


@transaction.atomic
def import_workflow_version(
    *,
    template: WorkflowTemplate,
    graph: dict,
    input_contract: dict,
    semantic_bindings: list[dict[str, Any]],
    model_requirements: list[dict[str, Any]],
    provider: ProviderProfile | None,
    model_profile_version: ModelProfileVersion | None,
) -> WorkflowTemplateVersion:
    if not isinstance(graph, dict) or not graph:
        raise ValidationError({"graph": "ComfyUI API graph 必须是非空对象"})
    if not isinstance(input_contract, dict):
        raise ValidationError({"input_contract": "必须是对象"})
    configuration = resolve_configuration(required_keys=("workflow.max_graph_bytes",))
    graph_limit = int(configuration.values["workflow.max_graph_bytes"])
    graph_contract = json_contract(graph, max_bytes=min(graph_limit, 64 * 1024))
    input_contract_meta = json_contract(input_contract)
    try:
        validate_comfy_graph(graph)
    except WorkflowBuildError as exc:
        raise ValidationError({"graph": str(exc)}) from exc
    try:
        compiler_id, compiler_version = compiler_contract_from_input(input_contract)
    except WorkflowCompilerError:
        compiler_id, compiler_version = "", 0
    _validate_semantic_bindings(
        graph,
        input_contract,
        semantic_bindings,
        strict=compiler_id == "generic.comfy",
    )
    _validate_requirements(model_requirements)
    if compiler_id == "generic.comfy":
        _validate_model_binding_coverage(input_contract, semantic_bindings, model_requirements)

    WorkflowTemplate.objects.select_for_update().get(pk=template.pk)
    next_version = (
        WorkflowTemplateVersion.objects.filter(template=template)
        .aggregate(value=Max("version"))["value"]
        or 0
    ) + 1
    binding_rows = canonical_binding_rows(semantic_bindings)
    requirement_rows = canonical_requirement_rows([
        {
            **item,
            "artifact_id": str(item.get("artifact_id")) if item.get("artifact_id") else None,
        }
        for item in model_requirements
    ])
    _, execution_digest = workflow_execution_contract(
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        graph_sha256=graph_contract.sha256,
        input_contract_sha256=input_contract_meta.sha256,
        bindings=binding_rows,
        requirements=requirement_rows,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template,
        version=next_version,
        provider=provider,
        model_profile_version=model_profile_version,
        graph=graph,
        graph_schema_version=graph_contract.schema_version,
        graph_size_bytes=graph_contract.size_bytes,
        graph_sha256=graph_contract.sha256,
        input_contract=input_contract,
        input_contract_schema_version=input_contract_meta.schema_version,
        input_contract_size_bytes=input_contract_meta.size_bytes,
        input_contract_sha256=input_contract_meta.sha256,
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        semantic_bindings_sha256=json_contract(binding_rows).sha256,
        model_requirements_sha256=json_contract(requirement_rows).sha256,
        execution_contract_sha256=execution_digest,
        status=WorkflowTemplateVersion.Status.DRAFT,
        validation_message=(
            "结构校验已通过；尚未连接运行节点校验。"
            if compiler_id
            else "needs_review: input_contract 缺少已注册 compiler，禁止 validate/publish。"
        ),
    )
    WorkflowSemanticBinding.objects.bulk_create([
        WorkflowSemanticBinding(
            workflow_version=version,
            semantic_key=str(item["semantic_key"]).strip(),
            node_id=str(item["node_id"]).strip(),
            input_name=str(item["input_name"]).strip(),
            value_type=str(item.get("value_type") or "string")[:32],
            is_required=bool(item.get("is_required", True)),
        )
        for item in semantic_bindings
    ])
    artifact_ids = {
        str(item.get("artifact_id"))
        for item in model_requirements
        if item.get("artifact_id")
    }
    artifacts = {
        str(item.id): item
        for item in ModelArtifact.objects.filter(pk__in=artifact_ids)
    }
    if artifact_ids - set(artifacts):
        raise ValidationError({"model_requirements": "存在无效 artifact_id"})
    if model_profile_version is not None:
        foreign = sorted(
            item.logical_name
            for item in artifacts.values()
            if item.profile_version_id != model_profile_version.id
        )
        if foreign:
            raise ValidationError({
                "model_requirements": "artifact 必须属于 workflow 绑定的 model profile: "
                + ", ".join(foreign),
            })
    WorkflowModelRequirement.objects.bulk_create([
        WorkflowModelRequirement(
            workflow_version=version,
            role=str(item["role"]),
            alias=str(item["alias"]).strip(),
            artifact=artifacts.get(str(item.get("artifact_id"))),
            expected_filename=str(item["expected_filename"]).strip(),
            expected_sha256=str(item.get("expected_sha256") or "").strip(),
            is_required=bool(item.get("is_required", True)),
        )
        for item in model_requirements
    ])
    return version


def verify_workflow_version_integrity(version: WorkflowTemplateVersion) -> None:
    configuration = resolve_configuration(required_keys=("workflow.max_graph_bytes",))
    graph_limit = int(configuration.values["workflow.max_graph_bytes"])
    verify_json_contract(
        version.graph,
        schema_version=version.graph_schema_version,
        size_bytes=version.graph_size_bytes,
        sha256=version.graph_sha256,
        max_bytes=min(graph_limit, 64 * 1024),
    )
    verify_json_contract(
        version.input_contract,
        schema_version=version.input_contract_schema_version,
        size_bytes=version.input_contract_size_bytes,
        sha256=version.input_contract_sha256,
    )
    try:
        validate_comfy_graph(version.graph)
    except WorkflowBuildError as exc:
        raise ValidationError({"graph": str(exc)}) from exc
    try:
        compiler_id, compiler_version = compiler_contract_from_input(version.input_contract)
    except WorkflowCompilerError as exc:
        raise ValidationError({"input_contract": str(exc)}) from exc
    if (compiler_id, compiler_version) != (version.compiler_id, version.compiler_version):
        raise ValidationError("workflow compiler 字段与 input_contract 不一致")
    bindings = _binding_rows(version)
    requirements = _requirement_rows(version)
    _validate_semantic_bindings(
        version.graph,
        version.input_contract,
        bindings,
        strict=compiler_id == "generic.comfy",
    )
    _validate_requirements(requirements)
    if compiler_id == "generic.comfy":
        _validate_model_binding_coverage(version.input_contract, bindings, requirements)
    payload, digest = workflow_execution_contract(
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        graph_sha256=version.graph_sha256,
        input_contract_sha256=version.input_contract_sha256,
        bindings=bindings,
        requirements=requirements,
    )
    if payload["semantic_bindings_sha256"] != version.semantic_bindings_sha256:
        raise ValidationError("semantic bindings 已在证据生成后漂移")
    if payload["model_requirements_sha256"] != version.model_requirements_sha256:
        raise ValidationError("model requirements 已在证据生成后漂移")
    if digest != version.execution_contract_sha256:
        raise ValidationError("workflow execution contract hash 不一致")


def resolve_workflow_canvas_export_deployment(
    version: WorkflowTemplateVersion,
    *,
    requested_deployment_id=None,
) -> tuple[WorkflowTemplateVersion, ModelDeployment, str]:
    """Resolve the only deployment whose node catalog may shape a UI export.

    Validated lifecycle rows are pinned to their persisted evidence. A draft
    without evidence must name an active ComfyUI deployment explicitly; there
    is intentionally no process-global/default deployment fallback.
    """

    version = WorkflowTemplateVersion.objects.select_related(
        "template", "provider", "model_profile_version",
        "validated_deployment__profile_version__provider",
    ).prefetch_related("model_requirements__artifact").get(pk=version.pk)
    requested_id = str(requested_deployment_id or "").strip()
    evidence_bound = version.status in {
        WorkflowTemplateVersion.Status.VALIDATED,
        WorkflowTemplateVersion.Status.PUBLISHED,
        WorkflowTemplateVersion.Status.RETIRED,
    }

    if evidence_bound:
        if not version.validated_deployment_id:
            raise ValidationError({
                "deployment_id": "validated/published workflow 缺少持久化 validated deployment；禁止回退默认 ComfyUI",
            })
        if requested_id and requested_id != str(version.validated_deployment_id):
            raise ValidationError({
                "deployment_id": "该 workflow 已绑定 validated deployment，不能改用其他 ComfyUI 部署导出",
            })
        deployment = version.validated_deployment
        deployment_source = "validated"
    else:
        if version.status != WorkflowTemplateVersion.Status.DRAFT:
            raise ValidationError({"deployment_id": "当前 workflow 状态不能导出 ComfyUI 画布"})
        if version.validated_deployment_id:
            raise ValidationError({
                "deployment_id": "draft workflow 残留 validated deployment，状态与证据不一致；请重新校验",
            })
        if not requested_id:
            raise ValidationError({
                "deployment_id": "未校验草稿必须显式选择 active ComfyUI deployment；禁止回退系统默认值",
            })
        deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).filter(pk=requested_id).first()
        if deployment is None:
            raise ValidationError({"deployment_id": "所选 ModelDeployment 不存在"})
        deployment_source = "explicit_draft"

    provider = deployment.profile_version.provider
    if deployment.status != ModelDeployment.Status.ACTIVE:
        raise ValidationError({"deployment_id": "所选 ModelDeployment 已停用或异常"})
    if not provider.is_active:
        raise ValidationError({"deployment_id": "所选 ModelDeployment 的 Provider 已停用"})
    if deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED:
        raise ValidationError({"deployment_id": "所选 ModelDeployment 的模型版本不是 published"})
    if provider.driver_type != ProviderProfile.DriverType.COMFYUI:
        raise ValidationError({"deployment_id": "所选 ModelDeployment 不是 ComfyUI 驱动"})
    if version.provider_id and version.provider_id != provider.id:
        raise ValidationError({"deployment_id": "deployment provider 与 workflow 绑定已漂移"})
    if (
        version.model_profile_version_id
        and version.model_profile_version_id != deployment.profile_version_id
    ):
        raise ValidationError({"deployment_id": "deployment model profile 与 workflow 精确绑定已漂移"})

    verify_workflow_version_integrity(version)
    if evidence_bound:
        if (
            version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED
            or not version.provider_id
            or not version.model_profile_version_id
        ):
            raise ValidationError({
                "deployment_id": "validated/published workflow 的绑定或通过状态不完整",
            })
        evidence = version.validation_evidence
        if (
            not isinstance(evidence, dict)
            or _evidence_digest(evidence) != version.validation_evidence_sha256
            or str(evidence.get("deployment_id") or "") != str(deployment.id)
            or evidence.get("execution_target_sha256")
            != _execution_target_sha256(version, deployment)
        ):
            raise ValidationError({
                "deployment_id": "validated deployment/provider/model/artifact 配置或证据已漂移",
            })

    return version, deployment, deployment_source


def validate_workflow_version(
    version: WorkflowTemplateVersion,
    *,
    deployment: ModelDeployment,
) -> tuple[WorkflowTemplateVersion, dict[str, Any]]:
    # Invalidate previous evidence before doing network I/O.  This first,
    # committed transaction prevents a concurrent publish from reusing stale
    # PASSED evidence while a re-validation is in flight.
    with transaction.atomic():
        deployment = ModelDeployment.objects.select_for_update().select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        version = WorkflowTemplateVersion.objects.select_for_update().select_related(
            "provider", "template", "model_profile_version",
        ).prefetch_related("model_requirements__artifact").get(pk=version.pk)
        if version.status in {
            WorkflowTemplateVersion.Status.PUBLISHED,
            WorkflowTemplateVersion.Status.RETIRED,
        }:
            raise ValidationError("已发布/停用版本不可重新写入校验状态，请导入新版本")
        if not version.provider_id:
            raise ValidationError("workflow version 未绑定 provider")
        if deployment.profile_version.provider_id != version.provider_id:
            raise ValidationError("deployment provider 与 workflow provider 不匹配")
        if deployment.profile_version_id != version.model_profile_version_id:
            raise ValidationError("deployment model profile 与 workflow 精确绑定不一致")
        if (
            deployment.status != ModelDeployment.Status.ACTIVE
            or not deployment.profile_version.provider.is_active
            or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
        ):
            raise ValidationError("deployment/provider/model profile 当前不可用于验证")
        verify_workflow_version_integrity(version)
        expected_graph_sha256 = version.graph_sha256
        expected_execution_sha256 = version.execution_contract_sha256
        expected_target_sha256 = _execution_target_sha256(version, deployment)
        version.status = WorkflowTemplateVersion.Status.DRAFT
        version.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
        version.validation_message = "live validation 执行中。"
        version.validated_deployment = None
        version.validation_evidence = {}
        version.validation_evidence_sha256 = ""
        version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
        version.smoke_test_message = "live validation 更新后必须重新执行真实 smoke-test。"
        version.smoke_test_evidence = {}
        version.smoke_test_evidence_sha256 = ""
        version.save(update_fields=(
            "status", "validation_state", "validation_message", "validated_deployment",
            "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
            "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
            "updated_at",
        ))
    driver = driver_for(version.provider)
    try:
        _validate_smoke_contract_preflight(version)
        object_info = driver.invoke(deployment, "object_info", {})
        report = validate_comfyui_api_contract(version.graph, object_info)
        inventory = _validate_model_inventory(version, deployment, driver)
    except Exception as exc:
        with transaction.atomic():
            failed = WorkflowTemplateVersion.objects.select_for_update().get(pk=version.pk)
            failed.status = WorkflowTemplateVersion.Status.DRAFT
            failed.validation_state = WorkflowTemplateVersion.ValidationState.FAILED
            failed.validation_message = str(exc)[:2000]
            failed.validated_deployment = None
            failed.validation_evidence = {}
            failed.validation_evidence_sha256 = ""
            failed.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            failed.smoke_test_message = "live validation 失败，smoke-test 证据已失效。"
            failed.smoke_test_evidence = {}
            failed.smoke_test_evidence_sha256 = ""
            failed.save(update_fields=(
                "status", "validation_state", "validation_message", "validated_deployment",
                "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
                "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
                "updated_at",
            ))
        raise ValidationError({"graph": str(exc)}) from exc
    validation_drifted = False
    with transaction.atomic():
        version = WorkflowTemplateVersion.objects.select_for_update().select_related(
            "template",
        ).get(pk=version.pk)
        current_deployment = ModelDeployment.objects.select_for_update().select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        if (
            version.graph_sha256 != expected_graph_sha256
            or version.execution_contract_sha256 != expected_execution_sha256
            or _execution_target_sha256(version, current_deployment) != expected_target_sha256
        ):
            validation_drifted = True
            version.status = WorkflowTemplateVersion.Status.DRAFT
            version.validation_state = WorkflowTemplateVersion.ValidationState.FAILED
            version.validation_message = "workflow 在 live validation 期间已变更，请重新校验"
            version.validated_deployment = None
            version.validation_evidence = {}
            version.validation_evidence_sha256 = ""
        else:
            version.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
            version.status = WorkflowTemplateVersion.Status.VALIDATED
            version.validation_message = "已通过连接节点的 ComfyUI object_info 契约校验。"
            version.validated_deployment = deployment
            version.validation_evidence = {
                "schema_version": 1,
                "deployment_id": str(deployment.id),
                "model_profile_version_id": str(deployment.profile_version_id),
                "graph_sha256": version.graph_sha256,
                "execution_contract_sha256": version.execution_contract_sha256,
                "contract": report.as_dict(),
                "inventory": inventory,
                "execution_target": _execution_target_evidence(version, current_deployment),
                "execution_target_sha256": expected_target_sha256,
            }
            version.validation_evidence_sha256 = _evidence_digest(version.validation_evidence)
        version.save(update_fields=(
            "validation_state", "status", "validation_message", "validated_deployment",
            "validation_evidence", "validation_evidence_sha256", "updated_at",
        ))
    if validation_drifted:
        raise ValidationError("workflow 在 live validation 期间已变更，请重新校验")
    return version, report.as_dict()


def smoke_test_workflow_version(
    version: WorkflowTemplateVersion,
    *,
    deployment: ModelDeployment,
    submission_token: str | None = None,
    submitted_before: bool = False,
    on_submission_started=None,
    on_submitted=None,
    poll_control=None,
) -> tuple[WorkflowTemplateVersion, dict[str, Any]]:
    preflight_target_drifted = False
    with transaction.atomic():
        deployment = ModelDeployment.objects.select_for_update().select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        version = WorkflowTemplateVersion.objects.select_for_update().select_related(
            "provider", "template", "model_profile_version", "validated_deployment",
        ).get(pk=version.pk)
        if version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED:
            raise ValidationError("必须先通过 live validate")
        if not version.provider_id:
            raise ValidationError("workflow version 未绑定 provider")
        if deployment.profile_version_id != version.model_profile_version_id:
            raise ValidationError("deployment model profile 与 workflow 精确绑定不一致")
        if version.validated_deployment_id != deployment.id:
            raise ValidationError("smoke-test 必须使用通过 live validation 的同一 deployment")
        verify_workflow_version_integrity(version)
        expected_target_sha256 = _execution_target_sha256(version, deployment)
        if (
            version.validation_evidence.get("execution_target_sha256")
            != expected_target_sha256
        ):
            preflight_target_drifted = True
            version.status = WorkflowTemplateVersion.Status.DRAFT
            version.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            version.validation_message = "validated deployment/provider/model/artifact 配置已漂移"
            version.validated_deployment = None
            version.validation_evidence = {}
            version.validation_evidence_sha256 = ""
            version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            version.smoke_test_message = "live validation 证据漂移，smoke-test 证据已失效。"
            version.smoke_test_evidence = {}
            version.smoke_test_evidence_sha256 = ""
            version.save(update_fields=(
                "status", "validation_state", "validation_message", "validated_deployment",
                "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
                "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
                "updated_at",
            ))
        expected_graph_sha256 = version.graph_sha256
        expected_execution_sha256 = version.execution_contract_sha256
        expected_validation_sha256 = version.validation_evidence_sha256
        if not preflight_target_drifted:
            version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            version.smoke_test_message = "真实 smoke-test 执行中。"
            version.smoke_test_evidence = {}
            version.smoke_test_evidence_sha256 = ""
            version.save(update_fields=(
                "smoke_test_state", "smoke_test_message", "smoke_test_evidence",
                "smoke_test_evidence_sha256", "updated_at",
            ))
    if preflight_target_drifted:
        raise ValidationError("validated deployment/provider/model/artifact 配置已漂移")
    driver = driver_for(version.provider)
    request_id = ""
    try:
        operations = set((driver.capabilities() or {}).get("operations") or [])
        required_operations = {"submit", "poll", "cancel", "collect"}
        if not required_operations <= operations:
            raise ValidationError(
                "provider driver 缺少真实 smoke-test submit/poll/cancel/collect 契约；版本保持 experimental",
            )
        configuration = resolve_configuration(required_keys=(
            "workflow.smoke_hard_timeout_seconds", "artifact.max_bytes",
        ))
        timeout = int(configuration.values["workflow.smoke_hard_timeout_seconds"])
        artifact_max_bytes = int(configuration.values["artifact.max_bytes"])
        health = driver.invoke(deployment, "health", {})
        object_info = driver.invoke(deployment, "object_info", {})
        compiled = _materialize_smoke_graph(version)
        graph, uploaded_paths = _stage_smoke_assets(
            compiled=compiled,
            version=version,
            deployment=deployment,
            driver=driver,
        )
        report = validate_comfyui_api_contract(
            graph,
            object_info,
            uploaded_paths=uploaded_paths,
        )
        request_id = str(submission_token or "") if submitted_before else ""
        if not request_id:
            if callable(on_submission_started):
                on_submission_started()
            submitted = driver.invoke(deployment, "submit", {
                "workflow": graph,
                "client_id": f"autoprompt-smoke-{version.id}",
                "submission_token": str(submission_token or ""),
            })
            request_id = str(submitted.get("request_id") or "") if isinstance(submitted, dict) else ""
            if callable(on_submitted):
                on_submitted(request_id)
        if not request_id:
            raise ProviderDriverError("smoke submit 未返回 request_id", code="provider_response_invalid")
        deadline = time.monotonic() + timeout
        while True:
            poll = driver.invoke(deployment, "poll", {"request_id": request_id})
            remote_status = str(poll.get("status") or "") if isinstance(poll, dict) else ""
            control = poll_control(remote_status) if callable(poll_control) else ""
            if control:
                try:
                    driver.invoke(deployment, "cancel", {"request_id": request_id})
                except Exception:
                    pass
                raise ProviderDriverError(
                    f"真实 smoke-test {control} 并已请求取消",
                    code=f"provider_smoke_{control}",
                )
            if remote_status == "succeeded":
                break
            if remote_status == "failed":
                raise ProviderDriverError("真实 smoke-test 远端执行失败", code="provider_smoke_failed")
            if time.monotonic() >= deadline:
                try:
                    driver.invoke(deployment, "cancel", {"request_id": request_id})
                except Exception:
                    pass
                raise ProviderDriverError("真实 smoke-test 超时并已请求取消", code="provider_smoke_timeout")
            time.sleep(0.25)
        collected = driver.invoke(deployment, "collect", {"request_id": request_id})
        outputs = collected.get("outputs") if isinstance(collected, dict) else None
        if not isinstance(outputs, list) or not outputs:
            raise ProviderDriverError("真实 smoke-test 没有输出", code="provider_smoke_output_missing")
        primary_output = compiled.output_contract.get("primary")
        if not isinstance(primary_output, dict):
            raise ProviderDriverError(
                "真实 smoke-test 缺少冻结 primary output 契约",
                code="provider_smoke_output_invalid",
            )
        expected_kind = str(primary_output.get("kind") or "")
        expected_node_id = str(primary_output.get("node_id") or "")
        matching = [
            item for item in outputs
            if classify_task_output(item) == expected_kind
            and str(item.get("node_id") or "") == expected_node_id
        ]
        if len(matching) != 1:
            raise ProviderDriverError(
                "真实 smoke-test 的声明 primary node 必须且只能产生一个匹配输出",
                code="provider_smoke_output_invalid",
            )
        output_root_raw = str(getattr(settings, "COMFYUI_OUTPUT_DIR", "") or "").strip()
        if not output_root_raw:
            raise ProviderDriverError(
                "production smoke-test 需要可访问的 ComfyUI 输出目录",
                code="provider_smoke_output_unavailable",
            )
        output_root = Path(output_root_raw).resolve()
        verified_outputs = []
        for item in matching:
            filename = PurePosixPath(str(item.get("filename") or "").replace("\\", "/")).name
            subfolder = str(item.get("subfolder") or "").replace("\\", "/")
            candidate = (output_root / subfolder / filename).resolve()
            if not filename or not candidate.is_relative_to(output_root):
                raise ProviderDriverError(
                    "smoke-test 输出路径越过受控目录",
                    code="provider_smoke_output_invalid",
                )
            try:
                facts = verified_local_media_facts(
                    candidate,
                    expected_kind=expected_kind,
                    max_bytes=artifact_max_bytes,
                )
            except Exception as exc:
                raise ProviderDriverError(
                    "smoke-test 输出真实字节校验失败",
                    code="provider_smoke_output_invalid",
                ) from exc
            verified_outputs.append(facts)
    except Exception as exc:
        with transaction.atomic():
            failed = WorkflowTemplateVersion.objects.select_for_update().get(pk=version.pk)
            failed.status = WorkflowTemplateVersion.Status.DRAFT
            failed.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            failed.validation_message = "真实 smoke-test 失败，live validation 证据已失效。"
            failed.validated_deployment = None
            failed.validation_evidence = {}
            failed.validation_evidence_sha256 = ""
            failed.smoke_test_state = WorkflowTemplateVersion.ValidationState.FAILED
            failed.smoke_test_message = str(exc)[:2000]
            failed.smoke_test_evidence = {}
            failed.smoke_test_evidence_sha256 = ""
            failed.save(update_fields=(
                "status", "validation_state", "validation_message", "validated_deployment",
                "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
                "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
                "updated_at",
            ))
        raise ValidationError({"detail": str(exc)}) from exc
    smoke_drifted = False
    with transaction.atomic():
        version = WorkflowTemplateVersion.objects.select_for_update().select_related(
            "template",
        ).get(pk=version.pk)
        current_deployment = ModelDeployment.objects.select_for_update().select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        if (
            version.graph_sha256 != expected_graph_sha256
            or version.execution_contract_sha256 != expected_execution_sha256
            or version.validation_evidence_sha256 != expected_validation_sha256
            or version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED
            or _execution_target_sha256(version, current_deployment) != expected_target_sha256
        ):
            smoke_drifted = True
            version.status = WorkflowTemplateVersion.Status.DRAFT
            version.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            version.validation_message = "workflow 在 smoke-test 期间已变更，请重新校验"
            version.validated_deployment = None
            version.validation_evidence = {}
            version.validation_evidence_sha256 = ""
            version.smoke_test_state = WorkflowTemplateVersion.ValidationState.FAILED
            version.smoke_test_message = "workflow 在 smoke-test 期间已变更，请重新校验"
            version.smoke_test_evidence = {}
            version.smoke_test_evidence_sha256 = ""
        else:
            version.smoke_test_state = WorkflowTemplateVersion.ValidationState.PASSED
            version.smoke_test_message = "真实受限执行已提交、完成并收集到声明类型输出。"
            version.smoke_test_evidence = {
                "schema_version": 1,
                "execution_submitted": True,
                "request_id_sha256": json_contract(request_id).sha256,
                "deployment_id": str(deployment.id),
                "graph_sha256": version.graph_sha256,
                "execution_contract_sha256": version.execution_contract_sha256,
                "output_kind": expected_kind,
                "output_count": len(matching),
                "outputs": verified_outputs,
                "contract_schema_sha256": report.schema_sha256,
                "execution_target": _execution_target_evidence(version, current_deployment),
                "execution_target_sha256": expected_target_sha256,
            }
            version.smoke_test_evidence_sha256 = _evidence_digest(version.smoke_test_evidence)
        version.save(update_fields=(
            "status", "validation_state", "validation_message", "validated_deployment",
            "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
            "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
            "updated_at",
        ))
    if smoke_drifted:
        raise ValidationError("workflow 在 smoke-test 期间已变更，请重新校验")
    return version, {
        "schema_version": 1,
        "execution_submitted": True,
        "health_version": (
            (health.get("system") or {}).get("comfyui_version")
            if isinstance(health, dict) else ""
        ),
        "contract": report.as_dict(),
    }


@transaction.atomic
def _publish_workflow_version_atomic(
    version: WorkflowTemplateVersion,
    *,
    published_by,
) -> WorkflowTemplateVersion:
    version = WorkflowTemplateVersion.objects.select_for_update().select_related(
        "template", "provider", "model_profile_version",
    ).get(pk=version.pk)
    if version.status != WorkflowTemplateVersion.Status.VALIDATED:
        raise ValidationError("只有 validated 版本可以发布")
    if version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED:
        raise ValidationError("live validate 未通过")
    if version.smoke_test_state != WorkflowTemplateVersion.ValidationState.PASSED:
        raise ValidationError("smoke-test 未通过")
    if not version.validated_deployment_id:
        raise ValidationError("缺少 validated deployment 证据")
    if _evidence_digest(version.validation_evidence) != version.validation_evidence_sha256:
        raise ValidationError("validation evidence 校验和不一致")
    if _evidence_digest(version.smoke_test_evidence) != version.smoke_test_evidence_sha256:
        raise ValidationError("smoke evidence 校验和不一致")
    if version.smoke_test_evidence.get("execution_submitted") is not True:
        raise ValidationError("contract-only 检查不能发布 production workflow")
    if version.validation_evidence.get("execution_contract_sha256") != version.execution_contract_sha256:
        raise ValidationError("live validation 证据未绑定当前 execution contract")
    if version.smoke_test_evidence.get("execution_contract_sha256") != version.execution_contract_sha256:
        raise ValidationError("smoke-test 证据未绑定当前 execution contract")
    deployment = version.validated_deployment
    if (
        deployment.status != ModelDeployment.Status.ACTIVE
        or deployment.profile_version_id != version.model_profile_version_id
        or not deployment.profile_version.provider.is_active
        or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
    ):
        raise ValidationError("validated deployment 已停用或模型绑定发生变化")
    validate_version_configuration_defaults(workflow_version=version)
    current_target_sha256 = _execution_target_sha256(version, deployment)
    if (
        version.validation_evidence.get("execution_target_sha256") != current_target_sha256
        or version.smoke_test_evidence.get("execution_target_sha256") != current_target_sha256
    ):
        raise ValidationError("validated deployment/provider/model/artifact 配置已漂移")
    verify_workflow_version_integrity(version)
    WorkflowTemplateVersion.objects.filter(
        template=version.template,
        is_current=True,
    ).exclude(pk=version.pk).update(is_current=False, updated_at=timezone.now())
    version.status = WorkflowTemplateVersion.Status.PUBLISHED
    version.is_current = True
    version.published_at = timezone.now()
    version.published_by = published_by
    version.save(update_fields=(
        "status", "is_current", "published_at", "published_by", "updated_at",
    ))
    return version


def publish_workflow_version(
    version: WorkflowTemplateVersion,
    *,
    published_by,
) -> WorkflowTemplateVersion:
    target_drifted = False
    with transaction.atomic():
        locked = WorkflowTemplateVersion.objects.select_for_update().select_related(
            "template", "validated_deployment__profile_version__provider",
        ).get(pk=version.pk)
        deployment = locked.validated_deployment
        if deployment is not None and (
            locked.validation_evidence.get("execution_target_sha256")
            or locked.smoke_test_evidence.get("execution_target_sha256")
        ):
            current = _execution_target_sha256(locked, deployment)
            target_drifted = (
                locked.validation_evidence.get("execution_target_sha256") != current
                or locked.smoke_test_evidence.get("execution_target_sha256") != current
            )
        if target_drifted:
            locked.status = WorkflowTemplateVersion.Status.DRAFT
            locked.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            locked.validation_message = "validated deployment/provider/model/artifact 配置已漂移"
            locked.validated_deployment = None
            locked.validation_evidence = {}
            locked.validation_evidence_sha256 = ""
            locked.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            locked.smoke_test_message = "发布前目标配置漂移，smoke-test 证据已失效。"
            locked.smoke_test_evidence = {}
            locked.smoke_test_evidence_sha256 = ""
            locked.save(update_fields=(
                "status", "validation_state", "validation_message", "validated_deployment",
                "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
                "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
                "updated_at",
            ))
    if target_drifted:
        raise ValidationError("validated deployment/provider/model/artifact 配置已漂移")
    return _publish_workflow_version_atomic(version, published_by=published_by)


def node_metadata(
    *,
    deployment: ModelDeployment,
    search: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValidationError({"offset": "offset 必须是非负整数"})
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValidationError({"limit": "limit 必须是 1 到 200 的整数"})
    provider = deployment.profile_version.provider
    driver = driver_for(provider)
    raw = driver.invoke(deployment, "object_info", {})
    if not isinstance(raw, dict):
        raise ValidationError("object_info 响应无效")
    query = search.strip().casefold()
    items = []
    for class_type in sorted(raw):
        definition = raw[class_type]
        if query and query not in class_type.casefold():
            continue
        if not isinstance(definition, dict):
            continue
        inputs = definition.get("input") if isinstance(definition.get("input"), dict) else {}
        required = inputs.get("required") if isinstance(inputs.get("required"), dict) else {}
        optional = inputs.get("optional") if isinstance(inputs.get("optional"), dict) else {}
        items.append({
            "class_type": class_type,
            "display_name": str(definition.get("display_name") or class_type),
            "category": str(definition.get("category") or ""),
            "required_inputs": sorted(required)[:100],
            "optional_inputs": sorted(optional)[:100],
            "output_count": len(definition.get("output") or []),
        })
    total_count = len(items)
    page = items[offset:offset + limit]
    consumed = offset + len(page)
    return {
        "schema_version": 2,
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "next_offset": consumed if consumed < total_count else None,
        "catalog_sha256": hashlib.sha256(canonical_json_bytes(items)).hexdigest(),
        "items": page,
    }
