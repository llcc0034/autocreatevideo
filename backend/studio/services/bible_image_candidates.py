"""Read-only entity image history from actual execution preparations.

No PromptArtifact, generated graph, Binding key or unit-name interpretation
supplies provenance. Unknown/corrupt targets are explicit integrity errors.
"""
from django.core.exceptions import ValidationError

from studio.models import GeneratedArtifact
from studio.platform_serializers import GeneratedArtifactListSerializer
from studio.serializers import AssetSerializer
from studio.services.bible_reference_contract import BIBLE_IMAGE_GENERATION_STAGE_KEY
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_production_candidates import read_production_media_candidate
from studio.services.generated_artifacts import _artifact_limit
from studio.services.json_contracts import json_contract
from studio.services.production_bible_targets import (bible_target_content_matches, read_bible_candidate_target,
    read_bible_target_contents, is_recipe_variant_candidate)


class BibleImageCandidateProjectionIntegrityError(RuntimeError):
    code = "bible_image_candidate_provenance_unattributable"


def _asset_payload(asset, *, request, owner_id):
    if asset is not None and asset.owner_id not in (None, owner_id):
        raise BibleImageCandidateProjectionIntegrityError("实体素材关系超出项目所有者范围")
    return None if asset is None else AssetSerializer(asset, context={"request": request}).data


def _candidate_identity(artifact):
    try:
        if is_recipe_variant_candidate(artifact):
            return None
        return read_bible_candidate_target(artifact)
    except ValidationError as exc:
        raise BibleImageCandidateProjectionIntegrityError("实体参考图目标合同无法归属") from exc


def _history_row(artifact, *, entity, entity_type, preparation, target, request, content):
    attempt = artifact.generation_attempt
    version = preparation.execution_instance_version
    reference_ids = {str(asset.pk) for asset in entity.reference_assets.all()}
    # Promoting a candidate changes the entity's media relations itself. Source
    # freshness here means authored content; full relation CAS belongs to review.
    source_current = bible_target_content_matches(target, entity, content=content)
    integrity_error = ""
    try:
        read_production_media_candidate(artifact, max_media_bytes=_artifact_limit(artifact.production_run))
    except (ValidationError, OSError, ValueError):
        integrity_error = "bible_image_candidate_provenance_invalid"
    media = GeneratedArtifactListSerializer(artifact, context={"request": request}).data if not integrity_error else {}
    role = ("anchor" if artifact.asset_id and artifact.asset_id == entity.anchor_asset_id else
        "reference" if artifact.asset_id and str(artifact.asset_id) in reference_ids else "unbound")
    return {
        "artifact_id": str(artifact.pk), "production_run_id": str(artifact.production_run_id),
        "stage_run_id": str(artifact.stage_run_id), "generation_attempt_id": str(attempt.pk),
        "generation_task_id": str(artifact.generation_task_id) if artifact.generation_task_id else None,
        "preparation_id": str(preparation.pk), "execution_instance_version_id": str(version.pk),
        "execution_instance_name": version.instance.name, "request_sha256": attempt.request_sha256,
        "workflow_version_id": str(version.workflow_version_id) if version.workflow_version_id else None,
        "model_profile_version_id": str(version.deployment.profile_version_id),
        "model_identifier": version.deployment.profile_version.model_identifier,
        "prompt_profile_version_id": str(version.prompt_profile_version_id) if version.prompt_profile_version_id else None,
        "target_snapshot_sha256": json_contract(target, max_bytes=execution_configuration_max_bytes()).sha256,
        "source_current": source_current, "integrity_status": "invalid" if integrity_error else "valid",
        "integrity_error_code": integrity_error, "variant_group": artifact.variant_group,
        "variant_index": artifact.variant_index, "status": artifact.status, "binding_role": role,
        "materialized_asset": _asset_payload(artifact.asset, request=request, owner_id=artifact.production_run.owner_id),
        "preview_url": media.get("preview_url", ""), "download_url": media.get("download_url", ""),
        "sha256": artifact.sha256, "size_bytes": artifact.size_bytes, "mime_type": artifact.mime_type,
        "created_at": artifact.created_at,
    }


def project_bible_image_candidates(project, *, request):
    entities = {}
    counts = {"characters": 0, "scenes": 0, "props": 0, "total": 0, "with_candidate": 0}
    for kind, plural in (("character", "characters"), ("scene", "scenes"), ("prop", "props")):
        # The authenticated project query owns the common entity/asset prefetch.
        rows = sorted(getattr(project, plural).all(), key=lambda row: (row.slug, str(row.pk)))
        counts[plural] = len(rows)
        for entity in rows:
            entities[(kind, str(entity.pk))] = (entity, [])
    try:
        contents = read_bible_target_contents({key: row[0] for key, row in entities.items()}, project_id=project.pk)
    except ValidationError as exc:
        raise BibleImageCandidateProjectionIntegrityError("实体主图设定无法读取，不能判断候选是否仍有效") from exc
    artifacts = GeneratedArtifact.objects.filter(production_run__project=project,
        stage_run__stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY, kind="image").select_related(
            "production_run__project", "stage_run", "asset", "generation_attempt__preparation__execution_instance_version__instance",
            "generation_attempt__preparation__execution_instance_version__deployment__profile_version",
        ).order_by("-created_at", "-id")
    # One candidate scan, not a full history query and file probe per entity.
    for artifact in artifacts.iterator(chunk_size=100):
        identity = _candidate_identity(artifact)
        if identity is None:
            continue
        preparation, target = identity
        key = (target["scope"]["kind"], target["scope"]["id"])
        if key not in entities:
            raise BibleImageCandidateProjectionIntegrityError("参考图的真实目标已不在项目目录中")
        entity, history = entities[key]
        history.append(_history_row(artifact, entity=entity, entity_type=key[0],
            preparation=preparation, target=target, request=request, content=contents[key]))
    candidates = []
    for (kind, identifier), (entity, history) in entities.items():
        candidates.append({"entity_type": kind, "entity_id": identifier, "entity_name": entity.name,
            "anchor_asset": _asset_payload(entity.anchor_asset, request=request, owner_id=project.owner_id),
            "reference_assets": [_asset_payload(asset, request=request, owner_id=project.owner_id) for asset in
                sorted(entity.reference_assets.all(), key=lambda row: (row.created_at, str(row.pk)))],
            "latest_candidate": history[0] if history else None, "candidate_history": history})
        counts["total"] += 1
        counts["with_candidate"] += bool(history)
    return {"project_id": str(project.pk), "candidates": candidates, "counts": counts}
