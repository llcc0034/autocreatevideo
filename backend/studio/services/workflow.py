from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
import re

from studio.models import PromptArtifact, SeriesProject, WorkflowTemplate
from studio.services.resolution import (
    H3_RESOLUTION_MULTIPLE_DEFAULT,
    H3_RESOLUTION_PROFILES,
    ResolutionProfileError,
    resolve_production_resolution,
    validate_resolution_multiple,
)


PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
H3_MIN_REQUEST_DURATION_MS = 5_000
H3_MAX_REQUEST_DURATION_MS = 15_000
H3_MIN_ALIGNED_FRAMES = 124
H3_MAX_ALIGNED_FRAMES = 362

# The API prompt is data, but a Comfy workflow is executable configuration.
# Keep the approved surface deliberately small and review additions here before
# exposing them to any user-triggered submission path.
SAFE_COMFY_NODE_TYPES = frozenset({
    "UNETLoader",
    "CheckpointLoaderSimple",
    "CLIPLoader",
    "CLIPTextEncode",
    "VAELoader",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "MiniMaxH3AudioConditioningT8",
    "MiniMaxH3DualClockSamplerT8",
    "MiniMaxH3AVDecodeT8",
    "LoraLoaderModelOnly",
    "LoraLoaderBypassModelOnly",
    "RandomNoise",
    "BasicGuider",
    "KSamplerSelect",
    "BasicScheduler",
    "SamplerCustomAdvanced",
    "KSampler",
    "EmptyLatentImage",
    "VAEDecode",
    "VAEDecodeAudio",
    "LoadImage",
    "LoadVideo",
    "GetVideoComponents",
    "LoadAudio",
    "SaveImage",
    "PreviewImage",
    "CreateVideo",
    "SaveVideo",
})

_DANGEROUS_NODE_MARKERS = (
    "api", "partner", "http", "url", "websocket", "network",
    "checkpointsave", "savecheckpoint", "savetext", "savemodel", "modelsave",
    "download", "upload",
)


def _extra_allowed_node_types() -> frozenset[str]:
    """Read the server-owned extension list; clients cannot influence it."""
    raw = os.environ.get("COMFYUI_EXTRA_ALLOWED_NODE_TYPES", "")
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


class WorkflowBuildError(ValueError):
    pass


def h3_aligned_frames(duration_ms: int) -> int:
    requested = max(5, round(duration_ms * 24 / 1000))
    return requested + (5 - requested % 17) % 17


def _safe_output_prefix(value: str) -> str:
    raw = value.replace("\\", "/")
    normalized = raw.strip("/")
    segments = normalized.split("/")
    if (
        not normalized or len(normalized) > 300 or raw.startswith("/")
        or any(segment in {"", ".", ".."} for segment in segments) or ":" in normalized
    ):
        raise WorkflowBuildError("OUTPUT_PREFIX 必须是安全的相对子路径")
    return normalized


def _replace(value, variables: dict):
    if isinstance(value, dict):
        return {key: _replace(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    exact = PLACEHOLDER.fullmatch(value)
    if exact:
        key = exact.group(1)
        if key not in variables:
            raise WorkflowBuildError(f"工作流缺少变量 {key}")
        return deepcopy(variables[key])

    def substitute(match):
        key = match.group(1)
        if key not in variables:
            raise WorkflowBuildError(f"工作流缺少变量 {key}")
        replacement = variables[key]
        if isinstance(replacement, (dict, list)):
            return json.dumps(replacement, ensure_ascii=False)
        return str(replacement)

    return PLACEHOLDER.sub(substitute, value)


def _next_node_id(workflow: dict) -> str:
    numeric = [int(key) for key in workflow if str(key).isdigit()]
    return str(max(numeric, default=0) + 1)


def _find_node(workflow: dict, class_type: str) -> tuple[str, dict] | tuple[None, None]:
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type") == class_type:
            return node_id, node
    return None, None


def _validate_comfy_graph(workflow: dict) -> None:
    """Reject graphs outside the reviewed local H3/image/video node surface."""
    if not isinstance(workflow, dict) or not workflow:
        raise WorkflowBuildError("Workflow definition must be a non-empty JSON object.")
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise WorkflowBuildError("Every Comfy workflow node must be an object with a string node id.")
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type:
            raise WorkflowBuildError(f"Workflow node {node_id!r} has no valid class_type.")
        normalized = class_type.casefold().replace("_", "").replace("-", "")
        if any(marker in normalized for marker in _DANGEROUS_NODE_MARKERS):
            raise WorkflowBuildError(
                f"Dangerous Comfy node {class_type!r} is forbidden (API/Partner/network/model-save/text-save nodes are not allowed)."
            )
        if class_type not in SAFE_COMFY_NODE_TYPES | _extra_allowed_node_types():
            raise WorkflowBuildError(f"Comfy node {class_type!r} is not on the approved workflow whitelist.")
        if not isinstance(node.get("inputs", {}), dict):
            raise WorkflowBuildError(f"Workflow node {node_id!r} inputs must be an object.")


def validate_comfy_graph(workflow: dict) -> None:
    """Public worker-side validation for already persisted/generated graphs."""
    _validate_comfy_graph(workflow)


def _adapt_native_h3(workflow: dict, variables: dict, mode: str) -> dict:
    """Add optional reference nodes and safely strip audio output for native H3 graphs."""
    references = variables.get("REFERENCE_IMAGES") or []
    def staged_token(asset_id: str) -> str:
        return f"__AUTOPROMPT_ASSET__:{asset_id}"

    by_category: dict[str, list[dict]] = {}
    for item in references:
        by_category.setdefault(item.get("category", ""), []).append(item)
    base_id, base_node = _find_node(workflow, "MiniMaxH3ImageToVideo")
    ref_id, ref_node = _find_node(workflow, "MiniMaxH3ReferenceToVideo")
    if not ref_node:
        ref_id, ref_node = _find_node(workflow, "MiniMaxH3AudioConditioningT8")
    if base_node:
        invalid = set(by_category) - {"first_frame", "last_frame"}
        if invalid:
            raise WorkflowBuildError(
                f"ImageToVideo 工作流不能接收分类参考图: {', '.join(sorted(invalid))}"
            )
        first_items = by_category.get("first_frame", [])
        last_items = by_category.get("last_frame", [])
        if len(first_items) > 1 or len(last_items) > 1:
            raise WorkflowBuildError("首帧/尾帧每个槽位最多一张图片")
        first_token = staged_token(first_items[0]["asset_id"]) if first_items else None
        last_token = staged_token(last_items[0]["asset_id"]) if last_items else None
        if first_token:
            node_id = _next_node_id(workflow)
            workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": first_token}}
            base_node["inputs"]["first_frame"] = [node_id, 0]
        if last_token:
            node_id = _next_node_id(workflow)
            workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": last_token}}
            base_node["inputs"]["last_frame"] = [node_id, 0]
    elif ref_node:
        if mode != "Ref2VA":
            raise WorkflowBuildError("Reference 工作流只能用于 Ref2VA")
        allowed_categories = {"character", "scene", "prop", "video", "audio"}
        if set(by_category) - allowed_categories:
            raise WorkflowBuildError(
                "Ref2VA 工作流只能接收图片分类、参考视频和参考音频"
            )
        image_references = [
            item for item in references if item.get("category") in {"character", "scene", "prop"}
        ]
        if len(image_references) > 9:
            raise WorkflowBuildError("Ref2VA 工作流最多允许 9 张参考图")
        for index, item in enumerate(image_references):
            token = staged_token(item["asset_id"])
            node_id = _next_node_id(workflow)
            workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": token}}
            ref_node["inputs"][f"ref_images.ref_image_{index}"] = [node_id, 0]
        video_references = by_category.get("video", [])
        if len(video_references) > 3:
            raise WorkflowBuildError("Ref2VA 工作流最多允许 3 个参考视频")
        for index, item in enumerate(video_references):
            load_id = _next_node_id(workflow)
            workflow[load_id] = {
                "class_type": "LoadVideo",
                "inputs": {"file": staged_token(item["asset_id"])},
            }
            components_id = _next_node_id(workflow)
            workflow[components_id] = {
                "class_type": "GetVideoComponents",
                "inputs": {"video": [load_id, 0]},
            }
            ref_node["inputs"][f"ref_videos.ref_video_{index}"] = [components_id, 0]
            if item.get("audio_asset_id"):
                audio_id = _next_node_id(workflow)
                workflow[audio_id] = {
                    "class_type": "LoadAudio",
                    "inputs": {"audio": staged_token(item["audio_asset_id"])},
                }
                ref_node["inputs"][f"ref_video_audios.ref_video_audio_{index}"] = [audio_id, 0]
            elif item.get("use_embedded_audio"):
                ref_node["inputs"][f"ref_video_audios.ref_video_audio_{index}"] = [components_id, 1]
        audio_references = by_category.get("audio", [])
        if len(audio_references) > 3:
            raise WorkflowBuildError("Ref2VA 工作流最多允许 3 个独立参考音频")
        for index, item in enumerate(audio_references):
            audio_id = _next_node_id(workflow)
            workflow[audio_id] = {
                "class_type": "LoadAudio",
                "inputs": {"audio": staged_token(item["asset_id"])},
            }
            ref_node["inputs"][f"ref_audios.ref_audio_{index}"] = [audio_id, 0]

    if not variables.get("AUDIO_ENABLED", True):
        audio_decode_ids = {
            node_id for node_id, node in workflow.items()
            if isinstance(node, dict) and node.get("class_type") == "VAEDecodeAudio"
        }
        for node_id in audio_decode_ids:
            workflow.pop(node_id, None)
        for node in workflow.values():
            if isinstance(node, dict) and node.get("class_type") == "CreateVideo":
                node.get("inputs", {}).pop("audio", None)
        # Base does not require audio_vae; Ref2VA does. Remove only an unreferenced loader.
        referenced_ids = {
            value[0]
            for node in workflow.values() if isinstance(node, dict)
            for value in node.get("inputs", {}).values()
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
        }
        for node_id, node in list(workflow.items()):
            if (
                isinstance(node, dict)
                and node.get("class_type") == "VAELoader"
                and "audio_vae" in str(node.get("inputs", {}).get("vae_name", ""))
                and node_id not in referenced_ids
            ):
                workflow.pop(node_id)
    return workflow


def _validated_user_parameters(schema: dict, overrides: dict) -> dict:
    """Resolve only the typed, bounded parameters explicitly exposed by a template."""
    definitions = schema.get("user_parameters") or {}
    if not isinstance(definitions, dict):
        raise WorkflowBuildError("user_parameters 必须是参数定义对象")
    values = {}
    for key, definition in definitions.items():
        if not isinstance(key, str) or not PLACEHOLDER.fullmatch(f"${{{key}}}"):
            raise WorkflowBuildError("user_parameters 参数名必须是大写工作流变量")
        if not isinstance(definition, dict):
            raise WorkflowBuildError(f"参数 {key} 的定义必须是对象")
        if "default" not in definition:
            raise WorkflowBuildError(f"参数 {key} 缺少默认值")
        value = overrides.get(key, definition["default"])
        value_type = definition.get("type")
        if value_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise WorkflowBuildError(f"参数 {key} 必须是整数")
        elif value_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise WorkflowBuildError(f"参数 {key} 必须是数字")
            value = float(value)
            if not math.isfinite(value):
                raise WorkflowBuildError(f"参数 {key} 必须是有限数字")
        elif value_type == "boolean":
            if not isinstance(value, bool):
                raise WorkflowBuildError(f"参数 {key} 必须是布尔值")
        elif value_type == "select":
            options = definition.get("options") or []
            allowed = [
                item.get("value") if isinstance(item, dict) else item
                for item in options
            ]
            if value not in allowed:
                raise WorkflowBuildError(f"参数 {key} 不在允许选项内")
        else:
            raise WorkflowBuildError(f"参数 {key} 使用了不支持的类型 {value_type!r}")
        minimum = definition.get("min")
        maximum = definition.get("max")
        if minimum is not None and value < minimum:
            raise WorkflowBuildError(f"参数 {key} 不能小于 {minimum}")
        if maximum is not None and value > maximum:
            raise WorkflowBuildError(f"参数 {key} 不能大于 {maximum}")
        step = definition.get("step")
        if step is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                step = float(step)
                origin = float(minimum or 0)
                quotient = (float(value) - origin) / step
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                raise WorkflowBuildError(f"参数 {key} 的 step 定义无效") from exc
            if step <= 0 or not math.isclose(quotient, round(quotient), abs_tol=1e-7):
                raise WorkflowBuildError(f"参数 {key} 必须按步长 {step:g} 调整")
        values[key] = value
    return values


def _validate_h3_prompt_contract(template: WorkflowTemplate, prompt: PromptArtifact) -> tuple[list[dict], str]:
    schema = template.input_schema or {}
    workflow_mode = schema.get("workflow_mode")
    references = prompt.structured_payload.get("reference_bindings", [])
    if not workflow_mode:
        # Legacy/admin templates retain their historical behavior; reviewed
        # system templates always declare an explicit workflow_mode.
        return prompt.structured_payload.get("reference_assets", []), prompt.h3_mode
    if not isinstance(references, list):
        raise WorkflowBuildError("structured_payload.reference_bindings 必须是规范化数组")
    if any(not isinstance(item, dict) for item in references):
        raise WorkflowBuildError("structured_payload.reference_bindings 每项必须是对象")
    asset_ids = [item.get("asset_id") for item in references]
    asset_ids.extend(
        item.get("audio_asset_id") for item in references if item.get("audio_asset_id")
    )
    if not all(asset_ids) or len(asset_ids) != len(set(asset_ids)):
        raise WorkflowBuildError("reference_bindings.asset_id 必须存在且全局唯一")
    categories = [item.get("category") for item in references]
    labels = [
        label
        for item in references
        for label in (item.get("input_label"), item.get("audio_input_label"))
        if label
    ]
    effective_mode = prompt.structured_payload.get("effective_conditioning_mode")
    prompt_template_key = prompt.structured_payload.get("prompt_template_key")
    expected_prompt_keys = schema.get("prompt_template_keys") or []
    if prompt_template_key not in expected_prompt_keys:
        raise WorkflowBuildError(
            f"提示词模板 {prompt_template_key!r} 不属于工作流 {template.slug}"
        )
    if workflow_mode == "text_to_video":
        if references or effective_mode != "T2VA" or re.search(r"<(?:Picture|Video|Audio)\s+", prompt.prompt_text):
            raise WorkflowBuildError("T2VA 文生视频提示词和工作流均禁止任何 Picture/reference binding")
    elif workflow_mode == "first_last_to_video":
        if set(categories) - {"first_frame", "last_frame"}:
            raise WorkflowBuildError("首尾帧工作流只能接收 first_frame/last_frame")
        if len(categories) > 2 or len(categories) != len(set(categories)):
            raise WorkflowBuildError("首尾帧工作流每个帧槽最多一张图片")
        if [item.get("input_slot") for item in references] != list(range(1, len(references) + 1)):
            raise WorkflowBuildError("首尾帧 reference_bindings.input_slot 必须连续")
        if [item.get("input_label") for item in references] != [
            f"<Picture {index}>" for index in range(1, len(references) + 1)
        ]:
            raise WorkflowBuildError("首尾帧 Picture 标签与槽位不一致")
        expected_effective = (
            "FL2VA" if set(categories) == {"first_frame", "last_frame"}
            else "I2VA" if categories == ["first_frame"]
            else "L2VA" if categories == ["last_frame"]
            else "T2VA"
        )
        if effective_mode != expected_effective:
            raise WorkflowBuildError("effective_conditioning_mode 与实际首/尾帧槽位不一致")
    elif workflow_mode == "reference_to_video":
        allowed = {"character", "scene", "prop", "video", "audio"}
        if set(categories) - allowed:
            raise WorkflowBuildError("Ref2VA 只允许分类图片、参考视频和参考音频")
        images = [item for item in references if item.get("category") in {"character", "scene", "prop"}]
        videos = [item for item in references if item.get("category") == "video"]
        audios = [item for item in references if item.get("category") == "audio"]
        if len(images) > 9 or len(videos) > 3 or len(audios) > 3:
            raise WorkflowBuildError("Ref2VA 上限为 9 图 / 3 视频 / 3 独立音频")
        if [item.get("input_slot") for item in images] != list(range(1, len(images) + 1)) or [
            item.get("input_label") for item in images
        ] != [f"<Picture {index}>" for index in range(1, len(images) + 1)]:
            raise WorkflowBuildError("Ref2VA Picture 标签/槽位不连续")
        if [item.get("input_slot") for item in videos] != list(range(1, len(videos) + 1)) or [
            item.get("input_label") for item in videos
        ] != [f"<Video {index}>" for index in range(1, len(videos) + 1)]:
            raise WorkflowBuildError("Ref2VA Video 标签/槽位不连续")
        audio_labels = [
            label
            for item in videos for label in [item.get("audio_input_label")] if label
        ] + [item.get("input_label") for item in audios]
        if audio_labels != [f"<Audio {index}>" for index in range(1, len(audio_labels) + 1)]:
            raise WorkflowBuildError("Ref2VA Audio 标签必须按实际连接连续编号")
        for item in videos:
            if item.get("audio_asset_id") and item.get("use_embedded_audio"):
                raise WorkflowBuildError("参考视频不能同时使用内嵌音轨和独立配套音频")
            if bool(item.get("audio_input_label")) != bool(
                item.get("audio_asset_id") or item.get("use_embedded_audio")
            ):
                raise WorkflowBuildError("参考视频 Audio 标签与实际配套音轨连接不一致")
        if effective_mode != "Ref2VA":
            raise WorkflowBuildError("Ref2VA effective_conditioning_mode 必须为 Ref2VA")
        for item in images:
            if not item.get("entity_id") or not item.get("entity_name"):
                raise WorkflowBuildError("Ref2VA 每个 Picture 必须绑定明确的分类实体")
    else:
        raise WorkflowBuildError(f"未知的系统 workflow_mode: {workflow_mode}")
    found_labels = set(re.findall(r"<(?:Picture|Video|Audio)\s+[1-9][0-9]*>", prompt.prompt_text))
    if found_labels != set(labels):
        raise WorkflowBuildError("提示词引用标签与 reference_bindings 不完全一致")
    return references, effective_mode


def build_workflow(
    template: WorkflowTemplate,
    prompt: PromptArtifact,
    project: SeriesProject,
    overrides: dict | None = None,
) -> tuple[dict, dict, str]:
    if not template.is_active:
        raise WorkflowBuildError("工作流模板已停用")
    overrides = overrides or {}
    supported_modes = (template.input_schema or {}).get("h3_modes")
    if supported_modes and prompt.h3_mode not in supported_modes:
        raise WorkflowBuildError(f"模板不支持 {prompt.h3_mode}；允许模式: {', '.join(supported_modes)}")
    schema = template.input_schema or {}
    user_parameters = _validated_user_parameters(schema, overrides)
    workflow_mode = str(schema.get("workflow_mode") or "legacy")
    fixed_model = bool(schema.get("fixed_model"))
    if fixed_model and "MODEL" in overrides:
        raise WorkflowBuildError("该系统工作流固定使用已审核模型，禁止覆盖 MODEL")
    model_name = template.model if fixed_model else overrides.get("MODEL", prompt.model)
    if fixed_model and not model_name:
        raise WorkflowBuildError("固定模型工作流缺少 template.model")
    is_h3 = "minimax-h3" in str(model_name).lower()
    if is_h3 and prompt.prompt_type == "video":
        reference_images, effective_mode = _validate_h3_prompt_contract(template, prompt)
    elif prompt.prompt_type == "image" and workflow_mode == "text_to_image":
        # Bible/reference-library material may influence the compiled text, but
        # this reviewed v1 graph has no pixel-conditioning input. Never expose
        # those assets as if they were connected to ComfyUI.
        reference_images = []
        effective_mode = "TEXT_ONLY"
    else:
        reference_images = prompt.structured_payload.get("reference_assets", [])
        effective_mode = prompt.h3_mode
    forbidden_semantic_overrides = {
        "H3_MODE", "REQUESTED_H3_MODE", "EFFECTIVE_CONDITIONING_MODE",
        "REFERENCE_IMAGES", "REFERENCE_BINDINGS", "PROMPT_TEMPLATE_KEY",
        "WORKFLOW_TEMPLATE_SLUG", "WORKFLOW_MODE", "SOURCE_WORKFLOW",
    }
    supplied_forbidden = forbidden_semantic_overrides & set(overrides)
    if supplied_forbidden:
        raise WorkflowBuildError(
            "工作流模式与参考素材槽位由已保存分镜决定，禁止覆盖: "
            + ", ".join(sorted(supplied_forbidden))
        )
    if is_h3 and "FPS" in overrides and int(overrides["FPS"]) != 24:
        raise WorkflowBuildError("MiniMax H3 固定使用 24 fps")
    fps = 24 if is_h3 else int(overrides.get("FPS", project.fps))
    duration_ms = int(overrides.get("DURATION_MS", prompt.total_duration_ms))
    is_h3_video = is_h3 and prompt.prompt_type == "video"
    if is_h3_video and not H3_MIN_REQUEST_DURATION_MS <= duration_ms <= H3_MAX_REQUEST_DURATION_MS:
        raise WorkflowBuildError(
            "MiniMax H3 单次工作流请求时长必须在 5000–15000ms；"
            "17k+5 帧对齐后的有效范围约为 5167–15083ms。"
        )
    resolution_profile = str(overrides.get("RESOLUTION_PROFILE", "project"))
    if resolution_profile not in H3_RESOLUTION_PROFILES:
        raise WorkflowBuildError(f"不支持的 RESOLUTION_PROFILE: {resolution_profile}")
    try:
        resolution_multiple = validate_resolution_multiple(int(overrides.get(
            "RESOLUTION_MULTIPLE", H3_RESOLUTION_MULTIPLE_DEFAULT,
        )))
    except (TypeError, ValueError, ResolutionProfileError) as exc:
        raise WorkflowBuildError(str(exc)) from exc
    try:
        resolved_width, resolved_height = resolve_production_resolution(
            profile=resolution_profile,
            aspect_ratio=project.aspect_ratio,
            project_width=project.width,
            project_height=project.height,
            multiple=resolution_multiple,
        )
    except ResolutionProfileError as exc:
        raise WorkflowBuildError(str(exc)) from exc
    width = int(overrides.get("WIDTH", resolved_width))
    height = int(overrides.get("HEIGHT", resolved_height))
    if width <= 0 or height <= 0:
        raise WorkflowBuildError("WIDTH 和 HEIGHT 必须为正整数")
    if is_h3 and (
        width % resolution_multiple
        or height % resolution_multiple
        or max(width, height) > 2048
    ):
        raise WorkflowBuildError(
            "当前 MiniMax H3 profile 要求宽高符合 RESOLUTION_MULTIPLE，且最大边不超过 2048；"
            "4K 需要尚未配置的独立放大流程"
        )
    frames = (
        h3_aligned_frames(duration_ms) if is_h3_video
        else max(1, round(duration_ms * fps / 1000)) if prompt.prompt_type == "video"
        else 1
    )
    effective_duration_ms = round(frames * 1000 / fps) if prompt.prompt_type == "video" else 0
    if is_h3_video and not H3_MIN_ALIGNED_FRAMES <= frames <= H3_MAX_ALIGNED_FRAMES:
        raise WorkflowBuildError("MiniMax H3 帧数对齐结果超出 124–362 帧安全范围")
    required_dimension_multiple = schema.get("required_dimension_multiple")
    if required_dimension_multiple is not None:
        try:
            required_dimension_multiple = int(required_dimension_multiple)
        except (TypeError, ValueError) as exc:
            raise WorkflowBuildError("required_dimension_multiple 必须是正整数") from exc
        if required_dimension_multiple <= 0:
            raise WorkflowBuildError("required_dimension_multiple 必须是正整数")
        if width % required_dimension_multiple or height % required_dimension_multiple:
            raise WorkflowBuildError(
                f"当前图片工作流要求宽高均可被 {required_dimension_multiple} 整除；"
                "请选择兼容的分辨率倍数"
            )
    variables = {
        "PROMPT": prompt.prompt_text,
        "NEGATIVE_PROMPT": prompt.negative_prompt,
        "SEED": int(overrides.get("SEED", project.continuity_seed)),
        "WIDTH": width,
        "HEIGHT": height,
        "RESOLUTION_PROFILE": resolution_profile,
        "RESOLUTION_MULTIPLE": resolution_multiple,
        "FPS": fps,
        "DURATION_MS": effective_duration_ms,
        "REQUESTED_DURATION_MS": duration_ms,
        "EFFECTIVE_DURATION_MS": effective_duration_ms,
        "FRAMES": frames,
        "MODEL": model_name,
        "H3_MODE": effective_mode,
        "REQUESTED_H3_MODE": prompt.h3_mode,
        "EFFECTIVE_CONDITIONING_MODE": effective_mode,
        "AUDIO_ENABLED": bool(overrides.get("AUDIO_ENABLED", prompt.include_audio)),
        "REFERENCE_IMAGES": reference_images,
        "REFERENCE_BINDINGS": reference_images,
        "REFERENCE_IMAGES_JSON": json.dumps(reference_images, ensure_ascii=False),
        "PROMPT_TEMPLATE_KEY": prompt.structured_payload.get("prompt_template_key", ""),
        "WORKFLOW_TEMPLATE_SLUG": template.slug,
        "WORKFLOW_MODE": workflow_mode,
        "SOURCE_WORKFLOW": schema.get("source_workflow", ""),
        "OUTPUT_PREFIX": _safe_output_prefix(
            str(overrides.get("OUTPUT_PREFIX", f"autoprompt/{project.id}/{prompt.id}"))
        ),
        "H3_SAFE_DURATION": not is_h3 or 5_000 <= duration_ms <= 15_000,
        # Backward-compatible fallback for older reviewed H3 definitions. New
        # system templates declare and validate this through user_parameters.
        "STEPS": 20,
    }
    variables.update(user_parameters)
    allowed_overrides = set(schema.get("allowed_overrides", []))
    # Runtime-derived variables are substitution values, not an implicit
    # public override surface. A caller-controlled key must be explicitly
    # advertised either as a typed user parameter or an allowed override.
    unknown = set(overrides) - allowed_overrides - set(user_parameters)
    if unknown:
        raise WorkflowBuildError(f"模板不允许覆盖变量: {', '.join(sorted(unknown))}")
    variables.update({
        key: value for key, value in overrides.items()
        if key in allowed_overrides and key not in user_parameters
    })
    # Preserve the validated canonical values even when the template advertises
    # the setting as an allowed override.
    variables["RESOLUTION_PROFILE"] = resolution_profile
    variables["RESOLUTION_MULTIPLE"] = resolution_multiple
    workflow = _replace(template.definition, variables)
    workflow = _adapt_native_h3(workflow, variables, effective_mode)
    validate_comfy_graph(workflow)
    if not isinstance(workflow, dict) or not workflow:
        raise WorkflowBuildError("工作流模板 definition 必须是非空 JSON 对象")
    encoded = json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return workflow, variables, hashlib.sha256(encoded.encode("utf-8")).hexdigest()
