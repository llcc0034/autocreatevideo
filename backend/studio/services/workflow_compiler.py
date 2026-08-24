from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from types import SimpleNamespace
from typing import Any

from studio.models import PromptArtifact, SeriesProject
from studio.services.json_contracts import json_contract
from studio.services.workflow import WorkflowBuildError, build_workflow, validate_comfy_graph
from studio.services.workflow_family_registry import (
    WorkflowFamilyError,
    register_workflow_family,
    workflow_family_for,
)
from studio.services.workflow_semantics import (
    WorkflowSemanticError,
    canonical_binding_rows,
    canonical_requirement_rows,
    contains_legacy_placeholder,
    execution_contract_payload,
    resolve_workflow_semantics,
)


class WorkflowCompilerError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledWorkflow:
    graph: dict[str, Any]
    graph_sha256: str
    compiler_id: str
    compiler_version: int
    resolved_semantics: dict[str, Any]
    resolved_semantics_sha256: str
    asset_bindings: list[dict[str, Any]]
    asset_bindings_sha256: str
    model_requirements: list[dict[str, Any]]
    model_requirements_sha256: str
    output_contract: dict[str, Any]
    workflow_contract_sha256: str


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def workflow_execution_contract(
    *,
    compiler_id: str,
    compiler_version: int,
    graph_sha256: str,
    input_contract_sha256: str,
    bindings: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    payload = execution_contract_payload(
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        graph_sha256=graph_sha256,
        input_contract_sha256=input_contract_sha256,
        bindings=bindings,
        requirements=requirements,
    )
    return payload, json_contract(payload).sha256


def compiler_contract_from_input(input_contract: dict[str, Any]) -> tuple[str, int]:
    compiler = input_contract.get("compiler") if isinstance(input_contract, dict) else None
    if not isinstance(compiler, dict):
        raise WorkflowCompilerError("input_contract 缺少 compiler 契约")
    compiler_id = str(compiler.get("id") or "").strip()
    version = compiler.get("version")
    if not compiler_id or isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise WorkflowCompilerError("input_contract.compiler id/version 无效")
    try:
        workflow_family_for(compiler_id, version)
    except WorkflowFamilyError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
    return compiler_id, version


def _output_contract(contract: dict[str, Any], graph: dict[str, Any], expected_kind: str) -> dict[str, Any]:
    outputs = contract.get("outputs")
    if isinstance(outputs, dict) and isinstance(outputs.get("primary"), dict):
        primary = deepcopy(outputs["primary"])
    else:
        legacy = contract.get("output_contract")
        if expected_kind == "video" and isinstance(legacy, dict):
            primary = deepcopy(legacy.get("primary_video"))
        else:
            primary = None
    if not isinstance(primary, dict):
        raise WorkflowCompilerError("input_contract 缺少 primary output contract")
    node_id = str(primary.get("node_id") or "")
    kind = str(primary.get("kind") or primary.get("media_kind") or "")
    if kind != expected_kind or node_id not in graph:
        raise WorkflowCompilerError("primary output contract 与工作流输出不一致")
    class_type = str(graph[node_id].get("class_type") or "")
    accepted = {
        "image": {"SaveImage", "PreviewImage"},
        "video": {"SaveVideo", "CreateVideo"},
    }
    if class_type not in accepted.get(expected_kind, set()):
        raise WorkflowCompilerError("primary output node 不是受支持的媒体输出节点")
    return {
        "schema_version": 1,
        "primary": {"node_id": node_id, "kind": expected_kind, "required": True},
    }


def _compile_generic(
    *,
    workflow_snapshot: dict[str, Any],
    prompt: PromptArtifact,
    project: SeriesProject,
    overrides: dict[str, Any] | None = None,
) -> CompiledWorkflow:
    graph = deepcopy(workflow_snapshot["graph"])
    contract = workflow_snapshot["input_contract"]
    bindings = canonical_binding_rows(workflow_snapshot.get("semantic_bindings") or [])
    requirements = canonical_requirement_rows(workflow_snapshot.get("model_requirements") or [])
    if contains_legacy_placeholder(graph):
        raise WorkflowCompilerError("generic workflow graph 仍包含 legacy ${TOKEN} 占位符")
    resolved = resolve_workflow_semantics(
        contract=contract,
        bindings=bindings,
        requirements=requirements,
        prompt=prompt,
        project=project,
        overrides=overrides,
    )
    seen_targets: set[tuple[str, str]] = set()
    for binding in bindings:
        target = (binding["node_id"], binding["input_name"])
        if target in seen_targets:
            raise WorkflowCompilerError("semantic bindings 包含重复 node/input 目标")
        seen_targets.add(target)
        node = graph.get(binding["node_id"])
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict) or binding["input_name"] not in inputs:
            raise WorkflowCompilerError(
                f"semantic binding 目标不存在: {binding['node_id']}.{binding['input_name']}"
            )
        current = inputs[binding["input_name"]]
        if (
            isinstance(current, list) and len(current) == 2
            and isinstance(current[0], str) and isinstance(current[1], int)
        ):
            raise WorkflowCompilerError("generic semantic binding 禁止覆盖 ComfyUI 节点连接")
        key = binding["semantic_key"]
        if key not in resolved.values:
            if binding["is_required"]:
                raise WorkflowCompilerError(f"必填 semantic 未解析: {key}")
            continue
        value = resolved.values[key]
        if isinstance(value, (dict, list)):
            raise WorkflowCompilerError("semantic compiler 禁止向 widget 注入对象或数组")
        inputs[binding["input_name"]] = deepcopy(value)
    try:
        validate_comfy_graph(graph)
    except WorkflowBuildError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
    if contains_legacy_placeholder(graph):
        raise WorkflowCompilerError("compiled workflow 仍包含 legacy 占位符")
    output_contract = _output_contract(contract, graph, prompt.prompt_type)
    return CompiledWorkflow(
        graph=graph,
        graph_sha256=_canonical_sha256(graph),
        compiler_id="generic.comfy",
        compiler_version=1,
        resolved_semantics=resolved.values,
        resolved_semantics_sha256=resolved.values_sha256,
        asset_bindings=resolved.asset_bindings,
        asset_bindings_sha256=resolved.asset_bindings_sha256,
        model_requirements=resolved.model_requirements,
        model_requirements_sha256=resolved.model_requirements_sha256,
        output_contract=output_contract,
        workflow_contract_sha256=str(workflow_snapshot["execution_contract_sha256"]),
    )


def _compile_minimax_h3(
    *,
    workflow_snapshot: dict[str, Any],
    prompt: PromptArtifact,
    project: SeriesProject,
    overrides: dict[str, Any] | None = None,
    video_duration_policy: dict[str, int] | None = None,
) -> CompiledWorkflow:
    """Controlled bridge for H3 dynamic topology while it is migrated out of the legacy module."""
    template_data = workflow_snapshot.get("template") or {}
    template = SimpleNamespace(
        is_active=True,
        definition=deepcopy(workflow_snapshot["graph"]),
        input_schema=deepcopy(workflow_snapshot["input_contract"]),
        model=str(workflow_snapshot.get("model_identifier") or "minimax-h3"),
        slug=str(template_data.get("slug") or "frozen-minimax-h3"),
    )
    legacy_overrides = dict(overrides or {})
    try:
        graph, injected, _ = build_workflow(
            template,
            prompt,
            project,
            legacy_overrides,
            video_duration_policy=video_duration_policy,
        )
    except WorkflowBuildError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
    requirements = canonical_requirement_rows(workflow_snapshot.get("model_requirements") or [])
    output_contract = _output_contract(workflow_snapshot["input_contract"], graph, prompt.prompt_type)
    asset_bindings = [{
        "semantic_key": f"h3.reference.{index + 1}",
        "asset_id": str(item.get("asset_id") or item.get("id") or ""),
        "token": f"__AUTOPROMPT_ASSET__:{item.get('asset_id') or item.get('id')}",
        "media_kind": (
            "video" if item.get("category") == "video"
            else "audio" if item.get("category") == "audio"
            else "image"
        ),
        "mime_type": str(item.get("mime_type") or ""),
        "checksum": str(item.get("checksum") or ""),
        "category": str(item.get("category") or ""),
        "audio_asset_id": item.get("audio_asset_id"),
        "audio_checksum": str(item.get("audio_checksum") or ""),
        "use_embedded_audio": bool(item.get("use_embedded_audio", False)),
    } for index, item in enumerate(
        prompt.structured_payload.get("reference_bindings", []) or []
    )]
    return CompiledWorkflow(
        graph=graph,
        graph_sha256=_canonical_sha256(graph),
        compiler_id="minimax_h3",
        compiler_version=1,
        resolved_semantics=injected,
        resolved_semantics_sha256=json_contract(injected).sha256,
        asset_bindings=asset_bindings,
        asset_bindings_sha256=json_contract(asset_bindings).sha256,
        model_requirements=requirements,
        model_requirements_sha256=json_contract(requirements).sha256,
        output_contract=output_contract,
        workflow_contract_sha256=str(workflow_snapshot["execution_contract_sha256"]),
    )


register_workflow_family("generic.comfy", 1, _compile_generic)
register_workflow_family("minimax_h3", 1, _compile_minimax_h3)


def compile_frozen_workflow(
    *,
    workflow_snapshot: dict[str, Any],
    prompt: PromptArtifact,
    project: SeriesProject,
    overrides: dict[str, Any] | None = None,
    video_duration_policy: dict[str, int] | None = None,
) -> CompiledWorkflow:
    if not isinstance(workflow_snapshot, dict):
        raise WorkflowCompilerError("workflow snapshot 必须是对象")
    graph = workflow_snapshot.get("graph")
    contract = workflow_snapshot.get("input_contract")
    if not isinstance(graph, dict) or not graph or not isinstance(contract, dict):
        raise WorkflowCompilerError("workflow snapshot graph/input_contract 无效")
    compiler_id, compiler_version = compiler_contract_from_input(contract)
    if compiler_id != workflow_snapshot.get("compiler_id") or compiler_version != workflow_snapshot.get("compiler_version"):
        raise WorkflowCompilerError("workflow snapshot compiler 与 input_contract 不一致")
    bindings = canonical_binding_rows(workflow_snapshot.get("semantic_bindings") or [])
    requirements = canonical_requirement_rows(workflow_snapshot.get("model_requirements") or [])
    payload, digest = workflow_execution_contract(
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        graph_sha256=str(workflow_snapshot.get("graph_sha256") or ""),
        input_contract_sha256=str(workflow_snapshot.get("input_contract_sha256") or ""),
        bindings=bindings,
        requirements=requirements,
    )
    if digest != workflow_snapshot.get("execution_contract_sha256"):
        raise WorkflowCompilerError("workflow execution contract hash 不一致")
    if payload["semantic_bindings_sha256"] != workflow_snapshot.get("semantic_bindings_sha256"):
        raise WorkflowCompilerError("workflow semantic bindings hash 不一致")
    if payload["model_requirements_sha256"] != workflow_snapshot.get("model_requirements_sha256"):
        raise WorkflowCompilerError("workflow model requirements hash 不一致")
    compiler = workflow_family_for(compiler_id, compiler_version)
    compiler_kwargs = {
        "workflow_snapshot": workflow_snapshot,
        "prompt": prompt,
        "project": project,
        "overrides": overrides,
    }
    if compiler_id == "minimax_h3":
        compiler_kwargs["video_duration_policy"] = video_duration_policy
    try:
        return compiler(**compiler_kwargs)
    except WorkflowSemanticError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
