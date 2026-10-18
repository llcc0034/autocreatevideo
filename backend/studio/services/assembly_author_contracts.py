"""Shared authored geometry, timing and adopted-clip identities via M1 contracts.

This does not adopt media, map codec settings, or create a second execution path.
The author supplies JSON in its own field layout; a frozen stage retains the
exact output/structure contract, raw values, defaults and mapping provenance.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.models import BusinessOutputVersion
from studio.services.assembly_contracts import (
    project_assembly_output_fields, project_assembly_timing_fields, project_assembly_media_fields)
from studio.services.business_outputs import JSON_PARSER_KEY, output_reference, verify_output_reference
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract


ASSEMBLY_AUTHOR_KEYS = ("assembly_output", "assembly_timing", "assembly_media")
PRODUCTION_AUTHOR_KEYS = (*ASSEMBLY_AUTHOR_KEYS, "delivery_metadata")


def validate_assembly_author_shape(value, *, key="assembly_output", allow_selected=False):
    if key not in PRODUCTION_AUTHOR_KEYS:
        raise ValueError("Unknown assembly author contract")
    path = key
    fields = {"output_version_id", "expected_contract_sha256", "mapping"}
    allowed = [fields | {"source_text"}]
    if allow_selected:
        allowed.append(fields | {"source"})
    if not isinstance(value, dict) or set(value) not in allowed:
        raise ValidationError({path: "需要完整的确切输出版本、原文或上游来源及M1映射"})
    json_contract(value, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    try:
        if type(value["output_version_id"]) is not str or str(UUID(value["output_version_id"])) != value["output_version_id"]:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise ValidationError({path: "必须提供确切输出版本UUID"}) from None
    digest = value["expected_contract_sha256"]
    if type(digest) is not str or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValidationError({path: "必须提供确切输出合同SHA256"})
    mapping = value["mapping"]
    if (("source_text" in value and type(value["source_text"]) is not str) or not isinstance(mapping, dict)
            or set(mapping) != {"schema_version", "node"} or type(mapping["schema_version"]) is not int
            or mapping["schema_version"] != 1 or not isinstance(mapping["node"], dict)):
        raise ValidationError({path: "原文必须是文本，映射使用M1 schema_version=1；不猜测字段"})


def assembly_author_projection(rule, *, max_bytes, require_published=False, frozen=None, key="assembly_output",
        selected=None):
    """Resolve once, or revalidate the complete saved contract without registry reads."""
    validate_assembly_author_shape(rule, key=key, allow_selected=selected is not None)
    if frozen is None:
        output = BusinessOutputVersion.objects.select_related("data_structure_version").filter(
            pk=rule["output_version_id"]).first()
        if output is None:
            raise ValidationError({key: "所选输出版本不存在，不使用最新版本代替"})
        reference = output_reference(output, require_published=require_published)
    else:
        expected = {"output_reference", "projection"} | ({"selected_source"} if selected is not None else set())
        if not isinstance(frozen, dict) or set(frozen) != expected:
            raise ValidationError({key: "冻结装配映射不完整，不能补当前合同"})
        reference = verify_output_reference(frozen["output_reference"])
    if (reference["id"] != rule["output_version_id"] or reference["sha256"] != rule["expected_contract_sha256"]
            or reference["parser"]["key"] != JSON_PARSER_KEY):
        raise ValidationError({key: "输出版本、摘要或通用JSON协议不一致，请重新选择"})
    project = project_assembly_timing_fields if key == "assembly_timing" else project_assembly_output_fields
    if key == "assembly_media":
        project = project_assembly_media_fields
    if key == "delivery_metadata":
        from studio.services.delivery_contracts import project_delivery_metadata_fields
        project = project_delivery_metadata_fields
    inputs = ({"selected_value": selected[0], "from_selected": True} if selected is not None
        else {"source_text": rule["source_text"]})
    projection = project(source_structure=reference["data_structure"],
        **inputs, mapping=rule["mapping"], max_bytes=max_bytes)
    if not projection["valid"]:
        raise ValidationError({key + "." + path: messages for path, messages in projection["errors"].items()})
    result = {"output_reference": reference, "projection": projection}
    if selected is not None:
        result["selected_source"] = deepcopy(selected[1])
    json_contract(result, max_bytes=max_bytes)
    if frozen is not None and result != frozen:
        raise ValidationError({key: "冻结装配原值、默认来源或映射结果不一致，不静默重算修复"})
    return deepcopy(result)
