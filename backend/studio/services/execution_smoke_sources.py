"""Read-only production context for an independent, explicitly authorized Smoke.

This grants only the reviewed-visual reader access, never production execution,
parameter policies, selected media, stage advancement or approval authority.
"""
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F, OuterRef, Subquery

from studio.models import ProductionRun, StageRun
from studio.services.execution_source_readers import VISUAL_RULE_SOURCE_FIELDS


@dataclass(frozen=True)
class ReviewedSmokeContext:
    run_id: str
    project_id: str
    stage_key: str
    run_plan_sha256: str
    eligible_stage_ids: tuple


def context_request(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"run_id", "stage_key", "run_plan_sha256"}:
        raise ValidationError("审核来源上下文必须明确生产轮次、参考步骤及计划摘要")
    try:
        run_id = str(UUID(str(value["run_id"])))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("审核来源轮次需要正式UUID") from exc
    if (not isinstance(value["stage_key"], str) or not 1 <= len(value["stage_key"]) <= 100
            or not isinstance(value["run_plan_sha256"], str) or len(value["run_plan_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["run_plan_sha256"])):
        raise ValidationError("审核来源上下文的步骤或计划摘要无效")
    return {**value, "run_id": run_id}


def reviewed_sources_requested(inputs):
    from studio.services.execution_structure_mapping import validate_structure, structure_leaves
    if not isinstance(inputs, dict):
        raise ValidationError("烟测输入必须是明确对象")
    found = False
    for key, source in inputs.items():
        if not isinstance(source, dict):
            continue  # Original input validator reports the malformed source.
        leaves = [source]
        if source.get("kind") == "structured":
            template = source.get("template")
            validate_structure(template, leaf_validator=lambda *_: None, path=key)
            leaves = structure_leaves(template)
        found = found or any(row.get("kind") == "context_field" and isinstance(row.get("selector_key"), str)
            and row["selector_key"] in VISUAL_RULE_SOURCE_FIELDS for row in leaves)
    return found


def smoke_source_context_rows(actor):
    if not actor or not actor.is_active or not actor.is_staff:
        raise PermissionDenied("查询烟测审核来源需要有效管理员身份")
    reviewed = StageRun.objects.filter(production_run_id=OuterRef("production_run_id"),
        stage_key="visual_rules_review", status="succeeded").order_by().values("ordinal")[:1]
    # Candidate identities only; previews/creation validate frozen plan and
    # actual approval. No JSON payloads, artifact reads or per-row lookups here.
    return StageRun.objects.filter(production_run__owner=actor, production_run__project__owner=actor,
        ordinal__gt=Subquery(reviewed), production_run__execution_plan__isnull=False).order_by(
            "-production_run__created_at", "production_run_id", "ordinal", "id").values(
                "stage_key", run_id=F("production_run_id"), project_id=F("production_run__project_id"),
                project_title=F("production_run__project__title"), run_created_at=F("production_run__created_at"),
                run_plan_sha256=F("production_run__execution_plan__definition_sha256"))


def lock_reviewed_smoke_context(value, *, actor):
    """Called within the original Smoke creation transaction, before allocation."""
    from studio.services.production_execution_sources import execution_source_stage_ids
    from studio.services.production_run_plans import get_run_execution_plan
    value = context_request(value)
    run = ProductionRun.objects.select_for_update().filter(pk=value["run_id"], owner=actor, project__owner=actor).first()
    if run is None:
        raise ValidationError("来源生产轮次不存在或不属于当前用户")
    if get_run_execution_plan(run).sha256 != value["run_plan_sha256"]:
        raise ValidationError("来源运行计划已变化，请重新选择", code="execution_preparation_conflict")
    stage = run.stages.filter(stage_key=value["stage_key"]).first()
    if stage is None:
        raise ValidationError("来源运行没有所选参考步骤")
    return ReviewedSmokeContext(str(run.pk), str(run.project_id), stage.stage_key,
        value["run_plan_sha256"], execution_source_stage_ids(stage))
