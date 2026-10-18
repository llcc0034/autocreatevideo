"""Read safe text-rejection facts from the original task, never its parent.

This is detail-only presentation, not readiness, retry policy or a result store.
None means no recorded text-rejection diagnostic, not that a task succeeded.
"""
from django.core.exceptions import ValidationError

from studio.models import GenerationAttempt, ExecutionTextResponseReview
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import verify_json_contract
from studio.services.provider_registry import ProviderDriverError, safe_chat_failure_details
from studio.services.security_boundaries import boundary_int


REJECTION_HELP = {
    "response_output_budget_exhausted": "模型已用完本次输出长度额度，未返回完整可用正文。请核对实例的输出长度参数和输入内容，再明确创建新候选。",
    "response_content_filtered": "服务商过滤了本次文本响应。请检查提交内容与服务商规则，再明确创建新候选。",
    "response_missing_text": "服务商返回的响应中没有可用正文，请核对模型接口和返回格式。",
    "response_tool_call_without_text": "模型只返回工具调用，没有平台需要的文本正文。请核对实例的接口与请求参数。",
    "response_reasoning_without_text": "模型只返回推理内容，没有最终正文。请核对模型能力和输出长度参数。",
}


def _presentation(task, observed_at, evidence):
    error = ProviderDriverError("", code="provider_response_invalid")
    error.metadata = evidence
    safe = safe_chat_failure_details(error)
    items = []
    if "finish_reason" in safe:
        items.append({"label": "服务商结束原因", "value": safe["finish_reason"]})
    for key, label in (("prompt_tokens", "输入 token"), ("completion_tokens", "输出 token"), ("total_tokens", "总 token")):
        if key in safe.get("usage", {}):
            items.append({"label": label, "value": str(safe["usage"][key])})
    return {"state": "recorded", "task_id": task.pk, "observed_at": observed_at,
        "summary": REJECTION_HELP.get(safe.get("contract_error_code"), "本次模型响应未通过文本校验，没有形成可用正文。请核对原任务和实例配置。"),
        "details": items}


def read_preparation_response_diagnostic(preparation):
    if not preparation.request_frozen_at:
        return None
    task = None
    try:
        if preparation.stage_run_id:
            # A field child has its own preparation. Its result must not be
            # shown as a failure of the main preparation receiving the fields.
            task = GenerationAttempt.objects.filter(preparation=preparation).order_by("-attempt_number", "-id").only(
                "id", "status", "error_code", "stage_run_id", "request_sha256", "finished_at").first()
            if task is None or task.status != "failed" or task.error_code != "execution_provider_failed":
                if (task is not None and task.status == "failed" and task.error_code == "execution_response_invalid"
                        and not ExecutionTextResponseReview.objects.filter(attempt=task).exists()):
                    return {"state": "unverifiable", "task_id": task.pk, "observed_at": None,
                        "summary": "本次历史调用的实际返回正文及具体校验原因当时未保存，无法恢复；不是模型返回了空内容。可人工补填字段，或明确重试此批次（会产生新调用）。", "details": []}
                return None
            resolution = task.resolutions.filter(outcome="known_response_rejected").order_by("-revision").only(
                "id", "attempt_id", "created_at", "evidence", "evidence_schema_version", "evidence_size_bytes", "evidence_sha256").first()
            if resolution is None:
                return None
            evidence = verify_json_contract(resolution.evidence, schema_version=resolution.evidence_schema_version,
                size_bytes=resolution.evidence_size_bytes, sha256=resolution.evidence_sha256,
                max_bytes=boundary_int("security.snapshot.text_max_bytes")).value
            if (not isinstance(evidence, dict) or task.stage_run_id != preparation.stage_run_id
                    or not task.finished_at or task.request_sha256 != preparation.request_sha256
                    or evidence.get("preparation_id") != str(preparation.pk)
                    or evidence.get("request_sha256") != preparation.request_sha256):
                raise ValidationError("响应诊断与本次准备不一致")
            return _presentation(task, resolution.created_at, evidence)
        if preparation.deployment_smoke_id:
            task = preparation.deployment_smoke
            if task.status != "failed" or task.error_code != "provider_response_invalid":
                return None
            from studio.services.execution_smoke_evidence import read_prepared_smoke_fact
            read_prepared_smoke_fact(task)
            result = verify_json_contract(task.result, schema_version=task.result_schema_version,
                size_bytes=task.result_size_bytes, sha256=task.result_sha256,
                max_bytes=execution_configuration_max_bytes()).value
            return _presentation(task, task.finished_at, result["evidence"])
        return None
    except ValidationError:
        # Do not substitute old successful facts or raw exceptions. Other
        # preparation details remain usable; the original fact stays unchanged.
        return {"state": "unverifiable", "task_id": task.pk if task else None, "observed_at": None,
            "summary": "本次文本响应诊断无法核验，不能据此判断具体失败原因。请核对原任务记录；不会自动重跑。", "details": []}
