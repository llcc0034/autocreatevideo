from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
import re
import time
from typing import Any
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from studio.models import (
    ComfyWorkflowSource,
    ComfyWorkflowContractDraft,
    Asset,
    GeneratedWorkflow,
    GenerationTask,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    WorkflowProjectorVersion,
)
from studio.services.comfyui import ComfyUIClient
from studio.services.comfyui_workflow_library import (
    SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY,
    saved_workflow_directory_resolution_cache,
)
from studio.services.comfyui_artifacts import (
    DEFAULT_MODEL_FOLDER_BY_ROLE,
    artifact_inventory_spec,
    object_info_input_options,
    safe_model_inventory_options,
    safe_model_relative_filename,
)
from studio.services.comfyui_canvas import (
    ComfyUICanvasImportError,
    ui_workflow_to_api_graph,
)
from studio.services.comfyui_contract import ComfyUIContractError, validate_comfyui_api_contract
from studio.services.comfyui_schema import dynamic_widget_definitions
from studio.services.json_contracts import (
    assign_json_contract,
    canonical_json_bytes,
    find_forbidden_secret_fields,
    json_contract,
    verify_json_contract,
)
from studio.services.media_capabilities import resolve_conditioning_contract
from studio.services.model_profile_contracts import verified_model_profile_specification
from studio.services.provider_registry import (
    COMFYUI_WORKFLOW_SOURCE_SCOPE_SCHEMA_VERSION,
    ProviderDriverError,
    comfyui_workflow_source_scope,
    driver_for,
)
from studio.services.provenance import sha256_file
from studio.services.workflow import WorkflowBuildError, validate_comfy_graph
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    compiler_contract_from_input,
)
from studio.services.workflow_semantics import canonical_binding_rows, canonical_requirement_rows
from studio.services.workflow_versions import (
    import_workflow_version,
    verify_workflow_version_integrity,
)
from studio.services.workflow_projectors import (
    ProjectorContract,
    WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS,
    WorkflowProjectorError,
    current_projector_catalog,
    projector_model_selector,
    projector_output_kind,
    select_workflow_projector,
    validate_workflow_projector_definition,
    validate_frozen_workflow_projector_contract,
)
from studio.services.security_boundaries import (
    boundary_int,
    boundary_number,
    security_boundary_resolution_cache,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger(__name__)
_TASK_FILENAME = re.compile(
    r"(?:^|/)autoprompt-task-([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})-comfyui-ui\.json$",
    re.IGNORECASE,
)
# These endpoints are synchronous, staff-only review operations.  Bound the
# whole request as well as each individual file so a large userdata tree cannot
# monopolize a web worker.  The current production library has twelve files.


def saved_workflow_scan_max_items() -> int:
    return boundary_int("security.comfy.import.scan_max_items")


def saved_workflow_batch_max_items() -> int:
    return boundary_int("security.comfy.import.batch_max_items")


def saved_workflow_request_max_bytes() -> int:
    return boundary_int("security.comfy.import.request_max_bytes")


def saved_workflow_scan_max_seconds() -> float:
    return boundary_number("security.comfy.import.scan_max_seconds", minimum=0.001)


def saved_workflow_batch_max_seconds() -> float:
    return boundary_number("security.comfy.import.batch_max_seconds", minimum=0.001)


def _workflow_graph_max_bytes() -> int:
    return boundary_int("security.workflow.max_graph_bytes")


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")
# RFC 8259 allows arbitrarily large JSON numbers, but the Settings client and
# the other supported JavaScript callers use IEEE-754 doubles.  Keep every
# integer-valued workflow contract scalar inside the exact interoperable range
# so preview -> browser -> batch import cannot change an authoritative value.
JSON_INTEROPERABLE_INTEGER_MAX = (1 << 53) - 1
JSON_INTEROPERABLE_INTEGER_MIN = -JSON_INTEROPERABLE_INTEGER_MAX
class SavedWorkflowImportError(ValidationError):
    code = "workflow_import_failed"

    def __init__(self, message):
        super().__init__(message, code=type(self).code)


class SavedWorkflowSourceChanged(SavedWorkflowImportError):
    code = "source_changed"


class SavedWorkflowSourceConflict(SavedWorkflowImportError):
    code = "source_already_classified"


class SavedWorkflowSecretField(SavedWorkflowImportError):
    code = "secret_field_forbidden"


class SavedWorkflowUnsafeLiteral(SavedWorkflowImportError):
    code = "unsafe_source_literal"


class SavedWorkflowConversionChanged(SavedWorkflowImportError):
    code = "conversion_contract_changed"


class SavedWorkflowStructuralContractChanged(SavedWorkflowImportError):
    code = "workflow_structural_contract_changed"


class SavedWorkflowTargetProfileMismatch(SavedWorkflowImportError):
    code = "incompatible_model_profile"


class SavedWorkflowDependencyUnmanaged(SavedWorkflowImportError):
    code = "model_dependency_unmanaged"


class SavedWorkflowSourceDeploymentChanged(SavedWorkflowImportError):
    code = "source_deployment_changed"


class SavedWorkflowSourceScopeChanged(SavedWorkflowImportError):
    code = "source_scope_changed"


class SavedWorkflowOutputContractInvalid(SavedWorkflowImportError):
    code = "workflow_output_contract_invalid"


class SavedWorkflowRequestBudgetExceeded(SavedWorkflowImportError):
    code = "workflow_request_budget_exceeded"


class SavedWorkflowContractBlocked(SavedWorkflowImportError):
    code = "workflow_contract_blocked"


class SavedWorkflowCandidateRequired(SavedWorkflowImportError):
    code = "workflow_candidate_required"


class SavedWorkflowContractDraftMismatch(SavedWorkflowImportError):
    code = "workflow_contract_draft_mismatch"


@dataclass(frozen=True)
class _SourceDocument:
    source_path: str
    source_sha256: str
    size_bytes: int
    workflow: dict


@dataclass(frozen=True)
class _SavedWorkflowPreviewContext:
    deployment: ModelDeployment
    document: _SourceDocument
    object_info: dict[str, Any]
    object_info_schema_sha256: str
    output_candidates: list[dict[str, Any]]


def _source_path_sha256(source_path: str) -> str:
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()


def _contains_forbidden_canvas_field(value: Any) -> bool:
    """Detect secret-shaped UI field declarations without reading their value."""

    if find_forbidden_secret_fields(value):
        return True
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                str(key).casefold().replace("-", "_")
                in {"name", "input_name", "widget_name"}
                and isinstance(child, str)
                and find_forbidden_secret_fields({child: None})
            ):
                return True
            if _contains_forbidden_canvas_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_canvas_field(child) for child in value)
    return False


def _assert_safe_canvas_preflight(workflow: dict) -> None:
    if _contains_forbidden_canvas_field(workflow):
        raise SavedWorkflowSecretField(
            "工作流含禁止持久化的明文密钥字段",
        )


@dataclass
class _RequestBudget:
    max_items: int
    max_bytes: int
    max_seconds: float
    operation_label: str = "工作流请求"
    time_config_key: str = "security.comfy.import.scan_max_seconds"
    started_at: float = field(default_factory=lambda: time.monotonic())
    item_count: int = 0
    byte_count: int = 0

    def _timeout_error(self) -> SavedWorkflowRequestBudgetExceeded:
        return SavedWorkflowRequestBudgetExceeded(
            f"{self.operation_label}已超过系统配置的 {self.max_seconds:g} 秒同步请求时限"
            f"（已处理 {self.item_count} 个文件、{self.byte_count} bytes）；"
            f"请在“默认值与限制”缩小 {SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY}，"
            f"或由超级管理员复核 {self.time_config_key}",
        )

    def assert_available(self) -> None:
        if time.monotonic() - self.started_at >= self.max_seconds:
            raise self._timeout_error()

    def remaining_seconds(self) -> float:
        remaining = self.max_seconds - (time.monotonic() - self.started_at)
        if remaining <= 0:
            raise self._timeout_error()
        return remaining

    def consume(self, size_bytes: int) -> None:
        self.assert_available()
        if self.item_count + 1 > self.max_items:
            raise SavedWorkflowRequestBudgetExceeded(
                f"单次请求最多读取 {self.max_items} 个已保存工作流",
            )
        if self.byte_count + size_bytes > self.max_bytes:
            raise SavedWorkflowRequestBudgetExceeded(
                f"单次请求读取总量不能超过 {self.max_bytes} bytes",
            )
        self.item_count += 1
        self.byte_count += size_bytes


def _invoke_with_budget(
    driver,
    deployment: ModelDeployment,
    operation: str,
    payload: dict[str, Any],
    *,
    budget: _RequestBudget | None,
):
    bounded_payload = dict(payload)
    if budget is not None:
        bounded_payload["_transport_timeout_seconds"] = budget.remaining_seconds()
    result = driver.invoke(deployment, operation, bounded_payload)
    if budget is not None:
        budget.assert_available()
    return result


def source_deployment_fingerprint(deployment: ModelDeployment) -> str:
    provider = deployment.profile_version.provider
    source_scope = comfyui_workflow_source_scope(deployment)
    return json_contract({
        "schema_version": 1,
        "deployment_id": str(deployment.id),
        "deployment_status": deployment.status,
        "deployment_runtime_config_sha256": deployment.runtime_config_sha256,
        "runtime_host_id": str(deployment.runtime_host_id or ""),
        "profile_version_id": str(deployment.profile_version_id),
        "profile_status": deployment.profile_version.status,
        "provider_id": str(provider.id),
        "provider_active": provider.is_active,
        "provider_driver_type": provider.driver_type,
        "provider_driver_config_sha256": provider.driver_config_sha256,
        "source_scope_schema_version": source_scope.schema_version,
        "source_scope_sha256": source_scope.sha256,
    }, max_bytes=_stage_snapshot_max_bytes()).sha256


def _assert_expected_source_scope(
    deployment: ModelDeployment,
    *,
    expected_source_scope_schema_version: int,
    expected_source_scope_sha256: str,
) -> None:
    scope = comfyui_workflow_source_scope(deployment)
    if (
        scope.schema_version != expected_source_scope_schema_version
        or scope.sha256 != str(expected_source_scope_sha256 or "").casefold()
    ):
        raise SavedWorkflowSourceScopeChanged(
            "ComfyUI 工作流来源范围自扫描后已变化，请重新扫描",
        )


def _locked_source_deployment(
    deployment: ModelDeployment,
    *,
    expected_fingerprint: str,
) -> ModelDeployment:
    locked = ModelDeployment.objects.select_for_update().select_related(
        "profile_version__provider",
        "runtime_host",
    ).get(pk=deployment.pk)
    if (
        locked.status != ModelDeployment.Status.ACTIVE
        or not locked.profile_version.provider.is_active
        or locked.profile_version.provider.driver_type != ProviderProfile.DriverType.COMFYUI
        or locked.runtime_host_id is None
        or locked.runtime_host.status != RuntimeHost.Status.ACTIVE
    ):
        raise SavedWorkflowSourceDeploymentChanged(
            "读取 workflow 后 source deployment/provider/runtime host 已停用，请重新扫描",
        )
    try:
        locked_fingerprint = source_deployment_fingerprint(locked)
    except (ValidationError, ProviderDriverError) as exc:
        raise SavedWorkflowSourceDeploymentChanged(
            "读取 workflow 后 source deployment/provider/runtime host 配置已失效，请重新扫描",
        ) from exc
    if locked_fingerprint != expected_fingerprint:
        raise SavedWorkflowSourceDeploymentChanged(
            "读取 workflow 后 source deployment/provider/runtime host 已变化，请重新扫描",
        )
    return locked


def _locked_target_profile(
    target: ModelProfileVersion,
    *,
    source_provider_id,
    output_kind: str,
) -> ModelProfileVersion:
    locked = ModelProfileVersion.objects.select_for_update().select_related(
        "provider",
    ).get(pk=target.pk)
    if (
        locked.status not in {
            ModelProfileVersion.Status.DRAFT,
            ModelProfileVersion.Status.PUBLISHED,
        }
        or locked.provider_id != source_provider_id
        or locked.provider.driver_type != ProviderProfile.DriverType.COMFYUI
        or not locked.provider.is_active
        or locked.modality not in {output_kind, ModelProfileVersion.Modality.MULTIMODAL}
    ):
        raise SavedWorkflowTargetProfileMismatch(
            "目标 model profile 必须是 source provider 下可审阅且输出模态兼容的 ComfyUI 版本",
        )
    # Promoting a closed contract draft creates only a DRAFT workflow version.
    # Runtime availability and fresh host/artifact proof belong exclusively to
    # Live Validate, where the operator selects the concrete Deployment.
    return locked


def _assert_preview_target_profile(
    deployment: ModelDeployment,
    target: ModelProfileVersion | None,
) -> None:
    """Keep previews on the source provider; drafts remain valid for attestation."""

    if target is None:
        return
    source_provider = deployment.profile_version.provider
    if (
        target.status not in {
            ModelProfileVersion.Status.DRAFT,
            ModelProfileVersion.Status.PUBLISHED,
        }
        or target.provider_id != source_provider.id
        or target.provider.driver_type != ProviderProfile.DriverType.COMFYUI
        or not target.provider.is_active
    ):
        raise SavedWorkflowTargetProfileMismatch(
            "预览目标 model profile 必须属于同一启用的 ComfyUI source provider",
        )


def _source_driver(
    deployment: ModelDeployment,
    *,
    required_operations: set[str] | None = None,
):
    if (
        deployment.status != ModelDeployment.Status.ACTIVE
        or deployment.profile_version.provider.driver_type != ProviderProfile.DriverType.COMFYUI
        or not deployment.profile_version.provider.is_active
        or deployment.runtime_host_id is None
        or deployment.runtime_host.status != RuntimeHost.Status.ACTIVE
    ):
        raise SavedWorkflowImportError("source deployment 与运行主机必须均为启用状态")
    driver = driver_for(deployment.profile_version.provider)
    operations = set((driver.capabilities() or {}).get("operations") or [])
    required = required_operations or {
        "workflow_library.list", "workflow_library.read", "object_info",
    }
    if not required <= operations:
        missing = sorted(required - operations)
        raise SavedWorkflowImportError(
            f"ComfyUI driver 缺少当前阶段所需操作: {', '.join(missing)}",
        )
    return driver


def _read_source(
    driver,
    deployment: ModelDeployment,
    source_path: str,
    *,
    budget: _RequestBudget | None = None,
) -> _SourceDocument:
    normalized = ComfyUIClient._saved_workflow_path(source_path)
    payload = _invoke_with_budget(
        driver,
        deployment,
        "workflow_library.read",
        {"source_path": normalized},
        budget=budget,
    )
    if not isinstance(payload, dict):
        raise SavedWorkflowImportError("workflow_library.read 响应无效")
    returned_path = ComfyUIClient._saved_workflow_path(str(payload.get("source_path") or ""))
    digest = str(payload.get("source_sha256") or "").casefold()
    size_bytes = payload.get("size_bytes")
    workflow = payload.get("workflow")
    if (
        returned_path != normalized
        or _SHA256.fullmatch(digest) is None
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 < size_bytes <= ComfyUIClient.SAVED_WORKFLOW_MAX_BYTES
        or not isinstance(workflow, dict)
    ):
        raise SavedWorkflowImportError("workflow_library.read 来源合同无效")
    if budget is not None:
        budget.consume(size_bytes)
    return _SourceDocument(returned_path, digest, size_bytes, workflow)


def _assert_expected_sha(document: _SourceDocument, expected_sha256: str) -> None:
    expected = str(expected_sha256 or "").casefold()
    if _SHA256.fullmatch(expected) is None:
        raise SavedWorkflowImportError("expected_sha256 必须是 64 位小写十六进制")
    if document.source_sha256 != expected:
        raise SavedWorkflowSourceChanged("ComfyUI source_path 内容已变化，请重新扫描和预览")


def _object_info_sha256(object_info: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(object_info)).hexdigest()


def _converted_graph_sha256(graph: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(graph)).hexdigest()


def _assert_conversion_expectations(
    item: dict[str, Any],
    *,
    object_info: dict,
    converted_graph: dict,
) -> tuple[str, str]:
    """Verify a user-reviewed conversion for the separate attestation flow."""

    object_info_sha = _object_info_sha256(object_info)
    converted_sha = _converted_graph_sha256(converted_graph)
    expected_object_info = str(
        item.get("expected_object_info_schema_sha256") or "",
    ).casefold()
    expected_converted = str(
        item.get("expected_converted_graph_sha256") or "",
    ).casefold()
    if object_info_sha != expected_object_info or converted_sha != expected_converted:
        raise SavedWorkflowConversionChanged(
            "ComfyUI /object_info 或 canvas 转换结果已变化，请重新预览后再证明",
        )
    return object_info_sha, converted_sha


def _requested_output_kind(item: dict[str, Any]) -> str:
    template = item.get("template")
    if template is not None:
        return str(template.output_kind or "")
    requested = item.get("new_template")
    return str(requested.get("output_kind") or "") if isinstance(requested, dict) else ""


def _validate_import_output_contract(
    *,
    item: dict[str, Any],
    converted,
    object_info: dict,
) -> None:
    """Bind one canvas primary to one server-projected compiler output contract."""

    requested_canvas_ids = item.get("primary_output_node_ids") or []
    if len(requested_canvas_ids) != 1 or len(converted.primary_output_node_ids) != 1:
        raise SavedWorkflowOutputContractInvalid(
            "template_version 必须且只能选择一个主输出节点",
        )
    primary_api_id = str(converted.primary_output_node_ids[0])
    node = converted.api_graph.get(primary_api_id)
    if not isinstance(node, dict):
        raise SavedWorkflowOutputContractInvalid("转换后的主输出节点不存在")
    class_type = str(node.get("class_type") or "")
    output_kind = _requested_output_kind(item)
    input_contract = item.get("input_contract")
    frozen_projector = (
        validate_frozen_workflow_projector_contract(input_contract)
        if isinstance(input_contract, dict)
        and isinstance(input_contract.get("contract_projector"), dict)
        else None
    )
    if projector_output_kind(
        class_type,
        catalog=[frozen_projector] if frozen_projector is not None else None,
    ) != output_kind:
        raise SavedWorkflowOutputContractInvalid(
            f"主输出节点 {class_type!r} 与 template output_kind {output_kind!r} 不匹配",
        )
    node_info = object_info.get(class_type)
    if not isinstance(node_info, dict) or node_info.get("output_node") is not True:
        raise SavedWorkflowOutputContractInvalid(
            f"主输出节点 {class_type!r} 未被实时 /object_info 声明为 output_node",
        )
    # Preview/batch clients no longer submit a workflow contract.  The server
    # derives it from the same source bytes + live object_info below.  This
    # first pass therefore validates only the selected primary; a second pass
    # over the authoritative item validates compiler/output details.
    if not isinstance(input_contract, dict) or not input_contract:
        return
    try:
        compiler_id, compiler_version = compiler_contract_from_input(input_contract)
    except WorkflowCompilerError as exc:
        raise SavedWorkflowOutputContractInvalid(str(exc)) from exc
    if (compiler_id, compiler_version) not in {
        ("generic.comfy", 1), ("generic.comfy", 2),
        ("generic.comfy", 3),
    }:
        raise SavedWorkflowOutputContractInvalid(
            "saved workflow 导入没有匹配受控 compiler projector",
        )
    outputs = input_contract.get("outputs") if isinstance(input_contract, dict) else None
    primary = outputs.get("primary") if isinstance(outputs, dict) else None
    if not isinstance(primary, dict) or (
        str(primary.get("node_id") or "") != primary_api_id
        or str(primary.get("kind") or "") != output_kind
        or primary.get("required") is not True
    ):
        raise SavedWorkflowOutputContractInvalid(
            "input_contract.outputs.primary 必须精确绑定转换后的唯一主输出 API node/kind",
        )


def _canvas_output_candidates(workflow: dict, object_info: dict) -> list[dict[str, Any]]:
    nodes = [item for item in workflow.get("nodes", []) if isinstance(item, dict)]
    try:
        projector_catalog = current_projector_catalog()
    except WorkflowProjectorError:
        projector_catalog = []
    outgoing = {
        item[1]
        for item in workflow.get("links", [])
        if isinstance(item, list) and len(item) == 6
    }
    rows = []
    for node in nodes:
        node_id = node.get("id")
        node_type = str(node.get("type") or "")
        info = object_info.get(node_type) if isinstance(object_info, dict) else None
        if node_type in {"Note", "MarkdownNote"}:
            continue
        if (isinstance(info, dict) and info.get("output_node") is True) or node_id not in outgoing:
            try:
                if not projector_catalog:
                    raise WorkflowProjectorError("工作流合同方案目录不可用")
                converted = ui_workflow_to_api_graph(
                    workflow,
                    object_info,
                    primary_output_node_ids=[node_id],
                )
                projector = select_workflow_projector(
                    converted.api_graph,
                    catalog=projector_catalog,
                )
                suggested_output_kind = projector_output_kind(
                    node_type,
                    catalog=[projector],
                )
            except (
                ComfyUICanvasImportError,
                WorkflowBuildError,
                WorkflowProjectorError,
            ):
                # Discovery and candidate admission do not depend on a
                # published Projector. Keep the live output node visible and
                # let contract analysis record the missing interpretation.
                suggested_output_kind = ""
            rows.append({
                "canvas_node_id": node_id,
                "node_type": node_type,
                "suggested_output_kind": suggested_output_kind,
            })
    return rows


def _input_definition(object_info: dict, class_type: str, input_name: str):
    info = object_info.get(class_type)
    inputs = info.get("input") if isinstance(info, dict) else None
    if not isinstance(inputs, dict):
        return None
    for category in ("required", "optional"):
        definitions = inputs.get(category)
        if isinstance(definitions, dict) and input_name in definitions:
            return definitions[input_name]
    return None


def _node_input_definition(object_info: dict, node: dict, input_name: str):
    """Resolve direct and selected dynamic-widget definitions for one API node."""

    class_type = str(node.get("class_type") or "")
    direct = _input_definition(object_info, class_type, input_name)
    if direct is not None:
        return direct
    info = object_info.get(class_type)
    sections = info.get("input") if isinstance(info, dict) else None
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    if not isinstance(sections, dict):
        return None
    for category in ("required", "optional"):
        definitions = sections.get(category)
        if not isinstance(definitions, dict):
            continue
        for parent_name, definition in definitions.items():
            input_type = _definition_type_name(definition)
            if input_type not in {"COMFY_DYNAMICCOMBO_V3", "COMBO"}:
                continue
            selected = inputs.get(parent_name)
            if isinstance(selected, list):
                continue
            for child_name, child_definition, _required in dynamic_widget_definitions(
                str(parent_name),
                input_type,
                _definition_config(definition),
                selected_value=selected,
            ):
                if child_name == input_name:
                    return child_definition
    return None


def _definition_options(definition) -> tuple[Any, ...]:
    if not isinstance(definition, (list, tuple)) or not definition:
        return ()
    raw_type = definition[0]
    config = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
    if isinstance(raw_type, (list, tuple)):
        raw_options = raw_type
    elif raw_type in {"COMBO", "COMFY_DYNAMICCOMBO_V3"}:
        raw_options = config.get("options")
    else:
        return ()
    if not isinstance(raw_options, (list, tuple)):
        return ()
    values = []
    for option in raw_options:
        if isinstance(option, dict) and isinstance(option.get("key"), str):
            values.append(option["key"])
        elif isinstance(option, (str, int, float, bool)):
            values.append(option)
    return tuple(values)


def _definition_config(definition) -> dict:
    return (
        definition[1]
        if isinstance(definition, (list, tuple))
        and len(definition) > 1
        and isinstance(definition[1], dict)
        else {}
    )


def _definition_type_name(definition) -> str:
    if not isinstance(definition, (list, tuple)) or not definition:
        return ""
    raw_type = definition[0]
    if isinstance(raw_type, (list, tuple)):
        return "COMBO"
    return str(raw_type or "")


def _bounded_parameter_slug(value: str, *, maximum: int = 32) -> str:
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", value).casefold()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"value_{normalized}" if normalized else "value"
    return normalized[:maximum].rstrip("_") or "value"


def _parameter_value_type(definition) -> str:
    type_name = _definition_type_name(definition)
    if type_name == "INT":
        return "integer"
    if type_name in {"FLOAT", "NUMBER"}:
        return "number"
    if _definition_options(definition):
        return "enum"
    return ""


def _base_parameter_semantic(class_type: str, input_name: str) -> str:
    normalized_input = input_name.casefold().replace("-", "_")
    sampling = {
        "seed": "sampling.seed",
        "noise_seed": "sampling.seed",
        "steps": "sampling.steps",
        "cfg": "sampling.cfg_scale",
        "cfg_scale": "sampling.cfg_scale",
        "denoise": "sampling.denoise",
        "sampler": "sampling.sampler",
        "sampler_name": "sampling.sampler",
        "scheduler": "sampling.scheduler",
    }
    if normalized_input in sampling:
        return sampling[normalized_input]
    if normalized_input in {"fps", "frame_rate"}:
        return "video.fps"
    if normalized_input in {"frames", "frame_count", "num_frames"}:
        return "video.frames"
    if normalized_input == "batch_size":
        return "generation.batch_size"
    class_slug = _bounded_parameter_slug(class_type)
    input_slug = _bounded_parameter_slug(input_name)
    if class_type == "TextGenerate" and normalized_input == "max_length":
        return "parameter.text.max_length"
    if class_type == "ImageScaleToTotalPixels" and normalized_input == "megapixels":
        return "parameter.image.scale_megapixels"
    if class_type.startswith("SeedVR2") and normalized_input in {
        "resolution", "max_resolution",
    }:
        return f"parameter.seedvr2.{normalized_input}"
    return f"parameter.{class_slug}.{input_slug}"[:90].rstrip("._-")


def _value_matches_parameter_definition(value: Any, definition, value_type: str) -> bool:
    config = _definition_config(definition)
    if value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return False
    elif value_type == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not float("-inf") < float(value) < float("inf")
        ):
            return False
    elif value_type == "enum":
        return value in _definition_options(definition)
    else:
        return False
    minimum = config.get("min")
    maximum = config.get("max")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
        return False
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
        return False
    return True


def _assert_json_interoperable_integer_scalar(value: Any) -> None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and not JSON_INTEROPERABLE_INTEGER_MIN
        <= value
        <= JSON_INTEROPERABLE_INTEGER_MAX
    ):
        raise SavedWorkflowImportError(
            "可配置参数含无法在浏览器 JSON 中精确往返的整数值",
        )


def _json_interoperable_numeric_bound(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(
            JSON_INTEROPERABLE_INTEGER_MIN,
            min(JSON_INTEROPERABLE_INTEGER_MAX, value),
        )
    return value


def _safe_numeric_options(
    semantic_key: str,
    definition,
    value_type: str,
    source_value: Any,
) -> list[Any]:
    if semantic_key in {"image.width", "image.height"}:
        candidates = [candidate for candidate in (512, 768) if candidate <= source_value]
    elif semantic_key == "image.megapixels":
        safe_maximum = min(float(source_value), 0.4)
        candidates = [candidate for candidate in (0.1, 0.2, 0.4) if candidate <= safe_maximum]
    elif semantic_key == "parameter.text.max_length":
        candidates = [min(int(source_value), 64)]
    elif semantic_key in {
        "parameter.seedvr2.resolution",
        "parameter.seedvr2.max_resolution",
    }:
        candidates = [min(int(source_value), 1024)]
    elif semantic_key == "parameter.image.scale_megapixels":
        candidates = [min(float(source_value), 0.25)]
    else:
        candidates = [source_value]
    values = []
    for candidate in candidates:
        if (
            _value_matches_parameter_definition(candidate, definition, value_type)
            and candidate not in values
        ):
            values.append(candidate)
    return values


def _aligned_dimension(value: float, definition) -> int:
    config = _definition_config(definition)
    step = config.get("step")
    step = step if isinstance(step, int) and not isinstance(step, bool) and step > 0 else 1
    aligned = max(step, int(round(float(value) / step)) * step)
    minimum = config.get("min")
    maximum = config.get("max")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        aligned = max(aligned, int(minimum))
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        aligned = min(aligned, int(maximum))
    return aligned


def _frozen_integer_multiple(definition) -> int | None:
    """Return the exact Comfy INT lattice represented by object_info."""

    config = _definition_config(definition)
    if "step" not in config:
        # Comfy INT widgets without an explicit step use the unit lattice.
        # Freeze that source-schema fact now; production must never recreate it.
        return 1
    step = config.get("step")
    if isinstance(step, bool) or not isinstance(step, int) or step < 1:
        return None
    return step


def _explicit_resolution_parameter_policy(
    graph: dict,
    object_info: dict,
    bindings: list[dict[str, Any]],
    resolution_strategy: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(resolution_strategy, dict)
        or resolution_strategy.get("mode") != "explicit_width_height"
    ):
        return {}
    width_rows = [row for row in bindings if row["semantic_key"] == "image.width"]
    height_rows = [row for row in bindings if row["semantic_key"] == "image.height"]
    if len(width_rows) != 1 or len(height_rows) != 1:
        return {}
    width_row = width_rows[0]
    height_row = height_rows[0]
    if width_row["node_id"] != height_row["node_id"]:
        return {}
    node = graph.get(str(width_row["node_id"]))
    inputs = node.get("inputs") if isinstance(node, dict) else None
    if not isinstance(inputs, dict):
        return {}
    source_width = inputs.get(str(width_row["input_name"]))
    source_height = inputs.get(str(height_row["input_name"]))
    if (
        isinstance(source_width, bool)
        or isinstance(source_height, bool)
        or not isinstance(source_width, int)
        or not isinstance(source_height, int)
        or source_width <= 0
        or source_height <= 0
    ):
        return {}
    width_definition = _node_input_definition(
        object_info, node, str(width_row["input_name"]),
    )
    height_definition = _node_input_definition(
        object_info, node, str(height_row["input_name"]),
    )
    width_multiple = _frozen_integer_multiple(width_definition)
    height_multiple = _frozen_integer_multiple(height_definition)
    if width_multiple is None or height_multiple is None:
        return {}
    pairs: list[tuple[int, int]] = []
    for target_long_edge in (512, 768):
        if target_long_edge > max(source_width, source_height):
            continue
        scale = target_long_edge / max(source_width, source_height)
        width = _aligned_dimension(source_width * scale, width_definition)
        height = _aligned_dimension(source_height * scale, height_definition)
        if (
            _value_matches_parameter_definition(width, width_definition, "integer")
            and _value_matches_parameter_definition(height, height_definition, "integer")
            and width % width_multiple == 0
            and height % height_multiple == 0
            and (width, height) not in pairs
        ):
            pairs.append((width, height))
    if not pairs:
        return {}
    group = f"resolution.explicit.node_{width_row['node_id']}"
    return {
        "image.width": {
            "options": [pair[0] for pair in pairs],
            "recommended": pairs[-1][0],
            "selection_group": group,
        },
        "image.height": {
            "options": [pair[1] for pair in pairs],
            "recommended": pairs[-1][1],
            "selection_group": group,
        },
    }


def _recommended_parameter_value(
    semantic_key: str,
    source_value: Any,
    definition,
    value_type: str,
    options: list[Any],
) -> Any:
    candidates: list[Any] = []
    if options and value_type in {"integer", "number"}:
        candidates = options
    elif semantic_key == "parameter.text.max_length":
        candidates = [64]
    elif semantic_key in {
        "parameter.seedvr2.resolution",
        "parameter.seedvr2.max_resolution",
    }:
        candidates = [1024]
    elif semantic_key == "parameter.image.scale_megapixels":
        candidates = [0.25]
    legal = [
        value for value in candidates
        if _value_matches_parameter_definition(value, definition, value_type)
    ]
    if legal:
        if isinstance(source_value, (int, float)) and not isinstance(source_value, bool):
            return min(legal, key=lambda value: (abs(float(value) - float(source_value)), value))
        return legal[0]
    if _value_matches_parameter_definition(source_value, definition, value_type):
        return source_value
    config = _definition_config(definition)
    default = config.get("default")
    if _value_matches_parameter_definition(default, definition, value_type):
        return default
    raise SavedWorkflowImportError(
        "可配置参数的来源值和受控安全建议均不满足实时 object_info 合同",
    )


def _parameter_schema_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "parameters": sorted(
            ({
                "semantic_key": str(row.get("semantic_key") or ""),
                "type": str(row.get("type") or ""),
                "schema_min": row.get("schema_min"),
                "schema_max": row.get("schema_max"),
                "multiple_of": row.get("multiple_of"),
                "alignment_modulus": row.get("alignment_modulus"),
                "alignment_remainder": row.get("alignment_remainder"),
                "options": deepcopy(row.get("options") or [])
                if row.get("type") == "enum" else [],
                "selection_group": row.get("selection_group"),
                "target": deepcopy(row.get("target") or {}),
            } for row in rows),
            key=lambda row: (
                str(row.get("semantic_key") or ""),
                str((row.get("target") or {}).get("node_id") or ""),
                str((row.get("target") or {}).get("input_name") or ""),
            ),
        ),
    }


def _parameter_schema_sha256(rows: list[dict[str, Any]]) -> str:
    return json_contract(
        _parameter_schema_payload(rows),
        max_bytes=_workflow_graph_max_bytes(),
    ).sha256


def _is_upload_backed(definition) -> bool:
    config = _definition_config(definition)
    return any(
        bool(config.get(flag))
        for flag in ("image_upload", "video_upload", "audio_upload")
    )


def _asset_dependency_candidates(graph: dict, object_info: dict) -> list[dict[str, Any]]:
    """Describe reachable upload slots without disclosing concrete local names."""

    rows: list[dict[str, Any]] = []
    ordinal_by_kind: dict[str, int] = {}
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        class_type = str(node.get("class_type") or "")
        info = object_info.get(class_type)
        schema = info.get("input") if isinstance(info, dict) else None
        if not isinstance(schema, dict):
            continue
        inputs = node["inputs"]
        candidates: list[tuple[str, Any, bool]] = []
        for category in ("required", "optional"):
            definitions = schema.get(category)
            if not isinstance(definitions, dict):
                continue
            for input_name, definition in definitions.items():
                current = inputs.get(input_name)
                if isinstance(current, list):
                    continue
                config = _definition_config(definition)
                known_upload = (
                    (class_type == "LoadImage" and str(input_name).casefold() == "image")
                    or (
                        class_type == "LoadVideo"
                        and str(input_name).casefold() in {"file", "video"}
                    )
                    or (class_type == "LoadAudio" and str(input_name).casefold() == "audio")
                )
                if not known_upload and not _is_upload_backed(definition):
                    continue
                candidates.append((str(input_name), definition, category == "required"))
        for input_name, definition, is_required in candidates:
            config = _definition_config(definition)
            kind = (
                "image" if config.get("image_upload") or (
                    class_type == "LoadImage" and input_name.casefold() == "image"
                ) else
                "video" if config.get("video_upload") or (
                    class_type == "LoadVideo" and input_name.casefold() in {"file", "video"}
                ) else
                "audio" if config.get("audio_upload") or (
                    class_type == "LoadAudio" and input_name.casefold() == "audio"
                ) else ""
            )
            if not kind:
                continue
            ordinal_by_kind[kind] = ordinal_by_kind.get(kind, 0) + 1
            ordinal = ordinal_by_kind[kind]
            definitions = schema.get("required") if isinstance(schema, dict) else None
            rows.append({
                "node_id": str(node_id),
                "node_type": class_type,
                "input_name": str(input_name),
                "media_kind": kind,
                "value_type": f"asset_{kind}",
                "suggested_semantic_key": (
                    f"asset.{kind}.source" if ordinal == 1
                    else f"asset.{kind}.source_{ordinal}"
                ),
                "input_slot": ordinal,
                "asset_category": kind,
                "is_required": is_required,
                "min_items": 1 if is_required else 0,
                "max_items": 1,
            })
    return rows


def _connection_source_node_id(value: Any) -> str:
    """Return the source node id for one Comfy API connection value."""

    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return ""
    node_id, output_index = value
    if (
        isinstance(node_id, bool)
        or not isinstance(node_id, (str, int))
        or isinstance(output_index, bool)
        or not isinstance(output_index, int)
    ):
        return ""
    return str(node_id)


def _conditioning_input_role(input_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", input_name.casefold()).strip("_")
    if normalized in {
        "positive", "positive_cond", "positive_conditioning",
    }:
        return "positive"
    if normalized in {
        "negative", "negative_cond", "negative_conditioning",
    }:
        return "negative"
    return ""


def _upstream_node_ids(
    nodes_by_id: dict[str, dict[str, Any]],
    roots: list[str],
) -> set[str]:
    visited: set[str] = set()
    pending = list(roots)
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        node = nodes_by_id.get(node_id)
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for value in inputs.values():
            source_id = _connection_source_node_id(value)
            if source_id and source_id not in visited:
                pending.append(source_id)
    return visited


def _prompt_conditioning_roles(graph: dict) -> dict[str, frozenset[str]]:
    """Classify prompt-bearing nodes by their sampler conditioning branch.

    Comfy prompt encoder inputs are commonly named only ``text``.  Their
    positive/negative meaning lives on the downstream sampler connection, so
    input-name inspection alone cannot distinguish the two encoders.  A node is
    treated as a conditioning sink only when it exposes both linked branches;
    this avoids assigning roles from unrelated inputs that happen to contain
    the words positive or negative.
    """

    nodes_by_id = {
        str(node_id): node
        for node_id, node in graph.items()
        if isinstance(node, dict)
    }
    roles_by_node: dict[str, set[str]] = {}
    for node in nodes_by_id.values():
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        roots_by_role: dict[str, list[str]] = {"positive": [], "negative": []}
        for input_name, value in inputs.items():
            role = _conditioning_input_role(str(input_name))
            source_id = _connection_source_node_id(value)
            if role and source_id:
                roots_by_role[role].append(source_id)
        if not all(roots_by_role.values()):
            continue
        for role, roots in roots_by_role.items():
            for upstream_id in _upstream_node_ids(nodes_by_id, roots):
                roles_by_node.setdefault(upstream_id, set()).add(role)
    return {
        node_id: frozenset(roles)
        for node_id, roles in roles_by_node.items()
    }


def _prompt_semantic_key(
    *,
    node_id: str,
    input_name: str,
    conditioning_roles: dict[str, frozenset[str]],
) -> str:
    roles = conditioning_roles.get(str(node_id), frozenset())
    if roles == {"negative"}:
        return "prompt.negative"
    if roles == {"positive"}:
        return "prompt.positive"
    normalized = input_name.casefold().replace("-", "_")
    if "negative" in normalized:
        return "prompt.negative"
    return "prompt.positive"


def _suggested_generic_import_contract(
    graph: dict,
    object_info: dict,
    *,
    primary_api_node_ids: list[str],
    model_dependencies: list[dict[str, Any]],
    asset_dependencies: list[dict[str, Any]],
    model_profile_version: ModelProfileVersion | None,
    projector: ProjectorContract | None = None,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    """Build a value-free, reviewable generic.comfy import contract.

    The browser never receives prompt or userdata asset literals.  It receives
    only semantic targets, model filenames, exact artifact evidence candidates,
    and the API primary node selected by the server conversion.
    """

    bindings: list[dict[str, Any]] = []
    requirements: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    seen_targets: set[tuple[str, str]] = set()
    prompt_conditioning_roles = _prompt_conditioning_roles(graph)

    def add_binding(
        semantic_key: str,
        node_id: str,
        input_name: str,
        value_type: str,
        *,
        is_required: bool = True,
    ) -> None:
        target = (str(node_id), str(input_name))
        if target in seen_targets:
            return
        seen_targets.add(target)
        bindings.append({
            "semantic_key": semantic_key,
            "node_id": target[0],
            "input_name": target[1],
            "value_type": value_type,
            "is_required": bool(is_required),
        })

    for row in asset_dependencies:
        add_binding(
            str(row["suggested_semantic_key"]),
            str(row["node_id"]),
            str(row["input_name"]),
            str(row["value_type"]),
            is_required=bool(row.get("is_required", True)),
        )

    primary_id = str(primary_api_node_ids[0]) if len(primary_api_node_ids) == 1 else ""
    primary_node = graph.get(primary_id) if primary_id else None
    output_kind = projector_output_kind(
        str(primary_node.get("class_type") or "") if isinstance(primary_node, dict) else "",
        catalog=[projector] if projector is not None else None,
    )
    resolution_strategy: dict[str, Any] | None = None
    if output_kind in {"image", "video"}:
        explicit_nodes: list[str] = []
        for node_id, node in graph.items():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                continue
            inputs = node["inputs"]
            if (
                "width" in inputs and "height" in inputs
                and not isinstance(inputs["width"], list)
                and not isinstance(inputs["height"], list)
                and _definition_type_name(_input_definition(
                    object_info, str(node.get("class_type") or ""), "width",
                )) == "INT"
                and _definition_type_name(_input_definition(
                    object_info, str(node.get("class_type") or ""), "height",
                )) == "INT"
            ):
                explicit_nodes.append(str(node_id))
                add_binding("image.width", str(node_id), "width", "integer")
                add_binding("image.height", str(node_id), "height", "integer")
        if explicit_nodes:
            resolution_strategy = {
                "mode": "explicit_width_height",
                "node_ids": sorted(explicit_nodes),
            }
        else:
            aspect_nodes: list[str] = []
            for node_id, node in graph.items():
                if not isinstance(node, dict) or node.get("class_type") != "ResolutionSelector":
                    continue
                inputs = node.get("inputs")
                if not isinstance(inputs, dict) or (
                    "aspect_ratio" not in inputs or "megapixels" not in inputs
                    or isinstance(inputs["aspect_ratio"], list)
                    or isinstance(inputs["megapixels"], list)
                ):
                    continue
                aspect_definition = _input_definition(
                    object_info, "ResolutionSelector", "aspect_ratio",
                )
                megapixels_definition = _input_definition(
                    object_info, "ResolutionSelector", "megapixels",
                )
                if (
                    _definition_type_name(aspect_definition) not in {
                        "STRING", "COMBO", "COMFY_DYNAMICCOMBO_V3",
                    }
                    or _definition_type_name(megapixels_definition) not in {"FLOAT", "NUMBER"}
                ):
                    continue
                aspect_nodes.append(str(node_id))
                add_binding("image.aspect_ratio", str(node_id), "aspect_ratio", "enum")
                add_binding("image.megapixels", str(node_id), "megapixels", "number")
            if aspect_nodes:
                resolution_strategy = {
                    "mode": "aspect_megapixels",
                    "node_ids": sorted(aspect_nodes),
                }
            else:
                source_driver_nodes = sorted(
                    str(node_id)
                    for node_id, node in graph.items()
                    if isinstance(node, dict)
                    and node.get("class_type") == "FluxKontextImageScale"
                )
                source_semantics = sorted(
                    str(row["suggested_semantic_key"])
                    for row in asset_dependencies
                    if row.get("media_kind") == "image"
                )
                if source_driver_nodes and source_semantics:
                    resolution_strategy = {
                        "mode": "source_image_derived",
                        "node_ids": source_driver_nodes,
                        "asset_semantic_keys": source_semantics,
                    }
                else:
                    blockers.append({
                        "code": "resolution_strategy_required",
                        "detail": "可达图未形成受支持的显式尺寸、画幅像素或输入图驱动分辨率合同",
                    })

    for node_id, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        class_type = str(node.get("class_type") or "")
        for input_name, value in node["inputs"].items():
            if isinstance(value, list):
                continue
            if _is_output_path_literal(class_type, str(input_name), value):
                add_binding("output.prefix", str(node_id), str(input_name), "string")
            elif _is_prompt_literal(class_type, str(input_name), value):
                key = _prompt_semantic_key(
                    node_id=str(node_id),
                    input_name=str(input_name),
                    conditioning_roles=prompt_conditioning_roles,
                )
                add_binding(key, str(node_id), str(input_name), "string")

    for dependency in model_dependencies:
        alias = str(dependency["suggested_alias"])
        semantic_key = f"model.{alias}.filename"
        add_binding(
            semantic_key,
            str(dependency["node_id"]),
            str(dependency["input_name"]),
            "model_artifact",
        )
        matches = [
            item for item in dependency.get("artifact_matches", [])
            if isinstance(item.get("size_bytes"), int)
            and item["size_bytes"] > 0
            and len(str(item.get("sha256") or "")) == 64
        ]
        artifact = matches[0] if len(matches) == 1 else None
        requirements.append({
            "role": dependency["role"],
            "alias": alias,
            "artifact_id": artifact["id"] if artifact else None,
            "expected_filename": dependency["filename"],
            "expected_sha256": artifact["sha256"] if artifact else "",
            "is_required": True,
        })
        if not dependency.get("available_in_object_info"):
            blockers.append({
                "code": "model_inventory_missing",
                "phase": "live_validate",
                "detail": (
                    f"节点 {dependency['node_id']} ({dependency['node_type']})."
                    f"{dependency['input_name']} 的模型文件未出现在实时 ComfyUI 库存"
                ),
            })
        if artifact is None:
            blockers.append({
                "code": (
                    "model_artifact_ambiguous" if len(matches) > 1
                    else "model_artifact_contract_required"
                ),
                "detail": (
                    f"模型 {dependency['filename']} 缺少唯一、带 size/hash 的同 profile "
                    "artifact 声明"
                ),
            })
        elif (
            artifact.get("status") != ModelArtifact.Status.AVAILABLE
            or not artifact.get("verified_snapshot")
        ):
            blockers.append({
                "code": "model_artifact_attestation_required",
                "phase": "live_validate",
                "detail": (
                    f"模型 {dependency['filename']} 的声明已冻结；Live Validate 时仍需"
                    "目标主机最新 size/hash 证明"
                ),
            })

    pending_parameters: dict[str, list[tuple[str, str, str, Any]]] = {}
    for node_id, node in sorted(graph.items(), key=lambda item: str(item[0])):
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        class_type = str(node.get("class_type") or "")
        for input_name, source_value in sorted(node["inputs"].items()):
            target = (str(node_id), str(input_name))
            if target in seen_targets or isinstance(source_value, list):
                continue
            definition = _node_input_definition(object_info, node, str(input_name))
            value_type = _parameter_value_type(definition)
            if not value_type:
                continue
            if not _value_matches_parameter_definition(source_value, definition, value_type):
                raise SavedWorkflowImportError(
                    "可配置参数的来源值不满足实时 object_info 类型或范围",
                )
            _assert_json_interoperable_integer_scalar(source_value)
            base_key = _base_parameter_semantic(class_type, str(input_name))
            pending_parameters.setdefault(base_key, []).append((
                str(node_id), str(input_name), value_type, source_value,
            ))
    occupied_keys = {str(row["semantic_key"]) for row in bindings}
    for base_key, candidates in sorted(pending_parameters.items()):
        collision = len(candidates) > 1 or base_key in occupied_keys
        for node_id, input_name, value_type, _source_value in candidates:
            semantic_key = base_key
            if collision:
                node = graph[node_id]
                semantic_key = (
                    f"parameter.{_bounded_parameter_slug(str(node.get('class_type') or 'node'))}."
                    f"{_bounded_parameter_slug(input_name)}.node_{_bounded_parameter_slug(node_id)}"
                )[:90].rstrip("._-")
            if semantic_key in occupied_keys:
                raise SavedWorkflowImportError(
                    "可配置参数 semantic key 在转换后的可达图中不唯一",
                )
            add_binding(semantic_key, node_id, input_name, value_type)
            occupied_keys.add(semantic_key)

    parameters: dict[str, dict[str, Any]] = {}
    configurable_parameters: list[dict[str, Any]] = []
    configurable_by_key: dict[str, dict[str, Any]] = {}
    requirement_alias_by_key = {
        f"model.{row['alias']}.filename": row["alias"] for row in requirements
    }
    asset_dependency_by_key = {
        str(row["suggested_semantic_key"]): row for row in asset_dependencies
    }
    explicit_resolution_policy = _explicit_resolution_parameter_policy(
        graph,
        object_info,
        bindings,
        resolution_strategy,
    )
    for binding in bindings:
        key = binding["semantic_key"]
        definition = parameters.setdefault(key, {
            "type": binding["value_type"],
            "required": bool(binding.get("is_required", True)),
        })
        if definition["type"] != binding["value_type"]:
            raise SavedWorkflowImportError(
                f"semantic {key} 被推导为不兼容的 value type",
            )
        if key in requirement_alias_by_key:
            definition["requirement_alias"] = requirement_alias_by_key[key]
        if key in asset_dependency_by_key:
            dependency = asset_dependency_by_key[key]
            definition["input_slot"] = int(dependency["input_slot"])
            definition["asset_category"] = str(dependency["asset_category"])
            definition["media_kind"] = str(dependency["media_kind"])
            definition["min_items"] = int(dependency["min_items"])
            definition["max_items"] = int(dependency["max_items"])
        if binding["value_type"] not in {"integer", "number", "enum"}:
            continue
        target_node = graph.get(str(binding["node_id"]))
        if not isinstance(target_node, dict):
            raise SavedWorkflowImportError("可配置参数目标节点不存在")
        target_definition = _node_input_definition(
            object_info,
            target_node,
            str(binding["input_name"]),
        )
        value_type = str(binding["value_type"])
        source_value = (target_node.get("inputs") or {}).get(str(binding["input_name"]))
        if not _value_matches_parameter_definition(source_value, target_definition, value_type):
            raise SavedWorkflowImportError("可配置参数来源值不满足实时 object_info 合同")
        _assert_json_interoperable_integer_scalar(source_value)
        config = _definition_config(target_definition)
        resolution_policy = explicit_resolution_policy.get(key) or {}
        dimension_multiple = (
            _frozen_integer_multiple(target_definition)
            if (
                value_type == "integer"
                and key in {"image.width", "image.height"}
                and isinstance(resolution_strategy, dict)
                and resolution_strategy.get("mode") == "explicit_width_height"
            )
            else None
        )
        if (
            dimension_multiple is not None
            and isinstance(source_value, int)
            and source_value % dimension_multiple
        ):
            blockers.append({
                "code": "dimension_source_value_misaligned",
                "detail": (
                    f"{key} 的保存图来源值 {source_value} 不是实时 object_info "
                    f"INT step {dimension_multiple} 的倍数"
                ),
            })
        options = (
            list(_definition_options(target_definition))
            if value_type == "enum"
            else list(resolution_policy.get("options") or _safe_numeric_options(
                key, target_definition, value_type, source_value,
            ))
        )
        for option in options:
            _assert_json_interoperable_integer_scalar(option)
        recommended = resolution_policy.get("recommended")
        if recommended is None:
            recommended = _recommended_parameter_value(
                key,
                source_value,
                target_definition,
                value_type,
                options,
            )
        _assert_json_interoperable_integer_scalar(recommended)
        default = config.get("default")
        _assert_json_interoperable_integer_scalar(default)
        if not _value_matches_parameter_definition(default, target_definition, value_type):
            default = None
        schema_minimum = config.get("min")
        schema_maximum = config.get("max")
        schema_minimum = (
            schema_minimum
            if isinstance(schema_minimum, (int, float)) and not isinstance(schema_minimum, bool)
            else None
        )
        schema_maximum = (
            schema_maximum
            if isinstance(schema_maximum, (int, float)) and not isinstance(schema_maximum, bool)
            else None
        )
        schema_minimum = _json_interoperable_numeric_bound(schema_minimum)
        schema_maximum = _json_interoperable_numeric_bound(schema_maximum)
        minimum = min(options) if value_type in {"integer", "number"} and options else schema_minimum
        maximum = max(options) if value_type in {"integer", "number"} and options else schema_maximum
        definition.update({
            # Production compilation falls back to parameter.default when no
            # project/runtime override exists. Freeze the reviewed source
            # literal here; the object_info schema default remains separately
            # visible on the preview row and never silently changes behavior.
            "default": source_value,
            # Saved-workflow typed controls are reviewed canvas literals.  A
            # project builtin may not silently replace them in production;
            # explicit runtime overrides remain allowed. Smoke forms may use
            # smoke_recommended, but that suggestion is not an execution
            # fixture and is never part of the imported version identity.
            "use_source_default": True,
            "min": schema_minimum,
            "max": schema_maximum,
            **(
                {"multiple_of": dimension_multiple}
                if dimension_multiple is not None
                # Widget step is an editing increment, not a zero-anchored
                # divisibility contract (e.g. 5 + 17k). Non-dimension integer
                # lattices must be declared explicitly by the Projector.
                else {}
            ),
            **({"options": deepcopy(options)} if value_type == "enum" else {}),
        })
        selection_group = str(resolution_policy.get("selection_group") or "")
        if selection_group:
            definition["selection_group"] = selection_group
        row = {
            "semantic_key": key,
            "source_value": source_value,
            "recommended_safe_value": recommended,
            "default": default,
            "min": minimum,
            "max": maximum,
            "schema_min": schema_minimum,
            "schema_max": schema_maximum,
            "multiple_of": definition.get("multiple_of"),
            "alignment_modulus": None,
            "alignment_remainder": None,
            "options": deepcopy(options),
            "type": value_type,
            "selection_group": selection_group or None,
            "target": {
                "node_id": str(binding["node_id"]),
                "node_type": str(target_node.get("class_type") or ""),
                "input_name": str(binding["input_name"]),
            },
        }
        prior = configurable_by_key.get(key)
        if prior is not None and prior != row:
            raise SavedWorkflowImportError(
                "同一可配置 parameter semantic 指向多个不同来源值或目标",
            )
        if prior is None:
            configurable_by_key[key] = row
            configurable_parameters.append(row)
    required_dimension_multiple = None
    resolution_mode = (
        str(resolution_strategy.get("mode") or "")
        if isinstance(resolution_strategy, dict) else ""
    )
    if output_kind in {"image", "video"} and resolution_mode == "explicit_width_height":
        width_definition = parameters.get("image.width")
        height_definition = parameters.get("image.height")
        width_multiple = (
            width_definition.get("multiple_of")
            if isinstance(width_definition, dict) else None
        )
        height_multiple = (
            height_definition.get("multiple_of")
            if isinstance(height_definition, dict) else None
        )
        if (
            not isinstance(width_multiple, bool)
            and isinstance(width_multiple, int)
            and width_multiple > 0
            and height_multiple == width_multiple
        ):
            required_dimension_multiple = width_multiple
        else:
            blockers.append({
                "code": "required_dimension_multiple_unresolved",
                "detail": (
                    "显式宽高的实时 object_info 未形成一致的正整数 "
                    "width/height multiple_of 合同"
                ),
            })
    elif output_kind in {"image", "video"} and resolution_mode in {
        "aspect_megapixels", "source_image_derived",
    }:
        from studio.services.workflow_resolution import (
            infer_source_image_resolution, WorkflowResolutionError, SOURCE_DIMENSION_MULTIPLE,
        )
        try:
            if resolution_mode != "source_image_derived":
                raise WorkflowResolutionError(f"{resolution_mode} 尺寸策略尚无已注册的运行适配器")
            resolution_strategy = infer_source_image_resolution(graph, bindings, primary_id)
            required_dimension_multiple = SOURCE_DIMENSION_MULTIPLE
        except WorkflowResolutionError as exc:
            blockers.append({
                "code": "resolution_execution_adapter_unavailable",
                "detail": str(exc),
            })
    if not output_kind:
        blockers.append({
            "code": "unsupported_primary_output",
            "detail": "所选主输出不是平台支持的 image/video/audio/text 输出节点",
        })
    contract = {
        "schema_version": 3,
        "workflow_mode": "semantic",
        "compiler": {"id": "generic.comfy", "version": 1},
        "parameters": parameters,
        **({"resolution_strategy": resolution_strategy} if resolution_strategy else {}),
        **({
            "required_dimension_multiple": required_dimension_multiple,
        } if required_dimension_multiple is not None else {}),
        "outputs": {"primary": {
            "node_id": primary_id,
            "kind": output_kind,
            "required": True,
        }},
    }
    return (
        contract,
        canonical_binding_rows(bindings),
        canonical_requirement_rows(requirements),
        blockers,
        sorted(
            configurable_parameters,
            key=lambda row: (
                str(row["semantic_key"]),
                str(row["target"]["node_id"]),
                str(row["target"]["input_name"]),
            ),
        ),
    )


def _graph_class_rows(graph: dict[str, Any], class_type: str) -> list[tuple[str, dict]]:
    return sorted(
        (
            (str(node_id), node)
            for node_id, node in graph.items()
            if isinstance(node, dict) and node.get("class_type") == class_type
        ),
        key=lambda row: row[0],
    )


def _projector_role_nodes(
    projector: ProjectorContract,
    graph: dict[str, Any],
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    result: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for role, definition in projector.definition["node_roles"].items():
        rows = _graph_class_rows(graph, definition["node_type"])
        if definition["cardinality"] == "one" and len(rows) != 1:
            raise WorkflowCompilerError(f"projector node role {role} 未解析为唯一节点")
        if definition["cardinality"] == "many" and not rows:
            raise WorkflowCompilerError(f"projector node role {role} 没有匹配节点")
        result[role] = rows
    return result


def _apply_generic_projector_configuration(
    *,
    projector: ProjectorContract,
    graph: dict[str, Any],
    contract: dict[str, Any],
    bindings: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    configurable_parameters: list[dict[str, Any]],
) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]],
    list[dict[str, Any]], list[dict[str, Any]],
]:
    """Apply declarative role/parameter policy to the generic graph primitive."""

    contract = deepcopy(contract)
    bindings = deepcopy(bindings)
    blockers = deepcopy(blockers)
    configurable_parameters = deepcopy(configurable_parameters)
    role_nodes = _projector_role_nodes(projector, graph)
    parameters = contract.get("parameters") or {}
    binding_by_target = {
        (str(row.get("node_id") or ""), str(row.get("input_name") or "")): row
        for row in bindings
    }
    configurable_by_target = {
        (
            str((row.get("target") or {}).get("node_id") or ""),
            str((row.get("target") or {}).get("input_name") or ""),
        ): row
        for row in configurable_parameters
    }
    frozen_targets: set[tuple[str, str]] = set()
    for policy in projector.definition["parameter_policies"]:
        rows = role_nodes.get(policy["node_role"]) or []
        if len(rows) != 1:
            blockers.append({
                "code": "workflow_projector_parameter_target_unresolved",
                "detail": f"参数 {policy['semantic_key']} 的 node role 无法唯一解析",
            })
            continue
        node_id, node = rows[0]
        target = (node_id, policy["input_name"])
        binding = binding_by_target.get(target)
        definition = parameters.get(str((binding or {}).get("semantic_key") or ""))
        if binding is None or not isinstance(definition, dict):
            blockers.append({
                "code": "workflow_projector_parameter_target_unresolved",
                "detail": f"参数 {policy['semantic_key']} 未命中 {policy['node_role']}.{policy['input_name']}",
            })
            continue
        old_key = str(binding["semantic_key"])
        new_key = str(policy["semantic_key"])
        if new_key != old_key and new_key in parameters:
            blockers.append({
                "code": "workflow_projector_parameter_semantic_conflict",
                "detail": f"参数语义键 {new_key} 与自动投影结果冲突",
            })
            continue
        parameters.pop(old_key, None)
        parameters[new_key] = definition
        binding["semantic_key"] = new_key
        configurable = configurable_by_target.get(target)
        if configurable is not None:
            configurable["semantic_key"] = new_key
        policy_kind = str(policy["policy"])
        source_value = (node.get("inputs") or {}).get(policy["input_name"])
        if policy_kind == "fixed":
            fixed_value = policy.get("value")
            if source_value != fixed_value:
                blockers.append({
                    "code": "workflow_projector_fixed_value_mismatch",
                    "detail": f"{new_key} 来源值不等于 projector 冻结值",
                })
            definition["default"] = fixed_value
            definition["const"] = fixed_value
            definition["use_source_default"] = True
            if definition.get("type") in {"integer", "number"}:
                definition["min"] = fixed_value
                definition["max"] = fixed_value
            frozen_targets.add(target)
        elif policy_kind == "source_locked":
            definition["default"] = deepcopy(source_value)
            definition["use_source_default"] = True
            frozen_targets.add(target)
        else:
            definition.pop("use_source_default", None)
    for condition in projector.definition["detector"]["input_equals"]:
        nodes = _graph_class_rows(graph, condition["node_type"])
        if len(nodes) != 1:
            continue
        target = (nodes[0][0], condition["input_name"])
        binding = binding_by_target.get(target)
        if binding:
            definition = parameters[binding["semantic_key"]]
            definition.update(default=condition["value"], const=condition["value"], use_source_default=True)
            frozen_targets.add(target)
    configurable_parameters = [
        row for row in configurable_parameters
        if (
            str((row.get("target") or {}).get("node_id") or ""),
            str((row.get("target") or {}).get("input_name") or ""),
        ) not in frozen_targets
    ]
    contract["parameters"] = parameters
    from studio.services.workflow_parameter_ranges import apply_projector_parameter_ranges, parameter_range_preview, ParameterRangeError
    try:
        contract = apply_projector_parameter_ranges(contract, projector.definition["parameter_policies"])
        configurable_parameters = parameter_range_preview(configurable_parameters, contract, projector.definition["parameter_policies"])
    except ParameterRangeError as exc:
        blockers.append({"code": "workflow_projector_parameter_range_invalid", "detail": str(exc)})
    contract["workflow_adapters"] = deepcopy(projector.definition["adapters"])
    contract["projector_asset_slots"] = deepcopy(projector.definition["asset_slots"])
    policy = projector.definition["projection"]["config"].get("image_resolution")
    if policy is not None:
        from studio.services.workflow_resolution import configure_source_image_resolution, WorkflowResolutionError
        try:
            contract["resolution_strategy"] = configure_source_image_resolution(contract.get("resolution_strategy"), policy)
        except WorkflowResolutionError as exc:
            blockers.append({"code": "resolution_execution_adapter_unavailable", "detail": str(exc)})
    return (
        contract,
        canonical_binding_rows(bindings),
        canonical_requirement_rows(requirements),
        blockers,
        sorted(configurable_parameters, key=lambda row: str(row.get("semantic_key") or "")),
    )




def _suggested_import_contract(
    graph: dict,
    object_info: dict,
    *,
    primary_api_node_ids: list[str],
    model_dependencies: list[dict[str, Any]],
    asset_dependencies: list[dict[str, Any]],
    model_profile_version: ModelProfileVersion | None,
    projector: ProjectorContract | None = None,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    projector = projector or select_workflow_projector(graph)
    source_graph = graph
    group_plans = []
    if projector.engine_key == "generic.references":
        from studio.services.workflow_asset_groups import build_group_plans, expand_group_graph, AssetGroupError
        try:
            group_plans = build_group_plans(projector.definition, graph, object_info, model_profile_version)
            graph = expand_group_graph(graph, group_plans)
            asset_dependencies = _asset_dependency_candidates(graph, object_info)
        except (AssetGroupError, ValidationError) as exc:
            raise WorkflowProjectorError(str(exc)) from exc
    generic = _suggested_generic_import_contract(
        graph,
        object_info,
        primary_api_node_ids=primary_api_node_ids,
        model_dependencies=model_dependencies,
        asset_dependencies=asset_dependencies,
        model_profile_version=model_profile_version,
        projector=projector,
    )
    if projector.engine_key not in {"generic.fixed", "generic.references", "generic.classified"}:
        raise WorkflowProjectorError("工作流执行引擎未注册")
    contract, bindings, requirements, blockers, parameters = generic
    contract, bindings, requirements, blockers, parameters = _apply_generic_projector_configuration(
        projector=projector, graph=source_graph, contract=contract, bindings=bindings,
        requirements=requirements, blockers=blockers, configurable_parameters=parameters,
    )
    if group_plans:
        from studio.services.workflow_asset_groups import attach_group_bindings, AssetGroupError
        try:
            attach_group_bindings(contract, bindings, group_plans)
        except AssetGroupError as exc:
            raise WorkflowProjectorError(str(exc)) from exc
    if projector.engine_key == "generic.classified":
        from studio.services.workflow_classified_references import attach_plan, validate_live_ports, ClassifiedReferenceError
        try:
            if model_profile_version is None:
                raise ClassifiedReferenceError("分类参考素材须先选择模型版本以核对能力边界")
            projected = deepcopy(contract)
            attach_plan(projected, projector.definition, source_graph, verified_model_profile_specification(model_profile_version))
            validate_live_ports(source_graph, projected, object_info)
            contract = projected
        except (ClassifiedReferenceError, ValidationError) as exc:
            blockers.append({"code": "workflow_projector_contract_mismatch", "detail": str(exc)})
    return {**contract, "contract_projector": projector.identity}, bindings, requirements, blockers, parameters


def _is_prompt_literal(class_type: str, input_name: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = input_name.casefold().replace("-", "_")
    if normalized in {
        "text", "prompt", "positive", "negative", "positive_prompt",
        "negative_prompt", "system_prompt", "user_prompt", "instruction",
    }:
        return True
    return class_type in {"PrimitiveStringMultiline"} and normalized == "value"


def _is_output_path_literal(class_type: str, input_name: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = input_name.casefold().replace("-", "_")
    return normalized in {"filename_prefix", "output_prefix"} and (
        class_type.startswith("Save") or class_type == "VHS_VideoCombine"
    )


def _assert_safe_source_output_prefix(value: str) -> None:
    raw = value.replace("\\", "/")
    normalized = raw.strip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or len(normalized) > boundary_int(
            "security.workflow.output_prefix_max_characters"
        )
        or raw.startswith("/")
        or ":" in normalized
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SavedWorkflowUnsafeLiteral("canvas 输出路径不是安全相对前缀")


def _model_requirement_for_binding(item: dict[str, Any], binding: dict[str, Any]) -> dict:
    semantic_key = str(binding.get("semantic_key") or "")
    fallback_alias = (
        semantic_key[len("model."):-len(".filename")]
        if semantic_key.startswith("model.") and semantic_key.endswith(".filename")
        else ""
    )
    parameters = item.get("input_contract", {}).get("parameters", {})
    definition = parameters.get(semantic_key) if isinstance(parameters, dict) else None
    alias = str((definition or {}).get("requirement_alias") or fallback_alias)
    matches = [
        row for row in item.get("model_requirements") or []
        if isinstance(row, dict) and str(row.get("alias") or "") == alias
    ]
    if (
        len(matches) != 1
        or safe_model_relative_filename(
            str(matches[0].get("expected_filename") or "").strip(),
        ) is None
    ):
        raise SavedWorkflowUnsafeLiteral(
            f"{semantic_key} 缺少唯一且已审阅的 model requirement",
        )
    return matches[0]


def _neutral_binding_value(
    *,
    item: dict[str, Any],
    binding: dict[str, Any],
    definition,
) -> Any:
    value_type = str(binding.get("value_type") or "string")
    semantic_key = str(binding.get("semantic_key") or "")
    parameters = item.get("input_contract", {}).get("parameters", {})
    parameter = parameters.get(semantic_key) if isinstance(parameters, dict) else None
    reviewed_default = parameter.get("default") if isinstance(parameter, dict) else None
    if value_type == "string":
        if semantic_key == "output.prefix":
            return "autoprompt/managed-output"
        if semantic_key.startswith("prompt."):
            return "managed prompt"
        return "managed value"
    if value_type == "integer":
        if _value_matches_parameter_definition(reviewed_default, definition, "integer"):
            return reviewed_default
        raise SavedWorkflowUnsafeLiteral(f"{semantic_key} 缺少受审阅的合法整数默认值")
    if value_type == "number":
        if _value_matches_parameter_definition(reviewed_default, definition, "number"):
            return reviewed_default
        raise SavedWorkflowUnsafeLiteral(f"{semantic_key} 缺少受审阅的合法数值默认值")
    if value_type == "boolean":
        return reviewed_default if isinstance(reviewed_default, bool) else False
    if value_type == "enum":
        options = _definition_options(definition)
        if not options:
            raise SavedWorkflowUnsafeLiteral(f"{semantic_key} 没有可验证的受控枚举")
        if reviewed_default not in options:
            raise SavedWorkflowUnsafeLiteral(f"{semantic_key} 的受审阅默认值不在实时枚举中")
        return reviewed_default
    if value_type in {"asset_image", "asset_video", "asset_audio"}:
        return "__AUTOPROMPT_ASSET__:managed"
    if value_type == "model_artifact":
        normalized = safe_model_relative_filename(
            str(_model_requirement_for_binding(item, binding)["expected_filename"]).strip(),
        )
        if normalized is None:  # pragma: no cover - guarded above
            raise SavedWorkflowUnsafeLiteral(f"{semantic_key} 的模型文件名不安全")
        return normalized
    raise SavedWorkflowUnsafeLiteral(f"{semantic_key} 使用不支持的 value_type {value_type!r}")


def _sanitized_import_graph(
    graph: dict,
    object_info: dict,
    *,
    item: dict[str, Any],
) -> dict:
    sensitive = find_forbidden_secret_fields(graph)
    if sensitive:
        raise SavedWorkflowSecretField(
            "ComfyUI canvas 含禁止持久化的明文密钥字段",
        )
    from studio.services.workflow_asset_groups import expand_group_graph
    sanitized = expand_group_graph(graph, (item.get("input_contract") or {}).get("asset_groups", []))
    bindings = canonical_binding_rows(item.get("semantic_bindings") or [])
    binding_by_target = {
        (row["node_id"], row["input_name"]): row
        for row in bindings
    }
    for binding in bindings:
        if not str(binding.get("value_type") or "").startswith("asset_"):
            continue
        node = sanitized.get(str(binding["node_id"]))
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            raise SavedWorkflowUnsafeLiteral("asset binding 指向不存在的节点")
        input_name = str(binding["input_name"])
        if input_name not in inputs:
            if bool(binding.get("is_required", True)):
                raise SavedWorkflowUnsafeLiteral("必填 asset binding 指向缺失的节点输入")
            inputs[input_name] = "__AUTOPROMPT_ASSET__:managed"
    for node_id, node in sanitized.items():
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        for input_name, original in list(inputs.items()):
            if isinstance(original, list):
                continue
            definition = _node_input_definition(object_info, node, input_name)
            binding = binding_by_target.get((str(node_id), str(input_name)))
            is_output_path = _is_output_path_literal(class_type, input_name, original)
            is_upload = _is_upload_backed(definition) or (
                class_type in {"LoadImage", "LoadVideo", "LoadAudio"}
                and input_name.casefold() in {"image", "file", "video", "audio"}
            )
            is_prompt = _is_prompt_literal(class_type, input_name, original)
            if is_output_path:
                _assert_safe_source_output_prefix(original)
                if not binding or (
                    binding["semantic_key"] != "output.prefix"
                    or binding["value_type"] != "string"
                ):
                    raise SavedWorkflowUnsafeLiteral(
                        f"节点 {node_id} ({class_type}).{input_name} 必须精确绑定 output.prefix",
                    )
            if is_upload:
                expected_asset_type = {
                    "LoadImage": "asset_image",
                    "LoadVideo": "asset_video",
                    "LoadAudio": "asset_audio",
                }.get(class_type)
                if not binding or (
                    expected_asset_type is not None
                    and binding["value_type"] != expected_asset_type
                ) or not str((binding or {}).get("semantic_key") or "").startswith("asset."):
                    raise SavedWorkflowUnsafeLiteral(
                        f"节点 {node_id} ({class_type}).{input_name} 必须绑定匹配媒体类型的受管 asset semantic",
                    )
            if is_prompt and (
                not binding
                or not (
                    str(binding["semantic_key"]).startswith("prompt.")
                    or str(binding["semantic_key"]).startswith("parameter.")
                )
                or binding["value_type"] != "string"
            ):
                raise SavedWorkflowUnsafeLiteral(
                    f"节点 {node_id} ({class_type}).{input_name} 必须绑定 prompt/parameter string semantic",
                )
            if binding is not None:
                inputs[input_name] = _neutral_binding_value(
                    item=item,
                    binding=binding,
                    definition=definition,
                )
                continue
            if is_upload:
                raise SavedWorkflowUnsafeLiteral(
                    f"节点 {node_id} ({class_type}).{input_name} 必须绑定受管 asset semantic，"
                    "不能保存 canvas 本地素材名",
                )
            if is_prompt:
                raise SavedWorkflowUnsafeLiteral(
                    f"节点 {node_id} ({class_type}).{input_name} 必须绑定 prompt semantic，"
                    "不能保存 canvas 原始提示词",
                )
            if is_output_path:
                raise SavedWorkflowUnsafeLiteral(
                    f"节点 {node_id} ({class_type}).{input_name} 必须绑定 output.prefix，"
                    "不能保存 canvas 输出路径",
                )
    sensitive = find_forbidden_secret_fields(sanitized)
    if sensitive:
        raise SavedWorkflowSecretField(
            "中和后的 workflow 仍含禁止持久化的明文密钥字段",
        )
    return sanitized


def _preview_structural_graph(graph: dict, object_info: dict) -> dict:
    """Return a value-redacted graph for live schema validation.

    Preview needs the live node/input/link contract, but it must not feed raw
    prompts, userdata asset names or output paths into an exception formatter.
    Model selectors remain intact because their exact filenames are exposed on
    the dedicated, reviewed dependency surface; the contract validator itself
    is value-free for enum failures.
    """

    sensitive = find_forbidden_secret_fields(graph)
    if sensitive:
        raise SavedWorkflowSecretField(
            "ComfyUI canvas 含禁止持久化的明文密钥字段",
        )
    redacted = deepcopy(graph)
    for node_id, node in redacted.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        class_type = str(node.get("class_type") or "")
        for input_name, original in list(node["inputs"].items()):
            if isinstance(original, list):
                continue
            definition = _node_input_definition(object_info, node, str(input_name))
            is_upload = _is_upload_backed(definition) or (
                class_type in {"LoadImage", "LoadVideo", "LoadAudio"}
                and str(input_name).casefold() in {"image", "file", "video", "audio"}
            )
            if is_upload:
                node["inputs"][input_name] = "__AUTOPROMPT_ASSET__:managed"
                continue
            if _is_prompt_literal(class_type, str(input_name), original):
                node["inputs"][input_name] = "managed prompt"
                continue
            if _is_output_path_literal(class_type, str(input_name), original):
                _assert_safe_source_output_prefix(original)
                node["inputs"][input_name] = "autoprompt/managed-output"
    return redacted


def _safe_projected_error(exc: Exception, *, fallback: str) -> str:
    """Project only reviewed, value-free domain diagnostics to API responses."""

    if isinstance(exc, SavedWorkflowImportError):
        return {
            "source_changed": "ComfyUI 来源内容已变化，请重新扫描和预览",
            "source_already_classified": "该来源已按不同导入合同分类，请刷新后重试",
            "secret_field_forbidden": "工作流含禁止持久化的明文密钥字段",
            "unsafe_source_literal": "工作流含未受管的本地输入或输出路径",
            "conversion_contract_changed": "ComfyUI schema 或转换合同已变化，请重新预览",
            "workflow_structural_contract_changed": "工作流参数 Schema 或结构合同已变化，请重新预览",
            "incompatible_model_profile": "目标模型版本与来源 ComfyUI provider 不兼容",
            "model_dependency_unmanaged": "工作流模型依赖未被完整受管和证明",
            "source_deployment_changed": "来源 deployment 配置已变化，请重新扫描",
            "source_scope_changed": "ComfyUI 工作流来源范围已变化，请重新扫描",
            "workflow_output_contract_invalid": "工作流主输出合同无效或类型不匹配",
            "workflow_request_budget_exceeded": "请求超过已保存工作流同步处理预算",
            "workflow_contract_blocked": "工作流结构合同仍有未解决的导入阻断项",
            "workflow_candidate_required": "请先纳管候选来源，再生成平台执行合同草稿",
            "workflow_contract_draft_mismatch": "平台执行合同草稿已过期或与来源不一致，请重新计算",
        }.get(getattr(exc, "code", ""), fallback)
    if isinstance(
        exc,
        (ComfyUICanvasImportError, ComfyUIContractError, WorkflowBuildError, WorkflowCompilerError),
    ):
        return "ComfyUI 工作流结构不满足平台安全导入合同"
    return fallback


def _public_source_workflow_id(workflow: dict) -> str:
    raw = workflow.get("id") if isinstance(workflow, dict) else None
    if raw in (None, ""):
        return ""
    try:
        return str(uuid.UUID(str(raw)))
    except (TypeError, ValueError, AttributeError):
        # Preserve stable provenance correlation without exposing an arbitrary
        # root canvas identifier supplied by userdata.
        return hashlib.sha256(canonical_json_bytes(raw)).hexdigest()


def _public_conversion_report(report: dict) -> dict[str, Any]:
    dropped = report.get("dropped_nodes") if isinstance(report, dict) else None
    reason_counts = {"ui_only": 0, "bypassed": 0, "unreachable": 0, "other": 0}
    for row in dropped if isinstance(dropped, list) else []:
        reason = row.get("reason") if isinstance(row, dict) else None
        reason_counts[reason if reason in reason_counts else "other"] += 1

    def safe_count(key: str) -> int:
        value = report.get(key) if isinstance(report, dict) else 0
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    return {
        "schema_version": 1,
        "source_node_count": safe_count("source_node_count"),
        "api_node_count": safe_count("api_node_count"),
        "expanded_subgraph_count": safe_count("expanded_subgraph_count"),
        "bypassed_node_count": len(report.get("bypassed_nodes") or [])
        if isinstance(report, dict) and isinstance(report.get("bypassed_nodes"), list) else 0,
        "dropped_node_count": sum(reason_counts.values()),
        "dropped_reason_counts": reason_counts,
    }


def _model_dependency_candidates(
    graph: dict,
    object_info: dict,
    *,
    model_profile_version: ModelProfileVersion | None,
    projector: ProjectorContract | None = None,
) -> list[dict[str, Any]]:
    projector = projector or select_workflow_projector(graph)
    artifacts = list(
        ModelArtifact.objects.filter(profile_version=model_profile_version)
        if model_profile_version is not None else []
    )
    rows = []
    ordinal_by_role: dict[str, int] = {}
    for node_id, node in graph.items():
        class_type = str(node.get("class_type") or "") if isinstance(node, dict) else ""
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for input_name, value in inputs.items():
            if not isinstance(value, str) or not value:
                continue
            selector = projector_model_selector(
                class_type,
                input_name,
                catalog=[projector],
            )
            if selector is None:
                continue
            normalized_value = safe_model_relative_filename(value)
            if normalized_value is None:
                raise SavedWorkflowDependencyUnmanaged(
                    "工作流模型 selector 不是受控 models 相对文件名",
                )
            role, custom_folder, inventory_node_type, inventory_input_name = selector
            definition = _input_definition(object_info, class_type, input_name)
            choices = safe_model_inventory_options(definition)
            ordinal_by_role[role] = ordinal_by_role.get(role, 0) + 1
            alias = f"{role}_{ordinal_by_role[role]}"
            matches = [
                artifact for artifact in artifacts
                if artifact.role == role
                and safe_model_relative_filename(artifact.filename) == normalized_value
            ]
            rows.append({
                "node_id": str(node_id),
                "node_type": class_type,
                "input_name": input_name,
                "filename": normalized_value,
                "role": role,
                "suggested_alias": alias,
                "inventory_folder": custom_folder or DEFAULT_MODEL_FOLDER_BY_ROLE.get(role, ""),
                "inventory_node_type": inventory_node_type,
                "inventory_input_name": inventory_input_name,
                "available_in_object_info": normalized_value in choices,
                "artifact_matches": [{
                    "id": str(artifact.id),
                    "status": artifact.status,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "verified_snapshot": str(artifact.verified_snapshot_id or ""),
                } for artifact in matches],
            })
    return rows


def _validate_model_dependency_coverage(
    graph: dict,
    object_info: dict,
    *,
    item: dict[str, Any],
) -> None:
    profile = item["model_profile_version"]
    dependencies = _model_dependency_candidates(
        graph,
        object_info,
        model_profile_version=profile,
    )
    dependency_targets = {
        (row["node_id"], row["input_name"]): row
        for row in dependencies
    }
    bindings = canonical_binding_rows(item.get("semantic_bindings") or [])
    model_bindings = {
        (row["node_id"], row["input_name"]): row
        for row in bindings
        if str(row.get("value_type") or "") == "model_artifact"
        or str(row.get("semantic_key") or "").startswith("model.")
    }
    if set(model_bindings) != set(dependency_targets):
        missing = sorted(set(dependency_targets) - set(model_bindings))
        extra = sorted(set(model_bindings) - set(dependency_targets))
        raise SavedWorkflowDependencyUnmanaged(
            f"模型 selector binding 覆盖不完整；missing={missing}, extra={extra}",
        )
    artifact_ids = {
        str(row.get("artifact_id"))
        for row in item.get("model_requirements") or []
        if isinstance(row, dict) and row.get("artifact_id")
    }
    artifacts = {
        str(artifact.id): artifact
        for artifact in ModelArtifact.objects.filter(pk__in=artifact_ids)
    }
    used_aliases: set[str] = set()
    for target, dependency in dependency_targets.items():
        binding = model_bindings[target]
        if binding["value_type"] != "model_artifact":
            raise SavedWorkflowDependencyUnmanaged(
                f"节点 {target[0]}.{target[1]} 必须使用 model_artifact binding",
            )
        requirement = _model_requirement_for_binding(item, binding)
        semantic_key = str(binding["semantic_key"])
        fallback_alias = semantic_key[len("model."):-len(".filename")]
        parameters = item.get("input_contract", {}).get("parameters", {})
        definition = parameters.get(semantic_key) if isinstance(parameters, dict) else None
        alias = str((definition or {}).get("requirement_alias") or fallback_alias)
        used_aliases.add(alias)
        artifact = artifacts.get(str(requirement.get("artifact_id") or ""))
        expected_sha = str(requirement.get("expected_sha256") or "").casefold()
        expected_filename = safe_model_relative_filename(
            str(requirement.get("expected_filename") or ""),
        )
        artifact_filename = (
            safe_model_relative_filename(artifact.filename)
            if artifact is not None else None
        )
        if (
            not bool(requirement.get("is_required", True))
            or str(requirement.get("role") or "") != dependency["role"]
            or expected_filename != dependency["filename"]
            or artifact is None
            or artifact.profile_version_id != profile.id
            or artifact.role != dependency["role"]
            or artifact_filename != dependency["filename"]
            or artifact.size_bytes is None
            or artifact.size_bytes <= 0
            or len(expected_sha) != 64
            or expected_sha != str(artifact.sha256 or "").casefold()
        ):
            raise SavedWorkflowDependencyUnmanaged(
                f"节点 {target[0]}.{target[1]} 缺少同 profile 且有 size/hash 的 artifact 声明",
            )
        try:
            spec = artifact_inventory_spec(artifact)
        except ValueError as exc:
            raise SavedWorkflowDependencyUnmanaged(str(exc)) from exc
        if (
            spec.folder != dependency["inventory_folder"]
            or spec.node_type != dependency["inventory_node_type"]
            or spec.input_name != dependency["inventory_input_name"]
        ):
            raise SavedWorkflowDependencyUnmanaged(
                f"节点 {target[0]}.{target[1]} artifact inventory selector 不一致",
            )
    requirement_aliases = {
        str(row.get("alias") or "")
        for row in item.get("model_requirements") or []
        if isinstance(row, dict)
    }
    if requirement_aliases != used_aliases:
        raise SavedWorkflowDependencyUnmanaged(
            "model requirements 含未使用或缺失 alias",
        )


def _validate_saved_import_structural_contract(
    graph: dict,
    object_info: dict,
    *,
    item: dict[str, Any],
) -> None:
    contract = item.get("input_contract")
    parameters = contract.get("parameters") if isinstance(contract, dict) else None
    if not isinstance(parameters, dict) or not parameters:
        raise SavedWorkflowImportError("saved workflow 缺少参数结构合同")
    bindings = canonical_binding_rows(item.get("semantic_bindings") or [])
    dependencies = _asset_dependency_candidates(graph, object_info)
    grouped = {
        key: index < group["min_items"]
        for group in contract.get("asset_groups", [])
        for index, key in enumerate(group["semantic_keys"])
    }
    for dependency in dependencies:
        key = dependency["suggested_semantic_key"]
        if key in grouped:
            dependency["is_required"] = grouped[key]
            dependency["min_items"] = int(grouped[key])
    rows_by_key = {
        str(row["semantic_key"]): row
        for row in bindings
        if str(row.get("value_type") or "").startswith("asset_")
    }
    expected_keys = {str(row["suggested_semantic_key"]) for row in dependencies}
    if set(rows_by_key) != expected_keys:
        raise SavedWorkflowImportError(
            "asset semantic bindings 必须精确覆盖可达素材 node/input slot",
        )
    for dependency in dependencies:
        key = str(dependency["suggested_semantic_key"])
        definition = parameters.get(key)
        binding = rows_by_key[key]
        if (
            not isinstance(definition, dict)
            or definition.get("type") != dependency["value_type"]
            or definition.get("input_slot") != dependency["input_slot"]
            or definition.get("asset_category") != dependency["asset_category"]
            or definition.get("media_kind") != dependency["media_kind"]
            or definition.get("min_items") != dependency["min_items"]
            or definition.get("max_items") != dependency["max_items"]
            or bool(definition.get("required", True)) != dependency["is_required"]
            or bool(binding.get("is_required", True)) != dependency["is_required"]
        ):
            raise SavedWorkflowImportError(
                f"{key} 的 node/input slot、媒体类型或数量合同不一致",
            )


def _structural_contract_sha256(
    contract: dict[str, Any],
    bindings: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> str:
    return json_contract({
        "schema_version": 1,
        "input_contract": contract,
        "semantic_bindings": canonical_binding_rows(bindings),
        "model_requirements": canonical_requirement_rows(requirements),
    }, max_bytes=_workflow_graph_max_bytes()).sha256


def _source_path_imports(document: _SourceDocument, deployment: ModelDeployment):
    scope = comfyui_workflow_source_scope(deployment)
    return ComfyWorkflowSource.objects.select_related("workflow_version__template").filter(
        source_scope_schema_version=scope.schema_version,
        source_scope_sha256=scope.sha256,
        source_path_sha256=_source_path_sha256(document.source_path),
        # ComfyUI userdata paths are POSIX/NFC-normalized by the client, but
        # remain case-sensitive on the authoritative Linux deployment.
        source_path_key=document.source_path,
    )


def _source_imports(document: _SourceDocument, deployment: ModelDeployment):
    return _source_path_imports(document, deployment).filter(
        source_sha256=document.source_sha256,
    )


def _existing_source(
    document: _SourceDocument,
    deployment: ModelDeployment,
    *,
    import_contract_sha256: str,
):
    return _source_imports(document, deployment).filter(
        import_contract_sha256=import_contract_sha256,
    ).order_by("-created_at").first()


def _load_saved_workflow_preview_context(
    deployment: ModelDeployment,
    *,
    driver,
    source_path: str,
    expected_sha256: str,
    budget: _RequestBudget,
    document: _SourceDocument | None = None,
    object_info: dict[str, Any] | None = None,
) -> _SavedWorkflowPreviewContext:
    if document is None:
        document = _read_source(driver, deployment, source_path, budget=budget)
    _assert_expected_sha(document, expected_sha256)
    # Run over the entire UI canvas before output selection so an
    # unreachable/unfinished branch cannot hide a secret.
    _assert_safe_canvas_preflight(document.workflow)
    if object_info is None:
        object_info = _invoke_with_budget(
            driver, deployment, "object_info", {}, budget=budget,
        )
    if not isinstance(object_info, dict):
        raise SavedWorkflowImportError("object_info 响应无效")
    return _SavedWorkflowPreviewContext(
        deployment=deployment,
        document=document,
        object_info=object_info,
        object_info_schema_sha256=_object_info_sha256(object_info),
        output_candidates=_canvas_output_candidates(document.workflow, object_info),
    )


def _project_saved_workflow_preview_context(
    context: _SavedWorkflowPreviewContext,
    *,
    primary_output_node_ids: list[int | str],
    model_profile_version: ModelProfileVersion | None,
) -> dict[str, Any]:
    document = context.document
    object_info = context.object_info
    candidates = _supported_output_candidates(context)
    if not primary_output_node_ids:
        return {
            "schema_version": 1,
            "source_path": document.source_path,
            "source_sha256": document.source_sha256,
            "size_bytes": document.size_bytes,
            "source_workflow_id": _public_source_workflow_id(document.workflow),
            "primary_output_node_ids": [],
            "primary_api_node_ids": [],
            "output_candidates": candidates,
            "model_dependencies": [],
            "asset_dependencies": [],
            "conversion_report": {},
            "object_info_schema_sha256": context.object_info_schema_sha256,
            "converted_graph_sha256": "",
            "configurable_parameters": [],
            "parameter_schema_sha256": "",
            "structural_contract_sha256": "",
            "suggested_input_contract": {},
            "suggested_semantic_bindings": [],
            "suggested_model_requirements": [],
            "blockers": [{"code": "primary_output_required", "detail": "请选择主输出节点"}],
            "live_validate_requirements": [],
            "admission_tier": "candidate",
            "actionability": "select_primary_output",
            "recovery_actions": [{
                "code": "select_primary_output",
                "label": "选择一个由实时 object_info 证明的主输出节点",
            }],
            "contract_draft_id": "",
            "contract_draft_revision": 0,
        }
    try:
        converted = ui_workflow_to_api_graph(
            document.workflow,
            object_info,
            primary_output_node_ids=primary_output_node_ids,
        )
        validate_comfy_graph(converted.api_graph)
        structural_graph = _preview_structural_graph(converted.api_graph, object_info)
        # Source canvases can refer to an older Comfy userdata filename that is
        # no longer present in the current enum. Preview validates only the
        # safe structural shape of that reference and never returns its value;
        # batch import still requires exact managed-asset bindings and then
        # runs the strict contract with a managed sentinel.
        schema_blockers: list[dict[str, Any]] = []
        try:
            validate_comfyui_api_contract(
                structural_graph,
                object_info,
                allow_managed_asset_tokens=True,
            )
        except ComfyUIContractError as exc:
            # Candidate admission is already complete.  Schema interpretation
            # belongs to the contract-draft stage, so retain an unresolved
            # item instead of rejecting the immutable source candidate.
            schema_blockers.append({
                "code": "node_schema_unresolved",
                "detail": _safe_projected_error(
                    exc,
                    fallback="实时 object_info 尚不能完整解释该工作流节点合同",
                ),
                "phase": "live_validate",
            })
        projector = None
        projector_error = None
        try:
            projector = select_workflow_projector(converted.api_graph)
        except (WorkflowProjectorError, WorkflowCompilerError) as exc:
            projector_error = exc
        asset_dependencies = _asset_dependency_candidates(converted.api_graph, object_info)
        model_dependencies = (
            _model_dependency_candidates(
                converted.api_graph,
                object_info,
                model_profile_version=model_profile_version,
                projector=projector,
            )
            if projector is not None else []
        )
        try:
            if projector_error is not None:
                raise projector_error
            if projector is None:
                raise WorkflowProjectorError(
                    "当前图尚未解析出唯一的工作流合同方案",
                )
            (
                suggested_input_contract,
                suggested_semantic_bindings,
                suggested_model_requirements,
                suggestion_blockers,
                configurable_parameters,
            ) = _suggested_import_contract(
                converted.api_graph,
                object_info,
                primary_api_node_ids=list(converted.primary_output_node_ids),
                model_dependencies=model_dependencies,
                asset_dependencies=asset_dependencies,
                model_profile_version=model_profile_version,
                projector=projector,
            )
        except (WorkflowProjectorError, WorkflowCompilerError) as exc:
            # A safe candidate remains manageable even when no unique
            # projector/adapter can yet explain it. Preserve the generic
            # structural extraction as a non-executable draft and make the
            # unresolved projector decision explicit.
            (
                suggested_input_contract,
                suggested_semantic_bindings,
                suggested_model_requirements,
                suggestion_blockers,
                configurable_parameters,
            ) = _suggested_generic_import_contract(
                converted.api_graph,
                object_info,
                primary_api_node_ids=list(converted.primary_output_node_ids),
                model_dependencies=model_dependencies,
                asset_dependencies=asset_dependencies,
                model_profile_version=model_profile_version,
                projector=projector,
            )
            suggestion_blockers = [
                *suggestion_blockers,
                {
                    "code": getattr(exc, "code", "workflow_projector_unresolved"),
                    "detail": _safe_projected_error(
                        exc,
                        fallback="当前图尚未解析出唯一的工作流合同方案",
                    ),
                },
            ]
        suggestion_blockers = [*schema_blockers, *suggestion_blockers]
        live_validate_requirements = [
            row for row in suggestion_blockers
            if isinstance(row, dict) and row.get("phase") == "live_validate"
        ]
        blockers = [
            row for row in suggestion_blockers
            if not (isinstance(row, dict) and row.get("phase") == "live_validate")
        ]
        if model_profile_version is None:
            blockers.append({
                "code": "model_profile_selection_required",
                "detail": "尚未选择要冻结到工作流版本的精确模型版本",
            })
    except SavedWorkflowSecretField as exc:
        logger.warning(
            "saved Comfy workflow preview rejected by %s",
            type(exc).__name__,
        )
        raise SavedWorkflowImportError(
            _safe_projected_error(
                exc,
                fallback="ComfyUI 工作流结构不满足平台安全导入合同",
            ),
        ) from exc
    except (
        ComfyUICanvasImportError,
        WorkflowBuildError,
        ComfyUIContractError,
        SavedWorkflowImportError,
        WorkflowProjectorError,
        WorkflowCompilerError,
        LookupError,
        ValidationError,
    ) as exc:
        # The source already passed candidate-admission safety checks.  Keep a
        # persistent, non-executable contract draft so operators can see and
        # resolve interpretation gaps without losing the admitted source.
        code = getattr(exc, "code", None) or "workflow_contract_unresolved"
        detail = _safe_projected_error(
            exc,
            fallback="当前工作流尚不能生成完整的平台执行合同",
        )
        return {
            "schema_version": 1,
            "source_path": document.source_path,
            "source_sha256": document.source_sha256,
            "size_bytes": document.size_bytes,
            "source_workflow_id": _public_source_workflow_id(document.workflow),
            "primary_output_node_ids": [str(value) for value in primary_output_node_ids],
            "primary_api_node_ids": [],
            "output_candidates": candidates,
            "model_dependencies": [],
            "asset_dependencies": [],
            "conversion_report": {},
            "object_info_schema_sha256": context.object_info_schema_sha256,
            "converted_graph_sha256": "",
            "configurable_parameters": [],
            "parameter_schema_sha256": "",
            "structural_contract_sha256": "",
            "suggested_input_contract": {},
            "suggested_semantic_bindings": [],
            "suggested_model_requirements": [],
            "blockers": [{"code": str(code), "detail": detail}],
            "live_validate_requirements": [],
            "admission_tier": "candidate",
            "actionability": "candidate_only",
            "recovery_actions": [{
                "code": str(code),
                "label": "修复待处理合同项后重新生成平台执行合同草稿",
            }],
            "contract_draft_id": "",
            "contract_draft_revision": 0,
        }
    parameter_schema_sha256 = _parameter_schema_sha256(
        configurable_parameters,
    )
    structural_contract_sha256 = _structural_contract_sha256(
        suggested_input_contract,
        suggested_semantic_bindings,
        suggested_model_requirements,
    )
    projector = suggested_input_contract.get("contract_projector")
    dynamic_projection = (suggested_input_contract.get("workflow_adapters", {}).get("dynamic") or "none/v1") != "none/v1"
    blocker_actions = {
        "model_artifact_attestation_required": "证明目标模型版本所需文件",
        "model_inventory_missing": "在目标 ComfyUI 主机安装声明的模型文件后重新探测",
        "workflow_projector_contract_mismatch": "补齐模型能力合同或选择匹配的工作流合同方案",
        "workflow_projector_engine_unavailable": "部署包含该受审引擎的服务版本",
        "required_dimension_multiple_unresolved": (
            "在 ComfyUI 修正显式 width/height 整数输入，使两者的 "
            "object_info step 形成一致正数倍数；保存新 revision 后重新扫描"
        ),
        "resolution_execution_adapter_unavailable": (
            "核对主输出的尺寸来源链；支持显式宽高和受管 LoadImage 经 "
            "FluxKontextImageScale 驱动的图片尺寸。未知缩放/分支需受审适配器，"
            "不是重新登记模型即可修复；修改来源后重新生成合同草稿"
        ),
        "model_profile_selection_required": "选择精确模型版本后重新生成合同草稿",
    }
    recovery_actions = [
        {"code": code, "label": blocker_actions.get(code, "按阻断详情修复后重新计算合同")}
        for code in dict.fromkeys(
            str(row.get("code") or "workflow_contract_blocked")
            for row in [*blockers, *live_validate_requirements]
            if isinstance(row, dict)
        )
    ]
    return {
        "schema_version": 1,
        "source_path": document.source_path,
        "source_sha256": document.source_sha256,
        "size_bytes": document.size_bytes,
        "source_workflow_id": _public_source_workflow_id(document.workflow),
        # The request contract uses canvas ids. API ids are separate so a UI
        # cannot accidentally feed a renumbered id back into batch import.
        "primary_output_node_ids": [str(value) for value in primary_output_node_ids],
        "primary_api_node_ids": list(converted.primary_output_node_ids),
        "output_candidates": candidates,
        "model_dependencies": model_dependencies,
        "asset_dependencies": asset_dependencies,
        "conversion_report": _public_conversion_report(converted.conversion_report),
        "object_info_schema_sha256": context.object_info_schema_sha256,
        "converted_graph_sha256": _converted_graph_sha256(converted.api_graph),
        "configurable_parameters": configurable_parameters,
        "parameter_schema_sha256": parameter_schema_sha256,
        "structural_contract_sha256": structural_contract_sha256,
        "suggested_input_contract": suggested_input_contract,
        "suggested_semantic_bindings": suggested_semantic_bindings,
        "suggested_model_requirements": suggested_model_requirements,
        "blockers": blockers,
        "live_validate_requirements": live_validate_requirements,
        "admission_tier": (
            "candidate" if blockers else "projected_dynamic" if dynamic_projection
            else "generic_fixed"
        ),
        "actionability": "candidate_only" if blockers else "ready_for_executable_import",
        "recovery_actions": recovery_actions,
        "contract_draft_id": "",
        "contract_draft_revision": 0,
    }


def _freeze_contract_draft_graph(
    *,
    document: _SourceDocument,
    object_info: dict[str, Any],
    preview: dict[str, Any],
    model_profile_version: ModelProfileVersion | None,
) -> dict[str, Any]:
    if preview.get("actionability") != "ready_for_executable_import":
        return {}
    converted = ui_workflow_to_api_graph(
        document.workflow,
        object_info,
        primary_output_node_ids=preview["primary_output_node_ids"],
    )
    if _converted_graph_sha256(converted.api_graph) != preview["converted_graph_sha256"]:
        raise SavedWorkflowConversionChanged("合同草稿生成期间转换图发生变化")
    primary = preview["suggested_input_contract"].get("outputs", {}).get("primary", {})
    output_kind = str(primary.get("kind") or "")
    authoritative_item = {
        "model_profile_version": model_profile_version,
        "primary_output_node_ids": list(preview["primary_output_node_ids"]),
        "new_template": {"output_kind": output_kind},
        "input_contract": preview["suggested_input_contract"],
        "semantic_bindings": preview["suggested_semantic_bindings"],
        "model_requirements": preview["suggested_model_requirements"],
    }
    _validate_import_output_contract(
        item=authoritative_item,
        converted=converted,
        object_info=object_info,
    )
    _validate_model_dependency_coverage(
        converted.api_graph,
        object_info,
        item=authoritative_item,
    )
    sanitized = _sanitized_import_graph(
        converted.api_graph,
        object_info,
        item=authoritative_item,
    )
    _validate_saved_import_structural_contract(
        sanitized,
        object_info,
        item=authoritative_item,
    )
    validate_comfy_graph(sanitized)
    return sanitized


def _create_or_reuse_contract_draft(
    *,
    candidate_source: ComfyWorkflowSource,
    document: _SourceDocument,
    object_info: dict[str, Any],
    preview: dict[str, Any],
    model_profile_version: ModelProfileVersion | None,
    actor,
) -> ComfyWorkflowContractDraft:
    candidate = candidate_source
    if (
        candidate.import_kind != ComfyWorkflowSource.ImportKind.CANDIDATE
        or candidate.source_path_key != document.source_path
        or candidate.source_sha256 != document.source_sha256
        or candidate.import_contract_sha256
        != _classification_contract_sha256(ComfyWorkflowSource.ImportKind.CANDIDATE)
    ):
        raise SavedWorkflowCandidateRequired(
            "候选来源身份无效，请重新扫描并纳管",
        )
    try:
        frozen_source = verify_json_contract(
            candidate.source_document,
            schema_version=candidate.source_document_schema_version,
            size_bytes=candidate.source_document_size_bytes,
            sha256=candidate.source_document_sha256,
            max_bytes=_workflow_graph_max_bytes(),
        ).value
    except ValidationError as exc:
        raise SavedWorkflowContractDraftMismatch("候选工作流快照完整性校验失败") from exc
    if frozen_source != document.workflow:
        raise SavedWorkflowSourceChanged("候选快照与当前来源内容不一致，请重新扫描")
    projector_identity = (
        preview.get("suggested_input_contract", {}).get("contract_projector")
        if isinstance(preview.get("suggested_input_contract"), dict) else None
    )
    projector_version_id = (
        projector_identity.get("version_id") if isinstance(projector_identity, dict) else None
    )
    projector_definition_sha = (
        str(projector_identity.get("definition_sha256") or "")
        if isinstance(projector_identity, dict) else ""
    )
    projector_version = None
    if projector_version_id:
        try:
            projector_version = WorkflowProjectorVersion.objects.select_related(
                "projector",
            ).get(
                pk=projector_version_id,
                status=WorkflowProjectorVersion.Status.PUBLISHED,
            )
        except (WorkflowProjectorVersion.DoesNotExist, ValueError, TypeError):
            preview["blockers"] = [
                *preview.get("blockers", []),
                {
                    "code": "workflow_projector_version_unavailable",
                    "detail": "投影结果绑定的 published 工作流合同方案版本已失效",
                },
            ]
            preview["recovery_actions"] = [
                *preview.get("recovery_actions", []),
                {
                    "code": "workflow_projector_version_unavailable",
                    "label": "选择当前已发布方案并重新生成平台执行合同草稿",
                },
            ]
            preview["admission_tier"] = "candidate"
            preview["actionability"] = "candidate_only"
        if (
            projector_version is not None
            and projector_version.definition_sha256 != projector_definition_sha
        ):
            preview["blockers"] = [
                *preview.get("blockers", []),
                {
                    "code": "workflow_projector_definition_changed",
                    "detail": "工作流合同方案 definition SHA 已变化",
                },
            ]
            preview["admission_tier"] = "candidate"
            preview["actionability"] = "candidate_only"
            projector_version = None
    elif preview.get("actionability") == "ready_for_executable_import":
        preview["blockers"] = [
            *preview.get("blockers", []),
            {
                "code": "workflow_projector_unresolved",
                "detail": "可提升的合同草稿必须冻结唯一 published projector 版本",
            },
        ]
        preview["recovery_actions"] = [
            *preview.get("recovery_actions", []),
            {
                "code": "workflow_projector_unresolved",
                "label": "发布唯一匹配的工作流合同方案后重新生成草稿",
            },
        ]
        preview["admission_tier"] = "candidate"
        preview["actionability"] = "candidate_only"
    try:
        executable_graph = _freeze_contract_draft_graph(
            document=document,
            object_info=object_info,
            preview=preview,
            model_profile_version=model_profile_version,
        )
    except SavedWorkflowSecretField:
        raise
    except (
        ComfyUICanvasImportError,
        WorkflowBuildError,
        ComfyUIContractError,
        SavedWorkflowImportError,
        WorkflowProjectorError,
        WorkflowCompilerError,
        LookupError,
        ValidationError,
    ) as exc:
        code = getattr(exc, "code", None) or "workflow_contract_freeze_unresolved"
        preview["blockers"] = [
            *preview.get("blockers", []),
            {
                "code": str(code),
                "detail": _safe_projected_error(
                    exc,
                    fallback="当前工作流执行图尚不能安全冻结",
                ),
            },
        ]
        preview["recovery_actions"] = [
            *preview.get("recovery_actions", []),
            {
                "code": str(code),
                "label": "修复待处理合同项后重新生成平台执行合同草稿",
            },
        ]
        preview["admission_tier"] = "candidate"
        preview["actionability"] = "candidate_only"
        executable_graph = {}
    executable_graph_contract = json_contract(
        executable_graph,
        max_bytes=_workflow_graph_max_bytes(),
    )
    projection = {
        "schema_version": 1,
        "source_sha256": document.source_sha256,
        "primary_output_node_ids": list(preview.get("primary_output_node_ids") or []),
        "primary_api_node_ids": list(preview.get("primary_api_node_ids") or []),
        "object_info_schema_sha256": preview["object_info_schema_sha256"],
        "converted_graph_sha256": preview["converted_graph_sha256"],
        "parameter_schema_sha256": preview["parameter_schema_sha256"],
        "structural_contract_sha256": preview["structural_contract_sha256"],
        "input_contract": deepcopy(preview["suggested_input_contract"]),
        "semantic_bindings": deepcopy(preview["suggested_semantic_bindings"]),
        "model_requirements": deepcopy(preview["suggested_model_requirements"]),
        "configurable_parameters": deepcopy(preview["configurable_parameters"]),
        "executable_graph": executable_graph,
        "executable_graph_schema_version": executable_graph_contract.schema_version,
        "executable_graph_size_bytes": executable_graph_contract.size_bytes,
        "executable_graph_sha256": executable_graph_contract.sha256,
        "model_dependencies": deepcopy(preview["model_dependencies"]),
        "asset_dependencies": deepcopy(preview["asset_dependencies"]),
        "blockers": deepcopy(preview["blockers"]),
        "live_validate_requirements": deepcopy(
            preview["live_validate_requirements"],
        ),
        "recovery_actions": deepcopy(preview["recovery_actions"]),
        "admission_tier": preview["admission_tier"],
        "actionability": preview["actionability"],
        "model_profile_version_id": str(model_profile_version.id) if model_profile_version else "",
    }
    contract = json_contract(projection, max_bytes=_workflow_graph_max_bytes())
    with transaction.atomic():
        locked_candidate = ComfyWorkflowSource.objects.select_for_update().get(pk=candidate.pk)
        existing = ComfyWorkflowContractDraft.objects.filter(
            candidate_source=locked_candidate,
            projection_sha256=contract.sha256,
        ).order_by("-revision").first()
        if existing is not None:
            return existing
        latest_revision = (
            ComfyWorkflowContractDraft.objects.filter(candidate_source=locked_candidate)
            .order_by("-revision")
            .values_list("revision", flat=True)
            .first()
        ) or 0
        draft = ComfyWorkflowContractDraft(
            candidate_source=locked_candidate,
            revision=latest_revision + 1,
            primary_output_node_id=str(preview["primary_output_node_ids"][0]),
            model_profile_version=model_profile_version,
            projector_version=projector_version,
            object_info_schema_sha256=preview["object_info_schema_sha256"],
            converted_graph_sha256=preview["converted_graph_sha256"],
            structural_contract_sha256=preview["structural_contract_sha256"],
            admission_tier=preview["admission_tier"],
            actionability=preview["actionability"],
            created_by=actor,
        )
        assign_json_contract(
            draft,
            "projection",
            projection,
            max_bytes=_workflow_graph_max_bytes(),
        )
        draft.full_clean()
        try:
            with transaction.atomic():
                draft.save()
        except IntegrityError:
            return ComfyWorkflowContractDraft.objects.get(
                candidate_source=locked_candidate,
                projection_sha256=contract.sha256,
            )
    return draft


@security_boundary_resolution_cache()
@saved_workflow_directory_resolution_cache()
def preview_saved_workflow(
    deployment: ModelDeployment,
    *,
    source_path: str,
    expected_sha256: str,
    expected_source_scope_schema_version: int,
    expected_source_scope_sha256: str,
    primary_output_node_ids: list[int | str],
    model_profile_version: ModelProfileVersion | None = None,
    actor=None,
) -> dict[str, Any]:
    _assert_preview_target_profile(deployment, model_profile_version)
    candidate = ComfyWorkflowSource.objects.select_related(
        "source_deployment__profile_version__provider",
    ).filter(
        source_scope_schema_version=expected_source_scope_schema_version,
        source_scope_sha256=str(expected_source_scope_sha256 or "").casefold(),
        source_path_sha256=_source_path_sha256(source_path),
        source_path_key=source_path,
        source_sha256=str(expected_sha256 or "").casefold(),
        import_kind=ComfyWorkflowSource.ImportKind.CANDIDATE,
    ).order_by("-created_at").first()
    if candidate is None:
        raise SavedWorkflowCandidateRequired(
            "请先纳管该来源候选，再生成平台执行合同草稿",
        )
    if (
        candidate.source_deployment.profile_version.provider_id
        != deployment.profile_version.provider_id
    ):
        raise SavedWorkflowTargetProfileMismatch("候选来源与当前 ComfyUI provider 不一致")
    document = _verified_candidate_document(candidate)
    driver = _source_driver(deployment, required_operations={"object_info"})
    budget = _RequestBudget(
        max_items=1,
        max_bytes=saved_workflow_request_max_bytes(),
        max_seconds=saved_workflow_scan_max_seconds(),
        operation_label="工作流预览",
    )
    context = _load_saved_workflow_preview_context(
        deployment,
        driver=driver,
        source_path=source_path,
        expected_sha256=expected_sha256,
        budget=budget,
        document=document,
    )
    preview = _project_saved_workflow_preview_context(
        context,
        primary_output_node_ids=primary_output_node_ids,
        model_profile_version=model_profile_version,
    )
    budget.assert_available()
    if model_profile_version is not None and len(preview["primary_output_node_ids"]) == 1:
        selected_output_id = str(preview["primary_output_node_ids"][0])
        selected_output = next((
            row for row in preview["output_candidates"]
            if str(row.get("canvas_node_id") or "") == selected_output_id
        ), None)
        output_kind = str((selected_output or {}).get("suggested_output_kind") or "")
        if output_kind and model_profile_version.modality not in {
            output_kind,
            ModelProfileVersion.Modality.MULTIMODAL,
        }:
            raise SavedWorkflowTargetProfileMismatch(
                "预览目标 model profile 与所选主输出节点的模态不兼容",
            )
    if primary_output_node_ids:
        if actor is None or not getattr(actor, "is_authenticated", False):
            raise SavedWorkflowImportError("生成平台执行合同草稿必须记录操作人")
        draft = _create_or_reuse_contract_draft(
            candidate_source=candidate,
            document=context.document,
            object_info=context.object_info,
            preview=preview,
            model_profile_version=model_profile_version,
            actor=actor,
        )
        preview["contract_draft_id"] = str(draft.id)
        preview["contract_draft_revision"] = draft.revision
    return preview


def _source_key(scope_sha256: str, source_path: str) -> str:
    return hashlib.sha256(
        f"{scope_sha256}:{_source_path_sha256(source_path)}".encode("ascii"),
    ).hexdigest()


def _promoted_contract_primary_output_node_id(
    source: ComfyWorkflowSource | None,
    candidate_source: ComfyWorkflowSource | None,
) -> str:
    """Recover the reviewed canvas output frozen by an earlier promotion."""

    if source is None or candidate_source is None:
        return ""
    report = source.conversion_report if isinstance(source.conversion_report, dict) else {}
    reported_primary = str(report.get("primary_output_node_id") or "")
    if reported_primary:
        return reported_primary
    try:
        draft_id = uuid.UUID(str(report.get("contract_draft_id") or ""))
    except (TypeError, ValueError, AttributeError):
        return ""
    draft = ComfyWorkflowContractDraft.objects.filter(
        pk=draft_id,
        candidate_source=candidate_source,
    ).only("primary_output_node_id").first()
    return str(draft.primary_output_node_id or "") if draft else ""


def _supported_output_candidates(
    context: _SavedWorkflowPreviewContext,
) -> list[dict[str, Any]]:
    return [
        row for row in context.output_candidates
        if row.get("suggested_output_kind") in {"", "image", "video", "audio", "text"}
        and isinstance(context.object_info.get(str(row.get("node_type") or "")), dict)
        and context.object_info[str(row["node_type"])].get("output_node") is True
    ]


def _profile_candidate_evidence_codes(
    preview: dict[str, Any],
    *,
    target_deployment: ModelDeployment,
) -> list[str]:
    from studio.services.runtime_platform import verified_artifact_availability

    codes: set[str] = set()
    for dependency in preview.get("model_dependencies") or []:
        if not dependency.get("available_in_object_info"):
            codes.add("model_inventory_missing")
        artifact_ids = [
            row.get("id")
            for row in dependency.get("artifact_matches") or []
            if isinstance(row, dict) and row.get("id")
        ]
        valid = []
        fresh_artifact_seen = False
        for artifact in ModelArtifact.objects.select_related("verified_snapshot").filter(
            pk__in=artifact_ids,
        ):
            snapshot = artifact.verified_snapshot
            try:
                if snapshot is None:
                    raise ValidationError("artifact 缺少已验证快照")
                verified_artifact_availability(
                    snapshot=snapshot,
                    deployment=target_deployment,
                    artifact=artifact,
                )
                fresh_artifact_seen = True
                spec = artifact_inventory_spec(artifact)
                if (
                    spec.folder != dependency.get("inventory_folder")
                    or spec.node_type != dependency.get("inventory_node_type")
                    or spec.input_name != dependency.get("inventory_input_name")
                ):
                    continue
                valid.append(artifact)
            except ValidationError:
                continue
            except ValueError:
                fresh_artifact_seen = True
                continue
        if len(valid) == 0:
            codes.add(
                "model_artifact_inventory_mismatch"
                if fresh_artifact_seen
                else "model_artifact_attestation_required"
            )
        elif len(valid) > 1:
            codes.add("model_artifact_ambiguous")
    return sorted(codes)


def _evaluate_profile_candidates(
    context: _SavedWorkflowPreviewContext,
    *,
    outputs: list[dict[str, Any]],
    profiles: list[ModelProfileVersion],
    source_deployments: list[ModelDeployment],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    deployments_by_profile: dict[str, list[ModelDeployment]] = {}
    for candidate_deployment in source_deployments:
        deployments_by_profile.setdefault(
            str(candidate_deployment.profile_version_id), [],
        ).append(candidate_deployment)
    candidate_rows: list[dict[str, Any]] = []
    eligible_combinations: list[dict[str, Any]] = []
    for profile in profiles:
        rejection_codes: set[str] = set()
        live_validate_rejection_codes: set[str] = set()
        if profile.status != ModelProfileVersion.Status.PUBLISHED:
            live_validate_rejection_codes.add("model_profile_not_published")
        target_deployments = deployments_by_profile.get(str(profile.id), [])
        matching_outputs = [
            output for output in outputs
            if profile.modality in {
                str(output.get("suggested_output_kind") or ""),
                ModelProfileVersion.Modality.MULTIMODAL,
            }
        ]
        if not matching_outputs:
            rejection_codes.add("model_profile_modality_mismatch")
        profile_matches: list[dict[str, Any]] = []
        projected_failures: set[str] = set()
        if not rejection_codes:
            for output in matching_outputs:
                try:
                    preview = _project_saved_workflow_preview_context(
                        context,
                        primary_output_node_ids=[output["canvas_node_id"]],
                        model_profile_version=profile,
                    )
                except (SavedWorkflowImportError, ValidationError):
                    projected_failures.add("workflow_profile_projection_failed")
                    continue
                output_codes = {
                    str(row.get("code") or "workflow_contract_blocked")
                    for row in preview.get("blockers") or []
                    if isinstance(row, dict)
                }
                if not preview.get("model_dependencies"):
                    output_codes.add("model_identity_evidence_missing")
                if output_codes:
                    projected_failures.update(output_codes)
                    continue
                evidence_failures: set[str] = set()
                matched_target = None
                if not target_deployments:
                    evidence_failures.add("compatible_deployment_missing")
                else:
                    for target_deployment in target_deployments:
                        evidence_codes = _profile_candidate_evidence_codes(
                            preview,
                            target_deployment=target_deployment,
                        )
                        if not evidence_codes:
                            matched_target = target_deployment
                            evidence_failures.clear()
                            break
                        evidence_failures.update(evidence_codes)
                if profile.status != ModelProfileVersion.Status.PUBLISHED:
                    evidence_failures.add("model_profile_not_published")
                live_validate_rejection_codes.update(evidence_failures)
                match = {
                    "profile": profile,
                    "output": output,
                    "preview": preview,
                    "target_deployment": matched_target,
                    "live_validate_ready": (
                        matched_target is not None and not evidence_failures
                    ),
                    "live_validate_rejection_codes": sorted(evidence_failures),
                }
                profile_matches.append(match)
                eligible_combinations.append(match)
        if not profile_matches:
            rejection_codes.update(projected_failures)
        representative_target = (
            profile_matches[0]["target_deployment"]
            if profile_matches and profile_matches[0]["target_deployment"] is not None
            else (target_deployments[0] if target_deployments else None)
        )
        candidate_rows.append({
            "model_profile_version_id": str(profile.id),
            "profile_key": profile.profile_key,
            "version": profile.version,
            "status": profile.status,
            "modality": profile.modality,
            "target_deployment_id": (
                str(representative_target.id) if representative_target else ""
            ),
            "eligible": bool(profile_matches),
            "live_validate_ready": any(
                bool(match["live_validate_ready"]) for match in profile_matches
            ),
            "eligible_output_node_ids": sorted({
                str(match["output"]["canvas_node_id"])
                for match in profile_matches
            }, key=int),
            "rejection_codes": sorted(rejection_codes),
            "live_validate_rejection_codes": sorted(
                live_validate_rejection_codes,
            ),
        })
    return candidate_rows, eligible_combinations


def _candidate_failure_summary(candidate_rows: list[dict]) -> list[str]:
    """Keep non-dominated diagnostic sets; retain every candidate's full evidence.

    A wrong model's missing files/modality must not mask the only blocker on an
    otherwise exact model. This changes explanation only, never eligibility.
    """
    rows = [row for row in candidate_rows if "model_profile_modality_mismatch" not in row["rejection_codes"]]
    failures = [set(row["rejection_codes"]) for row in (rows or candidate_rows) if row["rejection_codes"]]
    minimal = [codes for codes in failures if not any(other < codes for other in failures)]
    return sorted(set().union(*minimal)) if minimal else ["compatible_model_profile_missing"]


def _candidate_failure_actions(reason_codes: list[str], candidate_rows: list[dict]) -> list[dict]:
    if not set(reason_codes) & {
        "model_artifact_contract_required", "model_artifact_ambiguous", "model_identity_evidence_missing",
        "compatible_model_profile_missing", "model_profile_modality_mismatch",
    }:
        return [{"code": "review_workflow_contract"}]
    return [{"code": "attest_or_publish_model_profile" if any(row["status"] == "draft" for row in candidate_rows) else "register_model_profile"}]


def _template_suggestion(
    document: _SourceDocument,
    *,
    scope_sha256: str,
    output_kind: str,
    existing_template: WorkflowTemplate | None,
) -> dict[str, Any]:
    if existing_template is not None:
        return {
            "use_existing": True,
            "template_id": str(existing_template.id),
            "name": existing_template.name,
            "slug": existing_template.slug,
            "output_kind": existing_template.output_kind,
        }
    basename = Path(document.source_path).name
    name = re.sub(r"\.json$", "", basename, flags=re.IGNORECASE).strip()
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", name).strip()[:160] or "Saved workflow"
    stem = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if not stem or not stem[0].isalpha():
        stem = "saved-workflow"
    slug = stem[:72].rstrip("-")
    if WorkflowTemplate.objects.filter(owner__isnull=True, slug=slug, version=1).exists():
        suffix = f"-{scope_sha256[:8]}-{document.source_sha256[:8]}"
        slug = f"{slug[:100-len(suffix)].rstrip('-')}{suffix}"
    return {
        "use_existing": False,
        "template_id": "",
        "name": name,
        "slug": slug,
        "output_kind": output_kind,
    }


def _discover_candidate_without_analysis(
    deployment: ModelDeployment,
    document: _SourceDocument,
    *,
    source_scope_schema_version: int,
    source_scope_sha256: str,
    analysis_error: Exception,
) -> dict[str, Any]:
    """Return an admissible source row even when object_info is unavailable."""

    import_kind = (
        ComfyWorkflowSource.ImportKind.EXECUTION_SNAPSHOT
        if _TASK_FILENAME.search(document.source_path)
        else ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION
    )
    source_rows = list(_source_imports(document, deployment).order_by("-created_at"))
    candidate = next((
        row for row in source_rows
        if row.import_kind == ComfyWorkflowSource.ImportKind.CANDIDATE
    ), None)
    existing = next((
        row for row in source_rows
        if row.import_kind != ComfyWorkflowSource.ImportKind.CANDIDATE
    ), None)
    version = existing.workflow_version if existing else None
    code = getattr(analysis_error, "code", "workflow_analysis_unavailable")
    message = _safe_projected_error(
        analysis_error,
        fallback="实时 object_info 暂不可用；候选仍可纳管，稍后重试合同分析",
    )[:500]
    return {
        "source_scope_schema_version": source_scope_schema_version,
        "source_scope_sha256": source_scope_sha256,
        "source_key": _source_key(source_scope_sha256, document.source_path),
        "source_deployment_id": str(deployment.id),
        "source_provider_id": str(deployment.profile_version.provider_id),
        "source_runtime_host_id": str(deployment.runtime_host_id or ""),
        "source_path": document.source_path,
        "source_sha256": document.source_sha256,
        "size_bytes": document.size_bytes,
        "source_workflow_id": _public_source_workflow_id(document.workflow),
        "suggested_import_kind": import_kind,
        "candidate_managed": candidate is not None,
        "candidate_source_id": str(candidate.id) if candidate else "",
        "imported": existing is not None,
        "source_id": str(existing.id) if existing else "",
        "workflow_version_id": str(version.id) if version else "",
        "template_id": str(version.template_id) if version else "",
        "contract_upgrade_required": False,
        "error_code": "",
        "error_message": "",
        "analysis_status": "blocked",
        "analysis_code": str(code),
        "analysis_message": message,
        "admission_tier": "candidate",
        "actionability": "candidate_only",
        "recovery_actions": [{
            "code": "retry_object_info",
            "label": "恢复 ComfyUI object_info 后重新生成平台执行合同草稿",
        }],
        "match_status": "blocked",
        "match_reason_codes": ["workflow_analysis_unavailable"],
        "match_actions": [{"code": "retry_object_info"}],
        "model_profile_candidates": [],
        "matched_model_profile_version_id": "",
        "matched_target_deployment_id": "",
        "matched_primary_output_node_id": "",
        "matched_output_kind": "",
        "suggested_template": None,
        "preview": None,
        "output_candidates": [],
    }


def _discover_saved_workflow_item(
    context: _SavedWorkflowPreviewContext,
    *,
    source_scope_schema_version: int,
    source_scope_sha256: str,
    source_deployments: list[ModelDeployment],
    profiles: list[ModelProfileVersion],
) -> dict[str, Any]:
    deployment = context.deployment
    document = context.document
    import_kind = (
        ComfyWorkflowSource.ImportKind.EXECUTION_SNAPSHOT
        if _TASK_FILENAME.search(document.source_path)
        else ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION
    )
    all_source_rows = list(_source_imports(document, deployment).order_by("-created_at"))
    candidate_source = next((
        row for row in all_source_rows
        if row.import_kind == ComfyWorkflowSource.ImportKind.CANDIDATE
    ), None)
    source_rows = [
        row for row in all_source_rows
        if row.import_kind != ComfyWorkflowSource.ImportKind.CANDIDATE
    ]
    exact_targets = {
        (
            row.import_kind,
            str(row.workflow_version.template_id or "")
            if row.workflow_version_id else "",
            str(row.workflow_version.model_profile_version_id or "")
            if row.workflow_version_id else "",
        )
        for row in source_rows
    }
    existing = source_rows[0] if source_rows else None
    version = existing.workflow_version if existing else None
    path_sources = list(ComfyWorkflowSource.objects.select_related(
        "workflow_version__template",
    ).filter(
        source_scope_schema_version=source_scope_schema_version,
        source_scope_sha256=source_scope_sha256,
        source_path_sha256=_source_path_sha256(document.source_path),
        source_path_key=document.source_path,
        workflow_version__isnull=False,
    ).order_by("-created_at"))
    previous = path_sources[0] if path_sources else None
    historical_template_ids = {
        row.workflow_version.template_id for row in path_sources
        if row.workflow_version_id
    }
    version_contract = version.input_contract if version and isinstance(version.input_contract, dict) else {}
    projector_contract_upgrade_required = False
    if version is not None:
        try:
            projector_contract_upgrade_required = (
                validate_frozen_workflow_projector_contract(version_contract) is None
            )
        except (WorkflowProjectorError, ValidationError):
            projector_contract_upgrade_required = True
    contract_upgrade_required = bool(
        version
        and (
            int(version_contract.get("schema_version") or 0) < 3
            or "smoke_inputs" in version_contract
            or "smoke_asset_bindings" in version_contract
            or projector_contract_upgrade_required
        )
    )
    base = {
        "source_scope_schema_version": source_scope_schema_version,
        "source_scope_sha256": source_scope_sha256,
        "source_key": _source_key(source_scope_sha256, document.source_path),
        "source_deployment_id": str(deployment.id),
        "source_provider_id": str(deployment.profile_version.provider_id),
        "source_runtime_host_id": str(deployment.runtime_host_id or ""),
        "source_path": document.source_path,
        "source_sha256": document.source_sha256,
        "size_bytes": document.size_bytes,
        "source_workflow_id": _public_source_workflow_id(document.workflow),
        "suggested_import_kind": import_kind,
        "candidate_managed": candidate_source is not None,
        "candidate_source_id": str(candidate_source.id) if candidate_source else "",
        "imported": existing is not None,
        "source_id": str(existing.id) if existing else "",
        "workflow_version_id": str(version.id) if version else "",
        "template_id": str(version.template_id) if version else (
            str(previous.workflow_version.template_id) if previous else ""
        ),
        "contract_upgrade_required": contract_upgrade_required,
        "error_code": "",
        "error_message": "",
        "analysis_status": "complete",
        "analysis_code": "",
        "analysis_message": "",
        "admission_tier": "candidate",
        "actionability": "candidate_only",
        "recovery_actions": [],
        "match_status": "unmatched",
        "match_reason_codes": [],
        "match_actions": [],
        "model_profile_candidates": [],
        "matched_model_profile_version_id": "",
        "matched_target_deployment_id": "",
        "matched_primary_output_node_id": "",
        "matched_output_kind": "",
        "suggested_template": None,
        "preview": None,
    }
    if len(exact_targets) > 1:
        return {
            **base,
            "match_status": "blocked",
            "match_reason_codes": ["source_provenance_conflict"],
            "match_actions": [{"code": "review_source_provenance"}],
        }
    if existing is not None and existing.import_kind != import_kind:
        return {
            **base,
            "match_status": "blocked",
            "match_reason_codes": ["source_classification_conflict"],
            "match_actions": [{"code": "review_source_provenance"}],
        }
    if len(historical_template_ids) > 1:
        return {
            **base,
            "match_status": "blocked",
            "match_reason_codes": ["source_path_template_conflict"],
            "match_actions": [{"code": "review_source_provenance"}],
        }
    if existing is not None and not contract_upgrade_required:
        if version is not None:
            try:
                _assert_reusable_workflow_version(version)
            except SavedWorkflowSourceConflict:
                return {
                    **base,
                    "match_status": "blocked",
                    "match_reason_codes": ["historical_workflow_not_reusable"],
                    "match_actions": [{"code": "review_source_provenance"}],
                }
        output_candidates = _supported_output_candidates(context)
        matched_output_kind = version.template.output_kind if version else ""
        matched_primary_output_node_id = _promoted_contract_primary_output_node_id(
            existing,
            candidate_source,
        )
        output_candidate_ids = {
            str(row.get("canvas_node_id") or "") for row in output_candidates
        }
        if matched_primary_output_node_id not in output_candidate_ids:
            compatible_outputs = [
                row for row in output_candidates
                if str(row.get("suggested_output_kind") or "") in {
                    "", matched_output_kind,
                }
            ]
            matched_primary_output_node_id = (
                str(compatible_outputs[0].get("canvas_node_id") or "")
                if len(compatible_outputs) == 1 else ""
            )
        target_deployments = [
            row for row in source_deployments
            if version is not None
            and row.profile_version_id == version.model_profile_version_id
        ]
        frozen_profile = version.model_profile_version if version else None
        matched_target = target_deployments[0] if target_deployments else None
        live_validate_rejection_codes: list[str] = []
        if frozen_profile is not None:
            if frozen_profile.status != ModelProfileVersion.Status.PUBLISHED:
                live_validate_rejection_codes.append("model_profile_not_published")
            if matched_target is None:
                live_validate_rejection_codes.append("compatible_deployment_missing")
        model_profile_candidates = []
        if frozen_profile is not None and matched_primary_output_node_id:
            # An exact imported source already froze this profile/output pair.
            # Return that same evidence row so the client can verify the
            # matched result without re-running admission or guessing a model.
            model_profile_candidates.append({
                "model_profile_version_id": str(frozen_profile.id),
                "profile_key": frozen_profile.profile_key,
                "version": frozen_profile.version,
                "status": frozen_profile.status,
                "modality": frozen_profile.modality,
                "target_deployment_id": (
                    str(matched_target.id) if matched_target else ""
                ),
                "eligible": True,
                "live_validate_ready": not live_validate_rejection_codes,
                "eligible_output_node_ids": [matched_primary_output_node_id],
                "rejection_codes": [],
                "live_validate_rejection_codes": live_validate_rejection_codes,
            })
        return {
            **base,
            "match_status": "matched",
            "match_reason_codes": [
                "exact_provenance_match",
                *(
                    []
                    if version is None or not live_validate_rejection_codes
                    else ["live_validate_prerequisites_pending"]
                ),
            ],
            "matched_model_profile_version_id": (
                str(version.model_profile_version_id) if version else ""
            ),
            "matched_target_deployment_id": (
                str(matched_target.id) if matched_target else ""
            ),
            "matched_primary_output_node_id": matched_primary_output_node_id,
            "matched_output_kind": matched_output_kind,
            "admission_tier": (
                "projected_dynamic"
                if version and version.input_contract.get("workflow_adapters", {}).get("dynamic", "none/v1") != "none/v1"
                else "generic_fixed"
            ),
            "actionability": "ready_for_executable_import",
            "suggested_template": _template_suggestion(
                document,
                scope_sha256=source_scope_sha256,
                output_kind=version.template.output_kind if version else "image",
                existing_template=version.template if version else None,
            ),
            "output_candidates": output_candidates,
            "model_profile_candidates": model_profile_candidates,
        }
    if import_kind == ComfyWorkflowSource.ImportKind.EXECUTION_SNAPSHOT:
        linked_workflow, linked_task = _linked_execution_snapshot(document)
        return {
            **base,
            "match_status": "matched" if linked_task else "unmatched",
            "match_reason_codes": [
                "execution_snapshot_linked" if linked_task else "execution_snapshot_unlinked"
            ],
            "match_actions": [] if linked_task else [{"code": "review_execution_snapshot"}],
        }

    outputs = _supported_output_candidates(context)
    if not outputs:
        return {
            **base,
            "match_status": "blocked",
            "match_reason_codes": ["supported_primary_output_missing"],
            "match_actions": [{"code": "fix_in_comfyui"}],
        }
    if any(not str(row.get("suggested_output_kind") or "") for row in outputs):
        return {
            **base,
            "analysis_status": "blocked",
            "analysis_code": "workflow_projector_unresolved",
            "analysis_message": "主输出节点尚无 published Projector 可解释媒体类型",
            "match_status": "blocked",
            "match_reason_codes": ["workflow_projector_unresolved"],
            "match_actions": [{"code": "publish_workflow_projector"}],
            "recovery_actions": [{
                "code": "publish_workflow_projector",
                "label": "发布可解释该图族与主输出的工作流合同方案",
            }],
            "output_candidates": outputs,
        }
    historical_template = previous.workflow_version.template if previous else None
    if historical_template is not None and not historical_template.is_active:
        return {
            **base,
            "match_status": "blocked",
            "match_reason_codes": ["historical_template_incompatible"],
            "match_actions": [{"code": "review_source_provenance"}],
            "output_candidates": outputs,
        }
    eligible_outputs = (
        [
            output for output in outputs
            if str(output.get("suggested_output_kind") or "")
            == historical_template.output_kind
        ]
        if historical_template is not None else outputs
    )
    if not eligible_outputs:
        return {
            **base,
            "match_status": "blocked",
            "match_reason_codes": ["historical_template_incompatible"],
            "match_actions": [{"code": "review_source_provenance"}],
            "output_candidates": outputs,
        }
    candidate_rows, eligible_combinations = _evaluate_profile_candidates(
        context,
        outputs=eligible_outputs,
        profiles=profiles,
        source_deployments=source_deployments,
    )
    if len(eligible_combinations) == 1:
        combination = eligible_combinations[0]
        output = combination["output"]
        output_kind = str(output["suggested_output_kind"])
        profile = combination["profile"]
        target_deployment = combination["target_deployment"]
        suggestion = _template_suggestion(
            document,
            scope_sha256=source_scope_sha256,
            output_kind=output_kind,
            existing_template=historical_template,
        )
        return {
            **base,
            "match_status": "matched",
            "match_reason_codes": [
                "single_contract_compatible_combination",
                *(
                    [] if combination["live_validate_ready"]
                    else ["live_validate_prerequisites_pending"]
                ),
            ],
            "model_profile_candidates": candidate_rows,
            "matched_model_profile_version_id": str(profile.id),
            "matched_target_deployment_id": (
                str(target_deployment.id) if target_deployment else ""
            ),
            "matched_primary_output_node_id": str(output["canvas_node_id"]),
            "matched_output_kind": output_kind,
            "suggested_template": suggestion,
            "preview": combination["preview"],
            "admission_tier": combination["preview"]["admission_tier"],
            "actionability": combination["preview"]["actionability"],
            "recovery_actions": combination["preview"]["recovery_actions"],
            "output_candidates": eligible_outputs,
        }
    if len(eligible_outputs) > 1:
        actions = []
        reason_codes = []
        eligible_output_ids = {
            str(row["output"]["canvas_node_id"])
            for row in eligible_combinations
        }
        eligible_profile_ids = {
            str(row["profile"].id)
            for row in eligible_combinations
        }
        known_output = next((
            output for output in eligible_outputs
            if str(output["canvas_node_id"]) in eligible_output_ids
        ), None) if len(eligible_output_ids) == 1 else None
        if len(eligible_output_ids) > 1 or not eligible_combinations:
            reason_codes.append("multiple_primary_outputs")
            actions.append({"code": "select_primary_output"})
        if len(eligible_profile_ids) > 1:
            reason_codes.append("multiple_model_profile_matches")
            actions.append({"code": "select_model_profile"})
        if not eligible_combinations:
            candidate_failures = _candidate_failure_summary(candidate_rows)
            reason_codes.extend(candidate_failures)
            actions.extend(_candidate_failure_actions(candidate_failures, candidate_rows))
        if not actions:
            reason_codes.append("workflow_match_requires_review")
            actions.append({"code": "select_model_profile"})
        return {
            **base,
            "match_status": "ambiguous",
            "match_reason_codes": reason_codes,
            "match_actions": actions,
            "output_candidates": eligible_outputs,
            "model_profile_candidates": candidate_rows,
            "matched_primary_output_node_id": (
                str(known_output["canvas_node_id"]) if known_output else ""
            ),
            "matched_output_kind": (
                str(known_output["suggested_output_kind"]) if known_output else ""
            ),
            "suggested_template": (
                _template_suggestion(
                    document,
                    scope_sha256=source_scope_sha256,
                    output_kind=str(known_output["suggested_output_kind"]),
                    existing_template=historical_template,
                )
                if known_output else None
            ),
        }
    output = eligible_outputs[0]
    output_kind = str(output["suggested_output_kind"])
    suggestion = _template_suggestion(
        document,
        scope_sha256=source_scope_sha256,
        output_kind=output_kind,
        existing_template=historical_template,
    )
    if len(eligible_combinations) > 1:
        return {
            **base,
            "match_status": "ambiguous",
            "match_reason_codes": ["multiple_model_profile_matches"],
            "match_actions": [{"code": "select_model_profile"}],
            "model_profile_candidates": candidate_rows,
            "matched_primary_output_node_id": str(output["canvas_node_id"]),
            "matched_output_kind": output_kind,
            "suggested_template": suggestion,
            "output_candidates": outputs,
        }
    reason_codes = _candidate_failure_summary(candidate_rows)
    actions = _candidate_failure_actions(reason_codes, candidate_rows)
    return {
        **base,
        "match_status": "unmatched",
        "match_reason_codes": reason_codes,
        "match_actions": actions,
        "model_profile_candidates": candidate_rows,
        "matched_primary_output_node_id": str(output["canvas_node_id"]),
        "matched_output_kind": output_kind,
        "suggested_template": suggestion,
        "output_candidates": outputs,
    }


@security_boundary_resolution_cache()
@saved_workflow_directory_resolution_cache()
def discover_saved_workflows() -> dict[str, Any]:
    """Scan every configured ComfyUI userdata source once and auto-match rows."""

    deployments = list(ModelDeployment.objects.select_related(
        "profile_version__provider__credential__active_version",
        "runtime_host",
        "credential__active_version",
    ).filter(
        status=ModelDeployment.Status.ACTIVE,
        profile_version__provider__driver_type=ProviderProfile.DriverType.COMFYUI,
        profile_version__provider__is_active=True,
        runtime_host__isnull=False,
        runtime_host__status=RuntimeHost.Status.ACTIVE,
    ).order_by("-is_default", "-priority", "key", "id"))
    scopes: dict[tuple[int, str], list[ModelDeployment]] = {}
    scope_errors: list[tuple[ModelDeployment, Exception]] = []
    for deployment in deployments:
        try:
            scope = comfyui_workflow_source_scope(deployment)
        except Exception as exc:
            scope_errors.append((deployment, exc))
            continue
        scopes.setdefault((scope.schema_version, scope.sha256), []).append(deployment)
    budget = _RequestBudget(
        max_items=saved_workflow_scan_max_items(),
        max_bytes=saved_workflow_request_max_bytes(),
        max_seconds=saved_workflow_scan_max_seconds(),
        operation_label="工作流扫描",
    )
    source_results = []
    items = []
    for deployment, exc in scope_errors:
        source_results.append({
            "source_scope_schema_version": COMFYUI_WORKFLOW_SOURCE_SCOPE_SCHEMA_VERSION,
            "source_scope_sha256": "",
            "source_deployment_id": str(deployment.id),
            "provider_id": str(deployment.profile_version.provider_id),
            "runtime_host_id": str(deployment.runtime_host_id or ""),
            "source_label": deployment.runtime_host.name if deployment.runtime_host else deployment.name,
            "deployment_count": 1,
            "status": "failed",
            "item_count": 0,
            "error_code": getattr(exc, "code", "source_configuration_invalid"),
            "error_message": _safe_projected_error(
                exc,
                fallback="ComfyUI 工作流来源配置无效",
            )[:500],
        })
    for (scope_version, scope_sha256), grouped_deployments in scopes.items():
        deployment = grouped_deployments[0]
        source_row = {
            "source_scope_schema_version": scope_version,
            "source_scope_sha256": scope_sha256,
            "source_deployment_id": str(deployment.id),
            "provider_id": str(deployment.profile_version.provider_id),
            "runtime_host_id": str(deployment.runtime_host_id or ""),
            "source_label": deployment.runtime_host.name if deployment.runtime_host else deployment.name,
            "deployment_count": len(grouped_deployments),
            "status": "succeeded",
            "item_count": 0,
            "error_code": "",
            "error_message": "",
        }
        source_items: list[dict[str, Any]] = []
        try:
            driver = _source_driver(
                deployment,
                required_operations={
                    "workflow_library.list", "workflow_library.read",
                },
            )
            listed = _invoke_with_budget(
                driver, deployment, "workflow_library.list", {}, budget=budget,
            )
            paths = listed.get("items") if isinstance(listed, dict) else None
            if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                raise SavedWorkflowImportError("workflow_library.list 响应无效")
            if len(paths) > budget.max_items - budget.item_count:
                raise SavedWorkflowRequestBudgetExceeded(
                    f"单次扫描最多读取 {budget.max_items} 个已保存工作流",
                )
            object_info = None
            object_info_error: Exception | None = None
            try:
                object_info = _invoke_with_budget(
                    driver, deployment, "object_info", {}, budget=budget,
                )
                if not isinstance(object_info, dict):
                    raise SavedWorkflowImportError("object_info 响应无效")
            except SavedWorkflowRequestBudgetExceeded:
                raise
            except Exception as exc:
                # Discovery/admission is intentionally independent from live
                # node-schema analysis. Every safe JSON source still receives
                # a row and can be frozen as a candidate.
                object_info = None
                object_info_error = exc
            profiles = list(ModelProfileVersion.objects.filter(
                provider_id=deployment.profile_version.provider_id,
                status__in=(
                    ModelProfileVersion.Status.DRAFT,
                    ModelProfileVersion.Status.PUBLISHED,
                ),
            ).order_by("profile_key", "-version"))
            for source_path in paths:
                try:
                    document = _read_source(
                        driver, deployment, source_path, budget=budget,
                    )
                    _assert_safe_canvas_preflight(document.workflow)
                    if object_info is None:
                        source_items.append(_discover_candidate_without_analysis(
                            deployment,
                            document,
                            source_scope_schema_version=scope_version,
                            source_scope_sha256=scope_sha256,
                            analysis_error=(
                                object_info_error
                                or SavedWorkflowImportError("object_info 响应无效")
                            ),
                        ))
                        budget.assert_available()
                        continue
                    try:
                        context = _load_saved_workflow_preview_context(
                            deployment,
                            driver=driver,
                            source_path=document.source_path,
                            expected_sha256=document.source_sha256,
                            budget=budget,
                            document=document,
                            object_info=object_info,
                        )
                        discovered = _discover_saved_workflow_item(
                            context,
                            source_scope_schema_version=scope_version,
                            source_scope_sha256=scope_sha256,
                            source_deployments=grouped_deployments,
                            profiles=profiles,
                        )
                    except SavedWorkflowRequestBudgetExceeded:
                        raise
                    except Exception as analysis_exc:
                        # Projector/model matching is advisory at discovery.
                        # Once source bytes pass the admission boundary, an
                        # analysis failure must not erase their candidate path.
                        discovered = _discover_candidate_without_analysis(
                            deployment,
                            document,
                            source_scope_schema_version=scope_version,
                            source_scope_sha256=scope_sha256,
                            analysis_error=analysis_exc,
                        )
                    source_items.append(discovered)
                    budget.assert_available()
                except SavedWorkflowRequestBudgetExceeded:
                    raise
                except Exception as exc:
                    source_items.append({
                        "source_scope_schema_version": scope_version,
                        "source_scope_sha256": scope_sha256,
                        "source_key": _source_key(scope_sha256, str(source_path)[:500]),
                        "source_deployment_id": str(deployment.id),
                        "source_provider_id": str(deployment.profile_version.provider_id),
                        "source_runtime_host_id": str(deployment.runtime_host_id or ""),
                        "source_path": str(source_path)[:500],
                        "source_sha256": "",
                        "size_bytes": 0,
                        "source_workflow_id": "",
                        "suggested_import_kind": "",
                        "candidate_managed": False,
                        "candidate_source_id": "",
                        "imported": False,
                        "source_id": "",
                        "workflow_version_id": "",
                        "template_id": "",
                        "contract_upgrade_required": False,
                        "error_code": getattr(exc, "code", "workflow_read_failed"),
                        "error_message": _safe_projected_error(
                            exc,
                            fallback="读取或分析 ComfyUI 已保存工作流失败",
                        )[:500],
                        "analysis_status": "failed",
                        "analysis_code": getattr(exc, "code", "workflow_read_failed"),
                        "analysis_message": _safe_projected_error(
                            exc,
                            fallback="工作流候选来源未通过基础安全检查",
                        )[:500],
                        "admission_tier": "candidate",
                        "actionability": "candidate_only",
                        "recovery_actions": [{"code": "fix_in_comfyui"}],
                        "match_status": "blocked",
                        "match_reason_codes": [
                            getattr(exc, "code", "workflow_read_failed")
                        ],
                        "match_actions": [{"code": "fix_in_comfyui"}],
                        "model_profile_candidates": [],
                        "matched_model_profile_version_id": "",
                        "matched_target_deployment_id": "",
                        "matched_primary_output_node_id": "",
                        "matched_output_kind": "",
                        "suggested_template": None,
                        "preview": None,
                    })
            source_row["item_count"] = len(paths)
            items.extend(source_items)
        except SavedWorkflowRequestBudgetExceeded:
            raise
        except Exception as exc:
            source_row.update({
                "status": "failed",
                "item_count": 0,
                "error_code": getattr(exc, "code", "workflow_source_scan_failed"),
                "error_message": _safe_projected_error(
                    exc,
                    fallback="扫描 ComfyUI 工作流来源失败",
                )[:500],
            })
        source_results.append(source_row)
    failed_sources = sum(row["status"] == "failed" for row in source_results)
    succeeded_sources = len(source_results) - failed_sources
    status_value = (
        "failed" if failed_sources and not succeeded_sources
        else "partial" if failed_sources
        else "complete"
    )
    budget.assert_available()
    return {
        "schema_version": 2,
        "status": status_value,
        "source_count": len(source_results),
        "succeeded_source_count": succeeded_sources,
        "failed_source_count": failed_sources,
        "matched_count": sum(row.get("match_status") == "matched" for row in items),
        "review_count": sum(row.get("match_status") != "matched" for row in items),
        "sources": source_results,
        "items": items,
    }


def _import_contract_sha256(item: dict[str, Any]) -> str:
    return json_contract({
        "import_kind": ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION,
        "template_id": str(item["template"].id),
        "model_profile_version_id": str(item["model_profile_version"].id),
        "contract_draft_id": str(item["contract_draft"].id),
        "contract_draft_projection_sha256": str(
            item["contract_draft"].projection_sha256,
        ),
        "primary_output_node_ids": sorted(
            {str(value) for value in item.get("primary_output_node_ids") or []},
        ),
        "expected_object_info_schema_sha256": str(
            item.get("expected_object_info_schema_sha256") or ""
        ).casefold(),
        "expected_converted_graph_sha256": str(
            item.get("expected_converted_graph_sha256") or ""
        ).casefold(),
        "expected_parameter_schema_sha256": str(
            item.get("expected_parameter_schema_sha256") or ""
        ).casefold(),
        "expected_structural_contract_sha256": str(
            item.get("expected_structural_contract_sha256") or ""
        ).casefold(),
    }, max_bytes=_workflow_graph_max_bytes()).sha256


def _classification_contract_sha256(import_kind: str) -> str:
    return json_contract(
        {"import_kind": import_kind},
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256


def _assert_reusable_workflow_version(version: WorkflowTemplateVersion) -> None:
    if (
        version.status not in {
            WorkflowTemplateVersion.Status.DRAFT,
            WorkflowTemplateVersion.Status.VALIDATED,
            WorkflowTemplateVersion.Status.PUBLISHED,
        }
        or not version.template.is_active
    ):
        raise SavedWorkflowSourceConflict(
            "既有来源绑定的 workflow version 已退役或模板已停用",
        )
    try:
        verify_workflow_version_integrity(version)
    except ValidationError as exc:
        raise SavedWorkflowSourceConflict(
            "既有来源绑定的 workflow version 完整性校验失败",
        ) from exc


def _assert_existing_source_compatible(
    existing: ComfyWorkflowSource,
    item: dict[str, Any],
    *,
    source_scope_sha256: str,
) -> None:
    if existing.source_scope_sha256 != source_scope_sha256:
        raise SavedWorkflowSourceDeploymentChanged("既有来源证据属于不同的 ComfyUI 来源范围")
    requested_kind = str(item.get("import_kind") or "")
    if existing.import_kind != requested_kind:
        raise SavedWorkflowSourceConflict(
            f"该 source bytes 已分类为 {existing.import_kind}，不能静默改为 {requested_kind}",
        )
    if requested_kind != ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION:
        expected = _classification_contract_sha256(requested_kind)
        if existing.import_contract_sha256 != expected:
            raise SavedWorkflowSourceConflict("既有来源的分类合同与本次请求不一致")
        return
    version = existing.workflow_version
    if version is None:
        raise SavedWorkflowSourceConflict("既有 template_version 来源缺少 workflow version")
    _assert_reusable_workflow_version(version)
    requested_template = item.get("template")
    requested_new = item.get("new_template")
    if requested_template is not None:
        target_matches = version.template_id == requested_template.id
    elif isinstance(requested_new, dict):
        target_matches = (
            version.template.owner_id is None
            and version.template.version == 1
            and version.template.slug == requested_new.get("slug")
            and version.template.name == requested_new.get("name")
            and version.template.output_kind == requested_new.get("output_kind")
        )
    else:
        target_matches = False
    if not target_matches or version.model_profile_version_id != item["model_profile_version"].id:
        raise SavedWorkflowSourceConflict("既有来源绑定的 template 或 model profile 与本次请求不一致")
    resolved_item = {**item, "template": version.template}
    expected = _import_contract_sha256(resolved_item)
    if existing.import_contract_sha256 != expected:
        raise SavedWorkflowSourceConflict("既有来源的完整导入合同与本次请求不一致")


def _resolve_target_template(item: dict[str, Any]) -> WorkflowTemplate:
    template = item.get("template")
    if template is not None:
        locked = WorkflowTemplate.objects.select_for_update().get(pk=template.pk)
        if not locked.is_active or locked.provider != "comfyui":
            raise SavedWorkflowImportError("目标 workflow template 必须是启用的 ComfyUI 模板")
        return locked
    requested = item.get("new_template")
    if not isinstance(requested, dict):
        raise SavedWorkflowImportError("template_version 缺少目标 template")
    existing = WorkflowTemplate.objects.select_for_update().filter(
        owner__isnull=True,
        slug=requested["slug"],
        version=1,
    ).first()
    if existing is not None:
        if (
            not existing.is_active
            or existing.provider != "comfyui"
            or existing.name != requested["name"]
            or existing.output_kind != requested["output_kind"]
        ):
            raise SavedWorkflowImportError("new_template slug 已被不同模板占用，请显式选择目标")
        return existing
    try:
        # Keep the uniqueness race inside its own savepoint. Catching an
        # IntegrityError directly in the outer import transaction would leave
        # that transaction unusable on PostgreSQL.
        with transaction.atomic():
            return WorkflowTemplate.objects.create(
                owner=None,
                name=requested["name"],
                slug=requested["slug"],
                provider="comfyui",
                output_kind=requested["output_kind"],
                model=item["model_profile_version"].model_identifier,
                version=1,
                definition={},
                input_schema={},
                description=str(requested.get("description") or ""),
                is_system=False,
                is_active=True,
            )
    except IntegrityError:
        existing = WorkflowTemplate.objects.select_for_update().filter(
            owner__isnull=True,
            slug=requested["slug"],
            version=1,
        ).first()
        if existing is None or (
            not existing.is_active
            or existing.provider != "comfyui"
            or existing.name != requested["name"]
            or existing.output_kind != requested["output_kind"]
        ):
            raise SavedWorkflowImportError(
                "new_template slug 并发创建冲突，请刷新后显式选择目标",
            )
        return existing


def _linked_execution_snapshot(document: _SourceDocument):
    match = _TASK_FILENAME.search(document.source_path)
    task = GenerationTask.objects.filter(pk=match.group(1)).first() if match else None
    workflow = None
    if task is not None:
        workflow = GeneratedWorkflow.objects.filter(tasks=task).first()
    return workflow, task


def _create_source(
    *,
    deployment: ModelDeployment,
    document: _SourceDocument,
    import_kind: str,
    actor,
    conversion_report: dict,
    converted_graph_sha256: str = "",
    workflow_version=None,
    generated_workflow=None,
    generation_task=None,
) -> ComfyWorkflowSource:
    source_scope = comfyui_workflow_source_scope(deployment)
    source = ComfyWorkflowSource(
        source_deployment=deployment,
        source_scope_schema_version=source_scope.schema_version,
        source_scope_sha256=source_scope.sha256,
        source_path=document.source_path,
        source_path_key=document.source_path,
        source_path_sha256=_source_path_sha256(document.source_path),
        source_sha256=document.source_sha256,
        source_size_bytes=document.size_bytes,
        source_workflow_id=_public_source_workflow_id(document.workflow),
        import_contract_sha256=str(conversion_report.get("import_contract_sha256") or ""),
        import_kind=import_kind,
        converted_graph_sha256=converted_graph_sha256,
        workflow_version=workflow_version,
        generated_workflow=generated_workflow,
        generation_task=generation_task,
        imported_by=actor,
    )
    assign_json_contract(
        source,
        "conversion_report",
        conversion_report,
        max_bytes=_workflow_graph_max_bytes(),
    )
    assign_json_contract(
        source,
        "source_document",
        document.workflow if import_kind == ComfyWorkflowSource.ImportKind.CANDIDATE else {},
        max_bytes=_workflow_graph_max_bytes(),
    )
    source.full_clean()
    source.save()
    return source


def _verified_candidate_document(candidate: ComfyWorkflowSource) -> _SourceDocument:
    if candidate.import_kind != ComfyWorkflowSource.ImportKind.CANDIDATE:
        raise SavedWorkflowContractDraftMismatch("合同草稿未绑定候选来源")
    try:
        workflow = verify_json_contract(
            candidate.source_document,
            schema_version=candidate.source_document_schema_version,
            size_bytes=candidate.source_document_size_bytes,
            sha256=candidate.source_document_sha256,
            max_bytes=_workflow_graph_max_bytes(),
        ).value
    except ValidationError as exc:
        raise SavedWorkflowContractDraftMismatch("候选工作流快照完整性校验失败") from exc
    if not isinstance(workflow, dict) or not workflow:
        raise SavedWorkflowContractDraftMismatch("候选工作流快照为空或格式无效")
    _assert_safe_canvas_preflight(workflow)
    return _SourceDocument(
        source_path=candidate.source_path_key,
        source_sha256=candidate.source_sha256,
        size_bytes=candidate.source_size_bytes,
        workflow=workflow,
    )


def _projector_recommendation_role_key(node_type: str, occupied: set[str]) -> str:
    base = f"node_{_bounded_parameter_slug(node_type, maximum=48)}"
    value = base
    ordinal = 2
    while value in occupied:
        value = f"{base}_{ordinal}"
        ordinal += 1
    occupied.add(value)
    return value


def _projector_recommendation_inputs(
    graph: dict[str, Any],
    object_info: dict[str, Any],
) -> dict[str, list[str]]:
    """Return names only; source literals and inventory values stay server-side."""

    result: dict[str, list[str]] = {}
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("class_type") or "")
        if not node_type:
            continue
        names = {
            str(name)
            for name in (node.get("inputs") or {})
            if isinstance(name, str) and name
        }
        info = object_info.get(node_type)
        schema = info.get("input") if isinstance(info, dict) else None
        if isinstance(schema, dict):
            for category in ("required", "optional"):
                definitions = schema.get(category)
                if isinstance(definitions, dict):
                    names.update(
                        str(name) for name in definitions
                        if isinstance(name, str) and name
                    )
        result[node_type] = sorted(names)
    return dict(sorted(result.items()))


def _projector_recommendation_model_selectors(
    graph: dict[str, Any],
    *,
    output_kind: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    graph_targets = {
        (str(node.get("class_type") or ""), str(input_name))
        for node in graph.values()
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict)
        for input_name in node["inputs"]
    }
    warnings: list[str] = []
    try:
        catalog = current_projector_catalog(output_kind=output_kind)
    except WorkflowProjectorError:
        return [], [
            "当前没有可读取的已发布 Projector 目录，模型文件 selector 未自动补充。",
        ]
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: set[tuple[str, str]] = set()
    for contract in catalog:
        if contract.engine_key != "generic.fixed":
            continue
        for raw in contract.definition.get("model_selectors", []):
            if not isinstance(raw, dict):
                continue
            target = (str(raw.get("node_type") or ""), str(raw.get("input_name") or ""))
            if target not in graph_targets:
                continue
            row = deepcopy(raw)
            prior = candidates.get(target)
            if prior is not None and prior != row:
                conflicts.add(target)
            else:
                candidates[target] = row
    for target in conflicts:
        candidates.pop(target, None)
    if conflicts:
        warnings.append(
            f"已发布目录对 {len(conflicts)} 个模型输入存在 selector 分歧，相关项未自动填入。",
        )
    return [candidates[key] for key in sorted(candidates)], warnings


def recommend_workflow_projector_from_candidate(
    *,
    candidate_source_id,
    expected_source_scope_schema_version: int,
    expected_source_scope_sha256: str,
    expected_source_sha256: str,
    primary_output_node_id: str,
    output_kind: str,
) -> dict[str, Any]:
    """Build one evidence-bound, unsaved Generic Projector recommendation.

    The immutable candidate supplies the canvas, live object_info supplies the
    current node catalog, and the operator supplies only the selected primary
    output and its platform media kind.  This function never creates or
    publishes a Projector and never submits a ComfyUI prompt.
    """

    allowed_output_kinds = {
        value for value, _label in WorkflowTemplate.OutputKind.choices
    }
    if output_kind not in allowed_output_kinds:
        raise SavedWorkflowOutputContractInvalid("Projector 建议的产物类型无效")
    try:
        candidate = ComfyWorkflowSource.objects.select_related(
            "source_deployment__profile_version__provider",
            "source_deployment__runtime_host",
        ).get(pk=candidate_source_id)
    except (ComfyWorkflowSource.DoesNotExist, ValueError, TypeError) as exc:
        raise SavedWorkflowCandidateRequired("未找到受管的不可变工作流候选") from exc
    if candidate.import_kind != ComfyWorkflowSource.ImportKind.CANDIDATE:
        raise SavedWorkflowCandidateRequired("Projector 建议只能从候选来源生成")
    if (
        candidate.source_scope_schema_version != expected_source_scope_schema_version
        or candidate.source_scope_sha256
        != str(expected_source_scope_sha256 or "").casefold()
    ):
        raise SavedWorkflowSourceScopeChanged("请求与候选冻结的来源范围不一致，请重新扫描")
    if candidate.source_sha256 != str(expected_source_sha256 or "").casefold():
        raise SavedWorkflowSourceChanged("请求与候选冻结的工作流摘要不一致，请重新扫描")

    deployment = candidate.source_deployment
    _assert_expected_source_scope(
        deployment,
        expected_source_scope_schema_version=expected_source_scope_schema_version,
        expected_source_scope_sha256=expected_source_scope_sha256,
    )
    document = _verified_candidate_document(candidate)
    driver = _source_driver(deployment, required_operations={"object_info"})
    budget = _RequestBudget(
        max_items=1,
        max_bytes=saved_workflow_request_max_bytes(),
        max_seconds=saved_workflow_scan_max_seconds(),
        operation_label="Projector 方案建议",
    )
    context = _load_saved_workflow_preview_context(
        deployment,
        driver=driver,
        source_path=document.source_path,
        expected_sha256=document.source_sha256,
        budget=budget,
        document=document,
    )
    converted = ui_workflow_to_api_graph(
        document.workflow,
        context.object_info,
        primary_output_node_ids=[primary_output_node_id],
    )
    validate_comfy_graph(converted.api_graph)
    graph = converted.api_graph
    primary_api_ids = list(converted.primary_output_node_ids)
    if len(primary_api_ids) != 1:
        raise SavedWorkflowOutputContractInvalid("Projector 建议必须选择唯一主输出节点")
    primary_api_id = primary_api_ids[0]
    primary_node = graph.get(primary_api_id)
    primary_node_type = (
        str(primary_node.get("class_type") or "")
        if isinstance(primary_node, dict) else ""
    )
    if not primary_node_type:
        raise SavedWorkflowOutputContractInvalid("所选主输出无法映射到 API 图节点")

    node_counts = Counter(
        str(node.get("class_type") or "")
        for node in graph.values()
        if isinstance(node, dict) and str(node.get("class_type") or "")
    )
    occupied_roles: set[str] = set()
    role_by_node_type: dict[str, str] = {}
    node_roles: dict[str, dict[str, str]] = {}
    for node_type, count in sorted(node_counts.items()):
        if count != 1:
            continue
        if node_type == primary_node_type:
            role = "output"
            occupied_roles.add(role)
        else:
            role = _projector_recommendation_role_key(node_type, occupied_roles)
        role_by_node_type[node_type] = role
        node_roles[role] = {"node_type": node_type, "cardinality": "one"}

    observed_edges: list[dict[str, Any]] = []
    recommended_edges: list[dict[str, Any]] = []
    for target_id, target_node in sorted(graph.items()):
        if not isinstance(target_node, dict) or not isinstance(target_node.get("inputs"), dict):
            continue
        target_type = str(target_node.get("class_type") or "")
        for input_name, value in sorted(target_node["inputs"].items()):
            source_id = _connection_source_node_id(value)
            if not source_id:
                continue
            source_node = graph.get(source_id)
            source_type = (
                str(source_node.get("class_type") or "")
                if isinstance(source_node, dict) else ""
            )
            output_index = value[1]
            observed_edges.append({
                "from_node_type": source_type,
                "output_index": output_index,
                "to_node_type": target_type,
                "input_name": str(input_name),
                "recommended": bool(
                    source_type in role_by_node_type and target_type in role_by_node_type
                ),
            })
            if source_type in role_by_node_type and target_type in role_by_node_type:
                recommended_edges.append({
                    "from_role": role_by_node_type[source_type],
                    "output_index": output_index,
                    "to_role": role_by_node_type[target_type],
                    "input_name": str(input_name),
                })

    warnings = [
        "必需节点数量来自这一份候选快照；单份样本不能证明哪些节点可选，发布前需人工复核。",
        "语义角色、关键连接和参数治理是初始建议；保存仍只创建草稿，不会自动发布。",
        "本操作只读取候选快照与实时 object_info，不会运行 ComfyUI 工作流或生成媒体。",
    ]
    repeated_types = sorted(node_type for node_type, count in node_counts.items() if count > 1)
    if repeated_types:
        warnings.append(
            "以下重复节点类型无法仅凭单份图安全映射为唯一角色，未自动生成角色、连接或参数目标："
            + "、".join(repeated_types),
        )
    if len(recommended_edges) > 128:
        warnings.append("可证明的关键连接超过合同上限，仅填入排序后的前 128 项。")
        recommended_edges = recommended_edges[:128]

    model_selectors, selector_warnings = _projector_recommendation_model_selectors(
        graph,
        output_kind=output_kind,
    )
    warnings.extend(selector_warnings)
    if len(model_selectors) > 128:
        warnings.append("可信模型 selector 超过合同上限，仅填入排序后的前 128 项。")
        model_selectors = model_selectors[:128]
    asset_dependencies = _asset_dependency_candidates(graph, context.object_info)
    temporary_projector = ProjectorContract(
        version_id="recommendation",
        key="recommendation.generic.fixed",
        version=1,
        provider_driver_type="comfyui",
        operation="workflow.import",
        output_kind=output_kind,
        engine_key="generic.fixed",
        engine_version=1,
        definition_sha256="",
        definition={
            "output_nodes": {primary_node_type: output_kind},
            "model_selectors": model_selectors,
        },
    )
    _contract, _bindings, _requirements, generic_blockers, configurable_parameters = (
        _suggested_generic_import_contract(
            graph,
            context.object_info,
            primary_api_node_ids=primary_api_ids,
            model_dependencies=[],
            asset_dependencies=asset_dependencies,
            model_profile_version=None,
            projector=temporary_projector,
        )
    )
    if generic_blockers:
        warnings.append(
            f"通用导入分析另有 {len(generic_blockers)} 项运行合同阻断；发布 Projector 不会自动解决这些阻断。",
        )

    parameter_policies: list[dict[str, Any]] = []
    parameter_catalog: list[dict[str, str]] = []
    seen_semantics: set[str] = set()
    seen_parameter_targets: set[tuple[str, str]] = set()
    for row in configurable_parameters:
        target = row.get("target") if isinstance(row, dict) else None
        if not isinstance(target, dict):
            continue
        node_type = str(target.get("node_type") or "")
        role = role_by_node_type.get(node_type)
        semantic_key = str(row.get("semantic_key") or "")
        input_name = str(target.get("input_name") or "")
        value_type = str(row.get("type") or "")
        if not role or not semantic_key or not input_name:
            continue
        parameter_catalog.append({
            "semantic_key": semantic_key,
            "node_type": node_type,
            "input_name": input_name,
            "value_type": value_type,
        })
        target_key = (role, input_name)
        if semantic_key in seen_semantics or target_key in seen_parameter_targets:
            continue
        seen_semantics.add(semantic_key)
        seen_parameter_targets.add(target_key)
        policy = (
            "runtime"
            if semantic_key.startswith(("prompt.", "output."))
            or semantic_key == "sampling.seed"
            else "object_info"
        )
        parameter_policies.append({
            "semantic_key": semantic_key,
            "node_role": role,
            "input_name": input_name,
            "policy": policy,
            "value": None,
        })
    if len(parameter_policies) > 128:
        warnings.append("可配置参数超过合同上限，仅填入排序后的前 128 项。")
        parameter_policies = parameter_policies[:128]

    asset_slots: list[dict[str, Any]] = []
    seen_asset_keys: set[str] = set()
    for row in asset_dependencies:
        key = str(row.get("suggested_semantic_key") or "")
        if not key or key in seen_asset_keys:
            continue
        seen_asset_keys.add(key)
        asset_slots.append({
            "key": key,
            "media_kind": str(row.get("media_kind") or ""),
            "dynamic": bool((row.get("max_items") or 1) > 1),
            "contract_source": "workflow",
        })
    if len(asset_slots) > 64:
        warnings.append("素材槽超过合同上限，仅填入排序后的前 64 项。")
        asset_slots = asset_slots[:64]

    definition = {
        "schema_version": 1,
        "priority": 100,
        "detector": {
            "fallback": False,
            "allow_other_nodes": False,
            "node_counts": dict(sorted(node_counts.items())),
            "optional_node_counts": {},
            "input_equals": [],
        },
        "output_nodes": {primary_node_type: output_kind},
        "model_selectors": model_selectors,
        "node_roles": node_roles,
        "required_edges": recommended_edges,
        "parameter_policies": parameter_policies,
        "asset_slots": asset_slots,
        "adapters": deepcopy(WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS["generic.fixed"]),
        "projection": {"kind": "generic_fixed", "config": {}},
    }
    from studio.services.workflow_asset_groups import recommend_group_definition
    dynamic_definition = recommend_group_definition(definition, graph, context.object_info, asset_dependencies)
    engine_key = "generic.references" if dynamic_definition else "generic.fixed"
    if dynamic_definition:
        definition = dynamic_definition
        warnings.append("发现同类型可选素材端口，已推荐通用动态素材草稿。数量上限仅是节点端口上限，不证明模型可用数量；请复核每个端口含义、模板节点和模型限制，再保存发布。")
    elif asset_dependencies:
        warnings.append("未证明可自动展开的单素材模板与可选端口；保留固定合同建议。可在新建草稿中选择通用动态素材引擎后人工配置，不会猜测多图能力。")
    definition = validate_workflow_projector_definition(
        definition,
        engine_key=engine_key,
        engine_version=1,
    )
    payload = {
        "schema_version": 1,
        "persistence": "unsaved_draft",
        "requires_human_confirmation": True,
        "engine_key": engine_key,
        "engine_version": 1,
        "provider_driver_type": "comfyui",
        "operation": "workflow.import",
        "output_kind": output_kind,
        "definition": definition,
        "source_evidence": {
            "candidate_source_id": str(candidate.id),
            "source_scope_schema_version": candidate.source_scope_schema_version,
            "source_scope_sha256": candidate.source_scope_sha256,
            "source_sha256": candidate.source_sha256,
            "source_path_sha256": candidate.source_path_sha256,
            "object_info_schema_sha256": context.object_info_schema_sha256,
            "converted_graph_sha256": _converted_graph_sha256(graph),
            "primary_canvas_node_id": str(primary_output_node_id),
            "primary_api_node_id": primary_api_id,
            "primary_node_type": primary_node_type,
        },
        "catalog": {
            "node_types": [
                {
                    "value": node_type,
                    "count": count,
                    "primary_output": node_type == primary_node_type,
                }
                for node_type, count in sorted(node_counts.items())
            ],
            "inputs_by_node_type": _projector_recommendation_inputs(
                graph, context.object_info,
            ),
            "observed_edges": observed_edges,
            "parameter_targets": parameter_catalog,
        },
        "warnings": warnings,
    }
    payload["recommendation_sha256"] = json_contract(
        payload,
        max_bytes=_workflow_graph_max_bytes(),
    ).sha256
    return payload


def _verified_contract_draft_projection(
    draft: ComfyWorkflowContractDraft,
) -> dict[str, Any]:
    try:
        projection = verify_json_contract(
            draft.projection,
            schema_version=draft.projection_schema_version,
            size_bytes=draft.projection_size_bytes,
            sha256=draft.projection_sha256,
            max_bytes=_workflow_graph_max_bytes(),
        ).value
    except ValidationError as exc:
        raise SavedWorkflowContractDraftMismatch("平台执行合同草稿完整性校验失败") from exc
    if not isinstance(projection, dict):
        raise SavedWorkflowContractDraftMismatch("平台执行合同草稿格式无效")
    return projection


def _create_promoted_source(
    *,
    candidate: ComfyWorkflowSource,
    workflow_version: WorkflowTemplateVersion,
    actor,
    conversion_report: dict[str, Any],
    converted_graph_sha256: str,
) -> ComfyWorkflowSource:
    source = ComfyWorkflowSource(
        source_deployment=candidate.source_deployment,
        source_scope_schema_version=candidate.source_scope_schema_version,
        source_scope_sha256=candidate.source_scope_sha256,
        source_path=candidate.source_path,
        source_path_key=candidate.source_path_key,
        source_path_sha256=candidate.source_path_sha256,
        source_sha256=candidate.source_sha256,
        source_size_bytes=candidate.source_size_bytes,
        source_workflow_id=candidate.source_workflow_id,
        import_contract_sha256=str(conversion_report.get("import_contract_sha256") or ""),
        import_kind=ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION,
        converted_graph_sha256=converted_graph_sha256,
        workflow_version=workflow_version,
        imported_by=actor,
    )
    assign_json_contract(
        source,
        "source_document",
        {},
        max_bytes=_workflow_graph_max_bytes(),
    )
    assign_json_contract(
        source,
        "conversion_report",
        conversion_report,
        max_bytes=_workflow_graph_max_bytes(),
    )
    source.full_clean()
    source.save()
    return source


def _promote_contract_draft(
    deployment: ModelDeployment,
    *,
    item: dict[str, Any],
    actor,
) -> tuple[dict[str, Any], None]:
    draft = item.get("contract_draft")
    if not isinstance(draft, ComfyWorkflowContractDraft):
        raise SavedWorkflowContractDraftMismatch("正式提升缺少平台执行合同草稿")
    projection = _verified_contract_draft_projection(draft)
    candidate = draft.candidate_source
    document = _verified_candidate_document(candidate)
    if (
        str(item.get("source_path") or "") != candidate.source_path_key
        or str(item.get("expected_sha256") or "").casefold() != candidate.source_sha256
        or int(item.get("expected_source_scope_schema_version") or 0)
        != candidate.source_scope_schema_version
        or str(item.get("expected_source_scope_sha256") or "").casefold()
        != candidate.source_scope_sha256
        or deployment.profile_version.provider_id
        != candidate.source_deployment.profile_version.provider_id
    ):
        raise SavedWorkflowContractDraftMismatch("请求来源与合同草稿冻结的候选不一致")
    if (
        draft.actionability
        != ComfyWorkflowContractDraft.Actionability.READY_FOR_EXECUTABLE_IMPORT
        or projection.get("actionability") != "ready_for_executable_import"
        or projection.get("blockers")
        or draft.projector_version_id is None
    ):
        raise SavedWorkflowContractBlocked("平台执行合同草稿仍有未解决的结构阻断项")
    target = item["model_profile_version"]
    if (
        draft.model_profile_version_id != target.id
        or projection.get("model_profile_version_id") != str(target.id)
        or list(projection.get("primary_output_node_ids") or [])
        != [str(value) for value in item.get("primary_output_node_ids") or []]
    ):
        raise SavedWorkflowContractDraftMismatch("合同草稿的模型版本或主输出选择不一致")
    expected_fields = {
        "object_info_schema_sha256": "expected_object_info_schema_sha256",
        "converted_graph_sha256": "expected_converted_graph_sha256",
        "parameter_schema_sha256": "expected_parameter_schema_sha256",
        "structural_contract_sha256": "expected_structural_contract_sha256",
    }
    if any(
        str(projection.get(projection_key) or "").casefold()
        != str(item.get(request_key) or "").casefold()
        for projection_key, request_key in expected_fields.items()
    ):
        raise SavedWorkflowContractDraftMismatch("请求审阅 SHA 与合同草稿不一致")
    projector = draft.projector_version
    projector_identity = projection.get("input_contract", {}).get("contract_projector")
    if (
        not isinstance(projector_identity, dict)
        or str(projector.id) != str(projector_identity.get("version_id") or "")
        or projector.version != projector_identity.get("version")
        or projector.definition_sha256
        != str(projector_identity.get("definition_sha256") or "")
    ):
        raise SavedWorkflowContractDraftMismatch("合同草稿冻结的 projector 版本或 SHA 不一致")
    graph = projection.get("executable_graph")
    if not isinstance(graph, dict) or not graph:
        raise SavedWorkflowContractDraftMismatch("合同草稿缺少可提升的安全转换图")
    try:
        verify_json_contract(
            graph,
            schema_version=int(projection.get("executable_graph_schema_version") or 0),
            size_bytes=int(projection.get("executable_graph_size_bytes") or 0),
            sha256=str(projection.get("executable_graph_sha256") or ""),
            max_bytes=_workflow_graph_max_bytes(),
        )
        validate_comfy_graph(graph)
    except (ValidationError, WorkflowBuildError) as exc:
        raise SavedWorkflowContractDraftMismatch("合同草稿安全转换图完整性校验失败") from exc
    input_contract = projection.get("input_contract")
    bindings = projection.get("semantic_bindings")
    requirements = projection.get("model_requirements")
    if (
        not isinstance(input_contract, dict)
        or not isinstance(bindings, list)
        or not isinstance(requirements, list)
    ):
        raise SavedWorkflowContractDraftMismatch("合同草稿缺少结构合同、绑定或模型要求")
    output_kind = str(
        input_contract.get("outputs", {}).get("primary", {}).get("kind") or ""
    )
    locked_item = {
        **item,
        "input_contract": input_contract,
        "semantic_bindings": bindings,
        "model_requirements": requirements,
    }
    if _requested_output_kind(locked_item) != output_kind:
        raise SavedWorkflowOutputContractInvalid("目标模板与合同草稿主输出类型不一致")
    graph_sha256 = str(projection.get("executable_graph_sha256") or "")
    with transaction.atomic():
        locked_draft = ComfyWorkflowContractDraft.objects.select_for_update().select_related(
            "candidate_source__source_deployment__profile_version__provider",
            "projector_version",
        ).get(pk=draft.pk)
        if locked_draft.projection_sha256 != draft.projection_sha256:
            raise SavedWorkflowContractDraftMismatch("合同草稿在提升期间发生变化")
        locked_candidate = ComfyWorkflowSource.objects.select_for_update().get(
            pk=candidate.pk,
        )
        conflicting = ComfyWorkflowSource.objects.filter(
            source_scope_schema_version=locked_candidate.source_scope_schema_version,
            source_scope_sha256=locked_candidate.source_scope_sha256,
            source_path_sha256=locked_candidate.source_path_sha256,
            source_path_key=locked_candidate.source_path_key,
            source_sha256=locked_candidate.source_sha256,
        ).exclude(
            import_kind__in=(
                ComfyWorkflowSource.ImportKind.CANDIDATE,
                ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION,
            ),
        )
        if conflicting.exists():
            raise SavedWorkflowSourceConflict("同一候选已按其他来源类型分类")
        locked_target = _locked_target_profile(
            target,
            source_provider_id=(
                locked_candidate.source_deployment.profile_version.provider_id
            ),
            output_kind=output_kind,
        )
        locked_item["model_profile_version"] = locked_target
        template = _resolve_target_template(locked_item)
        resolved_item = {**locked_item, "template": template}
        import_contract_sha256 = _import_contract_sha256(resolved_item)
        existing = ComfyWorkflowSource.objects.select_related(
            "workflow_version__template",
        ).filter(
            source_scope_schema_version=locked_candidate.source_scope_schema_version,
            source_scope_sha256=locked_candidate.source_scope_sha256,
            source_path_sha256=locked_candidate.source_path_sha256,
            source_path_key=locked_candidate.source_path_key,
            source_sha256=locked_candidate.source_sha256,
            import_contract_sha256=import_contract_sha256,
        ).order_by("-created_at").first()
        if existing is not None:
            _assert_existing_source_compatible(
                existing,
                resolved_item,
                source_scope_sha256=locked_candidate.source_scope_sha256,
            )
            return _result(existing, result="reused"), None
        prior_template_ids = set(ComfyWorkflowSource.objects.filter(
            source_scope_schema_version=locked_candidate.source_scope_schema_version,
            source_scope_sha256=locked_candidate.source_scope_sha256,
            source_path_sha256=locked_candidate.source_path_sha256,
            source_path_key=locked_candidate.source_path_key,
            import_kind=ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION,
        ).values_list("workflow_version__template_id", flat=True))
        prior_template_ids.discard(None)
        if prior_template_ids and prior_template_ids != {template.id}:
            raise SavedWorkflowSourceConflict("同一候选的新合同只能追加到原 template")
        canonical_input_sha256 = json_contract(
            input_contract,
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256
        canonical_bindings_sha256 = json_contract(
            canonical_binding_rows(bindings),
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256
        canonical_requirements_sha256 = json_contract(
            canonical_requirement_rows(requirements),
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256
        canonical_source = ComfyWorkflowSource.objects.select_related(
            "workflow_version__template",
        ).filter(
            source_scope_schema_version=locked_candidate.source_scope_schema_version,
            source_scope_sha256=locked_candidate.source_scope_sha256,
            source_sha256=locked_candidate.source_sha256,
            import_kind=ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION,
            converted_graph_sha256=graph_sha256,
            workflow_version__template=template,
            workflow_version__model_profile_version=locked_target,
            workflow_version__graph_sha256=graph_sha256,
            workflow_version__input_contract_sha256=canonical_input_sha256,
            workflow_version__semantic_bindings_sha256=canonical_bindings_sha256,
            workflow_version__model_requirements_sha256=canonical_requirements_sha256,
            workflow_version__status__in=(
                WorkflowTemplateVersion.Status.DRAFT,
                WorkflowTemplateVersion.Status.VALIDATED,
                WorkflowTemplateVersion.Status.PUBLISHED,
            ),
            workflow_version__template__is_active=True,
        ).first()
        if canonical_source is not None:
            _assert_reusable_workflow_version(canonical_source.workflow_version)
            version = canonical_source.workflow_version
        else:
            version = import_workflow_version(
                template=template,
                graph=graph,
                input_contract=input_contract,
                semantic_bindings=bindings,
                model_requirements=requirements,
                provider=locked_target.provider,
                model_profile_version=locked_target,
            )
        report = {
            "schema_version": 1,
            "promotion_source": "comfy_workflow_contract_draft",
            "import_contract_sha256": import_contract_sha256,
            "object_info_schema_sha256": projection["object_info_schema_sha256"],
            "source_converted_graph_sha256": projection["converted_graph_sha256"],
            "structural_contract_sha256": projection["structural_contract_sha256"],
            "contract_draft_id": str(locked_draft.id),
            "contract_draft_projection_sha256": locked_draft.projection_sha256,
            "primary_output_node_id": locked_draft.primary_output_node_id,
            "contract_projector": deepcopy(projector_identity),
            "live_validate_requirements": deepcopy(
                projection.get("live_validate_requirements") or [],
            ),
        }
        source = _create_promoted_source(
            candidate=locked_candidate,
            workflow_version=version,
            actor=actor,
            conversion_report=report,
            converted_graph_sha256=graph_sha256,
        )
    return _result(source, result="created"), None


def _result(source: ComfyWorkflowSource, *, result: str) -> dict[str, Any]:
    return {
        "source_path": source.source_path,
        "source_sha256": source.source_sha256,
        "source_id": str(source.id),
        "import_kind": source.import_kind,
        "result": result,
        "code": "",
        "detail": "",
        "workflow_version_id": str(source.workflow_version_id or ""),
        "structural_contract_sha256": str(
            (source.conversion_report or {}).get("structural_contract_sha256") or ""
        ),
    }


def _import_one(
    deployment: ModelDeployment,
    *,
    item: dict[str, Any],
    actor,
    driver,
    object_info: dict | None,
    budget: _RequestBudget | None = None,
) -> tuple[dict[str, Any], dict | None]:
    import_kind = item["import_kind"]
    if import_kind == ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION:
        return _promote_contract_draft(
            deployment,
            item=item,
            actor=actor,
        )

    if driver is None:
        raise SavedWorkflowImportError("读取来源候选必须使用可用的 ComfyUI source deployment")
    source_fingerprint = source_deployment_fingerprint(deployment)
    source_scope = comfyui_workflow_source_scope(deployment)
    document = _read_source(driver, deployment, item["source_path"], budget=budget)
    _assert_expected_sha(document, item["expected_sha256"])

    if import_kind == ComfyWorkflowSource.ImportKind.CANDIDATE:
        # Candidate admission freezes only safe source bytes and provenance.
        # It deliberately grants no Validate, Smoke or production route.
        _assert_safe_canvas_preflight(document.workflow)
        classification_contract_sha256 = _classification_contract_sha256(import_kind)
        with transaction.atomic():
            locked_source = _locked_source_deployment(
                deployment,
                expected_fingerprint=source_fingerprint,
            )
            existing = _existing_source(
                document,
                deployment,
                import_contract_sha256=classification_contract_sha256,
            )
            if existing is not None:
                _assert_existing_source_compatible(
                    existing,
                    item,
                    source_scope_sha256=source_scope.sha256,
                )
                return _result(existing, result="reused"), object_info
            source = _create_source(
                deployment=locked_source,
                document=document,
                import_kind=import_kind,
                actor=actor,
                conversion_report={
                    "schema_version": 1,
                    "classification": import_kind,
                    "admission_tier": "candidate",
                    "executable": False,
                    "import_contract_sha256": classification_contract_sha256,
                    "source_deployment_fingerprint_sha256": source_fingerprint,
                },
            )
        return _result(source, result="created"), object_info

    if import_kind not in {
        ComfyWorkflowSource.ImportKind.EXECUTION_SNAPSHOT,
        ComfyWorkflowSource.ImportKind.IGNORED,
    }:
        raise SavedWorkflowImportError("不支持的 ComfyUI 工作流来源分类")

    generated_workflow = generation_task = None
    if import_kind == ComfyWorkflowSource.ImportKind.EXECUTION_SNAPSHOT:
        generated_workflow, generation_task = _linked_execution_snapshot(document)
    classification_contract_sha256 = _classification_contract_sha256(import_kind)
    with transaction.atomic():
        locked_source = _locked_source_deployment(
            deployment,
            expected_fingerprint=source_fingerprint,
        )
        if _source_imports(document, locked_source).exclude(
            import_kind__in=(import_kind, ComfyWorkflowSource.ImportKind.CANDIDATE),
        ).exists():
            raise SavedWorkflowSourceConflict(
                "同一 source bytes 已按其他 import kind 分类，不能静默改类",
            )
        existing = _existing_source(
            document,
            deployment,
            import_contract_sha256=classification_contract_sha256,
        )
        if existing is not None:
            _assert_existing_source_compatible(
                existing,
                item,
                source_scope_sha256=source_scope.sha256,
            )
            return _result(existing, result="reused"), object_info
        source = _create_source(
            deployment=locked_source,
            document=document,
            import_kind=import_kind,
            actor=actor,
            conversion_report={
                "schema_version": 1,
                "classification": import_kind,
                "import_contract_sha256": classification_contract_sha256,
                "source_deployment_fingerprint_sha256": source_fingerprint,
                "linked_task": bool(generation_task),
                "linked_generated_workflow": bool(generated_workflow),
            },
            generated_workflow=generated_workflow,
            generation_task=generation_task,
        )
    return _result(source, result="created"), object_info

@security_boundary_resolution_cache()
@saved_workflow_directory_resolution_cache()
def batch_import_saved_workflows(
    deployment: ModelDeployment,
    *,
    items: list[dict[str, Any]],
    actor,
) -> dict[str, Any]:
    if len(items) > saved_workflow_batch_max_items():
        raise SavedWorkflowRequestBudgetExceeded(
            f"单次批量导入最多处理 {saved_workflow_batch_max_items()} 个已保存工作流",
        )
    expected_scopes = {
        (
            item.get("expected_source_scope_schema_version"),
            str(item.get("expected_source_scope_sha256") or "").casefold(),
        )
        for item in items
    }
    if len(expected_scopes) != 1:
        raise SavedWorkflowSourceScopeChanged(
            "同一批次必须属于同一个已扫描的 ComfyUI 工作流来源范围",
        )
    expected_scope_version, expected_scope_sha256 = next(iter(expected_scopes))
    requires_source_read = any(
        item.get("import_kind") != ComfyWorkflowSource.ImportKind.TEMPLATE_VERSION
        for item in items
    )
    if requires_source_read:
        _assert_expected_source_scope(
            deployment,
            expected_source_scope_schema_version=expected_scope_version,
            expected_source_scope_sha256=expected_scope_sha256,
        )
    driver = (
        _source_driver(
            deployment,
            required_operations={"workflow_library.read"},
        )
        if requires_source_read
        else None
    )
    results = []
    budget = _RequestBudget(
        max_items=saved_workflow_batch_max_items(),
        max_bytes=saved_workflow_request_max_bytes(),
        max_seconds=saved_workflow_batch_max_seconds(),
        operation_label="工作流批量导入",
        time_config_key="security.comfy.import.batch_max_seconds",
    )
    for index, item in enumerate(items):
        try:
            result, _object_info = _import_one(
                deployment,
                item=item,
                actor=actor,
                driver=driver,
                # Every template item freezes its own live schema observation;
                # sibling items never inherit a prior /object_info response.
                object_info=None,
                budget=budget,
            )
        except SavedWorkflowRequestBudgetExceeded as exc:
            remaining = items[index:]
            results.extend({
                "source_path": str(row.get("source_path") or "")[:500],
                "source_sha256": str(row.get("expected_sha256") or "")[:64],
                "source_id": "",
                "import_kind": str(row.get("import_kind") or ""),
                "result": "failed",
                "code": exc.code,
                "detail": _safe_projected_error(
                    exc,
                    fallback="请求超过已保存工作流同步处理预算",
                ),
                "workflow_version_id": "",
                "structural_contract_sha256": "",
            } for row in remaining)
            break
        except Exception as exc:
            result = {
                "source_path": str(item.get("source_path") or "")[:500],
                "source_sha256": str(item.get("expected_sha256") or "")[:64],
                "source_id": "",
                "import_kind": str(item.get("import_kind") or ""),
                "result": "failed",
                "code": getattr(exc, "code", None) or "workflow_import_failed",
                "detail": _safe_projected_error(
                    exc,
                    fallback="工作流导入失败，请查看服务端诊断",
                ),
                "workflow_version_id": "",
                "structural_contract_sha256": "",
            }
        results.append(result)
    succeeded = sum(item["result"] != "failed" for item in results)
    status = "complete" if succeeded == len(results) else "failed" if not succeeded else "partial"
    return {
        "schema_version": 1,
        "deployment_id": str(deployment.id),
        "status": status,
        "created_count": sum(item["result"] == "created" for item in results),
        "reused_count": sum(item["result"] == "reused" for item in results),
        "failed_count": sum(item["result"] == "failed" for item in results),
        "items": results,
    }
