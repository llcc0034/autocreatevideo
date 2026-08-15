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
        "ModelProviderEnum": ("local_qwen", "deepseek_api"),
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
VIDEO_SHOT_MIN_DURATION_MS = int(os.getenv("VIDEO_SHOT_MIN_DURATION_MS", "5000"))
VIDEO_SHOT_MAX_DURATION_MS = int(os.getenv("VIDEO_SHOT_MAX_DURATION_MS", "15000"))
H3_MIN_ALIGNED_FRAMES = int(os.getenv("H3_MIN_ALIGNED_FRAMES", "124"))
H3_MAX_ALIGNED_FRAMES = int(os.getenv("H3_MAX_ALIGNED_FRAMES", "362"))
if not 1 <= VIDEO_SHOT_MIN_DURATION_MS <= VIDEO_SHOT_MAX_DURATION_MS:
    raise ImproperlyConfigured(
        "VIDEO_SHOT_MIN_DURATION_MS and VIDEO_SHOT_MAX_DURATION_MS must define "
        "a positive ordered range"
    )
if not 1 <= H3_MIN_ALIGNED_FRAMES <= H3_MAX_ALIGNED_FRAMES:
    raise ImproperlyConfigured(
        "H3_MIN_ALIGNED_FRAMES and H3_MAX_ALIGNED_FRAMES must define a positive "
        "ordered range"
    )


def _configured_h3_aligned_frames(duration_ms: int) -> int:
    requested = max(5, round(duration_ms * 24 / 1000))
    return requested + (5 - requested % 17) % 17


if (
    _configured_h3_aligned_frames(VIDEO_SHOT_MIN_DURATION_MS) < H3_MIN_ALIGNED_FRAMES
    or _configured_h3_aligned_frames(VIDEO_SHOT_MAX_DURATION_MS) > H3_MAX_ALIGNED_FRAMES
):
    raise ImproperlyConfigured(
        "VIDEO_SHOT duration bounds must align inside H3_MIN_ALIGNED_FRAMES and "
        "H3_MAX_ALIGNED_FRAMES"
    )


def _strict_environment_boolean(name: str, default: str) -> bool:
    raw = os.getenv(name, default).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off"
    )


SCRIPT_BREAKDOWN_DEFAULT_SHOT_DURATION_MS = int(
    os.getenv("SCRIPT_BREAKDOWN_DEFAULT_SHOT_DURATION_MS", "5000")
)
SCRIPT_BREAKDOWN_DEFAULT_FIRST_FRAME_CONTINUITY = _strict_environment_boolean(
    "SCRIPT_BREAKDOWN_DEFAULT_FIRST_FRAME_CONTINUITY", "false",
)
SCRIPT_BREAKDOWN_DEFAULT_LAST_FRAME_CONTINUITY = _strict_environment_boolean(
    "SCRIPT_BREAKDOWN_DEFAULT_LAST_FRAME_CONTINUITY", "false",
)
SCRIPT_BREAKDOWN_AUTO_SPLIT_TRIGGER_CHARS = int(
    os.getenv("SCRIPT_BREAKDOWN_AUTO_SPLIT_TRIGGER_CHARS", "320")
)
SCRIPT_BREAKDOWN_AUTO_SPLIT_TARGET_CHARS = int(
    os.getenv("SCRIPT_BREAKDOWN_AUTO_SPLIT_TARGET_CHARS", "220")
)
if not (
    VIDEO_SHOT_MIN_DURATION_MS
    <= SCRIPT_BREAKDOWN_DEFAULT_SHOT_DURATION_MS
    <= VIDEO_SHOT_MAX_DURATION_MS
):
    raise ImproperlyConfigured(
        "SCRIPT_BREAKDOWN_DEFAULT_SHOT_DURATION_MS must stay inside the configured "
        "VIDEO_SHOT duration range"
    )
if not (
    1
    <= SCRIPT_BREAKDOWN_AUTO_SPLIT_TARGET_CHARS
    <= SCRIPT_BREAKDOWN_AUTO_SPLIT_TRIGGER_CHARS
):
    raise ImproperlyConfigured(
        "SCRIPT_BREAKDOWN_AUTO_SPLIT_TARGET_CHARS and "
        "SCRIPT_BREAKDOWN_AUTO_SPLIT_TRIGGER_CHARS must define a positive ordered range"
    )
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
    "max_tokens": int(os.getenv("DEEPSEEK_MAX_TOKENS", "16384")),
}
if DEEPSEEK_API["timeout_seconds"] <= 0:
    raise ImproperlyConfigured("DEEPSEEK_TIMEOUT_SECONDS must be greater than zero")
if not 0 <= DEEPSEEK_API["max_retries"] <= 3:
    raise ImproperlyConfigured("DEEPSEEK_MAX_RETRIES must be between 0 and 3")
if DEEPSEEK_API["max_tokens"] < 1:
    raise ImproperlyConfigured("DEEPSEEK_MAX_TOKENS must be greater than zero")
if not DEEPSEEK_API["base_url"]:
    raise ImproperlyConfigured("DEEPSEEK_BASE_URL must not be empty")
if not DEEPSEEK_API["model"]:
    raise ImproperlyConfigured("DEEPSEEK_MODEL must not be empty")
if DEEPSEEK_API["enabled"] and not DEEPSEEK_API["api_key"]:
    raise ImproperlyConfigured(
        "DEEPSEEK_API_KEY must be set when DEEPSEEK_ENABLED is true"
    )


# Human-review guidance is deliberately separate from model transport/schema
# limits. Exceeding these values only creates an editable review issue; it must
# never discard a provider response or prevent saving the proposal as a draft.
CREATIVE_DRAFT_REVIEW = {
    "recommended_title_zh_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_TITLE_ZH_CHARS", "48")
    ),
    "recommended_title_en_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_TITLE_EN_CHARS", "72")
    ),
    "recommended_narrative_zh_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_NARRATIVE_ZH_CHARS", "180")
    ),
    "recommended_narrative_en_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_NARRATIVE_EN_CHARS", "1000")
    ),
    "recommended_camera_zh_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_CAMERA_ZH_CHARS", "220")
    ),
    "recommended_camera_en_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_CAMERA_EN_CHARS", "320")
    ),
    "recommended_action_zh_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_ACTION_ZH_CHARS", "120")
    ),
    "recommended_action_en_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_ACTION_EN_CHARS", "1000")
    ),
    "recommended_continuity_zh_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_CONTINUITY_ZH_CHARS", "160")
    ),
    "recommended_continuity_en_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_CONTINUITY_EN_CHARS", "240")
    ),
    "recommended_soundscape_zh_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_SOUNDSCAPE_ZH_CHARS", "120")
    ),
    "recommended_soundscape_en_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_SOUNDSCAPE_EN_CHARS", "180")
    ),
    "recommended_music_zh_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_MUSIC_ZH_CHARS", "80")
    ),
    "recommended_music_en_chars": int(
        os.getenv("CREATIVE_REVIEW_RECOMMENDED_MUSIC_EN_CHARS", "120")
    ),
    "max_camera_movements": int(
        os.getenv("CREATIVE_REVIEW_MAX_CAMERA_MOVEMENTS", "1")
    ),
    "max_action_sequence_markers": int(
        os.getenv("CREATIVE_REVIEW_MAX_ACTION_SEQUENCE_MARKERS", "4")
    ),
    "min_script_action_coverage_percent": int(
        os.getenv("CREATIVE_REVIEW_MIN_SCRIPT_ACTION_COVERAGE_PERCENT", "80")
    ),
}
def _validate_creative_review_policy(config: dict) -> None:
    for key, value in config.items():
        if int(value) <= 0:
            raise ImproperlyConfigured(
                f"CREATIVE_REVIEW_{key.upper()} must be greater than zero"
            )
    if int(config["min_script_action_coverage_percent"]) > 100:
        raise ImproperlyConfigured(
            "CREATIVE_REVIEW_MIN_SCRIPT_ACTION_COVERAGE_PERCENT must be between 1 and 100"
        )


_validate_creative_review_policy(CREATIVE_DRAFT_REVIEW)


def _validate_creative_provider_policy(config: dict, prefix: str) -> None:
    positive_keys = (
        "min_shots",
        "recommended_max_shots",
        "batch_shots",
        "max_dialogue_per_shot",
        "max_episode_title_chars",
        "max_dialogue_text_chars",
        "max_dialogue_delivery_chars",
        "max_dialogue_delivery_en_chars",
        "min_shot_duration_ms",
        "max_shot_duration_ms",
        "output_min_tokens",
        "output_base_tokens",
        "output_tokens_per_shot",
        "output_tokens_per_dialogue",
        "max_source_chars",
        "max_regeneration_context_chars",
        "max_input_chars",
    )
    for key in positive_keys:
        if int(config[key]) <= 0:
            raise ImproperlyConfigured(f"{prefix}_{key.upper()} must be greater than zero")
    if config["recommended_max_shots"] < config["min_shots"]:
        raise ImproperlyConfigured(
            f"{prefix}_RECOMMENDED_MAX_SHOTS must not be less than "
            f"{prefix}_MIN_SHOTS"
        )
    if config["batch_shots"] < config["min_shots"]:
        raise ImproperlyConfigured(
            f"{prefix}_BATCH_SHOTS must not be less than {prefix}_MIN_SHOTS"
        )
    if config["max_source_chars"] > config["max_input_chars"]:
        raise ImproperlyConfigured(
            f"{prefix}_MAX_SOURCE_CHARS must not exceed {prefix}_MAX_INPUT_CHARS"
        )
    if config["max_regeneration_context_chars"] > config["max_input_chars"]:
        raise ImproperlyConfigured(
            f"{prefix}_MAX_REGENERATION_CONTEXT_CHARS must not exceed "
            f"{prefix}_MAX_INPUT_CHARS"
        )
    if config["min_shot_duration_ms"] > config["max_shot_duration_ms"]:
        raise ImproperlyConfigured(
            f"{prefix}_MIN_SHOT_DURATION_MS must not exceed "
            f"{prefix}_MAX_SHOT_DURATION_MS"
        )
    if config["min_shot_duration_ms"] < VIDEO_SHOT_MIN_DURATION_MS:
        raise ImproperlyConfigured(
            f"{prefix}_MIN_SHOT_DURATION_MS must not be less than "
            "VIDEO_SHOT_MIN_DURATION_MS"
        )
    if config["max_shot_duration_ms"] > VIDEO_SHOT_MAX_DURATION_MS:
        raise ImproperlyConfigured(
            f"{prefix}_MAX_SHOT_DURATION_MS must not exceed "
            "VIDEO_SHOT_MAX_DURATION_MS"
        )
    if int(config["output_dialogue_char_threshold"]) < 0:
        raise ImproperlyConfigured(
            f"{prefix}_OUTPUT_DIALOGUE_CHAR_THRESHOLD must be zero or greater"
        )
    if int(config["output_headroom_per_shot"]) < 0:
        raise ImproperlyConfigured(
            f"{prefix}_OUTPUT_HEADROOM_PER_SHOT must be zero or greater"
        )


DEEPSEEK_CREATIVE = {
    **CREATIVE_DRAFT_REVIEW,
    "min_shots": int(os.getenv("DEEPSEEK_CREATIVE_MIN_SHOTS", "1")),
    "recommended_max_shots": int(
        os.getenv("DEEPSEEK_CREATIVE_RECOMMENDED_MAX_SHOTS", "4")
    ),
    "batch_shots": int(os.getenv("DEEPSEEK_CREATIVE_BATCH_SHOTS", "1")),
    "max_dialogue_per_shot": int(
        os.getenv("DEEPSEEK_CREATIVE_MAX_DIALOGUE_PER_SHOT", "2")
    ),
    "max_episode_title_chars": int(
        os.getenv("DEEPSEEK_CREATIVE_MAX_EPISODE_TITLE_CHARS", "80")
    ),
    "max_dialogue_text_chars": int(
        os.getenv("DEEPSEEK_CREATIVE_MAX_DIALOGUE_TEXT_CHARS", "240")
    ),
    "max_dialogue_delivery_chars": int(
        os.getenv("DEEPSEEK_CREATIVE_MAX_DIALOGUE_DELIVERY_CHARS", "100")
    ),
    "max_dialogue_delivery_en_chars": int(
        os.getenv("DEEPSEEK_CREATIVE_MAX_DIALOGUE_DELIVERY_EN_CHARS", "150")
    ),
    "min_shot_duration_ms": int(
        os.getenv(
            "DEEPSEEK_CREATIVE_MIN_SHOT_DURATION_MS",
            str(VIDEO_SHOT_MIN_DURATION_MS),
        )
    ),
    "max_shot_duration_ms": int(
        os.getenv(
            "DEEPSEEK_CREATIVE_MAX_SHOT_DURATION_MS",
            str(VIDEO_SHOT_MAX_DURATION_MS),
        )
    ),
    "output_min_tokens": int(
        os.getenv("DEEPSEEK_CREATIVE_OUTPUT_MIN_TOKENS", "1024")
    ),
    "output_base_tokens": int(
        os.getenv("DEEPSEEK_CREATIVE_OUTPUT_BASE_TOKENS", "768")
    ),
    "output_tokens_per_shot": int(
        os.getenv("DEEPSEEK_CREATIVE_OUTPUT_TOKENS_PER_SHOT", "384")
    ),
    "output_tokens_per_dialogue": int(
        os.getenv("DEEPSEEK_CREATIVE_OUTPUT_TOKENS_PER_DIALOGUE", "128")
    ),
    "output_dialogue_char_threshold": int(
        os.getenv("DEEPSEEK_CREATIVE_OUTPUT_DIALOGUE_CHAR_THRESHOLD", "32")
    ),
    "max_source_chars": int(
        os.getenv("DEEPSEEK_CREATIVE_MAX_SOURCE_CHARS", "24000")
    ),
    "max_regeneration_context_chars": int(
        os.getenv("DEEPSEEK_CREATIVE_MAX_REGENERATION_CONTEXT_CHARS", "96000")
    ),
    "max_input_chars": int(
        os.getenv("DEEPSEEK_CREATIVE_MAX_INPUT_CHARS", "120000")
    ),
    "output_headroom_per_shot": int(
        os.getenv("DEEPSEEK_CREATIVE_OUTPUT_HEADROOM_PER_SHOT", "384")
    ),
}
_validate_creative_provider_policy(DEEPSEEK_CREATIVE, "DEEPSEEK_CREATIVE")

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
    **CREATIVE_DRAFT_REVIEW,
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
    # Child inference deadline and parent cleanup grace are independently
    # configurable so operators can size them for the selected runtime.
    "timeout_seconds": float(os.getenv("QWEN_CREATIVE_TIMEOUT_SECONDS", "900")),
    "process_cleanup_grace_seconds": float(
        os.getenv("QWEN_CREATIVE_PROCESS_CLEANUP_GRACE_SECONDS", "60")
    ),
    "max_new_tokens": int(os.getenv("QWEN_CREATIVE_MAX_NEW_TOKENS", "3072")),
    "context_size": int(os.getenv("QWEN_CREATIVE_CONTEXT_SIZE", "8192")),
    "min_context_size": int(os.getenv("QWEN_CREATIVE_MIN_CONTEXT_SIZE", "8192")),
    "max_context_size": int(os.getenv("QWEN_CREATIVE_MAX_CONTEXT_SIZE", "32768")),
    "reserved_input_tokens": int(
        os.getenv("QWEN_CREATIVE_RESERVED_INPUT_TOKENS", "2048")
    ),
    "min_shots": int(os.getenv("QWEN_CREATIVE_MIN_SHOTS", "1")),
    "recommended_max_shots": int(
        os.getenv("QWEN_CREATIVE_RECOMMENDED_MAX_SHOTS", "4")
    ),
    "batch_shots": int(os.getenv("QWEN_CREATIVE_BATCH_SHOTS", "4")),
    "max_dialogue_per_shot": int(
        os.getenv("QWEN_CREATIVE_MAX_DIALOGUE_PER_SHOT", "2")
    ),
    "max_episode_title_chars": int(
        os.getenv("QWEN_CREATIVE_MAX_EPISODE_TITLE_CHARS", "80")
    ),
    "max_dialogue_text_chars": int(
        os.getenv("QWEN_CREATIVE_MAX_DIALOGUE_TEXT_CHARS", "240")
    ),
    "max_dialogue_delivery_chars": int(
        os.getenv("QWEN_CREATIVE_MAX_DIALOGUE_DELIVERY_CHARS", "100")
    ),
    "max_dialogue_delivery_en_chars": int(
        os.getenv("QWEN_CREATIVE_MAX_DIALOGUE_DELIVERY_EN_CHARS", "150")
    ),
    "min_shot_duration_ms": int(
        os.getenv(
            "QWEN_CREATIVE_MIN_SHOT_DURATION_MS",
            str(VIDEO_SHOT_MIN_DURATION_MS),
        )
    ),
    "max_shot_duration_ms": int(
        os.getenv(
            "QWEN_CREATIVE_MAX_SHOT_DURATION_MS",
            str(VIDEO_SHOT_MAX_DURATION_MS),
        )
    ),
    "output_min_tokens": int(os.getenv("QWEN_CREATIVE_OUTPUT_MIN_TOKENS", "1024")),
    "output_base_tokens": int(os.getenv("QWEN_CREATIVE_OUTPUT_BASE_TOKENS", "768")),
    "output_tokens_per_shot": int(
        os.getenv("QWEN_CREATIVE_OUTPUT_TOKENS_PER_SHOT", "384")
    ),
    "output_tokens_per_dialogue": int(
        os.getenv("QWEN_CREATIVE_OUTPUT_TOKENS_PER_DIALOGUE", "128")
    ),
    "output_dialogue_char_threshold": int(
        os.getenv("QWEN_CREATIVE_OUTPUT_DIALOGUE_CHAR_THRESHOLD", "32")
    ),
    "max_source_chars": int(os.getenv("QWEN_CREATIVE_MAX_SOURCE_CHARS", "16000")),
    "max_regeneration_context_chars": int(
        os.getenv("QWEN_CREATIVE_MAX_REGENERATION_CONTEXT_CHARS", "16000")
    ),
    "max_input_chars": int(os.getenv("QWEN_CREATIVE_MAX_INPUT_CHARS", "24000")),
    "output_headroom_per_shot": int(
        os.getenv("QWEN_CREATIVE_OUTPUT_HEADROOM_PER_SHOT", "0")
    ),
}
_validate_creative_provider_policy(LOCAL_QWEN_CREATIVE, "QWEN_CREATIVE")
if LOCAL_QWEN_CREATIVE["timeout_seconds"] <= 0:
    raise ImproperlyConfigured("QWEN_CREATIVE_TIMEOUT_SECONDS must be greater than zero")
if LOCAL_QWEN_CREATIVE["process_cleanup_grace_seconds"] < 0:
    raise ImproperlyConfigured(
        "QWEN_CREATIVE_PROCESS_CLEANUP_GRACE_SECONDS must be zero or greater"
    )
for _creative_key in (
    "max_new_tokens", "context_size", "min_context_size",
    "max_context_size", "reserved_input_tokens",
):
    if LOCAL_QWEN_CREATIVE[_creative_key] < 1:
        raise ImproperlyConfigured(
            f"QWEN_CREATIVE_{_creative_key.upper()} must be greater than zero"
        )
if not (
    LOCAL_QWEN_CREATIVE["min_context_size"]
    <= LOCAL_QWEN_CREATIVE["context_size"]
    <= LOCAL_QWEN_CREATIVE["max_context_size"]
):
    raise ImproperlyConfigured(
        "QWEN_CREATIVE_MIN_CONTEXT_SIZE, QWEN_CREATIVE_CONTEXT_SIZE and "
        "QWEN_CREATIVE_MAX_CONTEXT_SIZE must be ordered from smallest to largest"
    )
if (
    LOCAL_QWEN_CREATIVE["max_new_tokens"]
    > LOCAL_QWEN_CREATIVE["context_size"]
    - LOCAL_QWEN_CREATIVE["reserved_input_tokens"]
):
    raise ImproperlyConfigured(
        "QWEN_CREATIVE_MAX_NEW_TOKENS must leave QWEN_CREATIVE_RESERVED_INPUT_TOKENS "
        "context tokens for input"
    )

# Review-gated per-shot director-detail policy.  These values are shared by
# the HTTP API, the durable episode batch worker, and the isolated Qwen
# protocol request.  Recommended limits create review/confirmation messages;
# only the wider transport envelope protects storage and process boundaries.
_SHOT_DETAIL_PROTOCOL_LIMIT_DEFAULTS = {
    "project_title_max_chars": 200,
    "project_logline_max_chars": 2_000,
    "project_continuity_summary_max_chars": 3_000,
    "project_color_script_max_chars": 2_000,
    "project_aspect_ratio_max_chars": 16,
    "style_max_chars": 8_000,
    "glossary_max_terms": 200,
    "glossary_source_max_chars": 160,
    "glossary_target_max_chars": 240,
    "reference_bindings_max_chars": 4_000,
    "shot_title_max_chars": 200,
    "shot_narrative_max_chars": 2_000,
    "shot_detailed_description_max_chars": 3_500,
    "shot_camera_max_chars": 1_000,
    "shot_continuity_notes_max_chars": 1_000,
    "target_action_max_chars": 2_000,
    "target_sound_effects_max_chars": 1_000,
    "target_music_max_chars": 1_000,
    "target_h3_mode_max_chars": 16,
    "dialogue_speaker_id_max_chars": 16,
    "dialogue_text_max_chars": 240,
    "dialogue_delivery_max_chars": 160,
    "max_dialogue_per_shot": 2,
    "bible_max_characters": 20,
    "bible_max_scenes": 1,
    "bible_max_props": 30,
    "bible_speaker_id_max_chars": 16,
    "bible_entity_field_max_chars": 2_000,
    "feedback_max_chars": 2_000,
}
_SHOT_DETAIL_PROTOCOL_ENV_SUFFIXES = {
    key: key.upper() for key in _SHOT_DETAIL_PROTOCOL_LIMIT_DEFAULTS
}


def _shot_detail_protocol_limits(provider_prefix: str) -> dict[str, int]:
    """Resolve one provider's complete high-detail request envelope."""
    return {
        key: int(os.getenv(
            f"{provider_prefix}_SHOT_DETAIL_{_SHOT_DETAIL_PROTOCOL_ENV_SUFFIXES[key]}",
            str(default_value),
        ))
        for key, default_value in _SHOT_DETAIL_PROTOCOL_LIMIT_DEFAULTS.items()
    }


SHOT_DETAIL_POLICY = {
    "recommended_zh_chars": int(
        os.getenv("SHOT_DETAIL_RECOMMENDED_ZH_CHARS", "500")
    ),
    "recommended_en_chars": int(
        os.getenv("SHOT_DETAIL_RECOMMENDED_EN_CHARS", "750")
    ),
    "transport_zh_max_chars": int(
        os.getenv("SHOT_DETAIL_TRANSPORT_ZH_MAX_CHARS", "3000")
    ),
    "transport_en_max_chars": int(
        os.getenv("SHOT_DETAIL_TRANSPORT_EN_MAX_CHARS", "4500")
    ),
    "output_tokens": int(os.getenv("SHOT_DETAIL_OUTPUT_TOKENS", "2304")),
    "context_max_chars": int(
        os.getenv("SHOT_DETAIL_CONTEXT_MAX_CHARS", "18000")
    ),
    "review_min_zh_chars": int(
        os.getenv("SHOT_DETAIL_REVIEW_MIN_ZH_CHARS", "100")
    ),
    "review_min_en_chars": int(
        os.getenv("SHOT_DETAIL_REVIEW_MIN_EN_CHARS", "160")
    ),
    "review_near_zh_margin_chars": int(
        os.getenv("SHOT_DETAIL_REVIEW_NEAR_ZH_MARGIN_CHARS", "20")
    ),
    "review_near_en_margin_chars": int(
        os.getenv("SHOT_DETAIL_REVIEW_NEAR_EN_MARGIN_CHARS", "30")
    ),
    "maximum_generation_number": int(
        os.getenv("SHOT_DETAIL_MAX_GENERATION_NUMBER", "1000000")
    ),
    "minimum_duration_ms": VIDEO_SHOT_MIN_DURATION_MS,
    "maximum_duration_ms": VIDEO_SHOT_MAX_DURATION_MS,
    "max_attempts": int(os.getenv("SHOT_DETAIL_MAX_ATTEMPTS", "3")),
    "retry_seconds": float(os.getenv("SHOT_DETAIL_RETRY_SECONDS", "5")),
    "stale_minutes": int(os.getenv("SHOT_DETAIL_STALE_MINUTES", "30")),
    "lease_safety_seconds": float(
        os.getenv("SHOT_DETAIL_LEASE_SAFETY_SECONDS", "60")
    ),
    **_SHOT_DETAIL_PROTOCOL_LIMIT_DEFAULTS,
}
SHOT_DETAIL_PROVIDER_POLICIES = {
    "local_qwen": {
        "recommended_max_shots": int(
            os.getenv("QWEN_SHOT_DETAIL_RECOMMENDED_MAX_SHOTS", "4")
        ),
        "output_tokens": int(os.getenv(
            "QWEN_SHOT_DETAIL_OUTPUT_TOKENS",
            str(SHOT_DETAIL_POLICY["output_tokens"]),
        )),
        "context_max_chars": int(os.getenv(
            "QWEN_SHOT_DETAIL_CONTEXT_MAX_CHARS",
            str(SHOT_DETAIL_POLICY["context_max_chars"]),
        )),
        **_shot_detail_protocol_limits("QWEN"),
    },
    "deepseek_api": {
        "recommended_max_shots": int(
            os.getenv("DEEPSEEK_SHOT_DETAIL_RECOMMENDED_MAX_SHOTS", "16")
        ),
        "output_tokens": int(os.getenv(
            "DEEPSEEK_SHOT_DETAIL_OUTPUT_TOKENS",
            str(SHOT_DETAIL_POLICY["output_tokens"]),
        )),
        "context_max_chars": int(os.getenv(
            "DEEPSEEK_SHOT_DETAIL_CONTEXT_MAX_CHARS",
            str(SHOT_DETAIL_POLICY["context_max_chars"]),
        )),
        **_shot_detail_protocol_limits("DEEPSEEK"),
    },
}
for _shot_detail_key in (
    "recommended_zh_chars", "recommended_en_chars",
    "transport_zh_max_chars", "transport_en_max_chars", "output_tokens",
    "context_max_chars", "review_min_zh_chars", "review_min_en_chars",
    "max_dialogue_per_shot", "maximum_generation_number",
    "minimum_duration_ms", "maximum_duration_ms", "max_attempts", "stale_minutes",
):
    if int(SHOT_DETAIL_POLICY[_shot_detail_key]) < 1:
        raise ImproperlyConfigured(
            f"SHOT_DETAIL_{_shot_detail_key.upper()} must be greater than zero"
        )
if SHOT_DETAIL_POLICY["retry_seconds"] < 0:
    raise ImproperlyConfigured("SHOT_DETAIL_RETRY_SECONDS must be zero or greater")
if SHOT_DETAIL_POLICY["lease_safety_seconds"] < 0:
    raise ImproperlyConfigured(
        "SHOT_DETAIL_LEASE_SAFETY_SECONDS must be zero or greater"
    )
for _shot_detail_margin_key in (
    "review_near_zh_margin_chars", "review_near_en_margin_chars",
):
    if int(SHOT_DETAIL_POLICY[_shot_detail_margin_key]) < 0:
        raise ImproperlyConfigured(
            f"SHOT_DETAIL_{_shot_detail_margin_key.upper()} must be zero or greater"
        )
if (
    SHOT_DETAIL_POLICY["review_near_zh_margin_chars"]
    > SHOT_DETAIL_POLICY["recommended_zh_chars"]
    or SHOT_DETAIL_POLICY["review_near_en_margin_chars"]
    > SHOT_DETAIL_POLICY["recommended_en_chars"]
):
    raise ImproperlyConfigured(
        "SHOT_DETAIL review near-limit margins must not exceed their recommendations"
    )
if SHOT_DETAIL_POLICY["minimum_duration_ms"] > SHOT_DETAIL_POLICY["maximum_duration_ms"]:
    raise ImproperlyConfigured("SHOT_DETAIL duration policy must be ordered")
_SHOT_DETAIL_LONGEST_PROVIDER_RUNTIME_SECONDS = max(
    LOCAL_QWEN_CREATIVE["timeout_seconds"]
    + LOCAL_QWEN_CREATIVE["process_cleanup_grace_seconds"],
    DEEPSEEK_API["timeout_seconds"] * (DEEPSEEK_API["max_retries"] + 1),
)
if (
    SHOT_DETAIL_POLICY["stale_minutes"] * 60
    <= _SHOT_DETAIL_LONGEST_PROVIDER_RUNTIME_SECONDS
    + SHOT_DETAIL_POLICY["lease_safety_seconds"]
):
    raise ImproperlyConfigured(
        "SHOT_DETAIL_STALE_MINUTES must outlast the longest configured provider run plus "
        "SHOT_DETAIL_LEASE_SAFETY_SECONDS"
    )
if (
    SHOT_DETAIL_POLICY["transport_zh_max_chars"]
    < SHOT_DETAIL_POLICY["recommended_zh_chars"]
    or SHOT_DETAIL_POLICY["transport_en_max_chars"]
    < SHOT_DETAIL_POLICY["recommended_en_chars"]
):
    raise ImproperlyConfigured(
        "SHOT_DETAIL transport limits must not be lower than review recommendations"
    )
for _provider_name, _provider_policy in SHOT_DETAIL_PROVIDER_POLICIES.items():
    if any(int(_provider_policy[key]) < 1 for key in (
        "recommended_max_shots", "output_tokens", "context_max_chars",
        *_SHOT_DETAIL_PROTOCOL_LIMIT_DEFAULTS,
    )):
        raise ImproperlyConfigured(
            f"{_provider_name.upper()} shot-detail provider policy values must be positive"
        )
if (
    SHOT_DETAIL_PROVIDER_POLICIES["local_qwen"]["output_tokens"]
    > LOCAL_QWEN_CREATIVE["max_new_tokens"]
):
    raise ImproperlyConfigured(
        "QWEN_SHOT_DETAIL_OUTPUT_TOKENS must not exceed QWEN_CREATIVE_MAX_NEW_TOKENS"
    )
if (
    SHOT_DETAIL_PROVIDER_POLICIES["deepseek_api"]["output_tokens"]
    > DEEPSEEK_API["max_tokens"]
):
    raise ImproperlyConfigured(
        "DEEPSEEK_SHOT_DETAIL_OUTPUT_TOKENS must not exceed DEEPSEEK_MAX_TOKENS"
    )
LOCAL_QWEN_LOCK_PATH = os.getenv(
    "LOCAL_QWEN_LOCK_PATH", str(BASE_DIR.parent / ".runtime" / "locks" / "local-qwen.lock"),
).strip()
TASK_WORKER_POLL_SECONDS = float(os.getenv("TASK_WORKER_POLL_SECONDS", "2"))
TASK_MAX_CONCURRENT = int(os.getenv("TASK_MAX_CONCURRENT", "1"))
TASK_RECOMMENDED_MAX_PENDING = int(os.getenv("TASK_RECOMMENDED_MAX_PENDING", "20"))
TASK_RECOMMENDED_MAX_ACTIVE_PER_USER = int(
    os.getenv("TASK_RECOMMENDED_MAX_ACTIVE_PER_USER", "5")
)
for _task_limit_name, _task_limit in (
    ("TASK_MAX_CONCURRENT", TASK_MAX_CONCURRENT),
    ("TASK_RECOMMENDED_MAX_PENDING", TASK_RECOMMENDED_MAX_PENDING),
    ("TASK_RECOMMENDED_MAX_ACTIVE_PER_USER", TASK_RECOMMENDED_MAX_ACTIVE_PER_USER),
):
    if _task_limit < 1:
        raise ImproperlyConfigured(f"{_task_limit_name} must be greater than zero")
if TASK_RECOMMENDED_MAX_ACTIVE_PER_USER > TASK_RECOMMENDED_MAX_PENDING:
    raise ImproperlyConfigured(
        "TASK_RECOMMENDED_MAX_ACTIVE_PER_USER must not exceed "
        "TASK_RECOMMENDED_MAX_PENDING"
    )
SCRIPT_BREAKDOWN_MAX_SOURCE_CHARS = int(
    os.getenv("SCRIPT_BREAKDOWN_MAX_SOURCE_CHARS", "1000000")
)
if SCRIPT_BREAKDOWN_MAX_SOURCE_CHARS < 1:
    raise ImproperlyConfigured("SCRIPT_BREAKDOWN_MAX_SOURCE_CHARS must be greater than zero")
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
