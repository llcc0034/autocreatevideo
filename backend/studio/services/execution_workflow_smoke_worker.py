"""Prepared Comfy execution in the original WorkflowSmokeAttempt queue."""
from datetime import timedelta
import hashlib

from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django.utils import timezone

from studio.models import ExecutionPreparation, ModelDeployment, WorkflowSmokeAttempt
from studio.services.configuration import resolve_configuration
from studio.services.execution_deadlines import expired_kind, meaningful_deadline_updates
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_preparations import read_frozen_execution_request, preparation_execution_definition
from studio.services.execution_smoke_dispatch import begin_smoke_execution_submission, check_smoke_execution_staging
from studio.services.execution_smoke_evidence import finish_prepared_smoke
from studio.services.execution_capacity_observations import capacity_observation_request
from studio.services.execution_workflow_outputs import verify_comfy_primary_output
from studio.services.execution_workflow_staging import stage_execution_workflow_media, ExecutionWorkflowUploadClient
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import driver_for, ProviderDriverError, _comfyui_failure_message
from studio.services.security_boundaries import boundary_int


def _schedule(attempt, token, *, remote_status, submitted=False):
    runtime = resolve_configuration(required_keys=("workflow.smoke_poll_interval_seconds", "workflow.smoke_idle_timeout_seconds")).values
    if not isinstance(remote_status, str) or len(remote_status) > 120:
        raise ValidationError("ComfyUI 状态回执无效")
    with transaction.atomic():
        ModelDeployment.objects.select_for_update().get(pk=attempt.deployment_id)
        current = WorkflowSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
        if current.claim_token != token:
            return current
        now = timezone.now()
        if submitted:
            current.provider_request_id, current.submitted_at = attempt.provider_request_id, now
        if current.status != "cancel_requested":
            current.status = "running"
        current.remote_status = remote_status
        for key, value in meaningful_deadline_updates(current, marker=f"status:{remote_status}",
                idle_seconds=runtime["workflow.smoke_idle_timeout_seconds"], now=now).items():
            setattr(current, key, value)
        current.heartbeat_at, current.last_polled_at = now, now
        current.poll_count += int(not submitted)
        current.next_poll_at = now + timedelta(seconds=runtime["workflow.smoke_poll_interval_seconds"])
        current.claim_token, current.worker_id, current.claimed_at = None, "", None
        current.save()
        return current


def _stage(attempt, preparation, detail, definition, transport, driver):
    def check_active():
        # This also renews the original lease while processing/uploading media.
        updated = WorkflowSmokeAttempt.objects.filter(pk=attempt.pk, claim_token=attempt.claim_token,
            status="queued", submission_started_at__isnull=True).update(heartbeat_at=timezone.now())
        if updated != 1:
            raise ValidationError("烟测已取消、提交或租约变化，不能继续上传")

    def persist(receipts):
        json_contract(receipts, max_bytes=execution_configuration_max_bytes())
        with transaction.atomic():
            current = WorkflowSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
            if current.claim_token != attempt.claim_token or current.status != "queued" or current.submission_started_at is not None:
                raise ValidationError("上传回执不能写入已变化的验收任务")
            current.staged_inputs = receipts
            current.save(update_fields=("staged_inputs", "updated_at"))

    staged = stage_execution_workflow_media(definition=definition,
        resolved_inputs=json_contract(detail.resolved_inputs["inputs"], max_bytes=execution_resolved_inputs_max_bytes()),
        transport=transport, owner=preparation.created_by, root_id=attempt.pk,
        client=ExecutionWorkflowUploadClient(driver, attempt.deployment), staged_inputs=attempt.staged_inputs,
        persist_staged_inputs=persist, check_active=check_active,
        max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes", minimum=1))
    return {"workflow": staged.graph, "submission_token": str(preparation.pk)}


def process_prepared_workflow_smoke(attempt):
    token = attempt.claim_token
    if token is None or attempt.scope not in {"instance", "resource"}:
        raise ValidationError("工作流验收必须由原worker持有效租约执行")
    attempt.refresh_from_db()
    if attempt.claim_token != token or attempt.status not in {"queued", "submitting", "running", "cancel_requested"}:
        return attempt
    terminal_observed = False

    def finish(status, code="", *, message="", **facts):
        return finish_prepared_smoke(attempt, token, status=status, code=code,
            capacity_request=capacity_observation_request(attempt) if status == "succeeded" and attempt.scope == "instance" else None,
            message=message or ("" if not code else "工作流验收未通过，请查看错误码和原任务；不会自动重新提交。"),
            evidence=json_contract({"schema_version": 1, "execution_submitted": attempt.submission_started_at is not None,
                "request_id_sha256": hashlib.sha256(attempt.provider_request_id.encode()).hexdigest() if attempt.provider_request_id else "",
                "estimated_cost": str(attempt.estimated_cost) if attempt.estimated_cost is not None else None,
                "currency": attempt.cost_currency, **facts}, max_bytes=execution_configuration_max_bytes()))

    try:
        if attempt.status == "submitting":
            return finish("uncertain", "submission_uncertain")
        prep = ExecutionPreparation.objects.get(workflow_smoke=attempt)
        detail, definition, transport = read_frozen_execution_request(prep)
        current = preparation_execution_definition(prep, detail=detail, current=True)
        if current.sha256 != definition.sha256:
            raise ValidationError("验收目标已变化，不能使用新端点处理旧请求")
        compiled = transport.value["payload"]["compiled_workflow"]
        output_contract, semantics = compiled["output_contract"], compiled["resolved_semantics"]
        from studio.services.execution_workflow_outputs import frozen_comfy_output_prefix
        prefix = frozen_comfy_output_prefix(compiled, preparation_id=prep.pk)
        if expired_kind(attempt) and attempt.status != "cancel_requested":
            if attempt.status == "queued":
                return finish("failed", "smoke_deadline_expired_before_submission")
            WorkflowSmokeAttempt.objects.filter(pk=attempt.pk, claim_token=token).update(
                status="cancel_requested", cancel_requested_at=timezone.now(), updated_at=timezone.now())
            attempt.refresh_from_db()
        driver = driver_for(attempt.deployment.provider)
        if attempt.status == "queued":
            allowed, attempt = check_smoke_execution_staging(attempt)
            if not allowed:
                return attempt
            payload = _stage(attempt, prep, detail, definition, transport, driver)
            allowed, attempt = begin_smoke_execution_submission(attempt)
            if not allowed:
                return attempt
            response = driver.invoke(attempt.deployment, "submit", payload)
            request_id = response.get("request_id") if isinstance(response, dict) else None
            if request_id != str(prep.pk):
                return finish("uncertain", "comfyui_submission_token_mismatch",
                    received_id_sha256=hashlib.sha256(request_id.encode()).hexdigest() if isinstance(request_id, str) else None)
            attempt.provider_request_id = request_id
            return _schedule(attempt, token, remote_status="submitted", submitted=True)
        if attempt.provider_request_id != str(prep.pk):
            return finish("uncertain", "comfyui_submission_token_mismatch")
        if attempt.status == "cancel_requested":
            hard = resolve_configuration(required_keys=("workflow.smoke_hard_timeout_seconds",)).values["workflow.smoke_hard_timeout_seconds"]
            if attempt.cancel_requested_at and timezone.now() >= attempt.cancel_requested_at + timedelta(seconds=hard):
                return finish("uncertain", "smoke_cancel_timeout")
            if attempt.cancel_submitted_at is None:
                marked = WorkflowSmokeAttempt.objects.filter(pk=attempt.pk, claim_token=token, status="cancel_requested",
                    cancel_submitted_at__isnull=True).update(cancel_submitted_at=timezone.now(), updated_at=timezone.now())
                if marked != 1:
                    attempt.refresh_from_db()
                    return attempt
                driver.invoke(attempt.deployment, "cancel", {"request_id": attempt.provider_request_id})
                return _schedule(attempt, token, remote_status="cancel_requested")
        response = driver.invoke(attempt.deployment, "poll", {"request_id": attempt.provider_request_id})
        remote = response.get("status") if isinstance(response, dict) else None
        if remote not in {"queued", "running", "succeeded", "failed", "cancelled", "missing"}:
            raise ValidationError("ComfyUI 没有返回可识别的执行状态")
        if remote == "missing":
            if attempt.status == "cancel_requested" and attempt.cancel_submitted_at is not None:
                terminal_observed = True
                return finish("cancelled", "remote_cancelled")
            return finish("uncertain", "comfyui_request_missing")
        if remote in {"failed", "cancelled"}:
            terminal_observed = True
            return finish(remote, "smoke_remote_terminated",
                message=_comfyui_failure_message(response.get("failure"), graph=compiled["graph"]) if remote == "failed" else "")
        if remote != "succeeded":
            return _schedule(attempt, token, remote_status=remote)
        terminal_observed = True
        collected = driver.invoke(attempt.deployment, "collect", {"request_id": attempt.provider_request_id})
        _source, facts = verify_comfy_primary_output(collected, output_contract=output_contract,
            expected_kind=output_contract["primary"]["kind"], expected_prefix=prefix, resolved_semantics=semantics,
            max_bytes=resolve_configuration(required_keys=("artifact.max_bytes",)).values["artifact.max_bytes"])
        if output_contract["primary"]["kind"] == "text":
            from studio.services.execution_auxiliary_requests import read_preparation_operation_field_request
            from studio.services.execution_text_contracts import parse_execution_text_response
            batch = read_preparation_operation_field_request(prep, definition, detail)
            parsed = parse_execution_text_response(transport.value["operation"], {"text": facts["text"]}, field_request=batch)
            facts = {key: value for key, value in facts.items() if key != "text"}
            facts["result"], facts["warnings"] = parsed["result"], parsed["warnings"]
        receipts = json_contract(WorkflowSmokeAttempt.objects.values_list("staged_inputs", flat=True).get(pk=attempt.pk),
            max_bytes=execution_configuration_max_bytes())
        return finish("succeeded", artifact_facts=[facts], graph_sha256=compiled["graph_sha256"],
            staged_inputs_sha256=receipts.sha256, staged_input_count=len(receipts.value))
    except OperationalError:
        raise
    except ProviderDriverError as exc:
        # The driver distinguishes a definite rejected submit from a lost
        # response. A later poll/collect failure cannot establish that the
        # already accepted remote request failed or was never submitted.
        rejected_submit = (attempt.submission_started_at is not None and attempt.submitted_at is None
            and not attempt.provider_request_id and not exc.submission_uncertain)
        uncertain = attempt.submission_started_at is not None and not terminal_observed and not rejected_submit
        return finish("uncertain" if uncertain else "failed", exc.code)
    except ValidationError:
        return finish("uncertain" if attempt.submission_started_at is not None and not terminal_observed else "failed", "smoke_transport_or_result_invalid")
    except Exception:
        return finish("uncertain" if attempt.submission_started_at is not None and not terminal_observed else "failed", "smoke_worker_error")
