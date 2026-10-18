"""Source validation and explicit review of prompt-impacting translations.

Model execution belongs to the shared instance Job/Preparation path.
This module validates source facts and review contexts; it never replaces caller inputs.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Iterable
import unicodedata


from studio.models import CharacterBible, PromptFieldTranslation, PropBible, SceneBible, SeriesProject, Shot
from studio.services.prompt_normalization_contract import PromptNormalizationContractError, normalization_text_max_characters, validate_director_text_characters


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
class CanonicalTargetCollisionError(ValueError):
    """Two authoritative Bible entities resolve to one canonical identity."""

    code = "canonical_target_collision"

    def __init__(self, collisions: list[dict]):
        super().__init__("canonical English targets must be unique across Bible entities")
        self.collisions = collisions

# Editable translation records, not a catalog of executable generation inputs.
ALLOWED_FIELD_KEYS = {
    "project": {
        "style.visual_prompt",
        "style.negative_prompt",
        "project.production_settings.color_script",
        "project.production_settings.negative_prompt",
        "project.continuity_summary",
    },
    "character": {
        "character.name",
        "character.age_appearance",
        "character.primary_setting.identity_anchor",
        "character.primary_setting.appearance_anchor",
        "character.face_identity",
        "character.wardrobe",
        "character.voice_profile",
        "character.personality",
        "character.negative_constraints",
    },
    "scene": {
        "scene.name",
        "scene.primary_setting.identity_anchor",
        "scene.primary_setting.appearance_anchor",
        "scene.location",
        "scene.time_of_day",
        "scene.lighting",
        "scene.color_palette",
        "scene.ambient_sound",
        "scene.negative_constraints",
    },
    "prop": {
        "prop.name",
        "prop.primary_setting.identity_anchor",
        "prop.primary_setting.appearance_anchor",
        "prop.material",
        "prop.distinctive_marks",
        "prop.continuity_state",
        "prop.negative_constraints",
    },
    # Shot creative values use exact independent fields, not fixed columns.
    "shot": set(),
}
_DIALOGUE_DELIVERY_FIELD_RE = re.compile(r"^shot\.dialogue\.([0-9]+)\.delivery$")


def append_translation_history(
    entry: PromptFieldTranslation, *, event: str = "", actor=None,
) -> None:
    history_item = {
        "revision": entry.revision,
        "source_text": entry.source_text,
        "translated_text": entry.translated_text,
        "status": entry.status,
        "provider": entry.provider,
        "model": entry.model,
        "error_message": entry.error_message,
        "runtime_metadata": entry.runtime_metadata or {},
        "confirmed_at": entry.confirmed_at.isoformat() if entry.confirmed_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }
    if event:
        history_item["event"] = event
    if actor is not None:
        history_item["confirmed_by"] = str(actor.pk)
    entry.revision_history = [*(entry.revision_history or []), history_item]


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


def source_text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_terminology(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def canonical_identity_mappings(project: SeriesProject) -> dict[str, str]:
    """Lexical translation terms, never labels for reference-material identity.

    Implicit terms are added only for unambiguous entity names. Explicit glossary
    entries remain word mappings; renderers use their frozen reference-use names.
    """

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
    rows = []
    for scope, manager, identity_field in (
        ("character", project.characters, "stable_speaker_id"),
        ("scene", project.scenes, "slug"),
        ("prop", project.props, "slug"),
    ):
        for entity_id, name, identity in manager.order_by().values_list("id", "name", identity_field):
            stable_label = identity if scope == "character" else f"{scope}:{identity}"
            rows.append((scope, str(entity_id), name, stable_label))
    source_counts = Counter(_normalized_terminology(name) for _, _, name, _ in rows)
    for _, _, name, stable_label in rows:
        if source_counts[_normalized_terminology(name)] == 1:
            mappings.setdefault(name, stable_label)
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
            normalized_target = _normalized_terminology(target)
            if not normalized_target:
                continue
            inputs.append({
                "scope_type": scope_type,
                "scope_id": str(entity.id),
                "name": name,
                "canonical_target": target,
                "normalized_target": normalized_target,
                "normalized_source": _normalized_terminology(name),
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


def missing_translation_context_mappings(
    project: SeriesProject, context: str, translated_text: str,
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
        _canonical_mappings_from_context(context).items(),
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
    if not field_key_allowed(scope_type, field_key):
        raise TranslationValidationError("不支持的翻译字段")
    if source_text is None:
        source_text = resolve_current_source(
            project=project, scope_type=scope_type, scope_id=scope_id,
            field_key=field_key,
        )
    context = {
        "context_version": TRANSLATION_CONTEXT_VERSION,
        "purpose": "configured video prompt field",
        "scope": scope_type,
        "scope_id": str(scope_id or project.id),
        "field_key": field_key,
        "canonical_identity_mappings": relevant_canonical_identity_mappings(
            project, source_text,
        ),
        "rule": "When a source term is present, reuse its mapped target verbatim.",
    }
    # Name translation needs the entity's authored meaning, not just its name.
    # These are platform source fields; naming style/instructions belong to the
    # explicitly selected prompt strategy, not a model-specific branch here.
    entity_models = {"character": CharacterBible, "scene": SceneBible, "prop": PropBible}
    is_primary_field = field_key in {
        f"{scope_type}.primary_setting.identity_anchor", f"{scope_type}.primary_setting.appearance_anchor"}
    if scope_type in entity_models and (field_key == f"{scope_type}.name" or is_primary_field):
        from studio.services.bible_setting_evidence import read_entity_primary_setting, primary_setting_content
        from studio.services.bible_variants import BibleVariantConflict

        facts = entity_models[scope_type].objects.filter(project=project, pk=scope_id).values(
            "name", "description").first()
        if facts is None:
            raise TranslationValidationError("翻译来源实体不存在或不属于当前项目")
        try:
            setting = read_entity_primary_setting(project_id=project.pk, entity_kind=scope_type, entity_id=scope_id)
        except BibleVariantConflict as exc:
            raise TranslationValidationError("翻译来源的主图设定缺失或不一致，请先核对设定",
                code="translation_source_conflict") from exc
        if is_primary_field:
            if setting is None:
                raise TranslationValidationError("尚未登记主图设定，不能从旧实体文字补齐翻译来源",
                    code="translation_source_conflict")
            if source_text != getattr(setting, field_key.rsplit(".", 1)[1]):
                raise TranslationValidationError("主图设定原文已变化，请重新读取",
                    code="translation_source_conflict")
            context["purpose"] = "primary setting source facts"
            context["primary_setting"] = primary_setting_content(setting)
            return json.dumps(context, ensure_ascii=False, sort_keys=True)
        context["purpose"] = "canonical name source facts"
        # Naming is allowed before appearance authoring. Absence stays null;
        # never fill it from the entity's old duplicate visual text. Only
        # exact content identity participates, not later approval/media state.
        context["entity"] = {"source_name": facts["name"], "description": facts["description"],
            "primary_setting": primary_setting_content(setting)}
        glossary = (project.production_settings or {}).get("translation_glossary", {})
        if not isinstance(glossary, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in glossary.items()):
            raise TranslationValidationError("项目术语表必须包含真实文本名称及译名")
        # A name being translated must not become an internal speaker/slug ID.
        # Explicit terminology is still protected, using the same context as
        # the transport and subsequent manual review.
        context["canonical_identity_mappings"] = {key: value for key, value in context["canonical_identity_mappings"].items() if key in glossary and glossary[key].strip()}
    return json.dumps(context, ensure_ascii=False, sort_keys=True)


def translation_context_hash(context: str) -> str:
    return source_text_hash(str(context or ""))


def translation_entry_context_current(
    entry: PromptFieldTranslation, *, project: SeriesProject | None = None,
    current_source: str | None = None, translated_text: str | None = None,
) -> bool:
    """Require the current version and exact relevant-context evidence.

    Missing/older context metadata cannot be inferred from words in a result.
    Unrelated terminology changes remain harmless through the shared narrow
    context builder, not through a second legacy acceptance rule.
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
    if (not isinstance(metadata, dict)
            or type(metadata.get("translation_context_version")) is not int
            or metadata["translation_context_version"] != TRANSLATION_CONTEXT_VERSION):
        return False
    stored_hash = str(metadata.get("translation_context_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", stored_hash):
        return False
    candidate = str(
        entry.translated_text if translated_text is None else translated_text
    )
    relevant = _canonical_mappings_from_context(current_context)
    canonical_targets_present = bool(candidate) and all(
        canonical in candidate for canonical in relevant.values()
    )
    if stored_hash == translation_context_hash(current_context):
        if (translated_text is None and not candidate and metadata.get("manual_candidate") is True
                and entry.status == PromptFieldTranslation.Status.DRAFT_TRANSLATION):
            # A blank manual draft is editable, not an accepted empty result.
            # Confirmation supplies the actual candidate and still checks it.
            return True
        # A matching context proves which terminology was required, but a
        # human can still edit the candidate after generation.  Recheck the
        # actual text before confirmation so a manually edited candidate cannot
        # silently discard the canonical identity required by its source.
        return canonical_targets_present
    return False


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
        obj = model.objects.filter(pk=scope_id, episode__project=project).only("id", "dialogue").first()
    elif model:
        # Prove ownership without loading the superseded duplicate text.
        attribute = field_key.split(".", 1)[1]
        fields = ("id",) if attribute.startswith("primary_setting.") else ("id", attribute)
        obj = model.objects.filter(pk=scope_id, project=project).only(*fields).first()
    else:
        obj = None
    if obj is None:
        raise TranslationValidationError("翻译字段作用域不存在或不属于当前项目")
    if field_key.startswith(f"{scope_type}.primary_setting."):
        from studio.services.bible_setting_evidence import read_entity_primary_setting
        from studio.services.bible_variants import BibleVariantConflict
        try:
            setting = read_entity_primary_setting(project_id=project.pk, entity_kind=scope_type, entity_id=scope_id)
        except BibleVariantConflict as exc:
            raise TranslationValidationError("主图设定来源不一致，请先核对设定",
                code="translation_source_conflict") from exc
        if setting is None:
            raise TranslationValidationError("尚未登记主图设定，不能从旧实体文字补齐翻译来源",
                code="translation_source_conflict")
        return getattr(setting, field_key.rsplit(".", 1)[1])
    delivery_match = _DIALOGUE_DELIVERY_FIELD_RE.fullmatch(field_key)
    if delivery_match and isinstance(obj, Shot):
        index = int(delivery_match.group(1))
        dialogue = obj.dialogue if isinstance(obj.dialogue, list) else []
        if index >= len(dialogue) or not isinstance(dialogue[index], dict):
            raise TranslationValidationError("对白 delivery 索引已失效")
        return str(dialogue[index].get("delivery", "") or "")
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


def validate_manual_translation(source: str, translated: str) -> str:
    """Validate an edited candidate without invoking the model."""
    _, blocks = _protect_authored_blocks(source)
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
    try:
        validate_director_text_characters(director_text)
    except PromptNormalizationContractError as exc:
        raise TranslationValidationError(str(exc)) from exc
    return restored
