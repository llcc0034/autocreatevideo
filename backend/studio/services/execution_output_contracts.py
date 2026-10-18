"""One operation-to-output protocol registry for authoring and frozen reads."""
from django.core.exceptions import ValidationError

from studio.services.business_outputs import JSON_PARSER_KEY, PARSER_KEY, output_reference, verify_output_reference


STRUCTURED_TEXT_OPERATION = "text.structured"
STRUCTURED_TEXT_ADAPTER = "text.structured/v1"
_OUTPUT_PROTOCOLS = {
    "script.generate": ("script_semantics.v1",),
    "script.structure": ("script_semantics.v1",),
    "visual_rules.generate": (PARSER_KEY,),
    STRUCTURED_TEXT_OPERATION: (JSON_PARSER_KEY,),
}


def output_parser_keys(operation):
    return list(_OUTPUT_PROTOCOLS.get(operation, ()))


def verify_operation_output(operation, reference):
    output = verify_output_reference(reference)
    if output["parser"]["key"] not in output_parser_keys(operation):
        raise ValidationError({"output_version": "所选输出协议与执行能力不匹配"})
    return output


def execution_output_reference(version, *, required=False):
    if version.output_version is None:
        if required and output_parser_keys(version.operation):
            raise ValidationError({"output_version": "请选择此能力支持的确切已发布业务输出版本"})
        return None
    return verify_operation_output(version.operation, output_reference(version.output_version, require_published=True))
