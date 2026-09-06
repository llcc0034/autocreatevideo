"""Model-independent, versioned classified media wiring for ComfyUI.

Model nodes, ports, bounds and prompt keys are configuration. The executor
only owns reviewed media loading primitives and indexed/pair-indexed wiring.
"""
from copy import deepcopy
import re

from studio.services.media_capabilities import conditioning_mode_contract, resolve_conditioning_contract, normalize_conditioning_contract
from studio.services.security_boundaries import boundary_int

ENGINE = "generic.classified"
COMPILER = ("generic.comfy", 3)
MEDIA_GROUPS = {"images": "image", "videos": "video", "video_audios": "audio", "audios": "audio"}
MODEL_INPUTS = {"images": "entity_images", "videos": "reference_videos", "video_audios": "paired_video_audios", "audios": "reference_audios"}
CONFIG_FIELDS = {"capability", "conditioning_mode", "target_role", "prompt_template_keys", "static_semantics", "dynamic_groups"}
GROUP_FIELDS = {"input_names", "prefix", "mode_input", "minimum_items", "maximum_items"}


class ClassifiedReferenceError(ValueError):
    pass


def _int(value, minimum=0):
    return type(value) is int and value >= minimum


def validate_config(definition):
    config = definition["projection"]["config"]
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise ClassifiedReferenceError("分类素材接线配置字段不完整或包含未知字段")
    for key in ("capability", "conditioning_mode", "target_role"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ClassifiedReferenceError(f"分类素材配置 {key} 必须明确填写")
    role = definition["node_roles"].get(config["target_role"])
    if not role or role["cardinality"] != "one":
        raise ClassifiedReferenceError("分类素材目标角色必须在图中唯一")
    keys = config["prompt_template_keys"]
    if not isinstance(keys, list) or len(keys) > 64 or any(not isinstance(k, str) or not k.strip() for k in keys) or len(keys) != len(set(keys)):
        raise ClassifiedReferenceError("提示词模板键必须为唯一的有界字符串列表；空列表表示不限制模板键")
    if not isinstance(config["static_semantics"], dict):
        raise ClassifiedReferenceError("静态语义必须为对象")
    from studio.services.workflow_compiler import _validated_static_semantics, WorkflowCompilerError
    try:
        _validated_static_semantics(config)
    except WorkflowCompilerError as exc:
        raise ClassifiedReferenceError(str(exc)) from exc
    groups = config["dynamic_groups"]
    slots = {row["key"]: row for row in definition["asset_slots"]}
    if not isinstance(groups, dict) or set(groups) != set(MEDIA_GROUPS) or set(slots) != set(groups):
        raise ClassifiedReferenceError("分类素材组与素材槽必须包含 images/videos/video_audios/audios")
    limit = boundary_int("security.comfy.contract.max_autogrow_inputs")
    for key, row in groups.items():
        if not isinstance(row, dict) or set(row) != GROUP_FIELDS:
            raise ClassifiedReferenceError(f"素材组 {key} 配置字段无效")
        slot = slots[key]
        if slot["media_kind"] != MEDIA_GROUPS[key] or slot["dynamic"] is not True:
            raise ClassifiedReferenceError(f"素材组 {key} 媒体类型与动态声明不一致")
        ports = row["input_names"]
        if not isinstance(ports, list) or len(ports) > limit or any(not isinstance(p, str) or not p.strip() for p in ports) or len(set(ports)) != len(ports):
            raise ClassifiedReferenceError(f"素材组 {key} 输入端口必须为有界的唯一名称列表")
        prefix = row["prefix"]
        if not isinstance(prefix, str) or (prefix and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", prefix)) or bool(prefix) == bool(ports):
            raise ClassifiedReferenceError(f"素材组 {key} 必须选择端口列表或从 0 编号的动态前缀，不能同时填写")
        if row["mode_input"] != MODEL_INPUTS[key]:
            raise ClassifiedReferenceError(f"素材组 {key} 必须对应模型的 {MODEL_INPUTS[key]} 数量合同")
        if slot["contract_source"] == "model_profile":
            if row["minimum_items"] is not None or row["maximum_items"] is not None:
                raise ClassifiedReferenceError(f"素材组 {key} 的模型数量来源必须选择模型输入且不能另设数量")
        elif not _int(row["minimum_items"]) or not _int(row["maximum_items"]) or not row["minimum_items"] <= row["maximum_items"] <= limit:
            raise ClassifiedReferenceError(f"素材组 {key} 的 Projector 数量范围无效")


def project_plan(definition, graph, model_specification):
    validate_config(definition)
    config = definition["projection"]["config"]
    conditioning = resolve_conditioning_contract(model_specification=model_specification, required=True)
    mode = conditioning_mode_contract(conditioning, config["conditioning_mode"])
    if mode["input_kind"] != "classified_references":
        raise ClassifiedReferenceError("所选模型模式必须声明分类参考素材能力")
    node_type = definition["node_roles"][config["target_role"]]["node_type"]
    targets = [key for key, node in graph.items() if node.get("class_type") == node_type]
    if len(targets) != 1:
        raise ClassifiedReferenceError("分类素材目标节点无法唯一定位")
    slots = {row["key"]: row for row in definition["asset_slots"]}
    groups = {}
    occupied = set()
    for key, row in config["dynamic_groups"].items():
        bounds = mode["inputs"].get(row["mode_input"]) if slots[key]["contract_source"] == "model_profile" else row
        if not isinstance(bounds, dict):
            raise ClassifiedReferenceError(f"模型未声明 {key} 对应的数量合同")
        minimum, maximum = bounds.get("minimum_items"), bounds.get("maximum_items")
        if not _int(minimum) or not _int(maximum) or not minimum <= maximum <= boundary_int("security.comfy.contract.max_autogrow_inputs"):
            raise ClassifiedReferenceError(f"素材组 {key} 数量超出安全边界")
        model_bounds = mode["inputs"][row["mode_input"]]
        if minimum < model_bounds["minimum_items"] or maximum > model_bounds["maximum_items"]:
            raise ClassifiedReferenceError(f"素材组 {key} 的 Projector 范围不能超出模型版本能力")
        # Paired audio is indexed by VIDEO position, not by audio count. It may
        # use the last video port even when only one of many videos has audio.
        video_config = config["dynamic_groups"]["videos"]
        video_maximum = mode["inputs"]["reference_videos"]["maximum_items"] if slots["videos"]["contract_source"] == "model_profile" else video_config["maximum_items"]
        capacity = video_maximum if key == "video_audios" and maximum else maximum
        ports = row["input_names"] or [f"{row['prefix']}{index}" for index in range(capacity)]
        if len(ports) != capacity or occupied.intersection(ports):
            raise ClassifiedReferenceError("素材组端口数量与上限不一致或存在重叠")
        occupied.update(ports)
        if any(port in graph[targets[0]]["inputs"] for port in ports):
            raise ClassifiedReferenceError("动态目标端口已有来源；不能静默替换工作流中的素材连接")
        groups[key] = {"input_names": ports, "min_items": minimum, "max_items": maximum, "media_kind": MEDIA_GROUPS[key]}
    if groups["video_audios"]["max_items"] > groups["videos"]["max_items"]:
        raise ClassifiedReferenceError("配套音频数量不能超过视频数量")
    if groups["images"]["max_items"] > mode["inputs"]["total_images"]["maximum_items"]:
        raise ClassifiedReferenceError("图片数量不能超过模型总图片上限")
    selected = deepcopy(mode)
    for key, group in groups.items():
        selected["inputs"][MODEL_INPUTS[key]] = {"minimum_items": group["min_items"], "maximum_items": group["max_items"]}
    selected["inputs"]["total_images"] = deepcopy(selected["inputs"]["entity_images"])
    topology = selected.get("dynamic_reference_topology")
    if topology:
        for key in ("videos", "audios"):
            topology[key].update(minimum_items=groups[key]["min_items"], maximum_items=groups[key]["max_items"])
            purpose_maximum = max(groups["audios"]["max_items"], groups["video_audios"]["max_items"]) if key == "audios" else groups[key]["max_items"]
            for purpose in topology[key]["purposes"]:
                if purpose["minimum_items"] > purpose_maximum:
                    raise ClassifiedReferenceError("Projector 数量范围与模型素材用途的最少数量冲突")
                purpose["maximum_items"] = min(purpose["maximum_items"], purpose_maximum)
    frozen_conditioning = normalize_conditioning_contract({**conditioning, "modes": [selected]})
    return {
        "schema_version": 1, "target_node_id": targets[0], "groups": groups,
        "conditioning_mode": config["conditioning_mode"],
    }, frozen_conditioning


def attach_plan(contract, definition, graph, model_specification):
    plan, conditioning = project_plan(definition, graph, model_specification)
    contract.update({
        "compiler": {"id": COMPILER[0], "version": COMPILER[1]},
        "capability": definition["projection"]["config"]["capability"],
        "classified_references": plan, "conditioning_contract": conditioning,
        "prompt_template_keys": deepcopy(definition["projection"]["config"]["prompt_template_keys"]),
        "static_semantics": deepcopy(definition["projection"]["config"]["static_semantics"]),
    })
    parameters = contract["parameters"]
    if "video.frames" in parameters or "video.fps" in parameters:
        if not {"video.frames", "video.fps"} <= parameters.keys():
            raise ClassifiedReferenceError("视频时钟必须同时声明帧数与 FPS 参数")
        frames, fps = parameters["video.frames"], parameters["video.fps"]
        if frames.get("type") != "integer" or fps.get("type") not in {"integer", "number"}:
            raise ClassifiedReferenceError("视频帧数必须为整数参数，FPS 必须为数值参数")
        contract["frame_policy"] = {
            "fps": fps["default"], "minimum_frames": frames.get("min"), "maximum_frames": frames.get("max"),
            "alignment_modulus": frames.get("alignment_modulus", 1), "alignment_remainder": frames.get("alignment_remainder", 0),
        }
        from studio.services.workflow import aligned_frames, WorkflowBuildError
        try:
            aligned_frames(0, contract["frame_policy"])
        except (WorkflowBuildError, TypeError, ValueError) as exc:
            raise ClassifiedReferenceError("视频帧数范围与格点未完整配置") from exc


def partition(references):
    result = {key: [] for key in MEDIA_GROUPS}
    for row in references:
        category = row.get("category")
        if category in {"character", "scene", "prop", "continuity_frame"}:
            result["images"].append(row)
        elif category == "video":
            result["videos"].append(row)
            if row.get("audio_asset_id") and row.get("use_embedded_audio"):
                raise ClassifiedReferenceError("视频配套音频不能同时选择外部与内嵌来源")
            if row.get("audio_asset_id") or row.get("use_embedded_audio"):
                result["video_audios"].append(row)
        elif category == "audio":
            result["audios"].append(row)
        else:
            raise ClassifiedReferenceError("分类参考素材包含未声明的媒体分类")
    return result


def prompt_references(contract, prompt):
    """Check canonical provenance without recognizing a model's prompt syntax."""
    payload = prompt.structured_payload
    if not isinstance(payload, dict):
        raise ClassifiedReferenceError("提示词缺少结构化参考合同")
    references = payload.get("reference_bindings", [])
    if not isinstance(references, list) or any(not isinstance(row, dict) for row in references):
        raise ClassifiedReferenceError("reference_bindings 必须为规范化对象数组")
    mode = contract["classified_references"]["conditioning_mode"]
    if prompt.conditioning_mode != mode or payload.get("effective_conditioning_mode") != mode:
        raise ClassifiedReferenceError("提示词模式与冻结分类素材合同不一致")
    keys = contract["prompt_template_keys"]
    if keys and payload.get("prompt_template_key") not in keys:
        raise ClassifiedReferenceError("提示词模板键不在 Projector 配置的允许列表中")
    identities = [row.get("asset_id") for row in references]
    identities += [row["audio_asset_id"] for row in references if row.get("audio_asset_id")]
    if any(not isinstance(value, str) or not value for value in identities) or len(set(identities)) != len(identities):
        raise ClassifiedReferenceError("参考素材 ID 必须存在且全局唯一")
    groups = partition(references)
    labels = []
    for key in ("images", "videos", "audios"):
        rows = groups[key]
        if [row.get("input_slot") for row in rows] != list(range(1, len(rows) + 1)):
            raise ClassifiedReferenceError(f"{key} 素材序号必须连续")
        for row in rows:
            label = row.get("input_label")
            if not isinstance(label, str) or not label or label not in prompt.prompt_text:
                raise ClassifiedReferenceError("提示词缺少参考素材标签")
            labels.append(label)
    for row in groups["videos"]:
        paired = bool(row.get("audio_asset_id") or row.get("use_embedded_audio"))
        label = row.get("audio_input_label")
        if bool(label) != paired or paired and (not isinstance(label, str) or label not in prompt.prompt_text):
            raise ClassifiedReferenceError("视频配套音轨标签与实际来源不一致")
        if paired:
            labels.append(label)
    if len(labels) != len(set(labels)):
        raise ClassifiedReferenceError("参考素材标签不能重复")
    return references


def materialize(graph, contract, references):
    plan = contract["classified_references"]
    target = plan["target_node_id"]
    result = deepcopy(graph)
    if target not in result or any(key.startswith("cr_") for key in result):
        raise ClassifiedReferenceError("动态素材目标不存在或来源图占用了保留节点 ID")
    groups = partition(references)
    for key, rows in groups.items():
        spec = plan["groups"][key]
        if not spec["min_items"] <= len(rows) <= spec["max_items"]:
            raise ClassifiedReferenceError(f"{key} 数量不满足冻结范围 {spec['min_items']}–{spec['max_items']}")
    target_inputs = result[target]["inputs"]
    if any(port in target_inputs for spec in plan["groups"].values() for port in spec["input_names"]):
        raise ClassifiedReferenceError("动态素材不能覆盖基础图中的已有连接")
    def load(kind, asset_id, node_id):
        node_type, port = {"image": ("LoadImage", "image"), "video": ("LoadVideo", "file"), "audio": ("LoadAudio", "audio")}[kind]
        result[node_id] = {"class_type": node_type, "inputs": {port: f"__AUTOPROMPT_ASSET__:{asset_id}"}}
        return [node_id, 0]
    for key in ("images", "videos", "audios"):
        for index, item in enumerate(groups[key]):
            link = load(MEDIA_GROUPS[key], item["asset_id"], f"cr_{key}_{index}")
            if key == "videos":
                components = f"cr_components_{index}"
                result[components] = {"class_type": "GetVideoComponents", "inputs": {"video": link}}
                link = [components, 0]
                if item.get("audio_asset_id") or item.get("use_embedded_audio"):
                    ports = plan["groups"]["video_audios"]["input_names"]
                    if index >= len(ports):
                        raise ClassifiedReferenceError("视频配套音轨的序号超出已配置端口范围")
                    target_inputs[ports[index]] = load("audio", item["audio_asset_id"], f"cr_paired_audio_{index}") if item.get("audio_asset_id") else [components, 1]
            target_inputs[plan["groups"][key]["input_names"][index]] = link
    if len(result) > boundary_int("security.comfy.canvas.max_nodes"):
        raise ClassifiedReferenceError("动态素材图超过节点预算")
    return result


def validate_frozen(contract, graph, definition, model_specification, bindings=None, requirements=None):
    expected = deepcopy(contract)
    attach_plan(expected, definition, graph, model_specification)
    for key in ("compiler", "classified_references", "conditioning_contract", "capability", "prompt_template_keys", "static_semantics", "frame_policy"):
        if contract.get(key) != expected.get(key):
            raise ClassifiedReferenceError(f"冻结合同 {key} 与 Projector / 模型版本配置不一致")
    if bindings is not None:
        for policy in definition["parameter_policies"]:
            role = definition["node_roles"][policy["node_role"]]
            targets = [key for key, node in graph.items() if node.get("class_type") == role["node_type"]]
            matched = [row for row in bindings if row["semantic_key"] == policy["semantic_key"]]
            if len(targets) != 1 or len(matched) != 1 or (matched[0]["node_id"], matched[0]["input_name"]) != (targets[0], policy["input_name"]):
                raise ClassifiedReferenceError("参数接线与 Projector 节点角色配置不一致")
            parameter = contract["parameters"][policy["semantic_key"]]
            if policy["policy"] == "fixed" and (parameter.get("default") != policy["value"] or parameter.get("const") != policy["value"]):
                raise ClassifiedReferenceError("固定参数默认值与 Projector 不一致")
            for key, value in policy.get("constraints", {}).items():
                if parameter.get(key) != value:
                    raise ClassifiedReferenceError(f"参数 {policy['semantic_key']}.{key} 与 Projector 范围配置不一致")
        for condition in definition["detector"]["input_equals"]:
            targets = {key for key, node in graph.items() if node.get("class_type") == condition["node_type"]}
            for binding in bindings:
                if binding["node_id"] in targets and binding["input_name"] == condition["input_name"]:
                    parameter = contract["parameters"][binding["semantic_key"]]
                    if parameter.get("const") != condition["value"] or parameter.get("default") != condition["value"]:
                        raise ClassifiedReferenceError("识别条件参数必须保持 Projector 冻结值，不能运行时切换")
        if requirements is not None:
            for selector in definition["model_selectors"]:
                for node_id, node in graph.items():
                    if node.get("class_type") != selector["node_type"]:
                        continue
                    matched = [row for row in bindings if row["node_id"] == node_id and row["input_name"] == selector["input_name"] and row["value_type"] == "model_artifact"]
                    if len(matched) != 1:
                        raise ClassifiedReferenceError("模型选择器缺少唯一文件绑定")
                    alias = contract["parameters"][matched[0]["semantic_key"]].get("requirement_alias")
                    required = [row for row in requirements if row["alias"] == alias]
                    if len(required) != 1 or required[0]["role"] != selector["role"]:
                        raise ClassifiedReferenceError("模型文件角色与 Projector 选择器不一致")


def validate_live_ports(graph, contract, object_info):
    from studio.services.comfyui_contract import validate_comfyui_api_contract
    from studio.services.comfyui import ComfyUIError
    for _kind, references in probe_cases(contract):
        try:
            validate_comfyui_api_contract(materialize(graph, contract, references), object_info, allow_managed_asset_tokens=True)
        except (ValueError, ComfyUIError) as exc:
            raise ClassifiedReferenceError(f"分类参考素材接线不满足在线节点合同：{exc}") from exc


def probe_references(contract, maximum=True):
    groups = contract["classified_references"]["groups"]
    bound = "max_items" if maximum else "min_items"
    result = [{"asset_id": "managed", "category": "character"} for _ in range(groups["images"][bound])]
    paired_count = groups["video_audios"][bound]
    for index in range(max(groups["videos"][bound], paired_count)):
        row = {"asset_id": "managed", "category": "video"}
        if index < paired_count:
            row.update({"use_embedded_audio": True} if index % 2 else {"audio_asset_id": "managed"})
        result.append(row)
    result.extend({"asset_id": "managed", "category": "audio"} for _ in range(groups["audios"][bound]))
    return result


def probe_cases(contract):
    yield "classified_references_minimum", probe_references(contract, False)
    yield "classified_references_maximum", probe_references(contract, True)
    groups = contract["classified_references"]["groups"]
    paired = groups["video_audios"]
    if paired["max_items"]:
        references = probe_references(contract, True)
        videos = [row for row in references if row["category"] == "video"]
        count = max(1, paired["min_items"])
        for index, row in enumerate(videos):
            row.pop("audio_asset_id", None)
            row.pop("use_embedded_audio", None)
            if index >= len(videos) - count:
                row["audio_asset_id"] = "managed"
        yield "classified_references_last_video_audio", references
