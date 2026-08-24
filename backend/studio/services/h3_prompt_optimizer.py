"""Deterministic H3 prompt optimization boundary.

The former environment-driven HTTP optimizer was retired because it bypassed
the versioned Provider/ModelDeployment catalog, frozen ProductionRun inputs,
cost authorization and the shared SSRF transport. H3 compilation therefore
never performs an implicit network request. Project users can normalize source
fields through the reviewed ``prompt.normalize`` capability before starting a
new ProductionRun.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_PROTECTED_BLOCK_RE = re.compile(
    r"<(d|lyrics|screen_text)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)


class H3PromptOptimizationBlocked(RuntimeError):
    """Raised only when an explicit caller requests strict disabled behavior."""


@dataclass(frozen=True)
class H3PromptOptimizationResult:
    text: str
    report: dict


def h3_prompt_optimizer_input_fingerprint(
    *, deployment_fingerprint: str = "", language_gate: str = "block",
) -> str:
    """Hash the explicit policy; no endpoint or secret is read implicitly."""

    payload = {
        "version": 2,
        "automatic_optimizer": "retired",
        "deployment_fingerprint": str(deployment_fingerprint or ""),
        "language_gate": str(language_gate or "block").strip().lower(),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def optimize_h3_video_prompt(
    text: str,
    *,
    mode: str,
    deployment=None,
    failure_mode: str = "warn",
) -> H3PromptOptimizationResult:
    """Return the deterministic prompt without issuing a network request."""

    del mode, deployment
    director_text = _PROTECTED_BLOCK_RE.sub("", str(text or ""))
    requires_rewrite = bool(_CJK_RE.search(director_text))
    report = {
        "status": "disabled" if requires_rewrite else "not_required",
        "enabled": False,
        "provider": "none",
        "model": "",
        "transport": "none",
        "failure_mode": "block" if failure_mode == "block" else "warn",
        "message": (
            "自动 H3 外发优化器已退役；请先通过受审 prompt.normalize 流程改写导演字段。"
            if requires_rewrite
            else "导演描述已经是英文，未调用任何优化器。"
        ),
        "validation_errors": [],
    }
    if requires_rewrite and failure_mode == "block":
        raise H3PromptOptimizationBlocked(report["message"])
    return H3PromptOptimizationResult(text=str(text or ""), report=report)
