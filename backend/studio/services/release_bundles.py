"""Explicit release-candidate selection and immutable manifest sealing."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from studio.models import (
    AssemblyJob,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationTask,
    PromptArtifact,
    ReleaseBundle,
    SeriesProject,
    Shot,
)
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.json_contracts import verify_json_contract
from studio.services.provenance import (
    ProvenanceError,
    verified_assembly_manifest,
    verified_task_manifest,
)
from studio.services.distribution import DistributionError, verified_distribution_manifest
from studio.services.delivery_disclosure import (
    DeliveryDisclosureError,
    resolve_delivery_disclosure,
)
from studio.services.security_boundaries import boundary_int


RELEASE_BUNDLE_SCHEMA = "autoprompt-release-bundle-v1"
PRODUCTION_RELEASE_BUNDLE_SCHEMA = "autoprompt-production-release-bundle-v2"


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")
RELEASE_BUNDLE_SIGNING_SALT = "autoprompt.release-bundle.v1"


class ReleaseBundleError(ValueError):
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _uuid(value: object, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ReleaseBundleError(f"{field} 必须是有效 UUID") from exc


def _task_manifest_binds_shot(
    document: dict, *, task: GenerationTask, shot: Shot, output_index: int,
) -> bool:
    """Trust the signed provenance snapshot, never mutable prompt metadata."""

    generation = document.get("generation") if isinstance(document, dict) else None
    artifact = document.get("artifact") if isinstance(document, dict) else None
    if not isinstance(generation, dict) or not isinstance(artifact, dict):
        return False
    shot_ids = generation.get("shot_ids")
    return (
        str(generation.get("task_id") or "") == str(task.id)
        and str(generation.get("project_id") or "") == str(task.project_id)
        and str(generation.get("project_id") or "") == str(shot.episode.project_id)
        and str(generation.get("episode_id") or "") == str(task.episode_id or "")
        and str(generation.get("episode_id") or "") == str(shot.episode_id)
        and isinstance(shot_ids, list)
        and str(shot.id) in {str(value) for value in shot_ids}
        and artifact.get("output_index") == output_index
        and artifact.get("media_kind") == shot.media_type
    )


def _ordered_assembly_source_pairs(
    document: dict,
) -> tuple[list[tuple[str, str, int]] | None, str | None]:
    """Parse historical task-based provenance for diagnostics only."""

    rows = document.get("source_outputs") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return None, "stale_source_outputs"
    result: list[tuple[str, str, int]] = []
    for expected_clip_index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("clip_index") != expected_clip_index:
            return None, "stale_source_outputs"
        shot_id = str(row.get("shot_id") or "")
        if not shot_id:
            return None, "assembly_source_shot_ids_missing_reassemble"
        try:
            shot_id = str(UUID(shot_id))
        except (TypeError, ValueError, AttributeError):
            return None, "stale_source_outputs"
        task_id = str(row.get("task_id") or "")
        output_index = row.get("output_index")
        if (
            not task_id
            or isinstance(output_index, bool)
            or not isinstance(output_index, int)
            or output_index < 0
        ):
            return None, "stale_source_outputs"
        result.append((shot_id, task_id, output_index))
    return result, None


def normalize_release_selection(value: object) -> dict:
    if not isinstance(value, dict):
        raise ReleaseBundleError("发行选择必须是对象")
    unknown = set(value) - {"shots", "episodes", "series_artifact_id"}
    if unknown:
        raise ReleaseBundleError("发行选择包含未知字段", details={"unknown": sorted(unknown)})
    shot_rows = value.get("shots") or []
    episode_rows = value.get("episodes") or []
    if not isinstance(shot_rows, list) or not isinstance(episode_rows, list):
        raise ReleaseBundleError("shots 与 episodes 必须是数组")
    if len(shot_rows) > boundary_int("security.release.max_shots"):
        raise ReleaseBundleError("发行分镜数量超过安全上限")

    shots: list[dict] = []
    seen_shots: set[str] = set()
    for index, row in enumerate(shot_rows):
        if not isinstance(row, dict) or set(row) != {"shot_id", "artifact_id"}:
            raise ReleaseBundleError(f"shots[{index}] 字段不完整")
        shot_id = _uuid(row.get("shot_id"), f"shots[{index}].shot_id")
        artifact_id = _uuid(row.get("artifact_id"), f"shots[{index}].artifact_id")
        if shot_id in seen_shots:
            raise ReleaseBundleError("同一分镜不能重复选择")
        seen_shots.add(shot_id)
        shots.append({"shot_id": shot_id, "artifact_id": artifact_id})

    episodes: list[dict] = []
    seen_episodes: set[str] = set()
    for index, row in enumerate(episode_rows):
        if not isinstance(row, dict) or set(row) != {"episode_id", "artifact_id"}:
            raise ReleaseBundleError(f"episodes[{index}] 字段不完整")
        episode_id = _uuid(row.get("episode_id"), f"episodes[{index}].episode_id")
        artifact_id = _uuid(row.get("artifact_id"), f"episodes[{index}].artifact_id")
        if episode_id in seen_episodes:
            raise ReleaseBundleError("同一集不能重复选择成片")
        seen_episodes.add(episode_id)
        episodes.append({"episode_id": episode_id, "artifact_id": artifact_id})

    series_artifact_id = value.get("series_artifact_id")
    return {
        "shots": sorted(shots, key=lambda row: row["shot_id"]),
        "episodes": sorted(episodes, key=lambda row: row["episode_id"]),
        "series_artifact_id": (
            _uuid(series_artifact_id, "series_artifact_id") if series_artifact_id else None
        ),
    }


def _managed_generated_artifact_facts(artifact: GeneratedArtifact) -> dict:
    if artifact.storage_backend not in {"managed_local", "local_media"}:
        raise ReleaseBundleError("发行产物尚未进入受管存储")
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / artifact.object_key).resolve()
    if not path.is_relative_to(root):
        raise ReleaseBundleError("发行产物路径越过 MEDIA_ROOT")
    expected_kind = "video" if artifact.kind == GeneratedArtifact.Kind.ASSEMBLY else artifact.kind
    try:
        facts = verified_local_media_facts(
            path,
            expected_kind=expected_kind,
            max_bytes=int(artifact.size_bytes or 0),
        )
    except (ProvenanceError, OSError, ValueError) as exc:
        raise ReleaseBundleError("发行产物真实字节校验失败") from exc
    if (
        facts["sha256"] != artifact.sha256
        or facts["size_bytes"] != artifact.size_bytes
        or facts["mime_type"] != artifact.mime_type
    ):
        raise ReleaseBundleError("发行产物 object key/MIME/size/hash 与清单不一致")
    return facts


def _artifact_task_output_index(artifact: GeneratedArtifact) -> int:
    verify_json_contract(
        artifact.manifest_metadata,
        schema_version=artifact.manifest_schema_version,
        size_bytes=artifact.manifest_metadata_size_bytes,
        sha256=artifact.manifest_metadata_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    output_index = artifact.manifest_metadata.get("output_index")
    if isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
        raise ReleaseBundleError("Comfy GeneratedArtifact 缺少冻结 output_index")
    return output_index


def _artifact_binds_shot(artifact: GeneratedArtifact, shot: Shot) -> tuple[dict | None, int | None]:
    attempt = artifact.generation_attempt
    if attempt is None or attempt.status != GenerationAttempt.Status.SUCCEEDED:
        raise ReleaseBundleError("逐镜发行产物没有成功 GenerationAttempt")
    if artifact.kind != shot.media_type:
        raise ReleaseBundleError("逐镜发行产物类型与 Shot 不一致")
    task = artifact.generation_task
    if task is not None:
        if (
            task.status != GenerationTask.Status.SUCCEEDED
            or task.owner_id != shot.episode.project.owner_id
            or task.project_id != shot.episode.project_id
        ):
            raise ReleaseBundleError("Comfy GeneratedArtifact 任务归属或状态无效")
        output_index = _artifact_task_output_index(artifact)
        document = verified_task_manifest(task, output_index)
        if not _task_manifest_binds_shot(
            document, task=task, shot=shot, output_index=output_index,
        ):
            raise ReleaseBundleError("Comfy GeneratedArtifact 与 Shot 的签名来源不一致")
        return document, output_index
    if attempt.request_sha256 != attempt.request_snapshot_sha256:
        raise ReleaseBundleError("云端 GeneratedArtifact 请求 identity 已漂移")
    verify_json_contract(
        attempt.request_snapshot,
        schema_version=attempt.request_snapshot_schema_version,
        size_bytes=attempt.request_snapshot_size_bytes,
        sha256=attempt.request_snapshot_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    prompt_id = (
        attempt.request_snapshot.get("prompt_artifact_id")
        if isinstance(attempt.request_snapshot, dict) else None
    )
    prompt = PromptArtifact.objects.filter(pk=prompt_id).first()
    if (
        prompt is None
        or prompt.project_id != shot.episode.project_id
        or prompt.episode_id != shot.episode_id
        or prompt.shot_id != shot.id
    ):
        raise ReleaseBundleError("云端 GeneratedArtifact 与 Shot 的冻结 PromptArtifact 不一致")
    return None, None


def propose_release_selection(project: SeriesProject) -> dict:
    shots = list(
        Shot.objects.filter(episode__project=project)
        .select_related("episode__project")
        .order_by("episode__number", "number", "id")
    )
    artifacts = list(GeneratedArtifact.objects.filter(
        production_run__project=project,
        status=GeneratedArtifact.Status.SELECTED,
    ).filter(
        Q(kind=GeneratedArtifact.Kind.ASSEMBLY)
        | Q(stage_run__stage_key="media_generation"),
    ).select_related(
        "generation_attempt", "generation_task", "production_run",
    ).order_by("-created_at"))
    shot_rows = []
    for shot in shots:
        match = None
        for artifact in artifacts:
            if artifact.kind != shot.media_type:
                continue
            try:
                _artifact_binds_shot(artifact, shot)
                _managed_generated_artifact_facts(artifact)
            except (ReleaseBundleError, ProvenanceError, ValidationError, OSError, ValueError):
                continue
            match = artifact
            break
        if match is not None:
            shot_rows.append({"shot_id": str(shot.id), "artifact_id": str(match.id)})
    episode_rows = []
    for artifact in artifacts:
        if artifact.kind != GeneratedArtifact.Kind.ASSEMBLY or artifact.role != "episode_master":
            continue
        episode_id = str((artifact.manifest_metadata or {}).get("episode_id") or "")
        if episode_id:
            episode_rows.append({"episode_id": episode_id, "artifact_id": str(artifact.id)})
    series = next((
        item for item in artifacts
        if item.kind == GeneratedArtifact.Kind.ASSEMBLY and item.role == "series_master"
    ), None)
    return normalize_release_selection({
        "shots": shot_rows,
        "episodes": episode_rows,
        "series_artifact_id": str(series.id) if series else None,
    })


def validate_release_selection(
    *,
    project: SeriesProject,
    owner,
    selection: object,
    require_series_assembly: bool | None = None,
    production_run=None,
) -> dict:
    normalized = normalize_release_selection(selection)
    shot_rows = list(
        Shot.objects.filter(episode__project=project)
        .select_related("episode__project")
        .order_by("episode__number", "number", "id")
    )
    shots_by_id = {str(shot.id): shot for shot in shot_rows}
    selected_by_shot = {row["shot_id"]: row for row in normalized["shots"]}
    missing = sorted(set(shots_by_id) - set(selected_by_shot))
    extra = sorted(set(selected_by_shot) - set(shots_by_id))
    if missing or extra or not shot_rows:
        raise ReleaseBundleError(
            "发行候选必须为项目每个分镜选择一个产物",
            details={"missing_shot_ids": missing, "unknown_shot_ids": extra},
        )

    all_artifact_ids = {row["artifact_id"] for row in normalized["shots"]}
    all_artifact_ids.update(row["artifact_id"] for row in normalized["episodes"])
    if normalized["series_artifact_id"]:
        all_artifact_ids.add(normalized["series_artifact_id"])
    artifact_objects = {
        str(artifact.id): artifact
        for artifact in GeneratedArtifact.objects.filter(id__in=all_artifact_ids).select_related(
            "production_run__project", "generation_attempt__stage_run__production_run",
            "generation_attempt__model_deployment__profile_version", "generation_task",
        )
    }
    artifacts: list[dict] = []
    selected_artifacts_by_shot: dict[str, GeneratedArtifact] = {}
    errors: dict[str, list[str]] = {}
    for shot in shot_rows:
        shot_id = str(shot.id)
        row = selected_by_shot[shot_id]
        artifact = artifact_objects.get(row["artifact_id"])
        reasons: list[str] = []
        if artifact is None:
            reasons.append("artifact_unavailable")
        elif artifact.status != GeneratedArtifact.Status.SELECTED:
            reasons.append("artifact_not_selected")
        elif (
            artifact.production_run is None
            or artifact.production_run.project_id != project.id
            or artifact.production_run.project.owner_id != owner.id
            or (production_run is not None and artifact.production_run_id != production_run.id)
        ):
            reasons.append("artifact_unavailable")
        elif artifact.kind != shot.media_type:
            reasons.append("artifact_kind")
        else:
            try:
                source_document, output_index = _artifact_binds_shot(artifact, shot)
                _managed_generated_artifact_facts(artifact)
                selected_artifacts_by_shot[shot_id] = artifact
                if artifact.generation_task_id:
                    distribution_document = verified_distribution_manifest(
                        artifact.generation_task, output_index,
                    )
                    source_artifact = source_document.get("artifact") or {}
                    source_integrity = source_document.get("integrity") or {}
                    published = distribution_document.get("artifact") or {}
                    integrity = distribution_document.get("integrity") or {}
                    disclosure = distribution_document.get("disclosure") or {}
                    release = distribution_document.get("release") or {}
                    artifact_row = {
                        "artifact_sha256": str(published.get("sha256") or ""),
                        "provenance_sha256": str(integrity.get("payload_sha256") or ""),
                        "source_artifact_sha256": str(source_artifact.get("sha256") or ""),
                        "source_provenance_sha256": str(source_integrity.get("payload_sha256") or ""),
                        "distribution_release_id": str(release.get("release_id") or ""),
                        "distribution_model_key": str(release.get("model_key") or ""),
                        "distribution_model": str(release.get("model") or ""),
                        "distribution_label": str(disclosure.get("label") or ""),
                        "distribution_version": str(disclosure.get("version") or ""),
                        "source_media": [
                            {
                                "asset_id": str(source.get("asset_id") or "") or None,
                                "role": str(source.get("role") or "reference")[:80],
                                "source_sha256": str(source.get("source_sha256") or ""),
                                "submitted_sha256": str(source.get("submitted_sha256") or ""),
                            }
                            for source in (source_document.get("source_media") or [])
                            if isinstance(source, dict)
                        ],
                    }
                else:
                    deployment = artifact.generation_attempt.model_deployment
                    profile = deployment.profile_version
                    disclosure = resolve_delivery_disclosure(
                        model=profile.model_identifier,
                        model_key=f"{profile.profile_key}:v{profile.version}",
                        media_kind=artifact.kind,
                    )
                    artifact_row = {
                        "artifact_sha256": artifact.sha256,
                        "provenance_sha256": artifact.manifest_metadata_sha256,
                        "source_artifact_sha256": artifact.sha256,
                        "source_provenance_sha256": artifact.manifest_metadata_sha256,
                        "distribution_release_id": "",
                        "distribution_model_key": disclosure.model_key,
                        "distribution_model": disclosure.model,
                        "distribution_label": disclosure.label,
                        "distribution_version": "managed-original-v1",
                        "source_media": [],
                    }
                artifacts.append({
                    "shot_id": shot_id,
                    "episode_id": str(shot.episode_id),
                    "media_kind": shot.media_type,
                    "generated_artifact_id": str(artifact.id),
                    **artifact_row,
                })
            except (DistributionError, DeliveryDisclosureError):
                reasons.append("managed_distribution_invalid")
            except (
                ReleaseBundleError, ProvenanceError, ValidationError,
                OSError, ValueError, TypeError, IndexError,
            ):
                reasons.append("provenance_invalid")
        if reasons:
            errors[shot_id] = reasons
    if errors:
        details: dict = {"shots": errors}
        missing_distributions = [
            row for row in normalized["shots"]
            if "managed_distribution_invalid" in errors.get(row["shot_id"], [])
        ]
        if missing_distributions:
            details["managed_distribution_actions"] = [
                {
                    "shot_id": row["shot_id"],
                    "artifact_id": row["artifact_id"],
                }
                for row in missing_distributions
            ]
            raise ReleaseBundleError(
                "发行候选分镜缺少或未通过公开发行副本；"
                "请前往生产工作流任务详情生成公开副本后重新封存",
                details=details,
            )
        raise ReleaseBundleError("发行候选包含无效分镜产物", details=details)

    episode_ids = {str(shot.episode_id) for shot in shot_rows}
    selected_episodes = {row["episode_id"]: row for row in normalized["episodes"]}
    missing_episodes = sorted(episode_ids - set(selected_episodes))
    extra_episodes = sorted(set(selected_episodes) - episode_ids)
    if missing_episodes or extra_episodes:
        raise ReleaseBundleError(
            "每个含分镜的剧集都必须选择对应成片",
            details={"missing_episode_ids": missing_episodes, "unknown_episode_ids": extra_episodes},
        )

    assembly_artifacts: list[dict] = []

    def verify_assembly(master_artifact_id: str, *, scope: str, episode_id: str | None) -> None:
        master = artifact_objects.get(master_artifact_id)
        job = None
        if master is not None:
            try:
                verify_json_contract(
                    master.manifest_metadata,
                    schema_version=master.manifest_schema_version,
                    size_bytes=master.manifest_metadata_size_bytes,
                    sha256=master.manifest_metadata_sha256,
                    max_bytes=_stage_snapshot_max_bytes(),
                )
                job = AssemblyJob.objects.select_related("episode").filter(
                    pk=master.manifest_metadata.get("assembly_job_id"),
                    owner=owner,
                    project=project,
                ).first()
            except (ValidationError, TypeError, ValueError):
                job = None
        reasons: list[str] = []
        if master is None or job is None:
            reasons.append("assembly_artifact_unavailable")
        elif (
            master.status != GeneratedArtifact.Status.SELECTED
            or master.kind != GeneratedArtifact.Kind.ASSEMBLY
            or master.production_run is None
            or master.production_run.project_id != project.id
            or master.production_run.project.owner_id != owner.id
            or (production_run is not None and master.production_run_id != production_run.id)
        ):
            reasons.append("assembly_artifact_unavailable")
        elif job.status != AssemblyJob.Status.SUCCEEDED or job.dry_run:
            reasons.append("assembly_not_succeeded")
        elif job.scope != scope or str(job.episode_id or "") != str(episode_id or ""):
            reasons.append("assembly_context")
        else:
            try:
                master_facts = _managed_generated_artifact_facts(master)
                document = verified_assembly_manifest(job)
                if (document.get("artifact") or {}).get("sha256") != master_facts["sha256"]:
                    reasons.append("assembly_artifact_hash")
                source_items = list(job.source_items.select_related(
                    "shot", "generation_task", "generated_artifact",
                ).order_by("ordinal"))
                expected_shots = [
                    shot for shot in shot_rows
                    if (
                        job.source_contract_version >= 3
                        or shot.media_type == Shot.MediaType.VIDEO
                    )
                    and (scope == AssemblyJob.Scope.SERIES or str(shot.episode_id) == str(episode_id))
                ]
                if [str(item.shot_id) for item in source_items] != [str(shot.id) for shot in expected_shots]:
                    reasons.append("stale_source_outputs")
                provenance_rows = document.get("source_outputs")
                if not isinstance(provenance_rows, list) or len(provenance_rows) != len(source_items):
                    reasons.append("stale_source_outputs")
                    provenance_rows = []
                for index, (source, shot) in enumerate(zip(source_items, expected_shots, strict=True)):
                    selected = selected_artifacts_by_shot.get(str(shot.id))
                    row = provenance_rows[index] if index < len(provenance_rows) else {}
                    if (
                        selected is None
                        or source.sha256 != selected.sha256
                        or source.size_bytes != selected.size_bytes
                        or source.mime_type != selected.mime_type
                        or str(row.get("shot_id") or "") != str(shot.id)
                        or str(row.get("artifact_sha256") or "") != source.sha256
                        or (
                            job.source_contract_version >= 3
                            and (
                                source.media_kind != shot.media_type
                                or str(row.get("media_kind") or "") != shot.media_type
                            )
                        )
                    ):
                        reasons.append("assembly_source_artifact_drift")
                        continue
                    if source.generated_artifact_id:
                        if (
                            source.generated_artifact_id != selected.id
                            or str(row.get("generated_artifact_id") or "") != str(selected.id)
                        ):
                            reasons.append("assembly_source_artifact_drift")
                    elif (
                        source.generation_task_id != selected.generation_task_id
                        or str(row.get("task_id") or "") != str(selected.generation_task_id)
                    ):
                        reasons.append("assembly_source_artifact_drift")
                artifact = document.get("artifact") or {}
                integrity = document.get("integrity") or {}
                assembly_artifacts.append({
                    "assembly_id": str(job.id),
                    "generated_artifact_id": str(master.id),
                    "scope": scope,
                    "episode_id": str(job.episode_id) if job.episode_id else None,
                    "artifact_sha256": str(artifact.get("sha256") or ""),
                    "provenance_sha256": str(integrity.get("payload_sha256") or ""),
                })
            except (ProvenanceError, OSError, ValueError, TypeError):
                reasons.append("provenance_invalid")
        if reasons:
            raise ReleaseBundleError(
                "发行候选包含无效或过期成片",
                details={"artifact_id": master_artifact_id, "reasons": sorted(set(reasons))},
            )

    for episode_id, row in selected_episodes.items():
        verify_assembly(
            row["artifact_id"], scope=AssemblyJob.Scope.EPISODE, episode_id=episode_id,
        )
    if require_series_assembly is None:
        require_series_assembly = project.status == SeriesProject.Status.COMPLETED
    if require_series_assembly and not normalized["series_artifact_id"]:
        raise ReleaseBundleError("已完结项目必须选择整剧成片")
    if normalized["series_artifact_id"]:
        verify_assembly(
            normalized["series_artifact_id"], scope=AssemblyJob.Scope.SERIES, episode_id=None,
        )

    return {
        "selection": normalized,
        "artifacts": sorted(artifacts, key=lambda row: row["shot_id"]),
        "assemblies": sorted(
            assembly_artifacts,
            key=lambda row: (row["scope"], row.get("episode_id") or ""),
        ),
    }


def create_release_draft(*, project: SeriesProject, owner, created_by, title: str = "", notes: str = "") -> ReleaseBundle:
    with transaction.atomic():
        locked_project = SeriesProject.objects.select_for_update().get(pk=project.pk)
        latest = ReleaseBundle.objects.filter(project=locked_project).aggregate(Max("version"))[
            "version__max"
        ] or 0
        version = latest + 1
        selection = propose_release_selection(locked_project)
        return ReleaseBundle.objects.create(
            owner=owner,
            project=locked_project,
            created_by=created_by,
            version=version,
            title=title.strip() or f"{locked_project.title} · 发行候选 v{version}",
            notes=notes,
            selection=selection,
        )


def seal_release_bundle(bundle: ReleaseBundle) -> ReleaseBundle:
    with transaction.atomic():
        locked = ReleaseBundle.objects.select_for_update().select_related("project", "owner").get(
            pk=bundle.pk,
        )
        if locked.status != ReleaseBundle.Status.DRAFT:
            raise ReleaseBundleError("只有草稿发行候选可以封存")
        verified = validate_release_selection(
            project=locked.project,
            owner=locked.owner,
            selection=locked.selection,
            require_series_assembly=False if locked.production_run_id else None,
            production_run=locked.production_run,
        )
        sealed_at = timezone.now()
        body = {
            "schema": (
                PRODUCTION_RELEASE_BUNDLE_SCHEMA
                if locked.production_run_id else RELEASE_BUNDLE_SCHEMA
            ),
            "bundle_id": str(locked.id),
            "project_id": str(locked.project_id),
            "owner_id": str(locked.owner_id),
            "version": locked.version,
            "title": locked.title,
            "sealed_at": sealed_at.isoformat(),
            **({
                "production_run_id": str(locked.production_run_id),
                "delivery_scope": "episode_masters",
            } if locked.production_run_id else {}),
            **verified,
        }
        payload_sha256 = _canonical_sha256(body)
        document = {
            **body,
            "integrity": {
                "payload_sha256": payload_sha256,
                "signature": signing.Signer(salt=RELEASE_BUNDLE_SIGNING_SALT).sign(payload_sha256),
            },
        }
        locked.selection = verified["selection"]
        locked.manifest = document
        locked.manifest_sha256 = payload_sha256
        locked.sealed_at = sealed_at
        locked.status = ReleaseBundle.Status.SEALED
        locked.save(update_fields=(
            "selection", "manifest", "manifest_sha256", "sealed_at", "status", "updated_at",
        ))
        return locked


def verified_release_bundle(bundle: ReleaseBundle) -> dict:
    document = deepcopy(bundle.manifest)
    if (
        not isinstance(document, dict)
        or document.get("schema") not in {RELEASE_BUNDLE_SCHEMA, PRODUCTION_RELEASE_BUNDLE_SCHEMA}
    ):
        raise ReleaseBundleError("发行清单不存在或版本无效")
    integrity = document.pop("integrity", None)
    if not isinstance(integrity, dict):
        raise ReleaseBundleError("发行清单缺少完整性信封")
    payload_sha256 = _canonical_sha256(document)
    if payload_sha256 != bundle.manifest_sha256 or payload_sha256 != integrity.get("payload_sha256"):
        raise ReleaseBundleError("发行清单哈希不匹配")
    try:
        signed = signing.Signer(salt=RELEASE_BUNDLE_SIGNING_SALT).unsign(
            str(integrity.get("signature") or ""),
        )
    except signing.BadSignature as exc:
        raise ReleaseBundleError("发行清单签名无效") from exc
    if signed != payload_sha256:
        raise ReleaseBundleError("发行清单签名未覆盖当前内容")
    if (
        str(document.get("bundle_id")) != str(bundle.id)
        or str(document.get("project_id")) != str(bundle.project_id)
        or int(document.get("version") or 0) != bundle.version
    ):
        raise ReleaseBundleError("发行清单与数据库对象不匹配")
    if bundle.production_run_id and (
        document.get("schema") != PRODUCTION_RELEASE_BUNDLE_SCHEMA
        or str(document.get("production_run_id") or "") != str(bundle.production_run_id)
        or document.get("delivery_scope") != "episode_masters"
        or document.get("selection", {}).get("series_artifact_id") is not None
    ):
        raise ReleaseBundleError("ProductionRun 发行清单绑定无效")
    return {**document, "integrity": integrity, "verification": {"status": "verified"}}


def live_verified_release_bundle(bundle: ReleaseBundle) -> dict:
    """Verify both the sealed signature and every currently selected artifact.

    A signature only proves what was sealed.  This live verification also
    re-checks the managed distribution copies and source bindings,
    assemblies and the exact sealed selection before claiming the release is
    still usable.
    """

    sealed_document = verified_release_bundle(bundle)
    live = validate_release_selection(
        project=bundle.project,
        owner=bundle.owner,
        selection=bundle.selection,
        require_series_assembly=False if bundle.production_run_id else None,
        production_run=bundle.production_run,
    )
    if (
        live["selection"] != sealed_document.get("selection")
        or live["artifacts"] != sealed_document.get("artifacts")
        or live["assemblies"] != sealed_document.get("assemblies")
    ):
        raise ReleaseBundleError("发行候选产物已变化，必须重新创建并封存发行清单")
    return {
        **sealed_document,
        "verification": {
            "status": "verified",
            "signature_verified": True,
            "live_verified": True,
            "verified_at": timezone.now().isoformat(),
        },
    }


def _mark_release_bundle_released_transaction(bundle: ReleaseBundle) -> ReleaseBundle:
    with transaction.atomic():
        # One project mutex serializes competing release transitions before the
        # conditional unique constraint is touched. Lock ordering matches draft
        # creation (project first), avoiding v1/v2 cross-lock deadlocks.
        locked_project = SeriesProject.objects.select_for_update().get(pk=bundle.project_id)
        locked = ReleaseBundle.objects.select_for_update().get(
            pk=bundle.pk, project=locked_project,
        )
        if locked.status != ReleaseBundle.Status.SEALED:
            raise ReleaseBundleError("只有已封存候选可以标记为已发行")
        if ReleaseBundle.objects.filter(
            project=locked_project,
            version__gt=locked.version,
            status__in=(ReleaseBundle.Status.SEALED, ReleaseBundle.Status.RELEASED),
        ).exists():
            raise ReleaseBundleError("已存在更高版本的封存或已发行清单，不能回退发行旧版本")
        # Release transition reuses the same live verification exposed by the
        # verification endpoint; the UI and state transition therefore cannot
        # disagree about whether bytes are still releasable.
        live_verified_release_bundle(locked)
        ReleaseBundle.objects.filter(
            project=locked_project, status=ReleaseBundle.Status.RELEASED,
        ).exclude(pk=locked.pk).update(status=ReleaseBundle.Status.SUPERSEDED, updated_at=timezone.now())
        locked.status = ReleaseBundle.Status.RELEASED
        locked.released_at = timezone.now()
        locked.save(update_fields=("status", "released_at", "updated_at"))
        return locked


def mark_release_bundle_released(bundle: ReleaseBundle) -> ReleaseBundle:
    try:
        return _mark_release_bundle_released_transaction(bundle)
    except IntegrityError as exc:
        # Project row locking prevents this on databases with real row locks.
        # Keep a stable API failure instead of leaking a conditional-unique
        # constraint as HTTP 500 on weaker/test backends or operational races.
        raise ReleaseBundleError(
            "发行状态发生并发冲突，请刷新版本列表后重试",
            details={"code": "release_transition_conflict"},
        ) from exc


def task_is_release_evidence(task: GenerationTask) -> bool:
    for bundle in ReleaseBundle.objects.filter(
        project=task.project,
        status__in=(
            ReleaseBundle.Status.SEALED,
            ReleaseBundle.Status.RELEASED,
            ReleaseBundle.Status.SUPERSEDED,
        ),
    ).only("manifest").iterator():
        manifest = bundle.manifest if isinstance(bundle.manifest, dict) else {}
        artifact_ids = [
            row.get("generated_artifact_id")
            for row in (manifest.get("artifacts") or [])
            if isinstance(row, dict) and row.get("generated_artifact_id")
        ]
        if GeneratedArtifact.objects.filter(
            id__in=artifact_ids,
            generation_task=task,
        ).exists():
            return True
    return False


def asset_is_release_evidence(asset) -> bool:
    bundles = ReleaseBundle.objects.filter(
        status__in=(
            ReleaseBundle.Status.SEALED,
            ReleaseBundle.Status.RELEASED,
            ReleaseBundle.Status.SUPERSEDED,
        ),
    )
    # Global library assets (owner=None) may legitimately be referenced by any
    # tenant's sealed manifest. Tenant-owned assets remain strictly scoped.
    if asset.owner_id:
        bundles = bundles.filter(owner_id=asset.owner_id)
    for bundle in bundles.only("manifest").iterator():
        manifest = bundle.manifest if isinstance(bundle.manifest, dict) else {}
        for artifact in manifest.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            if any(
                isinstance(source, dict) and str(source.get("asset_id")) == str(asset.id)
                for source in (artifact.get("source_media") or [])
            ):
                return True
    return False
