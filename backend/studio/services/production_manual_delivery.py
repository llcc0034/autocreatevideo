"""Explicit human confirmation of existing episode masters; never a queue."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from studio.models import GeneratedArtifact, GenerationAttempt, GenerationTask, ProductionAssemblyLink, ProductionRun, ReleaseBundle
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract
from studio.services.production_manual_assembly import manual_assembly_detail
from studio.services.production_manual_commands import ManualActionResult, record_manual_approval


@transaction.atomic
def manual_delivery_detail(run):
    run = ProductionRun.objects.select_for_update().only("id", "project_id", "plan_source", "recipe_version_id",
        "status").get(pk=run.pk)
    assembly = manual_assembly_detail(run)
    blockers = list(assembly["blockers"])
    if not assembly["frozen"]:
        blockers.append("尚未提交本轮装配，不能封存成片。")
    jobs = {row["job"]["id"] for row in assembly["episodes"] if row["job"]}
    masters = list(GeneratedArtifact.objects.filter(production_run=run, stage_run__stage_key="assembly", kind="assembly", role="episode_master",
        manifest_key__in=[f"assembly:{key}" for key in jobs]).order_by("id").only(
            "id", "manifest_key", "status", "sha256", "manifest_metadata_sha256", "size_bytes", "mime_type"))
    by_job = {row.manifest_key: row for row in masters}
    episodes = []
    for row in assembly["episodes"]:
        job = row["job"]
        master = by_job.get(f"assembly:{job['id']}") if job else None
        if job is None or job["status"] != "succeeded" or master is None or master.status != "selected":
            blockers.append(f"第 {row['number']} 集装配尚未成功并归档母版。")
        episodes.append({"episode_id": row["episode_id"], "number": row["number"], "title": row["title"],
            "assembly_job_id": job["id"] if job else None,
            "artifact_id": str(master.pk) if master else None,
            "sha256": master.sha256 if master else None,
            "manifest_sha256": master.manifest_metadata_sha256 if master else None,
            "size_bytes": master.size_bytes if master else None,
            "mime_type": master.mime_type if master else None})
    attempts = GenerationAttempt.objects.filter(stage_run__production_run=run, retry_successor__isnull=True)
    unresolved_task = (Q(generation_task__isnull=False) & ~Q(generation_task__submission_token="")
        & ~Q(generation_task__submission_state__in=(GenerationTask.SubmissionState.TERMINAL_SUCCEEDED,
            GenerationTask.SubmissionState.TERMINAL_FAILED, GenerationTask.SubmissionState.TERMINAL_CANCELLED))
        & ~Q(generation_task__status="succeeded") & ~Q(generation_task__remote_state="succeeded"))
    if attempts.filter(~Q(status__in=("succeeded", "failed", "cancelled"))
            | Q(error_code="uncertain_remote_response")
            | Q(generation_task__status__in=("queued", "running", "cancel_requested")) | unresolved_task).exists():
        blockers.append("仍有生成调用在途或结果未确认，请先完成或处理这些调用。")
    contract = json_contract({"schema_version": 1, "kind": "manual_delivery",
        "run_plan_sha256": assembly["run_plan_sha256"], "assembly_contract_sha256": assembly["contract_sha256"],
        "selection_revision": assembly["approval"]["revision"], "episodes": episodes}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    bundle = ReleaseBundle.objects.filter(production_run=run).only("id", "status", "manifest_sha256", "sealed_at").first()
    return {"run_id": str(run.pk), "run_plan_sha256": assembly["run_plan_sha256"],
        "contract": contract.value, "contract_sha256": contract.sha256, "blockers": blockers,
        "bundle": None if bundle is None else {"id": str(bundle.pk), "status": bundle.status,
            "manifest_sha256": bundle.manifest_sha256, "sealed_at": bundle.sealed_at}}


def deliver_manual_run(*, run, stage, plan, request, actor):
    if stage.stage_key != "delivery" or stage.mode != "manual":
        raise ValidationError("必须使用本轮人工交付步骤")
    if request["scope"] != {"kind": "project", "id": str(run.project_id)} or request["unit_key"] != f"project:{run.project_id}":
        raise ValidationError("成片确认必须定位当前项目的本轮交付范围")
    payload = request["payload"]
    if set(payload) != {"expected_contract_sha256", "comment"}:
        raise ValidationError("请提交已核对的成片清单摘要与确认说明，不接受重新选片或转码参数")
    detail = manual_delivery_detail(run)
    if (payload["expected_contract_sha256"] != detail["contract_sha256"]
            or request["expected_selection_revision"] != detail["contract"]["selection_revision"]):
        raise ValidationError("成片清单或选片审核已变化，请重新读取后确认", code="manual_selection_conflict")
    if detail["blockers"]:
        raise ValidationError(detail["blockers"], code="manual_action_not_ready")
    from studio.services.production_runs import _seal_run_release_bundle, StageBlocked
    links = list(ProductionAssemblyLink.objects.filter(production_run=run).select_related("assembly_job", "episode").order_by("episode__number"))
    comment = normalize_optional_audit_comment(payload["comment"])
    try:
        bundle = _seal_run_release_bundle(run, stage, links)
    except StageBlocked as exc:
        raise ValidationError(exc.reason, code="manual_action_not_ready") from exc
    # The bundle, decision, Run terminal state and command receipt commit
    # together. No model call, encoder, filesystem repair or next-stage trigger.
    record_manual_approval(run, stage, value={"schema_version": 1, "contract": detail["contract"],
        "contract_sha256": detail["contract_sha256"], "release_bundle_id": str(bundle.pk),
        "manifest_sha256": bundle.manifest_sha256}, revision=0, comment=comment, actor=actor)
    stage.output_reference = f"release-bundle:{bundle.pk}"
    stage.save(update_fields=("output_reference", "updated_at"))
    run.status, run.current_stage_key = "succeeded", "delivery"
    run.blocked_code, run.blocked_reason = "", ""
    run.finished_at = timezone.now()
    run.save(update_fields=("status", "current_stage_key", "blocked_code", "blocked_reason", "finished_at", "updated_at"))
    return ManualActionResult("release_bundle", bundle.pk)
