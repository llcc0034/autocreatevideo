from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import (
    CharacterBible,
    GeneratedArtifact,
    PromptArtifact,
    PropBible,
    SceneBible,
    SeriesProject,
)
from studio.platform_serializers import GeneratedArtifactListSerializer
from studio.serializers import AssetSerializer
from studio.services.bible_reference_contract import (
    BIBLE_IMAGE_GENERATION_STAGE_KEY,
    parse_bible_candidate_unit_key,
    validate_bible_prompt_target,
)
from studio.services.json_contracts import verify_json_contract
from studio.services.release_bundles import (
    ReleaseBundleError,
    _managed_generated_artifact_facts,
)
from studio.services.security_boundaries import boundary_int


class BibleImageCandidateProjectionIntegrityError(RuntimeError):
    code = "bible_image_candidate_provenance_unattributable"


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _asset_payload(asset, *, request) -> dict[str, Any] | None:
    if asset is None:
        return None
    return AssetSerializer(asset, context={"request": request}).data


def _source_state(
    *, prompt: PromptArtifact | None, entity_type: str, entity,
) -> tuple[bool, str, str]:
    if prompt is None:
        return False, "invalid", "bible_image_prompt_missing"
    from studio.services.production_runs import (
        ProductionRunConflict,
        _assert_bible_prompt_source_current,
    )

    try:
        _assert_bible_prompt_source_current(
            prompt=prompt,
            entity_type=entity_type,
            entity=entity,
        )
    except ProductionRunConflict as exc:
        return False, "valid", exc.code
    except ValidationError:
        return False, "invalid", "bible_image_prompt_integrity_invalid"
    return True, "valid", ""


def _provenance_state(
    *, artifact: GeneratedArtifact, prompt: PromptArtifact | None, project: SeriesProject,
) -> tuple[bool, str]:
    if prompt is None or artifact.generation_attempt is None:
        return False, "bible_image_candidate_provenance_invalid"
    from studio.services.production_runs import _assert_bible_candidate_provenance

    try:
        _assert_bible_candidate_provenance(
            artifact=artifact,
            attempt=artifact.generation_attempt,
            prompt=prompt,
            project=project,
        )
    except (ValidationError, RuntimeError, ValueError, OSError):
        return False, "bible_image_candidate_provenance_invalid"
    return True, ""


def _candidate_target_state(
    artifact: GeneratedArtifact,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Verify manifest+target and retain an independently frozen entity identity."""

    attempt = artifact.generation_attempt
    identity = None
    if attempt is not None:
        try:
            identity = parse_bible_candidate_unit_key(attempt.unit_key)
        except ValidationError:
            identity = None
    metadata = artifact.manifest_metadata if isinstance(
        artifact.manifest_metadata, dict,
    ) else {}
    target = None
    error_code = ""
    try:
        verify_json_contract(
            metadata,
            schema_version=artifact.manifest_schema_version,
            size_bytes=artifact.manifest_metadata_size_bytes,
            sha256=artifact.manifest_metadata_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        target = validate_bible_prompt_target(metadata.get("target"))
    except ValidationError:
        error_code = "bible_image_candidate_manifest_invalid"
    if identity is None and target is None:
        raise BibleImageCandidateProjectionIntegrityError(
            "Bible candidate 同时缺少有效 attempt target 与 manifest target",
        )
    if identity is None:
        error_code = error_code or "bible_image_candidate_attempt_identity_invalid"
    elif target is not None and (
        target["entity_type"] != identity["entity_type"]
        or target["entity_id"] != identity["entity_id"]
        or artifact.variant_group != identity["variant_group"]
    ):
        error_code = "bible_image_candidate_target_conflict"
    return identity, target, error_code


def _history_for_entity(
    *,
    project: SeriesProject,
    entity_type: str,
    entity,
    request,
) -> list[dict[str, Any]]:
    rows = []
    artifacts = GeneratedArtifact.objects.filter(
        production_run__project=project,
        stage_run__stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
        kind=GeneratedArtifact.Kind.IMAGE,
        role="bible_reference_candidate",
    ).select_related(
        "production_run",
        "stage_run",
        "generation_attempt__model_deployment__profile_version",
        "generation_attempt__prompt_profile_version",
        "generation_task__generated_workflow__prompt_artifact",
        "generation_task__generated_workflow__workflow_template_version",
        "asset",
    ).order_by("-created_at", "-id")
    reference_ids = {
        str(item) for item in entity.reference_assets.values_list("id", flat=True)
    }
    for artifact in artifacts:
        metadata = artifact.manifest_metadata if isinstance(artifact.manifest_metadata, dict) else {}
        identity, target, integrity_error_code = _candidate_target_state(artifact)
        attributed_type = (
            identity["entity_type"] if identity is not None else target["entity_type"]
        )
        attributed_id = (
            identity["entity_id"] if identity is not None else target["entity_id"]
        )
        if attributed_type != entity_type or attributed_id != str(entity.id):
            continue
        attempt = artifact.generation_attempt
        prompt = None
        workflow_version_id = None
        if artifact.generation_task_id:
            generated_workflow = getattr(artifact.generation_task, "generated_workflow", None)
            prompt = getattr(generated_workflow, "prompt_artifact", None)
            workflow_version_id = getattr(generated_workflow, "workflow_template_version_id", None)
        if prompt is None:
            prompt = PromptArtifact.objects.select_related("prompt_profile_version").filter(
                pk=metadata.get("prompt_artifact_id"),
            ).first()
        provenance_valid, provenance_error = _provenance_state(
            artifact=artifact,
            prompt=prompt,
            project=project,
        )
        if not provenance_valid:
            integrity_error_code = integrity_error_code or provenance_error
        source_current = False
        if not integrity_error_code:
            source_current, source_integrity, source_error = _source_state(
                prompt=prompt,
                entity_type=entity_type,
                entity=entity,
            )
            if source_integrity == "invalid":
                integrity_error_code = source_error
        media_integrity_valid = True
        try:
            _managed_generated_artifact_facts(artifact)
        except (ReleaseBundleError, OSError, TypeError, ValueError):
            media_integrity_valid = False
            integrity_error_code = (
                integrity_error_code
                or "bible_image_candidate_media_integrity_invalid"
            )
        source_snapshot_sha256 = (
            str(target.get("source_snapshot_sha256") or "") if target else ""
        )
        if source_snapshot_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", source_snapshot_sha256,
        ):
            source_snapshot_sha256 = ""
            integrity_error_code = integrity_error_code or "bible_image_candidate_target_invalid"
        binding_role = "unbound"
        if artifact.asset_id and str(artifact.asset_id) == str(entity.anchor_asset_id or ""):
            binding_role = "anchor"
        elif artifact.asset_id and str(artifact.asset_id) in reference_ids:
            binding_role = "reference"
        media = (
            GeneratedArtifactListSerializer(
                artifact,
                context={"request": request},
            ).data
            if media_integrity_valid else {}
        )
        rows.append({
            "artifact_id": str(artifact.id),
            "production_run_id": str(artifact.production_run_id),
            "stage_run_id": str(artifact.stage_run_id),
            "generation_attempt_id": (
                str(artifact.generation_attempt_id) if artifact.generation_attempt_id else None
            ),
            "generation_task_id": (
                str(artifact.generation_task_id) if artifact.generation_task_id else None
            ),
            "prompt_artifact_id": str(prompt.id) if prompt else None,
            "workflow_version_id": str(workflow_version_id) if workflow_version_id else None,
            "model_profile_version_id": (
                str(attempt.model_deployment.profile_version_id)
                if attempt and attempt.model_deployment_id else None
            ),
            "model_identifier": (
                attempt.model_deployment.profile_version.model_identifier
                if attempt and attempt.model_deployment_id else ""
            ),
            "prompt_profile_version_id": (
                str(prompt.prompt_profile_version_id)
                if prompt and prompt.prompt_profile_version_id else None
            ),
            "prompt_profile_binding_key": (
                prompt.prompt_profile_binding_key if prompt else ""
            ),
            "prompt_profile_contract_sha256": (
                prompt.prompt_profile_contract_sha256 if prompt else ""
            ),
            "source_snapshot_sha256": source_snapshot_sha256,
            "source_current": source_current,
            "integrity_status": "invalid" if integrity_error_code else "valid",
            "integrity_error_code": integrity_error_code,
            "variant_group": artifact.variant_group,
            "variant_index": artifact.variant_index,
            "status": artifact.status,
            "binding_role": binding_role,
            "materialized_asset": _asset_payload(artifact.asset, request=request),
            "preview_url": media.get("preview_url", "") if not integrity_error_code else "",
            "download_url": media.get("download_url", "") if not integrity_error_code else "",
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "mime_type": artifact.mime_type,
            "created_at": artifact.created_at,
        })
    return rows


def project_bible_image_candidates(
    project: SeriesProject,
    *,
    request,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    counts = {"characters": 0, "scenes": 0, "props": 0, "total": 0, "with_candidate": 0}
    for entity_type, plural, model in (
        ("character", "characters", CharacterBible),
        ("scene", "scenes", SceneBible),
        ("prop", "props", PropBible),
    ):
        entities = model.objects.filter(project=project).select_related(
            "anchor_asset",
        ).prefetch_related("reference_assets").order_by("slug", "id")
        for entity in entities:
            history = _history_for_entity(
                project=project,
                entity_type=entity_type,
                entity=entity,
                request=request,
            )
            candidates.append({
                "entity_type": entity_type,
                "entity_id": str(entity.id),
                "entity_name": entity.name,
                "anchor_asset": _asset_payload(entity.anchor_asset, request=request),
                "reference_assets": [
                    _asset_payload(asset, request=request)
                    for asset in entity.reference_assets.all().order_by("created_at", "id")
                ],
                "latest_candidate": history[0] if history else None,
                "candidate_history": history,
            })
            counts[plural] += 1
            counts["total"] += 1
            if history:
                counts["with_candidate"] += 1
    return {
        "project_id": str(project.id),
        "candidates": candidates,
        "counts": counts,
    }
