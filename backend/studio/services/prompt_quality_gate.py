from __future__ import annotations

from dataclasses import dataclass
import re

from django.conf import settings

from studio.models import PromptArtifact


BIBLE_IMAGE_CANDIDATE_QUALITY_POLICY = "bible_image_candidate_advisory_v1"
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)?")


@dataclass(frozen=True)
class PromptLanguageGateReport:
    """Production-language decision for one persisted prompt artifact."""

    mode: str
    applies: bool
    requires_english_rewrite: bool
    optimizer_status: str
    fields_requiring_english_rewrite: tuple[str, ...]
    hard_block_fields: tuple[str, ...]

    @property
    def blocks_enqueue(self) -> bool:
        if self.mode == "advisory":
            return False
        return self.applies and self.requires_english_rewrite and (
            bool(self.hard_block_fields) or self.mode == "block"
        )

    def as_dict(self) -> dict:
        if not self.applies:
            message = "该提示词类型不适用当前生产语言门禁。"
            status = "not_applicable"
        elif self.blocks_enqueue:
            message = (
                "生产提交已阻断：以下导演字段仍需英文改写；对白、歌词和真实屏显文字无需翻译。"
            )
            status = "blocked"
        elif self.mode == "advisory" and self.requires_english_rewrite:
            message = (
                "实体参考图提示词存在待人工审阅项；该策略仅提示，不阻断排队或重试。"
            )
            status = "warning"
        elif self.requires_english_rewrite:
            message = (
                "生产语言门禁当前为 warn：任务允许提交，但以下导演字段仍建议先改写为英文。"
            )
            status = "warning"
        else:
            message = "生产提示词语言门禁检查通过。"
            status = "passed"
        return {
            "mode": self.mode,
            "status": status,
            "applies": self.applies,
            "would_block_enqueue": self.blocks_enqueue,
            "optimizer_status": self.optimizer_status,
            "fields_requiring_english_rewrite": list(self.fields_requiring_english_rewrite),
            "hard_block_fields": list(self.hard_block_fields),
            "message": message,
        }


def evaluate_prompt_language_gate(prompt: PromptArtifact) -> PromptLanguageGateReport:
    """Evaluate the enqueue gate without changing preview or prompt persistence."""

    quality = (prompt.structured_payload or {}).get("quality") or {}
    policy_id = str(quality.get("policy_id") or "")
    mode = (
        "advisory"
        if policy_id == BIBLE_IMAGE_CANDIDATE_QUALITY_POLICY
        else str(getattr(settings, "H3_PROMPT_LANGUAGE_GATE", "block")).strip().lower()
    )
    if mode not in {"block", "warn", "advisory"}:
        # Settings validates this at startup. Keep the runtime path fail-safe in
        # case a test or embedding overrides the value after Django is loaded.
        mode = "block"
    applies = prompt.prompt_type in {
        PromptArtifact.PromptType.VIDEO,
        PromptArtifact.PromptType.IMAGE,
    }
    optimizer = quality.get("optimizer") or {}
    optimizer_status = str(optimizer.get("status") or "not_configured")
    raw_fields = quality.get("fields_requiring_english_rewrite") or []
    fields = tuple(dict.fromkeys(
        str(value).strip() for value in raw_fields if str(value).strip()
    ))
    raw_hard_fields = quality.get("hard_block_fields") or []
    hard_fields = tuple(dict.fromkeys(
        str(value).strip() for value in raw_hard_fields if str(value).strip()
    ))
    requires_rewrite = applies and (
        bool(hard_fields) or (bool(fields) and optimizer_status != "optimized")
    )
    return PromptLanguageGateReport(
        mode=mode,
        applies=applies,
        requires_english_rewrite=requires_rewrite,
        optimizer_status=optimizer_status,
        fields_requiring_english_rewrite=fields,
        hard_block_fields=hard_fields,
    )


def build_bible_image_candidate_quality(
    *,
    prompt_text: str,
    source_fields: dict[str, str],
    visual_fallback_source: str,
    canonical_collisions: list[dict] | tuple[dict, ...],
) -> dict:
    """Return the one advisory-only quality policy for Bible image candidates."""

    issues: list[dict[str, str]] = []
    cjk_fields = [
        label
        for label, value in source_fields.items()
        if isinstance(value, str) and value and _CJK_RE.search(value)
    ]
    if visual_fallback_source != "visual_identity":
        issues.append({
            "code": "bible_image_visual_identity_fallback",
            "severity": "warning",
            "path": "bible.visual_identity",
            "message": (
                f"视觉锚点为空，本次按 {visual_fallback_source} 回退构建；"
                "请在确认图片前人工检查身份稳定性。"
            ),
        })
    non_empty_source_count = sum(bool(str(value or "").strip()) for value in source_fields.values())
    if non_empty_source_count < 2:
        issues.append({
            "code": "bible_image_source_fields_sparse",
            "severity": "warning",
            "path": "bible",
            "message": "实体可用的视觉字段较少，候选可能缺乏稳定细节。",
        })
    if cjk_fields:
        issues.append({
            "code": "bible_image_director_text_contains_cjk",
            "severity": "warning",
            "path": "prompt",
            "message": "进入 SDXL 的导演描述仍含中文：" + "、".join(cjk_fields),
        })
    prompt_word_count = len(_WORD_RE.findall(prompt_text))
    if prompt_word_count > 75:
        issues.append({
            "code": "bible_image_prompt_word_budget_high",
            "severity": "warning",
            "path": "prompt",
            "message": (
                f"正向提示词约含 {prompt_word_count} 个英文词；"
                "靠后细节可能超出 SDXL 文本编码有效预算。"
            ),
        })
    if canonical_collisions:
        issues.append({
            "code": "bible_image_canonical_target_collision",
            "severity": "warning",
            "path": "canonical_glossary",
            "message": "检测到跨实体 canonical English target 碰撞，请人工核对身份。",
        })
    warnings = [issue["message"] for issue in issues]
    return {
        "policy_id": BIBLE_IMAGE_CANDIDATE_QUALITY_POLICY,
        "status": "needs_attention" if issues else "ready",
        "advisory_only": True,
        "would_block_enqueue": False,
        "prompt_character_count": len(prompt_text),
        "prompt_word_count": prompt_word_count,
        "prompt_word_count_is_approximate": True,
        "visual_fallback_source": visual_fallback_source,
        "issues": issues,
        "warnings": warnings,
        "integrity_errors": list(canonical_collisions),
        "fields_requiring_english_rewrite": cjk_fields,
        "hard_block_fields": [],
        "source_fields_optimized": [],
        "optimizer": {
            "status": "not_configured" if cjk_fields else "not_required",
            "enabled": False,
            "provider": "none",
            "model": "",
            "transport": "local",
            "failure_mode": "warn",
            "message": (
                "实体图片候选采用 advisory 策略；系统不会伪装成已完成英文优化。"
                if cjk_fields else "当前实体图片导演描述无需自动英文改写。"
            ),
            "validation_errors": [],
        },
        "production_language_gate": {
            "mode": "advisory",
            "would_block_enqueue": False,
            "message": "Advisory only; enqueue and retry remain available for human review.",
        },
    }
