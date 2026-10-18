"""Reviewed, one-off 0144 cleanup; not a runtime reader or data converter.

Use the existing maintenance deletion and configuration authoring services.
The service manager stops platform writers before executing this operation.
"""
import hashlib
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from studio.management.commands.purge_incompatible_workflows import (
    _build_nullifications, _expand_reverse_dependencies, _hard_delete_plan,
    _incoming_fields, _semantic_reference_hits, _selected_uuid_strings,
)
from studio.management.execution_cutover_inventory import (
    OBSOLETE_CONFIGURATION_KEYS, SHARED_RESOURCES, _digest, _native_resource_checks,
    _row_facts, execution_cutover_inventory,
)


CONFIRMATION = "DELETE_REVIEWED_0144_EXECUTION_DATA_WITHOUT_BACKUP"


def _mentions_file(value, needles):
    if isinstance(value, dict):
        return any(_mentions_file(key, needles) or _mentions_file(item, needles) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_mentions_file(item, needles) for item in value)
    return isinstance(value, str) and any(needle in value.replace("\\", "/") for needle in needles)


def _registry():
    loader = MigrationExecutor(connection).loader
    return loader.project_state(list(loader.applied_migrations)).apps


def cutover_operator(username):
    actor = get_user_model().objects.filter(username=username, is_active=True, is_superuser=True).first()
    if actor is None:
        raise CommandError("操作人必须是存在且启用的平台超级管理员")
    return actor


def _owned_file(asset, selected, registry):
    if asset.local_path or asset.reference_url:
        raise CommandError("旧生成素材不是单一受管文件，需单独核对所有权")
    if not asset.file:
        raise CommandError("旧生成素材缺少受管文件，不能静默跳过")
    root = Path(settings.MEDIA_ROOT).resolve(strict=True)
    path = Path(asset.file.path).resolve(strict=True)
    if not path.is_relative_to(root) or path == root or not path.is_file():
        raise CommandError("旧生成素材文件不在已配置的媒体目录内")
    if Path(asset.file.path).is_symlink() or path.stat().st_nlink != 1:
        raise CommandError("旧生成素材文件存在链接共享，不能自动删除")
    needles = (path.as_posix(), str(asset.file.name).replace("\\", "/"))
    for model in registry.get_models():
        retained = model.objects.exclude(pk__in=selected.get(model, ()))
        json_fields = [field.name for field in model._meta.fields if field.get_internal_type() == "JSONField"]
        if json_fields:
            for payloads in retained.values_list(*json_fields).iterator(chunk_size=200):
                if any(_mentions_file(value, needles) for value in payloads):
                    raise CommandError(f"旧生成文件仍被保留对象的JSON引用：{model._meta.label}")
        for field in model._meta.fields:
            if field.get_internal_type() not in ("FileField", "ImageField"):
                continue
            for row in retained.exclude(
                    **{field.name: ""}).only("pk", field.name):
                value = getattr(row, field.name)
                if value and Path(value.path).resolve() == path:
                    raise CommandError("旧生成素材的文件仍被其他对象共享")
    Asset = registry.get_model("studio", "Asset")
    for value in Asset.objects.exclude(pk__in=selected.get(Asset, ())).exclude(local_path="").values_list("local_path", flat=True):
        if Path(value).resolve() == path:
            raise CommandError("旧生成素材的文件仍被其他素材引用")
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    if (sha != asset.checksum or sha != metadata.get("generated_sha256")
            or path.stat().st_size != metadata.get("size_bytes")):
        raise CommandError("旧生成素材文件的原始校验信息不一致")
    return {"asset_id": str(asset.pk), "path": path, "size_bytes": path.stat().st_size, "sha256": sha}


def build_execution_cutover_plan(prompt_ids=()):
    inventory = execution_cutover_inventory()
    if inventory["runtime_state"]["unsettled_rows"]:
        raise CommandError("仍有未结束的执行任务，不能清理或重放远端请求")
    registry = _registry()
    selected = defaultdict(set)
    for label, rows in inventory["rows"].items():
        model = registry.get_model(label)
        selected[model].update(model.objects.filter(pk__in=[row["id"] for row in rows]).values_list("pk", flat=True))
    Revision = registry.get_model("studio", "ConfigRevision")
    Definition = registry.get_model("studio", "ConfigDefinition")
    Prompt = registry.get_model("studio", "PromptProfileVersion")
    Asset = registry.get_model("studio", "Asset")
    baseline_rows = inventory["configuration"]["active_baselines"]
    if len(baseline_rows) != 1 or baseline_rows[0]["storage_integrity"] != "valid":
        raise CommandError("必须存在一个原始校验通过的系统配置基线")
    baseline = baseline_rows[0]
    if not baseline["in_retired_closure"]:
        raise CommandError("系统配置基线不属于本次已核对的旧配置范围")
    if Definition.objects.filter(restart_required=True, is_active=True).exists():
        raise CommandError("存在需重启激活的配置，本次无重启项基线方案不适用")
    selected[Revision].update(Revision.objects.filter(pk__in=[row["id"] for row in
        inventory["configuration"]["affected_revisions"]]).values_list("pk", flat=True))
    selected[Definition].update(Definition.objects.filter(key__in=OBSOLETE_CONFIGURATION_KEYS).values_list("pk", flat=True))
    requested = {str(pk) for pk in prompt_ids}
    prompts = set(Prompt.objects.filter(pk__in=requested).values_list("pk", flat=True))
    if set(map(str, prompts)) != requested:
        raise CommandError("指定的不兼容策略版本不存在")
    selected[Prompt].update(prompts)
    _expand_reverse_dependencies(selected)
    owned_assets = set()
    for asset in Asset.objects.filter(pk__in=inventory["owned_output_asset_ids_requiring_review"]):
        for child, field in _incoming_fields(Asset):
            if child.objects.filter(**{field.attname: asset.pk}).exclude(pk__in=selected.get(child, ())).exists():
                raise CommandError("旧任务生成的素材已被保留对象引用，不能自动扩大清理范围")
        jobs = registry.get_model("studio", "AssetGenerationJob").objects.filter(output_asset=asset)
        if (not isinstance(asset.metadata, dict) or
                set(map(str, jobs.values_list("pk", flat=True))) != {asset.metadata.get("generation_job_id")}):
            raise CommandError("旧生成素材的任务所有权不明确")
        selected[Asset].add(asset.pk)
        owned_assets.add(asset.pk)
    _expand_reverse_dependencies(selected)
    cache_rows = set()
    while True:
        added = False
        targets = _selected_uuid_strings(selected)
        for hit in _semantic_reference_hits(targets, model_registry=registry, all_fields=True):
            model, pk = hit["model"], hit["id"]
            if pk in selected.get(model, ()):
                continue
            if model._meta.label == "studio.PlatformLifecycleAuditEvent":
                selected[model].add(pk)
                added = True
            elif model._meta.label == "studio.WorkflowTemplateVersion" and hit["field"] == "smoke_test_evidence":
                cache_rows.add(pk)
            else:
                raise CommandError(f"保留对象含旧引用，需复核范围：{model._meta.label}.{hit['field']} ({pk})")
        Log = registry.get_model("admin", "LogEntry")
        new_logs = set(Log.objects.filter(object_id__in=targets).values_list("pk", flat=True)) - selected[Log]
        selected[Log].update(new_logs)
        if not added and not new_logs:
            break
        _expand_reverse_dependencies(selected)
    for name in SHARED_RESOURCES:
        model = registry.get_model("studio", name)
        permitted = prompts if model is Prompt else owned_assets if model is Asset else set()
        if selected.get(model, set()) - permitted:
            raise CommandError(f"清理依赖涉及未批准的共享资源：{name}")
    Workflow = registry.get_model("studio", "WorkflowTemplateVersion")
    files = [_owned_file(asset, selected, registry) for asset in Asset.objects.filter(pk__in=selected[Asset])]
    nullifications = _build_nullifications(selected)
    rows = {model._meta.label: _row_facts(model, ids) for model, ids in selected.items() if ids}
    report = {"mode": "reviewed_execution_cutover", "source_migration": inventory["source_migration"],
        "inventory_sha256": inventory["inventory_sha256"], "confirmation": CONFIRMATION,
        "obsolete_prompt_version_ids": sorted(requested), "counts": {label: len(items) for label, items in rows.items()},
        "rows": rows, "workflow_smoke_cache_resets": _row_facts(Workflow, cache_rows),
        "nullifications": [{"model": model._meta.label, "field": field, "rows": _row_facts(model, ids)}
            for (model, field), ids in sorted(nullifications.items(), key=lambda pair: (pair[0][0]._meta.label, pair[0][1]))],
        "owned_files": [{key: value for key, value in item.items() if key != "path"} |
            {"path_sha256": _digest(str(item["path"]))} for item in files],
        "baseline": baseline, "native_contracts_rechecked_inside_transaction": True}
    report["plan_sha256"] = _digest(report)
    return SimpleNamespace(registry=registry, selected=selected, nullifications=nullifications,
        cache_rows=cache_rows, files=files, report=report)


def execute_execution_cutover(*, prompt_ids, expected_sha256, confirmation, operator):
    if confirmation != CONFIRMATION or not expected_sha256:
        raise CommandError("必须提供本次精确预览哈希及不可恢复清理确认")
    actor = cutover_operator(operator)
    from studio.models import SecretVersion
    from studio.services.configuration import create_config_revision, publish_config_revision

    with transaction.atomic():
        # SQLite write reservation before the fresh plan; no read/write upgrade
        # race. The service manager separately stops all platform writers.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_configrevision SET revision = revision WHERE 0")
        plan = build_execution_cutover_plan(prompt_ids)
        if plan.report["plan_sha256"] != expected_sha256:
            raise CommandError("清理范围或内容已变化，必须重新预览")
        Revision = plan.registry.get_model("studio", "ConfigRevision")
        Definition = plan.registry.get_model("studio", "ConfigDefinition")
        source = Revision.objects.get(pk=plan.report["baseline"]["id"])
        values = {}
        for row in source.values.select_related("definition"):
            if row.definition.key in OBSOLETE_CONFIGURATION_KEYS:
                continue
            values[row.definition.key] = {"value": row.value, "credential_version":
                SecretVersion.objects.get(pk=row.credential_version_id) if row.credential_version_id else None}
        config_selected = defaultdict(set)
        for model in (Revision, Definition):
            config_selected[model].update(plan.selected[model])
        _expand_reverse_dependencies(config_selected)
        if any(ids - plan.selected.get(model, set()) for model, ids in config_selected.items()):
            raise CommandError("配置清理依赖超出已复核范围")
        deleted = _hard_delete_plan(SimpleNamespace(selected=config_selected, nullifications=_build_nullifications(config_selected)))
        baseline = create_config_revision(scope_type="system", values=values, created_by=actor,
            clone_published=False, change_summary="执行入口切换：保留已复核配置值，不推断执行目标")
        baseline = publish_config_revision(baseline, published_by=actor)
        if baseline.activation_state != "active" or baseline.values_sha256 != plan.report["baseline"]["retained_values_sha256"]:
            raise CommandError("新配置基线未精确保留已复核值或未生效，回滚清理")
        checks = _native_resource_checks(plan.registry)
        if checks["status"] != "checked":
            raise CommandError("新配置未解除资源合同核对阻断，回滚清理")
        failures = {name: {row["id"] for row in rows if row["contract_check"] != "passed"}
            for name, rows in checks["resources"].items()}
        if failures.get("PromptProfileVersion", set()) != set(plan.report["obsolete_prompt_version_ids"]) or any(
                ids for name, ids in failures.items() if name != "PromptProfileVersion"):
            detail = "; ".join(f"{name}: {','.join(sorted(ids))}" for name, ids in failures.items() if ids)
            raise CommandError("原生资源核对结果与批准删除的策略集合不一致，回滚清理：" + detail)
        remaining = defaultdict(set, {model: ids - config_selected.get(model, set()) for model, ids in plan.selected.items()})
        workflow = plan.registry.get_model("studio", "WorkflowTemplateVersion")
        changed = workflow.objects.filter(pk__in=plan.cache_rows).update(smoke_test_state="not_run",
            smoke_test_message="旧烟测已随执行入口切换清理，需要按当前执行实例重新验收",
            smoke_test_evidence={}, smoke_test_evidence_sha256="")
        if changed != len(plan.cache_rows):
            raise CommandError("工作流烟测缓存范围发生变化，回滚清理")
        for label, count in _hard_delete_plan(SimpleNamespace(selected=remaining,
                nullifications=_build_nullifications(remaining))).items():
            deleted[label] = deleted.get(label, 0) + count
    # Irreversible file removal only after a successful database transaction.
    # Errors propagate: service startup must not report the cutover complete.
    for item in plan.files:
        try:
            if item["path"].resolve(strict=True) != item["path"] or item["path"].stat().st_nlink != 1:
                raise ValueError("file ownership changed")
            with item["path"].open("rb") as stream:
                current_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if current_sha != item["sha256"] or item["path"].stat().st_size != item["size_bytes"]:
                raise ValueError("file changed")
            item["path"].unlink()
        except (OSError, ValueError) as exc:
            raise CommandError(f"数据库清理已提交，但素材 {item['asset_id']} 文件尚未清理；停止服务切换并复核") from exc
    return {"mode": "execution_cutover_deleted", "deleted": deleted,
        "baseline_id": str(baseline.pk), "baseline_sha256": baseline.values_sha256,
        "workflow_smoke_caches_reset": changed, "owned_files_deleted": len(plan.files),
        "backup_created": False, "migrations_applied": False, "model_calls": 0}
