"""Strict, deterministic image/video/audio reference bindings.

The user-facing ``Shot.reference_bindings`` JSON is deliberately separated
from ``Shot.reference_assets``.  The latter is only an asset picker/library
relationship; it must never decide the semantic role or input slot of an
image, video, or audio asset.

Shot persistence validates only the common binding shape, asset ownership and
safe identifiers.  A selected ProductionRun route must additionally call
:func:`validate_reference_bindings_against_mode_contract`; only its frozen
conditioning contract may decide cardinality, purposes, exclusions or pairing.

Model adapters consume the ordered records returned by
:func:`resolve_group_reference_bindings`.  Model-specific prompt semantics stay
inside their controlled adapters and are not global platform defaults.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from uuid import UUID

from django.conf import settings

from studio.models import Asset, Shot
from studio.services.asset_visibility import is_workflow_smoke_fixture_asset
from studio.services.media_capabilities import SAFE_CONDITIONING_IDENTIFIER_PATTERN
from studio.services.provenance import sha256_file
from studio.services.visual_reference_policy import (
    VisualReferencePolicyError,
    character_requires_visual_reference,
)
from studio.services.security_boundaries import boundary_int


REFERENCE_BINDING_KEYS = frozenset({
    "first_frame_asset_id", "last_frame_asset_id", "first_frame_reference_asset_id",
    "characters", "scene", "props", "videos", "audios",
})
ENTITY_BINDING_KEYS = frozenset({"entity_id", "asset_id"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm"})
AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".m4a", ".ogg"})
_SAFE_IDENTIFIER = re.compile(SAFE_CONDITIONING_IDENTIFIER_PATTERN)


def max_structural_media_bindings() -> int:
    return boundary_int("security.reference.max_bindings")


class ReferenceBindingError(ValueError):
    pass


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


def _normalize_entity_items(value, field: str) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ReferenceBindingError(f"reference_bindings.{field} 必须是数组")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != ENTITY_BINDING_KEYS:
            raise ReferenceBindingError(
                f"reference_bindings.{field}[{index}] 只能包含 entity_id 和 asset_id"
            )
        result.append({
            "entity_id": _uuid_string(item.get("entity_id"), f"{field}[{index}].entity_id"),
            "asset_id": _uuid_string(item.get("asset_id"), f"{field}[{index}].asset_id"),
        })
    entity_ids = [item["entity_id"] for item in result]
    if len(entity_ids) != len(set(entity_ids)):
        raise ReferenceBindingError(f"reference_bindings.{field} 不能重复绑定同一实体")
    return result


def _normalize_scene(value) -> dict | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict) or set(value) != ENTITY_BINDING_KEYS:
        raise ReferenceBindingError("reference_bindings.scene 只能包含 entity_id 和 asset_id")
    return {
        "entity_id": _uuid_string(value.get("entity_id"), "scene.entity_id"),
        "asset_id": _uuid_string(value.get("asset_id"), "scene.asset_id"),
    }


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
    try:
        minimum = int(contract["minimum_items"])
        maximum = int(contract["maximum_items"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceBindingError(f"{label} 冻结基数合同无效") from exc
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
    _validate_count(total_images, inputs.get("total_images") or {}, label="total_images")
    _validate_count(first_count, inputs.get("first_frame") or {}, label="first_frame")
    _validate_count(last_count, inputs.get("last_frame") or {}, label="last_frame")
    _validate_count(entity_count, inputs.get("entity_images") or {}, label="entity_images")
    _validate_count(len(videos), inputs.get("reference_videos") or {}, label="reference_videos")
    _validate_count(
        len(paired_videos),
        inputs.get("paired_video_audios") or {},
        label="paired_video_audios",
    )
    _validate_count(len(audios), inputs.get("reference_audios") or {}, label="reference_audios")

    if input_kind == "text_only" and bindings:
        raise ReferenceBindingError("text_only 条件模式不接受任何 reference_bindings")
    if input_kind == "frame_slots" and any((entity_count, videos, audios)):
        raise ReferenceBindingError("frame_slots 条件模式只接受首帧/尾帧槽位")
    if input_kind == "classified_references" and (first_count or last_count):
        raise ReferenceBindingError("classified_references 条件模式不接受首帧/尾帧执行槽位")

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
        paired_count = int(bool(video.get("use_embedded_audio"))) + int(
            bool(video.get("audio_asset_id"))
        )
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
        if video.get("use_embedded_audio") and not rule.get("allow_embedded_source"):
            raise ReferenceBindingError(f"videos[{index}] 的用途不允许使用内嵌音轨")
        if video.get("audio_asset_id") and not rule.get("allow_external_target"):
            raise ReferenceBindingError(f"videos[{index}] 的用途不允许外配音频")
        if (
            video.get("use_embedded_audio")
            and video.get("audio_asset_id")
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


def _is_image_asset(asset: Asset) -> bool:
    if asset.kind in {Asset.Kind.AUDIO, Asset.Kind.OUTPUT_VIDEO}:
        return False
    if (asset.mime_type or "").casefold().startswith("image/"):
        return True
    candidates = [asset.local_path]
    if asset.file:
        candidates.append(asset.file.name)
    return any(Path(value).suffix.casefold() in IMAGE_EXTENSIONS for value in candidates if value)


def _validate_executable_local_source(asset: Asset, user) -> None:
    """Use the same authorization and checksum gate as every staging worker."""

    resolved, _path = authorized_asset_source(owner=user, asset_id=str(asset.id))
    if resolved.id != asset.id:  # pragma: no cover - defensive identity guard
        raise ReferenceBindingError("参考素材身份校验失败")


def _validate_reference_media_asset(asset: Asset, *, media_type: str) -> None:
    expected_kind = (
        Asset.Kind.REFERENCE_VIDEO if media_type == "video" else Asset.Kind.REFERENCE_AUDIO
    )
    extensions = VIDEO_EXTENSIONS if media_type == "video" else AUDIO_EXTENSIONS
    if asset.kind != expected_kind:
        raise ReferenceBindingError(f"{asset.name} 必须是 {expected_kind} 素材")
    source = asset.file.name if asset.file else asset.local_path
    if Path(source or "").suffix.casefold() not in extensions:
        raise ReferenceBindingError(f"{asset.name} 的 {media_type} 格式不受支持")
    if not (asset.mime_type or "").casefold().startswith(f"{media_type}/"):
        raise ReferenceBindingError(f"{asset.name} 缺少已验证的 {media_type} MIME")


def _available_asset_ids(entity) -> set[str]:
    result = set()
    if entity.anchor_asset_id:
        result.add(str(entity.anchor_asset_id))
    result.update(str(value) for value in entity.reference_assets.values_list("id", flat=True))
    return result


def _asset_record(asset: Asset, *, category: str, entity=None) -> dict:
    return {
        "category": category,
        "entity_id": str(entity.id) if entity is not None else None,
        "entity_name": entity.name if entity is not None else "",
        "asset_id": str(asset.id),
        "name": asset.name,
        "description": asset.description,
        "prompt_hint": asset.prompt_hint,
        "media_metadata": asset.metadata,
        "local_path": asset.file.path if asset.file else asset.local_path,
        "checksum": asset.checksum,
    }


def normalize_shot_reference_bindings(
    *, mode: str, bindings, project, characters, scene, props, shot_reference_assets, user,
) -> dict:
    """Validate one Shot's common shape without guessing a workflow contract."""
    normalize_reference_identifier(mode, "conditioning_mode")
    if bindings in (None, ""):
        bindings = {}
    if not isinstance(bindings, dict):
        raise ReferenceBindingError("reference_bindings 必须是 JSON 对象")
    unknown = set(bindings) - REFERENCE_BINDING_KEYS
    if unknown:
        raise ReferenceBindingError(
            f"reference_bindings 包含未知字段: {', '.join(sorted(unknown))}"
        )

    normalized: dict = {}
    if bindings.get("first_frame_asset_id") not in (None, ""):
        normalized["first_frame_asset_id"] = _uuid_string(
            bindings["first_frame_asset_id"], "first_frame_asset_id",
        )
    if bindings.get("last_frame_asset_id") not in (None, ""):
        normalized["last_frame_asset_id"] = _uuid_string(
            bindings["last_frame_asset_id"], "last_frame_asset_id",
        )
    if bindings.get("first_frame_reference_asset_id") not in (None, ""):
        normalized["first_frame_reference_asset_id"] = _uuid_string(
            bindings["first_frame_reference_asset_id"],
            "first_frame_reference_asset_id",
        )
    character_items = _normalize_entity_items(bindings.get("characters", []), "characters")
    prop_items = _normalize_entity_items(bindings.get("props", []), "props")
    scene_item = _normalize_scene(bindings.get("scene"))
    video_items = _normalize_videos(bindings.get("videos", []))
    audio_items = _normalize_audios(bindings.get("audios", []))
    if character_items:
        normalized["characters"] = character_items
    if scene_item:
        normalized["scene"] = scene_item
    if prop_items:
        normalized["props"] = prop_items
    if video_items:
        normalized["videos"] = video_items
    if audio_items:
        normalized["audios"] = audio_items

    frame_ids = [
        value for key in ("first_frame_asset_id", "last_frame_asset_id")
        if (value := normalized.get(key))
    ]
    continuity_frame_id = normalized.get("first_frame_reference_asset_id")
    classified = [*character_items, *prop_items, *([scene_item] if scene_item else [])]
    classified_ids = [item["asset_id"] for item in classified]
    video_asset_ids = [item["asset_id"] for item in video_items]
    paired_audio_ids = [
        item["audio_asset_id"] for item in video_items if item.get("audio_asset_id")
    ]
    standalone_audio_ids = [item["asset_id"] for item in audio_items]
    all_asset_ids = [
        *frame_ids,
        *([continuity_frame_id] if continuity_frame_id else []),
        *classified_ids,
        *video_asset_ids,
        *paired_audio_ids,
        *standalone_audio_ids,
    ]
    if len(all_asset_ids) != len(set(all_asset_ids)):
        raise ReferenceBindingError(
            "reference_bindings 中 asset_id 必须全局唯一，不能让同一素材承担多个槽位"
        )

    if (
        len(classified) + (1 if continuity_frame_id else 0)
        > max_structural_media_bindings()
    ):
        raise ReferenceBindingError(
            f"分类参考图结构上限为 {max_structural_media_bindings()}；"
            "实际工作流上限由冻结合同决定"
        )

    character_map = {str(item.id): item for item in characters}
    prop_map = {str(item.id): item for item in props}
    scene_id = str(scene.id) if scene is not None else None
    if not scene_id and scene_item:
        raise ReferenceBindingError("当前分镜未选择场景，不能保存 scene 分类绑定")
    for field, items, entity_map in (
        ("characters", character_items, character_map),
        ("props", prop_items, prop_map),
    ):
        for item in items:
            entity = entity_map.get(item["entity_id"])
            if entity is None:
                raise ReferenceBindingError(
                    f"{field} 绑定实体 {item['entity_id']} 未绑定到当前分镜"
                )
            if field == "characters":
                try:
                    visual_reference_allowed = character_requires_visual_reference(
                        entity,
                    )
                except VisualReferencePolicyError as exc:
                    raise ReferenceBindingError(str(exc)) from exc
                if not visual_reference_allowed:
                    raise ReferenceBindingError(
                        f"characters 实体 {entity.name} 的视觉引用策略为排除；"
                        "说话人事实可保留，但不能绑定图片"
                    )
            if item["asset_id"] not in _available_asset_ids(entity):
                raise ReferenceBindingError(
                    f"{field} 的 asset_id 必须来自实体 {entity.name} 的 anchor/reference_assets"
                )
            asset = Asset.objects.filter(pk=item["asset_id"]).first()
            expected_kind = Asset.Kind.CHARACTER if field == "characters" else Asset.Kind.PROP
            generic_role = (asset.metadata or {}).get("reference_category") if asset else None
            if not asset or not (
                asset.kind == expected_kind
                or (asset.kind == Asset.Kind.REFERENCE and generic_role == expected_kind)
            ):
                raise ReferenceBindingError(
                    f"{field} 参考图 kind 必须为 {expected_kind}；通用 reference 必须声明 metadata.reference_category"
                )
    if scene_item:
        if scene_id != scene_item["entity_id"]:
            raise ReferenceBindingError("scene.entity_id 必须是当前分镜选择的场景")
        if scene_item["asset_id"] not in _available_asset_ids(scene):
            raise ReferenceBindingError(
                f"scene.asset_id 必须来自场景 {scene.name} 的 anchor/reference_assets"
            )
        scene_asset = Asset.objects.filter(pk=scene_item["asset_id"]).first()
        scene_role = (scene_asset.metadata or {}).get("reference_category") if scene_asset else None
        if not scene_asset or not (
            scene_asset.kind == Asset.Kind.SCENE
            or (scene_asset.kind == Asset.Kind.REFERENCE and scene_role == Asset.Kind.SCENE)
        ):
            raise ReferenceBindingError(
                "scene 参考图 kind 必须为 scene；通用 reference 必须声明 metadata.reference_category=scene"
            )

    shot_asset_ids = {str(item.id) for item in shot_reference_assets}
    for field in (
        "first_frame_asset_id",
        "last_frame_asset_id",
        "first_frame_reference_asset_id",
    ):
        asset_id = normalized.get(field)
        if asset_id and asset_id not in shot_asset_ids:
            raise ReferenceBindingError(
                f"{field} 必须先加入当前分镜的 reference_assets，禁止从无序素材池推断"
            )

    assets = Asset.objects.in_bulk(all_asset_ids)
    if len(assets) != len(set(all_asset_ids)):
        missing = sorted(set(all_asset_ids) - {str(value) for value in assets})
        raise ReferenceBindingError(f"reference_bindings 素材不存在: {', '.join(missing)}")
    for raw_id in all_asset_ids:
        asset = assets.get(UUID(raw_id))
        if asset is None or not asset.is_active:
            raise ReferenceBindingError(f"参考素材不存在或已停用: {raw_id}")
        if is_workflow_smoke_fixture_asset(asset):
            raise ReferenceBindingError(
                f"工作流 Smoke 专用素材不能用于生产分镜: {asset.name}"
            )
        if not getattr(user, "is_staff", False) and asset.owner_id not in (None, user.id):
            raise ReferenceBindingError(f"不能使用其他用户的参考素材: {raw_id}")
        if raw_id in video_asset_ids:
            _validate_reference_media_asset(asset, media_type="video")
        elif raw_id in {*paired_audio_ids, *standalone_audio_ids}:
            _validate_reference_media_asset(asset, media_type="audio")
        elif not _is_image_asset(asset):
            raise ReferenceBindingError(f"参考素材必须是可识别的图片: {asset.name}")
        _validate_executable_local_source(asset, user)
    for item in video_items:
        if item.get("use_embedded_audio"):
            video_asset = assets.get(UUID(item["asset_id"]))
            if not (video_asset.metadata or {}).get("has_audio"):
                raise ReferenceBindingError(
                    f"参考视频 {video_asset.name} 未检测到内嵌音轨，不能启用 use_embedded_audio"
                )

    # Entity lists are serialized in stable project order, never request order.
    character_order = {
        str(item.id): (item.stable_speaker_id, str(item.id)) for item in character_map.values()
    }
    prop_order = {str(item.id): (item.name, str(item.id)) for item in prop_map.values()}
    if "characters" in normalized:
        normalized["characters"].sort(key=lambda item: character_order[item["entity_id"]])
    if "props" in normalized:
        normalized["props"].sort(key=lambda item: prop_order[item["entity_id"]])
    return normalized


def _snapshot_slot_maps(snapshot: dict) -> tuple[dict, dict]:
    for lock in snapshot.get("locks", []):
        if lock.get("scope") == "global" and lock.get("key") == "reference_slots":
            value = lock.get("value") or {}
            return value.get("picture", {}), value.get("subject", {})
    return {}, {}


def _binding_entity_label(category: str, entity_id: str | None, subject_slots: dict) -> str:
    if not entity_id:
        return ""
    key = f"{category}:{entity_id}"
    locked = subject_slots.get(key) or {}
    if locked.get("label"):
        return locked["label"]
    return f"<Subject {entity_id[:8]}>"


def resolve_group_reference_bindings(
    *, project, shots: list[Shot], mode_contract: dict, snapshot: dict,
    single_shot_bindings: dict | None = None,
    single_shot_reference_assets: list[Asset] | None = None,
) -> list[dict]:
    """Return canonical media inputs using one explicit frozen mode contract.

    This resolver knows only the platform input kinds and persisted binding
    fields.  Model names, model-specific mode names, prompt templates and
    purpose vocabularies are deliberately absent.
    """
    if not shots:
        return []
    if not isinstance(mode_contract, dict):
        raise ReferenceBindingError("冻结 mode contract 缺失")
    mode_key = normalize_reference_identifier(mode_contract.get("key"), "mode.key")
    input_kind = str(mode_contract.get("input_kind") or "").strip()
    if input_kind not in {"text_only", "frame_slots", "classified_references"}:
        raise ReferenceBindingError("mode.input_kind 未注册")
    inputs = mode_contract.get("inputs")
    if not isinstance(inputs, dict):
        raise ReferenceBindingError("mode.inputs 冻结合同缺失")
    normalized = []
    if (single_shot_bindings is not None or single_shot_reference_assets is not None) and len(shots) != 1:
        raise ReferenceBindingError("临时 reference_bindings 只允许用于单个分镜")
    for shot in shots:
        normalized.append(normalize_shot_reference_bindings(
            mode=mode_key,
            bindings=single_shot_bindings if single_shot_bindings is not None else shot.reference_bindings,
            project=project,
            characters=list(shot.characters.all()),
            scene=shot.scene,
            props=list(shot.props.all()),
            shot_reference_assets=(
                single_shot_reference_assets
                if single_shot_reference_assets is not None else list(shot.reference_assets.all())
            ),
            user=project.owner,
        ))

    picture_slots, subject_slots = _snapshot_slot_maps(snapshot)
    asset_ids: list[str] = []
    categories: list[tuple[str, str | None, object | None]] = []
    media_videos: list[dict] = []
    media_audios: list[dict] = []

    if input_kind == "text_only":
        if any(item for item in normalized):
            raise ReferenceBindingError("text_only 条件模式不接受 reference_bindings")
        validate_reference_bindings_against_mode_contract({}, mode_contract)
    elif input_kind == "frame_slots":
        if any(
            item.get("first_frame_reference_asset_id")
            or item.get("characters")
            or item.get("scene")
            or item.get("props")
            or item.get("videos")
            or item.get("audios")
            for item in normalized
        ):
            raise ReferenceBindingError("frame_slots 条件模式只接受首帧/尾帧槽位")
        first_id = normalized[0].get("first_frame_asset_id")
        last_id = normalized[-1].get("last_frame_asset_id")
        for item in normalized:
            item_first = item.get("first_frame_asset_id")
            item_last = item.get("last_frame_asset_id")
            if (
                (item_first is not None and item_first != first_id)
                or (item_last is not None and item_last != last_id)
            ):
                raise ReferenceBindingError(
                    "多分镜 frame_slots 绑定必须与组首帧/组尾帧一致；"
                    "系统不会猜测、合并或重排槽位"
                )
        group_bindings: dict[str, str] = {}
        if first_id:
            group_bindings["first_frame_asset_id"] = first_id
            asset_ids.append(first_id)
            categories.append(("first_frame", None, None))
        if last_id:
            group_bindings["last_frame_asset_id"] = last_id
            asset_ids.append(last_id)
            categories.append(("last_frame", None, None))
        validate_reference_bindings_against_mode_contract(
            group_bindings,
            mode_contract,
        )
    else:
        if any(
            item.get("first_frame_asset_id") or item.get("last_frame_asset_id")
            for item in normalized
        ):
            raise ReferenceBindingError(
                "classified_references 条件模式不接受首帧/尾帧执行槽位"
            )
        continuity_ids = {
            item["first_frame_reference_asset_id"]
            for item in normalized
            if item.get("first_frame_reference_asset_id")
        }
        if len(continuity_ids) > 1:
            raise ReferenceBindingError(
                "同一生产组不能声明多个不同的 first_frame_reference_asset_id"
            )
        merged: dict[tuple[str, str], str] = {}
        for item in normalized:
            for category, values in (
                ("character", item.get("characters", [])),
                ("scene", [item["scene"]] if item.get("scene") else []),
                ("prop", item.get("props", [])),
            ):
                for value in values:
                    key = (category, value["entity_id"])
                    previous = merged.get(key)
                    if previous and previous != value["asset_id"]:
                        raise ReferenceBindingError(
                            f"多分镜中 {category}:{value['entity_id']} 绑定了不同 asset_id"
                        )
                    merged[key] = value["asset_id"]
        all_image_ids = [*continuity_ids, *merged.values()]
        if len(set(all_image_ids)) != len(all_image_ids):
            raise ReferenceBindingError("合并后同一 asset_id 不能映射到多个图片槽位")
        media_videos = normalized[0].get("videos", [])
        media_audios = normalized[0].get("audios", [])
        if any(
            item.get("videos", []) != media_videos or item.get("audios", []) != media_audios
            for item in normalized[1:]
        ):
            raise ReferenceBindingError(
                "多分镜的参考视频/音频绑定必须完全一致；系统不会猜测、合并或重排"
            )
        _validate_count(
            len(all_image_ids),
            inputs.get("total_images") or {},
            label="total_images",
        )
        _validate_count(0, inputs.get("first_frame") or {}, label="first_frame")
        _validate_count(0, inputs.get("last_frame") or {}, label="last_frame")
        _validate_count(
            len(all_image_ids),
            inputs.get("entity_images") or {},
            label="entity_images",
        )
        validate_dynamic_reference_bindings_against_mode_contract(
            {"videos": media_videos, "audios": media_audios},
            mode_contract,
        )
        entities = {
            ("character", str(value.id)): value
            for shot in shots for value in shot.characters.all()
        }
        entities.update({
            ("prop", str(value.id)): value for shot in shots for value in shot.props.all()
        })
        entities.update({
            ("scene", str(shot.scene.id)): shot.scene for shot in shots if shot.scene
        })
        ordered = sorted(merged.items(), key=lambda item: (
            int((picture_slots.get(item[1]) or {}).get("slot", 10**9)),
            {"character": 0, "scene": 1, "prop": 2}[item[0][0]],
            item[0][1],
        ))
        if any(not (picture_slots.get(asset_id) or {}).get("slot") for _, asset_id in ordered):
            raise ReferenceBindingError(
                "分类参考图尚未写入项目稳定槽位；请先运行 sync_consistency_locks"
            )
        asset_ids = []
        categories = []
        if continuity_ids:
            continuity_id = next(iter(continuity_ids))
            asset_ids.append(continuity_id)
            categories.append(("continuity_frame", None, None))
        asset_ids.extend(asset_id for _, asset_id in ordered)
        categories.extend(
            (category, entity_id, entities[(category, entity_id)])
            for (category, entity_id), _ in ordered
        )

    assets = Asset.objects.in_bulk(asset_ids)
    records = []
    for input_slot, (asset_id, (category, entity_id, entity)) in enumerate(
        zip(asset_ids, categories, strict=True), start=1,
    ):
        asset = assets.get(UUID(asset_id))
        if asset is None:
            raise ReferenceBindingError(f"参考素材不存在: {asset_id}")
        record = _asset_record(asset, category=category, entity=entity)
        record.update({
            "input_slot": input_slot,
            "input_label": f"<Picture {input_slot}>",
            "project_slot": (picture_slots.get(asset_id) or {}).get("slot"),
            "entity_label": _binding_entity_label(category, entity_id, subject_slots)
            if category in {"character", "scene", "prop"} else "",
        })
        records.append(record)
    audio_ordinal = 1
    for video_ordinal, item in enumerate(media_videos, start=1):
        asset = Asset.objects.get(pk=item["asset_id"])
        record = _asset_record(asset, category="video")
        record.update({
            "input_slot": video_ordinal,
            "input_label": f"<Video {video_ordinal}>",
            "purpose": item["purpose"],
            "use_embedded_audio": item.get("use_embedded_audio", False),
            "audio_asset_id": item.get("audio_asset_id"),
            "audio_input_label": None,
            "project_slot": None,
            "entity_label": "",
        })
        if item.get("use_embedded_audio") or item.get("audio_asset_id"):
            record["audio_input_label"] = f"<Audio {audio_ordinal}>"
            record["audio_purpose"] = item["audio_purpose"]
            if item.get("audio_asset_id"):
                audio_asset = Asset.objects.get(pk=item["audio_asset_id"])
                record.update({
                    "audio_name": audio_asset.name,
                    "audio_checksum": audio_asset.checksum,
                    "audio_local_path": audio_asset.file.path if audio_asset.file else audio_asset.local_path,
                    "audio_description": audio_asset.description,
                    "audio_prompt_hint": audio_asset.prompt_hint,
                })
            audio_ordinal += 1
        records.append(record)
    for input_slot, item in enumerate(media_audios, start=1):
        asset = Asset.objects.get(pk=item["asset_id"])
        record = _asset_record(asset, category="audio")
        record.update({
            "input_slot": input_slot,
            "input_label": f"<Audio {audio_ordinal}>",
            "purpose": item["purpose"],
            "project_slot": None,
            "entity_label": "",
        })
        audio_ordinal += 1
        records.append(record)
    return records
