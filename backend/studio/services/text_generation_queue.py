from __future__ import annotations

from copy import copy, deepcopy
import hashlib
import json
import math
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    GeneratedArtifact,
    GenerationAttempt,
    GenerationAttemptResolution,
    ModelDeployment,
    ProductionRun,
    ScriptRevision,
    ScriptSource,
    StageRun,
)
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.model_profile_contracts import (
    CHAT_REQUEST_OPTIONS_KEY,
    verified_frozen_model_profile_specification,
    verified_model_profile_specification,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    credential_binding_sha256,
    deployment_supports_script_generation,
    driver_for,
)
from studio.services.prompt_normalization_contract import (
    PROMPT_NORMALIZE_OPERATION,
    normalization_batch_max_items,
    normalization_request_max_bytes,
    normalization_text_max_characters,
    prompt_usage_max_count,
    prompt_normalization_chat_messages_input_bytes,
    prompt_normalization_chat_payload,
    prompt_normalization_policy_from_specification,
    safe_prompt_field_validation_context,
    safe_prompt_field_validation_reason_code,
)
from studio.services.prompt_translation import (
    PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC,
    PROMPT_NORMALIZATION_EXECUTION_PROVIDER,
    PROMPT_NORMALIZATION_LOCAL_SUBMISSION_SEMANTICS,
    deterministic_prompt_normalization_materialization,
    restore_prompt_normalization_candidate,
)
from studio.services.prompt_profiles import (
    verify_frozen_profile_assembly,
    verify_frozen_profile_reference,
)
from studio.services.script_sources import create_pasted_script_source
from studio.services.security_boundaries import boundary_int
from studio.services.production_visual_rules import (
    VISUAL_RULES_OPERATION,
    finalize_visual_rules,
    verify_visual_rules_smoke,
    visual_rules_provider_payload,
)
from studio.services.script_structure import (
    SCRIPT_STRUCTURE_OPERATION,
    finalize_script_structure,
    materialize_script_structure_preview,
    structure_provider_payload,
    verify_script_structure_smoke,
)
from studio.services.series_concept_drafts import (
    SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
    finalize_series_concept_draft,
)
from studio.services.text_field_contracts import (
    SERIES_CONCEPT_FIELD_KEYS,
    field_contract_prompt_lines,
    text_field_rule,
    validate_text_field_contract,
)


TEXT_OPERATION = "script.generate"
TEXT_UNIT_KEY = "series-script"
PROMPT_NORMALIZE_UNIT_KEY = "prompt-normalization"
SAFE_PROMPT_RESPONSE_CLASSIFIERS = frozenset({
    "prompt_field_generation_contract_invalid",
    "prompt_normalize_contract_invalid",
    "prompt_normalization_batch_input_exceeded",
    "prompt_normalization_batch_items_exceeded",
    "prompt_normalization_policy_invalid",
    "prompt_normalization_policy_missing",
    "response_content_filtered",
    "response_missing_json_text",
    "response_missing_text",
    "response_not_strict_json",
    "response_output_budget_exhausted",
    "response_reasoning_without_text",
    "response_root_invalid",
    "response_tool_call_without_text",
})
SAFE_PROMPT_FINISH_REASONS = frozenset({
    "content_filter", "function_call", "length", "stop", "tool_calls",
})
SAFE_PROMPT_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
TEXT_OPERATIONS = frozenset({
    TEXT_OPERATION,
    SCRIPT_STRUCTURE_OPERATION,
    VISUAL_RULES_OPERATION,
    PROMPT_NORMALIZE_OPERATION,
})
TERMINAL_RUN_STATUSES = frozenset({
    ProductionRun.Status.SUCCEEDED,
    ProductionRun.Status.FAILED,
    ProductionRun.Status.CANCELLED,
})


def _text_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.text_max_bytes")


def _prompt_advisory_max_items() -> int:
    return boundary_int("security.prompt_normalization.advisory_max_items")


def _verify_attempt_prompt_profile(
    attempt: GenerationAttempt,
    assembly: Any,
) -> None:
    profile = attempt.prompt_profile_version
    verify_frozen_profile_reference(
        profile,
        binding_key=attempt.prompt_profile_binding_key,
        contract_sha256=attempt.prompt_profile_contract_sha256,
    )
    if profile is None:
        if assembly is not None:
            raise ValidationError("attempt 未冻结 PromptProfile，但请求包含 assembly")
        return
    verify_frozen_profile_assembly(profile, assembly)


def _safe_prompt_normalization_advisories(
    value: Any,
    *,
    batch_index: int,
) -> list[dict[str, Any]]:
    """Re-allowlist server-authored advisory evidence before persistence."""

    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > _prompt_advisory_max_items():
        raise ValidationError("prompt.normalize warnings 结构无效")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("prompt.normalize warning 必须是对象")
        code = str(item.get("code") or "")
        if code == "recommended_input_bytes_exceeded":
            expected = {
                "schema_version", "code", "severity", "measured",
                "recommended_maximum", "hard_maximum",
            }
            numbers = (
                item.get("measured"),
                item.get("recommended_maximum"),
                item.get("hard_maximum"),
            )
            valid = (
                set(item) == expected
                and all(
                    type(number) is int
                    and 0 <= number <= normalization_request_max_bytes()
                    for number in numbers
                )
                and numbers[0] > numbers[1]
                and numbers[1] <= numbers[2]
            )
        elif code == "recommended_output_tokens_exceeded":
            expected = {
                "schema_version", "code", "severity", "usage_field",
                "measured", "recommended_maximum", "hard_maximum",
            }
            usage_field = item.get("usage_field")
            numbers = (
                item.get("measured"),
                item.get("recommended_maximum"),
                item.get("hard_maximum"),
            )
            valid = (
                set(item) == expected
                and usage_field in {"completion_tokens", "output_tokens"}
                and all(
                    type(number) is int
                    and 0 <= number <= prompt_usage_max_count()
                    for number in numbers
                )
                and numbers[0] > numbers[1]
                and numbers[1] <= numbers[2]
            )
        elif code == "recommended_length_out_of_range":
            expected = {
                "schema_version", "code", "severity", "request_id",
                "field_key", "measured", "minimum", "maximum",
                "length_unit", "request_index",
            }
            measured = item.get("measured")
            minimum = item.get("minimum")
            maximum = item.get("maximum")
            valid = (
                set(item) == expected
                and isinstance(item.get("request_id"), str)
                and 1 <= len(item["request_id"]) <= 191
                and isinstance(item.get("field_key"), str)
                and 1 <= len(item["field_key"]) <= 191
                and item.get("length_unit") in {"characters", "english_words", "items"}
                and type(measured) is int
                and 0 <= measured <= prompt_usage_max_count()
                and (
                    minimum is None
                    or type(minimum) is int
                    and 1 <= minimum <= normalization_text_max_characters()
                )
                and (
                    maximum is None
                    or type(maximum) is int
                    and 1 <= maximum <= normalization_text_max_characters()
                )
                and type(item.get("request_index")) is int
                and 0 <= item["request_index"] < normalization_batch_max_items()
                and (
                    (minimum is not None and measured < minimum)
                    or (maximum is not None and measured > maximum)
                )
            )
        else:
            valid = False
        if (
            not valid
            or item.get("schema_version") != 1
            or item.get("severity") != "warning"
        ):
            raise ValidationError("prompt.normalize warning 合同无效")
        result.append({**deepcopy(item), "batch_index": batch_index})
    return result


def _prompt_normalization_progress_snapshot(
    *,
    stage: StageRun,
    batch_plan: dict[str, Any],
    result: dict[str, Any],
    result_sha256: str,
    provider_request_id_sha256: str,
) -> dict[str, Any]:
    """Append one immutable batch result and finalize in original row order."""

    existing = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
    if existing:
        verify_json_contract(
            existing,
            schema_version=stage.output_snapshot_schema_version,
            size_bytes=stage.output_snapshot_size_bytes,
            sha256=stage.output_snapshot_sha256,
            max_bytes=_text_snapshot_max_bytes(),
        )
    if existing == {"schema_version": 1}:
        # Stage rows start with this signed empty snapshot.  It is not a
        # completed batch and must not become a competing progress kind.
        existing = {}
    if existing and existing.get("kind") != "prompt_normalization_batch_progress":
        raise ValidationError("prompt.normalize 批次进度快照类型无效")
    if existing and existing.get("batch_plan_sha256") != batch_plan.get(
        "batch_plan_sha256",
    ):
        raise ValidationError("prompt.normalize 批次进度与冻结计划不一致")
    completed = deepcopy(existing.get("completed_batches") or []) if existing else []
    if not isinstance(completed, list):
        raise ValidationError("prompt.normalize completed_batches 无效")
    batch_index = int(result["batch_index"])
    if any(
        not isinstance(item, dict)
        or item.get("batch_index") == batch_index
        or item.get("attempt_id") == result.get("attempt_id")
        for item in completed
    ):
        raise ValidationError("prompt.normalize 批次结果重复或结构无效")
    completed.append({
        "batch_index": batch_index,
        "attempt_id": str(result["attempt_id"]),
        "result_sha256": result_sha256,
        "provider_request_id_sha256": provider_request_id_sha256,
        "result": deepcopy(result),
    })
    completed.sort(key=lambda item: int(item["batch_index"]))
    advisories = [
        advisory
        for completed_batch in completed
        for advisory in _safe_prompt_normalization_advisories(
            (completed_batch.get("result") or {}).get("warnings"),
            batch_index=int(completed_batch["batch_index"]),
        )
    ]
    batches = batch_plan.get("batches")
    if not isinstance(batches, list):
        raise ValidationError("prompt.normalize 冻结批次计划无效")
    expected_prefix = list(range(len(completed)))
    if [int(item["batch_index"]) for item in completed] != expected_prefix:
        raise ValidationError("prompt.normalize 仅允许按冻结批次顺序提交")
    if len(completed) < len(batches):
        return {
            "schema_version": 1,
            "kind": "prompt_normalization_batch_progress",
            "batch_plan_sha256": str(batch_plan["batch_plan_sha256"]),
            "batch_count": len(batches),
            "completed_batches": completed,
            "advisories": advisories,
        }
    if len(completed) != len(batches):
        raise ValidationError("prompt.normalize 已完成批次数超过冻结计划")

    by_request_id: dict[str, dict[str, Any]] = {}
    providers: list[str] = []
    models: list[str] = []
    semantics: list[str] = []
    execution_kinds: list[str] = []
    batch_evidence: list[dict[str, Any]] = []
    for completed_batch in completed:
        batch_result = completed_batch.get("result")
        if not isinstance(batch_result, dict):
            raise ValidationError("prompt.normalize 批次结果缺失")
        result_batch_index = int(completed_batch["batch_index"])
        execution_kind = str(batch_result.get("execution_kind") or "")
        if (
            result_batch_index >= len(batches)
            or execution_kind
            != str((batches[result_batch_index] or {}).get("execution_kind") or "")
        ):
            raise ValidationError("prompt.normalize 批次执行来源与冻结计划不一致")
        if execution_kind not in execution_kinds:
            execution_kinds.append(execution_kind)
        if json_contract(
            batch_result,
            max_bytes=_text_snapshot_max_bytes(),
        ).sha256 != completed_batch.get("result_sha256"):
            raise ValidationError("prompt.normalize 批次结果 checksum 无效")
        candidates = batch_result.get("candidates")
        if not isinstance(candidates, list):
            raise ValidationError("prompt.normalize 批次 candidates 无效")
        for candidate in candidates:
            request_id = str(candidate.get("request_id") or "") if isinstance(candidate, dict) else ""
            if not request_id or request_id in by_request_id:
                raise ValidationError("prompt.normalize 合并 request_id 缺失或重复")
            by_request_id[request_id] = deepcopy(candidate)
        for value, destination in (
            (str(batch_result.get("provider") or ""), providers),
            (str(batch_result.get("model") or ""), models),
            (str(batch_result.get("submission_semantics") or ""), semantics),
        ):
            if value and value not in destination:
                destination.append(value)
        batch_evidence.append({
            "batch_index": int(completed_batch["batch_index"]),
            "attempt_id": str(completed_batch["attempt_id"]),
            "result_sha256": str(completed_batch["result_sha256"]),
            "execution_kind": execution_kind,
        })
    original_request_ids = batch_plan.get("original_request_ids")
    if (
        not isinstance(original_request_ids, list)
        or set(original_request_ids) != set(by_request_id)
        or len(original_request_ids) != len(by_request_id)
    ):
        raise ValidationError("prompt.normalize 合并结果未覆盖完整 request_id")
    merged_candidates = [by_request_id[request_id] for request_id in original_request_ids]
    aggregate_evidence = {
        "schema_version": 1,
        "batch_plan_sha256": str(batch_plan["batch_plan_sha256"]),
        "batch_results": batch_evidence,
        "candidates": merged_candidates,
    }
    frozen_input = batch_plan.get("full_frozen_input")
    return {
        "schema_version": 1,
        "kind": "prompt_normalization_candidates",
        "review_origin": (
            "server_deterministic_candidate"
            if execution_kinds == [PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC]
            else "hybrid_candidate"
            if PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC in execution_kinds
            else "model_candidate"
        ),
        "attempt_id": str(completed[-1]["attempt_id"]),
        "attempt_ids": [str(item["attempt_id"]) for item in completed],
        "result_sha256": json_contract(
            aggregate_evidence,
            max_bytes=_text_snapshot_max_bytes(),
        ).sha256,
        "batch_plan_sha256": str(batch_plan["batch_plan_sha256"]),
        "batch_count": len(batches),
        "batch_results": completed,
        "advisories": advisories,
        "provider": " / ".join(providers)[:80],
        "model": " / ".join(models)[:200],
        "submission_semantics": (
            PROMPT_NORMALIZATION_LOCAL_SUBMISSION_SEMANTICS
            if execution_kinds == [PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC]
            else "hybrid_batched_durable_attempt"
            if PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC in execution_kinds
            else semantics[0]
            if len(semantics) == 1 and len(completed) == 1
            else "batched_durable_attempt"
        ),
        "frozen_input": deepcopy(frozen_input),
        "frozen_input_schema_version": batch_plan.get(
            "full_frozen_input_schema_version",
        ),
        "frozen_input_size_bytes": batch_plan.get("full_frozen_input_size_bytes"),
        "frozen_input_sha256": batch_plan.get("full_frozen_input_sha256"),
        "candidates": merged_candidates,
    }


def append_attempt_resolution(
    attempt: GenerationAttempt,
    *,
    outcome: str,
    evidence: dict[str, Any],
    note: str = "",
    decided_by=None,
) -> GenerationAttemptResolution:
    contract = json_contract(evidence, max_bytes=_text_snapshot_max_bytes())
    revision = (
        attempt.resolutions.aggregate(value=Max("revision"))["value"] or 0
    ) + 1
    return GenerationAttemptResolution.objects.create(
        attempt=attempt,
        revision=revision,
        outcome=outcome,
        provider_request_id=attempt.provider_request_id,
        evidence=contract.value,
        evidence_schema_version=contract.schema_version,
        evidence_size_bytes=contract.size_bytes,
        evidence_sha256=contract.sha256,
        note=str(note or "")[:4000],
        decided_by=decided_by,
    )


def _messages(plan: dict[str, Any]) -> list[dict[str, str]]:
    request = plan["request"]
    options = request["options"]
    field_contract = validate_text_field_contract(
        plan.get("field_contract"),
        fields=SERIES_CONCEPT_FIELD_KEYS,
        label="series_concept frozen plan",
    )
    def placeholder(field: str, subject: str) -> str:
        rule = text_field_rule(field_contract, field)
        required = "required" if rule["required"] else "optional"
        line_shape = "single-line" if rule["single_line"] else "text"
        return f"<{required} {line_shape} {subject}, <= {int(rule['max_chars'])} chars>"
    shot_contract = {
        "setting": placeholder("shot.setting", "setting"),
        "action": placeholder("shot.action", "visual action"),
        "duration_ms": int(options["minimum_duration_ms"]),
        "dialogue": {
            "speaker": "",
            "delivery": "",
            "text": "",
            "start_ms": 0,
            "end_ms": 0,
        },
    }
    contract = {
        "schema_version": SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
        "title": placeholder("title", "project title"),
        "logline": placeholder("logline", "logline"),
        "bible": {
            "characters": [{
                "name": placeholder("bible.entity.name", "canonical character name"),
                "description": placeholder("bible.entity.description", "character description"),
                "visual_identity_en": placeholder("bible.entity.visual_identity_en", "English character visual identity"),
            }],
            "scenes": [{
                "name": placeholder("bible.entity.name", "canonical scene name"),
                "description": placeholder("bible.entity.description", "scene description"),
                "visual_identity_en": placeholder("bible.entity.visual_identity_en", "English scene visual identity"),
            }],
            "props": [{
                "name": placeholder("bible.entity.name", "canonical prop name"),
                "description": placeholder("bible.entity.description", "prop description"),
                "visual_identity_en": placeholder("bible.entity.visual_identity_en", "English prop visual identity"),
            }],
        },
        "episodes": [
            {
                "title": placeholder("episode.title", f"title for episode {episode_index + 1}"),
                "shots": [dict(shot_contract) for _ in range(int(options["shots_per_episode"]))],
            }
            for episode_index in range(int(options["episode_count"]))
        ],
    }
    duration_rule = (
        f"Every shot.duration_ms MUST equal {int(options['minimum_duration_ms'])}."
        if int(options["minimum_duration_ms"]) == int(options["maximum_duration_ms"])
        else (
            "Every shot.duration_ms MUST be an integer between "
            f"{int(options['minimum_duration_ms'])} and "
            f"{int(options['maximum_duration_ms'])}, inclusive."
        )
    )
    profile_assembly = plan.get("prompt_profile_assembly")
    profile_system = ""
    profile_user: dict[str, Any] = {}
    if profile_assembly is not None:
        if not isinstance(profile_assembly, dict):
            raise ValidationError("script.generate PromptProfile assembly 无效")
        channels = profile_assembly.get("channels")
        structured = profile_assembly.get("structured_fields")
        if not isinstance(channels, dict) or not isinstance(structured, dict):
            raise ValidationError("script.generate PromptProfile channels 无效")
        profile_system = str(channels.get("system_instruction") or "").strip()
        profile_user = {
            "positive_prompt": str(channels.get("positive_prompt") or ""),
            "negative_prompt": str(channels.get("negative_prompt") or ""),
            "structured_fields": structured,
        }
    system_content = (
        "Return exactly one JSON object and no Markdown or prose. Replace every angle-"
        "bracket placeholder in CONTRACT with real content. The root MUST contain exactly "
        "schema_version,title,logline,bible,episodes. Produce exactly "
        f"{int(options['episode_count'])} episode(s), each with exactly "
        f"{int(options['shots_per_episode'])} shot(s). {duration_rule} "
        "Each shot MUST contain exactly setting,action,duration_ms,dialogue. "
        "dialogue MUST contain exactly speaker,delivery,text,start_ms,end_ms. For a silent "
        "shot all three dialogue strings MUST be empty and start_ms/end_ms MUST both equal "
        "0. For spoken dialogue speaker and text MUST both be non-empty, delivery MUST be "
        "a non-empty printable ASCII phrase with no parentheses, and integer timing MUST "
        "satisfy 0 <= start_ms < end_ms <= the same shot.duration_ms. "
        "All configurable text fields MUST follow the frozen FIELD_CONTRACT below. "
        "Every declared Bible entity name MUST appear verbatim in at "
        "least one shot setting, action, dialogue speaker, or dialogue text; do not replace "
        "a canonical Bible name with a synonym. Every non-empty dialogue speaker MUST "
        "exactly equal one declared Bible character name. Use the exact keys and array "
        "lengths shown in CONTRACT. "
        "FIELD_CONTRACT:\n"
        + field_contract_prompt_lines(field_contract)
        + "\nCONTRACT="
        + json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
    )
    if profile_system:
        system_content += "\nREVIEWED_PROMPT_PROFILE=" + profile_system
    user_content = {
        "idea": request["idea"],
        "style_hint": request["style_hint"],
        "episode_count": options["episode_count"],
        "shots_per_episode": options["shots_per_episode"],
        "episode_duration_ms": options["episode_duration_ms"],
        "minimum_duration_ms": options["minimum_duration_ms"],
        "maximum_duration_ms": options["maximum_duration_ms"],
    }
    if profile_user:
        user_content["prompt_profile"] = profile_user
    return [
        {
            "role": "system",
            "content": system_content,
        },
        {
            "role": "user",
            "content": json.dumps(user_content, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def provider_payload(plan: dict[str, Any]) -> dict[str, Any]:
    request = plan["request"]
    return {
        "messages": _messages(plan),
        "max_tokens": int(plan["resource_estimate"]["configured_output_token_hint"]),
        "timeout_seconds": float(request["timeout_seconds"]),
        CHAT_REQUEST_OPTIONS_KEY: deepcopy(request[CHAT_REQUEST_OPTIONS_KEY]),
        "seed": int(request["seed"]),
    }


def frozen_text_deployment(snapshot: dict[str, Any]) -> ModelDeployment:
    route = snapshot.get("route")
    if not isinstance(route, dict):
        raise ValidationError("文本 attempt 缺少冻结 route")
    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).filter(pk=route.get("model_deployment_id"), status=ModelDeployment.Status.ACTIVE).first()
    if deployment is None:
        raise ValidationError("冻结的文本模型部署不存在或未启用")
    provider = deployment.profile_version.provider
    checks = {
        "credential_binding_sha256": credential_binding_sha256(deployment),
        "model_profile_version_id": str(deployment.profile_version_id),
        "provider_id": str(provider.id),
        "driver_type": provider.driver_type,
    }
    for key, current in checks.items():
        if str(route.get(key) or "") != str(current or ""):
            raise ValidationError(f"冻结 route 的 {key} 已漂移")
    if not provider.is_active or deployment.profile_version.status != deployment.profile_version.Status.PUBLISHED:
        raise ValidationError("冻结 route 的 provider/model 已停用")
    provider_config = route.get("provider_config")
    deployment_config = route.get("deployment_config")
    model_specification = verified_frozen_model_profile_specification(
        deployment.profile_version,
        route,
    )
    if (
        not isinstance(provider_config, dict)
        or not isinstance(deployment_config, dict)
    ):
        raise ValidationError("冻结 route 缺少 provider/deployment/model specification")
    provider_contract = json_contract(
        provider_config,
        max_bytes=_text_snapshot_max_bytes(),
    )
    deployment_contract = json_contract(
        deployment_config,
        max_bytes=_text_snapshot_max_bytes(),
    )
    if provider_contract.sha256 != route.get("provider_config_sha256"):
        raise ValidationError("冻结 route 的 provider_config hash 无效")
    if deployment_contract.sha256 != route.get("deployment_config_sha256"):
        raise ValidationError("冻结 route 的 deployment_config hash 无效")
    specification_contract = json_contract(
        model_specification,
        max_bytes=_text_snapshot_max_bytes(),
    )
    # Build an in-memory execution view.  Endpoint/model/timeout are immutable run
    # inputs; only enablement and credential binding are deliberately read live.
    frozen_provider = copy(provider)
    frozen_provider.driver_config = provider_config
    frozen_provider.driver_config_schema_version = provider_contract.schema_version
    frozen_provider.driver_config_size_bytes = provider_contract.size_bytes
    frozen_provider.driver_config_sha256 = provider_contract.sha256
    frozen_profile = copy(deployment.profile_version)
    frozen_profile.provider = frozen_provider
    frozen_profile.model_identifier = str(route.get("model_identifier") or "")
    frozen_profile.specification = model_specification
    frozen_profile.specification_schema_version = specification_contract.schema_version
    frozen_profile.specification_size_bytes = specification_contract.size_bytes
    frozen_profile.specification_sha256 = specification_contract.sha256
    frozen_deployment = copy(deployment)
    frozen_deployment.profile_version = frozen_profile
    frozen_deployment.runtime_config = deployment_config
    frozen_deployment.runtime_config_schema_version = deployment_contract.schema_version
    frozen_deployment.runtime_config_size_bytes = deployment_contract.size_bytes
    frozen_deployment.runtime_config_sha256 = deployment_contract.sha256
    if not deployment_supports_script_generation(frozen_deployment):
        raise ValidationError("冻结 deployment 没有安全文本 transport")
    return frozen_deployment


@transaction.atomic
def claim_next_text_attempt(worker_id: str) -> GenerationAttempt | None:
    attempt = GenerationAttempt.objects.select_for_update(skip_locked=True).select_related(
        "stage_run__production_run",
    ).filter(
        operation__in=TEXT_OPERATIONS,
        status=GenerationAttempt.Status.QUEUED,
        committed_cost__isnull=False,
        stage_run__status=StageRun.Status.RUNNING,
        stage_run__production_run__status=ProductionRun.Status.RUNNING,
    ).order_by("created_at").first()
    if attempt is None:
        return None
    attempt.status = GenerationAttempt.Status.RUNNING
    attempt.worker_id = str(worker_id)[:160]
    attempt.claim_token = uuid.uuid4()
    attempt.claimed_at = timezone.now()
    attempt.heartbeat_at = attempt.claimed_at
    attempt.save(update_fields=(
        "status", "worker_id", "claim_token", "claimed_at", "heartbeat_at", "updated_at",
    ))
    return attempt


@transaction.atomic
def release_text_attempt_claim(attempt: GenerationAttempt) -> bool:
    """Return a locally-blocked, not-yet-submitted claim to the queue."""

    updated = GenerationAttempt.objects.filter(
        pk=attempt.pk,
        status=GenerationAttempt.Status.RUNNING,
        claim_token=attempt.claim_token,
    ).update(
        status=GenerationAttempt.Status.QUEUED,
        worker_id="",
        claim_token=None,
        claimed_at=None,
        heartbeat_at=None,
        updated_at=timezone.now(),
    )
    return updated == 1


@transaction.atomic
def recover_stale_text_attempts(minutes: float) -> dict[str, int]:
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, (int, float))
        or not math.isfinite(float(minutes))
        or float(minutes) <= 0
    ):
        raise ValueError("文本 attempt 陈旧任务回收阈值必须是正有限分钟数")
    cutoff = timezone.now() - timedelta(minutes=float(minutes))
    requeued = 0
    uncertain = 0
    for attempt in GenerationAttempt.objects.select_for_update().select_related(
        "stage_run__production_run",
    ).filter(
        operation__in=TEXT_OPERATIONS,
        status__in=(GenerationAttempt.Status.RUNNING, GenerationAttempt.Status.SUBMITTED),
        heartbeat_at__lt=cutoff,
    ):
        stage = attempt.stage_run
        run = stage.production_run
        if (
            run.status in TERMINAL_RUN_STATUSES
            or stage.status == StageRun.Status.CANCELLED
            or attempt.ignore_late_result
        ):
            attempt.status = GenerationAttempt.Status.CANCELLED
            attempt.ignore_late_result = True
            attempt.error_code = "late_result_ignored"
            attempt.error_message = "运行已终态；陈旧文本 attempt 只保留审计证据。"
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=(
                "status", "ignore_late_result", "error_code", "error_message",
                "finished_at", "updated_at",
            ))
            if not attempt.resolutions.filter(
                outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
            ).exists():
                append_attempt_resolution(
                    attempt,
                    outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
                    evidence={
                        "schema_version": 1,
                        "attempt_id": str(attempt.id),
                        "request_sha256": attempt.request_sha256,
                        "reason": "terminal_run_stale_attempt",
                    },
                    note="Stale attempt ignored because ProductionRun is terminal",
                )
            continue
        if attempt.status == GenerationAttempt.Status.RUNNING:
            attempt.status = GenerationAttempt.Status.QUEUED
            attempt.worker_id = ""
            attempt.claim_token = None
            attempt.claimed_at = None
            attempt.heartbeat_at = None
            attempt.save(update_fields=(
                "status", "worker_id", "claim_token", "claimed_at", "heartbeat_at", "updated_at",
            ))
            requeued += 1
            continue
        attempt.status = GenerationAttempt.Status.FAILED
        attempt.error_code = "uncertain_remote_response"
        attempt.error_message = "worker 在持久化提交边界后失联；禁止自动重复 POST。"
        attempt.finished_at = timezone.now()
        attempt.save(update_fields=(
            "status", "error_code", "error_message", "finished_at", "updated_at",
        ))
        stage.status = StageRun.Status.BLOCKED
        stage.reason_code = attempt.error_code
        stage.reason = attempt.error_message
        stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
        run.status = ProductionRun.Status.BLOCKED
        run.blocked_code = stage.reason_code
        run.blocked_reason = stage.reason
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
        uncertain += 1
    return {"requeued": requeued, "uncertain": uncertain}


def _fail_attempt(
    attempt_id,
    claim_token,
    *,
    code: str,
    message: str,
    uncertain: bool,
    record_committed_cost: bool = False,
    release_committed_cost: bool = False,
    failure_metadata: dict[str, Any] | None = None,
) -> None:
    auto_fallback_run_id = None
    with transaction.atomic():
        attempt = GenerationAttempt.objects.select_for_update().select_related(
            "stage_run__production_run",
        ).get(pk=attempt_id)
        if attempt.claim_token != claim_token or attempt.status in {
            GenerationAttempt.Status.SUCCEEDED,
            GenerationAttempt.Status.CANCELLED,
        }:
            return
        stage = attempt.stage_run
        run = stage.production_run
        if record_committed_cost and attempt.actual_cost is None and attempt.committed_cost is not None:
            reserved = attempt.committed_cost
            attempt.actual_cost = reserved
            attempt.committed_cost = Decimal("0")
            run.actual_cost_total += reserved
            run.committed_cost_total = max(
                Decimal("0"), run.committed_cost_total - reserved,
            )
            run.save(update_fields=(
                "actual_cost_total", "committed_cost_total", "updated_at",
            ))
        elif not uncertain and release_committed_cost and attempt.committed_cost:
            reserved = attempt.committed_cost
            attempt.committed_cost = Decimal("0")
            run.committed_cost_total = max(
                Decimal("0"), run.committed_cost_total - reserved,
            )
            run.save(update_fields=("committed_cost_total", "updated_at"))
        if (
            run.status in TERMINAL_RUN_STATUSES
            or stage.status == StageRun.Status.CANCELLED
            or attempt.ignore_late_result
        ):
            attempt.status = GenerationAttempt.Status.CANCELLED
            attempt.ignore_late_result = True
            attempt.error_code = "late_result_ignored"
            attempt.error_message = str(message or "晚到结果已忽略")[:2000]
            attempt.finished_at = timezone.now()
            attempt.heartbeat_at = attempt.finished_at
            attempt.save(update_fields=(
                "status", "ignore_late_result", "actual_cost", "committed_cost",
                "error_code", "error_message",
                "finished_at", "heartbeat_at", "updated_at",
            ))
            append_attempt_resolution(
                attempt,
                outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
                evidence={
                    "schema_version": 1,
                    "attempt_id": str(attempt.id),
                    "request_sha256": attempt.request_sha256,
                    "failure_code": code,
                    "actual_cost": str(attempt.actual_cost) if attempt.actual_cost is not None else None,
                    "actual_cost_source": "committed_cost" if record_committed_cost else "unknown",
                },
                note="Late text failure ignored because ProductionRun is terminal",
            )
            return
        normalized_code = str(code or "text_attempt_failed")[:100]
        attempt.status = GenerationAttempt.Status.FAILED
        attempt.error_code = "uncertain_remote_response" if uncertain else normalized_code
        attempt.error_message = message[:2000]
        attempt.finished_at = timezone.now()
        attempt.heartbeat_at = attempt.finished_at
        attempt.save(update_fields=(
            "status", "actual_cost", "committed_cost", "error_code", "error_message", "finished_at",
            "heartbeat_at", "updated_at",
        ))
        if record_committed_cost:
            classifier = str(
                (failure_metadata or {}).get("contract_error_code") or "",
            )
            safe_classifier = (
                classifier if classifier in SAFE_PROMPT_RESPONSE_CLASSIFIERS else ""
            )
            safe_reason_code = safe_prompt_field_validation_reason_code(
                (failure_metadata or {}).get("reason_code"),
            )
            safe_validation_context = safe_prompt_field_validation_context(
                safe_reason_code,
                (failure_metadata or {}).get("validation_context"),
                request_payload=(
                    attempt.request_snapshot.get("payload")
                    if isinstance(attempt.request_snapshot, dict)
                    else None
                ),
            )
            finish_reason = str(
                (failure_metadata or {}).get("finish_reason") or "",
            )
            safe_finish_reason = (
                finish_reason if finish_reason in SAFE_PROMPT_FINISH_REASONS else ""
            )
            raw_usage = (failure_metadata or {}).get("usage")
            safe_usage = {
                field: raw_usage[field]
                for field in SAFE_PROMPT_USAGE_FIELDS
                if isinstance(raw_usage, dict)
                and type(raw_usage.get(field)) is int
                and 0 <= raw_usage[field] <= prompt_usage_max_count()
            }
            response_id_sha256 = str(
                (failure_metadata or {}).get("response_id_sha256") or "",
            )
            safe_response_id_sha256 = (
                response_id_sha256
                if len(response_id_sha256) == 64
                and all(character in "0123456789abcdef" for character in response_id_sha256)
                else ""
            )
            append_attempt_resolution(
                attempt,
                outcome=GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED,
                evidence={
                    "schema_version": 1,
                    "attempt_id": str(attempt.id),
                    "request_sha256": attempt.request_sha256,
                    "failure_code": code,
                    **(
                        {"contract_error_code": safe_classifier}
                        if safe_classifier else {}
                    ),
                    **(
                        {"reason_code": safe_reason_code}
                        if safe_reason_code else {}
                    ),
                    **(
                        {"validation_context": safe_validation_context}
                        if safe_validation_context else {}
                    ),
                    **(
                        {"finish_reason": safe_finish_reason}
                        if safe_finish_reason else {}
                    ),
                    **({"usage": safe_usage} if safe_usage else {}),
                    **(
                        {"response_id_sha256": safe_response_id_sha256}
                        if safe_response_id_sha256 else {}
                    ),
                    "actual_cost": str(attempt.actual_cost) if attempt.actual_cost is not None else None,
                    "actual_cost_source": "committed_cost",
                },
                note="Provider returned a response that failed strict finalization",
            )
        prompt_validation_recoverable = (
            attempt.operation == PROMPT_NORMALIZE_OPERATION
            and attempt.error_code in {
                "prompt_normalize_contract_failed",
                "provider_response_invalid",
            }
        )
        text_contract_recoverable = (
            attempt.operation in {
                TEXT_OPERATION,
                SCRIPT_STRUCTURE_OPERATION,
                VISUAL_RULES_OPERATION,
            }
            and attempt.error_code == "provider_response_invalid"
            and record_committed_cost
        )
        auto_fallback = (
            not uncertain
            and (
                prompt_validation_recoverable
                or text_contract_recoverable
                or (
                    isinstance(stage.routing, dict)
                    and stage.routing.get("mode") == "ordered_fallback"
                    and attempt.error_code in {
                        "text_attempt_pre_submit_validation_failed",
                        "series_concept_invalid_response",
                        "provider_request_rejected",
                        "remote_absence_confirmed",
                    }
                )
            )
        )
        stage.status = StageRun.Status.RUNNING if auto_fallback else StageRun.Status.BLOCKED
        stage.reason_code = "" if auto_fallback else attempt.error_code
        stage.reason = "" if auto_fallback else (
            "文本模型提交后的响应状态不确定；普通 retry 已禁止，需管理员核验远端缺失。"
            if uncertain else "文本模型没有返回可通过严格校验的候选结果。"
        )
        stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
        run.status = ProductionRun.Status.RUNNING if auto_fallback else ProductionRun.Status.BLOCKED
        run.blocked_code = "" if auto_fallback else stage.reason_code
        run.blocked_reason = "" if auto_fallback else stage.reason
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
        if auto_fallback:
            auto_fallback_run_id = run.id
    if auto_fallback_run_id is not None:
        from studio.services.production_runs import advance_production_run

        advance_production_run(ProductionRun.objects.get(pk=auto_fallback_run_id))


def fail_unhandled_text_attempt(attempt: GenerationAttempt, exc: Exception) -> None:
    current = GenerationAttempt.objects.filter(pk=attempt.pk).only(
        "status", "claim_token",
    ).first()
    if current is None or current.status not in {
        GenerationAttempt.Status.RUNNING,
        GenerationAttempt.Status.SUBMITTED,
    }:
        return
    submitted = current.status == GenerationAttempt.Status.SUBMITTED
    _fail_attempt(
        current.pk,
        current.claim_token,
        code="text_worker_unhandled_failure",
        message=str(exc),
        uncertain=submitted,
        # An unclassified post-boundary failure does not prove billing. Keep
        # the reservation committed until an operator resolves the attempt.
        record_committed_cost=False,
    )


def _process_script_attempt(attempt: GenerationAttempt) -> GenerationAttempt:
    attempt_id = attempt.pk
    claim_token = attempt.claim_token
    try:
        with transaction.atomic():
            locked = GenerationAttempt.objects.select_for_update().get(pk=attempt_id)
            if locked.status != GenerationAttempt.Status.RUNNING or locked.claim_token != claim_token:
                return locked
            verify_json_contract(
                locked.request_snapshot,
                schema_version=locked.request_snapshot_schema_version,
                size_bytes=locked.request_snapshot_size_bytes,
                sha256=locked.request_snapshot_sha256,
                max_bytes=_text_snapshot_max_bytes(),
            )
            snapshot = locked.request_snapshot
            plan = snapshot.get("plan")
            if not isinstance(plan, dict):
                raise ValidationError("script.generate attempt 缺少 plan")
            _verify_attempt_prompt_profile(
                locked,
                plan.get("prompt_profile_assembly"),
            )
            deployment = frozen_text_deployment(snapshot)
            driver = driver_for(deployment.profile_version.provider)
            locked.status = GenerationAttempt.Status.SUBMITTED
            locked.heartbeat_at = timezone.now()
            locked.save(update_fields=("status", "heartbeat_at", "updated_at"))
    except Exception as exc:
        _fail_attempt(
            attempt_id,
            claim_token,
            code=str(getattr(exc, "code", None) or "text_attempt_pre_submit_validation_failed"),
            message=str(exc),
            uncertain=False,
            release_committed_cost=True,
        )
        return GenerationAttempt.objects.get(pk=attempt_id)
    try:
        response = driver.invoke(
            deployment, "chat_completion", provider_payload(snapshot["plan"]),
        )
    except Exception as exc:
        known_driver_failure = (
            isinstance(exc, ProviderDriverError)
            and not exc.submission_uncertain
        )
        code = getattr(exc, "code", "provider_unclassified_failure")
        known_response_invalid = known_driver_failure and code == "provider_response_invalid"
        normalized_code = (
            code
            if code in {"provider_request_rejected", "provider_response_invalid"}
            else "text_attempt_pre_submit_validation_failed"
            if known_driver_failure
            else code
        )
        _fail_attempt(
            attempt_id,
            claim_token,
            code=normalized_code,
            message=str(exc),
            uncertain=not known_driver_failure,
            record_committed_cost=known_response_invalid,
            release_committed_cost=known_driver_failure and not known_response_invalid,
            failure_metadata=(
                exc.metadata
                if isinstance(getattr(exc, "metadata", None), dict)
                else None
            ),
        )
        return GenerationAttempt.objects.get(pk=attempt_id)
    try:
        decoded = json.loads(str(response.get("text") or ""))
        result = finalize_series_concept_draft(
            plan=snapshot["plan"],
            runtime_payload={
                "source_fingerprint": snapshot["plan"]["source_fingerprint"],
                "result": decoded,
                "review_issues": [],
            },
        )
    except Exception as exc:
        _fail_attempt(
            attempt_id, claim_token,
            code="series_concept_invalid_response", message=str(exc), uncertain=False,
            record_committed_cost=True,
        )
        return GenerationAttempt.objects.get(pk=attempt_id)

    with transaction.atomic():
        locked = GenerationAttempt.objects.select_for_update().select_related(
            "stage_run__production_run__owner", "stage_run__production_run__project",
        ).get(pk=attempt_id)
        if locked.status != GenerationAttempt.Status.SUBMITTED or locked.claim_token != claim_token:
            return locked
        stage = locked.stage_run
        run = stage.production_run
        result_contract = json_contract(result, max_bytes=_text_snapshot_max_bytes())
        if (
            run.status in TERMINAL_RUN_STATUSES
            or stage.status == StageRun.Status.CANCELLED
            or locked.ignore_late_result
        ):
            locked.provider_request_id = str(response.get("request_id") or "")[:191]
            locked.result_sha256 = result_contract.sha256
            if locked.actual_cost is None and locked.committed_cost is not None:
                reserved = locked.committed_cost
                locked.actual_cost = reserved
                locked.committed_cost = Decimal("0")
                run.actual_cost_total += reserved
                run.committed_cost_total = max(
                    Decimal("0"), run.committed_cost_total - reserved,
                )
                run.save(update_fields=(
                    "actual_cost_total", "committed_cost_total", "updated_at",
                ))
            locked.status = GenerationAttempt.Status.CANCELLED
            locked.ignore_late_result = True
            locked.error_code = "late_result_ignored"
            locked.error_message = "ProductionRun 已终态；晚到文本结果未创建正式剧本或产物。"
            locked.finished_at = timezone.now()
            locked.heartbeat_at = locked.finished_at
            locked.save(update_fields=(
                "provider_request_id", "result_sha256", "actual_cost", "committed_cost", "status",
                "ignore_late_result", "error_code", "error_message", "finished_at",
                "heartbeat_at", "updated_at",
            ))
            append_attempt_resolution(
                locked,
                outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
                evidence={
                    "schema_version": 1,
                    "attempt_id": str(locked.id),
                    "provider_request_id": locked.provider_request_id,
                    "result_sha256": locked.result_sha256,
                    "actual_cost": str(locked.actual_cost) if locked.actual_cost is not None else None,
                    "actual_cost_source": "committed_cost",
                },
                note="Strictly validated late result ignored after ProductionRun termination",
            )
            return locked
        source, revision = create_pasted_script_source(
            project=run.project,
            user=run.owner,
            content=result["candidate"]["master_script"],
            source_format=ScriptSource.SourceFormat.PLAIN,
            source_type=ScriptSource.SourceType.AI_GENERATED,
        )
        revision.origin_generation_attempt = locked
        revision.save(update_fields=("origin_generation_attempt", "updated_at"))
        metadata = {
            "schema_version": 1,
            "script_source_id": str(source.id),
            "script_revision_id": str(revision.id),
            "generation_attempt_id": str(locked.id),
            "source_fingerprint": result["source_fingerprint"],
            "title": result["candidate"]["title"],
            "logline": result["candidate"]["logline"],
            "warnings": result["warnings"],
        }
        manifest = json_contract(metadata, max_bytes=_text_snapshot_max_bytes())
        artifact = GeneratedArtifact.objects.create(
            manifest_key=f"run:{run.id}:generated-script:{locked.id}",
            production_run=run,
            stage_run=stage,
            generation_attempt=locked,
            kind=GeneratedArtifact.Kind.SCRIPT,
            role="generated_script",
            variant_group="script",
            variant_index=1,
            status=GeneratedArtifact.Status.AVAILABLE,
            storage_backend="database",
            object_key=f"script-revisions/{revision.id}",
            mime_type="text/plain; charset=utf-8",
            size_bytes=revision.content_size_bytes,
            sha256=revision.content_sha256,
            manifest_metadata=manifest.value,
            manifest_schema_version=manifest.schema_version,
            manifest_metadata_size_bytes=manifest.size_bytes,
            manifest_metadata_sha256=manifest.sha256,
        )
        run.script_revision = revision
        reserved = locked.committed_cost or Decimal("0")
        run.actual_cost_total += reserved
        run.committed_cost_total = max(
            Decimal("0"), run.committed_cost_total - reserved,
        )
        run.save(update_fields=(
            "script_revision", "actual_cost_total", "committed_cost_total", "updated_at",
        ))
        locked.provider_request_id = str(response.get("request_id") or "")[:191]
        locked.result_sha256 = result_contract.sha256
        locked.actual_cost = reserved
        locked.committed_cost = Decimal("0")
        locked.status = GenerationAttempt.Status.SUCCEEDED
        locked.error_code = ""
        locked.error_message = ""
        locked.finished_at = timezone.now()
        locked.heartbeat_at = locked.finished_at
        locked.save(update_fields=(
            "provider_request_id", "result_sha256", "actual_cost", "committed_cost", "status", "error_code",
            "error_message", "finished_at", "heartbeat_at", "updated_at",
        ))
        review_payload = {
            "schema_version": 1,
            "kind": "generated_script_preview",
            "script_revision_id": str(revision.id),
            "artifact_id": str(artifact.id),
            "attempt_id": str(locked.id),
            "content_sha256": revision.content_sha256,
            "result_sha256": locked.result_sha256,
            "title": result["candidate"]["title"],
            "logline": result["candidate"]["logline"],
            "warnings": result["warnings"],
        }
        contract = json_contract(review_payload, max_bytes=_text_snapshot_max_bytes())
        stage.output_snapshot = contract.value
        stage.output_snapshot_schema_version = contract.schema_version
        stage.output_snapshot_size_bytes = contract.size_bytes
        stage.output_snapshot_sha256 = contract.sha256
        stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
            "output_snapshot_sha256", "updated_at",
        ))
    from studio.services.production_runs import advance_production_run
    advance_production_run(run)
    return GenerationAttempt.objects.get(pk=attempt_id)


def _process_script_structure_attempt(attempt: GenerationAttempt) -> GenerationAttempt:
    attempt_id = attempt.pk
    claim_token = attempt.claim_token
    try:
        with transaction.atomic():
            locked = GenerationAttempt.objects.select_for_update().get(pk=attempt_id)
            if locked.status != GenerationAttempt.Status.RUNNING or locked.claim_token != claim_token:
                return locked
            if locked.operation != SCRIPT_STRUCTURE_OPERATION:
                raise ValidationError("文本 attempt operation 与 script.structure worker 不匹配")
            verify_json_contract(
                locked.request_snapshot,
                schema_version=locked.request_snapshot_schema_version,
                size_bytes=locked.request_snapshot_size_bytes,
                sha256=locked.request_snapshot_sha256,
                max_bytes=_text_snapshot_max_bytes(),
            )
            snapshot = locked.request_snapshot
            plan = snapshot.get("plan")
            if not isinstance(plan, dict) or plan.get("operation") != SCRIPT_STRUCTURE_OPERATION:
                raise ValidationError("script.structure attempt 缺少冻结 plan")
            _verify_attempt_prompt_profile(
                locked,
                plan.get("prompt_profile_assembly"),
            )
            if locked.model_deployment_id is None:
                raise ValidationError("script.structure attempt 缺少 model deployment")
            live_deployment = ModelDeployment.objects.select_related(
                "profile_version__provider",
            ).get(pk=locked.model_deployment_id)
            verify_script_structure_smoke(live_deployment, snapshot.get("route") or {})
            deployment = frozen_text_deployment(snapshot)
            driver = driver_for(deployment.profile_version.provider)
            locked.status = GenerationAttempt.Status.SUBMITTED
            locked.heartbeat_at = timezone.now()
            locked.save(update_fields=("status", "heartbeat_at", "updated_at"))
    except Exception as exc:
        _fail_attempt(
            attempt_id,
            claim_token,
            code=str(getattr(exc, "code", None) or "text_attempt_pre_submit_validation_failed"),
            message=str(exc),
            uncertain=False,
            release_committed_cost=True,
        )
        return GenerationAttempt.objects.get(pk=attempt_id)
    try:
        response = driver.invoke(
            deployment,
            "chat_completion",
            structure_provider_payload(snapshot["plan"]),
        )
    except Exception as exc:
        known_driver_failure = isinstance(exc, ProviderDriverError) and not exc.submission_uncertain
        code = getattr(exc, "code", "provider_unclassified_failure")
        known_response_invalid = known_driver_failure and code == "provider_response_invalid"
        normalized_code = (
            code
            if code in {"provider_request_rejected", "provider_response_invalid"}
            else "text_attempt_pre_submit_validation_failed"
            if known_driver_failure
            else code
        )
        _fail_attempt(
            attempt_id,
            claim_token,
            code=normalized_code,
            message=str(exc),
            uncertain=not known_driver_failure,
            record_committed_cost=known_response_invalid,
            release_committed_cost=known_driver_failure and not known_response_invalid,
            failure_metadata=(
                exc.metadata
                if isinstance(exc, ProviderDriverError)
                and isinstance(getattr(exc, "metadata", None), dict)
                else None
            ),
        )
        return GenerationAttempt.objects.get(pk=attempt_id)
    try:
        if not isinstance(response, dict):
            raise ValidationError("script.structure provider 响应必须是对象")
        json_contract(response, max_bytes=_text_snapshot_max_bytes())
        decoded = json.loads(str(response.get("text") or ""))
        result = finalize_script_structure(plan=snapshot["plan"], payload=decoded)
    except Exception as exc:
        _fail_attempt(
            attempt_id,
            claim_token,
            code=str(getattr(exc, "code", None) or "script_structure_invalid_response"),
            message=str(exc),
            uncertain=False,
            record_committed_cost=True,
        )
        return GenerationAttempt.objects.get(pk=attempt_id)

    with transaction.atomic():
        locked = GenerationAttempt.objects.select_for_update().select_related(
            "stage_run__production_run__owner",
            "stage_run__production_run__project",
            "stage_run__production_run__script_revision",
        ).get(pk=attempt_id)
        if locked.status != GenerationAttempt.Status.SUBMITTED or locked.claim_token != claim_token:
            return locked
        stage = locked.stage_run
        run = stage.production_run
        result_contract = json_contract(result, max_bytes=_text_snapshot_max_bytes())
        if (
            run.status in TERMINAL_RUN_STATUSES
            or stage.status == StageRun.Status.CANCELLED
            or locked.ignore_late_result
        ):
            locked.provider_request_id = str(response.get("request_id") or "")[:191]
            locked.result_sha256 = result_contract.sha256
            if locked.actual_cost is None and locked.committed_cost is not None:
                reserved = locked.committed_cost
                locked.actual_cost = reserved
                locked.committed_cost = Decimal("0")
                run.actual_cost_total += reserved
                run.committed_cost_total = max(
                    Decimal("0"), run.committed_cost_total - reserved,
                )
                run.save(update_fields=(
                    "actual_cost_total", "committed_cost_total", "updated_at",
                ))
            locked.status = GenerationAttempt.Status.CANCELLED
            locked.ignore_late_result = True
            locked.error_code = "late_result_ignored"
            locked.error_message = (
                "ProductionRun 已终态；晚到结构化结果未创建 preview revision 或产物。"
            )
            locked.finished_at = timezone.now()
            locked.heartbeat_at = locked.finished_at
            locked.save(update_fields=(
                "provider_request_id", "result_sha256", "actual_cost", "committed_cost",
                "status", "ignore_late_result", "error_code", "error_message",
                "finished_at", "heartbeat_at", "updated_at",
            ))
            append_attempt_resolution(
                locked,
                outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
                evidence={
                    "schema_version": 1,
                    "attempt_id": str(locked.id),
                    "provider_request_id": locked.provider_request_id,
                    "result_sha256": locked.result_sha256,
                    "actual_cost": str(locked.actual_cost) if locked.actual_cost is not None else None,
                    "actual_cost_source": "committed_cost",
                },
                note="Strictly validated late script.structure result ignored after run termination",
            )
            return locked
        revision, _artifact, review_payload = materialize_script_structure_preview(
            run=run,
            stage=stage,
            attempt=locked,
            result=result,
            result_sha256=result_contract.sha256,
        )
        run.script_revision = revision
        reserved = locked.committed_cost or Decimal("0")
        run.actual_cost_total += reserved
        run.committed_cost_total = max(
            Decimal("0"), run.committed_cost_total - reserved,
        )
        run.save(update_fields=(
            "script_revision", "actual_cost_total", "committed_cost_total", "updated_at",
        ))
        locked.provider_request_id = str(response.get("request_id") or "")[:191]
        locked.result_sha256 = result_contract.sha256
        locked.actual_cost = reserved
        locked.committed_cost = Decimal("0")
        locked.status = GenerationAttempt.Status.SUCCEEDED
        locked.error_code = ""
        locked.error_message = ""
        locked.finished_at = timezone.now()
        locked.heartbeat_at = locked.finished_at
        locked.save(update_fields=(
            "provider_request_id", "result_sha256", "actual_cost", "committed_cost",
            "status", "error_code", "error_message", "finished_at", "heartbeat_at",
            "updated_at",
        ))
        contract = json_contract(review_payload, max_bytes=_text_snapshot_max_bytes())
        stage.output_snapshot = contract.value
        stage.output_snapshot_schema_version = contract.schema_version
        stage.output_snapshot_size_bytes = contract.size_bytes
        stage.output_snapshot_sha256 = contract.sha256
        stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
            "output_snapshot_sha256", "updated_at",
        ))
    from studio.services.production_runs import advance_production_run

    advance_production_run(run)
    return GenerationAttempt.objects.get(pk=attempt_id)


def _process_visual_rules_attempt(attempt: GenerationAttempt) -> GenerationAttempt:
    attempt_id = attempt.pk
    claim_token = attempt.claim_token
    try:
        with transaction.atomic():
            locked = GenerationAttempt.objects.select_for_update().get(pk=attempt_id)
            if locked.status != GenerationAttempt.Status.RUNNING or locked.claim_token != claim_token:
                return locked
            if locked.operation != VISUAL_RULES_OPERATION:
                raise ValidationError("文本 attempt operation 与 visual_rules.generate worker 不匹配")
            verify_json_contract(
                locked.request_snapshot,
                schema_version=locked.request_snapshot_schema_version,
                size_bytes=locked.request_snapshot_size_bytes,
                sha256=locked.request_snapshot_sha256,
                max_bytes=_text_snapshot_max_bytes(),
            )
            snapshot = locked.request_snapshot
            plan = snapshot.get("plan")
            if not isinstance(plan, dict) or plan.get("operation") != VISUAL_RULES_OPERATION:
                raise ValidationError("visual_rules.generate attempt 缺少冻结 plan")
            _verify_attempt_prompt_profile(locked, plan.get("prompt_profile_assembly"))
            if locked.model_deployment_id is None:
                raise ValidationError("visual_rules.generate attempt 缺少 model deployment")
            live_deployment = ModelDeployment.objects.select_related(
                "profile_version__provider",
            ).get(pk=locked.model_deployment_id)
            verify_visual_rules_smoke(live_deployment, snapshot.get("route") or {})
            deployment = frozen_text_deployment(snapshot)
            driver = driver_for(deployment.profile_version.provider)
            submitted_at = timezone.now()
            locked.status = GenerationAttempt.Status.SUBMITTED
            locked.submission_started_at = locked.submission_started_at or submitted_at
            locked.submitted_at = locked.submitted_at or submitted_at
            locked.heartbeat_at = submitted_at
            locked.save(update_fields=(
                "status", "submission_started_at", "submitted_at", "heartbeat_at", "updated_at",
            ))
    except Exception as exc:
        _fail_attempt(
            attempt_id,
            claim_token,
            code=str(getattr(exc, "code", None) or "text_attempt_pre_submit_validation_failed"),
            message=str(exc),
            uncertain=False,
            release_committed_cost=True,
        )
        return GenerationAttempt.objects.get(pk=attempt_id)
    try:
        response = driver.invoke(
            deployment,
            "chat_completion",
            visual_rules_provider_payload(snapshot["plan"]),
        )
    except Exception as exc:
        known_driver_failure = isinstance(exc, ProviderDriverError) and not exc.submission_uncertain
        code = str(getattr(exc, "code", "provider_unclassified_failure"))
        known_response_invalid = known_driver_failure and code == "provider_response_invalid"
        normalized_code = (
            code
            if code in {"provider_request_rejected", "provider_response_invalid"}
            else "text_attempt_pre_submit_validation_failed"
            if known_driver_failure
            else code
        )
        _fail_attempt(
            attempt_id,
            claim_token,
            code=normalized_code,
            message=str(exc),
            uncertain=not known_driver_failure,
            record_committed_cost=known_response_invalid,
            release_committed_cost=known_driver_failure and not known_response_invalid,
            failure_metadata=(
                exc.metadata
                if isinstance(exc, ProviderDriverError)
                and isinstance(getattr(exc, "metadata", None), dict)
                else None
            ),
        )
        return GenerationAttempt.objects.get(pk=attempt_id)
    try:
        if not isinstance(response, dict):
            raise ValidationError("visual_rules.generate provider 响应必须是对象")
        json_contract(response, max_bytes=_text_snapshot_max_bytes())
        decoded = json.loads(str(response.get("text") or ""))
        result = finalize_visual_rules(plan=snapshot["plan"], payload=decoded)
        resource_estimate = snapshot["plan"].get("resource_estimate")
        warning_threshold = (
            resource_estimate.get("output_token_warning_threshold")
            if isinstance(resource_estimate, dict) else None
        )
        response_usage = response.get("usage")
        completion_tokens = (
            response_usage.get("completion_tokens")
            if isinstance(response_usage, dict) else None
        )
        if (
            type(warning_threshold) is int
            and type(completion_tokens) is int
            and completion_tokens > warning_threshold
        ):
            result["warnings"] = [
                *list(result.get("warnings") or []),
                (
                    "模型完成 token 超过配置的建议阈值；严格结构校验已通过，"
                    "本次候选继续进入审核。"
                ),
            ]
        result_contract = json_contract(result, max_bytes=_text_snapshot_max_bytes())
    except Exception as exc:
        _fail_attempt(
            attempt_id,
            claim_token,
            code=str(getattr(exc, "code", None) or "visual_rules_response_invalid"),
            message=str(exc),
            uncertain=False,
            record_committed_cost=True,
        )
        return GenerationAttempt.objects.get(pk=attempt_id)

    with transaction.atomic():
        locked = GenerationAttempt.objects.select_for_update().select_related(
            "stage_run__production_run",
            "model_deployment__profile_version__provider",
        ).get(pk=attempt_id)
        if locked.status != GenerationAttempt.Status.SUBMITTED or locked.claim_token != claim_token:
            return locked
        stage = locked.stage_run
        run = stage.production_run
        provider_request_id = str(response.get("request_id") or "")[:191]
        if (
            run.status in TERMINAL_RUN_STATUSES
            or stage.status == StageRun.Status.CANCELLED
            or locked.ignore_late_result
        ):
            locked.provider_request_id = provider_request_id
            locked.result_sha256 = result_contract.sha256
            if locked.actual_cost is None and locked.committed_cost is not None:
                reserved = locked.committed_cost
                locked.actual_cost = reserved
                locked.committed_cost = Decimal("0")
                run.actual_cost_total += reserved
                run.committed_cost_total = max(Decimal("0"), run.committed_cost_total - reserved)
                run.save(update_fields=(
                    "actual_cost_total", "committed_cost_total", "updated_at",
                ))
            locked.status = GenerationAttempt.Status.CANCELLED
            locked.ignore_late_result = True
            locked.error_code = "late_result_ignored"
            locked.error_message = "ProductionRun 已终态；晚到影视规则候选未写入正式项目字段。"
            locked.finished_at = timezone.now()
            locked.heartbeat_at = locked.finished_at
            locked.save(update_fields=(
                "provider_request_id", "result_sha256", "actual_cost", "committed_cost",
                "status", "ignore_late_result", "error_code", "error_message",
                "finished_at", "heartbeat_at", "updated_at",
            ))
            append_attempt_resolution(
                locked,
                outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
                evidence={
                    "schema_version": 1,
                    "attempt_id": str(locked.id),
                    "result_sha256": locked.result_sha256,
                    "actual_cost": str(locked.actual_cost) if locked.actual_cost is not None else None,
                    "actual_cost_source": "committed_cost",
                },
                note="Validated late visual_rules.generate result ignored after run termination",
            )
            return locked
        reserved = locked.committed_cost or Decimal("0")
        run.actual_cost_total += reserved
        run.committed_cost_total = max(Decimal("0"), run.committed_cost_total - reserved)
        run.save(update_fields=("actual_cost_total", "committed_cost_total", "updated_at"))
        locked.provider_request_id = provider_request_id
        locked.result_sha256 = result_contract.sha256
        locked.actual_cost = reserved
        locked.committed_cost = Decimal("0")
        locked.status = GenerationAttempt.Status.SUCCEEDED
        locked.error_code = ""
        locked.error_message = ""
        locked.finished_at = timezone.now()
        locked.heartbeat_at = locked.finished_at
        locked.save(update_fields=(
            "provider_request_id", "result_sha256", "actual_cost", "committed_cost",
            "status", "error_code", "error_message", "finished_at", "heartbeat_at",
            "updated_at",
        ))
        profile = locked.model_deployment.profile_version
        provider = profile.provider
        review_payload = {
            **result,
            "attempt_id": str(locked.id),
            "result_sha256": locked.result_sha256,
            "provider": str(provider.key or "")[:80],
            "model": str(profile.model_identifier or "")[:200],
        }
        contract = json_contract(review_payload, max_bytes=_text_snapshot_max_bytes())
        stage.output_snapshot = contract.value
        stage.output_snapshot_schema_version = contract.schema_version
        stage.output_snapshot_size_bytes = contract.size_bytes
        stage.output_snapshot_sha256 = contract.sha256
        stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
            "output_snapshot_sha256", "updated_at",
        ))
    from studio.services.production_runs import advance_production_run

    advance_production_run(run)
    return GenerationAttempt.objects.get(pk=attempt_id)


def _process_prompt_normalization_attempt(
    attempt: GenerationAttempt,
) -> GenerationAttempt:
    from studio.services.prompt_normalization import (
        ROUTE_CONTEXT_DURABLE_ATTEMPT,
        invoke_prompt_normalization,
    )
    from studio.services.production_runs import (
        _verify_prompt_normalization_batch_plan,
    )

    attempt_id = attempt.pk
    claim_token = attempt.claim_token
    try:
        with transaction.atomic():
            locked = GenerationAttempt.objects.select_for_update().get(pk=attempt_id)
            if locked.status != GenerationAttempt.Status.RUNNING or locked.claim_token != claim_token:
                return locked
            if locked.operation != PROMPT_NORMALIZE_OPERATION:
                raise ValidationError("文本 attempt operation 与 worker 不匹配")
            verify_json_contract(
                locked.request_snapshot,
                schema_version=locked.request_snapshot_schema_version,
                size_bytes=locked.request_snapshot_size_bytes,
                sha256=locked.request_snapshot_sha256,
                max_bytes=_text_snapshot_max_bytes(),
            )
            snapshot = locked.request_snapshot
            payload = snapshot.get("payload")
            if not isinstance(payload, dict):
                raise ValidationError("prompt.normalize attempt 缺少 payload")
            _verify_attempt_prompt_profile(
                locked,
                payload.get("prompt_profile_assembly"),
            )
            payload_contract = json_contract(payload, max_bytes=_text_snapshot_max_bytes())
            if (
                payload_contract.sha256 != locked.request_sha256
                or snapshot.get("payload_sha256") != payload_contract.sha256
            ):
                raise ValidationError("prompt.normalize request identity 已漂移")
            targets = snapshot.get("targets")
            if not isinstance(targets, list) or len(targets) != len(payload.get("items") or []):
                raise ValidationError("prompt.normalize targets 快照无效")
            deployment = frozen_text_deployment(snapshot)
            batch_plan = _verify_prompt_normalization_batch_plan(
                snapshot.get("batch_plan"),
            )
            batch_index = snapshot.get("batch_index")
            batches = batch_plan["batches"]
            if type(batch_index) is not int or not 0 <= batch_index < len(batches):
                raise ValidationError("prompt.normalize batch_index 无效")
            frozen_batch = batches[batch_index]
            request_ids = [
                str(item.get("request_id") or "")
                for item in payload.get("items") or []
                if isinstance(item, dict)
            ]
            target_request_ids = [
                str(item.get("request_id") or "")
                for item in targets
                if isinstance(item, dict)
            ]
            if (
                locked.unit_key != frozen_batch.get("unit_key")
                or request_ids != frozen_batch.get("request_ids")
                or target_request_ids != frozen_batch.get("request_ids")
                or snapshot.get("batch_count") != len(batches)
            ):
                raise ValidationError("prompt.normalize attempt 与冻结批次身份不一致")
            capacity_policy = prompt_normalization_policy_from_specification(
                verified_model_profile_specification(deployment.profile_version),
                required=True,
            )
            if (
                capacity_policy is None
                or snapshot.get("capacity_policy") != capacity_policy
                or snapshot.get("capacity_policy_sha256")
                != json_contract(
                    capacity_policy,
                    max_bytes=_text_snapshot_max_bytes(),
                ).sha256
            ):
                raise ValidationError("prompt.normalize 冻结模型容量合同已漂移")
            chat_payload = prompt_normalization_chat_payload(
                payload,
                policy=capacity_policy,
            )
            if snapshot.get(
                "chat_input_bytes",
            ) != prompt_normalization_chat_messages_input_bytes(
                chat_payload["messages"],
            ):
                raise ValidationError("prompt.normalize 冻结 chat 输入字节数已漂移")
            execution_kind = str(snapshot.get("execution_kind") or "")
            if (
                execution_kind != str(frozen_batch.get("execution_kind") or "")
                or snapshot.get("materialization_proofs_sha256")
                != frozen_batch.get("materialization_proofs_sha256")
            ):
                raise ValidationError("prompt.normalize attempt 执行种类已漂移")
            if execution_kind == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC:
                if (
                    locked.is_external
                    or locked.estimated_cost != Decimal("0")
                    or locked.committed_cost != Decimal("0")
                    or locked.actual_cost not in {None, Decimal("0")}
                    or locked.cost_currency
                    or snapshot.get("submission_semantics")
                    != PROMPT_NORMALIZATION_LOCAL_SUBMISSION_SEMANTICS
                    or snapshot.get("repair_of_attempt_id")
                ):
                    raise ValidationError("prompt.normalize 本地物化费用或提交合同无效")
                local_items = []
                local_warnings = []
                for item, target in zip(payload.get("items") or [], targets, strict=True):
                    materialization = deterministic_prompt_normalization_materialization(
                        item=item,
                        target=target,
                        request_payload=payload,
                    )
                    if materialization is None:
                        raise ValidationError(
                            "prompt.normalize 本地物化批次包含 Provider 字段",
                        )
                    local_items.append({
                        "request_id": str(item.get("request_id") or ""),
                        "translation": str(materialization["translated_text"]),
                    })
                    local_warnings.extend(deepcopy(materialization["warnings"]))
                response = {
                    "items": local_items,
                    "request_id": "",
                    "metadata": {
                        "provider": "",
                        "model_identifier": "",
                        "submission_semantics": (
                            PROMPT_NORMALIZATION_LOCAL_SUBMISSION_SEMANTICS
                        ),
                        "usage": {},
                        "warnings": local_warnings,
                    },
                }
                locked.heartbeat_at = timezone.now()
                locked.save(update_fields=("heartbeat_at", "updated_at"))
            elif execution_kind == PROMPT_NORMALIZATION_EXECUTION_PROVIDER:
                submission_boundary = timezone.now()
                locked.status = GenerationAttempt.Status.SUBMITTED
                locked.submission_started_at = (
                    locked.submission_started_at or submission_boundary
                )
                locked.submitted_at = locked.submitted_at or submission_boundary
                locked.heartbeat_at = submission_boundary
                locked.save(update_fields=(
                    "status", "submission_started_at", "submitted_at",
                    "heartbeat_at", "updated_at",
                ))
            else:
                raise ValidationError("prompt.normalize execution_kind 无效")
    except Exception as exc:
        _fail_attempt(
            attempt_id,
            claim_token,
            code=str(getattr(exc, "code", None) or "text_attempt_pre_submit_validation_failed"),
            message=str(exc),
            uncertain=False,
            release_committed_cost=True,
        )
        return GenerationAttempt.objects.get(pk=attempt_id)

    if execution_kind == PROMPT_NORMALIZATION_EXECUTION_PROVIDER:
        try:
            response = invoke_prompt_normalization(
                deployment_id=locked.model_deployment_id,
                payload=payload,
                execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
                generation_attempt=locked,
                execution_deployment=deployment,
            )
        except Exception as exc:
            known_driver_failure = (
                isinstance(exc, ProviderDriverError)
                and not exc.submission_uncertain
            )
            code = str(getattr(exc, "code", "provider_unclassified_failure"))
            known_response_invalid = known_driver_failure and code in {
                "provider_response_invalid",
                "prompt_normalize_contract_failed",
            }
            normalized_code = (
                code
                if code in {
                    "provider_request_rejected",
                    "provider_response_invalid",
                    "prompt_normalize_contract_failed",
                }
                else "text_attempt_pre_submit_validation_failed"
                if known_driver_failure
                else code
            )
            _fail_attempt(
                attempt_id,
                claim_token,
                code=normalized_code,
                message=str(exc),
                uncertain=not known_driver_failure,
                record_committed_cost=known_response_invalid,
                release_committed_cost=known_driver_failure and not known_response_invalid,
                failure_metadata=(
                    exc.metadata
                    if isinstance(exc, ProviderDriverError)
                    and isinstance(getattr(exc, "metadata", None), dict)
                    else None
                ),
            )
            return GenerationAttempt.objects.get(pk=attempt_id)

    try:
        response_items = response.get("items")
        if not isinstance(response_items, list):
            raise ValidationError("prompt.normalize 响应缺少 items")
        by_request_id = {
            str(item.get("request_id")): item
            for item in response_items
            if isinstance(item, dict)
        }
        candidates = []
        for target in targets:
            if not isinstance(target, dict):
                raise ValidationError("prompt.normalize target 必须是对象")
            row = by_request_id.get(str(target.get("request_id") or ""))
            if row is None:
                raise ValidationError("prompt.normalize 响应缺少冻结 request_id")
            translated = restore_prompt_normalization_candidate(
                target=target,
                translated_text=str(row.get("translation") or ""),
                request_payload=payload,
            )
            candidate = {
                "request_id": str(target.get("request_id") or ""),
                "generation_attempt_id": str(attempt_id),
                "batch_index": int(batch_index),
                "scope_type": str(target.get("scope_type") or ""),
                "scope_id": str(target.get("scope_id") or ""),
                "field_key": str(target.get("field_key") or ""),
                "label": str(target.get("label") or ""),
                "source_text": str(target.get("source_text") or ""),
                "source_hash": str(target.get("source_hash") or ""),
                "context_hash": str(target.get("context_hash") or ""),
                "translated_text": translated,
            }
            if target.get("target_kind") == "prompt_profile_field":
                candidate.update({
                    "target_kind": "prompt_profile_field",
                    "shot_id": str(target.get("shot_id") or ""),
                    "profile_selection_sha256": str(
                        target.get("profile_selection_sha256") or ""
                    ),
                    "prompt_profile_version_id": str(
                        target.get("prompt_profile_version_id") or ""
                    ),
                    "profile_field_key": str(target.get("profile_field_key") or ""),
                    "profile_source_selector": str(
                        target.get("profile_source_selector") or ""
                    ),
                    "generation_mode": str(target.get("generation_mode") or ""),
                    "generation_output_semantics": str(
                        target.get("generation_output_semantics") or ""
                    ),
                    "generation_source_selector": str(
                        target.get("generation_source_selector") or ""
                    ),
                    "generation_source_scope_type": str(
                        target.get("generation_source_scope_type") or ""
                    ),
                    "generation_source_scope_id": str(
                        target.get("generation_source_scope_id") or ""
                    ),
                    "generation_source_field_key": str(
                        target.get("generation_source_field_key") or ""
                    ),
                })
            candidates.append(candidate)
        metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
        result = {
            "schema_version": 1,
            "kind": "prompt_normalization_batch_result",
            "attempt_id": str(attempt_id),
            "batch_index": int(batch_index),
            "batch_count": len(batches),
            "unit_key": str(frozen_batch["unit_key"]),
            "generation_mode": str(frozen_batch["generation_mode"]),
            "execution_kind": execution_kind,
            "request_ids": list(frozen_batch["request_ids"]),
            "batch_plan_sha256": str(batch_plan["batch_plan_sha256"]),
            "provider": str(metadata.get("provider") or response.get("provider") or "")[:80],
            "model": str(metadata.get("model_identifier") or response.get("model") or "")[:200],
            "submission_semantics": str(
                metadata.get("submission_semantics")
                or snapshot.get("submission_semantics")
                or ""
            ),
            "usage": (
                deepcopy(metadata.get("usage"))
                if isinstance(metadata.get("usage"), dict)
                else {}
            ),
            "warnings": (
                deepcopy(metadata.get("warnings"))
                if isinstance(metadata.get("warnings"), list)
                else []
            ),
            "candidates": candidates,
        }
        result_contract = json_contract(result, max_bytes=_text_snapshot_max_bytes())
    except Exception as exc:
        _fail_attempt(
            attempt_id,
            claim_token,
            code=(
                "server_deterministic_materialization_failed"
                if execution_kind
                == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
                else "prompt_normalize_contract_failed"
            ),
            message=str(exc),
            uncertain=False,
            record_committed_cost=(
                execution_kind == PROMPT_NORMALIZATION_EXECUTION_PROVIDER
            ),
            release_committed_cost=(
                execution_kind
                == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
            ),
        )
        return GenerationAttempt.objects.get(pk=attempt_id)

    with transaction.atomic():
        locked = GenerationAttempt.objects.select_for_update().select_related(
            "stage_run__production_run",
        ).get(pk=attempt_id)
        expected_status = (
            GenerationAttempt.Status.RUNNING
            if execution_kind == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
            else GenerationAttempt.Status.SUBMITTED
        )
        if locked.status != expected_status or locked.claim_token != claim_token:
            return locked
        stage = locked.stage_run
        run = stage.production_run
        provider_request_id = str(response.get("request_id") or "")[:191]
        provider_request_id_sha256 = (
            hashlib.sha256(provider_request_id.encode("utf-8")).hexdigest()
            if provider_request_id else ""
        )
        if (
            run.status in TERMINAL_RUN_STATUSES
            or stage.status == StageRun.Status.CANCELLED
            or locked.ignore_late_result
        ):
            locked.provider_request_id = provider_request_id
            locked.result_sha256 = result_contract.sha256
            if locked.actual_cost is None and locked.committed_cost is not None:
                reserved = locked.committed_cost
                locked.actual_cost = reserved
                locked.committed_cost = Decimal("0")
                run.actual_cost_total += reserved
                run.committed_cost_total = max(
                    Decimal("0"), run.committed_cost_total - reserved,
                )
                run.save(update_fields=(
                    "actual_cost_total", "committed_cost_total", "updated_at",
                ))
            locked.status = GenerationAttempt.Status.CANCELLED
            locked.ignore_late_result = True
            locked.error_code = "late_result_ignored"
            locked.error_message = "ProductionRun 已终态；晚到规范化候选未写入正式翻译。"
            locked.finished_at = timezone.now()
            locked.heartbeat_at = locked.finished_at
            locked.save(update_fields=(
                "provider_request_id", "result_sha256", "actual_cost", "committed_cost",
                "status", "ignore_late_result", "error_code", "error_message",
                "finished_at", "heartbeat_at", "updated_at",
            ))
            append_attempt_resolution(
                locked,
                outcome=GenerationAttemptResolution.Outcome.LATE_RESULT_IGNORED,
                evidence={
                    "schema_version": 1,
                    "attempt_id": str(locked.id),
                    "provider_request_id_sha256": provider_request_id_sha256,
                    "result_sha256": locked.result_sha256,
                    "actual_cost": (
                        str(locked.actual_cost) if locked.actual_cost is not None else None
                    ),
                    "actual_cost_source": "committed_cost",
                },
                note="Validated late prompt normalization result ignored after run termination",
            )
            return locked

        reserved = locked.committed_cost or Decimal("0")
        run.actual_cost_total += reserved
        run.committed_cost_total = max(
            Decimal("0"), run.committed_cost_total - reserved,
        )
        run.save(update_fields=(
            "actual_cost_total", "committed_cost_total", "updated_at",
        ))
        locked.provider_request_id = provider_request_id
        locked.result_sha256 = result_contract.sha256
        locked.actual_cost = reserved
        locked.committed_cost = Decimal("0")
        locked.status = GenerationAttempt.Status.SUCCEEDED
        locked.error_code = ""
        locked.error_message = ""
        locked.finished_at = timezone.now()
        locked.heartbeat_at = locked.finished_at
        locked.save(update_fields=(
            "provider_request_id", "result_sha256", "actual_cost", "committed_cost",
            "status", "error_code", "error_message", "finished_at", "heartbeat_at",
            "updated_at",
        ))
        review_payload = _prompt_normalization_progress_snapshot(
            stage=stage,
            batch_plan=batch_plan,
            result=result,
            result_sha256=locked.result_sha256,
            provider_request_id_sha256=provider_request_id_sha256,
        )
        contract = json_contract(review_payload, max_bytes=_text_snapshot_max_bytes())
        stage.output_snapshot = contract.value
        stage.output_snapshot_schema_version = contract.schema_version
        stage.output_snapshot_size_bytes = contract.size_bytes
        stage.output_snapshot_sha256 = contract.sha256
        stage.save(update_fields=(
            "output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
            "output_snapshot_sha256", "updated_at",
        ))
    from studio.services.production_runs import advance_production_run
    advance_production_run(run)
    return GenerationAttempt.objects.get(pk=attempt_id)


def process_text_attempt(attempt: GenerationAttempt) -> GenerationAttempt:
    if attempt.operation == TEXT_OPERATION:
        return _process_script_attempt(attempt)
    if attempt.operation == SCRIPT_STRUCTURE_OPERATION:
        return _process_script_structure_attempt(attempt)
    if attempt.operation == VISUAL_RULES_OPERATION:
        return _process_visual_rules_attempt(attempt)
    if attempt.operation == PROMPT_NORMALIZE_OPERATION:
        return _process_prompt_normalization_attempt(attempt)
    raise ValidationError("text worker 收到不支持的 operation")
