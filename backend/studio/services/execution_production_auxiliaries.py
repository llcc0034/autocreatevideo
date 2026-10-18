"""Production field preparations use original Attempts and the Run's ledger."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db.models import F, Window
from django.db.models.functions import RowNumber

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_auxiliary_requests import (
    production_field_unit, read_preparation_field_batch, read_production_field_context,
)
from studio.services.execution_auxiliary_results import record_preparation_child_wait
from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
from studio.services.execution_field_review import production_field_review_contract
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_fields import _editable, _locked_preparations, _touch
from studio.services.execution_preparations import create_execution_instance_preparation, read_execution_preparation_detail
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import json_contract
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


def _reject_parameter_source(source):
    raise ValidationError("辅助叶子参数必须由精确实例配置，不能临时猜测或读取主调用参数")


def production_field_children(parent):
    """Bounded batch units use the existing Stage/unit/request unique index."""
    detail = read_execution_preparation_detail(parent)
    units = [production_field_unit(parent.pk, batch["sha256"]) for batch in detail.resolved_inputs["field_requests"]]
    return ExecutionPreparation.objects.filter(stage_run_id=parent.stage_run_id, unit_key__in=units).annotate(
        batch_rank=Window(expression=RowNumber(), partition_by=[F("unit_key")], order_by=[F("created_at").desc(), F("id").desc()])
    ).filter(batch_rank=1)


def production_field_retry_requires_acknowledgement(attempt):
    """A terminated provider failure is not proof of a zero external bill.

    This does not resolve a remote-uncertain attempt or permit automatic retry.
    The existing parent/batch/ordinal chain remains the retry identity.
    """
    needs_ack = (attempt.status == "failed" and attempt.is_external
        and (attempt.committed_cost != 0 or (attempt.actual_cost is None and attempt.submission_started_at is not None)))
    if needs_ack and (attempt.role != "auxiliary" or attempt.error_code != "execution_provider_failed"
            or attempt.claim_token or attempt.ignore_late_result or attempt.cancel_requested_at
            or attempt.generation_task_id or attempt.result_sha256 or attempt.artifacts.exists()
            or attempt.execution_field_values.exists() or hasattr(attempt, "text_response_review")):
        raise ValidationError("原辅助调用仍有未处理的提交或结果，请先查看原记录；不能新增自动尝试。")
    return needs_ack


@registry_reference_writer
@security_boundary_resolution_cache()
def create_production_field_child(*, preparation_id, expected_preparation_revision, field_batch_key, attempt_number, actor,
        acknowledged_uncertain_task_id=None):
    if type(attempt_number) is not int or attempt_number < 1:
        raise ValidationError("需要明确的字段批次尝试序号")
    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        if not parent.stage_run_id or parent.execution_instance_version.operation in FIELD_OPERATION_MODES:
            raise ValidationError("生产字段调用必须来自真实主准备；辅助叶子不能递归")
        batch, purpose, binding = read_preparation_field_batch(parent, field_batch_key)
        unit_key = production_field_unit(parent.pk, batch.sha256)
        children = ExecutionPreparation.objects.filter(stage_run_id=parent.stage_run_id, unit_key=unit_key)
        existing = children.filter(request_key=f"attempt:{attempt_number}").first()
        if existing is not None:
            read_production_field_context(existing)
            return existing, False
        if parent.revision != expected_preparation_revision:
            raise ValidationError("主准备已变化，请重新读取", code="execution_preparation_conflict")
        _editable(parent)
        stage = parent.stage_run
        run = stage.production_run
        if stage.status != "running" or run.status != "running":
            raise ValidationError("运行或阶段已经停止，不能新增辅助调用")
        acknowledged = False
        if attempt_number == 1:
            if acknowledged_uncertain_task_id is not None:
                raise ValidationError("首次调用不能带入重试确认")
            if children.exists():
                raise ValidationError("批次已经存在，不能重建首次调用")
        else:
            previous = GenerationAttempt.objects.filter(parent_preparation=parent, field_batch_key=field_batch_key).order_by("-attempt_number").first()
            if (previous is None or attempt_number != previous.attempt_number + 1
                    or previous.status not in {"failed", "cancelled", "awaiting_review"} or previous.claim_token
                    or previous.error_code == "uncertain_remote_response"
                    or children.count() != attempt_number - 1):
                raise ValidationError("仅允许处理明确结束的上一批次后显式重试；在途或远端不确定任务不能重发")
            acknowledged = production_field_retry_requires_acknowledgement(previous)
            if acknowledged and str(acknowledged_uncertain_task_id) != str(previous.pk):
                raise ValidationError("原调用费用尚未知，必须明确确认保留原记录和预留、接受可能重复计费后新增尝试。")
            if not acknowledged and acknowledged_uncertain_task_id is not None:
                raise ValidationError("确认的原调用与当前重试不一致", code="execution_preparation_conflict")
        definition = verify_current_execution_definition(stored_execution_contract(binding["version_id"]))
        if (definition.sha256 != binding["definition_sha256"] or definition.value["target_fingerprint"] != binding["target_fingerprint"]
                or definition.value["configuration"]["prompt_mode"] != "none" or definition.value["interface"]["auxiliaries"]):
            raise ValidationError("辅助叶子必须与主实例的冻结引用一致，且不能递归组装提示词")
        specs = definition.value["configuration"]["inputs"]
        if len(specs) != 1 or specs[0]["targets"] != [{"target_kind": "operation_input", "target_key": "field_request", "is_primary": True}]:
            raise ValidationError("辅助叶子只能接收服务端冻结的字段批次")
        requested = {"inputs": {specs[0]["key"]: {"kind": "literal", "value": deepcopy(batch.value)}},
            "parameters": {}, "manual_channels": {}, "subject_facts": [], "source_language": "", "target_language": ""}
        identity = {"parent_preparation_id": str(parent.pk), "parent_intent_sha256": parent.intent_sha256,
            "field_batch_key": batch.sha256, "purpose": purpose, "attempt_number": attempt_number, "leaf": binding}
        def resolve(definition, owner):
            return resolve_execution_request_facts(definition, requested, owner=owner.production_run.owner, unit_key=unit_key,
                max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
                review_contract=production_field_review_contract(owner), parameter_policies=[], resolve_parameter_source=_reject_parameter_source)
        child = create_execution_instance_preparation(owner_ref=("stage_run_id", stage.pk), version_id=binding["version_id"],
            expected_definition_sha256=definition.sha256, unit_key=unit_key, request_key=f"attempt:{attempt_number}",
            intent=json_contract({**requested, "production_field": identity}, max_bytes=execution_request_max_bytes()),
            resolve_facts=resolve, actor=actor)
        read_production_field_context(child)
        if acknowledged:
            from studio.services.platform_lifecycle_audit import record_platform_lifecycle_event
            record_platform_lifecycle_event(run, actor=actor, action="retry_auxiliary_unknown_cost",
                reason_code="production.auxiliary_explicit_retry",
                before_summary={"attempt_id": str(previous.pk), "status": previous.status,
                    "request_sha256": previous.request_sha256, "error_code": previous.error_code},
                after_summary={"preparation_id": str(child.pk), "parent_preparation_id": str(parent.pk),
                    "field_batch_key": field_batch_key, "attempt_number": attempt_number,
                    "duplicate_billing_risk_acknowledged": True})
        _touch(parent)
        return child, True


def _record_wait(parent, actor):
    # Do not make an old field evaluation current after a concurrent explicit
    # edit/selection. Keep the original root lock through child observation and
    # the shared wait writer, but never hold it across full field evaluation
    # (which may need additional source-selection roots).
    with _locked_preparations([parent.pk], actor) as rows:
        current = rows[str(parent.pk)]
        if current.revision != parent.revision:
            raise ValidationError("主准备已变化，请重新读取", code="execution_preparation_conflict")
        return _record_locked_wait(current, actor)


def _record_locked_wait(parent, actor):
    preparations = list(production_field_children(parent).order_by("unit_key", "created_at"))
    attempts = {row.preparation_id: row for row in GenerationAttempt.objects.filter(
        parent_preparation=parent, preparation_id__in=[row.pk for row in preparations]).select_related(
            "stage_run__production_run", "preparation", "execution_instance_version")}
    observations = []
    for child in preparations:
        _, batch, identity = read_production_field_context(child)
        attempt = attempts.get(child.pk)
        state = attempt.status if attempt else child.status
        recovery = None
        if attempt and state == "failed" and parent.stage_run.production_run.plan_source == "recipe":
            from studio.services.production_execution_recovery import production_retry_decision, retry_due
            recovery = production_retry_decision(attempt)
            if (retry_due(recovery) and parent.stage_run.status == "running"
                    and parent.stage_run.production_run.status == "running"):
                replacement, _ = create_production_field_child(preparation_id=parent.pk,
                    expected_preparation_revision=parent.revision, field_batch_key=batch.sha256,
                    attempt_number=identity["attempt_number"] + 1, actor=actor)
                parent.refresh_from_db()
                observations.append({"id": replacement.pk, "preparation_id": str(replacement.pk),
                    "auxiliary_purpose": identity["purpose"], "field_batch_key": batch.sha256,
                    "attempt_number": identity["attempt_number"] + 1, "status": replacement.status})
                continue
        observations.append({"id": attempt.pk if attempt else child.pk, "preparation_id": str(child.pk),
            "auxiliary_purpose": identity["purpose"], "field_batch_key": batch.sha256,
            "attempt_number": identity["attempt_number"], "status": state,
            **({"recovery": recovery} if recovery else {})})
    observations.sort(key=lambda row: (row["auxiliary_purpose"], row["field_batch_key"], row["attempt_number"]))
    return record_preparation_child_wait(parent.pk, actor, observations)


@registry_reference_writer
@security_boundary_resolution_cache()
def reconcile_production_preparation_fields(*, preparation_id, expected_preparation_revision, actor):
    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        if parent.revision != expected_preparation_revision:
            raise ValidationError("主准备已变化，请重新读取", code="execution_preparation_conflict")
        _editable(parent)
        if not parent.stage_run_id or parent.execution_instance_version.operation in FIELD_OPERATION_MODES:
            raise ValidationError("字段辅助协调只能作用于生产主准备")
        # Re-observe existing children before overwriting an unchanged wait
        # with a transient field evaluation. Every field edit/selection and
        # explicit auxiliary retry touches the parent, invalidating this
        # evaluation; child completion is read by the same original writer.
        detail = read_execution_preparation_detail(parent)
        if parent.evaluation_revision == parent.revision and detail.evaluation.get("phase") == "auxiliary":
            waiting = _record_wait(parent, actor)
            if waiting is not None:
                return waiting
    parent.refresh_from_db()
    evaluated = evaluate_preparation_fields(preparation_id=parent.pk, expected_preparation_revision=parent.revision, actor=actor)
    detail = read_execution_preparation_detail(evaluated)
    if not detail.evaluation["outstanding_fields"]:
        # Explicitly selected manual fields (or the frozen unattended policy)
        # can satisfy the request after a failed auxiliary. Never re-generate
        # accepted text merely because its earlier auxiliary failed.
        return evaluated
    waiting = _record_wait(evaluated, actor)
    if waiting is not None:
        return waiting
    missing = {(row["unit_key"], row["field_key"]) for row in detail.evaluation["outstanding_fields"] if row["action"] == "input"}
    for envelope in detail.resolved_inputs["field_requests"]:
        batch_keys = {(row["unit_key"], row["field_contract"]["field_key"]) for row in envelope["value"]["items"]}
        if missing & batch_keys:
            _, created = create_production_field_child(preparation_id=parent.pk, expected_preparation_revision=evaluated.revision,
                field_batch_key=envelope["sha256"], attempt_number=1, actor=actor)
            # Only our own successful child creation touched the parent once.
            # A fresh DB revision would hide a concurrent field change while
            # this loop still uses the earlier outstanding-field decision.
            evaluated.revision += int(created)
    return _record_wait(evaluated, actor) or evaluated
