"""Versioned, model-independent bounded graph wiring primitives.

Only reviewed nodes copied from the source graph are repeated. No expressions,
code, paths or model-name dispatch are accepted in the configuration.
"""
from copy import deepcopy
import re

from studio.services.security_boundaries import boundary_int


ENGINE = "generic.references"
COMPILER = ("generic.comfy", 2)
WIRING_CHOICES = (
    {"value": "ordered_ports", "label": "按顺序连接端口（含展开后的动态端口）"},
    {"value": "reduce", "label": "通过节点逐项合并（列表 / 批次）"},
)
GROUP_FIELDS = {
    "key", "source_role", "branch_roles", "output_role", "output_index",
    "target_roles", "input_names", "wiring", "reducer_role", "reducer_inputs",
    "reducer_output_index", "minimum_items", "maximum_items", "conditioning_mode", "model_input",
}


class AssetGroupError(ValueError):
    pass


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def validate_group_config(definition):
    config = definition["projection"]["config"]
    if not isinstance(config, dict) or set(config) != {"groups"}:
        raise AssetGroupError("动态素材配置必须仅包含 groups")
    groups = config["groups"]
    limit = boundary_int("security.comfy.contract.max_autogrow_inputs")
    if not isinstance(groups, list) or not 1 <= len(groups) <= min(limit, 64):
        raise AssetGroupError("动态素材组必须是非空有界列表")
    slots = {row["key"]: row for row in definition["asset_slots"]}
    roles = definition["node_roles"]
    seen = set()
    for row in groups:
        if not isinstance(row, dict) or set(row) != GROUP_FIELDS:
            raise AssetGroupError("动态素材组字段不完整或包含未注册字段")
        if any(not isinstance(row[field], str) for field in ("key", "source_role", "output_role", "wiring", "reducer_role", "conditioning_mode", "model_input")):
            raise AssetGroupError("动态素材组角色、接线与模型来源必须为字符串")
        key = row["key"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key) or key in seen:
            raise AssetGroupError("动态素材组 Key 必须唯一且使用小写字母/数字/下划线")
        seen.add(key)
        slot = slots.get(key)
        if not slot or slot["dynamic"] is not True:
            raise AssetGroupError(f"{key} 必须对应一个动态素材槽")
        for field in ("branch_roles", "target_roles", "input_names", "reducer_inputs"):
            values = row[field]
            if not isinstance(values, list) or len(values) > limit or any(not isinstance(v, str) or not v.strip() for v in values) or len(set(values)) != len(values):
                raise AssetGroupError(f"{key}.{field} 必须是唯一的有界名称列表")
        branch = row["branch_roles"]
        targets = row["target_roles"]
        if not branch or not targets or row["source_role"] not in branch or row["output_role"] not in branch or set(branch) & set(targets):
            raise AssetGroupError(f"{key} 来源/输出必须属于逐项模板，目标不能属于模板")
        unique_roles = branch + ([row["reducer_role"]] if row["reducer_role"] else [])
        if any(name not in roles or roles[name]["cardinality"] != "one" for name in unique_roles) or any(name not in roles for name in targets):
            raise AssetGroupError(f"{key} 模板角色必须唯一；目标可选择一个或多个同类节点")
        if not _integer(row["output_index"]) or not _integer(row["reducer_output_index"]):
            raise AssetGroupError(f"{key} 输出索引无效")
        if row["wiring"] not in {x["value"] for x in WIRING_CHOICES}:
            raise AssetGroupError(f"{key} 接线算法未注册")
        if row["wiring"] == "ordered_ports":
            if not row["input_names"] or row["reducer_role"] or row["reducer_inputs"]:
                raise AssetGroupError(f"{key} 顺序接线必须声明端口且不能附带合并节点")
        elif len(row["input_names"]) != 1 or not row["reducer_role"] or len(row["reducer_inputs"]) != 2 or row["reducer_role"] in branch + targets:
            raise AssetGroupError(f"{key} 合并接线需要独立合并节点、两个输入和一个目标端口")
        if slot["contract_source"] == "workflow":
            if not _integer(row["minimum_items"]) or not _integer(row["maximum_items"], 1) or not row["minimum_items"] <= row["maximum_items"] <= limit or row["conditioning_mode"] or row["model_input"]:
                raise AssetGroupError(f"{key} 工作流数量范围无效或混入模型数量来源")
        elif row["minimum_items"] is not None or row["maximum_items"] is not None or not row["conditioning_mode"] or not row["model_input"]:
            raise AssetGroupError(f"{key} 模型数量来源须选择 mode/input，不能另填数量覆盖模型合同")
    if seen != set(slots):
        raise AssetGroupError("通用动态引擎的素材槽必须与动态组逐项对应")


def _link(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and _integer(value[1])


def build_group_plans(definition, graph, object_info, model_profile):
    """Resolve roles/bounds; freeze only structural instructions, never literals."""
    from studio.services.comfyui_contract import _input_specs
    from studio.services.media_capabilities import resolve_conditioning_contract, conditioning_mode_contract
    from studio.services.model_profile_contracts import verified_model_profile_specification

    validate_group_config(definition)
    roles = {}
    for role, spec in definition["node_roles"].items():
        matches = [key for key, node in graph.items() if node.get("class_type") == spec["node_type"]]
        if matches and (len(matches) == 1 or spec["cardinality"] == "many"):
            roles[role] = matches
    slots = {row["key"]: row for row in definition["asset_slots"]}
    plans, used_nodes, used_targets = [], set(), set()
    for row in definition["projection"]["config"]["groups"]:
        slot = slots[row["key"]]
        try:
            source = roles[row["source_role"]][0]
            branch = [roles[name][0] for name in row["branch_roles"]]
            output = roles[row["output_role"]][0]
            targets = list(dict.fromkeys(node for name in row["target_roles"] for node in roles[name]))
            reducer = roles[row["reducer_role"]][0] if row["reducer_role"] else ""
        except KeyError as exc:
            raise AssetGroupError("动态素材角色无法在来源图中唯一定位") from exc
        minimum, maximum = row["minimum_items"], row["maximum_items"]
        if slot["contract_source"] == "model_profile":
            if model_profile is None:
                raise AssetGroupError("动态素材数量需要选择精确模型版本")
            conditioning = resolve_conditioning_contract(model_specification=verified_model_profile_specification(model_profile), required=True)
            mode = conditioning_mode_contract(conditioning, row["conditioning_mode"])
            bounds = mode.get("inputs", {}).get(row["model_input"])
            if not isinstance(bounds, dict):
                raise AssetGroupError("模型模式未声明所选数量输入")
            minimum, maximum = bounds.get("minimum_items"), bounds.get("maximum_items")
        if not _integer(minimum) or not _integer(maximum, 1) or not minimum <= maximum <= boundary_int("security.comfy.contract.max_autogrow_inputs"):
            raise AssetGroupError("动态素材数量合同超出受控范围")
        if row["wiring"] == "reduce" and maximum < 2:
            raise AssetGroupError("逐项合并的上限至少为 2，单素材请使用顺序端口")
        if row["wiring"] == "ordered_ports" and maximum != len(row["input_names"]):
            raise AssetGroupError("动态素材上限必须等于已声明端口数")
        if any(node.startswith((f"ag_{row['key']}_", f"ag_reduce_{row['key']}_")) for node in graph):
            raise AssetGroupError("来源图占用了动态素材保留节点 ID")
        owned = set(branch) | ({reducer} if reducer else set())
        if len(set(branch)) != len(branch):
            raise AssetGroupError("逐项模板的多个角色不能指向同一节点")
        if owned & set(targets):
            raise AssetGroupError("动态素材模板与目标节点不能重叠")
        if owned & used_nodes or owned & {target for target, _ in used_targets} or set(targets) & used_nodes:
            raise AssetGroupError("动态素材组不能共享逐项模板或合并节点")
        used_nodes |= owned
        seed = [output, row["output_index"]]
        for target in targets:
            ports = row["input_names"][:maximum] if row["wiring"] == "ordered_ports" else row["input_names"]
            trial = deepcopy(graph[target]["inputs"])
            for index, port in enumerate(ports):
                if (target, port) in used_targets:
                    raise AssetGroupError("动态素材组目标端口重复")
                used_targets.add((target, port))
                current = trial.get(port)
                allowed = seed if row["wiring"] == "ordered_ports" else [reducer, row["reducer_output_index"]]
                if current is not None and current != allowed:
                    raise AssetGroupError("目标端口已有其他来源；请先明确逐项模板，不能覆盖其他素材连接")
                trial[port] = allowed
            specs, issues = _input_specs(object_info.get(graph[target]["class_type"], {}), trial, node_label=target)
            if issues:
                raise AssetGroupError("目标动态端口 Schema 不完整: " + "; ".join(issues))
            for index, port in enumerate(ports):
                spec = specs.get(port)
                unused = index >= minimum if row["wiring"] == "ordered_ports" else minimum == 0
                if spec is None or (unused and getattr(spec, "required", False)):
                    raise AssetGroupError("目标缺少端口或未使用端口不是可选输入")
        # Shared model/CLIP/VAE connections remain outside the repeated branch.
        # No external consumer may accidentally depend on a removable template.
        for node_id, node in graph.items():
            if node_id in owned:
                continue
            for port, value in node.get("inputs", {}).items():
                if _link(value) and value[0] in owned and (node_id, port) not in used_targets:
                    raise AssetGroupError("逐项模板存在组外依赖，不能安全重复或删除")
        if reducer:
            for name in row["reducer_inputs"]:
                if graph[reducer]["inputs"].get(name) != seed:
                    raise AssetGroupError("来源合并节点的两个输入须连接同一逐项模板输出；不能丢弃其他分支")
        plans.append({
            "key": row["key"], "media_kind": slot["media_kind"],
            "min_items": minimum, "max_items": maximum,
            "source_node": source, "branch_nodes": branch, "output_node": output,
            "output_index": row["output_index"], "target_nodes": targets,
            "input_names": row["input_names"], "wiring": row["wiring"],
            "reducer_node": reducer, "reducer_inputs": row["reducer_inputs"],
            "reducer_output_index": row["reducer_output_index"],
        })
    projected_count = len(graph) + sum((p["max_items"] - 1) * len(p["branch_nodes"]) + (p["max_items"] - 2 if p["wiring"] == "reduce" else 0) for p in plans)
    if projected_count > boundary_int("security.comfy.canvas.max_nodes"):
        raise AssetGroupError("动态素材展开后的节点数超过受控画布预算")
    return plans


def expand_group_graph(graph, plans):
    """Deterministically build the bounded reviewed graph; source stays unchanged."""
    expanded = deepcopy(graph)
    for plan in plans:
        branches = []
        for index in range(plan["max_items"]):
            mapping = {node: node if index == 0 else f"ag_{plan['key']}_{index}_{node}" for node in plan["branch_nodes"]}
            for old, new in mapping.items():
                if index and new in expanded:
                    raise AssetGroupError("动态素材节点 ID 与来源图冲突")
                node = deepcopy(graph[old])
                for port, value in node["inputs"].items():
                    if _link(value) and value[0] in mapping:
                        node["inputs"][port] = [mapping[value[0]], value[1]]
                expanded[new] = node
            branches.append(mapping)
        outputs = [[mapping[plan["output_node"]], plan["output_index"]] for mapping in branches]
        _wire(expanded, graph, plan, outputs)
    return expanded


def _wire(graph, templates, plan, outputs):
    if plan["wiring"] == "ordered_ports":
        for target in plan["target_nodes"]:
            for port in plan["input_names"]:
                graph[target]["inputs"].pop(port, None)
            for port, output in zip(plan["input_names"], outputs):
                graph[target]["inputs"][port] = output
    else:
        reducer = plan["reducer_node"]
        template = deepcopy(templates[reducer])
        for node in [reducer, *(f"ag_reduce_{plan['key']}_{i}" for i in range(1, plan["max_items"]))]:
            graph.pop(node, None)
        result = outputs[0] if outputs else None
        for index, output in enumerate(outputs[1:], 1):
            node_id = f"ag_reduce_{plan['key']}_{index}"
            if node_id in graph:
                raise AssetGroupError("合并节点 ID 冲突")
            node = deepcopy(template)
            left, right = plan["reducer_inputs"]
            node["inputs"][left], node["inputs"][right] = result, output
            graph[node_id] = node
            result = [node_id, plan["reducer_output_index"]]
        for target in plan["target_nodes"]:
            port = plan["input_names"][0]
            if result is None:
                graph[target]["inputs"].pop(port, None)
            else:
                graph[target]["inputs"][port] = result


def attach_group_bindings(contract, bindings, plans):
    for plan in plans:
        keys, branches = [], []
        for index in range(plan["max_items"]):
            nodes = [node if index == 0 else f"ag_{plan['key']}_{index}_{node}" for node in plan["branch_nodes"]]
            source = plan["source_node"] if index == 0 else f"ag_{plan['key']}_{index}_{plan['source_node']}"
            rows = [row for row in bindings if row["node_id"] in nodes and row["value_type"].startswith("asset_")]
            if len(rows) != 1 or rows[0]["node_id"] != source or rows[0]["value_type"] != f"asset_{plan['media_kind']}":
                raise AssetGroupError("逐项模板必须包含一个且仅一个匹配媒体类型的受管素材输入")
            binding = rows[0]
            required = index < plan["min_items"]
            binding["is_required"] = required
            definition = contract["parameters"][binding["semantic_key"]]
            definition.update(required=required, min_items=1 if required else 0, asset_group=plan["key"])
            keys.append(binding["semantic_key"])
            branches.append(nodes)
        plan["semantic_keys"], plan["expanded_branches"] = keys, branches
        owned = {node for nodes in branches for node in nodes}
        if plan["wiring"] == "reduce":
            owned.update(f"ag_reduce_{plan['key']}_{i}" for i in range(1, plan["max_items"]))
        for binding in bindings:
            if binding["node_id"] in owned and not binding["value_type"].startswith("asset_"):
                if binding["value_type"] == "model_artifact":
                    raise AssetGroupError("逐项模板不能重复模型加载节点，请共享模型节点")
                value = contract["parameters"][binding["semantic_key"]]
                if "default" not in value:
                    raise AssetGroupError("逐项预处理参数必须具有冻结来源默认值")
                value["asset_group_constant"] = True
                value["use_source_default"] = True
    contract["asset_groups"] = plans
    contract["compiler"] = {"id": COMPILER[0], "version": COMPILER[1]}


def validate_frozen_groups(contract, definition=None):
    plans = contract.get("asset_groups")
    if not isinstance(plans, list) or not 1 <= len(plans) <= 64:
        raise AssetGroupError("冻结动态素材组必须是非空有界列表")
    parameters = contract.get("parameters", {})
    if not isinstance(parameters, dict):
        raise AssetGroupError("冻结动态素材参数必须是对象")
    owned, semantic_keys, seen = set(), set(), set()
    expanded_count = 0
    for plan in plans:
        fields = {"key", "media_kind", "min_items", "max_items", "source_node", "branch_nodes", "output_node", "output_index", "target_nodes", "input_names", "wiring", "reducer_node", "reducer_inputs", "reducer_output_index", "semantic_keys", "expanded_branches"}
        if not isinstance(plan, dict) or set(plan) != fields:
            raise AssetGroupError("冻结动态素材组结构不完整")
        if any(not isinstance(plan[field], str) for field in ("key", "media_kind", "source_node", "output_node", "wiring", "reducer_node")):
            raise AssetGroupError("冻结动态素材组名称无效")
        key = plan["key"]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key) or key in seen or plan["media_kind"] not in {"image", "video", "audio"}:
            raise AssetGroupError("冻结动态素材组 Key/媒体类型无效")
        seen.add(key)
        if not _integer(plan["min_items"]) or not _integer(plan["max_items"], 1) or not plan["min_items"] <= plan["max_items"] <= boundary_int("security.comfy.contract.max_autogrow_inputs"):
            raise AssetGroupError("冻结素材数量范围无效")
        for field in ("branch_nodes", "target_nodes", "input_names", "semantic_keys", "reducer_inputs"):
            values = plan[field]
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
                raise AssetGroupError("冻结动态素材组名称列表无效")
        if not plan["branch_nodes"] or not plan["target_nodes"] or plan["source_node"] not in plan["branch_nodes"] or plan["output_node"] not in plan["branch_nodes"] or not _integer(plan["output_index"]) or not _integer(plan["reducer_output_index"]):
            raise AssetGroupError("冻结动态素材模板与输出无效")
        expanded_count += len(plan["branch_nodes"]) * plan["max_items"] + (plan["max_items"] - 1 if plan["wiring"] == "reduce" else 0)
        if expanded_count > boundary_int("security.comfy.canvas.max_nodes"):
            raise AssetGroupError("冻结动态素材展开超过节点预算")
        if plan["wiring"] == "ordered_ports":
            if len(plan["input_names"]) != plan["max_items"] or plan["reducer_node"] or plan["reducer_inputs"]:
                raise AssetGroupError("冻结顺序端口与数量不一致")
        elif plan["wiring"] == "reduce":
            if len(plan["input_names"]) != 1 or len(plan["reducer_inputs"]) != 2 or not plan["reducer_node"] or plan["max_items"] < 2:
                raise AssetGroupError("冻结合并规则无效")
        else:
            raise AssetGroupError("冻结接线能力未注册")
        expected = [[node if index == 0 else f"ag_{key}_{index}_{node}" for node in plan["branch_nodes"]] for index in range(plan["max_items"])]
        if plan["expanded_branches"] != expected or len(plan["semantic_keys"]) != plan["max_items"]:
            raise AssetGroupError("冻结动态素材分支与数量不一致")
        nodes = {node for branch in expected for node in branch}
        if len(nodes) != sum(len(branch) for branch in expected) or nodes & owned or nodes & set(plan["target_nodes"]):
            raise AssetGroupError("冻结动态素材分支重叠")
        owned |= nodes
        for index, semantic in enumerate(plan["semantic_keys"]):
            param = parameters.get(semantic)
            if semantic in semantic_keys or not isinstance(param, dict) or param.get("asset_group") != key or param.get("type") != f"asset_{plan['media_kind']}" or param.get("required") is not (index < plan["min_items"]):
                raise AssetGroupError("冻结素材语义参数与分组不一致")
            semantic_keys.add(semantic)
        if definition is not None:
            configs = [row for row in definition["projection"]["config"]["groups"] if row["key"] == key]
            slots = [row for row in definition["asset_slots"] if row["key"] == key]
            if len(configs) != 1 or len(slots) != 1:
                raise AssetGroupError("冻结动态组未在 Projector 声明")
            config, slot = configs[0], slots[0]
            if slot["media_kind"] != plan["media_kind"] or any(config[field] != plan[field] for field in ("wiring", "input_names", "output_index", "reducer_inputs", "reducer_output_index")):
                raise AssetGroupError("冻结动态组与 Projector 接线不一致")
            if slot["contract_source"] == "workflow" and (plan["min_items"], plan["max_items"]) != (config["minimum_items"], config["maximum_items"]):
                raise AssetGroupError("冻结动态组与 Projector 数量不一致")
    if definition is not None and seen != {row["key"] for row in definition["projection"]["config"]["groups"]}:
        raise AssetGroupError("冻结动态组未完整覆盖 Projector")


def validate_group_selection(contract, selected_keys):
    if not contract.get("asset_groups"):
        if contract.get("compiler") == {"id": COMPILER[0], "version": COMPILER[1]}:
            raise AssetGroupError("动态编译器缺少冻结素材组")
        return
    validate_frozen_groups(contract)
    for plan in contract.get("asset_groups", []):
        keys = plan["semantic_keys"]
        selected = [key for key in keys if key in selected_keys]
        if not plan["min_items"] <= len(selected) <= plan["max_items"] or selected != keys[:len(selected)]:
            raise AssetGroupError(f"{plan['key']} 必须按顺序选择 {plan['min_items']}–{plan['max_items']} 个素材，不能跳过中间项")


def materialize_group_graph(graph, contract, selected_keys):
    """Only selected branches reach the executor; empty placeholders never execute."""
    validate_group_selection(contract, selected_keys)
    result = deepcopy(graph)
    for plan in contract.get("asset_groups", []):
        required_nodes = set(plan["target_nodes"]) | {node for branch in plan["expanded_branches"] for node in branch}
        if any(not isinstance(graph.get(node), dict) or not isinstance(graph[node].get("inputs"), dict) for node in required_nodes):
            raise AssetGroupError("冻结动态素材图缺少声明的模板或目标节点")
        outputs = []
        for index, key in enumerate(plan["semantic_keys"]):
            nodes = plan["expanded_branches"][index]
            if key not in selected_keys:
                for node in nodes:
                    result.pop(node, None)
            else:
                output = plan["output_node"] if index == 0 else f"ag_{plan['key']}_{index}_{plan['output_node']}"
                outputs.append([output, plan["output_index"]])
        if plan["wiring"] == "reduce":
            # The expanded graph retains the first actual reducer as its template.
            template_id = f"ag_reduce_{plan['key']}_1"
            template = graph.get(template_id)
            if template is None:
                raise AssetGroupError("冻结合并节点模板缺失")
            templates = {plan["reducer_node"]: template}
        else:
            templates = graph
        _wire(result, templates, plan, outputs)
    return result


def group_form_schema(contract):
    if contract.get("asset_groups") or contract.get("compiler") == {"id": COMPILER[0], "version": COMPILER[1]}:
        try:
            validate_frozen_groups(contract)
        except AssetGroupError as exc:
            from django.core.exceptions import ValidationError
            raise ValidationError({"workflow_inputs": str(exc)}) from exc
    return [{key: plan[key] for key in ("key", "media_kind", "min_items", "max_items", "semantic_keys", "wiring")} for plan in contract.get("asset_groups", [])]


def recommend_group_definition(definition, graph, object_info, asset_dependencies):
    """Conservative single-template recommendation from edges and typed ports.

    This is an unsaved proposal, not an assertion that similarly typed ports have
    interchangeable business meaning. Ambiguous branches remain manual review.
    """
    from studio.services.comfyui_contract import _input_specs
    from studio.services.workflow_projectors import WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS

    if len(asset_dependencies) != 1:
        return None
    candidate = deepcopy(definition)
    source = asset_dependencies[0]["node_id"]
    node_roles = candidate["node_roles"]
    role_by_type = {row["node_type"]: key for key, row in node_roles.items() if row["cardinality"] == "one"}
    branch, current = [], source
    while current not in branch and len(branch) < len(graph):
        current_role = role_by_type.get(graph[current]["class_type"])
        if not current_role:
            return None
        branch.append(current)
        consumers = [(node_id, port, value[1]) for node_id, node in graph.items() for port, value in node.get("inputs", {}).items() if _link(value) and value[0] == current]
        if not consumers or len({(graph[node]["class_type"], port, output) for node, port, output in consumers}) != 1:
            return None
        target, first_port, output_index = consumers[0]
        specs, issues = _input_specs(object_info.get(graph[target]["class_type"], {}), graph[target]["inputs"], node_label=target)
        if issues or first_port not in specs:
            return None
        first = specs[first_port]
        ports = [first_port, *(name for name, spec in specs.items() if name != first_port and spec.type_name == first.type_name and not spec.is_widget and not spec.required)]
        if len(ports) > 1:
            target_type = graph[target]["class_type"]
            target_role = role_by_type.get(target_type) or "dynamic_target"
            if target_role in node_roles and node_roles[target_role]["node_type"] != target_type:
                return None
            node_roles[target_role] = {"node_type": target_type, "cardinality": "one" if sum(node["class_type"] == target_type for node in graph.values()) == 1 else "many"}
            key = "references"
            candidate["asset_slots"] = [{"key": key, "media_kind": asset_dependencies[0]["media_kind"], "dynamic": True, "contract_source": "workflow"}]
            candidate["adapters"] = deepcopy(WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS[ENGINE])
            candidate["projection"] = {"kind": "generic_references", "config": {"groups": [{
                "key": key, "source_role": role_by_type[graph[source]["class_type"]],
                "branch_roles": [role_by_type[graph[node]["class_type"]] for node in branch],
                "output_role": current_role, "output_index": output_index,
                "target_roles": [target_role], "input_names": ports, "wiring": "ordered_ports",
                "reducer_role": "", "reducer_inputs": [], "reducer_output_index": 0,
                "minimum_items": 1, "maximum_items": len(ports),
                "conditioning_mode": "", "model_input": "",
            }]}}
            try:
                build_group_plans(candidate, graph, object_info, None)
            except AssetGroupError:
                return None
            return candidate
        if len(consumers) != 1:
            return None
        current = target
    return None
