"""Explain saved prompt assembly without compiling or querying current policy.

Channel comparison is structural, not substring matching or a claim that every
wire byte came from the strategy. Protocol-owned content remains separate.
"""
import json

from django.core.exceptions import ValidationError

from studio.task_center_public import public_value


def prompt_trace(transport, definition, resolved):
    result = {"state": "unavailable", "message": "没有完整的冻结策略装配记录；不按当前策略补造来源。",
              "profile_id": None, "profile_sha256": None, "fields": [], "channels": []}
    compilation = transport.get("profile_compilation")
    if compilation is None:
        return {**result, "message": "本次未记录策略编译结果，可能使用人工通道；最终正文仍以下方原消息为准。"}
    try:
        assembly = compilation["assembly"]
        profile = definition["interface"]["prompt"]
        if (type(assembly["schema_version"]) is not int or assembly["schema_version"] != 1
                or profile["mode"] != "profile" or assembly["prompt_profile_version_id"] != profile["profile_id"]
                or assembly["contract_sha256"] != profile["profile_sha256"]):
            return result
        channels, traces, rules = assembly["channels"], assembly["trace"], assembly["field_rules"]
        if (not isinstance(channels, dict) or not isinstance(traces, list) or not isinstance(rules, list)
                or any(not isinstance(value, str) for value in channels.values())):
            return result
        fields = []
        seen = set()
        for row in traces:
            key, selector, channel = row["field_key"], row["source_selector"], row["channel"]
            matching = [rule for rule in rules if rule["field_key"] == key]
            if (not all(isinstance(value, str) and value for value in (key, selector, channel))
                    or key in seen or channel not in channels or len(matching) != 1
                    or matching[0]["source_selector"] != selector or matching[0]["channel"] != channel
                    or key not in assembly["structured_fields"] or type(row["defaulted"]) is not bool
                    or type(row.get("reviewed", False)) is not bool):
                return result
            seen.add(key)
            sources = []
            if not row["defaulted"] and not row.get("reviewed", False):
                # Match only the explicit target mapping, never a same-name key.
                keys = {spec["key"] for spec in definition["configuration"]["inputs"]
                        if any(target["target_kind"] == "profile_input" and target["target_key"] == selector
                               for target in spec["targets"])}
                for source in resolved.get("inputs", {}).get("input_provenance", []):
                    if source.get("input_key") in keys:
                        reference = source.get("source_reference", {})
                        sources.append({"input_key": source["input_key"], "kind": source.get("kind"),
                            "location": reference.get("location"), "source_id": reference.get("source_id"),
                            "value_sha256": source.get("value_sha256"), "edited": source.get("edited")})
            fields.append({"field_key": key, "source_selector": selector, "channel": channel,
                "defaulted": row["defaulted"], "reviewed": row.get("reviewed", False),
                "projection_primitive": row.get("projection_primitive", ""), "sources": sources})
        prepared = transport["prompt_channels"]["values"]
        if (not isinstance(prepared, dict) or any(channels.get(key) != value for key, value in prepared.items())
                or any(value and key not in prepared for key, value in channels.items())):
            return result
        result.update(state="recorded", message="字段来源读取原策略装配审计；通道与冻结请求按已支持协议逐项比对，不重新编译，也不按同名或正文相似度推断。",
                      profile_id=assembly["prompt_profile_version_id"], profile_sha256=assembly["contract_sha256"], fields=fields,
                      channels=[_channel(transport, key, value, definition) for key, value in prepared.items()])
    except (KeyError, TypeError, AttributeError):
        return result
    return public_value(result)


def _channel(transport, channel, text, definition=None):
    row = {"channel": channel, "characters": len(text), "state": "unavailable", "destination": "",
           "message": "此协议尚未提供通道到消息的结构化比对；不能据此推断未发送。"}
    if transport.get("driver_type") in {"comfyui", "async_rest"}:
        return _media_channel(transport, definition, row, text)
    messages = transport["payload"].get("messages")
    if not isinstance(messages, list):
        return row
    operation, adapter = transport["operation"], transport["output"].get("adapter")
    if not isinstance(adapter, str):
        return row
    direct = operation == "prompt.draft" and adapter == "text.prompt_draft/v1"
    visual = operation == "visual_rules.generate" and adapter == "text.visual_rules/v2"
    script = (operation, adapter) in {("script.generate", "text.script_generate/v2"),
                                     ("script.structure", "text.script_structure/v3")}
    normalize = operation == "prompt.normalize" and adapter == "text.prompt_normalize/v1"
    from studio.services.execution_output_contracts import STRUCTURED_TEXT_OPERATION, STRUCTURED_TEXT_ADAPTER
    structured = operation == STRUCTURED_TEXT_OPERATION and adapter == STRUCTURED_TEXT_ADAPTER
    if not any((direct, visual, script, normalize, structured)):
        return row
    if channel not in {"system_instruction", "positive_prompt", "negative_prompt"}:
        return row
    role = "system" if channel == "system_instruction" and not normalize else "user"
    candidates = [(index, item) for index, item in enumerate(messages) if isinstance(item, dict) and item.get("role") == role]
    if len(candidates) != 1 or not isinstance(candidates[0][1].get("content"), str):
        return {**row, "state": "mismatch", "message": "该通道的目标消息缺失、重复或不是文本；请核对原请求。"}
    index, message = candidates[0]
    content = message["content"]
    destination = f"payload.messages.{index}.content"
    if script and channel == "system_instruction":
        from studio.task_center_system_message import system_message_trace
        trace = system_message_trace(transport, definition)
        if not trace or trace["state"] != "matched":
            return {**row, "message": "本次剧本输出协议的system分段不可核对；不重建旧策略正文。"}
        sections = [part for part in transport["operation_plan"]["plan"]["system_message_layout"]["sections"] if part["key"] == "strategy"]
        if not text.strip() and not sections:
            return {**row, "state": "omitted", "message": "空系统通道未附加；system保留确切输出结构及语义映射。"}
        matches = len(sections) == 1 and content[sections[0]["start"]:sections[0]["end"]] == text.strip()
        destination += " → operation_plan.plan.system_message_layout.strategy"
        note = "系统通道去除首尾空白后与冻结分段完全一致；输出结构和语义映射不属于策略字段。"
    elif structured and channel == "system_instruction":
        from studio.task_center_system_message import system_message_trace
        trace = system_message_trace(transport, definition or {})
        if not trace or trace["state"] != "matched":
            return {**row, "message": "本次结构化输出协议的system分段不可核对；不重建策略正文。"}
        sections = [part for part in transport["system_message_layout"]["sections"] if part["key"] == "prompt_channels"]
        if not text and not sections:
            return {**row, "state": "omitted", "message": "空系统通道未附加；system保留确切JSON输出结构。"}
        matches = len(sections) == 1 and content[sections[0]["start"]:sections[0]["end"]] == text
        destination += " → system_message_layout.prompt_channels"
        note = "系统通道与冻结分段完全一致；之前的JSON结构来自确切业务输出版本，不是重复策略字段。"
    elif (direct and channel in {"system_instruction", "positive_prompt"}) or (structured and channel == "positive_prompt"):
        matches, note = content == text, "通道正文与整条消息完全一致。"
    elif visual and channel == "system_instruction":
        if not text.strip():
            return {**row, "state": "omitted", "message": "空系统通道未附加；system 中仍有适配器的输出协议。"}
        marker = "CONFIGURED_SYSTEM_INSTRUCTION="
        matches = content.endswith(marker + text.strip())
        destination += " → " + marker.strip()
        note = "系统通道去除首尾空白后写入该配置段；此前输出协议不属于策略字段。"
    elif ((visual or script) and channel in {"positive_prompt", "negative_prompt"}) or normalize:
        expected, field = text, channel
        if normalize:
            try:
                expected = _normalization_channel(transport, text)
            except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
                return {**row, "message": "冻结规范化计划或保护词映射不完整，无法核对转换后的通道；不使用当前配置补造。"}
            if channel == "system_instruction":
                field = "system_constraints_as_data"
        try:
            def unique(pairs):
                value = {}
                for key, child in pairs:
                    if key in value:
                        raise ValueError("duplicate JSON key")
                    value[key] = child
                return value
            payload = json.loads(content, object_pairs_hook=unique)
            matches = isinstance(payload, dict) and isinstance(payload.get("prompt_profile"), dict) and payload["prompt_profile"].get(field) == expected
        except (ValueError, RecursionError):
            matches = False
        destination += f" → JSON.prompt_profile.{field}"
        note = ("原通道按冻结条目的保护词转换后，与user中该JSON字段完全一致；系统约束在此作为数据，而非system角色指令。"
                if normalize else "通道正文与user中该JSON字段完全一致；包括明确的空字符串。")
    else:
        return row
    return {**row, "state": "matched" if matches else "mismatch", "destination": destination,
            "message": note if matches else "已保存通道与原消息该位置不一致；不选择覆盖值，不改变原任务结果。"}


def _media_channel(transport, definition, row, text):
    """Compare only frozen semantic/standard payload slots, not inferred ports.

    The task reader verifies the containing request first. There is no graph
    search by equal text and no claim about provider-side remapping or receipt.
    """
    from studio.services.execution_api_contracts import ASYNC_PROMPT_CHANNELS
    from studio.services.execution_prompts import WORKFLOW_PROMPT_CHANNELS

    unavailable = {**row, "message": "缺少一致的冻结媒体通道声明或已支持编译身份；不能按同名字段或相同正文推断映射。"}
    try:
        driver = transport["driver_type"]
        interface, target = definition["interface"], definition["target"]
        channels = interface["prompt"]["channels"]
        binding = channels[row["channel"]]
        key = binding["semantic_key"]
        registered = WORKFLOW_PROMPT_CHANNELS if driver == "comfyui" else ASYNC_PROMPT_CHANNELS
        if (row["channel"] not in registered or not isinstance(key, str)
                or target["driver_type"] != driver or definition["configuration"]["operation"] != transport["operation"]
                or interface["output"] != transport["output"]
                or key != registered.get(row["channel"]) or binding["rule"]["type"] != "string"
                or sum(spec["semantic_key"] == key for spec in channels.values()) != 1):
            return unavailable
        if driver == "comfyui":
            compiled = transport["payload"]["compiled_workflow"]
            workflow = target["workflow"]
            if (compiled["compiler_id"] != "generic.comfy" or compiled["compiler_id"] != workflow["compiler_id"]
                    or type(compiled["compiler_version"]) is not int or compiled["compiler_version"] not in {1, 2, 3}
                    or type(workflow["compiler_version"]) is not int or compiled["compiler_version"] != workflow["compiler_version"]
                    or not isinstance(workflow["execution_contract_sha256"], str) or not workflow["execution_contract_sha256"]
                    or compiled["workflow_contract_sha256"] != workflow["execution_contract_sha256"]):
                return unavailable
            values = compiled["resolved_semantics"]
            destination = f"payload.compiled_workflow.resolved_semantics[{json.dumps(key)}]"
            note = "通道与本次已编译工作流的语义输入完全一致；仅核对此层，不按正文反推节点端口，也不证明ComfyUI已收到或执行成功。"
        else:
            if target["workflow"] is not None or transport["output"]["adapter"] != "async_rest":
                return unavailable
            values = transport["payload"]
            destination = f"payload[{json.dumps(key)}]"
            note = "通道与本次REST标准请求字段完全一致；此为供应商适配前的标准体，不证明供应商字段映射、已收到或执行成功。"
        if not isinstance(values, dict):
            return unavailable
        matches = isinstance(values.get(key), str) and values[key] == text
        return {**row, "destination": destination, "state": "matched" if matches else "mismatch",
                "message": note if matches else "冻结请求的声明位置缺值、类型异常或正文不一致；不选择覆盖值，不改变原任务结果。"}
    except (KeyError, TypeError, AttributeError):
        return unavailable


def _normalization_channel(transport, text):
    """Replay only the pure v1 token projection on original frozen inputs.

    No policy lookup, request builder, current limits, or content inference.
    Independent normalization has no trusted generation-context projection.
    """
    from studio.services.prompt_normalization_contract import _replace_protected_tokens_in_string
    from studio.task_center_native_inputs import frozen_normalization_items

    _, mapping = frozen_normalization_items(transport)
    return _replace_protected_tokens_in_string(text, mapping=mapping, label="prompt_profile")


def protocol_notes(transport, definition):
    """Describe registered chat roles, never reconstruct or certify saved text.

    The request reader verifies the frozen envelopes. The protocol identity is
    checked against the original definition, not today's instance configuration.
    These short notes use the existing input.notes API/UI, not a second trace.
    """
    from studio.services.execution_native_text import NATIVE_TEXT_ADAPTERS
    from studio.services.execution_text_contracts import _ADAPTERS
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES

    payload = transport.get("payload")
    if not isinstance(payload, dict) or "messages" not in payload:
        return []
    unavailable = ["请求协议说明不可用：冻结能力、适配器或消息结构无法与已登记协议对应；请查看原消息，不按模型名称猜测，也不使用当前配置补齐。"]
    try:
        operation, output = transport["operation"], transport["output"]
        adapters = {**_ADAPTERS, **NATIVE_TEXT_ADAPTERS}
        if (not isinstance(operation, str) or operation not in adapters
                or output != definition["interface"]["output"]
                or operation != definition["configuration"]["operation"]
                or output["adapter"] != adapters[operation]):
            return unavailable
        messages = payload["messages"]
        if not isinstance(messages, list) or any(
            not isinstance(m, dict) or not isinstance(m.get("content"), str) for m in messages
        ):
            return unavailable
        roles = [m.get("role") for m in messages]
        if roles != ["system", "user"] and not (operation == "prompt.draft" and roles == ["user"]):
            return unavailable
    except (KeyError, TypeError, AttributeError):
        return unavailable

    notes = [f"冻结协议 {operation} / {output['adapter']}：以下说明登记的组装规则及配置归属，不是本次正文校验、模型送达或成功证明。历史正文以下方原消息为准。"]
    if operation == "prompt.draft":
        notes.append("起草协议将 system_instruction 通道直接放入 system、positive_prompt 通道直接放入 user；没有额外拼接业务 JSON 输出协议。通道内容来自所选提示词策略，或实例允许的本次人工填写，不由模型名称推断。")
    elif operation in FIELD_OPERATION_MODES:
        notes.append("辅助字段协议的 system 由平台维护 JSON 返回形状及 request_id 对应规则；user 是父准备冻结的 field_request 批次。生成要求、来源、语言及保护文本在父提示词策略的字段规则配置，不是在辅助实例再写一份。返回仍需人工确认。")
    elif operation == "visual_rules.generate":
        notes.append("影视规则协议的 system 由平台输出格式约束加策略系统通道组成（CONFIGURED_SYSTEM_INSTRUCTION）；user JSON.prompt_profile 承载已装配通道和结构化字段。策略字段与结构化字段可能同时包含同一输入，并不代表多次调用。")
        notes.append("配置归属：提示词策略管理业务措辞与取值来源；执行实例选择精确策略及业务输出版本；业务输出版本管理已开放的字段形状、允许值及质量建议。平台 JSON／安全约束不是策略可任意覆盖的字段。是否带入剧本须查看本次原消息，不能据当前配置断言旧任务未发送全文。")
    elif operation in {"script.generate", "script.structure"}:
        notes.append("剧本协议的system包含所选业务输出版本的JSON结构、消费语义/映射说明及策略系统通道；user.inputs保留本次已解析输入，user.prompt_profile放实际策略通道，不重复发送字段审计副本。")
        notes.append("字段与返回层级在共享结构/业务输出版本维护，创作措辞在提示词策略维护，模型只维护传输设置。结构化原稿全文与显式planning在inputs中保留，原稿修订身份与摘要留在冻结计划，不要求模型回显来源或采用事实。")
    elif operation == "text.structured":
        notes.append("通用结构化文本的system由确切业务输出版本编译的JSON Schema及策略／人工系统通道组成；user为本次正向提示词原文。字段含义、层级、类型与缺键默认来自同一冻结结构，不套用剧本、影视规则或翻译格式。")
        notes.append("输出版本和结构随原请求计划冻结；解析值与默认来源由共享JSON解析器提供，协议校验不等于人工采用或下游业务入库。")
    elif operation == "prompt.normalize":
        notes.append("规范化协议的 system 由平台维护翻译与返回格式规则；user 含 items、返回骨架及 prompt_profile。策略系统通道进入 user JSON.prompt_profile.system_constraints_as_data，作为数据而不是 system 指令；保护文本会按冻结映射转换。来源、语言及保护文本在本次准备确认，业务措辞在提示词策略配置。")
    notes.append("查看与修改：先核对本任务下方来源追溯、原始填写与解析结果，再到设置中的对应策略／模型／执行实例／业务输出版本。修改已发布配置须创建后继版本并在新任务使用，不会改写本次冻结请求。")
    notes.extend(chat_body_notes(transport))
    return notes


def chat_body_notes(transport):
    """Read original compiler evidence; never rebuild a historical sent body."""
    from studio.services.chat_body import CHAT_DRIVERS, validate_chat_body_layout
    if transport.get("driver_type") not in CHAT_DRIVERS:
        return []
    if "chat_body_layout" not in transport:
        return ["本次没有冻结Chat组包位置记录；不能按当前适配器补造历史HTTP正文。以下原messages仍可查看。"]
    try:
        layout = validate_chat_body_layout(transport["chat_body_layout"], transport.get("payload"))
    except ValidationError:
        return ["原Chat组包位置记录异常，不能视为已核对的请求正文；原任务结果不因此改变。"]
    notes = ["Chat组包位置（原冻结记录）：这是适配器JSON字段声明，不是逐字节HTTP正文、发送记录或送达证明。"]
    for row in layout["fields"]:
        key = json.dumps(row["key"], ensure_ascii=False)
        source = row["source"] + (f"[{key}]" if row["source"] == "payload.chat_request_options" else "")
        notes.append(f"{source} → JSON[{key}]。" + (
            "模型名来自所选模型版本。" if row["key"] == "model" else
            "保留消息角色、顺序和正文。" if row["key"] == "messages" else
            "模型版本的请求选项展开为JSON顶层字段，不保留chat_request_options包装。" if row["source"] == "payload.chat_request_options" else
            "读取本次冻结参数，0值不省略。"))
    if layout["transport_only"]:
        notes.append("payload.timeout_seconds → HTTP客户端超时；不进入JSON正文。")
    notes.append("请求地址与凭据由模型部署／服务连接及密钥管理维护，不来自提示词策略、不进入此组包记录。修改模型选项须创建后继版本；本页面只读。")
    return notes
