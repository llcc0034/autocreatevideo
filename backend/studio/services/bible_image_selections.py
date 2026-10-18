"""One model-independent contract for explicit multi-image/anchor selection."""
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.production_execution_units import ENTITY_SCOPE_MODELS


def normalize_bible_image_selection(value, *, maximum_assets):
    allowed = {"entity_type", "entity_id", "selected_artifact_ids", "selected_library_assets",
        "anchor_artifact_id", "anchor_library_asset_id"}
    if (not isinstance(value, dict) or set(value) - allowed or not {"entity_type", "entity_id"} <= set(value)
            or not isinstance(value["entity_type"], str) or value["entity_type"] not in ENTITY_SCOPE_MODELS
            or type(maximum_assets) is not int or maximum_assets < 1):
        raise ValidationError("参考图选择需要明确实体及有效的服务端数量配置")

    def identifier(item):
        try:
            if not isinstance(item, (str, UUID)):
                raise ValueError
            return str(UUID(str(item)))
        except (ValueError, AttributeError) as exc:
            raise ValidationError("参考图选择必须引用正式 UUID，不按名称猜测") from exc

    artifacts, library = value.get("selected_artifact_ids", []), value.get("selected_library_assets", [])
    if not isinstance(artifacts, list) or not isinstance(library, list):
        raise ValidationError("请逐项提交模型候选和素材库图片")
    artifacts = [identifier(item) for item in artifacts]
    normalized_library = []
    for item in library:
        if not isinstance(item, dict) or set(item) != {"asset_id", "expected_sha256"}:
            raise ValidationError("素材库图片必须同时提交所见文件摘要")
        digest = item["expected_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValidationError("素材库图片缺少有效 SHA256")
        normalized_library.append({"asset_id": identifier(item["asset_id"]), "expected_sha256": digest})
    ids = [item["asset_id"] for item in normalized_library]
    if len(set(artifacts)) != len(artifacts) or len(set(ids)) != len(ids):
        raise ValidationError("同一模型候选或素材库图片不能重复选择")
    if not 1 <= len(artifacts) + len(ids) <= maximum_assets:
        raise ValidationError(f"每个实体须明确选择 1..{maximum_assets} 张图片")
    anchor_artifact = identifier(value["anchor_artifact_id"]) if value.get("anchor_artifact_id") is not None else None
    anchor_asset = identifier(value["anchor_library_asset_id"]) if value.get("anchor_library_asset_id") is not None else None
    if bool(anchor_artifact) == bool(anchor_asset):
        raise ValidationError("必须且只能指定一张主参考图")
    if (anchor_artifact is not None and anchor_artifact not in artifacts) or (anchor_asset is not None and anchor_asset not in ids):
        raise ValidationError("主参考图必须属于本次所选图片")
    return {"entity_type": value["entity_type"], "entity_id": identifier(value["entity_id"]),
        "selected_artifact_ids": artifacts, "selected_library_assets": normalized_library,
        "anchor_artifact_id": anchor_artifact, "anchor_library_asset_id": anchor_asset}
