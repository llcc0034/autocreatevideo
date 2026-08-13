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
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me-autoprompt-studio")
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
ALLOW_PUBLIC_REGISTRATION = os.getenv("ALLOW_PUBLIC_REGISTRATION", "false").lower() in {"1", "true", "yes", "on"}
ASSET_UPLOAD_MAX_BYTES = int(os.getenv("ASSET_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
ASSET_VIDEO_MAX_BYTES = int(os.getenv("ASSET_VIDEO_MAX_BYTES", str(250 * 1024 * 1024)))
ASSET_AUDIO_MAX_BYTES = int(os.getenv("ASSET_AUDIO_MAX_BYTES", str(50 * 1024 * 1024)))
ASSET_TENANT_QUOTA_BYTES = int(os.getenv("ASSET_TENANT_QUOTA_BYTES", str(2 * 1024 * 1024 * 1024)))
ASSET_IMAGE_MAX_WIDTH = int(os.getenv("ASSET_IMAGE_MAX_WIDTH", "8192"))
ASSET_IMAGE_MAX_HEIGHT = int(os.getenv("ASSET_IMAGE_MAX_HEIGHT", "8192"))
ASSET_IMAGE_MAX_PIXELS = int(os.getenv("ASSET_IMAGE_MAX_PIXELS", "40000000"))
MEDIA_SIGNED_URL_MAX_AGE_SECONDS = int(os.getenv("MEDIA_SIGNED_URL_MAX_AGE_SECONDS", "900"))

CORS_ALLOWED_ORIGINS = [x.strip() for x in os.getenv(
    "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",") if x.strip()]
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]
X_FRAME_OPTIONS = "DENY"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("rest_framework_simplejwt.authentication.JWTAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "studio.pagination.StandardPageNumberPagination",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.getenv("THROTTLE_ANON_RATE", "60/minute"),
        "user": os.getenv("THROTTLE_USER_RATE", "600/minute"),
        "registration": os.getenv("THROTTLE_REGISTRATION_RATE", "5/hour"),
        "asset_upload": os.getenv("THROTTLE_ASSET_UPLOAD_RATE", "30/hour"),
    },
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
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "AutoPrompt Studio API",
    "DESCRIPTION": "连续短剧、MiniMax H3 提示词、素材与 ComfyUI 工作流 API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SECURITY": [{"jwtAuth": []}],
    "ENUM_NAME_OVERRIDES": {
        "AssetKindEnum": "studio.models.Asset.Kind",
        "MediaKindEnum": "studio.models.Shot.MediaType",
        "SeriesProjectStatusEnum": "studio.models.SeriesProject.Status",
        "EpisodeStatusEnum": "studio.models.Episode.Status",
        "GenerationTaskStatusEnum": "studio.models.GenerationTask.Status",
        "AssemblyJobStatusEnum": "studio.models.AssemblyJob.Status",
        "ComfyUIWorkflowFormatEnum": ("comfyui_api", "comfyui_ui"),
        "PromptPolicyModeEnum": ("warn", "block"),
        # Video and SDXL requests intentionally share the same official
        # megapixel profile set. Give that shared choice set one stable schema
        # component name so drf-spectacular does not invent competing names.
        "ProductionResolutionProfileEnum": (
            "project", "0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7",
            "0.8", "0.9", "0.98", "1.0", "1.2", "1.5", "1.8", "2.0",
        ),
        "H3ReferenceVideoPurposeEnum": (
            "motion_camera_reference",
            "video_continuation",
            "video_editing",
        ),
        "H3ReferenceAudioPurposeEnum": ("audio_reference", "audio_reuse"),
        # Prompt translations use the complete model scope set, while Bible
        # selectors and promoted reference outputs intentionally share the
        # character/scene/prop subset. Stable names prevent schema generators
        # from inventing hash-suffixed ScopeType enums for the same contracts.
        "PromptTranslationScopeTypeEnum": "studio.models.PromptFieldTranslation.ScopeType",
        "BibleEntityTypeEnum": ("character", "scene", "prop"),
        # Episode and single-shot proposals deliberately use one lifecycle.
        # Name that shared choice set explicitly so the generated OpenAPI
        # component remains stable instead of receiving a hash suffix.
        "CreativeDraftStatusEnum": "studio.models.EpisodeCreativeDraft.Status",
    },
}

COMFYUI_BASE_URL = os.getenv("COMFYUI_BASE_URL", "http://127.0.0.1:8188").rstrip("/")
COMFYUI_CLIENT_ID = os.getenv("COMFYUI_CLIENT_ID", "autoprompt-studio")
COMFYUI_TIMEOUT_SECONDS = float(os.getenv("COMFYUI_TIMEOUT_SECONDS", "30"))
COMFYUI_OUTPUT_DIR = os.getenv("COMFYUI_OUTPUT_DIR", "")
COMFYUI_ALLOW_NON_LOOPBACK = os.getenv("COMFYUI_ALLOW_NON_LOOPBACK", "false").lower() in {"1", "true", "yes", "on"}
COMFYUI_EXTRA_ALLOWED_NODE_TYPES = tuple(
    item.strip() for item in os.getenv("COMFYUI_EXTRA_ALLOWED_NODE_TYPES", "").split(",") if item.strip()
)
MINIMAX_H3_MODEL = os.getenv("MINIMAX_H3_MODEL", "minimax-h3")
H3_PROMPT_OPTIMIZER = {
    # Privacy-first default: no prompt leaves this process until an operator
    # explicitly enables and configures an OpenAI-compatible endpoint.
    "enabled": os.getenv("H3_PROMPT_OPTIMIZER_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
    "base_url": os.getenv("H3_PROMPT_OPTIMIZER_BASE_URL", "").strip(),
    "api_key": os.getenv("H3_PROMPT_OPTIMIZER_API_KEY", "").strip(),
    "model": os.getenv("H3_PROMPT_OPTIMIZER_MODEL", "MiniMax-M2.7").strip(),
    "allow_non_loopback": os.getenv("H3_PROMPT_OPTIMIZER_ALLOW_NON_LOOPBACK", "false").lower()
    in {"1", "true", "yes", "on"},
    "timeout_seconds": float(os.getenv("H3_PROMPT_OPTIMIZER_TIMEOUT_SECONDS", "60")),
    "failure_mode": os.getenv("H3_PROMPT_OPTIMIZER_FAILURE_MODE", "warn").strip().lower(),
}
H3_PROMPT_LANGUAGE_GATE = os.getenv("H3_PROMPT_LANGUAGE_GATE", "block").strip().lower()
if H3_PROMPT_LANGUAGE_GATE not in {"block", "warn"}:
    raise ImproperlyConfigured("H3_PROMPT_LANGUAGE_GATE must be either 'block' or 'warn'")

# Optional remote prompt translation/expansion provider.  The API key stays in
# the backend process environment and must never be returned by capability APIs
# or sent to the browser.  Selecting this provider explicitly allows the
# relevant prompt and project context to leave the local deployment.
DEEPSEEK_API = {
    "enabled": os.getenv("DEEPSEEK_ENABLED", "false").lower()
    in {"1", "true", "yes", "on"},
    "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
    "api_key": os.getenv("DEEPSEEK_API_KEY", "").strip(),
    "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
    "timeout_seconds": float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "180")),
    "max_retries": int(os.getenv("DEEPSEEK_MAX_RETRIES", "1")),
    "max_tokens": int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096")),
}
if DEEPSEEK_API["timeout_seconds"] <= 0:
    raise ImproperlyConfigured("DEEPSEEK_TIMEOUT_SECONDS must be greater than zero")
if not 0 <= DEEPSEEK_API["max_retries"] <= 3:
    raise ImproperlyConfigured("DEEPSEEK_MAX_RETRIES must be between 0 and 3")
if not 1 <= DEEPSEEK_API["max_tokens"] <= 32768:
    raise ImproperlyConfigured("DEEPSEEK_MAX_TOKENS must be between 1 and 32768")
if not DEEPSEEK_API["base_url"]:
    raise ImproperlyConfigured("DEEPSEEK_BASE_URL must not be empty")
if not DEEPSEEK_API["model"]:
    raise ImproperlyConfigured("DEEPSEEK_MODEL must not be empty")
if DEEPSEEK_API["enabled"] and not DEEPSEEK_API["api_key"]:
    raise ImproperlyConfigured(
        "DEEPSEEK_API_KEY must be set when DEEPSEEK_ENABLED is true"
    )

_PROMPT_PROVIDER_IDS = {"local_qwen", "deepseek_api"}


def _validate_prompt_provider(value: str, setting_name: str) -> str:
    provider = str(value or "local_qwen").strip().lower()
    if provider not in _PROMPT_PROVIDER_IDS:
        allowed = ", ".join(sorted(_PROMPT_PROVIDER_IDS))
        raise ImproperlyConfigured(f"{setting_name} must be one of: {allowed}")
    return provider


PROMPT_TRANSLATION_DEFAULT_PROVIDER = _validate_prompt_provider(
    os.getenv("PROMPT_TRANSLATION_DEFAULT_PROVIDER", "local_qwen"),
    "PROMPT_TRANSLATION_DEFAULT_PROVIDER",
)
PROMPT_CREATIVE_DEFAULT_PROVIDER = _validate_prompt_provider(
    os.getenv("PROMPT_CREATIVE_DEFAULT_PROVIDER", "local_qwen"),
    "PROMPT_CREATIVE_DEFAULT_PROVIDER",
)

# Human-reviewed local translation.  The model is loaded in a disposable child
# process only for a translation request and exits before any ComfyUI task is
# submitted.  CPU remains the safe default for the 8 GB GTX 1070 workstation.
# CUDA is opt-in and must point at a separately installed CUDA llama.cpp
# runtime; the request runtime verifies GPU offload and never falls back to CPU.
_QWEN_GPU_LAYERS_RAW = os.getenv("QWEN_GPU_LAYERS", "-1").strip()
try:
    QWEN_GPU_LAYERS = int(_QWEN_GPU_LAYERS_RAW)
except ValueError as exc:
    raise ImproperlyConfigured("QWEN_GPU_LAYERS must be an integer; use -1 for all layers") from exc
if QWEN_GPU_LAYERS < -1:
    raise ImproperlyConfigured("QWEN_GPU_LAYERS must be -1 or a non-negative integer")


def _validate_qwen_device(value: str, setting_name: str) -> str:
    device = str(value or "cpu").strip().lower()
    if device not in {"cpu", "cuda"}:
        raise ImproperlyConfigured(f"{setting_name} must be either 'cpu' or 'cuda'")
    if device == "cuda" and QWEN_GPU_LAYERS == 0:
        raise ImproperlyConfigured(
            "QWEN_GPU_LAYERS must be -1 or greater than zero when Qwen CUDA is enabled; "
            "zero would silently disable GPU offload"
        )
    return device


LOCAL_PROMPT_TRANSLATION = {
    "enabled": os.getenv("QWEN_TRANSLATION_ENABLED", os.getenv("LOCAL_PROMPT_TRANSLATION_ENABLED", "true")).lower()
    in {"1", "true", "yes", "on"},
    "model_id": os.getenv(
        "QWEN_TRANSLATION_MODEL_ID",
        os.getenv("LOCAL_PROMPT_TRANSLATION_MODEL_ID", "Qwen/Qwen3-4B-GGUF"),
    ).strip(),
    "model_dir": os.getenv(
        "QWEN_TRANSLATION_MODEL_DIR",
        os.getenv("LOCAL_PROMPT_TRANSLATION_MODEL_DIR", str(BASE_DIR.parent / ".runtime" / "models" / "Qwen" / "Qwen3-4B-GGUF")),
    ).strip(),
    "runtime_script": os.getenv(
        "QWEN_TRANSLATION_RUNTIME_SCRIPT",
        os.getenv("LOCAL_PROMPT_TRANSLATION_RUNTIME_SCRIPT", str(BASE_DIR.parent / "scripts" / "qwen_translate_once.py")),
    ).strip(),
    "runtime_python": os.getenv(
        "QWEN_TRANSLATION_RUNTIME_PYTHON",
        os.getenv("LOCAL_PROMPT_TRANSLATION_RUNTIME_PYTHON", str(BASE_DIR.parent / ".runtime" / "qwen-python" / "Scripts" / "python.exe")),
    ).strip(),
    "device": _validate_qwen_device(
        os.getenv("QWEN_TRANSLATION_DEVICE", os.getenv("LOCAL_PROMPT_TRANSLATION_DEVICE", "cpu")),
        "QWEN_TRANSLATION_DEVICE",
    ),
    "gpu_layers": QWEN_GPU_LAYERS,
    "timeout_seconds": float(os.getenv("QWEN_TRANSLATION_TIMEOUT_SECONDS", os.getenv("LOCAL_PROMPT_TRANSLATION_TIMEOUT_SECONDS", "300"))),
    "max_new_tokens": int(os.getenv("QWEN_TRANSLATION_MAX_NEW_TOKENS", os.getenv("LOCAL_PROMPT_TRANSLATION_MAX_NEW_TOKENS", "768"))),
}

# Structured creative drafts use the same disposable model-process contract but
# a separate context/output budget. Keeping this distinct from translation
# prevents a 768-token translation limit from silently truncating storyboards.
LOCAL_QWEN_CREATIVE = {
    "enabled": os.getenv("QWEN_CREATIVE_ENABLED", "true").lower()
    in {"1", "true", "yes", "on"},
    "model_id": os.getenv(
        "QWEN_CREATIVE_MODEL_ID", LOCAL_PROMPT_TRANSLATION["model_id"],
    ).strip(),
    "model_dir": os.getenv(
        "QWEN_CREATIVE_MODEL_DIR", LOCAL_PROMPT_TRANSLATION["model_dir"],
    ).strip(),
    "runtime_script": os.getenv(
        "QWEN_CREATIVE_RUNTIME_SCRIPT", LOCAL_PROMPT_TRANSLATION["runtime_script"],
    ).strip(),
    "runtime_python": os.getenv(
        "QWEN_CREATIVE_RUNTIME_PYTHON", LOCAL_PROMPT_TRANSLATION["runtime_python"],
    ).strip(),
    "device": _validate_qwen_device(
        os.getenv("QWEN_CREATIVE_DEVICE", "cpu"), "QWEN_CREATIVE_DEVICE",
    ),
    "gpu_layers": QWEN_GPU_LAYERS,
    # Child inference deadline.  The Django subprocess owner adds a fixed
    # 60-second cleanup grace and therefore waits at most 960 seconds by
    # default; browser clients must stay above that parent ceiling.
    "timeout_seconds": float(os.getenv("QWEN_CREATIVE_TIMEOUT_SECONDS", "900")),
    "max_new_tokens": int(os.getenv("QWEN_CREATIVE_MAX_NEW_TOKENS", "3072")),
    "context_size": int(os.getenv("QWEN_CREATIVE_CONTEXT_SIZE", "8192")),
    "min_shots": 1,
    "max_shots": int(os.getenv("QWEN_CREATIVE_MAX_SHOTS", "4")),
    "max_source_chars": int(os.getenv("QWEN_CREATIVE_MAX_SOURCE_CHARS", "16000")),
}
if not 1 <= LOCAL_QWEN_CREATIVE["max_shots"] <= 4:
    raise ImproperlyConfigured("QWEN_CREATIVE_MAX_SHOTS must be between 1 and 4")
if not 2048 <= LOCAL_QWEN_CREATIVE["max_new_tokens"] <= 3072:
    raise ImproperlyConfigured("QWEN_CREATIVE_MAX_NEW_TOKENS must be between 2048 and 3072")
if not 8192 <= LOCAL_QWEN_CREATIVE["context_size"] <= 32768:
    raise ImproperlyConfigured("QWEN_CREATIVE_CONTEXT_SIZE must be between 8192 and 32768")
if not 4000 <= LOCAL_QWEN_CREATIVE["max_source_chars"] <= 16000:
    raise ImproperlyConfigured("QWEN_CREATIVE_MAX_SOURCE_CHARS must be between 4000 and 16000")
LOCAL_QWEN_LOCK_PATH = os.getenv(
    "LOCAL_QWEN_LOCK_PATH", str(BASE_DIR.parent / ".runtime" / "locks" / "local-qwen.lock"),
).strip()
TASK_WORKER_POLL_SECONDS = float(os.getenv("TASK_WORKER_POLL_SECONDS", "2"))
TASK_MAX_CONCURRENT = int(os.getenv("TASK_MAX_CONCURRENT", "1"))
TASK_MAX_PENDING = int(os.getenv("TASK_MAX_PENDING", "20"))
TASK_MAX_ACTIVE_PER_USER = int(os.getenv("TASK_MAX_ACTIVE_PER_USER", "5"))
TASK_GENERATION_TIMEOUT_SECONDS = float(os.getenv("TASK_GENERATION_TIMEOUT_SECONDS", "21600"))
TASK_REMOTE_POLL_SECONDS = float(os.getenv("TASK_REMOTE_POLL_SECONDS", "1"))
TASK_HEARTBEAT_SECONDS = float(os.getenv("TASK_HEARTBEAT_SECONDS", "5"))
TASK_PROGRESS_WEBSOCKET_ENABLED = os.getenv(
    "TASK_PROGRESS_WEBSOCKET_ENABLED", "true",
).lower() in {"1", "true", "yes", "on"}
TASK_HEALTH_CHECK_INTERVAL_SECONDS = float(os.getenv("TASK_HEALTH_CHECK_INTERVAL_SECONDS", "5"))
TASK_HEALTH_FAILURE_LIMIT = int(os.getenv("TASK_HEALTH_FAILURE_LIMIT", "3"))
TASK_REMOTE_MISSING_GRACE_SECONDS = float(os.getenv("TASK_REMOTE_MISSING_GRACE_SECONDS", "5"))
TASK_CANCEL_GRACE_SECONDS = float(os.getenv("TASK_CANCEL_GRACE_SECONDS", "30"))
TASK_STALL_THRESHOLD_SECONDS = float(os.getenv("TASK_STALL_THRESHOLD_SECONDS", "30"))
TASK_STALE_RECOVERY_INTERVAL_SECONDS = float(os.getenv("TASK_STALE_RECOVERY_INTERVAL_SECONDS", "5"))
READINESS_WORKER_MAX_AGE_SECONDS = int(os.getenv("READINESS_WORKER_MAX_AGE_SECONDS", "30"))
READINESS_MIN_DISK_FREE_BYTES = int(os.getenv("READINESS_MIN_DISK_FREE_BYTES", str(512 * 1024 * 1024)))
READINESS_COMFYUI_REQUIRED = os.getenv("READINESS_COMFYUI_REQUIRED", "true").lower() in {"1", "true", "yes", "on"}
READINESS_COMFYUI_TIMEOUT_SECONDS = float(os.getenv("READINESS_COMFYUI_TIMEOUT_SECONDS", "2"))
READINESS_GENERATION_WORKER_REQUIRED = os.getenv("READINESS_GENERATION_WORKER_REQUIRED", "true").lower() in {"1", "true", "yes", "on"}
READINESS_ASSEMBLY_WORKER_REQUIRED = os.getenv("READINESS_ASSEMBLY_WORKER_REQUIRED", "true").lower() in {"1", "true", "yes", "on"}
WORKER_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "5"))
FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg")
FFPROBE_BINARY = os.getenv("FFPROBE_BINARY", "ffprobe")
ASSEMBLY_TIMEOUT_SECONDS = float(os.getenv("ASSEMBLY_TIMEOUT_SECONDS", "7200"))
ASSEMBLY_WORKER_POLL_SECONDS = float(os.getenv("ASSEMBLY_WORKER_POLL_SECONDS", "1"))
ASSEMBLY_HEARTBEAT_SECONDS = float(os.getenv("ASSEMBLY_HEARTBEAT_SECONDS", "5"))
ASSEMBLY_STALE_MINUTES = float(os.getenv("ASSEMBLY_STALE_MINUTES", "30"))
ASSEMBLY_RECOVERY_INTERVAL_SECONDS = float(os.getenv("ASSEMBLY_RECOVERY_INTERVAL_SECONDS", "60"))
ASSEMBLY_AI_LABEL_ENABLED = os.getenv("ASSEMBLY_AI_LABEL_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
ASSEMBLY_AI_LABEL_TEXT = os.getenv("ASSEMBLY_AI_LABEL_TEXT", "AI-generated · MiniMax H3")[:120]
ASSEMBLY_AI_LABEL_FONTFILE = os.getenv("ASSEMBLY_AI_LABEL_FONTFILE", "")
# A task output is a private production master.  Public downloads are separate,
# content-addressed distribution copies with a visible disclosure and embedded
# metadata; disabling this setting makes public task-output downloads fail
# closed instead of exposing the ComfyUI master.
TASK_DISTRIBUTION_ENABLED = os.getenv("TASK_DISTRIBUTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
TASK_DISTRIBUTION_LABEL_TEXT = os.getenv(
    "TASK_DISTRIBUTION_LABEL_TEXT", "AI-generated · MiniMax H3",
)[:120]
TASK_IMAGE_DISTRIBUTION_LABEL_TEXT = os.getenv(
    "TASK_IMAGE_DISTRIBUTION_LABEL_TEXT", "AI-generated · SDXL 1.0",
)[:120]
TASK_DISTRIBUTION_FONTFILE = os.getenv(
    "TASK_DISTRIBUTION_FONTFILE", ASSEMBLY_AI_LABEL_FONTFILE,
)
TASK_DISTRIBUTION_VERSION = os.getenv("TASK_DISTRIBUTION_VERSION", "shot-public-v1")[:80]
TASK_DISTRIBUTION_TIMEOUT_SECONDS = float(os.getenv("TASK_DISTRIBUTION_TIMEOUT_SECONDS", "100"))
TASK_DISTRIBUTION_LOCK_STALE_SECONDS = float(
    os.getenv("TASK_DISTRIBUTION_LOCK_STALE_SECONDS", "180")
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
