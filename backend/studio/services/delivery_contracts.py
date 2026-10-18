"""Finite release metadata, projected by the shared M1 author and mapper."""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.assembly_contracts import _project_assembly_fields
from studio.services.data_structures import _value_errors


def delivery_metadata_schema():
    from studio.models import ReleaseBundle
    return {"type": "object", "additionalProperties": False, "required": ["title", "notes"],
        "properties": {"title": {"type": "string", "minLength": 1,
            "maxLength": ReleaseBundle._meta.get_field("title").max_length},
            "notes": {"type": "string"}}}


def validate_delivery_metadata(value):
    errors = _value_errors(delivery_metadata_schema(), value, path="delivery_metadata")
    if isinstance(value, dict):
        for key in ("title", "notes"):
            if isinstance(value.get(key), str) and "\x00" in value[key]:
                errors[f"delivery_metadata.{key}"] = ["交付文本不能包含NUL"]
    if errors:
        raise ValidationError(errors)
    return deepcopy(value)


def project_delivery_metadata_fields(**kwargs):
    return _project_assembly_fields(**kwargs, schema=delivery_metadata_schema(), validator=validate_delivery_metadata)


def verify_signed_delivery_metadata(plan, frozen, *, run_id, project_id):
    """Validate sealed author facts without consulting current stages or selections."""
    from studio.services.assembly_author_contracts import assembly_author_projection
    from studio.services.business_outputs import verify_output_reference
    from studio.services.production_recipe_contracts import recipe_stage_kind
    from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES

    stages = plan.value["recipe"]["definition"]["stages"] if plan.value["source"] == "recipe" else ()
    declaration = next((row for row in stages if row["key"] == "delivery"), {})
    rule = declaration.get("delivery_metadata")
    if rule is None:
        if frozen is not None:
            raise ValidationError("原冻结计划未声明交付字段来源")
        return None
    if not isinstance(frozen, dict) or not isinstance(frozen.get("projection"), dict):
        raise ValidationError("签名交付字段缺少完整的原始映射")
    selected = None
    if "source" in rule:
        source, proof = rule["source"], frozen.get("selected_source")
        prior = stages[:stages.index(declaration)]
        upstream = [row for row in prior if row["key"] == source["stage_key"]
            and recipe_stage_kind(row) == "structured_text" and row["mode"] == "ai"]
        if (len(upstream) != 1 or not isinstance(proof, dict)
                or source["output_key"] != "structured_text." + rule["output_version_id"]):
            raise ValidationError("签名交付字段缺少确切上游选择来源")
        reference = verify_output_reference(frozen.get("output_reference"))
        expected = {"kind": "selected_output", "production_run_id": str(run_id),
            "stage_key": source["stage_key"], "output_key": source["output_key"],
            "scope": {"kind": "project", "id": str(project_id)},
            "output_version_id": rule["output_version_id"],
            "output_contract_sha256": rule["expected_contract_sha256"],
            "structure_version_id": reference["data_structure"]["id"]}
        if any(proof.get(key) != value for key, value in expected.items()):
            raise ValidationError("签名交付来源与本轮冻结计划不一致")
        if "source_value" not in frozen["projection"]:
            raise ValidationError("签名交付字段缺少已选择原值")
        selected = (frozen["projection"]["source_value"], proof)
    return assembly_author_projection(rule, key="delivery_metadata", frozen=frozen,
        selected=selected, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
