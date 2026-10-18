"""Explicit approval of the current media selections, using original decisions.

The inventory is metadata-only. Approval verifies the actual media; neither
operation runs a model, completes a producer, or starts assembly.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import OuterRef, Subquery, UUIDField
from django.utils import timezone

from studio.models import ApprovalDecision, ArtifactSelectionDecision, GeneratedArtifact, ProductionRun, SeriesProject, StageRun
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.execution_production_candidates import read_production_media_candidate
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.generated_artifacts import _artifact_limit
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract, canonical_json_bytes, json_contract, verify_json_contract
from studio.services.production_manual_commands import ManualActionResult
from studio.services.production_run_plans import get_run_execution_plan


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def _stages(run):
    if run.plan_source != "manual":
        raise ValidationError("媒体选片审核仅用于无配方人工制作")
    stages = {row.stage_key: row for row in StageRun.objects.filter(production_run=run,
        stage_key__in=("media_generation", "artifact_selection")).only("id", "stage_key", "ordinal", "mode", "status",
            "output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256")}
    if set(stages) != {"media_generation", "artifact_selection"} or any(row.mode != "manual" for row in stages.values()):
        raise ValidationError("本轮缺少正式人工媒体制作或选片审核步骤")
    return stages["media_generation"], stages["artifact_selection"]


def _selection(run, source, plan, *, max_manifest_bytes):
    latest = ArtifactSelectionDecision.objects.filter(stage_run=source,
        variant_group=OuterRef("variant_group")).order_by("-revision")
    rows = GeneratedArtifact.objects.filter(production_run=run, stage_run=source, status="selected").annotate(
        decision_id=Subquery(latest.values("id")[:1]), decision_revision=Subquery(latest.values("revision")[:1]),
        decision_artifact_id=Subquery(latest.values("artifact_id")[:1], output_field=UUIDField())).only(
            "id", "kind", "sha256", "variant_group", "manifest_metadata", "manifest_schema_version",
            "manifest_metadata_size_bytes", "manifest_metadata_sha256").order_by("variant_group")
    members, groups, size = [], set(), 0
    for artifact in rows.iterator(chunk_size=100):
        if (artifact.kind not in {"image", "video", "audio"} or not artifact.variant_group
                or artifact.variant_group in groups or artifact.decision_artifact_id != artifact.pk):
            raise ValidationError("媒体选片与正式采用记录不一致，不能补造或推断当前选择")
        metadata = verify_json_contract(artifact.manifest_metadata, schema_version=artifact.manifest_schema_version,
            size_bytes=artifact.manifest_metadata_size_bytes, sha256=artifact.manifest_metadata_sha256,
            max_bytes=max_manifest_bytes).value
        unit = metadata.get("production_unit")
        if (not isinstance(unit, dict) or unit.get("run_plan_sha256") != plan.sha256
                or not isinstance(unit.get("scope"), dict) or not isinstance(metadata.get("unit_key"), str)):
            raise ValidationError("媒体候选缺少本轮明确的制作对象，不能从名称推断")
        member = {"artifact_id": str(artifact.pk), "kind": artifact.kind, "artifact_sha256": artifact.sha256,
            "manifest_sha256": artifact.manifest_metadata_sha256, "decision_id": str(artifact.decision_id),
            "decision_revision": artifact.decision_revision, "variant_group": artifact.variant_group,
            "scope": unit["scope"], "unit_key": metadata["unit_key"]}
        size += len(canonical_json_bytes(member))
        if size > DATABASE_JSON_FIELD_MAX_BYTES:
            raise ValidationError("本轮选片超过单次审核合同大小，请按实际交付范围拆分制作轮次")
        members.append(member)
        groups.add(artifact.variant_group)
    return json_contract({"schema_version": 1, "run_plan_sha256": plan.sha256,
        "source_stage_id": str(source.pk), "items": members}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)


def _review_selection(run, source, plan):
    """One reviewed timeline for selection and assembly; no media/provider I/O.

    This is a human delivery-fit review, not a rewrite of generation inputs.
    Callers hold Run -> Project locks, matching the original content writers.
    """
    from studio.services.production_runs import _selected_episodes, StageBlocked
    from studio.services.production_storyboard_content import storyboard_content

    selected = _selection(run, source, plan, max_manifest_bytes=execution_configuration_max_bytes())
    try:
        episodes = _selected_episodes(run)
    except StageBlocked as exc:
        raise ValidationError(exc.reason) from exc
    content = storyboard_content(project_id=run.project_id, episodes=episodes, lock_shots=True)
    return json_contract({**selected.value, "schema_version": 4,
        "content_basis": _content_basis(run, source, content), "delivery_timeline": [
        {"episode_id": episode["id"], "number": episode["number"], "title": episode["title"], "shots": [
            {"shot_id": shot["id"], "number": shot["number"], "title": shot["title"],
                "duration_ms": shot["duration_ms"], "media_kind": shot["media_type"],
                "episode_start_ms": shot["episode_start_ms"], "episode_end_ms": shot["episode_end_ms"]}
            for shot in episode["shots"]]}
        for episode in content["episodes"]]}, schema_version=4, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)


def _content_basis(run, source, storyboard):
    """Current human reuse basis, NOT re-resolution of a candidate's inputs.

    No selected_output reader is invoked here: a candidate may have originally
    used the previous selection in this same stage. Its generation provenance
    stays frozen; this review separately confirms fit to current authored facts.
    """
    from studio.services.production_reviewed_content import manual_review_context
    from studio.services.production_runs import _canonical_bible
    from studio.services.production_visual_rules import current_rules_snapshot

    # Bounded content contracts use the original fact projections. Never load
    # provider responses, preparation bodies, media files or external services.
    def digest(value):
        return json_contract(value, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256

    return {**manual_review_context(run, source),
        "storyboard_sha256": digest(storyboard),
        "bible_sha256": digest(_canonical_bible(run)),
        "visual_rules_sha256": current_rules_snapshot(run.project)["rules_sha256"]}


def _approval(stage, selection):
    decision = stage.approval_decisions.order_by("-revision").first()
    if decision is None:
        if stage.status not in {"pending", "running"}:
            raise ValidationError("选片审核状态缺少正式审核记录")
        return {"revision": 0, "decision_id": None, "status": "not_reviewed", "selection_sha256": None, "changes": []}
    snapshot = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    value = snapshot.value
    if (snapshot.schema_version != 4 or not isinstance(value, dict) or set(value) != {"schema_version", "kind", "selection", "selection_sha256"}
            or value["schema_version"] != 4 or value["kind"] != "manual_media_review" or stage.status != "succeeded"
            or not isinstance(value["selection"], dict) or value["selection"].get("schema_version") != 4
            or set(value["selection"]) != {"schema_version", "run_plan_sha256", "source_stage_id", "items", "delivery_timeline", "content_basis"}
            or value["selection"]["run_plan_sha256"] != selection.value["run_plan_sha256"]
            or value["selection"]["source_stage_id"] != selection.value["source_stage_id"]
            or not isinstance(value["selection"]["items"], list)
            or not isinstance(value["selection"].get("content_basis"), dict)
            or not isinstance(value["selection"].get("delivery_timeline"), list)
            or decision.actor != "human" or decision.decision != "approve" or not decision.decided_by_id
            or decision.output_snapshot_sha256 != snapshot.sha256
            or json_contract(value["selection"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).sha256 != value["selection_sha256"]):
        raise ValidationError("选片审核记录与冻结清单不一致，不能修补为通过")
    basis = value["selection"]["content_basis"]
    labels = {"script_sha256": "本轮采用剧本", "storyboard_sha256": "分镜内容与参考素材",
        "bible_sha256": "人物、场景或道具设定", "visual_rules_sha256": "影视规则", "upstream_reviews_sha256": "上游采用或审核修订"}
    if set(basis) != set(labels) or any(not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v) for v in basis.values()):
        raise ValidationError("选片审核缺少完整内容依据，不能推断旧结果适用于当前内容")
    changes = [label for key, label in labels.items() if basis[key] != selection.value["content_basis"][key]]
    for key, label in (("items", "采用媒体"), ("delivery_timeline", "交付分镜")):
        if value["selection"][key] != selection.value[key]:
            changes.append(label)
    return {"revision": decision.revision, "decision_id": str(decision.pk), "changes": changes,
        "status": "current" if value["selection_sha256"] == selection.sha256 else "changed",
        "selection_sha256": value["selection_sha256"]}


@transaction.atomic
def manual_media_review_detail(run):
    # Same Run lock as every formal selection writer; no file/provider reads.
    run = ProductionRun.objects.select_for_update().only("id", "plan_source", "project_id", "recipe_version_id", "owner_id",
        "script_revision_id", "script_adoption_decision_id").get(pk=run.pk)
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    source, review = _stages(run)
    plan = get_run_execution_plan(run)
    selection = _review_selection(run, source, plan)
    return {"run_id": str(run.pk), "run_plan_sha256": plan.sha256,
        "selection": selection.value, "selection_sha256": selection.sha256,
        "approval": _approval(review, selection)}


def approve_manual_media(*, run, stage, plan, request, actor):
    if request["scope"] != {"kind": "project", "id": str(run.project_id)} or request["unit_key"] != f"project:{run.project_id}":
        raise ValidationError("选片审核确认本轮完整集合，请明确定位本轮项目")
    payload = request["payload"]
    if set(payload) != {"selection", "expected_selection_sha256", "comment"}:
        raise ValidationError("请提交已查看的完整选片清单、摘要和审核说明")
    source, review = _stages(run)
    if stage.pk != review.pk:
        raise ValidationError("选片审核必须在原选片审核步骤执行")
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    current = _review_selection(run, source, plan)
    if current.value != payload["selection"] or current.sha256 != payload["expected_selection_sha256"]:
        _conflict("本轮选片、上游内容或审核依据已变化，请重新读取并逐项核对；未批准旧集合")
    if not current.value["items"]:
        raise ValidationError("尚未采用任何媒体，不能批准空选片集合")
    state = _approval(stage, current)
    if request["expected_selection_revision"] != state["revision"]:
        _conflict("选片审核修订已变化，请重新读取后确认")
    comment = normalize_optional_audit_comment(payload["comment"])
    # Probe only this explicit commit, never every row on a list refresh.
    max_media_bytes = _artifact_limit(run)
    for member in current.value["items"]:
        artifact = GeneratedArtifact.objects.get(pk=member["artifact_id"], production_run=run, stage_run=source)
        read_production_media_candidate(artifact, max_media_bytes=max_media_bytes)
    if state["status"] == "current":
        return ManualActionResult("approval_decision", ApprovalDecision.objects.get(pk=state["decision_id"]).pk)
    assign_json_contract(stage, "output_snapshot", {"schema_version": 4, "kind": "manual_media_review",
        "selection": current.value, "selection_sha256": current.sha256}, schema_version=4, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    stage.status, stage.reason_code, stage.reason = "succeeded", "", ""
    stage.started_at = stage.started_at or timezone.now()
    stage.finished_at = timezone.now()
    stage.save(update_fields=("output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
        "output_snapshot_sha256", "status", "reason_code", "reason", "started_at", "finished_at", "updated_at"))
    decision = ApprovalDecision.objects.create(stage_run=stage, revision=state["revision"] + 1, decision="approve",
        actor="human", decided_by=actor, comment=comment, output_snapshot_sha256=stage.output_snapshot_sha256)
    stage.output_reference = f"approval:{decision.pk}"
    stage.save(update_fields=("output_reference", "updated_at"))
    return ManualActionResult("approval_decision", decision.pk)
