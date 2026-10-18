"""Shared decision persistence for original Asset/Smoke/Production owners."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import assign_json_contract


def record_prepared_dispatch_gate(job, preparation, detail, policies, decision, root, *, phase):
    now, basis = timezone.now(), preparation.revision
    first = decision["blockers"][0] if decision["blockers"] else {"code": "", "message": ""}
    preparation.revision += 1
    preparation.evaluation_revision, preparation.evaluated_at = preparation.revision, now
    preparation.reason_code, preparation.reason = first["code"], first["message"]
    preparation.next_check_at = None if decision["can_submit"] else now + timedelta(seconds=policies.recheck_interval_seconds)
    if decision.get("awaiting_resource_confirmation"):
        preparation.next_check_at = None
    # 'ready' records immutable request completion; 'dispatched' records that
    # the original queue owns it. A later blocked gate changes neither fact.
    if decision["can_submit"]:
        preparation.status = "dispatched"
        pricing = root.value["budget"]["estimate"]
        if pricing["amount"] is None:
            raise ValidationError("当前费用未知，不能预留后提交")
        job.estimated_cost, job.cost_currency = Decimal(pricing["amount"]), pricing["currency"]
    job.error_code, job.error_message = first["code"], first["message"]
    job.next_poll_at = preparation.next_check_at
    evaluation = {"schema_version": 1, "phase": phase, "preparation_revision": preparation.revision,
        "basis_preparation_revision": basis, "evaluated_at": now.isoformat(),
        "status": "dispatched" if decision["can_submit"] else "blocked",
        "reason_code": first["code"], "reason": first["message"],
        "candidate_request_sha256": preparation.request_sha256, "configuration": policies.configuration.value,
        "root_checks": root.value, "admission": decision}
    assign_json_contract(detail, "evaluation", evaluation, max_bytes=execution_configuration_max_bytes())
    preparation.full_clean()
    detail.clean()
    preparation.save()
    detail.save(update_fields=("evaluation", "evaluation_schema_version", "evaluation_size_bytes", "evaluation_sha256", "updated_at"))
