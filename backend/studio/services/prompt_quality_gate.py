from __future__ import annotations

from dataclasses import dataclass

from studio.models import PromptArtifact


@dataclass(frozen=True)
class PromptLanguageGateReport:
    """Production-language decision for one persisted prompt artifact."""

    mode: str
    applies: bool
    requires_english_rewrite: bool
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
            "fields_requiring_english_rewrite": list(self.fields_requiring_english_rewrite),
            "hard_block_fields": list(self.hard_block_fields),
            "message": message,
        }


def evaluate_prompt_language_gate(prompt: PromptArtifact) -> PromptLanguageGateReport:
    """Evaluate the enqueue gate without changing preview or prompt persistence."""

    quality = (prompt.structured_payload or {}).get("quality") or {}
    stored_gate = quality.get("production_language_gate") or {}
    mode = str(stored_gate.get("mode") or "block").strip().lower()
    if mode not in {"block", "warn", "advisory"}:
        # Settings validates this at startup. Keep the runtime path fail-safe in
        # case a test or embedding overrides the value after Django is loaded.
        mode = "block"
    applies = prompt.prompt_type in {
        PromptArtifact.PromptType.VIDEO,
        PromptArtifact.PromptType.IMAGE,
    }
    raw_fields = quality.get("fields_requiring_english_rewrite") or []
    fields = tuple(dict.fromkeys(
        str(value).strip() for value in raw_fields if str(value).strip()
    ))
    raw_hard_fields = quality.get("hard_block_fields") or []
    hard_fields = tuple(dict.fromkeys(
        str(value).strip() for value in raw_hard_fields if str(value).strip()
    ))
    requires_rewrite = applies and (bool(hard_fields) or bool(fields))
    return PromptLanguageGateReport(
        mode=mode,
        applies=applies,
        requires_english_rewrite=requires_rewrite,
        fields_requiring_english_rewrite=fields,
        hard_block_fields=hard_fields,
    )
