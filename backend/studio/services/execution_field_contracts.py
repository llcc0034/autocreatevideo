"""Frozen, owner-neutral field tasks for the registered auxiliary operations.

This is an internal contract boundary, not an API accepting caller-authored
field rules or a new model queue. A Preparation source adapter must authorize
and resolve its facts first. This module freezes exact published Profile rules
and validates returned text; it neither selects/approves fields nor calls AI.
"""
from copy import deepcopy
from dataclasses import dataclass
import re

from django.core.exceptions import ValidationError

from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import JsonContract, canonical_json_bytes, json_contract, verify_json_contract
from studio.services.prompt_normalization_contract import (
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    validate_director_text_characters,
    validate_frozen_prompt_field_rule,
    validate_prompt_field_text,
)
from studio.services.prompt_profiles import verify_prompt_profile_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


# Registered operation semantics, not aliases for prompt.normalize or a model.
FIELD_OPERATION_MODES = {
    "prompt.fields.generate": "model_generated",
    "prompt.fields.translate": "translate_only",
}
_ROOT_FIELDS = {"schema_version", "operation", "profile", "profile_field_keys", "items"}
_ITEM_FIELDS = {
    "request_id", "unit_key", "field_contract", "source_text", "source_language",
    "target_language", "context_facts", "reference_facts",
}
FIELD_REQUEST_SCHEMA_VERSION = 2


def shared_field_request(payload):
    """Freeze common facts once per owning unit, without altering field prose."""
    shared, identities, items = {}, {}, []

    def reference(unit, kind, value):
        record = {"unit_key": unit, "kind": kind, "value": deepcopy(value)}
        identity = canonical_json_bytes(record)
        if identity not in identities:
            key = f"f{len(shared) + 1}"
            identities[identity] = key
            shared[key] = record
        return identities[identity]

    for row in payload["items"]:
        item = deepcopy(row)
        item["context_refs"] = {key: reference(row["unit_key"], "context", value)
                                for key, value in item.pop("context_facts").items()}
        facts = item["reference_facts"]
        for kind in ("logical_inputs", "subject_facts"):
            facts[kind + "_ref"] = reference(row["unit_key"], kind, facts.pop(kind))
        items.append(item)
    return {**payload, "items": items, "shared_facts": shared}


def expand_field_request(payload):
    """Pure lookup of the saved fact table; never query current sources.

    The expanded view is transient for existing field validators and displays.
    It must not replace the normalized stored or model-facing request.
    """
    if (not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS | {"shared_facts"}
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != FIELD_REQUEST_SCHEMA_VERSION
            or not isinstance(payload["shared_facts"], dict) or not isinstance(payload["items"], list)):
        _error("field_request", "字段请求必须使用共享资料结构v2；旧请求不自动转换或重发")
    shared, used, items = payload["shared_facts"], set(), []
    for key, fact in shared.items():
        if (not isinstance(key, str) or re.fullmatch(r"f[1-9][0-9]*", key) is None
                or not isinstance(fact, dict) or set(fact) != {"unit_key", "kind", "value"}):
            _error("shared_facts", "共享资料标识或结构无效")

    def resolve(key, unit, kind):
        if not isinstance(key, str) or key not in shared:
            _error("shared_facts", "字段引用的共享资料不存在")
        fact = shared[key]
        if fact["unit_key"] != unit or fact["kind"] != kind:
            _error("shared_facts", "共享资料类型或所属单元不匹配，不能串用其他镜头资料")
        used.add(key)
        return deepcopy(fact["value"])

    for row in payload["items"]:
        if (not isinstance(row, dict) or set(row) != (_ITEM_FIELDS - {"context_facts"}) | {"context_refs"}
                or not isinstance(row["context_refs"], dict) or not isinstance(row["reference_facts"], dict)
                or set(row["reference_facts"]) != {"protected_tokens", "logical_inputs_ref", "subject_facts_ref"}):
            _error("items", "字段必须显式引用自己的共享资料，不能混入内联重复事实")
        item = deepcopy(row)
        item["context_facts"] = {key: resolve(ref, row["unit_key"], "context")
                                 for key, ref in item.pop("context_refs").items()}
        for kind in ("logical_inputs", "subject_facts"):
            item["reference_facts"][kind] = resolve(item["reference_facts"].pop(kind + "_ref"), row["unit_key"], kind)
        items.append(item)
    if used != set(shared):
        _error("shared_facts", "共享资料含未被字段使用的内容")
    return {key: (items if key == "items" else value) for key, value in payload.items() if key != "shared_facts"}


@dataclass(frozen=True)
class ExecutionFieldInput:
    request_id: str
    unit_key: str
    field_key: str
    source_text: str
    source_language: str
    target_language: str
    context_facts: dict
    reference_facts: dict


def _error(path, message):
    raise ValidationError({path: message})


def _text(value, path, *, required=False, maximum=None):
    if not isinstance(value, str) or (required and not value.strip()) or (maximum is not None and len(value) > maximum):
        _error(path, "必须提供符合长度要求的文本")
    validate_director_text_characters(value)
    return value


def _validated_request(payload):
    if not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS or type(payload["schema_version"]) is not int or payload["schema_version"] != FIELD_REQUEST_SCHEMA_VERSION:
        _error("field_request", "字段请求结构或版本无效")
    operation = payload["operation"]
    if not isinstance(operation, str) or operation not in FIELD_OPERATION_MODES:
        _error("operation", "必须使用明确的字段生成或翻译能力，不能借用其他操作")
    profile = payload["profile"]
    if not isinstance(profile, dict) or set(profile) != {"version_id", "contract_sha256"}:
        _error("profile", "缺少精确策略来源")
    _text(profile["version_id"], "profile.version_id", required=True, maximum=36)
    if not isinstance(profile["contract_sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", profile["contract_sha256"]) is None:
        _error("profile.contract_sha256", "缺少冻结策略摘要")
    keys = payload["profile_field_keys"]
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key for key in keys) or len(keys) != len(set(keys)):
        _error("profile_field_keys", "策略字段目录无效")
    items = payload["items"]
    if not isinstance(items, list) or not items:
        _error("items", "字段请求不能为空")
    seen, destinations = set(), set()
    for index, row in enumerate(items):
        path = f"items.{index}"
        if not isinstance(row, dict) or set(row) != _ITEM_FIELDS:
            _error(path, "必须提供完整字段事实，不能混入自由编写的模型参数")
        request_id = _text(row["request_id"], f"{path}.request_id", required=True, maximum=191)
        unit_key = _text(row["unit_key"], f"{path}.unit_key", required=True, maximum=191)
        if request_id in seen:
            _error(f"{path}.request_id", "字段请求标识重复")
        seen.add(request_id)
        rule = validate_frozen_prompt_field_rule(
            row["field_contract"], label=f"{path}.field_contract", schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        if rule["field_key"] not in keys or rule["generation_mode"] != FIELD_OPERATION_MODES[operation]:
            _error(f"{path}.field_contract", "字段不属于此策略或生成方式与执行能力不符")
        destination = (unit_key, rule["field_key"])
        if destination in destinations:
            _error(path, "同一单元字段不能在一个请求内重复生成")
        destinations.add(destination)
        source_text = _text(row["source_text"], f"{path}.source_text")
        for name in ("source_language", "target_language"):
            _text(row[name], f"{path}.{name}", maximum=40)
        if rule["language_policy"] == "inherit" and not row["target_language"].strip():
            _error(f"{path}.target_language", "继承语言必须由当前来源明确提供，不能默认英文")
        if rule["language_policy"] == "preserve_source" and not row["source_language"].strip():
            _error(f"{path}.source_language", "保留来源语言需要明确来源语言")
        if operation == "prompt.fields.translate" and not source_text.strip():
            _error(f"{path}.source_text", "翻译必须提供来源正文")
        context = row["context_facts"]
        if not isinstance(context, dict) or set(context) != set(rule["generation_context_selectors"]):
            _error(f"{path}.context_facts", "上下文必须与策略选定来源精确对应，不能缺失或自动加入其他单元事实")
        reference = row["reference_facts"]
        if not isinstance(reference, dict) or set(reference) != {"protected_tokens", "logical_inputs", "subject_facts"}:
            _error(f"{path}.reference_facts", "必须明确提供保护文本、逻辑输入及主体事实")
        tokens = reference["protected_tokens"]
        if not isinstance(tokens, list) or any(not isinstance(token, str) or not token or token not in source_text for token in tokens) or len(tokens) != len(set(tokens)):
            _error(f"{path}.reference_facts.protected_tokens", "保护文本必须唯一且真实存在于来源正文")
        if any(not isinstance(reference[name], list) or any(not isinstance(item, dict) for item in reference[name]) for name in ("logical_inputs", "subject_facts")):
            _error(f"{path}.reference_facts", "逻辑输入与主体事实必须是已解析事实数组")
    return payload


@security_boundary_resolution_cache()
def freeze_execution_field_request(*, operation, profile, inputs):
    """Only server-resolved facts enter; field_contract is not a caller input.

    The resulting JsonContract belongs in the child's immutable Preparation.
    A replay verifies that saved contract, not current Profile rows or a latest
    field candidate. Persistence, owner checks and queue dispatch are separate.
    """
    if profile.status != "published":
        _error("profile", "字段任务必须引用已发布的精确提示词策略")
    contract = verify_prompt_profile_contract(profile)
    if contract["schema_version"] != PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION:
        _error("profile", "字段任务需要当前完整策略合同，不转换历史规则")
    rules = {row["field_key"]: row for row in contract["rules"]}
    if not isinstance(inputs, (list, tuple)) or not inputs:
        _error("items", "必须提供已解析的字段事实")
    items = []
    for entry in inputs:
        if not isinstance(entry, ExecutionFieldInput):
            _error("items", "必须通过来源适配器构建字段事实，不能提交自定义字段合同")
        if not isinstance(entry.field_key, str) or entry.field_key not in rules:
            _error("field_key", "当前策略未声明此字段")
        items.append({
            "request_id": entry.request_id, "unit_key": entry.unit_key,
            "field_contract": deepcopy(rules[entry.field_key]),
            "source_text": entry.source_text, "source_language": entry.source_language,
            "target_language": entry.target_language, "context_facts": deepcopy(entry.context_facts),
            "reference_facts": deepcopy(entry.reference_facts),
        })
    payload = {
        "schema_version": FIELD_REQUEST_SCHEMA_VERSION, "operation": operation,
        "profile": {"version_id": str(profile.pk), "contract_sha256": profile.contract_sha256},
        "profile_field_keys": list(rules), "items": items,
    }
    _validated_request(payload)
    frozen = json_contract(shared_field_request(payload), max_bytes=execution_request_max_bytes())
    return frozen


@security_boundary_resolution_cache()
def verify_execution_field_request(frozen_request):
    """Verify the stored internal request before dispatch as well as parsing."""
    if not isinstance(frozen_request, JsonContract) or type(frozen_request.schema_version) is not int or frozen_request.schema_version != 1:
        _error("field_request", "必须读取已冻结请求及其摘要")
    verify_json_contract(
        frozen_request.value, schema_version=frozen_request.schema_version,
        size_bytes=frozen_request.size_bytes, sha256=frozen_request.sha256,
        max_bytes=execution_request_max_bytes(),
    )
    return _validated_request(expand_field_request(frozen_request.value))


@security_boundary_resolution_cache()
def validate_execution_field(*, field_contract, profile_field_keys, source_text, source_language,
                             target_language, protected_tokens, text, request_id, request_index=None,
                             reference_facts=None):
    """Shared pure check for manual revisions and model responses.

    The preparation service supplies the frozen rule and authorized source
    facts. This function grants neither approval nor permission to edit a
    server-locked field; it does not select a revision or call a model.
    """
    rule = validate_frozen_prompt_field_rule(field_contract, label="field_contract", schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION)
    if not isinstance(profile_field_keys, list) or rule["field_key"] not in profile_field_keys or any(not isinstance(key, str) or not key for key in profile_field_keys):
        _error("profile_field_keys", "字段必须属于精确策略目录")
    for key, value in (("source_text", source_text), ("source_language", source_language), ("target_language", target_language), ("request_id", request_id)):
        _text(value, key, required=key == "request_id")
    if not isinstance(text, str):
        _error("text", "字段结果必须是文本，不能隐式转换类型")
    json_contract({"text": text}, max_bytes=execution_configuration_max_bytes())
    if len(text) > rule["max_chars"]:
        _error("text", "结果超过字段硬字符上限")
    if not isinstance(protected_tokens, list) or any(not isinstance(token, str) or not token or token not in source_text for token in protected_tokens) or len(protected_tokens) != len(set(protected_tokens)):
        _error("protected_tokens", "保护文本必须唯一且真实存在于来源正文")
    if rule["language_policy"] == "inherit" and not target_language.strip():
        _error("target_language", "继承语言必须由来源明确提供")
    if rule["language_policy"] == "preserve_source" and not source_language.strip():
        _error("source_language", "保留来源语言需要明确来源语言")
    return validate_prompt_field_text(
        rule=rule, field_key=rule["field_key"], text=text,
        request_id=request_id, request_index=request_index,
    )


@security_boundary_resolution_cache()
def validate_execution_field_response(frozen_request, response):
    """Require exact IDs and validate all text; no partial adoption/approval."""
    request = verify_execution_field_request(frozen_request)
    json_contract(response, max_bytes=execution_configuration_max_bytes())
    if not isinstance(response, dict) or set(response) != {"items"} or not isinstance(response["items"], list):
        _error("response", "字段响应必须是仅含 items 的对象")
    expected = {row["request_id"]: row for row in request["items"]}
    if len(response["items"]) != len(expected):
        _error("response.items", "字段响应数量不匹配，不能部分采用")
    results = {}
    for index, row in enumerate(response["items"]):
        if not isinstance(row, dict) or set(row) != {"request_id", "text"}:
            _error(f"response.items.{index}", "每项必须且只能包含 request_id 与 text")
        identity = row["request_id"]
        if not isinstance(identity, str) or identity not in expected or identity in results:
            _error(f"response.items.{index}.request_id", "响应字段标识缺失、重复或不属于本次请求")
        if not isinstance(row["text"], str):
            _error(f"response.items.{index}.text", "字段结果必须是文本，不能隐式转换其他类型")
        results[identity] = row["text"]
    warnings = []
    for index, row in enumerate(request["items"]):
        warnings.extend(validate_execution_field(
            field_contract=row["field_contract"], profile_field_keys=request["profile_field_keys"],
            source_text=row["source_text"], source_language=row["source_language"], target_language=row["target_language"],
            protected_tokens=row["reference_facts"]["protected_tokens"], text=results[row["request_id"]],
            reference_facts=row["reference_facts"],
            request_id=row["request_id"], request_index=index,
        ))
    return {
        "result": {"items": [{"request_id": row["request_id"], "text": results[row["request_id"]]} for row in request["items"]]},
        "warnings": warnings,
    }
