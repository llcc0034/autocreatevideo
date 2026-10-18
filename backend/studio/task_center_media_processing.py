"""Describe verified per-use staging evidence, without executing media processing."""
from math import isfinite

from studio.services.media_processing_profiles import REFERENCE_PROFILE_KEY
from studio.services.reference_media import REFERENCE_PREPROCESSING_DESCRIPTIONS


_FIELDS = {
    "minimum_duration_ms": ("允许的最短源时长", "ms", "integer"),
    "maximum_duration_ms": ("允许的最长源时长", "ms", "integer"),
    "target_fps": ("声明目标帧率", "fps", "number"),
    "requires_source_fps": ("要求可确定源帧率", "", "boolean"),
    "source_duration_ms": ("源时长", "ms", "integer"),
    "effective_duration_ms": ("适配器记录的有效时长", "ms", "integer"),
    "source_fps": ("源帧率", "fps", "number"),
    "effective_fps": ("适配器记录的有效帧率", "fps", "number"),
    "effective_frames": ("适配器记录的有效帧数", "帧", "integer"),
    "verified_frame_count": ("处理后探测帧数", "帧", "integer"),
    "verified_video_duration_ms": ("处理后探测视频时长", "ms", "integer"),
    "verified_audio_duration_ms": ("处理后探测音频时长", "ms", "integer"),
    "cfr_guaranteed": ("记录声明恒定帧率", "", "boolean"),
    "duration_aligned": ("记录声明时长对齐", "", "boolean"),
    "original_asset_preserved": ("记录声明保留源素材", "", "boolean"),
    "source_bytes_submitted": ("记录声明使用原始字节", "", "boolean"),
    "embedded_audio_policy": ("内嵌音频处理", "", "audio_policy"),
}
_COMMON = ("source_duration_ms", "effective_duration_ms", "original_asset_preserved")
_OBSERVED = {
    "cfr_video_v1": (*_COMMON, "source_fps", "effective_fps", "effective_frames", "verified_frame_count",
        "verified_video_duration_ms", "verified_audio_duration_ms", "cfr_guaranteed", "embedded_audio_policy"),
    "passthrough_video_v1": (*_COMMON, "source_fps", "effective_fps", "cfr_guaranteed", "source_bytes_submitted", "embedded_audio_policy"),
    "aligned_audio_v1": (*_COMMON, "effective_fps", "verified_audio_duration_ms", "duration_aligned"),
    "passthrough_audio_v1": (*_COMMON, "source_bytes_submitted"),
}
_AUDIO_POLICIES = {"aligned": "已对齐", "not_staged": "未暂存音轨", "preserved": "原样保留", "absent": "源无音轨"}


def _row(values, key, source):
    label, unit, kind = _FIELDS[key]
    value = values.get(key)
    state = "recorded"
    if key not in values:
        state = "missing"
    elif value is None:
        state = "unavailable"
    elif kind == "audio_policy":
        state = "recorded" if isinstance(value, str) and value in _AUDIO_POLICIES else "invalid"
        value = _AUDIO_POLICIES[value] if state == "recorded" else None
    elif not ((kind == "boolean" and type(value) is bool)
              or (kind == "integer" and type(value) is int and value >= 0)
              or (kind == "number" and ((type(value) is int and value >= 0)
                  or (type(value) is float and isfinite(value) and value >= 0)))):
        state = "invalid"
    return {"key": key, "label": label, "unit": unit, "source": f"{source}.{key}",
        "state": state, "value": value if state == "recorded" else None}


def processing_summary(*, adapter, spec, profile, receipt, metadata, shared_with=None):
    """Caller already checked owner/root/use/target and receipt metadata hashes.

    Rows describe only the original, versioned metadata, never fresh FFprobe
    results. Unknown versions remain available as raw evidence, not guessed.
    """
    result = {"state": "not_applicable", "label": "不执行上传前处理", "description": "仅上下文用途。",
        "configuration_source": "不适用", "shared_with": shared_with, "declared": [], "observed": [],
        "byte_identity": "not_applicable", "message": "不补造媒体处理记录。"}
    if adapter is None:
        return result
    info = REFERENCE_PREPROCESSING_DESCRIPTIONS.get(adapter)
    source_bytes = adapter == "source_bytes/v1"
    result.update(state="declared", label="使用原始字节" if source_bytes else info[0] if info else "未知预处理协议",
        description="未声明平台上传前转换；工作流节点内部处理另行核对。" if source_bytes else info[1] if info else "不猜测处理算法。",
        configuration_source="原执行组合的素材接线" if source_bytes else
            "原执行组合冻结的模型素材模式预处理合同；编码参数来自冻结的 reference.media_preprocessing_profile。",
        byte_identity="unavailable", message="只有处理声明，尚无本次暂存回执；不能把声明值当成实际结果。")
    if spec is not None:
        result["declared"] = [_row(spec["contract"], key, "processing.contract") for key in
            ("minimum_duration_ms", "maximum_duration_ms", "target_fps", "requires_source_fps") if key in spec["contract"]]
    if receipt is None:
        return result
    result["byte_identity"] = "same" if receipt["source_sha256"] == receipt["submitted_sha256"] else "changed"
    if source_bytes and metadata == {}:
        result.update(state="recorded", message="已读取原字节暂存回执；未记录独立转码或媒体探测值，不代表模型收到或生成成功。")
        return result
    role = {"videos": "reference_video", "paired_video_audios": "paired_reference_audio", "reference_audios": "reference_audio"}
    if (info is None or spec is None or profile is None or not isinstance(metadata, dict)
            or type(metadata.get("staging_version")) is not int or metadata["staging_version"] != 2
            or metadata.get("preprocessing_adapter_key") != adapter
            or metadata.get("media_role") != role.get(spec["slot"])
            or metadata.get("media_processing_profile_key") != REFERENCE_PROFILE_KEY
            or metadata.get("media_processing_profile_sha256") != profile["sha256"]):
        result.update(state="unverifiable", message="原回执存在，但处理元数据的版本、适配器、用途或配置摘要无法核对；不按当前算法解读，原证据保留。")
        return result
    result["observed"] = [_row(metadata, key, "processing_metadata") for key in _OBSERVED[adapter]]
    states = {row["state"] for row in result["observed"]}
    result.update(state="unverifiable" if "invalid" in states else "partial" if "missing" in states else "recorded",
        message="只读取原处理元数据；有效值与处理后探测值分列，缺值不回填，未重新探测文件，不证明工作流节点执行、模型送达或Smoke通过。")
    return result
