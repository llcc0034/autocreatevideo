"""Resume the original task's local publication phase, never model execution."""
from datetime import timedelta
import logging
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, close_old_connections
from django.utils import timezone

from studio.models import GenerationAttempt, GenerationTask, ProductionRun, ReleaseBundle
from studio.services.distribution import DistributionClaimLost, _lock_stale_seconds, prepare_all_task_distributions
from studio.services.production_run_auto_advance import schedule_generation_task_run_advance
from studio.services.worker_heartbeat import WorkerHeartbeat

logger = logging.getLogger(__name__)
PHASE_FIELDS = ("id", "kind", "distribution_state", "distribution_revision", "distribution_requested_at",
    "distribution_started_at", "distribution_finished_at", "distribution_error")


class DistributionTaskHeartbeat(WorkerHeartbeat):
    def __init__(self, task):
        super().__init__(f"distribution-{task.pk}")
        if self.interval >= _lock_stale_seconds():
            raise ValidationError("分发锁陈旧时间必须大于任务心跳间隔")
        self.task_id, self.claim = task.pk, task.distribution_claim

    def _touch(self):
        close_old_connections()
        try:
            if not GenerationTask.objects.filter(pk=self.task_id, distribution_state="running", distribution_claim=self.claim).update(
                    distribution_heartbeat_at=timezone.now()):
                raise DistributionClaimLost("Distribution heartbeat claim has expired")
        finally:
            close_old_connections()


def distribution_phase(task):
    return {"task_id": str(task.pk), "kind": task.kind, "state": task.distribution_state,
        "revision": task.distribution_revision, "requested_at": task.distribution_requested_at,
        "started_at": task.distribution_started_at, "finished_at": task.distribution_finished_at,
        "error": task.distribution_error}


def run_distribution_tasks(run):
    # Semi-join avoids duplicate tasks or wide task/preparation JSON per page.
    ids = GenerationAttempt.objects.filter(stage_run__production_run_id=run.pk,
        generation_task__isnull=False).values("generation_task_id")
    return GenerationTask.objects.filter(pk__in=ids, status="succeeded", kind__in=("image", "video"))


def _enqueue(task, now):
    task.distribution_state = "queued"
    task.distribution_revision += 1
    task.distribution_claim = None
    task.distribution_requested_at = now
    task.distribution_started_at = task.distribution_finished_at = None
    task.distribution_heartbeat_at = None
    task.distribution_error = ""
    task.save(update_fields=("distribution_state", "distribution_revision", "distribution_claim",
        "distribution_requested_at", "distribution_started_at", "distribution_heartbeat_at", "distribution_finished_at", "distribution_error", "updated_at"))


def schedule_initial_distribution(task):
    """Called inside the original success+artifact transaction, before commit."""
    if not transaction.get_connection().in_atomic_block or task.status != "succeeded":
        raise ValidationError("副本安排必须与原成功母版同事务提交")
    if task.kind in ("image", "video") and task.distribution_state == "not_requested":
        _enqueue(task, timezone.now())


@transaction.atomic
def request_distribution_recovery(*, run_id, task_id, expected_revision, actor):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    run = ProductionRun.objects.select_for_update().only("id", "owner_id", "status").get(pk=run_id)
    if actor.pk != run.owner_id and not actor.is_staff:
        raise PermissionDenied("无权操作此制作运行")
    task = run_distribution_tasks(run).select_for_update().filter(pk=task_id).only(
        *PHASE_FIELDS, "status", "distribution_claim", "distribution_heartbeat_at").first()
    if task is None:
        raise ValidationError("只能恢复本轮已成功图片或视频任务的分发副本")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError("请提交已读取的副本处理修订")
    # An uncertain HTTP response can replay the same compare-and-swap. It
    # never schedules another encode, even if that attempt has already failed.
    if task.distribution_revision == expected_revision + 1:
        return task, False
    if task.distribution_revision != expected_revision:
        raise ValidationError("副本处理修订已变化，请重新读取", code="distribution_revision_conflict")
    if run.status in ("succeeded", "failed", "cancelled") or ReleaseBundle.objects.filter(
            production_run=run, status__in=("sealed", "released")).exists():
        raise ValidationError("已结束或封存的制作运行不能重新安排副本")
    if task.distribution_state not in ("not_requested", "failed"):
        raise ValidationError("副本已安排或完成，不能重复提交", code="distribution_revision_conflict")
    _enqueue(task, timezone.now())
    return task, True


@transaction.atomic
def claim_task_distribution(task_id=None):
    rows = GenerationTask.objects.filter(distribution_state="queued", status="succeeded")
    if task_id is not None:
        rows = rows.filter(pk=task_id)
    candidate = rows.order_by("distribution_requested_at", "id").values_list("id", flat=True).first()
    if candidate is None:
        return None
    token = uuid.uuid4()
    # CAS also protects SQLite, where select_for_update alone is ineffective.
    if not GenerationTask.objects.filter(pk=candidate, distribution_state="queued", status="succeeded").update(
            distribution_state="running", distribution_claim=token, distribution_started_at=timezone.now(), distribution_heartbeat_at=timezone.now(), updated_at=timezone.now()):
        return None
    return GenerationTask.objects.get(pk=candidate)


def recover_stale_task_distributions():
    # Reuse the existing publisher-lock recovery threshold. Stale local work
    # is marked failed for an explicit retry, never silently re-enqueued.
    now = timezone.now()
    return GenerationTask.objects.filter(distribution_state="running",
        distribution_heartbeat_at__lt=now - timedelta(seconds=_lock_stale_seconds())).update(
            distribution_state="failed", distribution_claim=None, distribution_finished_at=now,
            distribution_error="副本处理已中断或超时，请确认后重新处理；未重新调用模型。", updated_at=now)


def process_task_distribution(task, *, advance=True):
    token = task.distribution_claim
    if token is None or task.distribution_state != "running":
        raise DistributionClaimLost("Distribution processing requires an actual task claim")
    rows = GenerationTask.objects.filter(pk=task.pk, distribution_state="running", distribution_claim=token)
    if not rows.exists():
        raise DistributionClaimLost("Distribution task claim has expired")
    runs = ProductionRun.objects.filter(stages__generation_attempts__generation_task_id=task.pk)
    # Cancellation/sealing after enqueue cannot authorize new local work.
    if runs.filter(status__in=("succeeded", "failed", "cancelled")).exists():
        rows.update(distribution_state="failed", distribution_claim=None, distribution_finished_at=timezone.now(),
            distribution_error="所属制作运行已结束，未继续处理副本。", updated_at=timezone.now())
        return GenerationTask.objects.get(pk=task.pk)
    try:
        with DistributionTaskHeartbeat(task):
            errors = prepare_all_task_distributions(task, claim=token)
        message = "部分副本处理失败，请查看原任务副本说明并修复配置后重试。" if errors else ""
    except DistributionClaimLost:
        return GenerationTask.objects.get(pk=task.pk)
    except Exception:
        logger.exception("Distribution phase failed for task %s", task.pk)
        message = "副本处理未完成，请检查服务和媒体配置后重试；母版未修改。"
    now = timezone.now()
    if rows.update(distribution_state="failed" if message else "ready", distribution_claim=None,
            distribution_finished_at=now, distribution_error=message, updated_at=now) and advance:
        schedule_generation_task_run_advance(task.pk)
    return GenerationTask.objects.get(pk=task.pk)
