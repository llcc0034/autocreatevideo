"""Read-only, bounded production preparation labels and linked call facts."""
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Count, F, Min, OuterRef, Q, Subquery, Window
from django.db.models.functions import Coalesce, RowNumber

from studio.models import AssetGenerationJob, CharacterBible, SceneBible, PropBible, ExecutionPreparation, GeneratedArtifact, GenerationAttempt, Shot
from studio.services.execution_field_contracts import FIELD_OPERATION_MODES


def preparation_unit_queryset(query, stage):
    """Group by verified business unit, never a display label or parsed child key."""
    parent_unit = GenerationAttempt.objects.filter(
        preparation_id=OuterRef("pk"), stage_run_id=stage.pk, role="auxiliary",
        parent_preparation__stage_run_id=stage.pk,
        preparation__execution_instance_version__operation__in=FIELD_OPERATION_MODES,
    ).order_by("-attempt_number", "-created_at", "-id").values("parent_preparation__unit_key")[:1]
    # An undispatched/unlinked preparation stays separately visible. Do not read
    # large frozen contracts or guess lineage from a field:<uuid>:<hash> string.
    return query.annotate(unit_group_key=Coalesce(Subquery(parent_unit), F("unit_key")))


def preparation_unit_groups(query):
    partition = [F("unit_group_key")]
    return query.annotate(
        unit_version_count=Window(expression=Count("pk"), partition_by=partition),
        unit_first_created=Window(expression=Min("created_at"), partition_by=partition),
        unit_latest_rank=Window(expression=RowNumber(), partition_by=partition,
            order_by=[F("created_at").desc(), F("pk").desc()]),
    ).filter(unit_latest_rank=1).order_by("unit_first_created", "unit_group_key")


def latest_preparation_attempts(ids):
    return GenerationAttempt.objects.filter(preparation_id__in=ids).order_by().annotate(
        presentation_rank=Window(expression=RowNumber(), partition_by=[F("preparation_id")],
            order_by=[F("attempt_number").desc(), F("created_at").desc(), F("id").desc()]),
    ).filter(presentation_rank=1).only("id", "preparation_id", "parent_preparation_id", "stage_run_id",
        "role", "status", "error_code", "attempt_number", "created_at", "ignore_late_result")


def preparation_outcome(preparation, task, *, auxiliary, artifacts=0, selected=0, asset_job=False, text_result=False, human_corrected=False):
    """Explain separate stored facts; never promote dispatch to task/output success."""
    title = "辅助调用" if auxiliary else "主调用"
    if preparation.status == "cancelled":
        message = "此准备已停止，不会继续准备或自动重新提交。"
        if task and (task.status not in {"succeeded", "failed", "cancelled"} or task.error_code == "uncertain_remote_response"):
            message += "关联调用尚未确认终态，请查看原任务；停止准备不等于远端已经结束。"
        elif task:
            message += "关联调用及既有产物仍按原记录展示。"
    elif task is None:
        message = (f"{title}尚未创建；输入准备受阻，请查看下方原因。" if preparation.status == "blocked" else
            f"未读取到{title}记录，请核对任务关联，不能据此判断执行成功。" if preparation.status == "dispatched" else
            f"{title}尚未创建；当前处于输入准备阶段。")
    elif task.ignore_late_result:
        message = "已停止采用此调用的后续结果；原调用状态仍单独展示，不会自动重新提交。"
    elif task.error_code == "uncertain_remote_response" or task.status == "uncertain":
        message = f"{title}远端结果尚不确定，请核对原调用；不能按明确失败重新提交。"
    elif task.status == "preparing":
        message = f"{title}任务已登记，仍在准备输入，尚未进入执行队列。"
        if preparation.status == "blocked":
            message += "输入准备受阻，请查看准备原因。"
    elif task.status == "succeeded":
        message = ("已保存人工补正结果；原模型返回与校验记录保留，未重新调用或自动采用。" if human_corrected else
            "辅助调用成功，仅代表输入字段生成或翻译完成，不代表主调用成功或已生成媒体。" if auxiliary else
            "主调用成功，已关联生成素材；入库不代表已被下游使用。" if asset_job and artifacts else
            "主调用成功，已保存文本结果；文本结果不代表已生成媒体素材。" if asset_job and text_result else
            "主调用成功，但未读取到关联产物；请核对输出解析与落库记录。" if not artifacts else
            f"主调用成功，已记录 {artifacts} 项产物，其中 {selected} 项标记为已选择。")
    elif task.status == "failed":
        message = f"{title}失败，请查看原调用错误与已保留的返回；辅助字段成功不能抵消主调用失败。" if not auxiliary else "辅助调用失败；请在主准备核对字段和本批次错误。"
    elif task.status == "awaiting_review":
        message = f"{title}已返回，但输出仍待处理，不能视为成功产物；请查看原返回及校验信息。"
    elif task.status == "cancelled":
        message = f"{title}已取消，不再等待本次生成完成；既有记录保留，不会自动重试。"
    elif task.status == "cancel_requested":
        message = f"{title}已请求取消，尚未确认远端终态；请核对原任务，不要重复提交。"
    else:
        message = f"{title}尚未结束，请按实际调用状态查看进度；已派发不代表已生成产物。"
    return {"message": message, "artifact_count": artifacts, "selected_count": selected}


def attempt_artifact_counts(attempt_ids, stage_id, run_id):
    """Counts belong to exact calls, never to sibling retries or auxiliary fields."""
    return {row["generation_attempt_id"]: row for row in GeneratedArtifact.objects.filter(
        generation_attempt_id__in=attempt_ids, stage_run_id=stage_id, production_run_id=run_id,
    ).order_by().values("generation_attempt_id").annotate(
        count=Count("pk"), selected=Count("pk", filter=Q(status="selected")))}


def selected_preparation_outcome(preparation, source=None):
    """Detail projection; a supplied source is the exact selected historical call."""
    if preparation.stage_run_id:
        auxiliary = preparation.execution_instance_version.operation in FIELD_OPERATION_MODES
        outputs = attempt_artifact_counts([source.pk], preparation.stage_run_id,
            preparation.stage_run.production_run_id).get(source.pk, {}) if source and not auxiliary else {}
        return preparation_outcome(preparation, source, auxiliary=auxiliary,
            artifacts=outputs.get("count", 0), selected=outputs.get("selected", 0))
    if preparation.asset_job_id:
        from studio.models import ExecutionTextResponseReview
        source = source if source is not None else AssetGenerationJob.objects.only(
            "id", "status", "error_code", "ignore_late_result", "job_type", "output_asset_id",
            "result_text_size_bytes", "result_text_sha256").get(pk=preparation.asset_job_id)
        return preparation_outcome(preparation, source, auxiliary=source.job_type == "field", asset_job=True,
            human_corrected=ExecutionTextResponseReview.objects.filter(asset_job_id=source.pk, confirmed_at__isnull=False).exists(),
            artifacts=int(source.output_asset_id is not None),
            text_result=bool(source.result_text_size_bytes and source.result_text_sha256))
    # Smoke has a separate evidence contract; it does not create durable assets.
    return None


def production_preparation_presentations(rows, stage, *, verified_parents=None):
    """Never read large request snapshots for a list, or infer success from dispatch.

    List parent links require an existing Attempt FK. A pre-dispatch detail may
    supply its parent from the formal frozen production-field context reader.
    """
    rows = list(rows)
    verified_parents = verified_parents or {}
    attempts = {row.preparation_id: row for row in latest_preparation_attempts([row.pk for row in rows])}
    artifact_counts = attempt_artifact_counts([task.pk for task in attempts.values() if task.role == "primary"],
        stage.pk, stage.production_run_id)
    parent_ids = {row.parent_preparation_id for row in attempts.values() if row.parent_preparation_id}
    parents = {row.pk: row for row in ExecutionPreparation.objects.filter(pk__in=parent_ids, stage_run=stage)
        .only("id", "stage_run_id", "unit_key", "status")}
    parents.update({row.pk: row for row in verified_parents.values()})
    shot_ids = {}
    for row in [*rows, *parents.values()]:
        if row.stage_run_id != stage.pk:
            raise ValidationError("准备关联不属于当前阶段")
        if row.unit_key.startswith("shot:"):
            try:
                shot_ids[row.pk] = UUID(row.unit_key[5:])
            except ValueError:
                pass  # Show an explicit unresolved object, never a guessed shot.
    shots = {row["id"]: row for row in Shot.objects.filter(pk__in=shot_ids.values(),
        episode__project_id=stage.production_run.project_id).values("id", "number", "episode__number")}
    bible_labels = {}
    for prefix, model, title in (("character", CharacterBible, "人物"), ("scene", SceneBible, "场景"), ("prop", PropBible, "道具")):
        ids = []
        for row in [*rows, *parents.values()]:
            if row.unit_key.startswith(prefix + ":"):
                try:
                    ids.append(UUID(row.unit_key[len(prefix) + 1:]))
                except ValueError:
                    pass
        for entity in model.objects.filter(pk__in=ids, project_id=stage.production_run.project_id).order_by().values("id", "name"):
            bible_labels[f"{prefix}:{entity['id']}"] = f"{title} · {entity['name']}"

    def label(row):
        shot = shots.get(shot_ids.get(row.pk))
        if shot:
            return f"第{shot['episode__number']}集 · 镜头{shot['number']}"
        if row.unit_key.startswith("shot:"):
            return "镜头准备（对象不可用）"
        return bible_labels.get(row.unit_key, "制作准备")

    for row in rows:
        operation = row.execution_instance_version.operation if row.execution_instance_version_id else ""
        auxiliary = operation in FIELD_OPERATION_MODES
        task = attempts.get(row.pk)
        parent = verified_parents.get(row.pk)
        if task:
            if task.stage_run_id != stage.pk or (task.role == "auxiliary") != auxiliary:
                raise ValidationError("调用与准备的阶段或职责不一致")
            if task.parent_preparation_id:
                linked = parents.get(task.parent_preparation_id)
                if linked is None or (parent is not None and parent.pk != linked.pk):
                    raise ValidationError("辅助调用的主准备关联不可核验")
                parent = linked
        activity = "提示词翻译" if operation == "prompt.fields.translate" else "提示词生成"
        outputs = artifact_counts.get(task.pk, {}) if task else {}
        row.outcome = preparation_outcome(row, task, auxiliary=auxiliary,
            artifacts=outputs.get("count", 0), selected=outputs.get("selected", 0))
        row.presentation = {"kind": "auxiliary" if auxiliary else "primary", "operation": operation,
            "label": f"{label(parent)} · {activity}" if auxiliary and parent else activity if auxiliary else label(row),
            "parent": {"id": parent.pk, "label": label(parent), "status": parent.status, "unit_key": parent.unit_key} if parent else None,
            "task": {"id": task.pk, "status": task.status, "error_code": task.error_code,
                "attempt_number": task.attempt_number} if task else None}
    return rows
