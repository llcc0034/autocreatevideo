"""Record system-message sections at assembly time, without copying their text.

Layouts are frozen with the existing request, never sent to a provider.
They describe provenance, not authority to edit platform protocol instructions.
"""
from hashlib import sha256
import re


SECTION_LABELS = {
    "semantic_mapping": ("剧本语义映射", "本次确切业务输出版本的 parser.mapping 及平台语义目录；不是第二份模型输出格式"),
    "output_structure": ("确切输出结构", "operation_plan.plan.business_output.data_structure 编译的 JSON Schema"),
    "prompt_channels": ("策略／人工系统通道", "本次冻结的 prompt_channels.values.system_instruction"),
    "protocol": ("平台协议与安全约束", "平台适配器；部分约束由本次冻结参数生成，不是策略字段"),
    "field_contract": ("文本字段规则", "operation_plan.plan.field_contract（字段类型、必填与长度）"),
    "output_contract": ("返回结构示例", "本次输出合同与冻结参数生成；是结构示例，不是模型返回"),
    "output_enums": ("色彩允许取值", "输出协议的色彩枚举；已选业务输出版本时采用其冻结允许值"),
    "negative_grammar": ("负面约束格式", "本次冻结输出格式对应的解析规则"),
    "quality_advisories": ("输出质量建议", "operation_plan.plan.resource_estimate.quality_advisories；不代替人工审核"),
    "strategy": ("策略／人工系统通道", "operation_plan.plan.prompt_profile_assembly.channels.system_instruction（去除首尾空白并加协议段标签）"),
    "planning": ("结构化规划约束", "operation_plan.plan.request.planning存在时，由结构化适配器附加"),
    "transport_probe": ("传输容量探测说明", "本次传输探测输入对应的平台约束；不是生成内容"),
    "trusted_tasks": ("受控字段任务约束", "本次已验证字段任务／翻译约束；不是普通user数据指令"),
    "footer": ("响应格式收束", "平台适配器的JSON响应要求"),
    "field_rules": ("字段批次消费与安全约束", "平台字段协议；具体写作、语言和来源规则在同次user批次items.*.field_contract，不是策略system通道"),
}
OPERATIONS = {
    "text.structured": {"protocol", "output_structure", "prompt_channels"},
    "visual_rules.generate": {"protocol", "output_contract", "output_enums", "negative_grammar", "quality_advisories", "strategy"},
    "script.generate": {"protocol", "field_contract", "output_contract", "strategy"},
    "script.structure": {"protocol", "field_contract", "output_contract", "strategy", "planning"},
    "prompt.normalize": {"protocol", "transport_probe", "trusted_tasks", "footer"},
    "prompt.fields.generate": {"protocol", "field_rules"},
    "prompt.fields.translate": {"protocol", "field_rules"},
}


def text_sha(value):
    return sha256(value.encode("utf-8")).hexdigest()


def assemble_system_message(parts, *, layout=None):
    """Join exact segments once; offsets count Unicode code points, not bytes."""
    content, rows, keys = "", [], set()
    for key, text in parts:
        if key not in SECTION_LABELS or key in keys or not isinstance(text, str):
            raise ValueError("invalid system message section")
        keys.add(key)
        if not text:
            continue
        start = len(content)
        content += text
        rows.append({"key": key, "start": start, "end": len(content), "sha256": text_sha(text)})
    if layout is not None:
        if not isinstance(layout, dict) or layout:
            raise ValueError("system message layout must be a new object")
        layout.update(schema_version=1, message_index=0, role="system", characters=len(content),
                      sha256=text_sha(content), sections=rows)
    return content


def inspect_system_message_layout(layout, messages, *, allowed_sections, required_sections):
    """Check frozen ranges/hashes, never reconstruct current protocol prose."""
    if (not isinstance(layout, dict) or set(layout) != {
            "schema_version", "message_index", "role", "characters", "sha256", "sections"}
            or type(layout["schema_version"]) is not int or layout["schema_version"] != 1
            or type(layout["message_index"]) is not int or layout["message_index"] != 0
            or layout["role"] != "system" or type(layout["characters"]) is not int or layout["characters"] <= 0
            or not isinstance(messages, list) or len(messages) != 2
            or any(not isinstance(m, dict) for m in messages)
            or [m.get("role") for m in messages] != ["system", "user"]
            or any(not isinstance(m.get("content"), str) for m in messages)):
        raise ValueError("invalid system message layout")
    sections = layout["sections"]
    if not isinstance(sections, list) or not 1 <= len(sections) <= len(allowed_sections):
        raise ValueError("invalid system message sections")
    body, end, keys, rows = messages[0]["content"], 0, set(), []

    def valid_hash(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    if not valid_hash(layout["sha256"]):
        raise ValueError("invalid system message hash")
    for section in sections:
        if (not isinstance(section, dict) or set(section) != {"key", "start", "end", "sha256"}
                or not isinstance(section["key"], str) or section["key"] not in allowed_sections
                or section["key"] in keys or type(section["start"]) is not int or section["start"] != end
                or type(section["end"]) is not int or not end < section["end"] <= layout["characters"]
                or not valid_hash(section["sha256"])):
            raise ValueError("invalid system message section")
        keys.add(section["key"])
        rows.append({"key": section["key"], "start": end, "end": section["end"],
            "state": "matched" if text_sha(body[end:section["end"]]) == section["sha256"] else "mismatch"})
        end = section["end"]
    if end != layout["characters"] or not set(required_sections) <= keys:
        raise ValueError("incomplete system message sections")
    matched = len(body) == end and text_sha(body) == layout["sha256"] and all(row["state"] == "matched" for row in rows)
    return {"state": "matched" if matched else "mismatch", "rows": rows}
