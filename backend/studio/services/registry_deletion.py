from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Model
from django.db.models.deletion import ProtectedError

from studio.models import (
    ConfigDefinition,
    ConfigValue,
    GenerationAttempt,
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    PromptFieldDefinition,
    PromptArtifact,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProductionRun,
    ProductionRecipeVersion,
    ProviderProfile,
    WorkflowModelRequirement,
    WorkflowSemanticBinding,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import canonical_json_bytes
from studio.services.platform_lifecycle_audit import (
    lifecycle_summary,
    normalize_reason_code,
    record_platform_lifecycle_event,
)
from studio.services.prompt_profiles import (
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    refresh_prompt_profile_contract,
)
from studio.services.registry_reference_lock import registry_reference_write_guard


RESOURCE_MODELS: dict[str, type[Model]] = {
    "provider_profile": ProviderProfile,
    "model_profile_version": ModelProfileVersion,
    "model_deployment": ModelDeployment,
    "model_artifact": ModelArtifact,
    "workflow_template_version": WorkflowTemplateVersion,
    "production_recipe_version": ProductionRecipeVersion,
    "config_definition": ConfigDefinition,
    "prompt_field_definition": PromptFieldDefinition,
    "prompt_profile_version": PromptProfileVersion,
    "prompt_profile_field_rule": PromptProfileFieldRule,
    "prompt_profile_binding": PromptProfileBinding,
}

AUTOMATIC_DEPENDENCY_MODELS: dict[str, type[Model]] = {
    "prompt_profile_field_rule": PromptProfileFieldRule,
    "workflow_semantic_binding": WorkflowSemanticBinding,
    "workflow_model_requirement": WorkflowModelRequirement,
}

SELECTABLE_DELETE = "selectable_delete"
AUTOMATIC_DELETE = "automatic_delete"
BLOCKING_REFERENCE = "blocking_reference"
PRESERVED_HISTORY = "preserved_history"

RECIPE_UUID_REFERENCE_MODELS: dict[str, type[Model]] = {
    "provider_profile_id": ProviderProfile,
    "model_profile_version_id": ModelProfileVersion,
    "model_deployment_id": ModelDeployment,
    "default_model_deployment_id": ModelDeployment,
    "model_artifact_id": ModelArtifact,
    "workflow_version_id": WorkflowTemplateVersion,
    "default_workflow_version_id": WorkflowTemplateVersion,
    "prompt_profile_binding_id": PromptProfileBinding,
    "bible_image_prompt_profile_binding_id": PromptProfileBinding,
}
RECIPE_KEY_REFERENCE_MODELS: dict[str, tuple[type[Model], str]] = {
    "prompt_profile_binding_key": (PromptProfileBinding, "key"),
    "bible_image_prompt_profile_binding_key": (PromptProfileBinding, "key"),
}


class RegistryDeletionConflict(Exception):
    def __init__(self, code: str, detail: str, *, impact: dict[str, Any] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.impact = impact


class RegistryDeletionTargetNotFound(Exception):
    pass


@dataclass(frozen=True)
class TargetPolicy:
    allowed: bool
    code: str = ""
    reason: str = ""
    advisory: str = ""


def _reference_paths(value: Any, target: str, path: str = "$") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, item in value.items():
            child = f"{path}.{key}"
            paths.extend(_reference_paths(item, target, child))
        return paths
    if isinstance(value, list):
        paths = []
        for index, item in enumerate(value):
            paths.extend(_reference_paths(item, target, f"{path}[{index}]"))
        return paths
    return [path] if isinstance(value, str) and value == target else []


def _recipe_registry_references(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in RECIPE_UUID_REFERENCE_MODELS or key in RECIPE_KEY_REFERENCE_MODELS:
                items = item if isinstance(item, list) else [item]
                for index, reference in enumerate(items):
                    if reference in (None, ""):
                        continue
                    yield key, reference, f"{child}[{index}]" if isinstance(item, list) else child
            yield from _recipe_registry_references(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _recipe_registry_references(item, f"{path}[{index}]")


def validate_recipe_registry_references(definition: Any) -> None:
    errors: list[str] = []
    for key, reference, path in _recipe_registry_references(definition):
        if key in RECIPE_UUID_REFERENCE_MODELS:
            try:
                object_id = UUID(str(reference))
            except (TypeError, ValueError, AttributeError):
                errors.append(f"{path} 必须是有效 UUID")
                continue
            if not RECIPE_UUID_REFERENCE_MODELS[key]._default_manager.filter(pk=object_id).exists():
                errors.append(f"{path} 引用的平台注册对象不存在")
        else:
            model, field = RECIPE_KEY_REFERENCE_MODELS[key]
            if not model._default_manager.filter(**{field: str(reference)}).exists():
                errors.append(f"{path} 引用的平台注册对象不存在")
    if errors:
        raise ValidationError({"definition": errors})


def recipe_references(identifier) -> list[tuple[ProductionRecipeVersion, list[str]]]:
    target = str(identifier)
    rows: list[tuple[ProductionRecipeVersion, list[str]]] = []
    for recipe in ProductionRecipeVersion.objects.all():
        paths = [
            path for _key, reference, path in _recipe_registry_references(recipe.definition)
            if str(reference) == target
        ]
        if paths:
            rows.append((recipe, paths))
    return rows


def recipe_references_any(*identifiers) -> list[tuple[ProductionRecipeVersion, list[str]]]:
    targets = {str(identifier) for identifier in identifiers if identifier not in (None, "")}
    rows: list[tuple[ProductionRecipeVersion, list[str]]] = []
    for recipe in ProductionRecipeVersion.objects.all():
        paths = sorted({
            path for _key, reference, path in _recipe_registry_references(recipe.definition)
            if str(reference) in targets
        })
        if paths:
            rows.append((recipe, paths))
    return rows


def recipe_reference_exists(identifier, *, statuses: Iterable[str] | None = None) -> bool:
    allowed = set(statuses or ())
    return any(
        not allowed or recipe.status in allowed
        for recipe, _paths in recipe_references(identifier)
    )


def _status(instance: Model) -> str:
    value = getattr(instance, "status", None)
    if value is not None:
        return str(value)
    if hasattr(instance, "is_active"):
        return "active" if bool(getattr(instance, "is_active")) else "inactive"
    return "recorded"


def _label(instance: Model) -> str:
    if isinstance(instance, ProviderProfile):
        return f"{instance.name} · {instance.key}"
    if isinstance(instance, ModelProfileVersion):
        return f"{instance.profile_key} · v{instance.version}"
    if isinstance(instance, ModelDeployment):
        return f"{instance.name} · {instance.key}"
    if isinstance(instance, ModelArtifact):
        return instance.logical_name or instance.filename
    if isinstance(instance, WorkflowTemplateVersion):
        return f"{instance.template.name} · v{instance.version}"
    if isinstance(instance, ProductionRecipeVersion):
        return f"{instance.name} · v{instance.version}"
    if isinstance(instance, ConfigDefinition):
        return instance.key
    if isinstance(instance, PromptFieldDefinition):
        return f"{instance.label} · {instance.key}"
    if isinstance(instance, PromptProfileVersion):
        return f"{instance.name} · v{instance.version}"
    if isinstance(instance, PromptProfileFieldRule):
        return f"{instance.field_definition.label} · {instance.channel}"
    if isinstance(instance, PromptProfileBinding):
        return f"{instance.name} · {instance.key}"
    return str(instance)


def _resource_type(instance: Model) -> str:
    for key, model in RESOURCE_MODELS.items():
        if isinstance(instance, model):
            return key
    return instance._meta.model_name


def _dependency_key(relationship: str, resource_type: str, object_id: str) -> str:
    raw = f"{relationship}:{resource_type}:{object_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _state_sha256(instance: Model) -> str:
    """Hash a safe freshness projection without exposing raw paths or JSON."""

    updated_at = getattr(instance, "updated_at", None)
    payload: dict[str, Any] = {
        "model": instance._meta.label_lower,
        "object_id": str(instance.pk),
        "updated_at": updated_at.isoformat() if updated_at else None,
    }
    if _resource_type(instance) in RESOURCE_MODELS:
        payload["lifecycle"] = lifecycle_summary(instance)
    else:
        payload["status"] = _status(instance)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _dependency(
    instance: Model,
    *,
    relationship: str,
    relationship_label: str,
    disposition: str,
    reason_code: str,
    reason: str,
    reference_paths: list[str] | None = None,
) -> dict[str, Any]:
    resource_type = _resource_type(instance)
    object_id = str(instance.pk)
    return {
        "dependency_key": _dependency_key(relationship, resource_type, object_id),
        "resource_type": resource_type,
        "object_id": object_id,
        "label": _label(instance),
        "status": _status(instance),
        "relationship": relationship,
        "relationship_label": relationship_label,
        "disposition": disposition,
        "reason_code": reason_code,
        "reason": reason,
        "reference_paths": list(reference_paths or []),
        "state_sha256": _state_sha256(instance),
    }


def _history_dependency(
    instance: Model,
    *,
    relationship: str,
    relationship_label: str,
    resource_type: str,
    label: str,
    status: str,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    object_id = str(instance.pk)
    return {
        "dependency_key": _dependency_key(relationship, resource_type, object_id),
        "resource_type": resource_type,
        "object_id": object_id,
        "label": label,
        "status": status,
        "relationship": relationship,
        "relationship_label": relationship_label,
        "disposition": PRESERVED_HISTORY,
        "reason_code": reason_code,
        "reason": reason,
        "reference_paths": [],
        "state_sha256": _state_sha256(instance),
    }


def _recipe_dependency(recipe: ProductionRecipeVersion, paths: list[str]) -> dict[str, Any]:
    if recipe.status == ProductionRecipeVersion.Status.DRAFT and _can_delete_as_dependency(recipe):
        disposition = SELECTABLE_DELETE
        code = "draft_recipe_can_be_deleted"
        reason = "该草稿配方没有运行或决策事实；只有人工勾选后才会与目标一并删除。"
    elif recipe.status == ProductionRecipeVersion.Status.DRAFT:
        disposition = PRESERVED_HISTORY
        code = "draft_recipe_has_history"
        reason = "该草稿配方已有运行、人工决策或配置引用事实，不能作为依赖级联删除。"
    else:
        disposition = BLOCKING_REFERENCE
        code = "frozen_recipe_reference"
        reason = "已发布/已退役配方是冻结版本；请发布替代版本并退役当前路由，不能级联删除。"
    return _dependency(
        recipe,
        relationship="recipe_definition_reference",
        relationship_label="生产配方定义引用",
        disposition=disposition,
        reason_code=code,
        reason=reason,
        reference_paths=paths,
    )


def _target_policy(instance: Model, actor=None) -> TargetPolicy:
    if isinstance(instance, ProviderProfile):
        if instance.is_system:
            return TargetPolicy(False, "system_provider", "系统 Provider 只能停用，不能删除。")
        if instance.is_active:
            return TargetPolicy(False, "provider_active", "请先停用 Provider，再发起删除复核。")
    elif isinstance(instance, ModelProfileVersion):
        if instance.status != ModelProfileVersion.Status.DRAFT:
            return TargetPolicy(
                False,
                "frozen_model_profile",
                "published/retired 模型能力版本必须保留；应使用退役而不是删除。",
                "保留版本 ID 与已冻结合同，必要时创建替代版本。",
            )
    elif isinstance(instance, ModelDeployment):
        if instance.status != ModelDeployment.Status.DISABLED:
            return TargetPolicy(
                False,
                "deployment_not_disabled",
                "只有 disabled 且没有运行证据的部署记录才可删除。",
                "已有证据的部署应退役并保留 ID。",
            )
    elif isinstance(instance, ModelArtifact):
        if instance.profile_version.status != ModelProfileVersion.Status.DRAFT:
            return TargetPolicy(False, "artifact_profile_frozen", "只有 draft 模型版本下的文件声明可删除。")
        if instance.status != ModelArtifact.Status.DECLARED:
            return TargetPolicy(False, "artifact_not_pristine", "只有从未核验的 declared 文件声明可删除。")
        if instance.verified_snapshot_id or instance.verified_at or instance.verified_by_id:
            return TargetPolicy(False, "artifact_verified", "文件声明已有核验证据，必须保留。")
    elif isinstance(instance, WorkflowTemplateVersion):
        if instance.status != WorkflowTemplateVersion.Status.DRAFT:
            return TargetPolicy(
                False,
                "workflow_frozen",
                "只有未使用的 draft 工作流版本可删除。",
                "validated/published/retired 工作流应保留或退役。",
            )
        if (
            instance.validation_state != WorkflowTemplateVersion.ValidationState.NOT_RUN
            or instance.smoke_test_state != WorkflowTemplateVersion.ValidationState.NOT_RUN
            or instance.validation_evidence_sha256
            or instance.smoke_test_evidence_sha256
        ):
            return TargetPolicy(
                False,
                "workflow_has_evidence",
                "工作流已经产生校验或烟测证据，不能按空草稿删除。",
                "保留该版本作为验证历史。",
            )
    elif isinstance(instance, ProductionRecipeVersion):
        if instance.status != ProductionRecipeVersion.Status.DRAFT:
            return TargetPolicy(
                False,
                "recipe_frozen",
                "published/retired 生产配方是冻结版本，不能删除。",
                "发布替代版本后退役当前配方。",
            )
    elif isinstance(instance, ConfigDefinition):
        if instance.configuration_class == ConfigDefinition.ConfigurationClass.SECURITY_BOUNDARY and not getattr(actor, "is_superuser", False):
            return TargetPolicy(False, "superuser_required", "只有超级管理员可以删除安全边界配置定义。")
        if instance.is_active:
            return TargetPolicy(False, "config_definition_active", "请先停用配置定义，再发起删除复核。")
        if instance.is_required:
            return TargetPolicy(False, "config_definition_required", "必需配置定义不能删除。")
    elif isinstance(instance, PromptProfileVersion):
        if instance.status != PromptProfileVersion.Status.DRAFT:
            return TargetPolicy(False, "prompt_profile_frozen", "published/retired PromptProfile 必须保留。")
    elif isinstance(instance, PromptProfileFieldRule):
        if instance.profile_version.status != PromptProfileVersion.Status.DRAFT:
            return TargetPolicy(False, "prompt_rule_frozen", "published/retired PromptProfile 的字段规则不能删除。")
    elif isinstance(instance, PromptProfileBinding):
        if instance.is_active:
            return TargetPolicy(False, "prompt_binding_active", "请先停用运行时绑定，再发起删除复核。")
    return TargetPolicy(True)


def _can_delete_as_dependency(instance: Model) -> bool:
    """Return whether one dependency can be safely selected in its parent plan.

    A parent review must never bypass the dependency's own lifecycle policy or
    hide another review level. Owned automatic children are safe because they
    are already surfaced as part of that dependency's deletion contract; every
    other nested relation requires a separate review.
    """

    if not _target_policy(instance).allowed:
        return False
    return not any(
        row["disposition"] != AUTOMATIC_DELETE
        for row in _dependencies(instance)
    )


def _dependencies(instance: Model) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(instance, ProviderProfile):
        for profile in instance.model_profile_versions.select_related("provider"):
            rows.append(_dependency(
                profile,
                relationship="provider_model_profile",
                relationship_label="模型能力版本引用",
                disposition=BLOCKING_REFERENCE,
                reason_code="separate_model_profile_review_required",
                reason="模型能力版本有自己的依赖图；请先在该版本上单独执行删除复核或保留冻结版本。",
            ))
        for workflow in instance.workflow_versions.select_related("template"):
            rows.append(_dependency(
                workflow,
                relationship="provider_workflow",
                relationship_label="工作流版本引用",
                disposition=BLOCKING_REFERENCE,
                reason_code="separate_workflow_review_required",
                reason="工作流版本必须单独复核其配方、烟测与运行证据，不能从 Provider 静默级联。",
            ))
        rows.extend(
            _recipe_dependency(recipe, paths)
            for recipe, paths in recipe_references(instance.id)
        )
    elif isinstance(instance, ModelProfileVersion):
        for binding in instance.prompt_profile_bindings.select_related("prompt_profile_version"):
            selectable = _can_delete_as_dependency(binding)
            rows.append(_dependency(
                binding,
                relationship="model_prompt_binding",
                relationship_label="提示词运行时绑定引用",
                disposition=SELECTABLE_DELETE if selectable else BLOCKING_REFERENCE,
                reason_code="prompt_binding_can_be_deleted" if selectable else "prompt_binding_requires_separate_review",
                reason="该运行时绑定可由人工明确勾选后删除。" if selectable else "该绑定仍启用或已进入配方、提示词产物、生产尝试或配置历史；请单独停用并复核。",
            ))
        for artifact in instance.artifacts.select_related("profile_version"):
            disposition = SELECTABLE_DELETE if _can_delete_as_dependency(artifact) else BLOCKING_REFERENCE
            rows.append(_dependency(
                artifact,
                relationship="model_artifact",
                relationship_label="模型文件声明引用",
                disposition=disposition,
                reason_code="model_artifact_can_be_deleted" if disposition == SELECTABLE_DELETE else "model_artifact_requires_separate_review",
                reason="从未核验且没有引用的文件声明可由人工勾选删除。" if disposition == SELECTABLE_DELETE else "文件声明已有核验、主机观测、工作流或配方引用，必须单独处理并保留证据。",
            ))
        for deployment in instance.deployments.select_related("profile_version"):
            rows.append(_dependency(
                deployment,
                relationship="model_deployment",
                relationship_label="模型部署引用",
                disposition=BLOCKING_REFERENCE,
                reason_code="separate_deployment_review_required",
                reason="部署可能包含运行、烟测和主机观测事实；请单独复核，不能从模型版本级联删除。",
            ))
        for workflow in instance.workflow_versions.select_related("template"):
            rows.append(_dependency(
                workflow,
                relationship="model_workflow",
                relationship_label="工作流版本引用",
                disposition=BLOCKING_REFERENCE,
                reason_code="separate_workflow_review_required",
                reason="工作流可能已冻结执行合同或证据；请单独复核。",
            ))
        rows.extend(_recipe_dependency(recipe, paths) for recipe, paths in recipe_references(instance.id))
    elif isinstance(instance, ModelDeployment):
        history_specs = (
            (instance.asset_generation_jobs.all(), "asset_generation_job", "asset_generation_job", "素材生成任务", lambda row: getattr(row, "name", "") or f"素材任务 {str(row.pk)[:8]}", lambda row: row.status),
            (instance.comfy_workflow_sources.all(), "comfy_workflow_source", "comfy_workflow_source", "ComfyUI 导入来源", lambda row: getattr(row, "source_path", "") or f"导入来源 {str(row.pk)[:8]}", lambda _row: "immutable"),
            (instance.workflow_smoke_attempts.all(), "workflow_smoke_attempt", "workflow_smoke_attempt", "工作流烟测", lambda row: f"工作流烟测 {str(row.pk)[:8]}", lambda row: row.status),
            (instance.capability_snapshots.all(), "deployment_capability_snapshot", "model_deployment_capability_snapshot", "部署能力快照", lambda row: f"{row.get_probe_kind_display()} · r{row.revision}", lambda row: row.status),
            (instance.smoke_attempts.all(), "deployment_smoke_attempt", "model_deployment_smoke_attempt", "部署真实烟测", lambda row: f"{row.operation} · {str(row.pk)[:8]}", lambda row: row.status),
            (instance.runtime_availability.select_related("snapshot"), "runtime_model_availability", "runtime_model_availability", "主机模型可用性观测", lambda row: f"{row.availability_key} · {str(row.snapshot_id)[:8]}", lambda row: row.status),
            (instance.recommendation_candidates.all(), "recommendation_candidate", "recommendation_candidate", "运行推荐候选事实", lambda row: f"{row.capability_key} · {str(row.pk)[:8]}", lambda row: row.status),
            (instance.generation_attempts.all(), "generation_attempt", "generation_attempt", "生产生成尝试", lambda row: f"{row.operation or row.unit_key} · {str(row.pk)[:8]}", lambda row: row.status),
        )
        for queryset, relationship, resource_type, relationship_label, labeler, statuser in history_specs:
            for row in queryset:
                rows.append(_history_dependency(
                    row,
                    relationship=relationship,
                    relationship_label=relationship_label,
                    resource_type=resource_type,
                    label=labeler(row),
                    status=str(statuser(row)),
                    reason_code="immutable_execution_history",
                    reason="这是不可变运行/探测事实，不能由删除部署操作级联清理。",
                ))
        for workflow in instance.validated_workflow_versions.select_related("template"):
            rows.append(_dependency(
                workflow,
                relationship="validated_workflow",
                relationship_label="已校验工作流绑定",
                disposition=BLOCKING_REFERENCE,
                reason_code="validated_workflow_reference",
                reason="工作流已将该部署冻结为验证目标；请保留并退役部署。",
            ))
        for recipe, paths in recipe_references(instance.id):
            rows.append(_recipe_dependency(recipe, paths))
            for run in recipe.production_runs.all():
                rows.append(_history_dependency(
                    run,
                    relationship="deployment_recipe_production_run",
                    relationship_label="间接关联的正式生产运行",
                    resource_type="production_run",
                    label=f"{_label(recipe)} · ProductionRun {str(run.pk)[:8]}",
                    status=run.status,
                    reason_code="immutable_production_run",
                    reason="该运行冻结了引用当前部署的配方版本，是必须保留的间接历史依赖。",
                ))
            for decision in recipe.recommendation_decisions.all():
                rows.append(_history_dependency(
                    decision,
                    relationship="deployment_recipe_recommendation_decision",
                    relationship_label="间接关联的推荐决策",
                    resource_type="recommendation_decision",
                    label=f"{_label(recipe)} · 推荐决策 {str(decision.pk)[:8]}",
                    status=decision.decision,
                    reason_code="immutable_recommendation_decision",
                    reason="该人工决策冻结了引用当前部署的配方版本，不能级联删除。",
                ))
    elif isinstance(instance, ModelArtifact):
        for availability in instance.runtime_availability.select_related("snapshot"):
            rows.append(_history_dependency(
                availability,
                relationship="artifact_runtime_availability",
                relationship_label="模型文件主机观测",
                resource_type="runtime_model_availability",
                label=f"{availability.availability_key} · {str(availability.snapshot_id)[:8]}",
                status=availability.status,
                reason_code="immutable_artifact_observation",
                reason="主机观测是不可变证据，文件声明必须保留。",
            ))
        for requirement in instance.workflow_requirements.select_related("workflow_version__template"):
            rows.append(_history_dependency(
                requirement,
                relationship="workflow_model_requirement",
                relationship_label="工作流模型要求",
                resource_type="workflow_model_requirement",
                label=f"{_label(requirement.workflow_version)} · {requirement.alias}",
                status=requirement.workflow_version.status,
                reason_code="workflow_contract_reference",
                reason="模型文件已进入工作流执行合同；请创建新工作流版本，不能删除单条冻结要求。",
            ))
        rows.extend(_recipe_dependency(recipe, paths) for recipe, paths in recipe_references(instance.id))
    elif isinstance(instance, WorkflowTemplateVersion):
        for binding in instance.semantic_bindings.all():
            rows.append(_history_dependency(
                binding,
                relationship="owned_semantic_binding",
                relationship_label="随工作流删除的语义绑定",
                resource_type="workflow_semantic_binding",
                label=f"{binding.semantic_key} · {binding.node_id}.{binding.input_name}",
                status="draft_contract",
                reason_code="owned_draft_child",
                reason="这是仅属于当前空草稿的结构项；确认删除目标时会一并删除。",
            ) | {"disposition": AUTOMATIC_DELETE})
        for requirement in instance.model_requirements.all():
            rows.append(_history_dependency(
                requirement,
                relationship="owned_model_requirement",
                relationship_label="随工作流删除的模型要求",
                resource_type="workflow_model_requirement",
                label=f"{requirement.alias} · {requirement.expected_filename}",
                status="draft_contract",
                reason_code="owned_draft_child",
                reason="这是仅属于当前空草稿的结构项；确认删除目标时会一并删除。",
            ) | {"disposition": AUTOMATIC_DELETE})
        history_specs = (
            (instance.generated_workflows.all(), "generated_workflow", "generated_workflow", "已生成工作流实例"),
            (instance.deferred_generation_requests.all(), "deferred_generation_request", "deferred_generation_request", "延迟生成请求"),
            (instance.asset_generation_jobs.all(), "asset_generation_job", "asset_generation_job", "素材生成任务"),
            (instance.comfy_sources.all(), "comfy_workflow_source", "comfy_workflow_source", "ComfyUI 导入来源"),
            (instance.smoke_attempts.all(), "workflow_smoke_attempt", "workflow_smoke_attempt", "工作流烟测"),
            (instance.recommendation_candidates.all(), "recommendation_candidate", "recommendation_candidate", "推荐候选事实"),
        )
        for queryset, relationship, resource_type, relationship_label in history_specs:
            for row in queryset:
                rows.append(_history_dependency(
                    row,
                    relationship=relationship,
                    relationship_label=relationship_label,
                    resource_type=resource_type,
                    label=f"{relationship_label} · {str(row.pk)[:8]}",
                    status=str(getattr(row, "status", "recorded")),
                    reason_code="immutable_workflow_history",
                    reason="该记录已进入执行、导入、烟测或推荐事实，不能级联删除。",
                ))
        rows.extend(_recipe_dependency(recipe, paths) for recipe, paths in recipe_references(instance.id))
    elif isinstance(instance, ProductionRecipeVersion):
        for run in instance.production_runs.all():
            rows.append(_history_dependency(
                run,
                relationship="production_run",
                relationship_label="正式生产运行",
                resource_type="production_run",
                label=f"ProductionRun · {str(run.pk)[:8]}",
                status=run.status,
                reason_code="immutable_production_run",
                reason="正式生产运行冻结了该配方版本，不能级联删除。",
            ))
        for decision in instance.recommendation_decisions.all():
            rows.append(_history_dependency(
                decision,
                relationship="recommendation_decision",
                relationship_label="人工推荐决策",
                resource_type="recommendation_decision",
                label=f"推荐决策 · {str(decision.pk)[:8]}",
                status=decision.decision,
                reason_code="immutable_recommendation_decision",
                reason="人工决策是审计事实，不能级联删除。",
            ))
    elif isinstance(instance, ConfigDefinition):
        for value in instance.values.select_related("revision"):
            rows.append(_history_dependency(
                value,
                relationship="config_revision_value",
                relationship_label="配置修订值",
                resource_type="config_value",
                label=f"{value.revision.scope_type} revision {value.revision.revision}",
                status=value.revision.status,
                reason_code="immutable_config_revision",
                reason="配置修订是历史事实；请保持定义停用，不能删除其字段定义。",
            ))
    elif isinstance(instance, PromptFieldDefinition):
        for rule in instance.profile_rules.select_related("profile_version", "field_definition"):
            selectable = _can_delete_as_dependency(rule)
            rows.append(_dependency(
                rule,
                relationship="prompt_profile_rule",
                relationship_label="PromptProfile 字段规则",
                disposition=SELECTABLE_DELETE if selectable else BLOCKING_REFERENCE,
                reason_code="draft_prompt_rule_can_be_deleted" if selectable else "frozen_prompt_rule_reference",
                reason="草稿规则可由人工勾选后一并删除。" if selectable else "字段规则已冻结或仍被配置历史引用，不能级联删除。",
            ))
    elif isinstance(instance, PromptProfileVersion):
        for rule in instance.field_rules.select_related("field_definition"):
            automatic = _can_delete_as_dependency(rule)
            rows.append(_dependency(
                rule,
                relationship="owned_prompt_rule",
                relationship_label="随 PromptProfile 删除的草稿规则",
                disposition=AUTOMATIC_DELETE if automatic else BLOCKING_REFERENCE,
                reason_code="owned_draft_child" if automatic else "owned_prompt_rule_has_external_reference",
                reason="该规则仅属于当前草稿；确认删除 Profile 时会一并删除。" if automatic else "该草稿规则仍被配置历史引用；请先单独处理该引用，不能静默级联。",
            ))
        for binding in instance.bindings.all():
            selectable = _can_delete_as_dependency(binding)
            rows.append(_dependency(
                binding,
                relationship="prompt_profile_binding",
                relationship_label="提示词运行时绑定",
                disposition=SELECTABLE_DELETE if selectable else BLOCKING_REFERENCE,
                reason_code="prompt_binding_can_be_deleted" if selectable else "prompt_binding_requires_separate_review",
                reason="该绑定可由人工明确勾选后删除；不会静默改写运行路由。" if selectable else "该绑定仍启用或已冻结到生产/配置历史；请先停用并单独复核。",
            ))
        history_specs = (
            (instance.prompt_artifacts.all(), "prompt_artifact", "prompt_artifact", "已生成提示词产物"),
            (instance.generation_attempts.all(), "generation_attempt", "generation_attempt", "生产生成尝试"),
        )
        for queryset, relationship, resource_type, relationship_label in history_specs:
            for row in queryset:
                rows.append(_history_dependency(
                    row,
                    relationship=relationship,
                    relationship_label=relationship_label,
                    resource_type=resource_type,
                    label=f"{relationship_label} · {str(row.pk)[:8]}",
                    status=str(getattr(row, "status", "recorded")),
                    reason_code="immutable_prompt_history",
                    reason="该记录冻结了提示词策略合同，不能级联删除。",
                ))
    elif isinstance(instance, PromptProfileBinding):
        for artifact in PromptArtifact.objects.filter(prompt_profile_binding_key=instance.key):
            rows.append(_history_dependency(
                artifact,
                relationship="prompt_binding_prompt_artifact",
                relationship_label="已生成提示词产物冻结绑定 key",
                resource_type="prompt_artifact",
                label=f"PromptArtifact · {str(artifact.pk)[:8]}",
                status="recorded",
                reason_code="immutable_prompt_history",
                reason="提示词产物已冻结该绑定 key；请停用绑定并保留记录。",
            ))
        for attempt in GenerationAttempt.objects.filter(prompt_profile_binding_key=instance.key):
            rows.append(_history_dependency(
                attempt,
                relationship="prompt_binding_generation_attempt",
                relationship_label="生产生成尝试冻结绑定 key",
                resource_type="generation_attempt",
                label=f"{attempt.operation or attempt.unit_key} · {str(attempt.pk)[:8]}",
                status=attempt.status,
                reason_code="immutable_prompt_history",
                reason="生产尝试已冻结该绑定 key；请停用绑定并保留记录。",
            ))
        for recipe, paths in recipe_references_any(instance.id, instance.key):
            dependency = _recipe_dependency(recipe, paths)
            dependency["relationship"] = "prompt_binding_recipe_reference"
            dependency["relationship_label"] = "生产配方冻结绑定 key"
            dependency["dependency_key"] = _dependency_key(
                dependency["relationship"], dependency["resource_type"], dependency["object_id"],
            )
            rows.append(dependency)
        # ProductionRun freezes the selected binding before PromptArtifact or
        # GenerationAttempt necessarily exists.  The immutable run input is
        # the authoritative snapshot; scanning StageRun routing as well would
        # duplicate the same historical dependency.
        for run in ProductionRun.objects.all():
            paths = sorted({
                *_reference_paths(run.input_snapshot, str(instance.id)),
                *_reference_paths(run.input_snapshot, instance.key),
            })
            if not paths:
                continue
            dependency = _history_dependency(
                run,
                relationship="prompt_binding_production_run",
                relationship_label="正式生产运行冻结绑定",
                resource_type="production_run",
                label=f"ProductionRun · {str(run.pk)[:8]}",
                status=run.status,
                reason_code="immutable_production_run",
                reason="正式生产运行输入快照已冻结该 Binding ID/key，必须保留绑定记录。",
            )
            dependency["reference_paths"] = paths
            rows.append(dependency)

        # A prompt.normalize deployment smoke freezes the representative
        # PromptProfile binding before any provider submission.  The durable
        # request snapshot is an immutable execution fact and can outlive both
        # the active route and its eventual capability snapshot, so it must be
        # surfaced independently in the deletion review.
        for attempt in ModelDeploymentSmokeAttempt.objects.all():
            paths = sorted({
                *_reference_paths(attempt.request_snapshot, str(instance.id)),
                *_reference_paths(attempt.request_snapshot, instance.key),
            })
            if not paths:
                continue
            dependency = _history_dependency(
                attempt,
                relationship="prompt_binding_deployment_smoke_attempt",
                relationship_label="部署烟测冻结绑定",
                resource_type="model_deployment_smoke_attempt",
                label=f"{attempt.operation} · {str(attempt.pk)[:8]}",
                status=attempt.status,
                reason_code="immutable_deployment_smoke_request",
                reason="部署烟测请求快照已冻结该 Binding ID/key，必须保留绑定记录。",
            )
            dependency["reference_paths"] = paths
            rows.append(dependency)

    # Config resource-reference values are versioned JSON facts rather than
    # foreign keys. They must participate in every registry deletion impact.
    for value in ConfigValue.objects.select_related("revision", "definition"):
        paths = _reference_paths(value.value, str(instance.pk))
        if not paths:
            continue
        dependency = _history_dependency(
            value,
            relationship="config_resource_reference",
            relationship_label="配置修订资源引用",
            resource_type="config_value",
            label=f"{value.definition.key} · {value.revision.scope_type} revision {value.revision.revision}",
            status=value.revision.status,
            reason_code="immutable_config_revision",
            reason="配置修订冻结了该资源 ID；请发布替代配置，历史修订不能级联删除。",
        )
        dependency["reference_paths"] = paths
        rows.append(dependency)
    return sorted(rows, key=lambda row: (row["disposition"], row["resource_type"], row["label"], row["object_id"]))


def _get_target(resource_type: str, object_id, *, lock: bool = False) -> Model:
    model = RESOURCE_MODELS.get(resource_type)
    if model is None:
        raise ValidationError({"resource_type": "不支持该删除资源类型"})
    queryset = model._default_manager.all()
    if lock:
        queryset = queryset.select_for_update()
    try:
        return queryset.get(pk=object_id)
    except (ObjectDoesNotExist, ValueError) as exc:
        raise RegistryDeletionTargetNotFound("删除目标不存在或 ID 无效") from exc


def build_registry_deletion_impact(resource_type: str, object_id, *, actor=None, lock: bool = False) -> dict[str, Any]:
    target = _get_target(resource_type, object_id, lock=lock)
    policy = _target_policy(target, actor)
    dependencies = _dependencies(target)
    blockers = [row for row in dependencies if row["disposition"] in {BLOCKING_REFERENCE, PRESERVED_HISTORY}]
    selectable = [row for row in dependencies if row["disposition"] == SELECTABLE_DELETE]
    automatic = [row for row in dependencies if row["disposition"] == AUTOMATIC_DELETE]
    label = _label(target)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "requested_action": "delete",
        "target": {
            "resource_type": resource_type,
            "object_id": str(target.pk),
            "label": label,
            "status": _status(target),
            "state_sha256": _state_sha256(target),
        },
        "target_policy": {
            "allowed": policy.allowed,
            "reason_code": policy.code,
            "reason": policy.reason,
        },
        "complete": True,
        "can_execute": policy.allowed and not blockers,
        "dependencies": dependencies,
        "summary": {
            "selectable_count": len(selectable),
            "automatic_count": len(automatic),
            "blocking_count": len([row for row in dependencies if row["disposition"] == BLOCKING_REFERENCE]),
            "preserved_history_count": len([row for row in dependencies if row["disposition"] == PRESERVED_HISTORY]),
        },
        "confirmation_phrase": f"删除 {label}",
        "advisories": [value for value in (
            policy.advisory,
            "所有可选依赖默认不选；只有逐项人工确认后才会删除。" if selectable else "",
            "运行、烟测、快照、主机观测、已发布/已退役版本和审计事实永不级联删除。" if blockers else "",
        ) if value],
    }
    payload["impact_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def _delete_with_audit(instance: Model, *, actor, reason_code: str) -> None:
    before = lifecycle_summary(instance)
    record_platform_lifecycle_event(
        instance,
        actor=actor,
        action="delete",
        reason_code=reason_code,
        before_summary=before,
    )
    instance.delete()


def _lock_parent_boundary(resource_type: str, object_id) -> None:
    if resource_type == "model_artifact":
        profile_id = ModelArtifact.objects.filter(pk=object_id).values_list(
            "profile_version_id", flat=True,
        ).first()
        if profile_id:
            ModelProfileVersion.objects.select_for_update().get(pk=profile_id)
    elif resource_type == "prompt_profile_field_rule":
        profile_id = PromptProfileFieldRule.objects.filter(pk=object_id).values_list(
            "profile_version_id", flat=True,
        ).first()
        if profile_id:
            PromptProfileVersion.objects.select_for_update().get(pk=profile_id)


def _lock_impact_dependencies(impact: dict[str, Any]) -> None:
    """Lock every mutable registry dependency before the final impact build.

    The first impact is only a lock-discovery pass.  A dependency can change
    lifecycle state while that pass is running, so execution must lock both
    the dependency and any parent whose lifecycle controls it, then rebuild
    the complete graph.  Missing rows are deliberately tolerated here: the
    second build changes the impact hash and fails the request atomically.
    """

    rows = sorted(
        impact["dependencies"],
        key=lambda row: (row["resource_type"], row["object_id"]),
    )
    for row in rows:
        resource_type = row["resource_type"]
        object_id = row["object_id"]
        if resource_type in RESOURCE_MODELS:
            try:
                _lock_parent_boundary(resource_type, object_id)
                _get_target(resource_type, object_id, lock=True)
            except (ObjectDoesNotExist, RegistryDeletionTargetNotFound):
                continue
            continue
        model = AUTOMATIC_DEPENDENCY_MODELS.get(resource_type)
        if model is not None:
            model._default_manager.select_for_update().filter(pk=object_id).first()


def execute_registry_deletion(
    resource_type: str,
    object_id,
    *,
    actor,
    expected_impact_sha256: str,
    selected_dependency_keys: Iterable[str],
    confirmation_phrase: str,
    reason_code: str,
) -> dict[str, Any]:
    normalized_reason = normalize_reason_code(reason_code, default="lifecycle_cleanup")
    selected = set(selected_dependency_keys)
    try:
        with registry_reference_write_guard():
            # Recipe JSON references do not have foreign keys. Locking the current
            # recipe set before the second impact calculation prevents concurrent
            # edits from being mistaken for the previewed graph.
            list(ProductionRecipeVersion.objects.select_for_update().values_list("id", flat=True))
            _lock_parent_boundary(resource_type, object_id)
            preliminary_impact = build_registry_deletion_impact(
                resource_type,
                object_id,
                actor=actor,
                lock=True,
            )
            if preliminary_impact["impact_sha256"] != expected_impact_sha256:
                raise RegistryDeletionConflict(
                    "deletion_impact_changed",
                    "关联引用已变化，未删除任何记录；请重新读取影响清单。",
                    impact=preliminary_impact,
                )
            if confirmation_phrase != preliminary_impact["confirmation_phrase"]:
                raise RegistryDeletionConflict(
                    "deletion_confirmation_mismatch",
                    "二次确认文本与当前目标不一致，未执行删除。",
                    impact=preliminary_impact,
                )
            if not preliminary_impact["can_execute"]:
                raise RegistryDeletionConflict(
                    "deletion_blocked",
                    "目标存在不可级联删除的引用或历史事实；请按清单处理后重新预览。",
                    impact=preliminary_impact,
                )

            # Lock the mutable closure discovered above, then rebuild.  This
            # second pass is the authoritative plan used for selection checks
            # and deletion; it closes the window where an inactive binding can
            # be enabled or a draft rule's parent can be published after the
            # preview has already been accepted.
            _lock_impact_dependencies(preliminary_impact)
            impact = build_registry_deletion_impact(
                resource_type,
                object_id,
                actor=actor,
                lock=True,
            )
            if impact["impact_sha256"] != expected_impact_sha256:
                raise RegistryDeletionConflict(
                    "deletion_impact_changed",
                    "关联引用在执行锁定期间发生变化，未删除任何记录；请重新读取影响清单。",
                    impact=impact,
                )
            selectable = {
                row["dependency_key"]: row
                for row in impact["dependencies"]
                if row["disposition"] == SELECTABLE_DELETE
            }
            unknown = selected - set(selectable)
            missing = set(selectable) - selected
            if unknown:
                raise RegistryDeletionConflict(
                    "invalid_dependency_selection",
                    "所选依赖不属于当前可删除清单，未执行删除。",
                    impact=impact,
                )
            if missing:
                raise RegistryDeletionConflict(
                    "dependency_confirmation_required",
                    "仍有可删除依赖未获人工确认；取消操作或逐项勾选全部依赖。",
                    impact=impact,
                )

            deleted_dependencies: list[dict[str, str]] = []
            prompt_profiles_to_refresh: set[str] = set()
            for key in sorted(selected):
                row = selectable[key]
                _lock_parent_boundary(row["resource_type"], row["object_id"])
                dependency = _get_target(row["resource_type"], row["object_id"], lock=True)
                if isinstance(dependency, PromptProfileFieldRule):
                    prompt_profiles_to_refresh.add(str(dependency.profile_version_id))
                _delete_with_audit(dependency, actor=actor, reason_code=normalized_reason)
                deleted_dependencies.append({
                    "dependency_key": key,
                    "resource_type": row["resource_type"],
                    "object_id": row["object_id"],
                    "disposition": SELECTABLE_DELETE,
                })

            automatic_rows = sorted(
                (
                    row for row in impact["dependencies"]
                    if row["disposition"] == AUTOMATIC_DELETE
                ),
                key=lambda row: row["dependency_key"],
            )
            for row in automatic_rows:
                model = AUTOMATIC_DEPENDENCY_MODELS.get(row["resource_type"])
                if model is None:
                    raise RegistryDeletionConflict(
                        "unsupported_automatic_dependency",
                        "发现未登记的自动级联类型，已失败关闭；未执行删除。",
                        impact=impact,
                    )
                dependency = model.objects.select_for_update().get(pk=row["object_id"])
                if isinstance(dependency, PromptProfileFieldRule):
                    prompt_profiles_to_refresh.add(str(dependency.profile_version_id))
                    _delete_with_audit(dependency, actor=actor, reason_code=normalized_reason)
                else:
                    dependency.delete()
                deleted_dependencies.append({
                    "dependency_key": row["dependency_key"],
                    "resource_type": row["resource_type"],
                    "object_id": row["object_id"],
                    "disposition": AUTOMATIC_DELETE,
                })

            target = _get_target(resource_type, object_id, lock=True)
            if isinstance(target, PromptProfileFieldRule):
                prompt_profiles_to_refresh.add(str(target.profile_version_id))
            _delete_with_audit(target, actor=actor, reason_code=normalized_reason)
            for profile_id in sorted(prompt_profiles_to_refresh):
                profile = PromptProfileVersion.objects.filter(pk=profile_id).first()
                if profile is not None:
                    refresh_prompt_profile_contract(
                        profile,
                        schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
                    )
    except RegistryDeletionConflict:
        raise
    except RegistryDeletionTargetNotFound as exc:
        raise RegistryDeletionConflict(
            "deletion_target_missing",
            "删除目标已不存在；未执行任何依赖操作，请刷新列表。",
        ) from exc
    except ProtectedError as exc:
        raise RegistryDeletionConflict(
            "deletion_impact_changed",
            "发现未包含在预览中的受保护引用，事务已回滚；请重新读取影响清单。",
        ) from exc

    return {
        "deleted": True,
        "target": impact["target"],
        "deleted_dependencies": deleted_dependencies,
    }
