from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from studio.models import ProductionRun


logger = logging.getLogger(__name__)


def active_recipe_runs():
    """Narrow persistent worklist; not another queue or inferred approval."""
    return ProductionRun.objects.filter(plan_source="recipe", status__in=(
        ProductionRun.Status.QUEUED, ProductionRun.Status.RUNNING,
    )).order_by("status", "id")


def _advance_active_recipe_run(run_id) -> None:
    from studio.services.production_runs import advance_production_run

    observed = active_recipe_runs().filter(pk=run_id).values("id", "updated_at").first()
    if observed is None:
        return
    try:
        advance_production_run(ProductionRun(pk=run_id), automatic=True)
    except Exception:
        logger.exception("Recipe automatic advance failed", extra={"production_run_id": str(run_id)})
        # The original executor's transaction has rolled back. Never spin on
        # a corrupt snapshot, or replace a newer concurrent operator decision.
        try:
            active_recipe_runs().filter(pk=run_id, updated_at=observed["updated_at"]).update(
                status=ProductionRun.Status.BLOCKED, blocked_code="production_auto_advance_failed",
                blocked_reason="自动推进未完成，请查看运行详情和服务日志；不会自动重新调用模型。",
                updated_at=timezone.now(),
            )
        except Exception:
            logger.exception("Could not record recipe advance failure", extra={"production_run_id": str(run_id)})


def schedule_recipe_run_advance(run_id) -> None:
    # Creation/worker facts must commit before the existing executor sees them.
    transaction.on_commit(lambda: _advance_active_recipe_run(run_id))


def advance_active_recipe_runs(*, heartbeat) -> int:
    """Recover lost wakeups after restart; reuse the configured maintenance tick."""
    reconcile_recovered_recipe_runs(heartbeat=heartbeat)
    checked = 0
    # Freeze only IDs before writes move rows between queued/running index
    # ranges; each run is considered once per sweep, never a live cursor loop.
    for run_id in list(active_recipe_runs().values_list("id", flat=True)):
        heartbeat()
        _advance_active_recipe_run(run_id)
        checked += 1
    return checked


def reconcile_recovered_recipe_runs(*, heartbeat) -> int:
    """Catch up parent state when original remote recovery predates this worker.

    This reads persisted recovery facts, not a second queue or a read-side
    display fallback. Other blocked runs remain outside automatic advancement.
    """
    from studio.models import StageRun
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    from studio.services.production_execution_recovery import reconcile_recovered_media_stage

    candidates = ProductionRun.objects.filter(plan_source="recipe", status="blocked",
        blocked_code="uncertain_remote_response").order_by("id").values_list("id", "current_stage_key")
    changed = 0
    for run_id, stage_key in list(candidates):
        heartbeat()
        stage_id = StageRun.objects.filter(production_run_id=run_id, stage_key=stage_key).values_list("id", flat=True).first()
        if stage_id is None:
            continue
        try:
            with locked_preparation_owner_rows([("stage_run_id", stage_id)]) as owners:
                changed += int(reconcile_recovered_media_stage(owners[("stage_run_id", str(stage_id))]))
        except Exception:
            logger.exception("Recovered request parent reconciliation failed", extra={"production_run_id": str(run_id)})
    return changed


def schedule_production_run_advance(production_run_id) -> None:
    """Advance a run only after the worker's terminal fact has committed.

    Duplicate callbacks are safe because the orchestration service locks the
    run row and stops at terminal or review-gated stages.
    """

    if not production_run_id:
        return
    try:
        normalized_id = uuid.UUID(str(production_run_id))
    except (TypeError, ValueError, AttributeError):
        logger.error("Refusing invalid ProductionRun auto-advance id")
        return

    def advance_after_commit() -> None:
        from studio.services.production_runs import advance_production_run

        run = ProductionRun.objects.filter(pk=normalized_id).only("id", "status", "plan_source").first()
        if run is None or run.status in {
            ProductionRun.Status.SUCCEEDED,
            ProductionRun.Status.FAILED,
            ProductionRun.Status.CANCELLED,
        }:
            return
        try:
            advance_production_run(run, automatic=run.plan_source == "recipe")
        except Exception:
            # The worker fact is already durable. Never rewrite it or report a
            # false worker failure because orchestration refresh encountered an
            # unrelated integrity/configuration problem.
            logger.exception(
                "ProductionRun auto-advance failed after worker commit",
                extra={"production_run_id": str(normalized_id)},
            )

    transaction.on_commit(advance_after_commit)


def schedule_generation_task_run_advance(task_id) -> None:
    """Schedule every run durably linked to one terminal ComfyUI task."""

    from studio.models import GenerationAttempt

    run_ids = list(
        GenerationAttempt.objects.filter(generation_task_id=task_id)
        .values_list("stage_run__production_run_id", flat=True)
        .distinct()
    )
    for run_id in run_ids:
        schedule_production_run_advance(run_id)


def schedule_assembly_job_run_advance(assembly_job_id) -> None:
    """Schedule every run durably linked to one terminal assembly job."""

    from studio.models import ProductionAssemblyLink

    run_ids = list(
        ProductionAssemblyLink.objects.filter(assembly_job_id=assembly_job_id)
        .values_list("production_run_id", flat=True)
        .distinct()
    )
    for run_id in run_ids:
        schedule_production_run_advance(run_id)
