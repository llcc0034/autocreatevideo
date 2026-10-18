"""Frozen adoption metadata; never an implicit source of model prompt content."""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.models import SeriesProject
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import json_contract


def input_digest(value):
    # Only the digest is stored in the baseline. Its source may be the full
    # file-backed request assembly; actual JSON columns retain their own caps.
    return json_contract(value, max_bytes=execution_request_max_bytes()).sha256


def capture_visual_rules_baseline(*, project_id, owner, inputs):
    from studio.services.production_visual_rules import current_rules_snapshot
    project = None
    if project_id is not None:
        project = SeriesProject.objects.filter(pk=project_id, owner=owner).first()
        if project is None:
            raise ValidationError("影视规则输入项目不存在或无权读取")
    references = []
    for row in inputs["input_provenance"]:
        reference = row.get("reference")
        if isinstance(reference, dict) and reference.get("selector") in {"project.logline", "project.visual_style"}:
            if project is None or reference.get("project_id") != str(project.pk):
                raise ValidationError("影视规则引用与本次项目不一致")
            references.append({"input_key": row["input_key"], **deepcopy(reference)})
    return {"schema_version": 1, "project_id": str(project.pk) if project else None,
        "base_rules_sha256": current_rules_snapshot(project)["rules_sha256"] if project else None,
        "input_provenance_sha256": input_digest(inputs["input_provenance"]),
        "logical_inputs_sha256": input_digest(inputs["logical_values"]), "project_references": references}


def validate_visual_rules_baseline(value):
    from uuid import UUID
    if not isinstance(value, dict) or set(value) != {"schema_version", "project_id", "base_rules_sha256",
            "input_provenance_sha256", "logical_inputs_sha256", "project_references"} or value["schema_version"] != 1:
        raise ValidationError("影视规则缺少本次输入与采用基线，不能从当前数据补造")
    def digest(raw):
        return isinstance(raw, str) and len(raw) == 64 and all(c in "0123456789abcdef" for c in raw)
    if not all(digest(value[key]) for key in ("input_provenance_sha256", "logical_inputs_sha256")):
        raise ValidationError("影视规则输入摘要无效")
    if value["project_id"] is not None:
        try:
            UUID(value["project_id"])
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError("影视规则项目标识无效") from exc
        if not digest(value["base_rules_sha256"]):
            raise ValidationError("影视规则采用基线摘要无效")
    elif value["base_rules_sha256"] is not None or value["project_references"]:
        raise ValidationError("独立烟测不能冒充项目采用基线")
    if not isinstance(value["project_references"], list):
        raise ValidationError("影视规则项目输入引用无效")
    for row in value["project_references"]:
        if (not isinstance(row, dict) or row.get("selector") not in {"project.logline", "project.visual_style"}
                or row.get("project_id") != value["project_id"] or not isinstance(row.get("content"), str)
                or input_digest(row["content"]) != row.get("content_sha256")):
            raise ValidationError("影视规则项目输入引用摘要无效")
    json_contract(value, max_bytes=execution_configuration_max_bytes())
    return deepcopy(value)


def validate_visual_rules_project(project, baseline):
    """A preview is an editable copy, not a binding to mutable latest content."""
    baseline = validate_visual_rules_baseline(baseline)
    if baseline["project_id"] != str(project.pk):
        raise ValidationError("影视规则候选不属于本项目")
    return baseline


def verify_visual_rules_candidate_source(preparation, candidate):
    from studio.services.execution_preparations import read_frozen_execution_request
    from studio.services.execution_native_text import native_text_source_reference
    detail, definition, transport = read_frozen_execution_request(preparation)
    baseline = native_text_source_reference(definition.value, detail.resolved_inputs["inputs"])
    plan = transport.value.get("operation_plan", {}).get("plan", {})
    if (plan.get("input_baseline") != baseline or candidate.get("input_baseline") != baseline
            or plan.get("business_input_sha256") != input_digest(plan.get("prompt_profile_assembly"))
            or candidate.get("business_input_sha256") != plan["business_input_sha256"]):
        raise ValidationError("影视规则候选与原准备的输入或实际提示词不一致")
    # A self-consistent result hash does not prove which output version produced
    # it. Bind both the contract and advisory policy to the original request.
    if (candidate.get("business_output") != plan.get("business_output")
            or candidate.get("quality_advisories", {}) != plan.get("resource_estimate", {}).get("quality_advisories", {})):
        raise ValidationError("影视规则候选的输出版本或质量建议与原准备不一致")
    from studio.services.production_visual_rules import validate_reviewed_rules
    sources = None
    if "business_output" in candidate:
        from studio.services.business_outputs import verify_output_reference
        from studio.services.visual_rules_output_contract import visual_rule_field_sources
        sources = visual_rule_field_sources(verify_output_reference(candidate["business_output"])["parser"])
    rules = validate_reviewed_rules(candidate.get("candidate"), field_sources=sources)
    if rules != candidate["candidate"] or input_digest(rules) != candidate.get("candidate_sha256"):
        raise ValidationError("影视规则候选字段或正文摘要与冻结结果不一致")
    if "business_output" in candidate:
        from studio.services.business_outputs import visual_rule_review_contract
        return visual_rule_review_contract(candidate["business_output"])
    return None
