from __future__ import annotations

import uuid
from typing import Any, Iterable

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import (
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    ProviderProfile,
    WorkflowSmokeAttempt,
    WorkflowTemplateVersion,
)
from studio.services.async_media_contract import (
    ASYNC_MEDIA_OPERATIONS,
    deployment_target_sha256,
)
from studio.services.deployment_smoke_contract import (
    DEPLOYMENT_SMOKE_OPERATIONS,
    TEXT_DEPLOYMENT_SMOKE_OPERATIONS,
)
from studio.services.json_contracts import verify_json_contract
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.model_profile_options import model_capability_values
from studio.services.provider_registry import validate_deployment_execution_contract
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache
from studio.services.workflow_versions import (
    inspect_workflow_acceptance,
    verify_workflow_version_integrity,
    workflow_execution_target_sha256,
)


def _snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _action(
    kind: str,
    *,
    url: str,
    ui_url: str,
    enabled: bool,
    reason_code: str,
    method: str | None = None,
    requires_fixture: bool = False,
    requires_confirmation: bool = False,
    may_call_external_service: bool = False,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "method": method or (
            "POST" if may_call_external_service or kind == "publish" else "GET"
        ),
        "url": url,
        "ui_url": ui_url,
        "enabled": enabled,
        "reason_code": reason_code,
        "requires_fixture": requires_fixture,
        "requires_confirmation": requires_confirmation,
        "may_call_external_service": may_call_external_service,
    }


def safe_workflow_attempt_summary(
    attempt: WorkflowSmokeAttempt | None,
) -> dict[str, Any] | None:
    if attempt is None:
        return None
    resolutions = getattr(attempt, "_prefetched_objects_cache", {}).get("resolutions")
    resolution_required = bool(
        attempt.status == WorkflowSmokeAttempt.Status.UNCERTAIN
        and not (resolutions if resolutions is not None else attempt.resolutions.exists())
    )
    return {
        "id": str(attempt.id),
        "deployment_id": str(attempt.deployment_id),
        "status": attempt.status,
        "target_sha256": attempt.target_sha256,
        "graph_sha256": attempt.graph_sha256,
        "error_code": attempt.error_code,
        "resolution_required": resolution_required,
        "created_at": attempt.created_at,
        "started_at": attempt.submission_started_at,
        "finished_at": attempt.finished_at,
    }


def safe_deployment_attempt_summary(
    attempt: ModelDeploymentSmokeAttempt | None,
) -> dict[str, Any] | None:
    if attempt is None:
        return None
    resolutions = getattr(attempt, "_prefetched_objects_cache", {}).get("resolutions")
    resolution_required = bool(
        attempt.status == ModelDeploymentSmokeAttempt.Status.UNCERTAIN
        and not (resolutions if resolutions is not None else attempt.resolutions.exists())
    )
    return {
        "id": str(attempt.id),
        "operation": attempt.operation,
        "prompt_profile": (attempt.request_snapshot or {}).get("prompt_profile"),
        "status": attempt.status,
        "request_sha256": attempt.request_sha256,
        "target_sha256": attempt.target_sha256,
        "error_code": attempt.error_code,
        "resolution_required": resolution_required,
        "created_at": attempt.created_at,
        "submission_started_at": attempt.submission_started_at,
        "finished_at": attempt.finished_at,
    }


def _snapshot_contract(
    snapshot: ModelDeploymentCapabilitySnapshot,
) -> tuple[list[str], dict[str, Any]] | None:
    try:
        operations = verify_json_contract(
            snapshot.operations,
            schema_version=snapshot.operations_schema_version,
            size_bytes=snapshot.operations_size_bytes,
            sha256=snapshot.operations_sha256,
            max_bytes=_snapshot_max_bytes(),
        ).value
        evidence = verify_json_contract(
            snapshot.evidence,
            schema_version=snapshot.evidence_schema_version,
            size_bytes=snapshot.evidence_size_bytes,
            sha256=snapshot.evidence_sha256,
            max_bytes=_snapshot_max_bytes(),
        ).value
    except (ValidationError, RuntimeError, TypeError, ValueError):
        return None
    if not isinstance(operations, list) or not isinstance(evidence, dict):
        return None
    return [str(item) for item in operations if isinstance(item, str)], evidence


def safe_snapshot_summary(
    snapshot: ModelDeploymentCapabilitySnapshot | None,
    *,
    current_target_sha256: str,
    operation: str | None = None,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    contract = _snapshot_contract(snapshot)
    target_matches = snapshot.target_sha256 == current_target_sha256
    unexpired = snapshot.expires_at > timezone.now()
    operations = contract[0] if contract is not None else []
    evidence = contract[1] if contract is not None else {}
    fresh = bool(
        contract is not None
        and target_matches
        and unexpired
        and snapshot.status == ModelDeploymentCapabilitySnapshot.Status.PASSED
    )
    if snapshot.probe_kind == ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE:
        fresh = bool(
            fresh
            and snapshot.status == ModelDeploymentCapabilitySnapshot.Status.PASSED
            and evidence.get("execution_submitted") is True
            and operation in operations
        )
        from studio.services.deployment_smoke_contract import TEXT_DEPLOYMENT_SMOKE_OPERATIONS
        from studio.services.deployment_smoke_profiles import snapshot_profile_matches
        if operation in TEXT_DEPLOYMENT_SMOKE_OPERATIONS:
            fresh = fresh and snapshot_profile_matches(snapshot)
    return {
        "id": str(snapshot.id),
        "probe_kind": snapshot.probe_kind,
        "status": snapshot.status,
        "operations": operations,
        "prompt_profile": evidence.get("prompt_profile"),
        "target_sha256": snapshot.target_sha256,
        "target_matches": target_matches,
        "fresh": fresh,
        "captured_at": snapshot.captured_at,
        "expires_at": snapshot.expires_at,
    }


def deployment_smoke_operations(deployment: ModelDeployment) -> tuple[str, ...]:
    driver_type = deployment.profile_version.provider.driver_type
    specification = verified_model_profile_specification(
        deployment.profile_version,
    )
    declared = set(model_capability_values(specification))
    if driver_type == ProviderProfile.DriverType.ASYNC_REST:
        supported = set(ASYNC_MEDIA_OPERATIONS)
    elif driver_type in {
        ProviderProfile.DriverType.LOCAL_QWEN,
        ProviderProfile.DriverType.DEEPSEEK_API,
        ProviderProfile.DriverType.OPENAI_COMPATIBLE,
    }:
        supported = set(TEXT_DEPLOYMENT_SMOKE_OPERATIONS)
    else:
        supported = set()
    return tuple(sorted(declared & supported & set(DEPLOYMENT_SMOKE_OPERATIONS)))


def workflow_acceptance_row(
    version: WorkflowTemplateVersion,
    *,
    selected_deployment: ModelDeployment | None,
    latest_attempt: WorkflowSmokeAttempt | None,
    evidence_attempt: WorkflowSmokeAttempt | None,
    has_selected_smoke_attempt: bool,
    graph_limit: int | None = None,
) -> dict[str, Any]:
    acceptance = inspect_workflow_acceptance(
        version,
        smoke_attempt=evidence_attempt,
        smoke_attempt_loaded=True,
        graph_limit=graph_limit,
    )
    validation = acceptance["validation"]
    smoke = acceptance["smoke"]
    validation["validated_deployment_id"] = (
        str(version.validated_deployment_id)
        if version.validated_deployment_id else None
    )
    relationship_deployment = selected_deployment
    if relationship_deployment is None:
        relationship_deployment = version.validated_deployment
    if relationship_deployment is None and latest_attempt is not None:
        relationship_deployment = latest_attempt.deployment

    relationship_attempt = (
        latest_attempt
        if latest_attempt is not None
        and relationship_deployment is not None
        and latest_attempt.deployment_id == relationship_deployment.id
        else None
    )
    relationship_attempt_summary = safe_workflow_attempt_summary(
        relationship_attempt,
    )
    smoke["latest_attempt"] = relationship_attempt_summary
    smoke_evidence = version.smoke_test_evidence
    smoke_evidence_deployment_id = (
        smoke_evidence.get("deployment_id")
        if isinstance(smoke_evidence, dict) else None
    )
    try:
        smoke["evidence_deployment_id"] = (
            str(uuid.UUID(str(smoke_evidence_deployment_id)))
            if smoke_evidence_deployment_id else None
        )
    except (TypeError, ValueError, AttributeError):
        smoke["evidence_deployment_id"] = None

    profile_matches = bool(
        relationship_deployment is not None
        and version.model_profile_version_id
        == relationship_deployment.profile_version_id
    )
    validated_matches = bool(
        relationship_deployment is not None
        and version.validated_deployment_id == relationship_deployment.id
    )
    has_smoke_attempt = bool(
        has_selected_smoke_attempt
        if selected_deployment is not None
        else relationship_attempt is not None
    )
    association_level = (
        "executed"
        if has_smoke_attempt
        else "validated"
        if validated_matches
        else "compatible"
        if profile_matches
        else "unbound"
    )

    selected_validation_mismatch = bool(
        selected_deployment is not None
        and version.validated_deployment_id
        and not validated_matches
    )
    if selected_validation_mismatch:
        validation.update({
            "state": "not_current_for_selected_deployment",
            "fresh": False,
            "reason_code": "workflow_not_validated_for_selected_deployment",
            "message": "历史 Live Validate 不属于当前筛选的模型实例。",
            "current_target_sha256": "",
        })
        if (
            version.smoke_test_state
            != WorkflowTemplateVersion.ValidationState.NOT_RUN
            or smoke["evidence_deployment_id"] is not None
        ):
            smoke.update({
                "state": "not_current_for_selected_deployment",
                "fresh": False,
                "reason_code": (
                    "workflow_smoke_not_current_for_selected_deployment"
                ),
                "message": "历史 Workflow Smoke 证据不属于当前筛选的模型实例。",
            })

    status_allows_validation = version.status in {
        WorkflowTemplateVersion.Status.DRAFT,
        WorkflowTemplateVersion.Status.VALIDATED,
    }
    can_validate_base = bool(
        status_allows_validation
        and relationship_deployment is not None
        and profile_matches
        and relationship_deployment.status == ModelDeployment.Status.ACTIVE
        and relationship_deployment.profile_version.status
        == ModelProfileVersion.Status.PUBLISHED
        and relationship_deployment.profile_version.provider.is_active
        and version.provider_id
        == relationship_deployment.profile_version.provider_id
    )
    validation_precheck_failed = False
    if can_validate_base and relationship_deployment is not None:
        try:
            verify_workflow_version_integrity(
                version,
                graph_limit=graph_limit,
            )
            workflow_execution_target_sha256(
                version,
                relationship_deployment,
            )
        except (ValidationError, RuntimeError, TypeError, ValueError):
            validation_precheck_failed = True
    can_validate = bool(can_validate_base and not validation_precheck_failed)
    unresolved_workflow_attempt = bool(
        relationship_attempt_summary
        and relationship_attempt_summary["resolution_required"]
    )
    can_smoke = bool(
        validation["fresh"]
        and validated_matches
        and version.status == WorkflowTemplateVersion.Status.VALIDATED
        and not unresolved_workflow_attempt
    )
    can_publish = bool(can_smoke and smoke["fresh"])
    editor_url = f"/settings/workflows/{version.id}"
    actions = [
        _action(
            "live_validate",
            url=f"/api/v1/workflow-template-versions/{version.id}/validate/",
            ui_url=f"{editor_url}?focus=validate",
            enabled=can_validate,
            reason_code=(
                "ready"
                if can_validate
                else "workflow_validation_precheck_failed"
                if validation_precheck_failed
                else "validation_target_unavailable"
            ),
            may_call_external_service=True,
        ),
        _action(
            "workflow_smoke",
            url=f"/api/v1/workflow-template-versions/{version.id}/smoke-test/",
            ui_url=f"{editor_url}?focus=smoke",
            enabled=can_smoke,
            reason_code=(
                "ready"
                if can_smoke
                else "workflow_smoke_resolution_required"
                if unresolved_workflow_attempt
                else "workflow_not_validated_for_selected_deployment"
                if selected_validation_mismatch
                else "live_validation_stale"
            ),
            requires_fixture=True,
            requires_confirmation=True,
            may_call_external_service=True,
        ),
        _action(
            "publish",
            url=f"/api/v1/workflow-template-versions/{version.id}/publish/",
            ui_url=f"{editor_url}?focus=publish",
            enabled=can_publish,
            reason_code=("ready" if can_publish else "workflow_smoke_stale"),
            requires_confirmation=True,
        ),
    ]
    if unresolved_workflow_attempt and relationship_attempt is not None:
        actions.append(_action(
            "resolve_workflow_smoke_uncertain",
            method="POST",
            url=(
                "/api/v1/workflow-smoke-attempts/"
                f"{relationship_attempt.id}/resolve-uncertain/"
            ),
            ui_url=f"{editor_url}?focus=smoke",
            enabled=True,
            reason_code="workflow_smoke_resolution_required",
            requires_confirmation=True,
        ))
    return {
        "subject": "workflow",
        "workflow": {
            "id": str(version.id),
            "template_id": str(version.template_id),
            "template_name": version.template.name,
            "template_slug": version.template.slug,
            "version": version.version,
            "status": version.status,
            "is_current": version.is_current,
            "output_kind": version.template.output_kind,
            "model_profile_version_id": (
                str(version.model_profile_version_id)
                if version.model_profile_version_id else None
            ),
            "provider_id": str(version.provider_id) if version.provider_id else None,
        },
        "deployment": (
            {
                "id": str(relationship_deployment.id),
                "key": relationship_deployment.key,
                "name": relationship_deployment.name,
                "status": relationship_deployment.status,
                "profile_version_id": str(relationship_deployment.profile_version_id),
                "provider_id": str(
                    relationship_deployment.profile_version.provider_id,
                ),
                "provider_driver": (
                    relationship_deployment.profile_version.provider.driver_type
                ),
                "runtime_host_id": (
                    str(relationship_deployment.runtime_host_id)
                    if relationship_deployment.runtime_host_id else None
                ),
            }
            if relationship_deployment is not None else None
        ),
        "association": {
            "level": association_level,
            "deployment_id": (
                str(relationship_deployment.id)
                if relationship_deployment is not None else None
            ),
            "profile_matches": profile_matches,
            "validated_deployment_matches": validated_matches,
            "has_smoke_attempt": has_smoke_attempt,
        },
        "validation": validation,
        "smoke": smoke,
        "actions": actions,
    }


def _snapshots_by_operation(
    snapshots: Iterable[ModelDeploymentCapabilitySnapshot],
    *,
    target_sha256: str,
    operations: Iterable[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    operation_set = set(operations)
    pending = operation_set.copy()
    found: dict[str, dict[str, Any]] = {}
    latest: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        contract = _snapshot_contract(snapshot)
        if contract is None:
            continue
        for operation in set(contract[0]) & operation_set:
            summary = safe_snapshot_summary(
                snapshot,
                current_target_sha256=target_sha256,
                operation=operation,
            )
            if summary:
                # Internal classification fact; the public snapshot serializer
                # only exposes its declared safe evidence fields.
                latest.setdefault(operation, {
                    **summary, "_execution_confirmed": contract[1].get("execution_submitted") is True,
                    "_strategy_scoped": bool(summary.get("prompt_profile")) or operation not in TEXT_DEPLOYMENT_SMOKE_OPERATIONS,
                })
            if summary and summary["fresh"] and operation in pending:
                found[operation] = summary
                pending.remove(operation)
    return latest, found


def _operation_evidence_state(*, applicable, target_available, latest, fresh, attempt):
    if not target_available:
        return "unavailable"
    if not applicable:
        return "not_applicable"
    if attempt and attempt["resolution_required"]:
        return "uncertain"
    if fresh:
        return "fresh"
    if latest:
        if not latest["target_matches"]:
            return "drifted"
        if latest["status"] == "failed":
            return "failed"
        if not latest.get("_strategy_scoped", True):
            return "unverified"
        if not latest["_execution_confirmed"]:
            return "unverified"
        if latest["status"] == "passed" and latest["expires_at"] <= timezone.now():
            return "expired"
        return "unverified"
    if attempt:
        return "failed" if attempt["status"] == "failed" else "unverified"
    return "missing"


@security_boundary_resolution_cache()
def deployment_acceptance_row(
    deployment: ModelDeployment,
    *,
    health_snapshot: ModelDeploymentCapabilitySnapshot | None,
    smoke_snapshots: Iterable[ModelDeploymentCapabilitySnapshot],
    latest_attempts: Iterable[ModelDeploymentSmokeAttempt],
    profile_match_count: int,
    profile_evidence_match_count: int,
    validated_count: int,
    smoke_history_count: int,
    fresh_validation_count: int,
    drifted_validation_count: int,
    fresh_smoke_count: int,
    unresolved_smoke_count: int,
) -> dict[str, Any]:
    try:
        executable_operations = set(deployment_smoke_operations(deployment))
    except (ValidationError, RuntimeError, TypeError, ValueError):
        executable_operations = set()
        contract_available = False
    else:
        contract_available = True

    if contract_available:
        try:
            validate_deployment_execution_contract(deployment)
            current_target_sha256 = deployment_target_sha256(deployment)
            target_available = True
        except (ValidationError, RuntimeError, TypeError, ValueError):
            current_target_sha256 = ""
            target_available = False
    else:
        current_target_sha256 = ""
        target_available = False

    attempts_by_operation = {
        attempt.operation: attempt for attempt in latest_attempts
    }
    configured_operations = set(executable_operations)
    configured_operations.update(attempts_by_operation)
    snapshots = list(smoke_snapshots)
    for snapshot in snapshots:
        snapshot.deployment = deployment
    for snapshot in snapshots:
        contract = _snapshot_contract(snapshot)
        if contract is not None:
            configured_operations.update(contract[0])
    latest_snapshots, fresh_snapshots = _snapshots_by_operation(
        snapshots,
        target_sha256=current_target_sha256,
        operations=configured_operations,
    )

    if not target_available:
        executable_operations = set()
        fresh_snapshots = {}
    health_summary = safe_snapshot_summary(
        health_snapshot,
        current_target_sha256=current_target_sha256,
    )
    if health_summary is not None and not target_available:
        health_summary["target_matches"] = False
        health_summary["fresh"] = False
    operation_rows = []
    for operation in sorted(configured_operations):
        from studio.services.deployment_smoke_contract import TEXT_DEPLOYMENT_SMOKE_OPERATIONS
        from studio.services.deployment_smoke_profiles import smoke_profile_choices, smoke_profile_reference
        strategy_results = []
        strategy_catalog_available = True
        if operation in TEXT_DEPLOYMENT_SMOKE_OPERATIONS and target_available:
            try:
                choices = smoke_profile_choices(deployment, operation)
            except (ValidationError, RuntimeError, TypeError, ValueError):
                choices = []
                strategy_catalog_available = False
            for selected in choices:
                reference = smoke_profile_reference(deployment, operation, selected)
                matching = [s for s in snapshots if s.prompt_profile_binding_id == selected.binding.id
                            and s.prompt_profile_version_id == selected.profile_version.id
                            and s.prompt_profile_contract_sha256 == selected.profile_version.contract_sha256]
                strategy_latest, strategy_fresh = _snapshots_by_operation(
                    matching, target_sha256=current_target_sha256, operations=[operation],
                )
                strategy_results.append({
                    "prompt_profile": reference,
                    "evidence_state": _operation_evidence_state(
                        applicable=True, target_available=True, latest=strategy_latest.get(operation),
                        fresh=strategy_fresh.get(operation), attempt=None,
                    ),
                    "latest_snapshot": strategy_latest.get(operation),
                    "fresh_passed_snapshot": strategy_fresh.get(operation),
                })
        latest_attempt = attempts_by_operation.get(operation)
        latest_attempt_summary = safe_deployment_attempt_summary(latest_attempt)
        attempt_blocks_new_submission = bool(
            latest_attempt
            and (
                latest_attempt.status in {
                    ModelDeploymentSmokeAttempt.Status.QUEUED,
                    ModelDeploymentSmokeAttempt.Status.SUBMITTING,
                    ModelDeploymentSmokeAttempt.Status.SUBMITTED,
                    ModelDeploymentSmokeAttempt.Status.CANCEL_REQUESTED,
                }
                or (
                    latest_attempt.status
                    == ModelDeploymentSmokeAttempt.Status.UNCERTAIN
                    and (latest_attempt_summary or {}).get("resolution_required")
                )
            )
        )
        enabled = bool(
            deployment.status == ModelDeployment.Status.ACTIVE
            and operation in executable_operations
            and strategy_catalog_available
            and not attempt_blocks_new_submission
        )
        operation_rows.append({
            "operation": operation,
            "prompt_profiles": strategy_results,
            "applicable": operation in executable_operations,
            "evidence_state": _operation_evidence_state(
                applicable=operation in executable_operations,
                target_available=target_available and strategy_catalog_available,
                latest=latest_snapshots.get(operation),
                fresh=fresh_snapshots.get(operation),
                attempt=latest_attempt_summary,
            ),
            "latest_snapshot": latest_snapshots.get(operation),
            "fresh_passed_snapshot": fresh_snapshots.get(operation) if strategy_catalog_available else None,
            "latest_attempt": latest_attempt_summary,
            "action": _action(
                "deployment_smoke",
                url=f"/api/v1/model-deployments/{deployment.id}/smoke-test/",
                ui_url=(
                    "/settings?section=models"
                    f"&deployment={deployment.id}&focus=smoke"
                ),
                enabled=enabled,
                reason_code=(
                    "ready"
                    if enabled
                    else "model_profile_contract_invalid"
                    if not contract_available
                    else "deployment_target_invalid"
                    if not target_available
                    else "prompt_profile_catalog_invalid"
                    if not strategy_catalog_available
                    else "attempt_in_progress_or_resolution_required"
                    if attempt_blocks_new_submission
                    else "deployment_smoke_not_applicable"
                ),
                requires_confirmation=True,
                may_call_external_service=True,
            ),
        })

    driver_type = deployment.profile_version.provider.driver_type
    actions = []
    if driver_type == ProviderProfile.DriverType.COMFYUI:
        if deployment.runtime_host_id:
            actions.append(_action(
                "runtime_host_probe",
                url=f"/api/v1/runtime-hosts/{deployment.runtime_host_id}/probe/",
                ui_url="/settings?section=runtime",
                enabled=deployment.status == ModelDeployment.Status.ACTIVE,
                reason_code=(
                    "ready"
                    if deployment.status == ModelDeployment.Status.ACTIVE
                    else "deployment_inactive"
                ),
                may_call_external_service=True,
            ))
    elif driver_type == ProviderProfile.DriverType.ASYNC_REST:
        probe_enabled = bool(
            deployment.status == ModelDeployment.Status.ACTIVE
            and contract_available
            and target_available
        )
        actions.append(_action(
            "deployment_probe",
            url=f"/api/v1/model-deployments/{deployment.id}/probe/",
            ui_url=(
                "/settings?section=models"
                f"&deployment={deployment.id}&focus=probe"
            ),
            enabled=probe_enabled,
            reason_code=(
                "ready"
                if probe_enabled
                else "model_profile_contract_invalid"
                if not contract_available
                else "deployment_target_invalid"
                if not target_available
                else "deployment_inactive"
            ),
            may_call_external_service=True,
        ))
    actions.append(_action(
        "view_workflow_associations",
        url=(
            "/api/v1/acceptance-smoke/?subject=workflow"
            f"&deployment_id={deployment.id}"
        ),
        ui_url=(
            "/settings/acceptance-smoke?subject=workflow"
            f"&deployment_id={deployment.id}"
        ),
        enabled=True,
        reason_code="ready",
    ))
    return {
        "subject": "deployment",
        "deployment": {
            "id": str(deployment.id),
            "key": deployment.key,
            "name": deployment.name,
            "status": deployment.status,
            "profile_version_id": str(deployment.profile_version_id),
            "provider_id": str(deployment.profile_version.provider_id),
            "provider_driver": driver_type,
            "runtime_host_id": (
                str(deployment.runtime_host_id)
                if deployment.runtime_host_id else None
            ),
            "current_target_sha256": current_target_sha256,
            "profile_contract_state": (
                "current" if contract_available else "invalid"
            ),
            "profile_contract_reason_code": (
                "current"
                if contract_available else "model_profile_contract_invalid"
            ),
            "target_state": "current" if target_available else "invalid",
            "target_reason_code": (
                "current"
                if target_available
                else "model_profile_contract_invalid"
                if not contract_available
                else "deployment_target_invalid"
            ),
        },
        "health": health_summary,
        "deployment_smoke": {
            "applicable": bool(executable_operations),
            "reason_code": (
                "ready"
                if executable_operations
                else "model_profile_contract_invalid"
                if not contract_available
                else "deployment_target_invalid"
                if not target_available
                else "use_workflow_smoke"
                if driver_type == ProviderProfile.DriverType.COMFYUI
                else "deployment_smoke_not_configured"
            ),
            "operations": operation_rows,
        },
        "workflow_association_summary": {
            "profile_match_count": profile_match_count,
            "compatible_only_count": max(
                0,
                profile_match_count - profile_evidence_match_count,
            ),
            "validated_count": validated_count,
            "smoke_history_count": smoke_history_count,
            "fresh_validation_count": fresh_validation_count,
            "drifted_validation_count": drifted_validation_count,
            "fresh_smoke_count": fresh_smoke_count,
            "unresolved_smoke_count": unresolved_smoke_count,
        },
        "actions": actions,
    }
