"""Original Smoke response evidence; never a candidate or a second result writer."""
import re
import hashlib
from collections import Counter

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.services.execution_response_storage import read_response, store_response
from studio.services.json_contracts import canonical_json_bytes


def script_smoke_validation_summary(report):
    """Evidence of validation, not a second script candidate or preview report.

    The retained original response and frozen request own the full content.
    Group every advisory by code/message instead of repeating it per field;
    the digest covers the complete parser report, including paths/provenance.
    """
    if report.get("valid") is not True or report.get("parsed") is not True:
        raise ValidationError("未通过的剧本响应不能生成成功验收摘要")
    encoded = canonical_json_bytes(report)
    warnings = Counter((row["code"], row["message"]) for row in report["warnings"])
    summary = {key: report[key] for key in ("output_version_id", "output_contract_sha256",
        "validation_scope", "configuration_sha256", "source_structure_version_id", "parsed", "valid")}
    summary.update(schema_version=1, representation="script_validation_summary",
        report_sha256=hashlib.sha256(encoded).hexdigest(), report_size_bytes=len(encoded),
        source_field_count=len(report["source_fields"]), warning_count=len(report["warnings"]),
        warnings=[{"code": code, "message": message, "count": count}
            for (code, message), count in sorted(warnings.items())])
    return summary


def retain_smoke_response(attempt, response):
    text = response.get("text") if isinstance(response, dict) else None
    if not isinstance(text, str):
        return {"schema_version": 1, "state": "unavailable", "reason": "response_text_missing"}
    key, size, digest = store_response(text)
    # A storage acknowledgement alone does not prove the retained bytes are readable.
    read_response(key, size, digest)
    return {"schema_version": 1, "state": "available", "attempt_id": str(attempt.pk),
        "storage_backend": "default", "object_key": key, "size_bytes": size, "sha256": digest,
        "content_type": "text/plain; charset=utf-8", "created_at": timezone.now().isoformat()}


def read_smoke_response(attempt, manifest):
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValidationError("Smoke 原始响应 manifest 不完整")
    if manifest.get("state") == "unavailable":
        if manifest != {"schema_version": 1, "state": "unavailable", "reason": "response_text_missing"}:
            raise ValidationError("Smoke 原始响应缺失原因无效")
        return None
    if (manifest.get("state") != "available" or manifest.get("attempt_id") != str(attempt.pk)
            or manifest.get("storage_backend") != "default"
            or type(manifest.get("size_bytes")) is not int or manifest["size_bytes"] < 0
            or not isinstance(manifest.get("object_key"), str)
            or not isinstance(manifest.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])):
        raise ValidationError("Smoke 原始响应与原任务不一致")
    return read_response(manifest.get("object_key"), manifest["size_bytes"], manifest.get("sha256"))
