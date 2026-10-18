"""Exact assembly retry targets shared by Run detail, preview and writer."""
from django.core.exceptions import ValidationError

from studio.models import ProductionAssemblyLink
from studio.services.production_recipe_stage_snapshots import recipe_assembly_configuration, recipe_assembly_sources


def assembly_retry_links(stage, *, lock=False):
    rows = ProductionAssemblyLink.objects.filter(production_run_id=stage.production_run_id, stage_run=stage)
    if lock:
        rows = rows.select_for_update()
    return rows.select_related("episode", "assembly_job").only(
        "id", "episode_id", "stage_run_id", "production_run_id", "assembly_job_id", "episode__number",
        "assembly_job__id", "assembly_job__status", "assembly_job__error_message",
        "assembly_job__consumer_stage_id", "assembly_job__consumer_input_sha256",
        "assembly_job__episode_id", "assembly_job__project_id", "assembly_job__owner_id", "assembly_job__scope", "assembly_job__dry_run",
    ).order_by("episode__number", "id")


def assembly_consumption_matches(link, stage):
    job = link.assembly_job
    return (job.consumer_stage_id == stage.pk and job.consumer_input_sha256 == stage.input_snapshot_sha256
        and job.episode_id == link.episode_id and job.project_id == stage.production_run.project_id
        and job.owner_id == stage.production_run.project.owner_id and job.scope == "episode" and not job.dry_run)


def assembly_retry_context(stage, links=None):
    if links is None:
        links = assembly_retry_links(stage)
    return {"expected_assembly_job_ids": sorted(str(row.assembly_job_id) for row in links
        if row.assembly_job.status == "failed"), "expected_input_sha256": stage.input_snapshot_sha256}


def require_assembly_retry(stage, links, *, expected_assembly_job_ids, expected_input_sha256, lock=True):
    current = assembly_retry_context(stage, links)
    expected = [str(key) for key in expected_assembly_job_ids or []]
    if (not expected or len(set(expected)) != len(expected)
            or sorted(expected) != current["expected_assembly_job_ids"]
            or not expected_input_sha256 or expected_input_sha256 != current["expected_input_sha256"]):
        raise ValidationError("失败装配任务或消费输入版本已变化或未明确，请重新读取后确认；不会重发。")
    failed = [row for row in links if row.assembly_job.status == "failed"]
    for row in links:
        job = row.assembly_job
        if not assembly_consumption_matches(row, stage):
            raise ValidationError(f"第{row.episode.number}集装配任务{job.pk}消费来源未记录或已变化，不能推测历史输入后重试。")
    # These are the same frozen contracts consumed by the original writer.
    configuration, sources = recipe_assembly_configuration(stage), recipe_assembly_sources(stage, lock=lock)
    if {str(row.episode_id) for row in links} != {row["episode_id"] for row in sources["episodes"]}:
        raise ValidationError("当前装配任务未完整覆盖冻结剧集，不能通过重试额外创建未确认任务。")
    return failed, configuration, sources


def assembly_retry_preview(run, stage, links):
    from studio.services.production_execution_retry import require_stage_retry_status

    context = {"expected_stage_id": str(stage.pk), "expected_attempt_ids": [], **assembly_retry_context(stage, links)}
    reason = ""
    try:
        require_stage_retry_status(run, stage)
        require_assembly_retry(stage, links, expected_assembly_job_ids=context["expected_assembly_job_ids"],
            expected_input_sha256=context["expected_input_sha256"], lock=False)
    except ValidationError as exc:
        reason = "；".join(exc.messages)
    return {"retry_context": context, "mode": "assembly_jobs", "can_submit": not reason, "reason": reason,
        "call_count": 0, "preparation_count": 0, "assembly_count": len(links),
        "message": "仅为明确失败的装配创建后继任务，绑定本次确认的任务ID和消费输入SHA；成功及在途任务保留，不生成媒体、不改变选片。"}


def assembly_retry_impact(link, stage):
    job = link.assembly_job
    disposition = "skip"
    message = "装配已成功，保留原输出，不重新装配。"
    if job.status == "failed":
        recorded = assembly_consumption_matches(link, stage)
        disposition = "retry" if recorded else "attention"
        message = "明确失败；仅使用原冻结来源重试。" if recorded else "消费来源未记录或输入版本变化，不能推测或补造历史。"
    elif job.status != "succeeded":
        disposition, message = "attention", "装配尚未确认终态；请读取原任务结果，不重新提交此任务。"
    return {"id": str(job.pk), "kind": "assembly", "unit_key": f"episode:{link.episode_id}",
        "preparation_id": None, "attempt_id": None, "status": job.status, "error_code": "",
        "disposition": disposition, "message": message, "episode_number": link.episode.number,
        "consumer_input_sha256": job.consumer_input_sha256, "error_message": job.error_message}
