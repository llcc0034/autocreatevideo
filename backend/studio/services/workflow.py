from __future__ import annotations

import math
import os

# The API prompt is data, but a Comfy workflow is executable configuration.
# Keep the approved surface deliberately small and review additions here before
# exposing them to any user-triggered submission path.
SAFE_COMFY_NODE_TYPES = frozenset({
    "UNETLoader",
    "CheckpointLoaderSimple",
    "CLIPLoader",
    "CLIPTextEncode",
    "ConditioningZeroOut",
    "TextEncodeQwenImageEditPlus",
    "VAELoader",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "MiniMaxH3AudioConditioningT8",
    "MiniMaxH3DualClockSamplerT8",
    "MiniMaxH3AVDecodeT8",
    "MiniMaxLowVRAMAttention",
    "LoraLoaderModelOnly",
    "LoraLoaderBypassModelOnly",
    "RandomNoise",
    "BasicGuider",
    "KSamplerSelect",
    "BasicScheduler",
    "SamplerCustomAdvanced",
    "KSampler",
    "CFGNorm",
    "ModelSamplingAuraFlow",
    "ComfySwitchNode",
    "PrimitiveBoolean",
    "PrimitiveFloat",
    "PrimitiveInt",
    "PrimitiveStringMultiline",
    "ComfyMathExpression",
    "ResolutionSelector",
    "EmptyLatentImage",
    "EmptySD3LatentImage",
    "VAEDecode",
    "VAEDecodeAudio",
    "VAEEncode",
    "FluxKontextImageScale",
    "ImageScale",
    "FluxKontextMultiReferenceLatentMethod",
    "ImageScaleToTotalPixels",
    "LoadImage",
    "LoadVideo",
    "GetVideoComponents",
    "LoadAudio",
    "SaveImage",
    "SaveImageAdvanced",
    "SaveAudioAdvanced",
    "PreviewImage",
    "ImageCompare",
    "CreateVideo",
    "SaveVideo",
    "VHS_VideoCombine",
    "DiffusionModelLoaderKJ",
    "SeedVR2LoadDiTModel",
    "SeedVR2LoadVAEModel",
    "SeedVR2VideoUpscaler",
    "TextGenerate",
    "PreviewAny",
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


def validate_frame_policy(frame_policy: dict) -> None:
    """Check the frozen clock without inventing a requested duration."""
    if not isinstance(frame_policy, dict):
        raise WorkflowBuildError("frame_policy 必须是冻结对象")
    fps = frame_policy.get("fps")
    minimum = frame_policy.get("minimum_frames")
    maximum = frame_policy.get("maximum_frames")
    modulus = frame_policy.get("alignment_modulus")
    remainder = frame_policy.get("alignment_remainder")
    if (
        isinstance(fps, bool)
        or not isinstance(fps, (int, float))
        or not math.isfinite(float(fps))
        or float(fps) <= 0
        or any(isinstance(value, bool) or not isinstance(value, int) for value in (
            minimum, maximum, modulus, remainder,
        ))
        or minimum < 1
        or maximum < minimum
        or modulus < 1
        or not 0 <= remainder < modulus
        or minimum % modulus != remainder
    ):
        raise WorkflowBuildError("frame_policy 的 fps/范围/模数合同无效")


def aligned_frames(duration_ms: int, frame_policy: dict) -> int:
    """Return the nearest legal frame count declared by a frozen contract.

    Always rounding upward can create a large duration drift at short lengths
    so preserve the requested duration by choosing the closest legal count.
    No model identifier or model-specific constants are interpreted here.
    """
    validate_frame_policy(frame_policy)
    fps, minimum, maximum, modulus, remainder = (
        frame_policy[key] for key in ("fps", "minimum_frames", "maximum_frames", "alignment_modulus", "alignment_remainder")
    )
    requested = min(maximum, max(minimum, round(duration_ms * float(fps) / 1000)))
    lower = remainder + ((requested - remainder) // modulus) * modulus
    upper = lower + modulus
    candidates = [
        value for value in (lower, upper)
        if minimum <= value <= maximum and value % modulus == remainder
    ]
    if not candidates:
        raise WorkflowBuildError("frame_policy 没有可用的对齐帧数")
    return min(candidates, key=lambda value: (abs(value - requested), value))




def _validate_comfy_graph(workflow: dict) -> None:
    """Reject graphs outside the reviewed local node surface."""
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
