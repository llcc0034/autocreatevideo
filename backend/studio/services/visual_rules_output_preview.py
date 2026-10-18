"""Read-only local sample validation, not Smoke, a candidate or an approval."""
from django.core.exceptions import ValidationError

from studio.services.json_contracts import parse_json_object_text
from studio.services.production_visual_rules import (
    _normalize_visual_rules_quality_advisories, _provider_response_max_bytes,
    parse_visual_rules_output,
)
from studio.services.visual_rules_output_contract import VISUAL_RULES_SCHEMA_VERSION, visual_rule_field_sources, validate_visual_rule_source_policy, visual_rule_response_keys


def preview_visual_rules_output(*, response_text, quality_advisories=None, allowed_values=None, field_sources=None, response_keys=None, output_version_id=None, expected_revision=None):
    # Bad configuration is an API error; an invalid sample is a failed validation
    # report. Neither path records a task or changes historical Smoke evidence.
    identity = {"output_version_id": None, "output_revision": None, "output_contract_sha256": None}
    structure = None
    if output_version_id is not None:
        from studio.models import BusinessOutputVersion
        from studio.services.business_outputs import verified_output_contract
        try:
            output = BusinessOutputVersion.objects.get(pk=output_version_id)
        except BusinessOutputVersion.DoesNotExist:
            raise ValidationError("指定输出版本不存在；不会改用当前目录") from None
        if (type(expected_revision) is not int or output.revision != expected_revision
                or quality_advisories is not None or allowed_values is not None or field_sources is not None or response_keys is not None):
            raise ValidationError("输出修订已变化或混入未保存建议，请重新读取", code="output_revision_conflict")
        contract = verified_output_contract(output).value
        if contract["parser"]["key"] != "visual_rules.v2":
            raise ValidationError("所选输出不是影视规则协议，请使用通用JSON预览")
        structure = contract["data_structure"]
        quality_advisories = output.quality_advisories or None
        allowed_values = output.parser_contract.get("allowed_values", {})
        field_sources = visual_rule_field_sources(output.parser_contract)
        response_keys = output.parser_contract.get("response_keys")
        identity = {"output_version_id": str(output.pk), "output_revision": output.revision,
            "output_contract_sha256": output.contract_sha256}
    quality = (_normalize_visual_rules_quality_advisories(quality_advisories)
        if quality_advisories is not None else None)
    from studio.services.visual_rules_output_contract import normalize_visual_rule_allowed_values
    allowed_values = normalize_visual_rule_allowed_values({} if allowed_values is None else allowed_values)
    field_sources = validate_visual_rule_source_policy(field_sources, allowed_values, quality)
    visual_rule_response_keys(field_sources, response_keys)
    report = {"preview_only": True, "response_schema_version": VISUAL_RULES_SCHEMA_VERSION,
        "valid": False, "candidate": None, "warnings": [], "errors": [], **identity}
    try:
        payload = parse_json_object_text(response_text, max_bytes=_provider_response_max_bytes())
        if structure is not None:
            from studio.services.data_structures import validate_structure_value
            validate_structure_value(structure, payload, path="response")
        parsed = parse_visual_rules_output(payload=payload, quality_advisories=quality, allowed_values=allowed_values, field_sources=field_sources, response_keys=response_keys)
    except ValidationError as exc:
        code = str(getattr(exc, "code", None) or "visual_rules_response_invalid")
        report["errors"] = ([{"code": code, "message": f"{path}: {message}"}
            for path, messages in exc.message_dict.items() for message in messages]
            if hasattr(exc, "error_dict") else [{"code": code, "message": message} for message in exc.messages])
        return report
    report.update(valid=True, candidate=parsed["candidate"], warnings=parsed["warnings"])
    return report
