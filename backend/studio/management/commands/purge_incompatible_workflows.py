from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.apps import apps
from django.db.models import Count, Model, Q, QuerySet, UUIDField
from django.db.models.deletion import DO_NOTHING, SET_DEFAULT, SET_NULL

from studio.models import (
    Asset,
    AssetGenerationJob,
    AssemblyJob,
    AssemblySourceItem,
    CharacterBible,
    Episode,
    GeneratedWorkflow,
    GenerationAttempt,
    GenerationTask,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    PromptProfileVersion,
    ProductionRecipeVersion,
    ProductionAssemblyLink,
    ProductionRun,
    PlatformLifecycleAuditEvent,
    PropBible,
    ProviderProfile,
    RecommendationSnapshot,
    SceneBible,
    SeriesProject,
    StageRun,
    WorkflowSmokeAttempt,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    WorkflowProjector,
    WorkflowProjectorVersion,
)
from studio.services.registry_deletion import recipe_references_any
from studio.services.registry_reference_lock import registry_reference_write_guard
from studio.services.workflow_versions import (
    declared_workflow_operation,
    verify_workflow_version_integrity,
)


CONFIRMATION_PREFIX = "PURGE_INCOMPATIBLE_WORKFLOWS"
UUID_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"(?![0-9a-f])",
)
ACTIVE_STATUSES: dict[type[Model], set[str]] = {
    ProductionRun: {
        ProductionRun.Status.QUEUED,
        ProductionRun.Status.RUNNING,
    },
    GenerationTask: {
        GenerationTask.Status.QUEUED,
        GenerationTask.Status.RUNNING,
        GenerationTask.Status.CANCEL_REQUESTED,
    },
    WorkflowSmokeAttempt: {
        WorkflowSmokeAttempt.Status.QUEUED,
        WorkflowSmokeAttempt.Status.SUBMITTING,
        WorkflowSmokeAttempt.Status.RUNNING,
        WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
    },
    AssetGenerationJob: {
        AssetGenerationJob.Status.QUEUED,
        AssetGenerationJob.Status.SUBMITTING,
        AssetGenerationJob.Status.SUBMITTED,
        AssetGenerationJob.Status.RUNNING,
        AssetGenerationJob.Status.CANCEL_REQUESTED,
    },
    AssemblyJob: {
        AssemblyJob.Status.QUEUED,
        AssemblyJob.Status.RUNNING,
    },
    StageRun: {
        StageRun.Status.RUNNING,
    },
    GenerationAttempt: {
        GenerationAttempt.Status.QUEUED,
        GenerationAttempt.Status.RUNNING,
        GenerationAttempt.Status.SUBMITTING,
        GenerationAttempt.Status.SUBMITTED,
        GenerationAttempt.Status.CANCEL_REQUESTED,
    },
}

PRESERVED_SHARED_MODELS: tuple[type[Model], ...] = (
    ProviderProfile,
    ModelProfileVersion,
    ModelDeployment,
    ModelArtifact,
    PromptProfileVersion,
    SeriesProject,
    Episode,
    CharacterBible,
    SceneBible,
    PropBible,
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exception_detail(exc: Exception) -> Any:
    return (
        getattr(exc, "message_dict", None)
        or getattr(exc, "messages", None)
        or str(exc)
    )


@dataclass
class PurgePlan:
    selected: dict[type[Model], set[Any]]
    nullifications: dict[tuple[type[Model], str], set[Any]]
    incompatible_versions: list[dict[str, Any]]
    recipe_references: list[dict[str, Any]]
    recommendation_snapshot_ids: list[str]
    generated_output_asset_ids: list[str]
    orphan_template_ids: list[str]
    kept_versions: list[dict[str, Any]]
    active_rows: list[dict[str, Any]]

    @property
    def model_rows(self) -> dict[str, list[str]]:
        return {
            model._meta.label: sorted(str(item) for item in identifiers)
            for model, identifiers in sorted(
                self.selected.items(), key=lambda item: item[0]._meta.label
            )
            if identifiers
        }

    @property
    def counts(self) -> dict[str, int]:
        return {label: len(items) for label, items in self.model_rows.items()}

    @property
    def nullification_rows(self) -> list[dict[str, Any]]:
        return [{
            "model": model._meta.label,
            "field": field_name,
            "ids": sorted(str(item) for item in identifiers),
        } for (model, field_name), identifiers in sorted(
            self.nullifications.items(),
            key=lambda item: (item[0][0]._meta.label, item[0][1]),
        ) if identifiers]

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_canonical_json({
            "models": self.model_rows,
            "nullifications": self.nullification_rows,
            "incompatible_versions": self.incompatible_versions,
            "recipe_references": self.recipe_references,
            "recommendation_snapshot_ids": self.recommendation_snapshot_ids,
            "generated_output_asset_ids": self.generated_output_asset_ids,
            "orphan_template_ids": self.orphan_template_ids,
            "kept_versions": self.kept_versions,
        })).hexdigest()

    @property
    def confirmation(self) -> str:
        return f"{CONFIRMATION_PREFIX}_{self.plan_sha256[:12].upper()}"

    def status_breakdown(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for model, identifiers in self.selected.items():
            if not identifiers or not any(
                field.name == "status" for field in model._meta.fields
            ):
                continue
            result[model._meta.label] = {
                str(status): int(total)
                for status, total in model._base_manager.filter(
                    pk__in=identifiers,
                ).values_list("status").annotate(total=Count("pk"))
            }
        return result


def _incompatible_workflow_versions(
    workflow_version_ids: frozenset[UUID] | None = None,
) -> tuple[list[WorkflowTemplateVersion], list[dict[str, Any]]]:
    versions: list[WorkflowTemplateVersion] = []
    details: list[dict[str, Any]] = []
    queryset = WorkflowTemplateVersion.objects.select_related("template")
    if workflow_version_ids is not None:
        if not workflow_version_ids:
            raise CommandError("指定的工作流范围不能为空")
        queryset = queryset.filter(pk__in=workflow_version_ids)
    queryset = queryset.order_by("template__slug", "version")
    for version in queryset:
        failures: list[dict[str, Any]] = []
        try:
            verify_workflow_version_integrity(version)
        except ValidationError as exc:  # Payloads vary by contract layer.
            failures.append({
                "check": "integrity",
                "error_type": type(exc).__name__,
                "detail": _exception_detail(exc),
            })
        try:
            declared_workflow_operation(version)
        except ValidationError as exc:
            failures.append({
                "check": "operation",
                "error_type": type(exc).__name__,
                "detail": _exception_detail(exc),
            })
        if not failures:
            continue
        versions.append(version)
        details.append({
            "id": str(version.id),
            "template": version.template.slug,
            "version": int(version.version),
            "status": version.status,
            "validation_state": version.validation_state,
            "failures": failures,
        })
    return versions, details


@lru_cache(maxsize=None)
def _incoming_fields(
    parent_model: type[Model],
) -> tuple[tuple[type[Model], Any], ...]:
    rows: list[tuple[type[Model], Any]] = []
    for child_model in apps.get_models(include_auto_created=True):
        for field in child_model._meta.fields:
            remote_field = getattr(field, "remote_field", None)
            if (
                remote_field is not None
                and remote_field.model is parent_model
                and (
                    getattr(field, "many_to_one", False)
                    or getattr(field, "one_to_one", False)
                )
            ):
                rows.append((child_model, field))
    return tuple(rows)


def _reverse_relation_querysets(
    parent_model: type[Model],
    parent_ids: set[Any],
) -> list[tuple[type[Model], QuerySet]]:
    rows: list[tuple[type[Model], QuerySet]] = []
    for child_model, field in _incoming_fields(parent_model):
        on_delete = field.remote_field.on_delete
        if on_delete in (SET_NULL, SET_DEFAULT, DO_NOTHING):
            continue
        rows.append((
            child_model,
            child_model._base_manager.filter(
                **{f"{field.attname}__in": parent_ids},
            ),
        ))
    return rows


def _expand_reverse_dependencies(selected: dict[type[Model], set[Any]]) -> None:
    changed = True
    while changed:
        changed = False
        for parent_model, parent_ids in list(selected.items()):
            if not parent_ids:
                continue
            for child_model, child_queryset in _reverse_relation_querysets(
                parent_model, parent_ids,
            ):
                child_ids = set(child_queryset.values_list("pk", flat=True))
                new_ids = child_ids - selected[child_model]
                if new_ids:
                    selected[child_model].update(new_ids)
                    changed = True


def _selected_uuid_strings(selected: dict[type[Model], set[Any]]) -> set[str]:
    return {
        str(identifier).lower()
        for model, identifiers in selected.items()
        if isinstance(model._meta.pk, UUIDField)
        for identifier in identifiers
    }


def _payload_references(value: Any, targets: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            _payload_references(key, targets) or _payload_references(item, targets)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_payload_references(item, targets) for item in value)
    if not isinstance(value, str):
        return False
    return bool({match.group(0).lower() for match in UUID_PATTERN.finditer(value)} & targets)


def _semantic_reference_hits(targets: set[str]) -> list[dict[str, Any]]:
    if not targets:
        return []
    hits: list[dict[str, Any]] = []
    for model in apps.get_models(include_auto_created=False):
        json_fields = [
            field for field in model._meta.fields
            if field.get_internal_type() == "JSONField"
        ]
        if not json_fields:
            continue
        for row in model._base_manager.values_list(
            "pk", *(field.name for field in json_fields),
        ).iterator(chunk_size=200):
            identifier, *values = row
            for field, value in zip(json_fields, values):
                if _payload_references(value, targets):
                    hits.append({
                        "model": model,
                        "id": identifier,
                        "field": field.name,
                    })
                    break
    for identifier in PlatformLifecycleAuditEvent._base_manager.filter(
        object_id__in=targets,
    ).values_list("pk", flat=True):
        hits.append({
            "model": PlatformLifecycleAuditEvent,
            "id": identifier,
            "field": "object_id",
        })
    return hits


def _expand_semantic_dependencies(selected: dict[type[Model], set[Any]]) -> None:
    while True:
        hits = _semantic_reference_hits(_selected_uuid_strings(selected))
        added = False
        for hit in hits:
            model = hit["model"]
            identifier = hit["id"]
            if identifier not in selected[model]:
                selected[model].add(identifier)
                added = True
        if not added:
            return
        _expand_reverse_dependencies(selected)


def _expand_owned_and_semantic_dependencies(
    selected: dict[type[Model], set[Any]],
) -> None:
    while True:
        before = sum(len(identifiers) for identifiers in selected.values())
        _expand_reverse_dependencies(selected)
        generated_output_asset_ids = set(
            AssetGenerationJob._base_manager.filter(
                pk__in=selected[AssetGenerationJob],
                output_asset__isnull=False,
            ).values_list("output_asset_id", flat=True)
        )
        selected[Asset].update(generated_output_asset_ids)
        assembly_job_ids = set(
            AssemblySourceItem._base_manager.filter(
                pk__in=selected[AssemblySourceItem],
            ).values_list("assembly_job_id", flat=True)
        ) | set(
            ProductionAssemblyLink._base_manager.filter(
                pk__in=selected[ProductionAssemblyLink],
            ).values_list("assembly_job_id", flat=True)
        )
        selected[AssemblyJob].update(assembly_job_ids)
        _expand_reverse_dependencies(selected)
        _expand_semantic_dependencies(selected)
        after = sum(len(identifiers) for identifiers in selected.values())
        if after == before:
            return


def _assert_no_preserved_shared_rows(
    selected: dict[type[Model], set[Any]],
) -> None:
    unexpected = {
        model._meta.label: sorted(str(item) for item in selected.get(model, set()))
        for model in PRESERVED_SHARED_MODELS
        if selected.get(model)
    }
    if unexpected:
        raise CommandError(
            "依赖闭包触及必须保留的共享上游，拒绝生成清理计划："
            + json.dumps(unexpected, ensure_ascii=False),
        )


def _build_nullifications(
    selected: dict[type[Model], set[Any]],
) -> dict[tuple[type[Model], str], set[Any]]:
    nullifications: dict[tuple[type[Model], str], set[Any]] = defaultdict(set)
    unsupported: list[dict[str, Any]] = []
    for parent_model, parent_ids in selected.items():
        if not parent_ids:
            continue
        for child_model, field in _incoming_fields(parent_model):
            on_delete = field.remote_field.on_delete
            if on_delete not in (SET_NULL, SET_DEFAULT, DO_NOTHING):
                continue
            queryset = child_model._base_manager.filter(
                **{f"{field.attname}__in": parent_ids},
            )
            selected_child_ids = selected.get(child_model, set())
            if selected_child_ids:
                queryset = queryset.exclude(pk__in=selected_child_ids)
            child_ids = set(queryset.values_list("pk", flat=True))
            if not child_ids:
                continue
            if on_delete is SET_NULL:
                nullifications[(child_model, field.name)].update(child_ids)
                continue
            unsupported.append({
                "model": child_model._meta.label,
                "field": field.name,
                "on_delete": getattr(on_delete, "__name__", str(on_delete)),
                "ids": sorted(str(item) for item in child_ids),
            })
    if unsupported:
        raise CommandError(
            "清理范围包含不支持的 SET_DEFAULT/DO_NOTHING 外键："
            + json.dumps(unsupported, ensure_ascii=False),
        )
    return dict(nullifications)


def _active_rows(selected: dict[type[Model], set[Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model, statuses in ACTIVE_STATUSES.items():
        identifiers = selected.get(model, set())
        if not identifiers:
            continue
        rows.extend({
            "resource_type": model._meta.label,
            "id": str(identifier),
            "status": str(status),
        } for identifier, status in model._base_manager.filter(
            pk__in=identifiers,
            status__in=statuses,
        ).values_list("pk", "status"))
    for model in (WorkflowSmokeAttempt, AssetGenerationJob):
        identifiers = selected.get(model, set())
        if not identifiers:
            continue
        rows.extend({
            "resource_type": model._meta.label,
            "id": str(identifier),
            "status": "uncertain_without_resolution",
        } for identifier in model._base_manager.filter(
            pk__in=identifiers,
            status=model.Status.UNCERTAIN,
            resolutions__isnull=True,
        ).values_list("pk", flat=True).distinct())
    generation_task_ids = selected.get(GenerationTask, set())
    if generation_task_ids:
        unresolved_remote = GenerationTask._base_manager.filter(
            pk__in=generation_task_ids,
        ).exclude(submission_token="").exclude(
            Q(submission_state__in=(
                GenerationTask.SubmissionState.TERMINAL_FAILED,
                GenerationTask.SubmissionState.TERMINAL_CANCELLED,
            ))
            | Q(remote_state__in=(
                GenerationTask.RemoteState.SUCCEEDED,
                GenerationTask.RemoteState.FAILED,
                GenerationTask.RemoteState.CANCELLED,
            )),
        )
        rows.extend({
            "resource_type": GenerationTask._meta.label,
            "id": str(identifier),
            "status": "remote_submission_not_terminal",
        } for identifier in unresolved_remote.values_list("pk", flat=True))
    return sorted(rows, key=lambda item: (item["resource_type"], item["id"]))


def build_purge_plan(
    workflow_version_ids: frozenset[UUID] | None = None,
    obsolete_projector_ids: frozenset[UUID] = frozenset(),
) -> PurgePlan:
    versions, incompatible_details = _incompatible_workflow_versions(workflow_version_ids)
    version_ids = {version.id for version in versions}
    selected: dict[type[Model], set[Any]] = defaultdict(set)
    selected[WorkflowTemplateVersion].update(version_ids)
    if obsolete_projector_ids:
        from studio.services.workflow_projectors import WORKFLOW_PROJECTOR_ENGINES
        if workflow_version_ids is None:
            raise CommandError("清理废弃 Projector 必须同时指定精确工作流范围")
        projectors = list(WorkflowProjectorVersion.objects.filter(pk__in=obsolete_projector_ids))
        if len(projectors) != len(obsolete_projector_ids):
            raise CommandError("指定的废弃 Projector 版本不存在")
        for projector in projectors:
            if WORKFLOW_PROJECTOR_ENGINES.get(projector.engine_key) == projector.engine_version:
                raise CommandError("不能用兼容清理删除仍受支持的 Projector")
            if WorkflowTemplateVersion.objects.exclude(pk__in=workflow_version_ids).filter(input_contract__contract_projector__version_id=str(projector.pk)).exists():
                raise CommandError("废弃 Projector 仍被范围外工作流引用")
            selected[WorkflowProjectorVersion].add(projector.pk)
        for projector in projectors:
            if not WorkflowProjectorVersion.objects.filter(projector_id=projector.projector_id).exclude(pk__in=obsolete_projector_ids).exists():
                selected[WorkflowProjector].add(projector.projector_id)

    recipe_rows = recipe_references_any(*version_ids)
    selected[ProductionRecipeVersion].update(
        recipe.id for recipe, _paths in recipe_rows
    )
    recipe_details = sorted(({
        "id": str(recipe.id),
        "recipe_key": recipe.recipe_key,
        "version": int(recipe.version),
        "status": recipe.status,
        "reference_paths": sorted(paths),
    } for recipe, paths in recipe_rows), key=lambda item: item["id"])

    snapshot_ids = set(
        RecommendationSnapshot.objects.filter(
            candidates__workflow_version_id__in=version_ids,
        ).values_list("id", flat=True).distinct()
    )
    selected[RecommendationSnapshot].update(snapshot_ids)
    _expand_owned_and_semantic_dependencies(selected)

    # AssetGenerationJob.output_asset points from the dependent job to its
    # generated output. That semantic ownership runs opposite to the FK, so
    # reverse-relation expansion alone cannot see it. Further Asset rows may
    # enter through the recursively verified semantic dependency closure. The
    # database rows are removed, while physical media files are deliberately
    # retained for recoverability.
    generated_output_asset_ids = set(
        AssetGenerationJob._base_manager.filter(
            pk__in=selected[AssetGenerationJob],
            output_asset__isnull=False,
        ).values_list("output_asset_id", flat=True)
    )
    selected[Asset].update(generated_output_asset_ids)

    target_template_ids = {version.template_id for version in versions}
    orphan_template_ids: set[Any] = set()
    for template_id in target_template_ids:
        remaining_versions = WorkflowTemplateVersion.objects.filter(
            template_id=template_id,
        ).exclude(id__in=selected[WorkflowTemplateVersion]).exists()
        remaining_generated = GeneratedWorkflow.objects.filter(
            template_id=template_id,
        ).exclude(id__in=selected[GeneratedWorkflow]).exists()
        if not remaining_versions and not remaining_generated:
            orphan_template_ids.add(template_id)
    selected[WorkflowTemplate].update(orphan_template_ids)
    _expand_owned_and_semantic_dependencies(selected)
    _assert_no_preserved_shared_rows(selected)
    if workflow_version_ids is not None:
        outside_scope = selected[WorkflowTemplateVersion] - workflow_version_ids
        if outside_scope:
            raise CommandError(
                "依赖闭包触及未指定的工作流版本，拒绝扩大删除范围："
                + ", ".join(sorted(str(item) for item in outside_scope)),
            )

    kept_versions = list(
        WorkflowTemplateVersion.objects.select_related("template")
        .exclude(id__in=version_ids)
        .order_by("template__slug", "version")
        .values(
            "id", "template__slug", "version", "status", "validation_state",
        )
    )
    kept_details = [{
        "id": str(item["id"]),
        "template": item["template__slug"],
        "version": int(item["version"]),
        "status": item["status"],
        "validation_state": item["validation_state"],
    } for item in kept_versions]
    nullifications = _build_nullifications(selected)
    return PurgePlan(
        selected=dict(selected),
        nullifications=nullifications,
        incompatible_versions=incompatible_details,
        recipe_references=recipe_details,
        recommendation_snapshot_ids=sorted(str(item) for item in snapshot_ids),
        generated_output_asset_ids=sorted(
            str(item) for item in generated_output_asset_ids
        ),
        orphan_template_ids=sorted(str(item) for item in orphan_template_ids),
        kept_versions=kept_details,
        active_rows=_active_rows(selected),
    )


def _validate_backup_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path.resolve(strict=True)
    if manifest_path.name != "manifest.json":
        raise CommandError("--backup-manifest 必须指向 backup.ps1 生成的 manifest.json")
    backup_root = manifest_path.parent
    if (backup_root / ".incomplete").exists():
        raise CommandError("备份仍带有 .incomplete 标记，拒绝执行清理")
    checksum_path = backup_root / "manifest.sha256"
    if not checksum_path.is_file():
        raise CommandError("备份缺少 manifest.sha256")
    expected_manifest_sha256 = checksum_path.read_text(encoding="ascii").split()[0].lower()
    actual_manifest_sha256 = _sha256_file(manifest_path)
    if expected_manifest_sha256 != actual_manifest_sha256:
        raise CommandError("备份 manifest SHA-256 校验失败")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    database = manifest.get("database") or {}
    if database.get("kind") != "sqlite" or not database.get("included"):
        raise CommandError("清理仅接受包含 SQLite 数据库的完整备份")
    relative_database_path = str(database.get("backup_relative_path") or "")
    if not relative_database_path:
        raise CommandError("备份 manifest 缺少数据库文件路径")
    database_path = (backup_root / relative_database_path).resolve(strict=True)
    if backup_root != database_path and backup_root not in database_path.parents:
        raise CommandError("备份数据库路径越过备份根目录")
    file_entry = next((
        item for item in manifest.get("files") or []
        if item.get("relative_path") == relative_database_path
    ), None)
    if file_entry is None:
        raise CommandError("备份 manifest 未登记数据库文件")
    if _sha256_file(database_path) != str(file_entry.get("sha256") or "").lower():
        raise CommandError("备份数据库 SHA-256 校验失败")
    live_database_path = Path(str(connection.settings_dict["NAME"])).resolve()
    if database_path == live_database_path:
        raise CommandError("备份数据库不能与当前数据库是同一文件")
    return {
        "backup_id": manifest.get("backup_id"),
        "created_utc": manifest.get("created_utc"),
        "manifest_sha256": actual_manifest_sha256,
        "database_sha256": str(file_entry["sha256"]),
        "database_bytes": int(file_entry["bytes"]),
        "manifest_path": str(manifest_path),
    }


def _lock_plan_rows(plan: PurgePlan) -> None:
    for model, identifiers in sorted(
        plan.selected.items(), key=lambda item: item[0]._meta.label,
    ):
        if identifiers:
            list(
                model._base_manager.select_for_update()
                .filter(pk__in=identifiers)
                .values_list("pk", flat=True)
            )
    for (model, _field_name), identifiers in sorted(
        plan.nullifications.items(),
        key=lambda item: (item[0][0]._meta.label, item[0][1]),
    ):
        if identifiers:
            list(
                model._base_manager.select_for_update()
                .filter(pk__in=identifiers)
                .values_list("pk", flat=True)
            )


def _deletion_order(selected: dict[type[Model], set[Any]]) -> list[type[Model]]:
    nodes = {model for model, identifiers in selected.items() if identifiers}
    edges: dict[type[Model], set[type[Model]]] = {model: set() for model in nodes}
    indegree: dict[type[Model], int] = {model: 0 for model in nodes}
    for parent_model in nodes:
        parent_ids = selected[parent_model]
        for child_model, child_queryset in _reverse_relation_querysets(
            parent_model, parent_ids,
        ):
            if child_model not in nodes or child_model is parent_model:
                continue
            has_selected_edge = child_queryset.filter(
                pk__in=selected[child_model],
            ).exists()
            if has_selected_edge and parent_model not in edges[child_model]:
                edges[child_model].add(parent_model)
                indegree[parent_model] += 1
    queue = deque(sorted(
        (model for model in nodes if indegree[model] == 0),
        key=lambda model: model._meta.label,
    ))
    order: list[type[Model]] = []
    while queue:
        model = queue.popleft()
        order.append(model)
        for parent_model in sorted(edges[model], key=lambda item: item._meta.label):
            indegree[parent_model] -= 1
            if indegree[parent_model] == 0:
                queue.append(parent_model)
    # SQLite defer_foreign_keys resolves the intentional ProductionRun /
    # ConfigRevision cycle.  Append any cycle members deterministically and
    # verify the entire database before commit.
    order.extend(sorted(nodes - set(order), key=lambda model: model._meta.label))
    return order


def _hard_delete_plan(plan: PurgePlan) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA defer_foreign_keys = ON")
    for (model, field_name), identifiers in sorted(
        plan.nullifications.items(),
        key=lambda item: (item[0][0]._meta.label, item[0][1]),
    ):
        field = model._meta.get_field(field_name)
        parent_ids = plan.selected.get(field.remote_field.model, set())
        count = model._base_manager.filter(
            pk__in=identifiers,
            **{f"{field.attname}__in": parent_ids},
        ).update(
            **{field_name: None},
        )
        if count != len(identifiers):
            raise CommandError(
                f"{model._meta.label}.{field_name} 置空数量漂移："
                f"计划 {len(identifiers)}，实际 {count}",
            )
    for model in _deletion_order(plan.selected):
        identifiers = plan.selected[model]
        count = model._base_manager.filter(pk__in=identifiers)._raw_delete(
            using=connection.alias,
        )
        if count != len(identifiers):
            raise CommandError(
                f"{model._meta.label} 删除数量漂移：计划 {len(identifiers)}，实际 {count}",
            )
        deleted[model._meta.label] = int(count)
    with connection.cursor() as cursor:
        foreign_key_errors = list(cursor.execute("PRAGMA foreign_key_check"))
    if foreign_key_errors:
        raise CommandError(
            "清理后出现外键错误，事务已回滚：" + json.dumps(foreign_key_errors[:20]),
        )
    return deleted


def _report(plan: PurgePlan, *, mode: str, **extra: Any) -> dict[str, Any]:
    return {
        "mode": mode,
        "database": str(Path(str(connection.settings_dict["NAME"])).resolve()),
        "plan_sha256": plan.plan_sha256,
        "confirmation": plan.confirmation,
        "incompatible_versions": plan.incompatible_versions,
        "kept_versions": plan.kept_versions,
        "recipe_references": plan.recipe_references,
        "recommendation_snapshot_ids": plan.recommendation_snapshot_ids,
        "generated_output_asset_ids": plan.generated_output_asset_ids,
        "orphan_template_ids": plan.orphan_template_ids,
        "counts": plan.counts,
        "nullifications": plan.nullification_rows,
        "status_breakdown": plan.status_breakdown(),
        "active_rows": plan.active_rows,
        **extra,
    }


class Command(BaseCommand):
    help = (
        "Preview or execute the pre-launch SQLite data repair that removes every "
        "workflow version rejected by the current contract and its dependent "
        "recipe/run/import/smoke/recommendation facts. Shared provider, model, "
        "prompt-profile, project, episode, and Bible rows are protected. Every "
        "derived or semantic-dependent Asset row listed by the exact plan is "
        "removed, while its physical media file is retained."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workflow-version", action="append", type=UUID, default=None,
            help="Limit repair to these exact workflow version UUIDs; repeat for multiple targets.",
        )
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--obsolete-projector-version", action="append", type=UUID, default=[])
        parser.add_argument("--expected-plan-sha256", default="")
        parser.add_argument("--confirmation", default="")
        parser.add_argument("--backup-manifest", default="")
        parser.add_argument("--irreversible-without-backup", action="store_true", help="Only for explicitly scoped, confirmed deletion where no backup is requested.")

    def handle(self, *args, **options):
        if connection.vendor != "sqlite":
            raise CommandError(
                "该未上线环境修复命令仅支持 SQLite；PostgreSQL 必须另行制定停机清理方案",
            )
        requested = options["workflow_version"]
        scope = frozenset(requested) if requested is not None else None
        if scope is not None:
            existing = set(WorkflowTemplateVersion.objects.filter(pk__in=scope).values_list("pk", flat=True))
            if missing := scope - existing:
                raise CommandError("指定的工作流版本不存在：" + ", ".join(sorted(str(item) for item in missing)))
        obsolete_projectors = frozenset(options["obsolete_projector_version"])
        preview = build_purge_plan(scope, obsolete_projectors)
        if not options["execute"]:
            self.stdout.write(json.dumps(
                _report(preview, mode="preview"),
                ensure_ascii=False,
                indent=2,
            ))
            return
        if not preview.incompatible_versions:
            self.stdout.write(json.dumps(
                _report(preview, mode="no_op"),
                ensure_ascii=False,
                indent=2,
            ))
            return
        if preview.active_rows:
            raise CommandError(
                "清理范围仍有活动任务，必须先停稳："
                + json.dumps(preview.active_rows, ensure_ascii=False),
            )
        expected_plan_sha256 = str(options["expected_plan_sha256"]).strip().lower()
        if expected_plan_sha256 != preview.plan_sha256:
            raise CommandError(
                "--expected-plan-sha256 与当前影响预览不一致，请重新预览",
            )
        if str(options["confirmation"]).strip() != preview.confirmation:
            raise CommandError(
                f"--confirmation 必须精确填写 {preview.confirmation}",
            )
        backup_manifest = str(options["backup_manifest"]).strip()
        irreversible = options["irreversible_without_backup"]
        if irreversible and (scope is None or backup_manifest):
            raise CommandError("不保留备份必须指定精确工作流范围，且不能同时提供备份")
        if not backup_manifest and not irreversible:
            raise CommandError("--backup-manifest 是执行清理的必填项")
        backup_evidence = {"retained": False, "irreversible": True} if irreversible else _validate_backup_manifest(Path(backup_manifest))
        preview_status_breakdown = preview.status_breakdown()

        with registry_reference_write_guard():
            with transaction.atomic():
                _lock_plan_rows(preview)
                current = build_purge_plan(scope, obsolete_projectors)
                if current.plan_sha256 != preview.plan_sha256:
                    raise CommandError("锁内影响范围已变化，清理事务已回滚")
                if current.active_rows:
                    raise CommandError("锁内发现活动任务，清理事务已回滚")
                deleted_uuid_strings = _selected_uuid_strings(current.selected)
                deleted = _hard_delete_plan(current)
                remaining = build_purge_plan(scope)
                if remaining.incompatible_versions:
                    raise CommandError("仍有不兼容工作流版本，清理事务已回滚")
                semantic_dangling = _semantic_reference_hits(deleted_uuid_strings)
                if semantic_dangling:
                    raise CommandError(
                        "清理后仍有 JSON 或生命周期引用，事务已回滚："
                        + json.dumps([{
                            "model": hit["model"]._meta.label,
                            "id": str(hit["id"]),
                            "field": hit["field"],
                        } for hit in semantic_dangling[:50]], ensure_ascii=False),
                    )

        self.stdout.write(json.dumps(
            _report(
                preview,
                mode="executed",
                deleted=deleted,
                backup=backup_evidence,
                status_breakdown=preview_status_breakdown,
                remaining_incompatible_versions=0,
            ),
            ensure_ascii=False,
            indent=2,
        ))
