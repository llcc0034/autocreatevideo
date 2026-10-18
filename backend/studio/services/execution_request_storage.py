"""One private canonical request object per frozen preparation.

Database manifests are small; request bytes are never a second editable source.
Only owner-authorized detail/dispatch readers may resolve this object. Storage
failures never truncate a request or grant permission to submit it.
"""
import json
import re
import uuid

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from studio.services.json_contracts import canonical_json_bytes, json_contract, verify_json_contract
from studio.services.security_boundaries import boundary_int


class ExecutionRequestStorageError(ValidationError):
    def __init__(self, message):
        super().__init__(message, code="execution_request_storage_invalid")


REQUEST_SIZE_PARTS = {
    "native_plan": "原生文本请求计划",
    "provider_payload": "模型请求正文",
    "transport": "请求传输正文",
    "document": "完整请求文档",
}


class ExecutionRequestSizeError(ValidationError):
    """Safe size facts only; never expose prompt text or storage paths."""
    def __init__(self, *, part, actual_bytes, max_bytes):
        label = REQUEST_SIZE_PARTS[part]
        message = (f"{label}为{actual_bytes}字节，超过当前{max_bytes}字节上限"
            "（security.snapshot.stage_max_bytes）。尚未提交主调用；请核对系统配置中的请求文档边界，不会截断正文。")
        self.diagnostic = {"code": "execution_request_too_large", "part": part,
            "actual_bytes": actual_bytes, "max_bytes": max_bytes,
            "configuration_key": "security.snapshot.stage_max_bytes", "message": message}
        super().__init__(message, code="execution_request_too_large")


def request_part_contract(value, *, max_bytes, part, schema_version=1):
    """Request components share the document policy, not a DB column ceiling."""
    if part not in REQUEST_SIZE_PARTS or type(max_bytes) is not int or max_bytes < 1:
        raise ValidationError("需要明确的请求组成及正整数字节边界")
    try:
        return json_contract(value, schema_version=schema_version, max_bytes=max_bytes)
    except ValidationError:
        size = len(canonical_json_bytes(value))
        if size > max_bytes:
            raise ExecutionRequestSizeError(part=part, actual_bytes=size, max_bytes=max_bytes) from None
        raise


def execution_request_max_bytes():
    # Runtime inputs and request documents share the configured policy;
    # configuration/review contracts keep their separate schema constraints.
    return boundary_int("security.snapshot.stage_max_bytes")


def request_document(value, *, schema_version=1):
    try:
        if not isinstance(value, dict) or schema_version != 1 or type(schema_version) is not int:
            raise ValidationError("请求正文必须是当前对象合同")
        return request_part_contract(value, schema_version=schema_version,
            max_bytes=execution_request_max_bytes(), part="document")
    except ExecutionRequestSizeError:
        raise
    except ValidationError as exc:
        raise ExecutionRequestStorageError("完整请求无法保存：正文或大小不符合阶段快照存储边界；不会截断内容。") from exc


def read_request_manifest(instance, prefix):
    """Validate recorded metadata only; never open or certify current file bytes."""
    key = getattr(instance, f"{prefix}_key")
    schema = getattr(instance, f"{prefix}_schema_version")
    size = getattr(instance, f"{prefix}_size_bytes")
    digest = getattr(instance, f"{prefix}_sha256")
    if key is None and schema is None and size is None and digest is None:
        return None
    if (not isinstance(key, str)
            or re.fullmatch(rf"execution-requests/{re.escape(str(instance.preparation_id))}/[a-f0-9-]+\.json", key) is None
            or type(schema) is not int or schema != 1
            or type(size) is not int or size < 2
            or not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None):
        raise ExecutionRequestStorageError("冻结请求的文件标识、所属准备或摘要元数据无效。")
    return {"key": key, "schema_version": schema, "size_bytes": size, "sha256": digest}


def read_request(instance, prefix):
    metadata = read_request_manifest(instance, prefix)
    if metadata is None:
        return None
    key, schema, size, digest = (metadata[name] for name in ("key", "schema_version", "size_bytes", "sha256"))
    if size > execution_request_max_bytes():
        raise ExecutionRequestStorageError("冻结请求的文件标识、所属准备或摘要元数据无效。")
    try:
        with default_storage.open(key, "rb") as stream:
            data = stream.read(size + 1)
        value = json.loads(data.decode("utf-8"))
        contract = verify_json_contract(value, schema_version=schema, size_bytes=size,
            sha256=digest, max_bytes=execution_request_max_bytes())
        if (not isinstance(value, dict) or canonical_json_bytes(value) != data
                or value.get("preparation_id") != str(instance.preparation_id)):
            raise ValidationError("request identity or canonical bytes mismatch")
    except (OSError, ValueError, UnicodeError, ValidationError, RecursionError) as exc:
        raise ExecutionRequestStorageError("冻结请求文件缺失、不可读或与原摘要不一致；不会重新组装或提交。") from exc
    return contract.value


def store_prepared_request(detail, value):
    if detail.prepared_request_key is not None:
        raise ExecutionRequestStorageError("最终请求已保存，不能改写或替换原文件。")
    contract = request_document(value)
    if value.get("preparation_id") != str(detail.preparation_id):
        raise ExecutionRequestStorageError("最终请求不属于本次准备，不能保存。")
    # Save then read back BEFORE a ready/index manifest can be committed. An
    # outer DB rollback can leave an unreferenced file, never a usable request.
    # Such files follow the documented retention/backup policy, not read repair.
    try:
        key = default_storage.save(f"execution-requests/{detail.preparation_id}/{uuid.uuid4()}.json",
            ContentFile(canonical_json_bytes(contract.value)))
    except OSError as exc:
        raise ExecutionRequestStorageError("完整请求写入正式文件存储失败；尚未冻结或提交。") from exc
    detail.prepared_request_key = key
    for suffix in ("schema_version", "size_bytes", "sha256"):
        setattr(detail, f"prepared_request_{suffix}", getattr(contract, suffix))
    read_request(detail, "prepared_request")
    return contract


def reference_prepared_request(attempt, detail):
    """An original production attempt references the same immutable object."""
    value = read_request(detail, "prepared_request")
    if value is None or attempt.preparation_id != detail.preparation_id:
        raise ExecutionRequestStorageError("生产调用缺少同一准备的已冻结请求。")
    for suffix in ("key", "schema_version", "size_bytes", "sha256"):
        setattr(attempt, f"request_snapshot_{suffix}", getattr(detail, f"prepared_request_{suffix}"))
