from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import (
    GeneratedArtifact,
    GenerationAttempt,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRun,
    SeriesProject,
    ScriptRevision,
    ScriptSource,
    StageRun,
)
from studio.services.json_contracts import (
    canonical_json_bytes,
    json_contract,
)
from studio.services.model_profile_contracts import (
    CHAT_REQUEST_OPTIONS_KEY,
    chat_request_options_from_policy,
    verified_model_profile_specification,
)
from studio.services.async_media_contract import fresh_smoke_snapshot
from studio.services.provider_registry import driver_for
from studio.services.security_boundaries import boundary_int, boundary_number
from studio.services.text_field_contracts import (
    SCRIPT_STRUCTURE_FIELD_KEYS,
    field_contract_prompt_lines,
    text_field_rule,
    validate_text_field_contract,
)
from studio.services.script_bible import (
    ScriptBibleParseError,
    parse_script_bible,
    validate_script_bible_ir,
)
from studio.services.script_sources import create_pasted_script_source
from studio.services.series_concept_drafts import (
    DIALOGUE_IR_KEYS,
    render_compatible_master_script,
    validate_dialogue_clock_contract,
)


SCRIPT_STRUCTURE_OPERATION = "script.structure"
SCRIPT_STRUCTURE_UNIT_KEY = "script-structure"
SCRIPT_STRUCTURE_SCHEMA_VERSION = 2


def _script_structure_count_boundaries() -> dict[str, int]:
    return {
        "max_episodes": boundary_int("security.production.max_episodes"),
        "max_shots_per_episode": boundary_int(
            "security.production.max_shots_per_episode"
        ),
        "max_total_shots": boundary_int("security.production.max_total_shots"),
    }

_EPISODE_HEADER = re.compile(r"^第(?P<number>[1-9][0-9]*)集：(?P<title>[^\r\n]+)$")
_SHOT_HEADER = re.compile(
    r"^镜头(?P<number>[1-9][0-9]*)"
    r"【时长=(?P<duration>[0-9]+(?:\.[0-9]{1,3})?)秒】"
    r"【首帧连续=(?P<first>是|否)】"
    r"【尾帧连续=(?P<last>是|否)】：(?P<setting>[^\r\n]+)$"
)
_ACTION_LINE = re.compile(
    r"^\[0-(?P<end>[0-9]+(?:\.[0-9]{1,3})?)秒\] 动作：(?P<action>[^\r\n]+)$"
)
_DIALOGUE_LINE = re.compile(
    r"^\[(?P<start>[0-9]+(?:\.[0-9]{1,3})?)-"
    r"(?P<end>[0-9]+(?:\.[0-9]{1,3})?)秒\] "
    r"(?P<speaker>[^：:（）()\r\n]{1,160})"
    r"（(?P<delivery>[^（）()\r\n]{1,240})）：(?P<text>[^\r\n]+)$"
)


class ScriptStructureError(ValidationError):
    def __init__(self, message: str, *, code: str):
        self.code = code
        super().__init__(message, code=code)


def _normalized_script(value: object) -> str:
    text = str(value or "")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _duration_ms(value: str) -> int:
    try:
        seconds = Decimal(value)
    except InvalidOperation as exc:
        raise ScriptStructureError(
            "规范剧本包含无效时长。",
            code="script_structure_canonical_duration_invalid",
        ) from exc
    milliseconds = seconds * 1_000
    if milliseconds != milliseconds.to_integral_value() or milliseconds <= 0:
        raise ScriptStructureError(
            "规范剧本时长必须精确到毫秒且大于零。",
            code="script_structure_canonical_duration_invalid",
        )
    return int(milliseconds)


def _timestamp_ms(value: str) -> int:
    try:
        seconds = Decimal(value)
    except InvalidOperation as exc:
        raise ScriptStructureError(
            "规范剧本包含无效时间点。",
            code="script_structure_canonical_duration_invalid",
        ) from exc
    milliseconds = seconds * 1_000
    if milliseconds != milliseconds.to_integral_value() or milliseconds < 0:
        raise ScriptStructureError(
            "规范剧本时间点必须是非负整数毫秒。",
            code="script_structure_canonical_duration_invalid",
        )
    return int(milliseconds)


def _safe_line(
    value: Any,
    *,
    field: str,
    maximum: int | None,
    required: bool = True,
) -> str:
    if not isinstance(value, str):
        raise ScriptStructureError(
            f"{field} 必须是字符串。",
            code="script_structure_ir_invalid",
        )
    normalized = value.strip()
    if (
        (required and not normalized)
        or (maximum is not None and len(normalized) > maximum)
        or any(character in normalized for character in "\r\n\t")
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized)
    ):
        raise ScriptStructureError(
            f"{field} 不是允许长度内的安全单行文本。",
            code="script_structure_ir_invalid",
        )
    return normalized


def _safe_contract_line(
    value: Any,
    *,
    field: str,
    contract_field: str,
    field_contract: dict[str, Any],
) -> str:
    rule = text_field_rule(field_contract, contract_field)
    if not isinstance(value, str):
        raise ScriptStructureError(
            f"{field} 必须是字符串。",
            code="script_structure_ir_invalid",
        )
    normalized = value.strip()
    if (
        (rule["required"] and not normalized)
        or len(normalized) > int(rule["max_chars"])
        or (
            rule["single_line"]
            and any(character in normalized for character in "\r\n\t")
        )
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized)
    ):
        raise ScriptStructureError(
            f"{field} 不符合冻结 field_contract。",
            code="script_structure_ir_invalid",
        )
    return normalized


def _bible_from_parse(parsed) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {
        "characters": [],
        "scenes": [],
        "props": [],
    }
    collection_by_type = {
        "character": "characters",
        "scene": "scenes",
        "prop": "props",
    }
    for candidate in parsed.candidates:
        collection = collection_by_type[candidate["entity_type"]]
        result[collection].append({
            "name": candidate["name"],
            "description": candidate["description"],
            "visual_identity_en": candidate["visual_identity_en"],
        })
    return result


def parse_canonical_script(value: object) -> dict[str, Any]:
    """Parse only the exact deterministic renderer form.

    This intentionally rejects merely parseable or approximately formatted text.
    A positive result therefore means ``script_structure`` can safely record a
    no-submission review candidate without silently rewriting the confirmed input.
    """

    source_text = str(value or "")
    normalized = _normalized_script(source_text).strip()
    if not normalized:
        raise ScriptStructureError(
            "剧本文本为空。",
            code="script_structure_source_empty",
        )
    try:
        parsed = parse_script_bible(normalized)
    except ScriptBibleParseError as exc:
        raise ScriptStructureError(
            str(exc),
            code="script_structure_canonical_bible_invalid",
        ) from exc
    if not parsed.has_block:
        raise ScriptStructureError(
            "剧本不是带 SCRIPT_BIBLE_V1 前置块的规范格式。",
            code="script_structure_not_canonical",
        )
    bible = _bible_from_parse(parsed)
    lines = parsed.body.strip().split("\n") if parsed.body.strip() else []
    episodes: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        if not lines[index]:
            index += 1
            continue
        episode_match = _EPISODE_HEADER.fullmatch(lines[index])
        expected_episode_number = len(episodes) + 1
        if (
            episode_match is None
            or int(episode_match.group("number")) != expected_episode_number
        ):
            raise ScriptStructureError(
                "规范剧本的集标题或集序号无效。",
                code="script_structure_not_canonical",
            )
        episode = {"title": episode_match.group("title"), "shots": []}
        episodes.append(episode)
        index += 1
        while index < len(lines):
            if not lines[index]:
                index += 1
                continue
            if _EPISODE_HEADER.fullmatch(lines[index]):
                break
            shot_match = _SHOT_HEADER.fullmatch(lines[index])
            expected_shot_number = len(episode["shots"]) + 1
            if (
                shot_match is None
                or int(shot_match.group("number")) != expected_shot_number
            ):
                raise ScriptStructureError(
                    "规范剧本的镜头标题或镜头序号无效。",
                    code="script_structure_not_canonical",
                )
            duration_text = shot_match.group("duration")
            duration_ms = _duration_ms(duration_text)
            index += 1
            content_lines: list[str] = []
            while index < len(lines):
                current = lines[index]
                if _SHOT_HEADER.fullmatch(current) or _EPISODE_HEADER.fullmatch(current):
                    break
                index += 1
                if current:
                    content_lines.append(current)
            if len(content_lines) not in {1, 2}:
                raise ScriptStructureError(
                    "规范镜头必须包含一行动作，或动作与对白各一行。",
                    code="script_structure_not_canonical",
                )
            action_match = _ACTION_LINE.fullmatch(content_lines[0])
            if action_match is None:
                raise ScriptStructureError(
                    "规范镜头动作行无效。",
                    code="script_structure_not_canonical",
                )
            dialogue = {
                "speaker": "",
                "delivery": "",
                "text": "",
                "start_ms": 0,
                "end_ms": 0,
            }
            action_end_ms = _duration_ms(action_match.group("end"))
            if action_end_ms != duration_ms:
                raise ScriptStructureError(
                    "规范镜头动作时间未覆盖完整镜头。",
                    code="script_structure_not_canonical",
                )
            if len(content_lines) == 1:
                pass
            else:
                dialogue_match = _DIALOGUE_LINE.fullmatch(content_lines[1])
                if dialogue_match is None:
                    raise ScriptStructureError(
                        "规范镜头对白行无效。",
                        code="script_structure_not_canonical",
                    )
                dialogue_start_ms = _timestamp_ms(dialogue_match.group("start"))
                dialogue_end_ms = _duration_ms(dialogue_match.group("end"))
                if not 0 <= dialogue_start_ms < dialogue_end_ms <= duration_ms:
                    raise ScriptStructureError(
                        "规范镜头对白时间范围必须位于镜头时长内。",
                        code="script_structure_not_canonical",
                    )
                dialogue = {
                    "speaker": dialogue_match.group("speaker"),
                    "delivery": dialogue_match.group("delivery"),
                    "text": dialogue_match.group("text"),
                    "start_ms": dialogue_start_ms,
                    "end_ms": dialogue_end_ms,
                }
            episode["shots"].append({
                "setting": shot_match.group("setting"),
                "action": action_match.group("action"),
                "duration_ms": duration_ms,
                "dialogue": dialogue,
                "_first": shot_match.group("first") == "是",
                "_last": shot_match.group("last") == "是",
            })
        if not episode["shots"]:
            raise ScriptStructureError(
                "规范剧本每集至少需要一个镜头。",
                code="script_structure_not_canonical",
            )
        for shot_index, shot in enumerate(episode["shots"]):
            expected_first = shot_index > 0
            expected_last = shot_index < len(episode["shots"]) - 1
            if shot.pop("_first") != expected_first or shot.pop("_last") != expected_last:
                raise ScriptStructureError(
                    "规范镜头的首尾帧连续标记与镜头位置不一致。",
                    code="script_structure_not_canonical",
                )
    if not episodes:
        raise ScriptStructureError(
            "规范剧本至少需要一集。",
            code="script_structure_not_canonical",
        )
    rendered = render_compatible_master_script(bible, episodes)
    # Parsing may normalize line endings/BOM/outer whitespace to produce a
    # useful diagnostic, but deterministic no-submission is allowed only when
    # the stored revision is byte-for-byte the renderer output.
    if rendered != source_text:
        raise ScriptStructureError(
            "剧本可以解析，但不是确定性渲染器的规范形式。",
            code="script_structure_not_canonical",
        )
    return {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "bible": bible,
        "episodes": episodes,
        "master_script": rendered,
        "content_sha256": _text_sha256(rendered),
        "content_size_bytes": len(rendered.encode("utf-8")),
    }


def is_canonical_script(value: object) -> bool:
    try:
        parse_canonical_script(value)
    except (ScriptStructureError, ValidationError, ValueError):
        return False
    return True


def validate_script_structure_model_profile(
    profile: ModelProfileVersion,
) -> dict[str, Any] | None:
    specification = verified_model_profile_specification(profile)
    aliases = sorted(
        field
        for field in ("operations", "protocols")
        if SCRIPT_STRUCTURE_OPERATION in (specification.get(field) or [])
    )
    if aliases:
        raise ValidationError({
            "specification": (
                "script.structure 只能在 specification.capabilities 中声明；"
                f"禁止兼容字段: {', '.join(aliases)}"
            ),
        })
    capabilities = specification.get("capabilities")
    if not isinstance(capabilities, list):
        return None
    if (
        any(not isinstance(item, str) or not item.strip() for item in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        raise ValidationError({"specification": "capabilities 必须是无重复的非空字符串数组"})
    if SCRIPT_STRUCTURE_OPERATION not in capabilities:
        return None
    if specification.get("schema_version") != 1:
        raise ValidationError({"specification": "script.structure specification.schema_version 必须为 1"})
    if profile.modality not in {
        ModelProfileVersion.Modality.TEXT,
        ModelProfileVersion.Modality.MULTIMODAL,
    }:
        raise ValidationError({"modality": "script.structure 只支持 text/multimodal 模型"})
    operations = set((driver_for(profile.provider).capabilities() or {}).get("operations") or [])
    if "chat_completion" not in operations:
        raise ValidationError({"specification": "provider driver 未实现受控 chat_completion transport"})
    policy = specification.get("script_structure")
    if not isinstance(policy, dict) or set(policy) != {
        "minimum_shot_duration_ms",
        "maximum_shot_duration_ms",
        "max_input_bytes",
        "max_output_tokens",
        "timeout_seconds",
        "field_contract",
        CHAT_REQUEST_OPTIONS_KEY,
    }:
        raise ValidationError({"specification": "script.structure 缺少精确的 script_structure policy"})
    integer_fields = (
        "minimum_shot_duration_ms",
        "maximum_shot_duration_ms",
        "max_input_bytes",
        "max_output_tokens",
    )
    invalid = [
        field
        for field in integer_fields
        if type(policy.get(field)) is not int or policy[field] < 1
    ]
    if invalid:
        raise ValidationError({"specification": f"script_structure policy 无效: {', '.join(invalid)}"})
    if policy["minimum_shot_duration_ms"] > policy["maximum_shot_duration_ms"]:
        raise ValidationError({"specification": "script_structure 最小时长不能大于最大时长"})
    timeout_value = policy.get("timeout_seconds")
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise ValidationError({"specification": "script_structure.timeout_seconds 必须是数值"})
    timeout = float(timeout_value)
    timeout_max = boundary_number(
        "security.provider.async_rest_timeout_max_seconds",
        minimum=0.001,
    )
    if not 0 < timeout <= timeout_max:
        raise ValidationError({
            "specification": (
                "script_structure.timeout_seconds 必须大于 0 且不超过"
                f"当前安全边界 {timeout_max:g} 秒"
            ),
        })
    field_contract = validate_text_field_contract(
        policy["field_contract"],
        fields=SCRIPT_STRUCTURE_FIELD_KEYS,
        label="script_structure",
    )
    return {
        **{field: int(policy[field]) for field in integer_fields},
        "timeout_seconds": timeout,
        "field_contract": field_contract,
        CHAT_REQUEST_OPTIONS_KEY: chat_request_options_from_policy(
            policy,
            label="script_structure",
        ),
    }


def intersect_script_structure_duration_policy(
    policy: dict[str, Any],
    *,
    production_minimum_duration_ms: int,
    production_maximum_duration_ms: int,
) -> dict[str, Any]:
    """Constrain model output to the frozen ProductionRun duration policy.

    ModelProfile limits describe what the selected structuring route can emit;
    the production configuration describes what downstream storyboard and
    media stages accept.  The provider must receive the intersection so a
    reviewed structure candidate cannot become invalid only after approval.
    """

    model_minimum = int(policy["minimum_shot_duration_ms"])
    model_maximum = int(policy["maximum_shot_duration_ms"])
    effective_minimum = max(model_minimum, int(production_minimum_duration_ms))
    effective_maximum = min(model_maximum, int(production_maximum_duration_ms))
    if effective_minimum > effective_maximum:
        raise ScriptStructureError(
            "所选 script.structure 模型的镜头时长范围与本次运行冻结的生产范围没有交集。",
            code="script_structure_duration_policy_incompatible",
        )
    return {
        **policy,
        "minimum_shot_duration_ms": effective_minimum,
        "maximum_shot_duration_ms": effective_maximum,
    }


def verify_script_structure_smoke(
    deployment: ModelDeployment,
    route: dict[str, Any],
):
    """Bind submission to a fresh probe for this exact operation and target."""

    try:
        current = fresh_smoke_snapshot(deployment, SCRIPT_STRUCTURE_OPERATION)
    except ValidationError as exc:
        raise ScriptStructureError(
            "script.structure 当前 smoke 证据合同无效。",
            code="script_structure_smoke_invalid",
        ) from exc
    if current is None:
        raise ScriptStructureError(
            "script.structure route 缺少当前有效的专用 smoke 证据。",
            code="script_structure_smoke_missing_or_stale",
        )
    frozen = (
        route.get("smoke_evidence", {}).get(SCRIPT_STRUCTURE_OPERATION)
        if isinstance(route.get("smoke_evidence"), dict) else None
    )
    if (
        not isinstance(frozen, dict)
        or frozen.get("snapshot_id") != str(current.id)
        or frozen.get("target_sha256") != current.target_sha256
        or frozen.get("evidence_sha256") != current.evidence_sha256
    ):
        raise ScriptStructureError(
            "冻结的 script.structure smoke 已漂移；请创建新的 ProductionRun。",
            code="script_structure_smoke_drift",
        )
    return current


def build_script_structure_plan(
    *,
    revision: ScriptRevision,
    model_identifier: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if revision.status != ScriptRevision.Status.CONFIRMED:
        raise ScriptStructureError(
            "script.structure 只接受已确认的 ScriptRevision。",
            code="script_structure_revision_unconfirmed",
        )
    content = _normalized_script(revision.content)
    payload = content.encode("utf-8")
    if len(payload) != revision.content_size_bytes or _text_sha256(content) != revision.content_sha256:
        raise ScriptStructureError(
            "ScriptRevision 内容与冻结大小或 hash 不一致。",
            code="script_structure_revision_drift",
        )
    if len(payload) > int(policy["max_input_bytes"]):
        raise ScriptStructureError(
            "完整剧本超过所选模型 script.structure 输入上限；不会截断后提交。",
            code="script_structure_input_too_large",
        )
    non_empty_lines = [
        index
        for index, line in enumerate(content.split("\n"), start=1)
        if line.strip()
    ]
    seed = int(revision.content_sha256[:8], 16)
    return {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "operation": SCRIPT_STRUCTURE_OPERATION,
        "source_fingerprint": revision.content_sha256,
        "source_revision": {
            "id": str(revision.id),
            "source_id": str(revision.source_id),
            "revision": revision.revision,
            "content_sha256": revision.content_sha256,
            "content_size_bytes": revision.content_size_bytes,
        },
        "request": {
            "source_text": content,
            "non_empty_source_line_numbers": non_empty_lines,
            "options": {
                "minimum_shot_duration_ms": int(policy["minimum_shot_duration_ms"]),
                "maximum_shot_duration_ms": int(policy["maximum_shot_duration_ms"]),
                "max_output_tokens": int(policy["max_output_tokens"]),
                "timeout_seconds": float(policy["timeout_seconds"]),
                CHAT_REQUEST_OPTIONS_KEY: deepcopy(
                    policy[CHAT_REQUEST_OPTIONS_KEY],
                ),
            },
            "seed": seed,
        },
        "model_identifier": str(model_identifier),
        "field_contract": deepcopy(policy["field_contract"]),
        "resource_estimate": {
            "source_size_bytes": len(payload),
            "source_line_count": len(content.split("\n")),
            "non_empty_source_line_count": len(non_empty_lines),
            "configured_output_token_hint": int(policy["max_output_tokens"]),
            "timeout_seconds": float(policy["timeout_seconds"]),
            "submission_policy": "full_source_or_fail_no_truncation",
        },
    }


def structure_provider_payload(plan: dict[str, Any]) -> dict[str, Any]:
    request = plan["request"]
    options = request["options"]
    field_contract = validate_text_field_contract(
        plan.get("field_contract"),
        fields=SCRIPT_STRUCTURE_FIELD_KEYS,
        label="script_structure frozen plan",
    )
    def placeholder(field: str, subject: str) -> str:
        rule = text_field_rule(field_contract, field)
        required = "required" if rule["required"] else "optional"
        line_shape = "single-line" if rule["single_line"] else "text"
        return f"<{required} {line_shape} {subject}, <= {int(rule['max_chars'])} chars>"
    contract = {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "source_revision_id": plan["source_revision"]["id"],
        "source_sha256": plan["source_revision"]["content_sha256"],
        "source_size_bytes": plan["source_revision"]["content_size_bytes"],
        "title": placeholder("title", "project title"),
        "logline": placeholder("logline", "logline"),
        "bible": {
            "characters": [{
                "name": placeholder("bible.entity.name", "canonical character name"),
                "description": placeholder("bible.entity.description", "character description"),
                "visual_identity_en": placeholder("bible.entity.visual_identity_en", "English character visual identity"),
            }],
            "scenes": [{
                "name": placeholder("bible.entity.name", "canonical scene name"),
                "description": placeholder("bible.entity.description", "scene description"),
                "visual_identity_en": placeholder("bible.entity.visual_identity_en", "English scene visual identity"),
            }],
            "props": [{
                "name": placeholder("bible.entity.name", "canonical prop name"),
                "description": placeholder("bible.entity.description", "prop description"),
                "visual_identity_en": placeholder("bible.entity.visual_identity_en", "English prop visual identity"),
            }],
        },
        "episodes": [{
            "title": placeholder("episode.title", "episode title"),
            "shots": [{
                "source_lines": [1],
                "setting": placeholder("shot.setting", "setting"),
                "action": placeholder("shot.action", "action preserving source facts"),
                "duration_ms": int(options["minimum_shot_duration_ms"]),
                "dialogue": {
                    "speaker": "",
                    "delivery": "",
                    "text": "",
                    "start_ms": 0,
                    "end_ms": 0,
                },
            }],
        }],
    }
    profile_assembly = plan.get("prompt_profile_assembly")
    profile_system = ""
    profile_payload: dict[str, Any] = {}
    if profile_assembly is not None:
        if not isinstance(profile_assembly, dict):
            raise ValidationError("script.structure PromptProfile assembly 无效")
        channels = profile_assembly.get("channels")
        structured = profile_assembly.get("structured_fields")
        if not isinstance(channels, dict) or not isinstance(structured, dict):
            raise ValidationError("script.structure PromptProfile channels 无效")
        profile_system = str(channels.get("system_instruction") or "").strip()
        profile_payload = {
            "positive_prompt": str(channels.get("positive_prompt") or ""),
            "negative_prompt": str(channels.get("negative_prompt") or ""),
            "structured_fields": structured,
        }
    system = (
        "Return exactly one JSON object and no Markdown or prose. The root and every "
        "nested object MUST contain exactly the keys shown in CONTRACT. Structure the "
        "complete imported script faithfully; do not invent, summarize away, or omit "
        "source facts. Every non-empty source line number MUST occur exactly once across "
        "all shot.source_lines arrays. Do not truncate any array or string. duration_ms "
        f"MUST be an integer from {int(options['minimum_shot_duration_ms'])} through "
        f"{int(options['maximum_shot_duration_ms'])}. Every dialogue object MUST contain "
        "exactly speaker, delivery, text, start_ms, end_ms. Spoken dialogue requires "
        "non-empty speaker, printable-ASCII delivery without parentheses, and text, plus "
        "integer timing satisfying 0 <= start_ms < end_ms <= the same shot.duration_ms. "
        "Silent dialogue uses three empty strings and the explicit start_ms=0,end_ms=0 "
        "sentinel. All configurable text fields MUST follow the frozen FIELD_CONTRACT. "
        "Echo the frozen source identity fields exactly. FIELD_CONTRACT:\n"
        + field_contract_prompt_lines(field_contract)
        + "\nCONTRACT="
        + json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
    )
    if profile_system:
        system += "\nREVIEWED_PROMPT_PROFILE=" + profile_system
    user: dict[str, Any] = {
        "source_revision": plan["source_revision"],
        "non_empty_source_line_numbers": request["non_empty_source_line_numbers"],
        "source_text": request["source_text"],
    }
    if profile_payload:
        user["prompt_profile"] = profile_payload
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, separators=(",", ":"))},
        ],
        "max_tokens": int(options["max_output_tokens"]),
        "timeout_seconds": float(options["timeout_seconds"]),
        CHAT_REQUEST_OPTIONS_KEY: deepcopy(options[CHAT_REQUEST_OPTIONS_KEY]),
        "seed": int(request["seed"]),
    }


def finalize_script_structure(
    *,
    plan: dict[str, Any],
    payload: object,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "source_revision_id",
        "source_sha256",
        "source_size_bytes",
        "title",
        "logline",
        "bible",
        "episodes",
    }:
        raise ScriptStructureError(
            "script.structure 响应根对象字段不完整或包含未知字段。",
            code="script_structure_ir_invalid",
        )
    source = plan.get("source_revision")
    if (
        payload.get("schema_version") != SCRIPT_STRUCTURE_SCHEMA_VERSION
        or not isinstance(source, dict)
        or payload.get("source_revision_id") != source.get("id")
        or payload.get("source_sha256") != source.get("content_sha256")
        or payload.get("source_size_bytes") != source.get("content_size_bytes")
    ):
        raise ScriptStructureError(
            "script.structure 响应未精确回显冻结来源。",
            code="script_structure_source_mismatch",
        )
    field_contract = validate_text_field_contract(
        plan.get("field_contract"),
        fields=SCRIPT_STRUCTURE_FIELD_KEYS,
        label="script_structure frozen plan",
    )
    title = _safe_contract_line(payload.get("title"), field="title", contract_field="title", field_contract=field_contract)
    logline = _safe_contract_line(payload.get("logline"), field="logline", contract_field="logline", field_contract=field_contract)
    try:
        bible = validate_script_bible_ir(
            payload.get("bible"),
            field_contract=field_contract,
        )
    except ScriptBibleParseError as exc:
        raise ScriptStructureError(
            str(exc),
            code="script_structure_ir_invalid",
        ) from exc
    count_boundaries = _script_structure_count_boundaries()
    episodes = payload.get("episodes")
    if (
        not isinstance(episodes, list)
        or not 1 <= len(episodes) <= count_boundaries["max_episodes"]
    ):
        raise ScriptStructureError(
            (
                "script.structure episodes 数量必须为 1 到当前安全边界 "
                f"{count_boundaries['max_episodes']}。"
            ),
            code="script_structure_ir_invalid",
        )
    options = plan["request"]["options"]
    minimum = int(options["minimum_shot_duration_ms"])
    maximum = int(options["maximum_shot_duration_ms"])
    expected_lines = list(plan["request"]["non_empty_source_line_numbers"])
    covered_lines: list[int] = []
    validated_episodes: list[dict[str, Any]] = []
    total_shots = 0
    for episode in episodes:
        if not isinstance(episode, dict) or set(episode) != {"title", "shots"}:
            raise ScriptStructureError(
                "script.structure episode 字段无效。",
                code="script_structure_ir_invalid",
            )
        shots = episode.get("shots")
        if (
            not isinstance(shots, list)
            or not 1 <= len(shots) <= count_boundaries["max_shots_per_episode"]
        ):
            raise ScriptStructureError(
                (
                    "script.structure 每集镜头数量必须为 1 到当前安全边界 "
                    f"{count_boundaries['max_shots_per_episode']}。"
                ),
                code="script_structure_ir_invalid",
            )
        total_shots += len(shots)
        if total_shots > count_boundaries["max_total_shots"]:
            raise ScriptStructureError(
                (
                    "script.structure 总镜头数量超过当前安全边界 "
                    f"{count_boundaries['max_total_shots']}。"
                ),
                code="script_structure_ir_invalid",
            )
        validated_shots: list[dict[str, Any]] = []
        for shot in shots:
            if not isinstance(shot, dict) or set(shot) != {
                "source_lines", "setting", "action", "duration_ms", "dialogue",
            }:
                raise ScriptStructureError(
                    "script.structure shot 字段无效。",
                    code="script_structure_ir_invalid",
                )
            source_lines = shot.get("source_lines")
            if (
                not isinstance(source_lines, list)
                or not source_lines
                or any(type(item) is not int or item < 1 for item in source_lines)
            ):
                raise ScriptStructureError(
                    "每个结构化镜头必须引用至少一个有效源行。",
                    code="script_structure_source_coverage_invalid",
                )
            covered_lines.extend(source_lines)
            duration = shot.get("duration_ms")
            if type(duration) is not int or not minimum <= duration <= maximum:
                raise ScriptStructureError(
                    "script.structure 镜头时长无效。",
                    code="script_structure_ir_invalid",
                )
            dialogue = shot.get("dialogue")
            if not isinstance(dialogue, dict) or set(dialogue) != DIALOGUE_IR_KEYS:
                raise ScriptStructureError(
                    "script.structure dialogue 字段无效。",
                    code="script_structure_ir_invalid",
                )
            speaker = _safe_contract_line(dialogue.get("speaker"), field="dialogue.speaker", contract_field="shot.dialogue.speaker", field_contract=field_contract)
            delivery = _safe_contract_line(dialogue.get("delivery"), field="dialogue.delivery", contract_field="shot.dialogue.delivery", field_contract=field_contract)
            dialogue_text = _safe_contract_line(dialogue.get("text"), field="dialogue.text", contract_field="shot.dialogue.text", field_contract=field_contract)
            if (
                bool(speaker) != bool(dialogue_text)
                or bool(delivery) != bool(dialogue_text)
                or any(character in speaker for character in "()（）[]【】<>:：")
                or (
                    delivery
                    and (
                        any(ord(character) < 32 or ord(character) > 126 for character in delivery)
                        or "(" in delivery
                        or ")" in delivery
                    )
                )
            ):
                raise ScriptStructureError(
                    "script.structure 对白人物、语气或正文组合无效。",
                    code="script_structure_ir_invalid",
                )
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
                    duration_ms=duration,
                )
            except ValueError as exc:
                raise ScriptStructureError(
                    "script.structure 对白时钟必须位于镜头时长内；空对白必须使用 0/0。",
                    code="script_structure_ir_invalid",
                ) from exc
            validated_shots.append({
                "source_lines": list(source_lines),
                "setting": _safe_contract_line(shot.get("setting"), field="shot.setting", contract_field="shot.setting", field_contract=field_contract),
                "action": _safe_contract_line(shot.get("action"), field="shot.action", contract_field="shot.action", field_contract=field_contract),
                "duration_ms": duration,
                "dialogue": normalized_dialogue,
            })
        validated_episodes.append({
            "title": _safe_contract_line(episode.get("title"), field="episode.title", contract_field="episode.title", field_contract=field_contract),
            "shots": validated_shots,
        })
    if covered_lines != expected_lines and sorted(covered_lines) != expected_lines:
        raise ScriptStructureError(
            "结构化结果未逐行完整覆盖冻结源剧本；不会接受可能被截断的候选。",
            code="script_structure_source_coverage_invalid",
        )
    if len(covered_lines) != len(set(covered_lines)):
        raise ScriptStructureError(
            "结构化结果重复引用源行，无法证明无截断覆盖。",
            code="script_structure_source_coverage_invalid",
        )
    render_episodes = [
        {
            "title": episode["title"],
            "shots": [
                {key: value for key, value in shot.items() if key != "source_lines"}
                for shot in episode["shots"]
            ],
        }
        for episode in validated_episodes
    ]
    master_script = render_compatible_master_script(bible, render_episodes)
    coverage = {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "source_sha256": source["content_sha256"],
        "source_size_bytes": source["content_size_bytes"],
        "source_line_numbers": covered_lines,
    }
    return {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "source_fingerprint": source["content_sha256"],
        "source_revision": dict(source),
        "candidate": {
            "title": title,
            "logline": logline,
            "bible": bible,
            "episodes": validated_episodes,
            "master_script": master_script,
        },
        "evidence": {
            "submission_policy": "full_source_or_fail_no_truncation",
            "source_line_count": len(plan["request"]["source_text"].split("\n")),
            "covered_non_empty_line_count": len(covered_lines),
            "coverage_sha256": hashlib.sha256(canonical_json_bytes(coverage)).hexdigest(),
            "candidate_sha256": hashlib.sha256(
                canonical_json_bytes({
                    "title": title,
                    "logline": logline,
                    "bible": bible,
                    "episodes": validated_episodes,
                    "master_script": master_script,
                })
            ).hexdigest(),
        },
    }


def canonical_no_submission_result(
    revision: ScriptRevision,
    *,
    title: str,
    logline: str,
) -> dict[str, Any]:
    canonical = parse_canonical_script(revision.content)
    candidate = {
        "title": _safe_line(
            title,
            field="title",
            maximum=SeriesProject._meta.get_field("title").max_length,
        ),
        "logline": _safe_line(
            logline,
            field="logline",
            maximum=None,
            required=False,
        ),
        "bible": canonical["bible"],
        "episodes": canonical["episodes"],
        "master_script": canonical["master_script"],
    }
    source_lines = [
        index
        for index, line in enumerate(_normalized_script(revision.content).split("\n"), start=1)
        if line.strip()
    ]
    coverage = {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "source_sha256": revision.content_sha256,
        "source_size_bytes": revision.content_size_bytes,
        "source_line_numbers": source_lines,
    }
    return {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "source_fingerprint": revision.content_sha256,
        "source_revision": {
            "id": str(revision.id),
            "source_id": str(revision.source_id),
            "revision": revision.revision,
            "content_sha256": revision.content_sha256,
            "content_size_bytes": revision.content_size_bytes,
        },
        "candidate": candidate,
        "evidence": {
            "submission_policy": "canonical_input_no_submission",
            "source_line_count": len(_normalized_script(revision.content).split("\n")),
            "covered_non_empty_line_count": len(source_lines),
            "coverage_sha256": hashlib.sha256(canonical_json_bytes(coverage)).hexdigest(),
            "candidate_sha256": hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
        },
    }


def materialize_script_structure_preview(
    *,
    run: ProductionRun,
    stage: StageRun,
    attempt: GenerationAttempt,
    result: dict[str, Any],
    result_sha256: str,
) -> tuple[ScriptRevision, GeneratedArtifact, dict[str, Any]]:
    """Persist one review-only structured preview with complete source lineage.

    The confirmed imported revision is never changed here.  The candidate uses
    a separate AI-generated source and remains PREVIEW until the ProductionRun
    ``script_review`` transaction explicitly adopts it.
    """

    source = result.get("source_revision")
    candidate = result.get("candidate")
    evidence = result.get("evidence")
    request_plan = attempt.request_snapshot.get("plan") if isinstance(attempt.request_snapshot, dict) else None
    frozen_source = request_plan.get("source_revision") if isinstance(request_plan, dict) else None
    if (
        not isinstance(source, dict)
        or not isinstance(candidate, dict)
        or not isinstance(evidence, dict)
        or source != frozen_source
        or source.get("id") != str(run.script_revision_id)
        or source.get("content_sha256") != run.script_revision.content_sha256
    ):
        raise ScriptStructureError(
            "script.structure 候选与运行冻结的原始 revision 不一致。",
            code="script_structure_materialization_source_mismatch",
        )
    source_object, revision = create_pasted_script_source(
        project=run.project,
        user=run.owner,
        content=candidate.get("master_script"),
        source_format=ScriptSource.SourceFormat.PLAIN,
        source_type=ScriptSource.SourceType.AI_GENERATED,
    )
    revision.origin_generation_attempt = attempt
    revision.save(update_fields=("origin_generation_attempt", "updated_at"))
    metadata = {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "operation": SCRIPT_STRUCTURE_OPERATION,
        "script_source_id": str(source_object.id),
        "script_revision_id": str(revision.id),
        "generation_attempt_id": str(attempt.id),
        "source_revision_id": str(source["id"]),
        "source_content_sha256": str(source["content_sha256"]),
        "source_content_size_bytes": int(source["content_size_bytes"]),
        "result_sha256": str(result_sha256),
        "candidate_sha256": str(evidence.get("candidate_sha256") or ""),
        "coverage_sha256": str(evidence.get("coverage_sha256") or ""),
        "covered_non_empty_line_count": int(evidence.get("covered_non_empty_line_count") or 0),
        "title_sha256": _text_sha256(str(candidate.get("title") or "")),
        "logline_sha256": _text_sha256(str(candidate.get("logline") or "")),
    }
    manifest = json_contract(
        metadata,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
    )
    artifact = GeneratedArtifact.objects.create(
        manifest_key=f"run:{run.id}:structured-script:{attempt.id}",
        production_run=run,
        stage_run=stage,
        generation_attempt=attempt,
        kind=GeneratedArtifact.Kind.SCRIPT,
        role="structured_script",
        # script_input already owns (run, "script", 1). A review candidate is
        # a separate variant group until the explicit script_review selects it.
        variant_group="script_structure",
        variant_index=1,
        status=GeneratedArtifact.Status.AVAILABLE,
        storage_backend="database",
        object_key=f"script-revisions/{revision.id}",
        mime_type="text/plain; charset=utf-8",
        size_bytes=revision.content_size_bytes,
        sha256=revision.content_sha256,
        manifest_metadata=manifest.value,
        manifest_schema_version=manifest.schema_version,
        manifest_metadata_size_bytes=manifest.size_bytes,
        manifest_metadata_sha256=manifest.sha256,
    )
    review_payload = {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "kind": "structured_script_preview",
        "submission_semantics": "remote_submission_succeeded",
        "source_revision_id": str(source["id"]),
        "source_content_sha256": str(source["content_sha256"]),
        "source_content_size_bytes": int(source["content_size_bytes"]),
        "script_revision_id": str(revision.id),
        "artifact_id": str(artifact.id),
        "attempt_id": str(attempt.id),
        "content_sha256": revision.content_sha256,
        "content_size_bytes": revision.content_size_bytes,
        "result_sha256": str(result_sha256),
        "candidate_sha256": str(evidence.get("candidate_sha256") or ""),
        "coverage_sha256": str(evidence.get("coverage_sha256") or ""),
        "covered_non_empty_line_count": int(evidence.get("covered_non_empty_line_count") or 0),
        "title": str(candidate.get("title") or ""),
        "logline": str(candidate.get("logline") or ""),
    }
    # Validate the review payload size now. The caller assigns this exact value
    # to StageRun.output_snapshot in the same materialization transaction.
    json_contract(
        review_payload,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
    )
    return revision, artifact, review_payload


def canonical_no_submission_review_payload(
    revision: ScriptRevision,
    *,
    title: str,
    logline: str,
    artifact_id: str,
) -> dict[str, Any]:
    result = canonical_no_submission_result(
        revision,
        title=title,
        logline=logline,
    )
    candidate = result["candidate"]
    evidence = result["evidence"]
    payload = {
        "schema_version": SCRIPT_STRUCTURE_SCHEMA_VERSION,
        "kind": "structured_script_preview",
        "submission_semantics": "no_submission",
        "source_revision_id": str(revision.id),
        "source_content_sha256": revision.content_sha256,
        "source_content_size_bytes": revision.content_size_bytes,
        "script_revision_id": str(revision.id),
        "artifact_id": str(artifact_id),
        "attempt_id": None,
        "content_sha256": revision.content_sha256,
        "content_size_bytes": revision.content_size_bytes,
        "result_sha256": "",
        "candidate_sha256": str(evidence["candidate_sha256"]),
        "coverage_sha256": str(evidence["coverage_sha256"]),
        "covered_non_empty_line_count": int(evidence["covered_non_empty_line_count"]),
        "title": str(candidate["title"]),
        "logline": str(candidate["logline"]),
    }
    return json_contract(
        payload,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
    ).value
