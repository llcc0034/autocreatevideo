from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Max, Prefetch
from rest_framework import status
from rest_framework.exceptions import APIException

from studio.models import (
    Asset,
    CharacterBible,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    PropBible,
    SceneBible,
    SeriesProject,
    Shot,
    WorkflowTemplate,
)
from studio.serializers import AssetSerializer, GenerationTaskSerializer
from studio.services.h3_prompts import (
    PromptBuildError,
    build_bible_image_candidate_prompt,
    project_bible_image_prompt_config,
)
from studio.services.production_pipeline import (
    BIBLE_IMAGE_CANDIDATE_PURPOSE,
    BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION,
    BIBLE_IMAGE_TEMPLATE_SLUG,
    assert_queue_capacity,
    bible_image_candidate_reserved_snapshot,
    bible_image_template_is_compliant,
    materialize_generated_workflow,
    materialize_generation_task,
    materialize_prompt_artifact,
)
from studio.services.workflow import WorkflowBuildError


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTITY_MODELS = {
    "character": CharacterBible,
    "scene": SceneBible,
    "prop": PropBible,
}


class BibleImageCandidateAPIError(APIException):
    def __init__(self, *, status_code: int, code: str, detail: str | dict):
        self.status_code = status_code
        self.default_code = code
        super().__init__(detail=detail, code=code)
        # Keep allowlisted scalars in their documented native JSON types.
        self.detail = detail


def _conflict(code: str, detail: str, **extra) -> BibleImageCandidateAPIError:
    return BibleImageCandidateAPIError(
        status_code=status.HTTP_409_CONFLICT,
        code=code,
        detail={"detail": detail, **extra},
    )


def _unavailable(detail: str) -> BibleImageCandidateAPIError:
    return BibleImageCandidateAPIError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="bible_image_candidate_template_unavailable",
        detail={"detail": detail},
    )


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_bible_image_candidate_input(
    project: SeriesProject,
    entity_type: str,
    entity: CharacterBible | SceneBible | PropBible,
    extra_direction: str,
    prompt_overrides: dict[str, str] | None = None,
):
    """Build once, then fingerprint only the exact adopted input snapshot."""

    result = build_bible_image_candidate_prompt(
        project, entity_type, entity, extra_direction, prompt_overrides,
    )
    if not isinstance(result.source_snapshot, dict) or not result.source_snapshot:
        raise PromptBuildError("实体参考图缺少实际采用输入快照")
    return result, _canonical_sha256(result.source_snapshot)


def assert_bible_image_candidate_source_current(
    *,
    task: GenerationTask,
    entity: CharacterBible | SceneBible | PropBible,
    frozen_target: dict,
    allow_stale: bool = False,
) -> dict:
    """Rebuild the exact adopted input before the first asset promotion.

    Candidate projection is advisory UI state.  Promotion is the authoritative
    write boundary, so it must independently prove that the current Bible,
    confirmed translations, canonical glossary and project visual rules still
    produce the fingerprint frozen into the task.  The caller holds the
    project and target-entity row locks while this function runs.
    """

    prompt = task.generated_workflow.prompt_artifact
    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    extra_direction = options.get("extra_direction")
    prompt_overrides = options.get("prompt_overrides")
    if type(extra_direction) is not str:
        raise _conflict(
            "bible_image_candidate_target_invalid",
            "实体图片候选缺少创建时冻结的导演要求，不能安全确认入库。",
        )
    if prompt_overrides is None:
        prompt_overrides = {}
    if not isinstance(prompt_overrides, dict) or any(
        type(key) is not str or type(value) is not str
        for key, value in prompt_overrides.items()
    ):
        raise _conflict(
            "bible_image_candidate_target_invalid",
            "实体图片候选缺少创建时冻结的提示词字段覆盖值，不能安全确认入库。",
        )
    try:
        _result, current_fingerprint = build_bible_image_candidate_input(
            task.project,
            frozen_target["reference_category"],
            entity,
            extra_direction,
            prompt_overrides,
        )
    except PromptBuildError as exc:
        raise _conflict(
            "bible_image_candidate_source_invalid",
            "当前 Bible 或已确认译文已无法构建同一实体图片输入；请重新生成候选。",
        ) from exc
    if current_fingerprint != frozen_target["source_fingerprint"]:
        if not allow_stale:
            raise _conflict(
                "bible_image_candidate_source_stale",
                "Bible、已确认译文或全局视觉规则已变化；旧图片候选需要显式确认后才能入库。",
                source_fingerprint=frozen_target["source_fingerprint"],
                current_source_fingerprint=current_fingerprint,
                can_accept_stale=True,
            )
        return {
            "source_current": False,
            "source_fingerprint": frozen_target["source_fingerprint"],
            "current_source_fingerprint": current_fingerprint,
        }
    return {
        "source_current": True,
        "source_fingerprint": frozen_target["source_fingerprint"],
        "current_source_fingerprint": current_fingerprint,
    }


def _request_fingerprint(
    *,
    entity_type: str,
    entity_id,
    source_fingerprint: str,
    extra_direction: str,
    prompt_overrides: dict[str, str],
    seed: int,
    priority: int,
    max_attempts: int,
) -> str:
    return _canonical_sha256({
        "schema_version": BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION,
        "generation_purpose": BIBLE_IMAGE_CANDIDATE_PURPOSE,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "source_fingerprint": source_fingerprint,
        "extra_direction": extra_direction,
        "prompt_overrides": {
            key: prompt_overrides[key] for key in sorted(prompt_overrides)
        },
        "seed": seed,
        "priority": priority,
        "max_attempts": max_attempts,
    })


def select_bible_image_template(*, for_update: bool = False) -> WorkflowTemplate:
    queryset = WorkflowTemplate.objects.filter(
        owner__isnull=True,
        slug=BIBLE_IMAGE_TEMPLATE_SLUG,
        is_active=True,
    ).order_by("-version", "-created_at", "-id")
    if for_update:
        queryset = queryset.select_for_update()
    template = queryset.first()
    if template is None:
        raise _unavailable("实体参考图系统模板未安装或已停用；系统不会回退到其他工作流。")
    if not bible_image_template_is_compliant(template):
        raise _unavailable("最新实体参考图系统模板不符合固定 SDXL 文生图安全契约。")
    return template


def load_bible_entity(
    project: SeriesProject,
    entity_type: str,
    entity_id,
    *,
    for_update: bool = False,
):
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise ValueError("unsupported Bible entity type")
    queryset = model.objects.filter(project=project).select_related(
        "project__style", "anchor_asset",
    ).prefetch_related("reference_assets")
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.filter(pk=entity_id).first()


@dataclass(frozen=True)
class BibleImageCandidateCreation:
    project: SeriesProject
    entity_type: str
    entity: CharacterBible | SceneBible | PropBible
    prompt: PromptArtifact
    workflow: GeneratedWorkflow
    task: GenerationTask
    idempotent_replay: bool


def _target_payload(
    *,
    entity_type: str,
    entity,
    source_fingerprint: str,
) -> dict:
    return {
        "schema_version": BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION,
        "reference_category": entity_type,
        "entity_id": str(entity.id),
        "entity_name": entity.name,
        "source_fingerprint": source_fingerprint,
    }


def _resolve_idempotent_chain(
    *,
    project: SeriesProject,
    idempotency_key: str,
    request_fingerprint: str,
) -> tuple[PromptArtifact, GeneratedWorkflow, GenerationTask] | None:
    prompts = list(PromptArtifact.objects.select_for_update().filter(
        project=project,
        prompt_type=PromptArtifact.PromptType.IMAGE,
        request_options__generation_purpose=BIBLE_IMAGE_CANDIDATE_PURPOSE,
        request_options__idempotency_key=idempotency_key,
    ).order_by("created_at", "id"))
    if not prompts:
        return None
    if len(prompts) != 1:
        raise _conflict(
            "bible_image_candidate_incomplete_chain",
            "该 idempotency_key 对应多条提示词记录，无法安全重放。",
            idempotency_key=idempotency_key,
        )
    prompt = prompts[0]
    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    if options.get("request_fingerprint") != request_fingerprint:
        raise _conflict(
            "bible_image_candidate_idempotency_conflict",
            "该 idempotency_key 已用于不同的实体图片候选请求。",
            idempotency_key=idempotency_key,
        )
    workflows = list(GeneratedWorkflow.objects.select_for_update().filter(
        prompt_artifact=prompt,
    ).select_related("template").order_by("created_at", "id"))
    if len(workflows) != 1:
        raise _conflict(
            "bible_image_candidate_incomplete_chain",
            "该幂等请求的工作流链不完整；系统不会静默补写或重复排队。",
            idempotency_key=idempotency_key,
        )
    workflow = workflows[0]
    tasks = list(GenerationTask.objects.select_for_update().filter(
        generated_workflow=workflow,
    ).order_by("queued_at", "id"))
    roots = [
        task for task in tasks
        if not str((task.capability_snapshot or {}).get("retry_of") or "")
    ]
    if len(roots) != 1:
        raise _conflict(
            "bible_image_candidate_incomplete_chain",
            "该幂等请求的根任务链不完整；系统不会静默补写或重复排队。",
            idempotency_key=idempotency_key,
        )
    for candidate_task in tasks:
        snapshot = (
            candidate_task.capability_snapshot
            if isinstance(candidate_task.capability_snapshot, dict) else {}
        )
        if (
            snapshot.get("generation_purpose") != BIBLE_IMAGE_CANDIDATE_PURPOSE
            or snapshot.get("idempotency_key") != idempotency_key
            or snapshot.get("request_fingerprint") != request_fingerprint
        ):
            raise _conflict(
                "bible_image_candidate_incomplete_chain",
                "该幂等请求存在不一致的任务快照。",
                idempotency_key=idempotency_key,
            )
    # The public generation_task_id always denotes the current candidate task.
    # Retries share the same immutable workflow and are ordered after their
    # source, matching the GET latest-candidate projection.
    task = tasks[-1]
    return prompt, workflow, task


def create_bible_image_candidate(
    *,
    user,
    project_id,
    entity_type: str,
    entity_id,
    idempotency_key: str,
    extra_direction: str,
    prompt_overrides: dict[str, str],
    seed: int | None,
    priority: int,
    max_attempts: int,
    extended_generation_confirmed: bool,
) -> BibleImageCandidateCreation:
    with transaction.atomic():
        project_queryset = SeriesProject.objects.select_for_update().select_related("style")
        if not getattr(user, "is_staff", False):
            project_queryset = project_queryset.filter(owner=user)
        project = project_queryset.filter(pk=project_id).first()
        if project is None:
            raise BibleImageCandidateAPIError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                detail={"detail": "连续剧项目不存在。"},
            )
        entity = load_bible_entity(
            project, entity_type, entity_id, for_update=True,
        )
        if entity is None:
            raise BibleImageCandidateAPIError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                detail={"detail": "Bible 实体不存在或不属于当前项目。"},
            )
        try:
            result, source_fingerprint = build_bible_image_candidate_input(
                project, entity_type, entity, extra_direction, prompt_overrides,
            )
        except PromptBuildError as exc:
            raise BibleImageCandidateAPIError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="bible_image_candidate_prompt_invalid",
                detail={"detail": str(exc)},
            ) from exc
        resolved_seed = project.continuity_seed if seed is None else seed
        request_fingerprint = _request_fingerprint(
            entity_type=entity_type,
            entity_id=entity.id,
            source_fingerprint=source_fingerprint,
            extra_direction=extra_direction,
            prompt_overrides=prompt_overrides,
            seed=resolved_seed,
            priority=priority,
            max_attempts=max_attempts,
        )
        existing = _resolve_idempotent_chain(
            project=project,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if existing is not None:
            prompt, workflow, task = existing
            return BibleImageCandidateCreation(
                project, entity_type, entity, prompt, workflow, task, True,
            )

        template = select_bible_image_template(for_update=True)
        task_owner = project.owner
        assert_queue_capacity(
            task_owner,
            extended_generation_confirmed=extended_generation_confirmed,
        )
        quality = result.payload.get("quality") or {}
        target = _target_payload(
            entity_type=entity_type,
            entity=entity,
            source_fingerprint=source_fingerprint,
        )
        consistency_snapshot = {
            **result.consistency_snapshot,
            "bible_target": target,
        }
        request_options = {
            "schema_version": BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION,
            "generation_purpose": BIBLE_IMAGE_CANDIDATE_PURPOSE,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "bible_target": target,
            "source_snapshot": result.source_snapshot,
            "quality": quality,
            "extra_direction": extra_direction,
            "prompt_overrides": {
                key: prompt_overrides[key] for key in sorted(prompt_overrides)
            },
            "seed": resolved_seed,
            "priority": priority,
            "max_attempts": max_attempts,
            "extended_generation_confirmed": bool(extended_generation_confirmed),
        }
        last_revision = PromptArtifact.objects.filter(
            project=project,
            episode__isnull=True,
            shot__isnull=True,
            prompt_type=PromptArtifact.PromptType.IMAGE,
        ).aggregate(value=Max("revision"))["value"] or 0
        prompt = materialize_prompt_artifact(
            project=project,
            episode=None,
            shot=None,
            created_by=user,
            prompt_type=PromptArtifact.PromptType.IMAGE,
            model=template.model,
            h3_mode=Shot.H3Mode.T2VA,
            revision=last_revision + 1,
            is_multi_shot=False,
            include_audio=False,
            total_duration_ms=0,
            prompt_text=result.text,
            negative_prompt=str(result.payload.get("negative_prompt") or ""),
            structured_payload=result.payload,
            consistency_snapshot=consistency_snapshot,
            request_options=request_options,
            content_hash=result.content_hash,
        )
        try:
            workflow = materialize_generated_workflow(
                user=user,
                template=template,
                prompt=prompt,
                overrides={"SEED": resolved_seed},
            )
            task = materialize_generation_task(
                user=task_owner,
                workflow=workflow,
                priority=priority,
                max_attempts=max_attempts,
                capability_snapshot_overrides=None,
            )
        except WorkflowBuildError as exc:
            raise _unavailable(
                "实体参考图系统模板无法按固定契约编译；系统没有写入部分候选链。"
            ) from exc
        return BibleImageCandidateCreation(
            project, entity_type, entity, prompt, workflow, task, False,
        )


def validated_frozen_bible_target(task: GenerationTask) -> dict | None:
    snapshot = task.capability_snapshot if isinstance(task.capability_snapshot, dict) else {}
    prompt = task.generated_workflow.prompt_artifact
    prompt_options = (
        prompt.request_options if isinstance(prompt.request_options, dict) else {}
    )
    task_is_candidate = snapshot.get("generation_purpose") == BIBLE_IMAGE_CANDIDATE_PURPOSE
    prompt_is_candidate = (
        prompt_options.get("generation_purpose") == BIBLE_IMAGE_CANDIDATE_PURPOSE
    )
    if not task_is_candidate and not prompt_is_candidate:
        return None
    try:
        reserved = bible_image_candidate_reserved_snapshot(prompt)
    except WorkflowBuildError as exc:
        raise _conflict(
            "bible_image_candidate_target_invalid",
            "实体图片候选 Prompt 的冻结来源校验失败。",
        ) from exc
    if reserved is None or not task_is_candidate:
        raise _conflict(
            "bible_image_candidate_target_invalid",
            "实体图片候选的 Prompt 与任务用途不一致。",
        )
    for key in (
        "generation_purpose", "idempotency_key", "request_fingerprint",
        "bible_target", "source_snapshot_sha256", "quality",
    ):
        if snapshot.get(key) != reserved.get(key):
            raise _conflict(
                "bible_image_candidate_target_invalid",
                "实体图片候选 Prompt 与任务冻结快照不一致。",
            )
    target = snapshot.get("bible_target")
    request_fingerprint = snapshot.get("request_fingerprint")
    if not isinstance(target, dict):
        raise _conflict(
            "bible_image_candidate_target_invalid",
            "实体图片候选任务缺少可信的冻结目标。",
        )
    category = target.get("reference_category")
    entity_id = target.get("entity_id")
    source_fingerprint = target.get("source_fingerprint")
    if (
        type(target.get("schema_version")) is not int
        or target["schema_version"] != BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION
        or category not in _ENTITY_MODELS
        or type(entity_id) is not str
        or type(target.get("entity_name")) is not str
        or type(source_fingerprint) is not str
        or not _SHA256_RE.fullmatch(source_fingerprint)
        or type(request_fingerprint) is not str
        or not _SHA256_RE.fullmatch(request_fingerprint)
    ):
        raise _conflict(
            "bible_image_candidate_target_invalid",
            "实体图片候选任务的冻结目标校验失败。",
        )
    try:
        uuid.UUID(entity_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise _conflict(
            "bible_image_candidate_target_invalid",
            "实体图片候选任务的冻结实体 ID 无效。",
        ) from exc
    return {
        **target,
        "request_fingerprint": request_fingerprint,
    }


def validate_bible_image_candidate_manifest(
    task: GenerationTask, manifest: dict,
) -> dict | None:
    """Require signed manifest, task snapshot and Prompt options to agree."""

    target = validated_frozen_bible_target(task)
    if target is None:
        return None
    generation = manifest.get("generation") if isinstance(manifest, dict) else None
    capability = task.capability_snapshot if isinstance(task.capability_snapshot, dict) else {}
    workflow = task.generated_workflow
    if not isinstance(generation, dict) or (
        generation.get("generation_purpose") != BIBLE_IMAGE_CANDIDATE_PURPOSE
        or generation.get("request_fingerprint") != target["request_fingerprint"]
        or generation.get("bible_target") != {
            key: target[key]
            for key in (
                "schema_version", "reference_category", "entity_id", "entity_name",
                "source_fingerprint",
            )
        }
        or generation.get("generated_workflow_id") != str(workflow.id)
        or generation.get("prompt_artifact_id") != str(workflow.prompt_artifact_id)
        or generation.get("workflow_sha256") != workflow.content_hash
        or generation.get("template_id") != str(capability.get("template_id") or "")
        or generation.get("template_hash") != str(capability.get("template_hash") or "")
        or generation.get("template_slug") != str(capability.get("template_slug") or "")
        or generation.get("template_version") != task.workflow_version
    ):
        raise _conflict(
            "bible_image_candidate_provenance_mismatch",
            "实体图片候选的签名来源清单与冻结任务目标不一致。",
        )
    return target


def bible_image_candidate_promotion_metadata(
    *,
    task: GenerationTask,
    output_index: int,
    entity: CharacterBible | SceneBible | PropBible,
    manifest: dict,
    confirmed_by,
    confirmed_at,
    source_status: dict | None = None,
) -> dict:
    target = validate_bible_image_candidate_manifest(task, manifest)
    if target is None:
        raise ValueError("task is not a Bible image candidate")
    if str(entity.id) != target["entity_id"] or entity.project_id != task.project_id:
        raise _conflict(
            "bible_image_candidate_target_conflict",
            "推广目标与任务冻结的 Bible 实体不一致。",
        )
    workflow = task.generated_workflow
    prompt = workflow.prompt_artifact
    generation = manifest["generation"]
    artifact_sha256 = str((manifest.get("artifact") or {}).get("sha256") or "")
    manifest_sha256 = str((manifest.get("integrity") or {}).get("payload_sha256") or "")
    source_current = (
        bool(source_status.get("source_current"))
        if isinstance(source_status, dict) and "source_current" in source_status
        else True
    )
    current_source_fingerprint = (
        str(source_status.get("current_source_fingerprint") or target["source_fingerprint"])
        if isinstance(source_status, dict)
        else target["source_fingerprint"]
    )
    return {
        "schema_version": BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION,
        "generation_purpose": BIBLE_IMAGE_CANDIDATE_PURPOSE,
        "reference_category": target["reference_category"],
        "entity_id": target["entity_id"],
        "entity_name": target["entity_name"],
        "source_fingerprint": target["source_fingerprint"],
        "source_current": source_current,
        "current_source_fingerprint": current_source_fingerprint,
        "stale_source_accepted": not source_current,
        "request_fingerprint": target["request_fingerprint"],
        "task_id": str(task.id),
        "output_index": output_index,
        "prompt_artifact_id": str(prompt.id),
        "prompt_content_hash": generation["prompt_sha256"],
        "generated_workflow_id": generation["generated_workflow_id"],
        "workflow_content_hash": generation["workflow_sha256"],
        "template_id": generation["template_id"],
        "template_hash": generation["template_hash"],
        "template_slug": generation["template_slug"],
        "template_version": generation["template_version"],
        "artifact_sha256": artifact_sha256,
        "manifest_sha256": manifest_sha256,
        "confirmed_by": str(confirmed_by.id),
        "confirmed_at": confirmed_at.isoformat(),
    }


def _asset_summary(asset: Asset | None, *, request) -> dict | None:
    if asset is None:
        return None
    preview_url = AssetSerializer(context={"request": request}).get_preview_url(asset)
    return {
        "id": str(asset.id),
        "kind": asset.kind,
        "name": asset.name,
        "checksum": asset.checksum,
        "preview_url": preview_url,
    }


def _candidate_chain_for_prompt(prompt: PromptArtifact):
    workflows = list(getattr(prompt, "_bible_image_workflows", []))
    workflow = workflows[0] if workflows else None
    tasks = list(getattr(workflow, "_bible_image_tasks", [])) if workflow else []
    task = tasks[0] if tasks else None
    return workflow, task


def _prompt_inputs_for_prompt(prompt: PromptArtifact, options: dict) -> dict:
    source_snapshot = options.get("source_snapshot")
    if isinstance(source_snapshot, dict) and isinstance(source_snapshot.get("prompt_inputs"), dict):
        return source_snapshot["prompt_inputs"]
    payload = prompt.structured_payload if isinstance(prompt.structured_payload, dict) else {}
    if isinstance(payload.get("prompt_inputs"), dict):
        return payload["prompt_inputs"]
    return {
        "schema_version": 1,
        "positive": [],
        "negative": [],
        "final_prompt_text": prompt.prompt_text or "",
        "negative_prompt": prompt.negative_prompt or "",
    }


def _candidate_projection_for_prompt(
    *,
    project: SeriesProject,
    entity_type: str,
    entity: CharacterBible | SceneBible | PropBible,
    prompt: PromptArtifact,
    current_source_fingerprint: str,
    output_serializer: GenerationTaskSerializer,
    request,
) -> dict:
    workflow, task = _candidate_chain_for_prompt(prompt)
    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    target = options.get("bible_target") if isinstance(options.get("bible_target"), dict) else {}
    candidate_direction = str(options.get("extra_direction") or "")
    prompt_overrides = options.get("prompt_overrides")
    if prompt_overrides is None:
        prompt_overrides = {}
    if not isinstance(prompt_overrides, dict) or any(
        type(key) is not str or type(value) is not str
        for key, value in prompt_overrides.items()
    ):
        prompt_overrides = {}
    _candidate_result, candidate_current_fingerprint = (
        build_bible_image_candidate_input(
            project, entity_type, entity, candidate_direction, prompt_overrides,
        )
    )
    output_media = output_serializer.get_output_media(task) if task else []
    promoted_ids = {
        item.get("promoted_asset_id")
        for item in output_media
        if item.get("promoted_asset_id")
    }
    promoted_assets = list(Asset.objects.filter(
        pk__in=promoted_ids, owner=project.owner,
    ).order_by("name", "id")) if promoted_ids else []
    persisted_as_asset = bool(promoted_assets)
    return {
        "prompt_artifact_id": str(prompt.id),
        "generated_workflow_id": str(workflow.id) if workflow else None,
        "generation_task_id": str(task.id) if task else None,
        "task_status": task.status if task else "incomplete",
        "created_at": task.created_at if task else prompt.created_at,
        "idempotency_key": str(options.get("idempotency_key") or ""),
        "request_fingerprint": str(options.get("request_fingerprint") or ""),
        "source_fingerprint": str(target.get("source_fingerprint") or ""),
        "source_current": (
            target.get("source_fingerprint") == candidate_current_fingerprint
        ),
        "quality": options.get("quality") if isinstance(options.get("quality"), dict) else {},
        "prompt_inputs": _prompt_inputs_for_prompt(prompt, options),
        "output_media": output_media,
        "promoted_assets": [
            _asset_summary(asset, request=request) for asset in promoted_assets
        ],
        "review_required": not persisted_as_asset,
        "persisted_as_asset": persisted_as_asset,
    }


def project_bible_image_candidates(project: SeriesProject, *, request) -> dict:
    entities: list[tuple[str, Any]] = []
    for entity_type, relation in (
        ("character", project.characters),
        ("scene", project.scenes),
        ("prop", project.props),
    ):
        entities.extend((entity_type, entity) for entity in relation.all())
    entities.sort(key=lambda item: (item[0], item[1].name.casefold(), str(item[1].id)))

    task_queryset = GenerationTask.objects.select_related(
        "generated_workflow__template",
    ).order_by("-queued_at", "-created_at", "-id")
    workflow_queryset = GeneratedWorkflow.objects.select_related("template").prefetch_related(
        Prefetch("tasks", queryset=task_queryset, to_attr="_bible_image_tasks"),
    ).order_by("-created_at", "-id")
    prompts = list(PromptArtifact.objects.filter(
        project=project,
        prompt_type=PromptArtifact.PromptType.IMAGE,
        request_options__generation_purpose=BIBLE_IMAGE_CANDIDATE_PURPOSE,
    ).prefetch_related(
        Prefetch("workflows", queryset=workflow_queryset, to_attr="_bible_image_workflows"),
    ).order_by("-created_at", "-id"))
    prompts_by_target: dict[tuple[str, str], list[PromptArtifact]] = {}
    for prompt in prompts:
        options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
        target = options.get("bible_target")
        if not isinstance(target, dict):
            continue
        key = (
            str(target.get("reference_category") or ""),
            str(target.get("entity_id") or ""),
        )
        prompts_by_target.setdefault(key, []).append(prompt)

    output_serializer = GenerationTaskSerializer(context={"request": request})
    candidates = []
    with_candidate = 0
    for entity_type, entity in entities:
        default_result, current_source_fingerprint = build_bible_image_candidate_input(
            project, entity_type, entity, "",
        )
        target_prompts = prompts_by_target.get((entity_type, str(entity.id)), [])
        candidate_history = [
            _candidate_projection_for_prompt(
                project=project,
                entity_type=entity_type,
                entity=entity,
                prompt=prompt,
                current_source_fingerprint=current_source_fingerprint,
                output_serializer=output_serializer,
                request=request,
            )
            for prompt in target_prompts
        ]
        latest = candidate_history[0] if candidate_history else None
        if candidate_history:
            with_candidate += 1
        candidates.append({
            "entity_type": entity_type,
            "entity_id": str(entity.id),
            "entity_name": entity.name,
            "source_fingerprint": current_source_fingerprint,
            "current_prompt_inputs": default_result.payload.get("prompt_inputs") or {},
            "anchor_asset": _asset_summary(entity.anchor_asset, request=request),
            "reference_assets": [
                _asset_summary(asset, request=request)
                for asset in sorted(
                    entity.reference_assets.all(),
                    key=lambda item: (item.name.casefold(), str(item.id)),
                )
            ],
            "latest_candidate": latest,
            "candidate_history": candidate_history,
        })
    counts = {
        "characters": sum(item[0] == "character" for item in entities),
        "scenes": sum(item[0] == "scene" for item in entities),
        "props": sum(item[0] == "prop" for item in entities),
        "total": len(entities),
        "with_candidate": with_candidate,
    }
    return {
        "project_id": str(project.id),
        "prompt_config": project_bible_image_prompt_config(project),
        "candidates": candidates,
        "counts": counts,
    }
