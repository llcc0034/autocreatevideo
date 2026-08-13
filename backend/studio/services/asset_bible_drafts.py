"""Review-only Bible field drafts grounded in saved asset metadata.

The local Qwen process receives no image bytes and this module never claims
pixel understanding.  It combines only the selected assets' persisted name,
description, prompt hint and style metadata, returns an editable candidate,
and never creates or mutates a Bible entity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from typing import Any, Iterable

from django.db.models import Q
from django.utils.dateparse import parse_datetime

from studio.models import (
    Asset, CharacterBible, GenerationTask, PropBible, SceneBible, SeriesProject,
)
from studio.services.prompt_translation import (
    PromptTranslationError,
    PromptTranslationResourceBusy,
    _config as local_qwen_config,
    _require_runtime as require_local_qwen_runtime,
    _run_runtime as run_local_qwen_runtime,
)


ASSET_BIBLE_DRAFT_OPERATION = "asset_bible_draft"
ASSET_BIBLE_DRAFT_MAX_SOURCE_CHARS = 12_000
_ACTIVE_TASK_STATUSES = (
    GenerationTask.Status.QUEUED,
    GenerationTask.Status.RUNNING,
    GenerationTask.Status.CANCEL_REQUESTED,
)
_ENTITY_TYPES = {"character", "scene", "prop"}
_OUTPUT_LANGUAGES = {"zh", "en"}


class AssetBibleDraftError(RuntimeError):
    pass


class AssetBibleDraftValidationError(AssetBibleDraftError):
    pass


class AssetBibleDraftResourceBusy(AssetBibleDraftError):
    pass


class AssetBibleDraftRuntimeUnavailable(AssetBibleDraftError):
    pass


class AssetBibleDraftConflict(AssetBibleDraftError):
    pass


def provenance_snapshots_equal(left: Any, right: Any) -> bool:
    """Compare immutable provenance while treating timezone offsets canonically."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return left == right
    left_copy = dict(left)
    right_copy = dict(right)
    left_time = left_copy.pop("asset_updated_at", None)
    right_time = right_copy.pop("asset_updated_at", None)
    try:
        parsed_left = parse_datetime(str(left_time or ""))
        parsed_right = parse_datetime(str(right_time or ""))
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        parsed_left is not None and parsed_right is not None
        and parsed_left == parsed_right and left_copy == right_copy
    )


def _metadata_payload(asset: Asset) -> dict[str, Any]:
    """Return the complete, bounded textual evidence supplied to the model.

    Opaque ``Asset.metadata`` values contain audit paths, crop coordinates and
    importer internals rather than authored descriptive evidence.  They stay
    server-side.  The public editable fields plus the related style's authored
    fields are the explicit metadata contract for this feature.
    """
    style = asset.style
    return {
        "asset_id": str(asset.id),
        "kind": asset.kind,
        "name": str(asset.name or "").strip(),
        "description": str(asset.description or "").strip(),
        "prompt_hint": str(asset.prompt_hint or "").strip(),
        "style": ({
            "name": str(style.name or "").strip(),
            "description": str(style.description or "").strip(),
            "visual_prompt": str(style.visual_prompt or "").strip(),
        } if style is not None else None),
    }


def _asset_snapshot(asset: Asset) -> dict[str, Any]:
    return {
        **_metadata_payload(asset),
        "checksum": str(asset.checksum or "").strip().casefold(),
        "updated_at": asset.updated_at.isoformat(),
    }


def _snapshot_hash(assets: Iterable[Asset]) -> str:
    payload = [_asset_snapshot(asset) for asset in assets]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_asset(*, user, asset_id, entity_type: str) -> Asset:
    queryset = Asset.objects.select_related("style", "owner").filter(pk=asset_id)
    if not user.is_staff:
        queryset = queryset.filter(Q(owner=user) | Q(owner__isnull=True))
    asset = queryset.first()
    if asset is None:
        # Do not reveal whether a rejected identifier belongs to another tenant.
        raise AssetBibleDraftValidationError("所选素材不存在、已失效或无权访问")
    if not asset.is_active:
        raise AssetBibleDraftValidationError(f"素材“{asset.name}”已停用")
    if asset.kind == Asset.Kind.REFERENCE:
        reference_category = str(
            (asset.metadata or {}).get("reference_category") or ""
        ).strip().casefold()
        if reference_category != entity_type:
            raise AssetBibleDraftValidationError(
                f"通用素材“{asset.name}”未标注 reference_category={entity_type}，不能用于该类设定草稿"
            )
    elif asset.kind != entity_type:
        raise AssetBibleDraftValidationError(
            f"素材“{asset.name}”类别为 {asset.kind}，不能用于 {entity_type} 设定草稿"
        )
    if not str(asset.mime_type or "").casefold().startswith("image/"):
        raise AssetBibleDraftValidationError(f"素材“{asset.name}”不是图片素材")
    if not str(asset.description or "").strip() or not str(asset.prompt_hint or "").strip():
        raise AssetBibleDraftValidationError(
            f"素材“{asset.name}”缺少 description 或 prompt_hint；本功能不读取图片像素，请先补全这两个元数据字段"
        )
    evidence_strings = [asset.name, asset.description, asset.prompt_hint]
    if asset.style is not None:
        evidence_strings.extend((
            asset.style.name, asset.style.description, asset.style.visual_prompt,
        ))
    if any(
        ord(char) < 32 and char not in "\n\r\t"
        for value in evidence_strings for char in str(value or "")
    ):
        raise AssetBibleDraftValidationError(
            f"素材“{asset.name}”的文本元数据含控制字符，请清理后重试"
        )
    return asset


def _validate_checksum(asset: Asset, supplied: str) -> None:
    actual = str(asset.checksum or "").strip().casefold()
    claimed = str(supplied or "").strip().casefold()
    if not actual:
        raise AssetBibleDraftValidationError(
            f"素材“{asset.name}”缺少服务端校验哈希，不能作为可追溯草稿依据"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", actual):
        raise AssetBibleDraftValidationError(
            f"素材“{asset.name}”没有有效的 SHA-256，不能作为可追溯草稿依据"
        )
    if not hmac.compare_digest(actual, claimed):
        raise AssetBibleDraftConflict(
            f"素材“{asset.name}”已发生变化，请刷新素材库后重新生成"
        )


def _source_assets(assets: list[Asset]) -> list[dict[str, Any]]:
    return [{
        "id": str(asset.id),
        "kind": asset.kind,
        "name": asset.name,
        "checksum": str(asset.checksum or "").casefold(),
        "updated_at": asset.updated_at.isoformat(),
        "metadata_sha256": _snapshot_hash([asset]),
        "metadata_fields_used": [
            key for key, value in (
                ("name", asset.name),
                ("description", asset.description),
                ("prompt_hint", asset.prompt_hint),
                ("style", asset.style_id),
            ) if value
        ],
    } for asset in assets]


def _validate_result(payload: Any, *, output_language: str) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != {
        "name", "description", "visual_identity",
    }:
        raise AssetBibleDraftError("本地 Qwen 返回了无效的素材设定草稿结构")
    limits = {"name": 160, "description": 2_000, "visual_identity": 3_000}
    result: dict[str, str] = {}
    for field, limit in limits.items():
        value = payload.get(field)
        if not isinstance(value, str):
            raise AssetBibleDraftError(f"本地 Qwen 返回的 {field} 不是文本")
        value = value.strip()
        if len(value) > limit:
            raise AssetBibleDraftError(f"本地 Qwen 返回的 {field} 超过 {limit} 字符")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise AssetBibleDraftError(f"本地 Qwen 返回的 {field} 含控制字符")
        result[field] = value
    if not result["name"]:
        raise AssetBibleDraftError("本地 Qwen 未生成可用名称")
    if output_language == "en" and any(
        re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", value)
        for field, value in result.items() if field != "name"
    ):
        raise AssetBibleDraftError("英文草稿仍包含中文字符，请重新生成或改用中文草稿")
    if output_language == "zh" and not re.search(
        r"[\u3400-\u4dbf\u4e00-\u9fff]",
        result["description"] + "\n" + result["visual_identity"],
    ):
        raise AssetBibleDraftError("中文草稿的基础描述和视觉锚点均未包含中文，请重新生成")
    pixel_claim = re.compile(
        r"(?:图中|照片中|画面中|图片显示|照片显示|从图(?:片)?看|"
        r"\b(?:the image|the photo|pictured|visible in (?:the )?(?:image|photo))\b)",
        re.IGNORECASE,
    )
    if any(pixel_claim.search(value) for value in result.values()):
        raise AssetBibleDraftError("草稿错误声称读取了图片像素，已拒绝该候选")
    if any(re.search(r"<\/?[A-Za-z][^>]*>", value) for value in result.values()):
        raise AssetBibleDraftError("草稿包含 HTML/XML 标签，已拒绝该候选")
    return result


def _name_conflict(project: SeriesProject, name: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(name or "")).strip().casefold()
    if not normalized:
        return False
    for model in (CharacterBible, SceneBible, PropBible):
        if any(
            unicodedata.normalize("NFKC", str(item.name or "")).strip().casefold()
            == normalized
            for item in model.objects.filter(project=project).only("name")
        ):
            return True
    return False


def generate_asset_bible_draft(
    *, project: SeriesProject, user, entity_type: str, output_language: str,
    asset_id, asset_checksum: str, attempt: int = 1,
) -> dict[str, Any]:
    """Generate an isolated editable candidate and leave all Bible rows intact."""
    if entity_type not in _ENTITY_TYPES:
        raise AssetBibleDraftValidationError("entity_type 仅支持 character、scene 或 prop")
    if output_language not in _OUTPUT_LANGUAGES:
        raise AssetBibleDraftValidationError("output_language 仅支持 zh 或 en")
    if project.owner_id != user.id and not user.is_staff:
        raise AssetBibleDraftValidationError("不能为其他用户的项目生成设定草稿")

    asset = _load_asset(user=user, asset_id=asset_id, entity_type=entity_type)
    assets = [asset]
    _validate_checksum(asset, asset_checksum)
    snapshot_hash = _snapshot_hash(assets)
    evidence = [_metadata_payload(asset) for asset in assets]
    source_chars = len(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))
    if source_chars > ASSET_BIBLE_DRAFT_MAX_SOURCE_CHARS:
        raise AssetBibleDraftValidationError(
            f"所选素材元数据共 {source_chars} 字符，超过 {ASSET_BIBLE_DRAFT_MAX_SOURCE_CHARS} 字符上限；请减少素材或精简描述"
        )
    if GenerationTask.objects.filter(status__in=_ACTIVE_TASK_STATUSES).exists():
        raise AssetBibleDraftResourceBusy(
            "视频任务正在排队或执行；为避免本地 Qwen 与 ComfyUI 争用内存，请稍后重试"
        )

    config = local_qwen_config()
    try:
        require_local_qwen_runtime(config)
        runtime_payload = run_local_qwen_runtime({
            "operation": ASSET_BIBLE_DRAFT_OPERATION,
            "request_id": snapshot_hash[:24],
            "source_fingerprint": snapshot_hash,
            "entity_type": entity_type,
            "output_language": output_language,
            "assets": evidence,
            "attempt": max(1, min(int(attempt), 100)),
            "seed": int(snapshot_hash[:8], 16) + max(1, int(attempt)),
            "model_dir": str(config["model_dir"]),
            "model_id": config["model_id"],
            "device": config["device"],
            "gpu_layers": config.get("gpu_layers", -1),
            "max_new_tokens": min(1_024, max(512, int(config["max_new_tokens"]))),
            "context_size": 4_096,
            "timeout_seconds": config["timeout_seconds"],
        }, config)
    except PromptTranslationResourceBusy as exc:
        raise AssetBibleDraftResourceBusy(str(exc)) from exc
    except PromptTranslationError as exc:
        raise AssetBibleDraftRuntimeUnavailable(str(exc)) from exc

    candidate = _validate_result(runtime_payload.get("result"), output_language=output_language)
    if candidate["name"] != asset.name:
        raise AssetBibleDraftError("本地 Qwen 改写了素材名称，已拒绝该候选")
    # Inference happens outside a database transaction. Re-read every metadata
    # row before returning so concurrent edits cannot produce a falsely-current
    # draft from stale evidence.
    current_asset = _load_asset(user=user, asset_id=asset_id, entity_type=entity_type)
    current_assets = [current_asset]
    _validate_checksum(current_asset, asset_checksum)
    if not hmac.compare_digest(snapshot_hash, _snapshot_hash(current_assets)):
        raise AssetBibleDraftConflict("素材元数据在生成期间已修改，请刷新后重新生成")

    warnings: list[str] = []
    if _name_conflict(project, candidate["name"]):
        warnings.append(
            "该名称已被本项目中的人物、场景或道具使用；保存 Bible 前必须修改为项目内唯一名称。"
        )
    return {
        "project_id": str(project.id),
        "entity_type": entity_type,
        "output_language": output_language,
        "evidence_basis": "asset_metadata",
        "reference_mode": "single_reference",
        "pixel_vision_used": False,
        "source_metadata_sha256": snapshot_hash,
        "draft": candidate,
        "source_assets": _source_assets(current_assets),
        "warnings": warnings,
        "runtime": {
            "provider": "local_qwen",
            "model": str(runtime_payload.get("model") or config["model_id"]),
            "loaded_on_demand": True,
            "unloaded_after_request": True,
            "review_required": True,
            "persisted": False,
            "pixel_vision_supported": False,
            "elapsed_ms": runtime_payload.get("elapsed_ms"),
        },
    }
