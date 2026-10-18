"""Configured JSON results over exact shared structures; no business writes."""
from django.core.exceptions import ValidationError

from studio.services.data_structures import resolve_structure_value, validate_data_structure_snapshot
from studio.services.json_contracts import JsonExpansionBudget, parse_json_value_text
from studio.services.structure_mapping import structure_path_options, unmapped_fields


def parse_structure_text(*, structure, response_text, max_bytes, path="response", budget=None):
    """One strict JSON/structure/default pass for generic and business outputs.

    A business mapper can share the expansion budget with this pass. No text
    heuristics, AI fallback, normalization or business interpretation occurs here.
    """
    schema = validate_data_structure_snapshot(structure)
    result = {"structure_version_id": structure["id"], "json_schema": schema,
        "fields": structure_path_options(structure), "parsed": False, "valid": False,
        "raw_value": None, "value": None, "defaults": [], "warnings": [], "errors": {}}
    if budget is None:
        budget = JsonExpansionBudget(max_bytes, path=f"{path}.parse_budget")
    try:
        raw = parse_json_value_text(response_text, max_bytes=max_bytes)
        result.update(parsed=True, raw_value=raw, value=raw)
        resolved = resolve_structure_value(structure, raw, path=path, consume=budget.consume)
        result.update(resolved)
        result["warnings"] = unmapped_fields(schema, raw, path=path, consume=budget.consume)
    except ValidationError as exc:
        result.update(valid=False, errors=getattr(exc, "message_dict", None) or {path: exc.messages})
    except RecursionError:
        result.update(valid=False, errors={path: ["JSON嵌套过深，无法完成结构解析；请检查原文"]})
    return result


def parse_structured_output(*, reference, response_text, max_bytes):
    """Configured generic result; validity never implies selection or writes."""
    from studio.services.business_outputs import JSON_PARSER_KEY, verify_output_reference

    reference = verify_output_reference(reference)
    if reference["parser"]["key"] != JSON_PARSER_KEY:
        raise ValidationError("请选择通用JSON输出协议；业务专用转换使用其原解析入口")
    return {"output_version_id": reference["id"], "output_contract_sha256": reference["sha256"],
        **parse_structure_text(structure=reference["data_structure"], response_text=response_text, max_bytes=max_bytes)}
