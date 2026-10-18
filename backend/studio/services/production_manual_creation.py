"""Manual-source initialization behind the sole create_production_run entry.

No separate endpoint, Recipe, queue, AI invocation or automatic approval. All
rows are committed together; subsequent actions refer to this original Run.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import ProductionRun, ScriptRevision, SeriesProject, Shot, StageRun
from studio.services.configuration import resolve_configuration
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract, json_contract, verify_json_contract
from studio.services.production_run_configuration import prepare_run_configuration, freeze_run_configuration
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.production_stage_catalog import production_stage_catalog, PRODUCTION_DELIVERY_KINDS


def manual_production_options(project):
    """The view authorizes the project first; facts aren't readiness evidence."""
    catalog = production_stage_catalog()
    return {"project_id": str(project.pk), "stage_catalog": {"value": catalog.value, "schema_version": catalog.schema_version,
        "size_bytes": catalog.size_bytes, "sha256": catalog.sha256},
        "scope_kinds": ["project", "episodes"], "delivery_kinds": list(PRODUCTION_DELIVERY_KINDS),
        "initial_content_kinds": ["script_revision"],
        "facts": {"confirmed_script_revisions": ScriptRevision.objects.filter(source__project=project, status="confirmed",
            confirmed_at__isnull=False, confirmed_by__isnull=False).count(), "episodes": project.episodes.count(),
            "shots": Shot.objects.filter(episode__project=project).count()}}


@registry_reference_writer
@transaction.atomic
def create_manual_run_root(*, user, project, manual_plan, idempotency_key, allow_external_calls,
        config_overrides):
    if not user or not user.is_authenticated or not user.is_active:
        raise PermissionDenied("需要有效登录身份")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 120:
        raise ValidationError({"idempotency_key": "长度必须为 1 到 120"})
    if not isinstance(manual_plan, dict) or set(manual_plan) != {
            "stage_catalog_key", "expected_stage_catalog_sha256", "scope", "delivery", "initial_content"}:
        raise ValidationError({"manual_plan": "必须明确提供阶段目录、范围、初始内容和交付合同；不接受任意 DAG"})
    if not isinstance(config_overrides, dict):
        raise ValidationError({"config_overrides": "必须提供明确的配置对象"})
    # Match original root creation's owner serialization before idempotency.
    type(user).objects.select_for_update().only("id").get(pk=user.pk)
    project = SeriesProject.objects.select_for_update().get(pk=project.pk)
    if project.owner_id != user.pk and not user.is_staff:
        raise PermissionDenied("无权为此项目创建制作运行")
    from studio.services.production_runs import _snapshot_config_overrides, ProductionRunConflict
    intent = json_contract({"schema_version": 4, "plan_source": "manual", "project_id": str(project.pk),
        "manual_plan": manual_plan, "allow_external_calls": allow_external_calls,
        "config_overrides": _snapshot_config_overrides(config_overrides)},
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    key = idempotency_key.strip()
    previous = ProductionRun.objects.select_for_update().filter(owner=user, idempotency_key=key).first()
    if previous is not None:
        if previous.request_sha256 != intent.sha256 or previous.plan_source != "manual":
            raise ProductionRunConflict({"idempotency_key": "该键已用于不同制作运行请求"})
        verified = verify_json_contract(previous.input_snapshot, schema_version=previous.input_snapshot_schema_version,
            size_bytes=previous.input_snapshot_size_bytes, sha256=previous.input_snapshot_sha256, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        plan = get_run_execution_plan(previous)
        if verified.value != {"schema_version": 4, "request_intent": intent.value, "run_plan_sha256": plan.sha256}:
            raise ProductionRunConflict({"run_plan": "制作运行的冻结来源与原始请求不一致"})
        expected_stages = [(row["ordinal"], row["key"]) for row in plan.value["stage_catalog"]["value"]["stages"] if row["manual_actions"]]
        actual_stages = list(previous.stages.order_by("ordinal").values_list("ordinal", "stage_key"))
        if actual_stages != expected_stages:
            raise ProductionRunConflict({"run_plan": "制作运行的正式阶段不完整，不能通过重放补造"})
        return previous, False
    base = resolve_configuration(project=project)
    # An empty manual run is not an external-call authorization.
    if allow_external_calls is None:
        allow_external_calls = False
    merged, _, _, external = prepare_run_configuration(base_config=base, config_overrides=config_overrides,
        allow_external_calls=allow_external_calls)
    run = ProductionRun(owner=user, project=project, plan_source="manual", recipe_version=None,
        idempotency_key=key, request_sha256=intent.sha256, status="awaiting_input",
        allow_external_calls=external)
    # Temporary empty fields are visible only inside this creation transaction.
    for field in ("config_snapshot", "input_snapshot"):
        assign_json_contract(run, field, {"schema_version": 1}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    run.save()
    plan = freeze_run_execution_plan(run=run, actor=user, **manual_plan)
    assign_json_contract(run, "input_snapshot", {"schema_version": 4, "request_intent": intent.value,
        "run_plan_sha256": plan.definition_sha256}, schema_version=4, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    run.full_clean()
    run.save(update_fields=("input_snapshot", "input_snapshot_schema_version", "input_snapshot_size_bytes", "input_snapshot_sha256", "updated_at"))
    freeze_run_configuration(run, overrides=merged, actor=user)
    stages = []
    for row in plan.definition["stage_catalog"]["value"]["stages"]:
        if not row["manual_actions"]:
            continue
        # Manual is the orchestration mode for every manual stage. Do not
        # choose the first recipe mode (or an AI resource) from an option list.
        stage = StageRun(production_run=run, stage_key=row["key"], ordinal=row["ordinal"], mode="manual",
            approval_required=True, status="pending", idempotency_key=f"{run.pk}:{row['key']}")
        for field in ("routing", "routing_decision", "config_snapshot", "input_snapshot", "output_snapshot"):
            assign_json_contract(stage, field, {"schema_version": 2, "run_plan_sha256": plan.definition_sha256},
                schema_version=2, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        stages.append(stage)
    StageRun.objects.bulk_create(stages)
    return run, True
