"""One library materialization writer for archived production image candidates.

Registration is not selection or adoption. Callers authorize/lock their original
Run and supply a file guard enclosing their outer database transaction.
"""
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import transaction

from studio.models import Asset, GeneratedArtifact
from studio.services.bible_reference_contract import BIBLE_IMAGE_GENERATION_PURPOSE
from studio.services.execution_media_sources import resolve_execution_asset_media
from studio.services.execution_production_candidates import read_production_media_candidate
from studio.services.generated_artifacts import _artifact_limit, verified_local_media_facts


def _origin(artifact):
    preparation = artifact.generation_attempt.preparation
    purpose = BIBLE_IMAGE_GENERATION_PURPOSE if artifact.stage_run.stage_key == "bible_image_generation" else "production_media"
    return {"schema_version": 3, "generation_purpose": purpose,
        "production_run_id": str(artifact.production_run_id), "generated_artifact_id": str(artifact.pk),
        "preparation_id": str(preparation.pk),
        "execution_instance_version_id": str(preparation.execution_instance_version_id),
        "request_sha256": preparation.request_sha256,
        "generation_attempt_id": str(artifact.generation_attempt_id),
        "generation_task_id": str(artifact.generation_task_id) if artifact.generation_task_id else None,
        "source_sha256": artifact.sha256, "size_bytes": artifact.size_bytes, "mime_type": artifact.mime_type,
        "source_manifest_sha256": artifact.manifest_metadata_sha256}


def read_registered_production_image(artifact):
    """Verify the original Asset and archived bytes, without repairing either."""
    verified = read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(artifact.production_run))
    if artifact.kind != "image" or not artifact.asset_id or verified["source_kind"] != "execution":
        raise ValidationError("图片候选尚未登记为原Asset")
    asset = artifact.asset
    if asset.owner_id != artifact.production_run.owner_id or asset.metadata != _origin(artifact):
        raise ValidationError("已登记图片Asset与原生成来源不一致")
    actual = resolve_execution_asset_media(owner=artifact.production_run.owner, asset_id=asset.pk,
        expected_kind="image", max_media_bytes=_artifact_limit(artifact.production_run))
    if (actual.asset.mime_type != actual.media_facts["mime_type"]
            or any(actual.media_facts[key] != verified["media_facts"][key] for key in ("sha256", "size_bytes", "mime_type"))):
        raise ValidationError("已登记图片Asset与原归档字节不一致")
    return actual.asset


def register_production_image_asset(*, artifact, kind, name, description="", asset_guard):
    """Copy once into the existing tenant Asset store; replay never copies again."""
    if not transaction.get_connection().in_atomic_block:
        raise ValidationError("素材登记需要原任务事务及文件回滚边界")
    artifact = GeneratedArtifact.objects.select_for_update().select_related(
        "production_run__project", "production_run__owner", "asset", "generation_attempt__preparation", "stage_run"
    ).get(pk=artifact.pk)
    if artifact.asset_id:
        return read_registered_production_image(artifact)
    verified = read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(artifact.production_run))
    if artifact.kind != "image" or verified["source_kind"] != "execution":
        raise ValidationError("登记需要真实生成的图片候选")
    if kind not in {"character", "scene", "prop", "output_image"} or not isinstance(name, str) or not name.strip():
        raise ValidationError("图片入库需要明确类型与名称")
    facts, source = verified["media_facts"], verified["path"]
    asset = Asset(owner=artifact.production_run.owner, style=artifact.production_run.project.style,
        kind=kind, name=name, description=description, prompt_hint="",
        source_info=f"ProductionRun {artifact.production_run_id} 模型生成 · 候选 {artifact.variant_index}",
        mime_type=facts["mime_type"], checksum=facts["sha256"], metadata=_origin(artifact))
    try:
        with source.open("rb") as handle:
            asset.file.save(f"{artifact.pk}{source.suffix.lower()[:16] or '.media'}", File(handle), save=False)
        asset_guard.track(asset.file)
        asset.full_clean()
        asset.save()
        copied = verified_local_media_facts(Path(asset.file.path).resolve(), expected_kind="image",
            max_bytes=facts["size_bytes"])
    except Exception:
        asset_guard.track(asset.file)
        raise
    if any(copied[key] != facts[key] for key in ("sha256", "size_bytes", "mime_type")):
        raise ValidationError("生成图片入库后二次校验失败")
    artifact.asset = asset
    artifact.save(update_fields=("asset", "updated_at"))
    return asset
