"""Database-only advancement inside existing workers, not a remote queue.

The persisted due time is the recovery mechanism; notifications are optional.
Leases share the approved root -> Preparation lock order. Production retries
obey the frozen policy through the original dispatcher; no extra worker service.
"""
from datetime import timedelta
import logging
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError
from django.db.models import Q
from django.utils import timezone

from studio.models import ExecutionPreparation
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_policy_configuration import read_execution_policies
from studio.services.execution_preparation_owners import locked_preparation_owner_rows, preparation_owner_ref, preparation_actor
from studio.services.provider_registry import ProviderDriverError
from studio.services.history_cleanup_pause import background_check_gate


logger = logging.getLogger(__name__)


def due_execution_preparations(now):
    from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
    from studio.services.async_media_contract import ASYNC_MEDIA_OPERATIONS
    return ExecutionPreparation.objects.filter(
        Q(asset_job__status__in=("preparing", "cancel_requested"))
        | Q(deployment_smoke__scope="instance", deployment_smoke__status__in=("preparing", "cancel_requested"))
        | Q(workflow_smoke__scope="instance", workflow_smoke__status__in=("preparing", "cancel_requested"))
        | Q(stage_run__status="running", stage_run__production_run__status="running",
            execution_instance_version__workflow_version__isnull=True,
            execution_instance_version__operation__in=(*PRODUCTION_TEXT_OPERATIONS, *FIELD_OPERATION_MODES))
        | Q(stage_run__status="running", stage_run__production_run__status="running",
            stage_run__stage_key__in=("media_generation", "bible_image_generation"),
            execution_instance_version__workflow_version__isnull=True,
            execution_instance_version__deployment__profile_version__provider__driver_type="async_rest",
            execution_instance_version__operation__in=ASYNC_MEDIA_OPERATIONS),
        status__in=("preparing", "blocked", "ready", "awaiting_input", "cancelled"), next_check_at__lte=now).filter(
        Q(claim_token__isnull=True) | Q(claim_until__lte=now))


def claim_next_execution_preparation():
    """Claim at most one local work unit; do not initialize remote deadlines."""
    with background_check_gate() as paused:
        if paused:
            return None
        return _claim_next_execution_preparation()


def _claim_next_execution_preparation():
    policies = read_execution_policies()
    candidate = due_execution_preparations(timezone.now()).order_by("next_check_at", "id").only("id", "asset_job_id", "deployment_smoke_id", "workflow_smoke_id", "stage_run_id").first()
    if candidate is None:
        return None
    with locked_preparation_owner_rows([preparation_owner_ref(candidate)]):
        now = timezone.now()
        preparation = due_execution_preparations(now).select_for_update(of=("self",)).filter(pk=candidate.pk).first()
        if preparation is None:
            return None
        preparation.claim_token, preparation.claim_until = uuid4(), now + timedelta(seconds=policies.claim_timeout_seconds)
        preparation.save(update_fields=("claim_token", "claim_until", "updated_at"))
        return preparation


def process_claimed_execution_preparation(claim):
    """Use the same explicit advance service; never call any provider here."""
    current = ExecutionPreparation.objects.select_related("asset_job__owner", "created_by").get(pk=claim.pk)
    if (claim.claim_token is None or current.claim_token != claim.claim_token
            or current.claim_until is None or current.claim_until <= timezone.now()):
        return current
    basis = current.revision
    failure = None
    try:
        advance_execution_preparation(preparation_id=current.pk, expected_preparation_revision=basis, actor=preparation_actor(current))
    except (ValidationError, PermissionDenied, ProviderDriverError) as exc:
        code = getattr(exc, "code", None)
        failure = (code if isinstance(code, str) and len(code) <= 100 else "execution_preparation_invalid",
            "执行准备未通过当前校验，请检查输入、字段、资源和预算配置后继续。")
    except OperationalError:
        # Existing worker_database owns rolled-back database-conflict retry;
        # do not turn transient lock contention into a business failure.
        raise
    except Exception as exc:
        # Do not let a single malformed preparation terminate the original
        # remote worker. Avoid serializing private source/config error text.
        logger.error("Execution preparation %s failed (%s)", current.pk, type(exc).__name__)
        failure = ("execution_preparation_error", "执行准备处理异常，尚未提交主调用；请检查配置或联系管理员。")
    with locked_preparation_owner_rows([preparation_owner_ref(current)]):
        latest = ExecutionPreparation.objects.select_for_update().get(pk=current.pk)
        if latest.claim_token != claim.claim_token:
            return latest
        if failure is not None:
            if latest.revision == basis:
                # Do not overwrite a concurrently changed field selection or a
                # newer successful evaluation with an obsolete scanner error.
                latest.revision += 1
                if latest.request_frozen_at is None and latest.status != "cancelled":
                    latest.status = "blocked"
                latest.reason_code, latest.reason = failure
            latest.next_check_at = timezone.now() + timedelta(seconds=read_execution_policies().recheck_interval_seconds)
        latest.claim_token, latest.claim_until = None, None
        latest.save(update_fields=("revision", "status", "reason_code", "reason", "next_check_at", "claim_token", "claim_until", "updated_at"))
        return latest


def advance_next_execution_preparation():
    claim = claim_next_execution_preparation()
    return process_claimed_execution_preparation(claim) if claim is not None else None
