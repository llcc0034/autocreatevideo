"""Capacity samples derived from original successful workers, never fake Smoke.

One sample per preparation, same transaction as original completion. Matching
uses an indexed exact signature only; partial dimensions have no match key.
"""
from django.core.exceptions import ValidationError
from django.db import connection

from studio.models import ExecutionCapacityObservation
from studio.services.execution_capacity_signatures import describe_execution_capacity
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparation_owners import nonproduction_preparation_root
from studio.services.execution_preparations import read_frozen_execution_request, read_execution_preparation_detail
from studio.services.json_contracts import JsonContract, assign_json_contract, json_contract, verify_json_contract


def _verified(contract):
    if not isinstance(contract, JsonContract):
        raise ValidationError("容量观测需要核对后的来源合同")
    return verify_json_contract(contract.value, schema_version=contract.schema_version, size_bytes=contract.size_bytes,
        sha256=contract.sha256, max_bytes=execution_configuration_max_bytes()).value


def capacity_observation_request(root):
    """Freeze the submission-time observation context, not today's new probe."""
    prep = root.execution_preparations.get()
    detail, definition, transport = read_frozen_execution_request(prep)
    evaluation = verify_json_contract(detail.evaluation, schema_version=detail.evaluation_schema_version,
        size_bytes=detail.evaluation_size_bytes, sha256=detail.evaluation_sha256,
        max_bytes=execution_configuration_max_bytes()).value
    if (evaluation.get("phase") != "submission" or evaluation.get("status") != "dispatched"
            or evaluation.get("candidate_request_sha256") != prep.request_sha256
            or evaluation.get("admission", {}).get("can_submit") is not True):
        raise ValidationError("容量样本缺少本次原工作器提交前的有效准入事实")
    checks = evaluation.get("root_checks")
    if (not isinstance(checks, dict) or not isinstance(checks.get("resources"), dict)
            or not isinstance(checks["resources"].get("capacity_runtime"), dict) or not isinstance(checks.get("capacity"), dict)):
        raise ValidationError("容量样本缺少已记录的资源与规格事实，不能推测补齐")
    capacity = describe_execution_capacity(definition, transport, runtime=checks["resources"]["capacity_runtime"])
    saved = checks["capacity"]
    for key in ("signature_key", "signature_sha256", "known_dimensions", "missing_dimensions"):
        if saved.get(key) != capacity.value[key]:
            raise ValidationError("容量规格与实际提交前记录不一致")
    return json_contract({"schema_version": 1, "preparation_id": str(prep.pk),
        "execution_instance_version_id": str(prep.execution_instance_version_id), "request_sha256": prep.request_sha256,
        "capacity": capacity.value}, max_bytes=execution_configuration_max_bytes())


def record_completed_capacity_observation(root, request):
    """Caller owns the original completion lock/transaction; no new queue."""
    source = _verified(request)
    prep = root.execution_preparations.get()
    if prep.asset_job_id:
        from studio.models import ExecutionTextResponseReview
        if ExecutionTextResponseReview.objects.filter(asset_job_id=prep.asset_job_id, confirmed_at__isnull=False).exists():
            raise ValidationError("人工补正不是模型成功执行，不能记录容量样本")
    if (not connection.in_atomic_block or root.status != "succeeded" or root.submission_started_at is None
            or root.finished_at is None or root.finished_at < root.submission_started_at
            or source.get("preparation_id") != str(prep.pk)
            or source.get("execution_instance_version_id") != str(prep.execution_instance_version_id)
            or source.get("request_sha256") != prep.request_sha256):
        raise ValidationError("容量观测只能随原工作器的真实成功终态写入")
    if source != capacity_observation_request(root).value:
        raise ValidationError("容量样本不能替换原提交时的规格事实")
    elapsed_ms = int((root.finished_at - root.submission_started_at).total_seconds() * 1000)
    values = {"preparation": prep, "execution_instance_version_id": prep.execution_instance_version_id,
        "request_sha256": prep.request_sha256, "signature_sha256": source["capacity"]["signature_sha256"],
        "observed_at": root.finished_at, "elapsed_ms": elapsed_ms}
    record = ExecutionCapacityObservation(**values)
    assign_json_contract(record, "details", source, max_bytes=execution_configuration_max_bytes())
    old = ExecutionCapacityObservation.objects.filter(preparation=prep).first()
    if old is not None:
        if old.details_sha256 != record.details_sha256 or old.observed_at != root.finished_at or old.elapsed_ms != elapsed_ms:
            raise ValidationError("同一次执行不能覆盖原容量观测")
        return old
    record.full_clean()
    record.save(force_insert=True)
    return record


def capacity_sample_candidates(version_id, signature_sha256):
    if not signature_sha256:
        raise ValidationError("缺少完整规格，不能查询相同规格样本")
    return ExecutionCapacityObservation.objects.filter(execution_instance_version_id=version_id,
        signature_sha256=signature_sha256).order_by("-observed_at", "-id").values("id", "observed_at", "request_sha256")


def _verified_capacity_observation(sample):
    saved = verify_json_contract(sample.details, schema_version=sample.details_schema_version, size_bytes=sample.details_size_bytes,
        sha256=sample.details_sha256, max_bytes=execution_configuration_max_bytes()).value
    if not isinstance(saved, dict) or not isinstance(saved.get("capacity"), dict):
        raise ValidationError("容量样本正文缺少完整规格")
    prep = sample.preparation
    detail = read_execution_preparation_detail(prep)
    root = nonproduction_preparation_root(prep)
    evaluation = detail.evaluation
    if (root.status != "succeeded" or root.finished_at != sample.observed_at or root.submission_started_at is None
            or root.finished_at < root.submission_started_at
            or sample.elapsed_ms != int((root.finished_at - root.submission_started_at).total_seconds() * 1000)
            or sample.request_sha256 != prep.request_sha256
            or sample.execution_instance_version_id != prep.execution_instance_version_id
            or saved.get("preparation_id") != str(prep.pk)
            or saved.get("execution_instance_version_id") != str(sample.execution_instance_version_id)
            or saved.get("request_sha256") != sample.request_sha256
            or not isinstance(evaluation, dict) or evaluation.get("phase") != "submission"
            or evaluation.get("candidate_request_sha256") != prep.request_sha256):
        raise ValidationError("容量样本与原成功任务或本次规格不一致")
    capacity, submitted = saved["capacity"], evaluation["root_checks"]["capacity"]
    if (capacity.get("signature_sha256") != sample.signature_sha256
            or bool(capacity.get("missing_dimensions")) != (sample.signature_sha256 is None)
            or any(capacity.get(key) != submitted.get(key) for key in
                ("signature_key", "signature_sha256", "known_dimensions", "missing_dimensions"))):
        raise ValidationError("容量样本不等于原提交时核对的规格，不能回退较早样本")
    return saved


def read_preparation_capacity_observation(preparation):
    """Authorized preparation detail only; absence does not fabricate a row."""
    sample = ExecutionCapacityObservation.objects.select_related("preparation").filter(preparation=preparation).first()
    if sample is None:
        return None
    saved = _verified_capacity_observation(sample)
    return {"id": str(sample.pk), "observed_at": sample.observed_at.isoformat(), "elapsed_ms": sample.elapsed_ms,
        "signature_sha256": sample.signature_sha256, "dimensions_complete": sample.signature_sha256 is not None,
        "known_dimensions": saved["capacity"]["known_dimensions"], "missing_dimensions": saved["capacity"]["missing_dimensions"]}


def read_capacity_coverage(version_id, description):
    capacity = _verified(description)
    if capacity["state"] == "unknown":
        return json_contract({**capacity, "observation_id": None}, max_bytes=execution_configuration_max_bytes())
    candidate = capacity_sample_candidates(version_id, capacity["signature_sha256"]).first()
    if candidate is None:
        return json_contract({**capacity, "observation_id": None}, max_bytes=execution_configuration_max_bytes())
    sample = ExecutionCapacityObservation.objects.select_related("preparation").get(pk=candidate["id"])
    saved = _verified_capacity_observation(sample)
    if (saved["capacity"].get("known_dimensions") != capacity["known_dimensions"]
            or saved["capacity"].get("missing_dimensions") != []
            or saved["capacity"].get("signature_sha256") != sample.signature_sha256):
        raise ValidationError("容量样本与原成功任务或本次规格不一致，不能回退较早样本")
    return json_contract({**capacity, "state": "covered", "reason": "same_specification_observed",
        "observation_id": str(sample.pk)}, max_bytes=execution_configuration_max_bytes())
