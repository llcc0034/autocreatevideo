from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError

from studio.models import Asset, WorkflowSmokeAttempt, WorkflowTemplateVersion
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.services.media_capabilities import (
    conditioning_mode_contract,
    resolve_conditioning_contract,
)
from studio.services.model_profile_contracts import verified_model_profile_specification
from studio.services.provenance import sha256_file
from studio.services.reference_bindings import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    ReferenceBindingError,
    VIDEO_EXTENSIONS,
    _normalize_audios,
    _normalize_videos,
    authorized_asset_source,
    max_structural_media_bindings,
    validate_dynamic_reference_bindings_against_mode_contract,
)
from studio.services.security_boundaries import boundary_int
from studio.services.smoke_assets import ManagedSmokeAssetBudget
from studio.services.workflow_semantics import (
    WorkflowSemanticError,
    _validate_value,
    canonical_binding_rows,
)


FIXTURE_SCHEMA_VERSION = 2
JSON_INTEROPERABLE_INTEGER_MAX = (2**53) - 1
_ASSET_VALUE_TYPES = {"asset_image", "asset_video", "asset_audio"}
_MEDIA_BY_VALUE_TYPE = {
    "asset_image": "image",
    "asset_video": "video",
    "asset_audio": "audio",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXECUTION_SERVER_MANAGED_KEYS = frozenset({
    "output.prefix",
    "generation.batch_size",
})


def _fixture_values_max_bytes() -> int:
    return boundary_int("security.workflow_smoke.fixture_values_max_bytes")


def _fixture_max_values() -> int:
    return boundary_int("security.workflow_smoke.fixture_max_values")


def _fixture_max_assets() -> int:
    return boundary_int("security.workflow_smoke.fixture_max_assets")


class WorkflowSmokeFixtureConflict(ValidationError):
    code = "workflow_smoke_fixture_conflict"

    def __init__(self, message):
        super().__init__(message, code=type(self).code)


def _fixture_error(section: str, message: str) -> ValidationError:
    # Django ValidationError supports one mapping level only. Keep a stable
    # top-level API field while preserving whether values or assets failed.
    return ValidationError({"fixture": f"{section}: {message}"})


@dataclass(frozen=True)
class FrozenWorkflowSmokeFixture:
    values: dict[str, Any]
    reference_bindings: tuple[dict[str, Any], ...]
    asset_facts: tuple[dict[str, Any], ...]
    persisted_values: dict[str, Any]
    values_size_bytes: int
    values_sha256: str
    fixture_sha256: str
    materialized_graph_sha256: str = ""


@dataclass(frozen=True)
class FrozenWorkflowExecutionInputs:
    values: dict[str, Any]
    reference_bindings: tuple[dict[str, Any], ...]
    asset_facts: tuple[dict[str, Any], ...]
    request: dict[str, Any]


def _contract_parameters(version: WorkflowTemplateVersion) -> dict[str, dict[str, Any]]:
    contract = version.input_contract if isinstance(version.input_contract, dict) else {}
    parameters = contract.get("parameters")
    if not isinstance(parameters, dict) or not parameters:
        raise ValidationError({"fixture": "workflow input_contract.parameters 无效"})
    if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in parameters.items()):
        raise ValidationError({"fixture": "workflow 参数合同必须是对象映射"})
    return parameters


def _binding_map(version: WorkflowTemplateVersion) -> dict[str, dict[str, Any]]:
    rows = canonical_binding_rows(version.semantic_bindings.values(
        "semantic_key", "node_id", "input_name", "value_type", "is_required",
    ))
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["semantic_key"])
        if key in result:
            if str(result[key].get("value_type") or "") != str(row.get("value_type") or ""):
                raise ValidationError({"fixture": f"workflow semantic {key} 绑定类型不一致"})
            result[key]["is_required"] = bool(
                result[key].get("is_required", True)
                or row.get("is_required", True),
            )
            continue
        result[key] = dict(row)
    return result


def _resolved_workflow_conditioning_contract(
    version: WorkflowTemplateVersion,
) -> dict[str, Any] | None:
    if str(version.template.output_kind or "") != "video":
        return None
    model_specification = (
        verified_model_profile_specification(version.model_profile_version)
        if version.model_profile_version_id else None
    )
    return resolve_conditioning_contract(
        workflow_input_contract=(
            version.input_contract if isinstance(version.input_contract, dict) else {}
        ),
        model_specification=model_specification,
        output_kind="video",
        required=True,
    )


def _conditioning_mode_parameter(
    version: WorkflowTemplateVersion,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    contract = _resolved_workflow_conditioning_contract(version)
    if contract is None:
        return None, None
    modes = contract.get("modes")
    if not isinstance(modes, list) or not modes:
        raise ValidationError({"workflow_inputs": "视频工作流缺少 conditioning modes"})
    keys = [str(item.get("key") or "") for item in modes if isinstance(item, dict)]
    if len(keys) != len(modes) or any(not key for key in keys):
        raise ValidationError({"workflow_inputs": "视频工作流 conditioning mode 无效"})
    definition: dict[str, Any] = {
        "type": "enum",
        "required": True,
        "options": keys,
        "selection_group": "conditioning.mode",
    }
    if len(keys) == 1:
        definition["default"] = keys[0]
    binding = {
        "semantic_key": "CONDITIONING_MODE",
        "node_id": "__workflow__",
        "input_name": "conditioning_mode",
        "value_type": "enum",
        "is_required": True,
    }
    return definition, binding


def _parameters_and_bindings(
    version: WorkflowTemplateVersion,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    parameters = dict(_contract_parameters(version))
    bindings = dict(_binding_map(version))
    definition, binding = _conditioning_mode_parameter(version)
    if definition is not None and binding is not None:
        parameters["CONDITIONING_MODE"] = definition
        bindings["CONDITIONING_MODE"] = binding
    return parameters, bindings


def _bounded_numeric(value: Any, definition: dict[str, Any]) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    minimum = definition.get("min")
    maximum = definition.get("max")
    result = value
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        result = max(result, minimum)
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        result = min(result, maximum)
    multiple = definition.get("multiple_of")
    if isinstance(multiple, int) and multiple > 1:
        result = max(multiple, int(round(float(result) / multiple)) * multiple)
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
            result = min(result, int(maximum))
    return int(result) if definition.get("type") == "integer" else result


def _default_smoke_value(key: str, definition: dict[str, Any]) -> Any:
    if "default" in definition:
        source = definition["default"]
        if key == "sampling.seed":
            return _bounded_numeric(1, definition)
        if key == "parameter.text.max_length" and isinstance(source, int):
            return _bounded_numeric(min(source, 64), definition)
        if key in {"parameter.seedvr2.resolution", "parameter.seedvr2.max_resolution"}:
            return _bounded_numeric(min(int(source), 1024), definition)
        if key == "parameter.image.scale_megapixels":
            return _bounded_numeric(min(float(source), 0.25), definition)
        if key == "image.megapixels":
            return _bounded_numeric(min(float(source), 0.4), definition)
        if isinstance(source, (int, float)) and not isinstance(source, bool):
            # Imported canvas defaults are not guaranteed to respect a custom
            # node's step/multiple constraint. Smoke fixtures must always be
            # executable, so normalize numeric defaults before validation.
            return _bounded_numeric(source, definition)
        return source
    if key in {"video.fps", "video.duration_ms", "video.frames"}:
        minimum = definition.get("min")
        candidate = minimum if isinstance(minimum, (int, float)) else 1
        return _bounded_numeric(candidate, definition)
    return {
        "prompt.positive": "smoke test",
        "prompt.negative": "",
        "sampling.seed": 1,
        "image.width": 256,
        "image.height": 256,
        "image.aspect_ratio": "16:9",
        "image.megapixels": 0.0625,
        "audio.enabled": False,
        "generation.batch_size": 1,
    }.get(key)


def _apply_grouped_smoke_defaults(
    values: dict[str, Any],
    parameters: dict[str, dict[str, Any]],
    explicit_keys: set[str],
) -> None:
    groups: dict[str, list[str]] = {}
    for key, definition in parameters.items():
        group = str(definition.get("selection_group") or "")
        if group and key in values:
            groups.setdefault(group, []).append(key)
    for keys in groups.values():
        if set(keys) != {"image.width", "image.height"}:
            continue
        if {"image.width", "image.height"} & explicit_keys:
            # A paired resolution selection is atomic.  Supplying only one
            # side would silently alter aspect ratio, so reject it below.
            if not {"image.width", "image.height"} <= explicit_keys:
                raise _fixture_error("values", "image.width/image.height 必须成对提交")
            continue
        width = values.get("image.width")
        height = values.get("image.height")
        if (
            isinstance(width, bool) or isinstance(height, bool)
            or not isinstance(width, int) or not isinstance(height, int)
            or width <= 0 or height <= 0
        ):
            continue
        target = min(max(width, height), 768)
        scale = target / max(width, height)
        values["image.width"] = _bounded_numeric(
            width * scale,
            parameters["image.width"],
        )
        values["image.height"] = _bounded_numeric(
            height * scale,
            parameters["image.height"],
        )


def _normalize_values(
    version: WorkflowTemplateVersion,
    requested: Any,
) -> tuple[dict[str, Any], int, str]:
    if requested is None:
        requested = {}
    if not isinstance(requested, dict):
        raise _fixture_error("values", "必须是对象")
    if len(requested) > _fixture_max_values():
        raise _fixture_error("values", "参数项过多")
    parameters, bindings = _parameters_and_bindings(version)
    allowed = {
        key for key, row in bindings.items()
        if str(row.get("value_type") or "") not in _ASSET_VALUE_TYPES | {"model_artifact"}
        and key != "output.prefix"
        and not parameters.get(key, {}).get("asset_group_constant")
    }
    unknown = sorted(set(requested) - allowed)
    if unknown:
        raise _fixture_error(
            "values", "包含未声明或不可覆盖的参数: " + ", ".join(unknown),
        )
    values: dict[str, Any] = {}
    for key in sorted(allowed):
        definition = parameters.get(key)
        if not isinstance(definition, dict):
            raise ValidationError({"fixture": f"{key} 缺少参数定义"})
        value_type = str(definition.get("type") or "")
        value = requested[key] if key in requested else _default_smoke_value(key, definition)
        required = bool(definition.get("required", True)) or bool(
            bindings[key].get("is_required", True),
        )
        if value is None:
            if required:
                raise _fixture_error("values", f"缺少必填参数 {key}")
            continue
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) > JSON_INTEROPERABLE_INTEGER_MAX
        ):
            raise _fixture_error("values", f"{key} 超出浏览器可精确表达的整数范围")
        try:
            values[key] = _validate_value(key, value, definition, value_type)
        except WorkflowSemanticError as exc:
            raise _fixture_error("values", str(exc)) from exc
    _apply_grouped_smoke_defaults(values, parameters, set(requested))
    for key, value in values.items():
        try:
            values[key] = _validate_value(
                key,
                value,
                parameters[key],
                str(parameters[key].get("type") or ""),
            )
        except WorkflowSemanticError as exc:
            raise _fixture_error("values", str(exc)) from exc
    payload = canonical_json_bytes(values)
    values_max_bytes = _fixture_values_max_bytes()
    if len(payload) > values_max_bytes:
        raise _fixture_error(
            "values",
            f"规范化参数超过安全边界 {values_max_bytes} bytes",
        )
    return values, len(payload), json_contract(
        values,
        max_bytes=_fixture_values_max_bytes(),
    ).sha256


def _execution_parameter_contract(
    version: WorkflowTemplateVersion,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    parameters, bindings = _parameters_and_bindings(version)
    editable: dict[str, dict[str, Any]] = {}
    for key, row in bindings.items():
        value_type = str(row.get("value_type") or "")
        if (
            value_type not in {"string", "integer", "number", "boolean", "enum"}
            or key.startswith("prompt.")
            or key.startswith("model.")
            or key in _EXECUTION_SERVER_MANAGED_KEYS
        ):
            continue
        definition = parameters.get(key)
        if not isinstance(definition, dict) or definition.get("type") != value_type:
            raise ValidationError({"workflow_inputs": f"{key} 参数合同不一致"})
        if definition.get("asset_group_constant"):
            continue
        editable[key] = definition
    return editable, bindings


def _execution_default_value(
    key: str,
    definition: dict[str, Any],
) -> Any:
    if "default" in definition:
        return _bounded_numeric(definition["default"], definition)
    return None


def _normalize_execution_values(
    version: WorkflowTemplateVersion,
    requested: Any,
) -> dict[str, Any]:
    if requested is None:
        requested = {}
    if not isinstance(requested, dict):
        raise ValidationError({"workflow_inputs": "values 必须是对象"})
    if len(requested) > _fixture_max_values():
        raise ValidationError({"workflow_inputs": "values 参数项过多"})
    editable, bindings = _execution_parameter_contract(version)
    unknown = sorted(set(requested) - set(editable))
    if unknown:
        raise ValidationError({
            "workflow_inputs": "values 包含未声明或由服务端管理的参数: " + ", ".join(unknown),
        })
    values: dict[str, Any] = {}
    for key in sorted(editable):
        definition = editable[key]
        required = bool(definition.get("required", True)) or bool(
            bindings[key].get("is_required", True),
        )
        value = (
            requested[key]
            if key in requested
            else _execution_default_value(key, definition)
        )
        if value is None:
            if required:
                raise ValidationError({"workflow_inputs": f"values 缺少必填参数 {key}"})
            continue
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) > JSON_INTEROPERABLE_INTEGER_MAX
        ):
            raise ValidationError({"workflow_inputs": f"{key} 超出浏览器可精确表达的整数范围"})
        try:
            values[key] = _validate_value(
                key,
                value,
                definition,
                str(definition.get("type") or ""),
            )
        except WorkflowSemanticError as exc:
            raise ValidationError({"workflow_inputs": str(exc)}) from exc
    # Paired dimensions are one user decision.  Reject a half override instead
    # of silently mixing a submitted width with a default height.
    explicit = set(requested)
    if bool({"image.width", "image.height"} & explicit) and not {
        "image.width", "image.height",
    } <= explicit:
        raise ValidationError({
            "workflow_inputs": "image.width/image.height 必须成对提交",
        })
    json_contract(values, max_bytes=_fixture_values_max_bytes())
    return values


def _asset_slot_contracts(
    version: WorkflowTemplateVersion,
) -> dict[str, dict[str, Any]]:
    parameters = _contract_parameters(version)
    bindings = _binding_map(version)
    slots: dict[str, dict[str, Any]] = {}
    seen_positions: set[tuple[str, int]] = set()
    for key, row in bindings.items():
        value_type = str(row.get("value_type") or "")
        if value_type not in _ASSET_VALUE_TYPES:
            continue
        definition = parameters.get(key)
        if not isinstance(definition, dict) or definition.get("type") != value_type:
            raise ValidationError({"fixture": f"{key} 素材参数合同不一致"})
        media_kind = _MEDIA_BY_VALUE_TYPE[value_type]
        if str(definition.get("media_kind") or media_kind) != media_kind:
            raise ValidationError({"fixture": f"{key} 素材媒体类型合同不一致"})
        input_slot = definition.get("input_slot")
        minimum = definition.get("min_items")
        maximum = definition.get("max_items")
        if (
            isinstance(input_slot, bool)
            or not isinstance(input_slot, int)
            or input_slot <= 0
            or maximum != 1
            or minimum not in {0, 1}
        ):
            raise ValidationError({
                "fixture": f"{key} 不是当前支持的固定单素材槽位合同",
            })
        required = bool(definition.get("required", True)) or bool(
            row.get("is_required", True),
        )
        if minimum != (1 if required else 0):
            raise ValidationError({"fixture": f"{key} 素材槽 required/cardinality 不一致"})
        position = (media_kind, input_slot)
        if position in seen_positions:
            raise ValidationError({"fixture": "素材槽位编号重复"})
        seen_positions.add(position)
        slots[key] = {
            "semantic_key": key,
            "input_slot": input_slot,
            "media_kind": media_kind,
            "category": str(definition.get("asset_category") or media_kind),
            "required": required,
        }
    return slots


def _asset_fact(
    asset: Asset,
    slot: dict[str, Any],
    *,
    budget: ManagedSmokeAssetBudget | None,
    owner=None,
) -> dict[str, Any]:
    if owner is None:
        if asset.owner_id is not None or not asset.is_active:
            raise _fixture_error("assets", "烟测素材必须是启用的全局受管素材")
        if asset.file or not asset.local_path:
            raise _fixture_error("assets", "烟测素材必须来自只读素材库 local_path")
        try:
            root = Path(settings.ASSET_LIBRARY_ROOT).resolve(strict=True)
            source = Path(asset.local_path).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _fixture_error("assets", "受管素材当前不可读取") from exc
        if not source.is_relative_to(root) or not source.is_file():
            raise _fixture_error("assets", "素材路径越过受控素材库")
    else:
        try:
            resolved_asset, source = authorized_asset_source(
                owner=owner,
                asset_id=str(asset.id),
            )
        except ReferenceBindingError as exc:
            raise ValidationError({"workflow_inputs": str(exc)}) from exc
        if resolved_asset.id != asset.id:
            raise ValidationError({"workflow_inputs": "素材身份校验失败"})
    mime_type = str(asset.mime_type or "").strip().casefold()
    checksum = str(asset.checksum or "").strip().casefold()
    if (
        not mime_type.startswith(f"{slot['media_kind']}/")
        or not _SHA256.fullmatch(checksum)
    ):
        raise _fixture_error("assets", "素材 MIME 或 checksum 声明无效")
    try:
        size_bytes = source.stat().st_size
    except OSError as exc:
        raise ValidationError({"workflow_inputs": "素材当前不可读取"}) from exc
    if budget is not None:
        budget_reason = budget.claim(size_bytes)
        if budget_reason:
            raise _fixture_error("assets", f"素材超出烟测预算: {budget_reason}")
    if sha256_file(source) != checksum:
        raise _fixture_error("assets", "素材类型、大小或 checksum 已漂移")
    return {
        "semantic_key": slot["semantic_key"],
        "input_slot": slot["input_slot"],
        "asset_id": str(asset.id),
        "media_kind": slot["media_kind"],
        "category": slot["category"],
        "checksum": checksum,
        "mime_type": str(asset.mime_type or ""),
        "size_bytes": size_bytes,
    }


def _normalize_assets(
    version: WorkflowTemplateVersion,
    requested: Any,
    *,
    owner=None,
) -> list[dict[str, Any]]:
    if requested is None:
        requested = []
    maximum_assets = (
        _fixture_max_assets() if owner is None else max_structural_media_bindings()
    )
    if not isinstance(requested, list) or len(requested) > maximum_assets:
        raise _fixture_error("assets", "必须是有界数组")
    slots = _asset_slot_contracts(version)
    by_semantic: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(requested):
        if not isinstance(row, dict) or set(row) != {"semantic_key", "asset_id"}:
            raise _fixture_error(
                "assets", f"第 {index + 1} 项只能包含 semantic_key/asset_id",
            )
        key = str(row.get("semantic_key") or "")
        if key not in slots:
            raise _fixture_error("assets", f"未知素材槽 {key}")
        if key in by_semantic:
            raise _fixture_error("assets", f"素材槽 {key} 重复")
        by_semantic[key] = row
    missing = sorted(key for key, slot in slots.items() if slot["required"] and key not in by_semantic)
    from studio.services.workflow_asset_groups import validate_group_selection, AssetGroupError
    try:
        validate_group_selection(version.input_contract, set(by_semantic))
    except AssetGroupError as exc:
        raise _fixture_error("assets", str(exc)) from exc
    if missing:
        raise _fixture_error("assets", "缺少必填素材槽: " + ", ".join(missing))
    budget = ManagedSmokeAssetBudget() if owner is None else None
    facts: list[dict[str, Any]] = []
    for key in sorted(by_semantic):
        try:
            asset = Asset.objects.get(pk=by_semantic[key]["asset_id"])
        except (Asset.DoesNotExist, TypeError, ValueError) as exc:
            raise _fixture_error("assets", f"素材槽 {key} 的 Asset 不存在") from exc
        facts.append(_asset_fact(asset, slots[key], budget=budget, owner=owner))
    return facts


def _dynamic_reference_mode_contracts(
    version: WorkflowTemplateVersion,
) -> tuple[dict[str, Any], ...]:
    """Return every classified-reference mode supported by its compiler.

    The media limits, purposes and preprocessing rules come exclusively from
    the frozen conditioning contract.  Compiler IDs are used only as a
    controlled graph-adapter registry; model identifiers never select this
    path.
    """

    if not (
        version.compiler_id == "generic.comfy"
        and version.compiler_version == 3
    ):
        return ()
    conditioning = _resolved_workflow_conditioning_contract(version)
    modes = conditioning.get("modes") if isinstance(conditioning, dict) else []
    return tuple(
        mode for mode in (modes or [])
        if isinstance(mode, dict)
        and mode.get("input_kind") == "classified_references"
        and isinstance(mode.get("dynamic_reference_topology"), dict)
    )


def _dynamic_reference_mode_contract(
    version: WorkflowTemplateVersion,
    conditioning_mode: str = "",
) -> dict[str, Any] | None:
    modes = _dynamic_reference_mode_contracts(version)
    selected = str(conditioning_mode or "").strip()
    if selected:
        return next((mode for mode in modes if mode.get("key") == selected), None)
    return modes[0] if len(modes) == 1 else None


def _reference_preprocessing_contract(
    mode_contract: dict[str, Any],
    media_group: str,
) -> dict[str, Any]:
    topology = mode_contract.get("dynamic_reference_topology")
    preprocessing = topology.get("preprocessing") if isinstance(topology, dict) else None
    result = preprocessing.get(media_group) if isinstance(preprocessing, dict) else None
    if not isinstance(result, dict):
        raise _fixture_error(
            "references",
            f"冻结 mode 缺少 {media_group} 预处理合同",
        )
    return result


def _validate_reference_duration(
    duration_ms: int,
    preprocessing: dict[str, Any],
    *,
    label: str,
) -> None:
    minimum = preprocessing.get("minimum_duration_ms")
    maximum = preprocessing.get("maximum_duration_ms")
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or not minimum <= duration_ms <= maximum
    ):
        raise _fixture_error(
            "references",
            f"{label}时长 {duration_ms}ms 不满足冻结范围 {minimum}–{maximum}ms",
        )


def _dynamic_asset_fact(
    asset: Asset,
    *,
    semantic_key: str,
    input_slot: int,
    media_kind: str,
    category: str,
    preprocessing: dict[str, Any] | None,
    budget: ManagedSmokeAssetBudget | None,
    owner=None,
) -> dict[str, Any]:
    if media_kind not in {"image", "video", "audio"} or input_slot <= 0:
        raise _fixture_error("references", "冻结的媒体类型或槽位无效")
    fact = _asset_fact(asset, {
        "semantic_key": semantic_key,
        "input_slot": input_slot,
        "media_kind": media_kind,
        "category": category,
    }, budget=budget, owner=owner)
    if owner is None:
        source = Path(asset.local_path or "")
    else:
        try:
            _resolved, source = authorized_asset_source(
                owner=owner,
                asset_id=str(asset.id),
            )
        except ReferenceBindingError as exc:
            raise ValidationError({"workflow_inputs": str(exc)}) from exc
    suffix = source.suffix.casefold()
    expected_extensions = {
        "image": IMAGE_EXTENSIONS,
        "video": VIDEO_EXTENSIONS,
        "audio": AUDIO_EXTENSIONS,
    }[media_kind]
    if suffix not in expected_extensions:
        raise _fixture_error("references", f"{media_kind} 素材扩展名不受支持")
    if media_kind == "video" and asset.kind != Asset.Kind.REFERENCE_VIDEO:
        raise _fixture_error("references", "参考视频必须来自 reference_video 素材")
    if media_kind == "audio" and asset.kind != Asset.Kind.REFERENCE_AUDIO:
        raise _fixture_error("references", "参考音频必须来自 reference_audio 素材")
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    duration_ms = None
    has_audio = None
    if media_kind in {"video", "audio"}:
        if not isinstance(preprocessing, dict):
            raise _fixture_error("references", f"{media_kind} 缺少冻结预处理合同")
        try:
            duration_ms = int(metadata.get("duration_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise _fixture_error("references", "参考音视频缺少已验证时长") from exc
        _validate_reference_duration(
            duration_ms,
            preprocessing,
            label="参考视频" if media_kind == "video" else "参考音频",
        )
    if media_kind == "video":
        has_audio = metadata.get("has_audio") is True
        raw_fps = metadata.get("source_fps", metadata.get("fps"))
        try:
            source_fps = float(raw_fps) if raw_fps not in (None, "", 0, "0") else None
        except (TypeError, ValueError) as exc:
            raise _fixture_error("references", "参考视频帧率事实无效") from exc
        if preprocessing.get("requires_source_fps") is True and not source_fps:
            raise _fixture_error("references", "冻结预处理合同要求参考视频具有已验证帧率")
        target_fps = preprocessing.get("target_fps")
        if owner is None and target_fps is not None and source_fps != float(target_fps):
            raise _fixture_error(
                "references",
                f"烟测参考视频必须预先满足冻结 target_fps={target_fps}",
            )
    else:
        source_fps = None
    return {
        **fact,
        "asset_kind": str(asset.kind),
        "source_suffix": suffix,
        "duration_ms": duration_ms,
        "has_audio": has_audio,
        "source_fps": source_fps,
    }


def _normalize_dynamic_references(
    version: WorkflowTemplateVersion,
    requested: Any,
    *,
    owner=None,
    conditioning_mode: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mode_contract = _dynamic_reference_mode_contract(version, conditioning_mode)
    if mode_contract is None:
        if requested not in (None, {}, {"pictures": [], "videos": [], "audios": []}):
            raise _fixture_error("references", "当前 workflow compiler 不支持动态参考素材")
        return [], []
    if requested is None:
        requested = {}
    if not isinstance(requested, dict) or set(requested) - {"pictures", "videos", "audios"}:
        raise _fixture_error("references", "只能包含 pictures/videos/audios")
    inputs = mode_contract.get("inputs")
    topology = mode_contract.get("dynamic_reference_topology")
    if not isinstance(inputs, dict) or not isinstance(topology, dict):
        raise _fixture_error("references", "冻结动态参考 mode 合同无效")
    raw_pictures = requested.get("pictures", [])
    picture_bounds = inputs.get("entity_images")
    if not isinstance(picture_bounds, dict):
        raise _fixture_error("references", "冻结 entity_images 基数合同缺失")
    picture_minimum = int(picture_bounds.get("minimum_items") or 0)
    picture_maximum = int(picture_bounds.get("maximum_items") or 0)
    if (
        not isinstance(raw_pictures, list)
        or not picture_minimum <= len(raw_pictures) <= picture_maximum
    ):
        raise _fixture_error(
            "references",
            f"pictures 数量必须在冻结范围 {picture_minimum}–{picture_maximum}",
        )
    pictures: list[dict[str, str]] = []
    allowed_picture_categories = {"character", "scene", "prop", "continuity_frame"}
    for index, item in enumerate(raw_pictures):
        if (
            not isinstance(item, dict)
            or set(item) != {"asset_id", "category"}
            or item.get("category") not in allowed_picture_categories
        ):
            raise _fixture_error(
                "references",
                f"pictures[{index}] 只能包含 asset_id 和受支持的 category",
            )
        pictures.append({
            "asset_id": str(item.get("asset_id") or ""),
            "category": str(item["category"]),
        })
    if sum(item["category"] == "continuity_frame" for item in pictures) > 1:
        raise _fixture_error("references", "continuity_frame 最多一张")
    pictures.sort(key=lambda item: item["category"] != "continuity_frame")
    try:
        videos = _normalize_videos(requested.get("videos", []))
        audios = _normalize_audios(requested.get("audios", []))
        validate_dynamic_reference_bindings_against_mode_contract(
            {"videos": videos, "audios": audios},
            mode_contract,
        )
    except ValueError as exc:
        raise _fixture_error("references", str(exc)) from exc
    physical_ids = [item["asset_id"] for item in pictures]
    physical_ids.extend(item["asset_id"] for item in videos)
    physical_ids.extend(
        item["audio_asset_id"] for item in videos if item.get("audio_asset_id")
    )
    physical_ids.extend(item["asset_id"] for item in audios)
    if not all(physical_ids) or len(physical_ids) != len(set(physical_ids)):
        raise _fixture_error("references", "所有图片、视频和音频 asset_id 必须非空且全局唯一")
    try:
        assets = {
            str(asset.id): asset
            for asset in Asset.objects.filter(pk__in=physical_ids)
        }
    except (TypeError, ValueError) as exc:
        raise _fixture_error("references", "包含无效 Asset UUID") from exc
    missing = [asset_id for asset_id in physical_ids if asset_id not in assets]
    if missing:
        raise _fixture_error("references", "引用的 Asset 不存在")

    budget = ManagedSmokeAssetBudget() if owner is None else None
    video_preprocessing = (
        _reference_preprocessing_contract(mode_contract, "videos")
        if videos else None
    )
    paired_audio_preprocessing = (
        _reference_preprocessing_contract(mode_contract, "paired_video_audios")
        if any(item.get("use_embedded_audio") or item.get("audio_asset_id") for item in videos)
        else None
    )
    reference_audio_preprocessing = (
        _reference_preprocessing_contract(mode_contract, "reference_audios")
        if audios else None
    )
    facts: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for picture_slot, item in enumerate(pictures, start=1):
        record_index = len(records) + 1
        asset = assets[item["asset_id"]]
        fact = _dynamic_asset_fact(
            asset,
            semantic_key=f"asset.reference.{record_index}",
            input_slot=picture_slot,
            media_kind="image",
            category=item["category"],
            preprocessing=None,
            budget=budget,
            owner=owner,
        )
        facts.append(fact)
        records.append({
            "category": item["category"],
            "entity_id": None if item["category"] == "continuity_frame" else str(asset.id),
            "entity_name": "" if item["category"] == "continuity_frame" else str(asset.name),
            "asset_id": str(asset.id),
            "id": str(asset.id),
            "input_slot": picture_slot,
            "input_label": f"<Picture {picture_slot}>",
            "checksum": fact["checksum"],
            "mime_type": fact["mime_type"],
        })
    audio_ordinal = 1
    for video_slot, item in enumerate(videos, start=1):
        record_index = len(records) + 1
        asset = assets[item["asset_id"]]
        fact = _dynamic_asset_fact(
            asset,
            semantic_key=f"asset.reference.{record_index}",
            input_slot=video_slot,
            media_kind="video",
            category="video",
            preprocessing=video_preprocessing,
            budget=budget,
            owner=owner,
        )
        if item.get("use_embedded_audio") and fact.get("has_audio") is not True:
            raise _fixture_error("references", "所选参考视频没有已验证的内嵌音轨")
        if item.get("use_embedded_audio"):
            _validate_reference_duration(
                int(fact["duration_ms"]),
                paired_audio_preprocessing or {},
                label="视频内嵌音轨",
            )
        facts.append(fact)
        record = {
            "category": "video",
            "asset_id": str(asset.id),
            "id": str(asset.id),
            "input_slot": video_slot,
            "input_label": f"<Video {video_slot}>",
            "checksum": fact["checksum"],
            "mime_type": fact["mime_type"],
            "purpose": item["purpose"],
            "use_embedded_audio": bool(item.get("use_embedded_audio", False)),
            "audio_asset_id": item.get("audio_asset_id"),
            "audio_input_label": None,
        }
        if item.get("use_embedded_audio") or item.get("audio_asset_id"):
            record["audio_input_label"] = f"<Audio {audio_ordinal}>"
            record["audio_purpose"] = item["audio_purpose"]
            if item.get("audio_asset_id"):
                audio_asset = assets[item["audio_asset_id"]]
                audio_fact = _dynamic_asset_fact(
                    audio_asset,
                    semantic_key=f"asset.reference.{record_index}.audio",
                    input_slot=audio_ordinal,
                    media_kind="audio",
                    category="video_audio",
                    preprocessing=paired_audio_preprocessing,
                    budget=budget,
                    owner=owner,
                )
                facts.append(audio_fact)
                record.update({
                    "audio_checksum": audio_fact["checksum"],
                    "audio_mime_type": audio_fact["mime_type"],
                })
            audio_ordinal += 1
        records.append(record)
    for independent_slot, item in enumerate(audios, start=1):
        record_index = len(records) + 1
        asset = assets[item["asset_id"]]
        fact = _dynamic_asset_fact(
            asset,
            semantic_key=f"asset.reference.{record_index}",
            input_slot=audio_ordinal,
            media_kind="audio",
            category="audio",
            preprocessing=reference_audio_preprocessing,
            budget=budget,
            owner=owner,
        )
        facts.append(fact)
        records.append({
            "category": "audio",
            "asset_id": str(asset.id),
            "id": str(asset.id),
            "input_slot": independent_slot,
            "input_label": f"<Audio {audio_ordinal}>",
            "checksum": fact["checksum"],
            "mime_type": fact["mime_type"],
            "purpose": item["purpose"],
        })
        audio_ordinal += 1
    maximum_assets = (
        _fixture_max_assets()
        if owner is None
        else max_structural_media_bindings()
    )
    if len(facts) > maximum_assets:
        raise _fixture_error(
            "references",
            f"动态参考物理素材总数最多 {maximum_assets} 个",
        )
    return records, facts


def normalize_workflow_smoke_fixture(
    version: WorkflowTemplateVersion,
    fixture: Any,
) -> FrozenWorkflowSmokeFixture:
    if not isinstance(fixture, dict):
        raise ValidationError({"fixture": "必须是对象"})
    if set(fixture) - {"schema_version", "values", "assets", "references"}:
        raise ValidationError({"fixture": "包含未注册字段"})
    if fixture.get("schema_version", FIXTURE_SCHEMA_VERSION) != FIXTURE_SCHEMA_VERSION:
        raise ValidationError({"fixture": "schema_version 不受支持"})
    values, _, _ = _normalize_values(version, fixture.get("values"))
    references, dynamic_assets = _normalize_dynamic_references(
        version,
        fixture.get("references"),
        conditioning_mode=str(values.get("CONDITIONING_MODE") or ""),
    )
    if references or _dynamic_reference_mode_contract(
        version,
        str(values.get("CONDITIONING_MODE") or ""),
    ) is not None:
        if fixture.get("assets") not in (None, []):
            raise _fixture_error("assets", "动态参考模式不接受固定 semantic 素材槽")
        assets = dynamic_assets
    else:
        assets = _normalize_assets(version, fixture.get("assets"))
    persisted = {
        "parameters": values,
        "reference_bindings": references,
        "media_facts": assets,
        "materialized_graph_sha256": "",
    }
    payload = canonical_json_bytes(persisted)
    fixture_max_bytes = _fixture_values_max_bytes()
    if len(payload) > fixture_max_bytes:
        raise _fixture_error(
            "fixture",
            f"规范化烟测事实超过安全边界 {fixture_max_bytes} bytes",
        )
    return FrozenWorkflowSmokeFixture(
        values=values,
        reference_bindings=tuple(references),
        asset_facts=tuple(assets),
        persisted_values=persisted,
        values_size_bytes=len(payload),
        values_sha256=json_contract(
            persisted,
            max_bytes=_fixture_values_max_bytes(),
        ).sha256,
        fixture_sha256="",
    )


def normalize_workflow_execution_inputs(
    version: WorkflowTemplateVersion,
    workflow_inputs: Any,
    *,
    owner,
) -> FrozenWorkflowExecutionInputs:
    """Normalize production inputs with the same workflow contracts as smoke.

    Smoke-only defaults and global-fixture restrictions deliberately stay out
    of this path.  The returned request contains only user-selected scalar
    values and Asset IDs; file paths and upload details are server-authored.
    """

    if workflow_inputs is None:
        workflow_inputs = {"schema_version": FIXTURE_SCHEMA_VERSION}
    if not isinstance(workflow_inputs, dict):
        raise ValidationError({"workflow_inputs": "必须是对象"})
    if set(workflow_inputs) - {"schema_version", "values", "assets", "references"}:
        raise ValidationError({"workflow_inputs": "包含未注册字段"})
    if workflow_inputs.get("schema_version", FIXTURE_SCHEMA_VERSION) != FIXTURE_SCHEMA_VERSION:
        raise ValidationError({"workflow_inputs": "schema_version 不受支持"})
    values = _normalize_execution_values(version, workflow_inputs.get("values"))
    try:
        references, dynamic_assets = _normalize_dynamic_references(
            version,
            workflow_inputs.get("references"),
            owner=owner,
            conditioning_mode=str(values.get("CONDITIONING_MODE") or ""),
        )
        dynamic_mode = _dynamic_reference_mode_contract(
            version,
            str(values.get("CONDITIONING_MODE") or ""),
        )
        if references or dynamic_mode is not None:
            if workflow_inputs.get("assets") not in (None, []):
                raise ValidationError({
                    "workflow_inputs": "动态参考模式不接受固定 semantic 素材槽",
                })
            asset_facts = dynamic_assets
            reference_bindings = references
        else:
            asset_facts = _normalize_assets(
                version,
                workflow_inputs.get("assets"),
                owner=owner,
            )
            reference_bindings = list(asset_facts)
    except ValidationError as exc:
        if hasattr(exc, "message_dict") and "workflow_inputs" in exc.message_dict:
            raise
        detail = "; ".join(exc.messages) if getattr(exc, "messages", None) else str(exc)
        raise ValidationError({"workflow_inputs": detail}) from exc

    if references:
        pictures = [{
            "asset_id": str(item["asset_id"]),
            "category": str(item["category"]),
        } for item in references if item.get("category") not in {"video", "audio"}]
        videos = [{
            "asset_id": str(item["asset_id"]),
            "purpose": str(item.get("purpose") or ""),
            "use_embedded_audio": bool(item.get("use_embedded_audio", False)),
            **({"audio_asset_id": str(item["audio_asset_id"])} if item.get("audio_asset_id") else {}),
            **({"audio_purpose": str(item["audio_purpose"])} if item.get("audio_purpose") else {}),
        } for item in references if item.get("category") == "video"]
        audios = [{
            "asset_id": str(item["asset_id"]),
            "purpose": str(item.get("purpose") or ""),
        } for item in references if item.get("category") == "audio"]
        normalized_assets: list[dict[str, str]] = []
    else:
        pictures, videos, audios = [], [], []
        normalized_assets = [{
            "semantic_key": str(item["semantic_key"]),
            "asset_id": str(item["asset_id"]),
        } for item in asset_facts]
    request = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "values": values,
        "assets": normalized_assets,
        "references": {
            "pictures": pictures,
            "videos": videos,
            "audios": audios,
        },
    }
    json_contract(request, max_bytes=_fixture_values_max_bytes())
    return FrozenWorkflowExecutionInputs(
        values=values,
        reference_bindings=tuple(reference_bindings),
        asset_facts=tuple(asset_facts),
        request=request,
    )


def finalize_workflow_smoke_fixture(
    fixture: FrozenWorkflowSmokeFixture,
    *,
    materialized_graph_sha256: str,
) -> FrozenWorkflowSmokeFixture:
    if not _SHA256.fullmatch(str(materialized_graph_sha256 or "").casefold()):
        raise _fixture_error("fixture", "materialized graph hash 无效")
    persisted = {
        "parameters": fixture.values,
        "reference_bindings": list(fixture.reference_bindings),
        "media_facts": list(fixture.asset_facts),
        "materialized_graph_sha256": str(materialized_graph_sha256).casefold(),
    }
    payload = canonical_json_bytes(persisted)
    fixture_max_bytes = _fixture_values_max_bytes()
    if len(payload) > fixture_max_bytes:
        raise _fixture_error(
            "fixture",
            f"规范化烟测事实超过安全边界 {fixture_max_bytes} bytes",
        )
    fixture_sha = json_contract(
        {
            "schema_version": FIXTURE_SCHEMA_VERSION,
            "values": persisted,
            "assets": list(fixture.asset_facts),
        },
        max_bytes=_fixture_values_max_bytes(),
    ).sha256
    return FrozenWorkflowSmokeFixture(
        values=fixture.values,
        reference_bindings=fixture.reference_bindings,
        asset_facts=fixture.asset_facts,
        persisted_values=persisted,
        values_size_bytes=len(payload),
        values_sha256=json_contract(
            persisted,
            max_bytes=_fixture_values_max_bytes(),
        ).sha256,
        fixture_sha256=fixture_sha,
        materialized_graph_sha256=str(materialized_graph_sha256).casefold(),
    )


def _dynamic_reference_schema(
    dynamic_mode: dict[str, Any],
    *,
    maximum_assets: int,
) -> dict[str, Any]:
    inputs = dynamic_mode["inputs"]
    topology = dynamic_mode["dynamic_reference_topology"]
    video_group = topology["videos"]
    audio_group = topology["audios"]
    pairing_rules = topology.get("pairing_rules") or []
    picture_maximum = min(
        int(inputs["total_images"]["maximum_items"]),
        int(inputs["entity_images"]["maximum_items"]),
    )
    maximum_physical_assets = min(
        maximum_assets,
        picture_maximum
        + int(inputs["reference_videos"]["maximum_items"])
        + int(inputs["paired_video_audios"]["maximum_items"])
        + int(inputs["reference_audios"]["maximum_items"]),
    )
    return {
        "mode": str(dynamic_mode["key"]),
        "conditioning_mode": dynamic_mode,
        "pictures": {
            "min_items": int(inputs["entity_images"]["minimum_items"]),
            "max_items": picture_maximum,
            "categories": ["character", "scene", "prop", "continuity_frame"],
        },
        "videos": {
            "min_items": int(video_group["minimum_items"]),
            "max_items": int(video_group["maximum_items"]),
            "purposes": [str(item["key"]) for item in video_group["purposes"]],
            "default_purpose_key": str(video_group.get("default_purpose_key") or ""),
            "supports_embedded_audio": any(
                rule.get("allow_embedded_source") is True for rule in pairing_rules
            ),
            "supports_external_audio": any(
                rule.get("allow_external_target") is True for rule in pairing_rules
            ),
        },
        "audios": {
            "min_items": int(audio_group["minimum_items"]),
            "max_items": int(audio_group["maximum_items"]),
            "purposes": [str(item["key"]) for item in audio_group["purposes"]],
            "default_purpose_key": str(audio_group.get("default_purpose_key") or ""),
        },
        "maximum_physical_assets": maximum_physical_assets,
    }


def workflow_smoke_fixture_schema(version: WorkflowTemplateVersion) -> dict[str, Any]:
    """Project an editable fixture form without changing version identity."""

    from studio.services.workflow_asset_groups import group_form_schema
    parameters, bindings = _parameters_and_bindings(version)
    mode_definition, _mode_binding = _conditioning_mode_parameter(version)
    seed_values = {}
    if mode_definition is not None and "default" not in mode_definition:
        seed_values["CONDITIONING_MODE"] = mode_definition["options"][0]
    recommended, _, _ = _normalize_values(version, seed_values)
    if seed_values:
        recommended["CONDITIONING_MODE"] = None
    value_rows = []
    for key in sorted(recommended):
        definition = parameters[key]
        value_rows.append({
            "semantic_key": key,
            "type": str(definition.get("type") or ""),
            "required": bool(definition.get("required", True)) or bool(
                bindings[key].get("is_required", True),
            ),
            "min": definition.get("min"),
            "max": definition.get("max"),
            "multiple_of": definition.get("multiple_of"),
            "alignment_modulus": definition.get("alignment_modulus"),
            "alignment_remainder": definition.get("alignment_remainder"),
            "options": [definition["const"]] if "const" in definition else list(definition.get("options") or [])
            if "const" in definition or definition.get("type") == "enum" else [],
            "selection_group": str(definition.get("selection_group") or ""),
            "recommended_value": recommended[key],
        })
    asset_rows = [{
        "semantic_key": key,
        "input_slot": slot["input_slot"],
        "media_kind": slot["media_kind"],
        "category": slot["category"],
        "min_items": 1 if slot["required"] else 0,
        "max_items": 1,
        "required": slot["required"],
    } for key, slot in sorted(_asset_slot_contracts(version).items())]
    dynamic_modes = {
        str(mode["key"]): _dynamic_reference_schema(
            mode,
            maximum_assets=_fixture_max_assets(),
        )
        for mode in _dynamic_reference_mode_contracts(version)
    }
    dynamic_schema = next(iter(dynamic_modes.values())) if len(dynamic_modes) == 1 else None
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "available": True,
        "parameters": value_rows,
        "asset_slots": asset_rows,
        "dynamic_references": dynamic_schema,
        "dynamic_reference_modes": dynamic_modes,
        "asset_groups": group_form_schema(version.input_contract),
    }


def workflow_execution_input_schema(version: WorkflowTemplateVersion) -> dict[str, Any]:
    """Project the editable production input surface for one immutable version."""
    from studio.services.workflow_asset_groups import group_form_schema

    editable, bindings = _execution_parameter_contract(version)
    parameters = []
    for key in sorted(editable):
        definition = editable[key]
        parameters.append({
            "semantic_key": key,
            "type": str(definition.get("type") or ""),
            "required": bool(definition.get("required", True)) or bool(
                bindings[key].get("is_required", True),
            ),
            "min": definition.get("min"),
            "max": definition.get("max"),
            "multiple_of": definition.get("multiple_of"),
            "alignment_modulus": definition.get("alignment_modulus"),
            "alignment_remainder": definition.get("alignment_remainder"),
            "min_length": definition.get("min_length"),
            "max_length": definition.get("max_length"),
            "options": [definition["const"]] if "const" in definition else list(definition.get("options") or [])
            if "const" in definition or definition.get("type") == "enum" else [],
            "selection_group": str(definition.get("selection_group") or ""),
            "recommended_value": _execution_default_value(key, definition),
        })
    smoke_schema = workflow_smoke_fixture_schema(version)
    dynamic_modes = {
        str(mode["key"]): _dynamic_reference_schema(
            mode,
            maximum_assets=max_structural_media_bindings(),
        )
        for mode in _dynamic_reference_mode_contracts(version)
    }
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "available": True,
        "reason_code": "",
        "parameters": parameters,
        "asset_slots": smoke_schema["asset_slots"],
        "dynamic_references": (
            next(iter(dynamic_modes.values())) if len(dynamic_modes) == 1 else None
        ),
        "dynamic_reference_modes": dynamic_modes,
        "asset_groups": group_form_schema(version.input_contract),
    }


def workflow_execution_conditioning_contract(
    version: WorkflowTemplateVersion,
    values: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    conditioning = _resolved_workflow_conditioning_contract(version)
    if conditioning is None:
        return None, None
    selected = str((values or {}).get("CONDITIONING_MODE") or "").strip()
    modes = conditioning.get("modes") or []
    if not selected and len(modes) == 1:
        selected = str(modes[0].get("key") or "")
    if not selected:
        raise ValidationError({"workflow_inputs": "必须选择 CONDITIONING_MODE"})
    return conditioning, conditioning_mode_contract(conditioning, selected)


def verify_persisted_workflow_smoke_fixture(
    attempt: WorkflowSmokeAttempt,
) -> FrozenWorkflowSmokeFixture:
    persisted = attempt.fixture_values if isinstance(attempt.fixture_values, dict) else None
    if persisted is None:
        raise ValidationError("workflow smoke fixture values 无效")
    payload = canonical_json_bytes(persisted)
    if (
        len(payload) != attempt.fixture_values_size_bytes
        or json_contract(
            persisted,
            max_bytes=_fixture_values_max_bytes(),
        ).sha256
        != attempt.fixture_values_sha256
    ):
        raise ValidationError("workflow smoke fixture values 完整性校验失败")
    if attempt.fixture_values_schema_version != FIXTURE_SCHEMA_VERSION:
        raise ValidationError("workflow smoke fixture schema version 不受支持")
    if set(persisted) != {
        "parameters", "reference_bindings", "media_facts", "materialized_graph_sha256",
    }:
        raise ValidationError("workflow smoke fixture v2 envelope 无效")
    values = persisted.get("parameters")
    references = persisted.get("reference_bindings")
    frozen_media_facts = persisted.get("media_facts")
    materialized_graph_sha256 = str(persisted.get("materialized_graph_sha256") or "")
    if (
        not isinstance(values, dict)
        or not isinstance(references, list)
        or any(not isinstance(row, dict) for row in references)
        or not isinstance(frozen_media_facts, list)
        or any(not isinstance(row, dict) for row in frozen_media_facts)
        or not _SHA256.fullmatch(materialized_graph_sha256.casefold())
    ):
        raise ValidationError("workflow smoke fixture v2 facts 无效")
    slots = _asset_slot_contracts(attempt.workflow_version)
    rows = list(attempt.asset_bindings.select_related("asset").order_by("semantic_key"))
    if len(rows) != len({row.semantic_key for row in rows}):
        raise ValidationError("workflow smoke fixture 素材绑定不唯一")
    by_semantic = {row.semantic_key: row for row in rows}
    budget = ManagedSmokeAssetBudget()
    facts: list[dict[str, Any]] = []
    dynamic_mode = _dynamic_reference_mode_contract(
        attempt.workflow_version,
        str(values.get("CONDITIONING_MODE") or ""),
    )
    if dynamic_mode is not None:
        expected_keys = {
            str(row.get("semantic_key") or "") for row in frozen_media_facts
        }
        if set(by_semantic) != expected_keys:
            raise ValidationError("workflow smoke fixture 动态素材集合已漂移")
        try:
            validate_dynamic_reference_bindings_against_mode_contract(
                {
                    "videos": [row for row in references if row.get("category") == "video"],
                    "audios": [row for row in references if row.get("category") == "audio"],
                },
                dynamic_mode,
            )
        except ValueError as exc:
            raise ValidationError("workflow smoke fixture 动态用途合同已漂移") from exc
    else:
        missing = sorted(
            key for key, slot in slots.items()
            if slot["required"] and key not in by_semantic
        )
        unknown = sorted(set(by_semantic) - set(slots))
        if missing or unknown or references:
            raise ValidationError("workflow smoke fixture 素材槽位合同已漂移")
    frozen_by_semantic = {
        str(row.get("semantic_key") or ""): row for row in frozen_media_facts
    }
    # Preserve the logical reference order used while finalizing the fixture.
    # Lexical semantic sorting would put asset.reference.10 before .2 and make a
    # valid dynamic attempt fail integrity verification after it is queued.
    ordered_keys = (
        [str(row.get("semantic_key") or "") for row in frozen_media_facts]
        if dynamic_mode is not None
        else sorted(by_semantic)
    )
    for key in ordered_keys:
        row = by_semantic[key]
        if dynamic_mode is not None:
            frozen_fact = frozen_by_semantic.get(key) or {}
            media_kind = str(frozen_fact.get("media_kind") or "")
            category = str(frozen_fact.get("category") or "")
            preprocessing_group = (
                "videos" if media_kind == "video"
                else "paired_video_audios" if category == "video_audio"
                else "reference_audios" if media_kind == "audio"
                else ""
            )
            fact = _dynamic_asset_fact(
                row.asset,
                semantic_key=key,
                input_slot=int(frozen_fact.get("input_slot") or 0),
                media_kind=media_kind,
                category=category,
                preprocessing=(
                    _reference_preprocessing_contract(dynamic_mode, preprocessing_group)
                    if preprocessing_group else None
                ),
                budget=budget,
            )
        else:
            fact = _asset_fact(row.asset, slots[key], budget=budget)
        frozen = {
            "semantic_key": row.semantic_key,
            "input_slot": row.input_slot,
            "asset_id": str(row.asset_id),
            "media_kind": row.media_kind,
            "category": row.category,
            "checksum": row.checksum,
            "mime_type": row.mime_type,
            "size_bytes": row.size_bytes,
        }
        if {key: fact[key] for key in frozen} != frozen:
            raise ValidationError("workflow smoke fixture 素材事实或文件已漂移")
        if dynamic_mode is not None and fact != frozen_by_semantic.get(key):
            raise ValidationError("workflow smoke fixture 媒体元数据或文件已漂移")
        facts.append(fact)
    fixture_sha = json_contract(
        {
            "schema_version": FIXTURE_SCHEMA_VERSION,
            "values": persisted,
            "assets": facts,
        },
        max_bytes=_fixture_values_max_bytes(),
    ).sha256
    if fixture_sha != attempt.fixture_sha256:
        raise ValidationError("workflow smoke fixture hash 不一致")
    return FrozenWorkflowSmokeFixture(
        values=values,
        reference_bindings=tuple(references),
        asset_facts=tuple(facts),
        persisted_values=persisted,
        values_size_bytes=len(payload),
        values_sha256=attempt.fixture_values_sha256,
        fixture_sha256=fixture_sha,
        materialized_graph_sha256=materialized_graph_sha256,
    )
