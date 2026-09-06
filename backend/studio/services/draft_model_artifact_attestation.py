from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath
import logging
import re
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import (
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    RuntimeHostProbeAttempt,
)
from studio.services.comfyui_artifacts import (
    artifact_inventory_spec,
    safe_model_relative_filename,
)
from studio.services.comfyui_canvas import (
    ComfyUICanvasImportError,
    ui_workflow_to_api_graph,
)
from studio.services.comfyui_workflow_imports import (
    SavedWorkflowImportError,
    SavedWorkflowSecretField,
    _assert_safe_canvas_preflight,
    _assert_conversion_expectations,
    _assert_expected_sha,
    _assert_expected_source_scope,
    _invoke_with_budget,
    _model_dependency_candidates,
    _read_source,
    _RequestBudget,
    _source_driver,
    _safe_projected_error,
    saved_workflow_batch_max_seconds,
    saved_workflow_request_max_bytes,
    source_deployment_fingerprint,
)
from studio.services.json_contracts import (
    assign_json_contract,
    json_contract,
    verify_json_contract,
)
from studio.services.provider_registry import comfyui_workflow_source_scope
from studio.services.runtime_platform import (
    _deployment_is_loopback,
    _host_probe_is_local,
    draft_model_profile_fingerprint,
)
from studio.services.security_boundaries import boundary_int


ACTIVE_ATTEMPT_STATUSES = (
    RuntimeHostProbeAttempt.Status.QUEUED,
    RuntimeHostProbeAttempt.Status.RUNNING,
    RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED,
)
_SAFE_LOGICAL = re.compile(r"[^a-z0-9._-]+")
logger = logging.getLogger(__name__)


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


class DraftArtifactAttestationConflict(ValidationError):
    code = "draft_artifact_attestation_conflict"


def _safe_filename(value: str) -> str:
    normalized = safe_model_relative_filename(value)
    if normalized is None:
        raise ValidationError("模型文件名必须是受控 models 目录内的安全相对路径")
    return normalized


def _logical_name(role: str, filename: str) -> str:
    stem = PurePosixPath(filename).stem.casefold()
    safe = _SAFE_LOGICAL.sub("-", stem).strip("-._") or "artifact"
    suffix = sha256(f"{role}\0{filename}".encode("utf-8")).hexdigest()[:12]
    return f"saved-{safe[:88]}-{suffix}"[:120]


def _dependency_specs(graph: dict, object_info: dict) -> list[dict[str, Any]]:
    rows = _model_dependency_candidates(
        graph,
        object_info,
        model_profile_version=None,
    )
    unique: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        filename = _safe_filename(row["filename"])
        if not row.get("available_in_object_info"):
            raise ValidationError(
                f"实时 ComfyUI 库存未确认模型文件 {filename!r}；不能生成 size/hash 证明",
            )
        key = (
            str(row["role"]),
            filename,
            str(row["inventory_folder"]),
            str(row["inventory_node_type"]),
            str(row["inventory_input_name"]),
        )
        unique[key] = {
            "role": key[0],
            "filename": key[1],
            "inventory_folder": key[2],
            "inventory_node_type": key[3],
            "inventory_input_name": key[4],
        }
    if not unique:
        raise ValidationError("所选主输出的可达图没有需要证明的模型文件")
    return [unique[key] for key in sorted(unique)]


def _assert_source_and_target(
    source: ModelDeployment,
    target: ModelProfileVersion,
) -> None:
    if (
        source.status != ModelDeployment.Status.ACTIVE
        or source.profile_version.provider.driver_type != ProviderProfile.DriverType.COMFYUI
        or not source.profile_version.provider.is_active
        or source.runtime_host_id is None
        or source.runtime_host.status != RuntimeHost.Status.ACTIVE
        or not _deployment_is_loopback(source)
        or not _host_probe_is_local(source.runtime_host)
    ):
        raise ValidationError(
            "模型文件证明只允许绑定 active 本机 loopback ComfyUI deployment 与 active runtime host",
        )
    if (
        target.status != ModelProfileVersion.Status.DRAFT
        or target.provider_id != source.profile_version.provider_id
        or target.provider.driver_type != ProviderProfile.DriverType.COMFYUI
    ):
        raise ValidationError("目标必须是 source provider 下的 draft ComfyUI model profile")


def _intent_payload(
    *,
    source: ModelDeployment,
    target: ModelProfileVersion,
    document,
    primary_output_node_ids: list[int | str],
    object_info_sha256: str,
    converted_graph_sha256: str,
    dependencies: list[dict[str, Any]],
) -> dict[str, Any]:
    source_scope = comfyui_workflow_source_scope(source)
    return {
        "schema_version": 1,
        "probe_kind": RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION,
        "host_id": str(source.runtime_host_id),
        "source_deployment_id": str(source.id),
        "source_deployment_fingerprint_sha256": source_deployment_fingerprint(source),
        "source_scope_schema_version": source_scope.schema_version,
        "source_scope_sha256": source_scope.sha256,
        "source_path": document.source_path,
        "source_sha256": document.source_sha256,
        "object_info_schema_sha256": object_info_sha256,
        "converted_graph_sha256": converted_graph_sha256,
        "primary_output_node_ids": [str(value) for value in primary_output_node_ids],
        "target_model_profile_version_id": str(target.id),
        "target_profile_fingerprint_sha256": draft_model_profile_fingerprint(target),
        "dependencies": dependencies,
    }


@transaction.atomic
def _create_or_reuse_attempt(
    *,
    source: ModelDeployment,
    target: ModelProfileVersion,
    intent: dict[str, Any],
    actor,
) -> tuple[RuntimeHostProbeAttempt, bool]:
    # Keep the same lock order as the worker commit path:
    # host -> active attempt -> deployment -> profile -> artifacts.
    locked_host = RuntimeHost.objects.select_for_update().get(pk=source.runtime_host_id)
    existing = RuntimeHostProbeAttempt.objects.select_for_update().filter(
        host=locked_host,
        status__in=ACTIVE_ATTEMPT_STATUSES,
    ).order_by("created_at").first()
    locked_source = ModelDeployment.objects.select_for_update().select_related(
        "profile_version__provider", "runtime_host",
    ).get(pk=source.pk)
    if locked_source.runtime_host_id != locked_host.id:
        raise ValidationError("读取 workflow 后 source deployment runtime host 已变化")
    locked_target = ModelProfileVersion.objects.select_for_update().select_related(
        "provider",
    ).get(pk=target.pk)
    _assert_source_and_target(locked_source, locked_target)
    if (
        intent["source_deployment_fingerprint_sha256"]
        != source_deployment_fingerprint(locked_source)
    ):
        raise ValidationError("读取 workflow 后 source deployment 已变化，请重新预览")
    dependency_artifacts: list[ModelArtifact] = []
    for dependency in intent["dependencies"]:
        matches = list(ModelArtifact.objects.select_for_update().filter(
            profile_version=locked_target,
            role=dependency["role"],
            filename=dependency["filename"],
        ))
        if len(matches) > 1:
            raise ValidationError("draft profile 中存在重复 role/filename artifact")
        if matches:
            artifact = matches[0]
        else:
            artifact = ModelArtifact.objects.create(
                profile_version=locked_target,
                role=dependency["role"],
                logical_name=_logical_name(dependency["role"], dependency["filename"]),
                filename=dependency["filename"],
                inventory_folder=dependency["inventory_folder"],
                inventory_node_type=dependency["inventory_node_type"],
                inventory_input_name=dependency["inventory_input_name"],
                status=ModelArtifact.Status.DECLARED,
            )
        if artifact.status in {ModelArtifact.Status.MISSING, ModelArtifact.Status.INVALID}:
            raise ValidationError("既有 artifact 状态不可用于重新证明；请修正 draft 配置")
        try:
            inventory_spec = artifact_inventory_spec(artifact)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if (
            inventory_spec.folder != dependency["inventory_folder"]
            or inventory_spec.node_type != dependency["inventory_node_type"]
            or inventory_spec.input_name != dependency["inventory_input_name"]
        ):
            raise ValidationError("既有 artifact 的受控库存 selector 与 workflow 依赖不一致")
        dependency_artifacts.append(artifact)

    # One attestation snapshot is the sole publish/readiness fact for a draft
    # profile.  A later workflow therefore re-attests every artifact already
    # declared on that profile, not only the new workflow's dependency subset.
    artifacts = list(
        ModelArtifact.objects.select_for_update()
        .filter(profile_version=locked_target)
        .order_by("pk")
    )
    dependency_ids = {artifact.id for artifact in dependency_artifacts}
    if not dependency_ids.issubset({artifact.id for artifact in artifacts}):
        raise ValidationError("workflow 模型依赖未进入 draft profile 的完整 artifact closure")
    seen_role_filenames: set[tuple[str, str]] = set()
    artifact_rows = []
    for artifact in artifacts:
        safe_filename = _safe_filename(artifact.filename)
        if safe_filename != artifact.filename:
            raise ValidationError("draft profile artifact 文件名不是规范化安全相对路径")
        role_filename = (artifact.role, safe_filename)
        if role_filename in seen_role_filenames:
            raise ValidationError("draft profile 中存在重复 role/filename artifact")
        seen_role_filenames.add(role_filename)
        if artifact.status in {ModelArtifact.Status.MISSING, ModelArtifact.Status.INVALID}:
            raise ValidationError("draft profile 含不可用于证明的 artifact 状态")
        try:
            artifact_inventory_spec(artifact)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        artifact_rows.append({
            "artifact_id": str(artifact.id),
            "role": artifact.role,
            "filename": artifact.filename,
            "inventory_folder": artifact.inventory_folder,
            "inventory_node_type": artifact.inventory_node_type,
            "inventory_input_name": artifact.inventory_input_name,
            "declared_status": artifact.status,
            "declared_sha256": artifact.sha256,
            "declared_size_bytes": artifact.size_bytes,
        })

    request_payload = {
        **intent,
        "artifacts": artifact_rows,
    }
    request_contract = json_contract(
        request_payload,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    request_snapshot = {
        **request_payload,
        "request_contract_sha256": request_contract.sha256,
    }
    if existing is not None:
        verify_json_contract(
            existing.request_snapshot,
            schema_version=existing.request_snapshot_schema_version,
            size_bytes=existing.request_snapshot_size_bytes,
            sha256=existing.request_snapshot_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        if (
            existing.probe_kind
            == RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION
            and existing.request_snapshot.get("request_contract_sha256")
            == request_contract.sha256
        ):
            return existing, False
        raise DraftArtifactAttestationConflict(
            "该主机已有不同的能力探测或模型证明任务；请等待完成或先取消",
        )
    attempt = RuntimeHostProbeAttempt(
        host=locked_source.runtime_host,
        created_by=actor,
        trigger=RuntimeHostProbeAttempt.Trigger.MANUAL,
        probe_kind=RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION,
    )
    assign_json_contract(
        attempt,
        "request_snapshot",
        request_snapshot,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    attempt.save()
    return attempt, True


def enqueue_draft_model_artifact_attestation(
    deployment: ModelDeployment,
    *,
    source_path: str,
    expected_sha256: str,
    expected_source_scope_schema_version: int,
    expected_source_scope_sha256: str,
    primary_output_node_ids: list[int | str],
    expected_object_info_schema_sha256: str,
    expected_converted_graph_sha256: str,
    model_profile_version: ModelProfileVersion,
    actor,
) -> tuple[RuntimeHostProbeAttempt, bool]:
    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider", "runtime_host",
    ).get(pk=deployment.pk)
    model_profile_version = ModelProfileVersion.objects.select_related("provider").get(
        pk=model_profile_version.pk,
    )
    _assert_expected_source_scope(
        deployment,
        expected_source_scope_schema_version=expected_source_scope_schema_version,
        expected_source_scope_sha256=expected_source_scope_sha256,
    )
    _assert_source_and_target(deployment, model_profile_version)
    if len(primary_output_node_ids) != 1:
        raise ValidationError("模型证明必须且只能选择一个 canvas 主输出")
    driver = _source_driver(deployment)
    budget = _RequestBudget(
        max_items=1,
        max_bytes=saved_workflow_request_max_bytes(),
        max_seconds=saved_workflow_batch_max_seconds(),
    )
    document = _read_source(driver, deployment, source_path, budget=budget)
    _assert_expected_sha(document, expected_sha256)
    try:
        _assert_safe_canvas_preflight(document.workflow)
    except SavedWorkflowSecretField as exc:
        logger.warning("saved workflow attestation rejected by %s", type(exc).__name__)
        raise
    object_info = _invoke_with_budget(
        driver, deployment, "object_info", {}, budget=budget,
    )
    if not isinstance(object_info, dict):
        raise ValidationError("object_info 响应无效")
    try:
        converted = ui_workflow_to_api_graph(
            document.workflow,
            object_info,
            primary_output_node_ids=primary_output_node_ids,
        )
    except ComfyUICanvasImportError as exc:
        logger.warning("saved workflow attestation rejected by %s", type(exc).__name__)
        raise SavedWorkflowImportError(
            _safe_projected_error(
                exc,
                fallback="ComfyUI 工作流结构不满足平台安全导入合同",
            ),
        ) from exc
    object_info_sha, converted_sha = _assert_conversion_expectations(
        {
            "expected_object_info_schema_sha256": expected_object_info_schema_sha256,
            "expected_converted_graph_sha256": expected_converted_graph_sha256,
        },
        object_info=object_info,
        converted_graph=converted.api_graph,
    )
    dependencies = _dependency_specs(converted.api_graph, object_info)
    intent = _intent_payload(
        source=deployment,
        target=model_profile_version,
        document=document,
        primary_output_node_ids=primary_output_node_ids,
        object_info_sha256=object_info_sha,
        converted_graph_sha256=converted_sha,
        dependencies=dependencies,
    )
    return _create_or_reuse_attempt(
        source=deployment,
        target=model_profile_version,
        intent=intent,
        actor=actor,
    )


def load_draft_attestation_context(
    attempt: RuntimeHostProbeAttempt,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    if attempt.probe_kind != RuntimeHostProbeAttempt.ProbeKind.DRAFT_MODEL_ARTIFACT_ATTESTATION:
        raise ValidationError("probe attempt 不是 draft model artifact attestation")
    queryset = RuntimeHostProbeAttempt.objects
    if for_update:
        queryset = queryset.select_for_update()
    attempt = queryset.get(pk=attempt.pk)
    verify_json_contract(
        attempt.request_snapshot,
        schema_version=attempt.request_snapshot_schema_version,
        size_bytes=attempt.request_snapshot_size_bytes,
        sha256=attempt.request_snapshot_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    request = attempt.request_snapshot
    if (
        request.get("probe_kind") != attempt.probe_kind
        or str(request.get("host_id") or "") != str(attempt.host_id)
        or request.get("request_contract_sha256")
        != json_contract(
            {
                key: value
                for key, value in request.items()
                if key != "request_contract_sha256"
            },
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
    ):
        raise ValidationError("attestation request snapshot 与 attempt 不一致")
    deployment_query = ModelDeployment.objects.select_related(
        "profile_version__provider", "runtime_host",
    )
    profile_query = ModelProfileVersion.objects.select_related("provider")
    artifact_query = ModelArtifact.objects.all()
    if for_update:
        deployment_query = deployment_query.select_for_update()
        profile_query = profile_query.select_for_update()
        artifact_query = artifact_query.select_for_update()
    deployment = deployment_query.get(pk=request["source_deployment_id"])
    target = profile_query.get(pk=request["target_model_profile_version_id"])
    _assert_source_and_target(deployment, target)
    source_scope = comfyui_workflow_source_scope(deployment)
    if (
        deployment.runtime_host_id != attempt.host_id
        or source_deployment_fingerprint(deployment)
        != request.get("source_deployment_fingerprint_sha256")
        or source_scope.schema_version != request.get("source_scope_schema_version")
        or source_scope.sha256 != request.get("source_scope_sha256")
        or draft_model_profile_fingerprint(target)
        != request.get("target_profile_fingerprint_sha256")
    ):
        raise ValidationError("attestation source deployment 配置已变化")
    artifact_rows = request.get("artifacts")
    if not isinstance(artifact_rows, list) or not artifact_rows:
        raise ValidationError("attestation request 缺少 artifact closure")
    current_artifacts = list(
        artifact_query.filter(profile_version=target).order_by("pk")
    )
    artifacts_by_id = {str(item.id): item for item in current_artifacts}
    frozen_ids = [
        str(row.get("artifact_id") or "")
        for row in artifact_rows
        if isinstance(row, dict)
    ]
    if (
        len(frozen_ids) != len(artifact_rows)
        or len(set(frozen_ids)) != len(frozen_ids)
        or set(artifacts_by_id) != set(frozen_ids)
    ):
        raise ValidationError("attestation artifact closure 已变化")
    artifacts = []
    for row in artifact_rows:
        artifact = artifacts_by_id.get(str(row.get("artifact_id") or ""))
        if artifact is None or artifact.profile_version_id != target.id:
            raise ValidationError("attestation artifact 不属于冻结的 draft profile")
        frozen = (
            row.get("role"), row.get("filename"), row.get("inventory_folder"),
            row.get("inventory_node_type"), row.get("inventory_input_name"),
            row.get("declared_status"), row.get("declared_sha256"),
            row.get("declared_size_bytes"),
        )
        current = (
            artifact.role, artifact.filename, artifact.inventory_folder,
            artifact.inventory_node_type, artifact.inventory_input_name,
            artifact.status, artifact.sha256, artifact.size_bytes,
        )
        if current != frozen:
            raise ValidationError("attestation artifact 声明在执行期间已变化")
        artifacts.append(artifact)
    return {
        "attempt": attempt,
        "source_deployment": deployment,
        "target_profile": target,
        "artifacts": artifacts,
        "verified_by": attempt.created_by,
    }
