from __future__ import annotations

import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import PurePosixPath
from typing import Any


SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY = "workflow.comfyui_saved_workflow_directory"
DEFAULT_SAVED_WORKFLOW_DIRECTORY = "workflows"


class SavedWorkflowDirectoryError(ValueError):
    pass


_CACHE_DISABLED = object()
_CACHE_UNRESOLVED = object()
_resolution_cache: ContextVar[object] = ContextVar(
    "saved_workflow_directory_resolution_cache",
    default=_CACHE_DISABLED,
)


def normalize_saved_workflow_directory(value: Any) -> str:
    """Validate one ComfyUI userdata-relative workflow library directory.

    The directory is deliberately constrained to ``workflows`` or one of its
    descendants.  AutoPrompt talks to ComfyUI through its userdata API, so a
    backend-local absolute path would be both non-portable and the wrong trust
    boundary for remote RuntimeHosts.
    """

    if not isinstance(value, str):
        raise SavedWorkflowDirectoryError("工作流扫描目录必须是字符串")
    normalized_unicode = unicodedata.normalize("NFC", value)
    if normalized_unicode != value or value != value.strip():
        raise SavedWorkflowDirectoryError("工作流扫描目录必须使用 NFC 且不能含首尾空白")
    if (
        not value
        or len(value) > 500
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or "%" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SavedWorkflowDirectoryError(
            "工作流扫描目录必须是 workflows 下的安全 POSIX 相对目录",
        )
    parts = value.split("/")
    if (
        not parts
        or parts[0] != DEFAULT_SAVED_WORKFLOW_DIRECTORY
        or any(
            part in {"", ".", ".."}
            or len(part) > 255
            or part != part.strip()
            for part in parts
        )
    ):
        raise SavedWorkflowDirectoryError(
            "工作流扫描目录只能是 workflows 或其子目录，且不能含空段、. 或 ..",
        )
    normalized = str(PurePosixPath(*parts))
    if normalized != value:
        raise SavedWorkflowDirectoryError("工作流扫描目录不是规范 POSIX 相对路径")
    return normalized


@contextmanager
def saved_workflow_directory_resolution_cache():
    """Freeze the active directory revision inside one top-level operation."""

    current = _resolution_cache.get()
    if current is not _CACHE_DISABLED:
        yield
        return
    token = _resolution_cache.set(_CACHE_UNRESOLVED)
    try:
        yield
    finally:
        _resolution_cache.reset(token)


def saved_workflow_directory(
    *,
    values: dict[str, Any] | None = None,
) -> str:
    from studio.services.configuration import (
        ConfigurationResolutionError,
        resolve_configuration,
    )

    if values is None:
        cached = _resolution_cache.get()
        if cached is _CACHE_DISABLED or cached is _CACHE_UNRESOLVED:
            resolved = resolve_configuration(
                required_keys=(SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY,),
            )
            if cached is _CACHE_UNRESOLVED:
                _resolution_cache.set(resolved)
        else:
            resolved = cached
        values = resolved.values
    if SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY not in values:
        raise ConfigurationResolutionError(
            f"缺少必需配置: {SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY}",
            code="required_config_missing",
            missing_keys=(SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY,),
        )
    try:
        return normalize_saved_workflow_directory(
            values[SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY],
        )
    except SavedWorkflowDirectoryError as exc:
        raise ConfigurationResolutionError(
            str(exc),
            code="saved_workflow_directory_invalid",
            missing_keys=(SAVED_WORKFLOW_DIRECTORY_CONFIG_KEY,),
        ) from exc
