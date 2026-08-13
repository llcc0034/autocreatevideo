"""Optional, fail-safe semantic rewrite for MiniMax H3 video prompts.

The deterministic H3 compiler remains the source of truth for structure.  This
module may rewrite only the director prose through an explicitly configured
OpenAI-compatible endpoint.  It never sends data anywhere when disabled and it
rejects responses that alter H3 structure or protected authored text.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlparse

from django.conf import settings
import requests


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_PROTECTED_BLOCK_RE = re.compile(
    r"<(d|lyrics|screen_text)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_REFERENCE_LABEL_RE = re.compile(r"<(?:Picture|Subject|Video|Audio)\s+[1-9][0-9]*>")
_SHOT_MARKER_RE = re.compile(
    r"\[Shot\s+[1-9][0-9]*\](?:\s+At\s+\d{2}:\d{2}\.\d{3},?)?"
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?(?![A-Za-z0-9_])")
_SECTION_RE = re.compile(
    r"^(subject_definitions|summary|retention_analysis|detailed_description|"
    r"integrated_multimodal_description|overall_soundscape|non_diegetic_music):",
    flags=re.MULTILINE,
)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", flags=re.IGNORECASE)

_BASE_SECTIONS = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)
_REFERENCE_SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)


class H3PromptOptimizationBlocked(RuntimeError):
    """Raised when strict mode refuses to use an unoptimized prompt."""


@dataclass(frozen=True)
class H3PromptOptimizationResult:
    text: str
    report: dict


def _config() -> dict:
    configured = getattr(settings, "H3_PROMPT_OPTIMIZER", {}) or {}
    return {
        "enabled": bool(configured.get("enabled", False)),
        "base_url": str(configured.get("base_url", "") or "").strip(),
        "api_key": str(configured.get("api_key", "") or "").strip(),
        "model": str(configured.get("model", "") or "").strip(),
        "allow_non_loopback": bool(configured.get("allow_non_loopback", False)),
        "timeout_seconds": float(configured.get("timeout_seconds", 60)),
        "failure_mode": str(configured.get("failure_mode", "warn") or "warn").strip().lower(),
    }


def get_h3_prompt_optimizer_capability() -> dict:
    """Return a read-only, non-secret optimizer configuration snapshot.

    ``configured`` describes whether the current server-side values are
    sufficient for the selected transport.  It is intentionally independent
    from ``enabled`` so an operator can safely stage a configuration before
    switching it on.  Endpoint URLs, credentials, headers, and connectivity
    errors must never cross this capability boundary.
    """

    config = _config()
    parsed = urlparse(config["base_url"])
    valid_endpoint = bool(
        config["base_url"]
        and parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )
    if not valid_endpoint:
        transport = "unconfigured"
    elif _is_non_loopback(config["base_url"]):
        transport = "remote"
    else:
        transport = "local"

    configured = bool(valid_endpoint and config["model"])
    if transport == "remote":
        configured = bool(
            configured
            and parsed.scheme == "https"
            and config["allow_non_loopback"]
            and config["api_key"]
        )

    failure_mode = config["failure_mode"]
    if failure_mode not in {"warn", "block"}:
        failure_mode = "warn"
    safe_model = re.sub(r"[\x00-\x1f\x7f]", "", config["model"])[:200]
    language_gate = str(
        getattr(settings, "H3_PROMPT_LANGUAGE_GATE", "block")
    ).strip().lower()
    if language_gate not in {"warn", "block"}:
        language_gate = "block"
    return {
        "enabled": config["enabled"],
        "configured": configured,
        "transport": transport,
        "model": safe_model,
        "failure_mode": failure_mode,
        "language_gate": language_gate,
    }


def h3_prompt_optimizer_input_fingerprint() -> str:
    """Return a non-secret signature for settings that can change prompt text.

    Prompt artifacts may be reused by production only when the editor inputs
    and the optimizer contract are unchanged.  Hashing the endpoint keeps the
    persisted fingerprint useful without exposing a configured local/remote
    URL or credentials.  API-key rotation is intentionally excluded because it
    changes authentication, not the requested rewrite semantics.
    """

    config = _config()
    payload = {
        "version": 1,
        "enabled": config["enabled"],
        "endpoint_sha256": (
            hashlib.sha256(config["base_url"].encode("utf-8")).hexdigest()
            if config["base_url"] else ""
        ),
        "model": config["model"],
        "allow_non_loopback": config["allow_non_loopback"],
        "timeout_seconds": config["timeout_seconds"],
        "failure_mode": config["failure_mode"],
        "language_gate": str(
            getattr(settings, "H3_PROMPT_LANGUAGE_GATE", "block")
        ).strip().lower(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _report(status: str, config: dict, *, message: str = "", validation_errors=None) -> dict:
    return {
        "status": status,
        "enabled": config["enabled"],
        "provider": "openai_compatible",
        "model": config["model"],
        "transport": "remote" if _is_non_loopback(config["base_url"]) else "local",
        "failure_mode": config["failure_mode"],
        "message": message,
        "validation_errors": list(validation_errors or []),
    }


def _is_non_loopback(value: str) -> bool:
    hostname = (urlparse(value).hostname or "").strip("[]").lower()
    return bool(hostname and hostname not in {"localhost", "127.0.0.1", "::1"})


def _endpoint(base_url: str) -> str:
    value = base_url.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return f"{value}/chat/completions"
    return f"{value}/v1/chat/completions"


def _protect_authored_blocks(text: str) -> tuple[str, dict[str, str]]:
    blocks: dict[str, str] = {}

    def replace(match: re.Match) -> str:
        token = f"__H3_PROTECTED_BLOCK_{len(blocks) + 1:04d}__"
        blocks[token] = match.group(0)
        return token

    return _PROTECTED_BLOCK_RE.sub(replace, text), blocks


def _without_protected_blocks(text: str) -> str:
    return _PROTECTED_BLOCK_RE.sub("", text)


def _structural_markers(text: str) -> list[str]:
    markers = []
    for line in text.splitlines():
        stripped = line.lstrip()
        section = _SECTION_RE.match(stripped)
        if section:
            markers.append(f"section:{section.group(1)}")
        elif stripped.startswith("- "):
            markers.append("list-item")
        elif stripped.startswith("[Shot "):
            match = _SHOT_MARKER_RE.match(stripped)
            markers.append(f"shot:{match.group(0) if match else 'INVALID'}")
        elif not stripped:
            markers.append("blank")
        else:
            markers.append("prose")
    return markers


def _ordered_tokens_by_line(text: str, pattern: re.Pattern) -> tuple[tuple[str, ...], ...]:
    """Return token occurrences in order while preserving their source line.

    Counters are insufficient for H3 prompts: swapping ``<Subject 1>`` with
    ``<Subject 2>`` keeps the same multiset but silently changes identity.  The
    optimizer may rewrite prose, but structural tokens must remain on the same
    line and in the same left-to-right order.
    """
    return tuple(tuple(pattern.findall(line)) for line in text.splitlines())


def _validate_rewrite(original: str, candidate: str, mode: str, blocks: dict[str, str]) -> list[str]:
    errors: list[str] = []
    expected_sections = _REFERENCE_SECTIONS if str(mode).casefold() == "ref2va" else _BASE_SECTIONS
    original_sections = tuple(_SECTION_RE.findall(original))
    candidate_sections = tuple(_SECTION_RE.findall(candidate))
    if original_sections != expected_sections:
        errors.append("源提示词的 H3 section 结构不符合当前模式")
    if candidate_sections != original_sections:
        errors.append("优化结果修改了 H3 section 名称或顺序")
    if _structural_markers(candidate) != _structural_markers(original):
        errors.append("优化结果修改了分行、列表或镜头结构")
    if _ordered_tokens_by_line(candidate, _SHOT_MARKER_RE) != _ordered_tokens_by_line(
        original, _SHOT_MARKER_RE,
    ):
        errors.append("优化结果修改、重排或跨行移动了镜头编号/时间戳")
    if _ordered_tokens_by_line(candidate, _REFERENCE_LABEL_RE) != _ordered_tokens_by_line(
        original, _REFERENCE_LABEL_RE,
    ):
        errors.append("优化结果修改、重排或跨行移动了 Picture/Subject/Video/Audio 标签")
    if _ordered_tokens_by_line(candidate, _NUMBER_RE) != _ordered_tokens_by_line(
        original, _NUMBER_RE,
    ):
        errors.append("优化结果修改、重排或跨行移动了数字")
    for token in blocks:
        if candidate.count(token) != 1:
            errors.append(f"优化结果修改了受保护内容占位符 {token}")
    unknown_tokens = set(re.findall(r"__H3_PROTECTED_BLOCK_\d{4}__", candidate)) - set(blocks)
    if unknown_tokens:
        errors.append("优化结果新增了未知受保护内容占位符")
    director_text = candidate
    for token in blocks:
        director_text = director_text.replace(token, "")
    if _CJK_RE.search(director_text):
        errors.append("优化结果的导演描述仍包含中文")
    if len(candidate) > 7_000:
        errors.append("优化结果超过 H3 7000 字符上限")
    return errors


def _parse_content(response: requests.Response) -> str:
    payload = response.json()
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("响应缺少 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise ValueError("响应缺少 message.content")
    raw = _JSON_FENCE_RE.sub("", content.strip())
    decoded = json.loads(raw)
    prompt = decoded.get("prompt") if isinstance(decoded, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("响应 JSON 缺少 prompt 字符串")
    return prompt.strip()


def _blocked_or_result(text: str, config: dict, status: str, message: str, *, errors=None):
    report = _report(status, config, message=message, validation_errors=errors)
    if config["failure_mode"] == "block":
        raise H3PromptOptimizationBlocked(message)
    return H3PromptOptimizationResult(text=text, report=report)


def optimize_h3_video_prompt(text: str, *, mode: str) -> H3PromptOptimizationResult:
    """Rewrite H3 director prose when explicitly configured.

    Disabled mode is guaranteed not to issue a request.  In ``warn`` mode every
    failure returns the original deterministic prompt with an honest report;
    ``block`` mode raises instead.
    """
    config = _config()
    if config["failure_mode"] not in {"warn", "block"}:
        config["failure_mode"] = "warn"
    has_cjk_director_prose = bool(_CJK_RE.search(_without_protected_blocks(text)))
    if not config["enabled"]:
        if not has_cjk_director_prose:
            return H3PromptOptimizationResult(
                text=text,
                report=_report("not_required", config, message="导演描述已经是英文，未调用优化器。"),
            )
        return _blocked_or_result(
            text,
            config,
            "disabled",
            "提示词优化器未启用；未向任何外部服务发送剧本，当前导演描述仍需英文改写。",
        )

    parsed = urlparse(config["base_url"])
    if not config["base_url"] or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return _blocked_or_result(
            text, config, "misconfigured", "提示词优化器 BASE_URL 未配置或无效。"
        )
    if not config["model"]:
        return _blocked_or_result(
            text, config, "misconfigured", "提示词优化器 MODEL 未配置。"
        )
    if _is_non_loopback(config["base_url"]):
        if not config["allow_non_loopback"]:
            return _blocked_or_result(
                text,
                config,
                "misconfigured",
                "远程提示词优化器未获授权；需显式启用 ALLOW_NON_LOOPBACK。",
            )
        if parsed.scheme != "https":
            return _blocked_or_result(
                text, config, "misconfigured", "远程提示词优化器必须使用 HTTPS。"
            )
        if not config["api_key"]:
            return _blocked_or_result(
                text, config, "misconfigured", "远程提示词优化器缺少 API_KEY。"
            )

    protected_text, blocks = _protect_authored_blocks(text)
    system_prompt = (
        "You rewrite MiniMax H3 video prompts. Return one JSON object with exactly one key named prompt. "
        "Rewrite director prose into precise, concise English while preserving meaning. Do not invent or "
        "remove characters, objects, actions, locations, sounds, identities, relationships, or events. "
        "Preserve every section name and order, line break, list marker, [Shot N] marker, timestamp, number, "
        "reference label, and __H3_PROTECTED_BLOCK_NNNN__ token exactly. Protected tokens contain authored "
        "dialogue, lyrics, or visible scene text and must not be translated, moved, duplicated, or removed. "
        "Do not use Markdown fences and do not add commentary."
    )
    request_payload = {
        "model": config["model"],
        "temperature": 0,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {"mode": mode, "prompt": protected_text}, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if config["api_key"]:
        headers["Authorization"] = f"Bearer {config['api_key']}"
    try:
        response = requests.post(
            _endpoint(config["base_url"]),
            headers=headers,
            json=request_payload,
            timeout=config["timeout_seconds"],
        )
        response.raise_for_status()
        candidate = _parse_content(response)
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
        return _blocked_or_result(
            text,
            config,
            "failed_request",
            "提示词优化器调用失败或返回格式无效；已保留本地编译的原提示词。",
        )

    errors = _validate_rewrite(protected_text, candidate, mode, blocks)
    if errors:
        return _blocked_or_result(
            text,
            config,
            "failed_validation",
            "提示词优化结果未通过结构/语言安全校验；已保留原提示词。",
            errors=errors,
        )
    for token, authored in blocks.items():
        candidate = candidate.replace(token, authored)
    return H3PromptOptimizationResult(
        text=candidate,
        report=_report(
            "optimized",
            config,
            message="导演描述已由配置的优化器改写为英文；对白、歌词和屏显文字保持原文。",
        ),
    )
