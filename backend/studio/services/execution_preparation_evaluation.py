"""Persist field readiness, separately from final request/admission/dispatch."""
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import ExecutionPreparation
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparation_fields import _locked_preparations, read_execution_field_selection_snapshot
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.json_contracts import assign_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


@security_boundary_resolution_cache()
def evaluate_preparation_fields(*, preparation_id, expected_preparation_revision, actor):
    """A formal advance step; GET must never call this mutating service.

    The latest evaluation remains as a dated fact when a field edit advances
    the preparation revision. Such an evaluation is stale, not a current gate.
    """
    from studio.services.execution_field_review import apply_preparation_field_policy
    expected_preparation_revision = apply_preparation_field_policy(preparation_id=preparation_id,
        expected_preparation_revision=expected_preparation_revision, actor=actor)
    selected = read_execution_field_selection_snapshot(preparation_id=preparation_id,
        expected_preparation_revision=expected_preparation_revision, actor=actor)
    preparation = ExecutionPreparation.objects.get(pk=preparation_id)
    statuses = {}
    for row in preparation.field_values.values("unit_key", "field_key", "status"):
        statuses.setdefault((row["unit_key"], row["field_key"]), set()).add(row["status"])
    outstanding = []
    for row in selected.value["unselected_fields"]:
        if not row["required"]:
            continue
        states = statuses.get((row["unit_key"], row["field_key"]), set())
        action = "select" if "confirmed" in states else "review" if "pending" in states else "input"
        outstanding.append({**row, "action": action})
    if any(row["action"] == "input" for row in outstanding):
        state, code, reason = "awaiting_input", "execution_fields_missing", "请填写缺少的必需字段，或明确启动字段生成"
        if preparation.purpose == "resource_smoke":
            reason = "请明确填写本次烟测所需的字段夹具；基础资源验收不会自动调用辅助模型"
    elif outstanding:
        state, code, reason = "awaiting_review", "execution_fields_unselected", "请确认字段内容并明确采用本次使用的修订"
    else:
        state, code, reason = "preparing", "", ""
    body = {"schema_version": 1, "phase": "fields", "status": state,
        "outstanding_fields": outstanding, "selected_count": len(selected.value["fields"]),
        "field_selection_sha256": selected.sha256, "reason_code": code, "reason": reason}
    with _locked_preparations([preparation_id], actor) as rows:
        current = rows[str(preparation_id)]
        if current.revision != expected_preparation_revision:
            raise ValidationError("字段在评估期间变化，请重新读取", code="execution_preparation_conflict")
        detail = read_execution_preparation_detail(current)
        if current.request_frozen_at is not None or current.status == "cancelled":
            raise ValidationError("准备已冻结或取消，不能重新评估字段状态", code="execution_preparation_conflict")
        # The selection snapshot also carries the preparation revision. Its
        # digest consequently changes on this evaluation's own revision bump;
        # compare actual field facts rather than scheduling endless rechecks.
        stable = {key: value for key, value in body.items() if key != "field_selection_sha256"}
        previous = detail.evaluation
        if (current.evaluation_revision == current.revision and isinstance(previous, dict)
                and {key: value for key, value in previous.items() if key not in {"field_selection_sha256", "basis_preparation_revision", "preparation_revision", "evaluated_at"}} == stable):
            return current
        now = timezone.now()
        current.revision += 1
        current.evaluation_revision, current.evaluated_at = current.revision, now
        current.status, current.reason_code, current.reason = state, code, reason
        # Human-only input/review waits do not need a hot scan. A field write
        # or explicit continue schedules them again; no scan grants approval.
        current.next_check_at = None if outstanding else now
        current.save(update_fields=("revision", "evaluation_revision", "evaluated_at", "status", "reason_code", "reason", "next_check_at", "updated_at"))
        assign_json_contract(detail, "evaluation", {**body, "preparation_revision": current.revision,
            "basis_preparation_revision": expected_preparation_revision,
            "evaluated_at": now.isoformat()}, max_bytes=execution_configuration_max_bytes())
        detail.save(update_fields=("evaluation", "evaluation_schema_version", "evaluation_size_bytes", "evaluation_sha256", "updated_at"))
        return current
