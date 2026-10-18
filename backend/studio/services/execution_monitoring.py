from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from django.core.exceptions import ValidationError

from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int


EXECUTION_MONITORING_SCHEMA_VERSION = 1


def _max_idle_timeout_seconds() -> int:
    return boundary_int("security.execution.max_idle_timeout_seconds")


def _max_type_overrides() -> int:
    return boundary_int("security.execution.max_type_overrides")


def model_execution_monitoring_editor_options() -> dict[str, int]:
    """Model edit limits share runtime validation, without production admission."""
    return {
        "max_idle_timeout_seconds": _max_idle_timeout_seconds(),
        "max_node_type_overrides": _max_type_overrides(),
    }


def _max_node_overrides() -> int:
    return boundary_int("security.execution.max_node_overrides")


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _exact_keys(value: dict[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError({label: f"包含未知字段: {', '.join(unknown)}"})


def _idle_seconds(value: object, *, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _max_idle_timeout_seconds()
    ):
        raise ValidationError({
            label: (
                f"必须是 1 到 {_max_idle_timeout_seconds()} 的整数秒"
            ),
        })
    return value


def _node_type_overrides(value: object, *, label: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > _max_type_overrides():
        raise ValidationError({
            label: f"必须是不超过 {_max_type_overrides()} 项的对象",
        })
    normalized: dict[str, int] = {}
    for raw_key, raw_seconds in value.items():
        key = str(raw_key or "").strip()
        # ComfyUI extensions are free to use display-style class_type names
        # such as ``KSampler (Efficient)``.  Monitoring keys must therefore
        # follow the workflow graph contract instead of a narrower identifier
        # grammar; only empty, unbounded or control-character keys are unsafe.
        if not key or len(key) > 191 or _CONTROL_RE.search(key) is not None:
            raise ValidationError({
                f"{label}.{key or '(empty)'}": (
                    "节点类型不能为空、超过 191 个字符或包含控制字符"
                ),
            })
        normalized[key] = _idle_seconds(raw_seconds, label=f"{label}.{key}")
    return dict(sorted(normalized.items()))


def _node_overrides(
    value: object,
    *,
    label: str,
    graph: object,
) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > _max_node_overrides():
        raise ValidationError({
            label: f"必须是不超过 {_max_node_overrides()} 项的对象",
        })
    if not isinstance(graph, dict) or not graph:
        raise ValidationError({label: "按节点覆盖必须同时提供非空工作流 graph"})
    normalized: dict[str, int] = {}
    for raw_key, raw_seconds in value.items():
        key = str(raw_key or "").strip()
        node = graph.get(key)
        if (
            not key
            or len(key) > 120
            or _CONTROL_RE.search(key) is not None
            or not isinstance(node, dict)
            or not str(node.get("class_type") or "").strip()
        ):
            raise ValidationError({f"{label}.{key or '(empty)'}": "节点不存在或声明无效"})
        normalized[key] = _idle_seconds(raw_seconds, label=f"{label}.{key}")
    return dict(sorted(normalized.items()))


def normalize_execution_monitoring_source(
    value: object,
    *,
    source: str,
    graph: object = None,
) -> dict[str, Any]:
    """Validate one editable ModelProfile or Workflow monitoring declaration.

    Model profiles may set a default and node-type windows. Node ids belong to
    a concrete workflow graph, so only Workflow declarations may set them.
    The declaration is data only; it cannot contain code, selectors or regular
    expressions executed by the worker.
    """

    label = f"{source}.execution_monitoring"
    if not isinstance(value, dict):
        raise ValidationError({label: "必须是对象"})
    if source not in {"model", "workflow"}:
        raise ValueError("execution monitoring source must be model or workflow")
    allowed = {
        "schema_version",
        "default_idle_timeout_seconds",
        "node_type_overrides",
    }
    if source == "workflow":
        allowed.add("node_overrides")
    _exact_keys(value, allowed, label=label)
    if type(value.get("schema_version")) is not int or value["schema_version"] != EXECUTION_MONITORING_SCHEMA_VERSION:
        raise ValidationError({label: "schema_version 必须为 1"})
    default = value.get("default_idle_timeout_seconds")
    return {
        "schema_version": EXECUTION_MONITORING_SCHEMA_VERSION,
        "default_idle_timeout_seconds": (
            _idle_seconds(default, label=f"{label}.default_idle_timeout_seconds")
            if default is not None
            else None
        ),
        "node_type_overrides": _node_type_overrides(
            value.get("node_type_overrides"),
            label=f"{label}.node_type_overrides",
        ),
        **({
            "node_overrides": _node_overrides(
                value.get("node_overrides"),
                label=f"{label}.node_overrides",
                graph=graph,
            ),
        } if source == "workflow" else {}),
    }


def empty_execution_monitoring_contract() -> dict[str, Any]:
    """Canonical absence of node overrides; use the root's frozen idle limit."""

    return {
        "schema_version": EXECUTION_MONITORING_SCHEMA_VERSION,
        "default_idle_timeout_seconds": None,
        "node_type_overrides": {},
        "node_overrides": {},
    }


def normalize_frozen_execution_monitoring_contract(value: object) -> dict[str, Any]:
    """Validate the effective immutable projection stored on a run/task."""

    if not isinstance(value, dict):
        raise ValidationError({"execution_monitoring": "冻结合同必须是对象"})
    _exact_keys(
        value,
        {
            "schema_version",
            "default_idle_timeout_seconds",
            "node_type_overrides",
            "node_overrides",
        },
        label="execution_monitoring",
    )
    if type(value.get("schema_version")) is not int or value["schema_version"] != EXECUTION_MONITORING_SCHEMA_VERSION:
        raise ValidationError({"execution_monitoring": "schema_version 必须为 1"})
    default = value.get("default_idle_timeout_seconds")
    node_overrides = value.get("node_overrides")
    if not isinstance(node_overrides, dict) or len(node_overrides) > _max_node_overrides():
        raise ValidationError({
            "execution_monitoring.node_overrides": (
                f"必须是不超过 {_max_node_overrides()} 项的对象"
            ),
        })
    normalized_nodes: dict[str, int] = {}
    for raw_key, raw_seconds in node_overrides.items():
        key = str(raw_key or "").strip()
        if not key or len(key) > 120 or _CONTROL_RE.search(key) is not None:
            raise ValidationError({"execution_monitoring.node_overrides": "节点 ID 格式无效"})
        normalized_nodes[key] = _idle_seconds(
            raw_seconds,
            label=f"execution_monitoring.node_overrides.{key}",
        )
    return {
        "schema_version": EXECUTION_MONITORING_SCHEMA_VERSION,
        "default_idle_timeout_seconds": (
            _idle_seconds(
                default,
                label="execution_monitoring.default_idle_timeout_seconds",
            )
            if default is not None
            else None
        ),
        "node_type_overrides": _node_type_overrides(
            value.get("node_type_overrides"),
            label="execution_monitoring.node_type_overrides",
        ),
        "node_overrides": dict(sorted(normalized_nodes.items())),
    }


def resolve_execution_monitoring_contract(
    *,
    model_specification: object = None,
    workflow_input_contract: object = None,
    workflow_graph: object = None,
) -> dict[str, Any]:
    """Merge model defaults with workflow-specific overrides.

    Precedence is concrete node id, workflow node type, model node type, then
    workflow/model default. When neither declares a default, execution uses
    the runtime idle timeout already frozen in the task capacity contract.
    """

    effective = empty_execution_monitoring_contract()
    model_raw = (
        model_specification.get("execution_monitoring")
        if isinstance(model_specification, dict)
        else None
    )
    if model_raw is not None:
        model = normalize_execution_monitoring_source(model_raw, source="model")
        effective["default_idle_timeout_seconds"] = model[
            "default_idle_timeout_seconds"
        ]
        effective["node_type_overrides"].update(model["node_type_overrides"])

    workflow_raw = (
        workflow_input_contract.get("execution_monitoring")
        if isinstance(workflow_input_contract, dict)
        else None
    )
    if workflow_raw is not None:
        workflow = normalize_execution_monitoring_source(
            workflow_raw,
            source="workflow",
            graph=workflow_graph,
        )
        if workflow["default_idle_timeout_seconds"] is not None:
            effective["default_idle_timeout_seconds"] = workflow[
                "default_idle_timeout_seconds"
            ]
        effective["node_type_overrides"].update(
            workflow["node_type_overrides"]
        )
        effective["node_overrides"].update(workflow["node_overrides"])

    return normalize_frozen_execution_monitoring_contract(effective)


def execution_monitoring_contract_sha256(contract: object) -> str:
    normalized = normalize_frozen_execution_monitoring_contract(contract)
    return json_contract(
        normalized,
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256


def verify_frozen_execution_monitoring_contract(
    contract: object,
    *,
    sha256: object,
) -> dict[str, Any]:
    normalized = normalize_frozen_execution_monitoring_contract(contract)
    expected = str(sha256 or "").strip().casefold()
    if (
        len(expected) != 64
        or json_contract(
            normalized,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        != expected
    ):
        raise ValidationError({"execution_monitoring": "冻结合同 SHA-256 无效"})
    if normalized != contract:
        raise ValidationError({"execution_monitoring": "冻结合同不是规范化形式"})
    return deepcopy(normalized)


def idle_timeout_for_node(
    contract: object,
    *,
    node_id: object,
    node_type: object,
    frozen_runtime_idle_timeout_seconds: float,
) -> float:
    normalized = normalize_frozen_execution_monitoring_contract(contract)
    node_key = str(node_id or "").strip()
    type_key = str(node_type or "").strip()
    if node_key and node_key in normalized["node_overrides"]:
        return float(normalized["node_overrides"][node_key])
    if type_key and type_key in normalized["node_type_overrides"]:
        return float(normalized["node_type_overrides"][type_key])
    if normalized["default_idle_timeout_seconds"] is not None:
        return float(normalized["default_idle_timeout_seconds"])
    return float(frozen_runtime_idle_timeout_seconds)
