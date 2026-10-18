"""Explicit independent translation through the shared native text protocol.

Reuses the existing wire/protected-token validators. No Binding lookup, default
language, target production-field authority, retry sample or hidden source.
"""
from copy import deepcopy
import json

from django.core.exceptions import ValidationError

from studio.services.prompt_normalization_contract import (
    PromptNormalizationContractError, parse_prompt_normalization_chat_response,
    prompt_normalization_chat_payload, _validated_items,
)


def normalization_request(items):
    # The reused historical validator coerces/shortens optional strings. This
    # public instance boundary requires exact types and lengths before it runs;
    # omitted metadata must be explicitly empty, languages must be selected.
    text_fields = {"request_id", "text", "source_lang", "target_lang", "field_key", "label", "context"}
    if not isinstance(items, list) or not items:
        raise ValidationError({"items": "请提供本次要翻译的非空条目列表"})
    for index, item in enumerate(items):
        path = f"items[{index}]"
        if not isinstance(item, dict) or set(item) != {*text_fields, "protected_tokens"}:
            raise ValidationError({path: "必须明确提供条目标识、正文、源/目标语言、字段标识、标签、上下文及保护词；未使用的说明可填空字符串"})
        for key in text_fields:
            value = item[key]
            if not isinstance(value, str):
                raise ValidationError({f"{path}.{key}": "需要文本，不能转换类型"})
            if key in {"request_id", "source_lang", "target_lang"} and (not value or value != value.strip()):
                raise ValidationError({f"{path}.{key}": "请明确填写，不能留空或使用隐式默认值"})
        if not isinstance(item["protected_tokens"], list):
            raise ValidationError({f"{path}.protected_tokens": "需要明确的保护词列表，未使用时填写空列表"})
    payload = {"schema_version": 1, "items": deepcopy(items)}
    try:
        validated = _validated_items(payload)
    except PromptNormalizationContractError as exc:
        raise ValidationError(str(exc), code=exc.code) from exc
    if validated != items:
        raise ValidationError({"items": "条目不符合翻译协议，不能截断、补值或修改后提交"})
    return payload


def normalization_payload(plan, *, system_layout=None):
    payload = deepcopy(plan["request"])
    if "prompt_profile_assembly" in plan:
        payload["prompt_profile_assembly"] = deepcopy(plan["prompt_profile_assembly"])
    try:
        request = prompt_normalization_chat_payload(payload, policy=plan["policy"], output_length=plan["output_length"], system_layout=system_layout)
    except PromptNormalizationContractError as exc:
        raise ValidationError(str(exc), code=exc.code) from exc
    request["seed"] = plan["seed"]
    return request


def normalization_result(plan, text):
    try:
        parsed = parse_prompt_normalization_chat_response(plan["request"], {"text": text})
    except PromptNormalizationContractError as exc:
        raise ValidationError(str(exc), code=exc.code) from exc
    result = {"schema_version": 1, "items": deepcopy(parsed["items"])}
    return {"result": result, "warnings": deepcopy(parsed["metadata"].get("warnings", [])),
        "text": json.dumps(result, ensure_ascii=False, sort_keys=True)}
