"""Read-only target choices inside the original Run's frozen scope."""
from django.core.exceptions import ValidationError
from studio.models import Episode, SeriesProject, Shot
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_execution_units import ENTITY_SCOPE_MODELS, production_entity_queryset


def production_target_queryset(run, kind):
    if run.project.owner_id != run.owner_id:
        raise ValidationError("需要归属一致的制作运行")
    plan = get_run_execution_plan(run)
    if kind == "project":
        rows = SeriesProject.objects.filter(pk=run.project_id).only("id", "title").order_by("id")
    elif kind == "episode":
        rows = Episode.objects.filter(project_id=run.project_id)
        if plan.value["scope"]["kind"] == "episodes":
            rows = rows.filter(pk__in=plan.value["scope"]["episode_ids"])
        rows = rows.only("id", "number", "title").order_by("number", "id")
    elif kind == "shot":
        rows = Shot.objects.filter(episode__project_id=run.project_id)
        if plan.value["scope"]["kind"] == "episodes":
            rows = rows.filter(episode_id__in=plan.value["scope"]["episode_ids"])
        rows = rows.select_related("episode").only("id", "number", "title", "episode__id", "episode__number").order_by("episode__number", "number", "id")
    elif kind in ENTITY_SCOPE_MODELS:
        rows = production_entity_queryset(run, plan, kind).only("id", "name").order_by("name", "id")
    else:
        raise ValidationError("请选择项目、剧集、镜头、角色、场景或道具类型")
    return plan, rows


def production_target_row(row, kind):
    label = (row.name if kind in ENTITY_SCOPE_MODELS else row.title if kind == "project" else
        f"第{row.number}集 · {row.title}" if kind == "episode" else
        f"第{row.episode.number}集 / 镜头{row.number} · {row.title}")
    return {"scope": {"kind": kind, "id": str(row.pk)}, "unit_key": f"{kind}:{row.pk}", "label": label}
