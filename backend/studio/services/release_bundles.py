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
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    AssemblyJob,
    GeneratedArtifact,
    GenerationTask,
    ProductionAssemblyLink,
    ReleaseBundle,
    SeriesProject,
    Shot,
)
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.production_run_plans import get_run_execution_plan, verify_run_execution_plan
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


PRODUCTION_RELEASE_BUNDLE_SCHEMA = "autoprompt-production-release-bundle-v3"


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


def _release_source_evidence(artifact: GeneratedArtifact, shot: Shot):
    from studio.services.assembly import AssemblyError, _assembly_artifact_evidence

    try:
        detail, _ = _assembly_artifact_evidence(artifact, shot)
    except AssemblyError as exc:
        raise ReleaseBundleError(str(exc)) from exc
    if artifact.generation_task_id:
        output_index = _artifact_task_output_index(artifact)
        return detail, verified_task_manifest(artifact.generation_task, output_index), output_index
    return detail, None, None


def validate_release_selection(
    *,
    project: SeriesProject,
    owner,
    selection: object,
    production_run,
) -> dict:
    from studio.services.production_runs import _selected_episodes, StageBlocked

    if production_run is None or production_run.project_id != project.pk or project.owner_id != owner.pk:
        raise ReleaseBundleError("发行必须属于正式生产运行的项目与所有者")
    normalized = normalize_release_selection(selection)
    try:
        episodes = _selected_episodes(production_run)
    except StageBlocked as exc:
        raise ReleaseBundleError(exc.reason) from exc
    except ValidationError as exc:
        raise ReleaseBundleError("本轮冻结执行计划无效，不能核对交付范围") from exc
    shot_rows = list(
        Shot.objects.filter(episode__in=episodes)
        .select_related("episode__project")
        .order_by("episode__number", "number", "id")
    )
    shots_by_id = {str(shot.id): shot for shot in shot_rows}
    selected_by_shot = {row["shot_id"]: row for row in normalized["shots"]}
    missing = sorted(set(shots_by_id) - set(selected_by_shot))
    extra = sorted(set(selected_by_shot) - set(shots_by_id))
    if missing or extra or not shot_rows:
        raise ReleaseBundleError(
            "发行候选必须为本轮交付范围的每个分镜选择一个产物",
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
            "generation_attempt__preparation", "stage_run", "generation_task",
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
                source, source_document, output_index = _release_source_evidence(artifact, shot)
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
                    disclosure = None
                    if source["source_kind"] == "execution":
                        from studio.services.execution_preparations import read_frozen_execution_request
                        _, definition, _ = read_frozen_execution_request(artifact.generation_attempt.preparation)
                        model = definition.value["target"]["model"]
                        disclosure = resolve_delivery_disclosure(
                            model=model["identifier"], model_key=model["id"], media_kind=artifact.kind,
                        )
                    artifact_row = {
                        "artifact_sha256": artifact.sha256,
                        "provenance_sha256": artifact.manifest_metadata_sha256,
                        "source_artifact_sha256": artifact.sha256,
                        "source_provenance_sha256": artifact.manifest_metadata_sha256,
                        "distribution_release_id": "",
                        "distribution_model_key": disclosure.model_key if disclosure else None,
                        "distribution_model": disclosure.model if disclosure else None,
                        "distribution_label": disclosure.label if disclosure else None,
                        "distribution_version": "managed-original-v1",
                        "source_media": ([{"asset_id": str(artifact.asset_id), "role": "adopted_media", "source_sha256": artifact.sha256, "submitted_sha256": artifact.sha256}] if source["source_kind"] == "asset" else []),
                    }
                artifacts.append({
                    "shot_id": shot_id,
                    "episode_id": str(shot.episode_id),
                    "media_kind": shot.media_type,
                    "generated_artifact_id": str(artifact.id),
                    "source_kind": source["source_kind"],
                    "preparation_id": source["preparation_id"],
                    "request_sha256": source["request_sha256"],
                    "source_asset_id": str(artifact.asset_id) if source["source_kind"] == "asset" else None,
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
                "请检查原生成任务的分发状态；本次未启动转码或修改归档文件",
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
    assembly_links = {str(row.assembly_job_id): row for row in ProductionAssemblyLink.objects.filter(
        production_run=production_run).only("assembly_job_id", "episode_id", "stage_run_id")}

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
        elif (str(job.pk) not in assembly_links
                or assembly_links[str(job.pk)].episode_id != job.episode_id
                or assembly_links[str(job.pk)].stage_run_id != master.stage_run_id):
            reasons.append("assembly_run_link_invalid")
        else:
            try:
                master_facts = _managed_generated_artifact_facts(master)
                document = verified_assembly_manifest(job)
                if (document.get("artifact") or {}).get("sha256") != master_facts["sha256"]:
                    reasons.append("assembly_artifact_hash")
                source_items = list(job.source_items.select_related(
                    "shot", "generated_artifact",
                ).order_by("ordinal"))
                from studio.services.assembly import verify_assembly_sources
                verify_assembly_sources(job)
                expected_shots = [shot for shot in shot_rows
                    if scope == AssemblyJob.Scope.SERIES or str(shot.episode_id) == str(episode_id)]
                if [str(item.shot_id) for item in source_items] != [str(shot.id) for shot in expected_shots]:
                    raise ReleaseBundleError("成片镜头顺序与本轮明确选择不一致")
                provenance_rows = document.get("source_outputs")
                if not isinstance(provenance_rows, list) or len(provenance_rows) != len(source_items):
                    raise ReleaseBundleError("成片签名缺少完整有序的正式产物来源")
                for index, (source, shot) in enumerate(zip(source_items, expected_shots, strict=True)):
                    selected = selected_artifacts_by_shot.get(str(shot.id))
                    row = provenance_rows[index]
                    if (selected is None or not isinstance(row, dict)
                            or row.get("clip_index") != index
                            or source.generated_artifact_id != selected.pk
                            or source.sha256 != selected.sha256
                            or source.size_bytes != selected.size_bytes
                            or source.mime_type != selected.mime_type
                            or row.get("shot_id") != str(shot.pk)
                            or row.get("generated_artifact_id") != str(selected.pk)
                            or row.get("artifact_sha256") != source.sha256
                            or row.get("manifest_sha256") != selected.manifest_metadata_sha256
                            or source.media_kind != shot.media_type
                            or row.get("media_kind") != shot.media_type):
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
            except (ProvenanceError, ValidationError, ReleaseBundleError, OSError, ValueError, TypeError):
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
    if normalized["series_artifact_id"]:
        raise ReleaseBundleError("本轮交付为逐集母版，不接受另选整剧成片")

    return {
        "selection": normalized,
        "artifacts": sorted(artifacts, key=lambda row: row["shot_id"]),
        "assemblies": sorted(
            assembly_artifacts,
            key=lambda row: (row["scope"], row.get("episode_id") or ""),
        ),
    }


def create_release_draft(*, production_run, selection, project: SeriesProject, owner, created_by, title: str = "", notes: str = "") -> ReleaseBundle:
    with transaction.atomic():
        locked_project = SeriesProject.objects.select_for_update().get(pk=project.pk)
        latest = ReleaseBundle.objects.filter(project=locked_project).aggregate(Max("version"))[
            "version__max"
        ] or 0
        version = latest + 1
        if production_run is None or production_run.project_id != project.pk or locked_project.owner_id != owner.pk:
            raise ReleaseBundleError("发布包必须明确引用本轮交付范围与已采用产物")
        selection = normalize_release_selection(selection)
        return ReleaseBundle.objects.create(
            owner=owner,
            project=locked_project,
            created_by=created_by,
            production_run=production_run,
            version=version,
            title=title.strip() or f"{locked_project.title} · 发行候选 v{version}",
            notes=notes,
            selection=selection,
        )


def _delivery_metadata_contract(run, plan):
    """Only the frozen recipe can authorize authored release fields."""
    if run.plan_source != "recipe":
        return None
    declarations = plan.value["recipe"]["definition"]["stages"]
    if not any(row["key"] == "delivery" and "delivery_metadata" in row for row in declarations):
        return None
    from studio.services.production_recipe_stage_snapshots import recipe_delivery_metadata
    from studio.services.configuration import ConfigurationResolutionError
    stage = run.stages.filter(stage_key="delivery").first()
    if stage is None:
        raise ReleaseBundleError("本轮缺少交付阶段，不能读取字段来源")
    try:
        return recipe_delivery_metadata(stage)
    except (ValidationError, ConfigurationResolutionError) as exc:
        raise ReleaseBundleError("交付字段冻结合同无效：" + str(exc)) from exc


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
            production_run=locked.production_run,
        )
        plan = get_run_execution_plan(locked.production_run)
        metadata = _delivery_metadata_contract(locked.production_run, plan)
        if metadata is not None:
            locked.title = metadata["projection"]["output"]["title"]
            locked.notes = metadata["projection"]["output"]["notes"]
        sealed_at = timezone.now()
        body = {
            "schema": PRODUCTION_RELEASE_BUNDLE_SCHEMA,
            "bundle_id": str(locked.id),
            "project_id": str(locked.project_id),
            "owner_id": str(locked.owner_id),
            "version": locked.version,
            "title": locked.title,
            "notes": locked.notes,
            "sealed_at": sealed_at.isoformat(),
            "production_run_id": str(locked.production_run_id),
            "run_plan_sha256": plan.sha256,
            "delivery_scope": "episode_masters",
            **verified,
        }
        if metadata is not None:
            body["delivery_metadata"] = metadata
        payload_sha256 = _canonical_sha256(body)
        document = {
            **body,
            "integrity": {
                "payload_sha256": payload_sha256,
                "signature": signing.Signer(salt=RELEASE_BUNDLE_SIGNING_SALT).sign(payload_sha256),
            },
        }
        try:
            json_contract(document, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        except ValidationError as exc:
            raise ReleaseBundleError("本轮发布清单超过正式合同大小，请按交付范围拆分制作轮次") from exc
        locked.selection = verified["selection"]
        locked.manifest = document
        locked.manifest_sha256 = payload_sha256
        locked.sealed_at = sealed_at
        locked.status = ReleaseBundle.Status.SEALED
        locked.save(update_fields=(
            "selection", "title", "notes", "manifest", "manifest_sha256", "sealed_at", "status", "updated_at",
        ))
        return locked


def verified_release_bundle(bundle: ReleaseBundle, *, run_plan_records=None) -> dict:
    document = deepcopy(bundle.manifest)
    if (
        not isinstance(document, dict)
        or document.get("schema") != PRODUCTION_RELEASE_BUNDLE_SCHEMA
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
        or document.get("owner_id") != str(bundle.owner_id)
        or type(document.get("version")) is not int
        or document["version"] != bundle.version
    ):
        raise ReleaseBundleError("发行清单与数据库对象不匹配")
    if bundle.production_run_id is None:
        raise ReleaseBundleError("ProductionRun 发行清单绑定缺失")
    try:
        if run_plan_records is None:
            run = bundle.production_run
            plan = get_run_execution_plan(run)
        else:
            # Read-only page batches pass freshly loaded records. Missing or
            # corrupt rows stay invalid; never requery/reconstruct a fallback.
            record = run_plan_records.get(bundle.production_run_id)
            if record is None:
                raise ValidationError("本轮缺少冻结计划")
            run = record.run
            plan = verify_run_execution_plan(record, run)
    except ValidationError as exc:
        raise ReleaseBundleError("本轮冻结执行计划无效，不能确认发布清单") from exc
    selection = document.get("selection")
    if (
        document.get("schema") != PRODUCTION_RELEASE_BUNDLE_SCHEMA
        or str(document.get("production_run_id") or "") != str(bundle.production_run_id)
        or document.get("run_plan_sha256") != plan.sha256
        or run.pk != bundle.production_run_id
        or run.project_id != bundle.project_id
        or run.project.owner_id != bundle.owner_id
        or document.get("delivery_scope") != "episode_masters"
        or not isinstance(selection, dict)
        or selection.get("series_artifact_id") is not None
    ):
        raise ReleaseBundleError("ProductionRun 发行清单绑定无效")
    from studio.services.delivery_contracts import verify_signed_delivery_metadata
    try:
        metadata = verify_signed_delivery_metadata(plan, document.get("delivery_metadata"),
            run_id=bundle.production_run_id, project_id=bundle.project_id)
    except ValidationError as exc:
        raise ReleaseBundleError("交付签名中的字段来源无效：" + str(exc)) from exc
    if metadata is not None:
        output = metadata["projection"]["output"]
        if (document.get("title") != output["title"] or document.get("notes") != output["notes"]
                or bundle.title != output["title"] or bundle.notes != output["notes"]):
            raise ReleaseBundleError("交付标题或说明与确切字段来源不一致")
    return {**document, "integrity": integrity, "verification": {"status": "verified"}}


def live_verified_release_bundle(bundle: ReleaseBundle) -> dict:
    """Verify both the sealed signature and every currently selected artifact.

    A signature only proves what was sealed.  This live verification also
    re-checks the managed distribution copies and source bindings,
    assemblies and the exact sealed selection before claiming the release is
    still usable.
    """

    sealed_document = verified_release_bundle(bundle)
    run = bundle.production_run
    metadata = _delivery_metadata_contract(run, get_run_execution_plan(run))
    if sealed_document.get("delivery_metadata") != metadata:
        raise ReleaseBundleError("交付字段来源与当前冻结阶段不一致")
    live = validate_release_selection(
        project=bundle.project,
        owner=bundle.owner,
        selection=bundle.selection,
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
