"""Explicit multi-image/one-anchor review over the original Bible writer."""
from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ProductionRun, SeriesProject, StageRun
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.production_bible_targets import bible_target_value, read_bible_target_contents
from studio.services.production_execution_units import ENTITY_SCOPE_MODELS, production_entity_queryset
from studio.services.production_manual_commands import record_manual_approval
from studio.services.production_manual_change_impact import require_replaceable_content_dependents
from studio.services.production_run_plans import get_run_execution_plan

_BASIS_LABELS = {"script_sha256": "本轮采用剧本", "bible_sha256": "人物、场景或道具设定",
    "visual_rules_sha256": "影视规则", "upstream_reviews_sha256": "上游采用或审核修订"}


def _content_basis(run, source):
    from studio.services.production_reviewed_content import manual_review_context
    from studio.services.production_runs import _canonical_bible, _bible_creative_drift_projection
    from studio.services.production_visual_rules import current_rules_snapshot

    # Image binding is the result of this action, not a change to its creative
    # review context. Full original creative Bible facts remain covered.
    return json_contract({**manual_review_context(run, source),
        "bible_sha256": json_contract(_bible_creative_drift_projection(_canonical_bible(run)),
            max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256,
        "visual_rules_sha256": current_rules_snapshot(run.project)["rules_sha256"]},
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def _stages(run, plan):
    if run.plan_source != "manual" or run.project.owner_id != run.owner_id:
        raise ValidationError("参考图人工审核只用于本项目的无配方制作")
    ordinals = {row["key"]: row["ordinal"] for row in plan.value["stage_catalog"]["value"]["stages"]}
    keys = {"bible_image_generation", "bible_image_review"}
    stages = {row.stage_key: row for row in StageRun.objects.filter(production_run=run, stage_key__in=keys).order_by()}
    if (set(stages) != keys or any(row.mode != "manual" or row.ordinal != ordinals.get(key)
            for key, row in stages.items())):
        raise ValidationError("本轮缺少与冻结目录一致的参考图生成/审核步骤")
    return stages["bible_image_generation"], stages["bible_image_review"]


def _targets(run, plan):
    from studio.services.production_runs import ProductionRunConflict, _assert_bible_asset_eligible_for_entity, _bible_binding_asset_fact
    rows, targets = [], {}
    for kind in ENTITY_SCOPE_MODELS:
        entities = production_entity_queryset(run, plan, kind).only(
            "id", "project_id", "name", "description", "anchor_asset_id", "image_binding_review_id",
        ).select_related("anchor_asset").prefetch_related("reference_assets").order_by("id")
        for entity in entities:
            targets[(kind, str(entity.pk))] = entity
    contents = read_bible_target_contents(targets, project_id=run.project_id)
    for key, entity in targets.items():
        references = list(entity.reference_assets.all())
        for asset in ([entity.anchor_asset] if entity.anchor_asset_id else []) + references:
            if entity.image_binding_review_id:
                try:
                    _bible_binding_asset_fact(asset, project=run.project, entity_type=key[0], entity=entity)
                except ProductionRunConflict as exc:
                    # Keep the review form available to replace stale links;
                    # the common writer partitions them, never adopts them.
                    if exc.code != "bible_image_source_stale":
                        raise
            else:
                _assert_bible_asset_eligible_for_entity(asset=asset, project=run.project, entity_type=key[0])
        rows.append(bible_target_value(entity, {"kind": key[0], "id": key[1]},
            [asset.pk for asset in references], content=contents[key]))
    return json_contract(rows, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)


def _approval(stage, candidates, targets, basis, plan):
    decision = stage.approval_decisions.order_by("-revision").first()
    if decision is None:
        if stage.status != "pending":
            _conflict("参考图审核状态缺少正式人工决策")
        return {"revision": 0, "decision_id": None, "snapshot_sha256": None, "status": "not_reviewed", "changes": []}
    frozen = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    value = frozen.value
    if (frozen.schema_version != 2 or not isinstance(value, dict) or value.get("schema_version") != 2
            or set(value) != {"schema_version", "kind", "candidates_sha256", "reviewed_targets_sha256",
                "result_targets", "result_targets_sha256", "bindings", "run_plan_sha256", "content_basis", "content_basis_sha256"}
            or value["kind"] != "manual_bible_image_review" or not isinstance(value["bindings"], list)
            or value["run_plan_sha256"] != plan.sha256 or not isinstance(value["result_targets"], list)
            or not isinstance(value["content_basis"], dict) or set(value["content_basis"]) != set(_BASIS_LABELS)
            or any(not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
                for v in value["content_basis"].values())
            or json_contract(value["content_basis"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != value["content_basis_sha256"]
            or json_contract(value["result_targets"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != value["result_targets_sha256"]
            or stage.status != "succeeded" or decision.actor != "human" or decision.decision != "approve"
            or not decision.decided_by_id or decision.output_snapshot_sha256 != frozen.sha256):
        _conflict("参考图审核记录与实际写回快照不一致，不能显示为已通过")
    changes = [label for key, label in _BASIS_LABELS.items() if value["content_basis"][key] != basis.value[key]]
    for key, current, label in (("candidates_sha256", candidates.sha256, "参考图候选"),
            ("result_targets_sha256", targets.sha256, "实体内容或图片关系")):
        if value[key] != current:
            changes.append(label)
    return {"revision": decision.revision, "decision_id": str(decision.pk), "snapshot_sha256": frozen.sha256,
        "status": "changed" if changes else "current", "changes": changes}


def _inventory(run, source, plan):
    from studio.services.production_runs import _canonical_bible_image_candidates

    # The source stage need not succeed: the user can select library images
    # without calling a model. Individual generated candidates remain verified.
    candidates = json_contract(_canonical_bible_image_candidates(run, source), max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    targets = _targets(run, plan)
    return candidates, targets


@transaction.atomic
def manual_bible_image_review_detail(run):
    from studio.services.production_runs import _bible_reference_asset_max_items

    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    plan = get_run_execution_plan(run)
    source, review = _stages(run, plan)
    candidates, targets = _inventory(run, source, plan)
    basis = _content_basis(run, source)
    return {"run_id": str(run.pk), "run_plan_sha256": plan.sha256, "candidates": candidates.value,
        "candidates_sha256": candidates.sha256, "targets": targets.value, "targets_sha256": targets.sha256,
        "content_basis": basis.value, "content_basis_sha256": basis.sha256,
        "maximum_selected_images": _bible_reference_asset_max_items(),
        "approval": _approval(review, candidates, targets, basis, plan)}


def approve_manual_bible_images(*, run, stage, plan, request, actor, asset_guard):
    from studio.services.production_runs import _apply_bible_image_bindings

    if request["scope"] != {"kind": "project", "id": str(run.project_id)} or request["unit_key"] != f"project:{run.project_id}":
        raise ValidationError("参考图审核必须明确核对本轮范围内完整实体集合")
    payload = request["payload"]
    if set(payload) != {"expected_candidates_sha256", "expected_targets_sha256", "expected_content_basis_sha256", "bindings", "comment"}:
        raise ValidationError("请提交候选、实体关系与当前内容依据摘要，逐项图片与唯一主图选择和说明")
    if not isinstance(payload["bindings"], list) or not payload["bindings"]:
        raise ValidationError("尚未选择任何实体参考图，不能批准空集合")
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    source, review = _stages(run, plan)
    if stage.pk != review.pk:
        raise ValidationError("参考图审核必须在原参考图审核步骤执行")
    candidates, targets = _inventory(run, source, plan)
    basis = _content_basis(run, source)
    state = _approval(review, candidates, targets, basis, plan)
    if (request["expected_selection_revision"] != state["revision"]
            or payload["expected_candidates_sha256"] != candidates.sha256
            or payload["expected_targets_sha256"] != targets.sha256
            or payload["expected_content_basis_sha256"] != basis.sha256):
        _conflict("上游内容依据、候选、实体内容/图片关系或审核修订已变化，请重新读取并确认，未覆盖")
    comment = normalize_optional_audit_comment(payload["comment"])
    require_replaceable_content_dependents(run=run, stage=stage)
    rows = _apply_bible_image_bindings(run, stage, actor, bindings=payload["bindings"],
        canonical=candidates.value, asset_guard=asset_guard)
    result = _targets(run, plan)
    receipt = record_manual_approval(run, stage, value={"schema_version": 2, "kind": "manual_bible_image_review",
        "run_plan_sha256": plan.sha256, "content_basis": basis.value, "content_basis_sha256": basis.sha256,
        "candidates_sha256": candidates.sha256, "reviewed_targets_sha256": targets.sha256,
        "result_targets": result.value, "result_targets_sha256": result.sha256, "bindings": rows},
        revision=state["revision"], comment=comment, actor=actor)
    from studio.services.bible_material_bindings import record_binding_review
    record_binding_review(stage, rows)
    return receipt
