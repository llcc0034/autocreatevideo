"""Reviewed one-off resource-policy cleanup, not a runtime compatibility path.

Database-only; never removes media, contacts providers, or reuses Smoke proof.
The regular restart entry stops writers before execution. Preview is read-only.
"""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from django.core.management.base import CommandError
from django.db import connection
from django.db.models import Max

from studio import models as m
from studio.management.commands.purge_incompatible_workflows import (
    _build_nullifications, _hard_delete_plan, _incoming_fields, _lock_plan_rows,
    _reverse_relation_querysets, _semantic_reference_hits, _selected_uuid_strings,
    _validate_backup_manifest,
)
from studio.management.execution_cutover_inventory import _digest, _row_facts
from studio.services.registry_reference_lock import registry_reference_write_guard


CONFIRMATION = "REPLACE_REVIEWED_RESOURCE_CONFIG_PRESERVE_CONTENT"
POLICIES = ("series_concept", "script_structure", "visual_rules", "prompt_normalization")
PRESERVED = (m.SeriesProject, m.Episode, m.Shot, m.CharacterBible, m.SceneBible, m.PropBible,
    m.Asset, m.ScriptSource, m.ScriptRevision, m.Style)


def incompatible(specification):
    return any(set(specification.get(key, {})) & {"max_output_tokens", "output_token_warning_threshold"}
        for key in POLICIES) or any("max_tokens" in row.get("parameters", {})
        for row in specification.get("text_execution", {}).get("operations", {}).values())


def clean_specification(specification):
    result = deepcopy(specification)
    for key in POLICIES:
        if key in result:
            result[key].pop("max_output_tokens", None)
            result[key].pop("output_token_warning_threshold", None)
    for row in result.get("text_execution", {}).get("operations", {}).values():
        row["parameters"].pop("max_tokens", None)
    return result


def _add(selected, model, ids):
    before = len(selected[model])
    selected[model].update(ids)
    return len(selected[model]) != before


def build_plan():
    if connection.vendor != "sqlite":
        raise CommandError("资源配置切换仅适用于已核对的本地 SQLite 环境")
    selected = defaultdict(set)
    _add(selected, m.ModelProfileVersion, [row.pk for row in m.ModelProfileVersion.objects.all()
        if incompatible(row.specification)])
    if not selected[m.ModelProfileVersion]:
        raise CommandError("没有不兼容模型，不重复执行清理")
    changed = True
    while changed:
        changed = False
        for model, ids in list(selected.items()):
            if not ids:
                continue
            for child, rows in _reverse_relation_querysets(model, ids):
                if child == m.ScriptRevision and model == m.GenerationAttempt:
                    continue  # Explicitly detach provenance below; preserve original text.
                if child in PRESERVED and rows.exists():
                    raise CommandError(f"清理将触及必须保留的 {child.__name__}，拒绝扩大范围")
                changed |= _add(selected, child, rows.values_list("pk", flat=True))
        for hit in _semantic_reference_hits(_selected_uuid_strings(selected), all_fields=True):
            model, pk = hit["model"], hit["id"]
            if pk in selected[model]:
                continue
            parent = {
                m.ExecutionVersionContract: (m.ModelExecutionInstanceVersion, "version_id"),
                m.ExecutionPreparationContract: (m.ExecutionPreparation, "preparation_id"),
                m.ProductionRunPlan: (m.ProductionRun, "production_run_id"),
            }.get(model)
            if parent:
                target, field = parent
                changed |= _add(selected, target, model.objects.filter(pk=pk).values_list(field, flat=True))
            elif model in (m.ProductionRecipeVersion, m.ProductionRun, m.GenerationTask, m.AssemblyJob):
                changed |= _add(selected, model, [pk])
        # A selected auxiliary binding invalidates its parent's frozen contract.
        changed |= _add(selected, m.ModelExecutionInstanceVersion,
            m.ExecutionAuxiliaryBinding.objects.filter(pk__in=selected[m.ExecutionAuxiliaryBinding]).values_list("version_id", flat=True))
        # Preparation owners use exact Run FKs, not a second cleanup ledger.
        changed |= _add(selected, m.ProductionRun,
            m.ExecutionPreparation.objects.filter(pk__in=selected[m.ExecutionPreparation], stage_run__isnull=False)
                .values_list("stage_run__production_run_id", flat=True))
    nullifications = _build_nullifications(selected)
    revisions = set(m.ScriptRevision.objects.filter(origin_generation_attempt_id__in=selected[m.GenerationAttempt]).values_list("pk", flat=True))
    if revisions:
        nullifications[(m.ScriptRevision, "origin_generation_attempt")] = revisions
    # Never delete an active system configuration through a dependent cycle.
    if m.ConfigRevision.objects.filter(pk__in=selected[m.ConfigRevision], scope_type="system").exists():
        raise CommandError("清理范围意外包含系统配置，拒绝执行")
    active = []
    uncertain = []
    for model in (m.GenerationAttempt, m.GenerationTask, m.AssetGenerationJob, m.ModelDeploymentSmokeAttempt,
            m.WorkflowSmokeAttempt, m.AssemblyJob):
        rows = model.objects.filter(pk__in=selected[model])
        active.extend({"model": model.__name__, "id": str(row["id"]), "status": row["status"]} for row in rows.filter(
            status__in=("queued", "running", "submitting", "submitted", "cancel_requested")).values("id", "status"))
        uncertain.extend({"model": model.__name__, "id": str(row["id"]), "status": row["status"]} for row in rows.filter(status="uncertain").values("id", "status"))
    report = {"mode": "resource_cutover_preview", "confirmation": CONFIRMATION,
        "rows": {model._meta.label: _row_facts(model, ids) for model, ids in selected.items() if ids},
        "nullifications": [{"model": model._meta.label, "field": field, "ids": sorted(map(str, ids))}
            for (model, field), ids in sorted(nullifications.items(), key=lambda item: (item[0][0]._meta.label, item[0][1]))],
        "active_calls": active, "uncertain_records": uncertain,
        "preserved": {model._meta.label: _row_facts(model) for model in PRESERVED}}
    report["counts"] = {key: len(rows) for key, rows in report["rows"].items()}
    report["plan_sha256"] = _digest(report)
    return SimpleNamespace(selected=selected, nullifications=nullifications, report=report)


def _capture_replacements(plan):
    from studio.services.execution_instances import execution_version_configuration
    versions = list(m.ModelExecutionInstanceVersion.objects.filter(pk__in=plan.selected[m.ModelExecutionInstanceVersion])
        .select_related("instance", "deployment__profile_version", "prompt_profile_version", "workflow_version").order_by("instance_id", "version"))
    deployments = {row.deployment_id: row.deployment for row in versions if row.deployment_id in plan.selected[m.ModelDeployment]}
    profiles = {row.profile_version_id: row.profile_version for row in deployments.values()}
    profiles = [(row, clean_specification(row.specification),
        m.ModelProfileVersion.objects.filter(profile_key=row.profile_key).aggregate(n=Max("version"))["n"])
        for row in profiles.values()]
    configurations = [(row, execution_version_configuration(row)) for row in versions]
    recipes = list(m.ProductionRecipeVersion.objects.filter(pk__in=plan.selected[m.ProductionRecipeVersion]))
    return profiles, deployments, configurations, recipes


def _publish_model(profile, actor):
    # Use the same publication action and all its validators, without an HTTP
    # request to a running server or any provider call.
    from rest_framework.test import APIRequestFactory, force_authenticate
    from studio.platform_views import ModelProfileVersionViewSet
    request = APIRequestFactory().post("/model-profiles/publish/", {}, format="json")
    force_authenticate(request, user=actor)
    response = ModelProfileVersionViewSet.as_view({"post": "publish"})(request, pk=str(profile.pk))
    if response.status_code != 200:
        raise CommandError(f"新模型发布校验失败，事务回滚：{response.data}")


def _replace(captured, actor):
    from studio.platform_serializers import ModelProfileVersionSerializer, ModelDeploymentSerializer, ProductionRecipeVersionSerializer
    from studio.services.execution_instances import create_execution_draft, publish_execution_version
    from studio.services.execution_definitions import build_execution_definition
    profiles, deployments, configurations, recipes = captured
    profile_map, deployment_map, version_map, digest_map, next_numbers = {}, {}, {}, {}, {}
    for old, specification, maximum in sorted(profiles, key=lambda item: (item[0].profile_key, item[0].version)):
        number = next_numbers.get(old.profile_key, maximum) + 1
        next_numbers[old.profile_key] = number
        serializer = ModelProfileVersionSerializer(data={"profile_key": old.profile_key, "provider": str(old.provider_id),
            "model_identifier": old.model_identifier, "modality": old.modality, "specification": specification})
        serializer.is_valid(raise_exception=True)
        new = serializer.save(version=number)
        _publish_model(new, actor)
        profile_map[old.pk] = new
    for pk, old in deployments.items():
        serializer = ModelDeploymentSerializer(data={"key": old.key, "name": old.name,
            "profile_version": str(profile_map[old.profile_version_id].pk), "runtime_host": old.runtime_host_id,
            "credential_id": old.credential_id, "runtime_config": old.runtime_config,
            "priority": old.priority, "is_default": old.is_default, "status": old.status},
            context={"request": SimpleNamespace(user=actor)})
        serializer.is_valid(raise_exception=True)
        deployment_map[pk] = serializer.save()
    pending = list(configurations)
    while pending:
        progressed = False
        for old, config in list(pending):
            if any(binding["target_version_id"] not in version_map for binding in config["auxiliaries"]
                    if binding["target_version_id"] in {str(row.pk) for row, _ in configurations}):
                continue
            instance = m.ModelExecutionInstance.objects.get(pk=old.instance_id)
            configuration = {key: deepcopy(config[key]) for key in ("inputs", "parameters", "auxiliaries")}
            if old.operation in {"script.generate", "script.structure", "visual_rules.generate", "prompt.normalize",
                    "prompt.draft", "prompt.fields.generate", "prompt.fields.translate"}:
                configuration["parameters"] = [row for row in configuration["parameters"] if row["key"] != "max_tokens"]
            for binding in configuration["auxiliaries"]:
                binding["target_version_id"] = version_map.get(binding["target_version_id"], binding["target_version_id"])
            deployment = deployment_map.get(old.deployment_id, old.deployment)
            new = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=actor,
                selection={"deployment": deployment, "operation": old.operation, "mode": old.mode,
                    "prompt_mode": old.prompt_mode, "prompt_profile_version": old.prompt_profile_version,
                    "workflow_version": old.workflow_version}, configuration=configuration)
            definition = build_execution_definition(new.pk)
            new = publish_execution_version(version_id=new.pk, expected_revision=new.revision,
                expected_definition_sha256=definition.sha256, actor=actor)
            version_map[str(old.pk)] = str(new.pk)
            digest_map[old._cutover_definition_sha256] = definition.sha256
            pending.remove((old, config)); progressed = True
        if not progressed:
            raise CommandError("辅助实例依赖无法重建；不会保留旧目标兜底")
    def replace_refs(value):
        if isinstance(value, str):
            return version_map.get(value, digest_map.get(value, value))
        if isinstance(value, list):
            return [replace_refs(row) for row in value]
        if isinstance(value, dict):
            return {key: replace_refs(row) for key, row in value.items()}
        return value
    recipe_ids = []
    for old in recipes:
        definition = replace_refs(old.definition)
        # Remove only the obsolete output policy from native text routes.
        def remove_output_policies(value):
            if isinstance(value, dict):
                if value.get("execution_instance_version_id") in set(version_map.values()):
                    value["parameter_policies"] = [row for row in value.get("parameter_policies", []) if row.get("key") != "max_tokens"]
                for child in value.values():
                    remove_output_policies(child)
            elif isinstance(value, list):
                for child in value:
                    remove_output_policies(child)
        remove_output_policies(definition)
        serializer = ProductionRecipeVersionSerializer(data={"recipe_key": old.recipe_key, "name": old.name, "definition": definition})
        serializer.is_valid(raise_exception=True)
        recipe_ids.append(str(serializer.save(version=old.version + 1).pk))
    return {"models": [str(row.pk) for row in profile_map.values()],
        "deployments": [str(row.pk) for row in deployment_map.values()], "instances": list(version_map.values()),
        "recipe_drafts": recipe_ids, "smoke_reused": False, "provider_calls": 0}


def execute(*, expected_sha256, confirmation, backup_manifest, operator):
    from studio.management.execution_cutover import cutover_operator
    from studio.services.platform_lifecycle_audit import record_platform_lifecycle_event
    if confirmation != CONFIRMATION or not backup_manifest:
        raise CommandError("需要明确清理确认及完整数据库备份")
    from django.db.migrations.recorder import MigrationRecorder
    if not MigrationRecorder.Migration.objects.filter(app="studio",
            name="0181_project_creation_without_call_allowance").exists():
        raise CommandError("须由正式重启入口先完成资源策略 Schema 迁移")
    backup = _validate_backup_manifest(Path(backup_manifest))
    actor = cutover_operator(operator)
    with registry_reference_write_guard():
        plan = build_plan()
        _lock_plan_rows(plan)
        if plan.report["plan_sha256"] != expected_sha256:
            raise CommandError("数据已变化，必须重新预览精确范围")
        if plan.report["active_calls"]:
            raise CommandError("仍有进行中的调用，不能清理")
        captured = _capture_replacements(plan)
        # Retain hashes before deletion; old IDs are audit facts, never runtime aliases.
        old_digests = {str(row.pk): row.contract.sha256 for row, _ in captured[2]}
        for row, _ in captured[2]:
            row._cutover_definition_sha256 = old_digests[str(row.pk)]
        preserved_expected = {}
        for model in PRESERVED:
            rows = list(model._base_manager.order_by("pk").values())
            for row in rows:
                for (target, field), ids in plan.nullifications.items():
                    if target == model and row["id"] in ids:
                        row[model._meta.get_field(field).attname] = None
            preserved_expected[model] = [{"id": str(row["id"]), "row_sha256": _digest(row)} for row in rows]
        deleted = _hard_delete_plan(plan)
        replacement = _replace(captured, actor)
        for model in PRESERVED:
            prior = plan.report["preserved"][model._meta.label]
            after = _row_facts(model)
            if {row["id"] for row in after} != {row["id"] for row in prior}:
                raise CommandError(f"保留内容数量发生变化：{model.__name__}")
            if preserved_expected[model] != after:
                raise CommandError(f"保留内容意外改变：{model.__name__}")
        report = {"mode": "resource_cutover_completed", "plan_sha256": expected_sha256, "counts": deleted,
            "backup": backup, "nullifications": plan.report["nullifications"],
            "uncertain_records": plan.report["uncertain_records"], "replacement": replacement,
            "preserved_counts": {key: len(rows) for key, rows in plan.report["preserved"].items()}}
        record_platform_lifecycle_event(m.ConfigDefinition.objects.get(key="execution.resources.review_policy"), actor=actor, action="resource_policy_cutover",
            reason_code="reviewed.resource_policy_cutover", before_summary={"sha256": expected_sha256},
            after_summary={"sha256": _digest(report), "status": "completed"})
        return report
