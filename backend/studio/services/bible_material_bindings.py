"""Read the exact original receipt for retained versioned Bible images.

The entity stores only its writer-owned review pointer. No search for a latest
review/version, new selection, media copy, or historical repair happens here.
"""
from django.core.exceptions import ValidationError
from django.db import transaction


def record_binding_review(stage, bindings):
    """Point at the saved original receipt in the same binding transaction."""
    from studio.services.production_execution_units import ENTITY_SCOPE_MODELS

    if not transaction.get_connection().in_atomic_block:
        raise ValidationError("图片回执引用必须与原审核一起提交")
    for binding in bindings:
        facts = [binding["anchor_asset"], *binding["reference_assets"]]
        versioned = any(fact.get("material_fulfillments") or fact.get("material_generations") for fact in facts)
        ENTITY_SCOPE_MODELS[binding["entity_type"]].objects.filter(
            pk=binding["entity_id"], project_id=stage.production_run.project_id,
        ).update(image_binding_review=stage if versioned else None)


def reviewed_binding(entity, entity_type):
    from studio.services.production_runs import _verify_stage_snapshot, _sha256

    if not entity.image_binding_review_id:
        return None
    stage = entity.image_binding_review
    if (stage.stage_key != "bible_image_review"
            or stage.production_run.project_id != entity.project_id):
        raise ValidationError("图片绑定回执不属于本项目的原图片审核")
    snapshot = _verify_stage_snapshot(stage, "output_snapshot")
    if snapshot.get("formal_object_type") == "bible_image_bindings":
        canonical = snapshot.get("canonical")
        if not isinstance(canonical, dict) or _sha256(canonical) != snapshot.get("canonical_sha256"):
            raise ValidationError("图片绑定审核的正式清单摘要不一致")
    elif snapshot.get("kind") in {"bible_image_bindings", "manual_bible_image_review"}:
        # The original writer also reads its pending receipt inside its transaction.
        canonical = snapshot
    else:
        raise ValidationError("图片绑定缺少原审核清单，不能从素材库补造")
    rows = canonical.get("bindings")
    if not isinstance(rows, list):
        raise ValidationError("图片绑定审核清单格式无效")
    matches = [row for row in rows if isinstance(row, dict)
        and (row.get("entity_type"), row.get("entity_id")) == (entity_type, str(entity.pk))]
    if len(matches) != 1:
        raise ValidationError("图片绑定审核没有该实体的唯一确切回执")
    return matches[0]


def read_preserved_material_fact(entity, entity_type, asset_id):
    from studio.services.production_material_references import read_material_result_reference
    from studio.services.production_recipe_material_review import _add_material_fact
    from studio.services.production_runs import ProductionRunConflict

    binding = reviewed_binding(entity, entity_type)
    if binding is None:
        return None
    if not isinstance(binding.get("reference_assets"), list):
        raise ValidationError("原审核的素材清单格式无效")
    facts = [binding.get("anchor_asset"), *binding["reference_assets"]]
    matches = [fact for fact in facts if isinstance(fact, dict) and fact.get("asset_id") == str(asset_id)]
    if not matches:
        return None  # A separately selected non-versioned library image uses its own contract.
    if len(matches) != 1:
        raise ValidationError("原审核重复绑定同一素材")
    fact = matches[0]
    if not fact.get("material_fulfillments") and not fact.get("material_generations"):
        return None
    result, settings = {}, []
    for field in ("material_fulfillments", "material_generations"):
        references = fact.get(field, [])
        if not isinstance(references, list):
            raise ValidationError("原审核的素材版本清单格式无效")
        for reference in references:
            choice = read_material_result_reference(project_id=entity.project_id, field=field, reference=reference)
            if (choice["unit"]["scope"] != {"kind": entity_type, "id": str(entity.pk)}
                    or str(choice["asset"].pk) != str(asset_id)):
                raise ValidationError("原审核素材引用与实体或Asset不一致")
            _add_material_fact(result, field, reference, choice)
            settings.append(choice["version"].setting_revision)
    actual = result[(entity_type, str(entity.pk))]["assets"][str(asset_id)]
    if actual != fact:
        raise ValidationError("保留素材与原审核文件事实不一致")
    for setting in settings:
        primary = setting.primary_setting_revision
        if (setting.variant.revision != setting.revision
                or (primary is not None and primary.variant.revision != primary.revision)):
            raise ProductionRunConflict({"detail": "已绑定素材的确切设定已被后续修订替代。"},
                code="bible_image_source_stale")
    return actual
