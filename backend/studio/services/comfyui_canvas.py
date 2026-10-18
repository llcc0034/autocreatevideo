from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

from studio.services.workflow import WorkflowBuildError, validate_comfy_graph
from studio.services.comfyui_schema import (
    autogrow_canvas_names,
    dynamic_widget_definitions,
)
from studio.services.security_boundaries import boundary_int


COMFYUI_CANVAS_VERSION = 0.4


def _canvas_max_nodes() -> int:
    return boundary_int("security.comfy.canvas.max_nodes")


def _canvas_max_inputs_per_node() -> int:
    return boundary_int("security.comfy.canvas.max_inputs_per_node")


def _canvas_max_outputs_per_node() -> int:
    return boundary_int("security.comfy.canvas.max_outputs_per_node")


def _canvas_max_subgraph_depth() -> int:
    return boundary_int("security.comfy.canvas.max_subgraph_depth")

_WIDGET_TYPES = frozenset({
    "BOOLEAN",
    "COMBO",
    "COMFY_DYNAMICCOMBO_V3",
    "FLOAT",
    "INT",
    "NUMBER",
    "STRING",
})
_UPLOAD_WIDGETS = (
    ("image_upload", "IMAGEUPLOAD", "image"),
    ("video_upload", "VIDEOUPLOAD", "video"),
    ("audio_upload", "AUDIOUPLOAD", "audio"),
)


class ComfyUICanvasExportError(ValueError):
    """The reviewed API prompt cannot be represented as a safe UI workflow."""


class ComfyUICanvasImportError(ValueError):
    """A UI canvas cannot be deterministically reduced to an API prompt."""


@dataclass(frozen=True)
class ComfyUICanvasImportResult:
    """Side-effect-free result of reducing one reviewed UI canvas."""

    api_graph: dict[str, dict]
    node_id_map: dict[str, str]
    primary_output_node_ids: tuple[str, ...]
    output_candidates: tuple[dict, ...]
    conversion_report: dict


def _node_sort_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value)


def _as_definition(raw, *, context: str) -> tuple[str, dict, bool]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ComfyUICanvasExportError(f"{context} has no valid ComfyUI input definition.")
    type_spec = raw[0]
    config = raw[1] if len(raw) > 1 and isinstance(raw[1], dict) else {}
    if isinstance(type_spec, list):
        return "COMBO", config, True
    if not isinstance(type_spec, str) or not type_spec:
        raise ComfyUICanvasExportError(f"{context} has an unsupported ComfyUI input type.")
    return type_spec, config, type_spec in _WIDGET_TYPES


def _ordered_names(info: dict, category: str, definitions: dict) -> list[str]:
    configured = (info.get("input_order") or {}).get(category)
    ordered = [name for name in configured or [] if name in definitions]
    ordered.extend(name for name in definitions if name not in ordered)
    return ordered


def _looks_like_link(value, api_graph: dict) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0] in api_graph
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
        and value[1] >= 0
    )


def _default_widget_value(input_type: str, config: dict):
    if "default" in config:
        return config["default"]
    options = config.get("options")
    if isinstance(options, list) and options:
        return options[0]
    if input_type == "BOOLEAN":
        return False
    if input_type in {"INT", "FLOAT", "NUMBER"}:
        return 0
    return ""


def _normalized_combo_widget_value(value, config: dict):
    """Map a reviewed display label back to its exact Comfy API enum key.

    Some custom nodes persist a keyed combo as ``"KEY · localized label"``
    even though /object_info accepts only ``KEY``. Mapping is allowed only
    when one live option is an exact key or an unambiguous reviewed prefix;
    arbitrary labels remain untouched and fail later contract validation.
    """

    raw_options = config.get("options")
    if not isinstance(raw_options, (list, tuple)):
        return value
    keyed: list[tuple[object, object]] = []
    for option in raw_options:
        if isinstance(option, dict) and "key" in option:
            keyed.append((option["key"], option.get("label")))
        elif isinstance(option, (str, int, float, bool)):
            keyed.append((option, None))
    keys = [key for key, _label in keyed]
    if value in keys:
        return value
    label_matches = [key for key, label in keyed if label is not None and value == label]
    if len(label_matches) == 1:
        return label_matches[0]
    if isinstance(value, str):
        delimiters = (" · ", " - ", " — ", " (", "（")
        prefix_matches = [
            key for key, _label in keyed
            if isinstance(key, str)
            and value.startswith(key)
            and value[len(key):].startswith(delimiters)
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
    return value


def _autogrow_definition(
    group_name: str,
    raw_definition,
    api_inputs: dict,
) -> tuple[list[tuple[str, str, dict, str]], set[str]]:
    input_type, config, _ = _as_definition(
        raw_definition, context=f"autogrow input {group_name!r}"
    )
    if input_type != "COMFY_AUTOGROW_V3":
        return [], set()
    template = config.get("template")
    required = (
        template.get("input", {}).get("required", {})
        if isinstance(template, dict) else {}
    )
    prefix = template.get("prefix") if isinstance(template, dict) else None
    maximum = template.get("max") if isinstance(template, dict) else None
    if (
        not isinstance(required, dict)
        or len(required) != 1
        or not isinstance(prefix, str)
        or not prefix
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum < 0
        or maximum > _canvas_max_inputs_per_node()
    ):
        raise ComfyUICanvasExportError(
            f"autogrow input {group_name!r} has an unsupported schema."
        )
    template_name, child_definition = next(iter(required.items()))
    child_type, child_config, is_widget = _as_definition(
        child_definition, context=f"autogrow input {group_name!r}.{template_name}"
    )
    if is_widget:
        raise ComfyUICanvasExportError(
            f"autogrow input {group_name!r} unexpectedly contains a widget."
        )
    pattern = re.compile(rf"^{re.escape(group_name)}\.{re.escape(prefix)}(0|[1-9][0-9]*)$")
    found: list[tuple[int, str]] = []
    for name in api_inputs:
        match = pattern.fullmatch(name)
        if match:
            index = int(match.group(1))
            if index >= maximum:
                raise ComfyUICanvasExportError(
                    f"autogrow input {name!r} exceeds its declared maximum {maximum}."
                )
            found.append((index, name))
    found.sort()
    if len({index for index, _ in found}) != len(found):
        raise ComfyUICanvasExportError(f"autogrow input {group_name!r} has duplicate slots.")
    records = [
        (name, child_type, child_config, f"{prefix}{index}")
        for index, name in found
    ]
    # Preserve one empty trailing socket for editable expandable groups.
    next_index = max((index for index, _ in found), default=-1) + 1
    if next_index < maximum:
        name = f"{group_name}.{prefix}{next_index}"
        records.append((name, child_type, child_config, f"{prefix}{next_index}"))
    return records, {name for _, name in found}


def _node_size(class_type: str, inputs: list[dict], outputs: list[dict], multiline: bool) -> list[float]:
    height = max(100, 64 + 26 * max(len(inputs), len(outputs), 1))
    if multiline:
        height = max(height, 250)
    if class_type == "LoadImage":
        height = max(height, 320)
    width = 430 if multiline else 310
    return [float(width), float(height)]


def _topological_layout(api_graph: dict) -> tuple[list[str], dict[str, int], dict[str, list[float]]]:
    edges: dict[str, set[str]] = defaultdict(set)
    indegree = {node_id: 0 for node_id in api_graph}
    for target_id, node in api_graph.items():
        for value in node.get("inputs", {}).values():
            if not _looks_like_link(value, api_graph):
                continue
            source_id = value[0]
            if target_id not in edges[source_id]:
                edges[source_id].add(target_id)
                indegree[target_id] += 1
    available = sorted(
        (node_id for node_id, count in indegree.items() if count == 0),
        key=_node_sort_key,
    )
    ordered: list[str] = []
    depth = {node_id: 0 for node_id in api_graph}
    while available:
        node_id = available.pop(0)
        ordered.append(node_id)
        for target_id in sorted(edges[node_id], key=_node_sort_key):
            depth[target_id] = max(depth[target_id], depth[node_id] + 1)
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                available.append(target_id)
                available.sort(key=_node_sort_key)
    if len(ordered) != len(api_graph):
        raise ComfyUICanvasExportError("ComfyUI API graph contains a cycle.")
    per_depth: dict[int, int] = defaultdict(int)
    positions: dict[str, list[float]] = {}
    for node_id in ordered:
        column = depth[node_id]
        row = per_depth[column]
        per_depth[column] += 1
        positions[node_id] = [80.0 + column * 390.0, 80.0 + row * 240.0]
    return ordered, depth, positions


def api_graph_to_ui_workflow(
    api_graph: dict,
    object_info: dict,
    *,
    workflow_id: str | uuid.UUID | None = None,
    metadata: dict | None = None,
) -> dict:
    """Convert a reviewed ComfyUI API prompt to an importable UI canvas.

    ``object_info`` is used only as a read-only port schema.  The converter
    refuses unknown node types, inputs, links, and unbounded dynamic sockets;
    it never copies model lists or arbitrary frontend properties from the
    remote server into the export.
    """

    try:
        validate_comfy_graph(api_graph)
    except WorkflowBuildError as exc:
        raise ComfyUICanvasExportError(str(exc)) from exc
    if not isinstance(object_info, dict):
        raise ComfyUICanvasExportError("ComfyUI /object_info did not return an object.")
    if len(api_graph) > _canvas_max_nodes():
        raise ComfyUICanvasExportError(
            f"ComfyUI workflow exceeds the canvas export limit ({_canvas_max_nodes()} nodes)."
        )

    ordered_ids, _, positions = _topological_layout(api_graph)
    ui_id_by_api_id = {node_id: index + 1 for index, node_id in enumerate(ordered_ids)}
    nodes: list[dict] = []
    ui_node_by_api_id: dict[str, dict] = {}
    input_index_by_api_id: dict[str, dict[str, int]] = {}

    for order, node_id in enumerate(ordered_ids):
        api_node = api_graph[node_id]
        class_type = api_node["class_type"]
        info = object_info.get(class_type)
        if not isinstance(info, dict):
            raise ComfyUICanvasExportError(
                f"ComfyUI /object_info does not define node type {class_type!r}."
            )
        api_inputs = api_node["inputs"]
        input_sections = info.get("input")
        if not isinstance(input_sections, dict):
            raise ComfyUICanvasExportError(f"node type {class_type!r} has no valid input schema.")
        consumed: set[str] = set()
        input_records: list[tuple[str, str, dict, bool, str | None, bool]] = []
        # name, type, config, is_widget, dynamic_label, required
        for category in ("required", "optional"):
            definitions = input_sections.get(category) or {}
            if not isinstance(definitions, dict):
                raise ComfyUICanvasExportError(
                    f"node type {class_type!r} has an invalid {category} input schema."
                )
            for name in _ordered_names(info, category, definitions):
                raw_definition = definitions[name]
                parsed_type, parsed_config, is_widget = _as_definition(
                    raw_definition, context=f"{class_type}.{name}"
                )
                if parsed_config.get("hidden") and name not in api_inputs:
                    continue
                if parsed_type == "COMFY_AUTOGROW_V3":
                    dynamic_records, dynamic_consumed = _autogrow_definition(
                        name, raw_definition, api_inputs
                    )
                    consumed.update(dynamic_consumed)
                    input_records.extend(
                        (dynamic_name, dynamic_type, dynamic_config, False, label, False)
                        for dynamic_name, dynamic_type, dynamic_config, label in dynamic_records
                    )
                    continue
                input_records.append(
                    (name, parsed_type, parsed_config, is_widget, None, category == "required")
                )
                if name in api_inputs:
                    consumed.add(name)
                try:
                    children = dynamic_widget_definitions(
                        name, parsed_type, parsed_config,
                        selected_value=api_inputs.get(name),
                        resolve_value=lambda child_name, _definition: api_inputs.get(child_name),
                    )
                except ValueError as exc:
                    raise ComfyUICanvasExportError(str(exc)) from exc
                for child_name, child_definition, child_required in children:
                    child_type, child_config, child_widget = _as_definition(
                        child_definition, context=f"{class_type}.{child_name}",
                    )
                    if not child_widget:
                        raise ComfyUICanvasExportError(f"dynamic input {child_name!r} must be a bounded widget.")
                    if child_name not in api_inputs:
                        raise ComfyUICanvasExportError(
                            f"{'required' if child_required else 'optional'} dynamic input "
                            f"{node_id!r}.{child_name} is missing; positional canvas export requires explicit widget values."
                        )
                    input_records.append((child_name, child_type, child_config, True, None, child_required))
                    consumed.add(child_name)
        names = [record[0] for record in input_records]
        if len(set(names)) != len(names):
            raise ComfyUICanvasExportError(f"node {node_id!r} has colliding dynamic inputs.")
        unknown = sorted(set(api_inputs) - consumed)
        if unknown:
            raise ComfyUICanvasExportError(
                f"node {node_id!r} ({class_type}) contains unknown inputs: {', '.join(unknown)}."
            )
        if len(input_records) > _canvas_max_inputs_per_node():
            raise ComfyUICanvasExportError(
                f"node {node_id!r} exceeds the canvas input limit ({_canvas_max_inputs_per_node()})."
            )

        outputs_raw = info.get("output") or []
        output_names_raw = info.get("output_name") or []
        if not isinstance(outputs_raw, list) or len(outputs_raw) > _canvas_max_outputs_per_node():
            raise ComfyUICanvasExportError(f"node type {class_type!r} has invalid outputs.")
        outputs: list[dict] = []
        for output_index, output_type in enumerate(outputs_raw):
            if not isinstance(output_type, str) or not output_type:
                raise ComfyUICanvasExportError(
                    f"node type {class_type!r} has an unsupported output type."
                )
            name = (
                output_names_raw[output_index]
                if isinstance(output_names_raw, list)
                and output_index < len(output_names_raw)
                and isinstance(output_names_raw[output_index], str)
                else output_type
            )
            outputs.append({
                "name": name,
                "type": output_type,
                "links": None,
            })

        ui_inputs: list[dict] = []
        widget_values: list = []
        multiline = False
        for name, input_type, config, is_widget, dynamic_label, required in input_records:
            value_present = name in api_inputs
            value = api_inputs.get(name)
            is_link = value_present and _looks_like_link(value, api_graph)
            if required and not is_widget and not is_link:
                raise ComfyUICanvasExportError(
                    f"required connection {node_id!r}.{name} is missing or invalid."
                )
            if value_present and not is_widget and not is_link:
                raise ComfyUICanvasExportError(
                    f"connection {node_id!r}.{name} is not a valid node link."
                )
            record = {
                "name": name,
                "type": input_type,
                "link": None,
            }
            if dynamic_label is not None:
                record.update({
                    "label": dynamic_label,
                    "localized_name": name,
                    "shape": 7,
                })
            if is_widget:
                record["widget"] = {"name": name}
                widget_value = (
                    value
                    if value_present and not is_link
                    else _default_widget_value(input_type, config)
                )
                widget_values.append(widget_value)
                if config.get("control_after_generate"):
                    # Preserve the exact task seed when the exported canvas is run.
                    widget_values.append("fixed")
                multiline = multiline or bool(config.get("multiline"))
            ui_inputs.append(record)

        # File-loader upload buttons are synthetic frontend widgets rather than
        # API prompt inputs.  Recreate only the reviewed flags from object_info.
        for _, _, config, is_widget, _, _ in input_records:
            if not is_widget:
                continue
            for flag, upload_type, marker in _UPLOAD_WIDGETS:
                if config.get(flag):
                    ui_inputs.append({
                        "name": "upload",
                        "type": upload_type,
                        "widget": {"name": "upload"},
                        "link": None,
                    })
                    widget_values.append(marker)
                    break

        ui_node = {
            "id": ui_id_by_api_id[node_id],
            "type": class_type,
            "pos": positions[node_id],
            "size": _node_size(class_type, ui_inputs, outputs, multiline),
            "flags": {},
            "order": order,
            "mode": 0,
            "inputs": ui_inputs,
            "outputs": outputs,
            "properties": {"Node name for S&R": class_type},
            "widgets_values": widget_values,
        }
        nodes.append(ui_node)
        ui_node_by_api_id[node_id] = ui_node
        input_index_by_api_id[node_id] = {
            record["name"]: index for index, record in enumerate(ui_inputs)
        }

    links: list[list] = []
    next_link_id = 1
    for target_id in ordered_ids:
        api_inputs = api_graph[target_id]["inputs"]
        target_node = ui_node_by_api_id[target_id]
        for input_name, value in api_inputs.items():
            if not _looks_like_link(value, api_graph):
                continue
            if input_name not in input_index_by_api_id[target_id]:
                raise ComfyUICanvasExportError(
                    f"linked input {target_id!r}.{input_name} is absent from the UI schema."
                )
            source_id, source_slot = value
            source_node = ui_node_by_api_id[source_id]
            if source_slot >= len(source_node["outputs"]):
                raise ComfyUICanvasExportError(
                    f"link {source_id!r}[{source_slot}] -> {target_id!r}.{input_name} uses an invalid output slot."
                )
            target_slot = input_index_by_api_id[target_id][input_name]
            link_type = source_node["outputs"][source_slot]["type"]
            links.append([
                next_link_id,
                source_node["id"],
                source_slot,
                target_node["id"],
                target_slot,
                link_type,
            ])
            target_node["inputs"][target_slot]["link"] = next_link_id
            existing = source_node["outputs"][source_slot]["links"]
            if existing is None:
                source_node["outputs"][source_slot]["links"] = []
            source_node["outputs"][source_slot]["links"].append(next_link_id)
            next_link_id += 1

    if workflow_id is None:
        canonical = json.dumps(api_graph, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        workflow_id = uuid.uuid5(uuid.NAMESPACE_URL, f"autoprompt:comfyui-canvas:{digest}")
    try:
        normalized_workflow_id = str(uuid.UUID(str(workflow_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ComfyUICanvasExportError("workflow_id must be a valid UUID.") from exc
    safe_metadata = {
        key: value
        for key, value in (metadata or {}).items()
        if isinstance(key, str)
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    return {
        "id": normalized_workflow_id,
        "revision": 0,
        "last_node_id": max((node["id"] for node in nodes), default=0),
        "last_link_id": len(links),
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {
            "ds": {"scale": 0.8, "offset": [0.0, 0.0]},
            "autoprompt": {
                "source_format": "comfyui_api",
                "read_only_export": True,
                **safe_metadata,
            },
        },
        "version": COMFYUI_CANVAS_VERSION,
    }


_UI_ONLY_NODE_TYPES = frozenset({"Note", "MarkdownNote"})
_KEYED_UI_ONLY_WIDGET_VALUES = {
    # VideoHelperSuite stores this frontend preview-state object alongside
    # serializable API widgets. It is not an API input and must never be
    # copied into the executable graph. Unknown keys remain fail-closed.
    "VHS_VideoCombine": frozenset({"videopreview"}),
}
_MISSING = object()


@dataclass(frozen=True)
class _CanvasLink:
    link_id: int | str
    origin_id: int | str
    origin_slot: int
    target_id: int | str
    target_slot: int
    link_type: str


@dataclass(frozen=True)
class _ResolvedEndpoint:
    scoped_id: str
    slot: int
    output_type: str


@dataclass(frozen=True)
class _InputSpec:
    name: str
    input_type: str
    config: dict
    is_widget: bool
    required: bool
    ui_index: int | None


@dataclass
class _CanvasScope:
    prefix: str
    definition: dict | None
    parent_instance: "_CanvasNodeInstance | None"
    nodes_by_id: dict
    target_links: dict
    links_by_id: dict


@dataclass
class _CanvasNodeInstance:
    raw: dict
    scope: _CanvasScope
    scoped_id: str
    sort_path: tuple
    definition: dict | None = None
    child_scope: _CanvasScope | None = None


def _canvas_id(value, *, context: str) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ComfyUICanvasImportError(f"{context} must be an integer or string.")
    if isinstance(value, str) and (not value or len(value) > 256 or any(ord(char) < 32 for char in value)):
        raise ComfyUICanvasImportError(f"{context} is empty or unsafe.")
    return value


def _canvas_numeric_id(value, *, context: str) -> int | str:
    value = _canvas_id(value, context=context)
    if isinstance(value, int):
        if -(10**18) < value < 10**18:
            return value
    elif re.fullmatch(r"-?(0|[1-9][0-9]{0,17})", value):
        return value
    raise ComfyUICanvasImportError(f"{context} must use a bounded numeric canvas id.")


def _canvas_component(value: int | str) -> str:
    if isinstance(value, int):
        return str(value)
    escaped = value.replace("%", "%25").replace("/", "%2F")
    return f"s:{escaped}" if value.lstrip("-").isdigit() else escaped


def _canvas_sort_key(value: int | str) -> tuple[int, int | str]:
    return (0, value) if isinstance(value, int) else (1, value)


def _is_proxy(value, expected: int) -> bool:
    return value == expected or value == str(expected)


class _CanvasImportBuilder:
    def __init__(self, canvas: dict, object_info: dict, primary_output_node_ids) -> None:
        if not isinstance(canvas, dict) or canvas.get("version") != COMFYUI_CANVAS_VERSION:
            raise ComfyUICanvasImportError("canvas must be a ComfyUI 0.4 UI workflow.")
        if not isinstance(object_info, dict):
            raise ComfyUICanvasImportError("ComfyUI /object_info did not return an object.")
        if not isinstance(primary_output_node_ids, (list, tuple)) or not primary_output_node_ids:
            raise ComfyUICanvasImportError(
                "primary_output_node_ids must explicitly select at least one canvas node."
            )
        self.canvas = canvas
        self.object_info = object_info
        self.requested_primary_ids = tuple(primary_output_node_ids)
        self.definitions = self._definitions(canvas.get("definitions"))
        self.instances: dict[str, _CanvasNodeInstance] = {}
        self.instance_count = 0
        self.expanded_subgraphs: list[str] = []
        self.bypassed: set[str] = set()
        self._source_cache: dict[tuple[str, int | str, int], object] = {}
        self._resolving_sources: set[tuple[str, int | str, int]] = set()
        self._matchtype_cache: dict[tuple[str, str], str] = {}
        self._resolving_matchtypes: set[tuple[str, str]] = set()
        self._spec_cache: dict[str, tuple[_InputSpec, ...]] = {}
        self._widget_cache: dict[str, dict[str, object]] = {}
        self.top_scope = self._build_scope(
            canvas.get("nodes"),
            canvas.get("links"),
            prefix="top",
            definition=None,
            parent_instance=None,
            definition_stack=(),
            depth=0,
        )

    def _definitions(self, raw) -> dict[int | str, dict]:
        if raw is None:
            return {}
        if not isinstance(raw, dict) or not isinstance(raw.get("subgraphs"), list):
            raise ComfyUICanvasImportError("definitions.subgraphs must be a list.")
        definitions: dict[int | str, dict] = {}
        for index, definition in enumerate(raw["subgraphs"]):
            if not isinstance(definition, dict):
                raise ComfyUICanvasImportError(f"subgraph definition {index} is not an object.")
            definition_id = _canvas_id(definition.get("id"), context=f"subgraph definition {index} id")
            if definition_id in definitions:
                raise ComfyUICanvasImportError(f"duplicate subgraph definition id {definition_id!r}.")
            for field in ("inputs", "outputs", "nodes", "links"):
                if not isinstance(definition.get(field), list):
                    raise ComfyUICanvasImportError(
                        f"subgraph definition {definition_id!r}.{field} must be a list."
                    )
            for field in ("inputs", "outputs"):
                if len(definition[field]) > _canvas_max_inputs_per_node():
                    raise ComfyUICanvasImportError(
                        f"subgraph definition {definition_id!r}.{field} exceeds its limit."
                    )
                for port in definition[field]:
                    if (
                        not isinstance(port, dict)
                        or not isinstance(port.get("name"), str)
                        or not port["name"]
                        or not isinstance(port.get("type"), str)
                        or not port["type"]
                    ):
                        raise ComfyUICanvasImportError(
                            f"subgraph definition {definition_id!r} has an invalid {field} port."
                        )
            definitions[definition_id] = definition
        return definitions

    def _build_scope(
        self,
        raw_nodes,
        raw_links,
        *,
        prefix: str,
        definition: dict | None,
        parent_instance: _CanvasNodeInstance | None,
        definition_stack: tuple[int | str, ...],
        depth: int,
    ) -> _CanvasScope:
        if depth > _canvas_max_subgraph_depth():
            raise ComfyUICanvasImportError(
                f"subgraph expansion exceeds the maximum depth {_canvas_max_subgraph_depth()}."
            )
        if not isinstance(raw_nodes, list) or not isinstance(raw_links, list):
            raise ComfyUICanvasImportError(f"canvas scope {prefix!r} has invalid nodes or links.")
        if definition is None and not raw_nodes:
            raise ComfyUICanvasImportError("canvas nodes must be a non-empty list.")
        scope = _CanvasScope(prefix, definition, parent_instance, {}, {}, {})
        for index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                raise ComfyUICanvasImportError(f"node {index} in {prefix!r} is not an object.")
            node_id = _canvas_numeric_id(
                raw_node.get("id"),
                context=f"node {index} id in {prefix!r}",
            )
            if node_id in scope.nodes_by_id or (
                definition is not None and (_is_proxy(node_id, -10) or _is_proxy(node_id, -20))
            ):
                raise ComfyUICanvasImportError(f"duplicate or reserved node id {node_id!r} in {prefix!r}.")
            node_type = raw_node.get("type")
            inputs = raw_node.get("inputs")
            outputs = raw_node.get("outputs")
            if (
                not isinstance(node_type, str)
                or not node_type
                or not isinstance(inputs, list)
                or not isinstance(outputs, list)
                or len(inputs) > _canvas_max_inputs_per_node()
                or len(outputs) > _canvas_max_outputs_per_node()
            ):
                raise ComfyUICanvasImportError(f"node {node_id!r} in {prefix!r} has an invalid schema.")
            component = _canvas_component(node_id)
            scoped_id = f"{prefix}/{component}"
            if scoped_id in self.instances:
                raise ComfyUICanvasImportError(f"scoped node id {scoped_id!r} is ambiguous.")
            sort_path = (
                (_canvas_sort_key(node_id),)
                if parent_instance is None
                else parent_instance.sort_path + ((_canvas_sort_key(definition["id"])), _canvas_sort_key(node_id))
            )
            instance = _CanvasNodeInstance(raw_node, scope, scoped_id, sort_path)
            scope.nodes_by_id[node_id] = instance
            self.instances[scoped_id] = instance
            self.instance_count += 1
            if self.instance_count > _canvas_max_nodes():
                raise ComfyUICanvasImportError(
                    f"expanded canvas exceeds the node limit ({_canvas_max_nodes()})."
                )

        for instance in scope.nodes_by_id.values():
            definition_for_node = self.definitions.get(instance.raw["type"])
            if definition_for_node is None:
                continue
            definition_id = definition_for_node["id"]
            if definition_id in definition_stack:
                chain = " -> ".join(str(item) for item in (*definition_stack, definition_id))
                raise ComfyUICanvasImportError(f"recursive subgraph cycle detected: {chain}.")
            instance.definition = definition_for_node
            child_prefix = f"{instance.scoped_id}/{_canvas_component(definition_id)}"
            instance.child_scope = self._build_scope(
                definition_for_node["nodes"],
                definition_for_node["links"],
                prefix=child_prefix,
                definition=definition_for_node,
                parent_instance=instance,
                definition_stack=(*definition_stack, definition_id),
                depth=depth + 1,
            )
            self.expanded_subgraphs.append(instance.scoped_id)

        for index, raw_link in enumerate(raw_links):
            if definition is None:
                if not isinstance(raw_link, list) or len(raw_link) != 6:
                    raise ComfyUICanvasImportError(f"top-level link {index} must contain six values.")
                link_id, origin_id, origin_slot, target_id, target_slot, link_type = raw_link
            else:
                if not isinstance(raw_link, dict):
                    raise ComfyUICanvasImportError(
                        f"subgraph link {index} in {prefix!r} must be an object."
                    )
                link_id = raw_link.get("id")
                origin_id = raw_link.get("origin_id")
                origin_slot = raw_link.get("origin_slot")
                target_id = raw_link.get("target_id")
                target_slot = raw_link.get("target_slot")
                link_type = raw_link.get("type")
            link_id = _canvas_numeric_id(link_id, context=f"link {index} id in {prefix!r}")
            origin_id = _canvas_numeric_id(
                origin_id,
                context=f"link {index} origin in {prefix!r}",
            )
            target_id = _canvas_numeric_id(
                target_id,
                context=f"link {index} target in {prefix!r}",
            )
            if (
                isinstance(origin_slot, bool)
                or not isinstance(origin_slot, int)
                or origin_slot < 0
                or isinstance(target_slot, bool)
                or not isinstance(target_slot, int)
                or target_slot < 0
                or not isinstance(link_type, str)
                or not link_type
            ):
                raise ComfyUICanvasImportError(f"link {link_id!r} in {prefix!r} has invalid slots or type.")
            if link_id in scope.links_by_id:
                raise ComfyUICanvasImportError(f"duplicate link id {link_id!r} in {prefix!r}.")
            origin_proxy = definition is not None and _is_proxy(origin_id, -10)
            target_proxy = definition is not None and _is_proxy(target_id, -20)
            if not origin_proxy and origin_id not in scope.nodes_by_id:
                raise ComfyUICanvasImportError(f"link {link_id!r} has an unknown origin node.")
            if not target_proxy and target_id not in scope.nodes_by_id:
                raise ComfyUICanvasImportError(f"link {link_id!r} has an unknown target node.")
            if origin_proxy:
                if origin_slot >= len(definition["inputs"]):
                    raise ComfyUICanvasImportError(f"link {link_id!r} uses an invalid input proxy slot.")
            else:
                origin = scope.nodes_by_id[origin_id]
                if origin_slot >= len(origin.raw["outputs"]):
                    raise ComfyUICanvasImportError(f"link {link_id!r} uses an invalid source output slot.")
            if target_proxy:
                if target_slot >= len(definition["outputs"]):
                    raise ComfyUICanvasImportError(f"link {link_id!r} uses an invalid output proxy slot.")
            else:
                target = scope.nodes_by_id[target_id]
                if target_slot >= len(target.raw["inputs"]):
                    raise ComfyUICanvasImportError(f"link {link_id!r} uses an invalid target input slot.")
            target_key = (target_id, target_slot)
            if target_key in scope.target_links:
                raise ComfyUICanvasImportError(
                    f"multiple links target node {target_id!r} input slot {target_slot} in {prefix!r}."
                )
            link = _CanvasLink(link_id, origin_id, origin_slot, target_id, target_slot, link_type)
            scope.links_by_id[link_id] = link
            scope.target_links[target_key] = link
        return scope

    def _node_info(self, instance: _CanvasNodeInstance) -> dict:
        info = self.object_info.get(instance.raw["type"])
        if not isinstance(info, dict):
            raise ComfyUICanvasImportError(
                f"ComfyUI /object_info does not define reachable node type {instance.raw['type']!r}."
            )
        return info

    def _input_specs(self, instance: _CanvasNodeInstance) -> tuple[_InputSpec, ...]:
        cached = self._spec_cache.get(instance.scoped_id)
        if cached is not None:
            return cached
        info = self._node_info(instance)
        sections = info.get("input")
        if not isinstance(sections, dict):
            raise ComfyUICanvasImportError(f"node type {instance.raw['type']!r} has no input schema.")
        ui_inputs = instance.raw["inputs"]
        index_by_name: dict[str, int] = {}
        for index, ui_input in enumerate(ui_inputs):
            if not isinstance(ui_input, dict) or not isinstance(ui_input.get("name"), str):
                raise ComfyUICanvasImportError(f"node {instance.scoped_id!r} has an invalid UI input.")
            name = ui_input["name"]
            if name in index_by_name:
                raise ComfyUICanvasImportError(f"node {instance.scoped_id!r} has duplicate input {name!r}.")
            index_by_name[name] = index
        specs: list[_InputSpec] = []
        known_names: set[str] = set()
        raw_widget_values = instance.raw.get("widgets_values")
        selected_widgets: dict[str, object] = {}
        if isinstance(raw_widget_values, dict):
            selected_widgets = dict(raw_widget_values)
        elif isinstance(raw_widget_values, list):
            # Newer Comfy canvases may omit widget-backed entries from the
            # node ``inputs`` array entirely. Resolve positional values from
            # the authoritative object_info order so a selected dynamic combo
            # (for example SaveAudioAdvanced.format) can expose and consume
            # its branch-specific widgets without shifting later values.
            offset = 0
            for category in ("required", "optional"):
                definitions = sections.get(category) or {}
                if not isinstance(definitions, dict):
                    continue
                for name in _ordered_names(info, category, definitions):
                    try:
                        input_type, config, is_widget = _as_definition(
                            definitions[name],
                            context=f"{instance.raw['type']}.{name}",
                        )
                    except ComfyUICanvasExportError as exc:
                        raise ComfyUICanvasImportError(str(exc)) from exc
                    if config.get("hidden") and name not in index_by_name:
                        continue
                    if not is_widget or offset >= len(raw_widget_values):
                        continue
                    selected_widgets[name] = raw_widget_values[offset]
                    offset += 1
                    if (
                        config.get("control_after_generate")
                        or (
                            input_type == "INT"
                            and name.casefold() in {"seed", "noise_seed"}
                            and offset < len(raw_widget_values)
                            and raw_widget_values[offset] in {
                                "fixed", "increment", "decrement", "randomize",
                            }
                        )
                    ) and offset < len(raw_widget_values):
                        offset += 1
                    selected_value = selected_widgets[name]
                    if input_type in {"COMBO", "COMFY_DYNAMICCOMBO_V3"}:
                        selected_value = _normalized_combo_widget_value(
                            selected_value,
                            config,
                        )
                    def consume_child(child_name, child_definition):
                        nonlocal offset
                        try:
                            child_type, child_config, child_widget = _as_definition(
                                child_definition,
                                context=f"{instance.raw['type']}.{child_name}",
                            )
                        except ComfyUICanvasExportError as exc:
                            raise ComfyUICanvasImportError(str(exc)) from exc
                        if not child_widget or offset >= len(raw_widget_values):
                            return None
                        selected_widgets[child_name] = raw_widget_values[offset]
                        offset += 1
                        if child_config.get("control_after_generate") and offset < len(raw_widget_values):
                            offset += 1
                        child_value = selected_widgets[child_name]
                        return (
                            _normalized_combo_widget_value(child_value, child_config)
                            if child_type in {"COMBO", "COMFY_DYNAMICCOMBO_V3"}
                            else child_value
                        )

                    try:
                        dynamic_widget_definitions(
                            name, input_type, config, selected_value=selected_value,
                            resolve_value=consume_child,
                        )
                    except ValueError as exc:
                        raise ComfyUICanvasImportError(str(exc)) from exc

        def selected_child(child_name, child_definition):
            try:
                child_type, child_config, _widget = _as_definition(
                    child_definition, context=f"{instance.raw['type']}.{child_name}",
                )
            except ComfyUICanvasExportError as exc:
                raise ComfyUICanvasImportError(str(exc)) from exc
            value = selected_widgets.get(child_name)
            return (
                _normalized_combo_widget_value(value, child_config)
                if child_type in {"COMBO", "COMFY_DYNAMICCOMBO_V3"} else value
            )

        for category in ("required", "optional"):
            definitions = sections.get(category) or {}
            if not isinstance(definitions, dict):
                raise ComfyUICanvasImportError(
                    f"node type {instance.raw['type']!r} has an invalid {category} schema."
                )
            for name in _ordered_names(info, category, definitions):
                if name in known_names:
                    raise ComfyUICanvasImportError(f"input {name!r} collides with a dynamic input.")
                try:
                    input_type, config, is_widget = _as_definition(
                        definitions[name], context=f"{instance.raw['type']}.{name}"
                    )
                except ComfyUICanvasExportError as exc:
                    raise ComfyUICanvasImportError(str(exc)) from exc
                if config.get("hidden") and name not in selected_widgets and name not in index_by_name:
                    continue
                if input_type == "COMFY_AUTOGROW_V3":
                    template = config.get("template") if isinstance(config, dict) else None
                    required = template.get("input", {}).get("required", {}) if isinstance(template, dict) else {}
                    allowed_names = autogrow_canvas_names(name, config)
                    if (
                        not isinstance(required, dict)
                        or len(required) != 1
                        or allowed_names is None
                    ):
                        raise ComfyUICanvasImportError(f"autogrow input {name!r} has an unsupported schema.")
                    _, child_definition = next(iter(required.items()))
                    try:
                        child_type, child_config, child_widget = _as_definition(
                            child_definition, context=f"{instance.raw['type']}.{name}"
                        )
                    except ComfyUICanvasExportError as exc:
                        raise ComfyUICanvasImportError(str(exc)) from exc
                    if child_widget:
                        raise ComfyUICanvasImportError(f"autogrow input {name!r} cannot be a widget.")
                    for ui_name, ui_index in index_by_name.items():
                        if ui_name not in allowed_names:
                            continue
                        specs.append(_InputSpec(ui_name, child_type, child_config, False, False, ui_index))
                        known_names.add(ui_name)
                    continue
                base_spec = _InputSpec(
                    name,
                    input_type,
                    config,
                    is_widget,
                    category == "required",
                    index_by_name.get(name),
                )
                specs.append(base_spec)
                known_names.add(name)
                selected_value = selected_widgets.get(name)
                if input_type in {"COMBO", "COMFY_DYNAMICCOMBO_V3"}:
                    selected_value = _normalized_combo_widget_value(
                        selected_value,
                        config,
                    )
                try:
                    children = dynamic_widget_definitions(
                        name, input_type, config, selected_value=selected_value,
                        resolve_value=selected_child,
                    )
                except ValueError as exc:
                    raise ComfyUICanvasImportError(str(exc)) from exc
                for child_name, child_definition, child_required in children:
                    try:
                        child_type, child_config, child_widget = _as_definition(
                            child_definition,
                            context=f"{instance.raw['type']}.{child_name}",
                        )
                    except ComfyUICanvasExportError as exc:
                        raise ComfyUICanvasImportError(str(exc)) from exc
                    if not child_widget:
                        raise ComfyUICanvasImportError(
                            f"dynamic input {child_name!r} must be a bounded widget.",
                        )
                    if child_name in known_names:
                        raise ComfyUICanvasImportError(
                            f"dynamic input {child_name!r} collides with a declared input.",
                        )
                    specs.append(_InputSpec(
                        child_name,
                        child_type,
                        child_config,
                        True,
                        child_required,
                        index_by_name.get(child_name),
                    ))
                    known_names.add(child_name)
        for name, index in index_by_name.items():
            if name in known_names:
                continue
            ui_input = ui_inputs[index]
            if name == "upload" and isinstance(ui_input.get("type"), str) and ui_input["type"].endswith("UPLOAD"):
                continue
            raise ComfyUICanvasImportError(
                f"node {instance.scoped_id!r} contains unknown UI input {name!r}."
            )
        result = tuple(specs)
        self._spec_cache[instance.scoped_id] = result
        return result

    def _widget_values(self, instance: _CanvasNodeInstance) -> dict[str, object]:
        cached = self._widget_cache.get(instance.scoped_id)
        if cached is not None:
            return cached
        raw_values = instance.raw.get("widgets_values")
        if raw_values is None:
            raw_values = []
        specs = self._input_specs(instance)
        if isinstance(raw_values, dict):
            allowed = {spec.name for spec in specs if spec.is_widget}
            ui_only = _KEYED_UI_ONLY_WIDGET_VALUES.get(
                str(instance.raw.get("type") or ""),
                frozenset(),
            )
            unknown = sorted(set(raw_values) - allowed - ui_only)
            if unknown:
                raise ComfyUICanvasImportError(
                    f"node {instance.scoped_id!r} has unknown keyed widget values: {', '.join(unknown)}.",
                )
            values = {name: raw_values[name] for name in allowed if name in raw_values}
            self._widget_cache[instance.scoped_id] = values
            return values
        if not isinstance(raw_values, list):
            raise ComfyUICanvasImportError(
                f"node {instance.scoped_id!r} widgets_values must be a list or keyed object.",
            )
        values: dict[str, object] = {}
        offset = 0
        upload_markers: list[str] = []
        for spec in specs:
            if not spec.is_widget:
                continue
            if offset < len(raw_values):
                values[spec.name] = raw_values[offset]
                offset += 1
            if spec.config.get("control_after_generate") and offset < len(raw_values):
                offset += 1
            elif (
                spec.input_type == "INT"
                and spec.name.casefold() in {"seed", "noise_seed"}
                and offset < len(raw_values)
                and raw_values[offset] in {
                    "fixed", "increment", "decrement", "randomize",
                }
            ):
                offset += 1
            for flag, _, marker in _UPLOAD_WIDGETS:
                if spec.config.get(flag):
                    upload_markers.append(marker)
                    break
        for marker in upload_markers:
            if offset < len(raw_values) and raw_values[offset] == marker:
                offset += 1
        if offset != len(raw_values):
            raise ComfyUICanvasImportError(
                f"node {instance.scoped_id!r} has unmatched positional widget values."
            )
        self._widget_cache[instance.scoped_id] = values
        return values

    def _subgraph_widget_values(self, instance: _CanvasNodeInstance) -> dict[int, object]:
        cache_key = f"{instance.scoped_id}#subgraph"
        cached = self._widget_cache.get(cache_key)
        if cached is not None:
            return cached
        raw_values = instance.raw.get("widgets_values") or []
        if not isinstance(raw_values, list):
            raise ComfyUICanvasImportError(f"subgraph {instance.scoped_id!r} widgets_values must be a list.")
        ui_inputs = instance.raw["inputs"]
        result: dict[int, object] = {}
        offset = 0
        for index, port in enumerate(instance.definition["inputs"]):
            ui_input = ui_inputs[index] if index < len(ui_inputs) else None
            is_widget = isinstance(ui_input, dict) and isinstance(ui_input.get("widget"), dict)
            is_widget = is_widget or port.get("type") in _WIDGET_TYPES
            if is_widget and offset < len(raw_values):
                result[index] = raw_values[offset]
                offset += 1
        if offset != len(raw_values):
            raise ComfyUICanvasImportError(
                f"subgraph {instance.scoped_id!r} has unmatched proxy widget values."
            )
        self._widget_cache[cache_key] = result
        return result

    def _output_type(self, instance: _CanvasNodeInstance, slot: int) -> str:
        if instance.definition is not None:
            outputs = instance.definition["outputs"]
        else:
            outputs = self._node_info(instance).get("output") or []
            outputs = [{"type": item} for item in outputs]
        if slot >= len(outputs):
            raise ComfyUICanvasImportError(
                f"node {instance.scoped_id!r} uses invalid output slot {slot}."
            )
        output_type = outputs[slot].get("type") if isinstance(outputs[slot], dict) else None
        if not isinstance(output_type, str) or not output_type:
            raise ComfyUICanvasImportError(f"node {instance.scoped_id!r} has an invalid output type.")
        if instance.definition is None and output_type == "COMFY_MATCHTYPE_V3":
            info = self._node_info(instance)
            matchtypes = info.get("output_matchtypes")
            template_id = (
                matchtypes[slot]
                if isinstance(matchtypes, list) and slot < len(matchtypes)
                else None
            )
            if not isinstance(template_id, str) or not template_id:
                raise ComfyUICanvasImportError(
                    f"node {instance.scoped_id!r} has an unresolved COMFY_MATCHTYPE_V3 output."
                )
            return self._resolved_matchtype(instance, template_id)
        return output_type

    @staticmethod
    def _matchtype_template(spec: _InputSpec) -> str | None:
        template = spec.config.get("template") if isinstance(spec.config, dict) else None
        template_id = template.get("template_id") if isinstance(template, dict) else None
        return template_id if isinstance(template_id, str) and template_id else None

    def _resolved_matchtype(self, instance: _CanvasNodeInstance, template_id: str) -> str:
        """Resolve one reviewed MATCHTYPE group from its concrete link sources.

        ComfyUI's ``COMFY_MATCHTYPE_V3`` is not a wildcard output.  The output
        type is the common type selected by all inputs carrying the same
        template id (for example the two branches of ``ComfySwitchNode``).
        Persisting the marker itself would allow a later target to be accepted
        without proving that both branches agree.
        """

        key = (instance.scoped_id, template_id)
        cached = self._matchtype_cache.get(key)
        if cached is not None:
            return cached
        if key in self._resolving_matchtypes:
            raise ComfyUICanvasImportError(
                f"node {instance.scoped_id!r} contains a recursive matchtype dependency."
            )
        self._resolving_matchtypes.add(key)
        try:
            candidates: list[set[str]] = []
            matched_specs = [
                spec
                for spec in self._input_specs(instance)
                if spec.input_type == "COMFY_MATCHTYPE_V3"
                and self._matchtype_template(spec) == template_id
            ]
            if not matched_specs:
                raise ComfyUICanvasImportError(
                    f"node {instance.scoped_id!r} matchtype {template_id!r} has no inputs."
                )
            for spec in matched_specs:
                link = (
                    instance.scope.target_links.get((instance.raw["id"], spec.ui_index))
                    if spec.ui_index is not None else None
                )
                if link is None:
                    if spec.required:
                        raise ComfyUICanvasImportError(
                            f"node {instance.scoped_id!r} matchtype input {spec.name!r} is unbound."
                        )
                    continue
                value = self._resolve_source(instance.scope, link.origin_id, link.origin_slot)
                if value is _MISSING:
                    if spec.required:
                        raise ComfyUICanvasImportError(
                            f"node {instance.scoped_id!r} matchtype input {spec.name!r} is missing."
                        )
                    continue
                if not isinstance(value, _ResolvedEndpoint):
                    raise ComfyUICanvasImportError(
                        f"node {instance.scoped_id!r} matchtype input {spec.name!r} is not linked."
                    )
                concrete = {
                    item.strip()
                    for item in value.output_type.split(",")
                    if item.strip() and item.strip() not in {"*", "COMFY_MATCHTYPE_V3"}
                }
                if not concrete:
                    raise ComfyUICanvasImportError(
                        f"node {instance.scoped_id!r} matchtype input {spec.name!r} is unresolved."
                    )
                candidates.append(concrete)
            if not candidates:
                raise ComfyUICanvasImportError(
                    f"node {instance.scoped_id!r} matchtype {template_id!r} has no concrete source."
                )
            common = set.intersection(*candidates)
            if not common:
                raise ComfyUICanvasImportError(
                    f"node {instance.scoped_id!r} matchtype {template_id!r} sources disagree."
                )
            resolved = ",".join(sorted(common))
            self._matchtype_cache[key] = resolved
            return resolved
        finally:
            self._resolving_matchtypes.remove(key)

    @staticmethod
    def _types_match(source_type: str, target_type: str) -> bool:
        if source_type == "*" or target_type == "*":
            return True
        source_types = {item.strip() for item in source_type.split(",") if item.strip()}
        target_types = {item.strip() for item in target_type.split(",") if item.strip()}
        return bool(source_types & target_types)

    def _container_input(self, instance: _CanvasNodeInstance, slot: int):
        if slot >= len(instance.definition["inputs"]):
            raise ComfyUICanvasImportError(f"subgraph {instance.scoped_id!r} uses invalid input slot {slot}.")
        link = instance.scope.target_links.get((instance.raw["id"], slot))
        if link is not None:
            return self._resolve_source(instance.scope, link.origin_id, link.origin_slot)
        return self._subgraph_widget_values(instance).get(slot, _MISSING)

    def _resolve_bypass(self, instance: _CanvasNodeInstance, output_slot: int):
        output_type = self._output_type(instance, output_slot)
        if instance.definition is not None:
            if output_slot != 0 or len(instance.definition["outputs"]) != 1:
                raise ComfyUICanvasImportError(
                    f"mode=4 node {instance.scoped_id!r} cannot be safely bypassed; "
                    "it must have exactly one type-compatible input and output."
                )
            candidates = [
                (index, port.get("type"))
                for index, port in enumerate(instance.definition["inputs"])
                if isinstance(port, dict) and self._types_match(str(port.get("type")), output_type)
            ]
        else:
            info_outputs = self._node_info(instance).get("output") or []
            connection_specs = [spec for spec in self._input_specs(instance) if not spec.is_widget]
            # A muted source-only node (for example a disabled LoadImage)
            # contributes no value. Returning MISSING lets an optional target
            # drop the link while a required target still fails closed.  A
            # source can expose multiple outputs (LoadImage exposes IMAGE and
            # MASK), so this check must happen before the pass-through shape
            # restriction below.
            if not connection_specs:
                self.bypassed.add(instance.scoped_id)
                return _MISSING
            if output_slot != 0 or len(info_outputs) != 1:
                raise ComfyUICanvasImportError(
                    f"mode=4 node {instance.scoped_id!r} cannot be safely bypassed; "
                    "it must have exactly one type-compatible input and output."
                )
            candidates = [
                (spec.ui_index, spec.input_type)
                for spec in connection_specs
                if spec.ui_index is not None
                and self._types_match(spec.input_type, output_type)
            ]
        if len(candidates) != 1 or candidates[0][0] is None:
            raise ComfyUICanvasImportError(
                f"mode=4 node {instance.scoped_id!r} cannot be safely bypassed; "
                "it must have exactly one type-compatible input and output."
            )
        input_slot = candidates[0][0]
        if instance.definition is not None:
            value = self._container_input(instance, input_slot)
        else:
            link = instance.scope.target_links.get((instance.raw["id"], input_slot))
            value = (
                self._resolve_source(instance.scope, link.origin_id, link.origin_slot)
                if link is not None else _MISSING
            )
        if not isinstance(value, _ResolvedEndpoint) or not self._types_match(value.output_type, output_type):
            raise ComfyUICanvasImportError(
                f"mode=4 node {instance.scoped_id!r} cannot be safely bypassed without a linked source."
            )
        self.bypassed.add(instance.scoped_id)
        return value

    def _resolve_source(self, scope: _CanvasScope, origin_id: int | str, origin_slot: int):
        key = (scope.prefix, origin_id, origin_slot)
        if key in self._source_cache:
            return self._source_cache[key]
        if key in self._resolving_sources:
            raise ComfyUICanvasImportError("canvas link/proxy resolution contains a cycle.")
        self._resolving_sources.add(key)
        try:
            if scope.definition is not None and _is_proxy(origin_id, -10):
                value = self._container_input(scope.parent_instance, origin_slot)
            else:
                instance = scope.nodes_by_id[origin_id]
                mode = instance.raw.get("mode", 0)
                if isinstance(mode, bool) or not isinstance(mode, int):
                    raise ComfyUICanvasImportError(f"node {instance.scoped_id!r} has an invalid mode.")
                if mode == 4:
                    value = self._resolve_bypass(instance, origin_slot)
                elif mode != 0:
                    raise ComfyUICanvasImportError(
                        f"reachable node {instance.scoped_id!r} uses unsupported mode {mode}."
                    )
                elif instance.raw["type"] in _UI_ONLY_NODE_TYPES:
                    raise ComfyUICanvasImportError(
                        f"UI-only node {instance.scoped_id!r} cannot be an executable source."
                    )
                elif instance.child_scope is not None:
                    link = instance.child_scope.target_links.get((-20, origin_slot))
                    if link is None:
                        link = instance.child_scope.target_links.get(("-20", origin_slot))
                    value = (
                        self._resolve_source(instance.child_scope, link.origin_id, link.origin_slot)
                        if link is not None else _MISSING
                    )
                else:
                    value = _ResolvedEndpoint(
                        instance.scoped_id,
                        origin_slot,
                        self._output_type(instance, origin_slot),
                    )
            self._source_cache[key] = value
            return value
        finally:
            self._resolving_sources.remove(key)

    def _convert_inputs(self, instance: _CanvasNodeInstance) -> dict[str, object]:
        specs = self._input_specs(instance)
        widgets = self._widget_values(instance)
        result: dict[str, object] = {}
        recognized_slots = {spec.ui_index for spec in specs if spec.ui_index is not None}
        for (target_id, target_slot), _ in instance.scope.target_links.items():
            if target_id == instance.raw["id"] and target_slot not in recognized_slots:
                raise ComfyUICanvasImportError(
                    f"node {instance.scoped_id!r} has a link to an unknown input slot {target_slot}."
                )
        for spec in specs:
            link = (
                instance.scope.target_links.get((instance.raw["id"], spec.ui_index))
                if spec.ui_index is not None else None
            )
            if link is not None:
                value = self._resolve_source(instance.scope, link.origin_id, link.origin_slot)
                if value is _MISSING:
                    if spec.required:
                        raise ComfyUICanvasImportError(
                            f"required proxied input {instance.scoped_id!r}.{spec.name} is unbound."
                        )
                    continue
                if isinstance(value, _ResolvedEndpoint):
                    expected_type = (
                        self._resolved_matchtype(instance, self._matchtype_template(spec))
                        if spec.input_type == "COMFY_MATCHTYPE_V3"
                        and self._matchtype_template(spec) is not None
                        else spec.input_type
                    )
                    if not self._types_match(value.output_type, expected_type):
                        raise ComfyUICanvasImportError(
                            f"input {instance.scoped_id!r}.{spec.name} has incompatible link types."
                        )
                    result[spec.name] = value
                elif spec.is_widget:
                    result[spec.name] = value
                else:
                    raise ComfyUICanvasImportError(
                        f"connection input {instance.scoped_id!r}.{spec.name} resolved to a literal."
                    )
                continue
            if spec.is_widget and spec.name in widgets:
                value = widgets[spec.name]
                if spec.input_type in {"COMBO", "COMFY_DYNAMICCOMBO_V3"}:
                    value = _normalized_combo_widget_value(value, spec.config)
                result[spec.name] = value
            elif spec.required:
                raise ComfyUICanvasImportError(
                    f"required input {instance.scoped_id!r}.{spec.name} is missing."
                )
        return result

    def _select_primary(self) -> list[_CanvasNodeInstance]:
        selected: list[_CanvasNodeInstance] = []
        seen: set[str] = set()
        for requested in self.requested_primary_ids:
            exact = self.top_scope.nodes_by_id.get(requested)
            candidates = [
                instance
                for raw_id, instance in self.top_scope.nodes_by_id.items()
                if str(raw_id) == str(requested)
            ]
            instance = exact if exact is not None else (candidates[0] if len(candidates) == 1 else None)
            if instance is None or instance.scoped_id in seen:
                raise ComfyUICanvasImportError(
                    f"primary output id {requested!r} does not identify one unique top-level node."
                )
            if instance.child_scope is not None or instance.raw["type"] in _UI_ONLY_NODE_TYPES:
                raise ComfyUICanvasImportError(
                    f"primary output {requested!r} must be an executable top-level node, not a UI/subgraph container."
                )
            if instance.raw.get("mode", 0) != 0:
                raise ComfyUICanvasImportError(f"primary output {requested!r} must use normal mode 0.")
            selected.append(instance)
            seen.add(instance.scoped_id)
        return sorted(selected, key=lambda item: item.sort_path)

    def build(self) -> ComfyUICanvasImportResult:
        primary = self._select_primary()
        converted: dict[str, dict[str, object]] = {}
        visiting: set[str] = set()

        def visit(instance: _CanvasNodeInstance) -> None:
            if instance.scoped_id in converted:
                return
            if instance.scoped_id in visiting:
                raise ComfyUICanvasImportError("reachable API graph contains a cycle.")
            if instance.child_scope is not None or instance.raw["type"] in _UI_ONLY_NODE_TYPES:
                raise ComfyUICanvasImportError(
                    f"non-executable node {instance.scoped_id!r} reached API materialization."
                )
            if instance.raw.get("mode", 0) != 0:
                raise ComfyUICanvasImportError(
                    f"reachable node {instance.scoped_id!r} cannot be materialized in mode {instance.raw.get('mode')}."
                )
            visiting.add(instance.scoped_id)
            inputs = self._convert_inputs(instance)
            for value in inputs.values():
                if isinstance(value, _ResolvedEndpoint):
                    visit(self.instances[value.scoped_id])
            visiting.remove(instance.scoped_id)
            converted[instance.scoped_id] = inputs

        for instance in primary:
            visit(instance)

        ordered = sorted(
            (self.instances[scoped_id] for scoped_id in converted),
            key=lambda item: item.sort_path,
        )
        node_id_map = {instance.scoped_id: str(index + 1) for index, instance in enumerate(ordered)}
        api_graph: dict[str, dict] = {}
        for instance in ordered:
            api_inputs: dict[str, object] = {}
            for name, value in converted[instance.scoped_id].items():
                if isinstance(value, _ResolvedEndpoint):
                    api_inputs[name] = [node_id_map[value.scoped_id], value.slot]
                else:
                    api_inputs[name] = value
            api_graph[node_id_map[instance.scoped_id]] = {
                "class_type": instance.raw["type"],
                "inputs": api_inputs,
            }

        dropped: list[dict] = []
        for instance in sorted(self.instances.values(), key=lambda item: item.sort_path):
            if instance.child_scope is not None:
                continue
            if instance.scoped_id in node_id_map:
                continue
            if instance.raw["type"] in _UI_ONLY_NODE_TYPES:
                reason = "ui_only"
            elif instance.scoped_id in self.bypassed:
                reason = "bypassed"
            else:
                reason = "unreachable"
            dropped.append({
                "scoped_id": instance.scoped_id,
                "node_type": instance.raw["type"],
                "reason": reason,
            })

        outgoing = {
            link.origin_id
            for link in self.top_scope.links_by_id.values()
            if link.origin_id in self.top_scope.nodes_by_id
        }
        candidates: list[dict] = []
        for instance in sorted(self.top_scope.nodes_by_id.values(), key=lambda item: item.sort_path):
            if instance.child_scope is not None or instance.raw["type"] in _UI_ONLY_NODE_TYPES:
                continue
            info = self.object_info.get(instance.raw["type"])
            if (isinstance(info, dict) and info.get("output_node") is True) or instance.raw["id"] not in outgoing:
                candidates.append({
                    "canvas_node_id": instance.raw["id"],
                    "scoped_id": instance.scoped_id,
                    "node_type": instance.raw["type"],
                })
        primary_api_ids = tuple(node_id_map[item.scoped_id] for item in primary)
        report = {
            "source_node_count": self.instance_count,
            "api_node_count": len(api_graph),
            "expanded_subgraph_count": len(self.expanded_subgraphs),
            "expanded_subgraphs": sorted(self.expanded_subgraphs),
            "bypassed_nodes": sorted(self.bypassed),
            "dropped_nodes": dropped,
            "primary_canvas_node_ids": [item.raw["id"] for item in primary],
            "primary_api_node_ids": list(primary_api_ids),
        }
        return ComfyUICanvasImportResult(
            api_graph=api_graph,
            node_id_map=node_id_map,
            primary_output_node_ids=primary_api_ids,
            output_candidates=tuple(candidates),
            conversion_report=report,
        )


def ui_workflow_to_api_graph(
    ui_workflow: dict,
    object_info: dict,
    *,
    primary_output_node_ids: list[int | str] | tuple[int | str, ...],
) -> ComfyUICanvasImportResult:
    """Reduce one ComfyUI UI canvas to a deterministic, pruned API graph.

    The caller must explicitly select top-level primary output nodes.  Only
    their backward-reachable graph is materialized.  Subgraph proxy values and
    links are resolved from each outer instance, so stale widgets embedded in a
    definition never override the reviewed outer canvas.
    """

    return _CanvasImportBuilder(
        ui_workflow,
        object_info,
        primary_output_node_ids,
    ).build()


def is_comfyui_ui_workflow(value) -> bool:
    """Perform a strict, side-effect-free sanity check on a canvas export."""

    if not isinstance(value, dict) or value.get("version") != COMFYUI_CANVAS_VERSION:
        return False
    nodes = value.get("nodes")
    links = value.get("links")
    if not isinstance(nodes, list) or not nodes or not isinstance(links, list):
        return False
    node_ids = {
        node.get("id") for node in nodes
        if isinstance(node, dict) and isinstance(node.get("id"), int)
    }
    if len(node_ids) != len(nodes):
        return False
    if not all(
        isinstance(node.get("type"), str)
        and isinstance(node.get("inputs"), list)
        and isinstance(node.get("outputs"), list)
        for node in nodes
    ):
        return False
    return all(
        isinstance(link, list)
        and len(link) == 6
        and isinstance(link[0], int)
        and link[1] in node_ids
        and isinstance(link[2], int)
        and link[3] in node_ids
        and isinstance(link[4], int)
        and isinstance(link[5], str)
        for link in links
    )
