from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from studio.authentication import StudioTokenObtainPairSerializer, StudioTokenRefreshSerializer
from studio.access_views import AccessAuditEventViewSet, AccessUserViewSet, PasswordChangeView

from studio.capability_views import (
    ProductionCapabilityView,
    VideoConditioningCapabilityView,
    ProjectCreationCapabilityView,
)
from studio.health_views import ReadinessView
from studio.maintenance_views import HistoryCleanupView, HistoryCleanupPauseView
from studio.content_source_views import ContentSourceVersionViewSet
from studio.field_definition_views import FieldDefinitionVersionViewSet
from studio.data_structure_views import DataStructureVersionViewSet
from studio.business_output_views import BusinessOutputVersionViewSet
from studio.registry_relation_views import RegistryRelationsView
from studio.platform_views import (
    AcceptanceSmokeView,
    ComfyWorkflowContractDraftViewSet,
    ComfyWorkflowSourceViewSet,
    ConfigDefinitionViewSet,
    ConfigRevisionViewSet,
    GeneratedArtifactViewSet,
    GenerationAttemptViewSet,
    ModelArtifactViewSet,
    ModelDeploymentViewSet,
    ModelDeploymentSmokeAttemptViewSet,
    ModelProfileVersionViewSet,
    PlatformLifecycleAuditEventViewSet,
    FieldDefinitionViewSet,
    PromptProfileFieldRuleViewSet,
    PromptProfileVersionViewSet,
    ProductionReadinessView,
    RegistryDeletionExecuteView,
    RegistryDeletionPreviewView,
    ProductionRecipeVersionViewSet,
    ProductionRunViewSet,
    ProjectConfigView,
    ProviderProfileViewSet,
    RecommendationSnapshotViewSet,
    RuntimeHostProbeAttemptViewSet,
    RuntimeHostViewSet,
    ScriptRevisionViewSet,
    ScriptSourceViewSet,
    SecretAuditEventViewSet,
    SecretBindingViewSet,
    WorkflowTemplateVersionViewSet,
    WorkflowProjectorViewSet,
    WorkflowProjectorVersionViewSet,
    WorkflowSmokeAttemptViewSet,
)
from studio.bible_image_candidate_views import ProjectBibleImageCandidatesView
from studio.bible_variant_views import BibleVariantViewSet
from studio.asset_reference_views import AssetReferenceSeriesViewSet, AssetReferenceVersionViewSet
from studio.views import (
    AssetViewSet,
    AssemblyJobViewSet,
    CharacterBibleViewSet,
    ComfyUIHealthView,
    ConsistencyLockViewSet,
    EpisodeViewSet,
    GenerationTaskViewSet,
    HealthView,
    MeView,
    PromptFieldTranslationViewSet,
    PropBibleViewSet,
    ReleaseBundleViewSet,
    RegisterView,
    SceneBibleViewSet,
    SeriesProjectViewSet,
    SignedMediaView,
    ShotViewSet,
    StyleViewSet,
    StyleCategoryViewSet,
    WorkflowTemplateViewSet,
)
from studio.asset_generation_views import AssetGenerationJobViewSet
from studio.execution_instance_views import ModelExecutionInstanceViewSet, ModelExecutionInstanceVersionViewSet
from studio.execution_preparation_views import ExecutionPreparationViewSet, ExecutionFieldValueViewSet
from studio.task_center_views import TaskCenterListView, TaskCenterDetailView, TaskCenterOptionsView

router = DefaultRouter()
router.register("field-definition-versions", FieldDefinitionVersionViewSet, basename="field-definition-version")
router.register("data-structure-versions", DataStructureVersionViewSet, basename="data-structure-version")
router.register("content-source-versions", ContentSourceVersionViewSet, basename="content-source-version")
router.register("business-output-versions", BusinessOutputVersionViewSet, basename="business-output-version")
router.register("styles", StyleViewSet, basename="style")
router.register("style-categories", StyleCategoryViewSet, basename="style-category")
router.register("assets", AssetViewSet, basename="asset")
router.register("asset-reference-series", AssetReferenceSeriesViewSet, basename="asset-reference-series")
router.register("asset-reference-versions", AssetReferenceVersionViewSet, basename="asset-reference-version")
router.register(
    "asset-generation-jobs",
    AssetGenerationJobViewSet,
    basename="asset-generation-job",
)
router.register("projects", SeriesProjectViewSet, basename="project")
router.register("bible-variants", BibleVariantViewSet, basename="bible-variant")
router.register("characters", CharacterBibleViewSet, basename="character")
router.register("scenes", SceneBibleViewSet, basename="scene")
router.register("props", PropBibleViewSet, basename="prop")
router.register("consistency-locks", ConsistencyLockViewSet, basename="consistency-lock")
router.register("episodes", EpisodeViewSet, basename="episode")
router.register("shots", ShotViewSet, basename="shot")
router.register("translations", PromptFieldTranslationViewSet, basename="translation")
router.register("workflow-templates", WorkflowTemplateViewSet, basename="workflow-template")
router.register("tasks", GenerationTaskViewSet, basename="task")
router.register("assemblies", AssemblyJobViewSet, basename="assembly")
router.register("release-bundles", ReleaseBundleViewSet, basename="release-bundle")
router.register("provider-profiles", ProviderProfileViewSet, basename="provider-profile")
router.register(
    "model-profile-versions",
    ModelProfileVersionViewSet,
    basename="model-profile-version",
)
router.register(
    "field-definitions",
    FieldDefinitionViewSet,
    basename="field-definition",
)
router.register(
    "prompt-profile-versions",
    PromptProfileVersionViewSet,
    basename="prompt-profile-version",
)
router.register(
    "prompt-profile-field-rules",
    PromptProfileFieldRuleViewSet,
    basename="prompt-profile-field-rule",
)
router.register("model-deployments", ModelDeploymentViewSet, basename="model-deployment")
router.register("model-execution-instances", ModelExecutionInstanceViewSet, basename="model-execution-instance")
router.register("model-execution-instance-versions", ModelExecutionInstanceVersionViewSet, basename="model-execution-instance-version")
router.register("execution-preparations", ExecutionPreparationViewSet, basename="execution-preparation")
router.register("execution-field-values", ExecutionFieldValueViewSet, basename="execution-field-value")
router.register(
    "model-deployment-smoke-attempts",
    ModelDeploymentSmokeAttemptViewSet,
    basename="model-deployment-smoke-attempt",
)
router.register("model-artifacts", ModelArtifactViewSet, basename="model-artifact")
router.register(
    "comfy-workflow-sources",
    ComfyWorkflowSourceViewSet,
    basename="comfy-workflow-source",
)
router.register(
    "comfy-workflow-contract-drafts",
    ComfyWorkflowContractDraftViewSet,
    basename="comfy-workflow-contract-draft",
)
router.register(
    "workflow-projectors",
    WorkflowProjectorViewSet,
    basename="workflow-projector",
)
router.register(
    "workflow-projector-versions",
    WorkflowProjectorVersionViewSet,
    basename="workflow-projector-version",
)
router.register(
    "workflow-template-versions",
    WorkflowTemplateVersionViewSet,
    basename="workflow-template-version",
)
router.register(
    "workflow-smoke-attempts",
    WorkflowSmokeAttemptViewSet,
    basename="workflow-smoke-attempt",
)
router.register("config-definitions", ConfigDefinitionViewSet, basename="config-definition")
router.register("config-revisions", ConfigRevisionViewSet, basename="config-revision")
router.register("runtime-hosts", RuntimeHostViewSet, basename="runtime-host")
router.register(
    "runtime-host-probe-attempts",
    RuntimeHostProbeAttemptViewSet,
    basename="runtime-host-probe-attempt",
)
router.register("recommendations", RecommendationSnapshotViewSet, basename="recommendation")
router.register("script-sources", ScriptSourceViewSet, basename="script-source")
router.register("script-revisions", ScriptRevisionViewSet, basename="script-revision")
router.register(
    "production-recipes",
    ProductionRecipeVersionViewSet,
    basename="production-recipe",
)
router.register("production-runs", ProductionRunViewSet, basename="production-run")
router.register("generation-attempts", GenerationAttemptViewSet, basename="generation-attempt")
router.register("generated-artifacts", GeneratedArtifactViewSet, basename="generated-artifact")
router.register("secret-bindings", SecretBindingViewSet, basename="secret-binding")
router.register("secret-audit-events", SecretAuditEventViewSet, basename="secret-audit-event")
router.register(
    "platform-lifecycle-audit-events",
    PlatformLifecycleAuditEventViewSet,
    basename="platform-lifecycle-audit-event",
)
router.register("access/users", AccessUserViewSet, basename="access-user")
router.register("access/audit-events", AccessAuditEventViewSet, basename="access-audit-event")

urlpatterns = [
    path("maintenance/history/", HistoryCleanupView.as_view(), name="history-cleanup"),
    path("maintenance/history/background-pause/", HistoryCleanupPauseView.as_view(), name="history-cleanup-pause"),
    path("task-center/", TaskCenterListView.as_view(), name="task-center-list"),
    path("task-center/options/", TaskCenterOptionsView.as_view(), name="task-center-options"),
    path("task-center/<str:kind>/<uuid:identifier>/", TaskCenterDetailView.as_view(), name="task-center-detail"),
    path("health/", HealthView.as_view(), name="health"),
    path("health/live/", HealthView.as_view(), name="health-live"),
    path("health/ready/", ReadinessView.as_view(), name="health-ready"),
    path(
        "production-readiness/",
        ProductionReadinessView.as_view(),
        name="production-readiness",
    ),
    path(
        "acceptance-smoke/",
        AcceptanceSmokeView.as_view(),
        name="acceptance-smoke",
    ),
    path("registry-relations/", RegistryRelationsView.as_view(), name="registry-relations"),
    path(
        "registry-deletion-preview/",
        RegistryDeletionPreviewView.as_view(),
        name="registry-deletion-preview",
    ),
    path(
        "registry-deletion-execute/",
        RegistryDeletionExecuteView.as_view(),
        name="registry-deletion-execute",
    ),
    path(
        "projects/<uuid:project_id>/config/",
        ProjectConfigView.as_view(),
        name="project-config",
    ),
    path(
        "capabilities/project-creation/",
        ProjectCreationCapabilityView.as_view(),
        name="project-creation-capability",
    ),
    path(
        "capabilities/video-conditioning/",
        VideoConditioningCapabilityView.as_view(),
        name="video-conditioning-capability",
    ),
    path(
        "capabilities/production/",
        ProductionCapabilityView.as_view(),
        name="production-capability",
    ),
    path(
        "projects/<uuid:id>/bible-image-candidates/",
        ProjectBibleImageCandidatesView.as_view(),
        name="project-bible-image-candidates",
    ),
    path("auth/register/", RegisterView.as_view(), name="register"),
    path(
        "auth/token/",
        TokenObtainPairView.as_view(serializer_class=StudioTokenObtainPairSerializer),
        name="token_obtain_pair",
    ),
    path(
        "auth/token/refresh/",
        TokenRefreshView.as_view(serializer_class=StudioTokenRefreshSerializer),
        name="token_refresh",
    ),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/password/change/", PasswordChangeView.as_view(), name="password-change"),
    path("media/<str:token>/", SignedMediaView.as_view(), name="signed-media"),
    path("comfyui/health/", ComfyUIHealthView.as_view(), name="comfyui-health"),
    path("", include(router.urls)),
]
