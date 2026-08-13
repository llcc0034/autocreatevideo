"""Explicit release-candidate selection and immutable manifest sealing."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from uuid import UUID

from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from studio.media_signing import classify_task_output
from studio.models import AssemblyJob, GenerationTask, ReleaseBundle, SeriesProject, Shot
from studio.services.provenance import (
    ProvenanceError,
    verified_assembly_manifest,
    verified_task_manifest,
)
from studio.services.distribution import DistributionError, verified_distribution_manifest


RELEASE_BUNDLE_SCHEMA = "autoprompt-release-bundle-v1"
RELEASE_BUNDLE_SIGNING_SALT = "autoprompt.release-bundle.v1"
MAX_RELEASE_SHOTS = 10_000


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
    """Return exact shot/task/output order with an actionable failure code."""

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
    unknown = set(value) - {"shots", "episodes", "series_assembly_id"}
    if unknown:
        raise ReleaseBundleError("发行选择包含未知字段", details={"unknown": sorted(unknown)})
    shot_rows = value.get("shots") or []
    episode_rows = value.get("episodes") or []
    if not isinstance(shot_rows, list) or not isinstance(episode_rows, list):
        raise ReleaseBundleError("shots 与 episodes 必须是数组")
    if len(shot_rows) > MAX_RELEASE_SHOTS:
        raise ReleaseBundleError("发行分镜数量超过安全上限")

    shots: list[dict] = []
    seen_shots: set[str] = set()
    for index, row in enumerate(shot_rows):
        if not isinstance(row, dict) or set(row) != {"shot_id", "task_id", "output_index"}:
            raise ReleaseBundleError(f"shots[{index}] 字段不完整")
        shot_id = _uuid(row.get("shot_id"), f"shots[{index}].shot_id")
        task_id = _uuid(row.get("task_id"), f"shots[{index}].task_id")
        output_index = row.get("output_index")
        if isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
            raise ReleaseBundleError(f"shots[{index}].output_index 必须是非负整数")
        if shot_id in seen_shots:
            raise ReleaseBundleError("同一分镜不能重复选择")
        seen_shots.add(shot_id)
        shots.append({"shot_id": shot_id, "task_id": task_id, "output_index": output_index})

    episodes: list[dict] = []
    seen_episodes: set[str] = set()
    for index, row in enumerate(episode_rows):
        if not isinstance(row, dict) or set(row) != {"episode_id", "assembly_id"}:
            raise ReleaseBundleError(f"episodes[{index}] 字段不完整")
        episode_id = _uuid(row.get("episode_id"), f"episodes[{index}].episode_id")
        assembly_id = _uuid(row.get("assembly_id"), f"episodes[{index}].assembly_id")
        if episode_id in seen_episodes:
            raise ReleaseBundleError("同一集不能重复选择成片")
        seen_episodes.add(episode_id)
        episodes.append({"episode_id": episode_id, "assembly_id": assembly_id})

    series_assembly_id = value.get("series_assembly_id")
    return {
        "shots": sorted(shots, key=lambda row: row["shot_id"]),
        "episodes": sorted(episodes, key=lambda row: row["episode_id"]),
        "series_assembly_id": (
            _uuid(series_assembly_id, "series_assembly_id") if series_assembly_id else None
        ),
    }


def propose_release_selection(project: SeriesProject) -> dict:
    shots = list(
        Shot.objects.filter(episode__project=project)
        .select_related("episode")
        .order_by("episode__number", "number", "id")
    )
    tasks = list(
        GenerationTask.objects.filter(
            owner=project.owner, project=project,
            status=GenerationTask.Status.SUCCEEDED,
        )
        .select_related(
            "episode", "generated_workflow__prompt_artifact",
            "generated_workflow__template",
        )
        .order_by("-finished_at", "-created_at")
    )
    verified_outputs: dict[tuple[object, int], dict] = {}
    for task in tasks:
        for output_index, item in enumerate(task.outputs or []):
            if classify_task_output(item) != task.kind:
                continue
            try:
                verified_outputs[(task.id, output_index)] = verified_task_manifest(
                    task, output_index,
                )
            except (ProvenanceError, OSError, ValueError, TypeError, IndexError):
                continue
    selected_shots: list[dict] = []
    for shot in shots:
        match = None
        for task in tasks:
            if task.kind != shot.media_type:
                continue
            for output_index, item in enumerate(task.outputs or []):
                document = verified_outputs.get((task.id, output_index))
                if (
                    classify_task_output(item) == shot.media_type
                    and document is not None
                    and _task_manifest_binds_shot(
                        document, task=task, shot=shot, output_index=output_index,
                    )
                ):
                    match = {
                        "shot_id": str(shot.id),
                        "task_id": str(task.id),
                        "output_index": output_index,
                    }
                    break
            if match:
                break
        if match:
            selected_shots.append(match)

    episode_rows = []
    video_episode_ids = {
        str(shot.episode_id) for shot in shots if shot.media_type == Shot.MediaType.VIDEO
    }
    assemblies = list(
        AssemblyJob.objects.filter(
            project=project, status=AssemblyJob.Status.SUCCEEDED, dry_run=False,
        ).order_by("-finished_at", "-created_at")
    )
    for episode_id in sorted(video_episode_ids):
        match = next(
            (
                job for job in assemblies
                if job.scope == AssemblyJob.Scope.EPISODE and str(job.episode_id) == episode_id
            ),
            None,
        )
        if match:
            episode_rows.append({"episode_id": episode_id, "assembly_id": str(match.id)})
    series = next((job for job in assemblies if job.scope == AssemblyJob.Scope.SERIES), None)
    return normalize_release_selection({
        "shots": selected_shots,
        "episodes": episode_rows,
        "series_assembly_id": str(series.id) if series else None,
    })


def validate_release_selection(*, project: SeriesProject, owner, selection: object) -> dict:
    normalized = normalize_release_selection(selection)
    shot_rows = list(
        Shot.objects.filter(episode__project=project)
        .select_related("episode")
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

    task_ids = {row["task_id"] for row in normalized["shots"]}
    tasks = {
        str(task.id): task
        for task in GenerationTask.objects.filter(
            id__in=task_ids, owner=owner, project=project,
        )
        .select_related(
            "episode", "generated_workflow__prompt_artifact",
            "generated_workflow__template",
        )
    }
    artifacts: list[dict] = []
    expected_episode_sources: dict[str, list[tuple[str, str, int]]] = {}
    errors: dict[str, list[str]] = {}
    for shot in shot_rows:
        shot_id = str(shot.id)
        row = selected_by_shot[shot_id]
        task = tasks.get(row["task_id"])
        reasons: list[str] = []
        if task is None:
            # The query is tenant/project scoped.  Do not reveal whether a
            # caller-supplied UUID exists in another tenant.
            reasons.append("task_unavailable")
        elif task.status != GenerationTask.Status.SUCCEEDED:
            reasons.append("task_not_succeeded")
        elif task.kind != shot.media_type:
            reasons.append("task_kind")
        elif row["output_index"] >= len(task.outputs or []):
            reasons.append("output_index")
        elif classify_task_output(task.outputs[row["output_index"]]) != shot.media_type:
            reasons.append("output_kind")
        else:
            try:
                source_document = verified_task_manifest(task, row["output_index"])
                if not _task_manifest_binds_shot(
                    source_document, task=task, shot=shot,
                    output_index=row["output_index"],
                ):
                    reasons.append("task_provenance_binding")
                else:
                    distribution_document = verified_distribution_manifest(
                        task, row["output_index"],
                    )
                    source_artifact = source_document.get("artifact") or {}
                    source_integrity = source_document.get("integrity") or {}
                    artifact = distribution_document.get("artifact") or {}
                    integrity = distribution_document.get("integrity") or {}
                    disclosure = distribution_document.get("disclosure") or {}
                    release = distribution_document.get("release") or {}
                    artifacts.append({
                        "shot_id": shot_id,
                        "episode_id": str(shot.episode_id),
                        "media_kind": shot.media_type,
                        "task_id": str(task.id),
                        "output_index": row["output_index"],
                        "artifact_sha256": str(artifact.get("sha256") or ""),
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
                    })
                    if shot.media_type == Shot.MediaType.VIDEO:
                        expected_episode_sources.setdefault(str(shot.episode_id), []).append(
                            (shot_id, str(task.id), row["output_index"]),
                        )
            except DistributionError:
                reasons.append("managed_distribution_invalid")
            except (ProvenanceError, OSError, ValueError):
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
                    "task_id": row["task_id"],
                    "output_index": row["output_index"],
                    "task_page_url": f"/workflows?task={row['task_id']}",
                    "publish_url": f"/api/v1/tasks/{row['task_id']}/publish-output/",
                }
                for row in missing_distributions
            ]
            raise ReleaseBundleError(
                "发行候选分镜缺少或未通过公开发行副本；"
                "请前往生产工作流任务详情生成公开副本后重新封存",
                details=details,
            )
        raise ReleaseBundleError("发行候选包含无效分镜产物", details=details)

    episode_ids = set(expected_episode_sources)
    selected_episodes = {row["episode_id"]: row for row in normalized["episodes"]}
    missing_episodes = sorted(episode_ids - set(selected_episodes))
    extra_episodes = sorted(set(selected_episodes) - episode_ids)
    if missing_episodes or extra_episodes:
        raise ReleaseBundleError(
            "每个含视频分镜的剧集都必须选择对应成片",
            details={"missing_episode_ids": missing_episodes, "unknown_episode_ids": extra_episodes},
        )

    assembly_ids = {row["assembly_id"] for row in normalized["episodes"]}
    if normalized["series_assembly_id"]:
        assembly_ids.add(normalized["series_assembly_id"])
    assemblies = {
        str(job.id): job
        for job in AssemblyJob.objects.filter(
            id__in=assembly_ids, owner=owner, project=project,
        ).select_related("episode")
    }
    assembly_artifacts: list[dict] = []

    def verify_assembly(job_id: str, *, scope: str, episode_id: str | None) -> None:
        job = assemblies.get(job_id)
        reasons: list[str] = []
        if job is None:
            # Tenant/project-scoped lookup deliberately collapses missing and
            # foreign identifiers into one non-oracular result.
            reasons.append("assembly_unavailable")
        elif job.status != AssemblyJob.Status.SUCCEEDED or job.dry_run:
            reasons.append("assembly_not_succeeded")
        elif job.scope != scope or str(job.episode_id or "") != str(episode_id or ""):
            reasons.append("assembly_context")
        else:
            try:
                document = verified_assembly_manifest(job)
                actual, source_error = _ordered_assembly_source_pairs(document)
                if scope == AssemblyJob.Scope.EPISODE:
                    expected = expected_episode_sources.get(str(episode_id), [])
                else:
                    expected = [
                        pair
                        for episode_sources in expected_episode_sources.values()
                        for pair in episode_sources
                    ]
                if source_error:
                    reasons.append(source_error)
                elif actual != expected:
                    reasons.append("stale_source_outputs")
                artifact = document.get("artifact") or {}
                integrity = document.get("integrity") or {}
                assembly_artifacts.append({
                    "assembly_id": str(job.id),
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
                details={"assembly_id": job_id, "reasons": reasons},
            )

    for episode_id, row in selected_episodes.items():
        verify_assembly(
            row["assembly_id"], scope=AssemblyJob.Scope.EPISODE, episode_id=episode_id,
        )
    if project.status == SeriesProject.Status.COMPLETED and not normalized["series_assembly_id"]:
        raise ReleaseBundleError("已完结项目必须选择整剧成片")
    if normalized["series_assembly_id"]:
        verify_assembly(
            normalized["series_assembly_id"], scope=AssemblyJob.Scope.SERIES, episode_id=None,
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
            project=locked.project, owner=locked.owner, selection=locked.selection,
        )
        sealed_at = timezone.now()
        body = {
            "schema": RELEASE_BUNDLE_SCHEMA,
            "bundle_id": str(locked.id),
            "project_id": str(locked.project_id),
            "owner_id": str(locked.owner_id),
            "version": locked.version,
            "title": locked.title,
            "sealed_at": sealed_at.isoformat(),
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
    if not isinstance(document, dict) or document.get("schema") != RELEASE_BUNDLE_SCHEMA:
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
        project=bundle.project, owner=bundle.owner, selection=bundle.selection,
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
        if any(
            isinstance(row, dict) and str(row.get("task_id")) == str(task.id)
            for row in (manifest.get("artifacts") or [])
        ):
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
