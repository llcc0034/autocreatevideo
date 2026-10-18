from __future__ import annotations

import hashlib
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q

from studio.models import (BibleEntityIdentity, BibleVariant, CharacterBible, PropBible, SceneBible,
    ScriptRevision, ScriptBibleEntityBinding, ScriptBibleAppearanceBinding)
from studio.services.json_contracts import canonical_json_bytes
from studio.services.bible_setting_evidence import (
    primary_setting_content_reference, read_entity_primary_settings, setting_revision_rows,
)


def _confirmed_reference_source(*, revision, project_id):
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.production_run_plans import confirmed_script_reference
    from studio.services.script_revision_structures import read_confirmed_script_structure
    if (revision is None or revision.status != ScriptRevision.Status.CONFIRMED or not revision.confirmed_by_id
            or not revision.confirmed_at or revision.source.project_id != project_id):
        raise ValidationError("声明物化需要本项目已明确确认的原始剧本修订")
    reference = confirmed_script_reference(project_id, {"kind": "script_revision",
        "id": str(revision.pk), "expected_sha256": revision.content_sha256})
    return read_confirmed_script_structure(project_id=project_id, reference=reference,
        max_bytes=execution_configuration_max_bytes())


def _declarations(document):
    return document["structure"]["document"]["projection"]["value"]["entities"]


def _appearance_declarations(entity):
    """Only authored appearances; missing creative text is not invented.

    A declared appearance needs its reference name for the existing setting
    writer. An entity without appearances is still an entity, not a fake base
    image. A variant cannot be persisted without its explicitly declared base.
    """
    primary, variants = entity.get("primary"), entity.get("variants", [])
    if variants and primary is None:
        raise ValidationError({f"entities.{entity['key']}.primary": "变体设定需要明确的主图设定声明"})
    rows = ([("", primary)] if primary is not None else []) + [(row["key"], row) for row in variants]
    for key, row in rows:
        if not row.get("reference_name"):
            raise ValidationError({f"entities.{entity['key']}.{key or 'primary'}.reference_name":
                "已声明的主图/变体缺少素材引用名，请在输出字段映射中提供"})
    return rows


def _reference_basis(document):
    return {"schema_version": 3, "kind": "script_reference_bible", "structure_source": document["source"],
        "declaration_sha256": hashlib.sha256(canonical_json_bytes(_declarations(document))).hexdigest()}


def freeze_script_reference_bible(*, revision, project_id, expected_structure_source=None):
    """Small exact-source receipt; authored detail stays in the original script.

    Used by the shared manual/recipe content review. Reading this receipt does not create entities or review decisions.
    """
    document = _confirmed_reference_source(revision=revision, project_id=project_id)
    if expected_structure_source is not None and document["source"] != expected_structure_source:
        raise ValidationError("Bible 输入与本轮已经采用的结构化依据不一致")
    return _reference_basis(document)


def read_script_bible_settings(bindings):
    """Read exact per-source uses, never current/latest settings as a fallback."""
    from studio.services.bible_variants import BibleVariantConflict, _entity_identity, verify_setting
    by_id = {row.pk: row for row in bindings.values()}
    uses = list(ScriptBibleAppearanceBinding.objects.filter(source_binding_id__in=by_id))
    settings = {row.pk: row for row in setting_revision_rows().filter(pk__in={use.setting_id for use in uses})}
    result = {}
    for use in uses:
        binding, setting = by_id[use.source_binding_id], settings.get(use.setting_id)
        if (setting is None or setting.content_sha256 != use.setting_sha256
                or _entity_identity(setting.variant) != _entity_identity(binding)
                or setting.variant.source_variant_key != use.variant_key
                or setting.variant.appearance_role != ("variant" if use.variant_key else "primary")):
            raise BibleVariantConflict("声明引用的确切设定缺失或归属不符；不能改取当前修订")
        origin = setting.variant.source_binding
        if (origin is None and use.variant_key) or (origin and origin.entity_key != binding.entity_key):
            raise BibleVariantConflict("声明引用的原始来源key不符；无声明key的人工变体不能按名称匹配")
        verify_setting(setting)
        result.setdefault(binding.pk, {})[use.variant_key] = (use, setting)
    return result


@transaction.atomic
def materialize_script_reference_bible(*, actor, project_id, script_revision_id, expected_source):
    """Persist explicit declarations once; never infer names or approve media.

    The original manual/recipe review must wrap this writer and its decisions
    in one transaction at cutover. It is not a public API or a new CLI entry.
    """
    from studio.services.bible_variants import (
        BibleVariantConflict, create_declared_appearance, project_for_actor, verify_setting,
    )
    project = project_for_actor(project_id, actor, lock=True)
    revision = ScriptRevision.objects.select_for_update().select_related("source").filter(
        pk=script_revision_id, source__project=project).first()
    if revision is None:
        raise ValidationError("声明剧本修订不存在或不属于本项目")
    document = _confirmed_reference_source(revision=revision, project_id=project.pk)
    basis = _reference_basis(document)
    if canonical_json_bytes(basis) != canonical_json_bytes(expected_source):
        raise BibleVariantConflict("所见声明来源已变化；未创建或覆盖设定")
    declarations = _declarations(document)
    existing = {row.entity_key: row for row in ScriptBibleEntityBinding.objects.select_for_update(of=("self",)).filter(
        script_revision=revision).select_related("character", "scene", "prop")}
    if existing and set(existing) != {item["key"] for item in declarations}:
        raise BibleVariantConflict("声明来源存在不完整或额外的物化绑定；不能增量猜补")
    identities = {row.entity_key: row for row in BibleEntityIdentity.objects.select_for_update(of=("self",)).filter(
        project=project, entity_key__in=[item["key"] for item in declarations]).select_related("character", "scene", "prop")}
    used_settings = read_script_bible_settings(existing) if existing else {}
    current_settings = {}
    if not existing and identities:
        identity_keys = {(kind, getattr(row, f"{kind}_id")): key for key, row in identities.items()
            for kind in ("character", "scene", "prop") if getattr(row, f"{kind}_id")}
        targets = Q(pk__in=[])
        looks = Q(pk__in=[])
        for kind in ("character", "scene", "prop"):
            ids = [getattr(row, f"{kind}_id") for row in identities.values() if getattr(row, f"{kind}_id")]
            targets |= Q(**{f"variant__{kind}_id__in": ids})
            looks |= Q(**{f"{kind}_id__in": ids})
        # Primary identity is unambiguous per entity. Manual variants have no
        # declaration key and are not inferred from a display/reference name.
        reusable = Q(appearance_role="primary") | Q(source_binding__isnull=False)
        expected_looks = set(BibleVariant.objects.filter(looks, reusable, project=project).values_list("pk", flat=True))
        loaded_looks = set()
        for setting in setting_revision_rows().filter(targets,
                Q(variant__appearance_role="primary") | Q(variant__source_binding__isnull=False),
                variant__project=project, revision=F("variant__revision")):
            verify_setting(setting)
            origin = setting.variant.source_binding
            kind = next(kind for kind in ("character", "scene", "prop") if getattr(setting.variant, f"{kind}_id"))
            entity_key = identity_keys.get((kind, getattr(setting.variant, f"{kind}_id")))
            if entity_key is None or (origin and origin.entity_key != entity_key):
                raise BibleVariantConflict("已有设定的来源key与项目实体身份不符")
            current_settings[entity_key, setting.variant.source_variant_key] = setting
            loaded_looks.add(setting.variant_id)
        if loaded_looks != expected_looks:
            raise BibleVariantConflict("已有设定缺少当前确切修订；不能重建或另取最新")
    next_speaker = None
    if any(item["kind"] == "character" and item["key"] not in identities for item in declarations):
        speakers = CharacterBible.objects.filter(project=project).values_list("stable_speaker_id", flat=True)
        next_speaker = max((int(item[1:]) for item in speakers if re.fullmatch(r"S[1-9][0-9]*", item)), default=0) + 1
    model_by_kind = {"character": CharacterBible, "scene": SceneBible, "prop": PropBible}
    result = []
    for entity in declarations:
        declared_appearances = _appearance_declarations(entity)
        digest = hashlib.sha256(canonical_json_bytes(entity)).hexdigest()
        binding = existing.get(entity["key"])
        identity = identities.get(entity["key"])
        if identity and (not getattr(identity, f"{entity['kind']}_id")
                or getattr(identity, entity["kind"]).project_id != project.pk):
            raise BibleVariantConflict(f"实体key {entity['key']} 已关联其他类型或项目，不能按同名替换")
        if binding:
            if (binding.declaration_sha256 != digest or identity is None
                    or getattr(binding, f"{entity['kind']}_id") != getattr(identity, f"{entity['kind']}_id")):
                raise BibleVariantConflict("同一声明key的事实或归属发生漂移；不能覆盖原来源")
            settings = used_settings.get(binding.pk, {})
            if set(settings) != {key for key, _ in declared_appearances}:
                raise BibleVariantConflict("已物化设定不完整或有额外来源；不能自动修复")
            originals = {key: settings[key][1] for key, _ in declared_appearances}
            for key, declaration in declared_appearances:
                if settings[key][0].declaration_sha256 != hashlib.sha256(canonical_json_bytes(declaration)).hexdigest():
                    raise BibleVariantConflict("原始设定引用与声明不符")
        else:
            # Slugs and speaker IDs are server identifiers, never translation/name matching.
            if identity:
                obj = getattr(identity, entity["kind"])
            else:
                slug = "decl-" + hashlib.sha256(f"{project.pk}:{entity['key']}".encode("utf-8")).hexdigest()[:64]
                attributes = dict(project=project, slug=slug, name=entity["name"],
                    description=entity.get("description", ""), is_locked=False)
                if entity["kind"] == "character":
                    attributes["stable_speaker_id"] = f"S{next_speaker}"
                    next_speaker += 1
                obj = model_by_kind[entity["kind"]].objects.create(**attributes)
                identities[entity["key"]] = BibleEntityIdentity.objects.create(project=project,
                    entity_key=entity["key"], **{entity["kind"]: obj})
            binding = ScriptBibleEntityBinding.objects.create(script_revision=revision, entity_key=entity["key"],
                declaration_sha256=digest, **{entity["kind"]: obj})
            originals = {}
            for key, declaration in declared_appearances:
                # A new source references existing creative content; it is not
                # an edit command. Its own declaration stays on its source.
                setting = current_settings.get((entity["key"], key))
                if setting is None:
                    setting = create_declared_appearance(actor=actor, project=project,
                        binding=binding, entity=entity, declaration=declaration,
                        primary_setting_revision=originals[""] if key else None)
                originals[key] = setting
                ScriptBibleAppearanceBinding.objects.create(source_binding=binding, variant_key=key,
                    declaration_sha256=hashlib.sha256(canonical_json_bytes(declaration)).hexdigest(),
                    setting=setting, setting_sha256=setting.content_sha256)
        result.append({"binding_id": str(binding.pk), "entity_key": entity["key"], "entity_kind": entity["kind"],
            "entity_id": str(getattr(binding, f"{entity['kind']}_id")), "declaration_sha256": digest,
            "settings": [{"variant_key": key or None, "appearance_id": str(setting.variant_id),
                "setting_revision_id": str(setting.pk), "setting_sha256": setting.content_sha256}
                for key, setting in originals.items()]})
    return {"source": basis, "entities": result}


def script_bible_review_inventory(project):
    """Read the proposed breakdown basis under the caller's project lock.

    Keep exact primary content references, not a second identity body or media
    readiness. A missing primary is explicit; a broken pointer is an error.
    """
    facts = {}
    common = ("id", "name", "description", "is_locked", "metadata")
    models = (("character", "characters", CharacterBible), ("scene", "scenes", SceneBible),
        ("prop", "props", PropBible))
    for kind, section, model in models:
        fields = common + (("stable_speaker_id",) if kind == "character" else ())
        facts[section] = [{**row, "id": str(row["id"])} for row in
            model.objects.filter(project=project).select_for_update().order_by("id").values(*fields)]
    settings = read_entity_primary_settings(project_id=project.pk,
        entity_keys=[(kind, row["id"]) for kind, section, _ in models for row in facts[section]])
    for kind, section, _ in models:
        for row in facts[section]:
            row["primary_setting_ref"] = primary_setting_content_reference(settings[(kind, row["id"])])
    return facts
