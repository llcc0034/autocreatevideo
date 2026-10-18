"""Explicit production target scope shared by preparation and its consumers.

unit_key is opaque. It cannot supply a missing project, episode or shot, and
source media scopes are not the target scope of the call.
"""
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.models import CharacterBible, Episode, PropBible, SceneBible, Shot
from studio.services.production_run_plans import get_run_execution_plan


ENTITY_SCOPE_MODELS = {"character": CharacterBible, "scene": SceneBible, "prop": PropBible}
PRODUCTION_SCOPE_KINDS = ("project", "episode", "shot", *ENTITY_SCOPE_MODELS)


def production_entity_queryset(run, plan, kind):
    """A selected-episode run cannot silently produce unrelated project entities."""
    model = ENTITY_SCOPE_MODELS.get(kind)
    if model is None:
        raise ValidationError("请选择已登记的角色、场景或道具对象")
    rows = model.objects.filter(project_id=run.project_id)
    if plan.value["scope"]["kind"] == "episodes":
        relation = "shot_relations__shot" if kind == "character" else "shots"
        rows = rows.filter(**{f"{relation}__episode_id__in": plan.value["scope"]["episode_ids"]}).distinct()
    return rows


def resolve_production_unit(run, scope):
    if (not isinstance(scope, dict) or set(scope) != {"kind", "id"}
            or scope["kind"] not in PRODUCTION_SCOPE_KINDS
            or not isinstance(scope["id"], str)):
        raise ValidationError("生产单元必须明确选择真实制作对象，不能从单元名称推断")
    try:
        identifier = str(UUID(scope["id"]))
    except (ValueError, AttributeError) as exc:
        raise ValidationError("生产单元必须引用正式对象UUID") from exc
    if identifier != scope["id"]:
        raise ValidationError("生产单元必须使用规范UUID")
    if run.project.owner_id != run.owner_id:
        raise ValidationError("运行和项目所有者不一致，不能转移生成单元")
    plan = get_run_execution_plan(run)
    episode_id = None
    if scope["kind"] == "project":
        if identifier != str(run.project_id):
            raise ValidationError("生产单元不属于本轮项目")
    elif scope["kind"] in ENTITY_SCOPE_MODELS:
        if not production_entity_queryset(run, plan, scope["kind"]).filter(pk=identifier).exists():
            raise ValidationError("角色、场景或道具不存在、不属于本轮项目，或未关联所选剧集的镜头")
    else:
        if scope["kind"] == "episode":
            episode_id = Episode.objects.filter(pk=identifier, project_id=run.project_id).values_list("pk", flat=True).first()
        else:
            episode_id = Shot.objects.filter(pk=identifier, episode__project_id=run.project_id).values_list("episode_id", flat=True).first()
        if episode_id is None:
            raise ValidationError("生产单元不存在或不属于本轮项目")
        episode_id = str(episode_id)
        if plan.value["scope"]["kind"] == "episodes" and episode_id not in plan.value["scope"]["episode_ids"]:
            raise ValidationError("生产单元不在本轮冻结剧集范围内")
    return {"schema_version": 1, "run_plan_sha256": plan.sha256,
        "scope": dict(scope), "project_id": str(run.project_id), "episode_id": episode_id}


def verify_production_unit(run, value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "run_plan_sha256", "scope", "project_id", "episode_id"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValidationError("准备缺少完整的生产单元合同，不能读取旧图或任务快照补齐")
    current = resolve_production_unit(run, value["scope"])
    if current != value:
        raise ValidationError("生产单元的计划、项目或剧集归属已变化，不能静默改归属")
    return current


def production_unit_scope_members(unit, unit_key):
    """Map known ancestor scopes to this call; never include sibling shots."""
    result = {("project", unit["project_id"]): [unit_key]}
    if unit["episode_id"] is not None:
        result[("episode", unit["episode_id"])] = [unit_key]
    if unit["scope"]["kind"] == "shot":
        result[("shot", unit["scope"]["id"])] = [unit_key]
    # Entity identity is the call's explicit target, not a new semantic input
    # scope or permission to reinterpret a subject as a shot. Project/request
    # sources and declared subject facts continue through the existing mapper.
    return result
