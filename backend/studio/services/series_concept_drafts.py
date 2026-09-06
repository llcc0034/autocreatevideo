"""Strict series-script contract for versioned text deployments.

This module freezes provider-neutral request plans, validates detached model
results, and deterministically renders the accepted structure into the one
master-script syntax understood by ``script_breakdown.py``. Provider execution,
cost accounting, persistence, and review remain in the ProductionRun pipeline.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from copy import deepcopy
from typing import Any

from studio.services.model_profile_contracts import CHAT_REQUEST_OPTIONS_KEY
from studio.services.script_bible import (
    ScriptBibleParseError,
    normalize_script_bible_name,
    parse_script_bible,
    render_script_bible,
    validate_script_bible_ir,
)
from studio.services.text_field_contracts import (
    SERIES_CONCEPT_FIELD_KEYS,
    text_field_rule,
    validate_text_field_contract,
)


SERIES_CONCEPT_DRAFT_OPERATION = "series_concept_draft"
SERIES_CONCEPT_DRAFT_SCHEMA_VERSION = 3
SERIES_CONCEPT_TEMPLATE_ID = "deterministic-script-breakdown-v4"
SERIES_CONCEPT_RESOURCE_ADVISORY_KEYS = frozenset({
    "schema_version",
    "input_protocol_overhead_chars",
    "output_base_units",
    "output_units_per_shot",
    "minimum_output_units",
    "output_hint_warning_percent",
    "script_breakdown_reference_chars",
})
DIALOGUE_IR_KEYS = frozenset({
    "speaker", "delivery", "text", "start_ms", "end_ms",
})


class SeriesConceptDraftError(RuntimeError):
    """A caller-safe failure in the current detached script contract."""

    def __init__(self, message: str, *, code: str = "series_concept_invalid_candidate"):
        super().__init__(message)
        self.code = str(code)


class SeriesConceptDraftValidationError(SeriesConceptDraftError):
    """Invalid operator input or frozen production policy."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "series_concept_validation_failed",
        public_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.public_context = dict(public_context or {})


def _source_fingerprint(
    *,
    idea: str,
    style_hint: str,
    episode_count: int,
    shots_per_episode: int,
    episode_duration_seconds: int,
    minimum_shot_duration_ms: int,
    maximum_shot_duration_ms: int,
) -> str:
    source = {
        "schema_version": SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
        "idea": idea,
        "style_hint": style_hint,
        "episode_count": episode_count,
        "shots_per_episode": shots_per_episode,
        "episode_duration_seconds": episode_duration_seconds,
        "minimum_shot_duration_ms": minimum_shot_duration_ms,
        "maximum_shot_duration_ms": maximum_shot_duration_ms,
    }
    canonical = json.dumps(
        source, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _format_seconds(milliseconds: int) -> str:
    if milliseconds % 1_000 == 0:
        return str(milliseconds // 1_000)
    return f"{milliseconds / 1_000:.3f}".rstrip("0").rstrip(".")


def validate_dialogue_clock_contract(
    dialogue: object,
    *,
    duration_ms: int,
) -> tuple[int, int] | None:
    """Validate the model-neutral one-line dialogue timing contract.

    Every dialogue object has one shape.  A silent shot uses three empty
    strings and the explicit ``0/0`` sentinel.  A spoken line must carry its
    authored integer-millisecond window inside the shot.  Missing clocks are
    deliberately rejected: silently inventing a midpoint here would erase the
    timing decision made by the text-model contract.
    """

    if not isinstance(dialogue, dict) or set(dialogue) != DIALOGUE_IR_KEYS:
        raise ValueError("dialogue 必须包含统一的文本与 start_ms/end_ms 字段")
    speaker = dialogue.get("speaker")
    delivery = dialogue.get("delivery")
    text = dialogue.get("text")
    if not all(isinstance(value, str) for value in (speaker, delivery, text)):
        raise ValueError("dialogue 文本字段必须是字符串")
    start_ms = dialogue.get("start_ms")
    end_ms = dialogue.get("end_ms")
    if type(start_ms) is not int or type(end_ms) is not int:
        raise ValueError("dialogue.start_ms/end_ms 必须是整数毫秒")
    has_dialogue = bool(text)
    if bool(speaker) != has_dialogue or bool(delivery) != has_dialogue:
        raise ValueError("dialogue 人物、语气与正文必须同时为空或同时非空")
    if not has_dialogue:
        if start_ms != 0 or end_ms != 0:
            raise ValueError("空对白必须显式使用 start_ms=0、end_ms=0")
        return None
    if not 0 <= start_ms < end_ms <= duration_ms:
        raise ValueError(
            "有声对白必须满足 0 <= start_ms < end_ms <= shot.duration_ms"
        )
    return start_ms, end_ms


def render_compatible_master_script(
    bible: dict[str, list[dict[str, str]]],
    episodes: list[dict[str, Any]],
) -> str:
    """Render validated IR into the parser's canonical explicit-marker form."""

    blocks: list[str] = [render_script_bible(bible)]
    for episode_index, episode in enumerate(episodes, start=1):
        lines = [f"第{episode_index}集：{episode['title']}"]
        shots = episode["shots"]
        for shot_index, shot in enumerate(shots, start=1):
            shot_duration_ms = shot["duration_ms"]
            duration = _format_seconds(shot_duration_ms)
            first_continuity = "是" if shot_index > 1 else "否"
            last_continuity = "是" if shot_index < len(shots) else "否"
            lines.append(
                f"镜头{shot_index}【时长={duration}秒】"
                f"【首帧连续={first_continuity}】"
                f"【尾帧连续={last_continuity}】：{shot['setting']}"
            )
            dialogue = shot["dialogue"]
            dialogue_clock = validate_dialogue_clock_contract(
                dialogue,
                duration_ms=shot_duration_ms,
            )
            lines.append(f"[0-{duration}秒] 动作：{shot['action']}")
            if dialogue_clock is not None:
                dialogue_start_ms, dialogue_end_ms = dialogue_clock
                lines.append(
                    f"[{_format_seconds(dialogue_start_ms)}-"
                    f"{_format_seconds(dialogue_end_ms)}秒] {dialogue['speaker']}"
                    f"（{dialogue['delivery']}）：{dialogue['text']}"
                )
            if shot_index < len(shots):
                lines.append("")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip()


def _validated_candidate(
    payload: dict[str, Any],
    *,
    fingerprint: str,
    episode_count: int,
    shots_per_episode: int,
    minimum_shot_duration_ms: int,
    maximum_shot_duration_ms: int,
    field_contract: dict[str, Any],
    require_all_bible_entities_used: bool = False,
) -> dict[str, Any]:
    def invalid_candidate(path: str = "result") -> None:
        raise SeriesConceptDraftError(
            f"模型候选字段 {path} 不符合已发布的剧本结构合同。",
            code="series_concept_invalid_candidate",
        )

    def safe_line(
        value: Any,
        *,
        contract_field: str,
        path: str,
    ) -> str:
        rule = text_field_rule(field_contract, contract_field)
        if not isinstance(value, str):
            invalid_candidate(path)
        normalized = value.strip()
        if (
            (rule["required"] and not normalized)
            or len(normalized) > int(rule["max_chars"])
            or (
                rule["single_line"]
                and any(character in normalized for character in "\r\n\t")
            )
            or any(
                unicodedata.category(character) in {"Cc", "Cf"}
                for character in normalized
            )
        ):
            invalid_candidate(path)
        return normalized

    echoed = str(payload.get("source_fingerprint") or "")
    result = payload.get("result")
    if echoed != fingerprint or not isinstance(result, dict):
        raise SeriesConceptDraftError(
            "模型未返回与当前创作想法匹配的结构化候选。",
            code="series_concept_invalid_candidate",
        )
    if (
        set(result) != {"schema_version", "title", "logline", "bible", "episodes"}
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != SERIES_CONCEPT_DRAFT_SCHEMA_VERSION
    ):
        raise SeriesConceptDraftError(
            "模型候选的结构版本无效。",
            code="series_concept_invalid_candidate",
        )
    title = safe_line(result.get("title"), contract_field="title", path="result.title")
    logline = safe_line(result.get("logline"), contract_field="logline", path="result.logline")
    try:
        bible = validate_script_bible_ir(
            result.get("bible"),
            field_contract=field_contract,
        )
    except ScriptBibleParseError:
        invalid_candidate("result.bible")
    episodes = result.get("episodes")
    if (
        not isinstance(episodes, list)
        or len(episodes) != episode_count
    ):
        invalid_candidate("result.episodes")

    validated_episodes: list[dict[str, Any]] = []
    for episode_index, episode in enumerate(episodes):
        episode_path = f"result.episodes[{episode_index}]"
        if not isinstance(episode, dict) or set(episode) != {"title", "shots"}:
            invalid_candidate(episode_path)
        episode_title = safe_line(
            episode.get("title"), contract_field="episode.title",
            path=f"{episode_path}.title",
        )
        shots = episode.get("shots")
        if not isinstance(shots, list) or len(shots) != shots_per_episode:
            invalid_candidate(f"{episode_path}.shots")
        validated_shots: list[dict[str, Any]] = []
        for shot_index, shot in enumerate(shots):
            shot_path = f"{episode_path}.shots[{shot_index}]"
            if (
                not isinstance(shot, dict)
                or set(shot) != {
                    "setting", "action", "dialogue", "duration_ms",
                }
            ):
                invalid_candidate(shot_path)
            duration_ms = shot.get("duration_ms")
            if (
                type(duration_ms) is not int
                or not minimum_shot_duration_ms
                <= duration_ms
                <= maximum_shot_duration_ms
            ):
                invalid_candidate(f"{shot_path}.duration_ms")
            dialogue = shot.get("dialogue")
            if (
                not isinstance(dialogue, dict)
                or set(dialogue) != DIALOGUE_IR_KEYS
            ):
                invalid_candidate(f"{shot_path}.dialogue")
            setting = safe_line(shot.get("setting"), contract_field="shot.setting", path=f"{shot_path}.setting")
            action = safe_line(shot.get("action"), contract_field="shot.action", path=f"{shot_path}.action")
            speaker = safe_line(dialogue.get("speaker"), contract_field="shot.dialogue.speaker", path=f"{shot_path}.dialogue.speaker")
            delivery = safe_line(dialogue.get("delivery"), contract_field="shot.dialogue.delivery", path=f"{shot_path}.dialogue.delivery")
            dialogue_text = safe_line(dialogue.get("text"), contract_field="shot.dialogue.text", path=f"{shot_path}.dialogue.text")
            if (
                any(character in speaker for character in "()（）[]【】<>:：")
                or bool(speaker) != bool(dialogue_text)
                or bool(delivery) != bool(dialogue_text)
                or (
                    delivery
                    and any(ord(character) < 32 or ord(character) > 126 for character in delivery)
                )
                or "(" in delivery
                or ")" in delivery
            ):
                invalid_candidate(f"{shot_path}.dialogue")
            normalized_dialogue = {
                "speaker": speaker,
                "delivery": delivery,
                "text": dialogue_text,
                "start_ms": dialogue.get("start_ms"),
                "end_ms": dialogue.get("end_ms"),
            }
            try:
                validate_dialogue_clock_contract(
                    normalized_dialogue,
                    duration_ms=duration_ms,
                )
            except ValueError:
                invalid_candidate(f"{shot_path}.dialogue")
            validated_shots.append({
                "setting": setting,
                "action": action,
                "duration_ms": duration_ms,
                "dialogue": normalized_dialogue,
            })
        validated_episodes.append({
            "title": episode_title,
            "shots": validated_shots,
        })
    if require_all_bible_entities_used:
        normalized_shot_text = normalize_script_bible_name(" ".join(
            value
            for episode in validated_episodes
            for shot in episode["shots"]
            for value in (
                shot["setting"],
                shot["action"],
                shot["dialogue"]["speaker"],
                shot["dialogue"]["text"],
            )
            if value
        ))
        unused: list[str] = []
        for collection, label in (
            ("characters", "人物"),
            ("scenes", "场景"),
            ("props", "道具"),
        ):
            for item in bible[collection]:
                canonical_name = normalize_script_bible_name(item["name"])
                if canonical_name and canonical_name not in normalized_shot_text:
                    unused.append(f"{label}“{item['name']}”")
        if unused:
            raise SeriesConceptDraftError(
                "模型候选未在任一镜头中使用全部已声明的 Bible 规范名称："
                + "、".join(unused)
                + "。全自动生产禁止把同义改写猜测为同一实体。",
                code="series_concept_bible_entity_unused",
            )
    return {
        "title": title,
        "logline": logline,
        "bible": bible,
        "episodes": validated_episodes,
    }


def _fit_episode_duration_budgets(
    episodes: list[dict[str, Any]],
    *,
    episode_duration_ms: int,
    minimum_shot_duration_ms: int,
) -> tuple[list[int], int]:
    """Preserve model timing unless one episode needs proportional compression."""

    episode_totals: list[int] = []
    adjusted_episode_count = 0
    for episode in episodes:
        shots = episode["shots"]
        proposed = [shot["duration_ms"] for shot in shots]
        proposed_total = sum(proposed)
        if proposed_total <= episode_duration_ms:
            episode_totals.append(proposed_total)
            continue

        minimum_total = len(shots) * minimum_shot_duration_ms
        if minimum_total > episode_duration_ms:
            raise SeriesConceptDraftValidationError(
                "单集时长不足以容纳请求的镜头数和生产系统最短镜头时长。",
                code="series_concept_episode_duration_infeasible",
            )
        available_surplus = episode_duration_ms - minimum_total
        weights = [duration - minimum_shot_duration_ms for duration in proposed]
        total_weight = sum(weights)
        if total_weight <= 0:
            raise SeriesConceptDraftError(
                "模型镜头时长无法在单集预算内安全分配。",
                code="series_concept_invalid_candidate",
            )

        allocated = [minimum_shot_duration_ms] * len(shots)
        remainders: list[tuple[int, int]] = []
        for index, weight in enumerate(weights):
            quotient, remainder = divmod(weight * available_surplus, total_weight)
            allocated[index] += quotient
            remainders.append((remainder, index))
        remaining = episode_duration_ms - sum(allocated)
        for _remainder, index in sorted(
            remainders, key=lambda item: (-item[0], item[1]),
        )[:remaining]:
            allocated[index] += 1

        for shot, duration_ms in zip(shots, allocated):
            previous_duration_ms = int(shot["duration_ms"])
            dialogue = shot["dialogue"]
            dialogue_clock = validate_dialogue_clock_contract(
                dialogue,
                duration_ms=previous_duration_ms,
            )
            if dialogue_clock is not None and duration_ms != previous_duration_ms:
                start_ms, end_ms = dialogue_clock
                scaled_start_ms = start_ms * duration_ms // previous_duration_ms
                scaled_end_ms = (
                    end_ms * duration_ms + previous_duration_ms - 1
                ) // previous_duration_ms
                scaled_end_ms = min(duration_ms, max(scaled_start_ms + 1, scaled_end_ms))
                dialogue["start_ms"] = scaled_start_ms
                dialogue["end_ms"] = scaled_end_ms
            shot["duration_ms"] = duration_ms
        adjusted_episode_count += 1
        episode_totals.append(sum(allocated))
    return episode_totals, adjusted_episode_count


def _validated_resource_advisory(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != SERIES_CONCEPT_RESOURCE_ADVISORY_KEYS:
        raise SeriesConceptDraftValidationError(
            "series concept resource advisory 合同字段无效",
            code="series_concept_resource_advisory_invalid",
        )
    if value.get("schema_version") != 1:
        raise SeriesConceptDraftValidationError(
            "series concept resource advisory schema_version 无效",
            code="series_concept_resource_advisory_invalid",
        )
    normalized: dict[str, int] = {"schema_version": 1}
    for key in sorted(SERIES_CONCEPT_RESOURCE_ADVISORY_KEYS - {"schema_version"}):
        item = value.get(key)
        minimum = 0 if key in {
            "input_protocol_overhead_chars", "output_base_units",
            "output_units_per_shot",
        } else 1
        if type(item) is not int or item < minimum:
            raise SeriesConceptDraftValidationError(
                f"series concept resource advisory.{key} 无效",
                code="series_concept_resource_advisory_invalid",
            )
        normalized[key] = item
    return normalized


def _estimated_output_tokens(shot_count: int, advisory: dict[str, int]) -> int:
    return max(
        advisory["minimum_output_units"],
        advisory["output_base_units"]
        + shot_count * advisory["output_units_per_shot"],
    )


def _resource_estimate(
    *,
    idea: str,
    style_hint: str,
    total_shots: int,
    configured_output_token_hint: int,
    resource_advisory: dict[str, int],
) -> dict[str, int]:
    advisory = _validated_resource_advisory(resource_advisory)
    estimated_output_tokens = _estimated_output_tokens(total_shots, advisory)
    utilization_percent = (
        estimated_output_tokens * 100 + configured_output_token_hint - 1
    ) // configured_output_token_hint
    return {
        "total_shots": total_shots,
        "estimated_input_chars": (
            len(idea) + len(style_hint)
            + advisory["input_protocol_overhead_chars"]
        ),
        "estimated_output_tokens": estimated_output_tokens,
        "configured_output_token_hint": configured_output_token_hint,
        "output_hint_utilization_percent": utilization_percent,
        "output_hint_warning_percent": advisory["output_hint_warning_percent"],
        "script_breakdown_reference_chars": advisory[
            "script_breakdown_reference_chars"
        ],
    }


def _runtime_warnings(
    payload: dict[str, Any],
    *,
    has_dialogue: bool,
    adjusted_episode_count: int,
    resource_estimate: dict[str, int],
    script_char_count: int,
    candidate_warnings: list[str],
) -> list[str]:
    warnings: list[str] = list(candidate_warnings)
    issue_codes = {
        str(issue.get("code") or "")
        for issue in (payload.get("review_issues") or [])
        if isinstance(issue, dict)
    }
    if "series_concept_no_dialogue" in issue_codes:
        warnings.append("候选未包含对白；采用前请确认纯动作叙事是否符合预期。")
    if "model_output_incomplete_finish_reason" in issue_codes:
        warnings.append("提供方标记输出可能未完整结束；请重点核对末集和末镜内容。")
    if "series_concept_structure_repaired" in issue_codes:
        warnings.append(
            "模型首轮结构未通过严格校验，运行时已完成一次结构修复；"
            "请人工重点核对修复后的末集、末镜和时长。"
        )
    if "series_concept_structure_whitespace_recovered" in issue_codes:
        warnings.append(
            "模型结构化输出包含可确定恢复的字符串空白，"
            "运行时已按结构规则恢复；请人工确认对白和动作文本。"
        )
    if adjusted_episode_count:
        warnings.append(
            f"{adjusted_episode_count} 集的模型建议镜头时长超过单集预算；"
            "服务端已按各镜建议时长的相对比例压缩，保留每镜生产最短时长，"
            "并将有声对白时间窗按所在镜头的同比例同步缩放。"
        )
    if (
        resource_estimate["output_hint_utilization_percent"]
        >= resource_estimate["output_hint_warning_percent"]
    ):
        warnings.append(
            "本次完整候选预计需要 "
            f"{resource_estimate['estimated_output_tokens']} 个输出 token，约为所选通道"
            f"配置提示值的 {resource_estimate['output_hint_utilization_percent']}%；"
            "本次候选按一次完整业务请求处理，不截断、不按集/镜分批；"
            "本地运行时只可能因首轮结构失败再生成一次完整候选；"
            "实际能否完成由所选模型和运行时决定，"
            "耗时、内存或上游费用可能较高。"
        )
    reference_chars = resource_estimate["script_breakdown_reference_chars"]
    if script_char_count > reference_chars:
        warnings.append(
            f"完整母稿共 {script_char_count} 字符，超过当前拆解服务配置的 "
            f"{reference_chars} 字符参考预算；"
            "候选已完整返回，未截断、未分批。"
            "人工确认后若要自动分集分镜，请先由管理员调整拆解服务预算。"
        )
    if has_dialogue:
        warnings.append(
            "候选包含对白；创建项目后请先人工确认并应用 SCRIPT_BIBLE_V1 人物候选，"
            "再执行自动分集分镜，否则未确认的实体会明确阻止拆解。"
        )
    return list(dict.fromkeys(warnings))


def _candidate_quality_warnings(
    candidate: dict[str, Any],
    *,
    master_script: str,
) -> list[str]:
    """Return review-only quality findings; none is a structure failure."""

    parsed = parse_script_bible(master_script)
    warnings: list[str] = []
    by_id = {item["candidate_id"]: item for item in parsed.candidates}
    for diagnostic in parsed.diagnostics:
        item = by_id.get(str(diagnostic.get("candidate_id") or ""))
        if item is None:
            continue
        code = diagnostic.get("code")
        label = {
            "character": "人物", "scene": "场景", "prop": "道具",
        }[item["entity_type"]]
        if code == "script_bible_description_empty":
            warnings.append(f"{label}“{item['name']}”缺少描述，请人工补充后再用于生产。")
        elif code == "script_bible_visual_identity_empty":
            warnings.append(
                f"{label}“{item['name']}”缺少英文视觉锚点，请人工补充后再用于生产。"
            )
        elif code == "script_bible_entity_unused":
            warnings.append(
                f"{label}“{item['name']}”未在任何镜头正文中出现，请确认是否保留。"
            )

    known_characters = {
        normalize_script_bible_name(item["name"])
        for item in candidate["bible"]["characters"]
    }
    known_scenes = [
        normalize_script_bible_name(item["name"])
        for item in candidate["bible"]["scenes"]
    ]
    unknown_speakers: set[str] = set()
    unknown_settings: set[str] = set()
    for episode in candidate["episodes"]:
        for shot in episode["shots"]:
            speaker = shot["dialogue"]["speaker"]
            if speaker and normalize_script_bible_name(speaker) not in known_characters:
                unknown_speakers.add(speaker)
            normalized_setting = normalize_script_bible_name(shot["setting"])
            if not any(name and name in normalized_setting for name in known_scenes):
                unknown_settings.add(shot["setting"])
    for speaker in sorted(unknown_speakers):
        warnings.append(f"对白人物“{speaker}”未命中人物候选，请人工补充或修正名称。")
    for setting in sorted(unknown_settings):
        warnings.append(f"镜头场景“{setting}”未命中场景候选，请人工补充或修正名称。")
    return list(dict.fromkeys(warnings))


def build_series_concept_plan(
    *,
    idea: str,
    style_hint: str,
    episode_count: int,
    shots_per_episode: int,
    episode_duration_seconds: int,
    minimum_shot_duration_ms: int,
    maximum_shot_duration_ms: int,
    model_identifier: str,
    timeout_seconds: float,
    chat_request_options: dict[str, Any],
    configured_output_token_hint: int,
    field_contract: dict[str, Any],
    resource_advisory: dict[str, Any],
    attempt: int = 1,
    require_all_bible_entities_used: bool = False,
) -> dict[str, Any]:
    """Build the deterministic series protocol without selecting a runtime.

    ProductionRun uses this function with a frozen ModelDeployment.  It must
    remain free of provider settings, credentials, network calls and database
    writes so the exact request can be persisted before a worker submits it.
    """

    frozen_field_contract = validate_text_field_contract(
        field_contract,
        fields=SERIES_CONCEPT_FIELD_KEYS,
        label="series_concept",
    )
    idea = str(idea or "").strip()
    style_hint = str(style_hint or "").strip()
    for value, field, label, required_code, length_code in (
        (idea, "idea", "创作想法", "series_concept_idea_required", "series_concept_idea_too_long"),
        (style_hint, "style_hint", "风格提示", "series_concept_style_hint_required", "series_concept_style_hint_too_long"),
    ):
        rule = text_field_rule(frozen_field_contract, field)
        if rule["required"] and not value:
            raise SeriesConceptDraftValidationError(f"{label}不能为空", code=required_code)
        if len(value) > int(rule["max_chars"]):
            raise SeriesConceptDraftValidationError(
                f"{label}不能超过 {int(rule['max_chars'])} 字符",
                code=length_code,
            )
        if rule["single_line"] and any(character in value for character in "\r\n\t"):
            raise SeriesConceptDraftValidationError(
                f"{label}必须是单行文本",
                code=length_code,
            )
    for value, label, code in (
        (episode_count, "计划集数", "series_concept_episode_count_invalid"),
        (shots_per_episode, "每集镜数", "series_concept_shots_per_episode_invalid"),
        (episode_duration_seconds, "单集时长", "series_concept_episode_duration_invalid"),
    ):
        if type(value) is not int or value < 1:
            raise SeriesConceptDraftValidationError(f"{label}必须是正整数。", code=code)
    if not (1 <= minimum_shot_duration_ms <= maximum_shot_duration_ms):
        raise SeriesConceptDraftValidationError(
            "冻结的镜头时长范围无效。", code="series_concept_duration_policy_invalid",
        )
    total_shots = episode_count * shots_per_episode
    episode_duration_ms = episode_duration_seconds * 1_000
    minimum_episode_duration_ms = shots_per_episode * minimum_shot_duration_ms
    if minimum_episode_duration_ms > episode_duration_ms:
        raise SeriesConceptDraftValidationError(
            "单集时长不足以容纳请求的镜头数；请增加单集时长或减少每集镜头数。",
            code="series_concept_episode_duration_infeasible",
            public_context={
                "episode_count": episode_count,
                "shots_per_episode": shots_per_episode,
                "total_shots": total_shots,
                "episode_duration_seconds": episode_duration_seconds,
                "minimum_episode_duration_seconds": (minimum_episode_duration_ms + 999) // 1_000,
            },
        )
    fingerprint = _source_fingerprint(
        idea=idea,
        style_hint=style_hint,
        episode_count=episode_count,
        shots_per_episode=shots_per_episode,
        episode_duration_seconds=episode_duration_seconds,
        minimum_shot_duration_ms=minimum_shot_duration_ms,
        maximum_shot_duration_ms=maximum_shot_duration_ms,
    )
    if type(attempt) is not int or attempt < 1:
        raise SeriesConceptDraftValidationError(
            "剧本候选 attempt 必须是正整数。",
            code="series_concept_attempt_invalid",
        )
    if type(configured_output_token_hint) is not int or configured_output_token_hint < 1:
        raise SeriesConceptDraftValidationError(
            "模型输出建议量必须是正整数。",
            code="series_concept_output_hint_invalid",
        )
    normalized_attempt = attempt
    resource_estimate = _resource_estimate(
        idea=idea,
        style_hint=style_hint,
        total_shots=total_shots,
        configured_output_token_hint=configured_output_token_hint,
        resource_advisory=resource_advisory,
    )
    request = {
        "operation": SERIES_CONCEPT_DRAFT_OPERATION,
        "request_id": f"{fingerprint[:20]}-{normalized_attempt}",
        "source_fingerprint": fingerprint,
        "idea": idea,
        "style_hint": style_hint,
        "options": {
            "episode_count": episode_count,
            "shots_per_episode": shots_per_episode,
            "minimum_duration_ms": minimum_shot_duration_ms,
            "maximum_duration_ms": maximum_shot_duration_ms,
            "episode_duration_ms": episode_duration_ms,
        },
        "attempt": normalized_attempt,
        "seed": int(fingerprint[:8], 16) + normalized_attempt,
        "model_id": str(model_identifier),
        "timeout_seconds": float(timeout_seconds),
        CHAT_REQUEST_OPTIONS_KEY: deepcopy(chat_request_options),
    }
    return {
        "schema_version": 1,
        "source_fingerprint": fingerprint,
        "request": request,
        "episode_count": episode_count,
        "shots_per_episode": shots_per_episode,
        "episode_duration_seconds": episode_duration_seconds,
        "minimum_shot_duration_ms": minimum_shot_duration_ms,
        "maximum_shot_duration_ms": maximum_shot_duration_ms,
        "field_contract": frozen_field_contract,
        "require_all_bible_entities_used": bool(require_all_bible_entities_used),
        "resource_estimate": resource_estimate,
    }


def finalize_series_concept_draft(
    *, plan: dict[str, Any], runtime_payload: dict[str, Any],
) -> dict[str, Any]:
    """Strictly validate one provider result and deterministically render it."""

    fingerprint = str(plan["source_fingerprint"])
    field_contract = validate_text_field_contract(
        plan.get("field_contract"),
        fields=SERIES_CONCEPT_FIELD_KEYS,
        label="series_concept frozen plan",
    )
    candidate = _validated_candidate(
        runtime_payload,
        fingerprint=fingerprint,
        episode_count=int(plan["episode_count"]),
        shots_per_episode=int(plan["shots_per_episode"]),
        minimum_shot_duration_ms=int(plan["minimum_shot_duration_ms"]),
        maximum_shot_duration_ms=int(plan["maximum_shot_duration_ms"]),
        field_contract=field_contract,
        require_all_bible_entities_used=bool(
            plan.get("require_all_bible_entities_used")
        ),
    )
    episode_duration_ms = int(plan["episode_duration_seconds"]) * 1_000
    episode_totals, adjusted_episode_count = _fit_episode_duration_budgets(
        candidate["episodes"],
        episode_duration_ms=episode_duration_ms,
        minimum_shot_duration_ms=int(plan["minimum_shot_duration_ms"]),
    )
    master_script = render_compatible_master_script(candidate["bible"], candidate["episodes"])
    candidate_warnings = _candidate_quality_warnings(candidate, master_script=master_script)
    has_dialogue = any(
        bool(shot["dialogue"]["text"])
        for episode in candidate["episodes"] for shot in episode["shots"]
    )
    warnings = _runtime_warnings(
        runtime_payload,
        has_dialogue=has_dialogue,
        adjusted_episode_count=adjusted_episode_count,
        resource_estimate=dict(plan["resource_estimate"]),
        script_char_count=len(master_script),
        candidate_warnings=candidate_warnings,
    )
    return {
        "schema_version": 1,
        "source_fingerprint": fingerprint,
        "candidate": {
            "title": candidate["title"],
            "logline": candidate["logline"],
            "master_script": master_script,
            "bible": candidate["bible"],
        },
        "format": {
            "schema_version": SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
            "template_id": SERIES_CONCEPT_TEMPLATE_ID,
            "compatible": True,
            "episode_count": int(plan["episode_count"]),
            "shot_count": int(plan["episode_count"]) * int(plan["shots_per_episode"]),
            "episode_duration_seconds": int(plan["episode_duration_seconds"]),
            "total_duration_ms": sum(episode_totals),
            "shot_durations_ms": [
                [shot["duration_ms"] for shot in episode["shots"]]
                for episode in candidate["episodes"]
            ],
        },
        "resource_estimate": dict(plan["resource_estimate"]),
        "warnings": warnings,
    }
