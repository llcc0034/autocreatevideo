"""Explicit whole-installation history cleanup, retaining configuration and media.

This is an administrative write path, not a fallback for broken historical data.
No file is removed here; asset deletion owns managed-file deletion separately.
"""
import hashlib
import json
from copy import deepcopy

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection, models
from django.utils import timezone
from rest_framework.exceptions import APIException

from studio import models as m
from studio.services.registry_reference_lock import registry_reference_write_guard
from studio.services.history_cleanup_pause import require_background_pause, resume_background_checks
from studio.services.history_cleanup_navigation import describe_blockers


CONFIRMATION = "CLEAR_ALL_HISTORY_KEEP_ASSETS"
HISTORY_MODELS = frozenset("""
AccessControlAuditEvent SecretAuditEvent PlatformLifecycleAuditEvent SeriesProject
CharacterBible SceneBible PropBible BibleEntityIdentity ScriptBibleEntityBinding
ScriptBibleAppearanceBinding BibleVariant BibleVariantRevision BibleVariantReview
ConsistencyLock Episode Shot ShotScriptSource ShotFieldRevision ShotCharacterRelation
ShotReferenceUse PromptFieldTranslation ProjectTranslationRequest ProjectTranslationSource
GenerationTask GenerationTaskObservation AssemblyJob ReleaseBundle AssetGenerationJob
AssetGenerationJobResolution RuntimeHostProbeAttempt ExecutionPreparation
ExecutionResourceConfirmation ExecutionCapacityObservation ExecutionPreparationContract
ExecutionFieldValue ExecutionFieldSelection WorkflowSmokeAttempt WorkflowSmokeAttemptResolution
WorkflowSmokeAssetBinding HostCapabilitySnapshot ModelDeploymentCapabilitySnapshot
ModelDeploymentSmokeAttempt ModelDeploymentSmokeAttemptResolution RuntimeModelAvailability
RecommendationSnapshot RecommendationCandidate RecommendationDecision ScriptSource ScriptRevision
ScriptRevisionStructure ProductionRun ProductionRunPlan ProductionStageInputAmendment
ProductionManualCommand ProductionMaterialFulfillment StageRun ApprovalDecision GenerationAttempt
ExecutionTextResponseReview GenerationAttemptResolution ArtifactSelectionDecision StageRouteEvent
ProductionAssemblyLink GeneratedArtifact AssemblySourceItem
""".split())
RETAINED_MODELS = frozenset("""
UserSecurityProfile SecretBinding SecretVersion StyleCategory Style Asset AssetReferenceSeries
AssetReferenceVersion WorkflowTemplate WorkerLease ProviderProfile ModelProfileVersion
FieldDefinition FieldDefinitionVersion DataStructureVersion DataStructureNode PromptProfileVersion
BusinessOutputVersion ContentSourceVersion PromptProfileFieldRule RuntimeHost ModelDeployment
ModelArtifact WorkflowTemplateVersion ModelExecutionInstance ModelExecutionInstanceVersion
ExecutionInputSpec ExecutionInputTarget ExecutionParameterSpec ExecutionAuxiliaryBinding
ExecutionVersionContract WorkflowProjector WorkflowProjectorVersion ComfyWorkflowSource
ComfyWorkflowContractDraft ComfyWorkflowPreviewReceipt ConfigDefinition
ProductionRecipeVersion WorkflowModelRequirement WorkflowSemanticBinding
""".split())
DETACHED_FIELDS = {
    ("AssetReferenceSeries", "bible_variant"), ("AssetReferenceVersion", "setting_revision"),
    ("AssetReferenceVersion", "generated_artifact"), ("AssetReferenceVersion", "generation_job"),
    ("ComfyWorkflowSource", "generation_task"), ("ModelArtifact", "verified_snapshot"),
}
ORIGIN_KEYS = frozenset(("project_id", "production_run_id", "stage_run_id", "task_id",
    "generation_task_id", "generation_attempt_id", "generated_artifact_id", "artifact_id",
    "generation_job_id", "preparation_id", "generation_purpose", "request_sha256",
    "source_manifest_sha256"))
CALL_MODELS = frozenset(("GenerationTask", "GenerationAttempt", "AssetGenerationJob",
    "AssemblyJob", "WorkflowSmokeAttempt", "ModelDeploymentSmokeAttempt", "RuntimeHostProbeAttempt"))
TERMINAL = frozenset(("succeeded", "failed", "cancelled"))
MAINTENANCE_MODELS = frozenset(("RuntimeHostProbeAttempt", "HostCapabilitySnapshot", "RuntimeModelAvailability",
    "RecommendationSnapshot", "RecommendationCandidate", "RecommendationDecision"))


class HistoryCleanupConflict(APIException):
    status_code = 409
    default_code = "history_cleanup_conflict"


def _rows(model, include_maintenance=True):
    rows = model._base_manager.all()
    if not include_maintenance:
        if model.__name__ in {"RuntimeHostProbeAttempt", "HostCapabilitySnapshot", "RuntimeModelAvailability"}:
            return rows.none()
        # Project recommendations belong to the deleted business source chain;
        # global recommendations remain maintenance facts, including their decisions.
        related = (models.Q(project__isnull=False) | models.Q(config_revision__scope_type__in=("project", "run")))
        if model is m.RecommendationSnapshot:
            return rows.filter(related)
        snapshots = m.RecommendationSnapshot.objects.filter(related).values("pk")
        if model is m.RecommendationCandidate:
            return rows.filter(recommendation_snapshot__in=snapshots)
        if model is m.RecommendationDecision:
            return rows.filter(related | models.Q(recommendation_snapshot__in=snapshots))
    if model.__name__ == "ConfigRevision":
        return rows.exclude(scope_type="system")
    if model.__name__ == "ConfigValue":
        return rows.exclude(revision__scope_type="system")
    return rows


def _digest_row(digest, model, row, include_maintenance=True):
    # A blocked preparation is periodically claimed, checked and rescheduled
    # without changing any reviewed fact. Its due time/temporary lease are
    # liveness, not scope. Active claims are still rejected by _active_reason
    # on the current rows under the cleanup write lock, even for a valid hash.
    ignored = {"updated_at", "heartbeat_at"}
    if model is m.ExecutionPreparation:
        ignored.update(("next_check_at", "claim_token", "claim_until"))
    if not include_maintenance and model is m.RuntimeHost:
        ignored.update(("name", "fingerprint_sha256", "status", "last_seen_at"))
    facts = {key: value for key, value in row.items() if key not in ignored}
    digest.update(json.dumps([model._meta.label, facts], sort_keys=True, default=str).encode())


def _catalog():
    catalog = list(apps.get_app_config("studio").get_models())
    unknown = {model.__name__ for model in catalog} - HISTORY_MODELS - RETAINED_MODELS - {"ConfigRevision", "ConfigValue"}
    if unknown:
        raise HistoryCleanupConflict("尚未定义新表的历史清理职责：" + ", ".join(sorted(unknown)))
    deleted = [model for model in catalog if model.__name__ in HISTORY_MODELS | {"ConfigRevision", "ConfigValue"}]
    for model in apps.get_app_config("studio").get_models(include_auto_created=True):
        if model._meta.auto_created in deleted:
            deleted.append(model)
    return catalog, deleted


def _contains_target(value, targets):
    if isinstance(value, dict):
        return any(_contains_target(child, targets) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_target(child, targets) for child in value)
    return isinstance(value, str) and value.replace("-", "").lower() in targets


def _configuration_input(model_name, field_name, value):
    # Runtime proofs are cleared below, not executable configuration inputs.
    if model_name == "WorkflowTemplateVersion" and field_name in {"validation_evidence", "smoke_test_evidence"}:
        return None
    if (model_name, field_name) in {
        ("ComfyWorkflowContractDraft", "projection"),
        ("ComfyWorkflowPreviewReceipt", "response"),
    } and isinstance(value, dict):
        # This immutable import projection retains what was observed when it
        # was authored. Promotion uses model_requirements and current artifact
        # validation, not these historical inventory-match annotations.
        value = deepcopy(value)
        for dependency in value.get("model_dependencies", []):
            if isinstance(dependency, dict):
                for match in dependency.get("artifact_matches", []):
                    if isinstance(match, dict):
                        match.pop("verified_snapshot", None)
    return value


def _active_reason(row, name):
    status = row.get("status")
    if name == "ExecutionPreparation":
        if row.get("claim_token") or status in ("preparing", "ready"):
            return "执行准备仍可领取或持有领取权"
        return ""
    if name not in CALL_MODELS:
        return ""
    if status not in TERMINAL | {"preparing"}:
        return "任务未结束或远端结果未知"
    # Assembly deliberately retains the last attempt token after finishing;
    # its writer only owns work while status=running. Do not confuse that
    # immutable identity with an active lease.
    completed_assembly = name == "AssemblyJob" and status in TERMINAL and row.get("finished_at")
    if (row.get("claim_token") and not completed_assembly) or row.get("distribution_claim") or row.get("distribution_state") in ("queued", "running"):
        return "执行或文件分发尚未释放领取权"
    if row.get("error_code") == "uncertain_remote_response":
        return "远端结果未解决"
    if name == "GenerationTask" and row.get("submission_token"):
        if row.get("remote_state") not in TERMINAL and row.get("submission_state") not in {"terminal_" + s for s in TERMINAL}:
            return "提交标识尚无可信远端终态"
    if status == "preparing" and any(row.get(key) for key in ("submitted_at", "submission_started_at", "provider_request_id", "started_at")):
        return "准备任务包含真实提交或启动事实"
    return ""


def build_history_cleanup_plan(*, include_maintenance=False):
    catalog, deleted = _catalog()
    selected, counts, blockers = {}, {}, []
    digest = hashlib.sha256()
    digest.update(json.dumps({"include_maintenance": include_maintenance}).encode())
    for model in deleted:
        rows = _rows(model, include_maintenance).order_by("pk")
        selected[model] = set()
        for row in rows.values().iterator(chunk_size=200):
            pk = row[model._meta.pk.attname]
            selected[model].add(pk)
            _digest_row(digest, model, row, include_maintenance)
            reason = _active_reason(row, model.__name__)
            if reason:
                blockers.append({"model": model._meta.label, "id": str(pk), "reason": reason})
        counts[model._meta.label] = len(selected[model])
    targets = {str(pk).replace("-", "").lower() for ids in selected.values() for pk in ids if len(str(pk)) == 36}
    preserved = {}
    for model in catalog:
        if model.__name__ == "WorkerLease":
            continue  # Leases are live service state, never deleted or hashed.
        if model.__name__ in HISTORY_MODELS and (include_maintenance or model.__name__ not in MAINTENANCE_MODELS):
            preserved[model._meta.label] = 0
            continue  # All rows were selected; avoid an unbounded NOT IN list.
        rows = model._base_manager.exclude(pk__in=selected.get(model, ())).order_by("pk")
        preserved[model._meta.label] = rows.count()
        for row in rows.values().iterator(chunk_size=200):
            # Unselected maintenance progress is not business cleanup scope.
            if include_maintenance or model.__name__ not in MAINTENANCE_MODELS | {"ModelArtifact"}:
                _digest_row(digest, model, row, include_maintenance)
            for field in model._meta.fields:
                target = field.remote_field.model if field.is_relation else None
                if target in selected and row[field.attname] in selected[target] and (model.__name__, field.name) not in DETACHED_FIELDS:
                    blockers.append({"model": model._meta.label, "id": str(row[model._meta.pk.attname]),
                        "reason": f"保留配置仍引用待清理的 {field.name}；请先修正该引用"})
                # Configuration JSON may be a live input, not mere provenance.
                if isinstance(field, models.JSONField) and model.__name__ not in {"Asset", "AssetReferenceVersion", "ComfyWorkflowSource"}:
                    value = _configuration_input(model.__name__, field.name, row[field.attname])
                    if _contains_target(value, targets):
                        blockers.append({"model": model._meta.label, "id": str(row[model._meta.pk.attname]),
                            "reason": f"配置 {field.name} 直接引用历史对象；请先删除或重新配置该试用配置"})
    blockers.sort(key=lambda item: (item["model"], item["id"], item["reason"]))
    report = {"plan_sha256": digest.hexdigest(), "delete_counts": counts, "preserve_counts": preserved,
        "blockers": describe_blockers(blockers), "can_execute": not blockers, "confirmation": CONFIRMATION,
        "files_deleted": False, "include_maintenance": include_maintenance}
    return report, selected


def _detach_preserved_history(*, include_maintenance):
    m.AssetReferenceSeries.objects.exclude(bible_variant=None).update(bible_variant=None)
    m.AssetReferenceVersion.objects.filter(source="generated").update(
        origin_history_cleared=True, generated_artifact=None, generation_job=None, setting_revision=None)
    m.AssetReferenceVersion.objects.exclude(setting_revision=None).update(setting_revision=None)
    m.ComfyWorkflowSource._base_manager.exclude(generation_task=None).update(generation_task=None)
    if include_maintenance:
        m.ModelArtifact.objects.exclude(verified_snapshot=None).update(verified_snapshot=None,
            verified_at=None, verified_by=None, status=m.ModelArtifact.Status.DECLARED)
    m.WorkflowTemplateVersion._base_manager.all().update(
        validation_state="not_run", validation_message="历史已清理，请重新校验。",
        validated_deployment=None, validation_evidence={}, validation_evidence_sha256="",
        smoke_test_state="not_run", smoke_test_message="历史已清理，未保留旧 Smoke 通过证明。",
        smoke_test_evidence={}, smoke_test_evidence_sha256="")
    for asset in m.Asset.objects.all().iterator(chunk_size=200):
        metadata = dict(asset.metadata) if isinstance(asset.metadata, dict) else {}
        origin = {key: metadata.pop(key) for key in ORIGIN_KEYS if key in metadata}
        if metadata.get("usage") == "project_cover":
            origin["usage"] = metadata.pop("usage")
        if origin:
            metadata["cleared_history_origin"] = {"cleared_at": timezone.now().isoformat(), "original": origin}
            m.Asset.objects.filter(pk=asset.pk).update(metadata=metadata, updated_at=timezone.now())


def clear_history(*, actor, expected_sha256, pause_token, include_maintenance=False):
    with registry_reference_write_guard():
        # Cross-process exclusion is essential: SQLite's local RLock alone is
        # insufficient. Take a database write lock before reading the plan.
        if connection.vendor == "sqlite":
            get_user_model().objects.filter(pk=actor.pk).update(last_login=models.F("last_login"))
        elif connection.vendor == "postgresql":
            tables = [model._meta.db_table for model in apps.get_app_config("studio").get_models(include_auto_created=True)]
            with connection.cursor() as cursor:
                cursor.execute("LOCK TABLE " + ", ".join(connection.ops.quote_name(t) for t in sorted(tables)) + " IN EXCLUSIVE MODE")
        else:
            raise HistoryCleanupConflict("当前数据库没有已实现的清理并发保护")
        require_background_pause(actor=actor, token=pause_token, include_maintenance=include_maintenance)
        report, selected = build_history_cleanup_plan(include_maintenance=include_maintenance)
        if report["plan_sha256"] != expected_sha256:
            raise HistoryCleanupConflict("清理范围已变化，请重新预览；未删除任何记录。")
        if report["blockers"]:
            raise HistoryCleanupConflict({"detail": "仍有执行中任务或保留配置引用；未删除任何记录。", "blockers": report["blockers"]})
        _detach_preserved_history(include_maintenance=include_maintenance)
        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                cursor.execute("PRAGMA defer_foreign_keys = ON")
            else:
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        for model, ids in selected.items():
            ordered = sorted(ids, key=str)
            count = sum(model._base_manager.filter(pk__in=ordered[index:index + 200])._raw_delete(connection.alias)
                for index in range(0, len(ordered), 200))
            if count != len(ids):
                raise HistoryCleanupConflict("清理数量漂移，已回滚。")
        connection.check_constraints()
        resume_background_checks(actor=actor, token=pause_token)
        return {"deleted_counts": report["delete_counts"], "retained_assets": m.Asset.objects.count(),
            "files_deleted": False}
