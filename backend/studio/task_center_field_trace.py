"""Explain original field batches, without compiling, resolving or approving.

The caller verifies the frozen request envelope. This projection checks its
batch digest and exact wire JSON; it is not a fresh admission/rule validation.
"""
from hashlib import sha256
import json

from django.core.exceptions import ValidationError

from studio.services.execution_field_contracts import FIELD_OPERATION_MODES, expand_field_request
from studio.services.json_contracts import canonical_json_bytes


def field_batch_trace(transport, resolved):
    operation = transport.get("operation")
    if operation not in FIELD_OPERATION_MODES:
        return None
    result = {"state": "unavailable", "message": "缺少可核对的原冻结字段批次；不读取当前策略或补造来源。",
              "profile_id": None, "profile_sha256": None, "batch_sha256": None,
              "destination": "", "items": []}
    if transport.get("output", {}).get("adapter") != "text.prompt_fields/v1":
        return result
    try:
        batch = resolved["inputs"]["operation_inputs"]["field_request"]
        digest = sha256(canonical_json_bytes(batch)).hexdigest()
        if digest != transport["field_request_sha256"] or batch["operation"] != operation:
            return result
        # Historical wire evidence remains readable without converting or
        # accepting the old batch for new execution.
        if (type(batch.get("schema_version")) is int and batch["schema_version"] == 1
                and set(batch) == {"schema_version", "operation", "profile", "profile_field_keys", "items"}):
            expanded = batch
        else:
            expanded = expand_field_request(batch)
        profile = batch["profile"]
        if (set(profile) != {"version_id", "contract_sha256"}
                or not _text(profile["version_id"]) or not _digest(profile["contract_sha256"])
                or not isinstance(batch["profile_field_keys"], list)
                or any(not _text(key) for key in batch["profile_field_keys"])
                or len(set(batch["profile_field_keys"])) != len(batch["profile_field_keys"])
                or not isinstance(batch["items"], list) or not batch["items"]):
            return result
        items, identities, fields = [], set(), set()
        for index, item in enumerate(expanded["items"]):
            rule = item["field_contract"]
            string_keys = ("field_key", "source_selector", "generation_source_selector", "channel",
                           "generation_mode", "generation_instruction", "language_policy", "generation_output_semantics")
            if (any(not _text(rule[key]) for key in string_keys)
                    or rule["field_key"] not in batch["profile_field_keys"]
                    or rule["generation_mode"] != FIELD_OPERATION_MODES[operation]
                    or any(not _text(item[key]) for key in ("request_id", "unit_key"))
                    or any(not isinstance(item[key], str) for key in ("source_text", "source_language", "target_language"))
                    or not isinstance(item["context_facts"], dict)
                    or not isinstance(rule["generation_context_selectors"], list)
                    or any(not _text(key) for key in rule["generation_context_selectors"])
                    or len(set(rule["generation_context_selectors"])) != len(rule["generation_context_selectors"])
                    or set(item["context_facts"]) != set(rule["generation_context_selectors"])):
                return result
            identity = item["request_id"]
            field = (item["unit_key"], rule["field_key"])
            if identity in identities or field in fields:
                return result
            identities.add(identity)
            fields.add(field)
            references = item["reference_facts"]
            if (set(references) != {"protected_tokens", "logical_inputs", "subject_facts"}
                    or any(not isinstance(references[key], list) for key in references)
                    or any(not _text(token) for token in references["protected_tokens"])
                    or any(not isinstance(fact, dict) for key in ("logical_inputs", "subject_facts") for fact in references[key])):
                return result
            items.append({"request_id": identity, "unit_key": item["unit_key"],
                **{key: rule[key] for key in string_keys}, "item_path": f"items.{index}",
                "source_characters": len(item["source_text"]), "source_language": item["source_language"],
                "target_language": item["target_language"], "context_selectors": rule["generation_context_selectors"],
                "protected_count": len(references["protected_tokens"]),
                "logical_input_count": len(references["logical_inputs"]), "subject_fact_count": len(references["subject_facts"])})
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError, ValidationError):
        return result
    result.update(profile_id=profile["version_id"], profile_sha256=profile["contract_sha256"],
                  batch_sha256=digest, items=items)
    messages = transport.get("payload", {}).get("messages")
    candidates = [(i, m) for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"] if isinstance(messages, list) else []
    if len(candidates) != 1 or not isinstance(candidates[0][1].get("content"), str):
        return {**result, "state": "mismatch", "message": "原字段批次已记录，但目标user消息缺失、重复或非文本。"}
    index, message = candidates[0]
    matches = False
    try:
        def unique(pairs):
            value = {}
            for key, child in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = child
            return value
        wire = json.loads(message["content"], object_pairs_hook=unique)
        matches = canonical_json_bytes(wire) == canonical_json_bytes(batch)
    except (ValueError, RecursionError, ValidationError):
        pass
    result.update(state="matched" if matches else "mismatch", destination=f"payload.messages.{index}.content → JSON",
        message=("冻结字段批次与原user消息JSON逐项一致；此处仅核对user批次，system协议正文以下方原消息为准。"
                 if matches else "原user消息与冻结字段批次不一致；下列为批次记录，不宣称其已按原样发送，不改变任务结果。"))
    return result


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
