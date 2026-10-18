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

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable

from studio.models import PromptProfileFieldRule, PromptProfileVersion
from studio.services.reference_context import compile_reference_context, reference_context_advisories
from studio.services.reference_context_facts import ReferenceContextFacts, validate_reference_context_facts
from studio.services.prompt_profiles import (
    DECLARATIVE_SOURCE_PROJECTION_KEY,
    DIRECT_SOURCE_PROJECTION_KEY,
    PromptProfileError,
    assemble_prompt_profile,
    normalize_prompt_profile_source_projection_config,
    prompt_profile_generation_length_audit,
    reference_context_profile_source,
    validate_prompt_profile_source_projection,
    validate_prompt_profile_generation_value,
)
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.source_projection_primitives import (
    REFERENCE_CONTEXT_ENGINE_KEY,
    SOURCE_PROJECTION_PRIMITIVES,
    contribution_target,
)


def _prompt_profile_preview_max_bytes() -> int:
    return boundary_int("security.prompt_profile.preview_max_bytes")


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
class PromptProfileCompilationFacts:
    """Values resolved by an authorized project/asset/manual source adapter.

    The adapter owns source revisions and explicit field selection; compilation
    never finds a latest candidate, approves text or calls an auxiliary model.
    """

    sources: dict[str, Any]
    source_language: str
    target_language: str
    reference_context: ReferenceContextFacts | None
    reviewed_values: dict[str, str]
    generation_contributions: dict[tuple[str, str], str]
    reviewed_evidence: dict[str, Any] | None = None
    reviewed_fingerprint: dict[str, Any] | None = None
    declared_reference_tokens: tuple[str, ...] = ()


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
        [PromptProfileVersion, PromptProfileCompilationFacts],
        _ProjectionResult,
    ]


def _compile_declarative_projection(profile, context) -> _ProjectionResult:
    config = normalize_prompt_profile_source_projection_config(profile.source_projection_config)
    rendering = (config.get("primitive_options", {}).get(REFERENCE_CONTEXT_ENGINE_KEY, {})
                 .get("rendering"))
    if rendering is None:
        raise PromptProfileError(
            "此策略尚未配置通用参考上下文模板；请创建后继草稿、补齐模板并复核发布",
            code="prompt_profile_rendering_required",
        )
    if context.reference_context is None:
        raise PromptProfileError("此策略需要明确的参考单元事实", code="prompt_profile_source_facts_missing")
    rules = list(profile.field_rules.select_related("field_definition").filter(
        source_selector=PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT,
    ).order_by("position", "id"))
    try:
        result = compile_reference_context(
            replace(context.reference_context, generation_contributions=context.generation_contributions), rendering,
            result_fields={SOURCE_PROJECTION_PRIMITIVES[rule.projection_primitive].result_field for rule in rules},
        )
    except ValueError as exc:
        raise PromptProfileError(str(exc), code="prompt_profile_reference_context_invalid") from exc
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
            "input_fingerprint": prompt_profile_facts_fingerprint(context),
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


def project_profile_reference_fields(profile, reference_context):
    """Deterministic source text, not accepted auxiliary results or a request.

    Preparation and final assembly use the same projector. A field may choose
    this source for final-value generation without registering a receiving slot.
    """
    return _compile_declarative_projection(profile, PromptProfileCompilationFacts(
        sources={}, source_language="", target_language=reference_context.language,
        reference_context=reference_context, reviewed_values={}, generation_contributions={},
    )).sources["projection.output"]


def _configured_projection_results(
    profile: PromptProfileVersion,
    context: PromptProfileCompilationFacts,
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


def prompt_profile_facts_fingerprint(facts: PromptProfileCompilationFacts) -> str:
    reference = asdict(replace(facts.reference_context, generation_contributions={})) if facts.reference_context is not None else None
    return json_contract({
        "schema_version": 1, "sources": facts.sources, "source_language": facts.source_language,
        "target_language": facts.target_language, "reference_context": reference,
        "reviewed_values": facts.reviewed_values, "reviewed_fingerprint": facts.reviewed_fingerprint,
        **({"declared_reference_tokens": list(facts.declared_reference_tokens)} if facts.declared_reference_tokens else {}),
        "generation_contributions": [
            {"unit_key": unit, "selector": selector, "text": text}
            for (unit, selector), text in sorted(facts.generation_contributions.items())
        ],
    }, max_bytes=_prompt_profile_preview_max_bytes()).sha256


def compile_prompt_profile_facts(profile: PromptProfileVersion, *, facts: PromptProfileCompilationFacts) -> MediaPromptProfileCompilation:
    """One deterministic assembly path; no owner lookup, writes or model calls."""
    if not isinstance(facts, PromptProfileCompilationFacts) or not isinstance(facts.sources, dict):
        raise PromptProfileError("提示词编译必须提供已解析的事实值对象", code="prompt_profile_source_facts_invalid")
    if any(not isinstance(value, str) for value in (facts.source_language, facts.target_language)):
        raise PromptProfileError("来源和目标语言必须明确提供", code="prompt_profile_source_facts_invalid")
    if "projection.output" in facts.sources:
        raise PromptProfileError("投影输出由冻结规则生成，不能作为来源注入", code="prompt_profile_source_facts_invalid")
    if not isinstance(facts.reviewed_values, dict) or not isinstance(facts.generation_contributions, dict):
        raise PromptProfileError("审核字段与贡献必须是明确对象", code="prompt_profile_source_facts_invalid")
    if (not isinstance(facts.declared_reference_tokens, tuple)
            or any(not isinstance(token, str) or not token for token in facts.declared_reference_tokens)
            or len(set(facts.declared_reference_tokens)) != len(facts.declared_reference_tokens)):
        raise PromptProfileError("输入引用标签必须来自本次唯一的实际素材接线", code="prompt_profile_source_facts_invalid")
    if any(not isinstance(key, str) or not key or not isinstance(value, str) for key, value in facts.reviewed_values.items()):
        raise PromptProfileError("审核字段必须使用精确字段 key 和文本", code="prompt_profile_source_facts_invalid")
    if any(not isinstance(key, tuple) or len(key) != 2 or any(not isinstance(part, str) or not part for part in key) or not isinstance(value, str)
           for key, value in facts.generation_contributions.items()):
        raise PromptProfileError("字段贡献必须引用明确单元和来源", code="prompt_profile_source_facts_invalid")
    if facts.reference_context is not None:
        validate_reference_context_facts(facts.reference_context)
        if facts.reference_context.generation_contributions:
            raise PromptProfileError("贡献只通过已选择字段提供，不能在参考事实中另存一份", code="prompt_profile_source_facts_invalid")
    contribution_rules = {}
    config = normalize_prompt_profile_source_projection_config(profile.source_projection_config)
    for rule in profile.field_rules.select_related("field_definition").all():
        if rule.generation_output_semantics == PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION:
            selector = contribution_target(rule.projection_primitive)
            if selector in contribution_rules:
                raise PromptProfileError("多个策略字段向同一来源提供贡献", code="prompt_profile_projection_contribution_ambiguous")
            contribution_rules[selector] = rule
    for (unit_key, selector), value in facts.generation_contributions.items():
        rule = contribution_rules.get(selector)
        if rule is None:
            raise PromptProfileError("字段贡献不属于此策略", code="prompt_profile_projection_contribution_unsupported")
        validate_prompt_profile_generation_value(rule, value)
    # Also enforce the bounded JSON value boundary for direct profiles, which
    # have no reference renderer. No model/ORM object can enter a source value.
    prompt_profile_facts_fingerprint(facts)
    sources = deepcopy(facts.sources)
    reviewed_values, generation_contributions = facts.reviewed_values, facts.generation_contributions
    reviewed_evidence, reviewed_fingerprint = deepcopy(facts.reviewed_evidence), deepcopy(facts.reviewed_fingerprint)
    projections = _configured_projection_results(profile, facts)
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
        require_generated_values=True,
    )
    assembly["warnings"] = [*(assembly.get("warnings") or []), *reference_context_advisories(
        facts.reference_context, assembly, config.get("quality_advisories", {}),
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
        field_key = rule.field_definition.key
        try:
            validate_prompt_profile_generation_value(rule, assembled_values.get(field_key))
        except PromptProfileError as exc:
            exc.field_key = field_key
            raise
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
                if selector == contribution_target(rule.projection_primitive)
            ]
        else:
            candidates = [("", assembled_values.get(rule.field_definition.key))]
        for unit_key, value in candidates:
            row = prompt_profile_generation_length_audit(rule, value)
            if unit_key:
                row["unit_key"] = unit_key
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
