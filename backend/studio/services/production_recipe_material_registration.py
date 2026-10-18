"""Selected recipe appearance -> original Asset/version, not Bible adoption."""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework.exceptions import ValidationError as APIValidationError
from studio.models import ArtifactSelectionDecision

from studio.services.asset_references import register_generated_material, read_generated_material
from studio.services.execution_preparation_owners import locked_preparation_owners, locked_preparation_owner_rows
from studio.services.production_media_assets import register_production_image_asset
from studio.services.production_recipe_material_selection import read_recipe_material_selection, read_recipe_primary_dependency


logger = logging.getLogger(__name__)


def register_recipe_material_selection(*, stage_run_id, unit_key, decision_id, actor, asset_guard):
    """Caller supplies the file guard around its outer transaction/commit."""
    if decision_id is None:
        raise ValidationError("素材登记必须提供确切选择决定")
    with locked_preparation_owners([("stage_run_id", stage_run_id)], actor) as owners:
        stage = owners[("stage_run_id", str(stage_run_id))]
        if not stage.production_run.owner.is_active or stage.production_run.status != "running" or stage.status != "running":
            raise ValidationError("只有有效所有者的运行中素材阶段可以登记素材")
        selection = read_recipe_material_selection(stage=stage, unit_key=unit_key, decision_id=decision_id)
        if selection is None:
            raise ValidationError("必须明确本轮已选素材，不能登记未选择候选")
        from studio.services.production_runs import _locked_bible_entity
        scope = selection["unit"]["scope"]
        entity = _locked_bible_entity(stage.production_run, scope["kind"], scope["id"])
        artifact = selection["artifact"]
        asset = register_production_image_asset(artifact=artifact, kind=scope["kind"],
            name=entity.name, description=entity.description, asset_guard=asset_guard)
        artifact.asset = asset
        version, _ = register_generated_material(artifact=artifact)
        return version


def read_registered_recipe_primary_dependency(*, stage, unit_key):
    """One in-run primary authority: a generated choice OR an explicit reuse.

    A library association alone never supplies a Run dependency. Reused media
    keeps its original source and needs no synthetic artifact or registration.
    """
    from studio.services.production_recipe_material_fulfillment import read_recipe_material_fulfillment
    from studio.services.production_recipe_material_selection import recipe_primary_dependency_target
    primary_unit, expected_setting = recipe_primary_dependency_target(stage=stage, unit_key=unit_key)
    fulfillment = read_recipe_material_fulfillment(stage=stage, unit_key=primary_unit["unit_key"])
    selection = read_recipe_primary_dependency(stage=stage, unit_key=unit_key)
    if fulfillment is not None:
        if selection is not None or fulfillment["target"]["setting"] != expected_setting:
            raise ValidationError("主图来源重复或不匹配变体固定的设定依赖，不能自动择一")
        return fulfillment
    if selection is None:
        return None
    version = read_generated_material(artifact=selection["artifact"])
    return None if version is None else {**selection, "version": version}


def schedule_recipe_material_registrations(*, stage_run_id, selections):
    """Register after the selection transaction commits, without a new queue.

    The stage stays running until original Asset/version facts are ready. The
    existing running-recipe sweep recovers a lost callback after a restart.
    """
    selected = tuple((str(key), str(decision_id)) for key, decision_id in selections)
    transaction.on_commit(lambda: _register_committed_selections(stage_run_id, selected))


def _register_committed_selections(stage_run_id, selections):
    from studio.services.production_runs import _StoredAssetFileGuard
    from studio.services.registry_reference_lock import registry_reference_write_guard
    from studio.services.production_run_auto_advance import schedule_production_run_advance

    observed = None
    try:
        # This callback runs outside the selecting transaction. The file guard
        # surrounds the registry transaction COMMIT, not just an inner savepoint.
        with _StoredAssetFileGuard() as guard, registry_reference_write_guard():
            with locked_preparation_owner_rows([("stage_run_id", stage_run_id)]) as owners:
                stage = owners[("stage_run_id", str(stage_run_id))]
                if stage.status != "running" or stage.production_run.status != "running":
                    return  # A later stop/review decision wins; do not resume it.
                observed = stage.updated_at
                for unit_key, decision_id in selections:
                    current = read_recipe_material_selection(stage=stage, unit_key=unit_key)
                    if current is not None and str(current["decision"].pk) != decision_id:
                        # An explicit later selection can win before this old
                        # callback acquires the Run lock. Only a real earlier
                        # decision of this same group is superseded, not a
                        # missing/corrupt receipt. Let the original sweep consume
                        # the current choice; don't register or block on the old one.
                        if ArtifactSelectionDecision.objects.filter(pk=decision_id, stage_run=stage,
                                variant_group=current["decision"].variant_group,
                                revision__lt=current["decision"].revision).exists():
                            continue
                    register_recipe_material_selection(stage_run_id=stage.pk, unit_key=unit_key,
                        decision_id=decision_id, actor=stage.production_run.owner, asset_guard=guard)
                run_id = stage.production_run_id
        # Asset/version commit has succeeded before the normal executor resumes.
        schedule_production_run_advance(run_id)
    except Exception as exc:
        logger.exception("Recipe material registration failed", extra={"stage_run_id": str(stage_run_id)})
        if observed is None:
            return
        with locked_preparation_owner_rows([("stage_run_id", stage_run_id)]) as owners:
            stage = owners[("stage_run_id", str(stage_run_id))]
            run = stage.production_run
            if stage.status != "running" or run.status != "running" or stage.updated_at != observed:
                return
            reason = (str(exc)[:2000] if isinstance(exc, (ValidationError, APIValidationError))
                else "素材登记失败；原候选已保留，请查看运行详情及服务日志后明确重试。")
            stage.status, stage.reason_code, stage.reason = "blocked", "material_registration_failed", reason
            run.status, run.blocked_code, run.blocked_reason = "blocked", stage.reason_code, reason
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
