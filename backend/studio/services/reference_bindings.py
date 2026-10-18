"""Shared media authorization and selected execution contract validation.

Execution references are projected from resolved per-call inputs by
``execution_reference_materials``. Shot content persistence does not select
materials or infer workflow slots from Bible or legacy asset pools.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from uuid import UUID

from django.conf import settings

from studio.models import Asset
from studio.services.asset_visibility import is_workflow_smoke_fixture_asset
from studio.services.media_capabilities import SAFE_CONDITIONING_IDENTIFIER_PATTERN, CONDITIONING_INPUT_SLOTS, CONDITIONING_INPUT_KINDS
from studio.services.provenance import sha256_file
from studio.services.security_boundaries import boundary_int


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm"})
AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".m4a", ".ogg"})

_SAFE_IDENTIFIER = re.compile(SAFE_CONDITIONING_IDENTIFIER_PATTERN)


def max_structural_media_bindings() -> int:
    return boundary_int("security.reference.max_bindings")


class ReferenceBindingError(ValueError):
    pass


def media_reference_label(kind: str, ordinal: int) -> str:
    """Label grammar of the registered media-reference protocol, not a model."""
    labels = {"image": "Picture", "video": "Video", "audio": "Audio"}
    if kind not in labels or type(ordinal) is not int or ordinal < 1:
        raise ReferenceBindingError("参考标签需要已注册媒体类型和正整数序号")
    return f"<{labels[kind]} {ordinal}>"


def authorized_asset_source(*, owner, asset_id: str) -> tuple[Asset, Path]:
    """Resolve one tenant-authorized immutable local Asset source.

    Workflow smoke, short-drama production and independent asset generation all
    need the same ownership, path and checksum boundary.  Keeping it here avoids
    each worker inventing a subtly different authorization rule.
    """

    try:
        asset = Asset.objects.get(pk=asset_id, is_active=True)
    except (Asset.DoesNotExist, ValueError, TypeError) as exc:
        raise ReferenceBindingError("参考素材不存在、已停用或当前用户无权使用") from exc
    owner_id = getattr(owner, "id", None)
    if asset.owner_id not in (None, owner_id):
        raise ReferenceBindingError("参考素材不存在、已停用或当前用户无权使用")
    if is_workflow_smoke_fixture_asset(asset):
        raise ReferenceBindingError("工作流 Smoke 专用素材不能用于正式生成任务")

    asset_root = Path(settings.ASSET_LIBRARY_ROOT).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    if asset.owner_id is None:
        if not asset.local_path or asset.file:
            raise ReferenceBindingError("全局参考素材必须来自只读素材库")
        path = Path(asset.local_path).resolve()
        allowed_root = asset_root
    elif asset.file:
        try:
            path = Path(asset.file.path).resolve()
        except (NotImplementedError, ValueError, OSError) as exc:
            raise ReferenceBindingError("用户参考素材不是可读取的本地上传") from exc
        allowed_root = (media_root / "assets" / str(owner_id)).resolve()
    elif getattr(owner, "is_staff", False) and asset.local_path:
        try:
            path = Path(asset.local_path).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ReferenceBindingError("管理员素材不是可读取的受控本地文件") from exc
        if path.is_relative_to(asset_root):
            allowed_root = asset_root
        elif path.is_relative_to(media_root):
            allowed_root = media_root
        else:
            raise ReferenceBindingError("管理员素材路径越出受控素材目录")
    else:
        raise ReferenceBindingError("用户参考素材必须由上传接口写入租户目录")
    try:
        allowed = path.is_relative_to(allowed_root)
    except (OSError, ValueError):
        allowed = False
    if not allowed:
        raise ReferenceBindingError("参考素材路径越出租户授权目录")
    if not path.is_file():
        raise ReferenceBindingError("参考素材文件不存在")
    recorded_checksum = str(asset.checksum or "").casefold()
    actual_checksum = sha256_file(path)
    if (
        len(recorded_checksum) != 64
        or any(character not in "0123456789abcdef" for character in recorded_checksum)
        or actual_checksum != recorded_checksum
    ):
        raise ReferenceBindingError("参考素材内容哈希与已确认记录不一致")
    return asset, path


def _uuid_string(value, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ReferenceBindingError(f"{field} 必须是有效 UUID") from exc


def normalize_reference_identifier(value, field: str = "purpose") -> str:
    normalized = str(value or "").strip()
    if _SAFE_IDENTIFIER.fullmatch(normalized) is None:
        raise ReferenceBindingError(
            f"{field} 必须是以字母开头、最长 64 字符的安全 identifier"
        )
    return normalized


def _normalize_videos(value) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ReferenceBindingError("reference_bindings.videos 必须是数组")
    result = []
    allowed = {
        "asset_id", "use_embedded_audio", "audio_asset_id", "purpose", "audio_purpose",
    }
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict)
            or not item.get("asset_id")
            or "purpose" not in item
            or "use_embedded_audio" not in item
            or set(item) - allowed
        ):
            raise ReferenceBindingError(
                f"videos[{index}] 只能包含 asset_id、use_embedded_audio、audio_asset_id、"
                "purpose、audio_purpose，且 purpose/use_embedded_audio 必须显式提交"
            )
        use_embedded = item.get("use_embedded_audio")
        if not isinstance(use_embedded, bool):
            raise ReferenceBindingError(f"videos[{index}].use_embedded_audio 必须是布尔值")
        purpose = normalize_reference_identifier(item.get("purpose"), f"videos[{index}].purpose")
        normalized = {
            "asset_id": _uuid_string(item["asset_id"], f"videos[{index}].asset_id"),
            "purpose": purpose,
            "use_embedded_audio": use_embedded,
        }
        if item.get("audio_asset_id") not in (None, ""):
            normalized["audio_asset_id"] = _uuid_string(
                item["audio_asset_id"], f"videos[{index}].audio_asset_id",
            )
        has_paired_audio = use_embedded or bool(normalized.get("audio_asset_id"))
        if has_paired_audio:
            if "audio_purpose" not in item:
                raise ReferenceBindingError(
                    f"videos[{index}] 启用配对音频时必须显式提交 audio_purpose"
                )
            audio_purpose = normalize_reference_identifier(
                item.get("audio_purpose"), f"videos[{index}].audio_purpose",
            )
            normalized["audio_purpose"] = audio_purpose
        elif "audio_purpose" in item:
            raise ReferenceBindingError(
                f"videos[{index}].audio_purpose 仅能在启用视频原音或绑定 audio_asset_id 时设置"
            )
        result.append(normalized)
    video_ids = [item["asset_id"] for item in result]
    if len(video_ids) != len(set(video_ids)):
        raise ReferenceBindingError("videos 不能重复绑定同一参考视频")
    if len(result) > max_structural_media_bindings():
        raise ReferenceBindingError(
            f"videos 结构上限为 {max_structural_media_bindings()}；"
            "实际工作流上限由冻结合同决定"
        )
    return result


def _normalize_audios(value) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ReferenceBindingError("reference_bindings.audios 必须是数组")
    result = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict)
            or not item.get("asset_id")
            or "purpose" not in item
            or set(item) - {"asset_id", "purpose"}
        ):
            raise ReferenceBindingError(
                f"audios[{index}] 只能包含 asset_id 和显式 purpose"
            )
        purpose = normalize_reference_identifier(item.get("purpose"), f"audios[{index}].purpose")
        result.append({
            "asset_id": _uuid_string(item["asset_id"], f"audios[{index}].asset_id"),
            "purpose": purpose,
        })
    asset_ids = [item["asset_id"] for item in result]
    if len(asset_ids) != len(set(asset_ids)):
        raise ReferenceBindingError("audios 不能重复绑定同一参考音频")
    if len(result) > max_structural_media_bindings():
        raise ReferenceBindingError(
            f"audios 结构上限为 {max_structural_media_bindings()}；"
            "实际工作流上限由冻结合同决定"
        )
    return result


def _validate_count(
    count: int,
    contract: dict,
    *,
    label: str,
) -> None:
    if not isinstance(contract, dict):
        raise ReferenceBindingError(f"{label} 冻结基数合同无效")
    minimum, maximum = contract.get("minimum_items"), contract.get("maximum_items")
    if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum or type(count) is not int or count < 0:
        raise ReferenceBindingError(f"{label} 冻结基数合同无效")
    if not minimum <= count <= maximum:
        raise ReferenceBindingError(
            f"{label} 数量 {count} 不满足冻结范围 {minimum}–{maximum}"
        )


def _validate_purpose_group(
    items: list[dict],
    group: dict,
    *,
    label: str,
    purpose_field: str = "purpose",
) -> None:
    _validate_count(len(items), group, label=label)
    definitions = group.get("purposes")
    if not isinstance(definitions, list):
        raise ReferenceBindingError(f"{label}.purposes 冻结合同无效")
    by_key = {
        str(item.get("key") or ""): item
        for item in definitions
        if isinstance(item, dict)
    }
    if len(by_key) != len(definitions):
        raise ReferenceBindingError(f"{label}.purposes 冻结合同重复或无效")
    counts: dict[str, int] = {key: 0 for key in by_key}
    for index, item in enumerate(items):
        purpose = str(item.get(purpose_field) or "")
        if purpose not in by_key:
            raise ReferenceBindingError(
                f"{label}[{index}].{purpose_field}={purpose or '(empty)'} 未在冻结合同声明"
            )
        counts[purpose] += 1
    for key, definition in by_key.items():
        _validate_count(counts[key], definition, label=f"{label}.purpose[{key}]")


def validate_reference_input_counts(counts, mode_contract):
    """One model count validator for real project bindings and logical inputs.

    Counts describe actual execution uses, not unique Asset IDs or prompt-only
    context. Slot-role assignment belongs to the frozen input adapter.
    """
    if (not isinstance(counts, dict) or set(counts) != set(CONDITIONING_INPUT_SLOTS)
            or any(type(value) is not int or value < 0 for value in counts.values())
            or not isinstance(mode_contract, dict) or not isinstance(mode_contract.get("inputs"), dict)
            or mode_contract.get("input_kind") not in CONDITIONING_INPUT_KINDS):
        raise ReferenceBindingError("模型输入计数与冻结模式必须完整且类型正确")
    if counts["total_images"] != counts["first_frame"] + counts["last_frame"] + counts["entity_images"]:
        raise ReferenceBindingError("total_images 与各图片槽位计数不一致")
    if counts["paired_video_audios"] > counts["reference_videos"]:
        raise ReferenceBindingError("配套音轨数量不能超过视频使用数量")
    for key in CONDITIONING_INPUT_SLOTS:
        _validate_count(counts[key], mode_contract["inputs"].get(key), label=key)
    kind = mode_contract["input_kind"]
    if kind == "text_only" and any(counts.values()):
        raise ReferenceBindingError("text_only 条件模式不接受任何参考素材")
    if kind == "frame_slots" and any(counts[key] for key in ("entity_images", "reference_videos", "paired_video_audios", "reference_audios")):
        raise ReferenceBindingError("frame_slots 条件模式只接受首帧/尾帧槽位")
    if kind == "classified_references" and (counts["first_frame"] or counts["last_frame"]):
        raise ReferenceBindingError("classified_references 条件模式不接受首帧/尾帧执行槽位")


def validate_reference_bindings_against_mode_contract(
    bindings: object,
    mode_contract: object,
) -> None:
    """Strictly validate normalized bindings against one frozen mode contract."""

    if not isinstance(bindings, dict) or not isinstance(mode_contract, dict):
        raise ReferenceBindingError("reference_bindings 或 mode contract 缺失")
    inputs = mode_contract.get("inputs")
    if not isinstance(inputs, dict):
        raise ReferenceBindingError("mode.inputs 冻结合同缺失")
    input_kind = str(mode_contract.get("input_kind") or "")
    first_count = int(bool(bindings.get("first_frame_asset_id")))
    last_count = int(bool(bindings.get("last_frame_asset_id")))
    entity_count = (
        int(bool(bindings.get("first_frame_reference_asset_id")))
        + len(bindings.get("characters") or [])
        + int(bool(bindings.get("scene")))
        + len(bindings.get("props") or [])
    )
    total_images = first_count + last_count + entity_count
    videos = list(bindings.get("videos") or [])
    audios = list(bindings.get("audios") or [])
    paired_videos = [
        item for item in videos
        if item.get("use_embedded_audio") or item.get("audio_asset_id")
    ]
    validate_reference_input_counts({"total_images": total_images, "first_frame": first_count,
        "last_frame": last_count, "entity_images": entity_count, "reference_videos": len(videos),
        "paired_video_audios": len(paired_videos), "reference_audios": len(audios)}, mode_contract)

    if input_kind == "text_only" and bindings:
        raise ReferenceBindingError("text_only 条件模式不接受任何 reference_bindings")
    validate_reference_media_topology(videos, audios, mode_contract)


def validate_reference_media_topology(videos, audios, mode_contract):
    """Project Asset-backed binding facts into the shared topology validator."""
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) for rows in (videos, audios)):
        raise ReferenceBindingError("动态素材必须是已解析的明确事实")
    return validate_reference_topology_facts([
        {"purpose": row.get("purpose"), "embedded_audio": bool(row.get("use_embedded_audio")),
         "external_audio": bool(row.get("audio_asset_id")), "audio_purpose": row.get("audio_purpose")}
        for row in videos
    ], [{"purpose": row.get("purpose")} for row in audios], mode_contract)


def validate_reference_topology_facts(videos, audios, mode_contract):
    """Shared video/audio purposes, exclusions and pairing, without Asset IDs.

    Callers still validate model image/frame counts and logical slot counts.
    This extraction lets logical-input preparations use the same frozen rules
    without inventing character/scene records to satisfy a project adapter.
    """
    if not isinstance(mode_contract, dict) or any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) for rows in (videos, audios)):
        raise ReferenceBindingError("动态素材与模式必须是已解析的明确事实")
    if any(set(row) != {"purpose", "embedded_audio", "external_audio", "audio_purpose"}
           or type(row["embedded_audio"]) is not bool or type(row["external_audio"]) is not bool for row in videos):
        raise ReferenceBindingError("视频必须明确提供用途及内嵌/外配音频事实")
    if any(set(row) != {"purpose"} for row in audios):
        raise ReferenceBindingError("独立音频必须明确提供用途事实")
    paired_videos = [row for row in videos if row["embedded_audio"] or row["external_audio"]]
    topology = mode_contract.get("dynamic_reference_topology")
    if not videos and not audios and not paired_videos:
        if topology is None:
            return
    if not isinstance(topology, dict):
        raise ReferenceBindingError("动态参考绑定存在，但冻结 mode 缺少 dynamic_reference_topology")
    video_group = topology.get("videos")
    audio_group = topology.get("audios")
    if not isinstance(video_group, dict) or not isinstance(audio_group, dict):
        raise ReferenceBindingError("dynamic_reference_topology 媒体组合同缺失")
    _validate_purpose_group(videos, video_group, label="videos")
    _validate_purpose_group(audios, audio_group, label="audios")

    for rule in topology.get("mutual_exclusion_rules") or []:
        if not isinstance(rule, dict):
            raise ReferenceBindingError("mutual_exclusion_rules 冻结合同无效")
        media_kind = str(rule.get("media_kind") or "")
        source = videos if media_kind == "video" else audios if media_kind == "audio" else None
        if source is None:
            raise ReferenceBindingError("mutual_exclusion_rules.media_kind 冻结合同无效")
        governed = set(rule.get("purpose_keys") or [])
        used = {str(item.get("purpose") or "") for item in source}.intersection(governed)
        maximum_distinct = rule.get("maximum_distinct_purposes")
        if isinstance(maximum_distinct, bool) or not isinstance(maximum_distinct, int):
            raise ReferenceBindingError("mutual_exclusion_rules 最大用途数无效")
        if len(used) > maximum_distinct:
            raise ReferenceBindingError(
                f"互斥规则 {rule.get('key') or '(unknown)'} 最多允许 {maximum_distinct} 种用途"
            )

    pairing_rules = topology.get("pairing_rules")
    if not isinstance(pairing_rules, list):
        raise ReferenceBindingError("pairing_rules 冻结合同无效")
    for index, video in enumerate(videos):
        purpose = str(video.get("purpose") or "")
        matches = [
            rule for rule in pairing_rules
            if isinstance(rule, dict) and purpose in set(rule.get("source_purpose_keys") or [])
        ]
        paired_count = int(video["embedded_audio"]) + int(video["external_audio"])
        if len(matches) > 1:
            raise ReferenceBindingError(f"videos[{index}] 命中多条冻结配对规则")
        if not matches:
            if paired_count:
                raise ReferenceBindingError(f"videos[{index}] 的用途未声明音频配对规则")
            continue
        rule = matches[0]
        minimum = rule.get("minimum_targets_per_source")
        maximum = rule.get("maximum_targets_per_source")
        if (
            isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or not minimum <= paired_count <= maximum
        ):
            raise ReferenceBindingError(
                f"videos[{index}] 的配对音频数量不满足规则 {rule.get('key') or '(unknown)'}"
            )
        if video["embedded_audio"] and not rule.get("allow_embedded_source"):
            raise ReferenceBindingError(f"videos[{index}] 的用途不允许使用内嵌音轨")
        if video["external_audio"] and not rule.get("allow_external_target"):
            raise ReferenceBindingError(f"videos[{index}] 的用途不允许外配音频")
        if (
            video["embedded_audio"]
            and video["external_audio"]
            and rule.get("source_and_external_mutually_exclusive")
        ):
            raise ReferenceBindingError(f"videos[{index}] 不能同时使用内嵌与外配音频")
        if paired_count and video.get("audio_purpose") not in set(
            rule.get("target_purpose_keys") or []
        ):
            raise ReferenceBindingError(
                f"videos[{index}].audio_purpose 未被冻结配对规则允许"
            )


def validate_dynamic_reference_bindings_against_mode_contract(
    bindings: object,
    mode_contract: object,
) -> None:
    """Validate a pre-production dynamic-media intent.

    Bible/frame inputs may be generated later in an unattended run, so this
    projection relaxes only their minimums.  Dynamic media cardinality,
    purposes, exclusions and pairing remain exact.
    """

    if not isinstance(mode_contract, dict):
        raise ReferenceBindingError("mode contract 缺失")
    projected = deepcopy(mode_contract)
    inputs = projected.get("inputs")
    if not isinstance(inputs, dict):
        raise ReferenceBindingError("mode.inputs 冻结合同缺失")
    for key in ("total_images", "first_frame", "last_frame", "entity_images"):
        slot = inputs.get(key)
        if not isinstance(slot, dict):
            raise ReferenceBindingError(f"mode.inputs.{key} 冻结合同缺失")
        slot["minimum_items"] = 0
    validate_reference_bindings_against_mode_contract(bindings, projected)
