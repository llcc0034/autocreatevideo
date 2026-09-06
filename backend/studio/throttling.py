from __future__ import annotations

from rest_framework.exceptions import APIException
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle

from studio.services.configuration import ConfigurationResolutionError, resolve_configuration


class RuntimeConfigurationUnavailable(APIException):
    status_code = 503
    default_detail = "系统访问策略暂不可用。"
    default_code = "runtime_configuration_unavailable"


def _rate(key: str) -> str:
    try:
        value = resolve_configuration(required_keys=(key,)).values[key]
    except (ConfigurationResolutionError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeConfigurationUnavailable() from exc
    rate = str(value or "").strip()
    if not rate:
        raise RuntimeConfigurationUnavailable()
    return rate


class DatabaseAnonRateThrottle(AnonRateThrottle):
    def get_rate(self):
        return _rate("access.throttle_anon_rate")


class DatabaseUserRateThrottle(UserRateThrottle):
    def get_rate(self):
        return _rate("access.throttle_user_rate")


class DatabaseScopedRateThrottle(ScopedRateThrottle):
    CONFIG_KEYS = {
        "registration": "access.throttle_registration_rate",
        "asset_upload": "access.throttle_asset_upload_rate",
        "signed_media": "access.throttle_signed_media_rate",
    }

    def get_rate(self):
        key = self.CONFIG_KEYS.get(str(self.scope or ""))
        return _rate(key) if key else None
