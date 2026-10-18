"""Read-only scalar port evidence from the exact, hash-pinned workflow version."""
from math import isfinite
import json

from django.core.exceptions import ValidationError
from django.db.models import Prefetch

from studio.models import WorkflowModelRequirement, WorkflowSemanticBinding, WorkflowTemplateVersion
from studio.services.workflow_compiler import WorkflowCompilerError, validate_frozen_workflow_snapshot
from studio.services.workflow_versions import workflow_version_execution_snapshot
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_media_tokens import is_workflow_media_use_token
from studio.task_center_public import public_value


def _same_json(left, right):
    # Python equality would mistake False/0 and True/1 for the same input.
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def _contains_media_token(value):
    if isinstance(value, dict):
        return any(_contains_media_token(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_media_token(child) for child in value)
    return isinstance(value, str) and is_workflow_media_use_token(value)


def graph_node_rows(graph, snapshot, node_ids):
    """Compare only exact node IDs in two verified original contracts.

    No algorithm inference, recompilation, live node catalog or execution claim.
    Sensitive inputs and media tokens stay hidden even in the raw API response.
    Missing registry evidence cannot become an invented unchanged baseline.
    """
    rows = []
    selected = [(node_id, node) for node_id, node in graph.items() if node_id in node_ids]
    # One shared redaction pass: resolving safety limits must not grow with ports.
    safe_inputs = public_value([node["inputs"] for _, node in selected]) if selected else []
    for (node_id, node), safe in zip(selected, safe_inputs):
        original = snapshot["graph"].get(node_id) if snapshot is not None else None
        origin = ("unavailable" if snapshot is None else "added" if original is None else
                  "replaced" if original["class_type"] != node["class_type"] else "original")
        baseline = original["inputs"] if origin == "original" else {}
        inputs = []
        for name, value in node["inputs"].items():
            change = ("unavailable" if origin in {"unavailable", "replaced"} else "added" if name not in baseline else
                      "unchanged" if _same_json(value, baseline[name]) else "changed")
            kind, shown = "literal", value
            if name not in safe or not _same_json(safe[name], value):
                kind, shown = "redacted", None
            elif _contains_media_token(value):
                kind, shown = "media", None
            elif (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
                    and value[0] in graph and type(value[1]) is int and value[1] >= 0):
                kind = "connection"
            inputs.append({"input_name": name, "change": change, "kind": kind, "value": shown})
        rows.append({"node_id": node_id, "class_type": node["class_type"], "origin": origin,
            "inputs": inputs, "removed_inputs": [name for name in baseline if name not in node["inputs"]]})
    return rows


def _scalar(value, kind):
    if kind in {"string", "enum"}:
        return isinstance(value, str)
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return type(value) is int or (type(value) is float and isfinite(value))
    return kind == "boolean" and type(value) is bool


def scalar_port_rows(snapshot, compiled):
    """Pure comparison, called only after both original contracts are verified.

    Never search for equal text or infer cloned/replaced dynamic node identities.
    Values remain in the original parameter/prompt view, not duplicated here.
    """
    rows = []
    graph, values = compiled["graph"], compiled["resolved_semantics"]
    for binding in snapshot["semantic_bindings"]:
        key, node_id, name = (binding[k] for k in ("semantic_key", "node_id", "input_name"))
        kind = binding["value_type"]
        if kind not in {"string", "enum", "integer", "number", "boolean"} or key == "output.prefix":
            continue  # Media/model files and internal storage identity have separate evidence.
        original = snapshot["graph"].get(node_id)
        node = graph.get(node_id)
        state, message = "unavailable", "原声明节点不存在或已被动态改写；不推断新节点映射。"
        if isinstance(original, dict) and isinstance(node, dict) and original.get("class_type") == node.get("class_type"):
            inputs = node.get("inputs", {})
            if key not in values and not binding["is_required"] and name not in inputs:
                state, message = "omitted", "可选语义未提供，原请求也未写入此端口。"
            elif key in values and name in inputs and _scalar(values[key], kind) and _scalar(inputs[name], kind) and values[key] == inputs[name]:
                state, message = "matched", "原请求声明端口的类型和值与冻结语义一致。"
            else:
                state, message = "mismatch", "原请求声明端口与冻结语义不一致或必需值缺失；不回填默认值。"
        rows.append({"semantic_key": key, "value_type": kind, "node_id": node_id, "input_name": name,
            "class_type": original.get("class_type", "") if isinstance(original, dict) else "",
            "state": state, "message": message})
    return rows


@security_boundary_resolution_cache()
def read_pinned_workflow(definition, transport):
    """The authorized task reader has already verified the frozen transport.

    Only the pinned version is read, even if retired. Missing/corrupt registry
    evidence cannot erase the valid frozen task/media facts or invoke a compiler.
    """
    target = definition.value["target"]["workflow"]
    compiled = transport.value["payload"]["compiled_workflow"]
    try:
        version = WorkflowTemplateVersion.objects.select_related("template", "model_profile_version").prefetch_related(
            Prefetch("semantic_bindings", queryset=WorkflowSemanticBinding.objects.only(
                "workflow_version_id", "semantic_key", "node_id", "input_name", "value_type", "is_required").order_by()),
            Prefetch("model_requirements", queryset=WorkflowModelRequirement.objects.only(
                "workflow_version_id", "role", "alias", "artifact_id", "expected_filename", "expected_sha256", "is_required").order_by())).only(
            "id", "version", "template_id", "template__slug", "template__name", "template__output_kind",
            "model_profile_version_id", "model_profile_version__model_identifier",
            "graph", "graph_schema_version", "graph_size_bytes", "graph_sha256",
            "input_contract", "input_contract_schema_version", "input_contract_size_bytes", "input_contract_sha256",
            "compiler_id", "compiler_version", "semantic_bindings_sha256", "model_requirements_sha256",
            "execution_contract_sha256").get(pk=target["id"])
        snapshot = workflow_version_execution_snapshot(version)
        if (snapshot["execution_contract_sha256"] != target["execution_contract_sha256"]
                or compiled["workflow_contract_sha256"] != target["execution_contract_sha256"]
                or snapshot["compiler_id"] != compiled["compiler_id"]
                or snapshot["compiler_version"] != compiled["compiler_version"]):
            return None
        validate_frozen_workflow_snapshot(snapshot)
    except (WorkflowTemplateVersion.DoesNotExist, ValidationError, WorkflowCompilerError):
        return None
    return snapshot


_UNREAD = object()


def scalar_wiring(definition, transport, *, snapshot=_UNREAD):
    if snapshot is _UNREAD:
        snapshot = read_pinned_workflow(definition, transport)
    result = {"state": "unavailable", "contract_sha256": definition.value["target"]["workflow"]["execution_contract_sha256"], "rows": [],
        "message": "精确工作流版本缺失或合同摘要不可核对；不读取最新版本、不推断端口。"}
    if snapshot is None:
        return result
    compiled = transport.value["payload"]["compiled_workflow"]
    rows = scalar_port_rows(snapshot, compiled)
    states = {row["state"] for row in rows}
    result.update(rows=rows, state="mismatch" if "mismatch" in states else "partial" if "unavailable" in states else "matched",
        message="已核对精确工作流合同；仅比较声明的标量端口，不包含媒体／模型文件及内部输出路径。不证明节点运行或模型收到；动态新增或替换节点不推断。接线来源：模型关联的精确工作流版本 → 随导入冻结的语义绑定；不是任务页可编辑配置，结构变更须重新导入新版本。值见核心参数和实际提示词。")
    return result
