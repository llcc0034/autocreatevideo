"""Original visual-rule approval as an exact, read-only downstream source."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import GeneratedArtifact, ProductionRun, SeriesProject, StageRun
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.production_visual_rules import current_rules_snapshot, validate_reviewed_rules, VISUAL_RULES_REVIEW_SCHEMA_VERSION
from studio.services.visual_rules_inputs import validate_visual_rules_baseline, validate_visual_rules_project
from studio.services.visual_rules_output_contract import VISUAL_RULE_REVIEW_FIELDS, visual_rule_field_sources


def _review_value(run, frozen):
    value = frozen.value
    if run.plan_source != "manual":
        # Recipe stages use their formal stage envelope; manual approvals store
        # the same business review directly. Neither is a legacy fallback.
        if (frozen.schema_version != 1 or not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("stage_key") != "visual_rules_review" or value.get("formal_object_type") != "visual_rules"
                or not isinstance(value.get("canonical"), dict)
                or json_contract(value["canonical"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != value.get("canonical_sha256")):
            raise ValidationError("影视规则配方审核缺少正式阶段快照")
        canonical = value["canonical"]
        value = value.get("review_payload")
        if (not isinstance(value, dict) or canonical.get("project_id") != str(run.project_id)
                or canonical.get("rules") != value.get("reviewed_rules")
                or canonical.get("rules_sha256") != value.get("reviewed_rules_sha256")):
            raise ValidationError("影视规则正式输出与审核内容不一致")
    elif not isinstance(value, dict) or frozen.schema_version != value.get("schema_version"):
        raise ValidationError("影视规则人工审核版本不一致")
    return value


def _verify_review_authority(run, stage, decision, frozen, value):
    if decision.actor == "human":
        if decision.decided_by_id and not decision.decision_policy and not decision.decision_policy_sha256:
            return
        raise ValidationError("影视规则人工审核缺少有效审核人或混入自动策略")
    if decision.actor != "recipe_policy" or decision.decided_by_id or run.plan_source != "recipe":
        raise ValidationError("影视规则采用来源未获得本轮授权")
    from studio.services.production_runs import StageBlocked, _verified_unattended_decision_policy
    try:
        policy, policy_sha256 = _verified_unattended_decision_policy(run, stage)
    except StageBlocked as exc:
        raise ValidationError("影视规则自动采用策略与本轮冻结授权不一致") from exc
    if decision.decision_policy != policy or decision.decision_policy_sha256 != policy_sha256:
        raise ValidationError("影视规则自动采用决定与冻结策略不一致")
    source = StageRun.objects.filter(pk=value.get("generation_stage_id"), production_run=run,
        stage_key="visual_rules_generate", status="succeeded").first()
    if source is None:
        raise ValidationError("影视规则自动采用缺少本轮成功生成阶段")
    original = verify_json_contract(source.output_snapshot, schema_version=source.output_snapshot_schema_version,
        size_bytes=source.output_snapshot_size_bytes, sha256=source.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    evidence = {"schema_version": 1, "kind": "unattended_visual_rules_policy",
        "source_snapshot_sha256": original.sha256, "artifact_id": value.get("artifact_id"),
        "artifact_sha256": value.get("artifact_sha256"), "result_sha256": value.get("result_sha256")}
    if (frozen.value.get("policy_evidence") != evidence
            or value.get("generation_snapshot_sha256") != original.sha256
            or value.get("public_generation_snapshot_sha256") != original.sha256
            or value.get("modified_fields") != [] or value.get("reviewed_rules") != value.get("candidate")):
        raise ValidationError("影视规则自动采用凭据与原生成候选不一致")


def _review(run, stage=None):
    if stage is None:
        try:
            stage = StageRun.objects.only("id", "production_run_id", "stage_key", "mode", "status", "output_snapshot",
                "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256",
                "decision_policy", "decision_policy_sha256").get(
                    production_run=run, stage_key="visual_rules_review")
        except StageRun.DoesNotExist as exc:
            raise ValidationError("本轮缺少正式影视规则审核步骤，不能补造") from exc
    expected_mode = "manual" if run.plan_source == "manual" else "review"
    if stage.production_run_id != run.pk or stage.stage_key != "visual_rules_review" or stage.mode != expected_mode:
        raise ValidationError("影视规则审核步骤与本轮不一致")
    decision = stage.approval_decisions.only("id", "stage_run_id", "revision", "actor", "decision",
        "decided_by_id", "output_snapshot_sha256", "decision_policy", "decision_policy_sha256").order_by("-revision").first()
    if decision is None:
        if stage.status != "pending":
            raise ValidationError("影视规则审核缺少正式决策，不能显示通过")
        return stage, None, None
    frozen = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    value = _review_value(run, frozen)
    if (stage.status != "succeeded" or not isinstance(value, dict) or value.get("schema_version") not in (2, 3)
            or value.get("kind") != "visual_rules_review_decision" or not isinstance(value.get("reviewed_rules"), dict)
            or json_contract(value["reviewed_rules"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != value.get("reviewed_rules_sha256")
            or decision.output_snapshot_sha256 != frozen.sha256 or decision.decision != "approve"):
        raise ValidationError("影视规则审核决策与采用内容不一致，不能修补为通过")
    _verify_review_authority(run, stage, decision, frozen, value)
    return stage, decision, frozen


def _state(run, decision, frozen):
    if decision is None:
        return {"revision": 0, "decision_id": None, "status": "not_reviewed", "reasons": []}
    value = _review_value(run, frozen)
    if value["schema_version"] != VISUAL_RULES_REVIEW_SCHEMA_VERSION:
        return {"revision": decision.revision, "decision_id": str(decision.pk), "status": "changed",
            "reasons": ["这是旧输入协议的历史审核记录，不作为当前输入依据。请配置新版实例并重新审核；原记录保留。"]}
    baseline = validate_visual_rules_baseline(value.get("input_baseline"))
    project = SeriesProject.objects.only("id", "production_settings", "continuity_summary").get(pk=run.project_id)
    validate_visual_rules_project(project, baseline)
    reasons = []
    if not VISUAL_RULE_REVIEW_FIELDS.issubset(value["reviewed_rules"]):
        raise ValidationError("审核记录缺少项目基础规则")
    project_rules = {key: value["reviewed_rules"][key] for key in VISUAL_RULE_REVIEW_FIELDS}
    project_rules_sha256 = json_contract(project_rules, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256
    if project_rules_sha256 != value.get("after_rules_sha256"):
        raise ValidationError("审核项目写回凭据与基础规则不一致")
    if project_rules_sha256 != current_rules_snapshot(project)["rules_sha256"]:
        reasons.append("项目中的影视规则已修改，现值不等于当时审核值。")
    return {"revision": decision.revision, "decision_id": str(decision.pk),
        "status": "changed" if reasons else "current", "reasons": reasons}


def visual_rules_review_state(run, stage=None):
    _, decision, frozen = _review(run, stage)
    return _state(run, decision, frozen)


def read_reviewed_visual_rules(*, run, eligible_stage_ids, decision_id=None):
    stage, decision, frozen = _review(run)
    if str(stage.pk) not in {str(value) for value in eligible_stage_ids}:
        raise ValidationError("影视规则必须来自本轮允许的前置审核阶段", code="reference_review_required")
    if _state(run, decision, frozen)["status"] != "current":
        raise ValidationError("影视规则尚未审核或已变化，请重新核对后引用", code="reference_review_changed")
    if decision_id is not None and decision_id != str(decision.pk):
        raise ValidationError("影视规则审核版本已变化，不会自动替换来源", code="reference_review_changed")
    value = _review_value(run, frozen)
    artifact = GeneratedArtifact.objects.filter(pk=value.get("artifact_id"), production_run=run,
        stage_run_id=value.get("generation_stage_id"), stage_run__stage_key="visual_rules_generate",
        status="selected", generation_attempt_id=value.get("attempt_id")).first()
    from studio.services.execution_production_candidates import read_production_text_candidate
    detail = read_production_text_candidate(artifact) if artifact is not None else None
    if (detail is None or detail["operation"] != "visual_rules.generate" or detail["adoption_block_reason"]
            or artifact.sha256 != value.get("artifact_sha256")
            or detail["preparation_id"] != value.get("preparation_id") or detail["result_sha256"] != value.get("result_sha256")):
        raise ValidationError("审核记录与原影视规则任务或候选不一致", code="reference_review_invalid")
    original = artifact.manifest_metadata["result"]["result"]
    from studio.services.business_outputs import verify_output_reference
    sources = visual_rule_field_sources(verify_output_reference(original["business_output"])["parser"])
    rules = validate_reviewed_rules(value["reviewed_rules"], field_sources=sources)
    if any(value.get(key) != original.get(key) for key in (
            "candidate", "candidate_sha256", "input_baseline", "business_input_sha256")):
        raise ValidationError("审核记录中的候选或输入凭据与原产物不一致", code="reference_review_invalid")
    return {"canonical": deepcopy(rules), "review_contract": detail["review_contract"],
        "review": {"stage_run_id": str(stage.pk), "decision_id": str(decision.pk),
        "revision": decision.revision, "snapshot_sha256": frozen.sha256},
        "output": {"protocol": "reviewed_visual_rules/v1", "schema_version": 3,
            **{key: value[key] for key in ("generation_stage_id", "artifact_id", "artifact_sha256", "attempt_id", "preparation_id", "result_sha256")},
            "reviewed_rules_sha256": value["reviewed_rules_sha256"]}}


@transaction.atomic
def reviewed_visual_source_options(*, run, stage_key, owner):
    """Preview only, under the same Run lock as approval/preparation writes."""
    from studio.services.production_execution_sources import execution_source_stage_ids
    from studio.services.production_run_plans import get_run_execution_plan
    run = ProductionRun.objects.select_for_update().filter(pk=run.pk, owner=owner, project__owner=owner).first()
    if run is None:
        raise ValidationError("运行或项目不属于当前用户，不能读取审核来源")
    stage = run.stages.filter(stage_key=stage_key).first()
    if stage is None:
        raise ValidationError("本轮没有此目标步骤")
    eligible = execution_source_stage_ids(stage)
    reviewed = read_reviewed_visual_rules(run=run, eligible_stage_ids=eligible)
    return {"run_id": str(run.pk), "project_id": str(run.project_id), "stage_key": stage_key,
        "run_plan_sha256": get_run_execution_plan(run).sha256,
        "review_contract": reviewed["review_contract"],
        "sources": [{"selector_key": row["source_selector"], "label": row["label"], "content": reviewed["canonical"][row["key"]],
            "content_sha256": json_contract(reviewed["canonical"][row["key"]], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256,
            "review": reviewed["review"], "output": {**reviewed["output"], "field": row["key"],
                "business_output": reviewed["review_contract"]["output"]}}
            for row in reviewed["review_contract"]["fields"]]}
