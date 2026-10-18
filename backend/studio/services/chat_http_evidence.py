"""Scoped, pre-network observation of Requests' serialized Chat body.

Only bytes/digest enter the original attempt audit. This is not a delivery
receipt, and neither headers nor the request body are persisted here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import re

from django.core.exceptions import ValidationError


_observer = ContextVar("chat_http_body_observer", default=None)


@contextmanager
def observe_chat_http_body(callback):
    if _observer.get() is not None or not callable(callback):
        raise ValidationError("Chat正文观察器必须属于单次明确调用")
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def current_chat_body_observer():
    return _observer.get()


def prepared_json_body_facts(request):
    if (request.method != "POST" or request.headers.get("Content-Type") != "application/json"
            or not isinstance(request.body, bytes) or not request.body):
        raise ValidationError("HTTP库没有生成可核验的Chat JSON字节正文")
    return {"schema_version": 1, "encoding": "requests.prepared_json/v1",
            "size_bytes": len(request.body), "sha256": sha256(request.body).hexdigest()}


def validate_body_facts(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "encoding", "size_bytes", "sha256"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["encoding"] != "requests.prepared_json/v1"
            or type(value["size_bytes"]) is not int or value["size_bytes"] < 1
            or not isinstance(value["sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is None):
        raise ValidationError("HTTP正文序列化证据结构无效")
    return value


def record_production_chat_body(attempt, facts):
    from studio.models import ExecutionPreparation, GenerationAttempt
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    from studio.services.text_generation_queue import append_attempt_resolution

    validate_body_facts(facts)
    with locked_preparation_owner_rows([("stage_run_id", attempt.stage_run_id)]) as owners:
        stage = owners[("stage_run_id", str(attempt.stage_run_id))]
        preparation = ExecutionPreparation.objects.select_for_update().get(pk=attempt.preparation_id, stage_run=stage)
        current = GenerationAttempt.objects.select_for_update().get(pk=attempt.pk)
        if (attempt.claim_token is None or current.claim_token != attempt.claim_token
                or current.status not in {"running", "submitting"} or current.submission_started_at is None
                or current.ignore_late_result or stage.status == "cancelled"
                or stage.production_run.status in {"succeeded", "failed", "cancelled"}
                or current.stage_run_id != stage.pk or current.preparation_id != preparation.pk
                or current.request_sha256 != preparation.request_sha256
                or current.request_sha256 != attempt.request_sha256):
            raise ValidationError("原调用领取状态或冻结请求已变化，不能发送")
        if current.resolutions.filter(outcome="http_body_prepared").exists():
            raise ValidationError("原调用已经记录HTTP正文；不得重复发送")
        append_attempt_resolution(current, outcome="http_body_prepared",
            evidence={"schema_version": 1, "preparation_id": str(preparation.pk),
                      "request_sha256": current.request_sha256, "body": facts},
            note="HTTP库完成JSON序列化，尚未交给网络发送；不是送达、扣费或业务成功证明。")


def record_smoke_chat_body(attempt, preparation_id, facts):
    from django.db.models import Max
    from studio.models import ExecutionPreparation, ModelDeploymentSmokeAttemptResolution
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    from studio.services.json_contracts import json_contract
    from studio.services.security_boundaries import boundary_int
    from studio.services.deployment_smoke_resolution import has_operator_resolution

    validate_body_facts(facts)
    with locked_preparation_owner_rows([("deployment_smoke_id", attempt.pk)]) as owners:
        current = owners[("deployment_smoke_id", str(attempt.pk))]
        prep = ExecutionPreparation.objects.select_for_update().get(pk=preparation_id, deployment_smoke=current)
        if (attempt.claim_token is None or current.claim_token != attempt.claim_token
                or current.status != "submitting" or current.submission_started_at is None
                or current.cancel_requested_at is not None or prep.status == "cancelled"
                or prep.request_frozen_at is None or not prep.request_sha256
                or current.request_sha256 != prep.request_sha256 or current.request_sha256 != attempt.request_sha256):
            raise ValidationError("原Smoke领取状态或冻结请求已变化，不能发送")
        if has_operator_resolution(current):
            raise ValidationError("原Smoke已有人工消歧；不能继续发送")
        if current.resolutions.filter(outcome="http_body_prepared").exists():
            raise ValidationError("原Smoke已经记录HTTP正文；不得重复发送")
        evidence = json_contract({"schema_version": 1, "preparation_id": str(prep.pk),
            "request_sha256": current.request_sha256, "body": facts}, max_bytes=boundary_int("security.snapshot.text_max_bytes"))
        ModelDeploymentSmokeAttemptResolution.objects.create(attempt=current,
            revision=(current.resolutions.aggregate(value=Max("revision"))["value"] or 0) + 1,
            outcome="http_body_prepared", decided_by=None,
            evidence=evidence.value, evidence_schema_version=evidence.schema_version,
            evidence_size_bytes=evidence.size_bytes, evidence_sha256=evidence.sha256,
            note="系统记录HTTP库JSON序列化摘要；不是人工消歧，也不是送达、扣费或Smoke通过证明。")


def record_asset_chat_body(job, preparation_id, request_sha256, facts):
    from django.db.models import Max
    from studio.models import AssetGenerationJobResolution, ExecutionPreparation
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    from studio.services.json_contracts import json_contract
    from studio.services.security_boundaries import boundary_int

    validate_body_facts(facts)
    with locked_preparation_owner_rows([("asset_job_id", job.pk)]) as owners:
        current = owners[("asset_job_id", str(job.pk))]
        prep = ExecutionPreparation.objects.select_for_update().get(pk=preparation_id, asset_job=current)
        if (job.claim_token is None or current.claim_token != job.claim_token
                or current.status != "submitting" or current.submission_started_at is None
                or current.cancel_requested_at is not None or current.ignore_late_result
                or prep.status not in {"ready", "dispatched"} or prep.request_frozen_at is None
                or not request_sha256 or prep.request_sha256 != request_sha256
                or prep.execution_instance_version_id != current.execution_instance_version_id
                or current.request_snapshot_sha256 != job.request_snapshot_sha256):
            raise ValidationError("原素材任务领取状态或冻结请求已变化，不能发送")
        for owner in owners.values():
            if owner is not current and (owner.status in {"succeeded", "failed", "cancelled", "cancel_requested", "uncertain"}
                    or owner.cancel_requested_at is not None or getattr(owner, "ignore_late_result", False)):
                raise ValidationError("辅助任务的原根任务已停止，不能发送")
        if current.resolutions.filter(outcome__in=AssetGenerationJobResolution.OPERATOR_OUTCOMES).exists():
            raise ValidationError("原素材任务已有人工消歧，不能发送")
        if current.resolutions.filter(outcome="http_body_prepared").exists():
            raise ValidationError("原素材任务已经记录HTTP正文；不得重复发送")
        evidence = json_contract({"schema_version": 1, "preparation_id": str(prep.pk),
            "request_sha256": request_sha256, "body": facts}, max_bytes=boundary_int("security.snapshot.text_max_bytes"))
        AssetGenerationJobResolution.objects.create(job=current,
            revision=(current.resolutions.aggregate(value=Max("revision"))["value"] or 0) + 1,
            outcome="http_body_prepared", decided_by=None, idempotency_key="system:http_body_prepared",
            request_sha256=evidence.sha256, evidence=evidence.value, evidence_schema_version=evidence.schema_version,
            evidence_size_bytes=evidence.size_bytes, evidence_sha256=evidence.sha256,
            note="系统记录HTTP库JSON序列化摘要；不是人工消歧，也不是送达、扣费或素材生成成功证明。")


def read_chat_body_note(attempt, *, preparation_id, request_sha256=None):
    from studio.services.json_contracts import verify_json_contract
    from studio.services.security_boundaries import boundary_int

    resolutions = attempt.resolutions
    rows = list(resolutions.filter(outcome="http_body_prepared").order_by("revision").only(
        "id", resolutions.field.attname, "created_at", "evidence", "evidence_schema_version", "evidence_size_bytes", "evidence_sha256")[:2])
    if not rows:
        return "本次原生文本未记录HTTP库序列化摘要；不能从冻结请求补造，也不代表从未发送。"
    try:
        if len(rows) != 1:
            raise ValidationError("原调用存在重复的HTTP正文证据")
        row = rows[0]
        value = verify_json_contract(row.evidence, schema_version=row.evidence_schema_version,
            size_bytes=row.evidence_size_bytes, sha256=row.evidence_sha256,
            max_bytes=boundary_int("security.snapshot.text_max_bytes")).value
        if (row.evidence_schema_version != 1 or not isinstance(value, dict)
                or set(value) != {"schema_version", "preparation_id", "request_sha256", "body"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["preparation_id"] != str(preparation_id)
                or value["request_sha256"] != (request_sha256 if request_sha256 is not None else attempt.request_sha256)):
            raise ValidationError("HTTP正文证据与原调用不一致")
        facts = validate_body_facts(value["body"])
        return (f"HTTP库序列化记录：{facts['size_bytes']} 字节，SHA256 {facts['sha256']}；"
                f"记录时间 {row.created_at.isoformat()}。仅为交给网络层之前的正文摘要，不含正文／请求头；不是已发送或已送达证明。")
    except ValidationError:
        return "原HTTP正文序列化证据无法核验；不重建、不改变原任务结果，不据此重试。"
