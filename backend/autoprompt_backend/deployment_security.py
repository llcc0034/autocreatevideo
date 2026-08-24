"""Fail-fast validation for the explicit production deployment profile.

Development remains convenient and loopback-only.  A container or external
deployment must opt into ``AUTOPROMPT_DEPLOYMENT_PROFILE=production`` and then
prove that its secrets, database, HTTPS controls and browser trust boundaries
are intentional before Django starts serving requests or workers.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


VALID_DEPLOYMENT_PROFILES = frozenset({"development", "production"})
_INTERNAL_ONLY_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "backend", "testserver"})
_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "dev-only",
    "example.invalid",
    "replace-with",
    "setup-generates",
)
def _contains_placeholder(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _validate_origin(origin: str, *, setting_name: str, allowed_hosts: set[str], errors: list[str]) -> None:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        errors.append(f"{setting_name} contains an invalid origin: {origin!r}")
        return
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        errors.append(f"{setting_name} must contain HTTPS origins without paths or credentials: {origin!r}")
        return
    hostname = parsed.hostname.casefold()
    if _contains_placeholder(hostname):
        errors.append(f"{setting_name} still contains a placeholder hostname: {origin!r}")
    if hostname not in allowed_hosts:
        errors.append(f"{setting_name} origin host {hostname!r} is absent from DJANGO_ALLOWED_HOSTS")


def validate_production_configuration(
    *,
    profile: str,
    debug: bool,
    secret_key: str,
    jwt_signing_key: str,
    database_engine: str,
    database_password: str,
    allowed_hosts: list[str],
    cors_allowed_origins: list[str],
    csrf_trusted_origins: list[str],
    secure_ssl_redirect: bool,
    session_cookie_secure: bool,
    csrf_cookie_secure: bool,
    secure_hsts_seconds: int,
) -> None:
    """Raise one actionable error containing every unsafe production value."""

    normalized_profile = str(profile or "").strip().casefold()
    if normalized_profile not in VALID_DEPLOYMENT_PROFILES:
        raise ImproperlyConfigured(
            "AUTOPROMPT_DEPLOYMENT_PROFILE must be 'development' or 'production'"
        )
    if normalized_profile != "production":
        return

    errors: list[str] = []
    if debug:
        errors.append("DJANGO_DEBUG must be false")

    key = str(secret_key or "")
    if len(key) < 50 or len(set(key)) < 8 or _contains_placeholder(key):
        errors.append("DJANGO_SECRET_KEY must be a non-placeholder value with at least 50 characters")
    jwt_key = str(jwt_signing_key or "")
    if (
        len(jwt_key.encode("utf-8")) < 32
        or len(set(jwt_key)) < 8
        or _contains_placeholder(jwt_key)
    ):
        errors.append(
            "AUTOPROMPT_JWT_SIGNING_KEY must be a non-placeholder value with at least 32 UTF-8 bytes"
        )
    if jwt_key and jwt_key == key:
        errors.append("AUTOPROMPT_JWT_SIGNING_KEY must differ from DJANGO_SECRET_KEY")

    if database_engine != "django.db.backends.postgresql":
        errors.append("DATABASE_URL must select PostgreSQL; SQLite is development-only")
    db_password = str(database_password or "")
    if len(db_password) < 20 or _contains_placeholder(db_password):
        errors.append("the PostgreSQL password must be a non-placeholder value with at least 20 characters")

    normalized_hosts = {str(host).strip().casefold() for host in allowed_hosts if str(host).strip()}
    if not normalized_hosts:
        errors.append("DJANGO_ALLOWED_HOSTS must be explicit and non-empty")
    if any("*" in host or host.startswith(".") or "://" in host or "/" in host for host in normalized_hosts):
        errors.append("DJANGO_ALLOWED_HOSTS must not contain wildcards, schemes or paths")
    if any(_contains_placeholder(host) for host in normalized_hosts):
        errors.append("DJANGO_ALLOWED_HOSTS still contains a placeholder hostname")
    if not (normalized_hosts - _INTERNAL_ONLY_HOSTS):
        errors.append("DJANGO_ALLOWED_HOSTS must include the real deployment hostname")

    if not cors_allowed_origins:
        errors.append("CORS_ALLOWED_ORIGINS must explicitly name the HTTPS frontend origin")
    if not csrf_trusted_origins:
        errors.append("CSRF_TRUSTED_ORIGINS must explicitly name the HTTPS frontend origin")
    for origin in cors_allowed_origins:
        _validate_origin(
            str(origin), setting_name="CORS_ALLOWED_ORIGINS",
            allowed_hosts=normalized_hosts, errors=errors,
        )
    for origin in csrf_trusted_origins:
        _validate_origin(
            str(origin), setting_name="CSRF_TRUSTED_ORIGINS",
            allowed_hosts=normalized_hosts, errors=errors,
        )

    if not secure_ssl_redirect:
        errors.append("SECURE_SSL_REDIRECT must be true")
    if not session_cookie_secure:
        errors.append("SESSION_COOKIE_SECURE must be true")
    if not csrf_cookie_secure:
        errors.append("CSRF_COOKIE_SECURE must be true")
    if int(secure_hsts_seconds or 0) < 3600:
        errors.append("SECURE_HSTS_SECONDS must be at least 3600 after HTTPS is verified")

    if errors:
        rendered = "\n - ".join(errors)
        raise ImproperlyConfigured(f"Unsafe production configuration:\n - {rendered}")
