"""One read-only correspondence projection from the original preparation.

No current library/Bible reads, recompilation, naming or version inference.
Callers have verified the frozen definition, inputs and request first.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_workflow_uses import execution_workflow_reference_slots
from studio.services.json_contracts import JsonContract
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.reference_context_facts import read_reference_context_facts


def frozen_reference_display(resolved, uses):
    facts = resolved.get("profile_sources", {})
    context = facts.get("reference_context") if isinstance(facts, dict) else None
    named = {}
    if context is not None:
        try:
            bindings = read_reference_context_facts(JsonContract(**context),
                max_bytes=execution_configuration_max_bytes()).reference_bindings
        except (TypeError, KeyError, PromptBuildError) as exc:
            raise ValidationError("原参考名称合同不可核对，不能重新计算替代") from exc
        slots = execution_workflow_reference_slots(uses, require_roles=True)
        for binding in bindings:
            if not isinstance(binding, dict):
                raise ValidationError("原参考名称记录不是对象")
            for row in [binding, *([binding["audio_use"]] if "audio_use" in binding else [])]:
                if not isinstance(row, dict):
                    raise ValidationError("原配对音轨名称记录不是对象")
                key = row.get("item_key")
                if not isinstance(key, str):
                    raise ValidationError("原参考名称缺少稳定使用标识")
                item, slot = uses.items.get(key), slots.get(key)
                if (key in named or item is None or slot is None
                        or any(row.get(field) != item[field] for field in ("asset_id", "artifact_id", "source_sha256"))
                        or row.get("shot_reference") != item.get("shot_reference")
                        or row.get("purpose") != (item["purpose"] or "")
                        or row.get("target_key") != uses.primary_targets.get(key)
                        or not any(assigned["item_key"] == key and assigned["target_port_ref"] == row.get("target_port_ref")
                            for assigned in uses.slots.get(row.get("target_key"), []))
                        or any(row.get(field) != slot[field] for field in ("input_slot", "input_label"))
                        or not isinstance(row.get("original_name"), str)
                        or not isinstance(row.get("entity_label"), str)
                        or type(row.get("name_disambiguated")) is not bool):
                    raise ValidationError("原参考名称、版本与本次素材槽位不一致，不能展示为已核对")
                named[key] = row
        if set(named) != set(uses.primary_targets):
            raise ValidationError("原参考名称未完整覆盖本次主输入，不能补算遗漏项")
    result = {}
    for key, item in uses.items.items():
        row = named.get(key)
        # No Profile binding is not evidence of a generated/disambiguated name.
        source = item["subject"] if item["subject"] is not None else item["asset_facts"]
        original = source.get("name", "") if source is not None else ""
        if not isinstance(original, str) or (row is not None and row["original_name"] != original):
            raise ValidationError("原参考名称与冻结素材事实不一致")
        result[key] = {"position": item["position"], "original_name": original,
            "reference_name": row["entity_label"] if row is not None else None,
            "name_disambiguated": row["name_disambiguated"] if row is not None else None,
            "input_slot": row["input_slot"] if row is not None else None,
            "shot_reference": deepcopy(item.get("shot_reference"))}
    return result
