"""Read original preparation/call facts without treating an open stage as a call."""
from collections import defaultdict

from django.db.models import Count

from studio.models import ExecutionPreparation, GenerationAttempt, StageRun
from studio.services.production_stage_catalog import MANUAL_STAGE_ACTIONS


def manual_execution_activity(run):
    keys = [key for key, actions in MANUAL_STAGE_ACTIONS.items() if "generate" in actions]
    preparations, calls = defaultdict(list), defaultdict(list)
    # Three bounded aggregate/index reads, independent of task count. No payloads.
    for row in ExecutionPreparation.objects.filter(stage_run__production_run_id=run.pk,
            stage_run__stage_key__in=keys).order_by().values("stage_run_id", "status").annotate(count=Count("pk")):
        preparations[row["stage_run_id"]].append({"status": row["status"], "count": row["count"],
            "label": dict(ExecutionPreparation.Status.choices).get(row["status"], "未知准备状态")})
    for row in GenerationAttempt.objects.filter(stage_run__production_run_id=run.pk,
            stage_run__stage_key__in=keys).order_by().values("stage_run_id", "role", "status").annotate(count=Count("pk")):
        calls[row["stage_run_id"]].append({"status": row["status"], "role": row["role"], "count": row["count"],
            "label": dict(GenerationAttempt.Status.choices).get(row["status"], "未知调用状态"),
            "role_label": dict(GenerationAttempt.Role.choices).get(row["role"], "未知调用类型")})
    return [{"stage_id": str(row["id"]), "stage_key": row["stage_key"], "stage_status": row["status"],
        "lifecycle_label": "步骤开放" if row["status"] == "running" else dict(StageRun.Status.choices).get(row["status"], "未知阶段状态"),
        "preparations": sorted(preparations[row["id"]], key=lambda item: item["status"]),
        "calls": sorted(calls[row["id"]], key=lambda item: (item["role"], item["status"]))}
        for row in StageRun.objects.filter(production_run_id=run.pk, stage_key__in=keys).order_by("ordinal", "id")
        .values("id", "stage_key", "status")]
