from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import (
    ModelProfileVersion,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
)
from studio.services.dialogue import (
    DialogueContractError,
    validate_dialogue_timing,
)
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.prompt_translation import confirmed_translation


PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION = 1
PROMPT_PROFILE_MAX_CONTRACT_BYTES = 64 * 1024
PROMPT_PROFILE_MAX_PREVIEW_BYTES = 64 * 1024
KNOWN_PROMPT_OPERATIONS = (
    "video.generate",
    "image.generate",
    "script.generate",
    "script.structure",
    "prompt.normalize",
)
KNOWN_MODEL_FAMILIES = ("minimax_h3", "sdxl", "deepseek", "qwen")
CAPABILITY_BOUND_PROMPT_OPERATIONS = frozenset({
    "script.generate",
    "script.structure",
    "prompt.normalize",
})
_SAFE_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")


class PromptProfileError(ValidationError):
    code = "prompt_profile_invalid"

    def __init__(self, message: str, *, code: str | None = None):
        self.code = code or self.code
        super().__init__(message, code=self.code)


class PromptProfileResolutionError(PromptProfileError):
    code = "prompt_profile_resolution_failed"


@dataclass(frozen=True)
class ResolvedPromptProfile:
    binding: PromptProfileBinding
    profile_version: PromptProfileVersion


_SEPARATORS = {
    PromptProfileFieldRule.Separator.NEWLINE: "\n",
    PromptProfileFieldRule.Separator.COMMA: ", ",
    PromptProfileFieldRule.Separator.SEMICOLON: "; ",
    PromptProfileFieldRule.Separator.SPACE: " ",
}


def infer_model_family(
    identifier: str,
    *,
    profile_key: str = "",
    provider_key: str = "",
) -> str:
    """Return a stable family without treating arbitrary identifiers as code."""

    combined = " ".join((identifier, profile_key, provider_key)).casefold()
    if any(token in combined for token in ("minimax", "hailuo", "h3")):
        return "minimax_h3"
    if "sdxl" in combined or "stable-diffusion-xl" in combined:
        return "sdxl"
    if "deepseek" in combined:
        return "deepseek"
    if "qwen" in combined:
        return "qwen"
    candidate = re.sub(r"[^a-z0-9_.-]+", "_", str(profile_key or identifier).casefold())
    candidate = candidate.strip("_.-")[:100]
    if len(candidate) == 1:
        candidate = f"model_{candidate}"
    if len(candidate) < 2 or _SAFE_FAMILY_RE.fullmatch(candidate) is None:
        raise PromptProfileResolutionError(
            "无法从模型标识解析安全的 model_family",
            code="prompt_profile_model_family_unknown",
        )
    return candidate


def model_family_for_profile(profile: ModelProfileVersion) -> str:
    specification = (
        profile.specification
        if isinstance(getattr(profile, "specification", None), dict)
        else {}
    )
    explicit = str(specification.get("model_family") or "").strip().casefold()
    if explicit:
        if _SAFE_FAMILY_RE.fullmatch(explicit) is None:
            raise PromptProfileResolutionError(
                "模型版本 specification.model_family 格式无效",
                code="prompt_profile_model_family_invalid",
            )
        return explicit
    provider_key = ""
    if getattr(profile, "provider_id", None):
        provider_key = str(getattr(profile.provider, "key", "") or "")
    return infer_model_family(
        profile.model_identifier,
        profile_key=profile.profile_key,
        provider_key=provider_key,
    )


def validate_field_definition(definition: PromptFieldDefinition) -> None:
    options = definition.value_options
    if not isinstance(options, list) or len(options) > 100:
        raise PromptProfileError("value_options 必须是不超过 100 项的数组")
    if any(
        not isinstance(item, str) or not item.strip() or len(item) > 500
        for item in options
    ):
        raise PromptProfileError("value_options 只能包含非空短文本")
    if len(options) != len(set(options)):
        raise PromptProfileError("value_options 不能重复")
    if definition.value_type == PromptFieldDefinition.ValueType.ENUM and not options:
        raise PromptProfileError("enum 字段必须配置 value_options")
    if definition.value_type != PromptFieldDefinition.ValueType.ENUM and options:
        raise PromptProfileError("只有 enum 字段可以配置 value_options")
    if definition.default_value is not None:
        _typed_value(definition, definition.default_value)


def validate_profile_rule(rule: PromptProfileFieldRule) -> None:
    if rule.profile_version.status != PromptProfileVersion.Status.DRAFT:
        raise PromptProfileError("published/retired PromptProfile 的字段规则不可修改")
    if not rule.field_definition.is_active:
        raise PromptProfileError("字段规则不能引用已停用字段")
    validate_field_definition(rule.field_definition)
    if (
        rule.channel == PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION
        and rule.source_selector != PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT
    ):
        raise PromptProfileError(
            "system_instruction 只能装配管理员维护的字段默认值；项目和请求内容不得提升为系统指令",
            code="prompt_profile_untrusted_system_source",
        )
    if (
        rule.source_selector == PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT
        and rule.field_definition.default_value is None
    ):
        raise PromptProfileError("definition.default 规则要求字段已配置默认值")


def prompt_profile_contract_payload(profile: PromptProfileVersion) -> dict[str, Any]:
    rules = []
    if profile.pk:
        queryset = profile.field_rules.select_related("field_definition").order_by(
            "position", "id",
        )
        for rule in queryset:
            definition = rule.field_definition
            rules.append({
                "field_key": definition.key,
                "field_value_type": definition.value_type,
                "field_default_value": definition.default_value,
                "field_value_options": definition.value_options,
                "field_required": definition.is_required,
                "source_selector": rule.source_selector,
                "channel": rule.channel,
                "position": rule.position,
                "label_style": rule.label_style,
                "separator": rule.separator,
                "required": rule.required,
                "omit_if_empty": rule.omit_if_empty,
                "max_chars": rule.max_chars,
            })
    return {
        "schema_version": PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        "profile_key": profile.profile_key,
        "version": profile.version,
        "model_family": profile.model_family,
        "operation": profile.operation,
        "mode": profile.mode,
        "rules": rules,
    }


def refresh_prompt_profile_contract(profile: PromptProfileVersion) -> PromptProfileVersion:
    if profile.status != PromptProfileVersion.Status.DRAFT:
        raise PromptProfileError("只有 draft PromptProfile 可以刷新合同")
    contract = json_contract(
        prompt_profile_contract_payload(profile),
        schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        max_bytes=PROMPT_PROFILE_MAX_CONTRACT_BYTES,
    )
    profile.contract_schema_version = contract.schema_version
    profile.contract_size_bytes = contract.size_bytes
    profile.contract_sha256 = contract.sha256
    profile.save(update_fields=(
        "contract_schema_version", "contract_size_bytes", "contract_sha256", "updated_at",
    ))
    return profile


def verify_prompt_profile_contract(profile: PromptProfileVersion) -> dict[str, Any]:
    payload = prompt_profile_contract_payload(profile)
    return verify_json_contract(
        payload,
        schema_version=profile.contract_schema_version,
        size_bytes=profile.contract_size_bytes,
        sha256=profile.contract_sha256,
        max_bytes=PROMPT_PROFILE_MAX_CONTRACT_BYTES,
    ).value


def validate_prompt_profile_for_publish(profile: PromptProfileVersion) -> None:
    if profile.status != PromptProfileVersion.Status.DRAFT:
        raise PromptProfileError("只有 draft PromptProfile 可以发布")
    if profile.review_state != PromptProfileVersion.ReviewState.READY:
        raise PromptProfileError("PromptProfile 仍为 needs_review，必须先完成审核")
    rules = list(profile.field_rules.select_related("field_definition").order_by("position"))
    if not rules:
        raise PromptProfileError("PromptProfile 至少需要一个字段规则")
    for rule in rules:
        validate_profile_rule(rule)
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
    if locked.bindings.filter(is_active=True).exists():
        raise PromptProfileError(
            "仍有 active binding 引用该 PromptProfile，请先停用绑定",
            code="prompt_profile_in_use",
        )
    locked.status = PromptProfileVersion.Status.RETIRED
    locked.save(update_fields=("status", "updated_at"))
    return locked


def validate_prompt_profile_binding(binding: PromptProfileBinding) -> None:
    profile = binding.prompt_profile_version
    if profile.status != PromptProfileVersion.Status.PUBLISHED:
        raise PromptProfileError("binding 只能引用 published PromptProfile")
    verify_prompt_profile_contract(profile)
    if binding.operation != profile.operation:
        raise PromptProfileError("binding.operation 必须与 PromptProfile 一致")
    if binding.model_family != profile.model_family:
        raise PromptProfileError("binding.model_family 必须与 PromptProfile 一致")
    if profile.mode and binding.mode != profile.mode:
        raise PromptProfileError("binding.mode 必须与非空 PromptProfile.mode 一致")
    model_profile = binding.model_profile_version
    if model_profile is not None:
        if model_profile.status != ModelProfileVersion.Status.PUBLISHED:
            raise PromptProfileError("binding 只能引用 published model profile")
        family = model_family_for_profile(model_profile)
        if family != binding.model_family:
            raise PromptProfileError(
                f"binding.model_family={binding.model_family} 与实际模型族 {family} 不一致",
            )
        if binding.is_active and binding.operation in CAPABILITY_BOUND_PROMPT_OPERATIONS:
            specification = verify_json_contract(
                model_profile.specification,
                schema_version=model_profile.specification_schema_version,
                size_bytes=model_profile.specification_size_bytes,
                sha256=model_profile.specification_sha256,
            ).value
            capabilities = (
                specification.get("capabilities")
                if isinstance(specification, dict)
                else None
            )
            if (
                not isinstance(capabilities, list)
                or any(
                    not isinstance(item, str)
                    or not item
                    or item != item.strip()
                    for item in capabilities
                )
                or len(capabilities) != len(set(capabilities))
            ):
                raise PromptProfileError(
                    "active exact binding 要求模型 specification.capabilities "
                    "为无重复的精确非空字符串数组",
                    code="prompt_profile_model_capabilities_invalid",
                )
            if binding.operation not in capabilities:
                raise PromptProfileError(
                    "active exact binding 的模型版本未在 "
                    f"specification.capabilities 精确声明 {binding.operation}",
                    code="prompt_profile_model_capability_missing",
                )


def resolve_prompt_profile_binding(
    *,
    operation: str,
    model_family: str,
    mode: str = "",
    model_profile_version: ModelProfileVersion | None = None,
) -> ResolvedPromptProfile | None:
    """Resolve one active binding; equal top candidates are a hard error."""

    operation = str(operation or "").strip()
    model_family = str(model_family or "").strip()
    mode = str(mode or "").strip()
    queryset = PromptProfileBinding.objects.select_related(
        "prompt_profile_version", "model_profile_version",
    ).filter(
        is_active=True,
        operation=operation,
        model_family=model_family,
    )
    candidates: list[tuple[int, int, PromptProfileBinding]] = []
    for binding in queryset:
        if binding.mode and binding.mode != mode:
            continue
        if binding.model_profile_version_id:
            if model_profile_version is None or binding.model_profile_version_id != model_profile_version.id:
                continue
            model_specificity = 2
        else:
            model_specificity = 0
        mode_specificity = 1 if binding.mode else 0
        candidates.append((model_specificity + mode_specificity, binding.priority, binding))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_specificity, best_priority, binding = candidates[0]
    ties = [
        item for item in candidates
        if item[0] == best_specificity and item[1] == best_priority
    ]
    if len(ties) != 1:
        keys = ", ".join(sorted(item[2].key for item in ties))
        raise PromptProfileResolutionError(
            f"PromptProfile binding 冲突: {keys}",
            code="prompt_profile_binding_ambiguous",
        )
    validate_prompt_profile_binding(binding)
    return ResolvedPromptProfile(
        binding=binding,
        profile_version=binding.prompt_profile_version,
    )


def require_prompt_profile_binding(
    *,
    operation: str,
    model_family: str,
    mode: str = "",
    model_profile_version: ModelProfileVersion | None = None,
) -> ResolvedPromptProfile:
    """Resolve the single production PromptProfile or fail closed.

    Draft/preview tools may call ``resolve_prompt_profile_binding`` directly,
    but a ProductionRun must never silently fall back to an unversioned prompt
    assembler after an actual model route has been selected.
    """

    resolved = resolve_prompt_profile_binding(
        operation=operation,
        model_family=model_family,
        mode=mode,
        model_profile_version=model_profile_version,
    )
    if resolved is None:
        target = str(
            getattr(model_profile_version, "model_identifier", "")
            or model_family
            or "unknown-model"
        )
        suffix = f"/{mode}" if mode else ""
        raise PromptProfileResolutionError(
            f"模型 {target} 的 {operation}{suffix} 缺少唯一 active PromptProfile binding",
            code="prompt_profile_binding_missing",
        )
    return resolved


def _typed_value(definition: PromptFieldDefinition, value: Any) -> Any:
    value_type = definition.value_type
    if value_type in {
        PromptFieldDefinition.ValueType.TEXT,
        PromptFieldDefinition.ValueType.LONG_TEXT,
    }:
        if not isinstance(value, str):
            raise PromptProfileError(f"{definition.key} 必须是文本")
        if len(value) > 30_000:
            raise PromptProfileError(f"{definition.key} 超过 30000 字符")
        return value.strip()
    if value_type == PromptFieldDefinition.ValueType.STRING_LIST:
        if not isinstance(value, list) or len(value) > 100 or any(
            not isinstance(item, str) or len(item) > 1000 for item in value
        ):
            raise PromptProfileError(f"{definition.key} 必须是不超过 100 项的文本数组")
        return [item.strip() for item in value if item.strip()]
    if value_type == PromptFieldDefinition.ValueType.BOOLEAN:
        if type(value) is not bool:
            raise PromptProfileError(f"{definition.key} 必须是布尔值")
        return value
    if value_type == PromptFieldDefinition.ValueType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PromptProfileError(f"{definition.key} 必须是数值")
        if not -1_000_000_000 <= float(value) <= 1_000_000_000:
            raise PromptProfileError(f"{definition.key} 数值超出安全范围")
        return value
    if value_type == PromptFieldDefinition.ValueType.ENUM:
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
    if rule.label_style == PromptProfileFieldRule.LabelStyle.BRACKETED:
        return f"[{label}] {text}"
    return f"{label}: {text}"


def assemble_prompt_profile(
    profile: PromptProfileVersion,
    *,
    sources: dict[str, Any],
    allow_draft: bool = False,
) -> dict[str, Any]:
    """Deterministically assemble allow-listed values without template eval."""

    if profile.status not in {
        PromptProfileVersion.Status.PUBLISHED,
        PromptProfileVersion.Status.RETIRED,
    } and not (
        allow_draft and profile.status == PromptProfileVersion.Status.DRAFT
    ):
        raise PromptProfileError("运行时只能装配 published PromptProfile")
    verify_prompt_profile_contract(profile)
    if not isinstance(sources, dict):
        raise PromptProfileError("sources 必须是对象")
    allowed = set(PromptProfileFieldRule.SourceSelector.values)
    unknown = sorted(set(sources) - allowed)
    if unknown:
        raise PromptProfileError(f"sources 包含未授权 selector: {', '.join(unknown)}")
    channel_fragments: dict[str, list[tuple[str, str]]] = {
        choice: [] for choice in PromptProfileFieldRule.Channel.values
    }
    structured_fields: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    rules = profile.field_rules.select_related("field_definition").order_by("position", "id")
    for rule in rules:
        validate_profile_rule_runtime(rule)
        definition = rule.field_definition
        defaulted = False
        if rule.source_selector == PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT:
            raw = definition.default_value
            defaulted = True
        else:
            raw = sources.get(rule.source_selector)
            if (raw is None or raw == "" or raw == []) and definition.default_value is not None:
                raw = definition.default_value
                defaulted = True
        missing = raw is None or raw == "" or raw == []
        required = rule.required or definition.is_required
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
        channel_fragments[rule.channel].append((separator, fragment))
        structured_fields[definition.key] = value
        trace.append({
            "field_key": definition.key,
            "source_selector": rule.source_selector,
            "channel": rule.channel,
            "defaulted": defaulted,
        })
    channels: dict[str, str] = {}
    for channel, fragments in channel_fragments.items():
        result = ""
        for separator, fragment in fragments:
            result = fragment if not result else result + separator + fragment
        channels[channel] = result
    payload = {
        "schema_version": 1,
        "prompt_profile_version_id": str(profile.id),
        "profile_key": profile.profile_key,
        "version": profile.version,
        "contract_sha256": profile.contract_sha256,
        "channels": channels,
        "structured_fields": structured_fields,
        "trace": trace,
    }
    json_contract(payload, max_bytes=PROMPT_PROFILE_MAX_PREVIEW_BYTES)
    return payload


def validate_profile_rule_runtime(rule: PromptProfileFieldRule) -> None:
    """Runtime validation that does not reject an already-published parent."""

    if not rule.field_definition.is_active:
        raise PromptProfileError(
            f"PromptProfile 字段 {rule.field_definition.key} 已停用",
            code="prompt_profile_field_inactive",
        )
    validate_field_definition(rule.field_definition)
    if (
        rule.channel == PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION
        and rule.source_selector != PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT
    ):
        raise PromptProfileError(
            "published PromptProfile 包含不安全 system_instruction source",
            code="prompt_profile_untrusted_system_source",
        )


def frozen_profile_reference(resolved: ResolvedPromptProfile | None) -> dict[str, Any]:
    if resolved is None:
        return {}
    return {
        "prompt_profile_version_id": str(resolved.profile_version.id),
        "prompt_profile_binding_key": resolved.binding.key,
        "prompt_profile_contract_sha256": resolved.profile_version.contract_sha256,
    }


def verify_frozen_profile_reference(
    profile: PromptProfileVersion | None,
    *,
    binding_key: str,
    contract_sha256: str,
) -> None:
    if profile is None:
        if binding_key or contract_sha256:
            raise PromptProfileResolutionError(
                "PromptProfile 冻结引用不完整",
                code="prompt_profile_frozen_reference_invalid",
            )
        return
    if profile.status not in {
        PromptProfileVersion.Status.PUBLISHED,
        PromptProfileVersion.Status.RETIRED,
    }:
        raise PromptProfileResolutionError(
            "冻结 PromptProfile 从未发布或状态无效",
            code="prompt_profile_frozen_reference_invalid",
        )
    if not binding_key or contract_sha256 != profile.contract_sha256:
        raise PromptProfileResolutionError(
            "PromptProfile 冻结 checksum 已漂移",
            code="prompt_profile_frozen_reference_invalid",
        )
    verify_prompt_profile_contract(profile)


def verify_frozen_profile_assembly(
    profile: PromptProfileVersion,
    assembly: Any,
) -> dict[str, Any]:
    if not isinstance(assembly, dict):
        raise PromptProfileResolutionError(
            "冻结 PromptProfile assembly 缺失",
            code="prompt_profile_frozen_assembly_invalid",
        )
    if (
        assembly.get("prompt_profile_version_id") != str(profile.id)
        or assembly.get("contract_sha256") != profile.contract_sha256
    ):
        raise PromptProfileResolutionError(
            "冻结 PromptProfile assembly identity 已漂移",
            code="prompt_profile_frozen_assembly_invalid",
        )
    channels = assembly.get("channels")
    if not isinstance(channels, dict) or set(channels) != set(
        PromptProfileFieldRule.Channel.values
    ) or any(not isinstance(value, str) for value in channels.values()):
        raise PromptProfileResolutionError(
            "冻结 PromptProfile channels 无效",
            code="prompt_profile_frozen_assembly_invalid",
        )
    if not isinstance(assembly.get("structured_fields"), dict) or not isinstance(
        assembly.get("trace"), list
    ):
        raise PromptProfileResolutionError(
            "冻结 PromptProfile structured fields 无效",
            code="prompt_profile_frozen_assembly_invalid",
        )
    json_contract(assembly, max_bytes=PROMPT_PROFILE_MAX_PREVIEW_BYTES)
    return assembly


def profile_fingerprint(base_fingerprint: str, resolved: ResolvedPromptProfile | None) -> str:
    if resolved is None:
        return base_fingerprint
    material = (
        f"{base_fingerprint}|{resolved.profile_version.id}|"
        f"{resolved.profile_version.contract_sha256}|{resolved.binding.key}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _translated_media_value(
    project,
    *,
    scope_type: str,
    scope_id,
    field_key: str,
    source: Any,
) -> str:
    """Read only a current, human-confirmed translation overlay.

    PromptProfile owns selection and ordering, while the translation overlay
    remains the authoritative execution-language view of one source field.  A
    missing/stale overlay intentionally returns the original value so the
    production language gate can report the real unresolved input.
    """

    return confirmed_translation(
        project_id=project.id,
        scope_type=scope_type,
        scope_id=scope_id,
        field_key=field_key,
        source_text=str(source or ""),
    ).strip()


def _joined_shot_values(shots: list, values: list[str]) -> str:
    rows = [
        (shot, str(value or "").strip())
        for shot, value in zip(shots, values, strict=True)
        if str(value or "").strip()
    ]
    if len(shots) == 1:
        return rows[0][1] if rows else ""
    return "\n".join(f"[Shot {shot.number}] {value}" for shot, value in rows)


def _reference_source_items(reference_bindings: list[dict[str, Any]]) -> list[str]:
    """Expose the canonical graph bindings without local paths or checksums."""

    values: list[str] = []
    for item in reference_bindings:
        if not isinstance(item, dict):
            raise PromptProfileError("reference_bindings 每项必须是对象")
        label = str(item.get("input_label") or "").strip()
        category = str(item.get("category") or "").strip()
        identity = str(
            item.get("entity_label")
            or item.get("entity_name")
            or item.get("name")
            or ""
        ).strip()
        purpose = str(item.get("purpose") or "").strip()
        if label:
            role = ":".join(part for part in (category, purpose) if part)
            suffix = f" {identity}" if identity else ""
            values.append(f"{label} {role}{suffix}".strip())
        audio_label = str(item.get("audio_input_label") or "").strip()
        if audio_label:
            audio_purpose = str(item.get("audio_purpose") or "audio_reference").strip()
            audio_name = str(item.get("audio_name") or item.get("name") or "").strip()
            suffix = f" {audio_name}" if audio_name else ""
            values.append(f"{audio_label} audio:{audio_purpose}{suffix}".strip())
    return values


def media_profile_sources(
    *,
    project,
    shot=None,
    shots: list | None = None,
    extra_direction: str = "",
    overall_soundscape: str = "",
    non_diegetic_music: str = "",
    include_audio: bool = True,
    effective_durations_ms: dict[str, int] | None = None,
    reference_bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build typed media facts; never assemble a model prompt.

    Every user-configurable semantic value enters the final request only when a
    published PromptProfile selects this allow-listed source.  Dialogue,
    delivery and voiceover are intentionally separate facts, and reference
    facts come from the already-normalized binding contract rather than the
    shot's untyped asset pool.
    """

    selected_shots = list(shots or ([shot] if shot is not None else []))
    if not selected_shots:
        raise PromptProfileError("media profile sources 至少需要一个分镜")
    if any(item.episode.project_id != project.id for item in selected_shots):
        raise PromptProfileError("media profile sources 的分镜不属于当前项目")
    effective_durations_ms = dict(effective_durations_ms or {})
    reference_bindings = list(reference_bindings or [])
    style = getattr(project, "style", None)

    def shot_value(item, field_key: str, source: Any) -> str:
        return _translated_media_value(
            project,
            scope_type="shot",
            scope_id=item.id,
            field_key=field_key,
            source=source,
        )

    def scene_value(item, field_key: str, source: Any) -> str:
        scene = getattr(item, "scene", None)
        if scene is None:
            return ""
        return _translated_media_value(
            project,
            scope_type="scene",
            scope_id=scene.id,
            field_key=field_key,
            source=source,
        )

    titles = [str(item.title or "").strip() for item in selected_shots]
    narratives = [shot_value(item, "shot.narrative", item.narrative) for item in selected_shots]
    details = [
        shot_value(item, "shot.detailed_description", item.detailed_description)
        for item in selected_shots
    ]
    cameras = [shot_value(item, "shot.camera", item.camera) for item in selected_shots]
    actions = [shot_value(item, "shot.action", item.action) for item in selected_shots]
    sound_effects = [
        shot_value(item, "shot.sound_effects", item.sound_effects)
        for item in selected_shots
    ]
    music = [shot_value(item, "shot.music", item.music) for item in selected_shots]
    lighting = [
        scene_value(item, "scene.lighting", getattr(item.scene, "lighting", ""))
        if item.scene else ""
        for item in selected_shots
    ]
    colors = [
        scene_value(item, "scene.color_palette", getattr(item.scene, "color_palette", ""))
        if item.scene else ""
        for item in selected_shots
    ]
    environments = []
    for item in selected_shots:
        scene = item.scene
        environments.append("; ".join(filter(None, (
            scene_value(item, "scene.visual_identity", getattr(scene, "visual_identity", ""))
            if scene else "",
            scene_value(item, "scene.location", getattr(scene, "location", ""))
            if scene else "",
            scene_value(item, "scene.time_of_day", getattr(scene, "time_of_day", ""))
            if scene else "",
        ))))

    on_camera_dialogue: list[str] = []
    dialogue_delivery: list[str] = []
    voiceover: list[str] = []
    for item in (selected_shots if include_audio else []):
        duration_ms = int(effective_durations_ms.get(str(item.id), item.duration_ms))
        try:
            dialogue = validate_dialogue_timing(item.dialogue or [], duration_ms=duration_ms)
        except DialogueContractError as exc:
            raise PromptProfileError(f"分镜 {item.number} 的 {exc}") from exc
        for index, entry in enumerate(dialogue):
            start_ms = int(entry.get("start_ms", 0))
            end_ms = int(entry.get("end_ms", duration_ms))
            identity = f"[Shot {item.number} {start_ms / 1000:.3f}-{end_ms / 1000:.3f}s] ({entry['speaker_id']})"
            authored = f"<d>[{project.language}] {entry['text']}</d>"
            delivery_source = str(entry.get("delivery") or "")
            delivery = _translated_media_value(
                project,
                scope_type="shot",
                scope_id=item.id,
                field_key=f"shot.dialogue.{index}.delivery",
                source=delivery_source,
            ) if delivery_source else ""
            if delivery:
                dialogue_delivery.append(f"{identity} {delivery}")
            if entry.get("voiceover") is True:
                voiceover.append(
                    f"{identity} off-screen voiceover: {authored}; visible on-screen speaker keeps lips closed"
                )
            else:
                on_camera_dialogue.append(f"{identity} on-camera dialogue: {authored}")

    negative_values: list[str] = []
    settings = project.production_settings if isinstance(project.production_settings, dict) else {}
    project_negative = str(settings.get("negative_prompt") or "")
    if project_negative:
        negative_values.append(_translated_media_value(
            project,
            scope_type="project",
            scope_id=project.id,
            field_key="project.production_settings.negative_prompt",
            source=project_negative,
        ))
    style_negative = str(getattr(style, "negative_prompt", "") or "") if style else ""
    translated_style_negative = ""
    if style_negative:
        translated_style_negative = _translated_media_value(
            project,
            scope_type="project",
            scope_id=project.id,
            field_key="style.negative_prompt",
            source=style_negative,
        )
        negative_values.append(translated_style_negative)
    shot_negatives: list[str] = []
    for item in selected_shots:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        value = str(metadata.get("negative_prompt") or "")
        translated = shot_value(item, "shot.negative_prompt", value) if value else ""
        shot_negatives.append(translated)
        if translated:
            negative_values.append(translated)
        for character in item.characters.all():
            source = str(getattr(character, "negative_constraints", "") or "")
            if source:
                negative_values.append(_translated_media_value(
                    project,
                    scope_type="character",
                    scope_id=character.id,
                    field_key="character.negative_constraints",
                    source=source,
                ))
        if item.scene:
            source = str(getattr(item.scene, "negative_constraints", "") or "")
            if source:
                negative_values.append(scene_value(item, "scene.negative_constraints", source))
        for prop in item.props.all():
            source = str(getattr(prop, "negative_constraints", "") or "")
            if source:
                negative_values.append(_translated_media_value(
                    project,
                    scope_type="prop",
                    scope_id=prop.id,
                    field_key="prop.negative_constraints",
                    source=source,
                ))
    negative_constraints = "; ".join(dict.fromkeys(
        value.strip() for value in negative_values if value.strip()
    ))

    subject_values: list[str] = []
    seen_subjects: set[tuple[str, str]] = set()
    for item in selected_shots:
        for character in item.characters.all():
            key = ("character", str(character.id))
            if key in seen_subjects:
                continue
            seen_subjects.add(key)
            canonical_name = _translated_media_value(
                project,
                scope_type="character",
                scope_id=character.id,
                field_key="character.name",
                source=getattr(character, "name", ""),
            )
            identity = _translated_media_value(
                project,
                scope_type="character",
                scope_id=character.id,
                field_key="character.visual_identity",
                source=getattr(character, "visual_identity", ""),
            )
            wardrobe = _translated_media_value(
                project,
                scope_type="character",
                scope_id=character.id,
                field_key="character.wardrobe",
                source=getattr(character, "wardrobe", ""),
            )
            subject_values.append(
                ": ".join(filter(None, (canonical_name, identity, wardrobe)))
            )
        for prop in item.props.all():
            key = ("prop", str(prop.id))
            if key in seen_subjects:
                continue
            seen_subjects.add(key)
            canonical_name = _translated_media_value(
                project,
                scope_type="prop",
                scope_id=prop.id,
                field_key="prop.name",
                source=getattr(prop, "name", ""),
            )
            identity = _translated_media_value(
                project,
                scope_type="prop",
                scope_id=prop.id,
                field_key="prop.visual_identity",
                source=getattr(prop, "visual_identity", ""),
            )
            subject_values.append(": ".join(filter(None, (canonical_name, identity))))

    style_visual = str(getattr(style, "visual_prompt", "") or "") if style else ""
    translated_style = _translated_media_value(
        project,
        scope_type="project",
        scope_id=project.id,
        field_key="style.visual_prompt",
        source=style_visual,
    ) if style_visual else ""
    ambient_values = [
        scene_value(item, "scene.ambient_sound", getattr(item.scene, "ambient_sound", ""))
        if item.scene else ""
        for item in selected_shots
    ]
    soundscape = str(overall_soundscape or "").strip() or "; ".join(dict.fromkeys(
        value for value in [*ambient_values, *sound_effects] if value
    ))
    bgm = str(non_diegetic_music or "").strip() or _joined_shot_values(selected_shots, music)
    durations = [
        int(effective_durations_ms.get(str(item.id), item.duration_ms))
        for item in selected_shots
    ]

    return {
        "project.title": project.title,
        "project.logline": project.logline,
        "project.master_script": project.master_script,
        "project.language": project.language,
        "project.aspect_ratio": project.aspect_ratio,
        "style.name": getattr(style, "name", "") if style else "",
        "style.description": getattr(style, "description", "") if style else "",
        "style.visual_prompt": translated_style,
        "style.negative_prompt": translated_style_negative,
        "shot.title": _joined_shot_values(selected_shots, titles),
        "shot.narrative": _joined_shot_values(selected_shots, narratives),
        "shot.detailed_description": _joined_shot_values(selected_shots, details),
        "shot.camera": _joined_shot_values(selected_shots, cameras),
        "shot.action": _joined_shot_values(selected_shots, actions),
        "shot.dialogue": "\n".join(on_camera_dialogue) if include_audio else "",
        "shot.sound_effects": _joined_shot_values(selected_shots, sound_effects) if include_audio else "",
        "shot.music": _joined_shot_values(selected_shots, music) if include_audio else "",
        "shot.negative_prompt": _joined_shot_values(selected_shots, shot_negatives),
        "request.theme": project.logline or project.title,
        "request.lighting": _joined_shot_values(selected_shots, lighting),
        "request.environment": _joined_shot_values(selected_shots, environments),
        "request.action": _joined_shot_values(selected_shots, actions),
        "request.camera": _joined_shot_values(selected_shots, cameras),
        "request.style": translated_style,
        "request.language": project.language,
        "request.text": str(extra_direction or "").strip(),
        "request.bgm_style": bgm if include_audio else "",
        "request.dialogue_delivery": "\n".join(dialogue_delivery) if include_audio else "",
        "request.negative_constraints": negative_constraints,
        "request.subject": "; ".join(value for value in subject_values if value),
        "request.composition": _joined_shot_values(selected_shots, cameras),
        "request.color_palette": _joined_shot_values(selected_shots, colors),
        "request.motion": _joined_shot_values(selected_shots, actions),
        "request.duration": sum(durations) / 1000,
        "request.aspect_ratio": project.aspect_ratio,
        "request.sound_effects": soundscape if include_audio else "",
        "request.voiceover": "\n".join(voiceover) if include_audio else "",
        "request.reference_assets": _reference_source_items(reference_bindings),
    }


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
