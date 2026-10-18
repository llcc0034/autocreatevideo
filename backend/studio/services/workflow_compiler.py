from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

from django.core.exceptions import ValidationError

from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.workflow import (
    WorkflowBuildError,
    aligned_frames,
    validate_frame_policy,
    validate_comfy_graph,
)
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
    expected_value_type,
    resolve_workflow_semantic_values,
    validate_semantic_key,
    validate_resolution_strategy,
)


class WorkflowCompilerError(ValueError):
    pass


def _workflow_graph_max_bytes() -> int:
    return boundary_int("security.workflow.max_graph_bytes")




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


def _validated_static_semantics(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate immutable, non-widget facts owned by a workflow version."""

    raw = contract.get("static_semantics", {})
    if not isinstance(raw, dict):
        raise WorkflowCompilerError("input_contract.static_semantics 必须是对象")
    values: dict[str, Any] = {}
    for key in sorted(raw):
        try:
            validate_semantic_key(key)
        except WorkflowSemanticError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
        expected = expected_value_type(key)
        if expected is None:
            raise WorkflowCompilerError(
                f"static semantic {key} 必须使用已登记的通用语义键",
            )
        value = raw[key]
        valid = (
            (expected == "boolean" and type(value) is bool)
            or (expected == "integer" and type(value) is int)
            or (
                expected == "number"
                and type(value) in {int, float}
                and math.isfinite(float(value))
            )
            or (expected in {"string", "enum"} and isinstance(value, str))
        )
        if not valid:
            raise WorkflowCompilerError(
                f"static semantic {key} 必须使用 {expected} 类型",
            )
        values[key] = deepcopy(value)
    return values


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
    return payload, json_contract(
        payload,
        max_bytes=_workflow_graph_max_bytes(),
    ).sha256


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


def workflow_primary_output_contract(contract: dict[str, Any], graph: dict[str, Any], expected_kind: str) -> dict[str, Any]:
    outputs = contract.get("outputs")
    if isinstance(outputs, dict) and isinstance(outputs.get("primary"), dict):
        primary = deepcopy(outputs["primary"])
    else:
        primary = None
    if not isinstance(primary, dict):
        raise WorkflowCompilerError("input_contract 缺少 primary output contract")
    node_id = str(primary.get("node_id") or "")
    kind = str(primary.get("kind") or "")
    if primary.get("required") is not True or kind != expected_kind or node_id not in graph:
        raise WorkflowCompilerError("primary output contract 与工作流输出不一致")
    class_type = str(graph[node_id].get("class_type") or "")
    accepted = {
        "image": {"SaveImage", "SaveImageAdvanced", "PreviewImage"},
        "video": {"SaveVideo", "CreateVideo", "VHS_VideoCombine"},
        "audio": {
            "SaveAudio", "SaveAudioAdvanced", "SaveAudioMP3", "SaveAudioOpus",
            "PreviewAudio",
        },
        "text": {"PreviewAny"},
    }
    if class_type not in accepted.get(expected_kind, set()):
        raise WorkflowCompilerError("primary output node 不是受支持的声明类型输出节点")
    return {
        "schema_version": 1,
        "primary": {"node_id": node_id, "kind": expected_kind, "required": True},
    }




def _compile_resolved_generic(*, workflow_snapshot, resolved, output_kind, compiler_version,
                              classified_groups, classified_bindings, resolution_evidence):
    contract = workflow_snapshot["input_contract"]
    static_semantics = _validated_static_semantics(contract)
    if set(static_semantics) & set(resolved.values):
        raise WorkflowCompilerError("static_semantics 不能重复 widget semantic")
    resolved_values = {**resolved.values, **static_semantics}
    if resolution_evidence is not None:
        resolved_values.update({"image.width": resolution_evidence["width"], "image.height": resolution_evidence["height"]})
    if not isinstance(classified_bindings, list) or any(not isinstance(row, dict) for row in classified_bindings):
        raise WorkflowCompilerError("分类素材缺少明确的来源绑定")
    if classified_groups is None and classified_bindings:
        raise WorkflowCompilerError("非分类图不能附带未消费的分类素材绑定")
    asset_bindings = [*resolved.asset_bindings, *classified_bindings]
    graph = workflow_snapshot["graph"]
    bindings = canonical_binding_rows(workflow_snapshot.get("semantic_bindings") or [])
    graph = materialize_generic_workflow_graph(graph=graph, contract=contract, bindings=bindings,
        values=resolved.values, compiler_version=compiler_version, classified_groups=classified_groups)
    if resolution_evidence is not None:
        from studio.services.workflow_resolution import materialize_resolution_graph, WorkflowResolutionError
        try:
            graph = materialize_resolution_graph(graph, contract["resolution_strategy"], resolution_evidence["width"], resolution_evidence["height"])
        except WorkflowResolutionError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
    try:
        validate_comfy_graph(graph)
    except WorkflowBuildError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
    if contains_legacy_placeholder(graph):
        raise WorkflowCompilerError("compiled workflow 仍包含 legacy 占位符")
    output_contract = workflow_primary_output_contract(contract, graph, output_kind)
    if resolution_evidence is not None:
        output_contract = {**output_contract, "schema_version": 2, "resolution": resolution_evidence}
    return CompiledWorkflow(
        graph=graph,
        graph_sha256=_canonical_sha256(graph),
        compiler_id="generic.comfy",
        compiler_version=compiler_version,
        resolved_semantics=resolved_values,
        resolved_semantics_sha256=json_contract(
            resolved_values,
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256,
        asset_bindings=asset_bindings,
        asset_bindings_sha256=_canonical_sha256(asset_bindings),
        model_requirements=resolved.model_requirements,
        model_requirements_sha256=resolved.model_requirements_sha256,
        output_contract=output_contract,
        workflow_contract_sha256=str(workflow_snapshot["execution_contract_sha256"]),
    )


def materialize_generic_workflow_graph(*, graph, contract, bindings, values, compiler_version,
                                      classified_groups):
    """One graph materializer for existing project and explicit request facts.

    Inputs are already resolved typed semantics and verified frozen wiring.
    No ORM owner, media download, default lookup or model-specific interpretation.
    Never mutate the caller's frozen graph, even when optional slots are absent.
    """
    if type(compiler_version) is not int or compiler_version not in {1, 2, 3}:
        raise WorkflowCompilerError("未注册的通用图编译版本")
    graph = deepcopy(graph)
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
        if key not in values:
            if binding["is_required"]:
                raise WorkflowCompilerError(f"必填 semantic 未解析: {key}")
            # Optional upload-backed inputs are persisted with a managed
            # marker so the immutable graph still has a stable binding
            # target.  When one concrete execution omits that slot, remove
            # the optional widget input entirely; leaving the marker behind
            # would turn a valid optional omission into an unauthorized file
            # reference at submit time.
            inputs.pop(binding["input_name"], None)
            continue
        value = values[key]
        if isinstance(value, (dict, list)):
            raise WorkflowCompilerError("semantic compiler 禁止向 widget 注入对象或数组")
        inputs[binding["input_name"]] = deepcopy(value)
    if compiler_version == 2:
        from studio.services.workflow_asset_groups import materialize_group_graph, AssetGroupError
        try:
            graph = materialize_group_graph(graph, contract, set(values))
        except AssetGroupError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
    elif contract.get("asset_groups"):
        raise WorkflowCompilerError("动态素材合同必须使用已注册动态编译能力")
    if compiler_version == 3:
        from studio.services.workflow_classified_references import materialize_reference_groups, ClassifiedReferenceError
        try:
            graph = materialize_reference_groups(graph, contract, classified_groups)
        except ClassifiedReferenceError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
    elif contract.get("classified_references"):
        raise WorkflowCompilerError("分类素材必须使用已注册的通用分类编译器")
    elif classified_groups is not None:
        raise WorkflowCompilerError("固定图不能混入分类参考组")
    return graph


def _validate_runtime_frame_policy(contract, values, total_duration_ms, video_duration_policy):
    policy = {**contract["frame_policy"], "fps": values.get("video.fps")}
    frames = values.get("video.frames")
    try:
        validate_frame_policy(policy)
        if total_duration_ms is not None and (type(total_duration_ms) is not int or total_duration_ms < 0):
            raise WorkflowBuildError("视频时长必须是明确的非负整数毫秒")
        if video_duration_policy is not None and total_duration_ms is None:
            raise WorkflowBuildError("已指定分镜时长合同，不能缺少本次时长")
        if type(frames) is not int or not policy["minimum_frames"] <= frames <= policy["maximum_frames"] or (frames - policy["alignment_remainder"]) % policy["alignment_modulus"]:
            raise WorkflowBuildError("video.frames 不满足冻结范围或格点")
    except (WorkflowBuildError, KeyError, TypeError, ValueError) as exc:
        raise WorkflowCompilerError("视频时钟配置无效或参数不满足冻结范围") from exc
    if video_duration_policy is not None:
        duration = total_duration_ms
        if not video_duration_policy["minimum_duration_ms"] <= duration <= video_duration_policy["maximum_duration_ms"]:
            raise WorkflowCompilerError("工作流请求时长超出冻结的生产路由范围")
        if frames != aligned_frames(duration, policy):
            raise WorkflowCompilerError("video.frames 与冻结分镜时长不一致")




def validate_frozen_workflow_snapshot(workflow_snapshot):
    """Shared frozen graph/Projector integrity; not current runtime admission."""
    if not isinstance(workflow_snapshot, dict):
        raise WorkflowCompilerError("workflow snapshot 必须是对象")
    if not str(workflow_snapshot.get("model_identifier") or "").strip():
        raise WorkflowCompilerError("workflow snapshot 缺少冻结 model_identifier")
    graph = workflow_snapshot.get("graph")
    contract = workflow_snapshot.get("input_contract")
    if not isinstance(graph, dict) or not graph or not isinstance(contract, dict):
        raise WorkflowCompilerError("workflow snapshot graph/input_contract 无效")
    from studio.services.workflow_projectors import (
        WorkflowProjectorError,
        validate_frozen_workflow_projector_contract,
    )

    try:
        projector = validate_frozen_workflow_projector_contract(contract)
    except WorkflowProjectorError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
    if _canonical_sha256(graph) != str(workflow_snapshot.get("graph_sha256") or ""):
        raise WorkflowCompilerError("workflow snapshot graph hash 不一致")
    if _canonical_sha256(contract) != str(
        workflow_snapshot.get("input_contract_sha256") or ""
    ):
        raise WorkflowCompilerError("workflow snapshot input_contract hash 不一致")
    if contract.get("compiler") == {"id": "generic.comfy", "version": 3}:
        from studio.services.workflow_classified_references import validate_frozen, ClassifiedReferenceError
        try:
            from studio.services.workflow_projectors import _matches
            if not _matches(projector, graph):
                raise ClassifiedReferenceError("图结构与冻结 Projector 不一致")
            validate_frozen(contract, graph, projector.definition, {"conditioning_contract": contract.get("conditioning_contract")}, workflow_snapshot.get("semantic_bindings") or [], workflow_snapshot.get("model_requirements") or [])
        except (ClassifiedReferenceError, ValidationError) as exc:
            raise WorkflowCompilerError(str(exc)) from exc
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
    return compiler_id, compiler_version




def compile_frozen_workflow_values(*, workflow_snapshot, scalar_values, resolve_asset,
                                   classified_groups, classified_bindings, output_kind, total_duration_ms,
                                   video_duration_policy=None):
    """Adapt explicitly prepared request values to the SAME graph compiler.

    The trusted preparation adapter supplies authorized media facts. This entry
    does not create PromptArtifact/Project stand-ins, read template examples as
    request defaults, or constitute permission to submit to ComfyUI.
    """
    compiler_id, compiler_version = validate_frozen_workflow_snapshot(workflow_snapshot)
    compiler = workflow_family_for(compiler_id, compiler_version)
    contract, graph = workflow_snapshot["input_contract"], workflow_snapshot["graph"]
    bindings = canonical_binding_rows(workflow_snapshot.get("semantic_bindings") or [])
    requirements = canonical_requirement_rows(workflow_snapshot.get("model_requirements") or [])
    media_types = {"model_artifact", "asset_image", "asset_video", "asset_audio"}
    scalar_keys = {row["semantic_key"] for row in bindings if row["value_type"] not in media_types}
    if not isinstance(scalar_values, dict) or set(scalar_values) - scalar_keys:
        raise WorkflowCompilerError("逐次值只能包含明确接线的标量，不能覆盖模型文件或素材来源")
    if not callable(resolve_asset):
        raise WorkflowCompilerError("需要正式来源适配器解析素材，不能采用图中示例文件")
    if compiler_version == 2:
        from studio.services.workflow_asset_groups import validate_frozen_groups, AssetGroupError
        try:
            validate_frozen_groups(contract)
        except AssetGroupError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
        for key, definition in contract["parameters"].items():
            if definition.get("asset_group_constant") and scalar_values.get(key) != definition["default"]:
                raise WorkflowCompilerError("逐项模板常量必须使用冻结值")
    try:
        validate_resolution_strategy(graph=graph, contract=contract, bindings=bindings, output_kind=output_kind)
        if contains_legacy_placeholder(graph):
            raise WorkflowCompilerError("generic workflow graph 仍包含 legacy ${TOKEN} 占位符")
        resolved = resolve_workflow_semantic_values(graph=graph, contract=contract, bindings=bindings,
            requirements=requirements, resolve_scalar=lambda key, definition, value_type: scalar_values.get(key),
            resolve_asset=resolve_asset)
    except WorkflowSemanticError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
    resolution_evidence = None
    if contract.get("resolution_strategy", {}).get("mode") == "source_image_derived":
        from studio.services.workflow_resolution import resolve_prepared_image_resolution, WorkflowResolutionError
        try:
            resolution_evidence = resolve_prepared_image_resolution(contract["resolution_strategy"], resolved.asset_bindings)
        except WorkflowResolutionError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
    if contract.get("frame_policy"):
        _validate_runtime_frame_policy(contract, {**resolved.values, **_validated_static_semantics(contract)},
            total_duration_ms, video_duration_policy)
    return compiler(workflow_snapshot=workflow_snapshot, resolved=resolved,
        output_kind=output_kind, compiler_version=compiler_version, classified_groups=classified_groups,
        classified_bindings=classified_bindings,
        resolution_evidence=resolution_evidence)


# Audited graph protocols share one explicit-facts implementation. No entry
# accepts a Project/PromptArtifact or discovers values from mutable defaults.
register_workflow_family("generic.comfy", 1, _compile_resolved_generic)
register_workflow_family("generic.comfy", 2, _compile_resolved_generic)
register_workflow_family("generic.comfy", 3, _compile_resolved_generic)
