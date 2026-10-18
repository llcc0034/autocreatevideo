"""Recipe stage placement rules around the shared M1 assembly author contract."""
from django.core.exceptions import ValidationError

from studio.services.assembly_author_contracts import (
    PRODUCTION_AUTHOR_KEYS, assembly_author_projection, validate_assembly_author_shape)
from studio.services.assembly_contracts import validate_assembly_configuration


def validate_recipe_assembly_output_shape(stage, *, key="assembly_output"):
    if key not in PRODUCTION_AUTHOR_KEYS:
        raise ValueError("Unknown assembly author contract")
    if key not in stage:
        return
    expected_stage = "delivery" if key == "delivery_metadata" else "assembly"
    if stage.get("key") != expected_stage or stage.get("mode") != "existing_pipeline":
        raise ValidationError({key: "字段映射必须声明在对应装配或交付阶段"})
    validate_assembly_author_shape(stage[key], key=key, allow_selected=True)
    if "source" in stage[key]:
        source = stage[key]["source"]
        if (not isinstance(source, dict) or set(source) != {
                "kind", "stage_key", "output_key", "scope", "selection_policy"}
                or source.get("kind") != "selected_output" or source.get("scope") != {"kind": "project"}
                or source.get("selection_policy") != "reviewed_selected"
                or any(type(source.get(field)) is not str or not source[field] for field in ("stage_key", "output_key"))):
            raise ValidationError({key: "装配上游必须明确引用本轮项目的已选择结构化输出，不接受候选或手填身份"})


def validate_recipe_assembly_author(stage, *, prior_stages, text_outputs, max_bytes, key):
    """Author validation has no future result; validate exact schema and mapping."""
    validate_recipe_assembly_output_shape(stage, key=key)
    rule = stage[key]
    if "source" not in rule:
        return recipe_assembly_output_projection(stage, max_bytes=max_bytes, require_published=True, key=key)
    from studio.models import BusinessOutputVersion
    from studio.services.business_outputs import output_reference
    from studio.services.data_structures import validate_data_structure_snapshot
    from studio.services.structure_mapping import validate_mapping
    from studio.services.assembly_contracts import assembly_output_schema, assembly_timing_schema, assembly_media_schema
    source = rule["source"]
    choices = [row for row in text_outputs if row["stage_key"] == source["stage_key"]
        and row["output_key"] == source["output_key"] and row.get("reader") == "structured_text"
        and row["scope_kind"] == "project" and row["kind"] == "json"]
    if source["stage_key"] not in prior_stages or len(choices) != 1:
        raise ValidationError({key: "只能引用本配方更早步骤的确切通用JSON输出，不读取自身、未来或最新候选"})
    option = choices[0]
    if (rule["output_version_id"] != option["output_version_id"]
            or rule["expected_contract_sha256"] != option["output_contract_sha256"]):
        raise ValidationError({key: "装配映射与上游的确切输出版本或摘要不一致"})
    output = BusinessOutputVersion.objects.select_related("data_structure_version").filter(pk=rule["output_version_id"]).first()
    if output is None:
        raise ValidationError({key: "所选输出版本不存在"})
    reference = output_reference(output, require_published=True)
    if (reference["sha256"] != rule["expected_contract_sha256"]
            or reference["data_structure"]["id"] != option["structure_version_id"]):
        raise ValidationError({key: "装配上游结构身份不一致"})
    schema = assembly_timing_schema() if key == "assembly_timing" else assembly_output_schema()
    if key == "assembly_media":
        schema = assembly_media_schema()
    if key == "delivery_metadata":
        from studio.services.delivery_contracts import delivery_metadata_schema
        schema = delivery_metadata_schema()
    validate_mapping(rule["mapping"], validate_data_structure_snapshot(reference["data_structure"]), schema,
        require_complete=True)


def recipe_assembly_output_projection(stage, *, max_bytes, require_published=False, frozen=None, key="assembly_output",
        stage_run=None):
    validate_recipe_assembly_output_shape(stage, key=key)
    selected = None
    if "source" in stage[key]:
        if stage_run is None:
            raise ValidationError({key: "装配上游结果必须由本轮已完成阶段提供"})
        from studio.services.production_recipe_preparations import recipe_stage_context, _project_inputs
        from studio.services.execution_selected_text import resolve_selected_execution_text
        plan, _ = recipe_stage_context(stage_run)
        bound, eligible = _project_inputs({key: stage[key]["source"]}, plan, stage_run)
        selected = resolve_selected_execution_text(bound[key], spec={"kind": "json"},
            owner=stage_run.production_run.owner, project_id=stage_run.production_run.project_id,
            production_run_id=str(stage_run.production_run_id), eligible_stage_ids=eligible)
        if (selected[1]["output_version_id"] != stage[key]["output_version_id"]
                or selected[1]["output_contract_sha256"] != stage[key]["expected_contract_sha256"]):
            raise ValidationError({key: "实际选择结果与装配作者输出合同不一致"})
    return assembly_author_projection(stage[key], max_bytes=max_bytes,
        require_published=require_published, frozen=frozen, key=key, selected=selected)


def apply_recipe_assembly_output(stage, configuration, *, max_bytes, frozen=None, stage_run=None):
    result = recipe_assembly_output_projection(stage, max_bytes=max_bytes, frozen=frozen, stage_run=stage_run)
    configured = validate_assembly_configuration({**configuration, **result["projection"]["output"]}, max_bytes=max_bytes)
    return configured, result
