"""Instance Smoke actions on the two original attempt owners.

Creation resolves immutable fixture facts only; common preparation advancement
and original workers own admission, queueing and external calls.
"""
from copy import deepcopy
from contextlib import ExitStack
import hashlib
import re

from django.core.exceptions import PermissionDenied, ValidationError

from studio.models import ModelDeployment, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt
from studio.services.execution_definitions import execution_definition_version, stored_execution_contract, verify_current_execution_definition
from studio.services.execution_instances import ExecutionResourceSelection, execution_configuration_max_bytes, execution_resolved_inputs_max_bytes, selectable_execution_versions
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_fields import create_manual_execution_field, locked_execution_field_selection_snapshot, select_execution_field
from studio.services.execution_preparations import create_execution_instance_preparation, create_resource_smoke_preparation, read_execution_preparation_detail, ResolvedExecutionPreparation
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


def _no_parameter_source(_source):
    raise ValidationError("独立验收没有生产步骤参数来源，请明确填写实例开放的运行参数")


def read_instance_smoke_reuse_fixture(*, preparation_id, expected_preparation_revision, actor):
    """Read the exact source choice for a new, separately authorized Smoke.

    No model lookup, redraw, approval, task allocation or refreshed evidence.
    The create action revalidates this revision and the current target later.
    """
    if not actor or not actor.is_active or not actor.is_staff:
        raise PermissionDenied("导出组合烟测输入需要有效管理员身份")
    with locked_execution_field_selection_snapshot(preparation_id=preparation_id,
            expected_preparation_revision=expected_preparation_revision, actor=actor) as (prep, snapshot):
        if prep.execution_instance_version_id is None:
            raise ValidationError("基础资源验收不能转成执行实例组合证据")
        detail = read_execution_preparation_detail(prep)
        requested = detail.intent["request"]["requested"]
        keys = ("inputs", "parameters", "manual_channels", "subject_facts", "source_language", "target_language")
        if any(key not in requested for key in keys):
            raise ValidationError("原准备缺少完整的显式输入，不能从当前配置重建")
        if "appearance_target" in requested:
            keys += ("appearance_target",)
        return json_contract({"execution_instance_version_id": str(prep.execution_instance_version_id),
            "expected_definition_sha256": detail.intent["request"]["definition_sha256"],
            "reuse_preparation_id": str(prep.pk), "expected_reuse_revision": prep.revision,
            "fixture": {key: deepcopy(requested[key]) for key in keys},
            "field_selections": [{key: row[key] for key in ("source_preparation_id", "field_value_id", "unit_key", "field_key")}
                for row in snapshot.value["fields"]]}, max_bytes=execution_request_max_bytes()).value


def create_instance_smoke(*, version_id, **kwargs):
    return _create_smoke(version_id=version_id, resource_selection=None, **kwargs)


def create_resource_smoke(*, selection: ExecutionResourceSelection, **kwargs):
    """Internal management action; the HTTP adapter owns its resource scope."""
    if not isinstance(selection, ExecutionResourceSelection):
        raise ValidationError("基础资源验收必须明确选择原资源，不能伪造执行实例")
    return _create_smoke(version_id=None, resource_selection=selection, **kwargs)


@registry_reference_writer
@security_boundary_resolution_cache()
def _create_smoke(*, version_id, resource_selection, expected_definition_sha256, fixture, field_values,
        client_idempotency_key, allow_external_calls, actor, budget_limit=None,
        reuse_preparation_id=None, expected_reuse_revision=None, field_selections=None, reviewed_source_context=None):
    resource = resource_selection is not None
    if resource and (reuse_preparation_id is not None or expected_reuse_revision is not None or field_selections is not None):
        raise ValidationError("基础资源验收使用明确fixture，不能借用实例准备身份")
    if not actor or not actor.is_active or not actor.is_staff:
        raise PermissionDenied("执行实例验收需要有效管理员身份")
    if not isinstance(client_idempotency_key, str) or not client_idempotency_key.strip() or len(client_idempotency_key) > 191:
        raise ValidationError("需要明确且不超过191字符的Idempotency-Key")
    if not isinstance(expected_definition_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_definition_sha256):
        raise ValidationError("需要本次验收的精确实例定义摘要")
    if type(allow_external_calls) is not bool or not isinstance(fixture, dict) or not isinstance(field_values, list):
        raise ValidationError("需要明确的夹具、字段列表和外部调用授权")
    required = {"inputs", "parameters", "manual_channels", "subject_facts", "source_language", "target_language"}
    if not required.issubset(fixture) or set(fixture) - required - {"appearance_target"}:
        raise ValidationError("验收夹具必须完整声明输入、参数、提示词通道、主体和语言")
    if "appearance_target" in fixture and reuse_preparation_id is None:
        raise ValidationError("验收外观声明只能复用原准备，不能新增或重选主图及差异来源")
    for row in field_values:
        if not isinstance(row, dict) or set(row) != {"unit_key", "field_key", "text"}:
            raise ValidationError("初始字段只能提交单元、字段和正文；不能伪造来源、批准或选择")
    if budget_limit is not None:
        raise ValidationError("烟测资源提醒由系统配置统一管理，不再接受单次预算")
    budget = None
    requested = {**deepcopy(fixture), "smoke_request": {"field_values": deepcopy(field_values),
        "allow_external_calls": allow_external_calls, "budget_limit": str(budget) if budget is not None else None}}
    from studio.services.execution_smoke_sources import context_request, reviewed_sources_requested, lock_reviewed_smoke_context
    reviewed_source_context = context_request(reviewed_source_context)
    if reviewed_source_context is not None:
        if resource or reuse_preparation_id is not None or not reviewed_sources_requested(fixture["inputs"]):
            raise ValidationError("审核来源上下文仅用于新建组合烟测的已审核字段；复用准备使用原冻结输入")
        requested["smoke_request"]["reviewed_source_context"] = reviewed_source_context
    elif reuse_preparation_id is None and reviewed_sources_requested(fixture["inputs"]):
        raise ValidationError("请明确选择已审核输出的来源生产轮次及参考步骤")
    if reuse_preparation_id is None:
        if expected_reuse_revision is not None or field_selections is not None:
            raise ValidationError("复用修订和字段选择必须明确指定来源准备")
    else:
        if type(expected_reuse_revision) is not int or expected_reuse_revision < 1 or not isinstance(field_selections, list) or field_values:
            raise ValidationError("复用准备需要精确修订及明确字段选择；不能同时新建人工字段")
        for row in field_selections:
            if not isinstance(row, dict) or set(row) != {"source_preparation_id", "field_value_id", "unit_key", "field_key"}:
                raise ValidationError("复用字段必须明确来源准备、字段ID、单元及字段键")
            if any(not isinstance(value, str) or not value for value in row.values()):
                raise ValidationError("复用字段身份必须是明确文本，不能推断缺失来源")
        requested["smoke_request"]["reuse"] = {"preparation_id": str(reuse_preparation_id),
            "expected_revision": expected_reuse_revision, "field_selections": deepcopy(field_selections)}
    intent = json_contract(requested, max_bytes=execution_request_max_bytes())
    version = resource_selection if resource else execution_definition_version(version_id)
    model = WorkflowSmokeAttempt if version.workflow_version_id else ModelDeploymentSmokeAttempt
    scope = "resource" if resource else "instance"
    key = f"{scope}-smoke:{actor.pk}:{hashlib.sha256(client_idempotency_key.encode()).hexdigest()}"
    existing = model.objects.filter(scope=scope, idempotency_key=key,
        **({"deployment_id": version.deployment_id} if resource else {"execution_instance_version": version})).first()
    if existing:
        prep = existing.execution_preparations.get()
        detail = read_execution_preparation_detail(prep)
        if (prep.created_by_id != actor.pk or detail.intent["request"]["requested"] != intent.value
                or detail.intent["request"]["definition_sha256"] != expected_definition_sha256):
            raise ValidationError("该幂等键已用于不同的实例验收请求", code="execution_preparation_conflict")
        if resource:
            from studio.services.execution_resource_smoke_definition import read_resource_smoke_definition
            saved = read_resource_smoke_definition(existing).value["configuration"]
            selected = {"deployment_id": str(version.deployment_id), "operation": version.operation, "mode": version.mode,
                "output_version_id": str(version.output_version_id) if version.output_version_id else None,
                "prompt_mode": version.prompt_mode, "prompt_profile_version_id": str(version.prompt_profile_version_id) if version.prompt_profile_version_id else None,
                "workflow_version_id": str(version.workflow_version_id) if version.workflow_version_id else None}
            if any(saved.get(key) != value for key, value in selected.items()):
                raise ValidationError("资源选择与原幂等请求不一致", code="execution_preparation_conflict")
        return existing, prep, False
    with ExitStack() as locks:
        reused, source_detail, chosen = None, None, []
        if reuse_preparation_id is not None:
            reused, snapshot = locks.enter_context(locked_execution_field_selection_snapshot(
                preparation_id=reuse_preparation_id, expected_preparation_revision=expected_reuse_revision, actor=actor))
            source_detail = read_execution_preparation_detail(reused)
            if reused.execution_instance_version_id != version.pk or source_detail.intent["request"]["definition_sha256"] != expected_definition_sha256:
                raise ValidationError("只能复用同一精确实例定义的准备")
            source_fixture = source_detail.intent["request"]["requested"]
            if any(source_fixture.get(name) != fixture[name] for name in required):
                raise ValidationError("复用验收必须使用原输入和参数；变更素材或参数应新建准备")
            if (("appearance_target" in source_fixture) != ("appearance_target" in fixture)
                    or source_fixture.get("appearance_target") != fixture.get("appearance_target")):
                raise ValidationError("复用验收必须保留原外观声明，不能遗漏或更改主图及差异来源")
            available = {row["field_value_id"]: row for row in snapshot.value["fields"]}
            seen = set()
            for selection in field_selections:
                row = available.get(str(selection["field_value_id"]))
                if row is None or row["field_value_id"] in seen or any(str(selection[name]) != row[name] for name in selection):
                    raise ValidationError("只能明确复用来源准备实际已确认、已采用的字段修订")
                chosen.append(row)
                seen.add(row["field_value_id"])
        source_context = lock_reviewed_smoke_context(reviewed_source_context, actor=actor) if reviewed_source_context is not None else None
        # New allocation and completion use the original Deployment mutex.
        ModelDeployment.objects.select_for_update().get(pk=version.deployment_id)
        if not resource and not selectable_execution_versions(actor).filter(pk=version.pk).exists():
            raise ValidationError("请选择有权限执行的启用实例及已发布精确版本")
        if resource:
            from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
            if version.workflow_version_id:
                from studio.models import WorkflowTemplateVersion
                from studio.services.workflow_versions import verify_workflow_live_validation
                workflow = WorkflowTemplateVersion.objects.select_for_update().select_related("template").get(pk=version.workflow_version_id)
                version = ExecutionResourceSelection(version.deployment, version.operation, version.mode, version.prompt_mode,
                    version.prompt_profile_version, workflow)
                if version.workflow_version.status != WorkflowTemplateVersion.Status.VALIDATED:
                    raise ValidationError("基础工作流烟测必须先完成Live Validate，不能用草稿或实例验收代替")
                verify_workflow_live_validation(version.workflow_version, version.deployment)
            definition = build_resource_smoke_definition(version)
        else:
            definition = verify_current_execution_definition(stored_execution_contract(version.pk))
        if definition.sha256 != expected_definition_sha256:
            raise ValidationError("验收定义已变化，请重新读取", code="execution_preparation_conflict")
        if field_values and version.prompt_mode != "profile":
            raise ValidationError("人工完整提示词或无策略模式不能混入策略字段")
        matching = model.objects.filter(deployment_id=version.deployment_id)
        matching = matching.filter(workflow_version_id=version.workflow_version_id) if version.workflow_version_id else matching.filter(operation=version.operation)
        if matching.filter(status__in=("preparing", "queued", "submitting", "submitted", "running", "cancel_requested")).exists():
            raise ValidationError("该执行资源已有未结束的验收，请查看原任务", code="execution_preparation_conflict")
        uncertain = matching.filter(status="uncertain")
        unresolved = (uncertain.filter(resolutions__isnull=True) if version.workflow_version_id
            else uncertain.without_operator_resolution())
        if unresolved.exists():
            raise ValidationError("原验收远端状态未确定，请先在原任务中消歧", code="execution_preparation_conflict")
        attempt = model(scope=scope, execution_instance_version=None if resource else version, deployment=version.deployment,
            status="preparing", target_sha256=definition.value["target_fingerprint"], idempotency_key=key,
            allow_external_calls=allow_external_calls, budget_limit=budget)
        if version.workflow_version_id:
            workflow = version.workflow_version
            attempt.workflow_version, attempt.created_by = workflow, actor
            attempt.graph_sha256, attempt.execution_contract_sha256 = workflow.graph_sha256, workflow.execution_contract_sha256
            attempt.validation_evidence_sha256 = workflow.validation_evidence_sha256
            # Only an immutable fixture identity here. Effective media, parameters
            # and field choices live once in Preparation, not in a second snapshot.
            if resource:
                from dataclasses import asdict
                assign_json_contract(attempt, "fixture_values", {"schema_version": 3, "resource_definition": asdict(definition)},
                    schema_version=3, max_bytes=execution_configuration_max_bytes())
            else:
                assign_json_contract(attempt, "fixture_values", {"schema_version": 1, "scope": "instance_smoke", "intent_sha256": intent.sha256},
                    max_bytes=execution_configuration_max_bytes())
            attempt.fixture_sha256 = attempt.fixture_values_sha256
        else:
            attempt.operation, attempt.request_sha256 = version.operation, ""
            if resource:
                from dataclasses import asdict
                attempt.request_snapshot = {"schema_version": 2, "resource_definition": asdict(definition)}
                attempt.prompt_profile_version = version.prompt_profile_version
                attempt.prompt_profile_contract_sha256 = version.prompt_profile_version.contract_sha256 if version.prompt_profile_version_id else ""
        attempt.save()
        def resolve(definition, _root):
            if source_detail is not None:
                resolved = ResolvedExecutionPreparation(json_contract(deepcopy(source_detail.resolved_inputs), max_bytes=execution_resolved_inputs_max_bytes()),
                    json_contract({"schema_version": 1, "mode": "manual"}, max_bytes=execution_configuration_max_bytes()))
            else:
                resolved = resolve_execution_request_facts(definition, fixture, owner=actor, unit_key="smoke",
                    max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
                    review_contract=json_contract({"schema_version": 1, "mode": "manual"}, max_bytes=execution_configuration_max_bytes()),
                    parameter_policies=[], resolve_parameter_source=_no_parameter_source, scope_units={},
                    reviewed_smoke_context=source_context)
            from studio.services.execution_field_smoke import verify_instance_field_fixture
            verify_instance_field_fixture(definition, resolved.resolved_inputs.value["inputs"])
            return resolved
        owner_key = "workflow_smoke_id" if version.workflow_version_id else "deployment_smoke_id"
        create_preparation = create_resource_smoke_preparation if resource else create_execution_instance_preparation
        prep = create_preparation(owner_ref=(owner_key, attempt.pk), **({} if resource else {"version_id": version.pk}),
            expected_definition_sha256=definition.sha256, unit_key=reused.unit_key if reused is not None else "smoke", request_key=client_idempotency_key,
            intent=intent, resolve_facts=resolve, actor=actor)
        for row in field_values:
            create_manual_execution_field(preparation_id=prep.pk, **row, revision=1, expected_preparation_revision=prep.revision, actor=actor)
            prep.refresh_from_db()
        for row in chosen:
            select_execution_field(preparation_id=prep.pk, source_preparation_id=row["source_preparation_id"], field_value_id=row["field_value_id"],
                expected_preparation_revision=prep.revision, expected_selection_revision=None, replace=False, actor=actor)
            prep.refresh_from_db()
        prep = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=actor)
        from studio.services.execution_advancement import schedule_authorized_asset_fields
        schedule_authorized_asset_fields(prep)
        if resource and version.workflow_version_id:
            workflow.smoke_test_state = "not_run"
            workflow.smoke_test_message = "基础烟测准备中，请在原任务中确认输入并继续。"
            workflow.smoke_test_evidence, workflow.smoke_test_evidence_sha256 = {}, ""
            workflow.save(update_fields=("smoke_test_state", "smoke_test_message", "smoke_test_evidence",
                "smoke_test_evidence_sha256", "updated_at"))
        return attempt, prep, True
