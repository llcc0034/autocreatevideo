"""Output-target facts captured by the actual preparation, not a prompt input.

These are the original fields read/written when promoting an entity image.
Capturing them never adds model context, chooses a reference, or adopts media.
"""
from django.core.exceptions import ValidationError

from studio.services.production_execution_units import ENTITY_SCOPE_MODELS, verify_production_unit
from studio.services.bible_setting_evidence import read_entity_primary_settings, read_setting_revisions
from studio.services.bible_variants import BibleVariantConflict


def _primary_reference(setting):
    return None if setting is None else {"appearance_id": str(setting.variant_id),
        "setting_revision_id": str(setting.pk), "revision": setting.revision, "content_sha256": setting.content_sha256}


def read_bible_target_contents(entities, *, project_id):
    """Read one current primary batch; never copy anchor text into targets."""
    if any(key[0] not in ENTITY_SCOPE_MODELS or not isinstance(entity, ENTITY_SCOPE_MODELS[key[0]])
            or key[1] != str(entity.pk) or str(entity.project_id) != str(project_id) for key, entity in entities.items()):
        raise ValidationError("参考图目标的项目、类型或实体身份不一致")
    try:
        settings = read_entity_primary_settings(project_id=project_id, entity_keys=entities)
    except BibleVariantConflict as exc:
        raise ValidationError(str(exc)) from exc
    return {key: {"scope": {"kind": key[0], "id": key[1]}, "project_id": str(project_id),
        "name": entity.name, "description": entity.description, "primary_setting": _primary_reference(settings[key])}
        for key, entity in entities.items()}


def bible_target_value(entity, scope, reference_asset_ids, *, content=None):
    return {"schema_version": 2, **(content if content is not None else bible_target_content_value(entity, scope["kind"])),
        "anchor_asset_id": str(entity.anchor_asset_id) if entity.anchor_asset_id else None,
        "reference_asset_ids": sorted(str(pk) for pk in reference_asset_ids)}


def capture_bible_target(run, unit):
    unit = verify_production_unit(run, unit)
    kind, identifier = unit["scope"]["kind"], unit["scope"]["id"]
    if kind not in ENTITY_SCOPE_MODELS:
        raise ValidationError("参考图结果须明确归属角色、场景或道具")
    entity = ENTITY_SCOPE_MODELS[kind].objects.select_for_update().only(
        "id", "project_id", "name", "description", "anchor_asset_id",
    ).filter(pk=identifier, project_id=run.project_id).first()
    if entity is None:
        raise ValidationError("参考图目标已不存在或不属于本项目，请重新选择")
    return bible_target_value(entity, unit["scope"], entity.reference_assets.values_list("id", flat=True))


def verify_bible_target(value, unit):
    """Validate the frozen target independently of its mutable current content."""
    from uuid import UUID

    if (not isinstance(value, dict) or set(value) != {"schema_version", "scope", "project_id",
            "name", "description", "primary_setting", "anchor_asset_id", "reference_asset_ids"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 2
            or unit["scope"]["kind"] not in ENTITY_SCOPE_MODELS
            or value["scope"] != unit["scope"] or value["project_id"] != unit["project_id"]
            or any(not isinstance(value[key], str) for key in ("name", "description"))
            or not isinstance(value["reference_asset_ids"], list)):
        raise ValidationError("准备缺少完整且归属一致的参考图目标基线，不能从当前实体补造")
    references = value["reference_asset_ids"]
    try:
        if (references != sorted(set(references)) or any(str(UUID(pk)) != pk for pk in references)
                or (value["anchor_asset_id"] is not None and str(UUID(value["anchor_asset_id"])) != value["anchor_asset_id"])):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("参考图目标基线的原素材关系无效") from exc
    primary = value["primary_setting"]
    if primary is not None:
        try:
            if (not isinstance(primary, dict) or set(primary) != {"appearance_id", "setting_revision_id", "revision", "content_sha256"}
                    or type(primary["revision"]) is not int or primary["revision"] < 1
                    or any(str(UUID(primary[key])) != primary[key] for key in ("appearance_id", "setting_revision_id"))):
                raise ValueError
            setting = read_setting_revisions([primary["setting_revision_id"]], project_id=value["project_id"])[primary["setting_revision_id"]]
            if (primary != _primary_reference(setting) or setting.variant.appearance_role != "primary"
                    or str(getattr(setting.variant, f'{unit["scope"]["kind"]}_id')) != unit["scope"]["id"]):
                raise ValueError
        except (ValueError, TypeError, AttributeError, BibleVariantConflict) as exc:
            raise ValidationError("参考图目标的确切主图设定无效，不能改用当前修订") from exc
    return value


def is_recipe_variant_candidate(artifact):
    """Separate declared variant results from entity-primary review/history."""
    if artifact.production_run.plan_source != "recipe":
        return False
    attempt = artifact.generation_attempt
    if (attempt is None or attempt.preparation_id is None or attempt.stage_run_id != artifact.stage_run_id
            or artifact.stage_run.production_run_id != artifact.production_run_id
            or attempt.preparation.stage_run_id != artifact.stage_run_id):
        raise ValidationError("外观候选缺少同轮原执行准备，不能猜测主图或变体")
    from studio.services.media_generation_targets import read_media_generation_target
    appearance = read_media_generation_target(attempt.preparation).value.get("appearance")
    if appearance and appearance["role"] == "variant":
        from studio.services.production_recipe_material_inputs import read_recipe_material_binding
        read_recipe_material_binding(attempt.preparation)
        return True
    return False


def read_bible_candidate_target(artifact):
    """Read a candidate's own frozen target, independently of result status.

    History can show an attributable failed/late result; adoption must also
    call the common media verifier. Neither reader repairs an old identity.
    """
    from studio.services.execution_preparations import read_execution_preparation_detail

    attempt = artifact.generation_attempt
    if (artifact.kind != "image" or artifact.stage_run is None
            or artifact.stage_run.stage_key != "bible_image_generation"
            or artifact.stage_run.production_run_id != artifact.production_run_id
            or attempt is None or attempt.preparation_id is None or attempt.role != "primary"
            or attempt.stage_run_id != artifact.stage_run_id):
        raise ValidationError("实体参考图缺少同一运行步骤的真实主调用及执行准备")
    preparation = attempt.preparation
    detail = read_execution_preparation_detail(preparation)
    unit = verify_production_unit(artifact.production_run, detail.resolved_inputs.get("production_unit"))
    target = verify_bible_target(detail.resolved_inputs.get("bible_target"), unit)
    if (preparation.stage_run_id != artifact.stage_run_id or preparation.purpose != "production"
            or preparation.execution_instance_version_id is None
            or preparation.execution_instance_version_id != attempt.execution_instance_version_id
            or preparation.request_frozen_at is None or len(preparation.request_sha256) != 64
            or preparation.request_sha256 != attempt.request_sha256):
        raise ValidationError("实体参考图归属与冻结请求不一致")
    return preparation, target


def bible_target_content_matches(target, entity, *, content=None):
    current = content if content is not None else bible_target_content_value(entity, target["scope"]["kind"])
    return all(target[key] == value for key, value in current.items())


def bible_target_content_value(entity, kind):
    key = (kind, str(entity.pk))
    return read_bible_target_contents({key: entity}, project_id=entity.project_id)[key]
