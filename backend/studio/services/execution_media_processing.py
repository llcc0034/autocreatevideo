"""Freeze configuration-backed media processing once, then verify without re-resolution."""
from copy import deepcopy
from dataclasses import asdict
import re

from django.core.exceptions import ValidationError

from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_workflow_uses import read_execution_workflow_uses, native_video_audio_uses
from studio.services.json_contracts import json_contract
from studio.services.media_processing_profiles import (
    REFERENCE_PROFILE_KEY, MediaProcessingProfileError,
    resolve_reference_media_profile, verify_media_processing_profile,
)
from studio.services.reference_media import REFERENCE_PREPROCESSING_ADAPTERS


def execution_media_processing_uses(definition, resolved_inputs):
    """Select processing by the frozen mode and real primary uses, never filenames."""
    uses = read_execution_workflow_uses(definition, resolved_inputs)
    mode = definition.value["resources"]["conditioning_mode"]
    topology = mode.get("dynamic_reference_topology") if mode is not None else None
    result = {}
    native_audio = native_video_audio_uses(uses)
    for key in uses.primary_targets:
        item = uses.items[key]
        if key in native_audio:
            continue
        if item["origin"].get("kind") == "video_audio" and mode is None:
            raise ValidationError("视频原音提取需要冻结的配套音频预处理合同，不能把视频文件传入音频槽")
        if item["kind"] == "image" or mode is None:
            continue  # Fixed graph without a conditioning mode consumes source bytes.
        contracts = topology.get("preprocessing") if isinstance(topology, dict) else None
        slot = "videos" if item["kind"] == "video" else (
            "paired_video_audios" if item["paired_to"] is not None else "reference_audios")
        contract = contracts.get(slot) if isinstance(contracts, dict) else None
        if not isinstance(contract, dict) or contract.get("adapter_key") not in REFERENCE_PREPROCESSING_ADAPTERS[slot]:
            raise ValidationError("视频/音频输入缺少冻结模式中已注册的预处理合同，不能原样绕过")
        parent = item["paired_to"]
        if parent is not None and (parent not in uses.primary_targets or uses.items[parent]["kind"] != "video"):
            raise ValidationError("配套音轨必须引用本次明确的视频使用")
        result[key] = {"slot": slot, "contract": deepcopy(contract), "paired_to": parent}
        embedded = [uses.items[audio]["source_sha256"] for audio in uses.items
            if audio in native_audio and uses.items[audio]["paired_to"] == key]
        if embedded:
            result[key]["embedded_audio_sources"] = embedded
    return result


def freeze_execution_media_processing_profile(definition, resolved_inputs, *, project):
    if not execution_media_processing_uses(definition, resolved_inputs):
        return None
    try:
        frozen = asdict(resolve_reference_media_profile(project=project))
    except MediaProcessingProfileError as exc:
        raise ValidationError(str(exc)) from exc
    frozen["revision_ids"] = list(frozen["revision_ids"])
    json_contract(frozen, max_bytes=execution_configuration_max_bytes())
    return frozen


def read_execution_media_processing_profile(definition, resolved_inputs):
    needed = bool(execution_media_processing_uses(definition, resolved_inputs))
    frozen = resolved_inputs.value.get("media_processing_profile")
    if not needed:
        if frozen is not None:
            raise ValidationError("没有预处理输入，不能混入未消费的媒体处理配置")
        return None
    if (not isinstance(frozen, dict) or set(frozen) != {"key", "value", "sha256", "configuration_sha256", "revision_ids"}
            or frozen["key"] != REFERENCE_PROFILE_KEY
            or any(not isinstance(frozen[name], str) or re.fullmatch(r"[a-f0-9]{64}", frozen[name]) is None
                for name in ("sha256", "configuration_sha256"))
            or not isinstance(frozen["revision_ids"], list)
            or any(not isinstance(key, str) or not key for key in frozen["revision_ids"])):
        raise ValidationError("本次输入缺少完整的冻结媒体处理配置，不能读取最新配置补齐")
    try:
        verify_media_processing_profile(REFERENCE_PROFILE_KEY, frozen["value"], sha256=frozen["sha256"])
    except MediaProcessingProfileError as exc:
        raise ValidationError(str(exc)) from exc
    return deepcopy(frozen)
