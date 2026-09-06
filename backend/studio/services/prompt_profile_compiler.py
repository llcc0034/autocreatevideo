"""Configuration-selected media PromptProfile compilation.

The production orchestrator intentionally knows nothing about a model's field
names or final grammar.  A published PromptProfile selects ordinary media facts
directly and may opt into one of the bounded deterministic source projections
below by selecting that projection's allow-listed source keys.  PromptProfile
still owns field inclusion, order, labels, separators, channels and per-field
limits.

Adding a model that uses the existing fact vocabulary therefore requires only
new versioned configuration.  A genuinely new deterministic protocol needs one
reviewed projection adapter here, without adding a model branch to the queue or
production orchestration path.
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, field, replace
import hashlib
from typing import Any, Callable

from studio.models import PromptProfileFieldRule, PromptProfileVersion
from studio.services.reference_context import compile_reference_context, reference_context_advisories
from studio.services.prompt_profiles import (
    DECLARATIVE_SOURCE_PROJECTION_KEY,
    DIRECT_SOURCE_PROJECTION_KEY,
    PromptProfileError,
    assemble_prompt_profile,
    media_profile_sources,
    normalize_prompt_profile_source_projection_config,
    prompt_profile_generation_length_audit,
    reference_context_profile_source,
    validate_prompt_profile_source_projection,
    validate_prompt_profile_generation_value,
)
from studio.services.prompt_generation_sources import MODEL_GENERATION_MODE_VALUES
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.source_projection_primitives import (
    REFERENCE_CONTEXT_ENGINE_KEY,
    SOURCE_PROJECTION_PRIMITIVES,
)


GENERIC_MEDIA_PROMPT_INPUT_FINGERPRINT_VERSION = 2


def _prompt_profile_preview_max_bytes() -> int:
    return boundary_int("security.prompt_profile.preview_max_bytes")


@dataclass(frozen=True)
class MediaPromptCompilationContext:
    project: Any
    shots: tuple[Any, ...]
    prompt_type: str
    mode: str
    include_audio: bool
    duration_overrides: dict[str, int] = field(default_factory=dict)
    overall_soundscape: str = ""
    non_diegetic_music: str = ""
    extra_direction: str = ""
    single_shot_reference_bindings: dict | None = None
    single_shot_reference_assets: tuple[Any, ...] = ()
    consistency_snapshot: dict[str, Any] = field(default_factory=dict)
    language_gate_mode: str = "block"
    effective_durations_ms: dict[str, int] = field(default_factory=dict)
    reference_bindings: tuple[dict[str, Any], ...] = ()
    profile_selection_sha256: str = ""
    reviewed_profile_fields: tuple[dict[str, Any], ...] | None = None
    generation_contributions: dict[tuple[str, str], str] = field(default_factory=dict)
    target_language: str = "English"
    model_identifier: str = ""
    conditioning_mode_contract: dict[str, Any] | None = None
    frame_policy: dict[str, Any] | None = None

@dataclass(frozen=True)
class MediaPromptProfileCompilation:
    assembly: dict[str, Any]
    structured_fields: dict[str, Any]
    quality_override: dict[str, Any] | None = None
    model_prompt_contract: dict[str, Any] | None = None
    execution_facts: dict[str, Any] | None = None
    fingerprint_materials: tuple[dict[str, Any], ...] = ()
    reviewed_field_evidence: dict[str, Any] | None = None
    generation_length_audit: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _ProjectionResult:
    sources: dict[str, Any]
    structured_fields: dict[str, Any]
    quality: dict[str, Any] | None = None
    contract_format: str = ""
    fingerprint_material: dict[str, Any] | None = None
    execution_facts: dict[str, Any] | None = None
    consumed_contributions: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class _SourceProjection:
    key: str
    selectors: frozenset[str]
    compile: Callable[
        [PromptProfileVersion, MediaPromptCompilationContext],
        _ProjectionResult,
    ]


def media_prompt_input_fingerprint(context: MediaPromptCompilationContext) -> str:
    """Hash generic media facts without constructing any model adapter input.

    The base compiler fingerprint is deliberately expressed only in the
    allow-listed fact vocabulary consumed by ``direct_v1``.  A controlled
    protocol adapter may append its own versioned fingerprint material after
    it has been selected by the frozen PromptProfile configuration.
    """

    reference_bindings = [deepcopy(item) for item in context.reference_bindings]
    source_facts = media_profile_sources(
        project=context.project,
        shots=list(context.shots),
        extra_direction=context.extra_direction,
        overall_soundscape=context.overall_soundscape,
        non_diegetic_music=context.non_diegetic_music,
        include_audio=context.include_audio,
        effective_durations_ms=context.effective_durations_ms,
        reference_bindings=reference_bindings,
    )
    payload = {
        "schema_version": GENERIC_MEDIA_PROMPT_INPUT_FINGERPRINT_VERSION,
        "prompt_type": context.prompt_type,
        "mode": context.mode,
        "include_audio": bool(context.include_audio),
        "source_facts": source_facts,
        "effective_durations_ms": dict(context.effective_durations_ms),
        "duration_overrides": dict(context.duration_overrides),
        "consistency_snapshot": deepcopy(context.consistency_snapshot),
        "reference_bindings": reference_bindings,
        "language_gate_mode": context.language_gate_mode,
        "target_language": context.target_language,
        "model_identifier": context.model_identifier,
        "conditioning_mode_contract": deepcopy(context.conditioning_mode_contract),
        "frame_policy": deepcopy(context.frame_policy),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _compile_declarative_projection(profile, context) -> _ProjectionResult:
    config = normalize_prompt_profile_source_projection_config(profile.source_projection_config)
    rendering = (config.get("primitive_options", {}).get(REFERENCE_CONTEXT_ENGINE_KEY, {})
                 .get("rendering"))
    if rendering is None:
        raise PromptProfileError(
            "此策略尚未配置通用参考上下文模板；请创建后继草稿、补齐模板并复核发布",
            code="prompt_profile_rendering_required",
        )
    try:
        result = compile_reference_context(context, rendering)
    except ValueError as exc:
        raise PromptProfileError(str(exc), code="prompt_profile_reference_context_invalid") from exc
    rules = list(profile.field_rules.select_related("field_definition").filter(
        source_selector=PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT,
    ).order_by("position", "id"))
    fields = result["fields"]
    values = {rule.field_definition.key: reference_context_profile_source(
        fields, SOURCE_PROJECTION_PRIMITIVES[rule.projection_primitive].result_field,
        separator=rendering["separator"], list_prefix=rendering["list_prefix"],
    ) for rule in rules}
    return _ProjectionResult(
        sources={PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT: values},
        structured_fields=deepcopy(fields),
        contract_format="reference_context_ir_v2",
        fingerprint_material={
            "projection_engine": REFERENCE_CONTEXT_ENGINE_KEY,
            "projection_version": 2,
            "input_fingerprint": media_prompt_input_fingerprint(context),
            "rendering": deepcopy(rendering),
        },
        execution_facts=result["execution_facts"],
        consumed_contributions=result["consumed_contributions"],
    )


_SOURCE_PROJECTIONS = {
    DECLARATIVE_SOURCE_PROJECTION_KEY: _SourceProjection(
        key=DECLARATIVE_SOURCE_PROJECTION_KEY,
        selectors=frozenset({PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT}),
        compile=_compile_declarative_projection,
    ),
}


_REVIEWED_FIELD_KEYS = frozenset({
    "profile_selection_sha256",
    "prompt_profile_version_id",
    "profile_field_key",
    "source_selector",
    "generation_mode",
    "generation_source_selector",
    "value",
    "value_sha256",
    "request_id",
    "shot_id",
    "output_semantics",
})
_MODEL_PROCESSING_MODES = MODEL_GENERATION_MODE_VALUES


def _reviewed_field_values(
    profile: PromptProfileVersion,
    context: MediaPromptCompilationContext,
) -> tuple[
    dict[str, str],
    dict[tuple[str, str], str],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    rows = context.reviewed_profile_fields
    if rows is None:
        # Historical/direct compilation had no run-scoped Layer-2 snapshot.
        # ProductionRun decides whether a selected schema requires the new
        # evidence and passes an explicit tuple when it does.
        return {}, {}, None, None
    if len(context.shots) != 1:
        raise PromptProfileError(
            "run-scoped reviewed_profile_fields 只支持逐镜头编译",
            code="prompt_profile_reviewed_fields_scope_invalid",
        )
    if not context.profile_selection_sha256:
        raise PromptProfileError(
            "reviewed_profile_fields 缺少冻结 PromptProfile selection hash",
            code="prompt_profile_reviewed_fields_selection_missing",
        )
    shot_id = str(context.shots[0].id)
    rules = {
        rule.field_definition.key: rule
        for rule in profile.field_rules.select_related("field_definition").all()
    }
    values: dict[str, str] = {}
    contributions: dict[tuple[str, str], str] = {}
    seen_fields: set[str] = set()
    evidence_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != _REVIEWED_FIELD_KEYS:
            raise PromptProfileError(
                f"reviewed_profile_fields[{index}] 合同字段无效",
                code="prompt_profile_reviewed_fields_invalid",
            )
        field_key = str(row.get("profile_field_key") or "")
        rule = rules.get(field_key)
        value = row.get("value")
        request_id = str(row.get("request_id") or "")
        if (
            rule is None
            or rule.generation_mode not in _MODEL_PROCESSING_MODES
            or row.get("profile_selection_sha256")
            != context.profile_selection_sha256
            or row.get("prompt_profile_version_id") != str(profile.id)
            or row.get("shot_id") != shot_id
            or row.get("source_selector") != rule.source_selector
            or row.get("generation_mode") != rule.generation_mode
            or row.get("generation_source_selector")
            != rule.generation_source_selector
            or row.get("output_semantics")
            != rule.generation_output_semantics
            or not request_id
            or not isinstance(value, str)
            or row.get("value_sha256")
            != hashlib.sha256(value.encode("utf-8")).hexdigest()
        ):
            raise PromptProfileError(
                f"reviewed_profile_fields[{index}] 与冻结字段规则不一致",
                code="prompt_profile_reviewed_field_contract_mismatch",
            )
        if field_key in seen_fields:
            raise PromptProfileError(
                f"reviewed_profile_fields 重复声明字段 {field_key}",
                code="prompt_profile_reviewed_field_duplicate",
            )
        seen_fields.add(field_key)
        validate_prompt_profile_generation_value(
            rule,
            value,
            source_language=str(context.project.language or ""),
            inherited_language=context.target_language,
        )
        if (
            rule.generation_output_semantics
            == PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        ):
            contribution_key = (shot_id, rule.generation_source_selector)
            if contribution_key in contributions:
                raise PromptProfileError(
                    "多个 PromptProfile 字段向同一 projection source 提供贡献: "
                    + rule.generation_source_selector,
                    code="prompt_profile_projection_contribution_ambiguous",
                )
            contributions[contribution_key] = value
        else:
            values[field_key] = value
        evidence_rows.append({
            key: deepcopy(row[key])
            for key in sorted(_REVIEWED_FIELD_KEYS - {"value"})
        })

    missing = sorted(
        field_key
        for field_key, rule in rules.items()
        if rule.generation_mode in _MODEL_PROCESSING_MODES
        and rule.generation_required
        and field_key not in seen_fields
    )
    if missing:
        raise PromptProfileError(
            "reviewed_profile_fields 缺少必需的模型处理字段: "
            + ", ".join(missing),
            code="prompt_profile_reviewed_field_missing",
        )
    evidence_rows.sort(key=lambda row: (
        str(row.get("shot_id") or ""),
        str(row.get("profile_field_key") or ""),
        str(row.get("request_id") or ""),
    ))
    evidence = {
        "schema_version": 1,
        "prompt_profile_version_id": str(profile.id),
        "profile_selection_sha256": context.profile_selection_sha256,
        "shot_id": shot_id,
        "rows": evidence_rows,
    }
    evidence_contract = json_contract(
        evidence,
        max_bytes=_prompt_profile_preview_max_bytes(),
    )
    evidence["contract_sha256"] = evidence_contract.sha256
    return values, contributions, evidence, {
        "reviewed_profile_fields_sha256": evidence_contract.sha256,
    }


def _configured_projection_results(
    profile: PromptProfileVersion,
    context: MediaPromptCompilationContext,
) -> list[_ProjectionResult]:
    selected_sources = {
        str(value)
        for value in profile.field_rules.values_list("source_selector", flat=True)
    }
    projection_key = str(
        getattr(profile, "source_projection_key", DIRECT_SOURCE_PROJECTION_KEY)
        or DIRECT_SOURCE_PROJECTION_KEY
    ).strip()
    if projection_key == DIRECT_SOURCE_PROJECTION_KEY:
        return []
    projection = _SOURCE_PROJECTIONS.get(projection_key)
    if projection is None:
        raise PromptProfileError(
            f"PromptProfile source_projection_key 未注册: {projection_key}",
            code="prompt_profile_projection_unknown",
        )
    validate_prompt_profile_source_projection(profile)
    missing = sorted(projection.selectors - selected_sources)
    if missing:
        raise PromptProfileError(
            f"PromptProfile source projection {projection.key} 配置不完整；"
            f"缺少 selector: {', '.join(missing)}",
            code="prompt_profile_projection_incomplete",
        )
    result = projection.compile(profile, context)
    if set(result.sources) != set(projection.selectors):
        raise PromptProfileError(
            f"PromptProfile source projection {projection.key} 输出合同漂移",
            code="prompt_profile_projection_output_invalid",
        )
    return [result]


def _projection_contract(
    profile: PromptProfileVersion,
    assembly: dict[str, Any],
    *,
    contract_format: str,
) -> dict[str, Any]:
    rules = list(
        profile.field_rules.select_related("field_definition").order_by(
            "position", "id",
        )
    )
    positive_order = [
        rule.field_definition.key
        for rule in rules
        if rule.channel == PromptProfileFieldRule.Channel.POSITIVE_PROMPT
    ]
    negative_supported = any(
        rule.channel == PromptProfileFieldRule.Channel.NEGATIVE_PROMPT
        for rule in rules
    )
    channels = assembly.get("channels") if isinstance(assembly, dict) else {}
    return {
        "schema_version": 1,
        "format": contract_format,
        "section_order": positive_order,
        "negative_prompt_supported": negative_supported,
        "negative_prompt_submitted": bool(
            str((channels or {}).get("negative_prompt") or "").strip(),
        ),
    }


def compile_media_prompt_profile(
    profile: PromptProfileVersion,
    *,
    context: MediaPromptCompilationContext,
) -> MediaPromptProfileCompilation:
    """Compile a media profile from versioned rules and safe source facts."""

    sources = media_profile_sources(
        project=context.project,
        shots=list(context.shots),
        extra_direction=context.extra_direction,
        overall_soundscape=context.overall_soundscape,
        non_diegetic_music=context.non_diegetic_music,
        include_audio=context.include_audio,
        effective_durations_ms=context.effective_durations_ms,
        reference_bindings=[deepcopy(item) for item in context.reference_bindings],
    )
    (
        reviewed_values,
        generation_contributions,
        reviewed_evidence,
        reviewed_fingerprint,
    ) = _reviewed_field_values(profile, context)
    projection_context = replace(
        context,
        generation_contributions=generation_contributions,
    )
    projections = _configured_projection_results(profile, projection_context)
    consumed_contributions: set[tuple[str, str]] = set()
    for result in projections:
        sources.update(result.sources)
        overlap = consumed_contributions & set(result.consumed_contributions)
        if overlap:
            raise PromptProfileError(
                "多个 source projection 消费了同一字段贡献",
                code="prompt_profile_projection_contribution_ambiguous",
            )
        consumed_contributions.update(result.consumed_contributions)
    unconsumed = sorted(set(generation_contributions) - consumed_contributions)
    if unconsumed:
        raise PromptProfileError(
            "PromptProfile contribution 没有匹配的受控 source projection: "
            + ", ".join(f"{shot_id}:{selector}" for shot_id, selector in unconsumed),
            code="prompt_profile_projection_contribution_unsupported",
        )

    assembly = assemble_prompt_profile(
        profile,
        sources=sources,
        reviewed_field_values=reviewed_values,
    )
    config = normalize_prompt_profile_source_projection_config(profile.source_projection_config)
    assembly["warnings"] = [*(assembly.get("warnings") or []), *reference_context_advisories(
        context, assembly, config.get("quality_advisories", {}),
    )]
    assembled_values = assembly.get("structured_fields") or {}
    for rule in profile.field_rules.select_related("field_definition").all():
        if (
            rule.generation_output_semantics
            == PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        ):
            # The reviewed contribution was validated above.  The projection's
            # complete section can legitimately add locked bindings, labels or
            # authored dialogue that are outside the contribution contract.
            continue
        validate_prompt_profile_generation_value(
            rule,
            assembled_values.get(rule.field_definition.key),
            source_language=str(context.project.language or ""),
            inherited_language=context.target_language,
        )
    structured_fields: dict[str, Any] = {}
    quality_override: dict[str, Any] | None = None
    contract_format = ""
    fingerprint_materials: list[dict[str, Any]] = []
    execution_facts: dict[str, Any] | None = None
    for result in projections:
        if result.structured_fields:
            structured_fields.update(deepcopy(result.structured_fields))
        if result.quality is not None:
            if quality_override is not None:
                raise PromptProfileError(
                    "多个 source projection 同时声明质量审计结果",
                    code="prompt_profile_projection_quality_ambiguous",
                )
            quality_override = deepcopy(result.quality)
        if result.contract_format:
            if contract_format:
                raise PromptProfileError(
                    "多个 source projection 同时声明模型提示词合同",
                    code="prompt_profile_projection_contract_ambiguous",
                )
            contract_format = result.contract_format
        if result.fingerprint_material is not None:
            fingerprint_materials.append(deepcopy(result.fingerprint_material))
        if result.execution_facts is not None:
            if execution_facts is not None:
                raise PromptProfileError(
                    "多个 source projection 同时声明执行事实",
                    code="prompt_profile_projection_execution_facts_ambiguous",
                )
            execution_facts = deepcopy(result.execution_facts)
    if reviewed_fingerprint is not None:
        fingerprint_materials.append(reviewed_fingerprint)
    # The actual configured field values are authoritative over any extra
    # diagnostic fields returned by a projection.
    structured_fields.update(deepcopy(assembly["structured_fields"]))

    generation_length_audit: list[dict[str, Any]] = []
    for rule in profile.field_rules.select_related("field_definition").order_by(
        "position", "id",
    ):
        if (
            rule.generation_output_semantics
            == PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
        ):
            candidates = [
                (shot_id, value)
                for (shot_id, selector), value in generation_contributions.items()
                if selector == rule.generation_source_selector
            ]
        else:
            candidates = [("", assembled_values.get(rule.field_definition.key))]
        for shot_id, value in candidates:
            row = prompt_profile_generation_length_audit(rule, value)
            if shot_id:
                row["shot_id"] = shot_id
            generation_length_audit.append(row)
    if quality_override is not None:
        quality_override = deepcopy(quality_override)
        warnings = [
            str(item) for item in (quality_override.get("warnings") or [])
            if isinstance(item, str) and item
        ]
        for item in assembly.get("warnings") or []:
            if isinstance(item, str) and item and item not in warnings:
                warnings.append(item)
        channel_audit = assembly.get("channel_audit")
        if not isinstance(channel_audit, dict):
            channel_audit = {}
        positive_audit = channel_audit.get("positive_prompt")
        if isinstance(positive_audit, dict):
            quality_override["prompt_character_count"] = int(
                positive_audit.get("character_count") or 0,
            )
            quality_override["prompt_character_limit"] = int(
                positive_audit.get("max_chars") or 0,
            )
        quality_override["prompt_profile_channel_audit"] = deepcopy(
            channel_audit,
        )
        quality_override["prompt_profile_generation_length_audit"] = deepcopy(
            generation_length_audit,
        )
        quality_override["warnings"] = warnings
        if warnings:
            quality_override["status"] = "needs_attention"

    model_prompt_contract = (
        _projection_contract(
            profile,
            assembly,
            contract_format=contract_format,
        )
        if contract_format else None
    )
    return MediaPromptProfileCompilation(
        assembly=assembly,
        structured_fields=structured_fields,
        quality_override=quality_override,
        model_prompt_contract=model_prompt_contract,
        execution_facts=execution_facts,
        fingerprint_materials=tuple(fingerprint_materials),
        reviewed_field_evidence=reviewed_evidence,
        generation_length_audit=tuple(generation_length_audit),
    )
