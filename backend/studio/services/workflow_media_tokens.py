"""Deterministic graph placeholders for exact, resolved media USES.

These are staging protocol identifiers, not Asset IDs, filenames or upload
authorization. Submitted Comfy graphs must contain none of these placeholders.
"""
import hashlib
import re

from studio.services.json_contracts import canonical_json_bytes


MEDIA_USE_TOKEN_PREFIX = "__AUTOPROMPT_MEDIA_USE__:"
_DIGEST = re.compile(r"[0-9a-f]{64}")


def workflow_media_use_token(*, item_key, source_sha256):
    if not isinstance(item_key, str) or not item_key or len(item_key) > 191:
        raise ValueError("素材使用必须有明确且有界的标识")
    if not isinstance(source_sha256, str) or _DIGEST.fullmatch(source_sha256) is None:
        raise ValueError("素材使用必须有完整来源摘要")
    digest = hashlib.sha256(canonical_json_bytes({"schema_version": 1, "item_key": item_key,
        "source_sha256": source_sha256})).hexdigest()
    return MEDIA_USE_TOKEN_PREFIX + digest


def is_workflow_media_use_token(value):
    return (isinstance(value, str) and value.startswith(MEDIA_USE_TOKEN_PREFIX)
        and _DIGEST.fullmatch(value[len(MEDIA_USE_TOKEN_PREFIX):]) is not None)


def collect_workflow_media_use_tokens(value):
    if isinstance(value, dict):
        return set().union(*(collect_workflow_media_use_tokens(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(collect_workflow_media_use_tokens(item) for item in value), set())
    if isinstance(value, str) and value.startswith(MEDIA_USE_TOKEN_PREFIX):
        if not is_workflow_media_use_token(value):
            raise ValueError("工作流包含无效素材使用占位符")
        return {value}
    return set()


def substitute_workflow_media_uses(value, mapping):
    """Only the staging adapter supplies verified upload paths in mapping."""
    if isinstance(value, dict):
        return {key: substitute_workflow_media_uses(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_workflow_media_uses(item, mapping) for item in value]
    if isinstance(value, str) and value.startswith(MEDIA_USE_TOKEN_PREFIX):
        if not is_workflow_media_use_token(value) or value not in mapping:
            raise ValueError("素材使用尚未完成正式上传")
        return mapping[value]
    return value
