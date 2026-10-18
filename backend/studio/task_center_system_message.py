"""Verify original assembly receipts. Never reconstruct historical protocol prose."""
from studio.services.system_message_layout import OPERATIONS, SECTION_LABELS, inspect_system_message_layout


def system_message_trace(transport, definition):
    operation = transport.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        return None
    unavailable = {"state": "unavailable", "rows": [], "message":
        "本次未记录可验证的system分段来源；保留原消息，不按当前适配器或正文相似度补造历史。"}
    try:
        from studio.services.execution_native_text import NATIVE_TEXT_ADAPTERS
        from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
        from studio.services.execution_output_contracts import STRUCTURED_TEXT_OPERATION, STRUCTURED_TEXT_ADAPTER
        field_operation = operation in FIELD_OPERATION_MODES
        structured = operation == STRUCTURED_TEXT_OPERATION
        semantic_script = operation in {"script.generate", "script.structure"}
        adapter = STRUCTURED_TEXT_ADAPTER if structured else "text.prompt_fields/v1" if field_operation else NATIVE_TEXT_ADAPTERS[operation]
        if (definition["configuration"]["operation"] != operation
                or definition["interface"]["output"] != transport["output"]
                or transport["output"]["adapter"] != adapter):
            return unavailable
        if field_operation or structured:
            if field_operation and "operation_plan" in transport:
                return unavailable
            if structured:
                from studio.services.execution_structured_text import verify_structured_text_plan
                from django.core.exceptions import ValidationError
                try:
                    verify_structured_text_plan(transport.get("operation_plan"), expected_output=definition["interface"]["business_output"])
                except ValidationError:
                    return unavailable
            layout = transport["system_message_layout"]
        else:
            if transport["operation_plan"]["operation"] != operation or "system_message_layout" in transport:
                return unavailable
            layout = transport["operation_plan"]["plan"]["system_message_layout"]
        allowed, required = OPERATIONS[operation], OPERATIONS[operation] if field_operation else {"protocol"}
        if semantic_script:
            from studio.services.script_semantic_requests import SCRIPT_SEMANTIC_SECTIONS
            output = definition["interface"]["business_output"]
            if not isinstance(output, dict) or output != transport["operation_plan"]["plan"]["business_output"]:
                return unavailable
            allowed, required = SCRIPT_SEMANTIC_SECTIONS, {"protocol", "output_structure", "semantic_mapping"}
        messages = transport["payload"]["messages"]
        trace = inspect_system_message_layout(layout, messages, allowed_sections=allowed, required_sections=required)
        rows = []
        for section in trace["rows"]:
            key = section["key"]
            label, source = SECTION_LABELS[key]
            if semantic_script and key == "strategy":
                source = "operation_plan.plan.prompt_profile_assembly.channels.system_instruction（去除首尾空白，不附加旧协议标签）"
            if field_operation and key == "protocol":
                label, source = "字段批次返回协议", "平台字段生成／翻译适配器的固定JSON结构及request_id完整性要求，不是主策略字段"
            rows.append({"label": label, "source": source, "start": section["start"], "end": section["end"],
                         "state": section["state"]})
        return {"state": trace["state"], "rows": rows, "message":
            "按本次组装时冻结的连续分段、字符位置与摘要核对整条system；不重复保存正文，不读取当前配置。"
            "分段来源说明不是可编辑权限，也不证明供应商收到、模型遵循或任务成功。"}
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
        return unavailable
