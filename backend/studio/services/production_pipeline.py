from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
import uuid
from copy import deepcopy

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from rest_framework import status
from rest_framework.exceptions import APIException

from studio.models import (
    Asset,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    SeriesProject,
    WorkflowTemplate,
    WorkerLease,
)
from studio.services.resolution import H3_RESOLUTION_MULTIPLE_DEFAULT
from studio.services.h3_prompts import (
    H3PromptOptions,
    PROMPT_INPUT_FINGERPRINT_VERSION,
    SDXL_BASE_MODEL_ID,
    build_consistent_image_prompt,
    build_h3_video_prompt,
    build_negative_prompt,
    capture_consistency_snapshot,
    prompt_input_fingerprint,
)
from studio.services.reference_bindings import (
    ReferenceBindingError,
    resolve_group_reference_bindings,
)
from studio.services.system_workflow_contracts import (
    canonical_workflow_json,
    compiled_minimax_reference_image_definition,
    is_approved_minimax_reference_image_definition,
    video_output_contract,
)
from studio.services.workflow import WorkflowBuildError, build_workflow


BIBLE_IMAGE_CANDIDATE_PURPOSE = "bible_image_candidate"
BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION = 1
BIBLE_IMAGE_TEMPLATE_SLUG = "minimax-reference-image"
BIBLE_IMAGE_PROMPT_TEMPLATE_KEY = "sdxl_txt2img_bible_v1"
BIBLE_IMAGE_MODEL_CHECKPOINT = "sd_xl_base_1.0.safetensors"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
def _workflow_template_contract_sha256(template: WorkflowTemplate) -> str:
    """Freeze the executable template contract, not one generated workflow."""

    payload = {
        "id": str(template.id),
        "owner_id": str(template.owner_id) if template.owner_id else None,
        "slug": template.slug,
        "version": template.version,
        "provider": template.provider,
        "output_kind": template.output_kind,
        "model": template.model,
        "definition": template.definition,
        "input_schema": template.input_schema,
        "is_system": template.is_system,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def workflow_template_contract_sha256(template: WorkflowTemplate) -> str:
    """Public immutable-template digest used by deferred production."""

    return _workflow_template_contract_sha256(template)


class ProductionQueueConfirmationRequired(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "production_queue_confirmation_required"
    default_detail = "本次生产任务超过当前队列推荐阈值，需要确认后继续。"

    def __init__(self, *, snapshot: dict[str, int]):
        super().__init__({"detail": self.default_detail, **snapshot})
        # APIException coerces nested scalar values to ErrorDetail.  Keep the
        # documented queue counters numeric for clients.
        self.detail = {"detail": self.default_detail, **snapshot}


class ProductionIdempotencyConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "production_idempotency_conflict"
    default_detail = "该 idempotency_key 已用于不同的生产请求。"


class BibleImageCandidateReservedOperation(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "bible_image_candidate_reserved_pipeline"
    default_detail = "实体图片候选只能由项目级 bible-image-candidates 入口物化。"


def bible_image_template_is_compliant(template: WorkflowTemplate) -> bool:
    """Validate the one executable text-only SDXL candidate graph."""

    schema = template.input_schema if isinstance(template.input_schema, dict) else {}
    return bool(
        is_approved_minimax_reference_image_definition(template.definition)
        and template.owner_id is None
        and template.is_system
        and template.is_active
        and template.slug == BIBLE_IMAGE_TEMPLATE_SLUG
        and template.provider == "comfyui"
        and template.output_kind == WorkflowTemplate.OutputKind.IMAGE
        and template.model == BIBLE_IMAGE_MODEL_CHECKPOINT
        and schema.get("workflow_mode") == "text_to_image"
        and schema.get("conditioning_mode") == "text_and_bible_anchors_only"
        and schema.get("pixel_reference_inputs") is False
        and schema.get("fixed_model") is True
        and schema.get("model_id") == SDXL_BASE_MODEL_ID
        and schema.get("prompt_template_keys") == [BIBLE_IMAGE_PROMPT_TEMPLATE_KEY]
        and "MODEL" not in set(schema.get("allowed_overrides") or [])
    )


def bible_image_candidate_reserved_snapshot(prompt: PromptArtifact) -> dict | None:
    """Derive trusted task fields from the persisted candidate Prompt only."""

    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    if options.get("generation_purpose") != BIBLE_IMAGE_CANDIDATE_PURPOSE:
        return None
    target = options.get("bible_target")
    source_snapshot = options.get("source_snapshot")
    request_fingerprint = options.get("request_fingerprint")
    idempotency_key = options.get("idempotency_key")
    quality = options.get("quality")
    if (
        prompt.prompt_type != PromptArtifact.PromptType.IMAGE
        or not isinstance(target, dict)
        or set(target) != {
            "schema_version", "reference_category", "entity_id", "entity_name",
            "source_fingerprint",
        }
        or type(target.get("schema_version")) is not int
        or target["schema_version"] != BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION
        or target.get("reference_category") not in {"character", "scene", "prop"}
        or type(target.get("entity_id")) is not str
        or type(target.get("entity_name")) is not str
        or type(target.get("source_fingerprint")) is not str
        or not _SHA256_RE.fullmatch(target["source_fingerprint"])
        or type(request_fingerprint) is not str
        or not _SHA256_RE.fullmatch(request_fingerprint)
        or type(idempotency_key) is not str
        or not 1 <= len(idempotency_key) <= 120
        or not isinstance(source_snapshot, dict)
        or not isinstance(quality, dict)
        or type(options.get("extended_generation_confirmed")) is not bool
    ):
        raise WorkflowBuildError("Bible image candidate Prompt reserved fields are invalid.")
    try:
        uuid.UUID(target["entity_id"])
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkflowBuildError("Bible image candidate target UUID is invalid.") from exc
    source_snapshot_hash = hashlib.sha256(json.dumps(
        source_snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    if source_snapshot_hash != target["source_fingerprint"]:
        raise WorkflowBuildError("Bible image candidate source snapshot hash is invalid.")
    return {
        "generation_purpose": BIBLE_IMAGE_CANDIDATE_PURPOSE,
        "idempotency_key": idempotency_key,
        "request_fingerprint": request_fingerprint,
        "bible_target": deepcopy(target),
        "source_snapshot_sha256": source_snapshot_hash,
        "quality": deepcopy(quality),
        "extended_generation_confirmed": options["extended_generation_confirmed"],
    }


def can_execute_template(template: WorkflowTemplate, user) -> bool:
    """Only curated global system templates may execute for non-staff users."""

    if not getattr(user, "is_authenticated", False) or not template.is_active:
        return False
    return bool(
        getattr(user, "is_staff", False)
        or (template.owner_id is None and template.is_system)
    )


def executable_templates(queryset, user):
    queryset = queryset.filter(is_active=True)
    if getattr(user, "is_staff", False):
        return queryset
    return queryset.filter(owner__isnull=True, is_system=True)


def assert_queue_capacity(
    user,
    additional: int = 1,
    *,
    extended_generation_confirmed: bool = False,
) -> dict[str, int]:
    """Require acknowledgement above queue guidance without capping volume."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("queue capacity must be checked inside transaction.atomic()")
    WorkerLease.objects.get_or_create(key="generation-capacity")
    WorkerLease.objects.select_for_update().get(key="generation-capacity")
    active_statuses = (
        GenerationTask.Status.QUEUED,
        GenerationTask.Status.RUNNING,
        GenerationTask.Status.CANCEL_REQUESTED,
    )
    active = GenerationTask.objects.filter(status__in=active_statuses).count()
    user_active = GenerationTask.objects.filter(
        owner=user, status__in=active_statuses,
    ).count()
    requested = max(1, int(additional))
    snapshot = {
        "requested_task_count": requested,
        "active_task_count": active,
        "user_active_task_count": user_active,
        "recommended_max_pending": settings.TASK_RECOMMENDED_MAX_PENDING,
        "recommended_max_active_per_user": settings.TASK_RECOMMENDED_MAX_ACTIVE_PER_USER,
        "max_concurrent": settings.TASK_MAX_CONCURRENT,
    }
    exceeds_recommendation = (
        active + requested > settings.TASK_RECOMMENDED_MAX_PENDING
        or user_active + requested > settings.TASK_RECOMMENDED_MAX_ACTIVE_PER_USER
    )
    if exceeds_recommendation and not extended_generation_confirmed:
        raise ProductionQueueConfirmationRequired(snapshot=snapshot)
    return snapshot


@dataclass(frozen=True)
class CompiledPromptInput:
    project: SeriesProject
    episode: object
    shot: object | None
    prompt_type: str
    requested_h3_mode: str
    effective_h3_mode: str
    include_audio: bool
    is_multi_shot: bool
    total_duration_ms: int
    prompt_text: str
    negative_prompt: str
    structured_payload: dict
    consistency_snapshot: dict
    request_options: dict
    content_hash: str
    input_fingerprint: str
    reference_bindings: list[dict]


def compile_prompt_input(
    *,
    project: SeriesProject,
    shots: list,
    prompt_type: str,
    mode: str,
    include_audio: bool,
    duration_overrides: dict[str, int] | None = None,
    overall_soundscape: str = "",
    non_diegetic_music: str = "",
    extra_direction: str = "",
    single_shot_reference_bindings: dict | None = None,
    single_shot_reference_assets: list[Asset] | None = None,
    consistency_snapshot: dict | None = None,
) -> CompiledPromptInput:
    """Compile one prompt group without writing Prompt/Workflow/Task rows."""

    if not shots:
        raise ValueError("at least one shot is required")
    if prompt_type == PromptArtifact.PromptType.IMAGE and len(shots) != 1:
        raise ValueError("image prompt compilation requires exactly one shot")
    if any(shot.episode.project_id != project.id for shot in shots):
        raise ValueError("prompt shots do not belong to the project")
    duration_overrides = dict(duration_overrides or {})
    snapshot = (
        deepcopy(consistency_snapshot)
        if consistency_snapshot is not None
        else capture_consistency_snapshot(project)
    )
    options = H3PromptOptions(
        mode=mode,
        include_audio=include_audio,
        language=project.language,
        duration_overrides=duration_overrides,
        overall_soundscape=overall_soundscape,
        non_diegetic_music=non_diegetic_music,
        extra_direction=extra_direction,
        single_shot_reference_bindings=single_shot_reference_bindings,
        single_shot_reference_assets=single_shot_reference_assets,
        consistency_snapshot=snapshot,
    )
    if prompt_type == PromptArtifact.PromptType.VIDEO:
        try:
            reference_bindings = resolve_group_reference_bindings(
                project=project,
                shots=shots,
                mode=mode,
                snapshot=snapshot,
                single_shot_bindings=single_shot_reference_bindings,
                single_shot_reference_assets=single_shot_reference_assets,
            )
        except ReferenceBindingError as exc:
            from studio.services.h3_prompts import PromptBuildError

            raise PromptBuildError(str(exc)) from exc
    else:
        reference_bindings = []
    fingerprint = prompt_input_fingerprint(
        project,
        shots,
        options,
        prompt_type=prompt_type,
        consistency_snapshot=snapshot,
        reference_bindings=reference_bindings,
    )
    negative_prompt = build_negative_prompt(project, shots)
    if prompt_type == PromptArtifact.PromptType.IMAGE:
        result = build_consistent_image_prompt(
            project,
            shots[0],
            extra_direction,
            consistency_snapshot=snapshot,
        )
    else:
        result = build_h3_video_prompt(project, shots, options)
    structured_payload = {
        **result.payload,
        "input_fingerprint_version": PROMPT_INPUT_FINGERPRINT_VERSION,
        "input_fingerprint": fingerprint,
    }
    effective_h3_mode = str(
        structured_payload.get("effective_conditioning_mode") or mode
    )
    target_shot = shots[0] if len(shots) == 1 else None
    shot_ids = [str(item.id) for item in shots]
    return CompiledPromptInput(
        project=project,
        episode=shots[0].episode,
        shot=target_shot,
        prompt_type=prompt_type,
        requested_h3_mode=mode,
        effective_h3_mode=effective_h3_mode,
        include_audio=(include_audio if prompt_type == PromptArtifact.PromptType.VIDEO else False),
        is_multi_shot=len(shots) > 1,
        total_duration_ms=result.total_duration_ms,
        prompt_text=result.text,
        negative_prompt=negative_prompt,
        structured_payload=structured_payload,
        consistency_snapshot=result.consistency_snapshot,
        request_options={
            "shot_ids": shot_ids,
            "duration_overrides": duration_overrides,
            "overall_soundscape": overall_soundscape,
            "non_diegetic_music": non_diegetic_music,
            "extra_direction": extra_direction,
            "reference_bindings_override": single_shot_reference_bindings,
            "reference_asset_ids_override": [
                str(item.id) for item in (single_shot_reference_assets or [])
            ],
            "input_fingerprint_version": PROMPT_INPUT_FINGERPRINT_VERSION,
            "input_fingerprint": fingerprint,
        },
        content_hash=result.content_hash,
        input_fingerprint=fingerprint,
        reference_bindings=deepcopy(reference_bindings),
    )


def materialize_compiled_prompt(
    *,
    compiled: CompiledPromptInput,
    created_by,
    request_options_additions: dict | None = None,
    h3_mode: str | None = None,
) -> PromptArtifact:
    """Persist an already compiled prompt without recompiling mutable inputs."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("compiled prompt materialization requires transaction.atomic()")
    additions = deepcopy(request_options_additions or {})
    protected = {
        "shot_ids", "duration_overrides", "overall_soundscape",
        "non_diegetic_music", "extra_direction", "reference_bindings_override",
        "reference_asset_ids_override", "input_fingerprint_version",
        "input_fingerprint",
    }
    if protected & set(additions):
        raise ValueError("request option additions cannot replace compiled input fields")
    last_revision = PromptArtifact.objects.filter(
        project=compiled.project,
        episode=compiled.episode,
        shot=compiled.shot,
        prompt_type=compiled.prompt_type,
    ).aggregate(value=Max("revision"))["value"] or 0
    artifact = materialize_prompt_artifact(
        project=compiled.project,
        episode=compiled.episode,
        shot=compiled.shot,
        created_by=created_by,
        prompt_type=compiled.prompt_type,
        model=str(
            compiled.structured_payload.get("model")
            or compiled.project.target_model
            or "minimax-h3"
        ),
        h3_mode=h3_mode or compiled.requested_h3_mode,
        revision=last_revision + 1,
        is_multi_shot=compiled.is_multi_shot,
        include_audio=compiled.include_audio,
        total_duration_ms=compiled.total_duration_ms,
        prompt_text=compiled.prompt_text,
        negative_prompt=compiled.negative_prompt,
        structured_payload=deepcopy(compiled.structured_payload),
        consistency_snapshot=deepcopy(compiled.consistency_snapshot),
        request_options={**deepcopy(compiled.request_options), **additions},
        content_hash=compiled.content_hash,
    )
    artifact._production_prompt_reused = False
    return artifact


def materialize_prompt_artifact(
    *,
    project: SeriesProject,
    episode,
    shot,
    created_by,
    prompt_type: str,
    model: str,
    h3_mode: str,
    revision: int,
    is_multi_shot: bool,
    include_audio: bool,
    total_duration_ms: int,
    prompt_text: str,
    negative_prompt: str,
    structured_payload: dict,
    consistency_snapshot: dict,
    request_options: dict,
    content_hash: str,
) -> PromptArtifact:
    """Persist one compiled prompt artifact through the shared production path."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("prompt materialization requires transaction.atomic()")
    if episode is not None and episode.project_id != project.id:
        raise ValueError("episode does not belong to the prompt project")
    if shot is not None and shot.episode.project_id != project.id:
        raise ValueError("shot does not belong to the prompt project")
    if shot is not None and episode is not None and shot.episode_id != episode.id:
        raise ValueError("shot does not belong to the prompt episode")
    if (
        not getattr(created_by, "is_staff", False)
        and project.owner_id != getattr(created_by, "id", None)
    ):
        raise ValueError("prompt creator cannot write to this project")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("prompt revision must be a positive integer")
    if content_hash != hashlib.sha256(prompt_text.encode("utf-8")).hexdigest():
        raise ValueError("prompt content_hash does not match prompt_text")
    if not isinstance(structured_payload, dict) or not isinstance(request_options, dict):
        raise ValueError("prompt payloads must be JSON objects")

    return PromptArtifact.objects.create(
        project=project,
        episode=episode,
        shot=shot,
        created_by=created_by,
        prompt_type=prompt_type,
        model=model,
        h3_mode=h3_mode,
        revision=revision,
        is_multi_shot=is_multi_shot,
        include_audio=include_audio,
        total_duration_ms=total_duration_ms,
        prompt_text=prompt_text,
        negative_prompt=negative_prompt,
        structured_payload=structured_payload,
        consistency_snapshot=consistency_snapshot,
        request_options=request_options,
        content_hash=content_hash,
    )


def materialize_generated_workflow(
    *,
    user,
    template: WorkflowTemplate,
    prompt: PromptArtifact,
    overrides: dict[str, Any] | None = None,
) -> GeneratedWorkflow:
    """Compile and persist one immutable workflow from a persisted prompt."""

    if not can_execute_template(template, user):
        raise WorkflowBuildError("The workflow template is not executable by this user.")
    if template.output_kind != prompt.prompt_type:
        raise WorkflowBuildError("模板输出类型与提示词类型不一致")
    if (
        not getattr(user, "is_staff", False)
        and prompt.project.owner_id != getattr(user, "id", None)
    ):
        raise WorkflowBuildError("提示词不属于当前用户项目")
    reserved_snapshot = bible_image_candidate_reserved_snapshot(prompt)
    if reserved_snapshot is not None:
        if not bible_image_template_is_compliant(template):
            raise WorkflowBuildError("Bible image candidate template is not compliant.")
        if prompt.model != template.model:
            raise WorkflowBuildError("Bible image candidate Prompt model is not frozen to its template.")
        if prompt.structured_payload.get("prompt_template_key") != BIBLE_IMAGE_PROMPT_TEMPLATE_KEY:
            raise WorkflowBuildError("Bible image candidate Prompt template key is invalid.")

    workflow_json, injected, content_hash = build_workflow(
        template, prompt, prompt.project, overrides or {},
    )
    if reserved_snapshot is not None:
        try:
            reviewed_workflow = compiled_minimax_reference_image_definition(injected)
        except (TypeError, ValueError, KeyError) as exc:
            raise WorkflowBuildError(
                "Bible image candidate compiled graph is missing reviewed injections."
            ) from exc
        injections_are_exact = bool(
            injected.get("MODEL") == BIBLE_IMAGE_MODEL_CHECKPOINT
            and injected.get("PROMPT") == prompt.prompt_text
            and injected.get("NEGATIVE_PROMPT") == prompt.negative_prompt
            and type(injected.get("SEED")) is int
            and type(injected.get("WIDTH")) is int
            and injected["WIDTH"] > 0
            and type(injected.get("HEIGHT")) is int
            and injected["HEIGHT"] > 0
            and type(injected.get("OUTPUT_PREFIX")) is str
            and bool(injected["OUTPUT_PREFIX"])
        )
        if (
            not injections_are_exact
            or canonical_workflow_json(workflow_json)
            != canonical_workflow_json(reviewed_workflow)
        ):
            raise WorkflowBuildError("Bible image candidate compiled graph is not text-only SDXL.")
    return GeneratedWorkflow.objects.create(
        project=prompt.project,
        episode=prompt.episode,
        prompt_artifact=prompt,
        template=template,
        created_by=user,
        workflow_json=workflow_json,
        injected_values=injected,
        content_hash=content_hash,
    )


def materialize_generation_task(
    *,
    user,
    workflow: GeneratedWorkflow,
    priority: int,
    max_attempts: int,
    capability_snapshot_overrides: dict | None = None,
) -> GenerationTask:
    """Persist one queue task and its immutable execution capability snapshot."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("task materialization requires transaction.atomic()")
    injected = workflow.injected_values or {}
    prompt = workflow.prompt_artifact
    reserved_snapshot = bible_image_candidate_reserved_snapshot(prompt)
    if not can_execute_template(workflow.template, user):
        raise WorkflowBuildError("The workflow template is not executable by this user.")
    if workflow.project_id != prompt.project_id:
        raise WorkflowBuildError("工作流与提示词项目不一致")
    if workflow.episode_id != prompt.episode_id:
        raise WorkflowBuildError("工作流与提示词剧集不一致")
    if workflow.template.output_kind != prompt.prompt_type:
        raise WorkflowBuildError("工作流模板与提示词输出类型不一致")
    if (
        not getattr(user, "is_staff", False)
        and workflow.project.owner_id != getattr(user, "id", None)
    ):
        raise WorkflowBuildError("工作流不属于当前用户项目")
    is_image = prompt.prompt_type == PromptArtifact.PromptType.IMAGE
    episode = workflow.episode or prompt.episode
    shot = prompt.shot
    request_shot_ids = (prompt.request_options or {}).get("shot_ids")
    shot_ids = (
        [str(value) for value in request_shot_ids]
        if isinstance(request_shot_ids, list)
        else ([str(shot.id)] if shot else [])
    )
    if is_image:
        backend = "comfyui_local"
        profile = f"image:{workflow.template.slug}:v{workflow.template.version}"
    else:
        backend = "comfy_local_h3"
        workflow_mode = (workflow.template.input_schema or {}).get(
            "workflow_mode", "legacy",
        )
        profile = (workflow.template.input_schema or {}).get("model_profile") or {
            "text_to_video": "local_h3_t2va_official",
            "first_last_to_video": "local_h3_i2v_official",
            "reference_to_video": "local_h3_ref2va_official",
        }.get(workflow_mode, "local_h3_t2va_official")
    reference_records = prompt.structured_payload.get("reference_bindings", [])
    user_parameter_definitions = (
        (workflow.template.input_schema or {}).get("user_parameters") or {}
    )
    workflow_parameters = {
        key: injected.get(key, definition.get("default"))
        for key, definition in user_parameter_definitions.items()
        if isinstance(key, str) and isinstance(definition, dict)
    }
    encoded_workflow = json.dumps(
        workflow.workflow_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if hashlib.sha256(encoded_workflow.encode("utf-8")).hexdigest() != workflow.content_hash:
        raise WorkflowBuildError("工作流 content_hash 与持久化图不一致")
    protected_snapshot_keys = {
        "generated_workflow_id", "workflow_hash", "prompt_artifact_id",
        "prompt_content_hash", "template_id", "template_hash", "template_slug",
        "model", "h3_mode",
        "effective_conditioning_mode", "prompt_template_key", "workflow_mode",
        "source_workflow", "workflow_parameters", "resolution_profile",
        "resolution_multiple", "width", "height", "reference_assets",
        "output_contract",
    }
    supplied_overrides = set(capability_snapshot_overrides or {})
    if supplied_overrides & protected_snapshot_keys:
        raise WorkflowBuildError(
            "capability snapshot overrides cannot replace authoritative workflow fields"
        )
    reserved_keys = {
        "generation_purpose", "idempotency_key", "request_fingerprint",
        "bible_target", "source_snapshot_sha256", "quality",
    }
    if reserved_snapshot is not None and supplied_overrides & reserved_keys:
        raise WorkflowBuildError(
            "candidate capability fields must be derived from Prompt request_options"
        )
    capability_snapshot = {
        "generated_workflow_id": str(workflow.id),
        "workflow_hash": workflow.content_hash,
        "prompt_artifact_id": str(prompt.id),
        "prompt_content_hash": prompt.content_hash,
        "template_id": str(workflow.template_id),
        "template_hash": _workflow_template_contract_sha256(workflow.template),
        "template_slug": workflow.template.slug,
        "model": injected.get("MODEL", prompt.model),
        "h3_mode": prompt.h3_mode,
        "effective_conditioning_mode": prompt.structured_payload.get(
            "effective_conditioning_mode", prompt.h3_mode,
        ),
        "prompt_template_key": prompt.structured_payload.get(
            "prompt_template_key", "",
        ),
        "workflow_mode": (workflow.template.input_schema or {}).get(
            "workflow_mode", "legacy",
        ),
        "source_workflow": (workflow.template.input_schema or {}).get(
            "source_workflow", "",
        ),
        "workflow_parameters": workflow_parameters,
        "resolution_profile": injected.get("RESOLUTION_PROFILE", "project"),
        "resolution_multiple": int(injected.get(
            "RESOLUTION_MULTIPLE", H3_RESOLUTION_MULTIPLE_DEFAULT,
        )),
        "width": int(injected.get("WIDTH", 0)),
        "height": int(injected.get("HEIGHT", 0)),
        "episode_id": str(episode.id) if episode else None,
        "episode_number": episode.number if episode else None,
        "episode_title": episode.title if episode else "",
        "shot_ids": shot_ids,
        "shot_id": str(shot.id) if shot else None,
        "shot_number": shot.number if shot else None,
        "shot_title": shot.title if shot else "",
        "reference_assets": [
            {
                "id": item.get("asset_id") or item.get("id"),
                "checksum": item.get("checksum"),
                "input_slot": item.get("input_slot"),
                "input_label": item.get("input_label"),
                "category": item.get("category"),
                "entity_id": item.get("entity_id"),
                "entity_name": item.get("entity_name"),
                "audio_asset_id": item.get("audio_asset_id"),
                "audio_input_label": item.get("audio_input_label"),
                "use_embedded_audio": item.get("use_embedded_audio", False),
            }
            for item in reference_records
        ],
    }
    if not is_image:
        schema = (
            workflow.template.input_schema
            if isinstance(workflow.template.input_schema, dict)
            else {}
        )
        declared_output_contract = schema.get("output_contract")
        try:
            expected_output_contract = video_output_contract(workflow.workflow_json)
        except ValueError as exc:
            raise WorkflowBuildError(str(exc)) from exc
        if declared_output_contract != expected_output_contract:
            raise WorkflowBuildError(
                "视频模板 output_contract 与已持久化执行图不一致；禁止猜测主视频输出。"
            )
        capability_snapshot.update({
            "fps": int(injected.get("FPS", 0)),
            "include_audio": prompt.include_audio,
            "requested_duration_ms": prompt.total_duration_ms,
            "output_contract": deepcopy(expected_output_contract),
        })
    capability_snapshot.update(capability_snapshot_overrides or {})
    if reserved_snapshot is not None:
        if not bible_image_template_is_compliant(workflow.template):
            raise WorkflowBuildError("Bible image candidate task template is not compliant.")
        capability_snapshot.update(reserved_snapshot)
    if is_image:
        for key in ("fps", "include_audio", "requested_duration_ms"):
            capability_snapshot.pop(key, None)
    return GenerationTask.objects.create(
        owner=user,
        project=workflow.project,
        episode=workflow.episode,
        generated_workflow=workflow,
        kind=prompt.prompt_type,
        priority=priority,
        max_attempts=max_attempts,
        backend=backend,
        model_profile=profile,
        workflow_version=workflow.template.version,
        seed=int(injected.get("SEED", workflow.project.continuity_seed)),
        effective_frames=0 if is_image else int(injected.get("FRAMES", 0)),
        effective_duration_ms=(
            0 if is_image else int(injected.get("EFFECTIVE_DURATION_MS", 0))
        ),
        capability_snapshot=capability_snapshot,
    )


def clone_generation_task_for_retry(
    task: GenerationTask,
    *,
    user,
    extended_generation_confirmed: bool = False,
) -> GenerationTask:
    """Create an auditable retry without mutating the terminal source task."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("retry materialization requires transaction.atomic()")
    if not can_execute_template(task.generated_workflow.template, user):
        raise WorkflowBuildError("The workflow template is not executable by this user.")
    if task.status not in {GenerationTask.Status.FAILED, GenerationTask.Status.CANCELLED}:
        raise WorkflowBuildError("Only failed or cancelled tasks can be retried.")
    if task.outputs:
        raise WorkflowBuildError("A task with successful outputs cannot be retried.")
    if GenerationTask.objects.filter(
        owner=task.owner,
        capability_snapshot__retry_of=str(task.id),
    ).exists():
        raise WorkflowBuildError("This failed task has already been retried.")
    injected = task.generated_workflow.injected_values or {}
    capability_snapshot = {
        **(task.capability_snapshot or {}),
        "resolution_multiple": int(
            (task.capability_snapshot or {}).get("resolution_multiple")
            or injected.get("RESOLUTION_MULTIPLE", H3_RESOLUTION_MULTIPLE_DEFAULT)
        ),
        "retry_of": str(task.id),
        "batch_role": "retry",
        "extended_generation_confirmed": bool(extended_generation_confirmed),
    }
    is_image = task.kind == GenerationTask.Kind.IMAGE
    if is_image:
        for key in ("fps", "include_audio", "requested_duration_ms"):
            capability_snapshot.pop(key, None)
    return GenerationTask.objects.create(
        owner=task.owner,
        project=task.project,
        episode=task.episode,
        generated_workflow=task.generated_workflow,
        kind=task.kind,
        backend=task.backend,
        model_profile=task.model_profile,
        workflow_version=task.workflow_version,
        seed=task.seed,
        effective_frames=0 if is_image else task.effective_frames,
        effective_duration_ms=0 if is_image else task.effective_duration_ms,
        capability_snapshot=capability_snapshot,
        priority=task.priority,
        max_attempts=task.max_attempts,
    )
