from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
import uuid

from studio.services.workflow import WorkflowBuildError, validate_comfy_graph


COMFYUI_CANVAS_VERSION = 0.4
MAX_CANVAS_NODES = 500
MAX_INPUTS_PER_NODE = 128
MAX_OUTPUTS_PER_NODE = 64

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
        or maximum > MAX_INPUTS_PER_NODE
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
    # ComfyUI's official H3 workflow persists one empty trailing socket for
    # each expandable group.  Keeping it makes the exported canvas editable.
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
    if class_type == "MiniMaxH3ReferenceToVideo":
        height = max(height, 420)
    width = 430 if class_type.startswith("MiniMaxH3") else 310
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
    if len(api_graph) > MAX_CANVAS_NODES:
        raise ComfyUICanvasExportError(
            f"ComfyUI workflow exceeds the canvas export limit ({MAX_CANVAS_NODES} nodes)."
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
        unknown = sorted(set(api_inputs) - consumed)
        if unknown:
            raise ComfyUICanvasExportError(
                f"node {node_id!r} ({class_type}) contains unknown inputs: {', '.join(unknown)}."
            )
        if len(input_records) > MAX_INPUTS_PER_NODE:
            raise ComfyUICanvasExportError(
                f"node {node_id!r} exceeds the canvas input limit ({MAX_INPUTS_PER_NODE})."
            )

        outputs_raw = info.get("output") or []
        output_names_raw = info.get("output_name") or []
        if not isinstance(outputs_raw, list) or len(outputs_raw) > MAX_OUTPUTS_PER_NODE:
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
