"""Scheduling and expiry at probe creation; consumers keep normal expires_at checks."""
from datetime import timedelta

from studio.services.configuration import ConfigurationResolutionError, resolve_configuration


PROBE_SETTING_KEYS = (
    "runtime.host_probe_mode",
    "runtime.capability_ttl_seconds",
    "runtime.host_probe_manual_validity_days",
)


def host_probe_settings(values=None):
    if values is None:
        values = resolve_configuration(required_keys=PROBE_SETTING_KEYS).values
    mode = values[PROBE_SETTING_KEYS[0]]
    seconds = values[PROBE_SETTING_KEYS[1]]
    days = values[PROBE_SETTING_KEYS[2]]
    if type(mode) is not str or mode not in {"automatic", "manual"} or type(seconds) is not int or seconds <= 0 or type(days) is not int or days <= 0:
        raise ConfigurationResolutionError("主机探测模式或有效期配置无效", code="runtime_host_probe_settings_invalid")
    return mode, timedelta(days=days) if mode == "manual" else timedelta(seconds=seconds)
