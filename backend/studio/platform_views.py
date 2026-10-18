from __future__ import annotations

from copy import deepcopy
from studio.services.prompt_source_catalog import prompt_source_options
from studio.execution_resource_smoke_views import ResourceSmokeMixin
from studio.task_distribution_views import TaskDistributionRecoveryMixin
from studio.script_revision_structure_views import ScriptRevisionStructureActions
from studio.production_material_fulfillment_views import ProductionMaterialFulfillmentActions
from studio.platform_serializers import ProductionRetryPreviewQuerySerializer, ProductionRetryPreviewSerializer
import json
import uuid
from urllib.parse import urlencode

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.db.models import (
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    Window,
)
from django.db.models.functions import Coalesce, RowNumber
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    PolymorphicProxySerializer,
    extend_schema,
)
from rest_framework import mixins, status, viewsets
from studio.production_manual_serializers import (
    RecipeBibleReviewSerializer,
    ProductionManualActionInputSerializer, ProductionManualActionResultSerializer,
    ProductionManualActionOptionsSerializer, ManualTargetPageSerializer, ManualMediaReviewSerializer, ManualBibleReviewSerializer, ManualBibleImageReviewSerializer,
    ManualStoryboardDetailSerializer, ManualStoryboardReviewSerializer, ManualAssemblyDetailSerializer, ManualAssemblyPreviewSerializer, ManualDeliveryDetailSerializer,
    ManualChangeImpactQuerySerializer, ManualChangeImpactSerializer, ManualScriptStructureCheckSerializer,
    ReviewedVisualSourceQuerySerializer, ReviewedVisualSourcesSerializer,
    ManualShotReferenceQuerySerializer, ManualShotReferenceDetailSerializer, RecipeShotReferenceDetailSerializer,
)
from studio.services.production_execution_units import PRODUCTION_SCOPE_KINDS
from studio.production_recipe_mapping_serializers import (
    RecipeMappingPreviewInputSerializer, RecipeMappingPreviewSerializer,
    StageInputAmendmentInputSerializer, StageInputAmendmentSerializer,
)
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.reverse import reverse

from studio.models import (
    Asset,
    ComfyWorkflowSource,
    ComfyWorkflowContractDraft,
    ConfigDefinition,
    ConfigRevision,
    GeneratedArtifact,
    GenerationAttempt,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    FieldDefinition,
    FieldDefinitionVersion,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    ProviderProfile,
    RecommendationCandidate,
    RecommendationSnapshot,
    RuntimeHost,
    RuntimeHostProbeAttempt,
    RuntimeModelAvailability,
    ScriptRevision,
    ScriptSource,
    SecretAuditEvent,
    SecretBinding,
    SeriesProject,
    Shot,
    WorkflowTemplateVersion,
    WorkflowModelRequirement,
    WorkflowSemanticBinding,
    WorkflowSmokeAttempt,
    WorkflowProjector,
    WorkflowProjectorVersion,
)
from studio.pagination import AcceptanceSmokePagination, StandardPageNumberPagination
from studio.permissions import IsStaffOrReadOnly, IsSuperUser
from studio.platform_serializers import (
    ApprovalDecisionInputSerializer,
    AcceptanceSmokePageSerializer,
    AcceptanceSmokeQuerySerializer,
    AuthorizeStageCostInputSerializer,
    BibleImageRegenerationInputSerializer,
    ConfigDefinitionSerializer,
    ConfigDefinitionCreateInputSerializer,
    ConfigDefinitionOptionsSerializer,
    CONFIG_DEFAULT_SOURCE_CHOICES,
    ConfigResourceOptionSerializer,
    ConfigurationErrorResponseSerializer,
    ConfigRevisionCreateSerializer,
    ConfigRevisionSerializer,
    ComfySavedWorkflowBatchInputSerializer,
    ComfySavedWorkflowBatchResponseSerializer,
    ComfySavedWorkflowDiscoveryResponseSerializer,
    ComfyDraftArtifactAttestationInputSerializer,
    ComfySavedWorkflowPreviewInputSerializer,
    ComfySavedWorkflowPreviewResponseSerializer,
    ComfyWorkflowSourceSerializer,
    ComfyWorkflowContractDraftSerializer,
    DeploymentActionSerializer,
    DeploymentAcceptanceRowSerializer,
    GeneratedArtifactDetailSerializer,
    GeneratedArtifactListSerializer,
    GenerationAttemptSerializer,
    HostCapabilitySnapshotSerializer,
    ModelArtifactSerializer,
    ModelArtifactVerificationInputSerializer,
    ModelDeploymentSerializer,
    ModelDeploymentCapabilitySnapshotSerializer,
    ModelDeploymentSmokeAttemptSerializer,
    ModelProfileNextVersionInputSerializer,
    ModelProfileRegistryOptionsSerializer,
    ModelProfileVersionSerializer,
    NodeMetadataQuerySerializer,
    NodeMetadataResponseSerializer,
    ProductionRunConflictResponseSerializer,
    PromptNormalizationSmokeResolutionSerializer,
    ProductionReadinessResponseSerializer,
    ProductionRecipeCloneSuccessorInputSerializer,
    ProductionRecipeEditorOptionsSerializer,
    ProductionRecipeSourceOptionsInputSerializer,
    ProductionRecipeSourceOptionsSerializer,
    ProductionRecipeVersionSerializer,
    ProductionRunCreateSerializer,
    ProductionManualOptionsSerializer,
    ProductionBudgetOptionsSerializer,
    ProductionRunDetailSerializer,
    ProductionRunRetryInputSerializer,
    ProductionRunListSerializer,
    ProjectConfigResponseSerializer,
    ProjectConfigWriteSerializer,
    ProjectEffectiveConfigurationResponseSerializer,
    ProviderProfileSerializer,
    PlatformLifecycleAuditEventSerializer,
    FieldDefinitionSerializer,
    FieldDefinitionCreateInputSerializer,
    FieldDefinitionOptionsSerializer,
    PromptProfileFieldRuleSerializer,
    PromptProfileOptionsSerializer,
    PromptProfileCloneSuccessorInputSerializer,
    PromptProfilePreviewInputSerializer,
    PromptProfilePreviewSerializer,
    PromptProfileVersionSerializer,
    PromptProfileVersionCreateInputSerializer,
    RecommendationDecisionInputSerializer,
    RecommendationDecisionSerializer,
    RecommendationSnapshotSerializer,
    RegistryDeletionExecuteInputSerializer,
    RegistryDeletionExecuteResponseSerializer,
    RegistryDeletionImpactSerializer,
    RegistryDeletionPreviewInputSerializer,
    RuntimeHostProbeAttemptSerializer,
    RuntimeHostSerializer,
    RuntimeHostStatusSerializer,
    RuntimeRecommendationInputSerializer,
    ResolveUncertainAttemptInputSerializer,
    ResolvePromptNormalizationSmokeInputSerializer,
    ResolvedConfigurationResponseSerializer,
    RuntimeReadinessResponseSerializer,
    ScriptRevisionCreateInputSerializer,
    ScriptRevisionDetailSerializer,
    ScriptRevisionListSerializer,
    ScriptSourceSerializer,
    ScriptSourceCreateInputSerializer,
    ScriptSourceCreateResponseSerializer,
    SecretAuditEventSerializer,
    SecretBindingCreateSerializer,
    SecretBindingSerializer,
    SecretRotateSerializer,
    SecretStatusSerializer,
    SelectArtifactInputSerializer,
    RecipeMaterialSelectionContextSerializer,
    SelectRouteInputSerializer,
    WorkflowTemplateVersionDetailSerializer,
    WorkflowTemplateCanvasExportSerializer,
    WorkflowTemplateCanvasExportQuerySerializer,
    WorkflowTemplateVersionListSerializer,
    WorkflowSmokeTestResponseSerializer,
    WorkflowSmokeAttemptSerializer,
    WorkflowSmokeResolutionInputSerializer,
    WorkflowSmokeResolutionSerializer,
    WorkflowProjectorSerializer,
    WorkflowProjectorVersionCloneSerializer,
    WorkflowProjectorVersionSerializer,
    WorkflowProjectorOptionsSerializer,
    WorkflowProjectorRecommendationInputSerializer,
    WorkflowProjectorRecommendationSerializer,
    WorkflowValidationResponseSerializer,
    WorkflowAcceptanceRowSerializer,
    config_definition_system_defaults,
    field_system_defaults,
    prompt_rule_system_defaults,
)
from studio.services.acceptance_smoke import (
    deployment_acceptance_row,
    workflow_acceptance_row,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    activate_config_revision,
    config_resource_reference_options,
    config_resource_reference_candidates,
    create_config_revision,
    publish_config_revision,
    redact_configuration_snapshot,
    resolve_configuration,
    validate_version_configuration_defaults,
)
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.comfy_workflow_preview_receipts import commit_preview_receipt, find_preview_receipt
from studio.services.security_boundaries import (
    boundary_int,
    boundary_values,
    security_boundary_resolution_cache,
)
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.model_profile_options import (
    model_capability_values,
    model_family_value,
    model_profile_registry_options,
)
from studio.services.media_capabilities import (
    validate_model_profile_media_capabilities,
)
from studio.exceptions import api_error_response
from studio.services.platform_lifecycle_audit import (
    lifecycle_summary,
    record_platform_lifecycle_event,
    request_reason_code,
)
from studio.services.registry_deletion import (
    RegistryDeletionConflict,
    RegistryDeletionTargetNotFound,
    build_registry_deletion_impact,
    execute_registry_deletion,
    validate_recipe_registry_references,
)
from studio.services.registry_reference_lock import registry_reference_write_guard
from studio.platform_serializers import RecipeStoryboardReferenceSelectionSerializer
from studio.services.production_runs import (
    ProductionRunConflict,
    ProductionRunCreateRejected,
    ProductionRunRetryConflict,
    _recipe_stages,
    advance_production_run,
    authorize_stage_cost,
    cancel_production_run,
    create_production_run,
    decide_stage,
    production_recipe_editor_options,
    regenerate_bible_image_candidates,
    retry_production_run,
    resolve_uncertain_attempt,
    select_stage_route,
    validate_production_recipe_executability,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    driver_for,
    validate_deployment_execution_contract,
)
from studio.services.prompt_profiles import (
    prompt_language_options,
    DECLARATIVE_SOURCE_PROJECTION_KEY,
    DIRECT_SOURCE_PROJECTION_KEY,
    KNOWN_MODEL_FAMILIES,
    KNOWN_PROMPT_OPERATIONS,
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    SUPPORTED_FIELD_SOURCE_SELECTORS,
    clone_prompt_profile_successor,
    model_family_for_profile,
    publish_prompt_profile,
    refresh_prompt_profile_contract,
    retire_prompt_profile,
)
from studio.services.prompt_generation_sources import (
    prompt_generation_source_options,
)
from studio.services.prompt_profile_preview import preview_prompt_profile
from studio.services.reference_context import rendering_options
from studio.services.reference_rendering_inputs import UNIT_RENDER_SLOTS
from studio.services.source_projection_primitives import (
    source_projection_primitive_options,
)
from studio.services.async_media_contract import (
    deployment_target_sha256,
    validate_async_model_profile,
)
from studio.services.deployment_smoke import (
    probe_async_deployment,
)
from studio.services.script_structure import (
    validate_script_structure_model_profile,
)
from studio.services.production_visual_rules import validate_visual_rules_model_profile
from studio.services.deployment_smoke_queue import (
    request_deployment_smoke_cancel,
)
from studio.services.deployment_smoke_resolution import (
    resolve_deployment_smoke_attempt,
    unresolved_deployment_smoke_attempt,
)
from studio.services.script_generation_contract import (
    validate_script_generation_model_profile,
)
from studio.services.prompt_normalization import (
    PROMPT_NORMALIZE_OPERATION,
    validate_prompt_normalization_model_profile,
)
from studio.services.runtime_platform import (
    RecommendationStale,
    capability_readiness,
    decide_recommendation,
    recommend_for_host,
    validate_draft_profile_artifacts_for_publish,
    verified_artifact_availability,
)
from studio.services.runtime_host_probe_queue import (
    enqueue_runtime_host_probe,
    request_runtime_host_probe_cancel,
)
from studio.services.draft_model_artifact_attestation import (
    DraftArtifactAttestationConflict,
    enqueue_draft_model_artifact_attestation,
)
from studio.services.script_sources import (
    ScriptSourceValidationError,
    confirm_script_revision,
    create_pasted_script_source,
    create_script_source_from_asset,
    create_script_revision,
    create_uploaded_script_source,
)
from studio.services.readiness import collect_production_readiness
from studio.services.workflow_versions import (
    WorkflowModelEvidenceUnavailable,
    node_metadata,
    publish_workflow_version,
    resolve_workflow_canvas_export_deployment,
    validate_workflow_version,
)
from studio.services.comfyui_canvas import (
    ComfyUICanvasExportError,
    api_graph_to_ui_workflow,
    is_comfyui_ui_workflow,
)
from studio.services.comfyui_workflow_imports import (
    SavedWorkflowImportError,
    SavedWorkflowRequestBudgetExceeded,
    _safe_projected_error,
    batch_import_saved_workflows,
    discover_saved_workflows,
    preview_saved_workflow,
    recommend_workflow_projector_from_candidate,
)
from studio.services.comfyui_workflow_library import (
    saved_workflow_directory,
    saved_workflow_directory_resolution_cache,
)
from studio.services.workflow_projectors import (
    WORKFLOW_PROJECTOR_DEFINITION_SCHEMA_VERSION,
    validate_workflow_projector_definition,
    validate_workflow_projector_publish_conflicts,
    workflow_projector_configuration_options,
    workflow_projector_engine_options,
)
from studio.services.workflow_smoke_queue import (
    request_workflow_smoke_cancel,
)
from studio.services.workflow_smoke_resolution import (
    WorkflowSmokeResolutionConflict,
    resolve_workflow_smoke_attempt,
)
from studio.services.secret_store import (
    SecretStoreError,
    create_secret_binding,
    rotate_secret_binding,
    set_secret_binding_active,
)


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _saved_workflow_effective_limits() -> dict[str, object]:
    limits = boundary_values(
        "security.comfy.import.scan_max_items",
        "security.comfy.import.scan_max_seconds",
        "security.comfy.import.request_max_bytes",
        "security.comfy.import.batch_max_items",
        "security.comfy.import.batch_max_seconds",
    )
    return {
        "scan_max_items": int(limits["security.comfy.import.scan_max_items"]),
        "scan_max_seconds": float(limits["security.comfy.import.scan_max_seconds"]),
        "request_max_bytes": int(limits["security.comfy.import.request_max_bytes"]),
        "batch_max_items": int(limits["security.comfy.import.batch_max_items"]),
        "batch_max_seconds": float(limits["security.comfy.import.batch_max_seconds"]),
        "saved_workflow_directory": saved_workflow_directory(),
    }


def _drf_validation(exc: DjangoValidationError) -> ValidationError:
    detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", None) or str(exc)
    code = str(getattr(exc, "code", None) or "invalid")
    error = ValidationError(detail, code=code)
    error.default_code = code
    return error


def _workflow_model_evidence_recovery(
    exc: WorkflowModelEvidenceUnavailable,
    *,
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
) -> dict[str, object]:
    """Describe the current safe recovery path without exposing file paths or hashes."""

    artifact = ModelArtifact.objects.filter(pk=exc.artifact_id).first()
    snapshot = None
    evidence_ready = False
    if deployment.runtime_host_id:
        snapshot = HostCapabilitySnapshot.objects.filter(
            host_id=deployment.runtime_host_id,
        ).order_by("-captured_at", "-revision").first()
    required_rows = list(
        version.model_requirements.select_related("artifact").filter(is_required=True),
    )
    evidence_artifacts = [
        row.artifact for row in required_rows if row.artifact_id and row.artifact is not None
    ] or ([artifact] if artifact is not None else [])
    if (
        snapshot is not None
        and evidence_artifacts
        and (not required_rows or len(evidence_artifacts) == len(required_rows))
    ):
        evidence_ready = True
        for required_artifact in evidence_artifacts:
            if (
                required_artifact.status != ModelArtifact.Status.AVAILABLE
                or not required_artifact.sha256
                or required_artifact.size_bytes is None
            ):
                evidence_ready = False
                break
            try:
                verified_artifact_availability(
                    snapshot=snapshot,
                    deployment=deployment,
                    artifact=required_artifact,
                )
            except DjangoValidationError:
                evidence_ready = False
                break

    active_probe = None
    if deployment.runtime_host_id:
        active_probe = RuntimeHostProbeAttempt.objects.filter(
            host_id=deployment.runtime_host_id,
            probe_kind=RuntimeHostProbeAttempt.ProbeKind.CAPABILITY,
            status__in=(
                RuntimeHostProbeAttempt.Status.QUEUED,
                RuntimeHostProbeAttempt.Status.RUNNING,
                RuntimeHostProbeAttempt.Status.CANCEL_REQUESTED,
            ),
        ).order_by("-created_at").first()

    retry_action = {
        "kind": "retry_live_validate",
        "label": "重新校验",
    }
    runtime_action = {
        "kind": "open_settings",
        "section": "runtime",
        "label": "前往运行主机",
    }
    model_action = {
        "kind": "open_settings",
        "section": "models",
        "label": "检查模型文件",
    }
    if evidence_ready:
        state = "ready_to_retry"
        title = "主机证明已刷新，可以重新校验"
        summary = (
            "系统已找到当前有效的主机证明，文件大小和 SHA-256 与模型声明一致；"
            "工作流模板和模型文件都不需要修改。"
        )
        steps = [
            "保持当前验收部署不变。",
            "点击“重新校验”，生成本次 Live Validate 的新证据。",
        ]
        actions = [retry_action]
    elif active_probe is not None:
        state = "host_probe_running"
        title = "主机证明正在刷新"
        summary = (
            "验收主机已有探测任务正在执行；本次失败通常是校验早于新证明完成，"
            "不是工作流节点或参数错误。"
        )
        steps = [
            "前往“系统设置 → 运行主机”查看当前探测任务。",
            "等待任务状态变为“成功”，不要重复创建探测。",
            "返回本页，点击“重新校验”。",
        ]
        actions = [runtime_action, retry_action]
    elif exc.reason_code in {
        "host_snapshot_stale",
        "host_snapshot_not_latest",
        "host_probe_failed",
        "artifact_host_mismatch",
    }:
        state = "host_probe_required"
        title = "需要刷新验收主机证明"
        summary = (
            "当前验收部署没有可用于本次校验的新鲜主机证明；"
            "这不是工作流参数或参考素材填写错误。"
        )
        steps = [
            "前往“系统设置 → 运行主机”，选择当前验收部署绑定的主机并执行探测。",
            "等待探测成功，确认模型文件观测状态为 available。",
            "返回本页，点击“重新校验”。",
        ]
        actions = [runtime_action, retry_action]
    else:
        state = "model_evidence_review_required"
        title = "模型文件证明与声明不一致"
        summary = (
            "最新主机证明仍未提供与模型声明一致的文件大小和 SHA-256；"
            "应检查主机文件和模型声明，不要修改工作流参数。"
        )
        steps = [
            "先在“系统设置 → 运行主机”重新探测，排除旧快照。",
            "若新探测仍不匹配，前往“系统设置 → 模型 → 文件与证明”核对该文件；已发布模型声明有误时创建下一版本。",
            "证明一致后返回本页，点击“重新校验”。",
        ]
        actions = [runtime_action, model_action, retry_action]

    return {
        "detail": summary,
        "reason_code": exc.reason_code,
        "requirement_alias": exc.requirement_alias,
        "artifact_id": exc.artifact_id,
        "workflow_version_id": str(version.id),
        "deployment_id": str(deployment.id),
        "runtime_host_id": str(deployment.runtime_host_id or ""),
        "recovery": {
            "schema_version": 1,
            "state": state,
            "title": title,
            "summary": summary,
            "steps": steps,
            "actions": actions,
        },
    }


class ProductionReadinessView(APIView):
    permission_classes = (IsSuperUser,)

    @extend_schema(responses=ProductionReadinessResponseSerializer)
    def get(self, request):
        return Response(collect_production_readiness())


class AcceptanceSmokeView(APIView):
    """Read-only, staff-only projection of acceptance and smoke evidence."""

    permission_classes = (IsAdminUser,)
    related_workflow_batch_size = 200

    @staticmethod
    def _attempt_ids_for_workflows(versions):
        ids = {
            version.latest_acceptance_attempt_id
            for version in versions
            if getattr(version, "latest_acceptance_attempt_id", None)
        }
        ids.update({
            version.latest_validated_acceptance_attempt_id
            for version in versions
            if getattr(
                version,
                "latest_validated_acceptance_attempt_id",
                None,
            )
        })
        for version in versions:
            evidence = version.smoke_test_evidence
            value = evidence.get("smoke_attempt_id") if isinstance(evidence, dict) else None
            try:
                ids.add(uuid.UUID(str(value)))
            except (TypeError, ValueError, AttributeError):
                continue
        return ids

    @staticmethod
    def _workflow_attempt_map(versions):
        attempt_ids = AcceptanceSmokeView._attempt_ids_for_workflows(versions)
        return {
            attempt.id: attempt
            for attempt in WorkflowSmokeAttempt.objects.filter(
                id__in=attempt_ids,
                scope="resource",
                execution_instance_version__isnull=True,
            ).select_related(
                "deployment__profile_version__provider",
            ).prefetch_related("resolutions")
        }

    @staticmethod
    def _workflow_queryset(*, selected_deployment=None):
        latest_attempt = WorkflowSmokeAttempt.objects.filter(
            workflow_version_id=OuterRef("pk"),
            scope="resource",
            execution_instance_version__isnull=True,
        )
        if selected_deployment is not None:
            latest_attempt = latest_attempt.filter(
                deployment_id=selected_deployment.id,
            )
        queryset = WorkflowTemplateVersion.objects.select_related(
            "template",
            "provider",
            "model_profile_version__provider",
            "validated_deployment__profile_version__provider",
            "validated_deployment__profile_version__provider__credential__active_version",
            "validated_deployment__runtime_host",
            "validated_deployment__credential__active_version",
        ).prefetch_related(
            Prefetch(
                "semantic_bindings",
                queryset=WorkflowSemanticBinding.objects.order_by(
                    "semantic_key", "node_id", "input_name", "id",
                ),
            ),
            Prefetch(
                "model_requirements",
                queryset=WorkflowModelRequirement.objects.select_related(
                    "artifact",
                ).order_by("role", "alias", "id"),
            ),
        ).annotate(
            latest_acceptance_attempt_id=Subquery(
                latest_attempt.order_by("-created_at", "-id").values("id")[:1],
            ),
            latest_validated_acceptance_attempt_id=Subquery(
                WorkflowSmokeAttempt.objects.filter(
                    workflow_version_id=OuterRef("pk"),
                    deployment_id=OuterRef("validated_deployment_id"),
                    scope="resource",
                    execution_instance_version__isnull=True,
                ).order_by("-created_at", "-id").values("id")[:1],
            ),
        )
        if selected_deployment is not None:
            queryset = queryset.annotate(
                has_selected_smoke_attempt=Exists(
                    WorkflowSmokeAttempt.objects.filter(
                        workflow_version_id=OuterRef("pk"),
                        deployment_id=selected_deployment.id,
                        scope="resource",
                        execution_instance_version__isnull=True,
                    ),
                ),
            ).filter(
                Q(model_profile_version_id=selected_deployment.profile_version_id)
                | Q(validated_deployment_id=selected_deployment.id)
                | Q(has_selected_smoke_attempt=True)
            ).distinct()
        return queryset

    @staticmethod
    def _workflow_rows(
        versions,
        *,
        selected_deployment=None,
        graph_limit: int,
    ):
        attempts = AcceptanceSmokeView._workflow_attempt_map(versions)
        rows = []
        for version in versions:
            attempt_id = getattr(version, "latest_acceptance_attempt_id", None)
            if selected_deployment is None and version.validated_deployment_id:
                attempt_id = getattr(
                    version,
                    "latest_validated_acceptance_attempt_id",
                    None,
                )
            latest_attempt = attempts.get(
                attempt_id,
            )
            evidence = version.smoke_test_evidence
            evidence_attempt = None
            if isinstance(evidence, dict):
                try:
                    evidence_attempt = attempts.get(
                        uuid.UUID(str(evidence.get("smoke_attempt_id"))),
                    )
                except (TypeError, ValueError, AttributeError):
                    evidence_attempt = None
            rows.append(workflow_acceptance_row(
                version,
                selected_deployment=selected_deployment,
                latest_attempt=latest_attempt,
                evidence_attempt=evidence_attempt,
                has_selected_smoke_attempt=(
                    bool(getattr(version, "has_selected_smoke_attempt", False))
                    if selected_deployment is not None
                    else latest_attempt is not None
                ),
                graph_limit=graph_limit,
            ))
        return rows

    def _workflow_response(self, request, query, *, graph_limit: int):
        selected_deployment = None
        deployment_id = query.get("deployment_id")
        if deployment_id:
            selected_deployment = ModelDeployment.objects.select_related(
                "profile_version__provider__credential__active_version",
                "runtime_host",
                "credential__active_version",
            ).filter(pk=deployment_id).first()
        queryset = (
            self._workflow_queryset(selected_deployment=selected_deployment)
            if selected_deployment is not None or not deployment_id
            else WorkflowTemplateVersion.objects.none()
        )
        workflow_id = query.get("workflow_id")
        if workflow_id:
            queryset = queryset.filter(pk=workflow_id)
        search = str(query.get("search") or "").strip()
        if search:
            search_filter = (
                Q(template__name__icontains=search)
                | Q(template__slug__icontains=search)
                | Q(model_profile_version__model_identifier__icontains=search)
                | Q(validated_deployment__name__icontains=search)
                | Q(validated_deployment__key__icontains=search)
            )
            try:
                search_filter |= Q(pk=uuid.UUID(search))
            except (TypeError, ValueError, AttributeError):
                pass
            if search.isdecimal():
                search_filter |= Q(version=int(search))
            queryset = queryset.filter(search_filter)
        queryset = queryset.order_by("-updated_at", "-created_at", "id")
        paginator = AcceptanceSmokePagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        rows = self._workflow_rows(
            list(page),
            selected_deployment=selected_deployment,
            graph_limit=graph_limit,
        )
        payload = WorkflowAcceptanceRowSerializer(rows, many=True).data
        return paginator.get_paginated_response(payload)

    def _deployment_response(self, request, query, *, graph_limit: int):
        latest_health = ModelDeploymentCapabilitySnapshot.objects.filter(
            deployment_id=OuterRef("pk"),
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH,
        ).order_by("-revision", "-id")
        profile_match_count = WorkflowTemplateVersion.objects.filter(
            model_profile_version_id=OuterRef("profile_version_id"),
        ).order_by().values("model_profile_version_id").annotate(
            total=Count("id"),
        ).values("total")[:1]
        profile_evidence_match_count = WorkflowTemplateVersion.objects.filter(
            model_profile_version_id=OuterRef("profile_version_id"),
        ).filter(
            Q(validated_deployment_id=OuterRef("pk"))
            | Q(smoke_attempts__deployment_id=OuterRef("pk")),
        ).order_by().values("model_profile_version_id").annotate(
            total=Count("id", distinct=True),
        ).values("total")[:1]
        validated_count = WorkflowTemplateVersion.objects.filter(
            validated_deployment_id=OuterRef("pk"),
        ).order_by().values("validated_deployment_id").annotate(
            total=Count("id"),
        ).values("total")[:1]
        smoke_history_count = WorkflowSmokeAttempt.objects.filter(
            deployment_id=OuterRef("pk"),
        ).order_by().values("deployment_id").annotate(
            total=Count("workflow_version_id", distinct=True),
        ).values("total")[:1]
        queryset = ModelDeployment.objects.select_related(
            "profile_version__provider__credential__active_version",
            "runtime_host",
            "credential__active_version",
        ).annotate(
            profile_match_count=Coalesce(
                Subquery(profile_match_count, output_field=IntegerField()),
                Value(0),
                output_field=IntegerField(),
            ),
            profile_evidence_match_count=Coalesce(
                Subquery(
                    profile_evidence_match_count,
                    output_field=IntegerField(),
                ),
                Value(0),
                output_field=IntegerField(),
            ),
            validated_count=Coalesce(
                Subquery(validated_count, output_field=IntegerField()),
                Value(0),
                output_field=IntegerField(),
            ),
            smoke_history_count=Coalesce(
                Subquery(smoke_history_count, output_field=IntegerField()),
                Value(0),
                output_field=IntegerField(),
            ),
            latest_health_snapshot_id=Subquery(latest_health.values("id")[:1]),
        )
        deployment_id = query.get("deployment_id")
        if deployment_id:
            queryset = queryset.filter(pk=deployment_id)
        search = str(query.get("search") or "").strip()
        if search:
            search_filter = (
                Q(name__icontains=search)
                | Q(key__icontains=search)
                | Q(profile_version__model_identifier__icontains=search)
                | Q(profile_version__provider__name__icontains=search)
                | Q(profile_version__provider__driver_type__icontains=search)
            )
            try:
                search_filter |= Q(pk=uuid.UUID(search))
            except (TypeError, ValueError, AttributeError):
                pass
            queryset = queryset.filter(search_filter)
        queryset = queryset.order_by("-is_default", "-priority", "key", "id")
        paginator = AcceptanceSmokePagination()
        deployments = list(paginator.paginate_queryset(queryset, request, view=self))
        deployment_ids = [row.id for row in deployments]
        from studio.services.execution_identity import execution_target_contracts
        current_targets = execution_target_contracts(deployment_ids=deployment_ids)

        health_ids = {
            row.latest_health_snapshot_id
            for row in deployments if row.latest_health_snapshot_id
        }
        health_by_id = {
            row.id: row
            for row in ModelDeploymentCapabilitySnapshot.objects.filter(id__in=health_ids)
        }

        # Keep the latest history for every operation contract, not just the
        # deployment's single latest snapshot. Expired operations must remain visible.
        latest_operation_snapshots = ModelDeploymentCapabilitySnapshot.objects.filter(
            deployment_id__in=deployment_ids,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        ).annotate(
            operation_row_number=Window(
                expression=RowNumber(),
                partition_by=(F("deployment_id"), F("operations_sha256"), F("prompt_profile_version_id")),
                order_by=(F("revision").desc(), F("id").desc()),
            ),
        ).filter(operation_row_number=1).values("id")
        smoke_snapshots_by_deployment = {}
        for snapshot in ModelDeploymentCapabilitySnapshot.objects.filter(
            id__in=latest_operation_snapshots,
            deployment_id__in=deployment_ids,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        ).select_related("smoke_attempt__prompt_profile_version").order_by("deployment_id", "-revision", "-id"):
            smoke_snapshots_by_deployment.setdefault(
                snapshot.deployment_id, [],
            ).append(snapshot)

        latest_attempts = ModelDeploymentSmokeAttempt.objects.filter(
            deployment_id__in=deployment_ids,
            scope="resource",
        ).annotate(
            acceptance_row_number=Window(
                expression=RowNumber(),
                partition_by=(F("deployment_id"), F("operation")),
                order_by=(F("created_at").desc(), F("id").desc()),
            ),
        ).filter(acceptance_row_number=1).prefetch_related("resolutions")
        deployment_attempts = {}
        for attempt in latest_attempts:
            deployment_attempts.setdefault(attempt.deployment_id, []).append(attempt)

        latest_workflow_attempts = WorkflowSmokeAttempt.objects.filter(
            deployment_id__in=deployment_ids,
        ).annotate(
            acceptance_row_number=Window(
                expression=RowNumber(),
                partition_by=(F("deployment_id"), F("workflow_version_id")),
                order_by=(F("created_at").desc(), F("id").desc()),
            ),
        ).filter(acceptance_row_number=1).prefetch_related("resolutions")
        unresolved_workflow_attempt_counts = {}
        for attempt in latest_workflow_attempts:
            if (
                attempt.status == WorkflowSmokeAttempt.Status.UNCERTAIN
                and not attempt.resolutions.all()
            ):
                unresolved_workflow_attempt_counts[attempt.deployment_id] = (
                    unresolved_workflow_attempt_counts.get(
                        attempt.deployment_id,
                        0,
                    ) + 1
                )

        related_queryset = self._workflow_queryset().filter(
            validated_deployment_id__in=deployment_ids,
        ).order_by("id")
        related_counts = {
            deployment_id: {
                "fresh_validation_count": 0,
                "drifted_validation_count": 0,
                "fresh_smoke_count": 0,
            }
            for deployment_id in deployment_ids
        }
        last_workflow_id = None
        while True:
            batch_queryset = related_queryset
            if last_workflow_id is not None:
                batch_queryset = batch_queryset.filter(pk__gt=last_workflow_id)
            related_versions = list(
                batch_queryset[:self.related_workflow_batch_size],
            )
            if not related_versions:
                break
            related_rows = self._workflow_rows(
                related_versions,
                graph_limit=graph_limit,
            )
            for version, row in zip(
                related_versions,
                related_rows,
                strict=True,
            ):
                counts = related_counts[version.validated_deployment_id]
                if row["validation"]["fresh"]:
                    counts["fresh_validation_count"] += 1
                elif (
                    row["validation"]["stored_state"]
                    == WorkflowTemplateVersion.ValidationState.PASSED
                ):
                    counts["drifted_validation_count"] += 1
                if row["smoke"]["fresh"]:
                    counts["fresh_smoke_count"] += 1
            last_workflow_id = related_versions[-1].id

        rows = [
            deployment_acceptance_row(
                deployment,
                current_target=current_targets[str(deployment.pk)],
                health_snapshot=health_by_id.get(
                    deployment.latest_health_snapshot_id,
                ),
                smoke_snapshots=smoke_snapshots_by_deployment.get(
                    deployment.id, (),
                ),
                latest_attempts=deployment_attempts.get(deployment.id, ()),
                profile_match_count=deployment.profile_match_count,
                profile_evidence_match_count=(
                    deployment.profile_evidence_match_count
                ),
                validated_count=deployment.validated_count,
                smoke_history_count=deployment.smoke_history_count,
                fresh_validation_count=related_counts[deployment.id][
                    "fresh_validation_count"
                ],
                drifted_validation_count=related_counts[deployment.id][
                    "drifted_validation_count"
                ],
                fresh_smoke_count=related_counts[deployment.id][
                    "fresh_smoke_count"
                ],
                unresolved_smoke_count=(
                    unresolved_workflow_attempt_counts.get(deployment.id, 0)
                ),
            )
            for deployment in deployments
        ]
        payload = DeploymentAcceptanceRowSerializer(rows, many=True).data
        return paginator.get_paginated_response(payload)

    @extend_schema(
        parameters=[AcceptanceSmokeQuerySerializer],
        responses={
            200: OpenApiResponse(
                response=AcceptanceSmokePageSerializer,
                description=(
                    "由必填 subject 决定返回 Workflow 或 Deployment 分页；"
                    "空 results 仍是合法分页。"
                ),
            ),
            400: ConfigurationErrorResponseSerializer,
            401: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Staff-only, paginated, read-only projection of persisted deployment and "
            "workflow acceptance/smoke facts. It never invokes a provider, changes "
            "historical states, or creates an attempt. The required subject query "
            "selects workflow or deployment rows."
        ),
    )
    @security_boundary_resolution_cache()
    def get(self, request):
        serializer = AcceptanceSmokeQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data
        graph_limit = int(resolve_configuration(
            required_keys=("workflow.max_graph_bytes",),
        ).values["workflow.max_graph_bytes"])
        if query["subject"] == "workflow":
            return self._workflow_response(
                request,
                query,
                graph_limit=graph_limit,
            )
        return self._deployment_response(
            request,
            query,
            graph_limit=graph_limit,
        )


class RegistryDeletionPreviewView(APIView):
    permission_classes = (IsAdminUser,)

    @extend_schema(
        request=RegistryDeletionPreviewInputSerializer,
        responses={
            200: RegistryDeletionImpactSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Return the complete server-authored dependency impact for one supported "
            "registry deletion. The preview never deletes, retires, detaches, or invokes a model."
        ),
    )
    def post(self, request):
        serializer = RegistryDeletionPreviewInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            impact = build_registry_deletion_impact(
                serializer.validated_data["resource_type"],
                serializer.validated_data["object_id"],
                actor=request.user,
            )
        except RegistryDeletionTargetNotFound as exc:
            return api_error_response(
                status_code=status.HTTP_404_NOT_FOUND,
                code="registry_resource_not_found",
                details={"detail": str(exc)},
            )
        return Response(RegistryDeletionImpactSerializer(impact).data)


class RegistryDeletionExecuteView(APIView):
    permission_classes = (IsAdminUser,)

    @extend_schema(
        request=RegistryDeletionExecuteInputSerializer,
        parameters=[OpenApiParameter(
            name="X-Audit-Reason-Code",
            type=str,
            location=OpenApiParameter.HEADER,
            required=True,
            description="2-80 位稳定审计原因代码。",
        )],
        responses={
            200: RegistryDeletionExecuteResponseSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Atomically execute one reviewed registry deletion. The server recomputes "
            "the complete graph under the shared recipe/config reference lock."
        ),
    )
    def post(self, request):
        serializer = RegistryDeletionExecuteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reason_code = request_reason_code(request, default="")
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        data = dict(serializer.validated_data)
        resource_type = data.pop("resource_type")
        object_id = data.pop("object_id")
        try:
            result = execute_registry_deletion(
                resource_type,
                object_id,
                actor=request.user,
                reason_code=reason_code,
                **data,
            )
        except RegistryDeletionConflict as exc:
            details: dict[str, object] = {"detail": exc.detail}
            if exc.impact is not None:
                details["impact"] = exc.impact
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details=details,
            )
        return Response(
            RegistryDeletionExecuteResponseSerializer(result).data,
            status=status.HTTP_200_OK,
        )


def _conflict(detail: str, *, code: str = "resource_in_use") -> Response:
    return api_error_response(
        status_code=status.HTTP_409_CONFLICT,
        code=code,
        details={"detail": detail, "missing_keys": []},
    )


class PlatformLifecycleAuditMixin:
    @staticmethod
    def _persisted_summary(instance, *, for_update: bool = False) -> dict:
        """Read the committed row; serializer validation may mutate its instance."""

        queryset = type(instance)._default_manager.all()
        if for_update:
            queryset = queryset.select_for_update()
        persisted = queryset.get(pk=instance.pk)
        return lifecycle_summary(persisted)

    def _audit_reason(self, default: str) -> str:
        try:
            return request_reason_code(self.request, default=default)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc

    def _record_lifecycle(
        self,
        instance,
        *,
        action: str,
        reason_default: str,
        before: dict | None = None,
        after: dict | None = None,
    ):
        return record_platform_lifecycle_event(
            instance,
            actor=self.request.user,
            action=action,
            reason_code=self._audit_reason(reason_default),
            before_summary=before,
            after_summary=after,
        )

    def perform_create(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            self._record_lifecycle(
                instance,
                action="create",
                reason_default="api.create",
                after=lifecycle_summary(instance),
            )

    def perform_update(self, serializer):
        with transaction.atomic():
            before = self._persisted_summary(serializer.instance, for_update=True)
            instance = serializer.save()
            self._record_lifecycle(
                instance,
                action="update",
                reason_default="api.update",
                before=before,
                after=lifecycle_summary(instance),
            )

class StaffPlatformModelViewSet(
    PlatformLifecycleAuditMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination


class SecretBindingViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsSuperUser,)
    pagination_class = StandardPageNumberPagination
    queryset = SecretBinding.objects.select_related("active_version", "created_by")
    search_fields = ("key", "label", "purpose")
    filterset_fields = ("is_active", "purpose")

    def get_serializer_class(self):
        return SecretBindingCreateSerializer if self.action == "create" else SecretBindingSerializer

    @extend_schema(request=SecretBindingCreateSerializer, responses={201: SecretBindingSerializer})
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            binding = create_secret_binding(actor=request.user, **serializer.validated_data)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        except IntegrityError as exc:
            raise ValidationError({"key": "受管凭据 key 已存在"}) from exc
        except SecretStoreError as exc:
            raise ValidationError({"code": exc.code, "detail": str(exc)}) from exc
        return Response(SecretBindingSerializer(binding).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=SecretRotateSerializer, responses=SecretBindingSerializer)
    @action(detail=True, methods=("post",))
    def rotate(self, request, pk=None):
        serializer = SecretRotateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            binding = rotate_secret_binding(
                self.get_object(), actor=request.user, **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        except SecretStoreError as exc:
            raise ValidationError({"code": exc.code, "detail": str(exc)}) from exc
        return Response(SecretBindingSerializer(binding).data)

    @extend_schema(request=SecretStatusSerializer, responses=SecretBindingSerializer)
    @action(detail=True, methods=("post",), url_path="status")
    def set_status(self, request, pk=None):
        serializer = SecretStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            binding = set_secret_binding_active(
                self.get_object(), actor=request.user, **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(SecretBindingSerializer(binding).data)


class SecretAuditEventViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsSuperUser,)
    pagination_class = StandardPageNumberPagination
    queryset = SecretAuditEvent.objects.select_related("binding", "actor")
    serializer_class = SecretAuditEventSerializer
    filterset_fields = ("binding", "action", "actor")


class PlatformLifecycleAuditEventViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    queryset = PlatformLifecycleAuditEvent.objects.select_related("actor")
    serializer_class = PlatformLifecycleAuditEventSerializer
    filterset_fields = ("actor", "action", "object_type", "object_id", "reason_code")
    search_fields = ("object_type", "reason_code")


class ProviderProfileViewSet(StaffPlatformModelViewSet):
    queryset = ProviderProfile.objects.all()
    serializer_class = ProviderProfileSerializer
    filterset_fields = ("driver_type", "is_active", "is_system")
    search_fields = ("key", "name")

    def perform_update(self, serializer):
        candidate = serializer.instance
        with transaction.atomic():
            persisted = ProviderProfile.objects.select_for_update().get(pk=candidate.pk)
            target_active = serializer.validated_data.get(
                "is_active",
                persisted.is_active,
            )
            target_driver_type = serializer.validated_data.get(
                "driver_type",
                persisted.driver_type,
            )
            target_driver_config = serializer.validated_data.get(
                "driver_config",
                persisted.driver_config,
            )
            execution_contract_changed = (
                target_driver_type != persisted.driver_type
                or target_driver_config != persisted.driver_config
            )
            if target_active and (
                not persisted.is_active or execution_contract_changed
            ):
                active_deployments = list(
                    ModelDeployment.objects.select_for_update().select_related(
                        "profile_version__provider",
                    ).filter(
                        profile_version__provider_id=persisted.id,
                        status=ModelDeployment.Status.ACTIVE,
                    )
                )
                for deployment in active_deployments:
                    deployment.profile_version.provider = candidate
                    try:
                        validate_deployment_execution_contract(deployment)
                    except (
                        DjangoValidationError,
                        ProviderDriverError,
                        RuntimeError,
                    ) as exc:
                        raise ValidationError({
                            "driver_config": (
                                "变更会使 active Deployment 的执行合同无效；"
                                "请先停用或修复关联 Deployment"
                            ),
                        }) from exc
            before = lifecycle_summary(persisted)
            instance = serializer.save()
            self._record_lifecycle(
                instance,
                action="update",
                reason_default="api.update",
                before=before,
                after=lifecycle_summary(instance),
            )

class ModelProfileVersionViewSet(StaffPlatformModelViewSet):
    queryset = ModelProfileVersion.objects.select_related("provider", "published_by")
    serializer_class = ModelProfileVersionSerializer
    filterset_fields = ("profile_key", "modality", "status", "provider")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile_key = serializer.validated_data["profile_key"]
        try:
            with transaction.atomic():
                if ModelProfileVersion.objects.filter(
                    profile_key=profile_key,
                ).exists():
                    raise ValidationError({
                        "profile_key": (
                            "该模型版本族已存在；请从模型卡片使用“新建下一版本”"
                        ),
                    })
                instance = serializer.save(version=1)
                self._record_lifecycle(
                    instance,
                    action="create",
                    reason_default="api.create",
                    after=lifecycle_summary(instance),
                )
        except IntegrityError as exc:
            raise ValidationError({
                "profile_key": "该模型版本族已被并发创建；请刷新后新建下一版本",
            }) from exc
        return Response(
            self.get_serializer(instance).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status in {
            ModelProfileVersion.Status.PUBLISHED,
            ModelProfileVersion.Status.RETIRED,
        }:
            raise ValidationError("published/retired model profile version 不可修改")
        if "version" in request.data:
            raise ValidationError({
                "version": "版本号由服务端分配，不能通过普通编辑修改",
            })
        return super().update(request, *args, **kwargs)

    @extend_schema(
        request=ModelProfileNextVersionInputSerializer,
        responses={
            201: ModelProfileVersionSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",), url_path="draft-next")
    def draft_next(self, request, pk=None):
        source = self.get_object()
        input_serializer = ModelProfileNextVersionInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        overrides = input_serializer.validated_data
        for attempt in range(4):
            try:
                with transaction.atomic():
                    # Every existing family has one stable anchor row. Lock it
                    # first so concurrent writers serialize before checking
                    # that the requested source is still the latest version.
                    anchor = (
                        ModelProfileVersion.objects.select_for_update()
                        .filter(profile_key=source.profile_key)
                        .order_by("version", "pk")
                        .first()
                    )
                    if anchor is None:
                        raise ValidationError({
                            "profile_key": "源模型版本族已不存在，请刷新后重试",
                        })
                    locked_source = (
                        ModelProfileVersion.objects.select_for_update()
                        .select_related("provider")
                        .get(pk=source.pk)
                    )
                    latest = (
                        ModelProfileVersion.objects.select_for_update()
                        .filter(profile_key=locked_source.profile_key)
                        .order_by("-version", "-pk")
                        .first()
                    )
                    if latest is None or latest.pk != locked_source.pk:
                        return _conflict(
                            "源模型版本已不是当前最高版本；请刷新后从最新版本重试",
                            code="model_profile_source_stale",
                        )
                    if locked_source.status == ModelProfileVersion.Status.DRAFT:
                        return _conflict(
                            "当前最高版本已是草稿；请继续编辑该草稿，不要重复创建",
                            code="model_profile_draft_already_exists",
                        )
                    try:
                        source_specification = verified_model_profile_specification(
                            locked_source,
                        )
                        allowed_legacy_capabilities = model_capability_values(
                            source_specification,
                        )
                        source_model_family = model_family_value(
                            source_specification,
                        )
                    except DjangoValidationError:
                        return _conflict(
                            "源模型版本规格完整性校验失败；请先修复服务端事实",
                            code="model_profile_source_contract_invalid",
                        )
                    provider = overrides.get("provider", locked_source.provider)
                    payload = {
                        "profile_key": locked_source.profile_key,
                        "provider": str(provider.pk),
                        "model_identifier": overrides.get(
                            "model_identifier",
                            locked_source.model_identifier,
                        ),
                        "modality": overrides.get(
                            "modality",
                            locked_source.modality,
                        ),
                        "specification": overrides.get(
                            "specification",
                            source_specification,
                        ),
                    }
                    serializer = self.get_serializer(
                        data=payload,
                        context={
                            **self.get_serializer_context(),
                            "allowed_legacy_capabilities": (
                                allowed_legacy_capabilities
                            ),
                            "allowed_model_families": (
                                (source_model_family,)
                                if source_model_family else ()
                            ),
                        },
                    )
                    serializer.is_valid(raise_exception=True)
                    instance = serializer.save(version=locked_source.version + 1)
                    self._record_lifecycle(
                        instance,
                        action="create",
                        reason_default="api.create_next_version",
                        after=lifecycle_summary(instance),
                    )
                return Response(
                    self.get_serializer(instance).data,
                    status=status.HTTP_201_CREATED,
                )
            except IntegrityError:
                if attempt == 3:
                    return _conflict(
                        "并发创建模型版本未能完成，请刷新后重试",
                        code="model_profile_version_allocation_conflict",
                    )
        raise AssertionError("unreachable model profile allocation loop")

    @extend_schema(
        request=None,
        responses={
            200: ModelProfileRegistryOptionsSerializer,
            403: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=False, methods=("get",), url_path="options")
    def registry_options(self, request):
        return Response(model_profile_registry_options())

    @extend_schema(request=None, responses={
        200: ModelProfileVersionSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        with transaction.atomic():
            profile = ModelProfileVersion.objects.select_for_update().get(pk=self.get_object().pk)
            before = lifecycle_summary(profile)
            if profile.status != ModelProfileVersion.Status.DRAFT:
                raise ValidationError("只有 draft model profile version 可以发布")
            try:
                specification = verified_model_profile_specification(profile)
                validate_model_profile_media_capabilities(
                    specification,
                    modality=profile.modality,
                )
                validate_version_configuration_defaults(
                    model_profile_version=profile,
                )
                validate_async_model_profile(profile)
                validate_prompt_normalization_model_profile(profile)
                validate_script_generation_model_profile(profile)
                validate_script_structure_model_profile(profile)
                validate_visual_rules_model_profile(profile)
                from studio.services.execution_text_contracts import validate_text_execution_model_profile

                validate_text_execution_model_profile(profile)
                # Freeze the exact artifact closure while the profile row is
                # locked.  Artifact CRUD follows the same profile -> artifact
                # lock order, so no file declaration can cross the immutable
                # publish boundary.
                locked_artifacts = list(
                    ModelArtifact.objects.select_for_update()
                    .filter(profile_version=profile)
                    .order_by("pk")
                )
                validate_draft_profile_artifacts_for_publish(
                    profile,
                    artifacts=locked_artifacts,
                )
            except (DjangoValidationError, ConfigurationResolutionError) as exc:
                raise _drf_validation(exc) from exc
            profile.status = ModelProfileVersion.Status.PUBLISHED
            profile.published_at = timezone.now()
            profile.published_by = request.user
            profile.save(update_fields=("status", "published_at", "published_by", "updated_at"))
            self._record_lifecycle(
                profile,
                action="publish",
                reason_default="api.publish",
                before=before,
                after=lifecycle_summary(profile),
            )
        return Response(self.get_serializer(profile).data)

    @extend_schema(request=None, responses={
        200: ModelProfileVersionSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        with transaction.atomic():
            profile = ModelProfileVersion.objects.select_for_update().get(
                pk=self.get_object().pk,
            )
            before = lifecycle_summary(profile)
            if profile.status != ModelProfileVersion.Status.PUBLISHED:
                raise ValidationError("只有 published model profile version 可以退役")
            if profile.deployments.filter(status__in=(
                ModelDeployment.Status.ACTIVE,
                ModelDeployment.Status.ERROR,
            )).exists():
                return _conflict("仍有 active/error deployment 引用该模型版本，请先停用或修复部署")
            if profile.workflow_versions.filter(
                status=WorkflowTemplateVersion.Status.PUBLISHED,
                is_current=True,
            ).exists():
                return _conflict("当前已发布工作流仍引用该模型版本，请先发布替代工作流")
            profile.status = ModelProfileVersion.Status.RETIRED
            profile.save(update_fields=("status", "updated_at"))
            self._record_lifecycle(
                profile,
                action="retire",
                reason_default="api.retire",
                before=before,
                after=lifecycle_summary(profile),
            )
        return Response(self.get_serializer(profile).data)


class FieldDefinitionViewSet(StaffPlatformModelViewSet):
    queryset = FieldDefinition.objects.all()
    serializer_class = FieldDefinitionSerializer
    filterset_fields = ("is_active",)
    search_fields = ("key",)

    @extend_schema(
        request=FieldDefinitionCreateInputSerializer,
        responses={201: FieldDefinitionSerializer},
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        # Serializing the append operation prevents concurrent creates from
        # receiving the same presentation order without making that order a
        # user-entered business field.
        with registry_reference_write_guard():
            serializer.validated_data["is_active"] = field_system_defaults()[
                "is_active"
            ]
            super().perform_create(serializer)

    def update(self, request, *args, **kwargs):
        with registry_reference_write_guard():
            FieldDefinition.objects.select_for_update().get(
                pk=self.get_object().pk,
            )
            return super().update(request, *args, **kwargs)

    @extend_schema(request=None, responses=FieldDefinitionOptionsSerializer)
    @action(detail=False, methods=("get",), url_path="options")
    def registry_options(self, request):
        data = {
            "system_defaults": field_system_defaults(),
        }
        return Response(FieldDefinitionOptionsSerializer(data).data)

class PromptProfileVersionViewSet(StaffPlatformModelViewSet):
    serializer_class = PromptProfileVersionSerializer
    filterset_fields = ("profile_key", "model_family", "operation", "mode", "status", "review_state")
    search_fields = ("profile_key", "name", "description")

    def get_queryset(self):
        return PromptProfileVersion.objects.select_related("published_by").annotate(
            field_rule_count=Count("field_rules"),
        )

    @extend_schema(
        request=PromptProfileVersionCreateInputSerializer,
        responses={201: PromptProfileVersionSerializer},
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        profile_key = serializer.validated_data["profile_key"]
        with registry_reference_write_guard():
            if PromptProfileVersion.objects.filter(profile_key=profile_key).exists():
                raise ValidationError({
                    "profile_key": (
                        "该 Profile Key 已有历史版本；请从最新不可变版本创建后继草稿"
                    ),
                })
            serializer.validated_data["version"] = 1
            serializer.validated_data["review_state"] = (
                PromptProfileVersion.ReviewState.NEEDS_REVIEW
            )
            super().perform_create(serializer)

    def update(self, request, *args, **kwargs):
        if "version" in request.data:
            raise ValidationError({
                "version": "版本号由服务端分配，不能通过普通编辑修改",
            })
        with registry_reference_write_guard():
            profile = PromptProfileVersion.objects.select_for_update().get(
                pk=self.get_object().pk,
            )
            if profile.status in {
                PromptProfileVersion.Status.PUBLISHED,
                PromptProfileVersion.Status.RETIRED,
            }:
                raise ValidationError("published/retired PromptProfile 不可修改；请创建新版本")
            return super().update(request, *args, **kwargs)

    @extend_schema(
        request=None,
        responses={200: PromptProfileVersionSerializer, 400: ConfigurationErrorResponseSerializer},
    )
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        try:
            with registry_reference_write_guard():
                profile = PromptProfileVersion.objects.select_for_update().get(
                    pk=self.get_object().pk,
                )
                before = lifecycle_summary(profile)
                refresh_prompt_profile_contract(
                    profile,
                    schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
                )
                published = publish_prompt_profile(profile, actor=request.user)
                self._record_lifecycle(
                    published,
                    action="publish",
                    reason_default="api.publish",
                    before=before,
                    after=lifecycle_summary(published),
                )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(self.get_serializer(published).data)

    @extend_schema(
        request=None,
        responses={200: PromptProfileVersionSerializer, 400: ConfigurationErrorResponseSerializer},
    )
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        try:
            with registry_reference_write_guard():
                profile = PromptProfileVersion.objects.select_for_update().get(
                    pk=self.get_object().pk,
                )
                before = lifecycle_summary(profile)
                retired = retire_prompt_profile(profile)
                self._record_lifecycle(
                    retired,
                    action="retire",
                    reason_default="api.retire",
                    before=before,
                    after=lifecycle_summary(retired),
                )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(self.get_serializer(retired).data)

    @extend_schema(
        request=PromptProfileCloneSuccessorInputSerializer,
        responses={
            201: PromptProfileVersionSerializer,
            400: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",), url_path="clone-successor")
    def clone_successor(self, request, pk=None):
        input_serializer = PromptProfileCloneSuccessorInputSerializer(
            data=request.data,
        )
        input_serializer.is_valid(raise_exception=True)
        source_id = self.get_object().pk
        try:
            with registry_reference_write_guard():
                source = self.get_queryset().get(pk=source_id)
                target = clone_prompt_profile_successor(
                    source,
                    **input_serializer.validated_data,
                )
                self._record_lifecycle(
                    target,
                    action="clone_successor",
                    reason_default="api.clone_successor",
                    after=lifecycle_summary(target),
                )
                target = self.get_queryset().get(pk=target.pk)
        except DjangoValidationError as exc:
            code = str(getattr(exc, "code", None) or "")
            if code in {
                "prompt_profile_clone_draft_exists",
                "prompt_profile_clone_source_not_latest",
                "prompt_profile_clone_source_stale",
                "prompt_profile_clone_source_contract_invalid",
            }:
                messages = getattr(exc, "messages", None) or [str(exc)]
                return _conflict(str(messages[0]), code=code)
            raise _drf_validation(exc) from exc
        return Response(self.get_serializer(target).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=PromptProfilePreviewInputSerializer,
        responses={
            200: PromptProfilePreviewSerializer,
            400: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def preview(self, request, pk=None):
        input_serializer = PromptProfilePreviewInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        profile = self.get_object()
        candidate = input_serializer.validated_data.get("candidate")
        if candidate is not None and (
            input_serializer.validated_data["expected_base_contract_sha256"]
            != profile.contract_sha256
        ):
            return Response({
                "code": "prompt_profile_preview_base_stale",
                "detail": "基础提示词策略合同已变化；请重新读取页面后再预览",
            }, status=status.HTTP_409_CONFLICT)
        try:
            preview = preview_prompt_profile(
                profile,
                sources=input_serializer.validated_data["sources"],
                candidate=candidate,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(PromptProfilePreviewSerializer(preview).data)

    @extend_schema(request=None, responses=PromptProfileOptionsSerializer)
    @action(detail=False, methods=("get",), url_path="options")
    def registry_options(self, request):
        operations = sorted(set(KNOWN_PROMPT_OPERATIONS) | set(
            PromptProfileVersion.objects.values_list("operation", flat=True),
        ))
        model_rows = list(ModelProfileVersion.objects.filter(
            status=ModelProfileVersion.Status.PUBLISHED,
        ).select_related("provider").order_by("profile_key", "-version"))
        model_families = set(KNOWN_MODEL_FAMILIES) | set(
            PromptProfileVersion.objects.values_list("model_family", flat=True)
        )
        for item in model_rows:
            try:
                model_families.add(model_family_for_profile(item))
            except DjangoValidationError:
                # Invalid historical specifications stay visible in the model
                # registry but are not offered as a new PromptProfile selector.
                continue
        families = sorted(model_families)
        modes = sorted(set(
            PromptProfileVersion.objects.exclude(mode="").values_list("mode", flat=True),
        ))

        def choices(enum):
            return [{"value": value, "label": label} for value, label in enum.choices]

        context_selector_descriptions = {
            PromptProfileFieldRule.GenerationContextSelector.PROJECT_CORE: (
                "项目标题、梗概、语言、画幅与连续性摘要；不包含视觉风格。 / "
                "Project identity, story, language, aspect ratio, and continuity summary; excludes style."
            ),
            PromptProfileFieldRule.GenerationContextSelector.PROJECT_STYLE: (
                "项目已确认的视觉风格、正向与负向风格说明。 / Reviewed project visual style facts."
            ),
            PromptProfileFieldRule.GenerationContextSelector.TARGET_SHOT_CORE: (
                "仅当前镜头的叙事、动作、镜头、声景等核心事实；不包含任何对白正文。 / "
                "Target-shot core facts only; excludes all authored dialogue text."
            ),
            PromptProfileFieldRule.GenerationContextSelector.TARGET_SHOT_DIALOGUE_METADATA: (
                "仅当前镜头的说话人、语气、起止时间与画外音标记；不包含对白正文。 / "
                "Target-shot speaker, delivery, timing, and voiceover metadata; excludes authored text."
            ),
            PromptProfileFieldRule.GenerationContextSelector.TARGET_SHOT_DIALOGUE_AUTHORED_TEXT: (
                "显式允许模型读取当前镜头已经确认的对白正文。 / Explicitly exposes reviewed target-shot dialogue text."
            ),
            PromptProfileFieldRule.GenerationContextSelector.TARGET_SHOT_LINKED_BIBLE: (
                "仅当前镜头关联的人物、场景和道具 Bible；不包含外镜事实或对白。 / "
                "Bible entities linked to the target shot only; excludes other-shot facts and dialogue."
            ),
            PromptProfileFieldRule.GenerationContextSelector.ADJACENT_SHOTS_CORE: (
                "显式允许读取前后相邻镜头核心事实；仍不包含相邻镜头对白正文。 / "
                "Explicitly exposes adjacent-shot core facts; still excludes adjacent authored dialogue."
            ),
            PromptProfileFieldRule.GenerationContextSelector.ALL_SHOTS_CORE: (
                "显式允许读取全剧镜头核心事实；仍不包含任何镜头对白正文。 / "
                "Explicitly exposes all-shot core facts; still excludes authored dialogue text."
            ),
        }

        models = [
            {
                "id": str(item.id),
                "profile_key": item.profile_key,
                "version": item.version,
                "model_identifier": item.model_identifier,
                "modality": item.modality,
            }
            for item in model_rows
        ]
        from studio.services.prompt_generation_sources import FIELD_AUXILIARY_OPERATIONS
        data = {
            "generation_leaf_operations": sorted(FIELD_AUXILIARY_OPERATIONS),
            "operations": operations,
            "model_families": families,
            "modes": modes,
            "custom_entry_fields": [
                "operation",
                "mode",
                "language_policy",
            ],
            "rule_system_defaults": prompt_rule_system_defaults(),
            "source_selectors": prompt_source_options(SUPPORTED_FIELD_SOURCE_SELECTORS),
            "channels": choices(PromptProfileFieldRule.Channel),
            "label_styles": choices(PromptProfileFieldRule.LabelStyle),
            "separators": choices(PromptProfileFieldRule.Separator),
            "generation_modes": choices(PromptProfileFieldRule.GenerationMode),
            "generation_output_semantics": choices(
                PromptProfileFieldRule.GenerationOutputSemantics,
            ),
            "source_projection_keys": [
                {
                    "value": DIRECT_SOURCE_PROJECTION_KEY,
                    "label": "直接字段装配 / Direct field assembly",
                },
                {
                    "value": DECLARATIVE_SOURCE_PROJECTION_KEY,
                    "label": "声明式原语组合 / Declarative primitive composition",
                },
            ],
            "source_projection_primitives": source_projection_primitive_options(),
            "reference_rendering_fields": rendering_options(),
                "reference_rendering_text_slots": [{"value": key, "label": label} for key, label in UNIT_RENDER_SLOTS.items()],
            "language_policies": [
                {"value": "inherit", "label": "继承运行上下文 / Inherit context"},
                {
                    "value": "preserve_source",
                    "label": "保留来源语言 / Preserve source",
                },
                {"value": "any", "label": "不限语言 / Any language"},
                *prompt_language_options(),
                {
                    "value": "bilingual:zh-Hans+en",
                    "label": "中英双语 / Chinese and English",
                },
            ],
            "length_units": choices(PromptProfileFieldRule.LengthUnit),
            "generation_validators": [
                {**item, "label": (
                    "结构检查 · " if item["value"] in {
                        "non_empty", "no_control_characters", "valid_json", "json_object", "json_array",
                    } else "生成指导 · "
                ) + item["label"]}
                for item in choices(PromptProfileFieldRule.GenerationValidator)
            ],
            "generation_context_selectors": [
                {
                    "value": value,
                    "label": label,
                    "description": context_selector_descriptions[value],
                }
                for value, label in (
                    PromptProfileFieldRule.GenerationContextSelector.choices
                )
            ],
            "generation_source_selectors": prompt_generation_source_options(),
            "model_profile_versions": models,
            "effective_limits": {
                "field_text_max_characters": boundary_int(
                    "security.prompt_profile.field_text_max_characters"
                ),
                "max_validators": boundary_int(
                    "security.contract_complexity.prompt_profile.max_validators"
                ),
                "max_context_selectors": boundary_int(
                    "security.contract_complexity.prompt_profile.max_context_selectors"
                ),
            },
        }
        return Response(PromptProfileOptionsSerializer(data).data)


class PromptProfileFieldRuleViewSet(StaffPlatformModelViewSet):
    queryset = PromptProfileFieldRule.objects.select_related(
        "profile_version", "field_definition", "field_definition_version", "source_version",
    )
    serializer_class = PromptProfileFieldRuleSerializer
    filterset_fields = (
        "profile_version", "field_definition", "channel", "source_selector",
        "generation_mode", "length_unit",
    )

    @staticmethod
    def _require_draft_profile(profile_id):
        profile = PromptProfileVersion.objects.select_for_update().get(
            pk=profile_id,
        )
        if profile.status != PromptProfileVersion.Status.DRAFT:
            raise ValidationError(
                "字段规则只能写入草稿 PromptProfile；已发布版本合同不可变化",
            )
        return profile

    def perform_create(self, serializer):
        with registry_reference_write_guard():
            profile = self._require_draft_profile(
                serializer.validated_data["profile_version"].pk,
            )
            serializer.save(profile_version=profile)

    def perform_update(self, serializer):
        with registry_reference_write_guard():
            locked_rule = PromptProfileFieldRule.objects.select_for_update().get(
                pk=serializer.instance.pk,
            )
            profile = self._require_draft_profile(
                locked_rule.profile_version_id,
            )
            serializer.instance = locked_rule
            serializer.save(profile_version=profile)



class ModelDeploymentViewSet(ResourceSmokeMixin, StaffPlatformModelViewSet):
    queryset = ModelDeployment.objects.select_related(
        "profile_version__provider", "runtime_host",
    )
    serializer_class = ModelDeploymentSerializer
    filterset_fields = ("status", "runtime_host", "profile_version", "is_default")
    search_fields = ("key", "name")

    def update(self, request, *args, **kwargs):
        if self.get_object().status == ModelDeployment.Status.RETIRED:
            raise ValidationError(
                "retired deployment 不可修改；请创建新部署记录",
            )
        return super().update(request, *args, **kwargs)

    def _save_with_host_lock(self, serializer, *, action: str):
        target_profile = serializer.validated_data.get(
            "profile_version",
            getattr(serializer.instance, "profile_version", None),
        )
        target_host = serializer.validated_data.get(
            "runtime_host",
            getattr(serializer.instance, "runtime_host", None),
        )
        target_status = serializer.validated_data.get(
            "status",
            getattr(serializer.instance, "status", ModelDeployment.Status.ACTIVE),
        )
        with transaction.atomic():
            locked_provider = ProviderProfile.objects.select_for_update().get(
                pk=target_profile.provider_id,
            )
            target_profile.provider = locked_provider
            if target_host is not None:
                locked_host = RuntimeHost.objects.select_for_update().get(pk=target_host.pk)
                if (
                    target_status == ModelDeployment.Status.ACTIVE
                    and locked_host.status != RuntimeHost.Status.ACTIVE
                ):
                    raise ValidationError({
                        "runtime_host": "active deployment 只能绑定状态为 active 的运行主机",
                    })
            before = (
                self._persisted_summary(serializer.instance, for_update=True)
                if serializer.instance else None
            )
            candidate = serializer.instance or ModelDeployment()
            for field, value in serializer.validated_data.items():
                setattr(candidate, field, value)
            candidate.profile_version = target_profile
            if target_status == ModelDeployment.Status.ACTIVE:
                try:
                    validate_deployment_execution_contract(candidate)
                except (
                    DjangoValidationError,
                    ProviderDriverError,
                    RuntimeError,
                ) as exc:
                    raise ValidationError({
                        "runtime_config": "Deployment 执行合同无效",
                    }) from exc
            instance = serializer.save()
            self._record_lifecycle(
                instance,
                action=action,
                reason_default=f"api.{action}",
                before=before,
                after=lifecycle_summary(instance),
            )

    def perform_create(self, serializer):
        self._save_with_host_lock(serializer, action="create")

    def perform_update(self, serializer):
        self._save_with_host_lock(serializer, action="update")

    @extend_schema(request=None, responses={
        200: ModelDeploymentSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        with transaction.atomic():
            deployment = ModelDeployment.objects.select_for_update().get(
                pk=self.get_object().pk,
            )
            if deployment.status != ModelDeployment.Status.DISABLED:
                raise ValidationError("只有 disabled deployment 可以退役")
            if deployment.execution_versions.filter(status="published", instance__is_active=True).exists():
                return _conflict(
                    "仍有启用且已发布的执行实例版本引用该模型部署，请先调整这些执行实例",
                )
            if deployment.validated_workflow_versions.filter(
                status=WorkflowTemplateVersion.Status.PUBLISHED,
                is_current=True,
            ).exists():
                return _conflict(
                    "当前已发布工作流仍以该 deployment 作为验证依据，请先发布替代工作流",
                )
            before = lifecycle_summary(deployment)
            deployment.status = ModelDeployment.Status.RETIRED
            deployment.is_default = False
            deployment.save(update_fields=("status", "is_default", "updated_at"))
            self._record_lifecycle(
                deployment,
                action="retire",
                reason_default="api.retire",
                before=before,
                after=lifecycle_summary(deployment),
            )
        return Response(self.get_serializer(deployment).data)

    @extend_schema(request=None, responses=ModelDeploymentCapabilitySnapshotSerializer)
    @action(detail=True, methods=("post",), url_path="probe")
    def probe(self, request, pk=None):
        if self.get_object().status != ModelDeployment.Status.ACTIVE:
            return _conflict("只有 active deployment 可以执行探测", code="deployment_inactive")
        try:
            snapshot = probe_async_deployment(self.get_object())
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ModelDeploymentCapabilitySnapshotSerializer(snapshot).data)

    @extend_schema(
        request=ResolvePromptNormalizationSmokeInputSerializer,
        responses={
            201: PromptNormalizationSmokeResolutionSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Append immutable operator evidence for an uncertain deployment smoke "
            "attempt. This never replays the provider request or creates passed evidence."
        ),
    )
    @action(
        detail=True,
        methods=("post",),
        url_path="resolve-smoke-uncertain",
        permission_classes=(IsAdminUser,),
    )
    def resolve_smoke_uncertain(self, request, pk=None):
        serializer = ResolvePromptNormalizationSmokeInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        deployment = self.get_object()
        try:
            resolution = resolve_deployment_smoke_attempt(
                deployment,
                user=request.user,
                **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None) or {}
            code = detail.get("code") if isinstance(detail, dict) else None
            if code:
                return _conflict(str(exc), code=str(code))
            raise _drf_validation(exc) from exc
        return Response(
            PromptNormalizationSmokeResolutionSerializer(resolution).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=ComfySavedWorkflowPreviewInputSerializer,
        responses={
            200: ComfySavedWorkflowPreviewResponseSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Analyze one already-managed immutable candidate against live object_info. "
            "With no primary output selected the response only lists candidates; with "
            "one primary output selected the server creates or reuses an immutable "
            "ComfyWorkflowContractDraft. Unresolved interpretation is persisted as "
            "candidate_only instead of deleting or rejecting the safe candidate. Raw "
            "canvas values and converted graphs are never returned to the browser. "
            "Send a stable request_key before submission to recover the exact response. "
            "Same actor/key and identity replays the persisted response; changed identity returns 409."
        ),
    )
    @action(detail=True, methods=("post",), url_path="saved-workflows/preview")
    @security_boundary_resolution_cache()
    @saved_workflow_directory_resolution_cache()
    def saved_workflow_preview(self, request, pk=None):
        serializer = ComfySavedWorkflowPreviewInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        deployment = self.get_object()
        def build(data, persist):
            def public_response(payload):
                return ComfySavedWorkflowPreviewResponseSerializer({
                    **payload, "effective_limits": _saved_workflow_effective_limits(),
                }).data
            payload = preview_saved_workflow(deployment, actor=request.user,
                persist=lambda save: persist(lambda: public_response(save())), **data)
            return public_response(payload)
        try:
            payload = commit_preview_receipt(
                owner=request.user, deployment=deployment,
                data=serializer.validated_data, build=build,
            )
        except (DjangoValidationError, ProviderDriverError) as exc:
            raise _drf_validation(exc) from exc
        response = Response(ComfySavedWorkflowPreviewResponseSerializer(payload).data)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(
        parameters=[OpenApiParameter("request_key", OpenApiTypes.STR, OpenApiParameter.QUERY, required=True)],
        responses={200: ComfySavedWorkflowPreviewResponseSerializer,
            400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer},
        description="Read the authenticated actor's original committed contract preview. No host access, projection, import or execution. A missing receipt is unknown, not proof of non-commit.",
    )
    @action(detail=True, methods=("get",), url_path="saved-workflows/preview-receipt")
    def saved_workflow_preview_receipt(self, request, pk=None):
        key = request.query_params.get("request_key", "")
        field = ComfySavedWorkflowPreviewInputSerializer().fields["request_key"]
        key = field.run_validation(key)
        try:
            payload = find_preview_receipt(owner=request.user, deployment=self.get_object(), request_key=key)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ComfySavedWorkflowPreviewResponseSerializer(payload).data,
            headers={"Cache-Control": "private, no-store"})

    @extend_schema(
        request=ComfyDraftArtifactAttestationInputSerializer,
        parameters=[OpenApiParameter(
            "Location",
            OpenApiTypes.URI,
            OpenApiParameter.HEADER,
            description="Durable RuntimeHostProbeAttempt detail URL.",
            response=[202],
        )],
        responses={
            202: RuntimeHostProbeAttemptSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Re-read and reconvert one saved workflow, derive its exact reachable model "
            "dependencies server-side, create/reuse declared artifacts on a same-provider "
            "draft profile, and enqueue durable loopback size/hash attestation. The request "
            "never trusts client-provided filenames or hashes and never submits a GPU prompt. "
            "Requires the source-scope schema version/hash returned by discover and rejects "
            "source_scope_changed before re-reading or mutating source state."
        ),
    )
    @action(
        detail=True,
        methods=("post",),
        url_path="saved-workflows/attest-model-artifacts",
    )
    def attest_saved_workflow_model_artifacts(self, request, pk=None):
        serializer = ComfyDraftArtifactAttestationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        deployment = self.get_object()
        before = lifecycle_summary(deployment)
        try:
            attempt, created = enqueue_draft_model_artifact_attestation(
                deployment,
                actor=request.user,
                **serializer.validated_data,
            )
        except DraftArtifactAttestationConflict as exc:
            return _conflict(
                str(exc),
                code=DraftArtifactAttestationConflict.code,
            )
        except SavedWorkflowImportError as exc:
            return api_error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=exc.code,
                details={
                    "detail": _safe_projected_error(
                        exc,
                        fallback="ComfyUI 工作流结构不满足平台安全导入合同",
                    ),
                },
            )
        except (DjangoValidationError, ProviderDriverError) as exc:
            raise _drf_validation(exc) from exc
        if created:
            self._record_lifecycle(
                deployment,
                action="attest_model_artifacts",
                reason_default="api.attest_model_artifacts",
                before=before,
                after=lifecycle_summary(deployment),
            )
        location = reverse(
            "runtime-host-probe-attempt-detail",
            kwargs={"pk": attempt.pk},
            request=request,
        )
        return Response(
            RuntimeHostProbeAttemptSerializer(attempt).data,
            status=status.HTTP_202_ACCEPTED,
            headers={"Location": location, "Cache-Control": "private, no-store"},
        )

    @extend_schema(
        request=ComfySavedWorkflowBatchInputSerializer,
        responses={
            200: ComfySavedWorkflowBatchResponseSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Commit independently isolated saved-workflow actions. candidate, ignored, "
            "and execution_snapshot items read the controlled source and verify its exact "
            "hash. template_version items instead consume the selected immutable candidate "
            "and ContractDraft, never re-read mutable local userdata, and create only a "
            "draft WorkflowTemplateVersion. Per-item failures remain explicit and do not "
            "roll back successful siblings. Live Validate, real Smoke, and publication are "
            "separate later lifecycle actions."
        ),
    )
    @action(detail=True, methods=("post",), url_path="saved-workflows/batch-import")
    @security_boundary_resolution_cache()
    @saved_workflow_directory_resolution_cache()
    def saved_workflow_batch_import(self, request, pk=None):
        serializer = ComfySavedWorkflowBatchInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = batch_import_saved_workflows(
                self.get_object(),
                items=serializer.validated_data["items"],
                actor=request.user,
            )
        except (DjangoValidationError, ProviderDriverError) as exc:
            raise _drf_validation(exc) from exc
        return Response(ComfySavedWorkflowBatchResponseSerializer(payload).data)

    @extend_schema(
        parameters=[
            OpenApiParameter("probe_kind", OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY),
        ],
        responses=ModelDeploymentCapabilitySnapshotSerializer(many=True),
    )
    @action(
        detail=True,
        methods=("get",),
        url_path="evidence",
        filter_backends=(),
    )
    def evidence(self, request, pk=None):
        queryset = self.get_object().capability_snapshots.all().order_by("-revision")
        if request.query_params.get("probe_kind"):
            queryset = queryset.filter(probe_kind=request.query_params["probe_kind"])
        if request.query_params.get("status"):
            queryset = queryset.filter(status=request.query_params["status"])
        page = self.paginate_queryset(queryset)
        serializer = ModelDeploymentCapabilitySnapshotSerializer(
            page if page is not None else queryset,
            many=True,
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class ComfyWorkflowSourceViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    serializer_class = ComfyWorkflowSourceSerializer
    queryset = ComfyWorkflowSource.objects.select_related(
        "source_deployment", "workflow_version",
        "generation_task", "imported_by",
    ).order_by("-created_at")
    filterset_fields = (
        "source_deployment", "source_scope_sha256", "import_kind", "workflow_version",
        "generation_task", "source_sha256",
    )
    search_fields = ("source_path", "source_workflow_id")

    @extend_schema(
        request=None,
        responses={
            200: ComfySavedWorkflowDiscoveryResponseSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Scan every distinct active ComfyUI source once, using the system-configured "
            "userdata-relative saved-workflow directory. "
            "The server chooses an internal transport deployment, derives output and "
            "model-profile candidates from live object_info plus registry evidence, "
            "and returns explicit matched/ambiguous/unmatched/blocked results. The "
            "operation is read-only and never submits a ComfyUI prompt."
        ),
    )
    @action(detail=False, methods=("get",), url_path="discover")
    @security_boundary_resolution_cache()
    @saved_workflow_directory_resolution_cache()
    def discover(self, request):
        try:
            payload = discover_saved_workflows()
        except SavedWorkflowRequestBudgetExceeded as exc:
            return api_error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=exc.code,
                details={"detail": str(exc)},
            )
        payload = {
            **payload,
            "effective_limits": _saved_workflow_effective_limits(),
        }
        response = Response(
            ComfySavedWorkflowDiscoveryResponseSerializer(payload).data,
        )
        response["Cache-Control"] = "private, no-store"
        return response


class ComfyWorkflowContractDraftViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    serializer_class = ComfyWorkflowContractDraftSerializer
    queryset = ComfyWorkflowContractDraft.objects.select_related(
        "candidate_source", "model_profile_version",
        "projector_version__projector", "created_by",
    ).order_by("-created_at")
    filterset_fields = (
        "candidate_source", "model_profile_version", "projector_version",
        "admission_tier", "actionability",
    )
    search_fields = (
        "candidate_source__source_path", "projector_version__projector__key",
    )


class ModelArtifactViewSet(StaffPlatformModelViewSet):
    queryset = ModelArtifact.objects.select_related("profile_version")
    serializer_class = ModelArtifactSerializer
    filterset_fields = ("profile_version", "role", "status")
    search_fields = ("logical_name", "filename", "sha256")

    _VERIFICATION_INPUT_FIELDS = (
        "profile_version",
        "role",
        "filename",
        "storage_uri",
        "inventory_folder",
        "inventory_node_type",
        "inventory_input_name",
        "sha256",
        "size_bytes",
        "status",
    )

    @classmethod
    def _verification_input_changed(cls, artifact, validated_data) -> bool:
        for field in cls._VERIFICATION_INPUT_FIELDS:
            if field not in validated_data:
                continue
            current = getattr(artifact, field)
            candidate = validated_data[field]
            current_value = getattr(current, "pk", current)
            candidate_value = getattr(candidate, "pk", candidate)
            if current_value != candidate_value:
                return True
        return False

    @staticmethod
    def _lock_draft_profiles(profile_ids) -> dict:
        normalized_ids = sorted({str(profile_id) for profile_id in profile_ids})
        profiles = list(
            ModelProfileVersion.objects.select_for_update()
            .select_related("provider")
            .filter(pk__in=normalized_ids)
            .order_by("pk")
        )
        if len(profiles) != len(normalized_ids):
            raise ValidationError({
                "profile_version": "model profile version 已不存在，请刷新后重试",
            })
        for profile in profiles:
            if profile.status != ModelProfileVersion.Status.DRAFT:
                raise ValidationError({
                    "profile_version": (
                        "published/retired model profile version 不再接受 artifact 变更；"
                        "请创建新版本"
                    ),
                })
        return {profile.pk: profile for profile in profiles}

    def create(self, request, *args, **kwargs):
        # The first validation only discovers the target FK.  It is repeated
        # after locking so a concurrent publish cannot make the validated
        # profile stale before the insert.
        discovery = self.get_serializer(data=request.data)
        discovery.is_valid(raise_exception=True)
        target_profile_id = discovery.validated_data["profile_version"].pk
        with transaction.atomic():
            locked_profiles = self._lock_draft_profiles((target_profile_id,))
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            locked_target = locked_profiles[target_profile_id]
            if serializer.validated_data["profile_version"].pk != locked_target.pk:
                return _conflict(
                    "model artifact 的目标 profile 已变化，请刷新后重试",
                    code="model_artifact_profile_changed",
                )
            instance = serializer.save(profile_version=locked_target)
            self._record_lifecycle(
                instance,
                action="create",
                reason_default="api.create",
                after=lifecycle_summary(instance),
            )
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        observed = self.get_object()
        observed_profile_id = observed.profile_version_id
        discovery = self.get_serializer(
            observed,
            data=request.data,
            partial=partial,
        )
        discovery.is_valid(raise_exception=True)
        target_profile_id = discovery.validated_data.get(
            "profile_version",
            observed.profile_version,
        ).pk
        with transaction.atomic():
            locked_profiles = self._lock_draft_profiles(
                (observed_profile_id, target_profile_id),
            )
            locked = ModelArtifact.objects.select_for_update().get(pk=observed.pk)
            if locked.profile_version_id != observed_profile_id:
                return _conflict(
                    "model artifact 的 profile 已被并发修改，请刷新后重试",
                    code="model_artifact_profile_changed",
                )
            serializer = self.get_serializer(
                locked,
                data=request.data,
                partial=partial,
            )
            serializer.is_valid(raise_exception=True)
            confirmed_target_id = serializer.validated_data.get(
                "profile_version",
                locked.profile_version,
            ).pk
            if confirmed_target_id != target_profile_id:
                return _conflict(
                    "model artifact 的目标 profile 已变化，请刷新后重试",
                    code="model_artifact_profile_changed",
                )
            before = lifecycle_summary(locked)
            verification_changed = self._verification_input_changed(
                locked,
                serializer.validated_data,
            )
            save_overrides = {
                "profile_version": locked_profiles[confirmed_target_id],
            }
            if verification_changed:
                # This updates only the registered locator/contract.  It never
                # moves or deletes the real model file.  Existing availability
                # rows remain immutable history, while readiness must wait for
                # a fresh attestation of the new declaration.
                save_overrides.update({
                    "status": ModelArtifact.Status.DECLARED,
                    "verified_snapshot": None,
                    "verified_at": None,
                    "verified_by": None,
                })
            instance = serializer.save(**save_overrides)
            self._record_lifecycle(
                instance,
                action="update",
                reason_default="api.update",
                before=before,
                after=lifecycle_summary(instance),
            )
        return Response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    @extend_schema(
        request=ModelArtifactVerificationInputSerializer,
        responses={
            200: ModelArtifactSerializer,
            400: OpenApiResponse(description="模型版本或请求状态不允许核验"),
            404: OpenApiResponse(description="模型文件、快照或可用性事实不存在"),
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def verify(self, request, pk=None):
        serializer = ModelArtifactVerificationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        artifact = self.get_object()
        if artifact.profile_version.status != ModelProfileVersion.Status.PUBLISHED:
            raise ValidationError("只有 published model profile 的文件可用主机快照核验")
        snapshot = get_object_or_404(
            HostCapabilitySnapshot.objects.select_related("host"),
            pk=serializer.validated_data["host_snapshot_id"],
        )
        if snapshot.expires_at is None or snapshot.expires_at <= timezone.now():
            return _conflict("主机能力快照已过期，请重新探测", code="host_snapshot_stale")
        try:
            verify_json_contract(
                snapshot.details,
                schema_version=snapshot.details_schema_version,
                size_bytes=snapshot.details_size_bytes,
                sha256=snapshot.details_sha256,
                max_bytes=_stage_snapshot_max_bytes(),
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        availability = get_object_or_404(
            RuntimeModelAvailability.objects.select_related(
                "deployment__profile_version",
            ),
            snapshot=snapshot,
            artifact=artifact,
        )
        if availability.deployment.profile_version_id != artifact.profile_version_id:
            return _conflict(
                "主机快照中的 deployment 与模型文件版本不一致",
                code="artifact_snapshot_mismatch",
            )
        try:
            verified_artifact_availability(
                snapshot=snapshot,
                deployment=availability.deployment,
                artifact=artifact,
            )
        except DjangoValidationError:
            return _conflict(
                "主机快照缺少与 artifact 声明一致的受信 size/hash 观测；请重新探测",
                code="artifact_observation_unverified",
            )
        with transaction.atomic():
            artifact = ModelArtifact.objects.select_for_update().get(pk=artifact.pk)
            artifact.status = ModelArtifact.Status.AVAILABLE
            artifact.verified_snapshot = snapshot
            artifact.verified_at = timezone.now()
            artifact.verified_by = request.user
            artifact.save(update_fields=(
                "status", "verified_snapshot", "verified_at", "verified_by", "updated_at",
            ))
        return Response(self.get_serializer(artifact).data)


class WorkflowProjectorViewSet(StaffPlatformModelViewSet):
    permission_classes = (IsSuperUser,)
    queryset = WorkflowProjector.objects.all().order_by("key")
    serializer_class = WorkflowProjectorSerializer
    filterset_fields = ("is_active",)
    search_fields = ("key", "name", "description")


class WorkflowProjectorVersionViewSet(
    PlatformLifecycleAuditMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsSuperUser,)
    pagination_class = StandardPageNumberPagination
    serializer_class = WorkflowProjectorVersionSerializer
    queryset = WorkflowProjectorVersion.objects.select_related(
        "projector", "published_by",
    ).order_by("projector__key", "-version")
    filterset_fields = (
        "projector", "status", "is_current", "engine_key",
        "provider_driver_type", "operation", "output_kind",
    )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        projector = serializer.validated_data["projector"]
        try:
            with transaction.atomic():
                locked = WorkflowProjector.objects.select_for_update().get(pk=projector.pk)
                if WorkflowProjectorVersion.objects.filter(projector=locked).exists():
                    raise ValidationError({
                        "projector": "该方案已有版本；请从最新版本创建后继草稿",
                    })
                instance = serializer.save(projector=locked, version=1)
                self._record_lifecycle(
                    instance,
                    action="create",
                    reason_default="api.create",
                    after=lifecycle_summary(instance),
                )
        except IntegrityError as exc:
            raise ValidationError({"projector": "方案版本已被并发创建，请刷新"}) from exc
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status != WorkflowProjectorVersion.Status.DRAFT:
            raise ValidationError("published/retired 工作流合同方案版本不可修改")
        forbidden = {"version", "status", "is_current", "published_at", "published_by"}
        if forbidden & set(request.data):
            raise ValidationError("版本号与生命周期字段只能由服务端动作维护")
        return super().update(request, *args, **kwargs)

    @extend_schema(
        request=None,
        responses={200: WorkflowProjectorOptionsSerializer},
    )
    @action(detail=False, methods=("get",), url_path="options")
    def options(self, request):
        return Response({
            "definition_schema_version": WORKFLOW_PROJECTOR_DEFINITION_SCHEMA_VERSION,
            "engines": workflow_projector_engine_options(),
            **workflow_projector_configuration_options(),
        })

    @extend_schema(
        request=WorkflowProjectorRecommendationInputSerializer,
        responses={
            200: WorkflowProjectorRecommendationSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Build an evidence-bound Generic Projector recommendation from one "
            "already-managed immutable ComfyUI candidate plus fresh object_info. "
            "The response contains only an unsaved draft suggestion and safe graph "
            "catalogs; it never creates or publishes a Projector, submits a prompt, "
            "or returns source literals."
        ),
    )
    @action(detail=False, methods=("post",), url_path="recommendation")
    @security_boundary_resolution_cache()
    def recommendation(self, request):
        serializer = WorkflowProjectorRecommendationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            payload = recommend_workflow_projector_from_candidate(
                **serializer.validated_data,
            )
        except (DjangoValidationError, ProviderDriverError) as exc:
            raise _drf_validation(exc) from exc
        response = Response(WorkflowProjectorRecommendationSerializer(payload).data)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(
        request=WorkflowProjectorVersionCloneSerializer,
        responses={201: WorkflowProjectorVersionSerializer},
    )
    @action(detail=True, methods=("post",), url_path="draft-next")
    def draft_next(self, request, pk=None):
        source = self.get_object()
        input_serializer = WorkflowProjectorVersionCloneSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        overrides = input_serializer.validated_data
        try:
            with transaction.atomic():
                projector = WorkflowProjector.objects.select_for_update().get(
                    pk=source.projector_id,
                )
                latest = WorkflowProjectorVersion.objects.select_for_update().filter(
                    projector=projector,
                ).order_by("-version", "-pk").first()
                if latest is None or latest.pk != source.pk:
                    return _conflict(
                        "源方案版本已不是最高版本，请刷新后从最新版本重试",
                        code="workflow_projector_source_stale",
                    )
                if latest.status == WorkflowProjectorVersion.Status.DRAFT:
                    return _conflict(
                        "当前最高版本已经是草稿，请继续编辑该草稿",
                        code="workflow_projector_draft_exists",
                    )
                payload = {
                    "projector": str(projector.pk),
                    "engine_key": overrides.get("engine_key", latest.engine_key),
                    "engine_version": overrides.get("engine_version", latest.engine_version),
                    "provider_driver_type": overrides.get(
                        "provider_driver_type", latest.provider_driver_type,
                    ),
                    "operation": overrides.get("operation", latest.operation),
                    "output_kind": overrides.get("output_kind", latest.output_kind),
                    "definition": overrides.get("definition", deepcopy(latest.definition)),
                }
                serializer = self.get_serializer(data=payload)
                serializer.is_valid(raise_exception=True)
                instance = serializer.save(version=latest.version + 1)
                self._record_lifecycle(
                    instance,
                    action="create",
                    reason_default="api.create_next_version",
                    after=lifecycle_summary(instance),
                )
        except IntegrityError as exc:
            return _conflict(
                "并发创建方案版本未能完成，请刷新后重试",
                code="workflow_projector_version_conflict",
            )
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: WorkflowProjectorVersionSerializer})
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        with transaction.atomic():
            # Serialize catalog publication so two different families cannot
            # both pass the overlap check against a stale peer snapshot.
            list(
                WorkflowProjector.objects.select_for_update()
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            version = WorkflowProjectorVersion.objects.select_for_update().select_related(
                "projector",
            ).get(pk=self.get_object().pk)
            if version.status != WorkflowProjectorVersion.Status.DRAFT:
                raise ValidationError("只有 draft 工作流合同方案版本可以发布")
            before = lifecycle_summary(version)
            try:
                validate_workflow_projector_definition(
                    version.definition,
                    engine_key=version.engine_key,
                    engine_version=version.engine_version,
                )
                verify_json_contract(
                    version.definition,
                    schema_version=version.definition_schema_version,
                    size_bytes=version.definition_size_bytes,
                    sha256=version.definition_sha256,
                    max_bytes=_stage_snapshot_max_bytes(),
                )
                validate_workflow_projector_publish_conflicts(version)
            except DjangoValidationError as exc:
                raise _drf_validation(exc) from exc
            WorkflowProjectorVersion.objects.select_for_update().filter(
                projector=version.projector,
                is_current=True,
            ).exclude(pk=version.pk).update(is_current=False)
            version.status = WorkflowProjectorVersion.Status.PUBLISHED
            version.is_current = True
            version.published_at = timezone.now()
            version.published_by = request.user
            version.save(update_fields=(
                "status", "is_current", "published_at", "published_by", "updated_at",
            ))
            self._record_lifecycle(
                version,
                action="publish",
                reason_default="api.publish",
                before=before,
                after=lifecycle_summary(version),
            )
        return Response(self.get_serializer(version).data)

    @extend_schema(
        request=None,
        responses={
            204: None,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Discard only the latest, unreferenced draft Projector version. "
            "Published/current/retired versions and historical workflow facts "
            "remain immutable."
        ),
    )
    @action(detail=True, methods=("post",), url_path="discard")
    def discard(self, request, pk=None):
        requested = self.get_object()
        try:
            with transaction.atomic():
                projector = WorkflowProjector.objects.select_for_update().get(
                    pk=requested.projector_id,
                )
                version = get_object_or_404(
                    WorkflowProjectorVersion.objects.select_for_update().select_related(
                        "projector",
                    ),
                    pk=requested.pk,
                )
                if (
                    version.status != WorkflowProjectorVersion.Status.DRAFT
                    or version.is_current
                ):
                    raise ValidationError(
                        "只有未发布且非 current 的 Projector 草稿可以放弃",
                    )
                latest = WorkflowProjectorVersion.objects.select_for_update().filter(
                    projector=projector,
                ).order_by("-version", "-pk").first()
                if latest is None or latest.pk != version.pk:
                    return _conflict(
                        "该草稿已不是方案族的最高版本，不能留下版本缺口",
                        code="workflow_projector_draft_not_latest",
                    )
                if version.contract_drafts.exists():
                    return _conflict(
                        "该草稿已被工作流合同草稿引用，不能放弃",
                        code="workflow_projector_draft_referenced",
                    )
                before = lifecycle_summary(version)
                self._record_lifecycle(
                    version,
                    action="discard",
                    reason_default="api.discard_draft",
                    before=before,
                )
                version.discard()
        except ProtectedError:
            return _conflict(
                "该草稿已被工作流合同草稿引用，不能放弃",
                code="workflow_projector_draft_referenced",
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses={200: WorkflowProjectorVersionSerializer})
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        with transaction.atomic():
            version = WorkflowProjectorVersion.objects.select_for_update().get(
                pk=self.get_object().pk,
            )
            if version.status != WorkflowProjectorVersion.Status.PUBLISHED:
                raise ValidationError("只有 published 工作流合同方案版本可以停用")
            before = lifecycle_summary(version)
            version.status = WorkflowProjectorVersion.Status.RETIRED
            version.is_current = False
            version.save(update_fields=("status", "is_current", "updated_at"))
            self._record_lifecycle(
                version,
                action="retire",
                reason_default="api.retire",
                before=before,
                after=lifecycle_summary(version),
            )
        return Response(self.get_serializer(version).data)


class WorkflowTemplateVersionViewSet(
    ResourceSmokeMixin,
    PlatformLifecycleAuditMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    http_method_names = ("get", "post", "head", "options")
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    queryset = WorkflowTemplateVersion.objects.select_related(
        "template", "provider", "model_profile_version", "published_by",
    ).annotate(
        semantic_binding_count=Count("semantic_bindings", distinct=True),
        model_requirement_count=Count("model_requirements", distinct=True),
    ).order_by("template_id", "-version")
    filterset_fields = (
        "template", "provider", "model_profile_version", "status",
        "validation_state", "smoke_test_state", "is_current",
    )

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == "list":
            # Relations use a bounded batch projection; no graph/contract/evidence
            # or related profile payload belongs in the ordinary list query.
            queryset = queryset.select_related(None).select_related("template").defer(
                "graph", "input_contract", "validation_evidence", "smoke_test_evidence",
                "template__definition", "template__input_schema", "template__description",
            )
        return queryset

    def get_serializer_class(self):
        if self.action == "retrieve":
            return WorkflowTemplateVersionDetailSerializer
        return WorkflowTemplateVersionListSerializer

    def _deployment(self, request):
        serializer = DeploymentActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return get_object_or_404(
            ModelDeployment.objects.select_related("profile_version__provider"),
            pk=serializer.validated_data["deployment_id"],
            status=ModelDeployment.Status.ACTIVE,
        )

    @extend_schema(
        parameters=[WorkflowTemplateCanvasExportQuerySerializer],
        responses={
            200: WorkflowTemplateCanvasExportSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
            503: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Convert one persisted WorkflowTemplateVersion API graph into a read-only, "
            "importable ComfyUI UI canvas. The operation reads object_info but never submits work. "
            "Validated/published versions are pinned to validated_deployment; an unvalidated draft "
            "requires an explicit active ComfyUI deployment_id and never falls back to a default. "
            "Browsers cannot inject the canvas into another ComfyUI page; clients must download and import it."
        ),
    )
    @action(detail=True, methods=("get",), url_path="comfyui-workflow")
    def comfyui_workflow(self, request, pk=None):
        query = WorkflowTemplateCanvasExportQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            version, deployment, deployment_source = (
                resolve_workflow_canvas_export_deployment(
                    self.get_object(),
                    requested_deployment_id=query.validated_data.get("deployment_id"),
                )
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        try:
            object_info = driver_for(deployment.profile_version.provider).invoke(
                deployment,
                "object_info",
                {},
            )
        except (ProviderDriverError, DjangoValidationError):
            return api_error_response(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="comfyui_object_info_unavailable",
                details={
                    "detail": (
                        "无法读取 ComfyUI /object_info，暂时不能把已保存 API 图转换为 UI 画布；"
                        "已停止在该 workflow 绑定的 deployment，未回退默认实例，也未提交生产任务。"
                    ),
                    "missing_keys": [],
                },
            )
        try:
            workflow = api_graph_to_ui_workflow(
                version.graph,
                object_info,
                workflow_id=version.id,
                metadata={
                    "workflow_version_id": str(version.id),
                    "template_slug": version.template.slug,
                    "template_version": version.version,
                },
            )
        except ComfyUICanvasExportError as exc:
            raise ValidationError({"workflow": str(exc)}) from exc
        if not is_comfyui_ui_workflow(workflow):
            raise ValidationError({"workflow": "转换结果不是有效的 ComfyUI UI 画布"})

        filename = (
            f"autoprompt-workflow-{version.template.slug}-v{version.version}-comfyui-ui.json"
        )
        if query.validated_data["download"]:
            response = HttpResponse(
                json.dumps(workflow, ensure_ascii=False, indent=2),
                content_type="application/json; charset=utf-8",
            )
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            response["X-AutoPrompt-Deployment-Id"] = str(deployment.id)
            return response

        payload = {
            "workflow_version_id": version.id,
            "format": "comfyui_ui",
            "filename": filename,
            "graph_sha256": version.graph_sha256,
            "template_slug": version.template.slug,
            "template_version": version.version,
            "deployment_id": deployment.id,
            "deployment_name": deployment.name,
            "deployment_source": deployment_source,
            "workflow": workflow,
            "import_ready": True,
            "open_reason": (
                f"已使用 deployment“{deployment.name}”读取节点目录并生成 UI 画布；"
                "请下载后拖入 ComfyUI，或从 Workflow 菜单打开。"
            ),
            "direct_open_supported": False,
            "direct_open_url": None,
            "open_mode": "download_import",
            "download_url": request.build_absolute_uri(
                f"{request.path}?{urlencode({'download': 'true', 'deployment_id': str(deployment.id)})}",
            ),
        }
        response = Response(WorkflowTemplateCanvasExportSerializer(payload).data)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    @extend_schema(
        request=DeploymentActionSerializer,
        responses={
            200: WorkflowValidationResponseSerializer,
            400: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def validate(self, request, pk=None):
        version = self.get_object()
        deployment = self._deployment(request)
        try:
            version, report = validate_workflow_version(
                version,
                deployment=deployment,
            )
        except WorkflowModelEvidenceUnavailable as exc:
            return api_error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=exc.reason_code,
                details=_workflow_model_evidence_recovery(
                    exc,
                    version=version,
                    deployment=deployment,
                ),
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response({
            "version": WorkflowTemplateVersionDetailSerializer(self.get_object()).data,
            "contract_report": report,
        })

    @extend_schema(request=None, responses={
        200: WorkflowTemplateVersionDetailSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        try:
            with transaction.atomic():
                current = self.get_object()
                before = lifecycle_summary(current)
                version = publish_workflow_version(current, published_by=request.user)
                self._record_lifecycle(
                    version,
                    action="publish",
                    reason_default="api.publish",
                    before=before,
                    after=lifecycle_summary(version),
                )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(WorkflowTemplateVersionDetailSerializer(self.get_object()).data)

    @extend_schema(request=None, responses={
        200: WorkflowTemplateVersionDetailSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        with transaction.atomic():
            version = WorkflowTemplateVersion.objects.select_for_update().get(
                pk=self.get_object().pk,
            )
            before = lifecycle_summary(version)
            if version.status != WorkflowTemplateVersion.Status.PUBLISHED:
                raise ValidationError("只有 published workflow version 可以退役")
            if version.execution_versions.filter(status="published", instance__is_active=True).exists():
                return _conflict("仍有启用且已发布的执行实例版本引用该工作流，请先调整这些执行实例")
            version.status = WorkflowTemplateVersion.Status.RETIRED
            version.is_current = False
            version.save(update_fields=("status", "is_current", "updated_at"))
            self._record_lifecycle(
                version,
                action="retire",
                reason_default="api.retire",
                before=before,
                after=lifecycle_summary(version),
            )
        return Response(WorkflowTemplateVersionDetailSerializer(self.get_object()).data)


class ConfigDefinitionViewSet(StaffPlatformModelViewSet):
    queryset = ConfigDefinition.objects.annotate(
        config_value_count=Count("values", distinct=True),
    )
    serializer_class = ConfigDefinitionSerializer
    filterset_fields = ("domain", "value_type", "is_required", "is_secret", "is_active")
    search_fields = ("key", "description")

    @extend_schema(
        request=ConfigDefinitionCreateInputSerializer,
        responses={201: ConfigDefinitionSerializer},
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        defaults = config_definition_system_defaults()
        serializer.validated_data["is_active"] = defaults["is_active"]
        serializer.validated_data["needs_review"] = defaults["needs_review"]
        serializer.validated_data["review_reason"] = ""
        super().perform_create(serializer)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        # Config revisions use the same advisory guard.  Hold it across row
        # locking, serializer validation and save so a first ConfigValue cannot
        # appear between the immutable-contract check and the update.
        with registry_reference_write_guard():
            visible = self.get_object()
            definition = ConfigDefinition.objects.select_for_update().get(
                pk=visible.pk,
            )
            serializer = self.get_serializer(
                definition,
                data=request.data,
                partial=partial,
            )
            serializer.is_valid(raise_exception=True)
            self.perform_update(serializer)
            if getattr(definition, "_prefetched_objects_cache", None):
                definition._prefetched_objects_cache = {}
            return Response(serializer.data)

    @extend_schema(request=None, responses=ConfigDefinitionOptionsSerializer)
    @action(detail=False, methods=("get",), url_path="options")
    def registry_options(self, request):
        definitions = list(ConfigDefinition.objects.all().only(
            "domain", "unit", "impact_modules",
        ))
        domains = sorted({item.domain for item in definitions if item.domain})
        units = sorted({item.unit for item in definitions if item.unit})
        modules = sorted({
            str(module).strip()
            for item in definitions
            for module in (item.impact_modules if isinstance(item.impact_modules, list) else [])
            if str(module).strip()
        })
        data = {
            "configuration_classes": [
                {"value": value, "label": label}
                for value, label in ConfigDefinition.ConfigurationClass.choices
            ],
            "value_types": [
                {"value": value, "label": label}
                for value, label in ConfigDefinition.ValueType.choices
            ],
            "scopes": [
                {"value": value, "label": label}
                for value, label in ConfigRevision.ScopeType.choices
            ],
            "default_sources": [
                {"value": value, "label": label}
                for value, label in CONFIG_DEFAULT_SOURCE_CHOICES
            ],
            "domains": [{"value": value, "label": value} for value in domains],
            "units": [{"value": value, "label": value} for value in units],
            "impact_modules": [
                {"value": value, "label": value} for value in modules
            ],
            "custom_entry_fields": ["domain", "unit", "impact_modules"],
            "system_defaults": config_definition_system_defaults(),
        }
        return Response(ConfigDefinitionOptionsSerializer(data).data)

    @extend_schema(responses={
        200: ConfigResourceOptionSerializer(many=True),
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("get",), url_path="resource-options")
    def resource_options(self, request, pk=None):
        definition = self.get_object()
        try:
            candidates = config_resource_reference_candidates(definition)
            page = self.paginate_queryset(candidates)
            rows = config_resource_reference_options(definition, page)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return self.get_paginated_response(ConfigResourceOptionSerializer(rows, many=True).data)

class ConfigRevisionViewSet(
    PlatformLifecycleAuditMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    queryset = ConfigRevision.objects.select_related(
        "project", "run", "created_by",
    ).prefetch_related("values__definition")
    filterset_fields = ("scope_type", "status", "project", "run")

    def get_serializer_class(self):
        return ConfigRevisionCreateSerializer if self.action == "create" else ConfigRevisionSerializer

    @extend_schema(
        request=ConfigRevisionCreateSerializer,
        responses={201: ConfigRevisionSerializer},
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            requested_publish = serializer.validated_data.get("publish", False)
            with transaction.atomic():
                revision = serializer.save()
                self._record_lifecycle(
                    revision,
                    action="create",
                    reason_default="api.create",
                    after=lifecycle_summary(revision),
                )
                if requested_publish:
                    self._record_lifecycle(
                        revision,
                        action="publish",
                        reason_default="api.publish",
                        before={
                            **lifecycle_summary(revision),
                            "status": ConfigRevision.Status.DRAFT,
                            "activation_state": ConfigRevision.ActivationState.INACTIVE,
                            "published_at": None,
                            "activated_at": None,
                        },
                        after=lifecycle_summary(revision),
                    )
        except ConfigurationResolutionError as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
            )
        return Response(
            ConfigRevisionSerializer(revision).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(request=None, responses={
        200: ConfigRevisionSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        try:
            with transaction.atomic():
                current = self.get_object()
                before = lifecycle_summary(current)
                revision = publish_config_revision(current, published_by=request.user)
                self._record_lifecycle(
                    revision,
                    action="publish",
                    reason_default="api.publish",
                    before=before,
                    after=lifecycle_summary(revision),
                )
        except ConfigurationResolutionError as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ConfigRevisionSerializer(revision).data)

    @extend_schema(request=None, responses={
        200: ConfigRevisionSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",))
    def activate(self, request, pk=None):
        try:
            with transaction.atomic():
                current = self.get_object()
                before = lifecycle_summary(current)
                revision = activate_config_revision(
                    current,
                    activated_by=request.user,
                )
                self._record_lifecycle(
                    revision,
                    action="activate",
                    reason_default="api.activate",
                    before=before,
                    after=lifecycle_summary(revision),
                )
        except (DjangoValidationError, ConfigurationResolutionError) as exc:
            if isinstance(exc, ConfigurationResolutionError):
                return api_error_response(
                    status_code=status.HTTP_409_CONFLICT,
                    code=exc.code,
                    details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
                )
            raise _drf_validation(exc) from exc
        return Response(ConfigRevisionSerializer(revision).data)

    @extend_schema(
        parameters=[
            OpenApiParameter("project_id", OpenApiTypes.UUID, OpenApiParameter.QUERY),
            OpenApiParameter("run_id", OpenApiTypes.UUID, OpenApiParameter.QUERY),
        ],
        responses={
            200: ResolvedConfigurationResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=False, methods=("get",))
    def resolved(self, request):
        project = None
        run = None
        if request.query_params.get("project_id"):
            project = get_object_or_404(SeriesProject, pk=request.query_params["project_id"])
        if request.query_params.get("run_id"):
            run = get_object_or_404(ProductionRun, pk=request.query_params["run_id"])
        try:
            resolved = resolve_configuration(project=project, run=run)
        except ConfigurationResolutionError as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
            )
        return Response(redact_configuration_snapshot(resolved.snapshot()))


class ProjectConfigView(APIView):
    """Owner-facing project override endpoint; secrets remain admin/deployment concerns."""

    permission_classes = (IsAuthenticated,)

    def _project(self, request, project_id):
        queryset = SeriesProject.objects.all()
        if not request.user.is_staff:
            queryset = queryset.filter(owner=request.user)
        return get_object_or_404(queryset, pk=project_id)

    @staticmethod
    def _definitions():
        return {
            item.key: item
            for item in ConfigDefinition.objects.filter(
                is_active=True,
                is_secret=False,
                restart_required=False,
            )
            if ConfigRevision.ScopeType.PROJECT in {
                value.strip() for value in str(item.allowed_scopes or "").split(",")
            }
        }

    def _effective(self, project):
        resolved = resolve_configuration(project=project)
        allowed = self._definitions()
        snapshot = resolved.snapshot()
        snapshot["values"] = {
            key: value for key, value in snapshot["values"].items() if key in allowed
        }
        snapshot["sources"] = {
            key: {
                field: value[field]
                for field in ("source", "scope", "source_type", "version", "revision")
                if field in value
            }
            for key, value in snapshot["sources"].items()
            if key in allowed and isinstance(value, dict)
        }
        snapshot.pop("revision_ids", None)
        snapshot["sha256"] = json_contract({
            "values": snapshot["values"],
            "sources": snapshot["sources"],
        }, max_bytes=_stage_snapshot_max_bytes()).sha256
        return snapshot

    @extend_schema(
        responses={
            200: ProjectEffectiveConfigurationResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    def get(self, request, project_id):
        project = self._project(request, project_id)
        try:
            return Response(self._effective(project))
        except ConfigurationResolutionError as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
            )

    def _write(self, request, project_id, *, replace: bool):
        project = self._project(request, project_id)
        values = request.data.get("values", {})
        unset_keys = request.data.get("unset_keys", [])
        if not isinstance(values, dict):
            raise ValidationError({"values": "必须是对象"})
        if not isinstance(unset_keys, list) or any(not isinstance(item, str) for item in unset_keys):
            raise ValidationError({"unset_keys": "必须是字符串数组"})
        if replace and unset_keys:
            raise ValidationError({"unset_keys": "PUT 是全量替换，不接受 unset_keys"})
        if not replace and not values and not unset_keys:
            raise ValidationError("PATCH 至少需要一个 values 或 unset_keys 变更")
        allowed = self._definitions()
        rejected = sorted((set(values) | set(unset_keys)) - set(allowed))
        if rejected:
            secret_keys = set(ConfigDefinition.objects.filter(
                key__in=rejected,
                is_secret=True,
            ).values_list("key", flat=True))
            detail = {
                key: (
                    "项目配置 API 不接受密钥；请由超级管理员绑定受管凭据"
                    if key in secret_keys else
                    "配置不存在、已停用或不允许 project scope"
                )
                for key in rejected
            }
            raise ValidationError({"values": detail})
        try:
            revision = create_config_revision(
                scope_type=ConfigRevision.ScopeType.PROJECT,
                project=project,
                run=None,
                values=values,
                unset_keys=unset_keys,
                created_by=request.user,
                change_summary=str(request.data.get("change_summary") or "")[:500],
                clone_published=not replace,
            )
            revision = publish_config_revision(revision, published_by=request.user)
            effective = self._effective(project)
        except ConfigurationResolutionError as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response({
            "revision": ConfigRevisionSerializer(revision).data,
            "effective": effective,
        })

    @extend_schema(
        request=ProjectConfigWriteSerializer,
        responses=ProjectConfigResponseSerializer,
    )
    def put(self, request, project_id):
        return self._write(request, project_id, replace=True)

    @extend_schema(
        request=ProjectConfigWriteSerializer,
        responses=ProjectConfigResponseSerializer,
    )
    def patch(self, request, project_id):
        return self._write(request, project_id, replace=False)


class RuntimeHostViewSet(PlatformLifecycleAuditMixin, viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    queryset = RuntimeHost.objects.all()
    serializer_class = RuntimeHostSerializer

    @extend_schema(
        request=None,
        responses={
            202: OpenApiResponse(
                response=RuntimeHostProbeAttemptSerializer,
                description=(
                    "Durable probe attempt accepted or existing active attempt reused. "
                    "Location identifies the attempt detail resource."
                ),
            ),
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def probe(self, request, pk=None):
        host = self.get_object()
        if host.status == RuntimeHost.Status.DISABLED:
            return _conflict("已禁用主机不能执行探测；请先启用", code="runtime_host_disabled")
        try:
            attempt, _created = enqueue_runtime_host_probe(
                host,
                created_by=request.user,
                trigger=RuntimeHostProbeAttempt.Trigger.MANUAL,
            )
        except DjangoValidationError as exc:
            return _conflict(str(exc), code="runtime_host_state_changed")
        response = Response(
            RuntimeHostProbeAttemptSerializer(attempt).data,
            status=status.HTTP_202_ACCEPTED,
        )
        response["Location"] = reverse(
            "runtime-host-probe-attempt-detail",
            kwargs={"pk": attempt.pk},
            request=request,
        )
        return response

    @extend_schema(responses=HostCapabilitySnapshotSerializer)
    @action(detail=True, methods=("get",), url_path="latest-snapshot")
    def latest_snapshot(self, request, pk=None):
        snapshot = self._snapshot(request, self.get_object())
        return Response(HostCapabilitySnapshotSerializer(snapshot).data)

    @extend_schema(request=RuntimeHostStatusSerializer, responses={
        200: RuntimeHostSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",))
    def status(self, request, pk=None):
        serializer = RuntimeHostStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_active = serializer.validated_data["is_active"]
        with transaction.atomic():
            host = RuntimeHost.objects.select_for_update().get(pk=self.get_object().pk)
            before = lifecycle_summary(host)
            if not requested_active and host.model_deployments.filter(
                status=ModelDeployment.Status.ACTIVE,
            ).exists():
                return _conflict("仍有 active deployment 绑定该主机，请先停用部署")
            next_status = (
                host.status
                if requested_active and host.status != RuntimeHost.Status.DISABLED
                else RuntimeHost.Status.UNAVAILABLE
                if requested_active
                else RuntimeHost.Status.DISABLED
            )
            if host.status != next_status:
                host.status = next_status
                host.save(update_fields=("status", "updated_at"))
            self._record_lifecycle(
                host,
                action="status",
                reason_default="api.status",
                before=before,
                after=lifecycle_summary(host),
            )
        return Response(RuntimeHostSerializer(host).data)

    def _snapshot(self, request, host):
        snapshot_id = request.data.get("snapshot_id") or request.query_params.get("snapshot_id")
        queryset = HostCapabilitySnapshot.objects.filter(host=host).select_related("host").prefetch_related(
            "model_availability",
        )
        if snapshot_id:
            return get_object_or_404(queryset, pk=snapshot_id)
        snapshot = queryset.order_by("-captured_at").first()
        if snapshot is None:
            raise ValidationError("尚无主机能力快照，请先 probe")
        return snapshot

    @extend_schema(
        request=RuntimeRecommendationInputSerializer,
        responses={
            200: RecommendationSnapshotSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def recommend(self, request, pk=None):
        snapshot = self._snapshot(request, self.get_object())
        project = None
        if request.data.get("project_id"):
            project = get_object_or_404(SeriesProject, pk=request.data["project_id"])
        try:
            recommendation = recommend_for_host(
                snapshot,
                project=project,
                created_by=request.user,
            )
        except (ConfigurationResolutionError, DjangoValidationError) as exc:
            if isinstance(exc, ConfigurationResolutionError):
                return api_error_response(
                    status_code=status.HTTP_409_CONFLICT,
                    code=exc.code,
                    details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
                )
            raise _drf_validation(exc) from exc
        return Response(RecommendationSnapshotSerializer(recommendation).data)

    @extend_schema(
        parameters=[
            OpenApiParameter("snapshot_id", OpenApiTypes.UUID, OpenApiParameter.QUERY),
        ],
        responses=RuntimeReadinessResponseSerializer,
    )
    @action(detail=True, methods=("get",))
    def readiness(self, request, pk=None):
        return Response(capability_readiness(self._snapshot(request, self.get_object())))

    @extend_schema(
        parameters=[NodeMetadataQuerySerializer],
        responses=NodeMetadataResponseSerializer,
    )
    @action(detail=False, methods=("get",), url_path="node-metadata")
    def node_metadata_action(self, request):
        query = NodeMetadataQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        deployment = get_object_or_404(
            ModelDeployment.objects.select_related("profile_version__provider"),
            pk=query.validated_data["deployment_id"],
            status=ModelDeployment.Status.ACTIVE,
        )
        try:
            payload = node_metadata(
                deployment=deployment,
                search=query.validated_data["search"],
                offset=query.validated_data["offset"],
                limit=query.validated_data.get("limit"),
            )
        except (DjangoValidationError, ProviderDriverError) as exc:
            raise _drf_validation(exc) from exc
        return Response(payload)


class RuntimeHostProbeAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    serializer_class = RuntimeHostProbeAttemptSerializer
    queryset = RuntimeHostProbeAttempt.objects.select_related(
        "host", "created_by", "final_snapshot",
    ).order_by("-created_at")
    filterset_fields = ("host", "status", "trigger", "probe_kind")

    @extend_schema(
        request=None,
        responses={202: OpenApiResponse(
            response=RuntimeHostProbeAttemptSerializer,
            description="Cancellation persisted; terminal attempts are returned idempotently.",
        )},
    )
    @action(detail=True, methods=("post",), url_path="cancel")
    def cancel(self, request, pk=None):
        attempt = request_runtime_host_probe_cancel(self.get_object())
        return Response(
            self.get_serializer(attempt).data,
            status=status.HTTP_202_ACCEPTED,
        )


class RecommendationSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    serializer_class = RecommendationSnapshotSerializer
    queryset = RecommendationSnapshot.objects.select_related(
        "host_snapshot__host", "project", "config_revision", "created_by",
    ).prefetch_related(
        "candidates__deployment", "candidates__workflow_version", "decisions",
    )
    filterset_fields = ("host_snapshot", "project", "status")

    @extend_schema(
        request=RecommendationDecisionInputSerializer,
        responses={
            201: RecommendationDecisionSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def decide(self, request, pk=None):
        serializer = RecommendationDecisionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        recommendation = self.get_object()
        candidate = None
        if data.get("candidate_id"):
            candidate = get_object_or_404(
                RecommendationCandidate.objects.select_related(
                    "deployment", "workflow_version",
                ),
                pk=data["candidate_id"],
                recommendation_snapshot=recommendation,
            )
        project = None
        if data.get("project_id"):
            project = get_object_or_404(SeriesProject, pk=data["project_id"])
        try:
            decision = decide_recommendation(
                recommendation,
                candidate=candidate,
                decision=data["decision"],
                scope_type=data["scope_type"],
                project=project,
                override_values=data["override_values"],
                reason=data.get("reason", ""),
                decided_by=request.user,
            )
        except RecommendationStale as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code="recommendation_stale",
                details={"detail": str(exc)},
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(
            RecommendationDecisionSerializer(decision).data,
            status=status.HTTP_201_CREATED,
        )


class ScriptSourceViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    serializer_class = ScriptSourceSerializer
    filterset_fields = ("project", "source_type", "source_format", "status")
    queryset = ScriptSource.objects.select_related("project", "owner")

    def get_queryset(self):
        queryset = self.queryset
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)

    @extend_schema(
        request=ScriptSourceCreateInputSerializer,
        responses={201: ScriptSourceCreateResponseSerializer},
    )
    def create(self, request, *args, **kwargs):
        input_serializer = ScriptSourceCreateInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        input_data = input_serializer.validated_data
        project = get_object_or_404(
            SeriesProject.objects.filter(owner=request.user) if not request.user.is_staff else SeriesProject.objects.all(),
            pk=input_data["project_id"],
        )
        try:
            if input_data.get("asset_id") is not None:
                assets = Asset.objects.filter(is_active=True)
                if not request.user.is_staff:
                    assets = assets.filter(
                        Q(owner=request.user) | Q(owner__isnull=True),
                    )
                source, revision = create_script_source_from_asset(
                    project=project,
                    user=request.user,
                    asset=get_object_or_404(assets, pk=input_data["asset_id"]),
                )
            elif input_data.get("file") is not None:
                source, revision = create_uploaded_script_source(
                    project=project,
                    user=request.user,
                    uploaded_file=input_data["file"],
                )
            else:
                source, revision = create_pasted_script_source(
                    project=project,
                    user=request.user,
                    content=input_data["content"],
                    source_format=str(input_data.get("source_format") or ScriptSource.SourceFormat.PLAIN),
                    source_type=ScriptSource.SourceType.PASTE,
                )
        except ScriptSourceValidationError as exc:
            payload = {"detail": str(exc)}
            if exc.source is not None:
                payload["source"] = ScriptSourceSerializer(exc.source).data
            return api_error_response(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code=exc.code,
                details=payload,
            )
        except ConfigurationResolutionError as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
            )
        return Response({
            "source": ScriptSourceSerializer(source).data,
            "revision": ScriptRevisionDetailSerializer(revision).data,
        }, status=status.HTTP_201_CREATED)


class ScriptRevisionViewSet(
    ScriptRevisionStructureActions,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    filterset_fields = ("source", "status")
    queryset = ScriptRevision.objects.select_related("source__project", "source__owner")

    def get_queryset(self):
        queryset = self.queryset
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(source__owner=self.request.user)

    def get_serializer_class(self):
        return ScriptRevisionDetailSerializer if self.action == "retrieve" else ScriptRevisionListSerializer

    @extend_schema(
        request=ScriptRevisionCreateInputSerializer,
        responses={201: ScriptRevisionDetailSerializer},
    )
    def create(self, request, *args, **kwargs):
        input_serializer = ScriptRevisionCreateInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        input_data = input_serializer.validated_data
        sources = ScriptSource.objects.all()
        if not request.user.is_staff:
            sources = sources.filter(owner=request.user)
        source_obj = get_object_or_404(sources, pk=input_data["source_id"])
        try:
            if "structure_basis" in input_data:
                from studio.services.script_revision_structures import revise_structured_script
                from studio.services.execution_instances import execution_configuration_max_bytes
                revision = revise_structured_script(source=source_obj, user=request.user, content=input_data["content"],
                    basis=input_data["structure_basis"], max_bytes=execution_configuration_max_bytes())
            else:
                revision = create_script_revision(source=source_obj, user=request.user, content=input_data["content"])
        except (DjangoValidationError, ConfigurationResolutionError) as exc:
            if isinstance(exc, ConfigurationResolutionError):
                return api_error_response(
                    status_code=status.HTTP_409_CONFLICT,
                    code=exc.code,
                    details={"detail": str(exc)},
                )
            raise _drf_validation(exc) from exc
        return Response(ScriptRevisionDetailSerializer(revision).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=ScriptRevisionDetailSerializer)
    @action(detail=True, methods=("post",))
    def confirm(self, request, pk=None):
        try:
            revision = confirm_script_revision(self.get_object(), user=request.user)
        except ScriptSourceValidationError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(ScriptRevisionDetailSerializer(revision).data)


class ProductionRecipeVersionViewSet(StaffPlatformModelViewSet):
    queryset = ProductionRecipeVersion.objects.select_related("published_by")
    serializer_class = ProductionRecipeVersionSerializer
    permission_classes = (IsStaffOrReadOnly,)
    filterset_fields = ("recipe_key", "status")

    def perform_create(self, serializer):
        with registry_reference_write_guard():
            recipe_key = serializer.validated_data["recipe_key"]
            if ProductionRecipeVersion.objects.filter(recipe_key=recipe_key).exists():
                raise ValidationError({
                    "recipe_key": (
                        "该 Recipe Key 已有历史版本；请从最新不可变版本创建后继草稿"
                    ),
                })
            try:
                from studio.services.production_recipe_contracts import validate_recipe_definition
                validate_recipe_definition(
                    serializer.validated_data.get("definition", {}),
                )
            except DjangoValidationError as exc:
                raise _drf_validation(exc) from exc
            serializer.validated_data["version"] = 1
            super().perform_create(serializer)

    def perform_update(self, serializer):
        with registry_reference_write_guard():
            definition = serializer.validated_data.get(
                "definition", serializer.instance.definition,
            )
            try:
                from studio.services.production_recipe_contracts import validate_recipe_definition
                validate_recipe_definition(definition)
            except DjangoValidationError as exc:
                raise _drf_validation(exc) from exc
            super().perform_update(serializer)

    def get_queryset(self):
        queryset = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        if not self.request.user.is_staff:
            queryset = queryset.filter(status=ProductionRecipeVersion.Status.PUBLISHED)
        return queryset

    def update(self, request, *args, **kwargs):
        if "version" in request.data:
            raise ValidationError({
                "version": "版本号由服务端分配，不能通过普通编辑修改",
            })
        with registry_reference_write_guard():
            recipe = ProductionRecipeVersion.objects.select_for_update().get(
                pk=self.get_object().pk,
            )
            if recipe.status in {
                ProductionRecipeVersion.Status.PUBLISHED,
                ProductionRecipeVersion.Status.RETIRED,
            }:
                raise ValidationError("published/retired recipe 不可修改；请创建新版本")
            return super().update(request, *args, **kwargs)

    @extend_schema(request=None, responses=ProductionRecipeEditorOptionsSerializer)
    @action(detail=False, methods=("get",), url_path="options")
    def editor_options(self, request):
        return Response(ProductionRecipeEditorOptionsSerializer(
            production_recipe_editor_options(),
        ).data)

    @extend_schema(request=ProductionRecipeSourceOptionsInputSerializer, responses={
        200: ProductionRecipeSourceOptionsSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
    })
    @action(detail=False, methods=("post",), url_path="source-options")
    def source_options(self, request):
        from studio.services.execution_text_output_catalog import recipe_text_source_options
        data = ProductionRecipeSourceOptionsInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            options = recipe_text_source_options(data.validated_data["definition"])
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRecipeSourceOptionsSerializer({"text_output_options": options}).data)

    @extend_schema(
        request=ProductionRecipeCloneSuccessorInputSerializer,
        responses={
            201: ProductionRecipeVersionSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",), url_path="clone-successor")
    def clone_successor(self, request, pk=None):
        input_serializer = ProductionRecipeCloneSuccessorInputSerializer(
            data=request.data,
        )
        input_serializer.is_valid(raise_exception=True)
        source_id = self.get_object().pk
        expected_hash = input_serializer.validated_data[
            "expected_definition_sha256"
        ]
        with registry_reference_write_guard():
            source = ProductionRecipeVersion.objects.get(pk=source_id)
            family = list(
                ProductionRecipeVersion.objects.select_for_update()
                .filter(recipe_key=source.recipe_key)
                .order_by("version", "pk")
            )
            locked_source = next(
                (item for item in family if item.pk == source_id),
                None,
            )
            if (
                locked_source is None
                or locked_source.definition_sha256 != expected_hash
            ):
                return _conflict(
                    "来源配方已变化；请刷新并重新确认精确版本与 SHA-256",
                    code="recipe_successor_source_stale",
                )
            try:
                verify_json_contract(
                    locked_source.definition,
                    schema_version=locked_source.definition_schema_version,
                    size_bytes=locked_source.definition_size_bytes,
                    sha256=locked_source.definition_sha256,
                    max_bytes=boundary_int("security.snapshot.stage_max_bytes"),
                )
            except DjangoValidationError:
                return _conflict(
                    "来源配方正文与持久化摘要不一致；请先修复合同再创建后继草稿",
                    code="recipe_successor_source_contract_invalid",
                )
            if locked_source.status not in {
                ProductionRecipeVersion.Status.PUBLISHED,
                ProductionRecipeVersion.Status.RETIRED,
            }:
                raise ValidationError("只能从 published/retired 配方创建后继草稿")
            latest = family[-1]
            if latest.pk != locked_source.pk:
                if latest.status == ProductionRecipeVersion.Status.DRAFT:
                    return _conflict(
                        f"该配方已存在 v{latest.version} 草稿；请继续编辑现有草稿",
                        code="recipe_successor_draft_exists",
                    )
                return _conflict(
                    f"来源不是最新版本；请从 v{latest.version} 创建后继草稿",
                    code="recipe_successor_source_not_latest",
                )
            from studio.services.production_recipe_contracts import verify_recipe_successor_references
            try:
                verify_recipe_successor_references(locked_source.definition)
            except DjangoValidationError as exc:
                raise _drf_validation(exc) from exc
            # This exact, immutable copy is not an ordinary authoring save.
            # Current contracts are checked when the draft is edited/published.
            target = ProductionRecipeVersion.objects.create(
                recipe_key=locked_source.recipe_key,
                name=locked_source.name,
                version=latest.version + 1,
                definition=deepcopy(locked_source.definition),
                definition_schema_version=locked_source.definition_schema_version,
                definition_size_bytes=locked_source.definition_size_bytes,
                definition_sha256=locked_source.definition_sha256,
            )
            self._record_lifecycle(
                target,
                action="clone_successor",
                reason_default="api.clone_successor",
                after=lifecycle_summary(target),
            )
        return Response(
            self.get_serializer(target).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(request=None, responses={
        200: ProductionRecipeVersionSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",), permission_classes=(IsAdminUser,))
    def publish(self, request, pk=None):
        with registry_reference_write_guard():
            recipe = ProductionRecipeVersion.objects.select_for_update().get(pk=self.get_object().pk)
            before = lifecycle_summary(recipe)
            if recipe.status != ProductionRecipeVersion.Status.DRAFT:
                raise ValidationError("只有 draft recipe 可以发布")
            try:
                validate_production_recipe_executability(recipe)
            except DjangoValidationError as exc:
                raise _drf_validation(exc) from exc
            recipe.status = ProductionRecipeVersion.Status.PUBLISHED
            recipe.published_at = timezone.now()
            recipe.published_by = request.user
            recipe.save(update_fields=("status", "published_at", "published_by", "updated_at"))
            self._record_lifecycle(
                recipe,
                action="publish",
                reason_default="api.publish",
                before=before,
                after=lifecycle_summary(recipe),
            )
        return Response(self.get_serializer(recipe).data)

    @extend_schema(request=None, responses={
        200: ProductionRecipeVersionSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",), permission_classes=(IsAdminUser,))
    def retire(self, request, pk=None):
        with registry_reference_write_guard():
            recipe = ProductionRecipeVersion.objects.select_for_update().get(
                pk=self.get_object().pk,
            )
            before = lifecycle_summary(recipe)
            if recipe.status != ProductionRecipeVersion.Status.PUBLISHED:
                raise ValidationError("只有 published recipe 可以退役")
            recipe.status = ProductionRecipeVersion.Status.RETIRED
            recipe.save(update_fields=("status", "updated_at"))
            self._record_lifecycle(
                recipe,
                action="retire",
                reason_default="api.retire",
                before=before,
                after=lifecycle_summary(recipe),
            )
        return Response(self.get_serializer(recipe).data)


class ProductionRunViewSet(
    TaskDistributionRecoveryMixin,
    ProductionMaterialFulfillmentActions,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    filterset_fields = ("project", "status", "recipe_version")
    queryset = ProductionRun.objects.select_related(
        "project", "owner", "script_revision", "recipe_version", "config_revision", "release_bundle",
    ).prefetch_related(
        "stages__approval_decisions", "stages__artifact_selection_decisions",
        "stages__route_events", "artifacts",
    )

    @extend_schema(responses={
        200: ProductionRunDetailSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
    })
    @security_boundary_resolution_cache()
    def retrieve(self, request, *args, **kwargs):
        # One response shares current boundary resolutions across all stages;
        # the next read resolves the active configuration again.
        try:
            return super().retrieve(request, *args, **kwargs)
        except DjangoValidationError as exc:
            # Stored contract failures remain explicit. Never reconstruct a
            # historical plan or turn its rejection into a generic HTTP 500.
            raise _drf_validation(exc) from exc

    def get_queryset(self):
        queryset = self.queryset
        if self.action == "list":
            queryset = queryset.select_related(None).prefetch_related(None).select_related("project", "release_bundle")
        if self.action in ("distribution_recovery", "retry_distribution", "targets", "reference_mapping_preview", "manual_action_options", "manual_script_structure", "material_selection_context", "select_artifact", "material_fulfillment_context", "fulfill_material", "manual_shot_references", "recipe_shot_references", "storyboard_reference_selection"):
            queryset = queryset.select_related(None).prefetch_related(None).only("id", "owner_id", "status")
        if self.action == "retry_preview":
            queryset = queryset.select_related(None).prefetch_related(None).only(
                "id", "plan_source", "current_stage_key", "status", "blocked_code")
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        return (
            queryset
            if self.request.user.is_staff
            else queryset.filter(project__owner=self.request.user)
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ProductionRunCreateSerializer
        if self.action == "retrieve":
            return ProductionRunDetailSerializer
        return ProductionRunListSerializer

    @extend_schema(responses=ProductionBudgetOptionsSerializer)
    @action(detail=False, methods=("get",), url_path="budget-options")
    def budget_options(self, request):
        from studio.services.production_budget_configuration import budget_currency_options
        try:
            return Response(budget_currency_options())
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc

    @extend_schema(
        parameters=[OpenApiParameter("project_id", OpenApiTypes.UUID, OpenApiParameter.QUERY, required=True)],
        responses={200: ProductionManualOptionsSerializer},
    )
    @action(detail=False, methods=("get",), url_path="manual-options")
    def manual_options(self, request):
        from studio.services.production_manual_creation import manual_production_options
        raw = request.query_params.get("project_id")
        try:
            project_id = uuid.UUID(raw) if raw else None
        except ValueError:
            project_id = None
        if project_id is None or set(request.query_params) != {"project_id"}:
            raise ValidationError({"project_id": "必须且只能提供明确的项目 UUID"})
        projects = SeriesProject.objects.all() if request.user.is_staff else SeriesProject.objects.filter(owner=request.user)
        project = get_object_or_404(projects, pk=project_id)
        return Response(manual_production_options(project))

    @extend_schema(responses=ProductionManualActionOptionsSerializer)
    @action(detail=True, methods=("get",), url_path="manual-action-options")
    def manual_action_options(self, request, pk=None):
        from studio.services.production_manual_actions import manual_action_options
        try:
            return Response(manual_action_options(self.get_object()))
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc

    @extend_schema(responses=ManualScriptStructureCheckSerializer)
    @action(detail=True, methods=("get",), url_path="manual-script-structure")
    def manual_script_structure(self, request, pk=None):
        from studio.services.production_manual_scripts import manual_script_structure_check
        if request.query_params:
            raise ValidationError("检查只读取本轮明确采用的修订，不接受替代来源或执行参数")
        try:
            response = Response(manual_script_structure_check(self.get_object()))
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(parameters=[ManualChangeImpactQuerySerializer], responses=ManualChangeImpactSerializer)
    @action(detail=True, methods=("get",), url_path="manual-change-impact")
    def manual_change_impact(self, request, pk=None):
        from studio.services.production_manual_change_impact import manual_content_change_impact
        run = self.get_object()
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise ValidationError("改选影响查询不接受重复参数")
        query = ManualChangeImpactQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        try:
            result = manual_content_change_impact(run, **query.validated_data)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(result, headers={"Cache-Control": "private, no-store"})

    @extend_schema(parameters=[ReviewedVisualSourceQuerySerializer], responses=ReviewedVisualSourcesSerializer)
    @action(detail=True, methods=("get",), url_path="reviewed-visual-sources")
    def reviewed_visual_sources(self, request, pk=None):
        from studio.services.production_reviewed_visual_rules import reviewed_visual_source_options
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise ValidationError("审核来源查询不接受重复参数")
        query = ReviewedVisualSourceQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        try:
            result = reviewed_visual_source_options(run=self.get_object(), owner=request.user, **query.validated_data)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(result, headers={"Cache-Control": "private, no-store"})

    @extend_schema(responses=ManualMediaReviewSerializer)
    @action(detail=True, methods=("get",), url_path="manual-media-review")
    def manual_media_review(self, request, pk=None):
        from studio.services.production_manual_media_review import manual_media_review_detail
        if request.query_params:
            raise ValidationError("选片审核读取本轮完整集合，不接受分页或隐藏成员筛选")
        try:
            response = Response(manual_media_review_detail(self.get_object()))
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(methods=["GET"], responses=ManualAssemblyDetailSerializer)
    @extend_schema(methods=["POST"], request=ManualAssemblyPreviewSerializer, responses=ManualAssemblyDetailSerializer)
    @action(detail=True, methods=("get", "post"), url_path="manual-assembly")
    def manual_assembly(self, request, pk=None):
        from studio.services.production_manual_assembly import manual_assembly_detail
        if request.query_params:
            raise ValidationError("装配读取本轮完整选片和输出配置，不接受隐藏成员筛选")
        data = {}
        if request.method == "POST":
            serializer = ManualAssemblyPreviewSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
        try:
            response = Response(manual_assembly_detail(self.get_object(), **data))
        except DjangoValidationError as exc:
            if getattr(exc, "code", "") == "manual_selection_conflict":
                return Response({"detail": exc.messages, "code": exc.code}, status=status.HTTP_409_CONFLICT)
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(responses=ManualDeliveryDetailSerializer)
    @action(detail=True, methods=("get",), url_path="manual-delivery")
    def manual_delivery(self, request, pk=None):
        from studio.services.production_manual_delivery import manual_delivery_detail
        if request.query_params:
            raise ValidationError("成片确认读取本轮完整范围，不接受隐藏成员筛选")
        try:
            response = Response(manual_delivery_detail(self.get_object()))
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(responses=PolymorphicProxySerializer(component_name="BibleContentReview",
        serializers={"manual": ManualBibleReviewSerializer, "recipe": RecipeBibleReviewSerializer},
        resource_type_field_name="plan_source"))
    @action(detail=True, methods=("get",), url_path="bible-review")
    def bible_review(self, request, pk=None):
        from studio.services.production_manual_bible import manual_bible_review_detail
        from studio.services.bible_content_review import recipe_bible_review_detail
        if request.query_params:
            raise ValidationError("Bible 审核读取明确来源的完整集合，不接受隐藏成员筛选")
        try:
            run = self.get_object()
            reader = manual_bible_review_detail if run.plan_source == "manual" else recipe_bible_review_detail
            response = Response(reader(run))
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(responses=ManualBibleImageReviewSerializer)
    @action(detail=True, methods=("get",), url_path="manual-bible-image-review")
    def manual_bible_image_review(self, request, pk=None):
        from studio.services.production_manual_bible_images import manual_bible_image_review_detail
        if request.query_params:
            raise ValidationError("参考图审核读取本轮完整集合，不接受隐藏成员筛选")
        try:
            response = Response(manual_bible_image_review_detail(self.get_object()))
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(responses=ManualStoryboardReviewSerializer)
    @action(detail=True, methods=("get",), url_path="manual-storyboard-review")
    def manual_storyboard_review(self, request, pk=None):
        from studio.services.production_manual_storyboard_review import manual_storyboard_review_detail
        if request.query_params:
            raise ValidationError("逐镜参考审核必须读取本轮完整范围，不接受隐藏成员筛选")
        try:
            response = Response(manual_storyboard_review_detail(self.get_object()))
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(responses=ManualStoryboardDetailSerializer)
    @action(detail=True, methods=("get",), url_path="manual-storyboard")
    def manual_storyboard(self, request, pk=None):
        from studio.services.production_manual_storyboard import manual_storyboard_detail
        if request.query_params:
            raise ValidationError("分镜采用读取本轮冻结范围的完整集合，不接受隐藏成员筛选")
        try:
            response = Response(manual_storyboard_detail(self.get_object()))
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(parameters=[ManualShotReferenceQuerySerializer], responses=ManualShotReferenceDetailSerializer)
    @action(detail=True, methods=("get",), url_path="manual-shot-references")
    def manual_shot_references(self, request, pk=None):
        from studio.services.production_manual_shot_references import manual_shot_reference_detail
        if set(request.query_params) != {"shot_id"} or len(request.query_params.getlist("shot_id")) != 1:
            raise ValidationError("请提供唯一shot_id，不接受隐藏范围或额外筛选")
        query = ManualShotReferenceQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        try:
            response = Response(ManualShotReferenceDetailSerializer(manual_shot_reference_detail(
                run_id=self.get_object().pk, actor=request.user, shot_id=query.validated_data["shot_id"])).data)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(parameters=[ManualShotReferenceQuerySerializer], responses=RecipeShotReferenceDetailSerializer)
    @action(detail=True, methods=("get",), url_path="recipe-shot-references")
    def recipe_shot_references(self, request, pk=None):
        from studio.services.production_recipe_shot_reference_commands import recipe_shot_reference_detail
        run = self.get_object()
        if set(request.query_params) != {"shot_id"} or len(request.query_params.getlist("shot_id")) != 1:
            raise ValidationError("配方单镜选材只接受唯一明确的shot_id")
        query = ManualShotReferenceQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        try:
            response = Response(RecipeShotReferenceDetailSerializer(recipe_shot_reference_detail(
                run_id=run.pk, actor=request.user, shot_id=query.validated_data["shot_id"])).data)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(responses={200: RecipeStoryboardReferenceSelectionSerializer,
        409: ProductionRunConflictResponseSerializer}, filters=False)
    @action(detail=True, methods=("get",), url_path="storyboard-reference-selection")
    def storyboard_reference_selection(self, request, pk=None):
        from studio.services.production_storyboard_policy import recipe_storyboard_reference_review
        if request.query_params:
            raise ValidationError("当前分镜参考审核读取不接受隐藏范围或额外筛选")
        try:
            response = Response(RecipeStoryboardReferenceSelectionSerializer(
                recipe_storyboard_reference_review(self.get_object(), actor=request.user)).data)
        except ProductionRunConflict as exc:
            details = getattr(exc, "message_dict", None) or {"detail": "; ".join(exc.messages)}
            return api_error_response(status_code=status.HTTP_409_CONFLICT, code=exc.code, details=details)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(parameters=[OpenApiParameter("kind", str, required=True, enum=list(PRODUCTION_SCOPE_KINDS)),
        OpenApiParameter("page", OpenApiTypes.INT, description="页码，从1开始；超出范围返回404。"),
        OpenApiParameter("page_size", OpenApiTypes.INT, description="复用全局分页大小与上限配置。")],
        responses=ManualTargetPageSerializer)
    @action(detail=True, methods=("get",), url_path="targets")
    def targets(self, request, pk=None):
        from studio.services.production_execution_targets import production_target_queryset, production_target_row
        if (set(request.query_params) - {"kind", "page", "page_size"}
                or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)
                or "kind" not in request.query_params):
            raise ValidationError("请提供唯一的目标类型及分页参数，不接受额外筛选")
        run = self.get_object()
        try:
            plan, rows = production_target_queryset(run, request.query_params["kind"])
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        page = self.paginate_queryset(rows)
        response = self.get_paginated_response([production_target_row(row, request.query_params["kind"]) for row in page])
        response.data.update(run_id=str(run.pk), run_plan_sha256=plan.sha256)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(request=StageInputAmendmentInputSerializer, responses=StageInputAmendmentSerializer)
    @action(detail=True, methods=("post",), url_path="stage-input-amendment")
    def stage_input_amendment(self, request, pk=None):
        from studio.services.production_stage_input_amendments import amend_stage_inputs
        run = self.get_object()
        serializer = StageInputAmendmentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not run.stages.filter(pk=data["stage_run_id"]).exists():
            raise ValidationError("所选阶段不属于当前运行")
        try:
            result = amend_stage_inputs(stage_run_id=data["stage_run_id"], policy=data["policy"], source_changes=data.get("source_changes"),
                expected_run_plan_sha256=data["expected_run_plan_sha256"], action=data["action"],
                expected_amendment_sha256=data.get("expected_amendment_sha256"), actor=request.user)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response = Response(StageInputAmendmentSerializer(result).data)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(request=RecipeMappingPreviewInputSerializer, responses=RecipeMappingPreviewSerializer)
    @action(detail=True, methods=("post",), url_path="reference-mapping-preview")
    def reference_mapping_preview(self, request, pk=None):
        from studio.services.production_recipe_media_preparations import preview_recipe_media_mapping
        run = self.get_object()
        serializer = RecipeMappingPreviewInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not run.stages.filter(pk=data["stage_run_id"]).exists():
            raise ValidationError("所选阶段不属于当前运行")
        try:
            result = preview_recipe_media_mapping(stage_run_id=data["stage_run_id"],
                expected_run_plan_sha256=data["expected_run_plan_sha256"], shot_id=str(data["shot_id"]),
                candidate_number=data["candidate_number"], actor=request.user)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        response = Response(RecipeMappingPreviewSerializer(result).data)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(request=ProductionManualActionInputSerializer,
        parameters=[OpenApiParameter("Location", OpenApiTypes.URI, OpenApiParameter.HEADER, response=[200, 202],
            description="generate/retry 动作返回原执行准备详情地址；其他动作不返回此头。")],
        responses={200: ProductionManualActionResultSerializer, 201: ProductionManualActionResultSerializer,
            202: ProductionManualActionResultSerializer,
            409: ProductionRunConflictResponseSerializer})
    @action(detail=True, methods=("post",), url_path="manual-actions")
    def manual_actions(self, request, pk=None):
        from studio.services.production_manual_actions import perform_manual_action
        run = self.get_object()
        serializer = ProductionManualActionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            receipt, created = perform_manual_action(run_id=run.pk, request=serializer.validated_data, actor=request.user)
        except DjangoValidationError as exc:
            if isinstance(exc, ProductionRunConflict) or getattr(exc, "code", "") in {"manual_selection_conflict", "run_plan_source_conflict"}:
                return api_error_response(status_code=status.HTTP_409_CONFLICT, code=exc.code,
                    details=getattr(exc, "message_dict", None) or {"detail": "; ".join(exc.messages)})
            raise _drf_validation(exc) from exc
        response_status = (status.HTTP_202_ACCEPTED if receipt.action in {"generate", "retry", "assemble"} else status.HTTP_201_CREATED) if created else status.HTTP_200_OK
        headers = {"Location": reverse("execution-preparation-detail", kwargs={"pk": receipt.target_id}, request=request)} if receipt.action in {"generate", "retry"} else {}
        return Response(ProductionManualActionResultSerializer(receipt).data, status=response_status, headers=headers)

    @extend_schema(
        request=ProductionRunCreateSerializer,
        responses={
            200: ProductionRunDetailSerializer,
            201: ProductionRunDetailSerializer,
            400: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        projects = SeriesProject.objects.all() if request.user.is_staff else SeriesProject.objects.filter(owner=request.user)
        project = get_object_or_404(projects, pk=data.pop("project_id"))
        recipe_id = data.pop("recipe_version_id", None)
        recipe = get_object_or_404(ProductionRecipeVersion, pk=recipe_id) if recipe_id else None
        revision_id = data.pop("script_revision_id", None)
        script_revision = None
        if revision_id:
            script_revision = get_object_or_404(
                ScriptRevision.objects.select_related("source"),
                pk=revision_id,
                source__project=project,
            )
        try:
            run, created = create_production_run(
                user=request.user,
                project=project,
                script_revision=script_revision,
                recipe=recipe,
                **data,
            )
        except ConfigurationResolutionError as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details={"detail": str(exc), "missing_keys": list(exc.missing_keys)},
            )
        except ProductionRunCreateRejected as exc:
            details = getattr(exc, "message_dict", None) or {"detail": "; ".join(exc.messages)}
            return api_error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=exc.code,
                details=details,
            )
        except ProductionRunConflict as exc:
            details = getattr(exc, "message_dict", None) or {
                "detail": "; ".join(exc.messages),
            }
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details=details,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(
            ProductionRunDetailSerializer(run).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        request=ApprovalDecisionInputSerializer,
        responses={
            200: ProductionRunDetailSerializer,
            409: ProductionRunConflictResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def approve(self, request, pk=None):
        serializer = ApprovalDecisionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = decide_stage(self.get_object(), user=request.user, **serializer.validated_data)
        except ProductionRunConflict as exc:
            details = getattr(exc, "message_dict", None) or {"detail": "; ".join(exc.messages)}
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details=details,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)

    @extend_schema(
        request=BibleImageRegenerationInputSerializer,
        responses={
            200: ProductionRunDetailSerializer,
            409: ProductionRunConflictResponseSerializer,
        },
        description=(
            "在 bible_image_review 待人工审核时，使用本 ProductionRun 已冻结的 "
            "Bible 模型、PromptProfile 与 route 为指定实体追加一个新候选。"
            "旧候选与正式 Bible 绑定均不会被覆盖。"
            "支持种子时首次准备抽取并冻结未被同实体使用的合法种子，固定值不绕过；重放和失败重试不重抽。"
        ),
    )
    @action(detail=True, methods=("post",), url_path="regenerate-bible-images")
    def regenerate_bible_images(self, request, pk=None):
        serializer = BibleImageRegenerationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = regenerate_bible_image_candidates(
                self.get_object(),
                user=request.user,
                **serializer.validated_data,
            )
        except ProductionRunConflict as exc:
            details = getattr(exc, "message_dict", None) or {
                "detail": "; ".join(exc.messages),
            }
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details=details,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)

    @extend_schema(request=None, responses=ProductionRunDetailSerializer,
        description="显式继续原运行。文本/媒体生成阶段的stage_validation_failed可重检已修正输入或合同，复用原准备及已有调用，不重发成功任务；未修正则返回真实阻断。待人工分镜审核时仅补齐自动选材，保留审核停点；不代替批准。来源不符或选择被执行锁定返回400。")
    @action(detail=True, methods=("post",), url_path="continue")
    def continue_run(self, request, pk=None):
        try:
            run = advance_production_run(self.get_object(), human_initiated=True)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)

    @extend_schema(parameters=[ProductionRetryPreviewQuerySerializer], responses=ProductionRetryPreviewSerializer,
        filters=False)
    @action(detail=True, methods=("get",), url_path="retry-preview")
    def retry_preview(self, request, pk=None):
        from studio.services.production_retry_preview import (
            retry_preview_sources, retry_preview_rows, retry_preview_summary, serialize_retry_preview_page,
        )
        query = ProductionRetryPreviewQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        run = self.get_object()
        if run.plan_source != "recipe":
            raise ValidationError("无配方制作请使用对应步骤的人工动作。")
        stage = run.stages.only("id", "status", "reason_code", "stage_key", "mode", "production_run_id").filter(
            stage_key=run.current_stage_key).first()
        if stage is None or stage.pk != query.validated_data["expected_stage_id"]:
            raise ValidationError("当前阶段已变化，请重新读取后确认。")
        if stage.stage_key == "assembly":
            from studio.services.production_assembly_retry import assembly_retry_links, assembly_retry_preview, assembly_retry_impact
            links = list(assembly_retry_links(stage))
            rows = self.paginate_queryset(links)
            response = self.get_paginated_response([assembly_retry_impact(row, stage) for row in rows])
            response.data.update(assembly_retry_preview(run, stage, links))
            return Response(ProductionRetryPreviewSerializer(response.data).data)
        calls, preparations = retry_preview_sources(stage)
        rows = self.paginate_queryset(retry_preview_rows(calls, preparations))
        response = self.get_paginated_response(serialize_retry_preview_page(rows, calls, preparations, stage=stage))
        response.data.update(retry_preview_summary(run, stage, calls, preparations))
        return Response(ProductionRetryPreviewSerializer(response.data).data)

    @extend_schema(
        request=ProductionRunRetryInputSerializer,
        responses={
            200: ProductionRunDetailSerializer,
            409: ProductionRunConflictResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def retry(self, request, pk=None):
        serializer = ProductionRunRetryInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = retry_production_run(self.get_object(), user=request.user, **serializer.validated_data)
        except ProductionRunRetryConflict as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code="uncertain_remote_response",
                details={"detail": str(exc)},
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)

    @extend_schema(
        request=ResolveUncertainAttemptInputSerializer,
        responses={
            200: ProductionRunDetailSerializer,
            409: ProductionRunConflictResponseSerializer,
        },
    )
    @action(
        detail=True,
        methods=("post",),
        url_path="resolve-uncertain",
        permission_classes=(IsAdminUser,),
    )
    def resolve_uncertain(self, request, pk=None):
        serializer = ResolveUncertainAttemptInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = resolve_uncertain_attempt(
                self.get_object(),
                user=request.user,
                **serializer.validated_data,
            )
        except ProductionRunRetryConflict as exc:
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code="uncertain_resolution_conflict",
                details={"detail": str(exc)},
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)

    @extend_schema(request=None, responses=ProductionRunDetailSerializer)
    @action(detail=True, methods=("post",))
    def cancel(self, request, pk=None):
        try:
            run = cancel_production_run(self.get_object(), user=request.user)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)

    @extend_schema(parameters=[OpenApiParameter("artifact_id", OpenApiTypes.UUID, OpenApiParameter.QUERY, required=True)],
        responses=RecipeMaterialSelectionContextSerializer)
    @action(detail=True, methods=("get",), url_path="material-selection-context")
    def material_selection_context(self, request, pk=None):
        from studio.services.production_recipe_material_selection import recipe_material_selection_context
        serializer = SelectArtifactInputSerializer(data={"artifact_id": request.query_params.get("artifact_id")})
        serializer.is_valid(raise_exception=True)
        artifact = get_object_or_404(GeneratedArtifact.objects.select_related("generation_attempt__preparation"),
            pk=serializer.validated_data["artifact_id"], production_run=self.get_object())
        try:
            result = recipe_material_selection_context(artifact=artifact, actor=request.user)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(RecipeMaterialSelectionContextSerializer(result).data, headers={"Cache-Control": "private, no-store"})

    @extend_schema(request=SelectArtifactInputSerializer,
        description="新媒体选片先核验真实归档文件和冻结来源；失败返回400且不写选择或回执。原键原正文重放仅返回历史决定，不代表重新验证文件。",
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER,
            required=True, description="1至191字符的稳定请求键；失联后使用原键及原正文重放，新意图使用新键。")],
        responses={200: ProductionManualActionResultSerializer, 409: ProductionRunConflictResponseSerializer})
    @action(detail=True, methods=("post",), url_path="select-artifact")
    def select_artifact(self, request, pk=None):
        from studio.services.production_artifact_selection_receipts import select_run_artifact_command
        run = self.get_object()
        serializer = SelectArtifactInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        artifact = get_object_or_404(
            GeneratedArtifact,
            pk=serializer.validated_data["artifact_id"],
            production_run=run,
        )
        try:
            receipt = select_run_artifact_command(run, artifact, actor=request.user,
                request_key=request.headers.get("Idempotency-Key"),
                material_selection=serializer.validated_data.get("material_selection"))
        except ProductionRunConflict as exc:
            return api_error_response(status_code=status.HTTP_409_CONFLICT, code=exc.code,
                details=getattr(exc, "message_dict", None) or {"detail": "; ".join(exc.messages)})
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionManualActionResultSerializer(receipt).data,
            headers={"Cache-Control": "private, no-store"})

    @extend_schema(
        request=SelectRouteInputSerializer,
        responses=ProductionRunDetailSerializer,
    )
    @action(detail=True, methods=("post",), url_path="select-route")
    def select_route(self, request, pk=None):
        serializer = SelectRouteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = select_stage_route(
                self.get_object(),
                user=request.user,
                **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)

    @extend_schema(
        request=AuthorizeStageCostInputSerializer,
        responses=ProductionRunDetailSerializer,
    )
    @action(detail=True, methods=("post",), url_path="authorize-cost")
    def authorize_cost(self, request, pk=None):
        serializer = AuthorizeStageCostInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = authorize_stage_cost(
                self.get_object(),
                user=request.user,
                **serializer.validated_data,
            )
        except ProductionRunConflict as exc:
            details = getattr(exc, "message_dict", None) or {
                "detail": "; ".join(exc.messages),
            }
            return api_error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=exc.code,
                details=details,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)


    @security_boundary_resolution_cache()
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)


class GenerationAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAuthenticated,)
    serializer_class = GenerationAttemptSerializer
    queryset = GenerationAttempt.objects.none()
    pagination_class = StandardPageNumberPagination
    filterset_fields = ("stage_run", "status", "operation", "model_deployment")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        queryset = GenerationAttempt.objects.select_related(
            "stage_run", "model_deployment", "generation_task",
        ).order_by("-created_at")
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(
            stage_run__production_run__project__owner=self.request.user,
        )


class WorkflowSmokeAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    serializer_class = WorkflowSmokeAttemptSerializer
    queryset = WorkflowSmokeAttempt.objects.select_related(
        "workflow_version", "deployment", "created_by",
    ).prefetch_related("asset_bindings", "resolutions__decided_by", "execution_preparations").order_by("-created_at")
    filterset_fields = ("workflow_version", "deployment", "status", "scope")

    @extend_schema(request=None, responses={202: WorkflowSmokeAttemptSerializer})
    @action(detail=True, methods=("post",), url_path="cancel")
    def cancel(self, request, pk=None):
        try:
            attempt = request_workflow_smoke_cancel(self.get_object(), actor=request.user)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(
            self.get_serializer(attempt).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        request=WorkflowSmokeResolutionInputSerializer,
        responses={
            200: WorkflowSmokeResolutionSerializer,
            201: WorkflowSmokeResolutionSerializer,
            400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer,
            409: ConfigurationErrorResponseSerializer,
        },
        description=(
            "Append immutable operator evidence resolving an uncertain workflow "
            "smoke. This never creates passed smoke evidence or replays the provider."
        ),
    )
    @action(detail=True, methods=("post",), url_path="resolve-uncertain")
    def resolve_uncertain(self, request, pk=None):
        serializer = WorkflowSmokeResolutionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            resolution, created = resolve_workflow_smoke_attempt(
                self.get_object(),
                user=request.user,
                **serializer.validated_data,
            )
        except WorkflowSmokeResolutionConflict as exc:
            return _conflict(str(exc), code=exc.code)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(
            WorkflowSmokeResolutionSerializer(resolution).data,
            status=(
                status.HTTP_201_CREATED
                if created else status.HTTP_200_OK
            ),
        )


class ModelDeploymentSmokeAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    serializer_class = ModelDeploymentSmokeAttemptSerializer
    queryset = ModelDeploymentSmokeAttempt.objects.select_related(
        "deployment", "final_snapshot",
    ).prefetch_related("execution_preparations").order_by("-created_at")
    filterset_fields = ("deployment", "operation", "status", "scope")

    @extend_schema(
        request=None,
        responses={202: ModelDeploymentSmokeAttemptSerializer},
        description=(
            "Cancel a queued text/media smoke before submission, or persist an async "
            "media remote-cancel request. Text smokes cannot be withdrawn after submitting."
        ),
    )
    @action(detail=True, methods=("post",), url_path="cancel")
    def cancel(self, request, pk=None):
        try:
            attempt = request_deployment_smoke_cancel(self.get_object(), actor=request.user)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(self.get_serializer(attempt).data, status=status.HTTP_202_ACCEPTED)


class GeneratedArtifactViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    filterset_fields = (
        "production_run", "stage_run", "generation_task", "kind", "status",
        "variant_group",
    )
    queryset = GeneratedArtifact.objects.select_related("production_run__project", "generation_task")

    def get_queryset(self):
        fields = [key for key in GeneratedArtifactListSerializer.Meta.fields if key not in {"preview_url", "download_url"}]
        fields += ["production_run__id", "production_run__owner_id", "production_run__project_id",
            "production_run__project__id", "production_run__project__owner_id",
            "generation_task__id", "generation_task__owner_id", "generation_task__project_id"]
        if self.action == "retrieve":
            fields.append("manifest_metadata")
        queryset = self.queryset.only(*fields)
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(production_run__project__owner=self.request.user)

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        # Media URL provenance needs its manifest; text candidates do not.
        # Read only the bounded page's media metadata in one batch, never the
        # native candidate bodies or related Run/Attempt request snapshots.
        media_ids = [row.pk for row in page if row.storage_backend in {"managed_local", "local_media"}
            and row.kind in {"image", "video", "audio", "assembly"} and row.status in {"available", "selected"}
            and row.production_run_id is not None and row.generation_task_id is None]
        metadata = dict(GeneratedArtifact.objects.filter(pk__in=media_ids).values_list("id", "manifest_metadata"))
        if len(metadata) != len(media_ids):
            raise ValidationError("产物已变化，请重新读取")
        for row in page:
            if row.pk in metadata:
                row.manifest_metadata = metadata[row.pk]
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def get_serializer_class(self):
        return (
            GeneratedArtifactDetailSerializer
            if self.action == "retrieve" else GeneratedArtifactListSerializer
        )
