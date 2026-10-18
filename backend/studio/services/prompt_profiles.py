from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import (
    ModelProfileVersion,
    FieldDefinitionVersion,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProviderProfile,
)
from studio.services.field_definitions import field_definition_snapshot, profile_rule_definition as resolve_rule_definition
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.prompt_generation_sources import (
    FIELD_AUXILIARY_OPERATIONS,
    MODEL_GENERATION_MODES,
    prompt_generation_source_values,
)
from studio.services.prompt_normalization_contract import PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
from studio.services.security_boundaries import boundary_int
from studio.services.source_projection_primitives import (
    REFERENCE_CONTEXT_ENGINE_KEY,
    SOURCE_PROJECTION_PRIMITIVES,
)


KNOWN_PROMPT_OPERATIONS = (
    "text.structured",
    "video.generate",
    "image.generate",
    "script.generate",
    "script.structure",
    "visual_rules.generate",
    "prompt.normalize",
)
CONTROLLED_PROMPT_OPERATION_REGISTRY = frozenset(KNOWN_PROMPT_OPERATIONS)
# Model families are configuration facts.  The control plane may suggest
# families already present in published ModelProfileVersion/PromptProfile rows,
# but it must never seed or infer them from vendor/model names.
KNOWN_MODEL_FAMILIES: tuple[str, ...] = ()
_BIBLE_ENTITY_TYPES = ("character", "scene", "prop")
# These values are non-empty for every formal Bible entity after the required
# prompt.normalize review.  Other selectors (for example lighting or action)
# are entity-specific or optional and therefore cannot satisfy a required rule
# for the shared Bible reference-image route.
_BIBLE_ALWAYS_AVAILABLE_SELECTORS = frozenset({
    PromptProfileFieldRule.SourceSelector.PROJECT_LANGUAGE,
    PromptProfileFieldRule.SourceSelector.PROJECT_ASPECT_RATIO,
    PromptProfileFieldRule.SourceSelector.REQUEST_THEME,
    PromptProfileFieldRule.SourceSelector.REQUEST_LANGUAGE,
    PromptProfileFieldRule.SourceSelector.REQUEST_SUBJECT,
    PromptProfileFieldRule.SourceSelector.REQUEST_COMPOSITION,
    PromptProfileFieldRule.SourceSelector.REQUEST_ASPECT_RATIO,
})
_BIBLE_POSITIVE_ANCHOR_SELECTORS = frozenset({
    PromptProfileFieldRule.SourceSelector.REQUEST_THEME,
    PromptProfileFieldRule.SourceSelector.REQUEST_SUBJECT,
    PromptProfileFieldRule.SourceSelector.REQUEST_COMPOSITION,
})
DIRECT_SOURCE_PROJECTION_KEY = "direct_v1"
DECLARATIVE_SOURCE_PROJECTION_KEY = "declarative_v1"
CLASSIFIED_REFERENCE_CONTEXT_PROJECTION_KEY = (
    "classified_reference_context_ir_v1"
)
_SAFE_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
_SAFE_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,}$")
_GENERATION_SOURCE_SELECTOR_RE = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,11}$",
)
_LANGUAGE_TAG_RE = re.compile(
    r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,5}$",
)
_LANGUAGE_POLICY_KEYWORDS = frozenset({"inherit", "preserve_source", "any"})
_STATIC_REFERENCE_TARGET_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_STATIC_REFERENCE_SNAPSHOT_SELECTORS = frozenset({"literal", "initial", "final"})
_STATIC_REFERENCE_VIOLATION_ACTIONS = frozenset({"block"})
_STATIC_REFERENCE_TRAILING_CLAUSE_ACTIONS = frozenset({
    "relocate_to_continuity",
})
_QUALITY_ADVISORY_KEYS = frozenset({
    "positive_negative_token_overlap",
    "short_shot_action_density",
    "motion_camera_reference_guidance",
})


def _prompt_profile_contract_max_bytes() -> int:
    return boundary_int("security.prompt_profile.contract_max_bytes")


def _prompt_profile_preview_max_bytes() -> int:
    return boundary_int("security.prompt_profile.preview_max_bytes")


def _generation_validators_max_items() -> int:
    return boundary_int(
        "security.contract_complexity.prompt_profile.max_validators",
    )


def _generation_context_selectors_max_items() -> int:
    return boundary_int(
        "security.contract_complexity.prompt_profile.max_context_selectors",
    )


def _static_reference_markers_max_items() -> int:
    return boundary_int("security.prompt_profile.max_static_reference_markers")


def _static_reference_marker_max_characters() -> int:
    return boundary_int(
        "security.prompt_profile.static_reference_marker_max_characters",
    )


def _field_text_max_characters() -> int:
    return boundary_int("security.prompt_profile.field_text_max_characters")


def _field_list_max_items() -> int:
    return boundary_int(
        "security.contract_complexity.prompt_profile.field_list_max_items",
    )


def _assignment_separator_max_characters() -> int:
    return boundary_int(
        "security.contract_complexity.prompt_profile.assignment_separator_max_characters",
    )


def _transition_patterns_max_items() -> int:
    return boundary_int(
        "security.contract_complexity.prompt_profile.transition_patterns_max_items",
    )


def _field_list_item_max_characters() -> int:
    return boundary_int(
        "security.prompt_profile.field_list_item_max_characters",
    )


def _field_numeric_absolute_max() -> int:
    return boundary_int("security.prompt_profile.numeric_absolute_max")

# A registered projection adapter owns only its typed source vocabulary and
# deterministic algorithm.  Target field keys, final order, channels, labels,
# separators and Layer-2 generation modes are versioned PromptProfile rules;
# keeping them here would turn a model example into an application invariant.
CLASSIFIED_REFERENCE_CONTEXT_OUTPUT_SELECTORS = frozenset({
    PromptProfileFieldRule.SourceSelector.H3_REF2VA_SUBJECT_DEFINITIONS,
    PromptProfileFieldRule.SourceSelector.H3_REF2VA_SUMMARY,
    PromptProfileFieldRule.SourceSelector.H3_REF2VA_RETENTION_ANALYSIS,
    PromptProfileFieldRule.SourceSelector.H3_REF2VA_DETAILED_DESCRIPTION,
    PromptProfileFieldRule.SourceSelector.H3_REF2VA_OVERALL_SOUNDSCAPE,
    PromptProfileFieldRule.SourceSelector.H3_REF2VA_NON_DIEGETIC_MUSIC,
})
SUPPORTED_FIELD_SOURCE_SELECTORS = tuple(
    value
    for value, _label in PromptProfileFieldRule.SourceSelector.choices
    if value not in CLASSIFIED_REFERENCE_CONTEXT_OUTPUT_SELECTORS
)


class PromptProfileError(ValidationError):
    code = "prompt_profile_invalid"

    def __init__(self, message: str, *, code: str | None = None):
        self.code = code or self.code
        super().__init__(message, code=self.code)


class PromptProfileResolutionError(PromptProfileError):
    code = "prompt_profile_resolution_failed"


class PromptProfileChannelLengthError(PromptProfileError):
    """Failed assembly evidence, not a compilation or permission to submit.

    Only the authorized read-only preparation display consumes this bounded
    payload. All execution callers still receive a ValidationError.
    """

    def __init__(self, messages, *, channels, lengths, profile_basis):
        self.diagnostic = json_contract({"channels": channels, "lengths": lengths, "profile_basis": profile_basis},
            max_bytes=_prompt_profile_preview_max_bytes()).value
        super().__init__(messages, code="prompt_profile_channel_too_long")


def normalize_prompt_profile_channel_limits(value: Any) -> dict[str, dict[str, int | None]]:
    """Return the strict, canonical per-channel output length contract.

    The registry stores only data. It cannot introduce a channel, validation
    expression, or executable rule that is unknown to the generic assembler.
    An absent channel is intentionally unlimited; there is no hidden fallback.
    """

    if not isinstance(value, dict):
        raise PromptProfileError(
            "channel_limits 必须是以受控输出通道为键的对象",
            code="prompt_profile_channel_limits_invalid",
        )
    unknown_channels = sorted(
        set(value) - set(PromptProfileFieldRule.Channel.values),
    )
    if unknown_channels:
        raise PromptProfileError(
            "channel_limits 包含未知通道: " + ", ".join(unknown_channels),
            code="prompt_profile_channel_limits_channel_unknown",
        )
    normalized: dict[str, dict[str, int | None]] = {}
    for channel in sorted(value):
        raw = value[channel]
        if not isinstance(raw, dict) or set(raw) - {"max_chars", "warn_at_chars"}:
            raise PromptProfileError(
                f"channel_limits.{channel} 只能包含 max_chars 与 warn_at_chars",
                code="prompt_profile_channel_limits_invalid",
            )
        maximum = raw.get("max_chars")
        warning = raw.get("warn_at_chars")
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or maximum < 1
            or maximum > _field_text_max_characters()
        ):
            raise PromptProfileError(
                "channel_limits."
                f"{channel}.max_chars 必须是 1–{_field_text_max_characters()} 的整数",
                code="prompt_profile_channel_limit_max_invalid",
            )
        if warning is not None and (
            isinstance(warning, bool)
            or not isinstance(warning, int)
            or warning < 1
            or warning > maximum
        ):
            raise PromptProfileError(
                f"channel_limits.{channel}.warn_at_chars 必须为空或 1–max_chars 的整数",
                code="prompt_profile_channel_limit_warning_invalid",
            )
        normalized[channel] = {
            "max_chars": maximum,
            "warn_at_chars": warning,
        }
    return normalized


def _normalize_static_reference_markers(
    value: Any,
    *,
    field_path: str,
) -> list[str]:
    if not isinstance(value, list) or len(value) > _static_reference_markers_max_items():
        raise PromptProfileError(
            f"{field_path} 必须是不超过 {_static_reference_markers_max_items()} 项的文本数组",
            code="prompt_profile_source_projection_config_invalid",
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for marker in value:
        if (
            not isinstance(marker, str)
            or not marker.strip()
            or len(marker) > _static_reference_marker_max_characters()
            or any(
                unicodedata.category(character) in {"Cc", "Cf"}
                for character in marker
            )
        ):
            raise PromptProfileError(
                f"{field_path} 只能包含非空、无控制字符且不超过 "
                f"{_static_reference_marker_max_characters()} 字符的字面量",
                code="prompt_profile_source_projection_config_invalid",
            )
        identity = marker.casefold()
        if identity in seen:
            raise PromptProfileError(
                f"{field_path} 不能包含忽略大小写后的重复项",
                code="prompt_profile_source_projection_config_invalid",
            )
        seen.add(identity)
        normalized.append(marker)
    return normalized


def _normalize_classified_reference_context_policy(value: Any) -> dict[str, Any]:
    """Normalize the inert formatting policy used by the classified Context-IR adapter."""

    if not isinstance(value, dict) or set(value) != {
        "retention_analysis",
        "detailed_description",
    }:
        raise PromptProfileError(
            "classified_reference_context_policy 必须且只能包含 "
            "retention_analysis 与 detailed_description",
            code="prompt_profile_source_projection_config_invalid",
        )
    retention = value.get("retention_analysis")
    if not isinstance(retention, dict) or set(retention) != {
        "include_subject_shot_usage",
        "assignment_separator",
    }:
        raise PromptProfileError(
            "classified_reference_context_policy.retention_analysis 字段不完整或包含未知字段",
            code="prompt_profile_source_projection_config_invalid",
        )
    include_subject_shot_usage = retention.get("include_subject_shot_usage")
    if type(include_subject_shot_usage) is not bool:
        raise PromptProfileError(
            "retention_analysis.include_subject_shot_usage 必须是布尔值",
            code="prompt_profile_source_projection_config_invalid",
        )
    separator = _normalize_static_reference_markers(
        [retention.get("assignment_separator")],
        field_path=(
            "classified_reference_context_policy.retention_analysis."
            "assignment_separator"
        ),
    )[0]
    if len(separator) > _assignment_separator_max_characters():
        raise PromptProfileError(
            "retention_analysis.assignment_separator 不得超过 "
            f"{_assignment_separator_max_characters()} 个字符",
            code="prompt_profile_source_projection_config_invalid",
        )

    detail = value.get("detailed_description")
    expected_detail_keys = {
        "style_opening_placement",
        "style_opening_max_sentences",
        "subject_reference_placement",
    }
    if not isinstance(detail, dict) or set(detail) != expected_detail_keys:
        raise PromptProfileError(
            "classified_reference_context_policy.detailed_description 字段不完整或包含未知字段",
            code="prompt_profile_source_projection_config_invalid",
        )
    style_opening_placement = str(
        detail.get("style_opening_placement") or "",
    ).strip()
    if style_opening_placement != "before_first_shot":
        raise PromptProfileError(
            "detailed_description.style_opening_placement 当前只支持 before_first_shot",
            code="prompt_profile_source_projection_config_invalid",
        )
    style_opening_max_sentences = detail.get("style_opening_max_sentences")
    if (
        isinstance(style_opening_max_sentences, bool)
        or not isinstance(style_opening_max_sentences, int)
        or style_opening_max_sentences not in {1, 2}
    ):
        raise PromptProfileError(
            "detailed_description.style_opening_max_sentences 必须是 1 或 2",
            code="prompt_profile_source_projection_config_invalid",
        )
    subject_reference_placement = str(
        detail.get("subject_reference_placement") or "",
    ).strip()
    if subject_reference_placement != "first_clear_appearance":
        raise PromptProfileError(
            "detailed_description.subject_reference_placement 当前只支持 first_clear_appearance",
            code="prompt_profile_source_projection_config_invalid",
        )
    return {
        "retention_analysis": {
            "include_subject_shot_usage": include_subject_shot_usage,
            "assignment_separator": separator,
        },
        "detailed_description": {
            "style_opening_placement": style_opening_placement,
            "style_opening_max_sentences": style_opening_max_sentences,
            "subject_reference_placement": subject_reference_placement,
        },
    }


def _normalize_declarative_primitive_options(value: Any, *, schema_version: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {REFERENCE_CONTEXT_ENGINE_KEY}:
        raise PromptProfileError("primitive_options 必须精确包含已注册的 reference_context_ir_v1",
                                 code="prompt_profile_source_projection_config_invalid")
    options = value[REFERENCE_CONTEXT_ENGINE_KEY]
    expected = {"rendering"} if schema_version in {4, 5} else {"reference_context_policy"}
    if not isinstance(options, dict) or set(options) != expected:
        raise PromptProfileError(f"primitive_options 字段必须为 {sorted(expected)}",
                                 code="prompt_profile_source_projection_config_invalid")
    if schema_version == 4:
        # Frozen legacy data remains inspectable and hashable, never executable.
        if not isinstance(options["rendering"], dict):
            raise PromptProfileError("历史 rendering 必须为对象", code="prompt_profile_rendering_invalid")
        return {REFERENCE_CONTEXT_ENGINE_KEY: {"rendering": deepcopy(options["rendering"])}}
    if schema_version == 5:
        from studio.services.reference_context import validate_rendering_policy
        from studio.services.prompt_compiler_contract import PromptBuildError
        try:
            return {REFERENCE_CONTEXT_ENGINE_KEY: {"rendering": validate_rendering_policy(options["rendering"])}}
        except PromptBuildError as exc:
            raise PromptProfileError(str(exc), code="prompt_profile_rendering_invalid") from exc
    # Historical normalization is for immutable hashes/read-only inspection only.
    # The runtime validator below rejects schemas v3/v4: no legacy compiler fallback.
    return {REFERENCE_CONTEXT_ENGINE_KEY: {"reference_context_policy":
        _normalize_classified_reference_context_policy(options["reference_context_policy"])}}


def _positive_integer(value: Any, *, field_path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PromptProfileError(
            f"{field_path} 必须是正整数",
            code="prompt_profile_source_projection_config_invalid",
        )
    return value


def normalize_prompt_profile_quality_advisories(value: Any) -> dict[str, Any]:
    """Normalize optional, versioned quality hints without hidden defaults.

    Each omitted advisory is intentionally disabled.  These values influence
    diagnostics only; structural, identity and frozen-contract checks remain
    outside this configuration and cannot be downgraded by it.
    """

    if not isinstance(value, dict):
        raise PromptProfileError(
            "quality_advisories 必须是对象",
            code="prompt_profile_source_projection_config_invalid",
        )
    if (
        "schema_version" not in value
        or value.get("schema_version") != 1
        or set(value) - ({"schema_version"} | _QUALITY_ADVISORY_KEYS)
    ):
        raise PromptProfileError(
            "quality_advisories 仅支持 schema_version=1 与受控建议项",
            code="prompt_profile_source_projection_config_invalid",
        )
    normalized: dict[str, Any] = {"schema_version": 1}

    overlap = value.get("positive_negative_token_overlap")
    if overlap is not None:
        expected = {"minimum_shared_tokens", "minimum_overlap_ratio"}
        if not isinstance(overlap, dict) or set(overlap) != expected:
            raise PromptProfileError(
                "quality_advisories.positive_negative_token_overlap 字段不完整或包含未知字段",
                code="prompt_profile_source_projection_config_invalid",
            )
        ratio = overlap.get("minimum_overlap_ratio")
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not 0 < ratio <= 1
        ):
            raise PromptProfileError(
                "positive_negative_token_overlap.minimum_overlap_ratio 必须大于 0 且不大于 1",
                code="prompt_profile_source_projection_config_invalid",
            )
        normalized["positive_negative_token_overlap"] = {
            "minimum_shared_tokens": _positive_integer(
                overlap.get("minimum_shared_tokens"),
                field_path=(
                    "positive_negative_token_overlap.minimum_shared_tokens"
                ),
            ),
            "minimum_overlap_ratio": float(ratio),
        }

    density = value.get("short_shot_action_density")
    if density is not None:
        expected = {
            "maximum_duration_ms",
            "maximum_action_characters",
            "minimum_clause_separators",
        }
        if not isinstance(density, dict) or set(density) != expected:
            raise PromptProfileError(
                "quality_advisories.short_shot_action_density 字段不完整或包含未知字段",
                code="prompt_profile_source_projection_config_invalid",
            )
        normalized["short_shot_action_density"] = {
            key: _positive_integer(
                density.get(key),
                field_path=f"short_shot_action_density.{key}",
            )
            for key in sorted(expected)
        }

    motion = value.get("motion_camera_reference_guidance")
    if motion is not None:
        expected = {"minimum_english_words", "generic_values"}
        if not isinstance(motion, dict) or set(motion) != expected:
            raise PromptProfileError(
                "quality_advisories.motion_camera_reference_guidance 字段不完整或包含未知字段",
                code="prompt_profile_source_projection_config_invalid",
            )
        normalized["motion_camera_reference_guidance"] = {
            "minimum_english_words": _positive_integer(
                motion.get("minimum_english_words"),
                field_path=(
                    "motion_camera_reference_guidance.minimum_english_words"
                ),
            ),
            "generic_values": _normalize_static_reference_markers(
                motion.get("generic_values"),
                field_path="motion_camera_reference_guidance.generic_values",
            ),
        }
    return normalized


def normalize_prompt_profile_source_projection_config(value: Any) -> dict[str, Any]:
    """Return the strict, inert source-projection configuration contract.

    The configuration contains only selectors, booleans and literal markers.
    It cannot execute templates, functions or administrator supplied regular
    expressions.  An empty object intentionally means that the selected
    projector performs no extra source transformation.
    """

    if not isinstance(value, dict):
        raise PromptProfileError(
            "source_projection_config 必须是对象",
            code="prompt_profile_source_projection_config_invalid",
        )
    if not value:
        return {}
    supported_policy_keys = {
        "static_reference_policy",
        "classified_reference_context_policy",
        "primitive_options",
    }
    optional_config_keys = {"quality_advisories"}
    configured_policy_keys = set(value) & supported_policy_keys
    if (
        set(value) - ({"schema_version"} | supported_policy_keys | optional_config_keys)
        or len(configured_policy_keys) != 1
        or "schema_version" not in value
    ):
        raise PromptProfileError(
            "source_projection_config 必须包含 schema_version，且只能选择一个受控投影策略",
            code="prompt_profile_source_projection_config_invalid",
        )
    config_schema_version = value.get("schema_version")
    if config_schema_version not in {1, 2, 3, 4, 5}:
        raise PromptProfileError(
            "source_projection_config.schema_version 仅支持 1、2、3、4 或 5；声明式执行需 5",
            code="prompt_profile_source_projection_config_schema_unsupported",
        )
    if "primitive_options" in value:
        if config_schema_version not in {3, 4, 5}:
            raise PromptProfileError(
                "primitive_options 需要 source_projection_config.schema_version=5",
                code="prompt_profile_source_projection_config_schema_unsupported",
            )
        return {
            "schema_version": config_schema_version,
            "primitive_options": _normalize_declarative_primitive_options(
                value.get("primitive_options"), schema_version=config_schema_version,
            ),
            **({
                "quality_advisories": normalize_prompt_profile_quality_advisories(
                    value.get("quality_advisories"),
                ),
            } if "quality_advisories" in value else {}),
        }
    if "classified_reference_context_policy" in value:
        if config_schema_version != 1:
            raise PromptProfileError(
                "classified_reference_context_policy 当前只支持 "
                "source_projection_config.schema_version=1",
                code="prompt_profile_source_projection_config_schema_unsupported",
            )
        return {
            "schema_version": 1,
            "classified_reference_context_policy": (
                _normalize_classified_reference_context_policy(
                    value.get("classified_reference_context_policy"),
                )
            ),
            **({
                "quality_advisories": normalize_prompt_profile_quality_advisories(
                    value.get("quality_advisories"),
                ),
            } if "quality_advisories" in value else {}),
        }

    policy = value.get("static_reference_policy")
    expected_policy_keys = {
        "snapshot_selector",
        "default_target_kind",
        "allow_context",
        "context_source_selectors",
        "transition_markers",
        "transition_patterns",
        "context_clause_policy",
        "negative_token_exclusions",
        "forbidden_markers",
        "violation_action",
    }
    if config_schema_version == 2:
        expected_policy_keys.add("initial_snapshot_clause_policy")
    if not isinstance(policy, dict) or set(policy) != expected_policy_keys:
        raise PromptProfileError(
            "static_reference_policy 字段不完整或包含未知字段",
            code="prompt_profile_source_projection_config_invalid",
        )
    snapshot_selector = str(policy.get("snapshot_selector") or "").strip()
    if snapshot_selector not in _STATIC_REFERENCE_SNAPSHOT_SELECTORS:
        raise PromptProfileError(
            "static_reference_policy.snapshot_selector 必须是 literal、initial 或 final",
            code="prompt_profile_source_projection_config_invalid",
        )
    default_target_kind = str(policy.get("default_target_kind") or "").strip()
    if _STATIC_REFERENCE_TARGET_KIND_RE.fullmatch(default_target_kind) is None:
        raise PromptProfileError(
            "static_reference_policy.default_target_kind 格式无效",
            code="prompt_profile_source_projection_config_invalid",
        )
    allow_context = policy.get("allow_context")
    if (
        not isinstance(allow_context, dict)
        or set(allow_context) != {"default", "target_overrides"}
        or type(allow_context.get("default")) is not bool
        or not isinstance(allow_context.get("target_overrides"), dict)
    ):
        raise PromptProfileError(
            "static_reference_policy.allow_context 必须包含布尔 default 与 target_overrides 对象",
            code="prompt_profile_source_projection_config_invalid",
        )
    target_overrides: dict[str, bool] = {}
    for target_kind, enabled in sorted(allow_context["target_overrides"].items()):
        if (
            not isinstance(target_kind, str)
            or _STATIC_REFERENCE_TARGET_KIND_RE.fullmatch(target_kind) is None
            or type(enabled) is not bool
        ):
            raise PromptProfileError(
                "static_reference_policy.allow_context.target_overrides 格式无效",
                code="prompt_profile_source_projection_config_invalid",
            )
        target_overrides[target_kind] = enabled
    selectors = policy.get("context_source_selectors")
    if (
        not isinstance(selectors, list)
        or not selectors
        or len(selectors) > len(PromptProfileFieldRule.SourceSelector.values)
        or any(
            not isinstance(selector, str)
            or selector not in PromptProfileFieldRule.SourceSelector.values
            or selector
            == PromptProfileFieldRule.SourceSelector.FIXED_VALUE
            for selector in selectors
        )
        or len(selectors) != len(set(selectors))
    ):
        raise PromptProfileError(
            "static_reference_policy.context_source_selectors 必须是非空且不重复的受控来源 selector 数组",
            code="prompt_profile_source_projection_config_invalid",
        )
    transition_markers = _normalize_static_reference_markers(
        policy.get("transition_markers"),
        field_path="static_reference_policy.transition_markers",
    )
    if snapshot_selector in {"initial", "final"} and not transition_markers:
        raise PromptProfileError(
            "initial/final snapshot_selector 必须配置 transition_markers",
            code="prompt_profile_source_projection_config_invalid",
        )
    transition_patterns = policy.get("transition_patterns")
    if (
        not isinstance(transition_patterns, list)
        or len(transition_patterns) > _transition_patterns_max_items()
    ):
        raise PromptProfileError(
            "static_reference_policy.transition_patterns 必须是不超过 "
            f"{_transition_patterns_max_items()} 项的受控模式数组",
            code="prompt_profile_source_projection_config_invalid",
        )
    normalized_patterns: list[dict[str, str]] = []
    seen_patterns: set[tuple[str, str, str]] = set()
    for pattern in transition_patterns:
        if not isinstance(pattern, dict) or set(pattern) != {
            "kind", "from_marker", "to_marker",
        } or pattern.get("kind") != "from_to":
            raise PromptProfileError(
                "transition_patterns 当前只接受 kind=from_to 及 from_marker/to_marker 字面量",
                code="prompt_profile_source_projection_config_invalid",
            )
        markers = _normalize_static_reference_markers(
            [pattern.get("from_marker"), pattern.get("to_marker")],
            field_path="static_reference_policy.transition_patterns",
        )
        identity = ("from_to", markers[0].casefold(), markers[1].casefold())
        if identity in seen_patterns:
            raise PromptProfileError(
                "static_reference_policy.transition_patterns 不能重复",
                code="prompt_profile_source_projection_config_invalid",
            )
        seen_patterns.add(identity)
        normalized_patterns.append({
            "kind": "from_to",
            "from_marker": markers[0],
            "to_marker": markers[1],
        })
    context_clause_policy = policy.get("context_clause_policy")
    if (
        not isinstance(context_clause_policy, dict)
        or set(context_clause_policy) != {"delimiters", "discard_markers"}
    ):
        raise PromptProfileError(
            "static_reference_policy.context_clause_policy 必须包含 delimiters 与 discard_markers",
            code="prompt_profile_source_projection_config_invalid",
        )
    clause_delimiters = _normalize_static_reference_markers(
        context_clause_policy.get("delimiters"),
        field_path="static_reference_policy.context_clause_policy.delimiters",
    )
    if not clause_delimiters:
        raise PromptProfileError(
            "static_reference_policy.context_clause_policy.delimiters 不能为空",
            code="prompt_profile_source_projection_config_invalid",
        )
    clause_discard_markers = _normalize_static_reference_markers(
        context_clause_policy.get("discard_markers"),
        field_path="static_reference_policy.context_clause_policy.discard_markers",
    )
    negative_token_exclusions = policy.get("negative_token_exclusions")
    if not isinstance(negative_token_exclusions, dict):
        raise PromptProfileError(
            "static_reference_policy.negative_token_exclusions 必须是 selector 到 token 数组的对象",
            code="prompt_profile_source_projection_config_invalid",
        )
    normalized_negative_exclusions: dict[str, list[str]] = {}
    for selector, tokens in sorted(negative_token_exclusions.items()):
        if (
            not isinstance(selector, str)
            or selector not in PromptProfileFieldRule.SourceSelector.values
            or selector == PromptProfileFieldRule.SourceSelector.FIXED_VALUE
        ):
            raise PromptProfileError(
                "static_reference_policy.negative_token_exclusions 包含未知 selector",
                code="prompt_profile_source_projection_config_invalid",
            )
        normalized_negative_exclusions[selector] = _normalize_static_reference_markers(
            tokens,
            field_path=(
                "static_reference_policy.negative_token_exclusions."
                + selector
            ),
        )
    forbidden_markers = policy.get("forbidden_markers")
    if (
        not isinstance(forbidden_markers, dict)
        or set(forbidden_markers) != {"always", "when_context_disallowed"}
    ):
        raise PromptProfileError(
            "static_reference_policy.forbidden_markers 必须包含 always 与 when_context_disallowed",
            code="prompt_profile_source_projection_config_invalid",
        )
    normalized_forbidden = {
        key: _normalize_static_reference_markers(
            forbidden_markers.get(key),
            field_path=f"static_reference_policy.forbidden_markers.{key}",
        )
        for key in ("always", "when_context_disallowed")
    }
    violation_action = str(policy.get("violation_action") or "").strip()
    if violation_action not in _STATIC_REFERENCE_VIOLATION_ACTIONS:
        raise PromptProfileError(
            "static_reference_policy.violation_action 当前只支持 block；"
            "静态参考装配属于模型调用前的确定性终检，不能在此无限重试",
            code="prompt_profile_source_projection_config_invalid",
        )
    initial_snapshot_clause_policy = None
    if config_schema_version == 2:
        clause_policy = policy.get("initial_snapshot_clause_policy")
        expected_clause_keys = {
            "delimiters",
            "initial_state_markers",
            "continuity_source_selector",
            "trailing_clause_action",
        }
        if not isinstance(clause_policy, dict) or set(clause_policy) != expected_clause_keys:
            raise PromptProfileError(
                "static_reference_policy.initial_snapshot_clause_policy "
                "字段不完整或包含未知字段",
                code="prompt_profile_source_projection_config_invalid",
            )
        if snapshot_selector != "initial":
            raise PromptProfileError(
                "initial_snapshot_clause_policy 只适用于 snapshot_selector=initial",
                code="prompt_profile_source_projection_config_invalid",
            )
        initial_clause_delimiters = _normalize_static_reference_markers(
            clause_policy.get("delimiters"),
            field_path=(
                "static_reference_policy.initial_snapshot_clause_policy.delimiters"
            ),
        )
        if not initial_clause_delimiters:
            raise PromptProfileError(
                "initial_snapshot_clause_policy.delimiters 不能为空",
                code="prompt_profile_source_projection_config_invalid",
            )
        initial_state_markers = _normalize_static_reference_markers(
            clause_policy.get("initial_state_markers"),
            field_path=(
                "static_reference_policy.initial_snapshot_clause_policy."
                "initial_state_markers"
            ),
        )
        if not initial_state_markers:
            raise PromptProfileError(
                "initial_snapshot_clause_policy.initial_state_markers 不能为空",
                code="prompt_profile_source_projection_config_invalid",
            )
        continuity_source_selector = str(
            clause_policy.get("continuity_source_selector") or "",
        ).strip()
        if (
            continuity_source_selector
            not in PromptProfileFieldRule.SourceSelector.values
            or continuity_source_selector
            == PromptProfileFieldRule.SourceSelector.FIXED_VALUE
        ):
            raise PromptProfileError(
                "initial_snapshot_clause_policy.continuity_source_selector "
                "必须是受控且非 fixed.value 的来源 selector",
                code="prompt_profile_source_projection_config_invalid",
            )
        trailing_clause_action = str(
            clause_policy.get("trailing_clause_action") or "",
        ).strip()
        if trailing_clause_action not in _STATIC_REFERENCE_TRAILING_CLAUSE_ACTIONS:
            raise PromptProfileError(
                "initial_snapshot_clause_policy.trailing_clause_action "
                "当前只支持 relocate_to_continuity",
                code="prompt_profile_source_projection_config_invalid",
            )
        initial_snapshot_clause_policy = {
            "delimiters": initial_clause_delimiters,
            "initial_state_markers": initial_state_markers,
            "continuity_source_selector": continuity_source_selector,
            "trailing_clause_action": trailing_clause_action,
        }
    return {
        "schema_version": config_schema_version,
        "static_reference_policy": {
            "snapshot_selector": snapshot_selector,
            "default_target_kind": default_target_kind,
            "allow_context": {
                "default": allow_context["default"],
                "target_overrides": target_overrides,
            },
            "context_source_selectors": list(selectors),
            "transition_markers": transition_markers,
            "transition_patterns": normalized_patterns,
            "context_clause_policy": {
                "delimiters": clause_delimiters,
                "discard_markers": clause_discard_markers,
            },
            "negative_token_exclusions": normalized_negative_exclusions,
            "forbidden_markers": normalized_forbidden,
            "violation_action": violation_action,
            **({
                "initial_snapshot_clause_policy": initial_snapshot_clause_policy,
            } if initial_snapshot_clause_policy is not None else {}),
        },
        **({
            "quality_advisories": normalize_prompt_profile_quality_advisories(
                value.get("quality_advisories"),
            ),
        } if "quality_advisories" in value else {}),
    }




_SEPARATORS = {
    PromptProfileFieldRule.Separator.NEWLINE: "\n",
    PromptProfileFieldRule.Separator.COMMA: ", ",
    PromptProfileFieldRule.Separator.SEMICOLON: "; ",
    PromptProfileFieldRule.Separator.SPACE: " ",
}


def model_family_for_profile(profile: ModelProfileVersion) -> str:
    try:
        specification = verified_model_profile_specification(profile)
    except ValidationError as exc:
        raise PromptProfileResolutionError(
            "模型版本 specification 完整性校验失败",
            code="prompt_profile_model_specification_invalid",
        ) from exc
    if not isinstance(specification, dict):
        raise PromptProfileResolutionError(
            "模型版本 specification 必须为对象",
            code="prompt_profile_model_specification_invalid",
        )
    explicit = str(specification.get("model_family") or "").strip().casefold()
    if not explicit:
        raise PromptProfileResolutionError(
            "模型版本必须在 specification.model_family 显式声明模型族；"
            "不得从 provider、profile_key 或 model_identifier 推断",
            code="prompt_profile_model_family_required",
        )
    if _SAFE_FAMILY_RE.fullmatch(explicit) is None:
        raise PromptProfileResolutionError(
            "模型版本 specification.model_family 格式无效",
            code="prompt_profile_model_family_invalid",
        )
    return explicit


def validate_prompt_operation_key(value: object) -> str:
    """Validate an operation identifier without interpreting it as code."""

    operation = str(value or "").strip()
    maximum = boundary_int(
        "security.contract_complexity.prompt_profile_operation_max_characters",
    )
    if _SAFE_OPERATION_RE.fullmatch(operation) is None or len(operation) > maximum:
        raise PromptProfileError(
            f"operation 必须为 2–{maximum} 位小写受控 key，仅包含字母、数字、点、下划线或短横线",
            code="prompt_profile_operation_key_invalid",
        )
    return operation


def prompt_operation_publish_evidence(
    profile: PromptProfileVersion,
) -> dict[str, str] | None:
    """Return immutable/control-plane evidence for a Profile operation.

    The operation is always data. Evidence only permits publishing a prompt
    contract; it never dispatches a function, imports a script, or creates an
    executable route. Runtime selection still requires its own exact binding,
    deployment/workflow contract, and readiness evidence.
    """

    operation = validate_prompt_operation_key(profile.operation)
    if operation in CONTROLLED_PROMPT_OPERATION_REGISTRY:
        return {"source": "controlled_operation_registry", "operation": operation}

    model_rows = ModelProfileVersion.objects.filter(
        status=ModelProfileVersion.Status.PUBLISHED,
        provider__is_active=True,
    ).select_related("provider").order_by("profile_key", "-version")
    for model_profile in model_rows.iterator(chunk_size=200):
        try:
            if model_family_for_profile(model_profile) != profile.model_family:
                continue
            specification = verified_model_profile_specification(model_profile)
        except ValidationError:
            continue
        capabilities = (
            specification.get("capabilities")
            if isinstance(specification, dict)
            else None
        )
        if (
            isinstance(capabilities, list)
            and all(
                isinstance(item, str)
                and item
                and item == item.strip()
                for item in capabilities
            )
            and len(capabilities) == len(set(capabilities))
            and operation in capabilities
        ):
            return {
                "source": "published_model_profile_capability",
                "operation": operation,
                "model_profile_version_id": str(model_profile.id),
            }

    # A registered provider driver may expose a bounded operation directly.
    # ProviderProfile.operations remains descriptive text and is deliberately
    # not accepted as capability evidence.
    from studio.services.provider_registry import driver_for

    for provider in ProviderProfile.objects.filter(is_active=True).order_by("key"):
        try:
            capabilities = driver_for(provider).capabilities() or {}
        except Exception:
            continue
        operations = capabilities.get("operations")
        if (
            isinstance(operations, list)
            and all(isinstance(item, str) and item for item in operations)
            and operation in operations
        ):
            return {
                "source": "registered_provider_driver_capability",
                "operation": operation,
                "provider_id": str(provider.id),
            }
    return None


def validate_prompt_operation_publish_evidence(
    profile: PromptProfileVersion,
) -> dict[str, str]:
    evidence = prompt_operation_publish_evidence(profile)
    if evidence is None:
        raise PromptProfileError(
            "operation 尚无可验证能力证据：请先发布同模型族且在 "
            "specification.capabilities 精确声明该 operation 的模型版本，或先注册受控 provider/operation 实现",
            code="prompt_profile_operation_evidence_missing",
        )
    return evidence


def profile_rule_definition(rule, *, require_published=False):
    try:
        return resolve_rule_definition(rule, require_published=require_published)
    except ValidationError as exc:
        raise PromptProfileError(str(exc), code="prompt_profile_field_version_invalid") from exc


def validate_field_source_selector(selector: str) -> None:
    """One current vocabulary for authoring and new execution input projection."""
    if selector not in SUPPORTED_FIELD_SOURCE_SELECTORS:
        raise PromptProfileError(
            "来源不在当前目录中；创作字段请使用确切结构版本的 request.structured_input 路径，"
            "不读取或转换旧镜头列",
            code="prompt_profile_source_selector_unsupported",
        )


def _validate_rule_values(rule: PromptProfileFieldRule) -> None:
    validate_field_source_selector(rule.source_selector)
    if (
        rule.source_selector == PromptProfileFieldRule.SourceSelector.FIXED_VALUE
        and rule.fixed_value is None
    ):
        raise PromptProfileError("fixed.value 规则必须在本规则配置固定值")
    if rule.source_selector == PromptProfileFieldRule.SourceSelector.FIXED_VALUE:
        _typed_value(profile_rule_definition(rule), rule.fixed_value)
        if rule.has_default:
            raise PromptProfileError("固定值来源不能同时配置缺失默认值")
    elif rule.fixed_value is not None and rule.generation_source_selector != PromptProfileFieldRule.SourceSelector.FIXED_VALUE:
        raise PromptProfileError("仅固定值来源可以配置 fixed_value")
    if rule.has_default and rule.default_value is not None:
        _typed_value(profile_rule_definition(rule), rule.default_value)
    if not rule.has_default and rule.default_value is not None:
        raise PromptProfileError("配置 default_value 时必须启用 has_default")


def validate_profile_rule(
    rule: PromptProfileFieldRule,
    *, require_published_source: bool = True,
) -> None:
    from studio.services.content_sources import validate_rule_source
    profile_rule_definition(rule, require_published=require_published_source)
    validate_rule_source(rule, require_published=require_published_source)
    from studio.services.prompt_profile_structures import validate_structured_rule
    validate_structured_rule(rule, require_published=require_published_source)
    if rule.profile_version.status != PromptProfileVersion.Status.DRAFT:
        raise PromptProfileError("published/retired PromptProfile 的字段规则不可修改")
    if not rule.field_definition.is_active:
        raise PromptProfileError("字段规则不能引用已停用字段")
    _validate_rule_values(rule)
    if (
        rule.channel == PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION
        and rule.source_selector != PromptProfileFieldRule.SourceSelector.FIXED_VALUE
    ):
        raise PromptProfileError(
            "system_instruction 只能装配本规则维护的固定值；项目和请求内容不得提升为系统指令",
            code="prompt_profile_untrusted_system_source",
        )
    primitive_key = str(rule.projection_primitive or "").strip()
    if (
        rule.source_selector
        == PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
    ):
        if rule.profile_version.source_projection_key != DECLARATIVE_SOURCE_PROJECTION_KEY:
            raise PromptProfileError(
                "projection.output 只能用于 declarative_v1 PromptProfile",
                code="prompt_profile_projection_primitive_incompatible",
            )
        if primitive_key not in SOURCE_PROJECTION_PRIMITIVES:
            raise PromptProfileError(
                f"projection.output 必须选择已注册投影原语: {primitive_key or '(empty)'}",
                code="prompt_profile_projection_primitive_unknown",
            )
    elif primitive_key:
        raise PromptProfileError(
            "非 projection.output 字段规则不得配置 projection_primitive",
            code="prompt_profile_projection_primitive_incompatible",
        )
    if (
        rule.profile_version.source_projection_key == DIRECT_SOURCE_PROJECTION_KEY
        and rule.generation_output_semantics
        == PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
    ):
        raise PromptProfileError(
            "direct_v1 字段规则不能保存 contribution；请改为 final 或选择支持贡献合成的声明式投影器",
            code="prompt_profile_source_projection_semantics_invalid",
        )
    if (
        rule.generation_output_semantics
        == PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        and rule.generation_mode in MODEL_GENERATION_MODES
    ):
        if (
            rule.source_selector
            != PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
        ):
            raise PromptProfileError(
                "contribution 必须由 projection.output 的确定性原语消费；"
                "不能把贡献值直接当作最终提示词来源",
                code="prompt_profile_projection_contribution_requires_projection_output",
            )
        primitive = SOURCE_PROJECTION_PRIMITIVES.get(primitive_key)
        generation_source = str(rule.generation_source_selector or "").strip()
        if (
            primitive is not None
            and (not primitive.contribution_source_selectors or generation_source != "request.structured_input")
        ):
            raise PromptProfileError(
                "所选 projection_primitive 不支持当前 contribution 主要内容来源",
                code="prompt_profile_projection_contribution_unsupported",
            )
    if (
        type(rule.max_chars) is not int
        or not 1 <= rule.max_chars <= _field_text_max_characters()
    ):
        raise PromptProfileError(
            f"max_chars 必须是 1–{_field_text_max_characters()} 的整数",
            code="prompt_profile_field_limit_invalid",
        )
    validate_generation_field_contract(rule)
    validate_current_generation_source(rule)


def validate_current_generation_source(rule: PromptProfileFieldRule) -> None:
    """Validate the same explicit source vocabulary when writing and executing.

    The model-backed modes reuse the implemented execution reader catalog.
    Modes that do not run field generation always read the final source
    directly and therefore do not expose a second independent selector.
    """

    mode = str(rule.generation_mode or "")
    generation_source = str(rule.generation_source_selector or "").strip()
    if mode in MODEL_GENERATION_MODES:
        if rule.profile_version.operation in FIELD_AUXILIARY_OPERATIONS:
            raise PromptProfileError(
                "字段辅助实例必须为叶子，不能再声明辅助生成或翻译",
                code="prompt_profile_generation_mode_operation_unsupported",
            )
        if rule.channel == PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION:
            raise PromptProfileError("系统约束须为维护者明确配置的固定文本，不能由辅助结果提升为系统指令",
                code="prompt_profile_untrusted_system_source")
        if generation_source == "projection.output":
            if (rule.source_selector != "projection.output" or rule.profile_version.source_projection_key != "declarative_v1"
                    or rule.generation_output_semantics != "final"):
                raise PromptProfileError("装配原文仅用于声明式字段的最终结果，不可注入或作为逐单元贡献",
                    code="prompt_profile_generation_source_unknown")
            return
        from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR, validate_structured_rule
        if rule.source_selector == STRUCTURED_INPUT_SELECTOR or generation_source == STRUCTURED_INPUT_SELECTOR:
            validate_structured_rule(rule)
            return
        if generation_source not in prompt_generation_source_values():
            raise PromptProfileError(
                "模型生成或仅翻译的主要内容来源不在服务端当前可解析目录中；"
                "请从 PromptProfile options 返回的 generation_source_selectors 选择",
                code="prompt_profile_generation_source_unknown",
            )
        return
    if mode in {
        PromptProfileFieldRule.GenerationMode.SERVER_LOCKED,
        PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
    } and generation_source != rule.source_selector:
        raise PromptProfileError(
            "服务端锁定或原值透传不单独选择主要内容来源；"
            "generation_source_selector 必须与 source_selector 一致",
            code="prompt_profile_generation_source_mismatch",
        )


def prompt_language_options():
    """Shared language-tag choices for policy authoring and execution forms."""
    return [
        {"value": "en", "label": "英文 / English"},
        {"value": "zh-Hans", "label": "简体中文 / Simplified Chinese"},
        {"value": "zh-Hant", "label": "繁体中文 / Traditional Chinese"},
    ]


def _valid_language_policy(value: str) -> bool:
    if value in _LANGUAGE_POLICY_KEYWORDS or _LANGUAGE_TAG_RE.fullmatch(value):
        return True
    if not value.startswith("bilingual:"):
        return False
    tags = value.removeprefix("bilingual:").split("+")
    return (
        len(tags) == 2
        and tags[0].casefold() != tags[1].casefold()
        and all(_LANGUAGE_TAG_RE.fullmatch(tag) for tag in tags)
    )


def validate_generation_field_contract(rule: PromptProfileFieldRule) -> None:
    """Validate the current explicit field contract without executing config."""

    mode = str(rule.generation_mode or "")
    if mode not in PromptProfileFieldRule.GenerationMode.values:
        raise PromptProfileError(
            f"未知 generation_mode: {mode or '(empty)'}",
            code="prompt_profile_generation_mode_invalid",
        )
    generation_source = str(rule.generation_source_selector or "").strip()
    context_selectors = rule.generation_context_selectors
    if not isinstance(context_selectors, list) or (
        len(context_selectors) > _generation_context_selectors_max_items()
    ):
        raise PromptProfileError(
            "generation_context_selectors 必须是不超过 "
            f"{_generation_context_selectors_max_items()} 项的数组",
            code="prompt_profile_generation_context_selectors_invalid",
        )
    if any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in context_selectors
    ):
        raise PromptProfileError(
            "generation_context_selectors 只能包含非空受控名称",
            code="prompt_profile_generation_context_selectors_invalid",
        )
    if len(context_selectors) != len(set(context_selectors)):
        raise PromptProfileError(
            "generation_context_selectors 不能重复",
            code="prompt_profile_generation_context_selectors_invalid",
        )
    from studio.services.prompt_generation_contexts import structured_context_path
    unknown_context_selectors = sorted(
        set(context_selectors)
        - set(PromptProfileFieldRule.GenerationContextSelector.values),
    )
    unknown_context_selectors = [item for item in unknown_context_selectors if structured_context_path(item) is None]
    if unknown_context_selectors:
        raise PromptProfileError(
            "generation_context_selectors 包含未知范围: "
            + ", ".join(unknown_context_selectors),
            code="prompt_profile_generation_context_selector_unknown",
        )
    if not generation_source:
        raise PromptProfileError(
            "字段规则必须显式配置 generation_source_selector",
            code="prompt_profile_generation_source_required",
        )
    elif _GENERATION_SOURCE_SELECTOR_RE.fullmatch(generation_source) is None:
        raise PromptProfileError(
            "generation_source_selector 只允许最多 12 段的小写点路径；"
            "不接受通配符、括号、函数、模板或正则",
            code="prompt_profile_generation_source_invalid",
        )
    if (
        mode == PromptProfileFieldRule.GenerationMode.PASSTHROUGH
        and generation_source
        and generation_source != rule.source_selector
    ):
        raise PromptProfileError(
            "passthrough 必须从本规则 source_selector 原值透传；"
            "需要其他来源时请选择另一 generation_mode",
            code="prompt_profile_generation_passthrough_source_mismatch",
        )
    instruction = str(rule.generation_instruction or "").strip()
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        and character not in "\n\r\t"
        for character in instruction
    ):
        raise PromptProfileError(
            "generation_instruction 包含禁止的控制字符",
            code="prompt_profile_generation_instruction_invalid",
        )
    if mode in {
        PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
        PromptProfileFieldRule.GenerationMode.TRANSLATE_ONLY,
    } and not instruction:
        raise PromptProfileError(
            "model_generated/translate_only 必须填写版本化 generation_instruction",
            code="prompt_profile_generation_instruction_required",
        )
    if any(marker in instruction for marker in ("{{", "}}", "${", "<%", "%>", "{%", "%}")):
        raise PromptProfileError(
            "generation_instruction 只能是说明文本，不能包含模板表达式",
            code="prompt_profile_generation_instruction_executable",
        )
    output_semantics = str(rule.generation_output_semantics or "")
    if output_semantics not in PromptProfileFieldRule.GenerationOutputSemantics.values:
        raise PromptProfileError(
            "generation_output_semantics 不受支持",
            code="prompt_profile_generation_output_semantics_invalid",
        )
    if (
        output_semantics == PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        and mode not in {
            PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
            PromptProfileFieldRule.GenerationMode.TRANSLATE_ONLY,
        }
    ):
        raise PromptProfileError(
            "contribution 只允许 model_generated/translate_only",
            code="prompt_profile_generation_output_semantics_inconsistent",
        )

    language_policy = str(rule.language_policy or "").strip()
    if not _valid_language_policy(language_policy):
        raise PromptProfileError(
            "language_policy 只允许 inherit、preserve_source、any、"
            "BCP 47 标签或 bilingual:<tag>+<tag>",
            code="prompt_profile_generation_language_invalid",
        )
    if (
        mode == PromptProfileFieldRule.GenerationMode.TRANSLATE_ONLY
        and language_policy in {"preserve_source", "any"}
    ):
        raise PromptProfileError(
            "translate_only 必须指定 inherit、目标语言标签或双语目标，"
            "不能使用 preserve_source/any",
            code="prompt_profile_generation_language_inconsistent",
        )

    length_unit = str(rule.length_unit or "")
    if length_unit not in PromptProfileFieldRule.LengthUnit.values:
        raise PromptProfileError(
            f"未知 length_unit: {length_unit or '(empty)'}",
            code="prompt_profile_generation_length_unit_invalid",
        )
    minimum = rule.generation_min
    maximum = rule.generation_max
    for field_name, value in (
        ("generation_min", minimum),
        ("generation_max", maximum),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > _field_text_max_characters()
        ):
            raise PromptProfileError(
                f"{field_name} 必须为空或 1–{_field_text_max_characters()} 的整数",
                code="prompt_profile_generation_length_invalid",
            )
    if length_unit == PromptProfileFieldRule.LengthUnit.NONE:
        if minimum is not None or maximum is not None:
            raise PromptProfileError(
                "length_unit=none 时 generation_min/generation_max 必须为空",
                code="prompt_profile_generation_length_inconsistent",
            )
    elif minimum is None and maximum is None:
        raise PromptProfileError(
            "启用长度单位后至少填写 generation_min 或 generation_max",
            code="prompt_profile_generation_length_required",
        )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise PromptProfileError(
            "generation_min 不能大于 generation_max",
            code="prompt_profile_generation_length_inconsistent",
        )

    validator_keys = rule.generation_validator_keys
    if not isinstance(validator_keys, list) or len(validator_keys) > _generation_validators_max_items():
        raise PromptProfileError(
            f"generation_validator_keys 必须是不超过 {_generation_validators_max_items()} 项的数组",
            code="prompt_profile_generation_validators_invalid",
        )
    if any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in validator_keys
    ):
        raise PromptProfileError(
            "generation_validator_keys 只能包含非空受控名称",
            code="prompt_profile_generation_validators_invalid",
        )
    if len(validator_keys) != len(set(validator_keys)):
        raise PromptProfileError(
            "generation_validator_keys 不能重复",
            code="prompt_profile_generation_validators_invalid",
        )
    unknown = sorted(
        set(validator_keys) - set(PromptProfileFieldRule.GenerationValidator.values),
    )
    if unknown:
        raise PromptProfileError(
            "generation_validator_keys 包含未知校验器: " + ", ".join(unknown),
            code="prompt_profile_generation_validator_unknown",
        )
    selected = set(validator_keys)
    if (
        PromptProfileFieldRule.GenerationValidator.NON_EMPTY in selected
        and not rule.generation_required
    ):
        raise PromptProfileError(
            "non_empty 校验器要求 generation_required=true",
            code="prompt_profile_generation_validators_inconsistent",
        )
    json_shapes = selected & {
        PromptProfileFieldRule.GenerationValidator.JSON_OBJECT,
        PromptProfileFieldRule.GenerationValidator.JSON_ARRAY,
    }
    if len(json_shapes) > 1:
        raise PromptProfileError(
            "json_object 与 json_array 不能同时启用",
            code="prompt_profile_generation_validators_inconsistent",
        )
    if json_shapes and PromptProfileFieldRule.GenerationValidator.VALID_JSON not in selected:
        raise PromptProfileError(
            "json_object/json_array 必须同时启用 valid_json",
            code="prompt_profile_generation_validators_inconsistent",
        )
    if (
        PromptProfileFieldRule.GenerationValidator.UNIQUE_ITEMS in selected
        and length_unit != PromptProfileFieldRule.LengthUnit.ITEMS
    ):
        raise PromptProfileError(
            "unique_items 要求 length_unit=items",
            code="prompt_profile_generation_validators_inconsistent",
        )
    if (
        PromptProfileFieldRule.GenerationValidator.LENGTH_ADVISORY in selected
        and length_unit == PromptProfileFieldRule.LengthUnit.NONE
    ):
        raise PromptProfileError(
            "length_advisory 要求配置长度单位与至少一个建议边界",
            code="prompt_profile_generation_validators_inconsistent",
        )
    protected_token_policies = selected & {
        PromptProfileFieldRule.GenerationValidator.PROTECTED_TOKENS_PRESERVE_COUNT,
        PromptProfileFieldRule.GenerationValidator.PROTECTED_TOKENS_AT_LEAST_ONCE,
    }
    if len(protected_token_policies) > 1:
        raise PromptProfileError(
            "protected_tokens_preserve_count 与 protected_tokens_at_least_once "
            "不能同时启用",
            code="prompt_profile_generation_validators_inconsistent",
        )


def _generation_value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, dict)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if type(value) is bool:
        return "true" if value else "false"
    return str(value).strip()


def _generation_check_rule(rule: PromptProfileFieldRule) -> dict[str, Any]:
    return {
        "generation_required": rule.generation_required,
        "generation_validator_keys": list(rule.generation_validator_keys or []),
        "length_unit": str(rule.length_unit or ""),
        "generation_min": rule.generation_min, "generation_max": rule.generation_max,
    }


def prompt_profile_generation_length_audit(
    rule: PromptProfileFieldRule, value: Any,
) -> dict[str, Any]:
    from studio.services.prompt_normalization_contract import prompt_field_length_audit

    contract = _generation_check_rule(rule)
    return {
        "field_key": rule.field_definition.key, "generation_mode": rule.generation_mode,
        "length_unit": contract["length_unit"], "generation_min": rule.generation_min,
        "generation_max": rule.generation_max,
        **prompt_field_length_audit(contract, _generation_value_text(value)),
    }


def validate_prompt_profile_generation_value(
    rule: PromptProfileFieldRule, value: Any,
) -> list[dict[str, Any]]:
    """Use the same generic execution checks for every strategy and field mode."""
    from studio.services.prompt_normalization_contract import (
        PromptNormalizationContractError, validate_prompt_field_text,
    )

    validate_generation_field_contract(rule)
    field_key = rule.field_definition.key
    try:
        return validate_prompt_field_text(
            rule=_generation_check_rule(rule), field_key=field_key,
            text="" if value is None or value == [] else _generation_value_text(value), request_id=f"字段 {field_key}",
        )
    except PromptNormalizationContractError as exc:
        code = ("prompt_profile_generation_value_required" if exc.reason_code == "empty"
                else "prompt_profile_generation_value_validator_failed")
        raise PromptProfileError(str(exc), code=code) from exc


def _validate_direct_source_projection(
    profile: PromptProfileVersion,
    *,
    rules: list[PromptProfileFieldRule],
) -> None:
    adapter_owned_selectors = sorted({
        rule.source_selector
        for rule in rules
        if rule.source_selector in (
            CLASSIFIED_REFERENCE_CONTEXT_OUTPUT_SELECTORS
            | {PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT}
        )
    })
    if adapter_owned_selectors:
        raise PromptProfileError(
            "direct_v1 不能读取受控来源投影器专属 selector；请显式选择 "
            f"{DECLARATIVE_SOURCE_PROJECTION_KEY}: "
            + ", ".join(adapter_owned_selectors),
            code="prompt_profile_source_projection_selector_invalid",
        )
    if any(
        rule.generation_output_semantics
        == PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        for rule in rules
    ):
        raise PromptProfileError(
            "direct_v1 不接受 contribution；请改为 final 或选择支持贡献合成的受控来源投影器",
            code="prompt_profile_source_projection_semantics_invalid",
        )

    configured_primitives = sorted({
        str(rule.projection_primitive or "")
        for rule in rules
        if str(rule.projection_primitive or "")
    })
    if configured_primitives:
        raise PromptProfileError(
            "direct_v1 字段规则不能声明 projection_primitive: "
            + ", ".join(configured_primitives),
            code="prompt_profile_projection_primitive_incompatible",
        )


def _validate_declarative_source_projection(
    profile: PromptProfileVersion,
    *,
    rules: list[PromptProfileFieldRule],
) -> None:
    config = normalize_prompt_profile_source_projection_config(
        profile.source_projection_config,
    )
    if config.get("schema_version") != 5 or "primitive_options" not in config:
        raise PromptProfileError(
            "此策略缺少当前参考关系合同；请在草稿中配置 schema v5 用途规则与渲染模板并复核发布",
            code="prompt_profile_source_projection_config_incompatible",
        )
    from studio.services.reference_rendering_inputs import validate_unit_text_bindings
    from studio.services.prompt_profile_structures import profile_input_structure
    rendering = config.get("primitive_options", {}).get(REFERENCE_CONTEXT_ENGINE_KEY, {}).get("rendering")
    if rendering is not None:
        validate_unit_text_bindings(rendering.get("unit_text_bindings"),
            structure=profile_input_structure(profile), check_structure=True)
    projection_rules = [
        rule for rule in rules
        if rule.source_selector
        == PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
    ]
    if not projection_rules:
        raise PromptProfileError(
            "declarative_v1 至少需要一条 projection.output 字段规则",
            code="prompt_profile_source_projection_selector_missing",
        )
    invalid_non_projection = sorted({
        rule.field_definition.key
        for rule in rules
        if (
            rule.source_selector
            != PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
            and str(rule.projection_primitive or "")
        )
    })
    if invalid_non_projection:
        raise PromptProfileError(
            "只有 projection.output 字段规则可以选择投影原语: "
            + ", ".join(invalid_non_projection),
            code="prompt_profile_projection_primitive_incompatible",
        )
    unknown_primitives = sorted({
        str(rule.projection_primitive or "")
        for rule in projection_rules
        if str(rule.projection_primitive or "") not in SOURCE_PROJECTION_PRIMITIVES
    })
    if unknown_primitives:
        raise PromptProfileError(
            "字段规则包含未注册 projection_primitive: "
            + ", ".join(value or "(empty)" for value in unknown_primitives),
            code="prompt_profile_projection_primitive_unknown",
        )
    unsupported_contributions: list[str] = []
    for rule in projection_rules:
        if (
            rule.generation_output_semantics
            != PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        ):
            continue
        primitive = SOURCE_PROJECTION_PRIMITIVES[rule.projection_primitive]
        source = str(rule.generation_source_selector or "")
        if not primitive.contribution_source_selectors or source != "request.structured_input":
            unsupported_contributions.append(
                f"{rule.field_definition.key}:{source or '(empty)'}",
            )
    if unsupported_contributions:
        raise PromptProfileError(
            "projection_primitive 不支持这些 contribution: "
            + ", ".join(sorted(unsupported_contributions)),
            code="prompt_profile_projection_contribution_unsupported",
        )
    configured_engines = set(config["primitive_options"])
    used_engines = {
        SOURCE_PROJECTION_PRIMITIVES[rule.projection_primitive].engine_key
        for rule in projection_rules
    }
    if configured_engines != used_engines:
        raise PromptProfileError(
            "primitive_options 必须与字段规则使用的原语引擎精确一致",
            code="prompt_profile_projection_primitive_options_mismatch",
        )


_SOURCE_PROJECTION_PROFILE_VALIDATORS = {
    DIRECT_SOURCE_PROJECTION_KEY: _validate_direct_source_projection,
    DECLARATIVE_SOURCE_PROJECTION_KEY: _validate_declarative_source_projection,
}
RUNTIME_SOURCE_PROJECTION_KEYS = tuple(_SOURCE_PROJECTION_PROFILE_VALIDATORS)
SUPPORTED_SOURCE_PROJECTION_KEYS = (
    DIRECT_SOURCE_PROJECTION_KEY,
    DECLARATIVE_SOURCE_PROJECTION_KEY,
)


def validate_prompt_profile_source_projection(
    profile: PromptProfileVersion,
    *,
    rules: list[PromptProfileFieldRule] | None = None,
) -> None:
    projection_key = str(profile.source_projection_key or "").strip()
    projection_config = normalize_prompt_profile_source_projection_config(
        profile.source_projection_config,
    )
    validator = _SOURCE_PROJECTION_PROFILE_VALIDATORS.get(projection_key)
    if validator is None:
        raise PromptProfileError(
            f"PromptProfile source_projection_key 未注册: {projection_key or '(empty)'}",
            code="prompt_profile_source_projection_unregistered",
        )
    candidate_rules = list(
        rules
        if rules is not None
        else profile.field_rules.select_related("field_definition", "field_definition_version__field_definition", "source_version").all()
    )
    if projection_config:
        if "quality_advisories" in projection_config and (
            projection_key != DECLARATIVE_SOURCE_PROJECTION_KEY
            or profile.operation != "video.generate"
        ):
            raise PromptProfileError(
                "quality_advisories 当前只适用于参考上下文投影的 "
                "video.generate PromptProfile",
                code="prompt_profile_source_projection_config_incompatible",
            )
        configured_policy = next(
            key for key in (
                "primitive_options",
                "classified_reference_context_policy",
                "static_reference_policy",
            )
            if key in projection_config
        )
        if configured_policy == "primitive_options":
            if projection_key != DECLARATIVE_SOURCE_PROJECTION_KEY:
                raise PromptProfileError(
                    "primitive_options 只允许由 declarative_v1 确定性执行",
                    code="prompt_profile_source_projection_config_incompatible",
                )
            validator(profile, rules=candidate_rules)
            return
        if configured_policy != "static_reference_policy" or projection_key != DIRECT_SOURCE_PROJECTION_KEY:
            raise PromptProfileError(
                "当前执行只支持已配置的 declarative_v1 或 direct_v1；历史投影只供查看",
                code="prompt_profile_source_projection_config_incompatible",
            )
        if (
            configured_policy == "static_reference_policy"
            and profile.operation != "image.generate"
        ):
            raise PromptProfileError(
                "static_reference_policy 只适用于 image.generate PromptProfile",
                code="prompt_profile_source_projection_config_incompatible",
            )
        selected_sources = {
            rule.source_selector
            for rule in candidate_rules
            if rule.source_selector
            != PromptProfileFieldRule.SourceSelector.FIXED_VALUE
        }
        clause_policy = projection_config["static_reference_policy"].get(
            "initial_snapshot_clause_policy",
        )
        if clause_policy is not None:
            continuity_selector = clause_policy["continuity_source_selector"]
            if continuity_selector in selected_sources:
                raise PromptProfileError(
                    "static_reference_policy 的 continuity_source_selector "
                    "只能保留连续性/动作事实，不得被静态图像 Profile 规则消费: "
                    + continuity_selector,
                    code=(
                        "prompt_profile_source_projection_config_"
                        "continuity_selector_consumed"
                    ),
                )
        configured_context_sources = set(
            projection_config["static_reference_policy"][
                "context_source_selectors"
            ],
        )
        unknown_context_sources = sorted(
            configured_context_sources - selected_sources,
        )
        if unknown_context_sources:
            raise PromptProfileError(
                "static_reference_policy.context_source_selectors 未被该 Profile 规则使用: "
                + ", ".join(unknown_context_sources),
                code="prompt_profile_source_projection_config_selector_unused",
            )
        configured_negative_sources = set(
            projection_config["static_reference_policy"][
                "negative_token_exclusions"
            ],
        )
        negative_sources = {
            rule.source_selector
            for rule in candidate_rules
            if rule.channel == PromptProfileFieldRule.Channel.NEGATIVE_PROMPT
        }
        invalid_negative_sources = sorted(
            configured_negative_sources - negative_sources,
        )
        if invalid_negative_sources:
            raise PromptProfileError(
                "static_reference_policy.negative_token_exclusions 只能引用该 Profile 的 negative_prompt 来源: "
                + ", ".join(invalid_negative_sources),
                code="prompt_profile_source_projection_config_selector_invalid",
            )
    validator(profile, rules=candidate_rules)


def prompt_profile_contract_payload(
    profile: PromptProfileVersion,
    *,
    schema_version: int | None = None,
    rules: list[PromptProfileFieldRule] | None = None,
) -> dict[str, Any]:
    resolved_schema_version = int(
        schema_version
        if schema_version is not None
        else profile.contract_schema_version
    )
    if (
        resolved_schema_version != PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
        or profile.contract_schema_version != PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
    ):
        raise PromptProfileError(
            "PromptProfile 只支持当前合同 schema "
            f"v{PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION}；不自动转换旧版本",
            code="prompt_profile_contract_schema_unsupported",
        )
    from studio.services.prompt_profile_structures import profile_input_structure, validate_structured_rule
    structure = profile_input_structure(profile)
    rule_payloads = []
    if rules is not None:
        candidate_rules = sorted(
            list(rules),
            key=lambda item: (item.position, str(item.pk or "")),
        )
    elif profile.pk:
        candidate_rules = list(
            profile.field_rules.select_related("field_definition", "field_definition_version__field_definition", "source_version").order_by(
                "position", "id",
            )
        )
    else:
        candidate_rules = []
    for rule in candidate_rules:
        validate_structured_rule(rule, snapshot=structure)
        definition = profile_rule_definition(rule)
        rule_payload = {
            "field_key": definition.key,
            "field_value_type": definition.value_type,
            "field_definition": field_definition_snapshot(definition),
            "field_value_options": definition.value_options,
            "fixed_value": rule.fixed_value,
            "has_default": rule.has_default,
            "default_value": rule.default_value,
            "source_selector": rule.source_selector,
            "channel": rule.channel,
            "position": rule.position,
            "label_style": rule.label_style,
            "separator": rule.separator,
            "required": rule.required,
            "omit_if_empty": rule.omit_if_empty,
            "max_chars": rule.max_chars,
        }
        rule_payload.update({
            "generation_mode": rule.generation_mode,
            "generation_source_selector": rule.generation_source_selector,
            "generation_instruction": rule.generation_instruction,
            "generation_output_semantics": rule.generation_output_semantics,
            "generation_required": rule.generation_required,
            "language_policy": rule.language_policy,
            "length_unit": rule.length_unit,
            "generation_min": rule.generation_min,
            "generation_max": rule.generation_max,
            "generation_validator_keys": rule.generation_validator_keys,
            "generation_context_selectors": list(rule.generation_context_selectors),
            "projection_primitive": str(rule.projection_primitive or ""),
        })
        if rule.source_version_id:
            from studio.services.content_sources import source_reference, validate_rule_source
            validate_rule_source(rule)
            rule_payload["source_definition"] = source_reference(rule.source_version)
        if rule.source_path is not None:
            rule_payload["source_path"] = deepcopy(rule.source_path)
        rule_payloads.append(rule_payload)
    payload = {
        "schema_version": resolved_schema_version,
        "profile_key": profile.profile_key,
        "version": profile.version,
        "model_family": profile.model_family,
        "operation": profile.operation,
        "mode": profile.mode,
        "rules": rule_payloads,
    }
    payload["source_projection_key"] = profile.source_projection_key
    if structure is not None:
        payload["input_structure"] = structure
    payload["channel_limits"] = normalize_prompt_profile_channel_limits(
        profile.channel_limits,
    )
    payload["source_projection_config"] = normalize_prompt_profile_source_projection_config(
        profile.source_projection_config,
    )
    return payload


def refresh_prompt_profile_contract(
    profile: PromptProfileVersion,
    *,
    schema_version: int | None = None,
) -> PromptProfileVersion:
    if profile.status != PromptProfileVersion.Status.DRAFT:
        raise PromptProfileError("只有 draft PromptProfile 可以刷新合同")
    resolved_schema_version = int(
        schema_version
        if schema_version is not None
        else profile.contract_schema_version
    )
    contract = json_contract(
        prompt_profile_contract_payload(
            profile,
            schema_version=resolved_schema_version,
        ),
        schema_version=resolved_schema_version,
        max_bytes=_prompt_profile_contract_max_bytes(),
    )
    profile.contract_schema_version = contract.schema_version
    profile.contract_size_bytes = contract.size_bytes
    profile.contract_sha256 = contract.sha256
    profile.save(update_fields=(
        "contract_schema_version", "contract_size_bytes", "contract_sha256", "updated_at",
    ))
    return profile


def clone_prompt_profile_successor(
    source: PromptProfileVersion,
    *,
    expected_source_contract_sha256: str,
    name: str,
) -> PromptProfileVersion:
    """Clone one exact immutable Profile and all rules into a new draft version."""

    expected_hash = str(expected_source_contract_sha256 or "").strip()
    successor_name = str(name or "").strip()
    if not expected_hash or expected_hash != str(source.contract_sha256 or ""):
        raise PromptProfileError(
            "来源 PromptProfile 合同已变化；请刷新并重新确认版本与 SHA-256",
            code="prompt_profile_clone_source_stale",
        )
    if source.status not in {
        PromptProfileVersion.Status.PUBLISHED,
        PromptProfileVersion.Status.RETIRED,
    }:
        raise PromptProfileError(
            "只能从 published/retired 的不可变 PromptProfile 创建后继版本",
            code="prompt_profile_clone_source_mutable",
        )
    if source.source_projection_key not in SUPPORTED_SOURCE_PROJECTION_KEYS:
        raise PromptProfileError(
            "该历史来源投影器仅供历史查看，不能再创建新版本；请选择已配置模板的声明式后继 Profile",
            code="prompt_profile_clone_source_legacy",
        )
    if not successor_name or len(successor_name) > 160:
        raise PromptProfileError(
            "后继版本名称必须为 1–160 个字符",
            code="prompt_profile_clone_name_invalid",
        )

    with transaction.atomic():
        family = list(
            PromptProfileVersion.objects.select_for_update()
            .filter(profile_key=source.profile_key)
            .order_by("version", "pk")
        )
        locked_source = next(
            (item for item in family if item.pk == source.pk),
            None,
        )
        if locked_source is None:
            raise PromptProfileError(
                "来源 PromptProfile 已不存在；未创建草稿",
                code="prompt_profile_clone_source_stale",
            )
        if (
            locked_source.contract_sha256 != expected_hash
            or locked_source.status not in {
                PromptProfileVersion.Status.PUBLISHED,
                PromptProfileVersion.Status.RETIRED,
            }
        ):
            raise PromptProfileError(
                "来源 PromptProfile 在确认后发生变化；未创建草稿",
                code="prompt_profile_clone_source_stale",
            )
        latest = family[-1]
        if latest.pk != locked_source.pk:
            if latest.status == PromptProfileVersion.Status.DRAFT:
                raise PromptProfileError(
                    f"该 Profile 已存在 v{latest.version} 草稿；请继续编辑现有草稿",
                    code="prompt_profile_clone_draft_exists",
                )
            raise PromptProfileError(
                f"来源不是最新版本；请从 v{latest.version} 创建后继草稿",
                code="prompt_profile_clone_source_not_latest",
            )
        try:
            verify_prompt_profile_contract(locked_source)
        except ValidationError as exc:
            raise PromptProfileError(
                "来源 PromptProfile 正文与持久化摘要不一致；请先修复合同",
                code="prompt_profile_clone_source_contract_invalid",
            ) from exc
        next_version = latest.version + 1
        target = PromptProfileVersion.objects.create(
            profile_key=locked_source.profile_key,
            version=next_version,
            name=successor_name,
            description=locked_source.description,
            model_family=locked_source.model_family,
            operation=locked_source.operation,
            mode=locked_source.mode,
            source_projection_key=locked_source.source_projection_key,
            input_structure_version_id=locked_source.input_structure_version_id,
            source_projection_config=deepcopy(
                locked_source.source_projection_config,
            ),
            official_reference=locked_source.official_reference,
            channel_limits=deepcopy(locked_source.channel_limits),
            status=PromptProfileVersion.Status.DRAFT,
            review_state=PromptProfileVersion.ReviewState.NEEDS_REVIEW,
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            contract_size_bytes=2,
            contract_sha256="",
        )
        source_rules = list(
            locked_source.field_rules.select_related("field_definition", "field_definition_version__field_definition", "source_version").order_by(
                "position",
                "id",
            )
        )
        PromptProfileFieldRule.objects.bulk_create([
            PromptProfileFieldRule(
                profile_version=target,
                field_definition_id=rule.field_definition_id,
                field_definition_version_id=rule.field_definition_version_id,
                source_version_id=rule.source_version_id,
                source_selector=rule.source_selector,
                source_path=deepcopy(rule.source_path),
                projection_primitive=str(rule.projection_primitive or ""),
                channel=rule.channel,
                position=rule.position,
                label_style=rule.label_style,
                separator=rule.separator,
                required=rule.required,
                fixed_value=deepcopy(rule.fixed_value),
                has_default=rule.has_default,
                default_value=deepcopy(rule.default_value),
                omit_if_empty=rule.omit_if_empty,
                max_chars=rule.max_chars,
                generation_mode=rule.generation_mode,
                generation_source_selector=rule.generation_source_selector,
                generation_instruction=rule.generation_instruction,
                generation_output_semantics=rule.generation_output_semantics,
                generation_required=rule.generation_required,
                language_policy=rule.language_policy,
                length_unit=rule.length_unit,
                generation_min=rule.generation_min,
                generation_max=rule.generation_max,
                generation_validator_keys=list(rule.generation_validator_keys),
                generation_context_selectors=list(rule.generation_context_selectors),
            )
            for rule in source_rules
        ])
        target_rules = list(
            target.field_rules.select_related("field_definition", "field_definition_version__field_definition", "source_version").order_by(
                "position",
                "id",
            )
        )
        for rule in target_rules:
            # A draft can carry the exact retired dependency for replacement;
            # publishing it still requires every source to be published.
            validate_profile_rule(rule, require_published_source=False)
        normalize_prompt_profile_channel_limits(target.channel_limits)
        normalize_prompt_profile_source_projection_config(
            target.source_projection_config,
        )
        validate_prompt_profile_source_projection(target, rules=target_rules)
        refresh_prompt_profile_contract(
            target,
            schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        return target


def verify_prompt_profile_contract(profile: PromptProfileVersion) -> dict[str, Any]:
    payload = prompt_profile_contract_payload(
        profile,
        schema_version=profile.contract_schema_version,
    )
    return verify_json_contract(
        payload,
        schema_version=profile.contract_schema_version,
        size_bytes=profile.contract_size_bytes,
        sha256=profile.contract_sha256,
        max_bytes=_prompt_profile_contract_max_bytes(),
    ).value


def validate_prompt_profile_for_publish(profile: PromptProfileVersion) -> None:
    if profile.status != PromptProfileVersion.Status.DRAFT:
        raise PromptProfileError("只有 draft PromptProfile 可以发布")
    if profile.review_state != PromptProfileVersion.ReviewState.READY:
        raise PromptProfileError("PromptProfile 仍为 needs_review，必须先完成审核")
    if profile.source_projection_key not in SUPPORTED_SOURCE_PROJECTION_KEYS:
        raise PromptProfileError(
            "该来源投影器仅供历史查看，不能发布新的 PromptProfile",
            code="prompt_profile_source_projection_runtime_only",
        )
    rules = list(profile.field_rules.select_related("field_definition", "field_definition_version__field_definition", "source_version").order_by("position"))
    if not rules:
        raise PromptProfileError("PromptProfile 至少需要一个字段规则")
    runtime_only_selectors = sorted({
        rule.source_selector
        for rule in rules
        if rule.source_selector in CLASSIFIED_REFERENCE_CONTEXT_OUTPUT_SELECTORS
    })
    if runtime_only_selectors:
        raise PromptProfileError(
            "这些来源 selector 仅供历史查看，不能发布新合同: "
            + ", ".join(runtime_only_selectors),
            code="prompt_profile_source_selector_runtime_only",
        )
    for rule in rules:
        validate_profile_rule(rule)
    normalize_prompt_profile_channel_limits(profile.channel_limits)
    normalize_prompt_profile_source_projection_config(
        profile.source_projection_config,
    )
    validate_prompt_profile_source_projection(profile, rules=rules)
    validate_prompt_operation_publish_evidence(profile)
    if profile.operation in {"video.generate", "image.generate"}:
        if any(
            rule.channel == PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION
            for rule in rules
        ):
            raise PromptProfileError("媒体生成策略不支持 system_instruction channel")
        if not any(
            rule.channel == PromptProfileFieldRule.Channel.POSITIVE_PROMPT
            for rule in rules
        ):
            raise PromptProfileError(
                "媒体生成策略必须至少装配一个 positive_prompt 字段；negative_prompt 不能替代正向输入"
            )
    refresh_prompt_profile_contract(profile)
    verify_prompt_profile_contract(profile)


@transaction.atomic
def publish_prompt_profile(profile: PromptProfileVersion, *, actor) -> PromptProfileVersion:
    locked = PromptProfileVersion.objects.select_for_update().get(pk=profile.pk)
    validate_prompt_profile_for_publish(locked)
    locked.status = PromptProfileVersion.Status.PUBLISHED
    locked.published_at = timezone.now()
    locked.published_by = actor
    locked.save(update_fields=("status", "published_at", "published_by", "updated_at"))
    return locked


@transaction.atomic
def retire_prompt_profile(profile: PromptProfileVersion) -> PromptProfileVersion:
    locked = PromptProfileVersion.objects.select_for_update().get(pk=profile.pk)
    if locked.status != PromptProfileVersion.Status.PUBLISHED:
        raise PromptProfileError("只有 published PromptProfile 可以退役")
    if locked.execution_versions.filter(status="published", instance__is_active=True).exists():
        raise PromptProfileError(
            "仍有启用且已发布的执行实例版本引用该策略，请先调整这些执行实例",
            code="prompt_profile_in_use",
        )
    locked.status = PromptProfileVersion.Status.RETIRED
    locked.save(update_fields=("status", "updated_at"))
    return locked




def _profile_rule_has_nonempty_default(rule: PromptProfileFieldRule) -> bool:
    value = (rule.fixed_value if rule.source_selector == PromptProfileFieldRule.SourceSelector.FIXED_VALUE
             else rule.default_value if rule.has_default else None)
    return value is not None and value != "" and value != []


def validate_bible_image_prompt_profile(profile: PromptProfileVersion) -> None:
    """Prove that one shared image profile is executable for all Bible types.

    Bible candidate generation has no Shot input and character/scene/prop expose
    different optional facts.  A route must therefore be rejected before a Run
    is created when any required selector can be empty for one of those entity
    types, or when every positive rule could be omitted at runtime.
    """

    if profile.operation != "image.generate" or profile.mode:
        raise PromptProfileResolutionError(
            "Bible 参考图只接受 image.generate 默认 mode PromptProfile",
            code="bible_image_prompt_profile_incompatible",
        )
    if profile.status in {
        PromptProfileVersion.Status.PUBLISHED,
        PromptProfileVersion.Status.RETIRED,
    }:
        verify_prompt_profile_contract(profile)
    rules = list(
        profile.field_rules.select_related("field_definition", "field_definition_version__field_definition", "source_version").order_by("position", "id")
    )
    unavailable_required: list[str] = []
    for rule in rules:
        required = rule.required
        if not required or _profile_rule_has_nonempty_default(rule):
            continue
        if rule.source_selector not in _BIBLE_ALWAYS_AVAILABLE_SELECTORS:
            unavailable_required.append(rule.source_selector)
    if unavailable_required:
        selectors = ", ".join(sorted(set(unavailable_required)))
        raise PromptProfileResolutionError(
            "Bible 参考图 PromptProfile 的必填来源无法同时由 "
            f"{'/'.join(_BIBLE_ENTITY_TYPES)} 提供: {selectors}",
            code="bible_image_prompt_profile_required_source_unavailable",
        )

    positive_rules = [
        rule for rule in rules
        if rule.channel == PromptProfileFieldRule.Channel.POSITIVE_PROMPT
    ]
    has_stable_positive = any(
        _profile_rule_has_nonempty_default(rule)
        or rule.source_selector in _BIBLE_POSITIVE_ANCHOR_SELECTORS
        for rule in positive_rules
    )
    if not has_stable_positive:
        raise PromptProfileResolutionError(
            "Bible 参考图 PromptProfile 对 character/scene/prop 均缺少恒定非空的正向来源；"
            "请至少装配 request.theme、request.subject、request.composition 或非空默认值",
            code="bible_image_prompt_profile_positive_source_unavailable",
        )


def _typed_value(definition: FieldDefinitionVersion, value: Any) -> Any:
    value_type = definition.value_type
    if value_type in {
        FieldDefinitionVersion.ValueType.TEXT,
        FieldDefinitionVersion.ValueType.LONG_TEXT,
    }:
        if not isinstance(value, str):
            raise PromptProfileError(f"{definition.key} 必须是文本")
        if len(value) > _field_text_max_characters():
            raise PromptProfileError(
                f"{definition.key} 超过 {_field_text_max_characters()} 字符",
            )
        # Type validation must not rewrite source or adopted text. Formatting
        # belongs to explicit projection/fragment rules; limits count the exact
        # submitted characters, including whitespace.
        return value
    if value_type == FieldDefinitionVersion.ValueType.LIST:
        if not isinstance(value, list) or len(value) > _field_list_max_items() or any(
            not isinstance(item, str)
            or len(item) > _field_list_item_max_characters()
            for item in value
        ):
            raise PromptProfileError(
                f"{definition.key} 必须是不超过 {_field_list_max_items()} 项、"
                f"单项不超过 {_field_list_item_max_characters()} 字符的文本数组",
            )
        return [item.strip() for item in value if item.strip()]
    if value_type == FieldDefinitionVersion.ValueType.BOOLEAN:
        if type(value) is not bool:
            raise PromptProfileError(f"{definition.key} 必须是布尔值")
        return value
    if value_type in {FieldDefinitionVersion.ValueType.INTEGER, FieldDefinitionVersion.ValueType.DECIMAL}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PromptProfileError(f"{definition.key} 必须是数值")
        if value_type == FieldDefinitionVersion.ValueType.INTEGER and type(value) is not int:
            raise PromptProfileError(f"{definition.key} 必须是整数")
        absolute_max = _field_numeric_absolute_max()
        if not -absolute_max <= float(value) <= absolute_max:
            raise PromptProfileError(f"{definition.key} 数值超出安全范围")
        return value
    if value_type == FieldDefinitionVersion.ValueType.ENUM:
        if not isinstance(value, str) or value not in definition.value_options:
            raise PromptProfileError(f"{definition.key} 不在允许选项中")
        return value
    raise PromptProfileError(f"{definition.key} value_type 不受支持")


def _text_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def _render_fragment(rule: PromptProfileFieldRule, value: Any) -> str:
    text = _text_value(value)
    # Model-facing output must be derived only from the immutable PromptProfile
    # contract. ``label`` is localized control-plane copy and is intentionally
    # excluded from that contract, while ``key`` is the frozen field identity.
    label = rule.field_definition.key
    if rule.label_style == PromptProfileFieldRule.LabelStyle.NONE:
        return text
    if rule.label_style == PromptProfileFieldRule.LabelStyle.LABEL_BLOCK:
        return f"{label}:\n{text}"
    if rule.label_style == PromptProfileFieldRule.LabelStyle.BRACKETED:
        return f"[{label}] {text}"
    return f"{label}: {text}"


def _static_reference_snapshot_text(
    value: str,
    *,
    snapshot_selector: str,
    transition_markers: list[str],
    transition_patterns: list[dict[str, str]],
) -> tuple[str, bool]:
    if snapshot_selector == "literal" or not value:
        return value, False
    folded = value.casefold()
    structured_matches: list[tuple[int, int, int, int]] = []
    for pattern in transition_patterns:
        from_marker = pattern["from_marker"]
        to_marker = pattern["to_marker"]
        from_start = folded.find(from_marker.casefold())
        if from_start < 0:
            continue
        initial_start = from_start + len(from_marker)
        to_start = folded.find(to_marker.casefold(), initial_start)
        if to_start < 0 or to_start == initial_start:
            raise PromptProfileError(
                "静态参考 from_to 状态短语不完整；"
                "请在上游有限重试或人工审核中明确单一静态状态。",
                code="static_reference_semantic_validation_failed",
            )
        structured_matches.append((
            from_start,
            initial_start,
            to_start,
            len(to_marker),
        ))
    if structured_matches:
        from_start, initial_start, to_start, to_length = min(
            structured_matches,
            key=lambda item: item[0],
        )
        selected_state = (
            value[initial_start:to_start]
            if snapshot_selector == "initial"
            else value[to_start + to_length:]
        ).strip(" \t\r\n,;，；。")
        projected = (
            value[:from_start].rstrip(" \t\r\n,;，；。")
            + " "
            + selected_state
        ).strip()
        if not selected_state or not projected:
            raise PromptProfileError(
                "静态参考 from_to 状态选择后产生空字段；"
                "请在上游有限重试或人工审核中明确单一静态状态。",
                code="static_reference_semantic_validation_failed",
            )
        return projected, projected != value
    matches: list[tuple[int, int]] = []
    for marker in transition_markers:
        marker_folded = marker.casefold()
        start = (
            folded.find(marker_folded)
            if snapshot_selector == "initial"
            else folded.rfind(marker_folded)
        )
        if start >= 0:
            matches.append((start, len(marker)))
    if not matches:
        return value, False
    if snapshot_selector == "initial":
        start, _marker_length = min(matches, key=lambda item: item[0])
        projected = value[:start].strip(" \t\r\n,;，；。")
    else:
        start, marker_length = max(matches, key=lambda item: item[0])
        projected = value[start + marker_length:].strip(" \t\r\n,;，；。")
    if not projected:
        raise PromptProfileError(
            "静态参考 snapshot_selector 裁剪后产生空字段；"
            "请在上游有限重试或人工审核中明确单一静态状态。",
            code="static_reference_semantic_validation_failed",
        )
    return projected, projected != value


def _discard_static_reference_context_clauses(
    value: str,
    *,
    delimiters: list[str],
    markers: list[str],
) -> tuple[str, list[str]]:
    if not value or not markers:
        return value, []
    clauses = re.split(
        "|".join(re.escape(delimiter) for delimiter in delimiters),
        value,
    )
    kept: list[str] = []
    removed: list[str] = []
    for clause in clauses:
        stripped = clause.strip()
        if not stripped:
            continue
        folded = stripped.casefold()
        matched = next(
            (
                marker
                for marker in markers
                if _contains_static_reference_marker(folded, marker)
            ),
            None,
        )
        if matched is not None:
            removed.append(matched.strip())
            continue
        kept.append(stripped)
    if not removed:
        return value, []
    if not kept:
        raise PromptProfileError(
            "静态参考剔除跨实体关系分句后没有可用主体事实；"
            "请在上游有限重试或人工审核中补充独立实体描述。",
            code="static_reference_semantic_validation_failed",
        )
    return "; ".join(kept), removed


def _contains_static_reference_marker(value: str, marker: str) -> bool:
    candidate = value.casefold()
    token = marker.strip().casefold()
    if any(ord(character) > 127 for character in token):
        return token in candidate
    return f" {token} " in f" {candidate} "


def _static_reference_initial_clause_snapshot(
    value: str,
    *,
    policy: dict[str, Any],
) -> tuple[str, list[str], str | None]:
    """Select a configured initial clause without domain-specific vocabulary."""

    if not value:
        return value, [], None
    delimiters = sorted(policy["delimiters"], key=len, reverse=True)
    clauses = [
        item.strip()
        for item in re.split(
            "|".join(re.escape(delimiter) for delimiter in delimiters),
            value,
        )
        if item.strip()
    ]
    if len(clauses) < 2:
        return value, [], None
    for index, clause in enumerate(clauses):
        marker = next(
            (
                candidate
                for candidate in policy["initial_state_markers"]
                if _contains_static_reference_marker(clause, candidate)
            ),
            None,
        )
        if marker is None or index >= len(clauses) - 1:
            continue
        static_value = "; ".join(clauses[: index + 1]).strip()
        continuity_facts = clauses[index + 1 :]
        if not static_value or not continuity_facts:
            raise PromptProfileError(
                "静态参考初态分句选择后没有产生完整事实；"
                "请在上游有限重试或人工审核中修正。",
                code="static_reference_semantic_validation_failed",
            )
        return static_value, continuity_facts, marker
    return value, [], None


def _append_static_reference_continuity_facts(
    sources: dict[str, Any],
    *,
    selector: str,
    facts: list[str],
) -> None:
    if not facts:
        return
    current = sources.get(selector)
    if current in (None, "", []):
        values: list[str] = []
    elif isinstance(current, str):
        values = [current]
    elif isinstance(current, list) and all(isinstance(item, str) for item in current):
        values = list(current)
    else:
        raise PromptProfileError(
            "静态参考 continuity_source_selector 必须是文本或文本数组",
            code="static_reference_semantic_validation_failed",
        )
    values.extend(facts)
    sources[selector] = "; ".join(dict.fromkeys(
        value.strip() for value in values if value.strip()
    ))


def _exclude_static_reference_negative_tokens(
    value: str,
    *,
    excluded_tokens: list[str],
) -> tuple[str, list[str]]:
    if not value or not excluded_tokens:
        return value, []
    exclusions = {item.strip().casefold(): item for item in excluded_tokens}
    tokens = [
        item.strip()
        for item in re.split(r"[,;，；\n]+", value)
        if item.strip()
    ]
    kept: list[str] = []
    removed: list[str] = []
    for token in tokens:
        configured = exclusions.get(token.casefold())
        if configured is None:
            kept.append(token)
        else:
            removed.append(configured)
    return ", ".join(kept), removed


def _project_static_reference_sources(
    profile: PromptProfileVersion,
    *,
    sources: dict[str, Any],
    projection_context: dict[str, Any] | None,
    rules: list[PromptProfileFieldRule] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = normalize_prompt_profile_source_projection_config(
        profile.source_projection_config,
    )
    if not config or "static_reference_policy" not in config:
        return dict(sources), {}
    policy = config["static_reference_policy"]
    context = projection_context or {}
    if not isinstance(context, dict) or set(context) - {"purpose", "target_kind"}:
        raise PromptProfileError(
            "projection_context 只能包含 purpose 与 target_kind",
            code="prompt_profile_projection_context_invalid",
        )
    purpose = str(context.get("purpose") or "static_reference").strip()
    if purpose != "static_reference":
        raise PromptProfileError(
            "static_reference_policy 只能用于 static_reference projection_context",
            code="prompt_profile_projection_context_invalid",
        )
    target_kind = str(
        context.get("target_kind") or policy["default_target_kind"],
    ).strip()
    if _STATIC_REFERENCE_TARGET_KIND_RE.fullmatch(target_kind) is None:
        raise PromptProfileError(
            "projection_context.target_kind 格式无效",
            code="prompt_profile_projection_context_invalid",
        )
    allow_context_contract = policy["allow_context"]
    context_allowed = bool(
        allow_context_contract["target_overrides"].get(
            target_kind,
            allow_context_contract["default"],
        ),
    )
    projected = dict(sources)
    dropped_context_selectors: list[str] = []
    if not context_allowed:
        for selector in policy["context_source_selectors"]:
            if selector not in projected:
                continue
            value = projected[selector]
            projected[selector] = [] if isinstance(value, list) else ""
            if value not in (None, "", []):
                dropped_context_selectors.append(selector)

    removed_negative_tokens: dict[str, list[str]] = {}
    for selector, exclusions in policy["negative_token_exclusions"].items():
        value = projected.get(selector)
        if not isinstance(value, str):
            continue
        next_value, removed = _exclude_static_reference_negative_tokens(
            value,
            excluded_tokens=exclusions,
        )
        projected[selector] = next_value
        if removed:
            removed_negative_tokens[selector] = removed

    candidate_rules = (
        list(rules)
        if rules is not None
        else list(profile.field_rules.all())
    )
    positive_selectors = {
        rule.source_selector
        for rule in candidate_rules
        if rule.channel == PromptProfileFieldRule.Channel.POSITIVE_PROMPT
        and rule.source_selector
        != PromptProfileFieldRule.SourceSelector.FIXED_VALUE
    }
    trimmed_selectors: list[str] = []
    removed_context_clauses: dict[str, list[str]] = {}
    initial_clause_projections: dict[str, dict[str, Any]] = {}
    continuity_facts_by_selector: dict[str, list[str]] = {}
    initial_clause_policy = policy.get("initial_snapshot_clause_policy")

    def select_initial_clauses(selector: str, value: str) -> str:
        if not initial_clause_policy:
            return value
        next_value, continuity_facts, marker = (
            _static_reference_initial_clause_snapshot(
                value,
                policy=initial_clause_policy,
            )
        )
        if not continuity_facts:
            return next_value
        continuity_selector = initial_clause_policy[
            "continuity_source_selector"
        ]
        continuity_facts_by_selector.setdefault(
            continuity_selector,
            [],
        ).extend(continuity_facts)
        audit_row = initial_clause_projections.setdefault(selector, {
            "matched_initial_markers": [],
            "relocated_clause_count": 0,
            "continuity_source_selector": continuity_selector,
            "continuity_facts": [],
        })
        if marker not in audit_row["matched_initial_markers"]:
            audit_row["matched_initial_markers"].append(marker)
        audit_row["relocated_clause_count"] += len(continuity_facts)
        audit_row["continuity_facts"].extend(continuity_facts)
        if selector not in trimmed_selectors:
            trimmed_selectors.append(selector)
        return next_value

    for selector in sorted(positive_selectors):
        value = projected.get(selector)
        if isinstance(value, str):
            if not context_allowed:
                value, removed = _discard_static_reference_context_clauses(
                    value,
                    delimiters=policy["context_clause_policy"]["delimiters"],
                    markers=policy["context_clause_policy"]["discard_markers"],
                )
                if removed:
                    removed_context_clauses[selector] = removed
            value = select_initial_clauses(selector, value)
            next_value, trimmed = _static_reference_snapshot_text(
                value,
                snapshot_selector=policy["snapshot_selector"],
                transition_markers=policy["transition_markers"],
                transition_patterns=policy["transition_patterns"],
            )
            projected[selector] = next_value
            if trimmed and selector not in trimmed_selectors:
                trimmed_selectors.append(selector)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            next_items: list[str] = []
            trimmed = False
            for item in value:
                if not context_allowed:
                    item, removed = _discard_static_reference_context_clauses(
                        item,
                        delimiters=policy["context_clause_policy"]["delimiters"],
                        markers=policy["context_clause_policy"]["discard_markers"],
                    )
                    if removed:
                        removed_context_clauses.setdefault(selector, []).extend(
                            removed,
                        )
                item = select_initial_clauses(selector, item)
                next_item, item_trimmed = _static_reference_snapshot_text(
                    item,
                    snapshot_selector=policy["snapshot_selector"],
                    transition_markers=policy["transition_markers"],
                    transition_patterns=policy["transition_patterns"],
                )
                next_items.append(next_item)
                trimmed = trimmed or item_trimmed
            projected[selector] = next_items
            if trimmed and selector not in trimmed_selectors:
                trimmed_selectors.append(selector)

    for continuity_selector, facts in continuity_facts_by_selector.items():
        _append_static_reference_continuity_facts(
            projected,
            selector=continuity_selector,
            facts=facts,
        )
    for audit_row in initial_clause_projections.values():
        facts_contract = json_contract(
            audit_row["continuity_facts"],
            max_bytes=_prompt_profile_preview_max_bytes(),
        )
        audit_row["continuity_facts_sha256"] = facts_contract.sha256

    forbidden = list(policy["forbidden_markers"]["always"])
    if not context_allowed:
        forbidden.extend(
            policy["forbidden_markers"]["when_context_disallowed"],
        )
    violations: list[str] = []
    for selector in sorted(positive_selectors):
        value = projected.get(selector)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            if not isinstance(candidate, str) or not candidate:
                continue
            folded = candidate.casefold()
            for marker in forbidden:
                if _contains_static_reference_marker(folded, marker):
                    violations.append(f"{selector}:{marker.strip()}")
    if violations:
        raise PromptProfileError(
            "静态参考语义终检未通过（" + ", ".join(sorted(set(violations))) + "）；"
            "请在上游已配置的有限重试或人工审核中修正，"
            "本确定性装配阶段不会再次调用模型或无限重试。",
            code="static_reference_semantic_validation_failed",
        )
    return projected, {
        "schema_version": 1,
        "policy": "static_reference",
        "target_kind": target_kind,
        "snapshot_selector": policy["snapshot_selector"],
        "context_allowed": context_allowed,
        "dropped_context_selectors": dropped_context_selectors,
        "trimmed_selectors": trimmed_selectors,
        "removed_context_clauses": removed_context_clauses,
        "initial_clause_projections": initial_clause_projections,
        "continuity_facts": {
            selector: projected.get(selector)
            for selector in sorted(continuity_facts_by_selector)
        },
        "removed_negative_tokens": removed_negative_tokens,
        "violation_action": policy["violation_action"],
    }


def assemble_prompt_profile(
    profile: PromptProfileVersion,
    *,
    sources: dict[str, Any],
    reviewed_field_values: dict[str, Any] | None = None,
    require_generated_values: bool = False,
    projection_context: dict[str, Any] | None = None,
    allow_draft: bool = False,
    rules: list[PromptProfileFieldRule] | None = None,
) -> dict[str, Any]:
    """Deterministically assemble allow-listed values without template eval.

    ``reviewed_field_values`` is a run-scoped Layer-2 result keyed by the
    immutable PromptProfile field identity.  It is intentionally distinct from
    ``sources``: one source fact may feed multiple independently generated
    fields, and a generated field must never overwrite the first-layer fact it
    was derived from.  Only model-processing rules may consume these values.
    """

    if profile.status not in {
        PromptProfileVersion.Status.PUBLISHED,
        PromptProfileVersion.Status.RETIRED,
    } and not (
        allow_draft and profile.status == PromptProfileVersion.Status.DRAFT
    ):
        raise PromptProfileError("运行时只能装配 published PromptProfile")
    if rules is None:
        verify_prompt_profile_contract(profile)
        candidate_rules = list(
            profile.field_rules.select_related("field_definition", "field_definition_version__field_definition", "source_version").order_by(
                "position", "id",
            )
        )
        effective_contract_sha256 = profile.contract_sha256
    else:
        candidate_rules = sorted(
            list(rules),
            key=lambda item: (item.position, str(item.pk or "")),
        )
        validate_prompt_profile_source_projection(profile, rules=candidate_rules)
        candidate_contract = json_contract(
            prompt_profile_contract_payload(
                profile,
                schema_version=profile.contract_schema_version,
                rules=candidate_rules,
            ),
            schema_version=profile.contract_schema_version,
            max_bytes=_prompt_profile_contract_max_bytes(),
        )
        effective_contract_sha256 = candidate_contract.sha256
    if not isinstance(sources, dict):
        raise PromptProfileError("sources 必须是对象")
    from studio.services.prompt_profile_structures import profile_input_structure, resolve_profile_structure, read_profile_path, STRUCTURED_INPUT_SELECTOR
    sources, structure_resolution = resolve_profile_structure(profile_input_structure(profile), sources,
                                                              max_bytes=_prompt_profile_contract_max_bytes())
    if reviewed_field_values is None:
        reviewed_field_values = {}
    if not isinstance(reviewed_field_values, dict) or any(
        not isinstance(key, str) or not key
        for key in reviewed_field_values
    ):
        raise PromptProfileError(
            "reviewed_field_values 必须是以 PromptProfile field key 为键的对象",
            code="prompt_profile_reviewed_fields_invalid",
        )
    allowed = set(PromptProfileFieldRule.SourceSelector.values)
    unknown = sorted(set(sources) - allowed)
    if unknown:
        raise PromptProfileError(f"sources 包含未授权 selector: {', '.join(unknown)}")
    sources, source_projection_audit = _project_static_reference_sources(
        profile,
        sources=sources,
        projection_context=projection_context,
        rules=candidate_rules,
    )
    channel_fragments: dict[str, list[tuple[str, str]]] = {
        choice: [] for choice in PromptProfileFieldRule.Channel.values
    }
    structured_fields: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    length_fragments: dict[str, list[dict[str, Any]]] = {key: [] for key in channel_fragments}
    configured_field_keys = {
        rule.field_definition.key for rule in candidate_rules
    }
    unknown_reviewed_fields = sorted(set(reviewed_field_values) - configured_field_keys)
    if unknown_reviewed_fields:
        raise PromptProfileError(
            "reviewed_field_values 包含不属于冻结 PromptProfile 的字段: "
            + ", ".join(unknown_reviewed_fields),
            code="prompt_profile_reviewed_field_unknown",
        )
    missing_source = object()
    for rule in candidate_rules:
        validate_profile_rule_runtime(rule)
        definition = profile_rule_definition(rule)
        defaulted = False
        reviewed = definition.key in reviewed_field_values
        if reviewed:
            if (
                rule.generation_mode not in {
                PromptProfileFieldRule.GenerationMode.MODEL_GENERATED,
                PromptProfileFieldRule.GenerationMode.TRANSLATE_ONLY,
                }
                or rule.generation_output_semantics
                != PromptProfileFieldRule.GenerationOutputSemantics.FINAL
            ):
                raise PromptProfileError(
                    f"字段 {definition.key} 不允许把已审核字段内容作为最终值覆盖",
                    code="prompt_profile_reviewed_field_not_generated",
                )
            raw = reviewed_field_values[definition.key]
        elif require_generated_values and rule.generation_mode in MODEL_GENERATION_MODES and rule.generation_output_semantics == "final":
            # Production must not mistake a shared raw input (or a rule default)
            # for a selected auxiliary result. Preview examples remain separate.
            if rule.generation_required:
                raise PromptProfileError(f"字段 {definition.key} 尚未明确采用生成结果",
                    code="prompt_profile_required_field_missing")
            raw = None
        elif rule.source_selector == PromptProfileFieldRule.SourceSelector.FIXED_VALUE:
            raw = rule.fixed_value
        elif (
            rule.source_selector
            == PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
        ):
            projected_values = sources.get(
                PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT,
            )
            if projected_values is None:
                raw = missing_source
            elif not isinstance(projected_values, dict):
                raise PromptProfileError(
                    "projection.output 必须是以冻结字段 key 为键的对象",
                    code="prompt_profile_projection_output_invalid",
                )
            else:
                raw = projected_values.get(definition.key, missing_source)
        else:
            raw = sources.get(rule.source_selector, missing_source)
            if rule.source_selector == STRUCTURED_INPUT_SELECTOR and raw is not missing_source:
                raw, _, present = read_profile_path(raw, rule.source_path, definition.key)
                if not present:
                    raw = missing_source
        if raw is missing_source:
            defaulted = rule.has_default
            raw = rule.default_value if rule.has_default else None
        missing = raw is None or raw == "" or raw == []
        required = rule.required
        if missing:
            if required:
                raise PromptProfileError(
                    f"缺少必填提示词字段 {definition.key}",
                    code="prompt_profile_required_field_missing",
                )
            if rule.omit_if_empty:
                continue
            raw = ""
        value = _typed_value(definition, raw)
        fragment = _render_fragment(rule, value)
        if len(fragment) > rule.max_chars:
            raise PromptProfileError(
                f"{definition.key} 装配后超过 {rule.max_chars} 字符",
                code="prompt_profile_field_too_long",
            )
        separator = _SEPARATORS[rule.separator]
        length_fragments[rule.channel].append({"field_key": definition.key, "label": definition.label,
            "field_definition_version_id": str(rule.field_definition_version_id),
            "source_selector": rule.source_selector, "max_chars": rule.max_chars,
            "text_chars": len(fragment), "separator_chars": 0})
        channel_fragments[rule.channel].append((separator, fragment))
        structured_fields[definition.key] = value
        trace_row = {
            "field_key": definition.key,
            "source_selector": rule.source_selector,
            "projection_primitive": str(rule.projection_primitive or ""),
            "channel": rule.channel,
            "defaulted": defaulted,
        }
        if rule.source_selector == STRUCTURED_INPUT_SELECTOR:
            trace_row["source_path"] = deepcopy(rule.source_path)
            trace_row["input_structure"] = structure_resolution
        if reviewed:
            trace_row["reviewed"] = True
        trace.append(trace_row)
    channels: dict[str, str] = {}
    for channel, fragments in channel_fragments.items():
        result = ""
        for index, (separator, fragment) in enumerate(fragments):
            length_fragments[channel][index]["separator_chars"] = len(separator) if result else 0
            result = fragment if not result else result + separator + fragment
        channels[channel] = result
    channel_limits = normalize_prompt_profile_channel_limits(profile.channel_limits)
    channel_audit: dict[str, dict[str, int | str | None]] = {}
    channel_warnings: list[str] = []
    length_errors: list[str] = []
    for channel, limit in channel_limits.items():
        character_count = len(channels.get(channel, ""))
        maximum = int(limit["max_chars"] or 0)
        warning = limit["warn_at_chars"]
        if character_count > maximum:
            length_errors.append(f"通道 {channel} 装配后为 {character_count} 字符，超过配置上限 {maximum}")
        status = (
            "warning"
            if warning is not None and character_count >= warning
            else "ready"
        )
        if status == "warning":
            channel_warnings.append(
                f"通道 {channel} 已达到 {character_count} 字符，接近配置上限 {maximum}。",
            )
        channel_audit[channel] = {
            "character_count": character_count,
            "max_chars": maximum,
            "warn_at_chars": warning,
            "status": status,
        }
    profile_basis = {"version_id": str(profile.pk), "version": profile.version,
        "contract_sha256": effective_contract_sha256}
    length_diagnostics = [
            {"channel": key, "character_count": len(value),
                "max_chars": channel_limits.get(key, {}).get("max_chars"),
                "over_by": max(0, len(value) - channel_limits[key]["max_chars"]) if key in channel_limits else 0,
                "fragments": length_fragments[key]}
            for key, value in channels.items()]
    if length_errors:
        raise PromptProfileChannelLengthError(length_errors, channels=channels,
            lengths=length_diagnostics, profile_basis=profile_basis)
    payload = {
        "schema_version": 1,
        "prompt_profile_version_id": str(profile.id),
        "profile_key": profile.profile_key,
        "profile_name": profile.name,
        "version": profile.version,
        "contract_sha256": effective_contract_sha256,
        "source_projection_key": profile.source_projection_key,
        "official_reference": profile.official_reference,
        "field_rules": [
            {
                "field_key": rule.field_definition.key,
                "source_selector": rule.source_selector,
                "projection_primitive": str(rule.projection_primitive or ""),
                "channel": rule.channel,
                "position": rule.position,
                "label_style": rule.label_style,
                "separator": rule.separator,
                "max_chars": rule.max_chars,
                "generation_mode": rule.generation_mode,
                "generation_output_semantics": rule.generation_output_semantics,
            }
            for rule in candidate_rules
        ],
        "channels": channels,
        "channel_limits": channel_limits,
        "channel_audit": channel_audit,
        "length_diagnostics": length_diagnostics,
        "warnings": channel_warnings,
        "structured_fields": structured_fields,
        "trace": trace,
    }
    if source_projection_audit:
        payload["source_projection"] = source_projection_audit
    json_contract(payload, max_bytes=_prompt_profile_preview_max_bytes())
    return payload


def validate_profile_rule_runtime(rule: PromptProfileFieldRule) -> None:
    """Runtime validation that does not reject an already-published parent."""

    if not rule.field_definition.is_active:
        raise PromptProfileError(
            f"PromptProfile 字段 {rule.field_definition.key} 已停用",
            code="prompt_profile_field_inactive",
        )
    profile_rule_definition(rule)
    from studio.services.prompt_profile_structures import validate_structured_rule
    validate_structured_rule(rule)
    _validate_rule_values(rule)
    primitive_key = str(rule.projection_primitive or "").strip()
    if (
        rule.source_selector
        == PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
    ):
        if (
            rule.profile_version.source_projection_key
            != DECLARATIVE_SOURCE_PROJECTION_KEY
            or primitive_key not in SOURCE_PROJECTION_PRIMITIVES
        ):
            raise PromptProfileError(
                "published PromptProfile 包含无效 projection.output 原语",
                code="prompt_profile_projection_primitive_unknown",
            )
    elif primitive_key:
        raise PromptProfileError(
            "published PromptProfile 的非投影字段包含 projection_primitive",
            code="prompt_profile_projection_primitive_incompatible",
        )
    if (
        rule.channel == PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION
        and rule.source_selector != PromptProfileFieldRule.SourceSelector.FIXED_VALUE
    ):
        raise PromptProfileError(
            "published PromptProfile 包含不安全 system_instruction source",
            code="prompt_profile_untrusted_system_source",
        )
    if (
        type(rule.max_chars) is not int
        or not 1 <= rule.max_chars <= _field_text_max_characters()
    ):
        raise PromptProfileError(
            f"max_chars 必须是 1–{_field_text_max_characters()} 的整数",
            code="prompt_profile_field_limit_invalid",
        )
    validate_generation_field_contract(rule)


def reference_context_profile_source(
    fields: dict[str, Any],
    result_field: str,
    *, separator: str = "\n", list_prefix: str = "- ",
) -> str:
    """Encode a fact group; required/empty policy belongs to the field rule."""

    if not isinstance(fields, dict):
        raise PromptProfileError(
            "Reference Context-IR fields 必须是对象",
            code="reference_context_ir_invalid",
        )

    def list_section(name: str) -> str:
        raw = fields.get(name)
        if not isinstance(raw, list):
            raise PromptProfileError(
                f"Reference Context-IR 缺少非空 {name} 列表",
                code="reference_context_ir_invalid",
            )
        values = [str(item).strip() for item in raw]
        if any(not item for item in values):
            raise PromptProfileError(
                f"Reference Context-IR 的 {name} 含空项",
                code="reference_context_ir_invalid",
            )
        return separator.join(f"{list_prefix}{item}" for item in values)

    def text_section(name: str) -> str:
        if not isinstance(fields.get(name), str):
            raise PromptProfileError(f"Reference Context-IR {name} 必须是文本", code="reference_context_ir_invalid")
        return fields[name].strip()

    if result_field in {"subject_definitions", "retention_analysis"}:
        return list_section(result_field)
    if result_field == "detailed_description":
        detailed = fields.get(result_field)
        if not isinstance(detailed, list) or not detailed:
            raise PromptProfileError(
                "Reference Context-IR 缺少非空 detailed_description 列表",
                code="reference_context_ir_invalid",
            )
        detailed_lines = [str(item).strip() for item in detailed]
        if any(not item for item in detailed_lines):
            raise PromptProfileError(
                "Reference Context-IR 的 detailed_description 含空项",
                code="reference_context_ir_invalid",
            )
        return separator.join(detailed_lines)
    if result_field in {"summary", "overall_soundscape", "non_diegetic_music"}:
        return text_section(result_field)
    raise PromptProfileError(
        f"Reference Context-IR 包含未注册输出字段: {result_field}",
        code="prompt_profile_projection_output_invalid",
    )


def text_profile_sources(*, project, inputs: dict[str, Any], operation: str) -> dict[str, Any]:
    items = inputs.get("items") if isinstance(inputs, dict) else None
    text = "\n".join(
        str(item.get("text") or "") for item in items if isinstance(item, dict)
    ) if isinstance(items, list) else str(inputs.get("idea") or inputs.get("text") or "")
    return {
        "project.title": project.title,
        "project.logline": project.logline,
        "project.master_script": project.master_script,
        "project.language": project.language,
        "project.aspect_ratio": project.aspect_ratio,
        "request.theme": str(inputs.get("idea") or project.logline or project.title),
        "request.style": str(inputs.get("style_hint") or ""),
        "request.language": project.language,
        "request.text": text,
        "request.duration": inputs.get("episode_duration_seconds") or "",
    }
