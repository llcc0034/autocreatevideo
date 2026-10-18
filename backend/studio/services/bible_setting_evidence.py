"""Exact setting references shared by creative approval snapshots and detail APIs.

No current-script fallback, media selection, mutation, or model invocation.
Bodies are verified in batches but kept out of the compact approval projection.
"""
from django.db.models import F, Q

from studio.models import BibleVariant, BibleVariantRevision
from studio.services.bible_variants import BibleVariantConflict, verify_setting


def setting_revision_rows():
    """One exact-revision body query, including fixed primary and source identity.

    Source bodies and the entity's old visual text are deliberately not loaded.
    Callers select explicit IDs or a stored revision number, never latest().
    """
    setting_fields = ("id", "variant_id", "revision", "name", "reference_name", "identity_anchor",
        "appearance_anchor", "basis_asset_id", "basis_asset_sha256", "payload_schema_version",
        "source_declaration_sha256", "primary_setting_revision_id", "content_sha256", "created_by_id", "created_at")
    variant_fields = ("id", "project_id", "character_id", "scene_id", "prop_id", "appearance_role",
        "source_variant_key", "source_binding_id")
    binding_fields = ("id", "entity_key", "declaration_sha256", "character_id", "scene_id", "prop_id",
        "script_revision_id", "script_revision__source_id", "script_revision__source__project_id")
    review_fields = ("id", "setting_id", "decision", "comment", "decided_by_id", "created_at")
    paths = ("variant__character", "variant__scene", "variant__prop",
        "variant__source_binding__script_revision__source", "review")
    fields = (*setting_fields, *(f"variant__{key}" for key in variant_fields),
        *(f"variant__{kind}__project_id" for kind in ("character", "scene", "prop")),
        *(f"variant__source_binding__{key}" for key in binding_fields),
        *(f"review__{key}" for key in review_fields))
    return BibleVariantRevision.objects.select_related(*paths,
        *(f"primary_setting_revision__{path}" for path in paths)).only(*fields,
        *(f"primary_setting_revision__{field}" for field in fields))


def read_setting_revisions(revision_ids, *, project_id):
    """Read verified bodies for exact references; absence is never an empty body."""
    identifiers = {str(identifier) for identifier in revision_ids}
    if not identifiers:
        return {}
    records = {str(row.pk): row for row in setting_revision_rows().filter(
        pk__in=identifiers, variant__project_id=project_id)}
    if set(records) != identifiers:
        raise BibleVariantConflict("所需确切设定修订缺失或不属于本项目；不能读取当前修订替代")
    for setting in records.values():
        verify_setting(setting)
    return records


def read_entity_primary_setting(*, project_id, entity_kind, entity_id):
    """Select the stored current primary, then use the shared exact-body reader.

    This is for a new current-content use, not a way to refresh a frozen use.
    No primary is an explicit absence; a broken current pointer is an error.
    Writers using the result must hold the existing project/entity transaction.
    """
    return read_entity_primary_settings(project_id=project_id,
        entity_keys=[(entity_kind, str(entity_id))])[(entity_kind, str(entity_id))]


def read_entity_primary_settings(*, project_id, entity_keys):
    """Batch the same current-pointer contract for an explicit entity set."""
    keys = {(kind, str(identifier)) for kind, identifier in entity_keys}
    if any(kind not in {"character", "scene", "prop"} for kind, _ in keys):
        raise BibleVariantConflict("主图设定必须属于明确的人物、场景或道具")
    if not keys:
        return {}
    targets = Q(pk__in=[])
    for kind in ("character", "scene", "prop"):
        targets |= Q(**{f"{kind}_id__in": [identifier for scope, identifier in keys if scope == kind]})
    rows = list(BibleVariant.objects.filter(targets, project_id=project_id, appearance_role="primary").values(
        "id", "revision", "name", "reference_name", "character_id", "scene_id", "prop_id"))
    result = dict.fromkeys(keys)
    if not rows:
        return result
    settings = {row.variant_id: row for row in setting_revision_rows().filter(
        variant_id__in=[row["id"] for row in rows], revision=F("variant__revision"))}
    for pointer in rows:
        kind = next(kind for kind in ("character", "scene", "prop") if pointer[f"{kind}_id"])
        key = (kind, str(pointer[f"{kind}_id"]))
        if result[key] is not None:
            raise BibleVariantConflict("实体主图设定不唯一；不能猜取其中一个")
        setting = settings.get(pointer["id"])
        if setting is None or setting.revision != pointer["revision"]:
            raise BibleVariantConflict("主图设定当前修订缺失或已变化；不能读取旧修订替代")
        verify_setting(setting)
        if (str(setting.variant.project_id) != str(project_id) or setting.variant.appearance_role != "primary"
                or str(getattr(setting.variant, f"{kind}_id")) != key[1]
                or (setting.name, setting.reference_name) != (pointer["name"], pointer["reference_name"])):
            raise BibleVariantConflict("主图设定当前归属或名称与修订不一致")
        result[key] = setting
    return result


def primary_setting_content_reference(setting):
    """Compact content identity, without a second writable body or review state."""
    if setting is None:
        return None
    return {"appearance_id": str(setting.variant_id), "setting_revision_id": str(setting.pk),
        "revision": setting.revision, "content_sha256": setting.content_sha256}


def primary_setting_content(setting):
    """Project an already verified primary for an explicitly selected input.

    This value is frozen request content, not a second writable setting. Review
    and media state are deliberately not part of its content identity.
    """
    if setting is None:
        return None
    return {**primary_setting_content_reference(setting),
        "identity_anchor": setting.identity_anchor, "appearance_anchor": setting.appearance_anchor}


def source_reference(variant):
    binding = variant.source_binding
    if binding is None:
        return None
    return {"binding_id": str(binding.pk), "script_revision_id": str(binding.script_revision_id),
        "entity_key": binding.entity_key, "variant_key": variant.source_variant_key or None,
        "declaration_sha256": binding.declaration_sha256}


def _review_reference(setting):
    review = getattr(setting, "review", None)
    return ({"id": str(review.pk), "decision": review.decision,
        "decided_by_id": str(review.decided_by_id)} if review else None)


def setting_reference(setting):
    primary = setting.primary_setting_revision
    return {"appearance_id": str(setting.variant_id), "appearance_role": setting.variant.appearance_role,
        "setting_revision_id": str(setting.pk), "revision": setting.revision,
        "content_sha256": setting.content_sha256, "payload_schema_version": setting.payload_schema_version,
        "name": setting.name, "reference_name": setting.reference_name,
        "source": source_reference(setting.variant), "source_declaration_sha256": setting.source_declaration_sha256,
        "primary_setting": ({"appearance_id": str(primary.variant_id), "setting_revision_id": str(primary.pk),
            "revision": primary.revision, "content_sha256": primary.content_sha256,
            "review": _review_reference(primary)} if primary else None),
        "review": _review_reference(setting)}


def project_setting_evidence(project_id):
    """Bounded-query graph, including old exact primary revisions, never latest."""
    entity_fields = [field for kind in ("character", "scene", "prop") for field in (f"{kind}__id", f"{kind}__project_id")]
    variants = list(BibleVariant.objects.filter(project_id=project_id).select_related(
        "character", "scene", "prop", "source_binding__script_revision__source").only(
        "id", "project_id", "character_id", "scene_id", "prop_id", "appearance_role", "source_variant_key",
        "name", "reference_name", "revision", *entity_fields, "source_binding__id", "source_binding__entity_key",
        "source_binding__declaration_sha256", "source_binding__character_id", "source_binding__scene_id",
        "source_binding__prop_id", "source_binding__script_revision__id", "source_binding__script_revision__source__id",
        "source_binding__script_revision__source__project_id").order_by("id"))
    if not variants:
        return {}
    by_variant = {row.pk: row for row in variants}
    fields = ("id", "variant_id", "revision", "name", "reference_name", "identity_anchor", "appearance_anchor",
        "basis_asset_id", "basis_asset_sha256", "payload_schema_version", "source_declaration_sha256",
        "primary_setting_revision_id", "content_sha256", "review__id", "review__setting_id", "review__decision",
        "review__decided_by_id")
    current = list(BibleVariantRevision.objects.filter(variant__project_id=project_id,
        revision=F("variant__revision")).select_related("review").only(*fields).order_by("variant_id"))
    if {row.variant_id for row in current} != set(by_variant):
        raise BibleVariantConflict("设定当前修订缺失；不能用旧修订补齐审核基线")
    settings = {row.pk: row for row in current}
    missing = {row.primary_setting_revision_id for row in current if row.primary_setting_revision_id} - set(settings)
    if missing:
        settings.update({row.pk: row for row in BibleVariantRevision.objects.filter(pk__in=missing,
            variant__project_id=project_id).select_related("review").only(*fields)})
    for setting in settings.values():
        if setting.variant_id not in by_variant or (setting.primary_setting_revision_id
                and setting.primary_setting_revision_id not in settings):
            raise BibleVariantConflict("确切主图设定不属于本项目或已缺失；不能自动选取其他主图")
        setting.variant = by_variant[setting.variant_id]
        setting.primary_setting_revision = settings.get(setting.primary_setting_revision_id)
    result = {}
    for setting in current:
        verify_setting(setting)
        variant = setting.variant
        if (setting.name, setting.reference_name) != (variant.name, variant.reference_name):
            raise BibleVariantConflict("设定当前名称与修订不一致；不能用展示值覆盖原正文")
        kind = next(kind for kind in ("character", "scene", "prop") if getattr(variant, f"{kind}_id"))
        result.setdefault((kind, str(getattr(variant, f"{kind}_id"))), []).append(setting_reference(setting))
    return result


def attach_project_setting_evidence(canonical, project_id):
    evidence = project_setting_evidence(project_id)
    for kind, section in (("character", "characters"), ("scene", "scenes"), ("prop", "props")):
        for row in canonical[section]:
            row["appearance_settings"] = evidence.pop((kind, row["id"]), [])
    if evidence:
        raise BibleVariantConflict("设定归属不在本次Bible审核集合中")
    return canonical
