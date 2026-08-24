from __future__ import annotations

import json
from urllib.parse import urlencode

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.reverse import reverse

from studio.models import (
    ConfigDefinition,
    ConfigRevision,
    GeneratedArtifact,
    GenerationAttempt,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    PromptFieldDefinition,
    PromptProfileBinding,
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
    WorkflowSmokeAttempt,
)
from studio.pagination import StandardPageNumberPagination
from studio.permissions import IsStaffOrReadOnly, IsSuperUser
from studio.platform_serializers import (
    ApprovalDecisionInputSerializer,
    AuthorizeStageCostInputSerializer,
    ConfigDefinitionSerializer,
    ConfigResourceOptionSerializer,
    ConfigurationErrorResponseSerializer,
    ConfigRevisionCreateSerializer,
    ConfigRevisionSerializer,
    DeploymentActionSerializer,
    DeploymentProbeInputSerializer,
    GeneratedArtifactDetailSerializer,
    GeneratedArtifactListSerializer,
    GenerationAttemptSerializer,
    HostCapabilitySnapshotSerializer,
    ModelArtifactSerializer,
    ModelArtifactVerificationInputSerializer,
    ModelDeploymentSerializer,
    ModelDeploymentCapabilitySnapshotSerializer,
    ModelDeploymentSmokeAttemptSerializer,
    ModelProfileVersionSerializer,
    NodeMetadataQuerySerializer,
    NodeMetadataResponseSerializer,
    ProductionRunConflictResponseSerializer,
    PromptNormalizationSmokeResolutionSerializer,
    ProductionReadinessResponseSerializer,
    ProductionRecipeMediaRouteOptionsSerializer,
    ProductionRecipeVersionSerializer,
    ProductionRunCreateSerializer,
    ProductionRunDetailSerializer,
    ProductionRunListSerializer,
    ProjectConfigResponseSerializer,
    ProjectConfigWriteSerializer,
    ProjectEffectiveConfigurationResponseSerializer,
    ProviderProfileSerializer,
    PlatformLifecycleAuditEventSerializer,
    PromptFieldDefinitionSerializer,
    PromptProfileBindingSerializer,
    PromptProfileFieldRuleSerializer,
    PromptProfileOptionsSerializer,
    PromptProfilePreviewInputSerializer,
    PromptProfilePreviewSerializer,
    PromptProfileVersionSerializer,
    RecommendationDecisionInputSerializer,
    RecommendationDecisionSerializer,
    RecommendationSnapshotSerializer,
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
    SelectRouteInputSerializer,
    WorkflowTemplateVersionDetailSerializer,
    WorkflowTemplateCanvasExportSerializer,
    WorkflowTemplateCanvasExportQuerySerializer,
    WorkflowTemplateVersionImportSerializer,
    WorkflowTemplateVersionListSerializer,
    WorkflowSmokeTestResponseSerializer,
    WorkflowSmokeAttemptSerializer,
    WorkflowValidationResponseSerializer,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    activate_config_revision,
    config_resource_reference_options,
    create_config_revision,
    publish_config_revision,
    redact_configuration_snapshot,
    resolve_configuration,
    validate_version_configuration_defaults,
)
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.exceptions import api_error_response
from studio.services.platform_lifecycle_audit import (
    lifecycle_summary,
    record_platform_lifecycle_event,
    request_reason_code,
)
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
    production_recipe_media_route_options,
    retry_production_run,
    resolve_uncertain_attempt,
    select_run_artifact,
    select_stage_route,
    validate_production_recipe_executability,
)
from studio.services.provider_registry import ProviderDriverError, driver_for
from studio.services.prompt_profiles import (
    KNOWN_MODEL_FAMILIES,
    KNOWN_PROMPT_OPERATIONS,
    assemble_prompt_profile,
    model_family_for_profile,
    publish_prompt_profile,
    refresh_prompt_profile_contract,
    retire_prompt_profile,
)
from studio.services.async_media_contract import validate_async_model_profile
from studio.services.deployment_smoke import (
    SCRIPT_GENERATE_OPERATION,
    probe_async_deployment,
)
from studio.services.script_structure import (
    SCRIPT_STRUCTURE_OPERATION,
    validate_script_structure_model_profile,
)
from studio.services.deployment_smoke_queue import (
    enqueue_deployment_smoke,
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
    verified_artifact_availability,
)
from studio.services.runtime_host_probe_queue import (
    enqueue_runtime_host_probe,
    request_runtime_host_probe_cancel,
)
from studio.services.script_sources import (
    ScriptSourceValidationError,
    confirm_script_revision,
    create_pasted_script_source,
    create_script_revision,
    create_uploaded_script_source,
)
from studio.services.readiness import collect_production_readiness
from studio.services.workflow_versions import (
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
from studio.services.workflow_smoke_queue import (
    enqueue_workflow_smoke,
    request_workflow_smoke_cancel,
)
from studio.services.secret_store import (
    SecretStoreError,
    create_secret_binding,
    rotate_secret_binding,
    set_secret_binding_active,
)


def _drf_validation(exc: DjangoValidationError) -> ValidationError:
    detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", None) or str(exc)
    return ValidationError(detail)


class ProductionReadinessView(APIView):
    permission_classes = (IsSuperUser,)

    @extend_schema(responses=ProductionReadinessResponseSerializer)
    def get(self, request):
        return Response(collect_production_readiness())


def _json_contains_identifier(value, identifier: str) -> bool:
    if isinstance(value, dict):
        return any(_json_contains_identifier(item, identifier) for item in value.values())
    if isinstance(value, list):
        return any(_json_contains_identifier(item, identifier) for item in value)
    return isinstance(value, str) and value == identifier


def _recipe_reference_exists(identifier) -> bool:
    target = str(identifier)
    return any(
        _json_contains_identifier(definition, target)
        for definition in ProductionRecipeVersion.objects.filter(
            status__in=(
                ProductionRecipeVersion.Status.DRAFT,
                ProductionRecipeVersion.Status.PUBLISHED,
            ),
        ).values_list("definition", flat=True)
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

    def perform_destroy(self, instance):
        with transaction.atomic():
            instance = type(instance)._default_manager.select_for_update().get(
                pk=instance.pk,
            )
            before = lifecycle_summary(instance)
            self._record_lifecycle(
                instance,
                action="delete",
                reason_default="api.delete",
                before=before,
            )
            instance.delete()


class StaffPlatformModelViewSet(PlatformLifecycleAuditMixin, viewsets.ModelViewSet):
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

    @extend_schema(responses={
        204: None,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    def destroy(self, request, *args, **kwargs):
        provider = self.get_object()
        if provider.is_system:
            raise ValidationError("系统 Provider 不能删除；只能停用")
        if provider.is_active:
            raise ValidationError("请先停用 Provider，再删除未被引用的记录")
        return super().destroy(request, *args, **kwargs)


class ModelProfileVersionViewSet(StaffPlatformModelViewSet):
    queryset = ModelProfileVersion.objects.select_related("provider", "published_by")
    serializer_class = ModelProfileVersionSerializer
    filterset_fields = ("profile_key", "modality", "status", "provider")

    def update(self, request, *args, **kwargs):
        if self.get_object().status in {
            ModelProfileVersion.Status.PUBLISHED,
            ModelProfileVersion.Status.RETIRED,
        }:
            raise ValidationError("published/retired model profile version 不可修改")
        return super().update(request, *args, **kwargs)

    @extend_schema(responses={
        204: None,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    def destroy(self, request, *args, **kwargs):
        if self.get_object().status in {
            ModelProfileVersion.Status.PUBLISHED,
            ModelProfileVersion.Status.RETIRED,
        }:
            raise ValidationError("published/retired model profile version 不可删除")
        return super().destroy(request, *args, **kwargs)

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
                verify_json_contract(
                    profile.specification,
                    schema_version=profile.specification_schema_version,
                    size_bytes=profile.specification_size_bytes,
                    sha256=profile.specification_sha256,
                )
                validate_version_configuration_defaults(
                    model_profile_version=profile,
                )
                validate_async_model_profile(profile)
                validate_prompt_normalization_model_profile(profile)
                validate_script_generation_model_profile(profile)
                validate_script_structure_model_profile(profile)
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
            if profile.deployments.filter(status=ModelDeployment.Status.ACTIVE).exists():
                return _conflict("仍有 active deployment 引用该模型版本，请先停用部署")
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


class PromptFieldDefinitionViewSet(StaffPlatformModelViewSet):
    queryset = PromptFieldDefinition.objects.all()
    serializer_class = PromptFieldDefinitionSerializer
    filterset_fields = ("category", "value_type", "is_required", "is_active")
    search_fields = ("key", "label", "description", "impact")

    def _frozen_reference_exists(self, definition) -> bool:
        return definition.profile_rules.filter(
            profile_version__status__in=(
                PromptProfileVersion.Status.PUBLISHED,
                PromptProfileVersion.Status.RETIRED,
            ),
        ).exists()

    def update(self, request, *args, **kwargs):
        if self._frozen_reference_exists(self.get_object()):
            raise ValidationError(
                "字段已被 published/retired PromptProfile 冻结引用；请新建字段与 PromptProfile 版本",
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        definition = self.get_object()
        if definition.profile_rules.exists():
            raise ValidationError("字段仍被 PromptProfile 规则引用，不能删除")
        return super().destroy(request, *args, **kwargs)


class PromptProfileVersionViewSet(StaffPlatformModelViewSet):
    serializer_class = PromptProfileVersionSerializer
    filterset_fields = ("profile_key", "model_family", "operation", "mode", "status", "review_state")
    search_fields = ("profile_key", "name", "description")

    def get_queryset(self):
        return PromptProfileVersion.objects.select_related("published_by").annotate(
            field_rule_count=Count("field_rules"),
        )

    def update(self, request, *args, **kwargs):
        if self.get_object().status in {
            PromptProfileVersion.Status.PUBLISHED,
            PromptProfileVersion.Status.RETIRED,
        }:
            raise ValidationError("published/retired PromptProfile 不可修改；请创建新版本")
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        profile = self.get_object()
        if profile.status != PromptProfileVersion.Status.DRAFT:
            raise ValidationError("published/retired PromptProfile 不可删除")
        if profile.bindings.exists() or profile.prompt_artifacts.exists() or profile.generation_attempts.exists():
            raise ValidationError("PromptProfile 已被运行事实引用，不能删除")
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        request=None,
        responses={200: PromptProfileVersionSerializer, 400: ConfigurationErrorResponseSerializer},
    )
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        try:
            with transaction.atomic():
                profile = self.get_object()
                before = lifecycle_summary(profile)
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
            with transaction.atomic():
                profile = self.get_object()
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
        request=PromptProfilePreviewInputSerializer,
        responses={200: PromptProfilePreviewSerializer, 400: ConfigurationErrorResponseSerializer},
    )
    @action(detail=True, methods=("post",))
    def preview(self, request, pk=None):
        input_serializer = PromptProfilePreviewInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        profile = self.get_object()
        try:
            if profile.status == PromptProfileVersion.Status.DRAFT:
                refresh_prompt_profile_contract(profile)
            preview = assemble_prompt_profile(
                profile,
                sources=input_serializer.validated_data["sources"],
                allow_draft=True,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(PromptProfilePreviewSerializer(preview).data)

    @extend_schema(request=None, responses=PromptProfileOptionsSerializer)
    @action(detail=False, methods=("get",))
    def options(self, request):
        operations = sorted(set(KNOWN_PROMPT_OPERATIONS) | set(
            PromptProfileVersion.objects.values_list("operation", flat=True),
        ))
        model_rows = list(ModelProfileVersion.objects.filter(
            status=ModelProfileVersion.Status.PUBLISHED,
        ).select_related("provider").order_by("profile_key", "-version")[:200])
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
        modes = sorted(set(Shot.H3Mode.values) | set(
            PromptProfileVersion.objects.exclude(mode="").values_list("mode", flat=True),
        ))

        def choices(enum):
            return [{"value": value, "label": label} for value, label in enum.choices]

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
        data = {
            "operations": operations,
            "model_families": families,
            "modes": modes,
            "source_selectors": choices(PromptProfileFieldRule.SourceSelector),
            "channels": choices(PromptProfileFieldRule.Channel),
            "label_styles": choices(PromptProfileFieldRule.LabelStyle),
            "separators": choices(PromptProfileFieldRule.Separator),
            "model_profile_versions": models,
        }
        return Response(PromptProfileOptionsSerializer(data).data)


class PromptProfileFieldRuleViewSet(StaffPlatformModelViewSet):
    queryset = PromptProfileFieldRule.objects.select_related(
        "profile_version", "field_definition",
    )
    serializer_class = PromptProfileFieldRuleSerializer
    filterset_fields = ("profile_version", "field_definition", "channel", "source_selector")

    def destroy(self, request, *args, **kwargs):
        rule = self.get_object()
        if rule.profile_version.status != PromptProfileVersion.Status.DRAFT:
            raise ValidationError("published/retired PromptProfile 的字段规则不可删除")
        with transaction.atomic():
            profile = PromptProfileVersion.objects.select_for_update().get(
                pk=rule.profile_version_id,
            )
            before = lifecycle_summary(rule)
            self._record_lifecycle(
                rule,
                action="delete",
                reason_default="api.delete",
                before=before,
            )
            rule.delete()
            refresh_prompt_profile_contract(profile)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PromptProfileBindingViewSet(StaffPlatformModelViewSet):
    queryset = PromptProfileBinding.objects.select_related(
        "prompt_profile_version", "model_profile_version",
    )
    serializer_class = PromptProfileBindingSerializer
    filterset_fields = (
        "operation", "model_family", "mode", "model_profile_version",
        "prompt_profile_version", "is_active",
    )
    search_fields = ("key", "name")


class ModelDeploymentViewSet(StaffPlatformModelViewSet):
    queryset = ModelDeployment.objects.select_related(
        "profile_version__provider", "runtime_host",
    )
    serializer_class = ModelDeploymentSerializer
    filterset_fields = ("status", "runtime_host", "profile_version", "is_default")
    search_fields = ("key", "name")

    def _save_with_host_lock(self, serializer, *, action: str):
        target_host = serializer.validated_data.get(
            "runtime_host",
            getattr(serializer.instance, "runtime_host", None),
        )
        target_status = serializer.validated_data.get(
            "status",
            getattr(serializer.instance, "status", ModelDeployment.Status.ACTIVE),
        )
        with transaction.atomic():
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

    @extend_schema(responses={
        204: None,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    def destroy(self, request, *args, **kwargs):
        deployment = self.get_object()
        if deployment.status != ModelDeployment.Status.DISABLED:
            raise ValidationError("请先停用 deployment，再删除未被引用且没有证据的记录")
        if _recipe_reference_exists(deployment.id):
            return _conflict("草稿或已发布配方仍引用该 deployment")
        return super().destroy(request, *args, **kwargs)

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
        request=DeploymentProbeInputSerializer,
        responses={202: ModelDeploymentSmokeAttemptSerializer},
        description=(
            "Create or reuse one durable deployment smoke attempt. All supported "
            "text and media operations return HTTP 202; workers submit only after "
            "the frozen request is persisted."
        ),
    )
    @action(detail=True, methods=("post",), url_path="smoke-test")
    def smoke_test(self, request, pk=None):
        serializer = DeploymentProbeInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        operation = serializer.validated_data.get("operation")
        if not operation:
            raise ValidationError({"operation": "真实烟测必须指定能力"})
        if self.get_object().status != ModelDeployment.Status.ACTIVE:
            return _conflict("只有 active deployment 可以执行烟测", code="deployment_inactive")
        unresolved = unresolved_deployment_smoke_attempt(
            self.get_object(),
            operation=operation,
        )
        if unresolved is not None:
            return _conflict(
                f"已有 {operation} smoke 提交状态不确定，必须先人工消歧",
                code="unresolved_smoke_submission",
            )
        try:
            attempt, _created = enqueue_deployment_smoke(
                self.get_object(), operation=operation,
            )
        except DjangoValidationError as exc:
            details = getattr(exc, "message_dict", None) or {}
            if isinstance(details, dict) and details.get("code") == [
                "unresolved_smoke_submission"
            ]:
                return _conflict(
                    f"已有 {operation} smoke 提交状态不确定，必须先人工消歧",
                    code="unresolved_smoke_submission",
                )
            raise _drf_validation(exc) from exc
        location = reverse(
            "model-deployment-smoke-attempt-detail",
            kwargs={"pk": attempt.pk},
            request=request,
        )
        return Response(
            ModelDeploymentSmokeAttemptSerializer(attempt).data,
            status=status.HTTP_202_ACCEPTED,
            headers={"Location": location},
        )

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


class ModelArtifactViewSet(StaffPlatformModelViewSet):
    queryset = ModelArtifact.objects.select_related("profile_version")
    serializer_class = ModelArtifactSerializer
    filterset_fields = ("profile_version", "role", "status")
    search_fields = ("logical_name", "filename", "sha256")

    @extend_schema(responses={
        204: None,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    def destroy(self, request, *args, **kwargs):
        artifact = self.get_object()
        if artifact.profile_version.status in {
            ModelProfileVersion.Status.PUBLISHED,
            ModelProfileVersion.Status.RETIRED,
        }:
            raise ValidationError(
                "绑定 published model profile version 的 artifact 不可删除；请创建新版本",
            )
        return super().destroy(request, *args, **kwargs)

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


class WorkflowTemplateVersionViewSet(StaffPlatformModelViewSet):
    http_method_names = ("get", "post", "delete", "head", "options")
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

    def get_serializer_class(self):
        if self.action == "create":
            return WorkflowTemplateVersionImportSerializer
        if self.action == "retrieve":
            return WorkflowTemplateVersionDetailSerializer
        return WorkflowTemplateVersionListSerializer

    @extend_schema(
        request=WorkflowTemplateVersionImportSerializer,
        responses={201: WorkflowTemplateVersionDetailSerializer},
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            version = serializer.save()
            self._record_lifecycle(
                version,
                action="create",
                reason_default="api.create",
                after=lifecycle_summary(version),
            )
        output = WorkflowTemplateVersionDetailSerializer(version, context=self.get_serializer_context())
        return Response(output.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        raise ValidationError("workflow version 不支持原地更新；请导入新版本")

    @extend_schema(responses={
        204: None,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    def destroy(self, request, *args, **kwargs):
        version = self.get_object()
        if version.status != WorkflowTemplateVersion.Status.DRAFT:
            raise ValidationError("只有未使用的 draft workflow version 可以删除")
        return super().destroy(request, *args, **kwargs)

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
        responses=WorkflowValidationResponseSerializer,
    )
    @action(detail=True, methods=("post",))
    def validate(self, request, pk=None):
        try:
            version, report = validate_workflow_version(
                self.get_object(),
                deployment=self._deployment(request),
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response({
            "version": WorkflowTemplateVersionDetailSerializer(version).data,
            "contract_report": report,
        })

    @extend_schema(
        request=DeploymentActionSerializer,
        responses={202: WorkflowSmokeAttemptSerializer},
    )
    @action(detail=True, methods=("post",), url_path="smoke-test")
    def smoke_test(self, request, pk=None):
        try:
            attempt, _created = enqueue_workflow_smoke(
                self.get_object(),
                deployment=self._deployment(request),
                created_by=request.user,
            )
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        location = reverse(
            "workflow-smoke-attempt-detail",
            kwargs={"pk": attempt.pk},
            request=request,
        )
        return Response(
            WorkflowSmokeAttemptSerializer(attempt).data,
            status=status.HTTP_202_ACCEPTED,
            headers={"Location": location},
        )

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
        return Response(WorkflowTemplateVersionDetailSerializer(version).data)

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
            if _recipe_reference_exists(version.id):
                return _conflict("草稿或已发布配方仍引用该 workflow version")
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
        return Response(WorkflowTemplateVersionDetailSerializer(version).data)


class ConfigDefinitionViewSet(StaffPlatformModelViewSet):
    queryset = ConfigDefinition.objects.all()
    serializer_class = ConfigDefinitionSerializer
    filterset_fields = ("domain", "value_type", "is_required", "is_secret", "is_active")
    search_fields = ("key", "description")

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
            rows = config_resource_reference_options(definition)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ConfigResourceOptionSerializer(rows, many=True).data)

    @extend_schema(responses={
        204: None,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    def destroy(self, request, *args, **kwargs):
        definition = self.get_object()
        if definition.values.exists():
            raise ValidationError("已有 revision 使用该配置定义，不能删除；请停用")
        return super().destroy(request, *args, **kwargs)


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
        }).sha256
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
                limit=query.validated_data["limit"],
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
    filterset_fields = ("host", "status", "trigger")

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
        project = get_object_or_404(
            SeriesProject.objects.filter(owner=request.user) if not request.user.is_staff else SeriesProject.objects.all(),
            pk=request.data.get("project_id"),
        )
        try:
            if request.FILES.get("file") is not None:
                source, revision = create_uploaded_script_source(
                    project=project,
                    user=request.user,
                    uploaded_file=request.FILES["file"],
                )
            else:
                source, revision = create_pasted_script_source(
                    project=project,
                    user=request.user,
                    content=str(request.data.get("content") or ""),
                    source_format=str(request.data.get("source_format") or ScriptSource.SourceFormat.PLAIN),
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
        sources = ScriptSource.objects.all()
        if not request.user.is_staff:
            sources = sources.filter(owner=request.user)
        source_obj = get_object_or_404(sources, pk=request.data.get("source_id"))
        try:
            revision = create_script_revision(
                source=source_obj,
                user=request.user,
                content=str(request.data.get("content") or ""),
            )
        except (ScriptSourceValidationError, ConfigurationResolutionError) as exc:
            if isinstance(exc, ConfigurationResolutionError):
                return api_error_response(
                    status_code=status.HTTP_409_CONFLICT,
                    code=exc.code,
                    details={"detail": str(exc)},
                )
            raise ValidationError(str(exc)) from exc
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

    def get_queryset(self):
        queryset = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        if not self.request.user.is_staff:
            queryset = queryset.filter(status=ProductionRecipeVersion.Status.PUBLISHED)
        return queryset

    def update(self, request, *args, **kwargs):
        if self.get_object().status in {
            ProductionRecipeVersion.Status.PUBLISHED,
            ProductionRecipeVersion.Status.RETIRED,
        }:
            raise ValidationError("published/retired recipe 不可修改；请创建新版本")
        return super().update(request, *args, **kwargs)

    @extend_schema(
        request=None,
        responses={200: ProductionRecipeMediaRouteOptionsSerializer},
    )
    @action(detail=True, methods=("get",), url_path="media-route-options")
    def media_route_options(self, request, pk=None):
        recipe = self.get_object()
        try:
            payload = production_recipe_media_route_options(recipe)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRecipeMediaRouteOptionsSerializer(payload).data)

    @extend_schema(responses={
        204: None,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    def destroy(self, request, *args, **kwargs):
        if self.get_object().status in {
            ProductionRecipeVersion.Status.PUBLISHED,
            ProductionRecipeVersion.Status.RETIRED,
        }:
            raise ValidationError("published/retired recipe 不可删除；请保留历史版本")
        return super().destroy(request, *args, **kwargs)

    @extend_schema(request=None, responses={
        200: ProductionRecipeVersionSerializer,
        400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("post",), permission_classes=(IsAdminUser,))
    def publish(self, request, pk=None):
        with transaction.atomic():
            recipe = ProductionRecipeVersion.objects.select_for_update().get(pk=self.get_object().pk)
            before = lifecycle_summary(recipe)
            if recipe.status != ProductionRecipeVersion.Status.DRAFT:
                raise ValidationError("只有 draft recipe 可以发布")
            try:
                validate_production_recipe_executability(
                    recipe,
                    require_delivery_closure=True,
                )
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
        with transaction.atomic():
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
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    filterset_fields = ("project", "status", "recipe_version")
    queryset = ProductionRun.objects.select_related(
        "project", "owner", "script_revision", "recipe_version", "config_revision",
    ).prefetch_related("stages__approval_decisions", "artifacts")

    def get_queryset(self):
        queryset = self.queryset
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return ProductionRunCreateSerializer
        if self.action == "retrieve":
            return ProductionRunDetailSerializer
        return ProductionRunListSerializer

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
        recipe = get_object_or_404(
            ProductionRecipeVersion,
            pk=data.pop("recipe_version_id"),
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
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
        except (DjangoValidationError, ProductionRunConflict) as exc:
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

    @extend_schema(request=None, responses=ProductionRunDetailSerializer)
    @action(detail=True, methods=("post",), url_path="continue")
    def continue_run(self, request, pk=None):
        run = advance_production_run(self.get_object())
        return Response(ProductionRunDetailSerializer(run).data)

    @extend_schema(
        request=None,
        responses={
            200: ProductionRunDetailSerializer,
            409: ProductionRunConflictResponseSerializer,
        },
    )
    @action(detail=True, methods=("post",))
    def retry(self, request, pk=None):
        try:
            run = retry_production_run(self.get_object(), user=request.user)
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

    @extend_schema(
        request=SelectArtifactInputSerializer,
        responses=GeneratedArtifactDetailSerializer,
    )
    @action(detail=True, methods=("post",), url_path="select-artifact")
    def select_artifact(self, request, pk=None):
        serializer = SelectArtifactInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        artifact = get_object_or_404(
            GeneratedArtifact,
            pk=serializer.validated_data["artifact_id"],
            production_run=self.get_object(),
        )
        try:
            artifact = select_run_artifact(self.get_object(), artifact, user=request.user)
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(GeneratedArtifactDetailSerializer(artifact).data)

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
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(ProductionRunDetailSerializer(run).data)


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
        return queryset.filter(stage_run__production_run__owner=self.request.user)


class WorkflowSmokeAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    serializer_class = WorkflowSmokeAttemptSerializer
    queryset = WorkflowSmokeAttempt.objects.select_related(
        "workflow_version", "deployment", "created_by",
    ).order_by("-created_at")
    filterset_fields = ("workflow_version", "deployment", "status")

    @extend_schema(request=None, responses={202: WorkflowSmokeAttemptSerializer})
    @action(detail=True, methods=("post",), url_path="cancel")
    def cancel(self, request, pk=None):
        try:
            attempt = request_workflow_smoke_cancel(self.get_object())
        except DjangoValidationError as exc:
            raise _drf_validation(exc) from exc
        return Response(
            self.get_serializer(attempt).data,
            status=status.HTTP_202_ACCEPTED,
        )


class ModelDeploymentSmokeAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = (IsAdminUser,)
    serializer_class = ModelDeploymentSmokeAttemptSerializer
    queryset = ModelDeploymentSmokeAttempt.objects.select_related(
        "deployment", "final_snapshot",
    ).order_by("-created_at")
    filterset_fields = ("deployment", "operation", "status")

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
            attempt = request_deployment_smoke_cancel(self.get_object())
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
    queryset = GeneratedArtifact.objects.select_related(
        "production_run__owner", "stage_run", "generation_attempt", "generation_task", "asset",
    )

    def get_queryset(self):
        queryset = self.queryset
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(production_run__owner=self.request.user)

    def get_serializer_class(self):
        return (
            GeneratedArtifactDetailSerializer
            if self.action == "retrieve" else GeneratedArtifactListSerializer
        )
