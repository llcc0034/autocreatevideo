"""Explicit asset retries keep resolved facts/seeds and real field provenance.

A retry is a new original Job, not a reset of a remote submission record. It
starts preparing and must pass current admission before any new external call.
Changing input/version is a new creation, never a retry of this request.
"""
from copy import deepcopy
import hashlib

from django.core.exceptions import ValidationError

from studio.models import AssetGenerationJob, ExecutionFieldValue
from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes, selectable_execution_versions
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_fields import (
    _field_context, _verify_value, _touch, locked_execution_field_selection_snapshot, select_execution_field,
)
from studio.services.execution_preparations import (
    ResolvedExecutionPreparation, create_execution_instance_preparation, read_execution_preparation_detail,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.registry_reference_lock import registry_reference_writer


@registry_reference_writer
def retry_prepared_asset_job(job, *, client_idempotency_key):
    from studio.services.asset_generation import _asset_request_header, _TERMINAL_ASSET_STATUSES
    source = AssetGenerationJob.objects.select_related("owner").get(pk=job.pk)
    if source.parent_preparation_id:
        raise ValidationError("字段子调用必须在父准备中按明确批次重试，不能作为素材重新创建")
    if not isinstance(client_idempotency_key, str) or client_idempotency_key.strip() != client_idempotency_key or len(client_idempotency_key) > 191:
        raise ValidationError("重试幂等键必须是明确且不超过191字符的文本")
    key = client_idempotency_key or f"retry:{source.pk}"
    idempotency_key = f"asset-retry:{source.owner_id}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    observed = source.execution_preparations.get()
    # The common lock helper discovers ALL selected-source roots before locking
    # any root; do not hold only the source Job lock while entering it.
    with locked_execution_field_selection_snapshot(preparation_id=observed.pk,
            expected_preparation_revision=observed.revision, actor=source.owner) as (original, selected):
        source.refresh_from_db()
        detail = read_execution_preparation_detail(original)
        header = _asset_request_header(source)
        intent = json_contract({"action": "retry", "source_job_id": str(source.pk), "preparation_id": str(original.pk),
            "intent_sha256": original.intent_sha256, "request_sha256": original.request_sha256,
            "selected_fields_sha256": selected.sha256}, max_bytes=execution_configuration_max_bytes())
        existing = AssetGenerationJob.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.retry_of_id != source.pk or _asset_request_header(existing)["client_request_sha256"] != intent.sha256:
                raise ValidationError("该重试幂等键已用于另一个请求", code="execution_preparation_conflict")
            read_execution_preparation_detail(existing.execution_preparations.get())
            return existing, False
        if source.status not in {"failed", "cancelled"}:
            raise ValidationError("只有明确失败或已取消的任务可以重试；不确定任务必须先消歧")
        if source.claim_token or original.auxiliary_asset_jobs.exclude(status__in=_TERMINAL_ASSET_STATUSES).exists():
            raise ValidationError("原任务或辅助调用仍在途/不确定，不能重复调用", code="execution_preparation_conflict")
        # A retry intent consumes this exact source once. Terminal successors
        # still own its lineage: branching from an ancestor can duplicate a
        # success or bypass an in-flight/uncertain descendant. Same-key replay
        # above remains valid; another retry must explicitly target the leaf.
        if source.retries.exists():
            raise ValidationError("该请求已有重试后继，请读取并处理该任务；后继失败时须明确重试该失败任务", code="execution_preparation_conflict")
        if not selectable_execution_versions(source.owner).filter(pk=source.execution_instance_version_id).exists():
            raise ValidationError("原执行实例已停用、退役或无执行权限，请新建请求")
        definition = verify_current_execution_definition(stored_execution_contract(source.execution_instance_version_id))
        if definition.sha256 != header["definition_sha256"]:
            raise ValidationError("原实例定义已变化，不能替换配置进行重试")
        values = {name: getattr(source, name) for name in (
            "owner_id", "style_id", "execution_instance_version_id", "model_deployment_id", "workflow_template_version_id",
            "job_type", "media_type", "operation", "execution_kind", "image_category", "name", "description", "is_external",
            "allow_external_calls", "budget_limit",
        )}
        retry = AssetGenerationJob(**values, retry_of=source, status="preparing", idempotency_key=idempotency_key, prompt="")
        assign_json_contract(retry, "request_snapshot", {**header, "client_request_sha256": intent.sha256},
            max_bytes=execution_configuration_max_bytes())
        retry.clean()
        retry.save()
        def reuse_facts(_definition, _owner):
            # These were verified by the original creation and by the shared
            # preparation verifier. Never reinterpret generated seeds as user
            # values or rerun the first-request initializer here.
            return ResolvedExecutionPreparation(json_contract(deepcopy(detail.resolved_inputs), max_bytes=execution_resolved_inputs_max_bytes()),
                json_contract(deepcopy(detail.review_contract), max_bytes=execution_configuration_max_bytes()))
        target = create_execution_instance_preparation(owner_ref=("asset_job_id", retry.pk), version_id=retry.execution_instance_version_id,
            expected_definition_sha256=definition.sha256, unit_key=original.unit_key, request_key=key,
            intent=json_contract(detail.intent["request"]["requested"], max_bytes=execution_resolved_inputs_max_bytes()),
            resolve_facts=reuse_facts, actor=source.owner)
        # Preserve unfinished editable work without approving or selecting it.
        # Model text keeps its real originating call, not a fake new AI result.
        revisions = {}
        pending = original.field_values.select_for_update().filter(status="pending").order_by("unit_key", "field_key", "revision")
        for value in pending:
            context = _field_context(target, value.unit_key, value.field_key)
            _verify_value(context, value)
            if value.origin_kind == "model":
                origin = value.origin_asset_job if value.origin_asset_job_id else value.origin_attempt
                if origin is None or origin.status != "succeeded":
                    raise ValidationError("待确认的模型字段缺少真实已完成调用，不能复制为重试输入")
            pair = (value.unit_key, value.field_key)
            revisions[pair] = revisions.get(pair, 0) + 1
            ExecutionFieldValue.objects.create(preparation=target, unit_key=value.unit_key, field_key=value.field_key,
                revision=revisions[pair], source_sha256=value.source_sha256, field_contract_sha256=value.field_contract_sha256,
                text=value.text, text_sha256=value.text_sha256, origin_kind=value.origin_kind,
                origin_attempt_id=value.origin_attempt_id, origin_asset_job_id=value.origin_asset_job_id,
                created_by_id=value.created_by_id, status="pending")
        if revisions:
            _touch(target)
        # Existing approvals remain on the original field records. Only the
        # explicit choices used by this source request are reused; no latest.
        for chosen in selected.value["fields"]:
            select_execution_field(preparation_id=target.pk, source_preparation_id=chosen["source_preparation_id"],
                field_value_id=chosen["field_value_id"], expected_preparation_revision=target.revision,
                expected_selection_revision=None, replace=False, actor=source.owner)
            target.refresh_from_db()
        evaluate_preparation_fields(preparation_id=target.pk, expected_preparation_revision=target.revision, actor=source.owner)
        from studio.services.project_translation_requests import copy_translation_sources_for_retry
        copy_translation_sources_for_retry(source, retry)
        return retry, True
