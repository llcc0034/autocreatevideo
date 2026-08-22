from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any


_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")

_MINIMAX_REFERENCE_IMAGE_DEFINITION = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
        "ckpt_name": "${MODEL}",
    }},
    "2": {"class_type": "CLIPTextEncode", "inputs": {
        "text": "${PROMPT}", "clip": ["1", 1],
    }},
    "3": {"class_type": "CLIPTextEncode", "inputs": {
        "text": "${NEGATIVE_PROMPT}", "clip": ["1", 1],
    }},
    "4": {"class_type": "EmptyLatentImage", "inputs": {
        "width": "${WIDTH}", "height": "${HEIGHT}", "batch_size": 1,
    }},
    "5": {"class_type": "KSampler", "inputs": {
        "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
        "latent_image": ["4", 0], "seed": "${SEED}",
        "steps": 30, "cfg": 6.5, "sampler_name": "dpmpp_2m",
        "scheduler": "karras", "denoise": 1.0,
    }},
    "6": {"class_type": "VAEDecode", "inputs": {
        "samples": ["5", 0], "vae": ["1", 2],
    }},
    "7": {"class_type": "SaveImage", "inputs": {
        "images": ["6", 0], "filename_prefix": "${OUTPUT_PREFIX}",
    }},
}


def canonical_workflow_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def canonical_workflow_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_workflow_json(value).encode("utf-8")).hexdigest()


def video_output_contract(definition: Any) -> dict:
    """Declare the single authoritative video artifact emitted by a graph.

    Deferred continuity must never guess from output ordering or filename
    extensions.  A reviewed video graph is therefore executable only when it
    has exactly one SaveVideo node whose stable node id can be frozen into the
    GenerationTask capability snapshot.
    """

    if not isinstance(definition, dict) or not definition:
        raise ValueError("video workflow definition must be a non-empty object")
    save_video_nodes = [
        str(node_id)
        for node_id, node in definition.items()
        if isinstance(node, dict) and node.get("class_type") == "SaveVideo"
    ]
    if len(save_video_nodes) != 1:
        raise ValueError("video workflow must contain exactly one SaveVideo node")
    return {
        "schema_version": 1,
        "primary_video": {
            "node_id": save_video_nodes[0],
            "media_kind": "video",
            "required": True,
        },
    }


_MINIMAX_REFERENCE_IMAGE_CANONICAL_JSON = canonical_workflow_json(
    _MINIMAX_REFERENCE_IMAGE_DEFINITION,
)
MINIMAX_REFERENCE_IMAGE_DEFINITION_SHA256 = canonical_workflow_sha256(
    _MINIMAX_REFERENCE_IMAGE_DEFINITION,
)


def approved_minimax_reference_image_definition() -> dict:
    """Return a copy of the one reviewed text-only SDXL API graph."""

    return deepcopy(_MINIMAX_REFERENCE_IMAGE_DEFINITION)


def is_approved_minimax_reference_image_definition(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        canonical = canonical_workflow_json(value)
    except (TypeError, ValueError):
        return False
    return bool(
        canonical == _MINIMAX_REFERENCE_IMAGE_CANONICAL_JSON
        and hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == MINIMAX_REFERENCE_IMAGE_DEFINITION_SHA256
    )


def compiled_minimax_reference_image_definition(values: dict) -> dict:
    """Inject reviewed placeholders while preserving every fixed node input/link."""

    required = {
        "MODEL", "PROMPT", "NEGATIVE_PROMPT", "WIDTH", "HEIGHT", "SEED",
        "OUTPUT_PREFIX",
    }
    missing = sorted(required - set(values))
    if missing:
        raise ValueError("missing reviewed SDXL workflow values: " + ", ".join(missing))

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str):
            match = _PLACEHOLDER_RE.fullmatch(value)
            if match:
                return deepcopy(values[match.group(1)])
        return deepcopy(value)

    return replace(_MINIMAX_REFERENCE_IMAGE_DEFINITION)
