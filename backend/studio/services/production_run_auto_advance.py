from __future__ import annotations

import logging
import uuid

from django.db import transaction

from studio.models import ProductionRun


logger = logging.getLogger(__name__)


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

        run = ProductionRun.objects.filter(pk=normalized_id).only("id", "status").first()
        if run is None or run.status in {
            ProductionRun.Status.SUCCEEDED,
            ProductionRun.Status.FAILED,
            ProductionRun.Status.CANCELLED,
        }:
            return
        try:
            advance_production_run(run)
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
