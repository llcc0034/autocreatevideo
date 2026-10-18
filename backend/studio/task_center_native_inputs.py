"""Compare original native plans to saved user JSON, without request rebuilding.

The caller authorizes and verifies both frozen envelopes first. No lookups,
defaults, provider calls, or comparison against today's protocol prose occur.
Only bounded registered paths are returned, never duplicate business text.
"""
import json

from django.core.exceptions import ValidationError

from studio.services.execution_native_text import NATIVE_TEXT_ADAPTERS


_MISSING = object()
_OPERATIONS = frozenset({"prompt.normalize", "visual_rules.generate", "script.generate", "script.structure"})


def _at(value, path):
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _comparison(expected, actual):
    return "unavailable" if expected is _MISSING else "mismatch" if actual is _MISSING else (
        "matched" if _encoded(expected) == _encoded(actual) else "mismatch")


def _result(rows, count, message):
    states = {row["state"] for row in rows}
    state = "mismatch" if "mismatch" in states else "partial" if count or "unavailable" in states else "matched"
    return {"state": state, "rows": rows, "unmapped_count": count, "message": message}


def frozen_normalization_items(transport):
    """Pure v1 projection shared with channel tracing; never validates via today's policy.

    Only independent normalization is supported, not auxiliary generation contexts.
    The caller has already verified the original request and definition envelopes.
    """
    from studio.services.prompt_normalization_contract import _provider_items_with_protected_placeholders

    envelope = transport["operation_plan"]
    if envelope["operation"] != "prompt.normalize":
        raise ValueError("different plan")
    plan = envelope["plan"]
    channels = plan["prompt_profile_assembly"]["channels"]
    if (not isinstance(channels, dict) or any(not isinstance(value, str) for value in channels.values())
            or channels != transport["prompt_channels"]["values"]
            or set(plan["request"]) != {"schema_version", "items"}
            or type(plan["request"]["schema_version"]) is not int or plan["request"]["schema_version"] != 1):
        raise ValueError("inconsistent independent normalization plan")
    items = plan["request"]["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("missing items")
    text_keys = {"request_id", "text", "source_lang", "target_lang", "field_key", "label", "context"}
    ids = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != text_keys | {"protected_tokens"}
                or any(not isinstance(item[key], str) for key in text_keys)
                or not item["request_id"] or item["request_id"] in ids
                or not isinstance(item["protected_tokens"], list)
                or any(not isinstance(token, str) or not token for token in item["protected_tokens"])
                or len(set(item["protected_tokens"])) != len(item["protected_tokens"])):
            raise ValueError("invalid frozen item")
        ids.add(item["request_id"])
    provider_items, _, mapping = _provider_items_with_protected_placeholders(items)
    return provider_items, mapping


def _normalization_trace(transport, user):
    from studio.services.prompt_normalization_contract import _replace_protected_tokens_in_fact_tree

    plan = transport["operation_plan"]["plan"]
    items, mapping = frozen_normalization_items(transport)
    rows = []

    def compare(key, source, label, expected):
        rows.append({"label": label, "source": source,
                     "destination": "payload.messages.1.content → JSON." + key,
                     "state": _comparison(expected, _at(user, key))})

    compare("schema_version", "operation_plan.plan.request.schema_version", "请求结构版本", plan["request"]["schema_version"])
    compare("task", "output.adapter → text.prompt_normalize/v1 固定任务类型", "平台翻译任务类型", "translate_prompt_fields")
    compare("items", "operation_plan.plan.request.items（保护词转换，保留顺序与类型）", "规范化条目与保护词", items)
    compare("response_skeleton", "operation_plan.plan.request.items[].request_id（保序；translation 为空）",
            "返回骨架（不是模型输出）", {"schema_version": 1, "items": [
                {"request_id": item["request_id"], "translation": ""} for item in items]})
    profile = user.get("prompt_profile")
    if not isinstance(profile, dict):
        raise ValueError("invalid profile")
    keys = set()
    for key, source, label in (
        ("profile_key", "profile_key", "策略标识"),
        ("version", "version", "策略版本"),
        ("system_constraints_as_data", "channels.system_instruction", "系统约束（作为user数据）"),
        ("positive_prompt", "channels.positive_prompt", "正向提示词"),
        ("negative_prompt", "channels.negative_prompt", "负向提示词"),
        ("structured_fields", "structured_fields", "随消息发送的字段审计副本"),
    ):
        keys.add(key)
        expected = _at(plan["prompt_profile_assembly"], source)
        if expected is not _MISSING:
            # Bound only this read-side traversal, not the original execution
            # policy. Never query current limits to certify historical content.
            expected = _replace_protected_tokens_in_fact_tree(
                expected, mapping=mapping, label="prompt_profile", _maximum_depth=64)
        compare("prompt_profile." + key, "operation_plan.plan.prompt_profile_assembly." + source + "（保护词转换）", label, expected)
    count = len(set(user) - {"schema_version", "task", "items", "response_skeleton", "prompt_profile"}) + len(set(profile) - keys)
    return _result(rows, count,
        "按原冻结条目复用v1保护词转换，核对全部items的顺序、身份、语言、正文、上下文及保护词，"
        "并核对返回骨架与策略数据；缺少源值不补默认。返回骨架不是模型输出，系统约束在user中作为数据。"
        "仅核对user JSON，不重建请求、不校验system平台正文，也不证明模型送达或任务成功。")


def _script_semantic_trace(operation, plan, user):
    from studio.services.native_prompt_payload import native_prompt_payload

    expected_inputs, actual_inputs = plan["inputs"], user.get("inputs")
    if not isinstance(expected_inputs, dict) or not isinstance(actual_inputs, dict):
        raise ValueError("invalid script inputs")
    _, profile = native_prompt_payload(plan["prompt_profile_assembly"], operation=operation)
    rows = [{"label": "剧本能力", "source": "operation_plan.operation",
        "destination": 'payload.messages.1.content → JSON["operation"]',
        "state": _comparison(operation, user.get("operation", _MISSING))}]
    for key, expected in expected_inputs.items():
        index = "[" + json.dumps(key, ensure_ascii=False) + "]"
        rows.append({"label": key, "source": "operation_plan.plan.inputs" + index,
            "destination": 'payload.messages.1.content → JSON["inputs"]' + index,
            "state": _comparison(expected, actual_inputs.get(key, _MISSING))})
    actual_profile = user.get("prompt_profile")
    if not isinstance(actual_profile, dict):
        raise ValueError("invalid profile")
    for key, expected in profile.items():
        rows.append({"label": key, "source": "operation_plan.plan.prompt_profile_assembly.channels." + key,
            "destination": "payload.messages.1.content → JSON.prompt_profile." + key,
            "state": _comparison(expected, actual_profile.get(key, _MISSING))})
    count = (len(set(user) - {"operation", "inputs", "prompt_profile"})
        + len(set(actual_inputs) - set(expected_inputs)) + len(set(actual_profile) - set(profile)))
    return _result(rows, count, "按本次冻结计划逐个核对实际输入字段及策略通道，嵌套内容完整比较；"
        "不读取旧剧本字段合同、不按字段名猜含义。原稿修订身份仅在计划中留存，不要求发给模型。"
        "此核对不证明模型收到或任务成功。")


def native_input_trace(transport, definition):
    operation = transport.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        return None
    result = {"state": "unavailable", "message": "冻结计划、协议身份或user JSON不完整，无法核对；原消息保留，不用当前配置补造。",
              "rows": [], "unmapped_count": 0}
    try:
        if (definition["configuration"]["operation"] != operation
                or definition["interface"]["output"] != transport["output"]
                or transport["output"]["adapter"] != NATIVE_TEXT_ADAPTERS[operation]
                or transport["operation_plan"]["operation"] != operation):
            return result
        plan = transport["operation_plan"]["plan"]
        semantic_script = operation in {"script.generate", "script.structure"}
        if not isinstance(plan, dict) or (not semantic_script and not isinstance(plan.get("request"), dict)):
            return result
        messages = transport["payload"]["messages"]
        if (not isinstance(messages, list) or len(messages) != 2
                or any(not isinstance(m, dict) or not isinstance(m.get("content"), str) for m in messages)
                or [m.get("role") for m in messages] != ["system", "user"]):
            return result
        user = json.loads(messages[1]["content"], object_pairs_hook=_unique)
        if not isinstance(user, dict):
            return result
        # Check malformed non-finite JSON even in unrecognized fields.
        _encoded(user)
        if semantic_script:
            output = definition["interface"]["business_output"]
            if not isinstance(output, dict) or plan.get("business_output") != output:
                return result
            return _script_semantic_trace(operation, plan, user)
        if operation == "prompt.normalize":
            return _normalization_trace(transport, user)
        rows, known = [], {"prompt_profile"}

        def compare(key, source, label):
            expected, actual = _at(plan, source), _at(user, key)
            rows.append({"label": label, "source": "operation_plan.plan." + source,
                         "destination": "payload.messages.1.content → JSON." + key, "state": _comparison(expected, actual)})

        for key, label in (("positive_prompt", "正向提示词"), ("negative_prompt", "负向提示词")):
            compare("prompt_profile." + key, "prompt_profile_assembly.channels." + key, label)
        profile = user.get("prompt_profile")
        if not isinstance(profile, dict):
            return result
        profile_keys = {"positive_prompt", "negative_prompt"}
        # Historical audit copies are inspected only when actually in the wire
        # message; this does not restore them to the current request builder.
        if "structured_fields" in profile:
            profile_keys.add("structured_fields")
            compare("prompt_profile.structured_fields", "prompt_profile_assembly.structured_fields", "随消息发送的字段审计副本")
        count = len(set(user) - known) + len(set(profile) - profile_keys)
        return _result(rows, count, "仅比对冻结计划与user JSON的已登记位置；类型和值均核对，不忽略额外字段。不校验system平台协议正文，也不证明模型已收到或任务成功。")
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError, ValidationError):
        return result
