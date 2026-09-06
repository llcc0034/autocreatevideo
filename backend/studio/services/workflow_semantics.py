from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

from studio.models import Asset, PromptArtifact, SeriesProject
from studio.services.comfyui_artifacts import safe_model_relative_filename
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int


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
    "image.aspect_ratio",
    "image.megapixels",
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


def _workflow_graph_max_bytes() -> int:
    return boundary_int("security.workflow.max_graph_bytes")


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
        "semantic_bindings_sha256": json_contract(
            canonical_bindings,
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256,
        "model_requirements_sha256": json_contract(
            canonical_requirements,
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256,
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
    if key in {"sampling.cfg_scale", "sampling.denoise", "video.fps", "image.megapixels"}:
        return "number"
    if key == "image.aspect_ratio":
        return "enum"
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


def validate_resolution_strategy(
    *,
    graph: dict[str, Any],
    contract: dict[str, Any],
    bindings: list[dict[str, Any]],
    output_kind: str,
) -> None:
    """Validate one truthful resolution authority for generic media graphs."""

    resolution_keys = {
        "image.width", "image.height", "image.aspect_ratio", "image.megapixels",
    }
    rows = [row for row in canonical_binding_rows(bindings) if row["semantic_key"] in resolution_keys]
    schema_version = contract.get("schema_version") if isinstance(contract, dict) else None
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise WorkflowSemanticError("input_contract.schema_version 必须是正整数")
    strategy = contract.get("resolution_strategy") if isinstance(contract, dict) else None
    if output_kind in {"text", "audio"}:
        if strategy is not None or rows:
            raise WorkflowSemanticError(
                f"{output_kind} workflow 不得声明图像 resolution_strategy",
            )
        return
    if not isinstance(strategy, dict):
        # Schema v2 pre-dates the explicit resolution_strategy field.  Its
        # only supported resolution shape was exact width/height bindings, so
        # reconstruct that one deterministic contract without rewriting the
        # immutable published version.  Schema v3+ must persist the strategy.
        if (
            isinstance(contract, dict)
            and schema_version < 3
            and rows
        ):
            by_node: dict[str, set[str]] = {}
            for row in rows:
                by_node.setdefault(row["node_id"], set()).add(row["semantic_key"])
            expected_keys = {"image.width", "image.height"}
            if by_node and all(keys == expected_keys for keys in by_node.values()):
                strategy = {
                    "mode": "explicit_width_height",
                    "node_ids": sorted(by_node),
                }
        if not isinstance(strategy, dict):
            raise WorkflowSemanticError("generic media workflow 缺少 resolution_strategy")
    mode = str(strategy.get("mode") or "")
    raw_node_ids = strategy.get("node_ids")
    if (
        not isinstance(raw_node_ids, list)
        or not raw_node_ids
        or not all(isinstance(value, str) and value for value in raw_node_ids)
        or raw_node_ids != sorted(set(raw_node_ids))
    ):
        raise WorkflowSemanticError("resolution_strategy.node_ids 必须是有序且唯一的非空节点列表")
    node_ids = set(raw_node_ids)

    def assert_target_rows(
        expected: dict[str, tuple[str, str]],
        *,
        required_class_type: str | None = None,
    ) -> None:
        if {row["semantic_key"] for row in rows} != set(expected):
            raise WorkflowSemanticError(
                f"{mode} resolution bindings 与策略不一致",
            )
        by_node: dict[str, set[str]] = {}
        for row in rows:
            input_name, value_type = expected[row["semantic_key"]]
            if row["input_name"] != input_name or row["value_type"] != value_type:
                raise WorkflowSemanticError(f"{row['semantic_key']} resolution binding 目标或类型无效")
            node = graph.get(row["node_id"])
            if not isinstance(node, dict) or row["input_name"] not in (node.get("inputs") or {}):
                raise WorkflowSemanticError("resolution binding 指向不存在的节点输入")
            if required_class_type and node.get("class_type") != required_class_type:
                raise WorkflowSemanticError(f"{mode} 只能绑定 {required_class_type}")
            by_node.setdefault(row["node_id"], set()).add(row["semantic_key"])
        if set(by_node) != node_ids or any(keys != set(expected) for keys in by_node.values()):
            raise WorkflowSemanticError("resolution_strategy.node_ids 未精确覆盖成对 resolution bindings")

    def assert_required_dimension_multiple() -> None:
        required_multiple = contract.get("required_dimension_multiple")
        if (
            isinstance(required_multiple, bool)
            or not isinstance(required_multiple, int)
            or required_multiple < 1
        ):
            raise WorkflowSemanticError(
                "explicit_width_height 缺少正整数 required_dimension_multiple",
            )
        parameters = contract.get("parameters")
        if not isinstance(parameters, dict):
            raise WorkflowSemanticError(
                "explicit_width_height 缺少 parameters 合同",
            )
        parameter_multiples: list[int] = []
        for key in ("image.width", "image.height"):
            definition = parameters.get(key)
            multiple = (
                definition.get("multiple_of")
                if isinstance(definition, dict) else None
            )
            if (
                isinstance(multiple, bool)
                or not isinstance(multiple, int)
                or multiple < 1
            ):
                raise WorkflowSemanticError(
                    f"{key} 缺少正整数 multiple_of",
                )
            parameter_multiples.append(multiple)
            if "default" in definition:
                try:
                    _validate_value(key, definition["default"], definition, "integer")
                except WorkflowSemanticError as exc:
                    raise WorkflowSemanticError(
                        f"{key} 的冻结 default 不满足尺寸倍数合同: {exc}",
                    ) from exc
            for row in rows:
                if row["semantic_key"] != key:
                    continue
                node = graph.get(row["node_id"])
                inputs = node.get("inputs") if isinstance(node, dict) else None
                source_value = (
                    inputs.get(row["input_name"])
                    if isinstance(inputs, dict) else None
                )
                try:
                    _validate_value(key, source_value, definition, "integer")
                except WorkflowSemanticError as exc:
                    raise WorkflowSemanticError(
                        f"{key} 的冻结 graph literal 不满足尺寸倍数合同: {exc}",
                    ) from exc
        if any(value != required_multiple for value in parameter_multiples):
            raise WorkflowSemanticError(
                "required_dimension_multiple 必须与 image.width/image.height "
                "multiple_of 完全一致",
            )

    if mode == "explicit_width_height":
        assert_target_rows({
            "image.width": ("width", "integer"),
            "image.height": ("height", "integer"),
        })
        assert_required_dimension_multiple()
        return
    if mode == "aspect_megapixels":
        assert_target_rows({
            "image.aspect_ratio": ("aspect_ratio", "enum"),
            "image.megapixels": ("megapixels", "number"),
        }, required_class_type="ResolutionSelector")
        raise WorkflowSemanticError(
            "aspect_megapixels 尺寸策略尚无已注册的生产运行适配器，"
            "不能导入或发布",
        )
    if mode == "source_image_derived":
        if rows:
            raise WorkflowSemanticError("source_image_derived 不得伪造 width/height resolution binding")
        from studio.services.workflow_resolution import (
            validate_source_image_resolution, WorkflowResolutionError,
        )
        try:
            validate_source_image_resolution(graph, contract, canonical_binding_rows(bindings))
        except WorkflowResolutionError as exc:
            raise WorkflowSemanticError(str(exc)) from exc
        return
    raise WorkflowSemanticError("resolution_strategy.mode 不受支持")


def _safe_output_prefix(value: str) -> str:
    raw = value.replace("\\", "/")
    normalized = raw.strip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or len(normalized) > boundary_int(
            "security.workflow.output_prefix_max_characters"
        )
        or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in parts) or ":" in normalized
    ):
        raise WorkflowSemanticError("output.prefix 必须是安全的相对子路径")
    return normalized


def _validate_value(key: str, value: Any, definition: dict[str, Any], value_type: str) -> Any:
    if "const" in definition and value != definition["const"]:
        raise WorkflowSemanticError(f"{key} 是 Projector 冻结的固定值，不能运行时覆盖")
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
        if multiple is not None:
            from studio.services.workflow_parameter_ranges import is_multiple
            if isinstance(multiple, bool) or not isinstance(multiple, (int, float)) or not math.isfinite(multiple) or multiple <= 0:
                raise WorkflowSemanticError(f"{key} 的数值步长合同无效")
            if not is_multiple(value, multiple):
                raise WorkflowSemanticError(f"{key} 必须是 {multiple} 的倍数")
        alignment_modulus = definition.get("alignment_modulus")
        alignment_remainder = definition.get("alignment_remainder")
        if alignment_modulus is not None or alignment_remainder is not None:
            if (
                value_type != "integer"
                or isinstance(alignment_modulus, bool)
                or not isinstance(alignment_modulus, int)
                or alignment_modulus < 1
                or isinstance(alignment_remainder, bool)
                or not isinstance(alignment_remainder, int)
                or not 0 <= alignment_remainder < alignment_modulus
            ):
                raise WorkflowSemanticError(f"{key} 的整数对齐合同无效")
            if value % alignment_modulus != alignment_remainder:
                raise WorkflowSemanticError(
                    f"{key} 必须满足 {alignment_remainder} + {alignment_modulus}k",
                )
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
    requested_slot = definition.get("input_slot")
    if isinstance(requested_slot, bool) or not isinstance(requested_slot, int) or requested_slot <= 0:
        raise WorkflowSemanticError(f"{key} 缺少合法 input_slot")
    candidates = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("input_slot") != requested_slot:
            continue
        recorded_semantic = str(record.get("semantic_key") or "")
        if recorded_semantic and recorded_semantic != key:
            continue
        category = str(record.get("category") or "")
        if requested_category not in {"image", "video", "audio"} and category != requested_category:
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
        "input_slot": requested_slot,
        "audio_asset_id": record.get("audio_asset_id"),
        "use_embedded_audio": bool(record.get("use_embedded_audio", False)),
    }


def _frozen_model_submission_value(
    key: str,
    *,
    requirement: dict[str, Any],
    graph: dict[str, Any],
    bindings: list[dict[str, Any]],
) -> str:
    """Resolve the exact immutable Comfy widget option for one model.

    Model requirements intentionally keep a platform-neutral filename plus a
    content SHA-256 as the portable identity.  Comfy combo widgets, however,
    can require the exact spelling exposed by the validated runtime (notably
    Windows backslashes).  The published graph is the immutable, reviewed
    source of that submission spelling.  Never reconstruct it from the current
    host or normalize it before submission.
    """

    expected_filename = safe_model_relative_filename(
        str(requirement.get("expected_filename") or ""),
    )
    expected_sha256 = str(requirement.get("expected_sha256") or "").casefold()
    if expected_filename is None or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise WorkflowSemanticError(
            f"{key} 缺少安全的 expected_filename/expected_sha256 模型身份",
        )

    targets = [row for row in bindings if row["semantic_key"] == key]
    if not targets:
        raise WorkflowSemanticError(f"{key} 缺少冻结 model widget binding")
    exact_values: list[str] = []
    for row in targets:
        node = graph.get(row["node_id"])
        inputs = node.get("inputs") if isinstance(node, dict) else None
        exact_value = (
            inputs.get(row["input_name"])
            if isinstance(inputs, dict) and row["input_name"] in inputs
            else None
        )
        if not isinstance(exact_value, str) or not exact_value:
            raise WorkflowSemanticError(
                f"{key} 的冻结 model widget 值缺失: "
                f"{row['node_id']}.{row['input_name']}",
            )
        if safe_model_relative_filename(exact_value) != expected_filename:
            raise WorkflowSemanticError(
                f"{key} 的冻结 model widget 值与 expected_filename 身份不一致",
            )
        if exact_value not in exact_values:
            exact_values.append(exact_value)
    if len(exact_values) != 1:
        raise WorkflowSemanticError(
            f"{key} 的多个 binding 使用了不同精确提交值；请拆分 semantic key",
        )
    return exact_values[0]


def resolve_workflow_semantics(
    *,
    graph: dict[str, Any],
    contract: dict[str, Any],
    bindings: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    prompt: PromptArtifact,
    project: SeriesProject,
    overrides: dict[str, Any] | None = None,
    output_prefix: str | None = None,
) -> ResolvedWorkflowSemantics:
    if not isinstance(graph, dict) or not graph:
        raise WorkflowSemanticError("workflow graph 必须是非空冻结对象")
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
    def project_fps() -> int:
        raw = getattr(project, "fps", None)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise WorkflowSemanticError(
                "video.fps 必须来自项目或冻结 workflow override 的正整数",
            )
        return raw

    def project_dimension(name: str) -> int:
        raw = getattr(project, name, None)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise WorkflowSemanticError(
                f"image.{name} 必须来自项目或冻结 workflow override 的正整数",
            )
        return raw

    def duration_ms() -> int:
        raw = getattr(prompt, "total_duration_ms", None)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise WorkflowSemanticError("video.duration_ms 必须是冻结的非负整数")
        return raw

    # Resolve built-ins lazily. Smoke compilation supplies every editable
    # scalar as a frozen override, so an unrelated text/image workflow must
    # never acquire synthetic dimensions or FPS merely because this mapping is
    # initialized.
    builtin_resolvers = {
        "prompt.positive": lambda: prompt.prompt_text,
        "prompt.negative": lambda: prompt.negative_prompt,
        "sampling.seed": lambda: int(project.continuity_seed),
        "image.width": lambda: project_dimension("width"),
        "image.height": lambda: project_dimension("height"),
        "image.aspect_ratio": lambda: str(project.aspect_ratio),
        "image.megapixels": lambda: round(
            project_dimension("width") * project_dimension("height") / (1024 * 1024),
            6,
        ),
        "video.fps": project_fps,
        "video.duration_ms": duration_ms,
        "video.frames": lambda: max(1, round(duration_ms() * project_fps() / 1000)),
        "audio.enabled": lambda: bool(prompt.include_audio),
        "output.prefix": lambda: output_prefix or f"autoprompt/{project.id}/{prompt.id}",
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
        elif definition.get("use_source_default") is True:
            if value_type in {"model_artifact", "asset_image", "asset_video", "asset_audio"}:
                raise WorkflowSemanticError(f"{key} 禁止通过 source default 绕过受管引用")
            if "default" not in definition:
                raise WorkflowSemanticError(f"{key} 声明 source default 但缺少冻结 default")
            value = definition["default"]
        elif key in builtin_resolvers:
            value = builtin_resolvers[key]()
        else:
            model_match = _MODEL_KEY_RE.fullmatch(key)
            asset_match = _ASSET_KEY_RE.fullmatch(key)
            if model_match:
                alias = str(definition.get("requirement_alias") or model_match.group(1))
                requirement = requirement_map.get(alias)
                if not requirement or not requirement.get("expected_filename"):
                    raise WorkflowSemanticError(f"{key} 找不到冻结的 ModelRequirement alias {alias}")
                value = _frozen_model_submission_value(
                    key,
                    requirement=requirement,
                    graph=graph,
                    bindings=bindings,
                )
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
        values_sha256=json_contract(
            values,
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256,
        asset_bindings_sha256=json_contract(
            sorted(references, key=lambda item: item["semantic_key"]),
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256,
        model_requirements_sha256=json_contract(
            canonical_requirements,
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256,
    )


def contains_legacy_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return any(contains_legacy_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_legacy_placeholder(item) for item in value)
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.search(value))
