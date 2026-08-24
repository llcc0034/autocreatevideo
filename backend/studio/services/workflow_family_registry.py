from __future__ import annotations

from collections.abc import Callable
from typing import Any


class WorkflowFamilyError(ValueError):
    pass


WorkflowFamilyCompiler = Callable[..., Any]

_COMPILERS: dict[tuple[str, int], WorkflowFamilyCompiler] = {}


def register_workflow_family(
    compiler_id: str,
    compiler_version: int,
    compiler: WorkflowFamilyCompiler,
) -> None:
    key = (str(compiler_id).strip(), int(compiler_version))
    if not key[0] or key[1] <= 0:
        raise WorkflowFamilyError("workflow compiler id/version 无效")
    existing = _COMPILERS.get(key)
    if existing is not None and existing is not compiler:
        raise WorkflowFamilyError(f"workflow compiler 重复注册: {key[0]}.v{key[1]}")
    _COMPILERS[key] = compiler


def workflow_family_for(compiler_id: str, compiler_version: int) -> WorkflowFamilyCompiler:
    key = (str(compiler_id).strip(), int(compiler_version))
    try:
        return _COMPILERS[key]
    except KeyError as exc:
        raise WorkflowFamilyError(
            f"workflow compiler 未注册: {key[0] or '<empty>'}.v{key[1]}"
        ) from exc


def registered_workflow_families() -> tuple[tuple[str, int], ...]:
    return tuple(sorted(_COMPILERS))
