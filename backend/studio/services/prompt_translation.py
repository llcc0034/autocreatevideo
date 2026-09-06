"""Review-gated translation for prompt-impacting fields.

All model calls use an explicitly selected, published ``prompt.normalize``
deployment. This module owns source/provenance validation and materialization;
it does not install models or launch a parallel local inference process.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable
import unicodedata

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import (
    CharacterBible,
    GenerationAttempt,
    PromptFieldTranslation,
    PropBible,
    SceneBible,
    SeriesProject,
    Shot,
)
from studio.services.prompt_normalization_contract import (
    PROMPT_NORMALIZE_OPERATION,
    PromptNormalizationContractError,
    deterministic_translate_only_source_result,
    normalization_text_max_characters,
    target_generation_rule_for_request,
    validate_target_contribution_text,
    validate_english_semantic_text,
)
from studio.services.content_contracts import translation_promotion_max_entries
from studio.services.prompt_generation_sources import (
    PROMPT_GENERATION_SOURCE_KEYS_BY_SCOPE,
)


_PROTECTED_BLOCK_RE = re.compile(
    r"<(d|lyrics|screen_text)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_PROTECTED_BLOCK_TAG_RE = re.compile(
    r"</?(?:d|lyrics|screen_text)\b[^>]*>",
    flags=re.IGNORECASE,
)
_PROTECTED_TOKEN_RE = re.compile(r"__AUTOPROMPT_LITERAL_[0-9A-F]{12}_[0-9]{4}__")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
TRANSLATION_CONTEXT_VERSION = 2
PROMPT_NORMALIZATION_EXECUTION_PROVIDER = "provider"
PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC = "server_deterministic"
PROMPT_NORMALIZATION_LOCAL_SUBMISSION_SEMANTICS = (
    "server_deterministic_no_submission"
)


class CanonicalTargetCollisionError(ValueError):
    """Two authoritative Bible entities resolve to one canonical identity."""

    code = "canonical_target_collision"

    def __init__(self, collisions: list[dict]):
        super().__init__("canonical English targets must be unique across Bible entities")
        self.collisions = collisions

ALLOWED_FIELD_KEYS = {
    scope_type: set(field_keys)
    for scope_type, field_keys in PROMPT_GENERATION_SOURCE_KEYS_BY_SCOPE.items()
}
_DIALOGUE_DELIVERY_FIELD_RE = re.compile(r"^shot\.dialogue\.([0-9]+)\.delivery$")


def field_key_allowed(scope_type: str, field_key: str) -> bool:
    if field_key in ALLOWED_FIELD_KEYS.get(scope_type, set()):
        return True
    return bool(
        scope_type == PromptFieldTranslation.ScopeType.SHOT
        and _DIALOGUE_DELIVERY_FIELD_RE.fullmatch(field_key)
    )


class PromptTranslationError(RuntimeError):
    """Safe, persisted description of one local-translation failure."""

    def __init__(
        self, message: str, *, code: str = "translation_failed",
        metadata: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "translation_failed")
        self.metadata = dict(metadata or {})


class TranslationValidationError(PromptTranslationError):
    pass


_SAFE_RUNTIME_ERROR_MESSAGES = {
    "visual_rules_validation_failed": "本地模型候选经一次确定性修复后仍未通过全剧视觉规则校验，已拒绝采用。",
    "visual_rules_runtime_failed": "本地模型未能生成可用的全剧视觉规则候选，请稍后重试。",
    "residual_cjk_after_repair": "英文候选经一次确定性修复后仍含中文字符，已拒绝采用；请重试或人工填写英文译文。",
    "semantic_guard_failed": "英文候选未通过专名、性别或导演术语校验，已拒绝采用。",
    "protected_token_mismatch": "英文候选改变了受保护的对白、歌词或屏显占位符，已拒绝采用。",
    "invalid_model_output": "本地模型未返回文本译文。",
    "empty_translation": "本地模型返回了空译文。",
    "invalid_repair_output": "本地模型的修复结果不是有效文本。",
    "empty_repair_output": "本地模型的修复结果为空。",
    "translation_failed": "该字段翻译失败；中文原文未被覆盖。",
}


def _safe_runtime_error(row: dict) -> PromptTranslationError:
    code = str(row.get("error_code") or "translation_failed")
    if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
        code = "translation_failed"
    raw_details = row.get("error_details")
    details = raw_details if isinstance(raw_details, dict) else {}
    residual = details.get("residual_cjk")
    safe_residual = [
        str(value)[:32] for value in residual[:8]
        if isinstance(value, str) and _CJK_RE.search(value)
    ] if isinstance(residual, list) else []
    metadata = {
        "error_code": code,
        "elapsed_ms": row.get("elapsed_ms"),
        "repair_attempted": bool(row.get("repair_attempted")),
        "repair_reason_code": str(row.get("repair_reason_code") or ""),
    }
    if safe_residual:
        metadata["residual_cjk"] = safe_residual
    return PromptTranslationError(
        _SAFE_RUNTIME_ERROR_MESSAGES.get(code, _SAFE_RUNTIME_ERROR_MESSAGES["translation_failed"]),
        code=code,
        metadata=metadata,
    )


@dataclass(frozen=True)
class TranslationResult:
    text: str
    provider: str
    model: str
    metadata: dict


def source_text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_identity_mappings(project: SeriesProject) -> dict[str, str]:
    """Return the project's current canonical source-to-target terminology."""

    mappings: dict[str, str] = {}
    configured_glossary = (project.production_settings or {}).get(
        "translation_glossary", {},
    )
    if isinstance(configured_glossary, dict):
        mappings.update({
            str(source): str(target)
            for source, target in configured_glossary.items()
            if str(source).strip() and str(target).strip()
        })
    for character in project.characters.all():
        mappings.setdefault(character.name, character.stable_speaker_id)
    for scene in project.scenes.all():
        mappings.setdefault(scene.name, f"scene:{scene.slug}")
    for prop in project.props.all():
        mappings.setdefault(prop.name, f"prop:{prop.slug}")
    return mappings


def canonical_target_collision_inputs(
    project: SeriesProject, *, production_settings: dict | None = None,
) -> list[dict]:
    """Return the exact Bible/glossary rows considered by collision checks."""

    settings_value = (
        production_settings
        if production_settings is not None
        else (project.production_settings or {})
    )
    raw_glossary = (
        settings_value.get("translation_glossary", {})
        if isinstance(settings_value, dict) else {}
    )
    if not isinstance(raw_glossary, dict):
        return []
    glossary = {
        str(source).strip(): str(target).strip()
        for source, target in raw_glossary.items()
        if str(source).strip() and str(target).strip()
    }
    inputs = []
    for scope_type, model in (
        (PromptFieldTranslation.ScopeType.CHARACTER, CharacterBible),
        (PromptFieldTranslation.ScopeType.SCENE, SceneBible),
        (PromptFieldTranslation.ScopeType.PROP, PropBible),
    ):
        for entity in model.objects.filter(project=project).only("id", "name"):
            name = str(entity.name or "").strip()
            target = glossary.get(name, "")
            if not target:
                continue
            normalized_target = " ".join(
                unicodedata.normalize("NFKC", target).split()
            ).casefold()
            if not normalized_target:
                continue
            inputs.append({
                "scope_type": scope_type,
                "scope_id": str(entity.id),
                "name": name,
                "canonical_target": target,
                "normalized_target": normalized_target,
                "normalized_source": " ".join(
                    unicodedata.normalize("NFKC", name).split()
                ).casefold(),
            })
    return sorted(
        inputs,
        key=lambda item: (item["scope_type"], item["name"], item["scope_id"]),
    )


def canonical_target_collisions_from_inputs(inputs: Iterable[dict]) -> list[dict]:
    """Return public collision diagnostics from a frozen input collection."""

    grouped: dict[str, list[dict]] = {}
    for item in inputs:
        grouped.setdefault(str(item["normalized_target"]), []).append(dict(item))
    collisions = []
    for normalized, entities in sorted(grouped.items()):
        if len({item["normalized_source"] for item in entities}) < 2:
            continue
        public_entities = [{
            key: value for key, value in item.items()
            if key not in {"normalized_source", "normalized_target"}
        } for item in entities]
        collisions.append({
            "code": CanonicalTargetCollisionError.code,
            "normalized_target": normalized,
            "entities": sorted(
                public_entities,
                key=lambda item: (item["scope_type"], item["name"], item["scope_id"]),
            ),
        })
    return collisions


def canonical_target_collisions(
    project: SeriesProject, *, production_settings: dict | None = None,
) -> list[dict]:
    """Return cross-entity collisions in explicit canonical-English mappings.

    Only current authoritative Character/Scene/Prop Bible names participate.
    Comparison is Unicode NFKC, whitespace-normalised and case-insensitive, so
    cosmetic spelling differences cannot create two identities that downstream
    prompts treat as the same proper name.
    """

    return canonical_target_collisions_from_inputs(
        canonical_target_collision_inputs(
            project, production_settings=production_settings,
        ),
    )


def ensure_no_canonical_target_collisions(
    project: SeriesProject, *, production_settings: dict | None = None,
) -> None:
    collisions = canonical_target_collisions(
        project, production_settings=production_settings,
    )
    if collisions:
        raise CanonicalTargetCollisionError(collisions)


def relevant_canonical_identity_mappings(
    project: SeriesProject, source_text: str,
) -> dict[str, str]:
    """Keep only terminology that can influence this exact source field.

    The previous context hashed every Bible/glossary entry. Adding an unrelated
    prop therefore invalidated already-confirmed character translations even
    though neither their source nor the required terminology changed.
    """

    source = str(source_text or "")
    return {
        term: canonical
        for term, canonical in canonical_identity_mappings(project).items()
        if term in source
    }


def missing_relevant_canonical_identity_mappings(
    project: SeriesProject, source_text: str, translated_text: str,
) -> list[dict[str, object]]:
    """Describe required canonical identities removed by a manual edit.

    Explicit glossary values are safe to show to the operator.  Stable
    character/scene/prop identifiers remain an internal fallback; for those we
    expose only the Chinese source term and require the operator to configure a
    human-readable canonical English name through the glossary workflow.
    """

    candidate = str(translated_text or "")
    production_settings = project.production_settings or {}
    raw_glossary = (
        production_settings.get("translation_glossary", {})
        if isinstance(production_settings, dict) else {}
    )
    glossary = raw_glossary if isinstance(raw_glossary, dict) else {}
    missing: list[dict[str, object]] = []
    for source_term, canonical_target in sorted(
        relevant_canonical_identity_mappings(project, source_text).items(),
        key=lambda item: item[0],
    ):
        if canonical_target in candidate:
            continue
        configured_target = str(glossary.get(source_term) or "").strip()
        item: dict[str, object] = {"source_term": source_term}
        if configured_target and configured_target == canonical_target:
            item["canonical_target"] = canonical_target
        else:
            item["requires_glossary"] = True
        missing.append(item)
    return missing


def build_translation_context(
    project: SeriesProject, *, scope_type: str, scope_id, field_key: str,
    source_text: str | None = None,
) -> str:
    """Build the canonical identity context that constrains local translation.

    Only mappings whose source term occurs in this authoritative field are
    hashed. A relevant canonical change still invalidates the candidate, while
    adding an unrelated Bible entity does not.
    """
    if source_text is None:
        source_text = resolve_current_source(
            project=project, scope_type=scope_type, scope_id=scope_id,
            field_key=field_key,
        )
    return json.dumps({
        "context_version": TRANSLATION_CONTEXT_VERSION,
        "purpose": "configured video prompt field",
        "scope": scope_type,
        "scope_id": str(scope_id or project.id),
        "field_key": field_key,
        "canonical_identity_mappings": relevant_canonical_identity_mappings(
            project, source_text,
        ),
        "rule": "When a source term is present, reuse its mapped target verbatim.",
    }, ensure_ascii=False, sort_keys=True)


def translation_context_hash(context: str) -> str:
    return source_text_hash(str(context or ""))


def translation_entry_context_current(
    entry: PromptFieldTranslation, *, project: SeriesProject | None = None,
    current_source: str | None = None, translated_text: str | None = None,
) -> bool:
    """Apply one context-currentness contract across review and compilation.

    Version-2 rows require an exact narrowed-context hash. Legacy rows did not
    record a version and hashed the entire project mapping. They can remain
    usable only when their source is unchanged and every *currently relevant*
    canonical target occurs verbatim in the reviewed translation. This safely
    tolerates unrelated additions without accepting a changed relevant name.
    """

    project = project or entry.project
    try:
        source = current_source if current_source is not None else resolve_current_source(
            project=project, scope_type=entry.scope_type, scope_id=entry.scope_id,
            field_key=entry.field_key,
        )
        if source_text_hash(str(source or "")) != entry.source_hash:
            return False
        current_context = build_translation_context(
            project, scope_type=entry.scope_type, scope_id=entry.scope_id,
            field_key=entry.field_key, source_text=str(source or ""),
        )
    except TranslationValidationError:
        return False

    metadata = entry.runtime_metadata or {}
    stored_hash = str(metadata.get("translation_context_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", stored_hash):
        return False
    candidate = str(
        entry.translated_text if translated_text is None else translated_text
    )
    relevant = relevant_canonical_identity_mappings(project, str(source or ""))
    canonical_targets_present = bool(candidate) and all(
        canonical in candidate for canonical in relevant.values()
    )
    if stored_hash == translation_context_hash(current_context):
        # A matching context proves which terminology was required, but a
        # human can still edit the candidate after generation.  Recheck the
        # actual text before confirmation/promotion so canonical names cannot
        # be deleted and then written into authoritative Shot fields.
        return canonical_targets_present
    if metadata.get("translation_context_version") is not None:
        return False

    return canonical_targets_present


def resolve_current_source(
    *, project: SeriesProject, scope_type: str, scope_id, field_key: str,
) -> str:
    """Resolve an allow-listed field and prove that it belongs to project."""
    if not field_key_allowed(scope_type, field_key):
        raise TranslationValidationError("不支持的翻译字段")
    if scope_type == PromptFieldTranslation.ScopeType.PROJECT:
        if scope_id and str(scope_id) != str(project.id):
            raise TranslationValidationError("project scope_id 与当前项目不一致")
        if field_key == "style.visual_prompt":
            return str(project.style.visual_prompt if project.style else "")
        if field_key == "style.negative_prompt":
            return str(project.style.negative_prompt if project.style else "")
        if field_key == "project.continuity_summary":
            return str(project.continuity_summary or "")
        setting_key = field_key.rsplit(".", 1)[-1]
        return str((project.production_settings or {}).get(setting_key, ""))

    models = {
        PromptFieldTranslation.ScopeType.CHARACTER: CharacterBible,
        PromptFieldTranslation.ScopeType.SCENE: SceneBible,
        PromptFieldTranslation.ScopeType.PROP: PropBible,
        PromptFieldTranslation.ScopeType.SHOT: Shot,
    }
    model = models.get(scope_type)
    if model is Shot:
        obj = model.objects.filter(pk=scope_id, episode__project=project).first()
    elif model:
        obj = model.objects.filter(pk=scope_id, project=project).first()
    else:
        obj = None
    if obj is None:
        raise TranslationValidationError("翻译字段作用域不存在或不属于当前项目")
    delivery_match = _DIALOGUE_DELIVERY_FIELD_RE.fullmatch(field_key)
    if delivery_match and isinstance(obj, Shot):
        index = int(delivery_match.group(1))
        dialogue = obj.dialogue if isinstance(obj.dialogue, list) else []
        if index >= len(dialogue) or not isinstance(dialogue[index], dict):
            raise TranslationValidationError("对白 delivery 索引已失效")
        return str(dialogue[index].get("delivery", "") or "")
    if isinstance(obj, Shot) and field_key == "shot.negative_prompt":
        return str((obj.metadata or {}).get("negative_prompt", "") or "")
    attribute = field_key.split(".", 1)[1]
    return str(getattr(obj, attribute, "") or "")


def _protect_authored_blocks(text: str) -> tuple[str, dict[str, str]]:
    blocks: dict[str, str] = {}
    salt = source_text_hash(text)[:12].upper()

    def replace(match: re.Match) -> str:
        token = f"__AUTOPROMPT_LITERAL_{salt}_{len(blocks) + 1:04d}__"
        blocks[token] = match.group(0)
        return token

    return _PROTECTED_BLOCK_RE.sub(replace, text), blocks


def _canonical_mappings_from_context(context: str) -> dict[str, str]:
    """Read the server-owned canonical map from one translation context.

    The context is also sent to the model as reference prose, but small models
    may paraphrase a required canonical target even after being told to copy it
    verbatim.  Extracting only this allow-listed object lets the transport
    protect each applicable source term as an exact token instead of relying
    on prompt compliance.
    """

    try:
        payload = json.loads(str(context or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    raw = payload.get("canonical_identity_mappings") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        source: target
        for source, target in raw.items()
        if isinstance(source, str) and source
        and isinstance(target, str) and target
    }


def _protect_translation_contract(
    text: str, context: str,
) -> tuple[str, dict[str, str]]:
    """Protect authored literals and canonical identities in source order."""

    protected, replacements = _protect_authored_blocks(text)
    mappings = _canonical_mappings_from_context(context)
    active_terms = sorted(
        (source for source in mappings if source in protected),
        key=len,
        reverse=True,
    )
    if active_terms:
        salt = source_text_hash(text)[:12].upper()
        pattern = re.compile("|".join(re.escape(source) for source in active_terms))

        def replace_canonical(match: re.Match) -> str:
            token = (
                f"__AUTOPROMPT_LITERAL_{salt}_"
                f"{len(replacements) + 1:04d}__"
            )
            replacements[token] = mappings[match.group(0)]
            return token

        protected = pattern.sub(replace_canonical, protected)

    # Authored blocks are protected before canonical terms, so rebuild the
    # dictionary in actual source order for the existing order guard.
    token_order = _PROTECTED_TOKEN_RE.findall(protected)
    replacements = {token: replacements[token] for token in token_order}
    return protected, replacements


def _restore_authored_blocks(candidate: str, blocks: dict[str, str]) -> str:
    for token in blocks:
        if candidate.count(token) != 1:
            raise TranslationValidationError(
                f"翻译结果修改、删除或复制了受保护占位符 {token}"
            )
    unknown = set(_PROTECTED_TOKEN_RE.findall(candidate)) - set(blocks)
    if unknown:
        raise TranslationValidationError("翻译结果新增了未知受保护占位符")
    if _PROTECTED_TOKEN_RE.findall(candidate) != list(blocks):
        raise TranslationValidationError("翻译结果改变了受保护原文块的顺序")
    restored = candidate
    for token, original in blocks.items():
        restored = restored.replace(token, original)
    if _PROTECTED_BLOCK_RE.findall(restored) != _PROTECTED_BLOCK_RE.findall(
        "".join(blocks.values())
    ):
        # The exact block strings are checked below; this shape check primarily
        # catches a model adding its own authored-text tag.
        original_blocks = list(blocks.values())
        restored_blocks = [match.group(0) for match in _PROTECTED_BLOCK_RE.finditer(restored)]
        if restored_blocks != original_blocks:
            raise TranslationValidationError("翻译结果改变了 <d>/<lyrics>/<screen_text> 原文块")
    return restored.strip()


def _validate_restored_generation_candidate(
    *,
    target: dict,
    request_payload: dict,
    request_id: str,
    translated_text: str,
) -> str:
    """Validate one human-visible field value against its frozen literal set."""

    blocks = target.get("protected_blocks")
    if not isinstance(blocks, dict) or any(
        not isinstance(token, str) or not isinstance(value, str)
        for token, value in blocks.items()
    ):
        raise TranslationValidationError("prompt.normalize 受保护原文快照无效")
    candidate = str(translated_text or "").strip()
    authored_blocks = {
        token: value
        for token, value in blocks.items()
        if _PROTECTED_BLOCK_RE.fullmatch(value)
    }
    expected_values = list(authored_blocks.values())
    unique_expected_values = list(dict.fromkeys(expected_values))
    if any(
        candidate.count(value) != expected_values.count(value)
        for value in unique_expected_values
    ):
        raise TranslationValidationError(
            "翻译结果改变了 <d>/<lyrics>/<screen_text> 原文块",
        )
    protected_parts: list[str] = []
    cursor = 0
    for token, value in authored_blocks.items():
        position = candidate.find(value, cursor)
        if position < 0:
            raise TranslationValidationError(
                "翻译结果改变了受保护原文块的顺序",
            )
        protected_parts.extend((candidate[cursor:position], token))
        cursor = position + len(value)
    protected_parts.append(candidate[cursor:])
    protected_candidate = "".join(protected_parts)
    residual = _PROTECTED_BLOCK_RE.sub("", candidate)
    if (
        _PROTECTED_BLOCK_TAG_RE.search(residual)
        or _PROTECTED_TOKEN_RE.search(candidate)
    ):
        raise TranslationValidationError(
            "翻译结果改变了 <d>/<lyrics>/<screen_text> 原文块",
        )
    restored = _restore_authored_blocks(protected_candidate, authored_blocks)
    if restored != candidate:
        raise TranslationValidationError("prompt.normalize 受保护原文恢复不一致")
    validate_target_contribution_text(
        request_payload,
        request_id=request_id,
        director_text=candidate,
    )
    return candidate


def prepare_prompt_normalization_item(
    *,
    project: SeriesProject,
    scope_type: str,
    scope_id,
    field_key: str,
    source_text: str,
    label: str,
    request_id: str,
) -> tuple[dict, dict]:
    """Build one provider item and its server-only approval materialization."""

    source = str(source_text or "")
    if not source.strip():
        raise TranslationValidationError("source_text 不能为空")
    current = resolve_current_source(
        project=project,
        scope_type=scope_type,
        scope_id=scope_id,
        field_key=field_key,
    )
    if current != source:
        raise TranslationValidationError("prompt.normalize source 已发生漂移")
    context = build_translation_context(
        project,
        scope_type=scope_type,
        scope_id=scope_id,
        field_key=field_key,
        source_text=source,
    )
    protected, blocks = _protect_translation_contract(source, context)
    item = {
        "request_id": str(request_id),
        "text": protected,
        "source_lang": "Chinese",
        "target_lang": "English",
        "field_key": field_key,
        "label": str(label or "")[:240],
        "context": context,
        "protected_tokens": list(blocks),
    }
    target = {
        "request_id": str(request_id),
        "scope_type": str(scope_type),
        "scope_id": str(scope_id),
        "field_key": str(field_key),
        "label": str(label or "")[:240],
        "source_text": source,
        "source_hash": source_text_hash(source),
        # The provider sees the protected form, while approval materialization
        # remains bound to the authored source above.  Keep both hashes so a
        # downstream field-generation contract can prove the exact model input
        # even when canonical Bible terms were replaced by opaque literals.
        "model_input_sha256": source_text_hash(protected),
        "context": context,
        "context_hash": translation_context_hash(context),
        # Kept only inside GenerationAttempt.request_snapshot. API serializers
        # never expose that snapshot or these authored literal replacements.
        "protected_blocks": blocks,
    }
    return item, target


def restore_prompt_normalization_candidate(
    *,
    target: dict,
    translated_text: str,
    request_payload: dict | None = None,
) -> str:
    blocks = target.get("protected_blocks")
    if not isinstance(blocks, dict) or any(
        not isinstance(token, str) or not isinstance(value, str)
        for token, value in blocks.items()
    ):
        raise TranslationValidationError("prompt.normalize 受保护原文快照无效")
    restored = _restore_authored_blocks(str(translated_text or ""), blocks)
    if isinstance(request_payload, dict):
        request_id = str(target.get("request_id") or "")
        if target_generation_rule_for_request(
            request_payload,
            request_id=request_id,
        ) is not None:
            return _validate_restored_generation_candidate(
                target=target,
                request_payload=request_payload,
                request_id=request_id,
                translated_text=restored,
            )
    return validate_manual_translation(str(target.get("source_text") or ""), restored)


def deterministic_prompt_normalization_materialization(
    *,
    item: dict,
    target: dict,
    request_payload: dict,
) -> dict | None:
    """Return a signed local materialization when the source needs no model.

    The frozen source is projected through the same provider placeholder and
    output validators used for remote responses.  Only ``translate_only``
    input that already satisfies the configured target-language, character,
    field, and protected-token contracts qualifies.  Contradictory frozen
    structure fails closed; source that still needs translation returns
    ``None`` and remains on the configured Provider route.
    """

    if not isinstance(item, dict) or not isinstance(target, dict):
        raise TranslationValidationError(
            "prompt.normalize 本地物化输入必须是对象",
        )
    if not isinstance(request_payload, dict):
        raise TranslationValidationError(
            "prompt.normalize 本地物化缺少冻结请求合同",
        )
    request_id = str(item.get("request_id") or "")
    if not request_id or request_id != str(target.get("request_id") or ""):
        raise TranslationValidationError(
            "prompt.normalize 本地物化 request_id 不一致",
        )
    rule = target_generation_rule_for_request(
        request_payload,
        request_id=request_id,
    )
    mode = str((rule or {}).get("generation_mode") or "translate_only")
    if mode != "translate_only":
        return None

    blocks = target.get("protected_blocks")
    if not isinstance(blocks, dict) or any(
        not isinstance(token, str)
        or _PROTECTED_TOKEN_RE.fullmatch(token) is None
        or not isinstance(value, str)
        or not value
        for token, value in blocks.items()
    ):
        raise TranslationValidationError(
            "prompt.normalize 受保护原文快照无效",
        )
    expected_tokens = list(blocks)
    protected_tokens = item.get("protected_tokens")
    if protected_tokens != expected_tokens:
        raise TranslationValidationError(
            "prompt.normalize 本地物化 protected_tokens 已漂移",
        )
    protected_text = item.get("text")
    if not isinstance(protected_text, str):
        raise TranslationValidationError(
            "prompt.normalize 本地物化 text 无效",
        )
    token_order = _PROTECTED_TOKEN_RE.findall(protected_text)
    if token_order != expected_tokens or any(
        protected_text.count(token) != 1 for token in expected_tokens
    ):
        raise TranslationValidationError(
            "prompt.normalize 本地物化 token 顺序或数量已漂移",
        )
    residual = protected_text
    for token in expected_tokens:
        residual = residual.replace(token, "")
    deterministic_result = deterministic_translate_only_source_result(
        request_payload,
        request_id=request_id,
    )
    if deterministic_result is None:
        return None
    deterministic_candidate = str(deterministic_result["translation"])
    if deterministic_candidate != protected_text.strip():
        raise TranslationValidationError(
            "prompt.normalize 确定性候选与冻结模型输入不一致",
        )
    model_input_sha256 = source_text_hash(protected_text)
    if str(target.get("model_input_sha256") or "") != model_input_sha256:
        raise TranslationValidationError(
            "prompt.normalize 本地物化 model input hash 已漂移",
        )
    restored_candidate = restore_prompt_normalization_candidate(
        target=target,
        translated_text=deterministic_candidate,
        request_payload=request_payload,
    )
    materialization_kind = (
        "protected_tokens_only"
        if not residual.strip()
        else "source_already_satisfies_target_contract"
    )
    proof = {
        "schema_version": 1,
        "execution_kind": PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC,
        "materialization_kind": materialization_kind,
        "request_id": request_id,
        "model_input_sha256": model_input_sha256,
        "protected_blocks_sha256": source_text_hash(json.dumps(
            blocks,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )),
        "restored_candidate_sha256": source_text_hash(restored_candidate),
    }
    proof_sha256 = source_text_hash(json.dumps(
        proof,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ))
    return {
        "translated_text": deterministic_candidate,
        "restored_candidate": restored_candidate,
        "warnings": list(deterministic_result.get("warnings") or []),
        "proof": proof,
        "proof_sha256": proof_sha256,
    }


def _translation_history_item(entry: PromptFieldTranslation) -> dict:
    return {
        "revision": entry.revision,
        "source_text": entry.source_text,
        "translated_text": entry.translated_text,
        "status": entry.status,
        "provider": entry.provider,
        "model": entry.model,
        "error_message": entry.error_message,
        "runtime_metadata": entry.runtime_metadata or {},
        "origin_generation_attempt_id": (
            str(entry.origin_generation_attempt_id)
            if entry.origin_generation_attempt_id else None
        ),
        "confirmed_at": entry.confirmed_at.isoformat() if entry.confirmed_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


@transaction.atomic
def confirm_prompt_normalization_candidates(
    *,
    project: SeriesProject,
    attempt: GenerationAttempt | None,
    candidates: list[dict],
    user,
    provider: str,
    model: str,
    result_sha256: str,
    submission_semantics: str,
    manual_validation_fallback: bool = False,
    manual_no_submission: bool = False,
    frozen_input: dict | None = None,
    manual_review_origin: str = "",
    attempts_by_request_id: dict[str, GenerationAttempt] | None = None,
) -> list[PromptFieldTranslation]:
    """Atomically materialize approved candidates as confirmed overlays."""

    project = SeriesProject.objects.select_for_update().select_related("style").get(
        pk=project.pk,
    )
    maximum_candidates = translation_promotion_max_entries()
    if not isinstance(candidates, list) or len(candidates) > maximum_candidates:
        raise ValidationError("prompt.normalize candidates 数量无效")
    if attempt is not None and manual_no_submission:
        raise ValidationError("prompt.normalize 人工无提交模式不能绑定 GenerationAttempt")
    locked_attempt: GenerationAttempt | None = None
    locked_attempts_by_request_id: dict[str, GenerationAttempt] = {}
    if attempts_by_request_id is not None:
        if manual_no_submission or not isinstance(attempts_by_request_id, dict):
            raise ValidationError("prompt.normalize 批次 attempt 映射与人工无提交语义冲突")
        candidate_request_ids = {
            str(item.get("request_id") or "")
            for item in candidates
            if isinstance(item, dict)
        }
        if (
            "" in candidate_request_ids
            or set(attempts_by_request_id) != candidate_request_ids
            or any(value is None for value in attempts_by_request_id.values())
        ):
            raise ValidationError("prompt.normalize 批次 attempt 映射未完整覆盖候选")
        attempt_ids = {value.pk for value in attempts_by_request_id.values()}
        locked_by_id = {
            item.pk: item
            for item in GenerationAttempt.objects.select_for_update().select_related(
                "stage_run__production_run",
            ).filter(pk__in=attempt_ids)
        }
        if len(locked_by_id) != len(attempt_ids):
            raise ValidationError("prompt.normalize 批次 GenerationAttempt 缺失")
        for request_id, source_attempt in attempts_by_request_id.items():
            locked = locked_by_id[source_attempt.pk]
            if (
                locked.operation != PROMPT_NORMALIZE_OPERATION
                or locked.stage_run.production_run.project_id != project.id
            ):
                raise ValidationError("prompt.normalize 批次 GenerationAttempt 与项目不匹配")
            if manual_validation_fallback:
                if locked.status not in {
                    GenerationAttempt.Status.SUCCEEDED,
                    GenerationAttempt.Status.FAILED,
                }:
                    raise ValidationError("prompt.normalize 人工接管批次 attempt 状态无效")
                if (
                    locked.status == GenerationAttempt.Status.FAILED
                    and locked.error_code not in {
                        "prompt_normalize_contract_failed",
                        "provider_response_invalid",
                    }
                ):
                    raise ValidationError("prompt.normalize 人工接管失败批次类型无效")
            elif locked.status != GenerationAttempt.Status.SUCCEEDED:
                raise ValidationError("prompt.normalize 批次 GenerationAttempt 尚未成功")
            locked_attempts_by_request_id[request_id] = locked
        if manual_validation_fallback and not any(
            item.status == GenerationAttempt.Status.FAILED
            for item in locked_attempts_by_request_id.values()
        ):
            raise ValidationError("prompt.normalize 人工接管缺少失败批次")
        locked_attempt = (
            locked_by_id.get(attempt.pk) if attempt is not None else None
        )
        if not re.fullmatch(r"[0-9a-f]{64}", str(result_sha256 or "")):
            raise ValidationError("prompt.normalize 批次聚合证据 checksum 无效")
    elif attempt is not None:
        locked_attempt = GenerationAttempt.objects.select_for_update().select_related(
            "stage_run__production_run",
        ).get(pk=attempt.pk)
        if (
            locked_attempt.operation != PROMPT_NORMALIZE_OPERATION
            or locked_attempt.stage_run.production_run.project_id != project.id
        ):
            raise ValidationError("prompt.normalize GenerationAttempt 与项目不匹配")
        if manual_validation_fallback:
            if (
                locked_attempt.status != GenerationAttempt.Status.FAILED
                or locked_attempt.error_code not in {
                    "prompt_normalize_contract_failed",
                    "provider_response_invalid",
                }
            ):
                raise ValidationError("prompt.normalize 人工接管必须绑定已知的校验失败 attempt")
            if not re.fullmatch(r"[0-9a-f]{64}", str(result_sha256 or "")):
                raise ValidationError("prompt.normalize 人工接管证据 checksum 无效")
        else:
            if locked_attempt.status != GenerationAttempt.Status.SUCCEEDED:
                raise ValidationError("prompt.normalize GenerationAttempt 尚未成功")
            if locked_attempt.result_sha256 != str(result_sha256 or ""):
                raise ValidationError("prompt.normalize candidate result checksum 已漂移")
    elif manual_no_submission:
        if manual_validation_fallback:
            raise ValidationError("prompt.normalize 人工无提交与失败重试接管语义冲突")
        if submission_semantics != "human_only_no_submission":
            raise ValidationError("prompt.normalize 人工无提交语义无效")
        if manual_review_origin not in {
            "human_only_recipe",
            "human_only_external_disabled",
        }:
            raise ValidationError("prompt.normalize 人工无提交来源无效")
        if str(provider or "") or str(model or ""):
            raise ValidationError("prompt.normalize 人工无提交不能声明 Provider 或模型")
        if not re.fullmatch(r"[0-9a-f]{64}", str(result_sha256 or "")):
            raise ValidationError("prompt.normalize 人工无提交证据 checksum 无效")
    else:
        raise ValidationError("prompt.normalize 审批必须绑定提交或明确的人工无提交合同")
    request_snapshot = (
        frozen_input
        if attempts_by_request_id is not None
        else locked_attempt.request_snapshot
        if locked_attempt is not None
        else frozen_input
    )
    request_payload = (
        request_snapshot.get("payload")
        if isinstance(request_snapshot, dict)
        else None
    )
    if not isinstance(request_payload, dict):
        raise ValidationError("prompt.normalize attempt 缺少冻结请求合同")
    frozen_targets = (
        request_snapshot.get("targets")
        if isinstance(request_snapshot, dict)
        else None
    )
    if not isinstance(frozen_targets, list):
        raise ValidationError("prompt.normalize attempt 缺少冻结目标合同")
    frozen_target_by_request_id = {
        str(item.get("request_id") or ""): item
        for item in frozen_targets
        if isinstance(item, dict) and item.get("request_id")
    }
    if len(frozen_target_by_request_id) != len(frozen_targets):
        raise ValidationError("prompt.normalize 冻结目标 request_id 缺失或重复")
    # Freeze every source/context owner before checking hashes and writing any
    # overlay. This prevents a concurrent Bible/glossary/source edit from
    # landing between approval validation and the confirmed rows.
    list(CharacterBible.objects.select_for_update().filter(project=project).order_by("id"))
    list(SceneBible.objects.select_for_update().filter(project=project).order_by("id"))
    list(PropBible.objects.select_for_update().filter(project=project).order_by("id"))
    shot_ids = sorted({
        str(item.get("scope_id"))
        for item in candidates
        if isinstance(item, dict)
        and item.get("scope_type") == PromptFieldTranslation.ScopeType.SHOT
        and item.get("scope_id")
    })
    if shot_ids:
        list(Shot.objects.select_for_update().filter(
            pk__in=shot_ids,
            episode__project=project,
        ).order_by("id"))
    if project.style_id:
        type(project.style).objects.select_for_update().get(pk=project.style_id)
    seen: set[tuple[str, str, str]] = set()
    # Imported lazily because canonical_glossary itself reuses this module's
    # translation primitives.  There must still be one canonical-name
    # validator for both the standalone glossary flow and run review.
    from studio.services.canonical_glossary import validate_canonical_english

    reviewed: list[dict] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValidationError("prompt.normalize candidate 必须是对象")
        scope_type = str(candidate.get("scope_type") or "")
        scope_id = candidate.get("scope_id")
        field_key = str(candidate.get("field_key") or "")
        request_id = str(candidate.get("request_id") or "")
        frozen_target = frozen_target_by_request_id.get(request_id)
        if frozen_target is None:
            raise ValidationError("prompt.normalize candidate 不属于冻结目标")
        generation_rule = target_generation_rule_for_request(
            request_payload,
            request_id=request_id,
        )
        if generation_rule is not None:
            if (
                frozen_target.get("target_kind") != "prompt_profile_field"
                or str(frozen_target.get("shot_id") or "")
                != str(generation_rule.get("shot_id") or "")
                or str(frozen_target.get("profile_selection_sha256") or "")
                != str(generation_rule.get("profile_selection_sha256") or "")
                or str(frozen_target.get("profile_field_key") or "")
                != str(generation_rule.get("profile_field_key") or "")
                or str(frozen_target.get("profile_source_selector") or "")
                != str(generation_rule.get("profile_source_selector") or "")
                or str(frozen_target.get("generation_mode") or "")
                != str(generation_rule.get("generation_mode") or "")
                or str(frozen_target.get("generation_output_semantics") or "")
                != str(generation_rule.get("generation_output_semantics") or "")
                or str(frozen_target.get("generation_source_selector") or "")
                != str(generation_rule.get("generation_source_selector") or "")
            ):
                raise ValidationError("prompt.normalize Profile 字段目标与冻结合同不一致")
            scope_type = str(frozen_target.get("scope_type") or "")
            scope_id = frozen_target.get("scope_id")
            field_key = str(frozen_target.get("profile_field_key") or "")
            source_scope_type = str(
                frozen_target.get("generation_source_scope_type") or ""
            )
            source_scope_id = frozen_target.get("generation_source_scope_id")
            source_field_key = str(
                frozen_target.get("generation_source_field_key") or ""
            )
        else:
            source_scope_type = scope_type
            source_scope_id = scope_id
            source_field_key = field_key
        if (
            scope_type != str(candidate.get("scope_type") or "")
            or str(scope_id) != str(candidate.get("scope_id") or "")
            or field_key != str(candidate.get("field_key") or "")
        ):
            raise ValidationError("prompt.normalize candidate 目标身份与冻结输入不一致")
        identity = (scope_type, str(scope_id), field_key)
        if identity in seen:
            raise ValidationError("prompt.normalize candidate 目标重复")
        seen.add(identity)
        source = resolve_current_source(
            project=project,
            scope_type=source_scope_type,
            scope_id=source_scope_id,
            field_key=source_field_key,
        )
        frozen_source_hash = str(frozen_target.get("source_hash") or "")
        if (
            source_text_hash(source) != frozen_source_hash
            or str(candidate.get("source_hash") or "") != frozen_source_hash
        ):
            raise ValidationError("prompt.normalize 审批时 source 已漂移")
        context = build_translation_context(
            project,
            scope_type=source_scope_type,
            scope_id=source_scope_id,
            field_key=source_field_key,
            source_text=source,
        )
        context_hash = translation_context_hash(context)
        frozen_context_hash = str(frozen_target.get("context_hash") or "")
        if (
            context_hash != frozen_context_hash
            or str(candidate.get("context_hash") or "") != frozen_context_hash
        ):
            raise ValidationError("prompt.normalize 审批时术语上下文已漂移")
        try:
            if generation_rule is not None:
                translated = _validate_restored_generation_candidate(
                    target=frozen_target,
                    request_payload=request_payload,
                    request_id=request_id,
                    translated_text=str(candidate.get("translated_text") or ""),
                )
            else:
                translated = validate_manual_translation(
                    source,
                    str(candidate.get("translated_text") or ""),
                )
                if field_key.endswith(".name"):
                    translated = validate_canonical_english(translated)
        except (TranslationValidationError, PromptNormalizationContractError) as exc:
            raise ValidationError({
                "candidate_overrides": ValidationError(
                    f"request_id={request_id}: {exc}",
                    code="prompt_translation_candidate_invalid",
                ),
            }) from exc
        reviewed.append({
            "request_id": request_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "field_key": field_key,
            "source": source,
            "translated": translated,
            "review_context_hash": context_hash,
            "generation_rule": generation_rule,
            "frozen_target": frozen_target,
            "source_scope_type": source_scope_type,
            "source_scope_id": str(source_scope_id),
            "source_field_key": source_field_key,
        })

    # A generated batch without an explicit project glossary uses internal
    # stable identities (S1 / scene:<slug> / prop:<slug>).  The review UI must
    # let an operator replace those placeholders with human-readable canonical
    # English names.  Derive the authoritative glossary from the reviewed name
    # rows and persist it in the same transaction as every confirmed overlay.
    # If any later candidate fails, both the glossary and all overlays roll
    # back together.
    glossary_updates: dict[str, str] = {}
    identity_scopes = {
        PromptFieldTranslation.ScopeType.CHARACTER,
        PromptFieldTranslation.ScopeType.SCENE,
        PromptFieldTranslation.ScopeType.PROP,
    }
    for item in reviewed:
        if item["scope_type"] not in identity_scopes or not item["field_key"].endswith(".name"):
            continue
        source_name = str(item["source"] or "").strip()
        target_name = str(item["translated"] or "").strip()
        previous = glossary_updates.get(source_name)
        if previous is not None and previous != target_name:
            raise ValidationError("prompt.normalize 同一实体名称存在冲突的人工英文候选")
        glossary_updates[source_name] = target_name

    if glossary_updates:
        production_settings = dict(project.production_settings or {})
        raw_glossary = production_settings.get("translation_glossary", {})
        if not isinstance(raw_glossary, dict):
            raise ValidationError(
                "项目 translation_glossary 结构无效；为避免覆盖历史术语，审批已中止"
            )
        glossary = dict(raw_glossary)
        glossary.update(glossary_updates)
        production_settings["translation_glossary"] = glossary
        try:
            ensure_no_canonical_target_collisions(
                project,
                production_settings=production_settings,
            )
        except CanonicalTargetCollisionError as exc:
            raise ValidationError({
                "candidate_overrides": ValidationError(
                    "canonical English targets 存在冲突",
                    code=exc.code,
                    params={"collisions": exc.collisions},
                ),
            }) from exc
        if production_settings != (project.production_settings or {}):
            project.production_settings = production_settings
            project.save(update_fields=("production_settings", "updated_at"))

    materialized: list[PromptFieldTranslation] = []
    for item in reviewed:
        scope_type = item["scope_type"]
        scope_id = item["scope_id"]
        field_key = item["field_key"]
        source = item["source"]
        translated = item["translated"]
        origin_attempt = (
            locked_attempts_by_request_id.get(item["request_id"])
            if attempts_by_request_id is not None
            else locked_attempt
        )
        origin_execution_kind = str(
            (origin_attempt.request_snapshot or {}).get("execution_kind")
            if origin_attempt is not None
            and isinstance(origin_attempt.request_snapshot, dict)
            else ""
        )
        server_deterministic_origin = (
            origin_execution_kind
            == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
        )
        optional_generated_empty = bool(
            item["generation_rule"] is not None
            and item["generation_rule"].get("generation_required") is False
            and not translated
        )
        missing = (
            {}
            if optional_generated_empty
            else missing_relevant_canonical_identity_mappings(
                project, source, translated,
            )
        )
        if missing:
            raise ValidationError("prompt.normalize 候选缺少当前 canonical identity")
        context_hash = translation_context_hash(build_translation_context(
            project,
            scope_type=item["source_scope_type"],
            scope_id=item["source_scope_id"],
            field_key=item["source_field_key"],
            source_text=source,
        ))
        entry = PromptFieldTranslation.objects.select_for_update().filter(
            project=project,
            scope_type=scope_type,
            scope_id=scope_id,
            field_key=field_key,
        ).first()
        if entry is None:
            entry = PromptFieldTranslation(
                project=project,
                scope_type=scope_type,
                scope_id=scope_id,
                field_key=field_key,
                revision=1,
            )
        else:
            entry.revision_history = [
                *(entry.revision_history or []),
                _translation_history_item(entry),
            ]
            entry.revision += 1
        entry.source_text = source
        entry.source_hash = source_text_hash(source)
        entry.translated_text = translated
        entry.status = PromptFieldTranslation.Status.CONFIRMED
        entry.provider = "" if server_deterministic_origin else str(provider or "")[:80]
        entry.model = "" if server_deterministic_origin else str(model or "")[:200]
        entry.error_message = ""
        entry.runtime_metadata = {
            "schema_version": 1,
            "translation_context_hash": context_hash,
            "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            "review_context_hash": item["review_context_hash"],
            "generation_attempt_id": (
                str(origin_attempt.id) if origin_attempt is not None else None
            ),
            "result_sha256": (
                str(result_sha256)
                if manual_validation_fallback or manual_no_submission
                else str(result_sha256)
                if attempts_by_request_id is not None
                else locked_attempt.result_sha256
            ),
            "submission_semantics": (
                PROMPT_NORMALIZATION_LOCAL_SUBMISSION_SEMANTICS
                if server_deterministic_origin
                else str(submission_semantics or "")
            ),
            "review_origin": (
                "human_after_validation_retry_exhausted"
                if manual_validation_fallback
                else manual_review_origin
                if manual_no_submission
                else "server_deterministic_candidate"
                if server_deterministic_origin
                else "model_candidate"
            ),
            "execution_kind": (
                origin_execution_kind
                or PROMPT_NORMALIZATION_EXECUTION_PROVIDER
                if origin_attempt is not None
                else "manual_no_submission"
            ),
            **({
                "target_kind": "prompt_profile_field",
                "shot_id": str(item["frozen_target"].get("shot_id") or ""),
                "profile_selection_sha256": str(
                    item["frozen_target"].get("profile_selection_sha256") or ""
                ),
                "prompt_profile_version_id": str(
                    item["frozen_target"].get("prompt_profile_version_id") or ""
                ),
                "profile_field_key": field_key,
                "profile_source_selector": str(
                    item["frozen_target"].get("profile_source_selector") or ""
                ),
                "generation_mode": str(
                    item["frozen_target"].get("generation_mode") or ""
                ),
                "generation_output_semantics": str(
                    item["frozen_target"].get(
                        "generation_output_semantics",
                    ) or ""
                ),
                "generation_source_selector": str(
                    item["frozen_target"].get("generation_source_selector") or ""
                ),
                "generation_source_scope_type": item["source_scope_type"],
                "generation_source_scope_id": item["source_scope_id"],
                "generation_source_field_key": item["source_field_key"],
            } if item["generation_rule"] is not None else {}),
        }
        entry.origin_generation_attempt = origin_attempt
        entry.confirmed_by = user
        entry.confirmed_at = timezone.now()
        entry.save()
        materialized.append(entry)
    return materialized


def translate_text_once(
    text: str,
    *,
    source_lang: str = "Chinese",
    target_lang: str = "English",
    field_key: str = "",
    label: str = "",
    context: str = "",
    attempt: int = 1,
    model_deployment_id=None,
) -> TranslationResult:
    """Translate one field through one explicitly selected deployment."""
    from studio.services.prompt_normalization import invoke_prompt_normalization
    from studio.services.provider_registry import ProviderDriverError

    source = str(text or "")
    if not source.strip():
        raise TranslationValidationError("source_text 不能为空")
    if not model_deployment_id:
        raise TranslationValidationError(
            "model_deployment_id 不能为空",
            code="model_deployment_required",
        )
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise TranslationValidationError("attempt 必须是正整数")

    protected_text, blocks = _protect_translation_contract(source, context)
    try:
        payload = invoke_prompt_normalization(
            deployment_id=model_deployment_id,
            payload={
                "schema_version": 1,
                "items": [{
                    "request_id": source_text_hash(source)[:24],
                    "text": protected_text,
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "field_key": field_key,
                    "label": label,
                    "context": context,
                    "protected_tokens": list(blocks),
                }],
            },
        )
    except ProviderDriverError as exc:
        raise PromptTranslationError(
            str(exc),
            code=exc.code,
            metadata={
                **(getattr(exc, "metadata", {}) or {}),
                "model_deployment_id": str(model_deployment_id),
                "submission_uncertain": bool(exc.submission_uncertain),
            },
        ) from exc
    rows = payload.get("items")
    row = rows[0] if isinstance(rows, list) and rows else {}
    candidate = row.get("translation") if isinstance(row, dict) else None
    if not isinstance(candidate, str) or not candidate.strip():
        raise PromptTranslationError("所选模型通道没有返回有效译文")
    restored = _restore_authored_blocks(candidate, blocks)
    restored = validate_manual_translation(source, restored)
    payload_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return TranslationResult(
        text=restored,
        provider=str(payload_metadata.get("provider") or payload.get("provider") or ""),
        model=str(payload.get("model") or payload_metadata.get("model") or ""),
        metadata={
            **payload_metadata,
            "attempt": attempt,
            "protected_block_count": len(blocks),
            "translation_context_hash": translation_context_hash(context),
            "translation_context_version": TRANSLATION_CONTEXT_VERSION,
        },
    )


def _frozen_translation_batch_size(model_deployment_id) -> int:
    """Read the published profile contract before splitting a provider call."""
    from studio.models import ModelDeployment
    from studio.services.model_profile_contracts import verified_model_profile_specification
    from studio.services.prompt_normalization_contract import (
        prompt_normalization_policy_from_specification,
    )

    deployment = ModelDeployment.objects.select_related("profile_version").filter(
        pk=model_deployment_id,
    ).first()
    if deployment is None:
        raise TranslationValidationError(
            "model_deployment 不存在", code="model_deployment_not_found",
        )
    policy = prompt_normalization_policy_from_specification(
        verified_model_profile_specification(deployment.profile_version),
        required=True,
    )
    assert policy is not None
    return int(policy["max_items_per_batch"])


def translate_texts_once(
    texts: list[str], *, source_lang: str = "Chinese", target_lang: str = "English",
    field_keys: list[str] | None = None,
    labels: list[str] | None = None,
    contexts: list[str] | None = None,
    model_deployment_id=None,
) -> list[TranslationResult | PromptTranslationError]:
    """Translate multiple fields through one explicitly selected deployment."""
    from studio.services.prompt_normalization import invoke_prompt_normalization
    from studio.services.provider_registry import ProviderDriverError

    if not texts:
        return []
    if not model_deployment_id:
        raise TranslationValidationError(
            "model_deployment_id 不能为空",
            code="model_deployment_required",
        )
    prepared = []
    blocks_by_id: dict[str, dict[str, str]] = {}
    sources_by_id: dict[str, str] = {}
    field_keys = field_keys or [""] * len(texts)
    labels = labels or [""] * len(texts)
    contexts = contexts or [""] * len(texts)
    if not (len(texts) == len(field_keys) == len(labels) == len(contexts)):
        raise TranslationValidationError("批量翻译 hints 数量与 texts 不一致")
    for index, raw in enumerate(texts):
        source = str(raw or "")
        if not source.strip():
            prepared.append(None)
            continue
        request_id = f"{index:04d}-{source_text_hash(source)[:20]}"
        protected, blocks = _protect_translation_contract(source, contexts[index])
        blocks_by_id[request_id] = blocks
        sources_by_id[request_id] = source
        prepared.append({
            "request_id": request_id,
            "text": protected,
            "protected_tokens": list(blocks),
            "source_lang": source_lang,
            "target_lang": target_lang,
            "field_key": field_keys[index],
            "label": labels[index],
            "context": contexts[index],
        })
    runtime_items = [item for item in prepared if item is not None]
    if not runtime_items:
        return [TranslationValidationError("source_text 不能为空") for _ in texts]
    batch_size = _frozen_translation_batch_size(model_deployment_id)
    by_id: dict[str, dict] = {}
    metadata_by_id: dict[str, dict] = {}
    for offset in range(0, len(runtime_items), batch_size):
        batch = runtime_items[offset:offset + batch_size]
        try:
            payload = invoke_prompt_normalization(
                deployment_id=model_deployment_id,
                payload={"schema_version": 1, "items": batch},
            )
        except ProviderDriverError as exc:
            raise PromptTranslationError(
                str(exc),
                code=exc.code,
                metadata={
                    **(getattr(exc, "metadata", {}) or {}),
                    "model_deployment_id": str(model_deployment_id),
                    "submission_uncertain": bool(exc.submission_uncertain),
                },
            ) from exc
        rows = payload.get("items")
        if not isinstance(rows, list):
            raise PromptTranslationError("批量翻译进程没有返回 items")
        batch_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        for row in rows:
            if isinstance(row, dict) and str(row.get("request_id") or ""):
                request_id = str(row["request_id"])
                by_id[request_id] = row
                metadata_by_id[request_id] = batch_metadata
    results: list[TranslationResult | PromptTranslationError] = []
    for item in prepared:
        if item is None:
            results.append(TranslationValidationError("source_text 不能为空"))
            continue
        row = by_id.get(item["request_id"])
        if not row or row.get("ok") is not True:
            results.append(_safe_runtime_error(row or {}))
            continue
        candidate = row.get("translation")
        if not isinstance(candidate, str) or not candidate.strip():
            results.append(PromptTranslationError("该字段没有返回有效译文"))
            continue
        try:
            restored = _restore_authored_blocks(
                candidate, blocks_by_id[item["request_id"]],
            )
            restored = validate_manual_translation(
                sources_by_id[item["request_id"]], restored,
            )
        except PromptTranslationError as exc:
            results.append(exc)
            continue
        payload_metadata = metadata_by_id.get(item["request_id"], {})
        results.append(TranslationResult(
            text=restored,
            provider=str(payload_metadata.get("provider") or payload.get("provider") or ""),
            model=str(payload.get("model") or payload_metadata.get("model") or ""),
            metadata={
                **payload_metadata,
                "protected_block_count": len(blocks_by_id[item["request_id"]]),
                "translation_context_hash": translation_context_hash(
                    contexts[int(item["request_id"].split("-", 1)[0])]
                ),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            },
        ))
    return results


def validate_manual_translation(source: str, translated: str) -> str:
    """Validate an edited candidate without invoking the model."""
    protected, blocks = _protect_authored_blocks(source)
    max_characters = normalization_text_max_characters()
    if len(translated) > max_characters:
        raise TranslationValidationError(
            f"英文译文不能超过当前安全边界 {max_characters} 个字符"
        )
    candidate = translated
    for token, original in blocks.items():
        candidate = candidate.replace(original, token)
    # Every original protected block must occur once and in order. Replacing
    # exact originals above makes the same fail-closed validator reusable.
    restored = _restore_authored_blocks(candidate, blocks)
    director_text = _PROTECTED_BLOCK_RE.sub("", restored)
    source_director_text = protected
    for token in blocks:
        source_director_text = source_director_text.replace(token, "")
    try:
        validate_english_semantic_text(
            source_director_text,
            director_text,
            field_label="人工英文定稿",
        )
    except PromptNormalizationContractError as exc:
        raise TranslationValidationError(str(exc)) from exc
    return restored


def confirmed_translation_with_snapshot(
    *, project_id, scope_type: str, scope_id, field_key: str, source_text: str,
) -> tuple[str, dict]:
    """Resolve one confirmed overlay and return the exact adopted evidence."""

    source = str(source_text or "")
    source_hash = source_text_hash(source)
    base_snapshot = {
        "scope_type": str(scope_type),
        "scope_id": str(scope_id),
        "field_key": str(field_key),
        "source_text": source,
        "source_hash": source_hash,
        "translation": None,
    }
    if not source:
        return source, {**base_snapshot, "effective_text": source}
    entry = PromptFieldTranslation.objects.filter(
        project_id=project_id,
        scope_type=scope_type,
        scope_id=scope_id,
        field_key=field_key,
        source_hash=source_hash,
        status=PromptFieldTranslation.Status.CONFIRMED,
    ).only(
        "id", "scope_type", "scope_id", "field_key", "source_hash",
        "translated_text", "runtime_metadata", "revision",
    ).first()
    if entry is None:
        return source, {**base_snapshot, "effective_text": source}
    try:
        project = SeriesProject.objects.get(pk=project_id)
    except SeriesProject.DoesNotExist:
        return source, {**base_snapshot, "effective_text": source}
    if not translation_entry_context_current(
        entry, project=project, current_source=source,
    ):
        return source, {**base_snapshot, "effective_text": source}
    effective = entry.translated_text or source
    metadata = entry.runtime_metadata if isinstance(entry.runtime_metadata, dict) else {}
    return effective, {
        **base_snapshot,
        "effective_text": effective,
        "translation": {
            "id": str(entry.id),
            "revision": entry.revision,
            "source_hash": entry.source_hash,
            "translated_text": entry.translated_text,
            "translated_text_sha256": source_text_hash(entry.translated_text),
            "context_hash": str(metadata.get("translation_context_hash") or ""),
            "context_version": metadata.get("translation_context_version"),
        },
    }


def confirmed_translation(
    *, project_id, scope_type: str, scope_id, field_key: str, source_text: str,
) -> str:
    value, _snapshot = confirmed_translation_with_snapshot(
        project_id=project_id,
        scope_type=scope_type,
        scope_id=scope_id,
        field_key=field_key,
        source_text=source_text,
    )
    return value


def confirmed_translation_fingerprint(
    *, project_id, scope_ids: Iterable,
) -> str:
    rows = PromptFieldTranslation.objects.filter(
        project_id=project_id,
        scope_id__in=list(scope_ids),
        status=PromptFieldTranslation.Status.CONFIRMED,
    ).values(
        "scope_type", "scope_id", "field_key", "source_hash", "translated_text", "revision",
    ).order_by("scope_type", "scope_id", "field_key")
    canonical = json.dumps(list(rows), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
