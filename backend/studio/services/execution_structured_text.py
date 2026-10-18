"""Structured text protocol over the shared output snapshot and JSON parser."""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.data_structures import validate_data_structure_snapshot, validate_structure_value
from studio.services.execution_output_contracts import STRUCTURED_TEXT_OPERATION, verify_operation_output
from studio.services.json_contracts import canonical_json_bytes
from studio.services.system_message_layout import assemble_system_message


class StructuredTextValidationError(ValidationError):
    def __init__(self, errors):
        self.structure_errors = deepcopy(errors)
        super().__init__(errors, code="structured_text_result_invalid")


def verify_structured_text_result(result, reference):
    """Validate stored result against its request, without defaults or repair."""
    reference = verify_operation_output(STRUCTURED_TEXT_OPERATION, reference)
    keys = {"schema_version", "output_version_id", "output_contract_sha256", "structure_version_id", "value", "defaults"}
    if (not isinstance(result, dict) or set(result) != keys or type(result["schema_version"]) is not int
            or result["schema_version"] != 1 or result["output_version_id"] != reference["id"]
            or result["output_contract_sha256"] != reference["sha256"]
            or result["structure_version_id"] != reference["data_structure"]["id"]
            or not isinstance(result["defaults"], list)):
        raise ValidationError("通用文本产物与原请求的确切输出合同不一致")
    validate_structure_value(reference["data_structure"], result["value"], path="response")
    return result


def structured_text_warning_messages(warnings):
    if (not isinstance(warnings, list) or any(not isinstance(item, dict)
            or set(item) != {"path", "code", "message"}
            or any(not isinstance(item[key], str) for key in ("path", "code", "message")) for item in warnings)):
        raise ValidationError("通用结构化文本的解析提醒格式不一致")
    return [f'{item["path"]}：{item["message"]}' for item in warnings]


def structured_text_plan(reference):
    return {"operation": STRUCTURED_TEXT_OPERATION, "plan": {
        "schema_version": 1, "business_output": verify_operation_output(STRUCTURED_TEXT_OPERATION, reference)}}


def verify_structured_text_plan(envelope, *, expected_output=None):
    if (not isinstance(envelope, dict) or set(envelope) != {"operation", "plan"}
            or envelope["operation"] != STRUCTURED_TEXT_OPERATION or not isinstance(envelope["plan"], dict)):
        raise ValidationError("通用结构化文本缺少确切输出计划")
    plan = envelope["plan"]
    if set(plan) != {"schema_version", "business_output"} or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValidationError("通用结构化文本输出计划版本或字段不一致")
    reference = verify_operation_output(STRUCTURED_TEXT_OPERATION, plan["business_output"])
    if expected_output is not None and reference != expected_output:
        raise ValidationError("请求输出计划与冻结执行定义不一致，不能更换输出结构")
    return reference


def structured_text_system(reference, instruction, *, layout=None):
    reference = verify_operation_output(STRUCTURED_TEXT_OPERATION, reference)
    schema = validate_data_structure_snapshot(reference["data_structure"])
    return assemble_system_message([
        ("protocol", "Return one JSON value matching the following JSON Schema. Do not add markdown fences or surrounding commentary.\n"),
        ("output_structure", canonical_json_bytes(schema).decode("utf-8") + "\n"),
        ("prompt_channels", instruction),
    ], layout=layout)


def parse_structured_text(text, envelope, *, max_bytes):
    from studio.services.structured_output_parsing import parse_structured_output

    reference = verify_structured_text_plan(envelope)
    report = parse_structured_output(reference=reference, response_text=text, max_bytes=max_bytes)
    if not report["valid"]:
        raise StructuredTextValidationError(report["errors"])
    # Actual raw text remains with the original response owner, not duplicated
    # into another result store. Defaults retain the shared parser's provenance.
    return {"result": {"schema_version": 1, "output_version_id": reference["id"],
        "output_contract_sha256": reference["sha256"], "structure_version_id": report["structure_version_id"],
        "value": deepcopy(report["value"]), "defaults": report["defaults"]}, "warnings": report["warnings"]}
