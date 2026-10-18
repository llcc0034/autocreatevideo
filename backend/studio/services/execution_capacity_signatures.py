"""Observed request dimensions, separate from Smoke and capacity coverage.

No provider/file I/O, token heuristics, range inference, or model-name switches.
An incomplete description has no matchable signature. Actual observation
persistence and matching consume this contract; description alone is no pass.
"""
from copy import deepcopy
import math

from django.core.exceptions import ValidationError

from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_parameters import execution_seed_parameter_keys
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract


def capacity_runtime_facts(version, snapshot):
    """Narrow already-verified host facts; omit volatile probe IDs/free space."""
    if version.deployment.runtime_host_id is None:
        kind = "unknown" if version.deployment.provider.driver_type in {"comfyui", "local_qwen"} else "provider_managed"
        return {"kind": kind, "host_id": None, "fingerprint": None}
    if (snapshot is None or snapshot.probe_status != "succeeded"
            or snapshot.details.get("probe_scope") != "local_process"
            or not snapshot.memory_total_bytes or not snapshot.logical_cpu_count):
        return {"kind": "unknown", "host_id": str(version.deployment.runtime_host_id), "fingerprint": None}
    gpus = snapshot.details.get("gpus")
    required = {"index", "name", "vram_total_bytes", "driver_version", "compute_capability"}
    if (not isinstance(gpus, list) or len(gpus) != snapshot.gpu_count
            or any(not isinstance(row, dict) or not required <= row.keys() for row in gpus)):
        return {"kind": "unknown", "host_id": str(version.deployment.runtime_host_id), "fingerprint": None}
    hardware = {"cpu_architecture": snapshot.cpu_architecture, "cpu_name": snapshot.cpu_name,
        "logical_cpu_count": snapshot.logical_cpu_count, "memory_total_bytes": snapshot.memory_total_bytes,
        "gpus": [{key: row[key] for key in sorted(required)} for row in gpus], "comfyui_version": snapshot.comfyui_version}
    return {"kind": "observed_host", "host_id": str(version.deployment.runtime_host_id),
        "fingerprint": json_contract(hardware, max_bytes=execution_configuration_max_bytes()).sha256}


def describe_execution_capacity(definition, transport, *, runtime):
    """Read only declared scalar roles and decoded source-media dimensions.

Tokenization and effective video/audio preprocessing are not silently
approximated. Their missing facts stay explicit until a reviewed measurement
adapter supplies them; character count/usage from another call are not tokens.
"""
    for contract, maximum in ((definition, execution_configuration_max_bytes()), (transport, execution_request_max_bytes())):
        if not isinstance(contract, JsonContract):
            raise ValidationError("规格观测必须读取核对后的执行定义及传输合同")
        verify_json_contract(contract.value, schema_version=contract.schema_version,
            size_bytes=contract.size_bytes, sha256=contract.sha256, max_bytes=maximum)
    request, frozen = transport.value, definition.value
    if (request.get("definition_sha256") != definition.sha256
            or request.get("execution_instance_version_id") != frozen.get("version_id")
            or request.get("target_fingerprint") != frozen.get("target_fingerprint")):
        raise ValidationError("规格观测与本次执行身份不一致")
    if (not isinstance(runtime, dict) or set(runtime) != {"kind", "host_id", "fingerprint"}
            or runtime["kind"] not in {"unknown", "observed_host", "provider_managed"}):
        raise ValidationError("运行环境规格缺少明确来源")
    interface = frozen["interface"]
    workflow = request["driver_type"] == "comfyui"
    body = request["payload"]
    compiled = body["compiled_workflow"] if workflow else None
    output = compiled["output_contract"]["primary"]["kind"] if workflow else request["output"]["kind"]
    values = compiled["resolved_semantics"] if workflow else request["parameters"]["values"]
    rules = {**interface.get("template_constants", {}), **interface["parameters"]}
    # These seed keys belong to the existing reviewed Comfy/chat/REST
    # protocols. Other seed fields are excluded only by their explicit policy.
    seed_keys = execution_seed_parameter_keys(rules, workflow=workflow) | ({"sampling.seed"} if workflow else {"seed"})
    selected, missing = {}, []
    for key in sorted(set(rules) | set(interface.get("static_semantics", {}))):
        if key in seed_keys:
            continue
        if key not in values:
            if key in interface.get("static_semantics", {}):
                value = interface["static_semantics"][key]
            else:
                continue  # Absent optional input is not an invented default.
        else:
            value = values[key]
        if isinstance(value, (bool, int, float)) and (not isinstance(value, float) or math.isfinite(value)):
            selected[key] = value
        elif isinstance(value, str) and value in rules.get(key, {}).get("rule", {}).get("enum", []):
            selected[key] = value
        else:
            missing.append(f"parameters.{key}.measured_size")
    roles = ({"width": "image.width", "height": "image.height", "frames": "video.frames", "fps": "video.fps",
        "duration_ms": "video.duration_ms", "steps": "sampling.steps", "batch": "generation.batch_size"}
        if workflow else interface.get("media_semantic_keys", {}))
    output_dimensions = {role: selected[key] for role, key in roles.items() if key in selected}
    if output in {"image", "video"}:
        missing.extend(f"output.{role}" for role in ("width", "height") if role not in output_dimensions)
    if output in {"video", "audio"} and not {"frames", "duration_ms", "duration_seconds"} & output_dimensions.keys():
        missing.append("output.duration_or_frames")
    if runtime["kind"] == "unknown":
        missing.append("runtime.observed_environment")
    references = []
    media = body.get("media_preparation") if workflow else None
    bindings = media["bindings"] if media is not None else []
    for index, binding in enumerate(bindings):
        observed = binding["media_facts"].get("observed_media")
        dimensions = {}
        if isinstance(observed, dict):
            for key in ("width", "height", "frame_count", "duration_ms", "fps", "has_audio"):
                if key in observed:
                    dimensions[key] = observed[key]
        if not dimensions:
            missing.append(f"references.{index}.decoded_dimensions")
        # Image input staging consumes decoded source bytes. Video/audio may
        # be transformed under the frozen processing profile; do not label
        # original dimensions as effective preprocessed dimensions.
        effective = dimensions if binding["kind"] == "image" or media["reference_media_profile"] is None else None
        if effective is None:
            missing.append(f"references.{index}.effective_dimensions")
        references.append({"kind": binding["kind"], "targets": [row["target_key"] for row in binding["ports"]],
            "source_dimensions": dimensions, "effective_dimensions": effective})
    # The actual assembled chat messages also include field instructions;
    # counting only the visible positive prompt would understate the input.
    if (not workflow and request["driver_type"] != "async_rest") or request["prompt_channels"]["values"]:
        missing.append("text.input_tokens_and_tokenizer")
    dimensions = {"output_kind": output, "output": output_dimensions, "parameters": selected,
        "references": references, "reference_count": len(references), "runtime": deepcopy(runtime), "input_tokens": None}
    signature = json_contract({"schema_version": 1, "signature_key": "capacity_signature_v1",
        "definition_sha256": definition.sha256, "target_fingerprint": request["target_fingerprint"],
        "dimensions": dimensions}, max_bytes=execution_configuration_max_bytes())
    return json_contract({"schema_version": 1, "state": "unknown" if missing else "uncovered",
        "signature_key": "capacity_signature_v1", "signature_sha256": None if missing else signature.sha256,
        "known_dimensions": dimensions, "missing_dimensions": sorted(set(missing)),
        "reason": "capacity_dimensions_incomplete" if missing else "capacity_sample_missing"},
        max_bytes=execution_configuration_max_bytes())
