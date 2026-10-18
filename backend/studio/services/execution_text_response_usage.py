"""Original production and asset text receipts, never billing.

The worker writes once under its existing claim lock. Missing historical facts
remain unknown; reads never reconstruct usage or call a provider.
"""
from django.core.exceptions import ValidationError

from studio.services.json_contracts import verify_json_contract
from studio.services.provider_registry import safe_chat_usage
from studio.services.security_boundaries import boundary_int


def record_asset_text_response_usage(job, preparation_id, request_sha256, usage):
    """Retain a bounded receipt before parsing, under the original claim lock.

    Cancelled/ignored late output still consumed a response. A lost claim or
    operator resolution cannot be overwritten, and audit failure must escape
    before a successful candidate can be created.
    """
    from django.db.models import Max
    from studio.models import AssetGenerationJobResolution, ExecutionPreparation
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    from studio.services.json_contracts import json_contract

    with locked_preparation_owner_rows([("asset_job_id", job.pk)]) as owners:
        current = owners[("asset_job_id", str(job.pk))]
        if (job.claim_token is None or current.claim_token != job.claim_token
                or current.status not in {"submitting", "cancel_requested"}
                or current.submission_started_at is None):
            return False
        prep = ExecutionPreparation.objects.select_for_update().get(pk=preparation_id, asset_job=current)
        if (prep.request_frozen_at is None or not request_sha256 or prep.request_sha256 != request_sha256
                or prep.execution_instance_version_id != current.execution_instance_version_id
                or current.request_snapshot_sha256 != job.request_snapshot_sha256):
            raise ValidationError("原素材文本响应与冻结请求不一致，不能记录用量")
        if current.resolutions.filter(outcome__in=AssetGenerationJobResolution.OPERATOR_OUTCOMES).exists():
            return False
        if current.resolutions.filter(outcome="response_received").exists():
            return False
        evidence = json_contract({"schema_version": 1, "preparation_id": str(prep.pk),
            "request_sha256": request_sha256, "usage": safe_chat_usage(usage)},
            max_bytes=boundary_int("security.snapshot.text_max_bytes"))
        AssetGenerationJobResolution.objects.create(job=current,
            revision=(current.resolutions.aggregate(value=Max("revision"))["value"] or 0) + 1,
            outcome="response_received", decided_by=None, idempotency_key="system:response_received",
            request_sha256=evidence.sha256, evidence=evidence.value, evidence_schema_version=evidence.schema_version,
            evidence_size_bytes=evidence.size_bytes, evidence_sha256=evidence.sha256,
            note="服务商响应已返回；仅记录白名单token用量，不代表内容审核或实际扣费。")
        return True


def read_text_response_usage(attempt, *, preparation_id=None, request_sha256=None):
    base = {"state": "unavailable", "task_id": str(attempt.pk), "observed_at": None, "usage": {}}
    # At most two establishes uniqueness without scanning unbounded audit rows.
    rows = list(attempt.resolutions.filter(outcome="response_received").order_by("revision").only(
        "id", attempt.resolutions.field.attname, "created_at", "evidence", "evidence_schema_version", "evidence_size_bytes", "evidence_sha256")[:2])
    if not rows:
        return base, "本次调用未记录服务商用量，token数量未知；不按0计，不据此推算实际费用。"
    try:
        if len(rows) != 1:
            raise ValidationError("同一调用存在重复用量回执")
        row = rows[0]
        evidence = verify_json_contract(row.evidence, schema_version=row.evidence_schema_version,
            size_bytes=row.evidence_size_bytes, sha256=row.evidence_sha256,
            max_bytes=boundary_int("security.snapshot.text_max_bytes")).value
        if (row.evidence_schema_version != 1 or not isinstance(evidence, dict)
                or set(evidence) != {"schema_version", "preparation_id", "request_sha256", "usage"}
                or type(evidence["schema_version"]) is not int or evidence["schema_version"] != 1
                or evidence["preparation_id"] != str(preparation_id if preparation_id is not None else attempt.preparation_id)
                or evidence["request_sha256"] != (request_sha256 if request_sha256 is not None else attempt.request_sha256)
                or not isinstance(evidence["usage"], dict)
                or evidence["usage"] != safe_chat_usage(evidence["usage"])):
            raise ValidationError("用量回执与原调用不一致")
        usage = evidence["usage"]
        result = {**base, "state": "recorded", "observed_at": row.created_at.isoformat(), "usage": usage}
        counts = [f"{label} {usage[key]}" for key, label in (("prompt_tokens", "输入token"),
            ("completion_tokens", "输出token"), ("total_tokens", "总token")) if key in usage]
        return result, ("服务商用量：" + "；".join(counts) + "。用量不是实际扣费，缺少的数量仍未知。" if counts else
            "服务商响应已记录，但未提供可核验的token数量；用量仍未知，不按0计。")
    except ValidationError:
        return {**base, "state": "unverifiable"}, "服务商用量回执无法核验；保留原结果，不回退或补造用量。"
