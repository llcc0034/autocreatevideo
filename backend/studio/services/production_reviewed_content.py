"""Read original reviewed content, without approving or repairing it."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db.models import OuterRef, Subquery

from studio.models import ApprovalDecision, StageRun
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.security_boundaries import boundary_int


BIBLE_PRIMARY_FIELDS = {
    "identity_anchor": "主图共同身份",
    "appearance_anchor": "主图基础外观",
}


def read_reviewed_entity_primary(row, unit, *, allow_absent=False):
    """Read the one primary revision named by an already verified Bible review.

    The review holds references, not another copy of setting text. Absence is
    not permission to use the old entity visual field or a current revision.
    """
    scope = unit["scope"]
    key = (scope["kind"], scope["id"])
    return read_reviewed_entity_primaries({key: row}, project_id=unit["project_id"],
        allow_absent=allow_absent)[key]


def _reviewed_primary_reference(row, key, *, allow_absent):
    kind, identifier = key
    references = row.get("appearance_settings")
    if (kind not in {"character", "scene", "prop"} or row.get("id") != identifier
            or not isinstance(references, list) or any(not isinstance(ref, dict) for ref in references)):
        raise ValidationError("本轮已审核实体没有明确主图设定，请先建立并核对设定", code="reference_setting_required")
    primary = [ref for ref in references if ref.get("appearance_role") == "primary"]
    if allow_absent and not references:
        return None
    if len(primary) != 1 or not primary[0].get("setting_revision_id"):
        raise ValidationError("本轮已审核实体必须明确唯一主图设定，不能猜测其他造型", code="reference_setting_required")
    return primary[0]


def read_reviewed_entity_primaries(rows, *, project_id, allow_absent=False):
    """Batch exact primary bodies from verified reviews, never current pointers."""
    from studio.services.bible_setting_evidence import read_setting_revisions, setting_reference
    from studio.services.bible_variants import BibleVariantConflict

    references = {key: _reviewed_primary_reference(row, key, allow_absent=allow_absent)
        for key, row in rows.items()}
    result = {}
    try:
        settings = read_setting_revisions([ref["setting_revision_id"] for ref in references.values()
            if ref is not None], project_id=project_id)
        for (kind, identifier), reference in references.items():
            if reference is None:
                result[(kind, identifier)] = (None, None)
                continue
            setting = settings[reference["setting_revision_id"]]
            if (setting.variant.appearance_role != "primary"
                    or str(getattr(setting.variant, f"{kind}_id")) != identifier
                    or setting_reference(setting) != reference):
                raise BibleVariantConflict("主图设定与原审核引用不一致")
            result[(kind, identifier)] = ({field: getattr(setting, field) for field in BIBLE_PRIMARY_FIELDS},
                deepcopy(reference))
    except BibleVariantConflict as exc:
        raise ValidationError("本轮主图设定缺失、损坏或与审核引用不一致，不能用其他正文补齐",
            code="reference_setting_changed") from exc
    return result


def reviewed_entity_content(row, primary_fields):
    """Explicit whole-entity input; no duplicate old anchor or media bindings."""
    value = {name: deepcopy(content) for name, content in row.items()
        if name not in {"reference_bindings", "reference_asset_ids", "anchor_asset_id"}}
    value["primary_setting"] = deepcopy(primary_fields)
    return value


def manual_review_context(run, source):
    """Fingerprint original adopted script/reviews for an explicit human review.

    This is current review context, not proof of exact generation dependencies.
    Never resolve old preparation inputs (including same-stage selected_output).
    Callers hold the original Run/Project locks and keep generation facts frozen.
    """
    from studio.services.production_manual_scripts import manual_script_states

    script = None
    if run.script_adoption_decision_id:
        script = manual_script_states(run)["current_script"]
    elif run.script_revision_id or ApprovalDecision.objects.filter(stage_run__production_run=run,
            stage_run__stage_key__in=("script_input", "script_review")).exists():
        raise ValidationError("本轮剧本缺少当前采用决定，不能据此审核复用结果")
    latest = ApprovalDecision.objects.filter(stage_run_id=OuterRef("stage_run_id")).order_by("-revision")
    decisions = ApprovalDecision.objects.filter(stage_run__production_run=run,
        stage_run__ordinal__lt=source.ordinal, pk=Subquery(latest.values("pk")[:1])).order_by("stage_run__ordinal", "stage_run_id")
    refs = []
    for decision in decisions.select_related("stage_run").only("id", "stage_run_id", "revision", "actor", "decision",
            "decided_by_id", "output_snapshot_sha256", "stage_run__stage_key", "stage_run__status",
            "stage_run__output_snapshot", "stage_run__output_snapshot_schema_version",
            "stage_run__output_snapshot_size_bytes", "stage_run__output_snapshot_sha256"):
        stage = decision.stage_run
        saved = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
            size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
            max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        if (decision.actor != "human" or decision.decision != "approve" or not decision.decided_by_id
                or stage.status != "succeeded" or decision.output_snapshot_sha256 != saved.sha256):
            raise ValidationError("上游采用或审核决定与原快照不一致，请先核对原记录")
        refs.append({"stage_run_id": str(stage.pk), "decision_id": str(decision.pk),
            "revision": decision.revision, "snapshot_sha256": saved.sha256})
    return {"script_sha256": json_contract({"content": script,
            "decision_id": str(run.script_adoption_decision_id) if run.script_adoption_decision_id else None},
            max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256,
        "upstream_reviews_sha256": json_contract(refs, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256}


def _read_reviewed_content(*, run, eligible_stage_ids, decision_id, stage_key, formal_type, current, project):
    rows = list(StageRun.objects.filter(production_run_id=run.pk, pk__in=eligible_stage_ids,
        stage_key=stage_key, status="succeeded").only("id", "stage_key", "mode", "decision_policy", "decision_policy_sha256", "output_snapshot",
            "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256")[:2])
    if len(rows) != 1:
        raise ValidationError("当前内容尚未完成唯一前置审核", code="reference_review_required")
    review = rows[0]
    limit = boundary_int("security.snapshot.stage_max_bytes")
    saved = verify_json_contract(review.output_snapshot, schema_version=review.output_snapshot_schema_version,
        size_bytes=review.output_snapshot_size_bytes, sha256=review.output_snapshot_sha256, max_bytes=limit)
    decision = review.approval_decisions.only("id", "revision", "decision", "output_snapshot_sha256",
        "actor", "decided_by_id", "decision_policy", "decision_policy_sha256").order_by("-revision").first()
    value = saved.value
    if (decision is None or decision.decision != "approve" or decision.output_snapshot_sha256 != saved.sha256
            or (decision_id is not None and str(decision.pk) != decision_id)
            or not isinstance(value, dict) or value.get("stage_key") != review.stage_key
            or value.get("formal_object_type") != formal_type or not isinstance(value.get("canonical"), dict)
            or json_contract(value["canonical"], max_bytes=limit).sha256 != value.get("canonical_sha256")):
        raise ValidationError("审核决定与内容快照不一致", code="reference_review_invalid")
    if stage_key == "bible_review" and decision.actor == "recipe_policy":
        from studio.services.bible_content_review import recipe_bible_review_source
        from studio.services.production_runs import StageBlocked, _verified_unattended_decision_policy
        # Verify the original frozen authority, not merely a policy-looking label.
        try:
            policy, policy_sha = _verified_unattended_decision_policy(run, review)
        except StageBlocked as exc:
            raise ValidationError("Bible策略授权不再与原决定一致", code="reference_review_invalid") from exc
        _, source_sha = recipe_bible_review_source(run)
        evidence = value.get("policy_evidence")
        if (decision.decided_by_id is not None or decision.decision_policy != policy
                or decision.decision_policy_sha256 != policy_sha or not isinstance(evidence, dict)
                or evidence.get("schema_version") != 1 or evidence.get("kind") != "bible_content_policy"
                or evidence.get("source_snapshot_sha256") != source_sha):
            raise ValidationError("Bible策略决定与本轮授权或声明来源不一致", code="reference_review_invalid")
    approved = project(value["canonical"])
    if json_contract(project(current(run)), max_bytes=limit).sha256 != json_contract(approved, max_bytes=limit).sha256:
        raise ValidationError("已审核内容已变更，请重新审核后再映射", code="reference_review_changed")
    return {"canonical": deepcopy(approved), "review": {"stage_run_id": str(review.pk),
        "decision_id": str(decision.pk), "revision": decision.revision, "snapshot_sha256": saved.sha256}}


def read_reviewed_storyboard(*, run, eligible_stage_ids, decision_id=None):
    if run.plan_source == "manual":
        from studio.services.production_manual_storyboard_review import read_manual_reviewed_storyboard
        return read_manual_reviewed_storyboard(run=run, eligible_stage_ids=eligible_stage_ids, decision_id=decision_id)
    from studio.services.production_runs import _canonical_storyboard
    return _read_reviewed_content(run=run, eligible_stage_ids=eligible_stage_ids, decision_id=decision_id,
        stage_key="storyboard_review", formal_type="storyboard", current=_canonical_storyboard, project=lambda value: value)


def read_reviewed_bible(*, run, eligible_stage_ids, decision_id=None):
    if run.plan_source == "manual":
        from studio.services.production_manual_bible import read_manual_reviewed_bible
        return read_manual_reviewed_bible(run=run, eligible_stage_ids=eligible_stage_ids, decision_id=decision_id)
    from studio.services.production_runs import _canonical_bible, _bible_creative_drift_projection
    return _read_reviewed_content(run=run, eligible_stage_ids=eligible_stage_ids, decision_id=decision_id,
        stage_key="bible_review", formal_type="bible", current=_canonical_bible, project=_bible_creative_drift_projection)


def read_context_dependencies(option, *, run, eligible_stage_ids, cache, revision_ids=None):
    """Freeze/read only the original reviews declared by the source catalog."""
    readers = {"bible_approval": read_reviewed_bible, "storyboard_approval": read_reviewed_storyboard}
    result = {}
    for dependency in option.get("revision_dependencies", []):
        decision_id = revision_ids[dependency["key"]] if revision_ids is not None else None
        cache_key = (str(run.pk), dependency["revision_kind"], decision_id)
        if cache_key not in cache:
            cache[cache_key] = readers[dependency["revision_kind"]](run=run,
                eligible_stage_ids=eligible_stage_ids, decision_id=decision_id)
        result[dependency["key"]] = cache[cache_key]
    return result


def reviewed_entity_row(reviewed, unit):
    sections = {"character": "characters", "scene": "scenes", "prop": "props"}
    section = sections.get(unit["scope"]["kind"])
    if section is None:
        raise ValidationError("实体内容需要本次明确的人物、场景或道具目标")
    rows = [row for row in reviewed["canonical"][section] if row["id"] == unit["scope"]["id"]]
    if len(rows) != 1:
        raise ValidationError("审核结果未明确覆盖当前实体", code="reference_review_invalid")
    return deepcopy(rows[0])


def reviewed_shot_row(reviewed, unit):
    rows = [shot for episode in reviewed["canonical"]["episodes"] if episode["id"] == unit["episode_id"]
        for shot in episode["shots"] if shot["id"] == unit["scope"]["id"]]
    if len(rows) != 1:
        raise ValidationError("审核结果未明确覆盖当前镜头", code="reference_review_invalid")
    return deepcopy(rows[0])
