from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from studio.authentication import StudioTokenObtainPairSerializer, StudioTokenRefreshSerializer
from studio.access_views import AccessAuditEventViewSet, AccessUserViewSet, PasswordChangeView

from studio.capability_views import (
    ProductionCapabilityView,
    PromptNormalizationCapabilityView,
)
from studio.creative_views import ProjectCreativeDraftListView
from studio.shot_detail_draft_views import (
    ProjectShotDetailDraftListView,
)
from studio.shot_detail_batch_views import (
    ProjectShotDetailDraftBatchListView,
    ShotDetailDraftBatchDetailView,
)
from studio.health_views import ReadinessView
from studio.platform_views import (
    ConfigDefinitionViewSet,
    ConfigRevisionViewSet,
    GeneratedArtifactViewSet,
    GenerationAttemptViewSet,
    ModelArtifactViewSet,
    ModelDeploymentViewSet,
    ModelDeploymentSmokeAttemptViewSet,
    ModelProfileVersionViewSet,
    PlatformLifecycleAuditEventViewSet,
    PromptFieldDefinitionViewSet,
    PromptProfileBindingViewSet,
    PromptProfileFieldRuleViewSet,
    PromptProfileVersionViewSet,
    ProductionReadinessView,
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
    WorkflowSmokeAttemptViewSet,
)
from studio.bible_image_candidate_views import ProjectBibleImageCandidatesView
from studio.deferred_generation_views import DeferredGenerationRequestViewSet
from studio.views import (
    AssetViewSet,
    AssemblyJobViewSet,
    CharacterBibleViewSet,
    ComfyUIHealthView,
    ConsistencyLockViewSet,
    EpisodeViewSet,
    GeneratedWorkflowViewSet,
    GenerationTaskViewSet,
    HealthView,
    MeView,
    PromptArtifactViewSet,
    PromptFieldTranslationViewSet,
    PropBibleViewSet,
    ReleaseBundleViewSet,
    RegisterView,
    SceneBibleViewSet,
    SeriesProjectViewSet,
    SignedMediaView,
    ShotViewSet,
    StyleViewSet,
    WorkflowTemplateViewSet,
)

router = DefaultRouter()
router.register("styles", StyleViewSet, basename="style")
router.register("assets", AssetViewSet, basename="asset")
router.register("projects", SeriesProjectViewSet, basename="project")
router.register("characters", CharacterBibleViewSet, basename="character")
router.register("scenes", SceneBibleViewSet, basename="scene")
router.register("props", PropBibleViewSet, basename="prop")
router.register("consistency-locks", ConsistencyLockViewSet, basename="consistency-lock")
router.register("episodes", EpisodeViewSet, basename="episode")
router.register("shots", ShotViewSet, basename="shot")
router.register("prompts", PromptArtifactViewSet, basename="prompt")
router.register("translations", PromptFieldTranslationViewSet, basename="translation")
router.register("workflow-templates", WorkflowTemplateViewSet, basename="workflow-template")
router.register("workflows", GeneratedWorkflowViewSet, basename="workflow")
router.register("tasks", GenerationTaskViewSet, basename="task")
router.register(
    "deferred-generation-requests",
    DeferredGenerationRequestViewSet,
    basename="deferred-generation-request",
)
router.register("assemblies", AssemblyJobViewSet, basename="assembly")
router.register("release-bundles", ReleaseBundleViewSet, basename="release-bundle")
router.register("provider-profiles", ProviderProfileViewSet, basename="provider-profile")
router.register(
    "model-profile-versions",
    ModelProfileVersionViewSet,
    basename="model-profile-version",
)
router.register(
    "prompt-field-definitions",
    PromptFieldDefinitionViewSet,
    basename="prompt-field-definition",
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
router.register(
    "prompt-profile-bindings",
    PromptProfileBindingViewSet,
    basename="prompt-profile-binding",
)
router.register("model-deployments", ModelDeploymentViewSet, basename="model-deployment")
router.register(
    "model-deployment-smoke-attempts",
    ModelDeploymentSmokeAttemptViewSet,
    basename="model-deployment-smoke-attempt",
)
router.register("model-artifacts", ModelArtifactViewSet, basename="model-artifact")
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
    path("health/", HealthView.as_view(), name="health"),
    path("health/live/", HealthView.as_view(), name="health-live"),
    path("health/ready/", ReadinessView.as_view(), name="health-ready"),
    path(
        "production-readiness/",
        ProductionReadinessView.as_view(),
        name="production-readiness",
    ),
    path(
        "projects/<uuid:project_id>/config/",
        ProjectConfigView.as_view(),
        name="project-config",
    ),
    path(
        "projects/<uuid:project_id>/capabilities/prompt-normalization/",
        PromptNormalizationCapabilityView.as_view(),
        name="project-prompt-normalization-capability",
    ),
    path(
        "capabilities/production/",
        ProductionCapabilityView.as_view(),
        name="production-capability",
    ),
    path(
        "projects/<uuid:project_id>/creative-drafts/",
        ProjectCreativeDraftListView.as_view(),
        name="project-creative-draft-list",
    ),
    path(
        "projects/<uuid:project_id>/shot-detail-drafts/",
        ProjectShotDetailDraftListView.as_view(),
        name="project-shot-detail-draft-list",
    ),
    path(
        "projects/<uuid:project_id>/shot-detail-draft-batches/",
        ProjectShotDetailDraftBatchListView.as_view(),
        name="project-shot-detail-draft-batch-list",
    ),
    path(
        "shot-detail-draft-batches/<uuid:batch_id>/",
        ShotDetailDraftBatchDetailView.as_view(),
        name="shot-detail-draft-batch-detail",
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
