from __future__ import annotations

import hashlib
import re
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import (
    ConfigDefinition,
    ConfigRevision,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProductionRecipeVersion,
    ProviderProfile,
    RuntimeHost,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import canonical_json_bytes


REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")


def normalize_reason_code(value: Any, *, default: str) -> str:
    candidate = str(value or "").strip().casefold()
    if not candidate:
        candidate = default
    if REASON_CODE_PATTERN.fullmatch(candidate) is None:
        raise ValidationError({
            "reason_code": "必须是 2-80 位小写字母开头的稳定代码",
        })
    return candidate


def request_reason_code(request, *, default: str) -> str:
    return normalize_reason_code(
        request.headers.get("X-Audit-Reason-Code", ""),
        default=default,
    )


def _id(value) -> str | None:
    return str(value) if value else None


def _time(value) -> str | None:
    return value.isoformat() if value else None


def lifecycle_object_type(instance) -> str:
    mapping = (
        (ProviderProfile, "provider_profile"),
        (ModelProfileVersion, "model_profile_version"),
        (ModelDeployment, "model_deployment"),
        (ModelArtifact, "model_artifact"),
        (PromptFieldDefinition, "prompt_field_definition"),
        (PromptProfileVersion, "prompt_profile_version"),
        (PromptProfileFieldRule, "prompt_profile_field_rule"),
        (PromptProfileBinding, "prompt_profile_binding"),
        (WorkflowTemplateVersion, "workflow_template_version"),
        (ConfigDefinition, "config_definition"),
        (ConfigRevision, "config_revision"),
        (RuntimeHost, "runtime_host"),
        (ProductionRecipeVersion, "production_recipe_version"),
    )
    for model, object_type in mapping:
        if isinstance(instance, model):
            return object_type
    raise ValidationError("不支持的生命周期审计对象")


def lifecycle_summary(instance) -> dict[str, Any]:
    """Return a flat allow-listed projection with no endpoint/path/raw JSON."""

    if isinstance(instance, ProviderProfile):
        return {
            "key": instance.key,
            "driver_type": instance.driver_type,
            "credential_bound": bool(instance.credential_id),
            "is_system": instance.is_system,
            "is_active": instance.is_active,
        }
    if isinstance(instance, ModelProfileVersion):
        return {
            "profile_key": instance.profile_key,
            "version": instance.version,
            "provider_id": _id(instance.provider_id),
            "modality": instance.modality,
            "specification_sha256": instance.specification_sha256,
            "status": instance.status,
            "published_at": _time(instance.published_at),
        }
    if isinstance(instance, ModelDeployment):
        return {
            "key": instance.key,
            "profile_version_id": _id(instance.profile_version_id),
            "runtime_host_id": _id(instance.runtime_host_id),
            "credential_bound": bool(instance.credential_id),
            "runtime_config_sha256": instance.runtime_config_sha256,
            "is_default": instance.is_default,
            "status": instance.status,
        }
    if isinstance(instance, ModelArtifact):
        return {
            "profile_version_id": _id(instance.profile_version_id),
            "role": instance.role,
            "sha256": instance.sha256,
            "size_bytes": instance.size_bytes,
            "status": instance.status,
            "verified_snapshot_id": _id(instance.verified_snapshot_id),
            "verified_at": _time(instance.verified_at),
        }
    if isinstance(instance, PromptFieldDefinition):
        return {
            "key": instance.key,
            "category": instance.category,
            "value_type": instance.value_type,
            "is_required": instance.is_required,
            "is_active": instance.is_active,
        }
    if isinstance(instance, PromptProfileVersion):
        return {
            "profile_key": instance.profile_key,
            "version": instance.version,
            "model_family": instance.model_family,
            "operation": instance.operation,
            "mode": instance.mode,
            "status": instance.status,
            "review_state": instance.review_state,
            "contract_sha256": instance.contract_sha256,
            "published_at": _time(instance.published_at),
        }
    if isinstance(instance, PromptProfileFieldRule):
        return {
            "profile_version_id": _id(instance.profile_version_id),
            "field_definition_id": _id(instance.field_definition_id),
            "source_selector": instance.source_selector,
            "channel": instance.channel,
            "position": instance.position,
        }
    if isinstance(instance, PromptProfileBinding):
        return {
            "key": instance.key,
            "operation": instance.operation,
            "model_family": instance.model_family,
            "mode": instance.mode,
            "model_profile_version_id": _id(instance.model_profile_version_id),
            "prompt_profile_version_id": _id(instance.prompt_profile_version_id),
            "priority": instance.priority,
            "is_active": instance.is_active,
        }
    if isinstance(instance, WorkflowTemplateVersion):
        return {
            "template_id": _id(instance.template_id),
            "version": instance.version,
            "provider_id": _id(instance.provider_id),
            "model_profile_version_id": _id(instance.model_profile_version_id),
            "graph_sha256": instance.graph_sha256,
            "input_contract_sha256": instance.input_contract_sha256,
            "status": instance.status,
            "validation_state": instance.validation_state,
            "validation_evidence_sha256": instance.validation_evidence_sha256,
            "smoke_test_state": instance.smoke_test_state,
            "smoke_test_evidence_sha256": instance.smoke_test_evidence_sha256,
            "is_current": instance.is_current,
            "published_at": _time(instance.published_at),
        }
    if isinstance(instance, ConfigDefinition):
        return {
            "key": instance.key,
            "domain": instance.domain,
            "value_type": instance.value_type,
            "restart_required": instance.restart_required,
            "is_required": instance.is_required,
            "is_secret": instance.is_secret,
            "is_active": instance.is_active,
            "value_schema_sha256": instance.value_schema_sha256,
            "initial_value_sha256": instance.initial_value_sha256,
        }
    if isinstance(instance, ConfigRevision):
        return {
            "scope_type": instance.scope_type,
            "project_id": _id(instance.project_id),
            "run_id": _id(instance.run_id),
            "revision": instance.revision,
            "status": instance.status,
            "based_on_revision_id": _id(instance.based_on_revision_id),
            "activation_state": instance.activation_state,
            "values_sha256": instance.values_sha256,
            "published_at": _time(instance.published_at),
            "activated_at": _time(instance.activated_at),
        }
    if isinstance(instance, RuntimeHost):
        return {
            "key": instance.key,
            "status": instance.status,
        }
    if isinstance(instance, ProductionRecipeVersion):
        return {
            "recipe_key": instance.recipe_key,
            "version": instance.version,
            "definition_sha256": instance.definition_sha256,
            "status": instance.status,
            "published_at": _time(instance.published_at),
        }
    raise ValidationError("不支持的生命周期审计对象")


def _digest(summary: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(summary)).hexdigest()


def record_platform_lifecycle_event(
    instance,
    *,
    actor,
    action: str,
    reason_code: str,
    before_summary: dict[str, Any] | None = None,
    after_summary: dict[str, Any] | None = None,
) -> PlatformLifecycleAuditEvent:
    before = dict(before_summary or {})
    after = dict(after_summary or {})
    return PlatformLifecycleAuditEvent.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=normalize_reason_code(action, default="audit.event"),
        object_type=lifecycle_object_type(instance),
        object_id=instance.pk,
        reason_code=normalize_reason_code(reason_code, default="api.lifecycle"),
        before_sha256=_digest(before),
        after_sha256=_digest(after),
        before_summary=before,
        after_summary=after,
    )
