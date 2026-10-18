"""Read-only destinations for the exact rows blocking cleanup, never latest-task guesses."""
from collections import defaultdict
from urllib.parse import urlencode

from django.apps import apps
from django.db.models import OuterRef
from studio import models as m
from studio.services import registry_relations


def _task(kind, identifier, **query):
    return "/workflows?" + urlencode(dict(kind=kind, id=str(identifier), **query))


def describe_blockers(blockers):
    grouped = defaultdict(list)
    for blocker in blockers:
        grouped[blocker["model"]].append(blocker)
    configuration = {model._meta.label: kind for kind, model in registry_relations.MODELS.items()}
    for model_label, items in grouped.items():
        model = apps.get_model(model_label)
        ids = {item["id"] for item in items}
        details = {}
        progress = {}
        if model_label in configuration:
            kind = configuration[model_label]
            fields = registry_relations.fields(kind) + ["relation_" + group.key for group in registry_relations.groups(kind, OuterRef)]
            for row in registry_relations.summary_queryset(kind).filter(pk__in=ids).values(*fields):
                # Reuse the canonical configuration destination and label.
                summary = registry_relations.row_summary(kind, row)
                details[str(row["id"])] = (summary["label"], summary["config_url"], "查看配置")
        else:
            rows = model._base_manager.filter(pk__in=ids)
            if model is m.ExecutionPreparation:
                rows = rows.select_related("asset_job", "execution_instance_version__instance")
            elif model is m.RuntimeHostProbeAttempt:
                rows = rows.select_related("host")
            elif model is m.GenerationAttempt:
                rows = rows.select_related("execution_instance_version__instance")
            prep_field = {m.AssetGenerationJob: "asset_job_id", m.WorkflowSmokeAttempt: "workflow_smoke_id",
                          m.ModelDeploymentSmokeAttempt: "deployment_smoke_id"}.get(model)
            preparations = defaultdict(list)
            if prep_field:
                for prep_id, source_id in m.ExecutionPreparation.objects.filter(**{prep_field + "__in": ids}).values_list("pk", prep_field):
                    preparations[str(source_id)].append(prep_id)
            for row in rows:
                progress[str(row.pk)] = {key: getattr(row, key, None) for key in
                    ("status", "started_at", "heartbeat_at", "progress_marker")}
                label, url, action = str(model._meta.verbose_name), None, "查看记录"
                if model is m.ExecutionPreparation:
                    label = "执行准备 · " + (row.asset_job.name if row.asset_job_id else
                        row.execution_instance_version.instance.name if row.execution_instance_version_id else row.unit_key)
                    url, action = _task("preparation", row.pk), "查看任务"
                elif model is m.RuntimeHostProbeAttempt:
                    label, action = "主机探测 · " + row.host.name, "查看探测"
                    url = "/settings?" + urlencode(dict(section="runtime", probe_attempt=str(row.pk)))
                elif model is m.GenerationAttempt:
                    label, action = "生产调用 · " + row.execution_instance_version.instance.name, "查看调用"
                    url = _task("preparation", row.preparation_id, attempt_id=str(row.pk))
                elif model in {m.GenerationTask, m.AssemblyJob}:
                    label, action = ("生成任务" if model is m.GenerationTask else "装配任务"), "查看任务"
                    url = _task("generation_task" if model is m.GenerationTask else "assembly", row.pk)
                elif prep_field:
                    label = "素材任务 · " + row.name if model is m.AssetGenerationJob else "Smoke 验证"
                    action = "查看任务"
                    exact = preparations[str(row.pk)]
                    url = _task("preparation", exact[0]) if len(exact) == 1 else "/workflows?" + urlencode(dict(source_id=str(row.pk)))
                details[str(row.pk)] = (label, url, action)
        for item in items:
            label, url, action = details.get(item["id"], ("记录已变化，请重新预览", None, "查看记录"))
            item.update(label=label, detail_url=url, link_label=action)
            item.update(progress.get(item["id"], dict.fromkeys(("status", "started_at", "heartbeat_at", "progress_marker"))))
    return blockers
