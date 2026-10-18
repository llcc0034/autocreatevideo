from __future__ import annotations

import hashlib
import re
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import (
    BusinessOutputVersion,
    ContentSourceVersion,
    ConfigDefinition,
    ConfigRevision,
    ModelArtifact,
    ModelDeployment,
    ModelExecutionInstance,
    ModelExecutionInstanceVersion,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    FieldDefinition,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    ProviderProfile,
    RuntimeHost,
    Style,
    StyleCategory,
    WorkflowProjector,
    WorkflowProjectorVersion,
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
        (BusinessOutputVersion, "business_output_version"),
        (ContentSourceVersion, "content_source_version"),
        (Style, "visual_style"),
        (StyleCategory, "style_category"),
        (ProviderProfile, "provider_profile"),
        (ModelProfileVersion, "model_profile_version"),
        (ModelDeployment, "model_deployment"),
        (ModelExecutionInstance, "model_execution_instance"),
        (ModelExecutionInstanceVersion, "model_execution_instance_version"),
        (ModelArtifact, "model_artifact"),
        (FieldDefinition, "field_definition"),
        (PromptProfileVersion, "prompt_profile_version"),
        (PromptProfileFieldRule, "prompt_profile_field_rule"),
        (WorkflowProjector, "workflow_projector"),
        (WorkflowProjectorVersion, "workflow_projector_version"),
        (WorkflowTemplateVersion, "workflow_template_version"),
        (ConfigDefinition, "config_definition"),
        (ConfigRevision, "config_revision"),
        (RuntimeHost, "runtime_host"),
        (ProductionRecipeVersion, "production_recipe_version"),
        (ProductionRun, "production_run"),
    )
    for model, object_type in mapping:
        if isinstance(instance, model):
            return object_type
    raise ValidationError("不支持的生命周期审计对象")


def lifecycle_summary(instance) -> dict[str, Any]:
    """Return a flat allow-listed projection with no endpoint/path/raw JSON."""
    if isinstance(instance, ContentSourceVersion):
        return {"key": str(instance.source_key), "version": instance.version, "revision": instance.revision,
            "status": instance.status, "source_selector": instance.reader_selector, "contract_sha256": instance.contract_sha256}
    if isinstance(instance, StyleCategory):
        return {"key": instance.key, "is_active": instance.is_active, "sort_order": instance.sort_order,
            "sha256": _digest({"name": instance.name, "description": instance.description})}
    if isinstance(instance, Style):
        return {"key": instance.slug, "category": instance.category_id,
            "is_active": instance.is_active, "is_system": instance.is_system,
            "scope_type": "global" if instance.owner_id is None else "personal",
            "sha256": _digest({"name": instance.name, "description": instance.description,
                "visual_prompt": instance.visual_prompt, "negative_prompt": instance.negative_prompt,
                "defaults": instance.defaults, "thumbnail_path": instance.thumbnail_path})}
    if isinstance(instance, ProductionRun):
        return {"allow_external_calls": instance.allow_external_calls, "cost_currency": instance.cost_currency}

    if isinstance(instance, ModelExecutionInstance):
        return {"key": instance.key, "is_active": instance.is_active, "revision": instance.revision}
    if isinstance(instance, ModelExecutionInstanceVersion):
        from studio.services.execution_instances import execution_version_configuration

        return {
            "version": instance.version, "revision": instance.revision, "status": instance.status,
            "operation": instance.operation, "mode": instance.mode,
            "prompt_profile_version_id": _id(instance.prompt_profile_version_id),
            # Include child choices, not just the six resource selectors. Raw
            # parameter/input content still never enters the flat audit record.
            "sha256": _digest(execution_version_configuration(instance)),
        }
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
        locator_sha256 = hashlib.sha256(canonical_json_bytes({
            "filename": instance.filename,
            "storage_uri": instance.storage_uri,
            "inventory_folder": instance.inventory_folder,
            "inventory_node_type": instance.inventory_node_type,
            "inventory_input_name": instance.inventory_input_name,
        })).hexdigest()
        return {
            "profile_version_id": _id(instance.profile_version_id),
            "role": instance.role,
            # Paths/endpoints stay out of audit payloads; their digest still
            # makes a registration-only relocation explicitly traceable.
            "locator_sha256": locator_sha256,
            "sha256": instance.sha256,
            "size_bytes": instance.size_bytes,
            "status": instance.status,
            "verified_snapshot_id": _id(instance.verified_snapshot_id),
            "verified_at": _time(instance.verified_at),
        }
    if isinstance(instance, FieldDefinition):
        return {
            "key": instance.key,
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
            "field_definition_version_id": _id(instance.field_definition_version_id),
            "source_selector": instance.source_selector,
            "channel": instance.channel,
            "position": instance.position,
        }
    if isinstance(instance, WorkflowProjector):
        return {
            "key": instance.key,
            "is_active": instance.is_active,
        }
    if isinstance(instance, WorkflowProjectorVersion):
        return {
            "projector_id": _id(instance.projector_id),
            "version": instance.version,
            "engine_key": instance.engine_key,
            "engine_version": instance.engine_version,
            "driver_type": instance.provider_driver_type,
            "operation": instance.operation,
            "output_kind": instance.output_kind,
            "definition_sha256": instance.definition_sha256,
            "status": instance.status,
            "is_current": instance.is_current,
            "published_at": _time(instance.published_at),
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
            "validated_deployment_id": _id(instance.validated_deployment_id),
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
            "configuration_class": instance.configuration_class,
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
