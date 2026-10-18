"""One output structure owner; model options describe only the wire format."""
from copy import deepcopy
from studio.services.model_profile_contracts import validate_chat_request_options


def resolve_chat_output_options(options, output_reference=None):
    result = project_chat_output_options(validate_chat_request_options(options), output_reference)
    return validate_chat_request_options(result, resolved=True)


def project_chat_output_options(options, output_reference=None):
    """Compile exact frozen output facts, without registry reads or calls.

    No output identity is inferred. Protocols without a BusinessOutputVersion
    can use text/json_object, not a second inline business schema.
    """
    from django.core.exceptions import ValidationError
    from studio.services.business_outputs import verify_output_reference
    from studio.services.data_structures import validate_data_structure_snapshot

    result = deepcopy(options)
    response_format = result.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return result
    if output_reference is None:
        raise ValidationError({"output_version": "json_schema 格式需要执行实例选择确切业务输出；此协议无业务输出时请使用其支持的其他响应格式"})
    output = verify_output_reference(output_reference)
    metadata = response_format.setdefault("json_schema", {})
    metadata.setdefault("name", "output_" + output["id"].replace("-", ""))
    metadata["schema"] = validate_data_structure_snapshot(output["data_structure"])
    return result
