from __future__ import annotations

import math
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from studio.services.configuration import ResolvedConfiguration


_resolution_cache: ContextVar[
    dict[tuple[str, ...], "ResolvedConfiguration"] | None
] = ContextVar("security_boundary_resolution_cache", default=None)


@contextmanager
def security_boundary_resolution_cache():
    """Reuse immutable boundary snapshots inside one top-level operation.

    The cache is deliberately scoped by ``ContextVar`` instead of process
    global state: concurrent requests remain isolated and every new request
    still observes the currently active system revision.  Nested service
    calls share the outer snapshot so graph/profile validation cannot turn a
    single readiness probe into hundreds of identical database resolutions.
    """

    current = _resolution_cache.get()
    if current is not None:
        yield
        return
    token = _resolution_cache.set({})
    try:
        yield
    finally:
        _resolution_cache.reset(token)


def boundary_resolution(keys: Iterable[str]) -> "ResolvedConfiguration":
    # Delayed import keeps this leaf helper usable from provider_registry,
    # which is itself imported by the configuration service.
    from studio.services.configuration import resolve_security_boundaries

    normalized = tuple(sorted({str(item) for item in keys if str(item)}))
    cache = _resolution_cache.get()
    if cache is not None and normalized in cache:
        return cache[normalized]
    resolved = resolve_security_boundaries(required_keys=normalized)
    if cache is not None:
        cache[normalized] = resolved
    return resolved


def boundary_values(*keys: str) -> dict[str, Any]:
    return boundary_resolution(keys).values


def boundary_int(key: str, *, minimum: int = 1) -> int:
    from studio.services.configuration import ConfigurationResolutionError

    value = boundary_values(key)[key]
    if type(value) is not int or value < minimum:
        raise ConfigurationResolutionError(
            f"安全边界 {key} 必须是大于等于 {minimum} 的整数",
            code="security_boundary_value_invalid",
            missing_keys=(key,),
        )
    return value


def boundary_number(key: str, *, minimum: float = 0.0) -> float:
    from studio.services.configuration import ConfigurationResolutionError

    value = boundary_values(key)[key]
    if (
        type(value) not in {int, float}
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise ConfigurationResolutionError(
            f"安全边界 {key} 必须是大于等于 {minimum} 的有限数字",
            code="security_boundary_value_invalid",
            missing_keys=(key,),
        )
    return float(value)


def boundary_bool(key: str) -> bool:
    from studio.services.configuration import ConfigurationResolutionError

    value = boundary_values(key)[key]
    if type(value) is not bool:
        raise ConfigurationResolutionError(
            f"安全边界 {key} 必须是布尔值",
            code="security_boundary_value_invalid",
            missing_keys=(key,),
        )
    return value
