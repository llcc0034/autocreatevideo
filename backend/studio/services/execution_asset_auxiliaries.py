"""Original child Jobs for frozen field batches; no duplicate provider queue.

Allocation only prepares a child. Original admission/dispatch still owns budget
reservation and every real external call. Results are pending, never approved.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import AssetGenerationJob, AssetGenerationJobResolution, ExecutionPreparation
from studio.services.execution_auxiliary_requests import read_asset_field_batch, read_preparation_field_batch
from studio.services.execution_auxiliary_results import save_pending_auxiliary_fields, record_preparation_child_wait
from studio.services.execution_definitions import execution_definition_version, stored_execution_contract, verify_current_execution_definition
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_preparation_fields import _editable, _locked_preparations, _touch
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_policy_configuration import read_execution_policies
from studio.services.execution_preparation_owners import locked_preparation_owners, preparation_owner_ref, nonproduction_preparation_root, preparation_actor
from studio.services.execution_preparations import create_execution_instance_preparation, read_execution_preparation_detail
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_text_contracts import parse_execution_text_response
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


@registry_reference_writer
@security_boundary_resolution_cache()
def create_asset_field_child(*, preparation_id, expected_preparation_revision, field_batch_key, attempt_number, actor,
        acknowledged_uncertain_task_id=None):
    """Explicit batch/attempt allocation; a scanner may only request attempt 1.

    Missing leaf parameter values are configuration errors, not guessed token
    budgets. They must be configured in the published leaf's parameter policy.
    """
    from studio.services.asset_generation import _asset_request_header, _execution_asset_header, _is_external, _reject_asset_parameter_source

    if type(attempt_number) is not int or attempt_number < 1:
        raise ValidationError("需要明确的字段批次尝试序号")
    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        if parent.purpose == "resource_smoke" or parent.execution_instance_version_id is None:
            raise ValidationError("基础资源烟测使用明确字段夹具，不自动分配辅助执行实例")
        if parent.stage_run_id or (parent.asset_job_id and parent.asset_job.parent_preparation_id):
            raise ValidationError("字段分配需要真实素材或验收父准备", code="execution_interface_unavailable")
        root = nonproduction_preparation_root(parent)
        read_execution_preparation_detail(parent)
        batch, purpose, binding = read_preparation_field_batch(parent, field_batch_key)
        children = parent.auxiliary_asset_jobs.filter(auxiliary_purpose=purpose, field_batch_key=field_batch_key)
        existing = children.filter(attempt_number=attempt_number).first()
        if existing is not None:
            read_asset_field_batch(existing)
            _asset_request_header(existing)
            read_execution_preparation_detail(existing.execution_preparations.get())
            return existing, False
        if parent.revision != expected_preparation_revision:
            raise ValidationError("父准备已变化，请重新读取", code="execution_preparation_conflict")
        _editable(parent)
        if root.status != "preparing":
            raise ValidationError("父主任务不在准备阶段，不能新增辅助调用")
        previous = children.select_for_update().order_by("-attempt_number").first()
        acknowledged = (previous is not None and previous.status == "uncertain" and previous.claim_token is None
            and str(previous.pk) == str(acknowledged_uncertain_task_id))
        if acknowledged_uncertain_task_id is not None and not acknowledged:
            raise ValidationError("原未决调用已变化，请重新读取并确认", code="execution_preparation_conflict")
        if ((previous is None and attempt_number != 1) or (previous is not None and (
                attempt_number != previous.attempt_number + 1
                or (previous.status not in {"failed", "cancelled"} and not acknowledged) or previous.claim_token))):
            raise ValidationError("只允许在明确失败或取消后显式重试下一个批次；在途或不确定任务不能重发", code="execution_preparation_conflict")
        leaf = execution_definition_version(binding["version_id"])
        definition = verify_current_execution_definition(stored_execution_contract(leaf.pk))
        if definition.sha256 != binding["definition_sha256"] or definition.value["target_fingerprint"] != binding["target_fingerprint"]:
            raise ValidationError("辅助实例定义已偏离父准备的精确关联")
        if definition.value["configuration"]["prompt_mode"] != "none" or definition.value["interface"]["auxiliaries"]:
            raise ValidationError("字段辅助实例必须是无策略递归的叶子")
        external = _is_external(leaf.deployment)
        if external and not root.allow_external_calls:
            raise ValidationError("父请求尚未授权外部字段调用", code="execution_external_authorization_required")
        specs = definition.value["configuration"]["inputs"]
        if len(specs) != 1 or specs[0]["targets"] != [{"target_kind": "operation_input", "target_key": "field_request", "is_primary": True}]:
            raise ValidationError("字段叶子必须明确接收服务端字段批次，不能隐含其他输入")
        requested = {"inputs": {specs[0]["key"]: {"kind": "literal", "value": deepcopy(batch.value)}},
            "parameters": {}, "manual_channels": {}, "subject_facts": [], "source_language": "", "target_language": ""}
        identity = json_contract({"parent_preparation_id": str(parent.pk), "parent_intent_sha256": parent.intent_sha256,
            "field_batch_key": field_batch_key, "purpose": purpose, "attempt_number": attempt_number,
            "leaf": binding}, max_bytes=execution_configuration_max_bytes())
        child = AssetGenerationJob(owner=preparation_actor(parent), execution_instance_version=leaf, model_deployment=leaf.deployment,
            job_type="field", media_type="text", operation=leaf.operation, execution_kind="deployment", status="preparing",
            parent_preparation=parent, auxiliary_purpose=purpose, field_batch_key=field_batch_key, attempt_number=attempt_number,
            idempotency_key=f"asset-field:{parent.pk}:{identity.sha256}", prompt="", is_external=external,
            allow_external_calls=root.allow_external_calls, budget_limit=root.budget_limit)
        assign_json_contract(child, "request_snapshot", _execution_asset_header(definition, leaf.deployment, identity.sha256, "text").value,
            max_bytes=execution_configuration_max_bytes())
        child.clean()
        child.save()
        if acknowledged:
            evidence = json_contract({"schema_version": 1, "previous_task_id": str(previous.pk),
                "previous_status": previous.status, "previous_actual_cost": str(previous.actual_cost) if previous.actual_cost is not None else None,
                "parent_preparation_id": str(parent.pk), "parent_intent_sha256": parent.intent_sha256,
                "field_batch_key": field_batch_key, "attempt_number": attempt_number,
                "previous_attempt_number": previous.attempt_number}, max_bytes=execution_configuration_max_bytes())
            receipt = AssetGenerationJobResolution(job=child, revision=1,
                outcome=AssetGenerationJobResolution.Outcome.UNCERTAIN_RETRY_AUTHORIZED,
                idempotency_key="operator:uncertain_retry_authorized", request_sha256=evidence.sha256, decided_by=actor,
                note="明确授权保留原未决记录及未知费用，并新增同批次自动尝试；不是原调用终态或扣费核验，不人工填字段。")
            assign_json_contract(receipt, "evidence", evidence.value, max_bytes=execution_configuration_max_bytes())
            receipt.save()
        def resolve(definition, owner):
            return resolve_execution_request_facts(definition, requested, owner=owner.owner, unit_key=parent.unit_key,
                max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
                review_contract=json_contract({"schema_version": 1, "mode": "manual"}, max_bytes=execution_configuration_max_bytes()), parameter_policies=[],
                resolve_parameter_source=_reject_asset_parameter_source)
        create_execution_instance_preparation(owner_ref=("asset_job_id", child.pk), version_id=leaf.pk,
            expected_definition_sha256=definition.sha256, unit_key=parent.unit_key, request_key=f"field:{field_batch_key}:{attempt_number}",
            intent=json_contract(requested, max_bytes=execution_request_max_bytes()), resolve_facts=resolve, actor=actor)
        _touch(parent)
        return child, True


@security_boundary_resolution_cache()
def ingest_asset_field_result(*, job_id, actor):
    """Recover after a crash between original child completion and field save.

    Both root locks precede the parent Preparation/field locks. Validation and
    saving the full response are atomic; repeat reads return original IDs even
    after manual approval/selection or finalization. No cost is copied upward.
    """
    observed = AssetGenerationJob.objects.select_related("parent_preparation").get(pk=job_id)
    if not observed.parent_preparation_id:
        raise ValidationError("只有真实字段子任务可以保存父字段")
    parent_ref = preparation_owner_ref(observed.parent_preparation)
    with locked_preparation_owners([parent_ref, ("asset_job_id", observed.pk)], actor) as roots:
        child = roots[("asset_job_id", str(observed.pk))]
        parent = ExecutionPreparation.objects.select_for_update().get(pk=observed.parent_preparation_id)
        if child.parent_preparation_id != parent.pk or child.status != "succeeded":
            raise ValidationError("只接收本父准备已明确成功的字段子任务")
        batch = read_asset_field_batch(child)
        encoded = child.result_text.encode("utf-8")
        if child.result_text_sha256 != hashlib.sha256(encoded).hexdigest() or child.result_text_size_bytes != len(encoded):
            raise ValidationError("子任务结果正文与已保存摘要不一致")
        parsed = parse_execution_text_response(child.operation, {"text": child.result_text}, field_request=batch)
        return save_pending_auxiliary_fields(parent, child, batch, parsed, child.owner)


def schedule_asset_parent_preparation(job_id):
    """Post-commit wakeup only; the common advancer owns result ingestion."""
    parent_id = AssetGenerationJob.objects.filter(pk=job_id).values_list("parent_preparation_id", flat=True).get()
    if parent_id is not None:
        ExecutionPreparation.objects.filter(pk=parent_id).update(next_check_at=timezone.now())


def _record_child_wait(preparation_id, actor):
    children = list(AssetGenerationJob.objects.filter(parent_preparation_id=preparation_id)
        .order_by("auxiliary_purpose", "field_batch_key", "attempt_number").values(
            "id", "auxiliary_purpose", "field_batch_key", "attempt_number", "status"))
    return record_preparation_child_wait(preparation_id, actor, children)


@security_boundary_resolution_cache()
def reconcile_asset_preparation_fields(*, preparation_id, expected_preparation_revision, actor):
    """The asset field phase of the common advance path; never a remote call.

    Reconcile an existing child before allocating anything. A retry ordinal is
    only provided by an explicit retry action; normal advancement creates 1.
    Finalization, current admission and dispatch remain the coordinator's next
    phase, not an implicit success of this function.
    """
    from studio.services.asset_generation import request_asset_generation_cancel

    with _locked_preparations([preparation_id], actor) as rows:
        parent = rows[str(preparation_id)]
        if type(expected_preparation_revision) is not int or expected_preparation_revision != parent.revision:
            raise ValidationError("父准备已变化，请重新读取", code="execution_preparation_conflict")
        if parent.stage_run_id or (parent.asset_job_id and parent.asset_job.parent_preparation_id):
            raise ValidationError("此字段推进阶段只处理素材或验收根准备")
        root = nonproduction_preparation_root(parent)
        detail = read_execution_preparation_detail(parent)
        cancelled = parent.status == "cancelled" or root.status == "cancel_requested"
        if not cancelled and parent.request_frozen_at is not None:
            return parent
    # Drop the parent lock before child locks or source-selection locks.
    if cancelled:
        if parent.asset_job_id:
            finished = request_asset_generation_cancel(root)
        else:
            from studio.services.execution_smoke_dispatch import cancel_prepared_smoke
            finished = cancel_prepared_smoke(parent, actor)
        with _locked_preparations([preparation_id], actor) as rows:
            parent = rows[str(preparation_id)]
            parent.next_check_at = (timezone.now() + timedelta(seconds=read_execution_policies().recheck_interval_seconds)
                if finished.status == "cancel_requested" else None)
            parent.save(update_fields=("next_check_at", "updated_at"))
            return parent
    succeeded_ids = list(parent.auxiliary_asset_jobs.filter(status="succeeded").order_by("id").values_list("id", flat=True))
    for job_id in succeeded_ids:
        ingest_asset_field_result(job_id=job_id, actor=actor)
    # No scanner silently revives an earlier failed/uncertain batch. A later
    # explicit retry (if any) is the authoritative active attempt of that batch.
    waiting = _record_child_wait(parent.pk, actor)
    if waiting is not None:
        return waiting
    # A child can finish between the initial observation and the root check.
    for job_id in parent.auxiliary_asset_jobs.filter(status="succeeded").exclude(pk__in=succeeded_ids).values_list("id", flat=True):
        ingest_asset_field_result(job_id=job_id, actor=actor)
    parent.refresh_from_db()
    evaluated = evaluate_preparation_fields(preparation_id=parent.pk, expected_preparation_revision=parent.revision, actor=actor)
    detail = read_execution_preparation_detail(evaluated)
    missing = {(row["unit_key"], row["field_key"]) for row in detail.evaluation["outstanding_fields"] if row["action"] == "input"}
    # The asset root's explicit external-call authorization permits allocation;
    # each new child still requires its own current budget/Smoke admission.
    root.refresh_from_db()
    if missing and root.allow_external_calls:
        for envelope in detail.resolved_inputs["field_requests"]:
            batch_keys = {(row["unit_key"], row["field_contract"]["field_key"]) for row in envelope["value"]["items"]}
            if batch_keys & missing:
                evaluated.refresh_from_db()
                create_asset_field_child(preparation_id=evaluated.pk, expected_preparation_revision=evaluated.revision,
                    field_batch_key=envelope["sha256"], attempt_number=1, actor=actor)
        waiting = _record_child_wait(parent.pk, actor)
        if waiting is not None:
            return waiting
    return evaluated
