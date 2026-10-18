"""Exact prepared transport execution inside the original Smoke worker.

No provider calls from views, no resource snapshots from combination results,
and no resubmission after the original single-submit boundary was crossed.
"""
import hashlib

from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.utils import timezone

from studio.models import ExecutionPreparation, ModelDeploymentSmokeAttempt
from studio.services.execution_auxiliary_requests import read_preparation_operation_field_request
from studio.services.execution_deadlines import expired_kind
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparations import read_frozen_execution_request, preparation_execution_definition
from studio.services.execution_smoke_dispatch import begin_smoke_execution_submission
from studio.services.execution_smoke_evidence import finish_prepared_smoke
from studio.services.execution_capacity_observations import capacity_observation_request
from studio.services.execution_text_contracts import parse_execution_text_response, TEXT_EXECUTION_OPERATIONS
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import driver_for, ProviderDriverError, safe_chat_failure_details, safe_chat_failure_message, safe_chat_usage


def process_prepared_deployment_smoke(attempt):
    token = attempt.claim_token
    if token is None or attempt.scope not in {"instance", "resource"}:
        raise ValidationError("验收必须由原worker持有效租约执行")
    attempt.refresh_from_db()
    if attempt.claim_token != token:
        return attempt
    if attempt.deployment.provider.driver_type == "async_rest":
        return _process_async_prepared_smoke(attempt, token)
    response = None
    response_manifest = None
    response_usage = {}
    phase = "request"
    def finish(status, code="", *, message="", **evidence):
        return finish_prepared_smoke(attempt, token, status=status, code=code,
            capacity_request=capacity_observation_request(attempt) if status == "succeeded" and attempt.scope == "instance" else None,
            message=message or ("" if not code else "验收未完成有效通过，请查看对应错误码和原任务；不会自动重复调用。"),
            evidence=json_contract({"schema_version": 1, "execution_submitted": attempt.submission_started_at is not None,
                "estimated_cost": str(attempt.estimated_cost) if attempt.estimated_cost is not None else None,
                "currency": attempt.cost_currency,
                **({"response_body": response_manifest} if response_manifest else {}),
                **({"usage": response_usage} if response_usage else {}),
                **evidence}, max_bytes=execution_configuration_max_bytes()))
    if attempt.status in {"submitting", "submitted", "cancel_requested"}:
        return finish("uncertain", "submission_uncertain")
    if attempt.status != "queued":
        return attempt
    if expired_kind(attempt):
        return finish("failed", "smoke_deadline_expired_before_submission")
    try:
        prep = ExecutionPreparation.objects.get(deployment_smoke=attempt)
        detail, definition, transport = read_frozen_execution_request(prep)
        from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
        if attempt.request_sha256 != prep.request_sha256 or attempt.operation not in (*TEXT_EXECUTION_OPERATIONS, *NATIVE_TEXT_OPERATIONS):
            raise ValidationError("原验收任务与冻结文本请求不一致")
        batch = read_preparation_operation_field_request(prep, definition, detail)
        allowed, current = begin_smoke_execution_submission(attempt)
        attempt = current
        if not allowed:
            return attempt
        from studio.services.chat_http_evidence import observe_chat_http_body, record_smoke_chat_body
        with observe_chat_http_body(lambda facts: record_smoke_chat_body(attempt, prep.pk, facts)):
            response = driver_for(attempt.deployment.provider).invoke(attempt.deployment, "chat_completion", transport.value["payload"])
        response_usage = safe_chat_usage(response.get("usage")) if isinstance(response, dict) else {}
        phase = "response_storage"
        from studio.services.execution_smoke_responses import retain_smoke_response
        response_manifest = retain_smoke_response(attempt, response)
        phase = "response_metadata"
        request_id = response.get("request_id", "") if isinstance(response, dict) else ""
        if not isinstance(request_id, str):
            return finish("uncertain", "invalid_provider_request_id")
        request_id_sha = hashlib.sha256(request_id.encode()).hexdigest() if request_id else ""
        ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk, claim_token=token).update(
            provider_request_id=request_id if len(request_id) <= 191 else "", provider_request_id_sha256=request_id_sha,
            submitted_at=timezone.now(), heartbeat_at=timezone.now(), updated_at=timezone.now())
        if len(request_id) > 191:
            return finish("uncertain", "provider_request_id_too_long", provider_request_id_sha256=request_id_sha)
        phase = "response_validation"
        parsed = parse_execution_text_response(attempt.operation, response, field_request=batch,
            operation_plan=transport.value.get("operation_plan"), request_payload=transport.value["payload"],
            expected_output=definition.value["interface"].get("business_output"))
        encoded = response["text"].encode("utf-8")
        phase = "evidence_finalization"
        result, warnings = parsed["result"], parsed["warnings"]
        if attempt.operation in {"script.generate", "script.structure"}:
            from studio.services.execution_smoke_responses import script_smoke_validation_summary
            result = script_smoke_validation_summary(result)
            warnings = [f'{row["message"]}（共 {row["count"]} 处）' for row in result["warnings"]]
        return finish("succeeded", response_sha256=hashlib.sha256(encoded).hexdigest(), response_size_bytes=len(encoded),
            result=result, warnings=warnings, field_request_sha256=batch.sha256 if batch else None,
            provider_request_id_sha256=request_id_sha)
    except OperationalError:
        # Original worker recovery decides whether a persisted submission was
        # crossed; a DB failure cannot trigger another provider call here.
        raise
    except ProviderDriverError as exc:
        return finish("uncertain" if exc.submission_uncertain else "failed", exc.code,
            message=safe_chat_failure_message(exc), **safe_chat_failure_details(exc))
    except ValidationError as exc:
        if phase == "response_validation":
            from studio.services.execution_text_response_review import response_validation_diagnostic
            diagnostic = response_validation_diagnostic(exc, response if isinstance(response, dict) else {})
            # Smoke is immutable evidence, not the editable production response review.
            summary = "返回内容未通过校验；请在任务详情查看原始输出和校验原因，修改配置后明确新建烟测。"
            diagnostic["summary"] = diagnostic["summary"].replace(
                "原始正文已保留；请核对并人工修正，不会自动重试。", "请核对原始输出和配置，不会自动重试。")
            return finish("failed", diagnostic["validation_code"], message=diagnostic["summary"] + " " + summary,
                failure_phase=phase, diagnostics=diagnostic)
        if phase == "response_storage":
            return finish("failed", "smoke_response_storage_failed",
                message="模型已返回，但原始响应未能安全保存；请检查正式存储或文本大小边界，不会自动重跑。", failure_phase=phase)
        if phase == "evidence_finalization":
            return finish("failed", "smoke_evidence_invalid", message="响应已解析，但验收证据未通过一致性检查；不能记为烟测通过，也不会自动重跑。", failure_phase=phase)
        return finish("failed", "smoke_request_invalid", message="冻结请求未通过执行前校验；请核对输入与实例配置。", failure_phase=phase)
    except OSError:
        if phase == "response_storage":
            return finish("failed", "smoke_response_storage_failed",
                message="模型已返回，但正式响应存储不可用；本次没有自动重试。", failure_phase=phase)
        raise
    except Exception:
        return finish("uncertain" if attempt.submission_started_at is not None else "failed", "smoke_worker_error")


def _process_async_prepared_smoke(attempt, token):
    """Original restricted REST submit/poll/collect and verified media probe."""
    from datetime import timedelta
    from django.db import transaction
    from studio.models import ModelDeployment
    from studio.services.deployment_smoke import smoke_artifact_max_bytes
    from studio.services.deployment_smoke_queue import _release_schedule, _config, async_smoke_deadline_seconds
    from studio.services.remote_artifacts import verify_remote_artifacts_for_smoke

    def finish(status, code="", *, actual_cost=None, currency="", **facts):
        return finish_prepared_smoke(attempt, token, status=status, code=code,
            capacity_request=capacity_observation_request(attempt) if status == "succeeded" and attempt.scope == "instance" else None,
            message="" if not code else "异步验收未通过，请查看原任务原因；不自动重复提交。",
            evidence=json_contract({"schema_version": 1, "execution_submitted": attempt.submission_started_at is not None,
                "request_id_sha256": attempt.provider_request_id_sha256,
                "estimated_cost": str(attempt.estimated_cost) if attempt.estimated_cost is not None else None,
                "estimated_currency": attempt.cost_currency, "actual_cost": actual_cost, "actual_currency": currency, **facts},
                max_bytes=execution_configuration_max_bytes()), actual_cost=actual_cost, cost_currency=currency)

    terminal_observed = False
    try:
        if attempt.status not in {"queued", "submitting", "submitted", "cancel_requested"}:
            return attempt
        if attempt.status == "submitting":
            return finish("uncertain", "submission_uncertain")
        prep = ExecutionPreparation.objects.get(deployment_smoke=attempt)
        _detail, definition, transport = read_frozen_execution_request(prep)
        if attempt.request_sha256 != prep.request_sha256:
            raise ValidationError("异步验收请求摘要不一致")
        # A changed endpoint cannot be used to poll/cancel an old remote ID.
        current = preparation_execution_definition(prep, detail=_detail, current=True)
        if current.sha256 != definition.sha256:
            raise ValidationError("异步验收执行目标已变化")
        driver = driver_for(attempt.deployment.provider)
        if expired_kind(attempt) and attempt.status != "cancel_requested":
            if attempt.status == "queued":
                return finish("failed", "smoke_deadline_expired_before_submission")
            ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk, claim_token=token).update(
                status="cancel_requested", cancel_requested_at=timezone.now(), updated_at=timezone.now())
            attempt.refresh_from_db()
        if attempt.status == "queued":
            allowed, attempt = begin_smoke_execution_submission(attempt)
            if not allowed:
                return attempt
            response = driver.invoke(attempt.deployment, "submit", transport.value["payload"])
            request_id = response.get("request_id") if isinstance(response, dict) else None
            if not isinstance(request_id, str) or not request_id or len(request_id) > 191:
                return finish("uncertain", "invalid_provider_request_id")
            with transaction.atomic():
                ModelDeployment.objects.select_for_update().get(pk=attempt.deployment_id)
                current = ModelDeploymentSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
                if current.claim_token != token:
                    return current
                current.provider_request_id, current.provider_request_id_sha256 = request_id, hashlib.sha256(request_id.encode()).hexdigest()
                current.submitted_at = timezone.now()
                if current.status != "cancel_requested":
                    current.status = "submitted"
                current.save(update_fields=("provider_request_id", "provider_request_id_sha256", "submitted_at", "status", "updated_at"))
            return _release_schedule(current, token, remote_status=response.get("status", "submitted"))
        if not attempt.provider_request_id:
            return finish("uncertain", "missing_provider_request_id")
        if attempt.status == "cancel_requested":
            if attempt.cancel_requested_at and timezone.now() >= attempt.cancel_requested_at + timedelta(seconds=async_smoke_deadline_seconds(attempt.deployment)):
                return finish("uncertain", "smoke_cancel_timeout")
            if attempt.cancel_submitted_at is None:
                # The cancel POST is also single-attempt. A crash after marking
                # it resumes polling instead of blindly issuing another cancel.
                ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk, claim_token=token).update(cancel_submitted_at=timezone.now(), updated_at=timezone.now())
                driver.invoke(attempt.deployment, "cancel", {"request_id": attempt.provider_request_id})
                return _release_schedule(attempt, token, remote_status="cancel_requested")
        response = driver.invoke(attempt.deployment, "poll", {"request_id": attempt.provider_request_id})
        if not isinstance(response, dict):
            raise ValidationError("远端状态响应必须是对象")
        if response.get("terminal_cancelled") or response.get("terminal_failure"):
            terminal_observed = True
            return finish("cancelled" if response.get("terminal_cancelled") else "failed", "smoke_remote_terminated")
        if not response.get("terminal_success"):
            return _release_schedule(attempt, token, remote_status=response.get("status", ""), progress_fraction=response.get("progress_fraction"))
        terminal_observed = True
        collected = driver.invoke(attempt.deployment, "collect", {"request_id": attempt.provider_request_id})
        kind = transport.value["output"]["kind"]
        artifacts = verify_remote_artifacts_for_smoke(deployment_id=attempt.deployment_id, artifacts=collected.get("artifacts"),
            provider_config=_config(attempt.deployment), expected_kind=kind, max_bytes=smoke_artifact_max_bytes(kind))
        return finish("succeeded", artifact_facts=artifacts, actual_cost=collected.get("actual_cost"), currency=collected.get("currency", ""))
    except OperationalError:
        raise
    except (ValidationError, ProviderDriverError):
        crossed = attempt.submission_started_at is not None
        return finish("uncertain" if crossed and not terminal_observed else "failed", "smoke_transport_or_result_invalid")
    except Exception:
        return finish("uncertain" if attempt.submission_started_at is not None and not terminal_observed else "failed", "smoke_worker_error")
