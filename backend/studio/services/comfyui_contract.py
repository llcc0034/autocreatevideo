from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re

from studio.services.comfyui import ComfyUIClient, ComfyUIError
from studio.services.workflow import WorkflowBuildError, validate_comfy_graph


MAX_CONTRACT_ISSUES = 40
MAX_AUTOGROW_INPUTS = 128
_STAGED_ASSET_PREFIX = "__AUTOPROMPT_ASSET__:"
_WIDGET_TYPES = frozenset({
    "BOOLEAN",
    "COMBO",
    "COMFY_DYNAMICCOMBO_V3",
    "FLOAT",
    "INT",
    "NUMBER",
    "STRING",
})
_ANY_TYPES = frozenset({"*", "MATCHTYPE"})


class ComfyUIContractError(ComfyUIError):
    """The built API graph is incompatible with the connected ComfyUI schema."""

    def __init__(self, issues: list[str] | tuple[str, ...]):
        normalized = tuple(str(item) for item in issues if str(item).strip())
        self.issues = normalized
        visible = normalized[:8]
        detail = "；".join(visible)
        if len(normalized) > len(visible):
            detail += f"；另有 {len(normalized) - len(visible)} 项"
        super().__init__(
            "ComfyUI 入队前契约预检失败，GPU 未提交且未创建 submission token"
            + (f"：{detail}" if detail else "")
        )


class ComfyUIContractUnavailable(ComfyUIError):
    """The live /object_info contract could not be read before submission."""


@dataclass(frozen=True)
class ComfyUIContractReport:
    node_count: int
    link_count: int
    class_types: tuple[str, ...]
    schema_sha256: str

    def as_dict(self) -> dict:
        value = asdict(self)
        value["class_types"] = list(self.class_types)
        return value


@dataclass(frozen=True)
class _InputSpec:
    name: str
    type_name: str
    config: dict
    required: bool
    enum_options: tuple | None = None

    @property
    def is_widget(self) -> bool:
        return self.enum_options is not None or self.type_name in _WIDGET_TYPES


def _append_issue(issues: list[str], message: str) -> None:
    if len(issues) < MAX_CONTRACT_ISSUES:
        issues.append(message)


def _parse_definition(raw, *, context: str) -> tuple[str, dict, tuple | None]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"{context} 没有有效的输入定义")
    raw_type = raw[0]
    config = raw[1] if len(raw) > 1 and isinstance(raw[1], dict) else {}
    if isinstance(raw_type, (list, tuple)):
        return "COMBO", config, tuple(raw_type)
    if not isinstance(raw_type, str) or not raw_type:
        raise ValueError(f"{context} 的输入类型无效")
    return raw_type, config, None


def _parse_link(value) -> tuple[str, int] | None:
    if not isinstance(value, list):
        return None
    if (
        len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
        and value[1] >= 0
    ):
        return value[0], value[1]
    return None


def _dynamic_combo_options(config: dict) -> tuple:
    raw_options = config.get("options")
    if not isinstance(raw_options, list):
        return ()
    found = []
    for item in raw_options:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            found.append(item["key"])
        elif isinstance(item, (str, int, float, bool)):
            found.append(item)
    return tuple(found)


def _input_specs(info: dict, api_inputs: dict, *, node_label: str) -> tuple[dict[str, _InputSpec], list[str]]:
    issues: list[str] = []
    input_schema = info.get("input")
    if not isinstance(input_schema, dict):
        return {}, [f"{node_label} 的 /object_info.input 不是对象"]

    specs: dict[str, _InputSpec] = {}
    dynamic_groups: list[tuple[str, dict, bool]] = []
    for category in ("required", "optional"):
        definitions = input_schema.get(category) or {}
        if not isinstance(definitions, dict):
            _append_issue(issues, f"{node_label} 的 /object_info.input.{category} 不是对象")
            continue
        for name, raw_definition in definitions.items():
            if not isinstance(name, str) or not name:
                _append_issue(issues, f"{node_label} 含无效输入名")
                continue
            try:
                type_name, config, enum_options = _parse_definition(
                    raw_definition, context=f"{node_label}.{name}",
                )
            except ValueError as exc:
                _append_issue(issues, str(exc))
                continue
            if type_name == "COMFY_AUTOGROW_V3":
                dynamic_groups.append((name, config, category == "required"))
                continue
            if type_name == "COMBO" and enum_options is None:
                configured_options = config.get("options")
                enum_options = (
                    tuple(configured_options)
                    if isinstance(configured_options, list) else None
                )
            elif type_name == "COMFY_DYNAMICCOMBO_V3":
                enum_options = _dynamic_combo_options(config)
            specs[name] = _InputSpec(
                name=name,
                type_name=type_name,
                config=config,
                required=category == "required",
                enum_options=enum_options,
            )

    for group_name, config, group_required in dynamic_groups:
        template = config.get("template")
        template_required = (
            template.get("input", {}).get("required", {})
            if isinstance(template, dict) else {}
        )
        prefix = template.get("prefix") if isinstance(template, dict) else None
        minimum = template.get("min", 0) if isinstance(template, dict) else None
        maximum = template.get("max") if isinstance(template, dict) else None
        if (
            not isinstance(template_required, dict)
            or len(template_required) != 1
            or not isinstance(prefix, str)
            or not prefix
            or not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or minimum < 0
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum < minimum
            or maximum > MAX_AUTOGROW_INPUTS
        ):
            _append_issue(issues, f"{node_label}.{group_name} 的 autogrow schema 不受支持")
            continue
        child_name, child_definition = next(iter(template_required.items()))
        try:
            child_type, child_config, child_options = _parse_definition(
                child_definition,
                context=f"{node_label}.{group_name}.{child_name}",
            )
        except ValueError as exc:
            _append_issue(issues, str(exc))
            continue
        if child_options is not None or child_type in _WIDGET_TYPES | {"COMFY_AUTOGROW_V3"}:
            _append_issue(issues, f"{node_label}.{group_name} 的 autogrow 子输入必须是连接端口")
            continue
        pattern = re.compile(
            rf"^{re.escape(group_name)}\.{re.escape(prefix)}(0|[1-9][0-9]*)$"
        )
        found: list[tuple[int, str]] = []
        for input_name in api_inputs:
            match = pattern.fullmatch(input_name)
            if match:
                found.append((int(match.group(1)), input_name))
        found.sort()
        indices = [index for index, _ in found]
        if indices and indices != list(range(len(indices))):
            _append_issue(
                issues,
                f"{node_label}.{group_name} 的 autogrow 槽位必须从 0 连续编号，实际为 {indices}",
            )
        if len(found) < minimum or (group_required and not found and minimum > 0):
            _append_issue(
                issues,
                f"{node_label}.{group_name} 至少需要 {minimum} 个 autogrow 输入",
            )
        for index, input_name in found:
            if index >= maximum:
                _append_issue(
                    issues,
                    f"{node_label}.{input_name} 超过 autogrow 最大槽位 {maximum - 1}",
                )
                continue
            specs[input_name] = _InputSpec(
                name=input_name,
                type_name=child_type,
                config=child_config,
                required=True,
                enum_options=child_options,
            )
    return specs, issues


def _compatible_types(actual: str, expected: str) -> bool:
    if actual in _ANY_TYPES or expected in _ANY_TYPES or actual == expected:
        return True
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    # Mirror ComfyUI's non-strict validate_node_input union behavior: comma-
    # separated socket types are compatible when they overlap.
    actual_types = {item.strip() for item in actual.split(",")}
    expected_types = {item.strip() for item in expected.split(",")}
    return bool(actual_types & expected_types)


def _has_dependency_cycle(api_graph: dict) -> bool:
    edges = {node_id: set() for node_id in api_graph}
    indegree = {node_id: 0 for node_id in api_graph}
    for target_id, node in api_graph.items():
        for value in node.get("inputs", {}).values():
            link = _parse_link(value)
            if link is None or link[0] not in api_graph:
                continue
            source_id = link[0]
            if target_id not in edges[source_id]:
                edges[source_id].add(target_id)
                indegree[target_id] += 1
    available = [node_id for node_id, count in indegree.items() if count == 0]
    visited = 0
    while available:
        source_id = available.pop()
        visited += 1
        for target_id in edges[source_id]:
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                available.append(target_id)
    return visited != len(api_graph)


def _check_numeric_constraints(value, config: dict, *, integer: bool) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        return "必须是整数" if integer else "必须是有限数值"
    if not integer and not math.isfinite(float(value)):
        return "必须是有限数值"
    minimum = config.get("min")
    maximum = config.get("max")
    if isinstance(minimum, (int, float)) and value < minimum:
        return f"不能小于 {minimum}"
    if isinstance(maximum, (int, float)) and value > maximum:
        return f"不能大于 {maximum}"
    # ``step`` is a UI increment hint, not part of ComfyUI's API validation
    # contract (execution.py enforces numeric conversion and min/max only).
    # Enforcing it here would incorrectly reject supported custom resolution
    # multiples such as 20 even though the live node accepts the resulting INT.
    return None


def _validate_widget_value(
    spec: _InputSpec,
    value,
    *,
    uploaded_paths: frozenset[str],
) -> str | None:
    if isinstance(value, str) and value.startswith(_STAGED_ASSET_PREFIX):
        return "仍包含未上传的素材占位符"
    options = spec.enum_options
    if options is not None:
        # Upload-backed loaders accept the relative path returned by
        # /upload/image. ComfyUI's LoadImage enum may list only root files and
        # LoadVideo/LoadAudio commonly expose an empty list, so enum membership
        # is not proof of validity for an uploaded subfolder path. The upload
        # response plus the absence of a staged placeholder is the contract.
        upload_backed = any(
            bool(spec.config.get(flag))
            for flag in ("image_upload", "video_upload", "audio_upload")
        )
        if upload_backed:
            if not isinstance(value, str) or not value:
                return "必须是 ComfyUI 上传接口返回的非空相对路径"
            if value in options or value in uploaded_paths:
                return None
            return "既不在实时文件枚举中，也不在本任务已确认的上传结果中"
        if options:
            if value not in options:
                return f"值 {value!r} 不在实时 /object_info 枚举中"
        else:
            return "实时 /object_info 未提供可用枚举值"
        if not isinstance(value, (str, int, float, bool)) or value == "":
            return "必须是非空枚举值"
        return None
    if spec.type_name == "STRING":
        return None if isinstance(value, str) else "必须是字符串"
    if spec.type_name == "BOOLEAN":
        return None if isinstance(value, bool) else "必须是布尔值"
    if spec.type_name == "INT":
        return _check_numeric_constraints(value, spec.config, integer=True)
    if spec.type_name in {"FLOAT", "NUMBER"}:
        return _check_numeric_constraints(value, spec.config, integer=False)
    if spec.type_name in {"COMBO", "COMFY_DYNAMICCOMBO_V3"}:
        return "实时 /object_info 未提供可验证的枚举选项"
    return f"不支持的 widget 类型 {spec.type_name}"


def validate_comfyui_api_contract(
    api_graph: dict,
    object_info: dict,
    *,
    uploaded_paths: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> ComfyUIContractReport:
    """Validate an executable API graph against the connected server contract.

    This is a pure, read-only check. It validates every node, required/optional
    and autogrow input, widget value, source output slot and connection type.
    It never calls ``/prompt`` and never modifies the graph.
    """

    try:
        validate_comfy_graph(api_graph)
    except WorkflowBuildError as exc:
        raise ComfyUIContractError([str(exc)]) from exc
    if not isinstance(object_info, dict) or not object_info:
        raise ComfyUIContractUnavailable(
            "ComfyUI /object_info 未返回有效对象；GPU 未提交且未创建 submission token"
        )

    confirmed_uploaded_paths = frozenset(
        value for value in uploaded_paths if isinstance(value, str) and value
    )
    issues: list[str] = []
    schemas: dict[str, dict] = {}
    outputs_by_node: dict[str, tuple[str, ...]] = {}
    input_specs_by_node: dict[str, dict[str, _InputSpec]] = {}

    for node_id, node in api_graph.items():
        class_type = node["class_type"]
        info = object_info.get(class_type)
        node_label = f"节点 {node_id} ({class_type})"
        if not isinstance(info, dict):
            _append_issue(issues, f"{node_label} 未安装或未在实时 /object_info 中注册")
            outputs_by_node[node_id] = ()
            input_specs_by_node[node_id] = {}
            continue
        schemas[class_type] = info
        outputs = info.get("output") or []
        if not isinstance(outputs, list) or not all(
            isinstance(item, str) and item for item in outputs
        ):
            _append_issue(issues, f"{node_label} 的输出 schema 无效")
            outputs_by_node[node_id] = ()
        else:
            outputs_by_node[node_id] = tuple(outputs)
        specs, spec_issues = _input_specs(info, node.get("inputs", {}), node_label=node_label)
        input_specs_by_node[node_id] = specs
        for issue in spec_issues:
            _append_issue(issues, issue)

    link_count = 0
    for node_id, node in api_graph.items():
        class_type = node["class_type"]
        node_label = f"节点 {node_id} ({class_type})"
        specs = input_specs_by_node.get(node_id, {})
        api_inputs = node.get("inputs", {})
        for name, spec in specs.items():
            if spec.required and name not in api_inputs:
                _append_issue(issues, f"{node_label}.{name} 缺少必填输入")
        for name, value in api_inputs.items():
            spec = specs.get(name)
            if spec is None:
                _append_issue(issues, f"{node_label}.{name} 不属于实时输入 schema")
                continue
            link = _parse_link(value)
            if isinstance(value, list) and link is None:
                _append_issue(issues, f"{node_label}.{name} 的链接格式无效")
                continue
            if link is None:
                if not spec.is_widget:
                    _append_issue(
                        issues,
                        f"{node_label}.{name} 需要 {spec.type_name} 连接，不能使用常量 {value!r}",
                    )
                    continue
                value_error = _validate_widget_value(
                    spec,
                    value,
                    uploaded_paths=confirmed_uploaded_paths,
                )
                if value_error:
                    _append_issue(issues, f"{node_label}.{name} {value_error}")
                continue

            link_count += 1
            source_id, source_slot = link
            if source_id not in api_graph:
                _append_issue(
                    issues,
                    f"{node_label}.{name} 引用了不存在的源节点 {source_id}",
                )
                continue
            source_outputs = outputs_by_node.get(source_id, ())
            if source_slot >= len(source_outputs):
                _append_issue(
                    issues,
                    f"{node_label}.{name} 引用了 {source_id}[{source_slot}]，"
                    f"但源节点仅有 {len(source_outputs)} 个输出",
                )
                continue
            actual_type = source_outputs[source_slot]
            if not _compatible_types(actual_type, spec.type_name):
                _append_issue(
                    issues,
                    f"{node_label}.{name} 需要 {spec.type_name}，"
                    f"但 {source_id}[{source_slot}] 输出 {actual_type}",
                )

    if _has_dependency_cycle(api_graph):
        _append_issue(issues, "ComfyUI API graph 含依赖环，无法安全入队")

    if issues:
        raise ComfyUIContractError(issues)

    canonical_schema = json.dumps(
        {name: schemas[name] for name in sorted(schemas)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ComfyUIContractReport(
        node_count=len(api_graph),
        link_count=link_count,
        class_types=tuple(sorted(schemas)),
        schema_sha256=hashlib.sha256(canonical_schema.encode("utf-8")).hexdigest(),
    )


def preflight_comfyui_workflow(
    client: ComfyUIClient,
    api_graph: dict,
    *,
    uploaded_paths: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> ComfyUIContractReport:
    """Fetch the live contract once and validate before any submission token/POST."""

    object_info_method = getattr(client, "object_info", None)
    if not callable(object_info_method):
        raise ComfyUIContractUnavailable(
            "ComfyUI 客户端不支持实时 /object_info 预检；GPU 未提交且未创建 submission token"
        )
    try:
        object_info = object_info_method()
    except ComfyUIError as exc:
        raise ComfyUIContractUnavailable(
            f"无法读取实时 ComfyUI /object_info：{exc}；"
            "GPU 未提交且未创建 submission token，可安全重试"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ComfyUIContractUnavailable(
            "实时 ComfyUI /object_info 响应无法解析；"
            "GPU 未提交且未创建 submission token，可安全重试"
        ) from exc
    return validate_comfyui_api_contract(
        api_graph,
        object_info,
        uploaded_paths=uploaded_paths,
    )
