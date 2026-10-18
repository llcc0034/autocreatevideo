"""Explicit, offline SQLite maintenance; never a business deletion endpoint.

Ownership selects rows. Reverse and JSON references can only block or be
preserved as provenance; they must never expand the operator's two targets.
"""
from collections import defaultdict
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import F, Q
from django.utils import timezone

from studio.management.commands.purge_incompatible_workflows import (
    _hard_delete_plan, _incoming_fields, _lock_plan_rows, _payload_references,
    _semantic_reference_hits, _selected_uuid_strings, _validate_backup_manifest,
)
from studio.management.execution_cutover import cutover_operator
from studio.management.execution_cutover_inventory import _digest
from studio.services.registry_reference_lock import registry_reference_write_guard


CONFIRMATION = "DELETE_ONE_REVIEWED_RUN_AND_RECIPE_PRESERVE_CONTENT"
TERMINAL = ("succeeded", "failed", "cancelled")
# Explicit ownership paths, not reverse-FK cascade. Unknown relations block.
OWNED = (
    ("StageRun", "production_run_id", "ProductionRun"),
    ("ProductionRunPlan", "run_id", "ProductionRun"),
    ("ProductionManualCommand", "run_id", "ProductionRun"),
    ("ConfigRevision", "run_id", "ProductionRun"),
    ("ConfigValue", "revision_id", "ConfigRevision"),
    ("ExecutionPreparation", "stage_run_id", "StageRun"),
    ("ExecutionPreparationContract", "preparation_id", "ExecutionPreparation"),
    ("ExecutionResourceConfirmation", "preparation_id", "ExecutionPreparation"),
    ("ExecutionCapacityObservation", "preparation_id", "ExecutionPreparation"),
    ("ExecutionFieldValue", "preparation_id", "ExecutionPreparation"),
    ("ExecutionFieldSelection", "preparation_id", "ExecutionPreparation"),
    ("ProductionStageInputAmendment", "stage_run_id", "StageRun"),
    ("GenerationAttempt", "stage_run_id", "StageRun"),
    ("ExecutionTextResponseReview", "attempt_id", "GenerationAttempt"),
    ("GenerationAttemptResolution", "attempt_id", "GenerationAttempt"),
    ("ApprovalDecision", "stage_run_id", "StageRun"),
    ("ArtifactSelectionDecision", "stage_run_id", "StageRun"),
    ("StageRouteEvent", "stage_run_id", "StageRun"),
    ("GeneratedArtifact", "production_run_id", "ProductionRun"),
)
ASSET_PROVENANCE_KEYS = frozenset((
    "task_id", "generation_task_id", "generation_attempt_id", "production_run_id",
    "stage_run_id", "generated_artifact_id", "artifact_id",
))


def _facts(model, ids=None, *, using="default"):
    rows = model._base_manager.using(using).order_by("pk")
    if ids is not None:
        rows = rows.filter(pk__in=ids)
    return [{"id": str(row[model._meta.pk.attname]), "row_sha256": _digest(row)}
        for row in rows.values().iterator(chunk_size=200)]


def _registry():
    loader = MigrationExecutor(connection).loader
    return loader.project_state(list(loader.applied_migrations)).apps


def build_plan(*, run_id, recipe_id, detach_script_provenance=False):
    if connection.vendor != "sqlite":
        raise CommandError("指定运行维护仅支持已核对的本地SQLite，不执行PostgreSQL迁移")
    try:
        run_id, recipe_id = UUID(str(run_id)), UUID(str(recipe_id))
    except (TypeError, ValueError) as exc:
        raise CommandError("必须明确一个运行UUID和一个配方版本UUID") from exc
    registry = _registry()
    models = {model.__name__: model for model in registry.get_models() if model._meta.app_label == "studio"}
    Run, Recipe = models["ProductionRun"], models["ProductionRecipeVersion"]
    run = Run.objects.filter(pk=run_id).first()
    recipe = Recipe.objects.filter(pk=recipe_id).first()
    if run is None or recipe is None:
        raise CommandError("目标运行或配方不存在；不扩展范围、不重复清理")
    if run.recipe_version_id != recipe_id or run.plan_source != "recipe":
        raise CommandError("配方不是指定运行的确切配方版本")
    selected = defaultdict(set, {Run: {run_id}, Recipe: {recipe_id}})
    for name, field, parent in OWNED:
        # Only inspect the applied schema. Optional later models are not a
        # reason to migrate the live database during maintenance.
        if name not in models:
            continue
        model = models[name]
        selected[model].update(model._base_manager.filter(
            **{f"{field}__in": selected[models[parent]]}).values_list("pk", flat=True))
    Attempt, Task = models["GenerationAttempt"], models["GenerationTask"]
    selected[Task].update(Attempt.objects.filter(pk__in=selected[Attempt], generation_task__isnull=False)
        .values_list("generation_task_id", flat=True))
    Observation = models["GenerationTaskObservation"]
    selected[Observation].update(Observation.objects.filter(task_id__in=selected[Task]).values_list("pk", flat=True))
    selected = {model: ids for model, ids in selected.items() if ids}
    blockers, historical, nullifications = [], [], {}

    def block(model, ids, reason, field=""):
        blockers.extend({"model": model._meta.label, "id": str(pk), "field": field, "reason": reason}
            for pk in sorted(ids, key=str))

    if run.status != "cancelled":
        block(Run, [run_id], "run_must_be_cancelled")
    if recipe.status != "retired":
        block(Recipe, [recipe_id], "recipe_must_be_retired")
    Revision = models["ScriptRevision"]
    for parent, ids in selected.items():
        for child, field in _incoming_fields(parent):
            outside = child._base_manager.filter(**{f"{field.attname}__in": ids}).exclude(
                pk__in=selected.get(child, ()))
            outside_ids = set(outside.values_list("pk", flat=True))
            if not outside_ids:
                continue
            if child == Revision and field.name == "origin_generation_attempt" and detach_script_provenance:
                if outside.exclude(source__project_id=run.project_id).exists():
                    block(child, outside_ids, "foreign_project_script_provenance", field.name)
                elif Run.objects.exclude(pk=run_id).filter(script_revision_id__in=outside_ids).exists():
                    block(child, outside_ids, "retained_run_uses_script_provenance", field.name)
                else:
                    nullifications[(child, field.name)] = outside_ids
            else:
                block(child, outside_ids, "outside_scope_reference", field.name)
    Config = models["ConfigRevision"]
    block(Config, Config.objects.filter(pk__in=selected.get(Config, ())).exclude(
        scope_type="run", run_id=run_id).values_list("pk", flat=True), "shared_configuration")
    for model in (Attempt, Task):
        rows = model.objects.filter(pk__in=selected.get(model, ()))
        block(model, rows.exclude(status__in=TERMINAL).values_list("pk", flat=True), "call_not_terminal")
    calls = Attempt.objects.filter(pk__in=selected.get(Attempt, ()))
    block(Attempt, calls.filter(error_code="uncertain_remote_response").values_list("pk", flat=True), "remote_response_uncertain")
    block(Task, Task.objects.filter(pk__in=selected.get(Task, ()), distribution_state__in=("queued", "running"))
        .values_list("pk", flat=True), "distribution_not_terminal")
    unresolved = Task.objects.filter(pk__in=selected.get(Task, ())).exclude(submission_token="").exclude(
        Q(submission_state__in=tuple(f"terminal_{state}" for state in TERMINAL)) | Q(remote_state__in=TERMINAL))
    block(Task, unresolved.values_list("pk", flat=True), "remote_submission_not_terminal")
    Prep = models["ExecutionPreparation"]
    block(Prep, Prep.objects.filter(pk__in=selected.get(Prep, ())).exclude(
        status__in=("cancelled", "dispatched")).values_list("pk", flat=True), "preparation_not_stopped")
    block(Prep, Prep.objects.filter(pk__in=selected.get(Prep, ()), claim_until__gt=timezone.now())
        .values_list("pk", flat=True), "preparation_claim_active")
    targets = _selected_uuid_strings(selected)
    for hit in _semantic_reference_hits(targets, model_registry=registry, all_fields=True):
        model, pk, field = hit["model"], hit["id"], hit["field"]
        if pk in selected.get(model, ()):
            continue
        is_history = model == models["PlatformLifecycleAuditEvent"]
        if model == models["Asset"] and field == "metadata":
            value = model.objects.get(pk=pk).metadata
            is_history = isinstance(value, dict) and not _payload_references(
                {key: val for key, val in value.items() if key not in ASSET_PROVENANCE_KEYS}, targets)
        if is_history:
            historical.append({"model": model._meta.label, "id": str(pk), "field": field})
        else:
            block(model, [pk], "outside_scope_json_reference", field)
    # Hash all retained studio data, including M2M rows, before/after. The
    # offline maintenance report is intentionally not a hot list endpoint.
    preserved = {}
    for model in registry.get_models(include_auto_created=True):
        if model._meta.app_label != "studio" or model == models["WorkerLease"]:
            continue
        ids = list(model._base_manager.exclude(pk__in=selected.get(model, ())).values_list("pk", flat=True))
        preserved[model] = set(ids)
    report = {"mode": "production_run_cleanup_preview", "confirmation": CONFIRMATION,
        "run_id": str(run_id), "recipe_id": str(recipe_id), "project_id": str(run.project_id),
        "detach_script_provenance": bool(detach_script_provenance),
        "rows": {model._meta.label: _facts(model, ids) for model, ids in selected.items()},
        "preserved": {model._meta.label: _facts(model, ids) for model, ids in preserved.items()},
        "nullifications": [{"model": model._meta.label, "field": field, "ids": sorted(map(str, ids))}
            for (model, field), ids in nullifications.items()],
        "preserved_provenance": sorted(historical, key=lambda row: (row["model"], row["id"], row["field"])),
        "blockers": sorted(blockers, key=lambda row: (row["model"], row["id"], row["field"], row["reason"])),
        "physical_files_deleted": False}
    report["counts"] = {key: len(rows) for key, rows in report["rows"].items()}
    report["plan_sha256"] = _digest(report)
    return SimpleNamespace(selected=selected, nullifications=nullifications, preserved=preserved,
        models=models, report=report)


def _backup_matches_plan(plan, manifest_path):
    evidence = _validate_backup_manifest(Path(manifest_path))
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    path = (Path(manifest_path).resolve().parent / manifest["database"]["backup_relative_path"]).resolve(strict=True)
    if path.stat().st_size != evidence["database_bytes"]:
        raise CommandError("备份数据库文件大小与manifest不符")
    backup_db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    try:
        for model in set(plan.selected) | set(plan.preserved):
            sql = f"SELECT * FROM {connection.ops.quote_name(model._meta.db_table)} ORDER BY {connection.ops.quote_name(model._meta.pk.column)}"
            with connection.cursor() as live:
                live.execute(sql)
                saved = backup_db.execute(sql)
                if tuple(col[0] for col in live.description) != tuple(col[0] for col in saved.description):
                    raise CommandError("备份Schema与当前精确清理范围不一致")
                while True:
                    live_rows, saved_rows = live.fetchmany(200), saved.fetchmany(200)
                    if live_rows != saved_rows:
                        raise CommandError("备份不包含本次待删除及保留对象的精确状态，须重新备份/预览")
                    if not live_rows:
                        break
    except sqlite3.DatabaseError as exc:
        raise CommandError("备份库无法验证，不能执行清理") from exc
    finally:
        backup_db.close()
    # Audit contains digests only, not sensitive filesystem paths.
    return {key: evidence[key] for key in ("manifest_sha256", "database_sha256", "database_bytes")}


def execute(*, run_id, recipe_id, detach_script_provenance=False, expected_sha256,
        confirmation, backup_manifest, operator, writers_stopped=False):
    if (confirmation != CONFIRMATION or not backup_manifest or not writers_stopped
            or len(expected_sha256) != 64):
        raise CommandError("须明确确认、完整备份、精确预览摘要，并通过正式服务入口停写")
    if connection.vendor != "sqlite":
        raise CommandError("指定运行维护只适用于SQLite")
    actor = cutover_operator(operator)
    with registry_reference_write_guard():
        # Acquire SQLite's cross-process writer lock before the inventory, not
        # merely the process-local registry lock. Failure rolls back everything.
        registry = _registry()
        Run = registry.get_model("studio", "ProductionRun")
        if Run.objects.filter(pk=run_id).update(status=F("status")) != 1:
            raise CommandError("目标运行不存在")
        Lease = registry.get_model("studio", "WorkerLease")
        if Lease.objects.filter(expires_at__gt=timezone.now()).exclude(holder_id="").exists():
            raise CommandError("仍有有效worker租约，拒绝清理")
        plan = build_plan(run_id=run_id, recipe_id=recipe_id, detach_script_provenance=detach_script_provenance)
        _lock_plan_rows(plan)
        if plan.report["plan_sha256"] != expected_sha256:
            raise CommandError("数据或引用已变化，必须重新预览")
        if plan.report["blockers"]:
            raise CommandError("精确范围存在阻断引用或未结束任务，不能执行")
        backup = _backup_matches_plan(plan, backup_manifest)
        preserved_expected = {}
        detachments = []
        for model, ids in plan.preserved.items():
            rows = list(model._base_manager.filter(pk__in=ids).order_by("pk").values())
            for row in rows:
                for (target, field), detached in plan.nullifications.items():
                    if target == model and row["id"] in detached:
                        detachments.append((model, row["id"], _digest(row), str(row[model._meta.get_field(field).attname])))
                        row[model._meta.get_field(field).attname] = None
            preserved_expected[model] = [{"id": str(row[model._meta.pk.attname]), "row_sha256": _digest(row)} for row in rows]
        deleted = _hard_delete_plan(plan)
        for model, expected in preserved_expected.items():
            if _facts(model) != expected:
                raise CommandError(f"保留内容发生未批准变化：{model._meta.label}，事务回滚")
        from studio.models import PlatformLifecycleAuditEvent
        def audit(object_id, object_type, before, after):
            return PlatformLifecycleAuditEvent.objects.create(actor=actor, action="production_run_cleanup",
                object_type=object_type, object_id=object_id, reason_code="reviewed.production_run_cleanup",
                before_summary=before, before_sha256=_digest(before), after_summary=after, after_sha256=_digest(after))
        for model, rows in plan.report["rows"].items():
            for row in rows:
                audit(row["id"], "maintenance_deleted_row",
                    {"key": model, "sha256": row["row_sha256"], "run_id": str(run_id)},
                    {"status": "deleted", "sha256": expected_sha256, "contract_sha256": backup["database_sha256"]})
        for model, pk, digest, attempt_id in detachments:
            audit(pk, "script_revision", {"sha256": digest, "key": attempt_id, "run_id": str(run_id)},
                {"status": "provenance_detached", "sha256": _facts(model, [pk])[0]["row_sha256"],
                    "contract_sha256": backup["database_sha256"]})
        return {"mode": "production_run_cleanup_completed", "run_id": str(run_id), "recipe_id": str(recipe_id),
            "plan_sha256": expected_sha256, "counts": deleted, "backup": backup,
            "nullifications": plan.report["nullifications"], "physical_files_deleted": False,
            "preserved_provenance": plan.report["preserved_provenance"]}
