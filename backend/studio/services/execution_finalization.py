"""Freeze one prepared request; never invoke a provider or enqueue a task.

Root consumers must supply their registered, read-only admission fact adapter.
This internal transaction is not an API that accepts client-provided evidence,
budget approval, capacity coverage or a skip-checks flag.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.services.execution_admission import ExecutionEvidenceIdentity, evaluate_execution_admission
from studio.services.execution_definitions import execution_definition_version
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_policy_configuration import read_execution_policies
from studio.services.execution_preparation_fields import _locked_preparations, locked_execution_field_selection_snapshot
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_preparations import read_execution_preparation_detail, preparation_execution_definition
from studio.services.execution_request_storage import request_document, store_prepared_request
from studio.services.json_contracts import JsonContract, assign_json_contract, json_contract, verify_json_contract
from studio.services.registry_reference_lock import registry_reference_write_guard
from studio.services.security_boundaries import security_boundary_resolution_cache


@dataclass(frozen=True)
class ExecutionAdmissionFacts:
    """Actual root checks and observations, produced by an internal adapter.

    It must check original root permissions, budget, concurrency, idempotency
    and unresolved remote attempts under the held root lock; no network I/O.
    root_contract records its precise check/evidence references, not secrets.
    """
    smoke_facts: tuple
    capacity_state: str
    hard_blockers: list
    root_contract: JsonContract


def _conflict(message):
    raise ValidationError(message, code="execution_preparation_conflict")


def _frozen_replay(preparation, expected):
    detail = read_execution_preparation_detail(preparation)
    if preparation.request_frozen_at is None:
        return False
    if expected not in {preparation.revision, detail.prepared_request.get("basis_preparation_revision")}:
        _conflict("请求已冻结，但本次修订号不是其冻结来源；请重新读取")
    # A replay does not dispatch, recompile, re-admit or rewrite the historical
    # decision. The existing worker must check current admission before I/O.
    return True


def evaluate_current_prepared_admission(preparation, definition, request, collect_admission_facts):
    """Same current policy and exact facts for freeze and original dispatch.

    Called under root/preparation locks; no caller-selected pass/freshness or
    evidence shortcuts. The caller records the decision in its actual phase.
    """
    policies = read_execution_policies()
    facts = collect_admission_facts(preparation, definition, request)
    if not isinstance(facts, ExecutionAdmissionFacts) or not isinstance(facts.root_contract, JsonContract):
        raise ValidationError("准入必须返回正式根任务检查事实与证据，不能传入布尔放行标志")
    root = verify_json_contract(facts.root_contract.value, schema_version=facts.root_contract.schema_version,
        size_bytes=facts.root_contract.size_bytes, sha256=facts.root_contract.sha256,
        max_bytes=execution_configuration_max_bytes())
    if not isinstance(root.value, dict) or root.value.get("preparation_id") != str(preparation.pk) or root.value.get("request_sha256") != request.sha256:
        raise ValidationError("根任务检查必须覆盖本次精确准备与最终请求")
    frozen = preparation_execution_definition(preparation)
    if frozen.sha256 != definition.sha256:
        _conflict("准入定义不属于本次真实准备")
    hard_blockers = list(facts.hard_blockers)
    from studio.services.execution_resource_policy import read_resource_policy, resource_review, REVIEW_REQUIRED
    resource_policy = read_resource_policy()
    review = resource_review(preparation, request, root.value, resource_policy)
    if review is not None:
        root = json_contract({**root.value, "resource_review": review}, max_bytes=execution_configuration_max_bytes())
        if review["issues"] and not review["confirmed"]:
            hard_blockers.append({"code": REVIEW_REQUIRED, "message": "本次资源用量需要人工确认；已保存输入和结果，不会自动重试。"})
    if preparation.execution_instance_version_id is not None:
        version = execution_definition_version(preparation.execution_instance_version_id)
        if version.status != "published" or not version.instance.is_active:
            hard_blockers.insert(0, {"code": "execution_instance_inactive", "message": "执行实例已停用或版本已退役，不能开始新的调用"})
    resource = preparation.purpose == "resource_smoke"
    result = evaluate_execution_admission(identity=ExecutionEvidenceIdentity(
        None if resource else str(preparation.execution_instance_version_id), definition.sha256, definition.value["target_fingerprint"],
        definition.value["configuration"]["operation"], definition.value["configuration"]["mode"], "resource_smoke" if resource else "instance_smoke"),
        smoke_facts=facts.smoke_facts, purpose=preparation.purpose,
        smoke_policy=policies.smoke_policy, capacity_policy=policies.capacity_policy,
        capacity_state=facts.capacity_state, hard_blockers=hard_blockers, now=timezone.now())
    if read_execution_policies().configuration.sha256 != policies.configuration.sha256:
        _conflict("准入配置在检查期间变化，请重新推进")
    if read_resource_policy().sha256 != resource_policy.sha256:
        _conflict("资源审核策略在检查期间变化，请重新推进")
    result["awaiting_resource_confirmation"] = any(row["code"] == REVIEW_REQUIRED for row in result["blockers"])
    return policies, result, root


@security_boundary_resolution_cache()
def finalize_execution(*, preparation_id, expected_preparation_revision, actor, collect_admission_facts):
    """Compile outside locks, then recheck/freeze under one ordered transaction.

    The required collector is a trusted root adapter, not a HTTP parameter.
    Queue integration supplies it only after completing its real root gates.
    Until those adapters are wired, this function has no dispatch/continue API.
    """
    if type(expected_preparation_revision) is not int or expected_preparation_revision < 1:
        raise ValidationError("必须提交明确的准备修订号")
    if not callable(collect_admission_facts):
        raise ValidationError("缺少正式根任务准入事实适配器，不能标记请求已就绪")
    with _locked_preparations([preparation_id], actor) as rows:
        preparation = rows[str(preparation_id)]
        if _frozen_replay(preparation, expected_preparation_revision):
            return preparation
    preview = compile_prepared_execution_transport(preparation_id=preparation_id,
        expected_preparation_revision=expected_preparation_revision, actor=actor)
    # No target-only root lock remains while we acquire all cross-preparation
    # source roots. PostgreSQL savepoints do not release transaction locks.
    with registry_reference_write_guard():
        with locked_execution_field_selection_snapshot(preparation_id=preparation_id,
                expected_preparation_revision=preview.preparation_revision, actor=actor) as (preparation, selected):
            detail = read_execution_preparation_detail(preparation)
            if preparation.status == "cancelled":
                _conflict("准备已取消，不能冻结或恢复")
            if preparation.intent_sha256 != preview.intent_sha256 or selected.sha256 != preview.field_selections.sha256:
                _conflict("准备意图或字段选择在编译期间变化，请重新读取")
            if any(row["required"] for row in selected.value["unselected_fields"]):
                raise ValidationError("必需字段尚未明确采用", code="execution_fields_incomplete")
            definition = preparation_execution_definition(preparation, detail=detail, current=True)
            if definition.sha256 != preview.transport.value["definition_sha256"]:
                _conflict("执行定义与本次编译请求不一致")
            request = request_document({"schema_version": 1, "preparation_id": str(preparation.pk),
                "basis_preparation_revision": preparation.revision, "intent_sha256": preparation.intent_sha256,
                "field_selections": selected.value, "transport": preview.transport.value})
            policies, result, root = evaluate_current_prepared_admission(preparation, definition, request, collect_admission_facts)
            now = timezone.now()
            preparation.revision += 1
            preparation.evaluation_revision, preparation.evaluated_at = preparation.revision, now
            preparation.status = "ready" if result["can_submit"] else "blocked"
            if result["awaiting_resource_confirmation"]:
                preparation.status = "awaiting_review"
            first = result["blockers"][0] if result["blockers"] else {"code": "", "message": ""}
            preparation.reason_code, preparation.reason = first["code"], first["message"]
            evaluation = {"schema_version": 1, "phase": "admission", "preparation_revision": preparation.revision,
                "basis_preparation_revision": preview.preparation_revision, "evaluated_at": now.isoformat(),
                "status": preparation.status, "reason_code": first["code"], "reason": first["message"],
                "candidate_request_sha256": request.sha256, "configuration": policies.configuration.value,
                "root_checks": root.value, "admission": result}
            assign_json_contract(detail, "evaluation", evaluation, max_bytes=execution_configuration_max_bytes())
            if result["can_submit"]:
                store_prepared_request(detail, request.value)
                preparation.request_sha256, preparation.request_frozen_at = request.sha256, now
                preparation.next_check_at = now
            else:
                preparation.next_check_at = None if result["awaiting_resource_confirmation"] else now + timedelta(seconds=policies.recheck_interval_seconds)
            # Validate widths/status constraints before either model is saved;
            # the enclosing transaction rolls back both on any storage error.
            preparation.full_clean()
            detail.clean()
            preparation.save()
            detail.save()
            # Field validation may have populated the reverse OneToOne cache
            # before this write. Return the just-persisted detail, not that old
            # in-memory object with a null request/evaluation.
            preparation.contract = detail
            return preparation
