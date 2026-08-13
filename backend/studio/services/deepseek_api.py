"""Strict DeepSeek transport for Qwen-compatible language operations.

The existing isolated Qwen script remains the protocol authority.  This module
loads its request normalisers, prompt builders, cleaners, and validators, but
replaces the local model call with the official DeepSeek HTTPS API.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from django.conf import settings
import requests


PROVIDER_ID = "deepseek_api"
_OFFICIAL_HOST = "api.deepseek.com"
_SAFE_MODEL_RE = re.compile(r"[^A-Za-z0-9._:/-]+")
_RETRY_BACKOFF_SECONDS = (0.25, 0.75, 1.5)
_SAFE_VALIDATOR_PATH_RE = re.compile(
    r"(?:result\.)?shots\[\d+\](?:\.[a-z][a-z0-9_]*(?:\[\d+\])?)*\Z"
)
_VALIDATOR_PATH_SEARCH_RE = re.compile(
    r"result(?:\.shots\[\d+\])?(?:\.[a-z][a-z0-9_]*(?:\[\d+\])?)*"
)
_CREATIVE_LENGTH_ERROR_RE = re.compile(
    r"^(?P<path>result\.shots\[(?P<shot_index>\d+)\]\."
    r"(?P<field>[a-z][a-z0-9_]*)) exceeds (?P<limit>\d+) characters$"
)
_CREATIVE_REPAIR_BUDGET_KEYS = {
    "title_zh": "shot_title_zh",
    "title_en": "shot_title_en",
    "narrative_zh": "narrative_zh",
    "narrative_en": "narrative_en",
    "action_zh": "action_zh",
    "action_en": "action_en",
    "continuity_zh": "continuity_zh",
    "continuity_en": "continuity_en",
    "soundscape_zh": "soundscape_zh",
    "soundscape_en": "soundscape_en",
    "music_zh": "music_zh",
    "music_en": "music_en",
}
_DEEPSEEK_CREATIVE_FIELD_CHARACTER_BUDGETS = {
    "narrative_en": 1000,
    "action_en": 1000,
}


class DeepSeekAPIError(RuntimeError):
    """A caller-safe remote transport or protocol failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "deepseek_api_failed",
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = _safe_code(code)
        self.runtime_metadata = _safe_metadata(runtime_metadata or {})


class DeepSeekConfigurationError(DeepSeekAPIError):
    """The optional remote provider is disabled or unsafe to call."""


def _safe_code(value: Any) -> str:
    candidate = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    return candidate[:64] or "deepseek_api_failed"


def _safe_model(value: Any) -> str:
    return _SAFE_MODEL_RE.sub("", str(value or ""))[:200]


def _safe_validator_path(value: Any) -> str:
    candidate = str(value or "")[:160]
    return candidate if _SAFE_VALIDATOR_PATH_RE.fullmatch(candidate) else ""


def _safe_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            safe[key] = raw
    return safe


def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Allow only non-secret scalar audit fields across the service boundary."""
    safe: dict[str, Any] = {}
    for key in (
        "provider", "model", "operation", "transport", "http_status",
        "attempts", "elapsed_ms", "finish_reason", "batch_size",
        "all_items_ok", "repair_attempted", "repair_succeeded",
        "repair_reason_code", "review_required", "review_issue_count",
        "validation_error_code", "validation_path",
    ):
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) and not isinstance(item, bytes):
            if key == "model":
                safe[key] = _safe_model(item)
            elif key in {"validation_error_code", "repair_reason_code"}:
                safe[key] = _safe_code(item)
            elif key == "validation_path":
                path = _safe_validator_path(item)
                if path:
                    safe[key] = path
            else:
                safe[key] = item
    usage = _safe_usage(value.get("usage"))
    if usage:
        safe["usage"] = usage
    safe.setdefault("provider", PROVIDER_ID)
    safe.setdefault("transport", "remote")
    return safe


def _config() -> dict[str, Any]:
    configured = getattr(settings, "DEEPSEEK_API", {}) or {}
    try:
        timeout = float(configured.get("timeout_seconds", 90))
    except (TypeError, ValueError):
        timeout = 0.0
    try:
        retries = int(configured.get("max_retries", 1))
    except (TypeError, ValueError):
        retries = -1
    try:
        max_tokens = int(configured.get("max_tokens", 4096))
    except (TypeError, ValueError):
        max_tokens = 0
    return {
        "enabled": bool(configured.get("enabled", False)),
        "base_url": str(configured.get("base_url") or "").strip(),
        "api_key": str(configured.get("api_key") or "").strip(),
        "model": _safe_model(configured.get("model")),
        "timeout_seconds": timeout,
        "max_retries": retries,
        "max_tokens": max_tokens,
    }


def _validated_endpoint(config: dict[str, Any]) -> str | None:
    try:
        parsed = urlparse(config["base_url"])
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != _OFFICIAL_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return None
    path = parsed.path.rstrip("/")
    if path in {"", "/v1"}:
        path = f"{path}/chat/completions"
    elif path not in {"/chat/completions", "/v1/chat/completions"}:
        return None
    return urlunparse(("https", parsed.netloc, path, "", "", ""))


def get_deepseek_capability() -> dict[str, Any]:
    """Return a detached capability with no credential or endpoint material."""
    config = _config()
    configured = bool(
        _validated_endpoint(config)
        and config["api_key"]
        and config["model"]
        and 0 < config["timeout_seconds"] <= 3600
        and 0 <= config["max_retries"] <= 5
        and 1 <= config["max_tokens"] <= 32768
    )
    return {
        "id": PROVIDER_ID,
        "provider": PROVIDER_ID,
        "enabled": config["enabled"],
        "configured": configured,
        "model": config["model"],
        "transport": "remote",
        "data_egress": True,
    }


def _require_config() -> dict[str, Any]:
    config = _config()
    if not config["enabled"]:
        raise DeepSeekConfigurationError(
            "DeepSeek API is disabled.", code="deepseek_api_disabled",
        )
    if not get_deepseek_capability()["configured"]:
        raise DeepSeekConfigurationError(
            "DeepSeek API configuration is incomplete or unsafe.",
            code="deepseek_api_misconfigured",
        )
    config["endpoint"] = _validated_endpoint(config)
    return config


@lru_cache(maxsize=1)
def _protocol_module():
    script = (Path(settings.BASE_DIR).parent / "scripts" / "qwen_translate_once.py").resolve()
    if not script.is_file():
        raise DeepSeekConfigurationError(
            "The shared language protocol is unavailable.",
            code="deepseek_protocol_unavailable",
        )
    spec = importlib.util.spec_from_file_location("_autoprompt_qwen_protocol", script)
    if spec is None or spec.loader is None:
        raise DeepSeekConfigurationError(
            "The shared language protocol is unavailable.",
            code="deepseek_protocol_unavailable",
        )
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` resolves postponed annotations through ``sys.modules``
    # while executing the class decorators in the protocol module.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise DeepSeekConfigurationError(
            "The shared language protocol could not be loaded.",
            code="deepseek_protocol_unavailable",
        ) from exc
    return module


def _http_error_code(status: int) -> str:
    if status in {401, 403}:
        return "deepseek_api_authentication_failed"
    if status == 429:
        return "deepseek_api_rate_limited"
    if 500 <= status <= 599:
        return "deepseek_api_unavailable"
    return "deepseek_api_request_rejected"


def _chat_completion(
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    operation: str,
    response_format: dict[str, str] | None = None,
    temperature: float = 0.0,
) -> tuple[str, dict[str, Any]]:
    config = _require_config()
    started = time.monotonic()
    body: dict[str, Any] = {
        "model": config["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": min(max_tokens, config["max_tokens"]),
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    if response_format is not None:
        body["response_format"] = response_format
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    attempts = 0
    response = None
    for attempt in range(config["max_retries"] + 1):
        attempts = attempt + 1
        try:
            response = requests.post(
                config["endpoint"], headers=headers, json=body,
                timeout=config["timeout_seconds"],
            )
        except requests.RequestException as exc:
            if attempt < config["max_retries"]:
                time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
                continue
            raise DeepSeekAPIError(
                "DeepSeek API could not be reached.",
                code="deepseek_api_network_failed",
                runtime_metadata={
                    "operation": operation, "attempts": attempts,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            ) from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status < 300:
            break
        # Officially documented retryable conditions are rate limiting and
        # transient service failures.  Client/auth/billing/validation errors
        # fail immediately so a bad request is never replayed.
        if status in {429, 500, 503}:
            if attempt < config["max_retries"]:
                time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
                continue
        raise DeepSeekAPIError(
            "DeepSeek API rejected the request.", code=_http_error_code(status),
            runtime_metadata={
                "operation": operation, "http_status": status,
                "attempts": attempts,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )
    try:
        payload = response.json()
        choices = payload["choices"]
        choice = choices[0]
        content = choice["message"]["content"]
        finish_reason = str(choice.get("finish_reason") or "").lower()
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeepSeekAPIError(
            "DeepSeek API returned an invalid response.",
            code="deepseek_api_invalid_response",
            runtime_metadata={
                "operation": operation, "attempts": attempts,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        ) from exc
    elapsed = round((time.monotonic() - started) * 1000)
    if finish_reason != "stop":
        code = (
            f"{operation}_output_budget_exhausted"
            if finish_reason == "length"
            else f"{operation}_incomplete_response"
        )
        raise DeepSeekAPIError(
            "DeepSeek API did not complete the requested output.", code=code,
            runtime_metadata={
                "operation": operation, "attempts": attempts,
                "elapsed_ms": elapsed, "finish_reason": finish_reason,
                "usage": _safe_usage(payload.get("usage")),
            },
        )
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekAPIError(
            "DeepSeek API returned no content.", code=f"{operation}_empty_output",
            runtime_metadata={
                "operation": operation, "attempts": attempts,
                "elapsed_ms": elapsed, "finish_reason": finish_reason,
                "usage": _safe_usage(payload.get("usage")),
            },
        )
    return content, _safe_metadata({
        "provider": PROVIDER_ID,
        "model": config["model"],
        "transport": "remote",
        "operation": operation,
        "attempts": attempts,
        "elapsed_ms": elapsed,
        "finish_reason": finish_reason,
        "usage": payload.get("usage"),
    })


def _translation_failure(
    item: Any, *, code: str, repair_attempted: bool, started: float,
) -> dict[str, Any]:
    return {
        "request_id": item.request_id,
        "ok": False,
        "translation": "",
        "attempt": item.attempt,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "error": "The translation candidate failed validation.",
        "error_code": _safe_code(code),
        "error_details": {},
        "repair_attempted": repair_attempted,
        "repair_reason_code": "residual_cjk" if repair_attempted else "",
    }


def _translate_one(
    item: Any, protocol: Any, *, contain_remote_error: bool,
) -> tuple[dict[str, Any], dict[str, int]]:
    started = time.monotonic()
    repair_attempted = False
    usage: dict[str, int] = {}
    try:
        content, metadata = _chat_completion(
            messages=protocol._build_messages(item),
            max_tokens=item.max_new_tokens,
            operation="translation",
            temperature=0.15 if item.attempt > 1 else 0.0,
        )
        usage = _safe_usage(metadata.get("usage"))
        translation = protocol._clean_translation(content)
        if not translation:
            return _translation_failure(
                item, code="empty_translation", repair_attempted=False, started=started,
            ), usage
        if item.target_lang.lower().startswith("english") and protocol._contains_unprotected_cjk(
            translation, item.protected_tokens,
        ):
            repair_attempted = True
            repair_content, repair_metadata = _chat_completion(
                messages=protocol._build_translation_repair_messages(
                    item, translation, protocol._residual_cjk_fragments(translation),
                ),
                max_tokens=item.max_new_tokens,
                operation="translation_repair",
                temperature=0.0,
            )
            usage = _merge_usage(usage, _safe_usage(repair_metadata.get("usage")))
            translation = protocol._clean_translation(repair_content)
            if not translation:
                return _translation_failure(
                    item, code="empty_repair_output", repair_attempted=True, started=started,
                ), usage
            if protocol._contains_unprotected_cjk(translation, item.protected_tokens):
                return _translation_failure(
                    item, code="residual_cjk_after_repair",
                    repair_attempted=True, started=started,
                ), usage
        if item.target_lang.lower().startswith("english"):
            try:
                protocol._validate_semantic_guards(item, translation)
            except RuntimeError:
                return _translation_failure(
                    item, code="semantic_guard_failed",
                    repair_attempted=repair_attempted, started=started,
                ), usage
        if any(
            translation.count(token) != item.text.count(token)
            for token in item.protected_tokens
        ):
            return _translation_failure(
                item, code="protected_token_mismatch",
                repair_attempted=repair_attempted, started=started,
            ), usage
        return ({
            "request_id": item.request_id,
            "ok": True,
            "translation": translation,
            "attempt": item.attempt,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "repair_attempted": repair_attempted,
            "repair_reason_code": "residual_cjk" if repair_attempted else "",
        }, usage)
    except DeepSeekAPIError as exc:
        if not contain_remote_error:
            raise
        failure = _translation_failure(
            item, code=exc.code, repair_attempted=repair_attempted, started=started,
        )
        return failure, usage
    except Exception:
        return _translation_failure(
            item, code="translation_failed", repair_attempted=repair_attempted, started=started,
        ), usage


def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {
        key: left.get(key, 0) + right.get(key, 0)
        for key in set(left) | set(right)
    }


def run_translation(request_payload: dict[str, Any]) -> dict[str, Any]:
    """Translate one item or a batch using the Qwen-compatible envelope."""
    config = _require_config()
    protocol = _protocol_module()
    try:
        items, is_batch = protocol._normalise_items(request_payload)
    except Exception as exc:
        raise DeepSeekAPIError(
            "The translation request is invalid.", code="translation_invalid_request",
        ) from exc
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    for item in items:
        row, row_usage = _translate_one(
            item, protocol, contain_remote_error=is_batch,
        )
        rows.append(row)
        usage = _merge_usage(usage, row_usage)
    elapsed = round((time.monotonic() - started) * 1000)
    metadata = _safe_metadata({
        "provider": PROVIDER_ID, "model": config["model"],
        "transport": "remote", "operation": "translation",
        "elapsed_ms": elapsed, "usage": usage, "batch_size": len(items),
        "all_items_ok": all(row["ok"] for row in rows),
    })
    if is_batch:
        return {
            "ok": True, "items": rows, "model": config["model"],
            "elapsed_ms": elapsed, "metadata": metadata,
        }
    row = rows[0]
    envelope = {
        "ok": row["ok"], "request_id": row["request_id"],
        "translation": row["translation"], "model": config["model"],
        "elapsed_ms": elapsed,
        "metadata": {
            **metadata,
            "repair_attempted": row["repair_attempted"],
            "repair_reason_code": row["repair_reason_code"],
        },
    }
    if not row["ok"]:
        envelope.update({
            "error": row["error"], "error_code": row["error_code"],
            "error_details": row["error_details"],
        })
    return envelope


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Return only a JSON object that conforms exactly to this JSON Schema. "
        "Every maxLength is a hard character limit (not a token target); keep each "
        "string comfortably below it. Do not add Markdown or commentary.\nJSON_SCHEMA:\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _deepseek_creative_response_schema(protocol: Any, request: Any) -> dict[str, Any]:
    """Use the remote-provider prose budget without widening local Qwen."""
    schema = copy.deepcopy(protocol._creative_response_schema(request))
    shot_properties = schema["properties"]["shots"]["items"]["properties"]
    for field, limit in _DEEPSEEK_CREATIVE_FIELD_CHARACTER_BUDGETS.items():
        shot_properties[field]["maxLength"] = limit
    return schema


def _deepseek_creative_messages(protocol: Any, request: Any) -> list[dict[str, str]]:
    limits = {
        **protocol.CREATIVE_MODEL_FIELD_CHARACTER_BUDGETS,
        **_DEEPSEEK_CREATIVE_FIELD_CHARACTER_BUDGETS,
    }
    return protocol._build_creative_messages(
        request, character_limits=limits,
    )


def _deepseek_creative_max_tokens(request: Any) -> int:
    """Give remote prose bounded headroom without changing local budgets."""
    return int(request.max_tokens) + (384 * int(request.shot_count))


def _deepseek_review_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop local-primary concision notices for fields within remote limits."""
    return [
        issue for issue in issues
        if not (
            issue.get("code") == "model_generation_length_exceeded"
            and re.fullmatch(
                r"shots\[\d+\]\.(?:narrative_en|action_en)",
                str(issue.get("path") or ""),
            )
        )
    ]


def _validation_diagnostic(exc: Exception) -> dict[str, str]:
    """Return only an allowlisted code/path, never validator prose."""
    message = str(exc)
    path_match = _VALIDATOR_PATH_SEARCH_RE.search(message)
    path = _safe_validator_path(path_match.group(0) if path_match else "")
    if path.startswith("result."):
        path = path.removeprefix("result.")
    if _CREATIVE_LENGTH_ERROR_RE.fullmatch(message):
        code = "field_too_long"
    elif "must be non-empty" in message:
        code = "field_required_empty"
    elif "keys mismatch" in message:
        code = "object_shape_invalid"
    elif "must be a string" in message:
        code = "field_type_invalid"
    elif "control character" in message:
        code = "control_character"
    else:
        code = "structured_validation_failed"
    return {"validation_error_code": code, "validation_path": path}


def _creative_length_repair_targets(
    exc: Exception, protocol: Any, decoded: Any,
) -> list[tuple[int, str, str, int]] | None:
    """Resolve every over-limit allowlisted shot string after a length failure."""
    match = _CREATIVE_LENGTH_ERROR_RE.fullmatch(str(exc))
    if match is None:
        return None
    first_field = match.group("field")
    if first_field not in _CREATIVE_REPAIR_BUDGET_KEYS:
        return None
    shots = decoded.get("shots") if isinstance(decoded, dict) else None
    if not isinstance(shots, list):
        return None
    final_budgets = protocol.CREATIVE_FINAL_REVIEW_FIELD_CHARACTER_BUDGETS
    targets: list[tuple[int, str, str, int]] = []
    for shot_index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        for field, budget_key in _CREATIVE_REPAIR_BUDGET_KEYS.items():
            original = shot.get(field)
            if not isinstance(original, str):
                continue
            hard_limit = int(final_budgets[budget_key])
            if len(original) > hard_limit:
                # Use the provider-specific acceptance ceiling; the repair
                # prompt below requests roughly half of it for reliable margin.
                acceptance_limit = int(
                    _DEEPSEEK_CREATIVE_FIELD_CHARACTER_BUDGETS.get(
                        field, final_budgets[budget_key],
                    )
                )
                targets.append((shot_index, field, original, acceptance_limit))
    first_path = (
        int(match.group("shot_index")), first_field
    )
    if not targets or not any(
        (shot_index, field) == first_path
        for shot_index, field, _original, _limit in targets
    ):
        return None
    return targets


def _repair_creative_length_once(
    *, decoded: dict[str, Any], targets: list[tuple[int, str, str, int]],
    request: Any, protocol: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    def compression_target(field: str, limit: int) -> tuple[int, int]:
        # DeepSeek JSON mode does not enforce JSON Schema.  Ask well below the
        # actual acceptance ceiling so ordinary verbosity cannot cross it.
        target_characters = max(24, limit // 2)
        maximum_words = max(6, target_characters // 8) if field.endswith("_en") else 0
        return target_characters, maximum_words

    target_rows = [
        {
            "field_path": f"result.shots[{shot_index}].{field}",
            "target_characters": compression_target(field, limit)[0],
            **(
                {"maximum_words": compression_target(field, limit)[1]}
                if field.endswith("_en") else {}
            ),
            "value": original,
        }
        for shot_index, field, original, limit in targets
    ]
    target_by_path = {
        row["field_path"]: (shot_index, field, limit)
        for row, (shot_index, field, _original, limit) in zip(target_rows, targets)
    }
    patch_variants = []
    for row in target_rows:
        cap_description = (
            f"One concise sentence or phrase; at most {row['target_characters']} characters"
            + (
                f" and at most {row['maximum_words']} words."
                if row.get("maximum_words") else "."
            )
        )
        patch_variants.append({
            "type": "object", "additionalProperties": False,
            "required": ["field_path", "value"],
            "properties": {
                "field_path": {"type": "string", "enum": [row["field_path"]]},
                "value": {
                    "type": "string", "minLength": 1,
                    "maxLength": row["target_characters"],
                    "description": cap_description,
                },
            },
        })
    repair_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["targets"],
        "properties": {
            "targets": {
                "type": "array", "minItems": len(targets), "maxItems": len(targets),
                "items": {"oneOf": patch_variants},
            },
        },
    }
    repair_messages = [{
        "role": "system",
        "content": (
            "HARD LENGTH TASK: rewrite every listed value MUCH SHORTER, never echo or expand it. "
            "Use exactly one concise sentence or production phrase per value. Never exceed its "
            "target_characters or maximum_words. Preserve every named actor, "
            "count, direction, ordered action, end state, canonical English name, and "
            "S-ID. Do not add facts. Return JSON only."
        ),
    }, {
        "role": "user",
        "content": (
            "MANDATORY: summarize each TARGET into ONE concise sentence or phrase, at or below "
            "target_characters and (when present) maximum_words. These are hard caps; count before "
            "returning. Keep each field's language. Return exactly one targets array with one "
            "field_path/value object for every supplied path; do not omit, duplicate, "
            "or add paths.\nTARGETS_JSON:\n"
            + json.dumps(target_rows, ensure_ascii=False, separators=(",", ":")) + "\n"
            + _schema_instruction(repair_schema)
            + "\nFINAL CHECK: every value must be one concise sentence and no longer than its "
            "target_characters or maximum_words. Return JSON only."
        ),
    }]
    content, metadata = _chat_completion(
        messages=repair_messages,
        max_tokens=min(
            request.max_tokens,
            max(512, sum(row["target_characters"] for row in target_rows) * 2),
        ),
        operation="creative_storyboard_repair",
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    try:
        patch = json.loads(protocol._clean_translation(content))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DeepSeekAPIError(
            "DeepSeek API returned invalid repair JSON.",
            code="creative_storyboard_repair_invalid_json",
            runtime_metadata={
                "operation": protocol.CREATIVE_OPERATION,
                "repair_attempted": True,
                "repair_succeeded": False,
                "repair_reason_code": "field_too_long",
            },
        ) from exc
    patch_rows = patch.get("targets") if isinstance(patch, dict) else None
    valid_patch = (
        isinstance(patch, dict)
        and set(patch) == {"targets"}
        and isinstance(patch_rows, list)
        and len(patch_rows) == len(targets)
    )
    seen_paths: set[str] = set()
    if valid_patch:
        for row in patch_rows:
            if not isinstance(row, dict) or set(row) != {"field_path", "value"}:
                valid_patch = False
                break
            field_path = row.get("field_path")
            value = row.get("value")
            target = target_by_path.get(field_path)
            if (
                target is None or field_path in seen_paths
                or not isinstance(value, str) or not value.strip()
                or len(value) > target[2]
            ):
                valid_patch = False
                break
            seen_paths.add(field_path)
    valid_patch = valid_patch and seen_paths == set(target_by_path)
    if not valid_patch:
        first_path = target_rows[0]["field_path"].removeprefix("result.")
        raise DeepSeekAPIError(
            "The structured repair candidate failed validation.",
            code="creative_storyboard_repair_validation_failed",
            runtime_metadata={
                "operation": protocol.CREATIVE_OPERATION,
                "repair_attempted": True,
                "repair_succeeded": False,
                "repair_reason_code": "field_too_long",
                "validation_error_code": "field_too_long",
                "validation_path": first_path,
            },
        )
    repaired = copy.deepcopy(decoded)
    for row in patch_rows:
        shot_index, field, _limit = target_by_path[row["field_path"]]
        repaired["shots"][shot_index][field] = row["value"]
    return repaired, metadata


def run_structured_operation(request_payload: dict[str, Any]) -> dict[str, Any]:
    """Run a validated creative storyboard or shot-detail proposal."""
    config = _require_config()
    protocol = _protocol_module()
    operation = str(request_payload.get("operation") or "").strip().lower()
    if operation == protocol.CREATIVE_OPERATION:
        normalise = protocol._normalise_creative_request
        build_messages = lambda request: _deepseek_creative_messages(protocol, request)
        schema_builder = lambda request: _deepseek_creative_response_schema(
            protocol, request,
        )
        validator = protocol._validate_creative_storyboard
    elif operation == protocol.SHOT_DETAIL_OPERATION:
        normalise = protocol._normalise_shot_high_detail_request
        build_messages = protocol._build_shot_high_detail_messages
        schema_builder = lambda _request: protocol.SHOT_DETAIL_DRAFT_SCHEMA
        validator = protocol._validate_shot_high_detail
    else:
        raise DeepSeekAPIError(
            "The structured operation is unsupported.",
            code="deepseek_unsupported_operation",
        )
    try:
        request = normalise(request_payload)
    except Exception as exc:
        raise DeepSeekAPIError(
            "The structured request is invalid.", code=f"{operation}_invalid_request",
        ) from exc
    schema = schema_builder(request)
    messages = [*build_messages(request), {"role": "user", "content": _schema_instruction(schema)}]
    started = time.monotonic()
    try:
        content, call_metadata = _chat_completion(
            messages=messages,
            max_tokens=(
                _deepseek_creative_max_tokens(request)
                if operation == protocol.CREATIVE_OPERATION else request.max_tokens
            ),
            operation=operation, response_format={"type": "json_object"},
            temperature=0.3,
        )
        decoded = json.loads(protocol._clean_translation(content))
    except DeepSeekAPIError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DeepSeekAPIError(
            "DeepSeek API returned invalid JSON.", code=f"{operation}_output_invalid_json",
            runtime_metadata={
                "operation": operation,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        ) from exc
    review_issues: list[dict[str, str]] = []
    repair_attempted = False
    repair_succeeded = False
    repair_reason_code = ""
    try:
        candidate = validator(decoded, request, review_issues=review_issues)
        if operation == protocol.CREATIVE_OPERATION:
            review_issues = _deepseek_review_issues(review_issues)
    except Exception as first_exc:
        targets = (
            _creative_length_repair_targets(first_exc, protocol, decoded)
            if operation == protocol.CREATIVE_OPERATION else None
        )
        if targets is None:
            diagnostic = _validation_diagnostic(first_exc)
            raise DeepSeekAPIError(
                "The structured candidate failed validation.",
                code=f"{operation}_validation_failed",
                runtime_metadata={
                    "operation": operation,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "review_required": bool(review_issues),
                    "review_issue_count": len(review_issues),
                    "repair_attempted": False,
                    **diagnostic,
                },
            ) from first_exc
        repair_attempted = True
        repair_reason_code = "field_too_long"
        decoded, repair_metadata = _repair_creative_length_once(
            decoded=decoded, targets=targets, request=request, protocol=protocol,
        )
        call_metadata = {
            **call_metadata,
            "usage": _merge_usage(
                _safe_usage(call_metadata.get("usage")),
                _safe_usage(repair_metadata.get("usage")),
            ),
            "attempts": int(call_metadata.get("attempts", 0))
            + int(repair_metadata.get("attempts", 0)),
        }
        review_issues = []
        try:
            candidate = validator(decoded, request, review_issues=review_issues)
            review_issues = _deepseek_review_issues(review_issues)
            repair_succeeded = True
        except Exception as repair_exc:
            diagnostic = _validation_diagnostic(repair_exc)
            raise DeepSeekAPIError(
                "The structured repair candidate failed validation.",
                code=f"{operation}_repair_validation_failed",
                runtime_metadata={
                    "operation": operation,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "review_required": bool(review_issues),
                    "review_issue_count": len(review_issues),
                    "repair_attempted": True,
                    "repair_succeeded": False,
                    "repair_reason_code": repair_reason_code,
                    **diagnostic,
                },
            ) from repair_exc
    # The source fingerprint is server-owned.  The model cannot forge or omit
    # it.  Creative's shared validator places it in the result like the local
    # runtime; shot detail keeps its strict three-key result and echoes it only
    # in the surrounding envelope, also matching the local runtime.
    candidate = dict(candidate)
    if operation == protocol.CREATIVE_OPERATION:
        candidate["source_fingerprint"] = request.source_fingerprint
    elapsed = round((time.monotonic() - started) * 1000)
    if operation == protocol.SHOT_DETAIL_OPERATION:
        review_summary = protocol._shot_detail_review_summary(review_issues)
    else:
        review_summary = {
            "status": "needs_review" if review_issues else "ready",
            "issue_count": len(review_issues),
            "risk_count": sum(i.get("severity") == "risk" for i in review_issues),
            "requires_edit_count": sum(
                i.get("severity") == "requires_edit" for i in review_issues
            ),
        }
    metadata = _safe_metadata({
        **call_metadata, "provider": PROVIDER_ID, "model": config["model"],
        "operation": operation, "elapsed_ms": elapsed,
        "review_required": bool(review_issues),
        "review_issue_count": len(review_issues),
        "repair_attempted": repair_attempted,
        "repair_succeeded": repair_succeeded,
        "repair_reason_code": repair_reason_code,
    })
    return {
        "ok": True, "request_id": request.request_id, "operation": operation,
        "result": candidate, "source_fingerprint": request.source_fingerprint,
        "model": config["model"], "elapsed_ms": elapsed,
        "metadata": metadata, "review_issues": review_issues,
        "review_summary": review_summary,
    }
