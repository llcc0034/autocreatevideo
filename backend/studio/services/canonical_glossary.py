"""Explicit adoption of reviewed Bible names into the project's sole glossary.

Generation uses ProjectTranslationRequest and the original Job/Preparation.
This module performs no inference, selects no deployment and never auto-fills.
"""
import re

from django.db import transaction
from django.utils import timezone

from studio.models import CharacterBible, SceneBible, PropBible, PromptFieldTranslation
from studio.services.configuration import resolve_configuration
from studio.services.content_contracts import CANONICAL_NAME_POLICY_KEY, canonical_name_policy
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import json_contract
from studio.services.prompt_translation import (
    TranslationValidationError, append_translation_history, build_translation_context,
    ensure_no_canonical_target_collisions, source_text_hash, translation_context_hash,
    TRANSLATION_CONTEXT_VERSION,
)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CANONICAL_ENGLISH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[ .'-][A-Za-z0-9]+)*$")
_SCOPE_MODELS = {"character": CharacterBible, "scene": SceneBible, "prop": PropBible}


def is_canonical_name(scope_type, field_key):
    return scope_type in _SCOPE_MODELS and field_key == f"{scope_type}.name"


def project_glossary(project):
    settings = project.production_settings
    if not isinstance(settings, dict):
        raise TranslationValidationError("项目生产配置不是有效对象")
    glossary = settings.get("translation_glossary", {})
    if not isinstance(glossary, dict) or any(
        not isinstance(key, str) or not key.strip() or key != key.strip()
        or not isinstance(value, str) or not value.strip() for key, value in glossary.items()
    ):
        raise TranslationValidationError("项目术语表必须包含有效的名称与英文译名")
    return dict(glossary)


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


def confirm_canonical_candidate(*, project, entry, expected_revision, expected_canonical, expected_project_updated_at, candidate, user):
    """Caller holds project then translation locks. All writes are one transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise TranslationValidationError("术语确认必须在项目事务中执行")
    if not is_canonical_name(entry.scope_type, entry.field_key):
        raise TranslationValidationError("只有人物、场景、道具名称可以写入术语表")
    entity = _SCOPE_MODELS[entry.scope_type].objects.select_for_update().filter(
        project=project, pk=entry.scope_id
    ).only("id", "name").first()
    if entity is None or entity.name != entry.source_text or source_text_hash(entity.name) != entry.source_hash:
        raise TranslationValidationError("名称来源已变化，请重新读取候选", code="translation_source_conflict")
    glossary = project_glossary(project)
    current_value = glossary.get(entity.name)
    metadata = entry.runtime_metadata
    if not isinstance(metadata, dict):
        raise TranslationValidationError("候选上下文不是有效对象，请重新准备候选", code="translation_source_conflict")
    context = build_translation_context(project, scope_type=entry.scope_type,
        scope_id=entry.scope_id, field_key=entry.field_key, source_text=entity.name)
    if (type(metadata.get("translation_context_version")) is not int
        or metadata["translation_context_version"] != TRANSLATION_CONTEXT_VERSION
        or metadata.get("translation_context_hash") != translation_context_hash(context)):
        raise TranslationValidationError("名称说明或关联术语已变化，请重新准备候选", code="translation_source_conflict")
    policy = resolve_configuration(project=project, required_keys=(CANONICAL_NAME_POLICY_KEY,))
    name_policy = canonical_name_policy(values=policy.values)
    candidate = validate_canonical_english(candidate, policy=name_policy)
    receipt = metadata.get("canonical_adoption", {})
    if (isinstance(receipt, dict) and receipt.get("applied_from_revision") == expected_revision
        and receipt.get("previous_value") == expected_canonical and receipt.get("value") == candidate
        and receipt.get("previous_project_updated_at") == expected_project_updated_at.isoformat()
        and receipt.get("applied_revision") == entry.revision
        and current_value == candidate and entry.translated_text == candidate
        and entry.status == PromptFieldTranslation.Status.CONFIRMED):
        return entry
    if (type(expected_revision) is not int or entry.revision != expected_revision or current_value != expected_canonical
            or project.updated_at != expected_project_updated_at):
        raise TranslationValidationError("候选修订或已配置术语发生变化，不会覆盖人工修改", code="translation_source_conflict")
    if entry.status not in (PromptFieldTranslation.Status.DRAFT_TRANSLATION, PromptFieldTranslation.Status.CONFIRMED):
        raise TranslationValidationError("只有待审核或已确认候选可用于术语确认")
    glossary[entity.name] = candidate
    settings = {**project.production_settings, "translation_glossary": glossary}
    ensure_no_canonical_target_collisions(project, production_settings=settings)
    json_contract(settings, max_bytes=execution_configuration_max_bytes())
    append_translation_history(entry, event="confirm_canonical_glossary", actor=user)
    entry.revision += 1
    project.production_settings = settings
    entry.translated_text, entry.status = candidate, PromptFieldTranslation.Status.CONFIRMED
    entry.error_message = ""
    entry.confirmed_by, entry.confirmed_at = user, timezone.now()
    # Keep inference evidence separate from the post-adoption review context.
    entry.runtime_metadata = {**metadata,
        "canonical_adoption": {"applied_from_revision": expected_revision, "applied_revision": entry.revision,
            "previous_project_updated_at": expected_project_updated_at.isoformat(),
            "name_policy": {"max_characters": name_policy["max_characters"], "max_words": name_policy["max_words"]},
            "configuration_sha256": policy.sha256,
            "previous_value": current_value, "value": candidate, "source_context_sha256": translation_context_hash(context)},
        "translation_context_hash": translation_context_hash(build_translation_context(project,
            scope_type=entry.scope_type, scope_id=entry.scope_id, field_key=entry.field_key, source_text=entity.name)),
    }
    json_contract(entry.revision_history, max_bytes=execution_configuration_max_bytes())
    json_contract(entry.runtime_metadata, max_bytes=execution_configuration_max_bytes())
    project.save(update_fields=("production_settings", "updated_at"))
    entry.runtime_metadata["canonical_adoption"]["project_updated_at"] = project.updated_at.isoformat()
    json_contract(entry.runtime_metadata, max_bytes=execution_configuration_max_bytes())
    entry.save()
    return entry
