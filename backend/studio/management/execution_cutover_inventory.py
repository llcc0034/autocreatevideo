"""One-off 0144 maintenance inventory, not a business compatibility reader.

The original purge command exposes this read-only step. No default instance,
rewritten evidence, route translation, deletion, migration or media call occurs.
Only the actually applied schema is queried; a changed schema requires review.
"""
import hashlib
import json
from collections import defaultdict

from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Count, Q

from studio.management.commands.purge_incompatible_workflows import (
    _build_nullifications, _expand_reverse_dependencies, _semantic_reference_hits,
)
from studio.services.configuration import verify_config_value_storage_contract
from studio.services.json_contracts import canonical_json_bytes, verify_json_contract


SOURCE_MIGRATION = "0144_execution_policy_configuration"
RETIRED_ROOTS = (
    "PromptProfileBinding", "PromptArtifact", "GeneratedWorkflow", "DeferredGenerationRequest",
    "AssetGenerationJob", "GenerationAttempt", "GenerationTask", "WorkflowSmokeAttempt",
    "ModelDeploymentSmokeAttempt", "ProductionRun",
)
OBSOLETE_CONFIGURATION_KEYS = (
    "production.prompt_normalization_deployment_id", "production.prompt_language_gate",
    "runtime.deferred_generation_retry_backoff_seconds",
)
SHARED_RESOURCES = (
    "ProviderProfile", "ModelProfileVersion", "ModelDeployment", "ModelArtifact",
    "PromptProfileVersion", "WorkflowTemplate", "WorkflowTemplateVersion",
    "WorkflowProjector", "WorkflowProjectorVersion", "SeriesProject", "Episode",
    "CharacterBible", "SceneBible", "PropBible", "Asset",
    "ModelExecutionInstance", "ModelExecutionInstanceVersion",
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _row_facts(model, ids=None):
    rows = model._base_manager.all()
    if ids is not None:
        rows = rows.filter(pk__in=ids)
    # Hash full rows internally, including payload changes; never print prompts,
    # credentials, user data, service addresses or storage paths in the report.
    return [{"id": str(row["id"]), "row_sha256": _digest(row)}
        for row in rows.order_by("pk").values().iterator(chunk_size=200)]


def _configuration_baseline(revision, affected):
    """Verify original stored bytes, not the current validity of retired keys.

    The 0144 credential digest uses the original immutable version identity.
    No secret is resolved/decrypted and no stale revision is silently repaired.
    """
    digest_values, retained, errors = {}, {}, []
    for row in revision.values.select_related("definition", "credential_version").order_by("definition__key"):
        key = row.definition.key
        if row.definition.is_secret:
            if (not row.credential_version_id or row.value is not None
                    or row.value_size_bytes != 0 or row.value_sha256 != ""):
                errors.append({"key": key, "code": "credential_storage_invalid"})
                continue
            identity = f"version:{row.credential_version_id}:{row.credential_version.version}"
            value = {"credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest()}
        else:
            try:
                verify_config_value_storage_contract(row)
            except ValidationError:
                errors.append({"key": key, "code": "configuration_value_integrity_invalid"})
            value = row.value
        digest_values[key] = value
        if key not in OBSOLETE_CONFIGURATION_KEYS:
            retained[key] = value
    digest = hashlib.sha256(canonical_json_bytes(digest_values)).hexdigest()
    if digest != revision.values_sha256:
        errors.append({"code": "configuration_revision_integrity_invalid"})
    return {"id": str(revision.pk), "revision": revision.revision, "values_sha256": revision.values_sha256,
        "in_retired_closure": revision.pk in affected, "storage_integrity": "invalid" if errors else "valid",
        "integrity_errors": errors, "retained_keys": sorted(retained),
        "retained_values_sha256": None if errors else hashlib.sha256(canonical_json_bytes(retained)).hexdigest(),
        "current_contract_review_required": True}


def _native_resource_checks(registry):
    """Use current validators without writing, publishing, or invoking models.

    A resource contract passing here does not imply that a particular
    composition is valid or has Smoke evidence. Failures only inform review.
    """
    from studio.services.model_profile_contracts import verified_model_profile_specification
    from studio.services.media_capabilities import validate_model_profile_media_capabilities
    from studio.services.prompt_profiles import verify_prompt_profile_contract, validate_prompt_profile_source_projection
    from studio.services.workflow_versions import verify_workflow_version_integrity
    from studio.services.workflow_projectors import _projector_max_bytes, validate_workflow_projector_definition
    from studio.services.security_boundaries import security_boundary_resolution_cache
    from studio.services.configuration import ConfigurationResolutionError, resolve_security_boundaries

    try:
        resolve_security_boundaries(required_keys=("security.snapshot.stage_max_bytes",))
    except (ValidationError, ConfigurationResolutionError) as exc:
        # Current validators depend on the full active configuration. An old
        # configuration failure is not evidence against every shared resource.
        return {"status": "blocked_by_configuration", "resources": {},
            "configuration_fields": sorted(getattr(exc, "error_dict", {})),
            "code": getattr(exc, "code", None) or "configuration_contract_invalid"}

    def model_contract(row):
        value = verified_model_profile_specification(row)
        validate_model_profile_media_capabilities(value, modality=row.modality)

    def prompt_contract(row):
        verify_prompt_profile_contract(row)
        validate_prompt_profile_source_projection(row)

    def projector_contract(row):
        verify_json_contract(row.definition, schema_version=row.definition_schema_version,
            size_bytes=row.definition_size_bytes, sha256=row.definition_sha256, max_bytes=_projector_max_bytes())
        validate_workflow_projector_definition(row.definition, engine_key=row.engine_key, engine_version=row.engine_version)

    result = {}
    with security_boundary_resolution_cache():
        for name, validator in (("ModelProfileVersion", model_contract), ("PromptProfileVersion", prompt_contract),
                ("WorkflowTemplateVersion", verify_workflow_version_integrity), ("WorkflowProjectorVersion", projector_contract)):
            rows = []
            for row in registry.get_model("studio", name).objects.order_by("pk"):
                entry = {"id": str(row.pk), "status": row.status, "contract_check": "passed"}
                try:
                    validator(row)
                except ValidationError as exc:
                    # Exception messages can contain values, paths or prompts.
                    # Return controlled check state and codes, never raw text.
                    errors = list(exc.error_dict.values()) if hasattr(exc, "error_dict") else [exc.error_list]
                    entry.update(contract_check="failed", codes=sorted({
                        item.code or "invalid_contract" for items in errors for item in items}))
                rows.append(entry)
            result[name] = rows
    return {"status": "checked", "resources": result}


def execution_cutover_inventory():
    if connection.vendor != "sqlite":
        raise CommandError("本次切换盘点只针对已核对的SQLite运行库，不扩展历史升级矩阵")
    loader = MigrationExecutor(connection).loader
    applied = sorted(name for app, name in loader.applied_migrations if app == "studio")
    if not applied or applied[-1] != SOURCE_MIGRATION:
        raise CommandError("实际Schema不是已核对的0144；请复核切换范围，不用当前模型猜读旧表")
    # Admin audit tables are in a separate migration branch. Use the complete
    # applied state, not only studio's dependency chain or the current apps.
    registry = loader.project_state(list(loader.applied_migrations)).apps
    selected = defaultdict(set)
    root_facts = []
    for name in RETIRED_ROOTS:
        model = registry.get_model("studio", name)
        ids = set(model.objects.values_list("pk", flat=True))
        selected[model].update(ids)
        root_facts.append({"model": model._meta.label, "reason": "retired_execution_storage_or_protocol",
            "ids": sorted(map(str, ids))})
    snapshot = registry.get_model("studio", "ModelDeploymentCapabilitySnapshot")
    ids = set(snapshot.objects.filter(probe_kind="smoke").values_list("pk", flat=True))
    selected[snapshot].update(ids)
    root_facts.append({"model": snapshot._meta.label, "reason": "pre_preparation_smoke_evidence", "ids": sorted(map(str, ids))})
    recipe = registry.get_model("studio", "ProductionRecipeVersion")
    ids = {pk for pk, value in recipe.objects.values_list("pk", "definition")
        if not isinstance(value, dict) or value.get("schema_version") != 3}
    selected[recipe].update(ids)
    root_facts.append({"model": recipe._meta.label, "reason": "retired_recipe_schema", "ids": sorted(map(str, ids))})
    # Match the actual upcoming migration guards. A shared assembly already
    # satisfying source contract 5 is not deleted merely because it is old.
    assembly = registry.get_model("studio", "AssemblyJob")
    sources = registry.get_model("studio", "AssemblySourceItem")
    source_job_ids = sources.objects.filter(Q(generation_task__isnull=False) | Q(generated_artifact__isnull=True))
    ids = set(assembly.objects.exclude(source_contract_version=5).values_list("pk", flat=True))
    ids.update(source_job_ids.values_list("assembly_job_id", flat=True))
    selected[assembly].update(ids)
    root_facts.append({"model": assembly._meta.label, "reason": "retired_assembly_source_contract", "ids": sorted(map(str, ids))})
    bundle = registry.get_model("studio", "ReleaseBundle")
    ids = {row.pk for row in bundle.objects.all() if row.production_run_id is None
        or (row.status != "draft" and (not isinstance(row.manifest, dict)
            or row.manifest.get("schema") != "autoprompt-production-release-bundle-v3"))}
    selected[bundle].update(ids)
    root_facts.append({"model": bundle._meta.label, "reason": "retired_release_source_contract", "ids": sorted(map(str, ids))})
    _expand_reverse_dependencies(selected)
    nullifications = _build_nullifications(selected)
    rows = {model._meta.label: _row_facts(model, ids) for model, ids in selected.items() if ids}
    shared = {name: _row_facts(registry.get_model("studio", name)) for name in SHARED_RESOURCES}
    shared_hits = {name: sorted(map(str, selected.get(registry.get_model("studio", name), ())))
        for name in SHARED_RESOURCES if selected.get(registry.get_model("studio", name))}
    revision = registry.get_model("studio", "ConfigRevision")
    value = registry.get_model("studio", "ConfigValue")
    affected = set(value.objects.filter(definition__key__in=OBSOLETE_CONFIGURATION_KEYS).values_list("revision_id", flat=True))
    while True:
        more = set(revision.objects.filter(based_on_revision_id__in=affected).values_list("pk", flat=True)) - affected
        if not more:
            break
        affected.update(more)
    configuration = {"obsolete_keys": list(OBSOLETE_CONFIGURATION_KEYS), "affected_revisions": _row_facts(revision, affected),
        "active_baselines": [_configuration_baseline(row, affected)
            for row in revision.objects.filter(scope_type="system", status="published", activation_state="active")],
        "value_rows_sha256": _digest(_row_facts(value, value.objects.filter(revision_id__in=affected).values_list("pk", flat=True)))}
    jobs = registry.get_model("studio", "AssetGenerationJob")
    output_assets = sorted(str(pk) for pk in jobs.objects.exclude(output_asset=None).values_list("output_asset_id", flat=True))
    targets = {str(pk) for ids in selected.values() for pk in ids} | set(map(str, affected)) | set(output_assets)
    # UUID mentions are evidence for review, not permission to delete the row.
    # In particular, shared assets/configuration may legitimately cite a source.
    semantic_hits = _semantic_reference_hits(targets, model_registry=registry, all_fields=True)
    semantic_rows = [{"model": hit["model"]._meta.label, "id": str(hit["id"]), "field": hit["field"],
        "already_in_fk_closure": hit["id"] in selected.get(hit["model"], ())} for hit in semantic_hits]
    admin_log = registry.get_model("admin", "LogEntry")
    admin_rows = list(admin_log.objects.filter(object_id__in=targets).order_by("pk").values(
        "id", "content_type__app_label", "content_type__model", "object_id"))
    status_counts, unsettled = {}, []
    for name in ("ProductionRun", "GenerationAttempt", "GenerationTask", "AssetGenerationJob",
            "WorkflowSmokeAttempt", "ModelDeploymentSmokeAttempt", "AssemblyJob"):
        model = registry.get_model("studio", name)
        status_counts[name] = dict(model.objects.values("status").annotate(count=Count("pk")).values_list("status", "count"))
        for row in model.objects.filter(status__in=("preparing", "queued", "running", "submitting", "submitted",
                "cancel_requested", "uncertain")).values("pk", "status"):
            unsettled.append({"model": model._meta.label, "id": str(row["pk"]), "status": row["status"]})
    report = {"mode": "read_only_execution_cutover_inventory", "source_migration": SOURCE_MIGRATION,
        "executable": False, "roots": root_facts, "rows": rows,
        "counts": {name: len(items) for name, items in rows.items()},
        "shared_resources": {name: {"count": len(items), "rows_sha256": _digest(items)} for name, items in shared.items()},
        "shared_dependency_conflicts": shared_hits, "configuration": configuration,
        "native_resource_contract_checks": _native_resource_checks(registry),
        "nullable_references_requiring_review": [{"model": model._meta.label, "field": field, "ids": sorted(map(str, ids))}
            for (model, field), ids in sorted(nullifications.items(), key=lambda item: (item[0][0]._meta.label, item[0][1]))],
        "owned_output_asset_ids_requiring_review": output_assets,
        "semantic_references_requiring_review": sorted(semantic_rows, key=lambda row: (row["model"], row["id"], row["field"])),
        "admin_audit_references_requiring_review": admin_rows,
        "runtime_state": {"status_counts": status_counts, "unsettled_rows": sorted(unsettled, key=lambda row: (row["model"], row["id"])),
            "remote_state_checked": False},
        "pending_checks": ["semantic_and_audit_references", "owned_and_shared_media_files", "active_and_remote_submission_state",
            "native_resource_contracts", "explicit_new_configuration_baseline", "locked_drift_and_deletion_order"]}
    report["inventory_sha256"] = _digest(report)
    return report
