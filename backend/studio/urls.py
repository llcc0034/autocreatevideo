from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenVerifyView

from studio.capability_views import (
    CreativeDraftCapabilityView,
    H3PromptOptimizerCapabilityView,
    ProductionCapabilityView,
    PromptTranslationCapabilityView,
)
from studio.creative_views import (
    EpisodeCreativeDraftViewSet,
    ProjectCreativeDraftGenerateView,
    ProjectCreativeDraftListView,
)
from studio.shot_detail_draft_views import (
    ProjectShotDetailDraftGenerateView,
    ProjectShotDetailDraftListView,
    ShotDetailDraftViewSet,
)
from studio.shot_detail_batch_views import (
    ProjectShotDetailDraftBatchListCreateView,
    ShotDetailDraftBatchApplyView,
    ShotDetailDraftBatchDetailView,
)
from studio.health_views import ReadinessView
from studio.series_concept_draft_views import SeriesConceptDraftGenerateView
from studio.asset_draft_views import ProjectAssetBibleDraftGenerateView
from studio.bible_image_candidate_views import ProjectBibleImageCandidatesView
from studio.deferred_generation_views import DeferredGenerationRequestViewSet
from studio.visual_rule_draft_views import (
    ProjectVisualRuleDraftApplyView,
    ProjectVisualRuleDraftGenerateView,
)
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
router.register("creative-drafts", EpisodeCreativeDraftViewSet, basename="creative-draft")
router.register("shot-detail-drafts", ShotDetailDraftViewSet, basename="shot-detail-draft")
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

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("health/live/", HealthView.as_view(), name="health-live"),
    path("health/ready/", ReadinessView.as_view(), name="health-ready"),
    path(
        "capabilities/h3-prompt-optimizer/",
        H3PromptOptimizerCapabilityView.as_view(),
        name="h3-prompt-optimizer-capability",
    ),
    path(
        "capabilities/prompt-translation/",
        PromptTranslationCapabilityView.as_view(),
        name="prompt-translation-capability",
    ),
    path(
        "capabilities/creative-drafts/",
        CreativeDraftCapabilityView.as_view(),
        name="creative-draft-capability",
    ),
    path(
        "capabilities/production/",
        ProductionCapabilityView.as_view(),
        name="production-capability",
    ),
    path(
        "series-concept-drafts/generate/",
        SeriesConceptDraftGenerateView.as_view(),
        name="series-concept-draft-generate",
    ),
    path(
        "projects/<uuid:project_id>/creative-drafts/generate/",
        ProjectCreativeDraftGenerateView.as_view(),
        name="project-creative-draft-generate",
    ),
    path(
        "projects/<uuid:project_id>/creative-drafts/",
        ProjectCreativeDraftListView.as_view(),
        name="project-creative-draft-list",
    ),
    path(
        "projects/<uuid:project_id>/shot-detail-drafts/generate/",
        ProjectShotDetailDraftGenerateView.as_view(),
        name="project-shot-detail-draft-generate",
    ),
    path(
        "projects/<uuid:project_id>/shot-detail-drafts/",
        ProjectShotDetailDraftListView.as_view(),
        name="project-shot-detail-draft-list",
    ),
    path(
        "projects/<uuid:project_id>/shot-detail-draft-batches/",
        ProjectShotDetailDraftBatchListCreateView.as_view(),
        name="project-shot-detail-draft-batch-list-create",
    ),
    path(
        "shot-detail-draft-batches/<uuid:batch_id>/",
        ShotDetailDraftBatchDetailView.as_view(),
        name="shot-detail-draft-batch-detail",
    ),
    path(
        "shot-detail-draft-batches/<uuid:batch_id>/apply/",
        ShotDetailDraftBatchApplyView.as_view(),
        name="shot-detail-draft-batch-apply",
    ),
    path(
        "projects/<uuid:project_id>/bible-drafts/from-assets/",
        ProjectAssetBibleDraftGenerateView.as_view(),
        name="project-asset-bible-draft-generate",
    ),
    path(
        "projects/<uuid:id>/bible-image-candidates/",
        ProjectBibleImageCandidatesView.as_view(),
        name="project-bible-image-candidates",
    ),
    path(
        "projects/<uuid:project_id>/visual-rules-drafts/generate/",
        ProjectVisualRuleDraftGenerateView.as_view(),
        name="project-visual-rule-draft-generate",
    ),
    path(
        "projects/<uuid:project_id>/visual-rules-drafts/apply/",
        ProjectVisualRuleDraftApplyView.as_view(),
        name="project-visual-rule-draft-apply",
    ),
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("media/<str:token>/", SignedMediaView.as_view(), name="signed-media"),
    path("comfyui/health/", ComfyUIHealthView.as_view(), name="comfyui-health"),
    path("", include(router.urls)),
]
