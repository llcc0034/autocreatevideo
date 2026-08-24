from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

from studio.models import Asset, PromptArtifact, SeriesProject
from studio.services.json_contracts import json_contract


ASSET_TOKEN_PREFIX = "__AUTOPROMPT_ASSET__:"
_PLACEHOLDER_RE = re.compile(r"\$\{[A-Z0-9_]+\}")
_PARAMETER_KEY_RE = re.compile(r"^parameter\.[a-z][a-z0-9_.-]{0,79}$")
_MODEL_KEY_RE = re.compile(r"^model\.([a-z][a-z0-9_.-]{0,79})\.filename$")
_ASSET_KEY_RE = re.compile(r"^asset\.(image|video|audio)\.([a-z][a-z0-9_.-]{0,79})$")

CORE_SEMANTICS = frozenset({
    "prompt.positive",
    "prompt.negative",
    "sampling.seed",
    "sampling.steps",
    "sampling.cfg_scale",
    "sampling.denoise",
    "sampling.sampler",
    "sampling.scheduler",
    "image.width",
    "image.height",
    "video.fps",
    "video.frames",
    "video.duration_ms",
    "audio.enabled",
    "generation.batch_size",
    "output.prefix",
})

VALUE_TYPES = frozenset({
    "string", "integer", "number", "boolean", "enum",
    "asset_image", "asset_video", "asset_audio", "model_artifact",
})


class WorkflowSemanticError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedWorkflowSemantics:
    values: dict[str, Any]
    asset_bindings: list[dict[str, Any]]
    model_requirements: list[dict[str, Any]]
    values_sha256: str
    asset_bindings_sha256: str
    model_requirements_sha256: str


def canonical_binding_rows(rows) -> list[dict[str, Any]]:
    values = [{
        "semantic_key": str(row["semantic_key"]),
        "node_id": str(row["node_id"]),
        "input_name": str(row["input_name"]),
        "value_type": str(row.get("value_type") or "string"),
        "is_required": bool(row.get("is_required", True)),
    } for row in rows]
    return sorted(values, key=lambda item: (
        item["semantic_key"], item["node_id"], item["input_name"],
    ))


def canonical_requirement_rows(rows) -> list[dict[str, Any]]:
    values = [{
        "role": str(row["role"]),
        "alias": str(row["alias"]),
        "artifact_id": str(row.get("artifact_id")) if row.get("artifact_id") else None,
        "expected_filename": str(row.get("expected_filename") or ""),
        "expected_sha256": str(row.get("expected_sha256") or ""),
        "is_required": bool(row.get("is_required", True)),
    } for row in rows]
    return sorted(values, key=lambda item: (item["role"], item["alias"]))


def execution_contract_payload(
    *,
    compiler_id: str,
    compiler_version: int,
    graph_sha256: str,
    input_contract_sha256: str,
    bindings: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    canonical_bindings = canonical_binding_rows(bindings)
    canonical_requirements = canonical_requirement_rows(requirements)
    return {
        "schema_version": 1,
        "compiler": {"id": compiler_id, "version": compiler_version},
        "graph_sha256": graph_sha256,
        "input_contract_sha256": input_contract_sha256,
        "semantic_bindings_sha256": json_contract(canonical_bindings).sha256,
        "model_requirements_sha256": json_contract(canonical_requirements).sha256,
    }


def validate_semantic_key(key: str) -> None:
    if key in CORE_SEMANTICS or _PARAMETER_KEY_RE.fullmatch(key):
        return
    if _MODEL_KEY_RE.fullmatch(key) or _ASSET_KEY_RE.fullmatch(key):
        return
    raise WorkflowSemanticError(f"未注册的 workflow semantic key: {key}")


def expected_value_type(key: str) -> str | None:
    if key in {"prompt.positive", "prompt.negative", "output.prefix"}:
        return "string"
    if key in {
        "sampling.seed", "sampling.steps", "image.width", "image.height",
        "video.frames", "video.duration_ms", "generation.batch_size",
    }:
        return "integer"
    if key in {"sampling.cfg_scale", "sampling.denoise", "video.fps"}:
        return "number"
    if key == "audio.enabled":
        return "boolean"
    if key in {"sampling.sampler", "sampling.scheduler"}:
        return "enum"
    if _MODEL_KEY_RE.fullmatch(key):
        return "model_artifact"
    asset_match = _ASSET_KEY_RE.fullmatch(key)
    if asset_match:
        return f"asset_{asset_match.group(1)}"
    return None


def _safe_output_prefix(value: str) -> str:
    raw = value.replace("\\", "/")
    normalized = raw.strip("/")
    parts = normalized.split("/")
    if (
        not normalized or len(normalized) > 300 or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in parts) or ":" in normalized
    ):
        raise WorkflowSemanticError("output.prefix 必须是安全的相对子路径")
    return normalized


def _validate_value(key: str, value: Any, definition: dict[str, Any], value_type: str) -> Any:
    if value_type == "string":
        if not isinstance(value, str):
            raise WorkflowSemanticError(f"{key} 必须是字符串")
        if key == "output.prefix":
            value = _safe_output_prefix(value)
        minimum_length = definition.get("min_length")
        maximum_length = definition.get("max_length")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise WorkflowSemanticError(f"{key} 长度不能小于 {minimum_length}")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            raise WorkflowSemanticError(f"{key} 长度不能大于 {maximum_length}")
    elif value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkflowSemanticError(f"{key} 必须是整数")
    elif value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise WorkflowSemanticError(f"{key} 必须是有限数值")
    elif value_type == "boolean":
        if not isinstance(value, bool):
            raise WorkflowSemanticError(f"{key} 必须是布尔值")
    elif value_type == "enum":
        options = definition.get("options")
        if not isinstance(options, list) or not options or value not in options:
            raise WorkflowSemanticError(f"{key} 不在声明的枚举范围内")
    elif value_type == "model_artifact":
        if not isinstance(value, str) or not value:
            raise WorkflowSemanticError(f"{key} 缺少冻结模型文件名")
    elif value_type.startswith("asset_"):
        if not isinstance(value, str) or not value.startswith(ASSET_TOKEN_PREFIX):
            raise WorkflowSemanticError(f"{key} 缺少受控资产引用")
    else:
        raise WorkflowSemanticError(f"{key} 使用不支持的类型 {value_type!r}")

    if value_type in {"integer", "number"}:
        minimum = definition.get("min")
        maximum = definition.get("max")
        multiple = definition.get("multiple_of")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise WorkflowSemanticError(f"{key} 不能小于 {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise WorkflowSemanticError(f"{key} 不能大于 {maximum}")
        if isinstance(multiple, int) and multiple > 0 and value % multiple:
            raise WorkflowSemanticError(f"{key} 必须是 {multiple} 的倍数")
    return value


def _asset_for_semantic(
    key: str,
    definition: dict[str, Any],
    *,
    prompt: PromptArtifact,
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    match = _ASSET_KEY_RE.fullmatch(key)
    if not match:
        raise WorkflowSemanticError(f"无效资产 semantic key: {key}")
    media_kind, slot = match.groups()
    records = prompt.structured_payload.get(
        "reference_bindings",
        prompt.structured_payload.get("reference_assets", []),
    )
    if not isinstance(records, list):
        raise WorkflowSemanticError("Prompt 资产引用快照必须是数组")
    requested_category = str(definition.get("asset_category") or "").strip()
    candidates = []
    for record in records:
        if not isinstance(record, dict):
            continue
        category = str(record.get("category") or "")
        if requested_category and category != requested_category:
            continue
        if not requested_category and slot not in {"source", "reference"} and category != slot:
            continue
        asset_id = record.get("asset_id") or record.get("id")
        if asset_id:
            candidates.append((str(asset_id), record))
    if not candidates:
        return None, None
    if len(candidates) > 1:
        raise WorkflowSemanticError(f"{key} 匹配到多个资产；generic compiler 要求单一固定槽位")
    asset_id, record = candidates[0]
    try:
        asset = Asset.objects.get(pk=asset_id, is_active=True)
    except (Asset.DoesNotExist, ValueError) as exc:
        raise WorkflowSemanticError(f"{key} 引用的资产不存在或已停用") from exc
    if asset.owner_id not in (None, prompt.project.owner_id):
        raise WorkflowSemanticError(f"{key} 引用的资产不属于当前项目租户")
    checksum = str(asset.checksum or "").casefold()
    recorded_checksum = str(record.get("checksum") or "").casefold()
    if len(checksum) != 64 or (recorded_checksum and recorded_checksum != checksum):
        raise WorkflowSemanticError(f"{key} 引用的资产 checksum 未冻结或已漂移")
    mime_type = str(asset.mime_type or "").casefold()
    if not mime_type.startswith(f"{media_kind}/"):
        raise WorkflowSemanticError(f"{key} 资产 MIME 与声明类型不匹配")
    token = f"{ASSET_TOKEN_PREFIX}{asset.id}"
    return token, {
        "semantic_key": key,
        "asset_id": str(asset.id),
        "token": token,
        "media_kind": media_kind,
        "mime_type": asset.mime_type,
        "checksum": checksum,
        "category": str(record.get("category") or ""),
        "audio_asset_id": record.get("audio_asset_id"),
        "use_embedded_audio": bool(record.get("use_embedded_audio", False)),
    }


def resolve_workflow_semantics(
    *,
    contract: dict[str, Any],
    bindings: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    prompt: PromptArtifact,
    project: SeriesProject,
    overrides: dict[str, Any] | None = None,
    output_prefix: str | None = None,
) -> ResolvedWorkflowSemantics:
    parameters = contract.get("parameters")
    if not isinstance(parameters, dict) or not parameters:
        raise WorkflowSemanticError("input_contract.parameters 必须是非空对象")
    overrides = dict(overrides or {})
    unknown_overrides = sorted(set(overrides) - set(parameters))
    if unknown_overrides:
        raise WorkflowSemanticError("未声明的 semantic override: " + ", ".join(unknown_overrides))
    requirement_map = {row["alias"]: row for row in canonical_requirement_rows(requirements)}
    references: list[dict[str, Any]] = []
    values: dict[str, Any] = {}
    duration_ms = int(prompt.total_duration_ms or 0)
    fps = int(project.fps or 24)
    builtins = {
        "prompt.positive": prompt.prompt_text,
        "prompt.negative": prompt.negative_prompt,
        "sampling.seed": int(project.continuity_seed),
        "image.width": int(project.width),
        "image.height": int(project.height),
        "video.fps": fps,
        "video.duration_ms": duration_ms,
        "video.frames": max(1, round(duration_ms * fps / 1000)),
        "audio.enabled": bool(prompt.include_audio),
        "generation.batch_size": 1,
        "output.prefix": output_prefix or f"autoprompt/{project.id}/{prompt.id}",
    }
    bound_keys = {row["semantic_key"] for row in bindings}
    for key in sorted(bound_keys):
        validate_semantic_key(key)
        definition = parameters.get(key)
        if not isinstance(definition, dict):
            raise WorkflowSemanticError(f"{key} 缺少 input_contract 参数定义")
        value_type = str(definition.get("type") or "")
        if value_type not in VALUE_TYPES:
            raise WorkflowSemanticError(f"{key} 参数类型无效")
        expected = expected_value_type(key)
        if expected is not None and value_type != expected and not (
            key == "video.fps" and value_type == "integer"
        ):
            raise WorkflowSemanticError(f"{key} 必须使用 {expected} 类型")
        if key in overrides:
            if value_type in {"model_artifact", "asset_image", "asset_video", "asset_audio"}:
                raise WorkflowSemanticError(f"{key} 禁止运行时覆盖")
            value = overrides[key]
        elif key in builtins:
            value = builtins[key]
        else:
            model_match = _MODEL_KEY_RE.fullmatch(key)
            asset_match = _ASSET_KEY_RE.fullmatch(key)
            if model_match:
                alias = str(definition.get("requirement_alias") or model_match.group(1))
                requirement = requirement_map.get(alias)
                if not requirement or not requirement.get("expected_filename"):
                    raise WorkflowSemanticError(f"{key} 找不到冻结的 ModelRequirement alias {alias}")
                value = requirement["expected_filename"]
            elif asset_match:
                value, manifest = _asset_for_semantic(key, definition, prompt=prompt)
                if manifest is not None:
                    references.append(manifest)
            elif "default" in definition:
                value = definition["default"]
            else:
                value = None
        required = bool(definition.get("required", True))
        if value is None:
            if required:
                raise WorkflowSemanticError(f"缺少必填 semantic: {key}")
            continue
        values[key] = _validate_value(key, value, definition, value_type)

    for row in bindings:
        key = row["semantic_key"]
        declared_type = str(row.get("value_type") or "string")
        definition = parameters.get(key) or {}
        if declared_type != definition.get("type"):
            raise WorkflowSemanticError(f"{key} binding 类型与 input_contract 不一致")
        if bool(row.get("is_required", True)) and key not in values:
            raise WorkflowSemanticError(f"binding 必填 semantic 未解析: {key}")

    canonical_requirements = canonical_requirement_rows(requirements)
    return ResolvedWorkflowSemantics(
        values=values,
        asset_bindings=sorted(references, key=lambda item: item["semantic_key"]),
        model_requirements=canonical_requirements,
        values_sha256=json_contract(values).sha256,
        asset_bindings_sha256=json_contract(sorted(references, key=lambda item: item["semantic_key"])).sha256,
        model_requirements_sha256=json_contract(canonical_requirements).sha256,
    )


def contains_legacy_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return any(contains_legacy_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_legacy_placeholder(item) for item in value)
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.search(value))
