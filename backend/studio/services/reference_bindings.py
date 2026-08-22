"""Strict, deterministic MiniMax H3 image/video/audio reference bindings.

The user-facing ``Shot.reference_bindings`` JSON is deliberately separated
from the legacy ``Shot.reference_assets`` many-to-many collection.  The latter
is only an asset picker/library relationship; it must never decide the semantic
role or input slot of an image, video, or audio asset.

Both prompt construction and ComfyUI dynamic input injection consume the
ordered records returned by :func:`resolve_group_reference_bindings`.  This
keeps ``<Picture N>`` / ``<Video N>`` / ``<Audio N>`` labels and graph inputs
on one auditable contract.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from django.conf import settings

from studio.models import Asset, Shot


REFERENCE_BINDING_KEYS = frozenset({
    "first_frame_asset_id", "last_frame_asset_id", "first_frame_reference_asset_id",
    "characters", "scene", "props", "videos", "audios",
})
ENTITY_BINDING_KEYS = frozenset({"entity_id", "asset_id"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm"})
AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".m4a", ".ogg"})
VIDEO_REFERENCE_PURPOSES = frozenset({
    "motion_camera_reference",
    "video_editing",
    "video_continuation",
})
AUDIO_REFERENCE_PURPOSES = frozenset({"audio_reference", "audio_reuse"})
DEFAULT_VIDEO_REFERENCE_PURPOSE = "motion_camera_reference"
DEFAULT_AUDIO_REFERENCE_PURPOSE = "audio_reference"


def prompt_template_key(requested_mode: str, effective_mode: str, reference_count: int = 0) -> str:
    """Version prompt syntax while preserving the user's workflow choice."""
    if requested_mode == Shot.H3Mode.T2VA:
        return "h3_t2v_text_v1"
    if requested_mode == Shot.H3Mode.REF2VA:
        return "h3_r2v_classified_v1" if reference_count else "h3_r2v_zero_reference_v1"
    return {
        Shot.H3Mode.T2VA: "h3_i2v_zero_frame_v1",
        Shot.H3Mode.I2VA: "h3_i2v_first_frame_v1",
        Shot.H3Mode.L2VA: "h3_i2v_last_frame_v1",
        Shot.H3Mode.FL2VA: "h3_i2v_first_last_v1",
    }[effective_mode]


class ReferenceBindingError(ValueError):
    pass


def effective_conditioning_mode(requested_mode: str, bindings: list[dict]) -> str:
    """Describe what the ImageToVideo node will actually receive."""
    if requested_mode == Shot.H3Mode.REF2VA:
        return Shot.H3Mode.REF2VA
    categories = {item.get("category") for item in bindings}
    if "first_frame" in categories and "last_frame" in categories:
        return Shot.H3Mode.FL2VA
    if "first_frame" in categories:
        return Shot.H3Mode.I2VA
    if "last_frame" in categories:
        return Shot.H3Mode.L2VA
    return Shot.H3Mode.T2VA


def _uuid_string(value, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ReferenceBindingError(f"{field} 必须是有效 UUID") from exc


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
        if not isinstance(item, dict) or not item.get("asset_id") or set(item) - allowed:
            raise ReferenceBindingError(
                f"videos[{index}] 只能包含 asset_id、use_embedded_audio、audio_asset_id、"
                "purpose、audio_purpose"
            )
        use_embedded = item.get("use_embedded_audio", False)
        if not isinstance(use_embedded, bool):
            raise ReferenceBindingError(f"videos[{index}].use_embedded_audio 必须是布尔值")
        purpose = item.get("purpose", DEFAULT_VIDEO_REFERENCE_PURPOSE)
        if purpose not in VIDEO_REFERENCE_PURPOSES:
            raise ReferenceBindingError(
                f"videos[{index}].purpose 必须是 "
                f"{', '.join(sorted(VIDEO_REFERENCE_PURPOSES))} 之一"
            )
        normalized = {
            "asset_id": _uuid_string(item["asset_id"], f"videos[{index}].asset_id"),
            "purpose": purpose,
            "use_embedded_audio": use_embedded,
        }
        if item.get("audio_asset_id") not in (None, ""):
            normalized["audio_asset_id"] = _uuid_string(
                item["audio_asset_id"], f"videos[{index}].audio_asset_id",
            )
        if use_embedded and normalized.get("audio_asset_id"):
            raise ReferenceBindingError(
                f"videos[{index}] 不能同时使用内嵌音轨和独立 audio_asset_id"
            )
        has_paired_audio = use_embedded or bool(normalized.get("audio_asset_id"))
        if has_paired_audio:
            audio_purpose = item.get("audio_purpose", DEFAULT_AUDIO_REFERENCE_PURPOSE)
            if audio_purpose not in AUDIO_REFERENCE_PURPOSES:
                raise ReferenceBindingError(
                    f"videos[{index}].audio_purpose 必须是 "
                    f"{', '.join(sorted(AUDIO_REFERENCE_PURPOSES))} 之一"
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
    if len(result) > 3:
        raise ReferenceBindingError("Ref2VA 最多允许 3 个参考视频")
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
            or set(item) - {"asset_id", "purpose"}
        ):
            raise ReferenceBindingError(f"audios[{index}] 只能包含 asset_id 和 purpose")
        purpose = item.get("purpose", DEFAULT_AUDIO_REFERENCE_PURPOSE)
        if purpose not in AUDIO_REFERENCE_PURPOSES:
            raise ReferenceBindingError(
                f"audios[{index}].purpose 必须是 "
                f"{', '.join(sorted(AUDIO_REFERENCE_PURPOSES))} 之一"
            )
        result.append({
            "asset_id": _uuid_string(item["asset_id"], f"audios[{index}].asset_id"),
            "purpose": purpose,
        })
    asset_ids = [item["asset_id"] for item in result]
    if len(asset_ids) != len(set(asset_ids)):
        raise ReferenceBindingError("audios 不能重复绑定同一参考音频")
    if len(result) > 3:
        raise ReferenceBindingError("Ref2VA 最多允许 3 个独立参考音频")
    return result


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
    """Mirror worker source constraints so bad references fail before queueing."""
    asset_root = Path(settings.ASSET_LIBRARY_ROOT).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    try:
        if asset.owner_id is None:
            if asset.file or not asset.local_path:
                raise ReferenceBindingError(
                    f"全局参考素材必须来自只读本地素材库: {asset.name}"
                )
            path = Path(asset.local_path).resolve()
            allowed_root = asset_root
        elif asset.file:
            path = Path(asset.file.path).resolve()
            allowed_root = (media_root / "assets" / str(asset.owner_id)).resolve()
        elif getattr(user, "is_staff", False) and asset.local_path:
            path = Path(asset.local_path).resolve()
            allowed_root = asset_root if path.is_relative_to(asset_root) else media_root
        else:
            raise ReferenceBindingError(
                f"用户参考素材必须由上传接口保存为本地文件: {asset.name}"
            )
        if not path.is_relative_to(allowed_root) or not path.is_file():
            raise ReferenceBindingError(f"参考素材本地文件不可执行或越出授权目录: {asset.name}")
    except (OSError, ValueError, NotImplementedError) as exc:
        if isinstance(exc, ReferenceBindingError):
            raise
        raise ReferenceBindingError(f"无法读取参考素材本地文件: {asset.name}") from exc


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
    try:
        duration_ms = int((asset.metadata or {}).get("duration_ms", 0))
    except (TypeError, ValueError):
        duration_ms = 0
    minimum_ms = 2_000
    if not minimum_ms <= duration_ms <= 15_000:
        raise ReferenceBindingError(
            f"{asset.name} 的已验证时长必须在 {minimum_ms / 1000:g}–15 秒"
        )


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
        # ``id`` is retained as a compatibility alias for the task staging
        # layer; new callers should use the explicit ``asset_id`` field.
        "id": str(asset.id),
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
    """Validate and canonicalize one Shot's persisted snake_case contract."""
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

    if mode == Shot.H3Mode.T2VA:
        if normalized:
            raise ReferenceBindingError("T2VA 是纯文生视频，reference_bindings 必须为空")
        return {}
    if mode == Shot.H3Mode.I2VA:
        if set(normalized) != {"first_frame_asset_id"}:
            raise ReferenceBindingError("I2VA 必须且只能绑定 first_frame_asset_id")
    elif mode == Shot.H3Mode.L2VA:
        if set(normalized) != {"last_frame_asset_id"}:
            raise ReferenceBindingError("L2VA 必须且只能绑定 last_frame_asset_id")
    elif mode == Shot.H3Mode.FL2VA:
        if set(normalized) - {"first_frame_asset_id", "last_frame_asset_id"}:
            raise ReferenceBindingError(
                "FL2VA 只能绑定 first_frame_asset_id / last_frame_asset_id"
            )
    elif mode == Shot.H3Mode.REF2VA:
        if frame_ids:
            raise ReferenceBindingError(
                "Ref2VA 不使用 first_frame_asset_id / last_frame_asset_id；"
                "可使用 first_frame_reference_asset_id、characters、scene、props、videos 和 audios"
            )
        if len(classified) + (1 if continuity_frame_id else 0) > 9:
            raise ReferenceBindingError("Ref2VA 最多允许 9 张分类参考图")
    else:
        raise ReferenceBindingError(f"不支持的 H3 模式: {mode}")

    character_map = {str(item.id): item for item in characters}
    prop_map = {str(item.id): item for item in props}
    scene_id = str(scene.id) if scene is not None else None
    if mode == Shot.H3Mode.REF2VA and not scene_id and scene_item:
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
    *, project, shots: list[Shot], mode: str, snapshot: dict,
    single_shot_bindings: dict | None = None,
    single_shot_reference_assets: list[Asset] | None = None,
) -> list[dict]:
    """Return the canonical ordered media-input list used by prompt and graph."""
    if not shots:
        return []
    normalized = []
    if (single_shot_bindings is not None or single_shot_reference_assets is not None) and len(shots) != 1:
        raise ReferenceBindingError("临时 reference_bindings 只允许用于单个分镜")
    for shot in shots:
        normalized.append(normalize_shot_reference_bindings(
            mode=mode,
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
    if mode == Shot.H3Mode.T2VA:
        return []

    picture_slots, subject_slots = _snapshot_slot_maps(snapshot)
    asset_ids: list[str]
    categories: list[tuple[str, str | None, object | None]]
    media_videos: list[dict] = []
    media_audios: list[dict] = []
    if mode in {Shot.H3Mode.I2VA, Shot.H3Mode.L2VA, Shot.H3Mode.FL2VA}:
        first_id = normalized[0].get("first_frame_asset_id")
        last_id = normalized[-1].get("last_frame_asset_id")
        if len(normalized) > 1:
            for index, item in enumerate(normalized, start=1):
                item_first = item.get("first_frame_asset_id")
                item_last = item.get("last_frame_asset_id")
                if (
                    (item_first is not None and item_first != first_id)
                    or (item_last is not None and item_last != last_id)
                    or (index > 1 and item_first is not None and first_id is None)
                    or (index < len(normalized) and item_last is not None and last_id is None)
                ):
                    raise ReferenceBindingError(
                        "多分镜首尾帧任务中间分镜不得新增或改变绑定；"
                        "执行时只读取 group[0].first_frame 与 group[-1].last_frame"
                    )
        asset_ids, categories = [], []
        if first_id:
            asset_ids.append(first_id)
            categories.append(("first_frame", None, None))
        if last_id:
            asset_ids.append(last_id)
            categories.append(("last_frame", None, None))
    elif mode == Shot.H3Mode.REF2VA:
        continuity_ids = {
            item["first_frame_reference_asset_id"]
            for item in normalized
            if item.get("first_frame_reference_asset_id")
        }
        if continuity_ids and len(normalized) > 1:
            raise ReferenceBindingError("Ref2VA 首帧连续参考只能用于单镜任务")
        if len(continuity_ids) > 1:
            raise ReferenceBindingError("Ref2VA 首帧连续参考只能绑定一张图片")
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
                            f"Ref2VA 多分镜中 {category}:{value['entity_id']} 绑定了不同 asset_id"
                        )
                    merged[key] = value["asset_id"]
        if len(set(merged.values())) != len(merged):
            raise ReferenceBindingError("Ref2VA 合并后同一 asset_id 不能映射到不同实体")
        if len(merged) + len(continuity_ids) > 9:
            raise ReferenceBindingError("Ref2VA 多分镜合并后最多允许 9 张参考图")
        media_videos = normalized[0].get("videos", [])
        media_audios = normalized[0].get("audios", [])
        if any(
            item.get("videos", []) != media_videos or item.get("audios", []) != media_audios
            for item in normalized[1:]
        ):
            raise ReferenceBindingError(
                "Ref2VA 多分镜的参考视频/音频绑定必须完全一致；系统不会静默合并或重排"
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
    else:
        raise ReferenceBindingError(f"不支持的 H3 模式: {mode}")

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
        if category == "continuity_frame":
            record.update({
                "purpose": "first_frame_continuity_reference",
                "entity_name": "",
                "prompt_hint": (
                    record.get("prompt_hint")
                    or "Use as the previous-shot tail frame and target starting-state reference."
                ),
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
