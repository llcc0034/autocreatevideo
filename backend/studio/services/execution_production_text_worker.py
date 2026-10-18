"""Frozen text calls inside the original production text worker.

Successful responses create review candidates, not selected stage outputs.
Unknown billed cost keeps its reservation; estimates never become actuals.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.utils import timezone

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionRun, ScriptSource
from studio.services.execution_asset_budget import _money
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
from studio.services.execution_production_candidates import new_production_text_candidate, save_production_text_candidate
from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
from studio.services.execution_auxiliary_requests import read_attempt_field_batch
from studio.services.execution_auxiliary_results import save_pending_auxiliary_fields
from studio.services.execution_preparation_owners import locked_preparation_owner_rows
from studio.services.execution_preparations import read_production_attempt_request
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_production_dispatch import begin_production_execution_submission
from studio.services.execution_text_contracts import parse_execution_text_response
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.provider_registry import ProviderDriverError, driver_for, safe_chat_failure_details, safe_chat_failure_message, safe_chat_usage
from studio.services.script_sources import create_pasted_script_source


def _candidate(attempt, stage, run, parsed, *, recovery=None):
    """Original Artifact/ScriptRevision facts, independently addressable per candidate."""
    # Store the parsed value here; original model text has its response owner.
    # Rendered script text remains in ScriptRevision, not duplicated metadata.
    maximum = execution_configuration_max_bytes()
    if "output_contract" in parsed:
        from studio.services.script_semantic_candidates import save_script_semantic_candidate
        return save_script_semantic_candidate(attempt, stage, run, response_text=parsed["text"],
            max_bytes=maximum, recovery=recovery)
    artifact, metadata, contract = new_production_text_candidate(attempt, stage, run,
        result={"result": parsed["result"], "warnings": parsed["warnings"]}, max_bytes=maximum, recovery=recovery)
    if attempt.operation in {"script.generate", "script.structure"}:
        source, revision = create_pasted_script_source(project=run.project, user=run.owner, content=parsed["text"],
            source_format=ScriptSource.SourceFormat.PLAIN, source_type=ScriptSource.SourceType.PASTE if recovery else ScriptSource.SourceType.AI_GENERATED)
        revision.origin_generation_attempt = attempt
        revision.save(update_fields=("origin_generation_attempt", "updated_at"))
        metadata.update(script_source_id=str(source.pk), script_revision_id=str(revision.pk))
        artifact.kind = "script"
        artifact.object_key = f"script-revisions/{revision.pk}"
        artifact.mime_type, artifact.size_bytes, artifact.sha256 = "text/plain; charset=utf-8", revision.content_size_bytes, revision.content_sha256
    else:
        artifact.kind = "other"
        artifact.object_key = f"generated-artifacts/{artifact.pk}/manifest_metadata/result"
        artifact.mime_type, artifact.size_bytes, artifact.sha256 = "application/json", contract.size_bytes, contract.sha256
    save_production_text_candidate(artifact, metadata, max_bytes=maximum)
    return contract.sha256


def finish_production_text(attempt, *, status, code="", response=None, parsed=None, provider_error=None,
        rejected_response=None, validation_error=None, validation_phase=""):
    """Claim-CAS settlement under original root locks, including late results."""
    with locked_preparation_owner_rows([("stage_run_id", attempt.stage_run_id)]) as owners:
        stage = owners[("stage_run_id", str(attempt.stage_run_id))]
        preparations = {row.pk: row for row in ExecutionPreparation.objects.select_for_update().filter(
            pk__in=[attempt.preparation_id, attempt.parent_preparation_id], stage_run=stage).order_by("id")}
        current = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk)
        if current.claim_token != attempt.claim_token or current.status not in {"running", "submitting", "submitted", "cancel_requested"}:
            return current
        run = ProductionRun.objects.get(pk=stage.production_run_id)
        parent = preparations.get(current.parent_preparation_id)
        late = (current.ignore_late_result or current.status == "cancel_requested"
            or run.status in {"succeeded", "failed", "cancelled"} or stage.status == "cancelled"
            or (parent is not None and (parent.status == "cancelled" or parent.request_frozen_at is not None)))
        if late:
            # Stopping local work cannot turn an unknown remote result into
            # confirmed cancellation. Preserve the submission and reservation.
            uncertain = code == "uncertain_remote_response" and response is None
            status, code, parsed = ("failed", "uncertain_remote_response", None) if uncertain else ("cancelled", "late_result_ignored", None)
            current.ignore_late_result = True
        actual = _money(response.get("actual_cost")) if response is not None else None
        if actual is not None:
            currency = response.get("currency")
            if not isinstance(currency, str) or not currency or currency != current.cost_currency:
                raise ValidationError("实际费用币种与本次预留不一致，需要核对原任务")
            if actual and run.cost_currency and run.cost_currency != currency:
                raise ValidationError("实际费用币种与本轮预算不一致，不能自动换算")
            if current.committed_cost is None or current.committed_cost > run.committed_cost_total:
                raise ValidationError("任务预留与运行账目不一致")
            run.committed_cost_total -= current.committed_cost
            run.actual_cost_total += actual
            if actual:
                run.cost_currency = currency
            current.actual_cost, current.committed_cost = actual, Decimal("0")
        elif (current.submission_started_at is None or (not current.is_external and status == "failed"
                and code not in {"uncertain_remote_response", "actual_cost_reconciliation_required"})) and current.committed_cost is not None:
            if current.committed_cost > run.committed_cost_total:
                raise ValidationError("任务释放预留与运行账目不一致")
            run.committed_cost_total -= current.committed_cost
            current.committed_cost = Decimal("0")
        if response is not None:
            request_id = response.get("request_id", "")
            if not isinstance(request_id, str) or len(request_id) > 191:
                raise ValidationError("远端请求标识无效，不能截断后记录成功")
            current.provider_request_id, current.submitted_at = request_id, timezone.now()
        review_text, review_diagnostics = None, None
        if (not late and validation_error is not None
                and isinstance(rejected_response, dict) and isinstance(rejected_response.get("text"), str)):
            from studio.services.execution_text_response_review import response_validation_diagnostic
            review_text = rejected_response["text"]
            review_diagnostics = response_validation_diagnostic(validation_error, rejected_response)
        elif (not late and status == "succeeded"
                and isinstance(rejected_response, dict) and isinstance(rejected_response.get("text"), str)):
            # The parsed candidate is not the original model response. Keep
            # both successful primary and auxiliary bodies without adopting.
            review_text = rejected_response["text"]
            auxiliary = current.role == "auxiliary"
            review_diagnostics = {"schema_version": 1,
                "contract_error_code": "response_fields_recorded" if auxiliary else "response_candidate_recorded",
                "summary": "已保存实际返回和待审核字段；生成成功不代表人工确认或采用。" if auxiliary else
                    "已保存模型原始返回与解析后的候选；生成成功不代表已确认或采用。",
                "warnings": parsed["warnings"]}
        elif (not late and isinstance(provider_error, ProviderDriverError) and not provider_error.submission_uncertain
                and safe_chat_failure_details(provider_error).get("contract_error_code") in {
                    "response_output_budget_exhausted", "request_text_capacity_rejected"}
                and isinstance(getattr(provider_error, "partial_text", None), str)):
            review_text = provider_error.partial_text
            review_diagnostics = {"schema_version": 1, **safe_chat_failure_details(provider_error)}
        if review_text is not None:
            from studio.models import ExecutionTextResponseReview
            from studio.services.execution_response_storage import store_response
            try:
                key, size, digest = store_response(review_text)
            except ExecutionResponseStorageError:
                # The call happened, but its body was not retained. No candidate
                # or pending fields may claim success, and no second call occurs.
                status, code = "failed", "execution_response_storage_failed"
            else:
                review = ExecutionTextResponseReview(attempt=current, original_file=key, original_size=size, original_sha256=digest)
                assign_json_contract(review, "diagnostics", review_diagnostics,
                    max_bytes=execution_configuration_max_bytes())
                review.save()
                if status != "succeeded":
                    status, code = "awaiting_review", "execution_output_review_required"
        if status == "succeeded":
            if current.role == "auxiliary":
                batch = read_attempt_field_batch(current)
                save_pending_auxiliary_fields(parent, current, batch, parsed, run.owner)
                current.result_sha256 = json_contract(parsed["result"], max_bytes=execution_configuration_max_bytes()).sha256
            else:
                current.result_sha256 = _candidate(current, stage, run, parsed)
        current.status, current.error_code = status, code
        current.error_message = "" if not code else "本次调用未形成可采用候选，请查看原任务；配方按冻结配置判断自动重试或转人工处理。"
        if (not late and code == "execution_request_invalid" and current.submission_started_at is None
                and validation_error is not None):
            from studio.task_center_public import public_value
            phases = {"request": "冻结请求校验", "fields": "辅助字段批次校验", "admission": "提交前准入校验"}
            explanation = public_value("；".join(validation_error.messages))
            if len(explanation) > 2000:
                explanation = explanation[:2000] + "（校验说明过长，此处仅显示前2000字符）"
            current.error_message = f"{phases.get(validation_phase, '提交前校验')}失败，尚未提交模型：{explanation}"
        if isinstance(provider_error, ProviderDriverError) and status in {"failed", "uncertain"}:
            current.error_message = safe_chat_failure_message(provider_error)
        if code == "execution_response_text_missing":
            current.error_message = "已收到服务响应，但没有有效文本正文；不能伪造可修正内容。配方按冻结次数重试，耗尽后请人工检查服务配置。"
        if code == "execution_response_storage_failed":
            current.error_message = "模型已经返回，但原始正文未能保存；没有可读取的原文或候选。请检查文件存储，不会自动重试。"
        if status == "awaiting_review":
            current.error_message = (review_diagnostics["summary"] if validation_error is not None else
                "服务商拒绝输出参数或上下文容量，已保留原输入。请调整系统额度或输入后明确新建候选，不会自动重试。"
                if review_diagnostics.get("contract_error_code") == "request_text_capacity_rejected" else
                "本次输出长度不足，已保留返回正文，等待人工补全或处理；不会自动重试。")
        current.finished_at = current.heartbeat_at = timezone.now()
        current.claim_token = None
        current.save()
        if response is not None:
            # Receipt belongs to this attempt even when parsing fails or a late
            # response is ignored. Token counts never settle actual money.
            from studio.services.text_generation_queue import append_attempt_resolution
            append_attempt_resolution(current, outcome="response_received",
                evidence={"schema_version": 1, "preparation_id": str(current.preparation_id),
                    "request_sha256": current.request_sha256, "usage": safe_chat_usage(response.get("usage"))},
                note="服务商响应已返回；仅记录白名单token用量，不代表内容审核或实际扣费。")
        if (not late and status == "failed" and isinstance(provider_error, ProviderDriverError)
                and provider_error.code == "provider_response_invalid" and not provider_error.submission_uncertain):
            from studio.services.text_generation_queue import append_attempt_resolution
            append_attempt_resolution(current, outcome="known_response_rejected",
                evidence={"schema_version": 1, "preparation_id": str(current.preparation_id),
                    "request_sha256": current.request_sha256, **safe_chat_failure_details(provider_error)},
                note="模型响应未通过文本传输校验；未采用候选。配方恢复以冻结次数及原失败证据判断。")
        if parent is not None:
            ExecutionPreparation.objects.filter(pk=parent.pk, request_frozen_at__isnull=True).exclude(status="cancelled").update(next_check_at=timezone.now())
        run.save(update_fields=("actual_cost_total", "committed_cost_total", "cost_currency", "updated_at"))
        if status == "succeeded" and actual is not None:
            from studio.services.execution_resource_policy import pause_production_actual_overage
            pause_production_actual_overage(run, stage)
        if code == "uncertain_remote_response" and not late:
            stage.status, stage.reason_code, stage.reason = "blocked", code, current.error_message
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status, run.blocked_code, run.blocked_reason = "blocked", code, current.error_message
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
        # Wake the original recipe executor only after the candidate commits.
        # That executor owns review/selection; manual work remains untouched.
        from studio.services.production_run_auto_advance import schedule_recipe_run_advance
        schedule_recipe_run_advance(run.pk)
        return current


def process_prepared_production_text(attempt):
    if attempt.operation not in {*PRODUCTION_TEXT_OPERATIONS, *FIELD_OPERATION_MODES}:
        raise ValidationError("需要生产文本或字段辅助能力及原worker有效领取凭据")
    token = attempt.claim_token
    attempt.refresh_from_db()
    if attempt.status in {"succeeded", "failed", "cancelled", "awaiting_review"}:
        return attempt
    if token is None:
        raise ValidationError("需要原worker有效领取凭据")
    if attempt.claim_token != token or attempt.status not in {"running", "submitting", "submitted", "cancel_requested"}:
        return attempt
    if attempt.submission_started_at is not None:
        return finish_production_text(attempt, status="failed", code="uncertain_remote_response")
    if attempt.ignore_late_result or attempt.status == "cancel_requested" or attempt.stage_run.production_run.status in {"succeeded", "failed", "cancelled"}:
        return finish_production_text(attempt, status="cancelled", code="cancelled_before_submission")
    response, billing = None, None
    validation_phase = "request"
    try:
        _, definition, transport = read_production_attempt_request(attempt)
        validation_phase = "fields"
        batch = read_attempt_field_batch(attempt) if attempt.role == "auxiliary" else None
        validation_phase = "admission"
        allowed, attempt = begin_production_execution_submission(attempt)
        if not allowed:
            return attempt
        from studio.services.chat_http_evidence import observe_chat_http_body, record_production_chat_body
        with observe_chat_http_body(lambda facts: record_production_chat_body(attempt, facts)):
            response = driver_for(attempt.model_deployment.provider).invoke(attempt.model_deployment, "chat_completion", transport.value["payload"])
        if not isinstance(response, dict) or not isinstance(response.get("request_id", ""), str) or len(response.get("request_id", "")) > 191:
            raise ValidationError("远端响应标识无效，不能截断后记录成功")
        actual = _money(response.get("actual_cost"))
        if actual is not None and response.get("currency") != attempt.cost_currency:
            raise ValidationError("远端账单币种不一致")
        billing = {"request_id": response.get("request_id", ""), "actual_cost": actual, "currency": response.get("currency"),
            "usage": safe_chat_usage(response.get("usage"))}
        try:
            parsed = parse_execution_text_response(attempt.operation, response, field_request=batch,
                operation_plan=transport.value.get("operation_plan"), request_payload=transport.value["payload"],
                expected_output=definition.value["interface"].get("business_output"))
        except ValidationError as exc:
            # Keep the provider's final text separately from audit metadata.
            # A parser rejection is human-recoverable, not a reason to call again.
            if not isinstance(response.get("text"), str):
                return finish_production_text(attempt, status="failed", code="execution_response_text_missing", response=billing)
            return finish_production_text(attempt, status="failed", code="execution_response_invalid",
                response=billing, rejected_response=response, validation_error=exc)
        return finish_production_text(attempt, status="succeeded", response=billing, parsed=parsed,
            rejected_response=response)
    except OperationalError:
        raise  # Original claim recovery must not repeat a possibly sent POST.
    except ProviderDriverError as exc:
        return finish_production_text(attempt, status="failed", code="uncertain_remote_response" if exc.submission_uncertain else "execution_provider_failed", provider_error=exc)
    except ValidationError as exc:
        return finish_production_text(attempt, status="failed",
            code="execution_response_invalid" if response is not None else "execution_request_invalid", response=billing,
            rejected_response=response, validation_error=exc, validation_phase=validation_phase)
