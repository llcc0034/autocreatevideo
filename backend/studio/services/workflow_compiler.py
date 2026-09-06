from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Callable

from django.core.exceptions import ValidationError

from studio.models import PromptArtifact, SeriesProject
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.workflow import (
    WorkflowBuildError,
    aligned_frames,
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
    resolve_workflow_semantics,
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


def _output_contract(contract: dict[str, Any], graph: dict[str, Any], expected_kind: str) -> dict[str, Any]:
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


def _compile_generic(
    *,
    workflow_snapshot: dict[str, Any],
    prompt: PromptArtifact,
    project: SeriesProject,
    overrides: dict[str, Any] | None = None,
    video_duration_policy: dict[str, int] | None = None,
    _compiler_version: int = 1,
) -> CompiledWorkflow:
    graph = deepcopy(workflow_snapshot["graph"])
    contract = workflow_snapshot["input_contract"]
    if _compiler_version == 2:
        from studio.services.workflow_asset_groups import validate_frozen_groups, AssetGroupError
        try:
            validate_frozen_groups(contract)
        except AssetGroupError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
        for key, value in (overrides or {}).items():
            parameter = contract.get("parameters", {}).get(key, {})
            if parameter.get("asset_group_constant") and value != parameter.get("default"):
                raise WorkflowCompilerError("逐项模板参数不能在运行时单独改变")
    bindings = canonical_binding_rows(workflow_snapshot.get("semantic_bindings") or [])
    requirements = canonical_requirement_rows(workflow_snapshot.get("model_requirements") or [])
    try:
        validate_resolution_strategy(
            graph=graph,
            contract=contract,
            bindings=bindings,
            output_kind=prompt.prompt_type,
        )
    except WorkflowSemanticError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
    if contains_legacy_placeholder(graph):
        raise WorkflowCompilerError("generic workflow graph 仍包含 legacy ${TOKEN} 占位符")
    resolved = resolve_workflow_semantics(
        graph=graph,
        contract=contract,
        bindings=bindings,
        requirements=requirements,
        prompt=prompt,
        project=project,
        overrides=overrides,
    )
    static_semantics = _validated_static_semantics(contract)
    if any(key in (overrides or {}) and overrides[key] != value for key, value in static_semantics.items()):
        raise WorkflowCompilerError("静态语义由冻结版本决定，不能运行时覆盖")
    overlap = sorted(set(static_semantics) & set(resolved.values))
    if overlap:
        raise WorkflowCompilerError(
            "static_semantics 不能重复 widget semantic: " + ", ".join(overlap),
        )
    resolved_values = {**resolved.values, **static_semantics}
    asset_bindings = resolved.asset_bindings
    if contract.get("frame_policy"):
        _validate_runtime_frame_policy(contract, resolved_values, prompt, video_duration_policy)
    resolution_evidence = None
    strategy = contract.get("resolution_strategy") or {}
    if strategy.get("mode") == "source_image_derived":
        from studio.services.workflow_resolution import resolve_source_image_resolution, WorkflowResolutionError
        try:
            resolution_evidence = resolve_source_image_resolution(
                strategy, resolved.asset_bindings, owner_id=project.owner_id,
            )
        except WorkflowResolutionError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
        resolved_values.update({
            "image.width": resolution_evidence["width"],
            "image.height": resolution_evidence["height"],
        })
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
            # Optional upload-backed inputs are persisted with a managed
            # marker so the immutable graph still has a stable binding
            # target.  When one concrete execution omits that slot, remove
            # the optional widget input entirely; leaving the marker behind
            # would turn a valid optional omission into an unauthorized file
            # reference at submit time.
            inputs.pop(binding["input_name"], None)
            continue
        value = resolved.values[key]
        if isinstance(value, (dict, list)):
            raise WorkflowCompilerError("semantic compiler 禁止向 widget 注入对象或数组")
        inputs[binding["input_name"]] = deepcopy(value)
    if _compiler_version == 2:
        from studio.services.workflow_asset_groups import materialize_group_graph, AssetGroupError
        try:
            graph = materialize_group_graph(graph, contract, set(resolved.values))
        except AssetGroupError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
    elif contract.get("asset_groups"):
        raise WorkflowCompilerError("动态素材合同必须使用已注册动态编译能力")
    if _compiler_version == 3:
        from studio.services.workflow_classified_references import materialize, prompt_references, ClassifiedReferenceError
        try:
            references = prompt_references(contract, prompt)
            graph = materialize(graph, contract, references)
            asset_bindings = [*asset_bindings, *_canonical_reference_asset_bindings(references)]
        except ClassifiedReferenceError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
    elif contract.get("classified_references"):
        raise WorkflowCompilerError("分类素材必须使用已注册的通用分类编译器")
    if resolution_evidence is not None:
        from studio.services.workflow_resolution import materialize_resolution_graph, WorkflowResolutionError
        try:
            graph = materialize_resolution_graph(graph, strategy, resolution_evidence["width"], resolution_evidence["height"])
        except WorkflowResolutionError as exc:
            raise WorkflowCompilerError(str(exc)) from exc
    try:
        validate_comfy_graph(graph)
    except WorkflowBuildError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
    if contains_legacy_placeholder(graph):
        raise WorkflowCompilerError("compiled workflow 仍包含 legacy 占位符")
    output_contract = _output_contract(contract, graph, prompt.prompt_type)
    if resolution_evidence is not None:
        output_contract = {**output_contract, "schema_version": 2, "resolution": resolution_evidence}
    return CompiledWorkflow(
        graph=graph,
        graph_sha256=_canonical_sha256(graph),
        compiler_id="generic.comfy",
        compiler_version=_compiler_version,
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


def _validate_runtime_frame_policy(contract, values, prompt, video_duration_policy):
    policy = {**contract["frame_policy"], "fps": values.get("video.fps")}
    frames = values.get("video.frames")
    try:
        expected = aligned_frames(int(prompt.total_duration_ms or 0), policy)
        if type(frames) is not int or not policy["minimum_frames"] <= frames <= policy["maximum_frames"] or (frames - policy["alignment_remainder"]) % policy["alignment_modulus"]:
            raise WorkflowBuildError("video.frames 不满足冻结范围或格点")
    except (WorkflowBuildError, KeyError, TypeError, ValueError) as exc:
        raise WorkflowCompilerError("视频时钟配置无效或参数不满足冻结范围") from exc
    if video_duration_policy is not None:
        duration = int(prompt.total_duration_ms or 0)
        if not video_duration_policy["minimum_duration_ms"] <= duration <= video_duration_policy["maximum_duration_ms"]:
            raise WorkflowCompilerError("工作流请求时长超出冻结的生产路由范围")
        if frames != expected:
            raise WorkflowCompilerError("video.frames 与冻结分镜时长不一致")


def _canonical_reference_asset_bindings(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Freeze only execution facts needed by staging and prompt provenance."""

    result: list[dict[str, Any]] = []
    for index, item in enumerate(references, start=1):
        asset_id = str(item.get("asset_id") or "")
        checksum = str(item.get("checksum") or "").casefold()
        if not asset_id or len(checksum) != 64:
            raise WorkflowCompilerError("reference binding 缺少 asset_id/checksum")
        category = str(item.get("category") or "")
        row = {
            "semantic_key": f"asset.reference.{index}",
            "asset_id": asset_id,
            "token": f"__AUTOPROMPT_ASSET__:{asset_id}",
            "media_kind": (
                "video" if category == "video"
                else "audio" if category == "audio"
                else "image"
            ),
            "mime_type": str(item.get("mime_type") or ""),
            "checksum": checksum,
            "category": category,
            "input_slot": item.get("input_slot"),
            "input_label": item.get("input_label"),
            "entity_id": item.get("entity_id"),
            "entity_name": str(item.get("entity_name") or ""),
            "purpose": item.get("purpose"),
            "audio_asset_id": item.get("audio_asset_id"),
            "audio_checksum": str(item.get("audio_checksum") or "").casefold(),
            "audio_input_label": item.get("audio_input_label"),
            "audio_purpose": item.get("audio_purpose"),
            "use_embedded_audio": bool(item.get("use_embedded_audio", False)),
        }
        if row["audio_asset_id"] and len(row["audio_checksum"]) != 64:
            raise WorkflowCompilerError("外部配套音频缺少 checksum")
        result.append(row)
    return result




RuntimeOverrideBuilder = Callable[..., dict[str, Any]]


def _require_runtime_semantic_bindings(
    workflow_snapshot: dict[str, Any],
    *,
    required_keys: set[str],
    adapter_label: str,
) -> None:
    binding_keys = {
        str(item.get("semantic_key") or "")
        for item in (workflow_snapshot.get("semantic_bindings") or [])
        if isinstance(item, dict)
    }
    missing = sorted(required_keys - binding_keys)
    if missing:
        raise WorkflowCompilerError(
            f"{adapter_label} workflow 缺少运行身份语义绑定: "
            + ", ".join(missing),
        )


def _generic_runtime_overrides(
    *,
    workflow_snapshot: dict[str, Any],
    prompt: PromptArtifact,
    width: int,
    height: int,
    seed: int,
    output_prefix: str,
) -> dict[str, Any]:
    derived = (workflow_snapshot.get("input_contract", {}).get("resolution_strategy") or {}).get("mode") == "source_image_derived"
    values = {"sampling.seed": seed, "output.prefix": output_prefix}
    if not derived:
        values.update({"image.width": width, "image.height": height})
    frame_policy = workflow_snapshot.get("input_contract", {}).get("frame_policy")
    if frame_policy:
        values["video.frames"] = aligned_frames(int(prompt.total_duration_ms or 0), frame_policy)
    _require_runtime_semantic_bindings(
        workflow_snapshot,
        required_keys=set(values),
        adapter_label="generic.comfy/v1",
    )
    return values




_RUNTIME_OVERRIDE_BUILDERS: dict[tuple[str, int], RuntimeOverrideBuilder] = {
    ("generic.comfy", 1): _generic_runtime_overrides,
    ("generic.comfy", 2): _generic_runtime_overrides,
    ("generic.comfy", 3): _generic_runtime_overrides,
}


def build_frozen_workflow_runtime_overrides(
    *,
    workflow_snapshot: dict[str, Any],
    prompt: PromptArtifact,
    width: int,
    height: int,
    seed: int,
    output_prefix: str,
) -> dict[str, Any]:
    """Dispatch execution-identity binding through the registered compiler adapter."""

    compiler_id, compiler_version = compiler_contract_from_input(
        workflow_snapshot.get("input_contract") or {},
    )
    if (
        workflow_snapshot.get("compiler_id") != compiler_id
        or workflow_snapshot.get("compiler_version") != compiler_version
    ):
        raise WorkflowCompilerError(
            "workflow snapshot compiler 与 input_contract 不一致",
        )
    builder = _RUNTIME_OVERRIDE_BUILDERS.get((compiler_id, compiler_version))
    if builder is None:
        raise WorkflowCompilerError(
            f"workflow compiler {compiler_id}/v{compiler_version} "
            "未注册运行身份绑定适配器",
        )
    return builder(
        workflow_snapshot=workflow_snapshot,
        prompt=prompt,
        width=int(width),
        height=int(height),
        seed=int(seed),
        output_prefix=str(output_prefix),
    )


register_workflow_family("generic.comfy", 1, _compile_generic)
register_workflow_family("generic.comfy", 2, lambda **kwargs: _compile_generic(**kwargs, _compiler_version=2))
register_workflow_family("generic.comfy", 3, lambda **kwargs: _compile_generic(**kwargs, _compiler_version=3))


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
    compiler = workflow_family_for(compiler_id, compiler_version)
    compiler_kwargs = {
        "workflow_snapshot": workflow_snapshot,
        "prompt": prompt,
        "project": project,
        "overrides": overrides,
    }
    compiler_kwargs["video_duration_policy"] = video_duration_policy
    try:
        return compiler(**compiler_kwargs)
    except WorkflowSemanticError as exc:
        raise WorkflowCompilerError(str(exc)) from exc
