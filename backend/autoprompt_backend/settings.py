"""Django settings for AutoPrompt Studio.

The development default is SQLite. Set DATABASE_URL to use PostgreSQL in a
commercial deployment, for example ``postgresql://user:pass@host/db``.
"""
from datetime import timedelta
from pathlib import Path
import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from autoprompt_backend.deployment_security import validate_production_configuration

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEPLOYMENT_PROFILE = os.getenv("AUTOPROMPT_DEPLOYMENT_PROFILE", "development").strip().casefold()
# A restart-required revision may only be activated by a process from a later
# application boot.  Local restart.ps1 supplies one UUID for migrations, API,
# and workers.  Other launchers intentionally leave this empty until they can
# provide an equally reliable per-boot identity.
AUTOPROMPT_BOOT_ID = os.getenv("AUTOPROMPT_BOOT_ID", "").strip()
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me-autoprompt-studio")
JWT_SIGNING_KEY = os.getenv(
    "AUTOPROMPT_JWT_SIGNING_KEY",
    "dev-only-independent-jwt-signing-key-2026-change-me",
)
if len(JWT_SIGNING_KEY.encode("utf-8")) < 32:
    raise ImproperlyConfigured("AUTOPROMPT_JWT_SIGNING_KEY must contain at least 32 UTF-8 bytes")
if JWT_SIGNING_KEY == SECRET_KEY:
    raise ImproperlyConfigured("AUTOPROMPT_JWT_SIGNING_KEY must differ from DJANGO_SECRET_KEY")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() in {"1", "true", "yes", "on"}
ALLOWED_HOSTS = [x.strip() for x in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if x.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_filters",
    "studio.apps.StudioConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # The anonymous report token is a bearer credential.  Route first, then
    # redact it before Django logs a rejected or throttled response.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # Resolve a trusted edge country assertion once per request.  Individual
    # views enforce it only after resolving that the selected model is H3, so
    # SDXL and unrelated APIs remain available.
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "autoprompt_backend.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "autoprompt_backend.wsgi.application"
ASGI_APPLICATION = "autoprompt_backend.asgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{(BASE_DIR / 'db.sqlite3').as_posix()}",
        conn_max_age=60,
        conn_health_checks=True,
    )
}

# ``sqlite:///db.sqlite3`` is intentionally convenient in .env files, but
# dj-database-url otherwise resolves it against the caller's current working
# directory.  Pin relative SQLite names to the backend root so management
# commands, tests and desktop launchers can never open a second accidental DB.
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    sqlite_name = str(DATABASES["default"].get("NAME") or "")
    if sqlite_name and sqlite_name != ":memory:" and not Path(sqlite_name).is_absolute():
        DATABASES["default"]["NAME"] = str((BASE_DIR / sqlite_name).resolve())

# SQLite is kept only as a convenient single-machine development profile. The
# API, generation worker, assembly worker, and their heartbeat threads can all
# write concurrently, so Django's short default busy timeout is too brittle.
# IMMEDIATE makes an atomic block wait for the write lock at BEGIN instead of
# failing later while upgrading a deferred transaction. This reduces transient
# local failures; it does not turn SQLite into a supported production backend.
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    DATABASES["default"].setdefault("OPTIONS", {}).update({
        "timeout": float(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30")),
        "transaction_mode": os.getenv("SQLITE_TRANSACTION_MODE", "IMMEDIATE"),
    })

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
ASSET_LIBRARY_URL = "/assets/"
ASSET_LIBRARY_ROOT = BASE_DIR.parent / "assets"
SERVE_ASSETS_WITH_DJANGO = os.getenv("SERVE_ASSETS_WITH_DJANGO", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
CORS_ALLOWED_ORIGINS = [x.strip() for x in os.getenv(
    "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",") if x.strip()]
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]
X_FRAME_OPTIONS = "DENY"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("studio.authentication.StudioJWTAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "studio.pagination.StandardPageNumberPagination",
    "DEFAULT_THROTTLE_CLASSES": (
        "studio.throttling.DatabaseAnonRateThrottle",
        "studio.throttling.DatabaseUserRateThrottle",
        "studio.throttling.DatabaseScopedRateThrottle",
    ),
    # Production traffic traverses the external TLS terminator and the bundled
    # Nginx container.  The terminator must overwrite X-Forwarded-For; DRF then
    # selects the address before those two trusted hops for anonymous limits.
    "NUM_PROXIES": int(os.getenv(
        "DRF_NUM_PROXIES", "2" if DEPLOYMENT_PROFILE == "production" else "0",
    )),
    "PAGE_SIZE": 24,
    "EXCEPTION_HANDLER": "studio.exceptions.api_exception_handler",
}

SIMPLE_JWT = {
    "ALGORITHM": "HS256",
    "SIGNING_KEY": JWT_SIGNING_KEY,
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "AutoPrompt Studio API",
    "DESCRIPTION": "连续短剧、版本化提示词、素材与受管媒体工作流 API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "BindingScopeEnum": ("exact_model", "model_family"),
        "AssetKindEnum": "studio.models.Asset.Kind",
        "AssetGenerationJobTypeEnum": "studio.models.AssetGenerationJob.JobType",
        "AssetGenerationMediaTypeEnum": "studio.models.AssetGenerationJob.MediaType",
        "AssetGenerationExecutionKindEnum": "studio.models.AssetGenerationJob.ExecutionKind",
        "AssetGenerationStatusEnum": "studio.models.AssetGenerationJob.Status",
        "AssetGenerationResolutionOutcomeEnum": "studio.models.AssetGenerationJobResolution.Outcome",
        "MediaKindEnum": "studio.models.Shot.MediaType",
        "SeriesProjectStatusEnum": "studio.models.SeriesProject.Status",
        "EpisodeStatusEnum": "studio.models.Episode.Status",
        "GenerationTaskStatusEnum": "studio.models.GenerationTask.Status",
        "AssemblyJobStatusEnum": "studio.models.AssemblyJob.Status",
        "ComfyUIWorkflowFormatEnum": ("comfyui_api", "comfyui_ui"),
        "OperationAggregateStatusEnum": ("complete", "partial", "failed"),
        "WorkflowDiscoverySourceStatusEnum": ("succeeded", "failed"),
        "WorkflowDiscoveryMatchStatusEnum": (
            "matched", "ambiguous", "unmatched", "blocked",
        ),
        "ComfyWorkflowAdmissionTierEnum": (
            "candidate", "generic_fixed", "projected_dynamic",
        ),
        "ComfyWorkflowContractActionabilityEnum": (
            "candidate_only", "ready_for_executable_import",
        ),
        "ComfyWorkflowPreviewActionabilityEnum": (
            "select_primary_output", "candidate_only", "ready_for_executable_import",
        ),
        "WorkflowOutputKindEnum": "studio.models.WorkflowTemplate.OutputKind",
        "DraftPublishedStatusEnum": ("draft", "published"),
        "PromptPolicyModeEnum": ("warn", "block"),
        # Video and SDXL requests intentionally share the same official
        # megapixel profile set. Give that shared choice set one stable schema
        # component name so drf-spectacular does not invent competing names.
        "ProductionResolutionProfileEnum": (
            "project", "0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7",
            "0.8", "0.9", "0.98", "1.0", "1.2", "1.5", "1.8", "2.0",
        ),
        # Prompt translations use the complete model scope set, while Bible
        # selectors and promoted reference outputs intentionally share the
        # character/scene/prop subset. Stable names prevent schema generators
        # from inventing hash-suffixed ScopeType enums for the same contracts.
        "PromptTranslationScopeTypeEnum": "studio.models.PromptFieldTranslation.ScopeType",
        "BibleEntityTypeEnum": ("character", "scene", "prop"),
        "DeferredGenerationStatusEnum": "studio.models.DeferredGenerationRequest.Status",
        "DeferredFrameSlotEnum": "studio.models.DeferredGenerationRequest.FrameSlot",
        "ProviderDriverTypeEnum": "studio.models.ProviderProfile.DriverType",
        "ModelProfileModalityEnum": "studio.models.ModelProfileVersion.Modality",
        "PublishedVersionStatusEnum": "studio.models.ModelProfileVersion.Status",
        "ModelDeploymentStatusEnum": "studio.models.ModelDeployment.Status",
        "ModelArtifactRoleEnum": "studio.models.ModelArtifact.Role",
        "AccessRoleEnum": ("member", "platform_admin", "super_admin"),
        "ModelArtifactStatusEnum": "studio.models.ModelArtifact.Status",
        "PromptFieldValueTypeEnum": "studio.models.PromptFieldDefinition.ValueType",
        "PromptProfileReviewStateEnum": "studio.models.PromptProfileVersion.ReviewState",
        "PromptProfileSourceSelectorEnum": "studio.models.PromptProfileFieldRule.SourceSelector",
        "PromptProfileChannelEnum": "studio.models.PromptProfileFieldRule.Channel",
        "PromptProfileLabelStyleEnum": "studio.models.PromptProfileFieldRule.LabelStyle",
        "PromptProfileSeparatorEnum": "studio.models.PromptProfileFieldRule.Separator",
        "WorkflowVersionStatusEnum": "studio.models.WorkflowTemplateVersion.Status",
        "WorkflowValidationStateEnum": "studio.models.WorkflowTemplateVersion.ValidationState",
        "ConfigValueTypeEnum": "studio.models.ConfigDefinition.ValueType",
        "ConfigScopeTypeEnum": "studio.models.ConfigRevision.ScopeType",
        "ConfigRevisionStatusEnum": "studio.models.ConfigRevision.Status",
        "RuntimeHostStatusEnum": "studio.models.RuntimeHost.Status",
        "HostProbeStatusEnum": "studio.models.HostCapabilitySnapshot.ProbeStatus",
        "RuntimeModelAvailabilityStatusEnum": "studio.models.RuntimeModelAvailability.Status",
        "RecommendationStatusEnum": "studio.models.RecommendationSnapshot.Status",
        "ScriptSourceTypeEnum": "studio.models.ScriptSource.SourceType",
        "ScriptSourceFormatEnum": "studio.models.ScriptSource.SourceFormat",
        "ScriptSourceStatusEnum": "studio.models.ScriptSource.Status",
        "ScriptRevisionStatusEnum": "studio.models.ScriptRevision.Status",
        "ProductionRunStatusEnum": "studio.models.ProductionRun.Status",
        "ProductionStageStatusEnum": "studio.models.StageRun.Status",
        "ApprovalDecisionEnum": "studio.models.ApprovalDecision.Decision",
        "RecommendationDecisionEnum": "studio.models.RecommendationDecision.Decision",
        "GenerationAttemptStatusEnum": "studio.models.GenerationAttempt.Status",
        "GenerationAttemptResolutionOutcomeEnum": "studio.models.GenerationAttemptResolution.Outcome",
        "SmokeAttemptResolutionOutcomeEnum": "studio.models.ModelDeploymentSmokeAttemptResolution.Outcome",
        "WorkflowSmokeResolutionOutcomeEnum": "studio.models.WorkflowSmokeAttemptResolution.Outcome",
        "GeneratedArtifactKindEnum": "studio.models.GeneratedArtifact.Kind",
        "GeneratedArtifactStatusEnum": "studio.models.GeneratedArtifact.Status",
        "ModelTransportEnum": ("local_process", "remote_api"),
        "BibleImageTransportEnum": ("local",),
    },
}

COMFYUI_OUTPUT_DIR = os.getenv("COMFYUI_OUTPUT_DIR", "")
COMFYUI_ALLOW_NON_LOOPBACK = os.getenv("COMFYUI_ALLOW_NON_LOOPBACK", "false").lower() in {"1", "true", "yes", "on"}
AI_PROVIDER_ALLOWED_HOSTS = tuple(
    item.strip().casefold()
    for item in os.getenv(
        "AI_PROVIDER_ALLOWED_HOSTS",
        "localhost,127.0.0.1,::1",
    ).split(",")
    if item.strip()
)
COMFYUI_EXTRA_ALLOWED_NODE_TYPES = tuple(
    item.strip() for item in os.getenv("COMFYUI_EXTRA_ALLOWED_NODE_TYPES", "").split(",") if item.strip()
)
LOCAL_QWEN_LOCK_PATH = os.getenv(
    "LOCAL_QWEN_LOCK_PATH", str(BASE_DIR.parent / ".runtime" / "locks" / "local-qwen.lock"),
).strip()
# Hard deployment ceiling for the local worker pool. Product defaults and
# recommended concurrency come from versioned configuration and host evidence.
TASK_MAX_CONCURRENT = int(os.getenv("TASK_MAX_CONCURRENT", "1"))
if TASK_MAX_CONCURRENT < 1:
    raise ImproperlyConfigured("TASK_MAX_CONCURRENT must be greater than zero")
TASK_PROGRESS_WEBSOCKET_ENABLED = os.getenv(
    "TASK_PROGRESS_WEBSOCKET_ENABLED", "true",
).lower() in {"1", "true", "yes", "on"}
FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg")
FFPROBE_BINARY = os.getenv("FFPROBE_BINARY", "ffprobe")
ASSEMBLY_AI_LABEL_FONTFILE = os.getenv("ASSEMBLY_AI_LABEL_FONTFILE", "")
# A task output is a private production master.  Public downloads are separate,
# content-addressed distribution copies with a visible disclosure and embedded
# metadata; disabling this setting makes public task-output downloads fail
# closed instead of exposing the ComfyUI master.
TASK_DISTRIBUTION_FONTFILE = os.getenv(
    "TASK_DISTRIBUTION_FONTFILE", ASSEMBLY_AI_LABEL_FONTFILE,
)

# Sensible security defaults for deployments that explicitly disable DEBUG.
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
    CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "true").lower() == "true"
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "true").lower() == "true"
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "false").lower() == "true"
    SECURE_HSTS_PRELOAD = os.getenv("SECURE_HSTS_PRELOAD", "false").lower() == "true"
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

validate_production_configuration(
    profile=DEPLOYMENT_PROFILE,
    debug=DEBUG,
    secret_key=SECRET_KEY,
    jwt_signing_key=JWT_SIGNING_KEY,
    database_engine=DATABASES["default"]["ENGINE"],
    database_password=DATABASES["default"].get("PASSWORD") or "",
    allowed_hosts=ALLOWED_HOSTS,
    cors_allowed_origins=CORS_ALLOWED_ORIGINS,
    csrf_trusted_origins=CSRF_TRUSTED_ORIGINS,
    secure_ssl_redirect=bool(globals().get("SECURE_SSL_REDIRECT", False)),
    session_cookie_secure=bool(globals().get("SESSION_COOKIE_SECURE", False)),
    csrf_cookie_secure=bool(globals().get("CSRF_COOKIE_SECURE", False)),
    secure_hsts_seconds=int(globals().get("SECURE_HSTS_SECONDS", 0)),
)
