"""Idempotently seed project canonical-English names from current Bible entities.

Bible persistence and model inference deliberately live in two HTTP
transactions.  The caller first saves the authoritative Bible snapshot, then
invokes :func:`ensure_canonical_glossary`.  Model loading therefore cannot roll
back or delay the entity-creation transaction.  A final project row lock uses
``setdefault`` semantics so a concurrent human edit always wins.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from studio.models import (
    CharacterBible,
    PromptFieldTranslation,
    PropBible,
    SceneBible,
    SeriesProject,
)
from studio.services.configuration import ConfigurationResolutionError, resolve_configuration
from studio.services.content_contracts import (
    CANONICAL_CONTEXT_POLICY_KEY,
    CANONICAL_NAME_POLICY_KEY,
    canonical_context_policy,
    canonical_glossary_max_entities,
    canonical_glossary_max_runtime_items,
    canonical_name_policy,
)
from studio.services.prompt_normalization import DEFAULT_DEPLOYMENT_CONFIG_KEY
from studio.services.prompt_translation import (
    CanonicalTargetCollisionError,
    PromptTranslationError,
    TranslationResult,
    TranslationValidationError,
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    ensure_no_canonical_target_collisions,
    source_text_hash,
    translation_entry_context_current,
    translation_context_hash,
    translate_texts_once,
    validate_manual_translation,
)


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CANONICAL_ENGLISH_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*(?:[ .'-][A-Za-z0-9]+)*$",
)
_SCOPE_MODELS = {
    PromptFieldTranslation.ScopeType.CHARACTER: CharacterBible,
    PromptFieldTranslation.ScopeType.SCENE: SceneBible,
    PromptFieldTranslation.ScopeType.PROP: PropBible,
}
_NAME_FIELD_KEYS = {
    PromptFieldTranslation.ScopeType.CHARACTER: "character.name",
    PromptFieldTranslation.ScopeType.SCENE: "scene.name",
    PromptFieldTranslation.ScopeType.PROP: "prop.name",
}
_VISUAL_FIELD_KEYS = {
    PromptFieldTranslation.ScopeType.CHARACTER: "character.visual_identity",
    PromptFieldTranslation.ScopeType.SCENE: "scene.visual_identity",
    PromptFieldTranslation.ScopeType.PROP: "prop.visual_identity",
}


class CanonicalGlossaryCapacityError(ValueError):
    """The safe one-load runtime batch limit would be exceeded."""


@dataclass(frozen=True)
class CanonicalTarget:
    scope_type: str
    scope_id: object
    name: str
    description: str
    visual_identity: str

    @property
    def key(self) -> tuple[str, object]:
        return self.scope_type, self.scope_id


@dataclass(frozen=True)
class VisualRuntimeItem:
    target: CanonicalTarget
    inference_source: str
    inference_context: str
    # (Chinese canonical source, stable inference token, occurrence count)
    canonical_tokens: tuple[tuple[str, str, int], ...] = ()


def _glossary(project: SeriesProject) -> dict[str, str]:
    raw = (project.production_settings or {}).get("translation_glossary", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(source).strip(): str(target).strip()
        for source, target in raw.items()
        if str(source).strip() and str(target).strip()
    }


def _snapshot(scope_type: str, entity) -> CanonicalTarget:
    return CanonicalTarget(
        scope_type=scope_type,
        scope_id=entity.pk,
        name=str(entity.name or "").strip(),
        description=str(entity.description or ""),
        # This is the authoritative translation source.  Do not truncate it:
        # PromptFieldTranslation source hashes must match resolve_current_source.
        visual_identity=str(entity.visual_identity or ""),
    )


def _proper_name_context(
    target: CanonicalTarget, glossary: dict[str, str],
) -> str:
    # Authored Bible/glossary facts stay complete.  Advisory capacities are
    # audited separately; the signed request-byte security boundary remains the
    # only size fail-closed guard before Provider submission.
    existing = {
        str(source): str(value)
        for source, value in sorted(glossary.items())
    }
    return json.dumps({
        "purpose": "canonical English proper-name glossary",
        "entity_type": target.scope_type,
        "entity": {
            "source_name": target.name,
            "description": target.description,
            "visual_identity": target.visual_identity,
        },
        "existing_canonical_glossary": existing,
        "rules": [
            "Return one short English proper name only.",
            "Use ASCII Pinyin without tone marks for person names unless an established rendering exists.",
            "Never return a speaker ID, database ID, slug, scope prefix, label, or explanation.",
            "Reuse an existing canonical spelling when the same named identity is present.",
        ],
    }, ensure_ascii=False, sort_keys=True)


def _visual_inference_context(
    target: CanonicalTarget, glossary: dict[str, str],
    canonical_tokens: tuple[tuple[str, str, int], ...],
) -> str:
    """Build the exact context shown to Qwen for a visual-identity candidate.

    It intentionally contains only human/canonical glossary values.  The
    broader compiler context can contain internal speaker or entity slugs;
    those are useful identity keys but must never leak into natural English
    visual prose.
    """
    existing = {
        str(source): str(value)
        for source, value in sorted(glossary.items())
    }
    rules = [
        "Translate only invariant visible appearance, silhouette, wardrobe, material, color, surface, and identifying marks.",
        "Do not add action, emotion, camera language, history, labels, technical IDs, speaker IDs, or database slugs.",
        "Reuse every canonical glossary rendering verbatim when its Chinese source occurs.",
    ]
    if canonical_tokens:
        rules.extend(
            f"Preserve the exact token {token} unchanged and exactly {count} time(s); it represents {source!r} and will be replaced by the canonical proper name after this batch."
            for source, token, count in canonical_tokens
        )
    return json.dumps({
        "purpose": "English Bible visual-identity review candidate",
        "entity_type": target.scope_type,
        "entity_name_source": target.name,
        "entity_description_reference": target.description,
        "canonical_glossary_at_inference": existing,
        "canonical_identity_tokens": [
            {"source": source, "token": token, "count": count}
            for source, token, count in canonical_tokens
        ],
        "rules": rules,
    }, ensure_ascii=False, sort_keys=True)


def _prepare_visual_runtime_item(
    target: CanonicalTarget, glossary: dict[str, str],
    pending_canonical_names: Iterable[str],
) -> VisualRuntimeItem:
    source = target.visual_identity
    tokens: list[tuple[str, str, int]] = []
    # Longest first prevents a short name from claiming a substring inside a
    # longer one. All missing names from this same model batch are protected,
    # not only the visual entity's own name.
    for canonical_source in sorted(set(pending_canonical_names), key=len, reverse=True):
        if not canonical_source or canonical_source not in source:
            continue
        count = source.count(canonical_source)
        token_id = "CanonicalIdentityToken" + source_text_hash(
            f"{target.scope_type}:{target.scope_id}:{canonical_source}"
        )[:12]
        while token_id in source:
            token_id += "X"
        # translate_texts_once already protects screen_text blocks with an
        # opaque literal token and validates exact restoration.  Reusing that
        # audited mechanism makes canonical placeholders deterministic instead
        # of merely asking the model to copy an arbitrary Latin identifier.
        token = (
            f'<screen_text data-autoprompt-canonical="{token_id}">'
            f"{token_id}</screen_text>"
        )
        source = source.replace(canonical_source, token)
        tokens.append((canonical_source, token, count))
    canonical_tokens = tuple(tokens)
    context = _visual_inference_context(target, glossary, canonical_tokens)
    return VisualRuntimeItem(
        target=target,
        inference_source=source,
        inference_context=context,
        canonical_tokens=canonical_tokens,
    )


def _finalize_visual_result(
    item: VisualRuntimeItem, result: TranslationResult, *,
    glossary: dict[str, str],
) -> TranslationResult:
    candidate = result.text
    for canonical_source, token, expected_count in item.canonical_tokens:
        canonical = str(glossary.get(canonical_source) or "").strip()
        try:
            canonical = validate_canonical_english(canonical)
        except PromptTranslationError:
            raise TranslationValidationError(
                f"视觉锚点引用的 {canonical_source!r} 尚无有效 canonical English；为避免名称漂移，未保存该候选",
                code="canonical_dependency_failed",
            )
        token_count = candidate.count(token)
        if token_count != expected_count:
            raise TranslationValidationError(
                "视觉锚点译文未完整保留 canonical 名称占位符；为避免名称漂移，未保存该候选",
                code="canonical_identity_token_mismatch",
            )
        candidate = candidate.replace(token, canonical)
    candidate = validate_manual_translation(item.target.visual_identity, candidate)
    inference_hash = str(
        (result.metadata or {}).get("translation_context_hash")
        or translation_context_hash(item.inference_context)
    )
    return TranslationResult(
        text=candidate,
        provider=result.provider,
        model=result.model,
        metadata={
            **(result.metadata or {}),
            "inference_context_hash": inference_hash,
            "canonical_identity_placeholder_used": bool(item.canonical_tokens),
            "canonical_identity_placeholder_count": len(item.canonical_tokens),
        },
    )


def _capacity_advisory(
    *, code: str, policy_key: str, field: str,
    actual: int, advisory: int, affected_items: int | None = None,
) -> dict | None:
    if actual <= advisory:
        return None
    row = {
        "severity": "warning",
        "code": code,
        "policy_key": policy_key,
        "field": field,
        "actual": int(actual),
        "advisory": int(advisory),
    }
    if affected_items is not None:
        row["affected_items"] = int(affected_items)
    return row


def _canonical_context_advisories(
    *,
    request_targets: list[CanonicalTarget],
    model_context_targets: list[CanonicalTarget],
    glossary: dict[str, str],
    runtime_item_count: int,
    proper_name_context_count: int,
    visual_context_count: int,
    ensure_visual_identity_translation: bool,
    name_policy: dict[str, int],
    context_policy: dict[str, int],
) -> list[dict]:
    """Return compact, content-free advisory facts for operator audit."""

    advisories: list[dict] = []

    def add(row: dict | None) -> None:
        if row is not None:
            advisories.append(row)

    entity_field = (
        "max_entities_with_visual_identity_translation"
        if ensure_visual_identity_translation
        else "max_entities_per_request"
    )
    add(_capacity_advisory(
        code="canonical_entity_count_above_advisory",
        policy_key=CANONICAL_NAME_POLICY_KEY,
        field=entity_field,
        actual=len(request_targets),
        advisory=name_policy[entity_field],
    ))
    add(_capacity_advisory(
        code="canonical_runtime_item_count_above_advisory",
        policy_key=CANONICAL_NAME_POLICY_KEY,
        field="max_runtime_items_per_request",
        actual=runtime_item_count,
        advisory=name_policy["max_runtime_items_per_request"],
    ))
    if proper_name_context_count:
        add(_capacity_advisory(
            code="proper_name_glossary_entries_above_advisory",
            policy_key=CANONICAL_CONTEXT_POLICY_KEY,
            field="proper_name_glossary_advisory_entries",
            actual=len(glossary),
            advisory=context_policy["proper_name_glossary_advisory_entries"],
        ))
    if visual_context_count:
        add(_capacity_advisory(
            code="visual_identity_glossary_entries_above_advisory",
            policy_key=CANONICAL_CONTEXT_POLICY_KEY,
            field="visual_identity_glossary_advisory_entries",
            actual=len(glossary),
            advisory=context_policy["visual_identity_glossary_advisory_entries"],
        ))

    unique_targets = list({
        target.key: target for target in model_context_targets
    }.values())
    descriptions = [len(target.description) for target in unique_targets]
    description_advisory = context_policy["description_advisory_characters"]
    add(_capacity_advisory(
        code="description_characters_above_advisory",
        policy_key=CANONICAL_CONTEXT_POLICY_KEY,
        field="description_advisory_characters",
        actual=max(descriptions, default=0),
        advisory=description_advisory,
        affected_items=sum(value > description_advisory for value in descriptions),
    ))

    visual_identities = [
        len(target.visual_identity) for target in unique_targets
    ]
    visual_advisory = context_policy["visual_identity_advisory_characters"]
    add(_capacity_advisory(
        code="visual_identity_characters_above_advisory",
        policy_key=CANONICAL_CONTEXT_POLICY_KEY,
        field="visual_identity_advisory_characters",
        actual=max(visual_identities, default=0),
        advisory=visual_advisory,
        affected_items=sum(value > visual_advisory for value in visual_identities),
    ))

    glossary_term_lengths = [
        max(len(str(source)), len(str(value)))
        for source, value in glossary.items()
    ] if model_context_targets else []
    glossary_term_advisory = context_policy["glossary_term_advisory_characters"]
    add(_capacity_advisory(
        code="glossary_term_characters_above_advisory",
        policy_key=CANONICAL_CONTEXT_POLICY_KEY,
        field="glossary_term_advisory_characters",
        actual=max(glossary_term_lengths, default=0),
        advisory=glossary_term_advisory,
        affected_items=sum(
            value > glossary_term_advisory for value in glossary_term_lengths
        ),
    ))
    return advisories


def _attach_canonical_policy_audit(
    result: TranslationResult | PromptTranslationError,
    *,
    name_policy: dict[str, int],
    context_policy: dict[str, int],
    policy_resolution_sha256: str,
    advisories: list[dict],
) -> TranslationResult | PromptTranslationError:
    metadata = {
        **(getattr(result, "metadata", {}) or {}),
        "canonical_name_policy": {"schema_version": 1, **name_policy},
        "canonical_context_policy": {"schema_version": 1, **context_policy},
        "canonical_policy_resolution_sha256": policy_resolution_sha256,
        "canonical_context_advisories": list(advisories),
    }
    if isinstance(result, TranslationResult):
        return TranslationResult(
            text=result.text,
            provider=result.provider,
            model=result.model,
            metadata=metadata,
        )
    result.metadata = metadata
    return result


def validate_canonical_english(value: str, *, policy: dict[str, int] | None = None) -> str:
    """Fail closed on prose, mixed language and internal technical identifiers."""
    candidate = " ".join(str(value or "").strip().split())
    if not candidate:
        raise TranslationValidationError("canonical English 不能为空")
    policy = policy or canonical_name_policy()
    max_characters = int(policy["max_characters"])
    max_words = int(policy["max_words"])
    if len(candidate) > max_characters or len(candidate.split()) > max_words:
        raise TranslationValidationError(
            f"canonical English 必须是 1 至 {max_words} 个词且不超过 {max_characters} 个字符的短专名"
        )
    if _CJK_RE.search(candidate) or "\n" in str(value) or "\r" in str(value):
        raise TranslationValidationError("canonical English 必须是单行纯英文专名")
    if not _CANONICAL_ENGLISH_RE.fullmatch(candidate):
        raise TranslationValidationError("canonical English 含有不允许的说明文字或标点")
    folded = candidate.casefold()
    if folded.startswith(("scene ", "scene:", "prop ", "prop:", "character ", "character:")):
        raise TranslationValidationError("canonical English 不能使用内部作用域前缀")
    if re.fullmatch(r"s[1-9][0-9]*", folded):
        raise TranslationValidationError("canonical English 不能使用角色技术 ID")
    return candidate


def _collect_targets(
    project: SeriesProject, selectors: Iterable[dict], *, include_all_missing: bool,
    ensure_visual_identity_translation: bool,
) -> tuple[list[CanonicalTarget], list[dict]]:
    ordered: list[CanonicalTarget] = []
    conflicts: list[dict] = []
    seen: set[tuple[str, object]] = set()
    selectors = list(selectors)
    for scope_type, model in _SCOPE_MODELS.items():
        ids = [item["scope_id"] for item in selectors if item["scope_type"] == scope_type]
        found = {
            item.pk: item for item in model.objects.filter(project=project, pk__in=ids)
        }
        for scope_id in ids:
            key = (scope_type, scope_id)
            if key in seen:
                continue
            seen.add(key)
            entity = found.get(scope_id)
            if entity is None:
                conflicts.append(_result_row(
                    scope_type=scope_type, scope_id=scope_id, name="",
                    status="conflict", origin="none", retryable=False,
                    error_code="entity_not_found",
                    message="Bible 实体不存在或不属于当前项目",
                ))
                continue
            ordered.append(_snapshot(scope_type, entity))

    if include_all_missing:
        glossary = _glossary(project)
        for scope_type, model in _SCOPE_MODELS.items():
            for entity in model.objects.filter(project=project).order_by("created_at"):
                target = _snapshot(scope_type, entity)
                if target.key in seen:
                    continue
                canonical_missing = bool(
                    _CJK_RE.search(target.name) and target.name not in glossary
                )
                visual_may_need_translation = bool(
                    ensure_visual_identity_translation
                    and _CJK_RE.search(target.visual_identity)
                )
                if not canonical_missing and not visual_may_need_translation:
                    continue
                seen.add(target.key)
                ordered.append(target)
    return ordered, conflicts


def _result_row(
    *, scope_type: str, scope_id, name: str, status: str, origin: str,
    canonical_english: str = "", retryable: bool = False,
    error_code: str = "", message: str = "",
) -> dict:
    return {
        "scope_type": scope_type,
        "scope_id": str(scope_id),
        "name": name,
        "canonical_english": canonical_english,
        "status": status,
        "origin": origin,
        "retryable": retryable,
        "error_code": error_code,
        "message": message,
    }


def _translation_failure(error: PromptTranslationError) -> tuple[str, str, bool]:
    code = str(getattr(error, "code", "translation_failed") or "translation_failed")
    if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
        code = "translation_failed"
    metadata = getattr(error, "metadata", {}) or {}
    retryable = (
        not bool(metadata.get("submission_uncertain"))
        and code not in {
            "invalid_source",
            "invalid_request",
            "model_deployment_required",
            "model_deployment_not_found",
            "deployment_inactive",
            "model_version_not_published",
            "provider_inactive",
            "text_modality_required",
            "capability_not_declared",
            "driver_capability_missing",
            "controlled_chat_transport_missing",
            "external_idempotency_unimplemented",
            "credential_unavailable",
            "deployment_configuration_invalid",
            "prompt_normalize_smoke_required",
        }
    )
    return code, str(error)[:500], retryable


def _name_is_used(project: SeriesProject, name: str, *, exclude: CanonicalTarget) -> bool:
    for scope_type, model in _SCOPE_MODELS.items():
        queryset = model.objects.filter(project=project, name=name)
        if scope_type == exclude.scope_type:
            queryset = queryset.exclude(pk=exclude.scope_id)
        if queryset.exists():
            return True
    return False


def _cleanup_replaced_auto_mapping(
    *, project: SeriesProject, target: CanonicalTarget, glossary: dict[str, str],
) -> bool:
    field_key = _NAME_FIELD_KEYS[target.scope_type]
    entry = PromptFieldTranslation.objects.select_for_update().filter(
        project=project, scope_type=target.scope_type, scope_id=target.scope_id,
        field_key=field_key,
    ).first()
    if (
        entry is not None
        and bool((entry.runtime_metadata or {}).get("auto_glossary"))
        and entry.source_text != target.name
        and not _name_is_used(project, entry.source_text, exclude=target)
        and glossary.get(entry.source_text) == entry.translated_text
    ):
        glossary.pop(entry.source_text, None)
        return True
    return False


def _append_history(entry: PromptFieldTranslation) -> list[dict]:
    return [*(entry.revision_history or []), {
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
    }]


def _record_generated_mapping(
    *, project: SeriesProject, target: CanonicalTarget, canonical: str,
    result: TranslationResult, inference_context_hash: str, user,
) -> None:
    field_key = _NAME_FIELD_KEYS[target.scope_type]
    entry = PromptFieldTranslation.objects.select_for_update().filter(
        project=project, scope_type=target.scope_type, scope_id=target.scope_id,
        field_key=field_key,
    ).first()
    if entry is None:
        entry = PromptFieldTranslation(
            project=project, scope_type=target.scope_type, scope_id=target.scope_id,
            field_key=field_key, revision=1,
        )
    elif entry.source_text != target.name or entry.translated_text != canonical:
        entry.revision_history = _append_history(entry)
        entry.revision += 1
    entry.source_text = target.name
    entry.source_hash = source_text_hash(target.name)
    entry.translated_text = canonical
    entry.status = PromptFieldTranslation.Status.CONFIRMED
    entry.provider = result.provider
    entry.model = result.model
    entry.error_message = ""
    entry.runtime_metadata = {
        **(result.metadata or {}),
        "auto_glossary": True,
        # Preserve the exact bounded context shown to the model separately
        # from the final compiler/review context written below.
        "inference_context_hash": inference_context_hash,
        "translation_context_hash": translation_context_hash(
            build_translation_context(
                project, scope_type=target.scope_type,
                scope_id=target.scope_id, field_key=field_key,
                source_text=target.name,
            )
        ),
        "translation_context_version": TRANSLATION_CONTEXT_VERSION,
    }
    entry.confirmed_by = user
    entry.confirmed_at = timezone.now()
    entry.save()


def _translation_row_snapshot(entry: PromptFieldTranslation | None) -> tuple | None:
    if entry is None:
        return None
    return (
        str(entry.pk), entry.revision, entry.source_hash, entry.translated_text,
        entry.status, entry.provider, entry.model,
        json.dumps(entry.runtime_metadata or {}, ensure_ascii=False, sort_keys=True),
    )


def _visual_entry_is_current(
    entry: PromptFieldTranslation | None, *, project: SeriesProject,
    target: CanonicalTarget,
) -> bool:
    if entry is None or entry.field_key != _VISUAL_FIELD_KEYS[target.scope_type]:
        return False
    if entry.status not in {
        PromptFieldTranslation.Status.DRAFT_TRANSLATION,
        PromptFieldTranslation.Status.CONFIRMED,
    } or not entry.translated_text:
        return False
    if entry.source_hash != source_text_hash(target.visual_identity):
        return False
    return translation_entry_context_current(
        entry, project=project, current_source=target.visual_identity,
    )


def _visual_result_row(
    *, target: CanonicalTarget, status: str, origin: str,
    entry: PromptFieldTranslation | None = None, context_current: bool = False,
    retryable: bool = False, error_code: str = "", message: str = "",
) -> dict:
    return {
        "scope_type": target.scope_type,
        "scope_id": str(target.scope_id),
        "field_key": _VISUAL_FIELD_KEYS[target.scope_type],
        "source_text": target.visual_identity,
        "translated_text": entry.translated_text if entry is not None else "",
        "translation_id": str(entry.pk) if entry is not None else None,
        "translation_status": entry.status if entry is not None else "",
        "revision": entry.revision if entry is not None else None,
        "status": status,
        "origin": origin,
        "context_current": context_current,
        "retryable": retryable,
        "error_code": error_code,
        "message": message,
    }


def _record_visual_failure(
    *, project: SeriesProject, target: CanonicalTarget,
    error: PromptTranslationError,
) -> PromptFieldTranslation:
    """Record a current-source failure without destroying a prior candidate."""
    field_key = _VISUAL_FIELD_KEYS[target.scope_type]
    source_hash = source_text_hash(target.visual_identity)
    entry = PromptFieldTranslation.objects.select_for_update().filter(
        project=project, scope_type=target.scope_type, scope_id=target.scope_id,
        field_key=field_key,
    ).first()
    is_new = entry is None
    source_changed = bool(entry is not None and entry.source_hash != source_hash)
    if is_new:
        entry = PromptFieldTranslation(
            project=project, scope_type=target.scope_type,
            scope_id=target.scope_id, field_key=field_key, revision=1,
        )
    elif source_changed:
        entry.revision_history = _append_history(entry)
        entry.revision += 1
    error_metadata = {
        **(getattr(error, "metadata", {}) or {}),
        "error_code": getattr(error, "code", "translation_failed"),
    }
    if is_new or source_changed:
        entry.source_text = target.visual_identity
        entry.source_hash = source_hash
        entry.translated_text = ""
        entry.status = PromptFieldTranslation.Status.FAILED
        entry.provider = str(error_metadata.get("provider") or "")[:80]
        entry.model = ""
        entry.error_message = str(error)
        entry.runtime_metadata = {
            **error_metadata,
            "translation_context_hash": translation_context_hash(
                build_translation_context(
                    project, scope_type=target.scope_type, scope_id=target.scope_id,
                    field_key=field_key, source_text=target.visual_identity,
                )
            ),
            "translation_context_version": TRANSLATION_CONTEXT_VERSION,
        }
        entry.confirmed_by = None
        entry.confirmed_at = None
    else:
        entry.error_message = str(error)
        entry.runtime_metadata = {
            **(entry.runtime_metadata or {}),
            "last_attempt_error": str(error),
            "last_attempt_error_code": getattr(error, "code", "translation_failed"),
            "last_attempt_metadata": error_metadata,
            "last_attempt_failed_at": timezone.now().isoformat(),
        }
    entry.save()
    return entry


def _record_visual_candidate(
    *, project: SeriesProject, target: CanonicalTarget,
    result: TranslationResult,
) -> PromptFieldTranslation:
    field_key = _VISUAL_FIELD_KEYS[target.scope_type]
    source_hash = source_text_hash(target.visual_identity)
    entry = PromptFieldTranslation.objects.select_for_update().filter(
        project=project, scope_type=target.scope_type, scope_id=target.scope_id,
        field_key=field_key,
    ).first()
    if entry is None:
        entry = PromptFieldTranslation(
            project=project, scope_type=target.scope_type,
            scope_id=target.scope_id, field_key=field_key, revision=1,
        )
    else:
        entry.revision_history = _append_history(entry)
        entry.revision += 1
    entry.source_text = target.visual_identity
    entry.source_hash = source_hash
    entry.translated_text = result.text
    entry.status = PromptFieldTranslation.Status.DRAFT_TRANSLATION
    entry.provider = result.provider
    entry.model = result.model
    entry.error_message = ""
    entry.runtime_metadata = {
        **(result.metadata or {}),
        "auto_visual_identity_candidate": True,
        # This is the final compiler/review context, after every canonical
        # name from the same inference batch has been merged.
        "translation_context_hash": translation_context_hash(
            build_translation_context(
                project, scope_type=target.scope_type, scope_id=target.scope_id,
                field_key=field_key, source_text=target.visual_identity,
            )
        ),
        "translation_context_version": TRANSLATION_CONTEXT_VERSION,
    }
    entry.confirmed_by = None
    entry.confirmed_at = None
    entry.save()
    return entry


def ensure_canonical_glossary(
    *, project: SeriesProject, selectors: Iterable[dict],
    include_all_missing: bool, ensure_visual_identity_translation: bool = False,
    user,
) -> dict:
    """Generate canonical names and optional review-gated visual candidates.

    All model work is submitted through one ``translate_texts_once`` call. The
    canonical names are auto-confirmed, while visual identity translations are
    drafts that cannot affect prompt compilation until a person confirms them.
    """
    targets, rows = _collect_targets(
        project, selectors, include_all_missing=include_all_missing,
        ensure_visual_identity_translation=ensure_visual_identity_translation,
    )
    policy_resolution = resolve_configuration(
        project=project,
        required_keys=(CANONICAL_NAME_POLICY_KEY, CANONICAL_CONTEXT_POLICY_KEY),
    )
    name_policy = canonical_name_policy(values=policy_resolution.values)
    context_policy = canonical_context_policy(values=policy_resolution.values)
    security_entity_limit = canonical_glossary_max_entities()
    if len(targets) > security_entity_limit:
        raise CanonicalGlossaryCapacityError(
            f"本次 Bible 实体数超过安全边界 {security_entity_limit}；请分批补全"
        )
    # Refuse to spend one local-model load while the current authoritative
    # identity map is already ambiguous.
    ensure_no_canonical_target_collisions(project)
    initial_glossary = _glossary(project)
    names_to_translate: list[str] = []
    target_by_name: dict[str, CanonicalTarget] = {}
    for target in targets:
        if not _CJK_RE.search(target.name) or target.name in initial_glossary:
            continue
        if target.name not in target_by_name:
            target_by_name[target.name] = target
            names_to_translate.append(target.name)

    visual_initial_snapshots: dict[tuple[str, object], tuple | None] = {}
    visual_runtime_items: list[VisualRuntimeItem] = []
    canonical_sources_to_protect = set(initial_glossary)
    for model in _SCOPE_MODELS.values():
        canonical_sources_to_protect.update(
            str(name).strip()
            for name in model.objects.filter(project=project).values_list(
                "name", flat=True,
            )
            if str(name).strip() and _CJK_RE.search(str(name))
        )
    # Any new/renamed auto glossary value changes every field's compiler
    # context. Existing visual candidates must then be regenerated rather than
    # returned as current under a context that is about to change.
    canonical_context_may_change = bool(names_to_translate)
    if not canonical_context_may_change:
        for target in targets:
            name_entry = PromptFieldTranslation.objects.filter(
                project=project, scope_type=target.scope_type,
                scope_id=target.scope_id,
                field_key=_NAME_FIELD_KEYS[target.scope_type],
                runtime_metadata__auto_glossary=True,
            ).first()
            if name_entry is not None and name_entry.source_text != target.name:
                canonical_context_may_change = True
                break
    if ensure_visual_identity_translation:
        for target in targets:
            entry = PromptFieldTranslation.objects.filter(
                project=project, scope_type=target.scope_type,
                scope_id=target.scope_id,
                field_key=_VISUAL_FIELD_KEYS[target.scope_type],
            ).first()
            visual_initial_snapshots[target.key] = _translation_row_snapshot(entry)
            if not _CJK_RE.search(target.visual_identity):
                continue
            if (
                _visual_entry_is_current(entry, project=project, target=target)
                and not canonical_context_may_change
            ):
                continue
            visual_runtime_items.append(
                _prepare_visual_runtime_item(
                    target, initial_glossary, canonical_sources_to_protect,
                )
            )

    runtime_item_count = len(names_to_translate) + len(visual_runtime_items)
    security_runtime_limit = canonical_glossary_max_runtime_items()
    if runtime_item_count > security_runtime_limit:
        raise CanonicalGlossaryCapacityError(
            "本次需要生成的英文名称与视觉锚点候选超过 canonical glossary "
            f"安全边界 {security_runtime_limit}；请分批补全"
        )

    translation_targets = [target_by_name[name] for name in names_to_translate]
    model_context_targets = [
        *translation_targets,
        *(item.target for item in visual_runtime_items),
    ]
    advisories = _canonical_context_advisories(
        request_targets=targets,
        model_context_targets=model_context_targets,
        glossary=initial_glossary,
        runtime_item_count=runtime_item_count,
        proper_name_context_count=len(translation_targets),
        visual_context_count=len(visual_runtime_items),
        ensure_visual_identity_translation=ensure_visual_identity_translation,
        name_policy=name_policy,
        context_policy=context_policy,
    )

    results_by_name: dict[str, TranslationResult | PromptTranslationError] = {}
    visual_results: dict[
        tuple[str, object], TranslationResult | PromptTranslationError
    ] = {}
    runtime = {
        "invocation_attempted": False,
        "submission_uncertain": False,
        "model_deployment_id": None,
        "provider": "",
        "data_egress": False,
        "canonical_name_policy": {"schema_version": 1, **name_policy},
        "canonical_context_policy": {"schema_version": 1, **context_policy},
        "canonical_policy_resolution_sha256": policy_resolution.sha256,
        "canonical_context_advisories": advisories,
    }
    if names_to_translate or visual_runtime_items:
        texts = [*names_to_translate, *(
            item.inference_source for item in visual_runtime_items
        )]
        field_keys = [
            f"canonical_glossary.{target.scope_type}_name"
            for target in translation_targets
        ] + [
            _VISUAL_FIELD_KEYS[item.target.scope_type]
            for item in visual_runtime_items
        ]
        labels = [
            f"{target.scope_type} canonical proper name"
            for target in translation_targets
        ] + [
            f"{item.target.scope_type} invariant visual identity"
            for item in visual_runtime_items
        ]
        contexts = [
            _proper_name_context(target, initial_glossary)
            for target in translation_targets
        ] + [item.inference_context for item in visual_runtime_items]
        try:
            resolved = resolve_configuration(
                project=project,
                required_keys=(DEFAULT_DEPLOYMENT_CONFIG_KEY,),
            )
            deployment_id = str(
                resolved.values[DEFAULT_DEPLOYMENT_CONFIG_KEY] or ""
            ).strip()
            if not deployment_id:
                raise PromptTranslationError(
                    "未配置默认 prompt.normalize deployment",
                    code="model_deployment_required",
                )
            runtime["model_deployment_id"] = deployment_id
            runtime["invocation_attempted"] = True
            translated = translate_texts_once(
                texts,
                field_keys=field_keys,
                labels=labels,
                contexts=contexts,
                model_deployment_id=deployment_id,
            )
        except ConfigurationResolutionError as exc:
            error = PromptTranslationError(str(exc), code=exc.code)
            translated = [error for _ in texts]
        except PromptTranslationError as error:
            translated = [error for _ in texts]
        translated = [
            _attach_canonical_policy_audit(
                result,
                name_policy=name_policy,
                context_policy=context_policy,
                policy_resolution_sha256=policy_resolution.sha256,
                advisories=advisories,
            )
            for result in translated
        ]
        for result in translated:
            metadata = getattr(result, "metadata", {}) or {}
            if metadata.get("model_deployment_id"):
                runtime["model_deployment_id"] = str(metadata["model_deployment_id"])
            result_provider = str(
                metadata.get("provider")
                or getattr(result, "provider", "")
                or ""
            )
            if result_provider:
                runtime["provider"] = result_provider
            runtime["data_egress"] = bool(
                runtime["data_egress"] or metadata.get("data_egress")
            )
            runtime["submission_uncertain"] = bool(
                runtime["submission_uncertain"]
                or metadata.get("submission_uncertain")
            )
        name_result_count = len(names_to_translate)
        results_by_name = dict(zip(
            names_to_translate, translated[:name_result_count], strict=True,
        ))
        visual_results = {
            item.target.key: result
            for item, result in zip(
                visual_runtime_items,
                translated[name_result_count:],
                strict=True,
            )
        }

    visual_rows: list[dict] = []
    with transaction.atomic():
        locked = SeriesProject.objects.select_for_update().get(pk=project.pk)
        settings_value = dict(locked.production_settings or {})
        glossary = _glossary(locked)
        glossary_changed = False
        generated_records: list[tuple[CanonicalTarget, str, TranslationResult, str]] = []
        current_entities: dict[tuple[str, object], object | None] = {}
        for target in targets:
            current = _SCOPE_MODELS[target.scope_type].objects.select_for_update().filter(
                project=locked, pk=target.scope_id,
            ).first()
            current_entities[target.key] = current
            if current is None or str(current.name or "").strip() != target.name:
                rows.append(_result_row(
                    scope_type=target.scope_type, scope_id=target.scope_id,
                    name=target.name, status="conflict", origin="none",
                    retryable=True, error_code="entity_changed",
                    message="翻译期间 Bible 名称已变化，请按当前名称重试",
                ))
                continue
            if not _CJK_RE.search(target.name):
                rows.append(_result_row(
                    scope_type=target.scope_type, scope_id=target.scope_id,
                    name=target.name, canonical_english=target.name,
                    status="reused", origin="source_english",
                ))
                continue
            existing = glossary.get(target.name, "").strip()
            if existing:
                glossary_changed = _cleanup_replaced_auto_mapping(
                    project=locked, target=target, glossary=glossary,
                ) or glossary_changed
                try:
                    validated_existing = validate_canonical_english(existing)
                except PromptTranslationError:
                    rows.append(_result_row(
                        scope_type=target.scope_type, scope_id=target.scope_id,
                        name=target.name, canonical_english=existing,
                        status="conflict", origin="existing", retryable=False,
                        error_code="invalid_existing_mapping",
                        message="已有术语不是合规的英文短专名；为保护人工内容未自动覆盖，请手工修正",
                    ))
                    continue
                rows.append(_result_row(
                    scope_type=target.scope_type, scope_id=target.scope_id,
                    name=target.name, canonical_english=validated_existing,
                    status="reused", origin="existing",
                ))
                continue
            result = results_by_name.get(target.name)
            if not isinstance(result, TranslationResult):
                error = result if isinstance(result, PromptTranslationError) else PromptTranslationError(
                    "canonical English 未生成", code="translation_failed",
                )
                code, message, retryable = _translation_failure(error)
                rows.append(_result_row(
                    scope_type=target.scope_type, scope_id=target.scope_id,
                    name=target.name, status="failed", origin="model",
                    retryable=retryable, error_code=code, message=message,
                ))
                continue
            try:
                canonical = validate_canonical_english(result.text)
            except PromptTranslationError as error:
                code, message, retryable = _translation_failure(error)
                rows.append(_result_row(
                    scope_type=target.scope_type, scope_id=target.scope_id,
                    name=target.name, status="failed", origin="model",
                    retryable=retryable, error_code=code, message=message,
                ))
                continue

            # A current value can only be absent here while the project row is
            # locked.  Never assign through a stale snapshot or overwrite a
            # value written by another request before this lock was acquired.
            glossary_changed = _cleanup_replaced_auto_mapping(
                project=locked, target=target, glossary=glossary,
            ) or glossary_changed
            if target.name in glossary:
                rows.append(_result_row(
                    scope_type=target.scope_type, scope_id=target.scope_id,
                    name=target.name, canonical_english=glossary[target.name],
                    status="reused", origin="existing",
                ))
                continue
            glossary[target.name] = canonical
            glossary_changed = True
            rows.append(_result_row(
                scope_type=target.scope_type, scope_id=target.scope_id,
                name=target.name, canonical_english=canonical,
                status="generated", origin="model",
            ))
            generated_records.append((
                target,
                canonical,
                result,
                str((result.metadata or {}).get("translation_context_hash") or (
                    translation_context_hash(
                        _proper_name_context(target, initial_glossary),
                    )
                )),
            ))

        if glossary_changed:
            settings_value["translation_glossary"] = glossary
            ensure_no_canonical_target_collisions(
                locked, production_settings=settings_value,
            )
            locked.production_settings = settings_value
            locked.save(update_fields=("production_settings", "updated_at"))
        # Hash every auto-confirmed name against the same final glossary. A
        # batch must not make its earlier provenance rows stale when a later
        # name in that same batch is appended.
        for target, canonical, result, inference_context_hash in generated_records:
            _record_generated_mapping(
                project=locked, target=target, canonical=canonical,
                result=result, inference_context_hash=inference_context_hash,
                user=user,
            )

        if ensure_visual_identity_translation:
            runtime_items_by_key = {
                item.target.key: item for item in visual_runtime_items
            }
            for target in targets:
                current_entity = current_entities.get(target.key)
                if (
                    current_entity is None
                    or str(current_entity.name or "").strip() != target.name
                    or str(current_entity.visual_identity or "") != target.visual_identity
                ):
                    visual_rows.append(_visual_result_row(
                        target=target, status="conflict", origin="none",
                        retryable=True, error_code="entity_changed",
                        message="翻译期间 Bible 名称或视觉锚点已变化，请按当前内容重试",
                    ))
                    continue
                if not target.visual_identity.strip():
                    visual_rows.append(_visual_result_row(
                        target=target, status="not_required", origin="none",
                        message="视觉锚点为空，未创建英文候选",
                    ))
                    continue
                if not _CJK_RE.search(target.visual_identity):
                    visual_rows.append(_visual_result_row(
                        target=target, status="not_required",
                        origin="source_english",
                        message="视觉锚点已是英文，无需翻译",
                    ))
                    continue

                field_key = _VISUAL_FIELD_KEYS[target.scope_type]
                current_entry = PromptFieldTranslation.objects.select_for_update().filter(
                    project=locked, scope_type=target.scope_type,
                    scope_id=target.scope_id, field_key=field_key,
                ).first()
                current_is_valid = _visual_entry_is_current(
                    current_entry, project=locked, target=target,
                )
                initial_snapshot = visual_initial_snapshots.get(target.key)
                concurrent_change = (
                    _translation_row_snapshot(current_entry) != initial_snapshot
                )
                if concurrent_change:
                    if current_is_valid:
                        visual_rows.append(_visual_result_row(
                            target=target, status="reused", origin="existing",
                            entry=current_entry, context_current=True,
                            message="翻译期间已有更新的当前候选，已保留且未覆盖",
                        ))
                    else:
                        visual_rows.append(_visual_result_row(
                            target=target, status="conflict", origin="existing",
                            entry=current_entry, context_current=False,
                            retryable=True, error_code="translation_changed",
                            message="翻译期间候选被修改；为保护并发编辑未自动覆盖，请刷新后重试",
                        ))
                    continue
                if current_is_valid:
                    visual_rows.append(_visual_result_row(
                        target=target, status="reused", origin="existing",
                        entry=current_entry, context_current=True,
                    ))
                    continue

                result = visual_results.get(target.key)
                if not isinstance(result, TranslationResult):
                    error = result if isinstance(result, PromptTranslationError) else PromptTranslationError(
                        "视觉锚点英文候选未生成", code="translation_failed",
                    )
                    saved = _record_visual_failure(
                        project=locked, target=target, error=error,
                    )
                    code, message, retryable = _translation_failure(error)
                    visual_rows.append(_visual_result_row(
                        target=target, status="failed", origin="model",
                        entry=saved,
                        context_current=_visual_entry_is_current(
                            saved, project=locked, target=target,
                        ),
                        retryable=retryable, error_code=code, message=message,
                    ))
                    continue

                runtime_item = runtime_items_by_key[target.key]
                try:
                    result = _finalize_visual_result(
                        runtime_item, result, glossary=glossary,
                    )
                except PromptTranslationError as error:
                    saved = _record_visual_failure(
                        project=locked, target=target, error=error,
                    )
                    code, message, retryable = _translation_failure(error)
                    visual_rows.append(_visual_result_row(
                        target=target, status="failed", origin="model",
                        entry=saved,
                        context_current=_visual_entry_is_current(
                            saved, project=locked, target=target,
                        ),
                        retryable=retryable, error_code=code, message=message,
                    ))
                    continue
                saved = _record_visual_candidate(
                    project=locked, target=target, result=result,
                )
                visual_rows.append(_visual_result_row(
                    target=target, status="generated", origin="model",
                    entry=saved, context_current=_visual_entry_is_current(
                        saved, project=locked, target=target,
                    ),
                ))

    summary = {
        "requested": len(rows),
        "generated": sum(row["status"] == "generated" for row in rows),
        "reused": sum(row["status"] == "reused" for row in rows),
        "conflict": sum(row["status"] == "conflict" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
    }
    visual_summary = {
        "requested": len(visual_rows),
        "generated": sum(row["status"] == "generated" for row in visual_rows),
        "reused": sum(row["status"] == "reused" for row in visual_rows),
        "not_required": sum(
            row["status"] == "not_required" for row in visual_rows
        ),
        "conflict": sum(row["status"] == "conflict" for row in visual_rows),
        "failed": sum(row["status"] == "failed" for row in visual_rows),
    }
    return {
        "project_id": str(project.pk),
        "updated_at": locked.updated_at,
        "translation_glossary": glossary,
        "summary": summary,
        "entries": rows,
        "visual_summary": visual_summary,
        "visual_translations": visual_rows,
        "runtime": runtime,
    }
