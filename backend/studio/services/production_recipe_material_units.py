"""Project M3 requirements onto the original recipe stage's execution units.

These are references, not another demand store or queue. An appearance owns a
unit; its setting revision owns the requirement. Candidate and route numbers
remain the original preparation request keys.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError


def _identity(setting):
    if not isinstance(setting, dict):
        raise ValidationError("素材单元缺少确切外观与设定身份")
    for key in ("appearance_id", "setting_revision_id"):
        value = setting.get(key)
        try:
            valid = isinstance(value, str) and str(UUID(value)) == value
        except ValueError:
            valid = False
        if not valid:
            raise ValidationError("素材单元需要规范的外观与设定UUID，不从名称推断")
    return {"unit_key": f'appearance:{setting["appearance_id"]}',
        "requirement_key": f'setting:{setting["setting_revision_id"]}'}


def project_recipe_material_units(requirements):
    """Project already verified requirements, preserving exact dependencies.

    A historical primary dependency can differ from this run's primary target.
    Keep that exact requirement key; never relink it to today's primary or add
    an undeclared target. Later fulfillment must satisfy that exact revision.
    """
    if not isinstance(requirements, dict) or not isinstance(requirements.get("targets"), list):
        raise ValidationError("素材单元缺少原审核需求清单")
    result, seen = [], set()
    for target in requirements["targets"]:
        if not isinstance(target, dict):
            raise ValidationError("素材单元需求必须为确切对象")
        identity = _identity(target.get("setting"))
        role, scope = target.get("appearance_role"), target.get("scope")
        if (target.get("requirement_key") != identity["requirement_key"]
                or identity["unit_key"] in seen or not isinstance(role, str) or role not in {"primary", "variant"}
                or not isinstance(scope, dict) or set(scope) != {"kind", "id"}
                or not isinstance(scope["kind"], str) or scope["kind"] not in {"character", "scene", "prop"}
                or not isinstance(scope["id"], str)):
            raise ValidationError("素材单元的外观、实体或需求身份无效或重复")
        dependency = target.get("primary_dependency")
        if (role == "primary") != (dependency is None):
            raise ValidationError("素材单元的主图与变体依赖职责不一致")
        dependency = None if dependency is None else _identity(dependency)
        if dependency is not None and dependency["unit_key"] == identity["unit_key"]:
            raise ValidationError("变体素材单元不能依赖自身")
        result.append({**identity, "scope": deepcopy(scope), "appearance_role": role,
            "primary_dependency": dependency})
        seen.add(identity["unit_key"])
    return result


def frozen_recipe_material_units(stage, plan):
    """Read a frozen stage, including on replay; never read today's M3 state."""
    from studio.services.production_runs import _verify_stage_snapshot
    from studio.services.production_recipe_media_stages import recipe_media_stage_contract
    value = _verify_stage_snapshot(stage, "input_snapshot")
    base = recipe_media_stage_contract(stage).value
    requirements = value.get("material_requirements")
    if (stage.stage_key != "bible_image_generation"
            or any(value.get(key) != item for key, item in base.items())
            or not isinstance(requirements, dict) or requirements.get("run_plan_sha256") != plan.sha256
            or value.get("units") != project_recipe_material_units(requirements)):
        raise ValidationError("冻结素材单元与本轮计划或原需求不一致，不能回填实体单元")
    return deepcopy(value["units"]), requirements


def frozen_recipe_material_unit(stage, plan, scope, appearance_target=None):
    units, requirements = frozen_recipe_material_units(stage, plan)
    role = appearance_target["role"] if appearance_target is not None else "primary"
    setting_id = appearance_target["setting_revision_id"] if appearance_target is not None else None
    if role == "variant" and setting_id is None:
        raise ValidationError("配方变体必须引用本轮已审核的确切设定修订")
    matches = [unit for unit in units if unit["scope"] == scope and unit["appearance_role"] == role
        and (setting_id is None or unit["requirement_key"] == f"setting:{setting_id}")]
    if len(matches) != 1:
        raise ValidationError("本次已审核素材需求没有此单元的唯一主图目标或指定变体，不能按实体存在创建候选")
    return deepcopy(matches[0]), requirements
