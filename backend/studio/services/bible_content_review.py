"""One explicit content-review inventory for manual and recipe production.

Declarations are read from their original receipt. Stored current settings and
their exact (possibly historical) primary dependencies are displayed separately.
Only current settings are approved; history and prior decisions are never rewritten.
"""
from hashlib import sha256

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import BibleEntityIdentity, ProductionRun, ScriptBibleEntityBinding, ScriptRevision, SeriesProject
from studio.services.bible_variants import BibleVariantConflict, review_variant, setting_payload
from studio.services.bible_setting_evidence import read_setting_revisions, setting_reference
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, canonical_json_bytes, json_contract
from studio.services.script_bible_materialization import (
    _appearance_declarations, _confirmed_reference_source, _declarations, _reference_basis, materialize_script_reference_bible,
)


def _key(*parts):
    return sha256(canonical_json_bytes(list(parts))).hexdigest()


def _body(setting):
    return {"id": str(setting.pk), "revision": setting.revision, "content_sha256": setting.content_sha256,
        **setting_payload(setting)}


def bible_review_inventory(*, run, frozen=None):
    """Call under the project lock when preparing a write; this function is read-only."""
    from studio.services.production_runs import _bible_creative_drift_projection, _canonical_bible
    baseline = json_contract(_bible_creative_drift_projection(_canonical_bible(run)),
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    entities = {row["id"]: row for group in baseline.value.values() for row in group}
    references = [(kind, row, ref) for kind, section in
        (("character", "characters"), ("scene", "scenes"), ("prop", "props"))
        for row in baseline.value[section] for ref in row.get("appearance_settings", [])]
    ids = {ref["setting_revision_id"] for _, _, ref in references}
    ids.update(ref["primary_setting"]["setting_revision_id"] for _, _, ref in references if ref["primary_setting"])
    records = read_setting_revisions(ids, project_id=run.project_id)
    settings = []
    for kind, entity, ref in references:
        setting = records[ref["setting_revision_id"]]
        if setting_reference(setting) != ref:
            raise BibleVariantConflict("审核读取期间设定已变化")
        primary = records[ref["primary_setting"]["setting_revision_id"]] if ref["primary_setting"] else None
        settings.append({"review_key": f"setting:{setting.pk}", "kind": "stored", "entity_type": kind,
            "entity_name": entity["name"], "entity_key": ref["source"]["entity_key"] if ref["source"] else None,
            "appearance_role": ref["appearance_role"], "variant_key": ref["source"]["variant_key"] if ref["source"] else None,
            "name": setting.name, "reference_name": setting.reference_name,
            "identity_anchor": setting.identity_anchor, "appearance_anchor": setting.appearance_anchor,
            "source_facts": None, "setting": {**ref, **_body(setting)},
            "primary_setting": {**ref["primary_setting"], **_body(primary)} if primary else None})
    candidates = []
    if frozen is not None:
        source = frozen.get("structure_source") if isinstance(frozen, dict) else None
        if (not isinstance(frozen, dict) or frozen.get("schema_version") != 3
                or frozen.get("kind") != "script_reference_bible" or not isinstance(source, dict)
                or not isinstance(source.get("script"), dict)):
            raise BibleVariantConflict("Bible 声明来源不是本轮采用的原始剧本")
        revision = ScriptRevision.objects.select_related("source").filter(
            pk=source["script"].get("id"), source__project_id=run.project_id).first()
        if revision is None:
            raise BibleVariantConflict("原声明剧本不存在或不属于本项目")
        document = _confirmed_reference_source(revision=revision, project_id=run.project_id)
        if canonical_json_bytes(_reference_basis(document)) != canonical_json_bytes(frozen):
            raise BibleVariantConflict("Bible 声明与冻结来源不一致")
        bindings = {row.entity_key: row for row in ScriptBibleEntityBinding.objects.filter(script_revision=revision)}
        declarations = _declarations(document)
        identities = {row.entity_key: row for row in BibleEntityIdentity.objects.filter(
            project_id=run.project_id, entity_key__in=[item["key"] for item in declarations])}
        if bindings and set(bindings) != {row["key"] for row in declarations}:
            raise BibleVariantConflict("声明入库关系不完整；不能猜测或补造")
        for index, entity in enumerate(declarations):
            binding = bindings.get(entity["key"])
            digest = sha256(canonical_json_bytes(entity)).hexdigest()
            existing = None
            identity = identities.get(entity["key"])
            if identity:
                entity_id = getattr(identity, f"{entity['kind']}_id")
                existing = entities.get(str(entity_id))
                if existing is None:
                    raise BibleVariantConflict(f"实体key {entity['key']} 已关联其他类型或项目")
            if binding and (identity is None or binding.declaration_sha256 != digest
                    or getattr(binding, f"{entity['kind']}_id") != getattr(identity, f"{entity['kind']}_id")):
                raise BibleVariantConflict("声明绑定与本项目实体或原始内容不一致")
            candidate_id = _key(str(revision.pk), entity["key"])
            candidates.append({"id": candidate_id, "entity_type": entity["kind"], "name": entity["name"],
                "description": entity.get("description", ""), "identity_anchor": entity.get("identity_anchor"),
                "action": "reuse" if identity else "create", "conflict_code": None, "existing": existing})
            for variant_key, declaration in _appearance_declarations(entity):
                variant_key = variant_key or None
                settings.append({"review_key": f"declaration:{_key(str(revision.pk), entity['key'], variant_key)}",
                    "kind": "declaration", "entity_type": entity["kind"], "entity_name": entity["name"],
                    "entity_key": entity["key"], "appearance_role": "primary" if variant_key is None else "variant",
                    "variant_key": variant_key, "name": entity["name"] if variant_key is None else declaration["name"], "reference_name": declaration["reference_name"],
                    "identity_anchor": entity.get("identity_anchor", "") if variant_key is None else "",
                    "appearance_anchor": declaration.get("anchor", ""),
                    "source_facts": {"script_revision_id": str(revision.pk), "already_materialized": binding is not None,
                        "structure_source": source, "semantic_path": f"value.entities[{index}]",
                        "description": entity.get("description"), "identity_anchor": entity.get("identity_anchor"),
                        "appearance_anchor": declaration.get("anchor"),
                        "primary_reference_name": entity["primary"]["reference_name"]},
                    "setting": None, "primary_setting": None})
    return baseline, candidates, settings


def require_reviewed_set(selected, expected, label):
    if (not isinstance(selected, list) or any(not isinstance(item, str) for item in selected)
            or len(selected) != len(set(selected)) or set(selected) != set(expected)):
        raise ValidationError(f"必须逐项核对完整{label}集合；不能遗漏、重复或加入其他项")


def recipe_bible_review_source(run):
    from studio.services.production_runs import _verify_stage_snapshot
    stages = {stage.stage_key: stage for stage in run.stages.filter(stage_key__in=("bible_materialize", "bible_review"))}
    review = stages.get("bible_review")
    if run.plan_source != "recipe" or review is None:
        raise ValidationError("此运行没有配方Bible审核阶段")
    source = stages.get("bible_materialize")
    if source is None:
        if review.mode != "manual":
            raise ValidationError("Bible声明审核缺少来源阶段")
        return None, None
    if source.status != "succeeded" or review.mode != "review":
        raise ValidationError("Bible声明来源尚未完成或审核模式不一致")
    snapshot = _verify_stage_snapshot(source, "output_snapshot")
    frozen = snapshot.get("review_payload")
    if (snapshot.get("formal_object_type") != "" or not isinstance(frozen, dict)
            or frozen.get("kind") != "script_reference_bible"
            or frozen.get("schema_version") != 3 or not isinstance(frozen.get("structure_source"), dict)
            or not isinstance(frozen["structure_source"].get("script"), dict)
            or frozen["structure_source"]["script"].get("id") != str(run.script_revision_id)):
        raise BibleVariantConflict("Bible声明来源快照无效或已不属于本轮剧本")
    return frozen, source.output_snapshot_sha256


@transaction.atomic
def recipe_bible_review_detail(run):
    from studio.services.production_run_plans import get_run_execution_plan
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    SeriesProject.objects.select_for_update().get(pk=run.project_id)
    frozen, source_sha = recipe_bible_review_source(run)
    baseline, candidates, settings = bible_review_inventory(run=run, frozen=frozen)
    return {"plan_source": "recipe", "run_id": str(run.pk), "run_plan_sha256": get_run_execution_plan(run).sha256,
        "source_snapshot_sha256": source_sha, "project_bible": baseline.value,
        "project_bible_sha256": baseline.sha256, "candidates": candidates, "settings": settings}


@transaction.atomic
def approve_bible_content(*, run, actor, frozen, expected_project_bible_sha256,
        reviewed_candidate_ids, reviewed_setting_keys, reviewed_project_bible, comment):
    """Must be wrapped by the original stage decision/receipt transaction."""
    from studio.services.bible_variants import project_for_actor
    project_for_actor(run.project_id, actor, lock=True)
    baseline, candidates, settings = bible_review_inventory(run=run, frozen=frozen)
    if baseline.sha256 != expected_project_bible_sha256:
        raise BibleVariantConflict("项目设定在审核期间已变化；请重新读取完整内容")
    if reviewed_project_bible is not True:
        raise ValidationError("请明确核对项目的完整创作设定")
    require_reviewed_set(reviewed_candidate_ids, [row["id"] for row in candidates], "实体声明")
    require_reviewed_set(reviewed_setting_keys, [row["review_key"] for row in settings], "主图与变体设定")
    materialization = _apply_bible_content(run=run, actor=actor, frozen=frozen, settings=settings,
        human=True, comment=comment)
    return {"schema_version": 1, "kind": "bible_content_review", "materialization": materialization,
        "material_requirements": _material_requirements(run, materialization),
        "reviewed_candidate_ids": sorted(reviewed_candidate_ids), "reviewed_setting_keys": sorted(reviewed_setting_keys),
        "before_project_bible_sha256": baseline.sha256}


def _material_requirements(run, materialization):
    from studio.services.bible_material_requirements import build_bible_material_requirements
    from studio.services.production_runs import _canonical_bible
    return build_bible_material_requirements(project_id=run.project_id, canonical=_canonical_bible(run),
        materialization=materialization).value


def _apply_bible_content(*, run, actor, frozen, settings, human, comment=""):
    """One materializer for both decisions; policy never writes human reviews."""
    current_ids = {row["setting"]["setting_revision_id"] for row in settings if row["kind"] == "stored"}
    known_appearances = {row["setting"]["appearance_id"] for row in settings if row["kind"] == "stored"}
    materialization = None
    if frozen is not None:
        materialization = materialize_script_reference_bible(actor=actor, project_id=run.project_id,
            script_revision_id=run.script_revision_id, expected_source=frozen)
        current_ids.update(setting["setting_revision_id"] for entity in materialization["entities"]
            for setting in entity["settings"] if setting["appearance_id"] not in known_appearances)
    records = read_setting_revisions(current_ids, project_id=run.project_id)
    for identifier in sorted(records):
        setting = records[identifier]
        existing = getattr(setting, "review", None)
        if existing:
            if existing.decision != "approve":
                raise BibleVariantConflict("有已拒绝的当前设定；请另存修订后重新审核，不覆盖旧决定")
            continue
        if human:
            review_variant(actor=actor, variant=setting.variant, expected_revision=setting.revision,
                expected_sha256=setting.content_sha256, decision="approve", comment=comment)
        else:
            from studio.services.bible_variants import _basis
            _basis(run.project, setting_payload(setting))
    if materialization is not None:
        from studio.models import CharacterBible, SceneBible, PropBible
        from studio.services.consistency import sync_consistency_locks
        for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible)):
            ids = [row["entity_id"] for row in materialization["entities"] if row["entity_kind"] == kind]
            model.objects.filter(project_id=run.project_id, pk__in=ids, is_locked=False).update(is_locked=True)
        sync_consistency_locks(run.project)
    return materialization


@transaction.atomic
def apply_policy_bible_content(*, run, stage):
    """Called inside the original policy-decision transaction, not a public action."""
    from studio.services.bible_variants import project_for_actor
    from studio.services.production_runs import _verified_unattended_decision_policy
    if stage.production_run_id != run.pk or stage.stage_key != "bible_review":
        raise ValidationError("Bible策略阶段不属于当前运行")
    _verified_unattended_decision_policy(run, stage)
    project_for_actor(run.project_id, run.owner, lock=True)
    frozen, source_sha = recipe_bible_review_source(run)
    baseline, _, settings = bible_review_inventory(run=run, frozen=frozen)
    materialization = _apply_bible_content(run=run, actor=run.owner, frozen=frozen, settings=settings, human=False)
    return {"schema_version": 1, "kind": "bible_content_policy", "source_snapshot_sha256": source_sha,
        "before_project_bible_sha256": baseline.sha256, "materialization": materialization,
        "material_requirements": _material_requirements(run, materialization)}
