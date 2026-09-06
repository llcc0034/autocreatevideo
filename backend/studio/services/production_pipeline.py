from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from copy import deepcopy

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from rest_framework import status
from rest_framework.exceptions import APIException

from studio.models import (
    Asset,
    GeneratedWorkflow,
    GenerationTask,
    ModelProfileVersion,
    PromptArtifact,
    PromptProfileFieldRule,
    SeriesProject,
    Shot,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    WorkerLease,
)
from studio.services.consistency import capture_consistency_snapshot
from studio.services.prompt_compiler_contract import (
    PROMPT_COMPILER_VERSION,
    PromptBuildError,
)
from studio.services.reference_bindings import (
    ReferenceBindingError,
    resolve_group_reference_bindings,
)
from studio.services.workflow import WorkflowBuildError
from studio.services.workflow_compiler import CompiledWorkflow
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.execution_monitoring import (
    execution_monitoring_contract_sha256,
    resolve_execution_monitoring_contract,
    verify_frozen_execution_monitoring_contract,
)
from studio.services.media_capabilities import (
    conditioning_mode_contract,
    resolve_conditioning_contract,
)
from studio.services.media_processing_profiles import resolve_reference_media_profile
from studio.services.configuration import resolve_configuration
from studio.services.prompt_profiles import (
    assemble_prompt_profile,
    bible_image_profile_sources,
    frozen_profile_reference,
    load_frozen_prompt_profile_selection,
    model_family_for_profile,
    frozen_profile_fingerprint,
    profile_fingerprint,
    require_prompt_profile_binding,
    validate_frozen_prompt_profile_binding_reference,
)
from studio.services.registry_reference_lock import registry_reference_write_guard
from studio.services.prompt_profile_compiler import (
    GENERIC_MEDIA_PROMPT_INPUT_FINGERPRINT_VERSION,
    MediaPromptCompilationContext,
    compile_media_prompt_profile,
    media_prompt_input_fingerprint,
)
from studio.services.prompt_translation import canonical_target_collisions
from studio.services.bible_reference_contract import (
    BIBLE_IMAGE_GENERATION_PURPOSE,
    BIBLE_IMAGE_SOURCE_SCHEMA_VERSION,
    is_retired_bible_image_candidate_prompt,
)


class ProductionIdempotencyConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "production_idempotency_conflict"
    default_detail = "该 idempotency_key 已用于不同的生产请求。"


class RetiredBibleImageCandidateOperation(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "retired_bible_image_candidate_pipeline"
    default_detail = "旧 Bible 图片候选入口已退役；请通过 ProductionRun 生成并审核。"


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _require_positive_contract_integer(
    value: Any,
    *,
    field: str,
    context: str,
) -> int:
    """Read a positive integer only from an already-frozen contract."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkflowBuildError(f"{context} 缺少有效 {field}")
    return value


def _require_audio_enabled_semantic(semantics: dict[str, Any]) -> bool:
    """Video audio intent must be present in the compiled semantic snapshot."""

    value = semantics.get("audio.enabled")
    if not isinstance(value, bool):
        raise WorkflowBuildError(
            "semantic compiled workflow 缺少布尔 audio.enabled 冻结语义",
        )
    return value


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


def queue_capacity_advisories(
    *,
    active_task_count: int,
    user_active_task_count: int,
    requested_task_count: int,
    recommended_max_pending: int,
    recommended_max_active_per_user: int,
) -> list[dict[str, Any]]:
    """Describe recommendation overages without turning them into gates."""

    projected_active = active_task_count + requested_task_count
    projected_user_active = user_active_task_count + requested_task_count
    advisories: list[dict[str, Any]] = []
    if projected_active > recommended_max_pending:
        advisories.append({
            "schema_version": 1,
            "code": "recommended_max_pending_exceeded",
            "severity": "warning",
            "scope": "platform",
            "measured": projected_active,
            "recommended_maximum": recommended_max_pending,
        })
    if projected_user_active > recommended_max_active_per_user:
        advisories.append({
            "schema_version": 1,
            "code": "recommended_max_active_per_user_exceeded",
            "severity": "warning",
            "scope": "user",
            "measured": projected_user_active,
            "recommended_maximum": recommended_max_active_per_user,
        })
    return advisories


def capture_queue_capacity_advisory(
    user,
    additional: int,
    *,
    project: SeriesProject,
) -> dict[str, Any]:
    """Freeze configured queue guidance for audit without blocking enqueue."""

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
    resolved = resolve_configuration(
        project=project,
        required_keys=(
            "production.recommended_max_pending",
            "production.recommended_max_active_per_user",
        ),
    )
    recommended_pending = int(resolved.values["production.recommended_max_pending"])
    recommended_user = int(
        resolved.values["production.recommended_max_active_per_user"]
    )
    if isinstance(additional, bool) or not isinstance(additional, int) or additional < 1:
        raise ValueError("queue capacity requested task count 必须是正整数")
    requested = additional
    snapshot = {
        "schema_version": 1,
        "enforcement": "advisory",
        "requested_task_count": requested,
        "active_task_count": active,
        "user_active_task_count": user_active,
        "recommended_max_pending": recommended_pending,
        "recommended_max_active_per_user": recommended_user,
        "max_concurrent": settings.TASK_MAX_CONCURRENT,
        "configuration_public_sha256": hashlib.sha256(
            json.dumps({
                "recommended_max_pending": recommended_pending,
                "recommended_max_active_per_user": recommended_user,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    snapshot["advisories"] = queue_capacity_advisories(
        active_task_count=active,
        user_active_task_count=user_active,
        requested_task_count=requested,
        recommended_max_pending=recommended_pending,
        recommended_max_active_per_user=recommended_user,
    )
    return snapshot


@dataclass(frozen=True)
class CompiledPromptInput:
    project: SeriesProject
    episode: object
    shot: object | None
    prompt_type: str
    requested_conditioning_mode: str
    effective_conditioning_mode: str
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
    prompt_profile_version: object | None = None
    prompt_profile_binding_key: str = ""
    prompt_profile_contract_sha256: str = ""


_PROFILE_REFERENCE_LABEL_RE = re.compile(
    r"<(?:Picture|Video|Audio)\s+[1-9][0-9]*>",
)
_PROFILE_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PROFILE_PROTECTED_VIDEO_TEXT_RE = re.compile(
    r"<(d|lyrics|screen_text)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_PROFILE_PROTECTED_IMAGE_TEXT_RE = re.compile(
    r"<screen_text\b[^>]*>.*?</screen_text>",
    flags=re.IGNORECASE | re.DOTALL,
)


def _validated_media_durations(
    shots: list,
    *,
    prompt_type: str,
    duration_overrides: dict[str, int],
) -> tuple[list[int], dict[str, int]]:
    if prompt_type == PromptArtifact.PromptType.IMAGE:
        return [0 for _shot in shots], {str(item.id): 0 for item in shots}
    durations: list[int] = []
    by_id: dict[str, int] = {}
    for shot in shots:
        raw = duration_overrides.get(str(shot.id), shot.duration_ms)
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise PromptBuildError(f"分镜 {shot.id} 的时长不是整数毫秒") from exc
        if value <= 0:
            raise PromptBuildError("每个分镜时长必须为正整数毫秒，才能保持严格递增时间戳")
        durations.append(value)
        by_id[str(shot.id)] = value
    return durations, by_id


def _media_execution_payload(
    *,
    prompt_type: str,
    mode: str,
    durations: list[int],
    reference_bindings: list[dict],
    model_identifier: str = "",
    adapter_execution_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return immutable execution facts without model-facing semantic prose."""

    if prompt_type == PromptArtifact.PromptType.IMAGE:
        if not str(model_identifier or "").strip():
            raise PromptBuildError("图片生成缺少冻结目标模型标识")
        return {
            "model": str(model_identifier),
            "prompt_compiler_version": PROMPT_COMPILER_VERSION,
            "semantic_assembler": "prompt_profile",
            "mode": "TEXT_TO_IMAGE",
            "effective_conditioning_mode": "TEXT_ONLY",
            "prompt_template_key": "prompt_profile_image_generate_v1",
            "conditioning_contract": "prompt_profile_text_only",
            "pixel_reference_inputs": False,
            "duration_ms": 0,
            "shot_durations_ms": durations,
            "reference_bindings": [],
            "reference_assets": [],
            "continuity_assets": [],
        }
    if not str(mode or "").strip():
        raise PromptBuildError("视频生成缺少冻结工作模式")
    if not str(model_identifier or "").strip():
        raise PromptBuildError("视频生成缺少冻结目标模型标识")
    adapter_facts = adapter_execution_facts or {}
    if not isinstance(adapter_facts, dict):
        raise PromptBuildError("PromptProfile adapter execution_facts 必须是对象")
    unknown_facts = set(adapter_facts) - {
        "effective_conditioning_mode", "prompt_template_key",
    }
    if unknown_facts:
        raise PromptBuildError(
            "PromptProfile adapter execution_facts 含未知字段: "
            + ", ".join(sorted(unknown_facts)),
        )
    effective_mode = str(
        adapter_facts.get("effective_conditioning_mode") or mode,
    )
    template_key = str(adapter_facts.get("prompt_template_key") or "")
    return {
        "model": str(model_identifier),
        "prompt_compiler_version": PROMPT_COMPILER_VERSION,
        "semantic_assembler": "prompt_profile",
        "mode": mode,
        "effective_conditioning_mode": effective_mode,
        "prompt_template_key": template_key,
        "duration_ms": sum(durations),
        "shot_durations_ms": durations,
        "reference_bindings": deepcopy(reference_bindings),
        "reference_assets": deepcopy(reference_bindings),
    }


def _profiled_media_quality(
    *,
    project: SeriesProject,
    prompt_type: str,
    prompt_text: str,
    negative_prompt: str,
    assembly: dict[str, Any],
    language_gate_mode: str,
) -> dict[str, Any]:
    """Evaluate only the semantic channels actually emitted by PromptProfile."""

    protected = (
        _PROFILE_PROTECTED_IMAGE_TEXT_RE
        if prompt_type == PromptArtifact.PromptType.IMAGE
        else _PROFILE_PROTECTED_VIDEO_TEXT_RE
    )
    fields = assembly.get("structured_fields") or {}
    rewrite_fields: list[str] = []
    hard_fields: list[str] = []
    for trace in assembly.get("trace") or []:
        if not isinstance(trace, dict):
            continue
        field_key = str(trace.get("field_key") or "").strip()
        channel = str(trace.get("channel") or "").strip()
        value = fields.get(field_key)
        if isinstance(value, list):
            value_text = "\n".join(str(item) for item in value)
        else:
            value_text = str(value or "")
        if not _PROFILE_CJK_RE.search(protected.sub("", value_text)):
            continue
        if channel == "negative_prompt":
            hard_fields.append(field_key or "prompt_profile.negative_prompt")
        elif channel == "positive_prompt":
            rewrite_fields.append(field_key or "prompt_profile.positive_prompt")
    if _PROFILE_CJK_RE.search(protected.sub("", prompt_text)) and not rewrite_fields:
        rewrite_fields.append("prompt_profile.positive_prompt")
    if _PROFILE_CJK_RE.search(protected.sub("", negative_prompt)) and not hard_fields:
        hard_fields.append("prompt_profile.negative_prompt")
    collisions = canonical_target_collisions(project)
    if collisions:
        hard_fields.append("canonical_target_collision")
    rewrite_fields = list(dict.fromkeys([*rewrite_fields, *hard_fields]))
    hard_fields = list(dict.fromkeys(hard_fields))
    mode = str(language_gate_mode or "block").strip().lower()
    if mode not in {"block", "warn"}:
        mode = "block"
    warnings: list[str] = []
    if rewrite_fields:
        warnings.append(
            "PromptProfile 实际输出的导演字段仍含待审核中文；对白、歌词和真实屏显文字除外。"
        )
    if collisions:
        warnings.append(
            "检测到多个当前 Bible 实体共用同一 canonical English target；请先修正术语表。"
        )
    word_count = len(re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", prompt_text))
    blocks = bool(hard_fields) or (bool(rewrite_fields) and mode == "block")
    return {
        "status": "needs_attention" if warnings else "ready",
        "language_policy": (
            "English image director prose; preserve authored screen text only"
            if prompt_type == PromptArtifact.PromptType.IMAGE
            else "English director prose; preserve original dialogue, lyrics, and visible text only"
        ),
        "prompt_character_count": len(prompt_text),
        "prompt_character_limit": None,
        "prompt_word_count": word_count,
        "prompt_word_count_is_approximate": True,
        "warnings": warnings,
        "integrity_errors": collisions,
        "fields_requiring_english_rewrite": rewrite_fields,
        "hard_block_fields": hard_fields,
        "production_language_gate": {
            "mode": mode,
            "would_block_enqueue": blocks,
            "message": (
                "Production enqueue will be blocked until the emitted PromptProfile fields are reviewed."
                if blocks else "Production enqueue is not blocked by the emitted PromptProfile fields."
            ),
        },
    }


def _apply_prompt_profile_channel_quality(
    quality: dict[str, Any],
    *,
    assembly: dict[str, Any],
    generation_length_audit: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Merge generic final-channel limits into any adapter quality report."""

    result = deepcopy(quality)
    audit = assembly.get("channel_audit")
    if not isinstance(audit, dict):
        audit = {}
    warnings = [
        str(item) for item in (result.get("warnings") or [])
        if isinstance(item, str) and item
    ]
    for item in assembly.get("warnings") or []:
        if isinstance(item, str) and item and item not in warnings:
            warnings.append(item)
    positive = audit.get("positive_prompt")
    if isinstance(positive, dict):
        result["prompt_character_count"] = int(
            positive.get("character_count") or 0,
        )
        result["prompt_character_limit"] = int(
            positive.get("max_chars") or 0,
        )
    result["prompt_profile_channel_audit"] = deepcopy(audit)
    result["prompt_profile_generation_length_audit"] = deepcopy(
        list(generation_length_audit),
    )
    result["warnings"] = warnings
    if warnings:
        result["status"] = "needs_attention"
    return result


def _validate_profiled_reference_prompt(
    *,
    prompt_text: str,
    negative_prompt: str,
    reference_bindings: list[dict],
) -> None:
    expected = {
        str(label)
        for item in reference_bindings
        for label in (item.get("input_label"), item.get("audio_input_label"))
        if label
    }
    found = set(_PROFILE_REFERENCE_LABEL_RE.findall(prompt_text))
    negative_found = set(_PROFILE_REFERENCE_LABEL_RE.findall(negative_prompt))
    if negative_found:
        raise PromptBuildError("reference binding 标签只能由 positive_prompt 引用")
    if found != expected:
        raise PromptBuildError(
            "PromptProfile positive_prompt 的 <Picture/Video/Audio N> 必须与规范化 "
            f"reference_bindings 一一对应；expected={sorted(expected)}, found={sorted(found)}"
        )


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
    language_gate_mode: str = "block",
    model_profile_version: ModelProfileVersion | None = None,
    frozen_conditioning_contract: dict[str, Any] | None = None,
    frozen_conditioning_contract_sha256: str = "",
    frozen_frame_policy: dict[str, Any] | None = None,
    frozen_prompt_profile: dict[str, Any] | None = None,
    reviewed_profile_fields: list[dict[str, Any]] | None = None,
) -> CompiledPromptInput:
    """Compile one prompt group through exactly one published PromptProfile."""

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
    durations, effective_durations_ms = _validated_media_durations(
        shots,
        prompt_type=prompt_type,
        duration_overrides=duration_overrides,
    )
    conditioning_contract: dict[str, Any] | None = None
    selected_mode_contract: dict[str, Any] | None = None
    conditioning_contract_sha256 = ""
    if prompt_type == PromptArtifact.PromptType.VIDEO:
        if model_profile_version is None:
            raise PromptBuildError(
                "媒体 Prompt 编译必须显式传入已配置的 ModelProfileVersion；"
                "不得从项目目标模型名称推断模型族",
            )
        try:
            model_specification = verified_model_profile_specification(
                model_profile_version,
            )
            explicit_contract = (
                frozen_conditioning_contract is not None
                or bool(str(frozen_conditioning_contract_sha256 or "").strip())
            )
            if explicit_contract:
                if (
                    not isinstance(frozen_conditioning_contract, dict)
                    or not str(frozen_conditioning_contract_sha256 or "").strip()
                ):
                    raise ValidationError({
                        "conditioning_contract": (
                            "显式冻结合同与 conditioning_contract_sha256 必须同时提供"
                        ),
                    })
                conditioning_contract = resolve_conditioning_contract(
                    workflow_input_contract={
                        "conditioning_contract": frozen_conditioning_contract,
                    },
                    output_kind="video",
                    required=True,
                )
                conditioning_contract_sha256 = json_contract(
                    conditioning_contract,
                    max_bytes=_stage_snapshot_max_bytes(),
                ).sha256
                if (
                    conditioning_contract_sha256
                    != str(frozen_conditioning_contract_sha256)
                ):
                    raise ValidationError({
                        "conditioning_contract": "显式冻结合同完整性校验失败",
                    })
            else:
                conditioning_contract = resolve_conditioning_contract(
                    model_specification=model_specification,
                    output_kind="video",
                    required=True,
                )
                conditioning_contract_sha256 = json_contract(
                    conditioning_contract,
                    max_bytes=_stage_snapshot_max_bytes(),
                ).sha256
            selected_mode_contract = conditioning_mode_contract(
                conditioning_contract,
                mode,
            )
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            raise PromptBuildError(
                f"视频 Prompt 编译缺少模式 {mode or '(empty)'} 的冻结条件合同: {exc}"
            ) from exc
        try:
            reference_bindings = resolve_group_reference_bindings(
                project=project,
                shots=shots,
                mode_contract=selected_mode_contract,
                snapshot=snapshot,
                single_shot_bindings=single_shot_reference_bindings,
                single_shot_reference_assets=single_shot_reference_assets,
            )
        except ReferenceBindingError as exc:
            raise PromptBuildError(str(exc)) from exc
    else:
        reference_bindings = []
    operation = (
        "image.generate"
        if prompt_type == PromptArtifact.PromptType.IMAGE
        else "video.generate"
    )
    if model_profile_version is None:
        raise PromptBuildError(
            "媒体 Prompt 编译必须显式传入已配置的 ModelProfileVersion；"
            "不得从项目目标模型名称推断模型族",
        )
    model_identifier = str(model_profile_version.model_identifier or "")
    if not model_identifier:
        raise PromptBuildError("媒体生成缺少目标模型标识")
    model_family = model_family_for_profile(model_profile_version)
    profile_mode = mode if prompt_type == PromptArtifact.PromptType.VIDEO else ""
    resolved_profile = None
    if frozen_prompt_profile is not None:
        if model_profile_version is None:
            raise PromptBuildError("冻结 PromptProfile 选择缺少目标模型版本")
        profile_version = load_frozen_prompt_profile_selection(
            frozen_prompt_profile,
            model_profile_version=model_profile_version,
            operation=operation,
            mode=profile_mode,
        )
        binding_key = str(frozen_prompt_profile["prompt_profile_binding_key"])
    else:
        resolved_profile = require_prompt_profile_binding(
            operation=operation,
            model_family=model_family,
            mode=profile_mode,
            model_profile_version=model_profile_version,
        )
        profile_version = resolved_profile.profile_version
        binding_key = resolved_profile.binding.key
    profile_selection_sha256 = (
        json_contract(
            frozen_prompt_profile,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        if frozen_prompt_profile is not None else ""
    )
    context = MediaPromptCompilationContext(
        project=project,
        shots=tuple(shots),
        prompt_type=prompt_type,
        mode=mode,
        include_audio=(
            include_audio if prompt_type == PromptArtifact.PromptType.VIDEO else False
        ),
        duration_overrides=duration_overrides,
        overall_soundscape=overall_soundscape,
        non_diegetic_music=non_diegetic_music,
        extra_direction=extra_direction,
        single_shot_reference_bindings=deepcopy(single_shot_reference_bindings),
        single_shot_reference_assets=tuple(single_shot_reference_assets or ()),
        consistency_snapshot=deepcopy(snapshot),
        language_gate_mode=language_gate_mode,
        effective_durations_ms=effective_durations_ms,
        reference_bindings=tuple(deepcopy(reference_bindings)),
        profile_selection_sha256=profile_selection_sha256,
        reviewed_profile_fields=(
            tuple(deepcopy(reviewed_profile_fields))
            if reviewed_profile_fields is not None else None
        ),
        model_identifier=model_identifier,
        conditioning_mode_contract=deepcopy(selected_mode_contract),
        frame_policy=deepcopy(frozen_frame_policy),
    )
    fingerprint = media_prompt_input_fingerprint(context)
    structured_payload = {
        "input_fingerprint_version": GENERIC_MEDIA_PROMPT_INPUT_FINGERPRINT_VERSION,
        "input_fingerprint": fingerprint,
    }
    if prompt_type == PromptArtifact.PromptType.VIDEO:
        selected_mode_contract_sha256 = json_contract(
            selected_mode_contract,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        frozen_conditioning = {
            "schema_version": 1,
            "conditioning_contract": deepcopy(conditioning_contract),
            "conditioning_contract_sha256": conditioning_contract_sha256,
            "conditioning_mode_contract": deepcopy(selected_mode_contract),
            "conditioning_mode_contract_sha256": selected_mode_contract_sha256,
        }
        fingerprint = json_contract({
            "schema_version": 1,
            "prompt_input_fingerprint": fingerprint,
            "frozen_conditioning": frozen_conditioning,
        }, max_bytes=_stage_snapshot_max_bytes()).sha256
        structured_payload.update(frozen_conditioning)
        structured_payload["input_fingerprint"] = fingerprint
    if model_profile_version is not None:
        target_model_profile = {
            "schema_version": 1,
            "model_profile_version_id": str(model_profile_version.id),
            "model_identifier": model_profile_version.model_identifier,
            "model_family": model_family,
            "specification_sha256": model_profile_version.specification_sha256,
        }
        fingerprint = json_contract({
            "schema_version": 1,
            "prompt_input_fingerprint": fingerprint,
            "target_model_profile": target_model_profile,
        }, max_bytes=_stage_snapshot_max_bytes()).sha256
        structured_payload["target_model_profile"] = target_model_profile
        structured_payload["input_fingerprint"] = fingerprint
    profile_compilation = compile_media_prompt_profile(
        profile_version,
        context=context,
    )
    execution_payload = _media_execution_payload(
        prompt_type=prompt_type,
        mode=mode,
        durations=durations,
        reference_bindings=reference_bindings,
        model_identifier=model_identifier,
        adapter_execution_facts=profile_compilation.execution_facts,
    )
    structured_payload.update(execution_payload)
    assembly = profile_compilation.assembly
    prompt_text = str(assembly["channels"].get("positive_prompt") or "").strip()
    negative_prompt = str(assembly["channels"].get("negative_prompt") or "").strip()
    if not prompt_text:
        raise PromptBuildError("PromptProfile 未生成非空 positive_prompt")
    _validate_profiled_reference_prompt(
        prompt_text=prompt_text,
        negative_prompt=negative_prompt,
        reference_bindings=reference_bindings,
    )
    structured_payload["fields"] = deepcopy(profile_compilation.structured_fields)
    if profile_compilation.quality_override is not None:
        quality = deepcopy(
            profile_compilation.quality_override,
        )
    else:
        quality = _profiled_media_quality(
            project=project,
            prompt_type=prompt_type,
            prompt_text=prompt_text,
            negative_prompt=negative_prompt,
            assembly=assembly,
            language_gate_mode=language_gate_mode,
        )
    structured_payload["quality"] = _apply_prompt_profile_channel_quality(
        quality,
        assembly=assembly,
        generation_length_audit=profile_compilation.generation_length_audit,
    )
    if profile_compilation.model_prompt_contract is not None:
        structured_payload["model_prompt_contract"] = deepcopy(
            profile_compilation.model_prompt_contract,
        )
    if profile_compilation.reviewed_field_evidence is not None:
        structured_payload["profile_field_value_contract"] = deepcopy(
            profile_compilation.reviewed_field_evidence,
        )
    for fingerprint_material in profile_compilation.fingerprint_materials:
        fingerprint = json_contract({
            "schema_version": 1,
            "prompt_input_fingerprint": fingerprint,
            **deepcopy(fingerprint_material),
        }, max_bytes=_stage_snapshot_max_bytes()).sha256
    fingerprint = (
        frozen_profile_fingerprint(
            fingerprint,
            profile=profile_version,
            binding_key=binding_key,
        )
        if frozen_prompt_profile is not None
        else profile_fingerprint(fingerprint, resolved_profile)
    )
    structured_payload["input_fingerprint"] = fingerprint
    structured_payload["prompt_profile"] = assembly
    effective_conditioning_mode = str(
        structured_payload.get("effective_conditioning_mode") or mode
    )
    target_shot = shots[0] if len(shots) == 1 else None
    shot_ids = [str(item.id) for item in shots]
    return CompiledPromptInput(
        project=project,
        episode=shots[0].episode,
        shot=target_shot,
        prompt_type=prompt_type,
        requested_conditioning_mode=mode,
        effective_conditioning_mode=effective_conditioning_mode,
        include_audio=(include_audio if prompt_type == PromptArtifact.PromptType.VIDEO else False),
        is_multi_shot=len(shots) > 1,
        total_duration_ms=sum(durations),
        prompt_text=prompt_text,
        negative_prompt=negative_prompt,
        structured_payload=structured_payload,
        consistency_snapshot=snapshot,
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
            "input_fingerprint_version": GENERIC_MEDIA_PROMPT_INPUT_FINGERPRINT_VERSION,
            "input_fingerprint": fingerprint,
            "model_profile_version_id": (
                str(model_profile_version.id) if model_profile_version is not None else ""
            ),
            "model_identifier": model_identifier,
            "model_family": model_family,
            **({
                "prompt_profile_selection": deepcopy(frozen_prompt_profile),
                "prompt_profile_selection_sha256": json_contract(
                    frozen_prompt_profile,
                    max_bytes=_stage_snapshot_max_bytes(),
                ).sha256,
                "prompt_profile_binding_id": str(
                    frozen_prompt_profile.get("prompt_profile_binding_id") or ""
                ),
            } if frozen_prompt_profile is not None else {}),
        },
        content_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        input_fingerprint=fingerprint,
        reference_bindings=deepcopy(reference_bindings),
        prompt_profile_version=profile_version,
        prompt_profile_binding_key=binding_key,
        prompt_profile_contract_sha256=profile_version.contract_sha256,
    )


def compile_bible_image_prompt_input(
    *,
    project: SeriesProject,
    entity_type: str,
    entity,
    model_profile_version: ModelProfileVersion,
    extra_direction: str = "",
    frozen_prompt_profile: dict[str, Any] | None = None,
) -> CompiledPromptInput:
    """Compile one Bible entity reference image through PromptProfile only."""

    if model_profile_version is None:
        raise PromptBuildError("Bible 参考图缺少冻结模型版本")
    model_family = model_family_for_profile(model_profile_version)
    if frozen_prompt_profile is None:
        resolved_profile = require_prompt_profile_binding(
            operation="image.generate",
            model_family=model_family,
            mode="",
            model_profile_version=model_profile_version,
        )
        frozen_prompt_profile = frozen_profile_reference(
            resolved_profile,
            model_profile_version=model_profile_version,
            operation="image.generate",
            mode="",
        )
    profile_version = load_frozen_prompt_profile_selection(
        frozen_prompt_profile,
        model_profile_version=model_profile_version,
        operation="image.generate",
        mode="",
        bible_image=True,
    )
    binding_key = str(frozen_prompt_profile["prompt_profile_binding_key"])
    binding_id = str(frozen_prompt_profile["prompt_profile_binding_id"])
    available_sources = bible_image_profile_sources(
        project=project,
        entity_type=entity_type,
        entity=entity,
        extra_direction=extra_direction,
    )
    selected_sources = set(
        profile_version.field_rules.exclude(
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
        ).values_list("source_selector", flat=True)
    )
    sources = {
        key: value for key, value in available_sources.items()
        if key in selected_sources
    }
    assembly = assemble_prompt_profile(
        profile_version,
        sources=sources,
        projection_context={
            "purpose": "static_reference",
            "target_kind": entity_type,
        },
    )
    prompt_text = str(assembly["channels"].get("positive_prompt") or "").strip()
    negative_prompt = str(assembly["channels"].get("negative_prompt") or "").strip()
    if not prompt_text:
        raise PromptBuildError("Bible 参考图 PromptProfile 未生成非空 positive_prompt")
    execution_payload = _media_execution_payload(
        prompt_type=PromptArtifact.PromptType.IMAGE,
        mode="",
        durations=[0],
        reference_bindings=[],
        model_identifier=model_profile_version.model_identifier,
    )
    source_snapshot = {
        "schema_version": BIBLE_IMAGE_SOURCE_SCHEMA_VERSION,
        "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
        "project_id": str(project.id),
        "entity_type": str(entity_type),
        "entity_id": str(entity.id),
        "entity_name": str(entity.name),
        "extra_direction": str(extra_direction or ""),
        "sources": sources,
        "prompt_profile": {
            "binding_id": binding_id,
            "binding_key": binding_key,
            "version_id": str(profile_version.id),
            "contract_sha256": profile_version.contract_sha256,
            "frozen_selection": deepcopy(frozen_prompt_profile),
            "assembly": assembly,
        },
        "model_profile": {
            "version_id": str(model_profile_version.id),
            "identifier": model_profile_version.model_identifier,
            "family": model_family,
            "specification_sha256": model_profile_version.specification_sha256,
        },
    }
    source_contract = json_contract(
        source_snapshot,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    target = {
        "schema_version": BIBLE_IMAGE_SOURCE_SCHEMA_VERSION,
        "entity_type": str(entity_type),
        "entity_id": str(entity.id),
        "entity_name": str(entity.name),
        "source_snapshot_sha256": source_contract.sha256,
    }
    structured_payload = {
        **execution_payload,
        "fields": deepcopy(assembly["structured_fields"]),
        "prompt_profile": assembly,
        "target_model_profile": {
            "schema_version": 1,
            "model_profile_version_id": str(model_profile_version.id),
            "model_identifier": model_profile_version.model_identifier,
            "model_family": model_family,
            "specification_sha256": model_profile_version.specification_sha256,
        },
        "quality": _profiled_media_quality(
            project=project,
            prompt_type=PromptArtifact.PromptType.IMAGE,
            prompt_text=prompt_text,
            negative_prompt=negative_prompt,
            assembly=assembly,
            language_gate_mode="block",
        ),
        "input_fingerprint_version": BIBLE_IMAGE_SOURCE_SCHEMA_VERSION,
        "input_fingerprint": source_contract.sha256,
    }
    return CompiledPromptInput(
        project=project,
        episode=None,
        shot=None,
        prompt_type=PromptArtifact.PromptType.IMAGE,
        requested_conditioning_mode="IMAGE_TEXT_ONLY",
        effective_conditioning_mode="TEXT_ONLY",
        include_audio=False,
        is_multi_shot=False,
        total_duration_ms=0,
        prompt_text=prompt_text,
        negative_prompt=negative_prompt,
        structured_payload=structured_payload,
        consistency_snapshot={
            "schema_version": BIBLE_IMAGE_SOURCE_SCHEMA_VERSION,
            "source_snapshot_sha256": source_contract.sha256,
        },
        request_options={
            "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
            "target": target,
            "source_snapshot": source_contract.value,
            "source_snapshot_schema_version": source_contract.schema_version,
            "source_snapshot_size_bytes": source_contract.size_bytes,
            "source_snapshot_sha256": source_contract.sha256,
            "extra_direction": str(extra_direction or ""),
            "input_fingerprint_version": BIBLE_IMAGE_SOURCE_SCHEMA_VERSION,
            "input_fingerprint": source_contract.sha256,
            "model_profile_version_id": str(model_profile_version.id),
            "model_identifier": model_profile_version.model_identifier,
            "model_family": model_family,
            "prompt_profile_binding_id": binding_id,
            "prompt_profile_selection": deepcopy(frozen_prompt_profile),
            "prompt_profile_selection_sha256": json_contract(
                frozen_prompt_profile,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256,
        },
        content_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        input_fingerprint=source_contract.sha256,
        reference_bindings=[],
        prompt_profile_version=profile_version,
        prompt_profile_binding_key=binding_key,
        prompt_profile_contract_sha256=profile_version.contract_sha256,
    )


def materialize_compiled_prompt(
    *,
    compiled: CompiledPromptInput,
    created_by,
    request_options_additions: dict | None = None,
) -> PromptArtifact:
    """Persist an already compiled prompt without recompiling mutable inputs."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("compiled prompt materialization requires transaction.atomic()")
    additions = deepcopy(request_options_additions or {})
    protected = {
        "shot_ids", "duration_overrides", "overall_soundscape",
        "non_diegetic_music", "extra_direction", "reference_bindings_override",
        "reference_asset_ids_override", "input_fingerprint_version",
        "input_fingerprint", "model_profile_version_id", "model_identifier",
        "model_family", "prompt_profile_selection",
        "prompt_profile_selection_sha256", "prompt_profile_binding_id",
    }
    if protected & set(additions):
        raise ValueError("request option additions cannot replace compiled input fields")
    options_model_identifier = str(
        compiled.request_options.get("model_identifier") or ""
    ).strip()
    payload_model_identifier = str(
        compiled.structured_payload.get("model") or ""
    ).strip()
    if not options_model_identifier or not payload_model_identifier:
        raise ValueError("compiled prompt missing frozen model identifier")
    if options_model_identifier != payload_model_identifier:
        raise ValueError("compiled prompt frozen model identifiers do not match")
    model_identifier = options_model_identifier
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
        model=model_identifier,
        conditioning_mode=compiled.requested_conditioning_mode,
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
        prompt_profile_version=compiled.prompt_profile_version,
        prompt_profile_binding_key=compiled.prompt_profile_binding_key,
        prompt_profile_contract_sha256=compiled.prompt_profile_contract_sha256,
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
    conditioning_mode: str,
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
    prompt_profile_version=None,
    prompt_profile_binding_key: str = "",
    prompt_profile_contract_sha256: str = "",
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

    with registry_reference_write_guard():
        validate_frozen_prompt_profile_binding_reference(
            binding_key=prompt_profile_binding_key,
            prompt_profile_version=prompt_profile_version,
            contract_sha256=prompt_profile_contract_sha256,
        )
        return PromptArtifact.objects.create(
            project=project,
            episode=episode,
            shot=shot,
            created_by=created_by,
            prompt_profile_version=prompt_profile_version,
            prompt_profile_binding_key=prompt_profile_binding_key,
            prompt_profile_contract_sha256=prompt_profile_contract_sha256,
            prompt_type=prompt_type,
            model=model,
            conditioning_mode=conditioning_mode,
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


def materialize_compiled_workflow_version(
    *,
    user,
    workflow_version: WorkflowTemplateVersion,
    prompt: PromptArtifact,
    compiled: CompiledWorkflow,
    route_snapshot_sha256: str,
    workflow_snapshot: dict,
) -> GeneratedWorkflow:
    """Persist one semantic-compiler result from a frozen workflow version."""
    if is_retired_bible_image_candidate_prompt(prompt):
        raise RetiredBibleImageCandidateOperation()
    template_snapshot = workflow_snapshot.get("template") if isinstance(workflow_snapshot, dict) else None
    if not isinstance(template_snapshot, dict) or template_snapshot.get("output_kind") != prompt.prompt_type:
        raise WorkflowBuildError("workflow version 输出类型与提示词类型不一致")
    if (
        str(workflow_snapshot.get("workflow_version_id") or "") != str(workflow_version.id)
        or workflow_snapshot.get("execution_contract_sha256") != compiled.workflow_contract_sha256
    ):
        raise WorkflowBuildError("workflow version 与冻结 route snapshot 不一致")
    if (
        not getattr(user, "is_staff", False)
        and prompt.project.owner_id != getattr(user, "id", None)
    ):
        raise WorkflowBuildError("提示词不属于当前用户项目")
    workflow_input_contract = workflow_snapshot.get("input_contract")
    output_kind = str(template_snapshot.get("output_kind") or "")
    try:
        model_specification = (
            verified_model_profile_specification(
                workflow_version.model_profile_version,
            )
            if workflow_version.model_profile_version_id else None
        )
        conditioning = resolve_conditioning_contract(
            workflow_input_contract=workflow_input_contract,
            model_specification=model_specification,
            output_kind=output_kind,
            required=output_kind == PromptArtifact.PromptType.VIDEO,
        )
        effective_mode = str(
            prompt.structured_payload.get("effective_conditioning_mode") or ""
        )
        if output_kind == PromptArtifact.PromptType.VIDEO and not effective_mode:
            raise WorkflowBuildError(
                "视频 PromptArtifact 缺少显式 effective_conditioning_mode",
            )
        selected_conditioning_mode = conditioning_mode_contract(
            conditioning,
            effective_mode,
            required=output_kind == PromptArtifact.PromptType.VIDEO,
        )
    except Exception as exc:
        raise WorkflowBuildError("workflow version conditioning contract 无效") from exc
    if output_kind == PromptArtifact.PromptType.VIDEO:
        prompt_payload = (
            prompt.structured_payload
            if isinstance(prompt.structured_payload, dict) else {}
        )
        prompt_conditioning = prompt_payload.get("conditioning_contract")
        prompt_conditioning_sha256 = str(
            prompt_payload.get("conditioning_contract_sha256") or ""
        )
        prompt_mode_contract = prompt_payload.get("conditioning_mode_contract")
        prompt_mode_contract_sha256 = str(
            prompt_payload.get("conditioning_mode_contract_sha256") or ""
        )
        if (
            not isinstance(prompt_conditioning, dict)
            or json_contract(
                prompt_conditioning,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256
            != prompt_conditioning_sha256
            or prompt_conditioning != conditioning
            or prompt_conditioning_sha256 != json_contract(
                conditioning,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256
            or not isinstance(prompt_mode_contract, dict)
            or json_contract(
                prompt_mode_contract,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256
            != prompt_mode_contract_sha256
            or prompt_mode_contract != selected_conditioning_mode
        ):
            raise WorkflowBuildError(
                "PromptArtifact 与 workflow version 的冻结 conditioning contract 不一致"
            )
    execution_monitoring = resolve_execution_monitoring_contract(
        model_specification=model_specification,
        workflow_input_contract=workflow_snapshot.get("input_contract"),
        workflow_graph=workflow_snapshot.get("graph"),
    )
    workflow_mode = str(
        (workflow_snapshot.get("input_contract") or {}).get("workflow_mode") or ""
    ).strip()
    if not workflow_mode:
        raise WorkflowBuildError("workflow input_contract 缺少显式 workflow_mode")
    input_contract = workflow_snapshot.get("input_contract")
    if not isinstance(input_contract, dict):
        raise WorkflowBuildError("workflow snapshot 缺少 input_contract")
    required_dimension_multiple = _require_positive_contract_integer(
        input_contract.get("required_dimension_multiple"),
        field="required_dimension_multiple",
        context="workflow input_contract",
    )
    execution_metadata = {
        "schema_version": 1,
        "workflow_version_id": str(workflow_version.id),
        "workflow_version": int(workflow_snapshot.get("workflow_version") or 0),
        "template": deepcopy(template_snapshot),
        "model_identifier": str(workflow_snapshot.get("model_identifier") or ""),
        "workflow_mode": workflow_mode,
        "source_workflow": str(input_contract.get("source_workflow") or ""),
        "required_dimension_multiple": required_dimension_multiple,
        "conditioning_contract": deepcopy(conditioning),
        "conditioning_contract_sha256": json_contract(
            conditioning,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256,
        "conditioning_mode_contract": deepcopy(selected_conditioning_mode),
        "conditioning_mode_contract_sha256": (
            json_contract(
                selected_conditioning_mode,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256
            if selected_conditioning_mode is not None else ""
        ),
        "execution_monitoring": execution_monitoring,
        "execution_monitoring_sha256": execution_monitoring_contract_sha256(
            execution_monitoring,
        ),
    }
    return GeneratedWorkflow.objects.create(
        project=prompt.project,
        episode=prompt.episode,
        prompt_artifact=prompt,
        template=workflow_version.template,
        workflow_template_version=workflow_version,
        created_by=user,
        workflow_json=compiled.graph,
        injected_values={
            "schema_version": 2,
            "resolved_semantics": deepcopy(compiled.resolved_semantics),
            "asset_bindings": deepcopy(compiled.asset_bindings),
            "model_requirements": deepcopy(compiled.model_requirements),
            "output_contract": deepcopy(compiled.output_contract),
            "execution_metadata": execution_metadata,
            "execution_metadata_sha256": json_contract(
                execution_metadata,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256,
        },
        content_hash=compiled.graph_sha256,
        compiler_id=compiled.compiler_id,
        compiler_version=compiled.compiler_version,
        route_snapshot_sha256=route_snapshot_sha256,
        workflow_contract_sha256=compiled.workflow_contract_sha256,
        resolved_semantics_sha256=compiled.resolved_semantics_sha256,
        asset_bindings_sha256=compiled.asset_bindings_sha256,
        model_requirements_sha256=compiled.model_requirements_sha256,
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
    if is_retired_bible_image_candidate_prompt(prompt):
        raise RetiredBibleImageCandidateOperation()
    versioned = workflow.workflow_template_version
    semantic_compiled = bool(
        versioned is not None
        and workflow.compiler_id
        and injected.get("schema_version") == 2
    )
    if not semantic_compiled:
        raise WorkflowBuildError(
            "未版本化 GeneratedWorkflow 已退役；只能从 WorkflowTemplateVersion 创建任务"
        )
    semantics = injected.get("resolved_semantics")
    if not isinstance(semantics, dict):
        raise WorkflowBuildError("semantic compiled workflow 缺少 resolved_semantics")
    execution_metadata = injected.get("execution_metadata")
    if (
        not isinstance(execution_metadata, dict)
        or json_contract(
            execution_metadata,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256 != injected.get("execution_metadata_sha256")
    ):
        raise WorkflowBuildError("GeneratedWorkflow 缺少完整冻结执行元数据")
    if (
        str(execution_metadata.get("workflow_version_id") or "") != str(versioned.id)
    ):
        raise WorkflowBuildError("semantic compiled workflow 执行元数据无效")
    try:
        execution_monitoring = verify_frozen_execution_monitoring_contract(
            execution_metadata.get("execution_monitoring"),
            sha256=execution_metadata.get("execution_monitoring_sha256"),
        )
    except Exception as exc:
        raise WorkflowBuildError("GeneratedWorkflow 执行监控合同无效") from exc
    execution_monitoring_sha256 = str(
        execution_metadata.get("execution_monitoring_sha256") or ""
    )
    if workflow.project_id != prompt.project_id:
        raise WorkflowBuildError("工作流与提示词项目不一致")
    if workflow.episode_id != prompt.episode_id:
        raise WorkflowBuildError("工作流与提示词剧集不一致")
    effective_output_kind = str(
        (execution_metadata.get("template") or {}).get("output_kind") or ""
    )
    if effective_output_kind != prompt.prompt_type:
        raise WorkflowBuildError("工作流模板与提示词输出类型不一致")
    conditioning = execution_metadata.get("conditioning_contract")
    selected_conditioning_mode = execution_metadata.get("conditioning_mode_contract")
    if (
        not isinstance(conditioning, dict)
        or json_contract(
            conditioning,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        != execution_metadata.get("conditioning_contract_sha256")
    ):
        raise WorkflowBuildError("GeneratedWorkflow conditioning contract 无效")
    selected_sha256 = str(
        execution_metadata.get("conditioning_mode_contract_sha256") or ""
    )
    if effective_output_kind == PromptArtifact.PromptType.VIDEO:
        if (
            not isinstance(selected_conditioning_mode, dict)
            or json_contract(
                selected_conditioning_mode,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256 != selected_sha256
        ):
            raise WorkflowBuildError("GeneratedWorkflow conditioning mode contract 无效")
        expected_mode = str(
            prompt.structured_payload.get("effective_conditioning_mode") or ""
        )
        if not expected_mode:
            raise WorkflowBuildError(
                "视频 PromptArtifact 缺少显式 effective_conditioning_mode",
            )
        if selected_conditioning_mode.get("key") != expected_mode:
            raise WorkflowBuildError("GeneratedWorkflow conditioning mode 与提示词不一致")
        prompt_conditioning = prompt.structured_payload.get("conditioning_contract")
        prompt_conditioning_sha256 = str(
            prompt.structured_payload.get("conditioning_contract_sha256") or ""
        )
        prompt_mode_contract = prompt.structured_payload.get(
            "conditioning_mode_contract"
        )
        prompt_mode_contract_sha256 = str(
            prompt.structured_payload.get("conditioning_mode_contract_sha256") or ""
        )
        if (
            not isinstance(prompt_conditioning, dict)
            or json_contract(
                prompt_conditioning,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256
            != prompt_conditioning_sha256
            or prompt_conditioning != conditioning
            or not isinstance(prompt_mode_contract, dict)
            or json_contract(
                prompt_mode_contract,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256
            != prompt_mode_contract_sha256
            or prompt_mode_contract != selected_conditioning_mode
        ):
            raise WorkflowBuildError(
                "GeneratedWorkflow 与 PromptArtifact 的冻结 conditioning contract 不一致"
            )
    elif selected_conditioning_mode is not None or selected_sha256:
        raise WorkflowBuildError("非视频工作流不能冻结 conditioning mode contract")
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
    backend = "comfyui_local"
    profile = str(execution_metadata.get("model_identifier") or "")
    if not profile:
        raise WorkflowBuildError("workflow version 缺少显式 model_identifier")
    reference_records = injected.get("asset_bindings", [])
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
        "model", "conditioning_mode",
        "effective_conditioning_mode", "prompt_template_key", "workflow_mode",
        "source_workflow", "workflow_parameters", "resolution_profile",
        "resolution_multiple", "width", "height",
        "continuity_assets", "continuity_assets_sha256",
        "output_contract", "workflow_template_version_id", "compiler_id",
        "compiler_version", "route_snapshot_sha256", "workflow_contract_sha256",
        "resolved_semantics_sha256", "asset_bindings", "asset_bindings_sha256",
        "model_requirements", "model_requirements_sha256", "execution_metadata",
        "execution_metadata_sha256",
        "execution_monitoring", "execution_monitoring_sha256",
        "conditioning_contract", "conditioning_contract_sha256",
        "conditioning_mode_contract", "conditioning_mode_contract_sha256",
        "reference_media_profile", "reference_media_profile_sha256",
        "reference_media_configuration_sha256",
        "generation_purpose", "idempotency_key", "request_fingerprint",
        "bible_target", "source_snapshot_sha256", "quality",
    }
    supplied_overrides = set(capability_snapshot_overrides or {})
    if supplied_overrides & protected_snapshot_keys:
        raise WorkflowBuildError(
            "capability snapshot overrides cannot replace authoritative workflow fields"
        )
    resolution_multiple = _require_positive_contract_integer(
        execution_metadata.get("required_dimension_multiple"),
        field="required_dimension_multiple",
        context="GeneratedWorkflow execution metadata",
    )
    capability_snapshot = {
        "generated_workflow_id": str(workflow.id),
        "workflow_hash": workflow.content_hash,
        "prompt_artifact_id": str(prompt.id),
        "prompt_content_hash": prompt.content_hash,
        "template_id": str(workflow.template_id),
        "template_hash": workflow.workflow_contract_sha256,
        "template_slug": str((execution_metadata.get("template") or {}).get("slug") or ""),
        "model": execution_metadata.get("model_identifier"),
        "conditioning_mode": prompt.conditioning_mode,
        "effective_conditioning_mode": prompt.structured_payload.get(
            "effective_conditioning_mode",
        ),
        "prompt_template_key": prompt.structured_payload.get(
            "prompt_template_key", "",
        ),
        "workflow_mode": execution_metadata.get("workflow_mode"),
        "source_workflow": execution_metadata.get("source_workflow"),
        "workflow_parameters": semantics,
        "resolution_profile": "semantic_contract",
        "resolution_multiple": resolution_multiple,
        "width": int(semantics.get("image.width", 0)),
        "height": int(semantics.get("image.height", 0)),
        "episode_id": str(episode.id) if episode else None,
        "episode_number": episode.number if episode else None,
        "episode_title": episode.title if episode else "",
        "shot_ids": shot_ids,
        "shot_count": len(shot_ids),
        "whole_episode": bool(
            (prompt.request_options or {}).get("whole_episode", False)
        ),
        "shot_id": str(shot.id) if shot else None,
        "shot_number": shot.number if shot else None,
        "shot_title": shot.title if shot else "",
        "execution_monitoring": deepcopy(execution_monitoring),
        "execution_monitoring_sha256": execution_monitoring_sha256,
    }
    if not is_image:
        reference_media_profile = resolve_reference_media_profile(
            project=workflow.project,
        )
        capability_snapshot.update({
            "reference_media_profile": deepcopy(reference_media_profile.value),
            "reference_media_profile_sha256": reference_media_profile.sha256,
            "reference_media_configuration_sha256": (
                reference_media_profile.configuration_sha256
            ),
        })
    continuity_assets = prompt.structured_payload.get("continuity_assets", [])
    if not isinstance(continuity_assets, list):
        raise WorkflowBuildError("PromptArtifact continuity_assets 合同无效")
    capability_snapshot.update({
        "continuity_assets": deepcopy(continuity_assets),
        "continuity_assets_sha256": json_contract(
            continuity_assets,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256,
    })
    capability_snapshot.update({
            "workflow_template_version_id": str(versioned.id),
            "compiler_id": workflow.compiler_id,
            "compiler_version": workflow.compiler_version,
            "route_snapshot_sha256": workflow.route_snapshot_sha256,
            "workflow_contract_sha256": workflow.workflow_contract_sha256,
            "resolved_semantics_sha256": workflow.resolved_semantics_sha256,
            "asset_bindings": deepcopy(reference_records),
            "asset_bindings_sha256": workflow.asset_bindings_sha256,
            "model_requirements": deepcopy(injected.get("model_requirements") or []),
            "model_requirements_sha256": workflow.model_requirements_sha256,
            "output_contract": deepcopy(injected.get("output_contract") or {}),
            "execution_metadata": deepcopy(execution_metadata),
            "execution_metadata_sha256": injected.get("execution_metadata_sha256"),
            "conditioning_contract": deepcopy(conditioning),
            "conditioning_contract_sha256": execution_metadata.get(
                "conditioning_contract_sha256"
            ),
            "conditioning_mode_contract": deepcopy(selected_conditioning_mode),
            "conditioning_mode_contract_sha256": execution_metadata.get(
                "conditioning_mode_contract_sha256"
            ),
        })
    expected_output_contract = injected.get("output_contract")
    if not isinstance(expected_output_contract, dict) or not expected_output_contract:
        raise WorkflowBuildError("semantic compiled workflow 缺少 output_contract")
    if not is_image:
        include_audio = _require_audio_enabled_semantic(semantics)
        capability_snapshot.update({
            "fps": int(semantics.get("video.fps", 0)),
            "include_audio": include_audio,
            "requested_duration_ms": int(semantics.get("video.duration_ms", prompt.total_duration_ms)),
            "output_contract": deepcopy(expected_output_contract),
        })
    capability_snapshot.update(capability_snapshot_overrides or {})
    if is_image:
        for key in ("fps", "include_audio", "requested_duration_ms"):
            capability_snapshot.pop(key, None)
    effective_frames = (
        0 if is_image else int(
            semantics.get("video.frames", 0)
        )
    )
    effective_duration_ms = (
        0 if is_image else int(
            semantics.get("video.duration_ms", 0)
        )
    )
    # ``effective_*`` is the immutable execution plan after workflow alignment,
    # not an observation of the returned container.  Generic workflows often
    # bind frames and FPS without binding duration; persist the equivalent plan
    # duration instead of the misleading zero while leaving the frozen resolved
    # semantics untouched.
    if not effective_duration_ms and effective_frames:
        try:
            effective_fps = float(semantics.get("video.fps", 0))
        except (TypeError, ValueError, OverflowError):
            effective_fps = 0
        if effective_fps > 0:
            effective_duration_ms = round(effective_frames * 1000 / effective_fps)

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
        workflow_version=int(execution_metadata.get("workflow_version") or 0),
        seed=int(semantics.get("sampling.seed", workflow.project.continuity_seed)),
        effective_frames=effective_frames,
        effective_duration_ms=effective_duration_ms,
        capability_snapshot=capability_snapshot,
    )


def clone_generation_task_for_retry(
    task: GenerationTask,
    *,
    user,
    generation_attempt_id,
    extended_generation_confirmed: bool = False,
) -> GenerationTask:
    """Create an auditable retry without mutating the terminal source task."""

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("retry materialization requires transaction.atomic()")
    if not can_execute_template(task.generated_workflow.template, user):
        raise WorkflowBuildError("The workflow template is not executable by this user.")
    if (
        not task.generated_workflow.workflow_template_version_id
        or not task.generated_workflow.compiler_id
        or not task.backend
        or not task.model_profile
    ):
        raise WorkflowBuildError("未版本化任务已退役，不能重试")
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
    source_snapshot = task.capability_snapshot if isinstance(task.capability_snapshot, dict) else {}
    execution_metadata = injected.get("execution_metadata")
    try:
        if (
            not isinstance(execution_metadata, dict)
            or json_contract(
                execution_metadata,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256
            != injected.get("execution_metadata_sha256")
        ):
            raise ValueError("workflow execution metadata invalid")
        workflow_monitoring = verify_frozen_execution_monitoring_contract(
            execution_metadata.get("execution_monitoring"),
            sha256=execution_metadata.get("execution_monitoring_sha256"),
        )
        task_monitoring = verify_frozen_execution_monitoring_contract(
            source_snapshot.get("execution_monitoring"),
            sha256=source_snapshot.get("execution_monitoring_sha256"),
        )
        if (
            workflow_monitoring != task_monitoring
            or execution_metadata.get("execution_monitoring_sha256")
            != source_snapshot.get("execution_monitoring_sha256")
        ):
            raise ValueError("execution monitoring drift")
    except Exception as exc:
        raise WorkflowBuildError(
            "retry source execution monitoring 合同缺失或漂移"
        ) from exc
    resolution_multiple = source_snapshot.get("resolution_multiple")
    if (
        isinstance(resolution_multiple, bool)
        or not isinstance(resolution_multiple, int)
        or resolution_multiple <= 0
    ):
        raise WorkflowBuildError("retry source 缺少冻结 resolution_multiple")
    capability_snapshot = {
        **source_snapshot,
        "resolution_multiple": resolution_multiple,
        "retry_of": str(task.id),
        "generation_attempt_id": str(generation_attempt_id),
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
