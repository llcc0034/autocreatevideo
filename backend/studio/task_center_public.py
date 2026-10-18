"""Public snapshot projection; media URLs come only from formal serializers."""
import json
import re

from studio.services.configuration import redact_sensitive_bindings


_REMOTE = re.compile(r"(?:https?|file|ftp|s3)://[^\s<>\"']+", re.I)
_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\|/(?:home|root|private|tmp|var|etc|mnt|opt|srv|Users|media|data)/)[^\r\n<>\"']+")
_SECRET = re.compile(r"(?i)(?:bearer\s+\S+|(?<![\w])(?:api[_-]?key|password|secret|(?:access[_-]?|refresh[_-]?)?token|authorization)\s*[:=]\s*[^\s,;]+)")
_PRIVATE_KEYS = {"endpoint", "base_url", "api_url", "url", "uri", "local_path", "path", "file", "filename", "filename_prefix",
                 "object_key", "subfolder", "storage_path", "output_path", "concat_manifest_path", "headers",
                 "authorization", "staged_inputs", "comfy_path", "provider_request_id", "worker_id"}
_TOKEN_COUNTS = {"max_tokens", "max_output_tokens", "max_completion_tokens"}


def public_value(value):
    """Retain complete allowed text; replacements are redactions, never truncation."""
    def retain_token_limits(original, redacted):
        # The general secret-key redactor treats *tokens* as credentials. These
        # three numeric request limits are public parameters, not secret tokens.
        if isinstance(original, dict) and isinstance(redacted, dict):
            for key, child in original.items():
                if str(key).split(".")[-1] in _TOKEN_COUNTS and type(child) is int and child >= 0:
                    redacted.pop(str(key) + "_configured", None)
                    redacted[key] = child
                elif key in redacted:
                    retain_token_limits(child, redacted[key])
        elif isinstance(original, list) and isinstance(redacted, list):
            for child, safe in zip(original, redacted):
                retain_token_limits(child, safe)
        return redacted
    def walk(item):
        if isinstance(item, dict):
            return {key: ("[redacted]" if str(key).lower().replace("-", "_") in _PRIVATE_KEYS else walk(child))
                    for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [walk(child) for child in item]
        if isinstance(item, str):
            return _SECRET.sub("[redacted]", _PATH.sub("[redacted path]", _REMOTE.sub("[redacted URL]", item)))
        return item
    return walk(retain_token_limits(value, redact_sensitive_bindings(value)) if isinstance(value, (dict, list)) else value)


def public_response_text(original, diagnostics):
    from studio.services.execution_text_response_review import COLLECT_RECEIPT_KIND
    if diagnostics.get("body_kind") == COLLECT_RECEIPT_KIND:
        # Redact object keys before serializing: regex over JSON text misses
        # relative filenames/subfolders and nested credential keys.
        return json.dumps(public_value(json.loads(original)), ensure_ascii=False, separators=(",", ":"))
    return public_value(original)


def public_media(serializer, obj, *, fields):
    """Whitelisted formal serializer fields, never raw output/storage locations."""
    data = serializer(obj).data
    result = {key: public_value(data[key]) for key in fields if key in data and key not in {"preview_url", "download_url"}}
    for key in ("preview_url", "download_url"):
        url = data.get(key, "")
        result[key] = url if isinstance(url, str) and url.startswith("/api/v1/media/") else ""
    return result
