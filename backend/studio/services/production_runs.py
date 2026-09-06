from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    ApprovalDecision,
    ArtifactSelectionDecision,
    Asset,
    CharacterBible,
    ConfigDefinition,
    ConfigRevision,
    Episode,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationAttemptResolution,
    GenerationTask,
    HostCapabilitySnapshot,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    ProviderProfile,
    ProductionAssemblyLink,
    ProductionRecipeVersion,
    ProductionRun,
    PromptArtifact,
    PromptFieldTranslation,
    PromptProfileBinding,
    PropBible,
    ReleaseBundle,
    SceneBible,
    ScriptRevision,
    SeriesProject,
    Shot,
    StageRun,
    StageRouteEvent,
    Style,
    WorkflowTemplateVersion,
)
from studio.services.assembly import AssemblyError, create_assembly
from studio.services.configuration import (
    ConfigurationResolutionError,
    create_config_revision,
    project_creation_policy,
    publish_config_revision,
    redact_sensitive_bindings,
    resolve_configuration,
    resolve_configuration_from_snapshot,
    validate_config_override_values,
    video_shot_duration_policy,
)
from studio.services.content_contracts import canonical_name_policy
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.prompt_normalization_contract import (
    normalization_batch_max_items,
    normalization_text_max_characters,
)
from studio.services.generated_artifacts import (
    record_assembly_artifact,
    record_generation_task_artifacts,
    select_artifact as select_generated_artifact,
    verified_local_media_facts,
)
from studio.services.json_contracts import canonical_json_bytes, json_contract, verify_json_contract
from studio.services.model_profile_contracts import (
    verified_frozen_model_profile_specification,
    verified_model_profile_specification,
)
from studio.services.media_capabilities import (
    conditioning_contract_from_frozen_route,
    conditioning_mode_contract,
    normalize_model_media_constraints,
    resolve_conditioning_contract,
    validate_media_execution_settings,
)
from studio.services.execution_monitoring import (
    execution_monitoring_contract_sha256,
    resolve_execution_monitoring_contract,
    verify_frozen_execution_monitoring_contract,
)
from studio.services.provenance import ProvenanceError, verified_task_manifest
from studio.services.provider_registry import (
    ProviderDriverError,
    credential_binding_sha256,
    deployment_supports_script_generation,
    driver_for,
    validate_deployment_execution_contract,
    validate_endpoint,
)
from studio.services.release_bundles import (
    ReleaseBundleError,
    _artifact_binds_shot,
    _managed_generated_artifact_facts,
    create_release_draft,
    seal_release_bundle,
    verified_release_bundle,
)
from studio.services.runtime_platform import (
    host_resolution_and_concurrency,
    verified_artifact_availability,
    verify_host_snapshot,
)
from studio.services.distribution import DistributionError, prepare_task_distribution
from studio.services.production_pipeline import (
    capture_queue_capacity_advisory,
    clone_generation_task_for_retry,
    compile_bible_image_prompt_input,
    compile_prompt_input,
    materialize_compiled_prompt,
    materialize_compiled_workflow_version,
    materialize_generation_task,
)
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    build_frozen_workflow_runtime_overrides,
    compile_frozen_workflow,
)
from studio.services.workflow_versions import workflow_version_execution_snapshot
from studio.services.script_breakdown import (
    breakdown_script,
    normalize_default_conditioning_mode,
)
from studio.services.consistency import sync_consistency_locks
from studio.services.visual_reference_policy import (
    visual_reference_characters,
)
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.services.prompt_profiles import (
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    PromptProfileResolutionError,
    assemble_prompt_profile,
    bible_image_profile_sources,
    compatible_prompt_profile_bindings,
    frozen_profile_reference,
    load_frozen_prompt_profile_selection,
    model_family_for_profile,
    prompt_profile_contract_payload,
    require_explicit_prompt_profile_binding,
    require_prompt_profile_binding,
    text_profile_sources,
    validate_frozen_prompt_profile_binding_reference,
    verify_frozen_profile_assembly,
    verify_frozen_profile_reference,
)
from studio.services.registry_reference_lock import (
    registry_reference_write_guard,
    registry_reference_writer,
)
from studio.services.bible_reference_contract import (
    BIBLE_IMAGE_GENERATION_PURPOSE,
    BIBLE_IMAGE_GENERATION_STAGE_KEY,
    BIBLE_IMAGE_REVIEW_STAGE_KEY,
    bible_candidate_unit_key,
    parse_bible_candidate_unit_key,
    validate_bible_prompt_target,
)
from studio.services.reference_bindings import (
    ReferenceBindingError,
    max_structural_media_bindings,
    normalize_shot_reference_bindings,
    normalize_reference_identifier,
    validate_dynamic_reference_bindings_against_mode_contract,
    validate_reference_bindings_against_mode_contract,
)
from studio.services.asset_visibility import is_workflow_smoke_fixture_asset
from studio.services.prompt_normalization import (
    ROUTE_CONTEXT_DURABLE_ATTEMPT,
    SUBMISSION_SEMANTICS as PROMPT_NORMALIZATION_SUBMISSION_SEMANTICS,
    route_state as prompt_normalization_route_state,
    validate_prompt_normalization_model_profile,
)
from studio.services.prompt_normalization_contract import (
    FIELD_GENERATION_TARGET_CONTRACT_KIND,
    FIELD_GENERATION_TARGET_CONTRACT_SCHEMA_VERSION,
    MODEL_PROCESSING_MODES,
    PROMPT_NORMALIZE_OPERATION,
    PromptNormalizationContractError,
    project_prompt_normalization_request_contract,
    prompt_normalization_chat_messages_input_bytes,
    prompt_normalization_chat_payload,
    safe_prompt_field_validation_context,
    safe_prompt_field_validation_reason_code,
    safe_prompt_normalization_retry_feedback,
    target_generation_rule_for_request,
    validate_target_contribution_text,
    validate_prompt_normalization_request_contract,
    validate_prompt_normalization_run_contract,
)
from studio.services.prompt_translation import (
    ALLOWED_FIELD_KEYS,
    PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC,
    PROMPT_NORMALIZATION_EXECUTION_PROVIDER,
    PROMPT_NORMALIZATION_LOCAL_SUBMISSION_SEMANTICS,
    TranslationValidationError,
    confirm_prompt_normalization_candidates,
    deterministic_prompt_normalization_materialization,
    prepare_prompt_normalization_item,
    resolve_current_source,
    source_text_hash,
)
from studio.services.script_bible_materialization import (
    apply_frozen_script_bible,
    freeze_script_bible_candidates,
)
from studio.services.script_generation_contract import (
    validate_script_generation_model_profile,
)
from studio.services.script_review import approve_frozen_script_candidate
from studio.services.production_visual_rules import (
    VISUAL_RULES_OPERATION,
    VISUAL_RULES_UNIT_KEY,
    VisualRulesConflict,
    apply_visual_rules_candidate,
    build_visual_rules_plan,
    current_rules_snapshot,
    current_visual_context_snapshot,
    validate_visual_rules_model_profile,
    verify_visual_rules_smoke,
    visual_rules_provider_payload,
    visual_rules_resource_estimate,
)
from studio.services.script_structure import (
    SCRIPT_STRUCTURE_OPERATION,
    SCRIPT_STRUCTURE_UNIT_KEY,
    build_script_structure_plan,
    canonical_no_submission_review_payload,
    intersect_script_structure_duration_policy,
    is_canonical_script,
    parse_canonical_script,
    structure_provider_payload,
    validate_script_structure_model_profile,
    verify_script_structure_smoke,
)
from studio.services.series_concept_drafts import build_series_concept_plan
from studio.services.text_generation_queue import (
    SAFE_PROMPT_RESPONSE_CLASSIFIERS,
    TEXT_OPERATION,
    TEXT_OPERATIONS,
    TEXT_UNIT_KEY,
    PROMPT_NORMALIZE_UNIT_KEY,
    append_attempt_resolution,
    frozen_text_deployment,
    provider_payload as text_provider_payload,
)
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache
from studio.services.async_media_contract import (
    ASYNC_MEDIA_OPERATIONS,
    async_operation_contract,
    fresh_smoke_snapshot,
    frozen_async_deployment,
    validate_async_model_profile,
    validate_standard_payload,
)


SUPPORTED_STAGE_MODES = frozenset({
    "confirmed_revision",
    "review",
    "deterministic",
    "existing_pipeline",
    "manual",
    "skip",
    "ai",
})
TERMINAL_RUN_STATUSES = frozenset({
    ProductionRun.Status.SUCCEEDED,
    ProductionRun.Status.FAILED,
    ProductionRun.Status.CANCELLED,
})
logger = logging.getLogger(__name__)
ROUTED_STAGE_KEYS = frozenset({
    "media_generation",
    BIBLE_IMAGE_GENERATION_STAGE_KEY,
    "script_generate",
    "script_structure",
    "visual_rules_generate",
    "script_normalize",
})
ROUTING_MODES = frozenset({"fixed", "ordered_fallback", "manual_choice"})
PROMPT_FIELD_RETRY_LIMIT_CONFIG_KEY = "production.prompt_field_retry_limit"
TEXT_CONTRACT_RETRY_LIMIT_CONFIG_KEY = "production.text_contract_retry_limit"
TEXT_CONTRACT_HUMAN_CONFIRMATION_REQUIRED = (
    "text_contract_human_confirmation_required"
)
TEXT_CONTRACT_AUTO_RETRY_CLASSIFIERS = frozenset({
    "response_missing_json_text",
    "response_missing_text",
    "response_not_strict_json",
    "response_output_budget_exhausted",
    "response_reasoning_without_text",
    "response_root_invalid",
    "response_tool_call_without_text",
})
PROMPT_VALIDATION_RETRYABLE_CODES = frozenset({
    "prompt_normalize_contract_failed",
    "provider_response_invalid",
})
PROMPT_VALIDATION_REVIEW_REQUIRED = "prompt_validation_review_required"
PROMPT_VALIDATION_STOP_RETRY_LIMIT = "retry_limit_reached"
PROMPT_VALIDATION_STOP_SAFE_FEEDBACK_UNAVAILABLE = "safe_feedback_unavailable"
PROMPT_VALIDATION_STOP_REASONS = frozenset({
    PROMPT_VALIDATION_STOP_RETRY_LIMIT,
    PROMPT_VALIDATION_STOP_SAFE_FEEDBACK_UNAVAILABLE,
})
PROMPT_NORMALIZATION_BATCH_PLAN_SCHEMA_VERSION = 3


def _create_generation_attempt(**values) -> GenerationAttempt:
    """Create a formal attempt while protecting its non-FK binding reference."""

    with registry_reference_write_guard():
        validate_frozen_prompt_profile_binding_reference(
            binding_key=str(values.get("prompt_profile_binding_key") or ""),
            prompt_profile_version=values.get("prompt_profile_version"),
            contract_sha256=str(
                values.get("prompt_profile_contract_sha256") or ""
            ),
        )
        return GenerationAttempt.objects.create(**values)


def _routing_routes_max_items() -> int:
    return boundary_int("security.production.max_routing_routes")


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _workflow_graph_max_bytes() -> int:
    return boundary_int("security.workflow.max_graph_bytes")


def _text_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.text_max_bytes")


def _text_provider_request_max_bytes() -> int:
    return boundary_int("security.snapshot.text_provider_request_max_bytes")


def _automatic_retry_max_items() -> int:
    return boundary_int("security.production.max_automatic_retries")


def _variant_max_items() -> int:
    return boundary_int("security.production.max_variants")


def _bible_candidate_max_items() -> int:
    return boundary_int("security.production.max_bible_candidates_per_entity")


def _bible_reference_asset_max_items() -> int:
    return boundary_int(
        "security.production.max_bible_reference_assets_per_entity"
    )


def _bible_regeneration_entity_max_items() -> int:
    return boundary_int(
        "security.production.max_bible_regeneration_entities_per_request"
    )


def _episode_max_items() -> int:
    return boundary_int("security.production.max_episodes")


def _project_creation_default_episode_count(values: dict[str, Any]) -> int:
    """Resolve the one creation-policy default used by ProductionRun input."""

    policy = project_creation_policy(values=values)
    value = policy["default_episode_count"]
    maximum = _episode_max_items()
    if type(value) is not int or not 1 <= value <= maximum:
        raise ConfigurationResolutionError(
            "项目创建策略的 default_episode_count 必须在当前安全范围内",
            code="project_creation_policy_invalid",
            missing_keys=("production.project_creation_policy",),
        )
    return value


def _frozen_input_episode_count(input_snapshot: Any) -> int:
    """Read the explicit business input without reviving a historical default."""

    value = input_snapshot.get("episode_count") if isinstance(input_snapshot, dict) else None
    if type(value) is not int or value < 1:
        raise StageBlocked(
            "production_run_input_snapshot_invalid",
            "ProductionRun 冻结输入缺少有效 episode_count；不会回退到历史默认值。",
        )
    return value


def _shots_per_episode_max_items() -> int:
    return boundary_int("security.production.max_shots_per_episode")


def _total_shot_max_items() -> int:
    return boundary_int("security.production.max_total_shots")


def _generation_task_absolute_limit() -> int:
    return boundary_int("security.production.max_generation_tasks")


def _effective_generation_task_limit(run: ProductionRun) -> int:
    """Apply the current superuser-owned ceiling to old and new runs alike."""

    return min(int(run.max_generation_tasks), _generation_task_absolute_limit())
STRUCTURED_SHOT_DURATION_CONFIG_KEYS = (
    "production.default_shot_duration_ms",
    "production.video_shot_min_duration_ms",
    "production.video_shot_max_duration_ms",
)
EXECUTABLE_STAGE_MODES = frozenset({
    ("script_input", "confirmed_revision"),
    ("script_generate", "ai"),
    ("script_structure", "ai"),
    ("script_review", "review"),
    ("visual_rules_generate", "ai"),
    ("visual_rules_review", "review"),
    ("bible_materialize", "deterministic"),
    ("bible_review", "review"),
    (BIBLE_IMAGE_GENERATION_STAGE_KEY, "existing_pipeline"),
    (BIBLE_IMAGE_REVIEW_STAGE_KEY, "review"),
    ("storyboard", "deterministic"),
    ("storyboard_review", "review"),
    ("script_normalize", "ai"),
    ("script_normalize", "manual"),
    ("script_normalize_review", "review"),
    ("prompt_compile", "existing_pipeline"),
    ("media_generation", "existing_pipeline"),
    ("artifact_selection", "review"),
    ("assembly", "existing_pipeline"),
    ("delivery", "existing_pipeline"),
    ("script_input", "manual"),
    ("bible_review", "manual"),
    ("storyboard", "manual"),
    ("storyboard_review", "manual"),
})

# One server-authored catalog drives both recipe validation and the structured
# settings form.  The browser may localize labels, but it must not invent stage
# keys, modes, approval gates, routing modes, or schema defaults.
RECIPE_STAGE_ORDER = (
    "script_input", "script_generate", "script_structure", "script_review",
    "bible_materialize", "bible_review", "visual_rules_generate",
    "visual_rules_review", "storyboard", "script_normalize",
    "script_normalize_review", BIBLE_IMAGE_GENERATION_STAGE_KEY,
    BIBLE_IMAGE_REVIEW_STAGE_KEY, "storyboard_review", "prompt_compile",
    "media_generation", "artifact_selection", "assembly", "delivery",
)
RECIPE_EDITOR_DEFINITION_SCHEMA_VERSION = 1
RECIPE_APPROVAL_OPTIONS = ("auto", "required", "on_warning")


def _recipe_editor_stage(
    key: str,
    mode: str,
    approval: str,
) -> dict[str, Any]:
    stage: dict[str, Any] = {"key": key, "mode": mode, "approval": approval}
    if key in ROUTED_STAGE_KEYS and not (key == "script_normalize" and mode == "manual"):
        stage["routing"] = {
            "mode": "fixed", "variant_count": 1, "routes": [], "evaluator": None,
        }
    return stage


def _recipe_editor_preset(script_mode: str) -> list[dict[str, Any]]:
    script = (
        [_recipe_editor_stage("script_generate", "ai", "auto")]
        if script_mode == "ai"
        else [
            _recipe_editor_stage("script_input", "confirmed_revision", "auto"),
            _recipe_editor_stage("script_structure", "ai", "auto"),
        ]
    )
    return [
        *script,
        _recipe_editor_stage("script_review", "review", "required"),
        _recipe_editor_stage("bible_materialize", "deterministic", "auto"),
        _recipe_editor_stage("bible_review", "review", "required"),
        _recipe_editor_stage("visual_rules_generate", "ai", "auto"),
        _recipe_editor_stage("visual_rules_review", "review", "required"),
        _recipe_editor_stage("storyboard", "deterministic", "auto"),
        _recipe_editor_stage("script_normalize", "ai", "auto"),
        _recipe_editor_stage("script_normalize_review", "review", "required"),
        _recipe_editor_stage(BIBLE_IMAGE_GENERATION_STAGE_KEY, "existing_pipeline", "auto"),
        _recipe_editor_stage(BIBLE_IMAGE_REVIEW_STAGE_KEY, "review", "required"),
        _recipe_editor_stage("storyboard_review", "review", "required"),
        _recipe_editor_stage("prompt_compile", "existing_pipeline", "auto"),
        _recipe_editor_stage("media_generation", "existing_pipeline", "auto"),
        _recipe_editor_stage("artifact_selection", "review", "required"),
        _recipe_editor_stage("assembly", "existing_pipeline", "auto"),
        _recipe_editor_stage("delivery", "existing_pipeline", "auto"),
    ]


def production_recipe_editor_options() -> dict[str, Any]:
    modes_by_stage = {
        key: [mode for stage_key, mode in sorted(EXECUTABLE_STAGE_MODES) if stage_key == key]
        for key in RECIPE_STAGE_ORDER
    }
    return {
        "definition_schema_version": RECIPE_EDITOR_DEFINITION_SCHEMA_VERSION,
        "stage_options": [
            {
                "value": key,
                "modes": modes_by_stage[key],
                "requires_routing": key in ROUTED_STAGE_KEYS,
            }
            for key in RECIPE_STAGE_ORDER
        ],
        "approval_options": [
            {"value": value, "label": value}
            for value in RECIPE_APPROVAL_OPTIONS
        ],
        "routing_mode_options": [
            {"value": value, "label": value}
            for value in sorted(ROUTING_MODES)
        ],
        "presets": [
            {
                "value": "ai_draft",
                "label": "人工智能起稿链路",
                "stages": _recipe_editor_preset("ai"),
            },
            {
                "value": "confirmed_script",
                "label": "已确认剧本链路",
                "stages": _recipe_editor_preset("confirmed"),
            },
        ],
    }

UNATTENDED_INTERACTION_MODE = "unattended"
UNATTENDED_DECISION_POLICY_REGISTRY = {
    "script_review": "approve_single_frozen_script_v1",
    "visual_rules_review": "accept_single_frozen_visual_rules_v1",
    "bible_review": "approve_frozen_bible_v1",
    "script_normalize_review": "accept_all_valid_frozen_prompt_translations_v2",
    BIBLE_IMAGE_REVIEW_STAGE_KEY: "select_single_bible_candidate_per_entity_v1",
    "storyboard_review": "bind_frozen_reference_inputs_by_contract_v2",
    "artifact_selection": "accept_single_artifact_per_group_v1",
}
UNATTENDED_STAGE_CONTRACTS = (
    ("script_generate", "ai"),
    ("script_review", "review"),
    ("bible_materialize", "deterministic"),
    ("bible_review", "review"),
    ("storyboard", "deterministic"),
    ("script_normalize", "ai"),
    ("script_normalize_review", "review"),
    (BIBLE_IMAGE_GENERATION_STAGE_KEY, "existing_pipeline"),
    (BIBLE_IMAGE_REVIEW_STAGE_KEY, "review"),
    ("storyboard_review", "review"),
    ("prompt_compile", "existing_pipeline"),
    ("media_generation", "existing_pipeline"),
    ("artifact_selection", "review"),
    ("assembly", "existing_pipeline"),
    ("delivery", "existing_pipeline"),
)
UNATTENDED_VISUAL_RULES_STAGE_CONTRACTS = (
    ("script_generate", "ai"),
    ("script_review", "review"),
    ("bible_materialize", "deterministic"),
    ("bible_review", "review"),
    ("visual_rules_generate", "ai"),
    ("visual_rules_review", "review"),
    ("storyboard", "deterministic"),
    ("script_normalize", "ai"),
    ("script_normalize_review", "review"),
    (BIBLE_IMAGE_GENERATION_STAGE_KEY, "existing_pipeline"),
    (BIBLE_IMAGE_REVIEW_STAGE_KEY, "review"),
    ("storyboard_review", "review"),
    ("prompt_compile", "existing_pipeline"),
    ("media_generation", "existing_pipeline"),
    ("artifact_selection", "review"),
    ("assembly", "existing_pipeline"),
    ("delivery", "existing_pipeline"),
)
_UNATTENDED_IDENTITY_FIELDS = {
    PromptFieldTranslation.ScopeType.CHARACTER: (
        "character.name", "character.visual_identity", "Cast",
    ),
    PromptFieldTranslation.ScopeType.SCENE: (
        "scene.name", "scene.visual_identity", "Location",
    ),
    PromptFieldTranslation.ScopeType.PROP: (
        "prop.name", "prop.visual_identity", "Object",
    ),
}
_UNATTENDED_CANONICAL_WORD_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:['-][A-Za-z0-9]+)*",
)
_UNATTENDED_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_UNATTENDED_PROVISIONAL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"S[1-9][0-9]*|"
    r"scene:[A-Za-z0-9][A-Za-z0-9_-]*|"
    r"prop:[A-Za-z0-9][A-Za-z0-9_-]*"
    r")(?![A-Za-z0-9_-])",
    flags=re.IGNORECASE,
)
_UNATTENDED_CANONICAL_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "at", "by", "for", "from", "has", "have",
    "in", "is", "of", "on", "or", "the", "to", "under", "with", "wearing",
    "wears", "holding", "holds", "featuring", "features", "standing", "stands",
    "character", "scene", "prop",
})


class ProductionRunConflict(ValidationError):
    def __init__(self, message, *, code: str = "production_run_conflict"):
        self.code = str(code)
        super().__init__(message)


class ProductionRunCreateRejected(ValidationError):
    """Stable 400 preflight error emitted before any run/audit row exists."""

    def __init__(self, message, *, code: str):
        self.code = str(code)
        super().__init__(message)


class ProductionRunRetryConflict(ValidationError):
    pass


class StageBlocked(RuntimeError):
    def __init__(self, code: str, reason: str, *, awaiting_approval: bool = False):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.awaiting_approval = awaiting_approval


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _non_secret_config(value: Any) -> Any:
    """Freeze operational configuration while excluding credential-shaped values."""

    if isinstance(value, list):
        return [_non_secret_config(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        normalized = key.casefold().replace("-", "_")
        sensitive = (
            normalized in {"secret", "token", "password", "api_key", "authorization", "credential"}
            or normalized.endswith((
                "_secret", "_token", "_password", "_api_key", "_authorization",
                "_credential", "_credential_ref",
            ))
        )
        if sensitive:
            continue
        result[key] = _non_secret_config(item)
    return result


def _snapshot_config_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    # Run-scoped secret definitions are rejected by configuration validation.
    # Keep the idempotency payload free of credential-shaped nested input too.
    return _non_secret_config(overrides)


def _endpoint_is_external(deployment: ModelDeployment) -> bool:
    config = {
        **(deployment.profile_version.provider.driver_config or {}),
        **(deployment.runtime_config or {}),
    }
    endpoint = str(config.get("base_url") or "").strip()
    hostname = (urlparse(endpoint).hostname or "").casefold()
    return bool(hostname and hostname not in {"localhost", "127.0.0.1", "::1"})


def _unconfigured_routing() -> dict[str, Any]:
    # This is a closed, non-executable protocol shape. Configured routes must
    # declare their business variant count before they can be frozen.
    return {
        "schema_version": 1,
        "mode": "unconfigured",
        "routes": [],
        "variant_count": 1,
        "evaluator": None,
    }


def _frozen_route_timeout_seconds(
    deployment_config: dict[str, Any],
    provider_config: dict[str, Any],
) -> float:
    """Read the explicit, versioned transport timeout for a frozen route."""

    if "timeout_seconds" in deployment_config:
        raw = deployment_config["timeout_seconds"]
    elif "timeout_seconds" in provider_config:
        raw = provider_config["timeout_seconds"]
    else:
        raise ValidationError("route 缺少显式 timeout_seconds；不会使用代码默认值")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValidationError("route timeout_seconds 必须是有限正数")
    timeout = float(raw)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValidationError("route timeout_seconds 必须是有限正数")
    return timeout


def _validate_bible_text_to_image_workflow_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, dict):
        raise ValidationError("Bible 参考图 ComfyUI route 缺少冻结 workflow contract")
    bindings = snapshot.get("semantic_bindings")
    contract = snapshot.get("input_contract")
    parameters = contract.get("parameters") if isinstance(contract, dict) else None
    if not isinstance(bindings, list) or not isinstance(parameters, dict):
        raise ValidationError("Bible 参考图 workflow 缺少 semantic bindings/input contract")
    keys = {
        str(item.get("semantic_key") or "")
        for item in bindings
        if isinstance(item, dict)
    }
    asset_keys = sorted(key for key in keys if key.startswith("asset."))
    if asset_keys:
        raise ValidationError(
            "Bible 参考图必须使用纯文生图 workflow，禁止 asset semantic: "
            + ", ".join(asset_keys),
        )
    required = {
        "prompt.positive", "prompt.negative", "image.width", "image.height",
        "sampling.seed", "output.prefix",
    }
    missing = sorted(required - keys)
    if missing:
        raise ValidationError(
            "Bible 参考图纯文生图 workflow 缺少可物化 semantic: "
            + ", ".join(missing),
        )
    invalid_definitions = sorted(
        key for key in required if not isinstance(parameters.get(key), dict)
    )
    if invalid_definitions:
        raise ValidationError(
            "Bible 参考图 workflow semantic 缺少参数 Schema: "
            + ", ".join(invalid_definitions),
        )


def _freeze_stage_routing(raw: object, *, stage_key: str) -> dict[str, Any]:
    if raw is None:
        return _unconfigured_routing()
    if not isinstance(raw, dict):
        raise ValidationError({"routing": f"阶段 {stage_key} routing 必须是对象"})
    unknown = sorted(set(raw) - {"mode", "routes", "evaluator", "variant_count"})
    if unknown:
        raise ValidationError({"routing": f"阶段 {stage_key} routing 含未知字段: {', '.join(unknown)}"})
    if "variant_count" not in raw:
        raise ValidationError({"routing": f"阶段 {stage_key} 必须显式声明 variant_count"})
    mode = str(raw.get("mode") or "").strip()
    routes = raw.get("routes")
    evaluator = raw.get("evaluator")
    variant_count = raw["variant_count"]
    if mode not in ROUTING_MODES:
        raise ValidationError({"routing": f"阶段 {stage_key} routing.mode 无效"})
    max_routes = _routing_routes_max_items()
    if not isinstance(routes, list) or not 1 <= len(routes) <= max_routes:
        raise ValidationError({"routing": f"阶段 {stage_key} routes 必须为 1 到 {max_routes} 项"})
    if mode == "fixed" and len(routes) != 1:
        raise ValidationError({"routing": f"阶段 {stage_key} fixed 必须且只能声明一个 route"})
    max_variants = _variant_max_items()
    if type(variant_count) is not int or not 1 <= variant_count <= max_variants:
        raise ValidationError({
            "routing": f"阶段 {stage_key} variant_count 必须为 1 到 {max_variants}",
        })
    if evaluator is not None:
        evaluator = str(evaluator).strip()
        if evaluator not in {"first_succeeded"}:
            raise ValidationError({"routing": f"阶段 {stage_key} evaluator 未注册"})
    frozen_routes: list[dict[str, Any]] = []
    for index, item in enumerate(routes):
        if not isinstance(item, dict):
            raise ValidationError({"routing": f"阶段 {stage_key} routes[{index}] 必须是对象"})
        route_unknown = sorted(set(item) - {"workflow_version_id", "model_deployment_id"})
        if route_unknown:
            raise ValidationError({"routing": f"routes[{index}] 含未知字段: {', '.join(route_unknown)}"})
        workflow_id = item.get("workflow_version_id")
        deployment_id = item.get("model_deployment_id")
        if not deployment_id:
            raise ValidationError({"routing": f"routes[{index}] 缺少 model_deployment_id"})
        try:
            deployment = ModelDeployment.objects.select_related(
                "profile_version__provider", "runtime_host",
            ).get(pk=deployment_id, status=ModelDeployment.Status.ACTIVE)
        except (ModelDeployment.DoesNotExist, ValueError) as exc:
            raise ValidationError({"routing": f"routes[{index}] deployment 不存在或未启用"}) from exc
        if not deployment.profile_version.provider.is_active:
            raise ValidationError({"routing": f"routes[{index}] provider 已停用"})
        if deployment.profile_version.status != deployment.profile_version.Status.PUBLISHED:
            raise ValidationError({"routing": f"routes[{index}] model profile 未发布或已停用"})
        workflow = None
        if workflow_id:
            try:
                workflow = WorkflowTemplateVersion.objects.select_related(
                    "template", "validated_deployment",
                ).get(
                    pk=workflow_id,
                    status=WorkflowTemplateVersion.Status.PUBLISHED,
                    is_current=True,
                    smoke_test_state=WorkflowTemplateVersion.ValidationState.PASSED,
                )
            except (WorkflowTemplateVersion.DoesNotExist, ValueError) as exc:
                raise ValidationError({"routing": f"routes[{index}] workflow 未发布或未通过 smoke-test"}) from exc
            if (
                workflow.model_profile_version_id
                and workflow.model_profile_version_id != deployment.profile_version_id
            ):
                raise ValidationError({"routing": f"routes[{index}] workflow 与 deployment 模型不一致"})
            if workflow.validated_deployment_id != deployment.id:
                raise ProductionRunCreateRejected(
                    {
                        "routing": (
                            f"routes[{index}] workflow 的真实 smoke 证据不属于所选 deployment"
                        ),
                    },
                    code="workflow_validated_deployment_mismatch",
                )
            if (
                not workflow.compiler_id
                or workflow.compiler_version <= 0
                or not workflow.semantic_bindings_sha256
                or not workflow.model_requirements_sha256
                or not workflow.execution_contract_sha256
            ):
                raise ValidationError({
                    "routing": f"routes[{index}] workflow 缺少已冻结 semantic execution contract",
                })
        try:
            validate_deployment_execution_contract(deployment)
        except (ProviderDriverError, ValidationError, RuntimeError) as exc:
            raise ProductionRunCreateRejected(
                {"routing": f"routes[{index}] deployment 执行合同无效"},
                code="deployment_execution_contract_invalid",
            ) from exc
        workflow_snapshot = None
        if workflow is not None:
            workflow_snapshot = workflow_version_execution_snapshot(workflow)
        provider_config = _non_secret_config(deployment.profile_version.provider.driver_config or {})
        deployment_config = _non_secret_config(deployment.runtime_config or {})
        model_specification = _non_secret_config(
            verified_model_profile_specification(deployment.profile_version),
        )
        effective_timeout_seconds = _frozen_route_timeout_seconds(
            deployment_config,
            provider_config,
        )
        media_operations: list[str] = []
        smoke_evidence: dict[str, dict[str, Any]] = {}
        submission_semantics = ""
        conditioning_contract: dict[str, Any] | None = None
        media_constraints: dict[str, Any] | None = None
        execution_monitoring: dict[str, Any] | None = None
        if deployment.profile_version.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
            media_operations = validate_async_model_profile(deployment.profile_version)
            for operation in media_operations:
                snapshot = fresh_smoke_snapshot(deployment, operation)
                if snapshot is not None:
                    smoke_evidence[operation] = {
                        "snapshot_id": str(snapshot.id),
                        "target_sha256": snapshot.target_sha256,
                        "evidence_sha256": snapshot.evidence_sha256,
                        "captured_at": snapshot.captured_at.isoformat(),
                        "expires_at": snapshot.expires_at.isoformat() if snapshot.expires_at else None,
                    }
        if stage_key == "script_normalize":
            state, _reason, normalization_route = prompt_normalization_route_state(
                deployment,
                execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
            )
            if normalization_route is not None and normalization_route.smoke is not None:
                snapshot = normalization_route.smoke
                smoke_evidence[PROMPT_NORMALIZE_OPERATION] = {
                    "snapshot_id": str(snapshot.id),
                    "target_sha256": snapshot.target_sha256,
                    "evidence_sha256": snapshot.evidence_sha256,
                    "captured_at": snapshot.captured_at.isoformat(),
                    "expires_at": snapshot.expires_at.isoformat() if snapshot.expires_at else None,
                    "route_state": state,
                }
            submission_semantics = (
                PROMPT_NORMALIZATION_SUBMISSION_SEMANTICS
                if _endpoint_is_external(deployment) else "local_synchronous"
            )
        if stage_key == "script_structure":
            # A structure probe exercises a different strict response contract
            # from script.generate, so only evidence for this exact operation
            # may be frozen into the route.
            snapshot = fresh_smoke_snapshot(deployment, SCRIPT_STRUCTURE_OPERATION)
            if snapshot is not None:
                smoke_evidence[SCRIPT_STRUCTURE_OPERATION] = {
                    "snapshot_id": str(snapshot.id),
                    "target_sha256": snapshot.target_sha256,
                    "evidence_sha256": snapshot.evidence_sha256,
                    "captured_at": snapshot.captured_at.isoformat(),
                    "expires_at": snapshot.expires_at.isoformat() if snapshot.expires_at else None,
                }
        if stage_key == "visual_rules_generate":
            snapshot = fresh_smoke_snapshot(deployment, VISUAL_RULES_OPERATION)
            if snapshot is not None:
                smoke_evidence[VISUAL_RULES_OPERATION] = {
                    "snapshot_id": str(snapshot.id),
                    "target_sha256": snapshot.target_sha256,
                    "evidence_sha256": snapshot.evidence_sha256,
                    "captured_at": snapshot.captured_at.isoformat(),
                    "expires_at": snapshot.expires_at.isoformat() if snapshot.expires_at else None,
                }
        if stage_key in {"media_generation", BIBLE_IMAGE_GENERATION_STAGE_KEY}:
            output_kinds: list[str] = []
            conditioning_mode_keys: list[str] = []
            if workflow is not None:
                output_kinds = [workflow.template.output_kind]
            else:
                if "image.text_to_image" in media_operations:
                    output_kinds.append(Shot.MediaType.IMAGE)
                if "video.text_to_video" in media_operations:
                    output_kinds.append(Shot.MediaType.VIDEO)
            if stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
                if workflow is not None and workflow.template.output_kind != Shot.MediaType.IMAGE:
                    raise ValidationError({
                        "routing": f"routes[{index}] 的 Bible 参考图工作流必须输出 image",
                    })
                if workflow is None and "image.text_to_image" not in media_operations:
                    raise ValidationError({
                        "routing": f"routes[{index}] 的 async 模型缺少 image.text_to_image 能力",
                    })
                output_kinds = [Shot.MediaType.IMAGE]
                conditioning_mode_keys = []
                if workflow_snapshot is not None:
                    _validate_bible_text_to_image_workflow_snapshot(workflow_snapshot)
            route_output_kind = (
                Shot.MediaType.VIDEO
                if Shot.MediaType.VIDEO in output_kinds
                else Shot.MediaType.IMAGE
            )
            conditioning_contract = resolve_conditioning_contract(
                workflow_input_contract=(
                    workflow.input_contract if workflow is not None else None
                ),
                workflow_compiler_id=(
                    workflow.compiler_id if workflow is not None else ""
                ),
                model_specification=model_specification,
                output_kind=route_output_kind,
                required=route_output_kind == Shot.MediaType.VIDEO,
            )
            if route_output_kind == Shot.MediaType.VIDEO:
                conditioning_mode_keys = [
                    str(row["key"])
                    for row in conditioning_contract["modes"]
                ]
            media_constraints = normalize_model_media_constraints(model_specification)
            execution_monitoring = resolve_execution_monitoring_contract(
                model_specification=model_specification,
                workflow_input_contract=(
                    workflow.input_contract if workflow is not None else None
                ),
                workflow_graph=(workflow.graph if workflow is not None else None),
            )
        frozen_routes.append({
            "index": index,
            "workflow_version_id": str(workflow.id) if workflow else None,
            "workflow_sha256": workflow.graph_sha256 if workflow else "",
            "workflow_output_kind": workflow.template.output_kind if workflow else "",
            "workflow_validated_deployment_id": (
                str(workflow.validated_deployment_id) if workflow else None
            ),
            "model_deployment_id": str(deployment.id),
            "runtime_host_id": (
                str(deployment.runtime_host_id) if deployment.runtime_host_id else None
            ),
            "model_profile_version_id": str(deployment.profile_version_id),
            "model_identifier": deployment.profile_version.model_identifier,
            "model_specification_sha256": deployment.profile_version.specification_sha256,
            "model_specification": model_specification,
            "frozen_model_specification_sha256": _sha256(model_specification),
            "conditioning_contract": deepcopy(conditioning_contract),
            "conditioning_contract_sha256": (
                _sha256(conditioning_contract)
                if conditioning_contract is not None else ""
            ),
            "media_constraints": deepcopy(media_constraints),
            "media_constraints_sha256": (
                _sha256(media_constraints) if media_constraints is not None else ""
            ),
            "execution_monitoring": deepcopy(execution_monitoring),
            "execution_monitoring_sha256": (
                execution_monitoring_contract_sha256(execution_monitoring)
                if execution_monitoring is not None else ""
            ),
            "provider_id": str(deployment.profile_version.provider_id),
            "driver_type": deployment.profile_version.provider.driver_type,
            "provider_config_record_sha256": deployment.profile_version.provider.driver_config_sha256,
            "deployment_config_record_sha256": deployment.runtime_config_sha256,
            "credential_binding_sha256": credential_binding_sha256(deployment),
            "is_external": _endpoint_is_external(deployment),
            "workflow_snapshot": workflow_snapshot,
            "provider_config": provider_config,
            "provider_config_sha256": _sha256(provider_config),
            "deployment_config": deployment_config,
            "deployment_config_sha256": _sha256(deployment_config),
            "effective_timeout_seconds": effective_timeout_seconds,
            "media_operations": media_operations,
            "smoke_evidence": smoke_evidence,
            "submission_semantics": submission_semantics,
            "prompt_profile": None,
        })
    return {
        "schema_version": 1,
        "mode": mode,
        "routes": frozen_routes,
        "variant_count": variant_count,
        "evaluator": evaluator,
    }


def _recipe_stages(recipe: ProductionRecipeVersion) -> list[dict[str, Any]]:
    verify_json_contract(
        recipe.definition,
        schema_version=recipe.definition_schema_version,
        size_bytes=recipe.definition_size_bytes,
        sha256=recipe.definition_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    definition = recipe.definition
    if not isinstance(definition, dict) or definition.get("schema_version") not in {1, 2}:
        raise ValidationError("recipe definition schema_version 不受支持")
    schema_version = int(definition["schema_version"])
    unattended = schema_version == 2
    if unattended:
        unknown_definition_fields = sorted(
            set(definition) - {"schema_version", "interaction_mode", "stages"}
        )
        if unknown_definition_fields:
            raise ValidationError(
                "unattended recipe definition 含未知字段: "
                + ", ".join(unknown_definition_fields),
            )
        if definition.get("interaction_mode") != UNATTENDED_INTERACTION_MODE:
            raise ValidationError(
                "recipe schema_version=2 必须显式声明 interaction_mode=unattended",
            )
    raw_stages = definition.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ValidationError("recipe 必须声明非空 stages")
    stages = []
    seen: set[str] = set()
    for ordinal, raw in enumerate(raw_stages, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"recipe stage {ordinal} 必须是对象")
        if unattended:
            unknown_stage_fields = sorted(
                set(raw) - {"key", "mode", "approval", "decision_policy", "routing"}
            )
            if unknown_stage_fields:
                raise ValidationError(
                    f"unattended recipe stage {ordinal} 含未知字段: "
                    + ", ".join(unknown_stage_fields),
                )
        key = str(raw.get("key") or "").strip()
        mode = str(raw.get("mode") or "").strip()
        if unattended and "approval" not in raw:
            raise ValidationError(
                f"unattended recipe stage {ordinal} 必须显式声明 approval",
            )
        approval = str(raw.get("approval") or "auto").strip()
        decision_policy = str(raw.get("decision_policy") or "").strip()
        if not key or key in seen or mode not in SUPPORTED_STAGE_MODES:
            raise ValidationError(f"recipe stage {ordinal} 的 key/mode 无效")
        allowed_approvals = {"auto", "required", "on_warning", "policy"}
        if approval not in allowed_approvals:
            raise ValidationError(f"recipe stage {ordinal} 的 approval 无效")
        routing = _freeze_stage_routing(raw.get("routing"), stage_key=key)
        decision_policy_sha256 = ""
        if unattended:
            if mode in {"manual", "skip"}:
                raise ValidationError("unattended recipe 禁止 manual/skip stage")
            if approval == "required":
                raise ValidationError("unattended recipe 禁止 approval=required")
            if routing.get("mode") == "manual_choice":
                raise ValidationError("unattended recipe 禁止 routing.mode=manual_choice")
            if routing.get("mode") != "unconfigured" and (
                routing.get("mode") != "fixed"
                or len(routing.get("routes") or []) != 1
                or routing["variant_count"] != 1
                or routing.get("evaluator") not in (None, "")
            ):
                raise ValidationError(
                    "unattended recipe 的已配置 routing 必须是单 route、单候选 fixed 合同",
                )
            registered_policy = UNATTENDED_DECISION_POLICY_REGISTRY.get(key)
            if mode == "review":
                if approval != "policy" or not registered_policy:
                    raise ValidationError(
                        f"unattended review stage {key} 必须使用已注册的 approval=policy",
                    )
                if decision_policy != registered_policy:
                    raise ValidationError(
                        f"unattended review stage {key} 的 decision_policy 未注册或不匹配",
                    )
                policy_contract = {
                    "schema_version": 1,
                    "interaction_mode": UNATTENDED_INTERACTION_MODE,
                    "stage_key": key,
                    "decision_policy": decision_policy,
                    "recipe_definition_sha256": recipe.definition_sha256,
                }
                decision_policy_sha256 = _sha256(policy_contract)
            elif approval != "auto" or decision_policy:
                raise ValidationError(
                    f"unattended 非 review stage {key} 必须使用 approval=auto 且不能声明 decision_policy",
                )
        elif approval == "policy" or decision_policy:
            raise ValidationError(
                "recipe schema_version=1 不支持 approval=policy/decision_policy",
            )
        seen.add(key)
        stages.append({
            "key": key,
            "mode": mode,
            "approval": approval,
            "decision_policy": decision_policy,
            "decision_policy_sha256": decision_policy_sha256,
            "routing": routing,
        })
    positions = {item["key"]: index for index, item in enumerate(stages)}
    present = [key for key in RECIPE_STAGE_ORDER if key in positions]
    if present != sorted(present, key=RECIPE_STAGE_ORDER.index):
        raise ValidationError("recipe 主阶段顺序无效")
    if unattended:
        actual_contracts = tuple((item["key"], item["mode"]) for item in stages)
        if actual_contracts not in {
            UNATTENDED_STAGE_CONTRACTS,
            UNATTENDED_VISUAL_RULES_STAGE_CONTRACTS,
        }:
            raise ValidationError(
                "unattended recipe 必须使用完整且唯一的脚本生成、策略审核、Bible、"
                "分镜、动态媒体、单产物、装配与交付闭包",
            )
    return stages


def _selected_conditioning_mode_contract_for_shot(
    run: ProductionRun,
    *,
    shot: Shot,
) -> dict[str, Any] | None:
    """Return the exact frozen capability row used by storyboard review."""

    if shot.media_type != Shot.MediaType.VIDEO:
        return None
    media_stage = run.stages.filter(stage_key="media_generation").first()
    if media_stage is None:
        return None
    selection = _frozen_workflow_selection(media_stage)
    if selection is None:
        return None
    route = _frozen_route_for_shot(selection, shot_id=shot.id)
    configured = route.get("conditioning_contract")
    configured_sha256 = str(route.get("conditioning_contract_sha256") or "")
    if configured is not None and (
        not isinstance(configured, dict)
        or not configured_sha256
        or _sha256(configured) != configured_sha256
    ):
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            f"镜头 {shot.id} 的冻结条件输入合同无效。",
        )
    try:
        return conditioning_mode_contract(
            conditioning_contract_from_frozen_route(route),
            shot.conditioning_mode,
        )
    except ValidationError as exc:
        raise StageBlocked(
            "conditioning_mode_mismatch",
            f"镜头 {shot.id} 的条件输入模式不属于冻结 route。",
        ) from exc


def _selected_unattended_conditioning_mode_contract(
    workflow_selection: object,
    *,
    mode: str,
) -> dict[str, Any]:
    if (
        not isinstance(workflow_selection, dict)
        or workflow_selection.get("mode") != "uniform"
        or not isinstance(workflow_selection.get("route"), dict)
    ):
        raise ProductionRunCreateRejected(
            {"workflow_selection": "全自动运行缺少整集统一的冻结条件模式合同"},
            code="unattended_conditioning_contract_invalid",
        )
    route = workflow_selection["route"]
    contract = route.get("conditioning_contract")
    if (
        not isinstance(contract, dict)
        or route.get("conditioning_contract_sha256") != _sha256(contract)
    ):
        raise ProductionRunCreateRejected(
            {"workflow_selection": "全自动运行的冻结条件模式合同完整性校验失败"},
            code="unattended_conditioning_contract_invalid",
        )
    try:
        return conditioning_mode_contract(contract, mode)
    except ValidationError as exc:
        raise ProductionRunCreateRejected(
            {"workflow_selection": "项目默认条件模式不属于全自动冻结 route"},
            code="unattended_conditioning_mode_invalid",
        ) from exc


def production_recipe_media_route_options(
    recipe: ProductionRecipeVersion,
) -> dict[str, Any]:
    """Return a current, secret-free dependency projection for media route pickers.

    Recipe definitions intentionally keep immutable ids only.  The production UI
    still needs human labels and explicit lifecycle blockers before it can freeze
    a run-scoped selection, so this projection resolves only the referenced rows
    and never exposes provider/deployment configuration.
    """

    verify_json_contract(
        recipe.definition,
        schema_version=recipe.definition_schema_version,
        size_bytes=recipe.definition_size_bytes,
        sha256=recipe.definition_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    definition = recipe.definition
    raw_stages = definition.get("stages") if isinstance(definition, dict) else None
    media_stage = next((
        item
        for item in (raw_stages or [])
        if isinstance(item, dict) and str(item.get("key") or "") == "media_generation"
    ), None)
    routing = media_stage.get("routing") if isinstance(media_stage, dict) else None
    raw_routes = routing.get("routes") if isinstance(routing, dict) else None
    if not isinstance(raw_routes, list):
        raw_routes = []
    bible_stage = next((
        item
        for item in (raw_stages or [])
        if isinstance(item, dict)
        and str(item.get("key") or "") == BIBLE_IMAGE_GENERATION_STAGE_KEY
    ), None)
    bible_routing = bible_stage.get("routing") if isinstance(bible_stage, dict) else None
    bible_raw_routes = (
        bible_routing.get("routes") if isinstance(bible_routing, dict) else None
    )
    if not isinstance(bible_raw_routes, list):
        bible_raw_routes = []
    catalog_routes = [*raw_routes, *bible_raw_routes]

    def canonical_uuid(value: object) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            return ""

    deployment_ids = {
        identifier
        for item in catalog_routes
        if isinstance(item, dict)
        if (identifier := canonical_uuid(item.get("model_deployment_id")))
    }
    workflow_ids = {
        identifier
        for item in catalog_routes
        if isinstance(item, dict)
        if (identifier := canonical_uuid(item.get("workflow_version_id")))
    }
    deployments = {
        str(item.id): item
        for item in ModelDeployment.objects.select_related(
            "profile_version__provider", "runtime_host",
        ).filter(pk__in=deployment_ids)
    }
    workflows = {
        str(item.id): item
        for item in WorkflowTemplateVersion.objects.select_related(
            "template", "model_profile_version", "validated_deployment",
        ).filter(pk__in=workflow_ids)
    }

    options: list[dict[str, Any]] = []
    for route_index, raw in enumerate(raw_routes):
        reasons: list[tuple[str, str]] = []
        item = raw if isinstance(raw, dict) else {}
        deployment_id = canonical_uuid(item.get("model_deployment_id"))
        workflow_id = canonical_uuid(item.get("workflow_version_id"))
        deployment = deployments.get(deployment_id)
        workflow = workflows.get(workflow_id)
        workflow_label = "异步媒体 API" if not workflow_id else "工作流不存在"
        deployment_label = "部署不存在"
        model_identifier = ""
        driver_type = ""
        output_kinds: list[str] = []
        conditioning_mode_keys: list[str] = []
        media_operations: list[str] = []
        prompt_profile_requirements: list[dict[str, Any]] = []
        conditioning_contract: dict[str, Any] | None = None
        media_constraints: dict[str, Any] | None = None

        if not isinstance(raw, dict):
            reasons.append(("route_contract_invalid", "route 合同不是对象"))
        if not deployment_id:
            reasons.append(("deployment_id_invalid", "模型部署 ID 缺失或无效"))
        elif deployment is None:
            reasons.append(("deployment_missing", "模型部署不存在"))
        else:
            profile = deployment.profile_version
            provider = profile.provider
            model_specification: dict[str, Any] | None = None
            try:
                model_specification = verified_model_profile_specification(profile)
            except ValidationError:
                reasons.append((
                    "model_profile_contract_invalid",
                    "ModelProfile specification 完整性校验失败",
                ))
            deployment_label = deployment.name or deployment.key
            model_identifier = profile.model_identifier
            driver_type = provider.driver_type
            if deployment.status != ModelDeployment.Status.ACTIVE:
                reasons.append(("deployment_not_active", "模型部署未启用"))
            if not provider.is_active:
                reasons.append(("provider_not_active", "Provider 已停用"))
            if profile.status != profile.Status.PUBLISHED:
                reasons.append(("model_not_published", "模型版本未发布"))
            try:
                validate_deployment_execution_contract(deployment)
            except (ProviderDriverError, ValidationError, RuntimeError):
                reasons.append((
                    "deployment_execution_contract_invalid",
                    "模型部署执行合同无效",
                ))

            if driver_type == ProviderProfile.DriverType.ASYNC_REST:
                if workflow_id:
                    reasons.append((
                        "async_workflow_forbidden",
                        "async REST route 不能绑定 ComfyUI 工作流",
                    ))
                if model_specification is not None:
                    try:
                        media_operations = validate_async_model_profile(profile)
                    except (ValidationError, ProviderDriverError, RuntimeError):
                        reasons.append((
                            "async_contract_invalid",
                            "异步媒体模型合同或驱动能力不完整",
                        ))
                    else:
                        missing_smoke = _missing_fresh_async_media_smoke_operations(
                            deployment,
                            media_operations,
                        )
                        if missing_smoke:
                            reasons.append((
                                "async_smoke_missing_or_stale",
                                "异步媒体 route 缺少当前有效 smoke 证据: "
                                + ", ".join(missing_smoke),
                            ))
                if "image.text_to_image" in media_operations:
                    output_kinds.append(Shot.MediaType.IMAGE)
                if "video.text_to_video" in media_operations:
                    output_kinds.append(Shot.MediaType.VIDEO)
            elif driver_type == ProviderProfile.DriverType.COMFYUI:
                if not workflow_id:
                    reasons.append(("workflow_required", "ComfyUI route 缺少工作流"))
                elif workflow is None:
                    reasons.append(("workflow_missing", "工作流版本不存在"))
                else:
                    workflow_label = f"{workflow.template.name} · v{workflow.version}"
                    output_kinds = [workflow.template.output_kind]
                    if workflow.status != WorkflowTemplateVersion.Status.PUBLISHED:
                        reasons.append(("workflow_not_published", "工作流未发布"))
                    if not workflow.is_current:
                        reasons.append(("workflow_not_current", "工作流不是 current 版本"))
                    if workflow.smoke_test_state != WorkflowTemplateVersion.ValidationState.PASSED:
                        reasons.append(("workflow_smoke_missing", "工作流未通过 smoke"))
                    if workflow.validated_deployment_id != deployment.id:
                        reasons.append((
                            "workflow_deployment_mismatch",
                            "工作流验证证据不属于当前部署",
                        ))
                    if workflow.model_profile_version_id not in {None, profile.id}:
                        reasons.append((
                            "workflow_model_mismatch",
                            "工作流与部署模型版本不兼容",
                        ))
                    if not all((
                        workflow.compiler_id,
                        workflow.compiler_version > 0,
                        workflow.semantic_bindings_sha256,
                        workflow.model_requirements_sha256,
                        workflow.execution_contract_sha256,
                    )):
                        reasons.append((
                            "workflow_contract_incomplete",
                            "工作流缺少已冻结执行合同",
                        ))
            else:
                reasons.append((
                    "driver_not_supported",
                    "媒体 route 只支持 ComfyUI 或 async REST 驱动",
                ))
            if output_kinds and model_specification is not None:
                try:
                    route_output_kind = (
                        Shot.MediaType.VIDEO
                        if Shot.MediaType.VIDEO in output_kinds
                        else Shot.MediaType.IMAGE
                    )
                    conditioning_contract = resolve_conditioning_contract(
                        workflow_input_contract=(
                            workflow.input_contract if workflow is not None else None
                        ),
                        workflow_compiler_id=(
                            workflow.compiler_id if workflow is not None else ""
                        ),
                        model_specification=model_specification,
                        output_kind=route_output_kind,
                        required=route_output_kind == Shot.MediaType.VIDEO,
                    )
                    media_constraints = normalize_model_media_constraints(
                        model_specification,
                    )
                    if route_output_kind == Shot.MediaType.VIDEO:
                        conditioning_mode_keys = [
                            str(row["key"])
                            for row in conditioning_contract["modes"]
                        ]
                except ValidationError:
                    reasons.append((
                        "media_capability_contract_invalid",
                        "Workflow/ModelProfile 缺少有效的冻结媒体能力合同",
                    ))
            reasons.extend(_media_prompt_profile_dependency_reasons(
                profile,
                output_kinds=output_kinds,
                conditioning_mode_keys=conditioning_mode_keys,
            ))
            prompt_profile_requirements = _safe_prompt_profile_choices(
                profile,
                output_kinds=output_kinds,
                conditioning_mode_keys=conditioning_mode_keys,
            )

        options.append({
            "route_index": route_index,
            "workflow_version_id": workflow_id or None,
            "workflow_label": workflow_label,
            "model_deployment_id": deployment_id or None,
            "deployment_label": deployment_label,
            "model_identifier": model_identifier,
            "driver_type": driver_type,
            "output_kinds": output_kinds,
            "conditioning_modes": (
                deepcopy(conditioning_contract.get("modes") or [])
                if isinstance(conditioning_contract, dict) else []
            ),
            "conditioning_contract_sha256": (
                _sha256(conditioning_contract)
                if isinstance(conditioning_contract, dict) else ""
            ),
            "media_constraints": deepcopy(media_constraints),
            "media_constraints_sha256": (
                _sha256(media_constraints)
                if isinstance(media_constraints, dict) else ""
            ),
            "media_operations": media_operations,
            "is_selectable": not reasons,
            "reason_codes": [code for code, _reason in reasons],
            "reasons": [reason for _code, reason in reasons],
            "prompt_profile_requirements": prompt_profile_requirements,
        })

    bible_requirement: dict[str, Any] | None = None
    bible_choice_maps: list[dict[str, dict[str, Any]]] = []
    bible_routes_valid = True
    if bible_raw_routes:
        try:
            _freeze_stage_routing(bible_routing, stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY)
        except (ValidationError, ProductionRunCreateRejected):
            bible_routes_valid = False
    for item in (bible_raw_routes if bible_routes_valid else []):
        if not isinstance(item, dict):
            continue
        deployment = deployments.get(canonical_uuid(item.get("model_deployment_id")))
        if deployment is None:
            continue
        if deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
            try:
                operations = validate_async_model_profile(deployment.profile_version)
            except (ValidationError, ProviderDriverError, RuntimeError):
                continue
            if _missing_fresh_async_media_smoke_operations(deployment, operations):
                continue
        requirements = _safe_prompt_profile_choices(
            deployment.profile_version,
            output_kinds=[Shot.MediaType.IMAGE],
            conditioning_mode_keys=[],
            bible_image=True,
        )
        choices = requirements[0]["choices"] if requirements else []
        bible_choice_maps.append({row["binding_id"]: row for row in choices})
    if bible_raw_routes:
        common_ids = (
            set.intersection(*(set(item) for item in bible_choice_maps))
            if bible_choice_maps and len(bible_choice_maps) == len(bible_raw_routes)
            else set()
        )
        bible_requirement = {
            "operation": "image.generate",
            "mode": "",
            "choices": [
                bible_choice_maps[0][binding_id]
                for binding_id in sorted(common_ids)
            ] if bible_choice_maps else [],
        }

    return {
        "schema_version": 2,
        "recipe_version_id": str(recipe.id),
        "recipe_definition_sha256": recipe.definition_sha256,
        "routes": options,
        "bible_image_generation": bible_requirement,
    }


def _missing_fresh_async_media_smoke_operations(
    deployment: ModelDeployment,
    operations: list[str],
) -> list[str]:
    """Return the exact async operations whose current smoke proof is unavailable."""

    return [
        operation
        for operation in operations
        if fresh_smoke_snapshot(deployment, operation) is None
    ]


def _media_prompt_profile_dependency_reasons(
    profile: ModelProfileVersion,
    *,
    output_kinds: list[str],
    conditioning_mode_keys: list[str],
) -> list[tuple[str, str]]:
    """Return safe dependency gaps for every prompt contract a media route advertises."""

    requirements = _media_prompt_profile_requirements(
        output_kinds=output_kinds,
        conditioning_mode_keys=conditioning_mode_keys,
    )
    family = model_family_for_profile(profile)
    reasons: list[tuple[str, str]] = []
    for operation, mode in requirements:
        label = f"{operation}/{mode}" if mode else operation
        choices = compatible_prompt_profile_bindings(
            operation=operation,
            model_family=family,
            mode=mode,
            model_profile_version=profile,
        )
        if not choices:
            reasons.append((
                "prompt_profile_binding_missing",
                f"{label} 缺少可显式选择的 active PromptProfile binding",
            ))
    return reasons


def _media_prompt_profile_requirements(
    *,
    output_kinds: list[str],
    conditioning_mode_keys: list[str],
) -> list[tuple[str, str]]:
    requirements: list[tuple[str, str]] = []
    if Shot.MediaType.IMAGE in output_kinds:
        requirements.append(("image.generate", ""))
    if Shot.MediaType.VIDEO in output_kinds:
        requirements.extend(
            ("video.generate", mode) for mode in conditioning_mode_keys
        )
    return list(dict.fromkeys(requirements))


def _safe_prompt_profile_choices(
    profile: ModelProfileVersion,
    *,
    output_kinds: list[str],
    conditioning_mode_keys: list[str],
    bible_image: bool = False,
) -> list[dict[str, Any]]:
    family = model_family_for_profile(profile)
    rows: list[dict[str, Any]] = []
    for operation, mode in _media_prompt_profile_requirements(
        output_kinds=output_kinds,
        conditioning_mode_keys=conditioning_mode_keys,
    ):
        choices = []
        for resolved in compatible_prompt_profile_bindings(
            operation=operation,
            model_family=family,
            mode=mode,
            model_profile_version=profile,
        ):
            if bible_image:
                try:
                    load_frozen_prompt_profile_selection(
                        frozen_profile_reference(
                            resolved,
                            model_profile_version=profile,
                            operation=operation,
                            mode=mode,
                        ),
                        model_profile_version=profile,
                        operation=operation,
                        mode=mode,
                        bible_image=True,
                    )
                except PromptProfileResolutionError:
                    continue
            binding = resolved.binding
            version = resolved.profile_version
            choices.append({
                "binding_id": str(binding.id),
                "binding_key": binding.key,
                "binding_name": binding.name,
                "prompt_profile_version_id": str(version.id),
                "profile_key": version.profile_key,
                "profile_version": version.version,
                "profile_name": version.name,
                "contract_sha256": version.contract_sha256,
                "priority": binding.priority,
                "binding_scope": (
                    "exact_model" if binding.model_profile_version_id else "model_family"
                ),
            })
        rows.append({"operation": operation, "mode": mode, "choices": choices})
    return rows


def _safe_media_route_projection(route: dict[str, Any]) -> dict[str, Any]:
    """Keep the selected immutable identities without exposing driver configuration."""

    smoke_evidence = route.get("smoke_evidence")
    execution_monitoring = _execution_monitoring_from_frozen_route(route)
    return {
        "schema_version": 1,
        "route_index": int(route["index"]),
        "workflow_version_id": route.get("workflow_version_id"),
        "workflow_sha256": str(route.get("workflow_sha256") or ""),
        "workflow_output_kind": str(route.get("workflow_output_kind") or ""),
        "conditioning_contract": deepcopy(route.get("conditioning_contract")),
        "conditioning_contract_sha256": str(
            route.get("conditioning_contract_sha256") or ""
        ),
        "media_constraints": deepcopy(route.get("media_constraints")),
        "media_constraints_sha256": str(
            route.get("media_constraints_sha256") or ""
        ),
        "execution_monitoring": execution_monitoring,
        "execution_monitoring_sha256": execution_monitoring_contract_sha256(
            execution_monitoring,
        ),
        "model_deployment_id": str(route.get("model_deployment_id") or ""),
        "model_profile_version_id": str(route.get("model_profile_version_id") or ""),
        "model_identifier": str(route.get("model_identifier") or ""),
        "provider_id": str(route.get("provider_id") or ""),
        "driver_type": str(route.get("driver_type") or ""),
        "runtime_host_id": route.get("runtime_host_id"),
        "is_external": bool(route.get("is_external")),
        "media_operations": list(route.get("media_operations") or []),
        "smoke_evidence_sha256": _sha256(
            smoke_evidence if isinstance(smoke_evidence, dict) else {},
        ),
        "prompt_profile": deepcopy(route.get("prompt_profile")),
    }


def _execution_monitoring_from_frozen_route(
    route: dict[str, Any],
) -> dict[str, Any]:
    """Read the exact execution-monitoring contract frozen with the route."""

    configured = route.get("execution_monitoring")
    digest = route.get("execution_monitoring_sha256")
    if configured is None or not digest:
        raise ValidationError({
            "workflow_selection": "冻结 route 缺少 execution monitoring 合同",
        })
    try:
        return verify_frozen_execution_monitoring_contract(
            configured,
            sha256=digest,
        )
    except ValidationError as exc:
        raise ValidationError({
            "workflow_selection": "冻结 execution monitoring 合同无效",
        }) from exc


def _media_prompt_profile_routing_projection(
    selection: dict[str, Any],
) -> dict[str, Any]:
    """Project exact binding refs into Stage routing without a hash cycle."""

    if selection.get("mode") == "uniform":
        rows = [{
            "operation": str(item.get("operation") or ""),
            "mode": str(item.get("mode") or ""),
            "prompt_profile": deepcopy(item.get("prompt_profile")),
        } for item in (selection.get("prompt_profile_selections") or [])
            if isinstance(item, dict)]
        return {
            "schema_version": 2,
            "mode": "uniform",
            "prompt_profile_selections": rows,
            "shot_routes": [],
        }
    rows = [{
        "shot_id": str(item.get("shot_id") or ""),
        "route_index": item.get("route", {}).get("route_index"),
        "prompt_profile": deepcopy(item.get("prompt_profile")),
    } for item in (selection.get("shot_routes") or [])
        if isinstance(item, dict) and isinstance(item.get("route"), dict)]
    return {
        "schema_version": 2,
        "mode": "per_shot",
        "prompt_profile_selections": [],
        "shot_routes": rows,
    }


def _freeze_media_prompt_profile_routing(
    media_stage: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    routing = media_stage.get("routing")
    if not isinstance(routing, dict):
        raise ValidationError({"workflow_selection": "媒体 Stage routing 快照无效"})
    routing["prompt_profile_selection"] = _media_prompt_profile_routing_projection(
        selection,
    )
    selection["media_stage_routing_sha256"] = _sha256(routing)
    return selection


def _validate_selected_media_route(
    route: dict[str, Any],
    *,
    project: SeriesProject,
    shot: Shot | None,
) -> tuple[ModelDeployment, WorkflowTemplateVersion | None]:
    """Revalidate the selected published recipe route against current catalog facts."""

    route_index = int(route.get("index", -1))
    try:
        deployment = ModelDeployment.objects.select_related(
            "profile_version__provider", "runtime_host",
        ).get(
            pk=route.get("model_deployment_id"),
            status=ModelDeployment.Status.ACTIVE,
        )
    except (ModelDeployment.DoesNotExist, ValueError) as exc:
        raise ValidationError({
            "workflow_selection": f"route {route_index} 的模型部署不存在或未启用",
        }) from exc
    profile = deployment.profile_version
    provider = profile.provider
    if not provider.is_active or profile.status != profile.Status.PUBLISHED:
        raise ValidationError({
            "workflow_selection": f"route {route_index} 的 Provider 或模型版本未发布/已停用",
        })
    if (
        str(profile.id) != str(route.get("model_profile_version_id") or "")
        or str(provider.id) != str(route.get("provider_id") or "")
        or provider.driver_type != route.get("driver_type")
        or (str(deployment.runtime_host_id) if deployment.runtime_host_id else None)
        != route.get("runtime_host_id")
        or credential_binding_sha256(deployment) != route.get("credential_binding_sha256")
    ):
        raise ValidationError({
            "workflow_selection": f"route {route_index} 的模型部署绑定已漂移，请发布新配方",
        })

    if provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
        if route.get("workflow_version_id") or route.get("workflow_snapshot"):
            raise ValidationError({
                "workflow_selection": f"route {route_index} 的 async REST 路由错误绑定了 ComfyUI 工作流",
            })
        if shot is not None:
            _validate_selected_media_route_for_shot(
                route,
                project=project,
                shot=shot,
                deployment=deployment,
                workflow=None,
            )
        return deployment, None

    if provider.driver_type != ProviderProfile.DriverType.COMFYUI:
        raise ValidationError({
            "workflow_selection": f"route {route_index} 不是可执行的 ComfyUI/async REST 媒体路由",
        })
    try:
        workflow = WorkflowTemplateVersion.objects.select_related(
            "template", "model_profile_version",
        ).get(
            pk=route.get("workflow_version_id"),
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            is_current=True,
            smoke_test_state=WorkflowTemplateVersion.ValidationState.PASSED,
        )
    except (WorkflowTemplateVersion.DoesNotExist, ValueError) as exc:
        raise ValidationError({
            "workflow_selection": (
                f"route {route_index} 的工作流未发布、不是 current 或未通过 smoke"
            ),
        }) from exc
    if (
        workflow.graph_sha256 != route.get("workflow_sha256")
        or workflow.model_profile_version_id not in {None, deployment.profile_version_id}
        or workflow.validated_deployment_id != deployment.id
    ):
        raise ValidationError({
            "workflow_selection": f"route {route_index} 的工作流与部署绑定已漂移，请发布新配方",
        })
    if shot is not None:
        _validate_selected_media_route_for_shot(
            route,
            project=project,
            shot=shot,
            deployment=deployment,
            workflow=workflow,
        )
    return deployment, workflow


def _validate_selected_media_route_for_shot(
    route: dict[str, Any],
    *,
    project: SeriesProject,
    shot: Shot,
    deployment: ModelDeployment,
    workflow: WorkflowTemplateVersion | None,
) -> None:
    route_index = int(route.get("index", -1))
    conditioning_contract = route.get("conditioning_contract")
    if (
        not isinstance(conditioning_contract, dict)
        or route.get("conditioning_contract_sha256") != _sha256(conditioning_contract)
    ):
        raise ValidationError({
            "workflow_selection": f"route {route_index} 的冻结 conditioning contract 无效",
        })
    media_constraints = route.get("media_constraints")
    if (
        not isinstance(media_constraints, dict)
        or route.get("media_constraints_sha256") != _sha256(media_constraints)
    ):
        raise ValidationError({
            "workflow_selection": f"route {route_index} 的冻结 media constraints 无效",
        })
    _execution_monitoring_from_frozen_route(route)
    if (
        workflow is not None
        and workflow.template.output_kind != shot.media_type
    ):
        raise ValidationError({
            "workflow_selection": (
                f"route {route_index} 输出 {workflow.template.output_kind}，"
                f"不能用于 {shot.media_type} 镜头"
            ),
        })
    width, height = _effective_route_resolution(
        route,
        requested_width=int(project.width),
        requested_height=int(project.height),
    )
    validate_media_execution_settings(
        constraints=media_constraints,
        width=width,
        height=height,
        fps=int(project.fps),
        duration_ms=int(
            shot.duration_ms
            if getattr(shot, "duration_ms", None)
            else project.default_shot_duration_ms
        ),
        media_type=shot.media_type,
    )
    mode_contract = None
    if shot.media_type == Shot.MediaType.VIDEO:
        mode_contract = conditioning_mode_contract(
            conditioning_contract,
            shot.conditioning_mode,
        )
        try:
            validate_reference_bindings_against_mode_contract(
                shot.reference_bindings
                if isinstance(shot.reference_bindings, dict)
                else {},
                mode_contract,
            )
        except ReferenceBindingError as exc:
            raise ValidationError({
                "workflow_selection": (
                    f"route {route_index} 的镜头 {shot.id} 参考绑定不满足冻结条件合同: {exc}"
                ),
            }) from exc
    if deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
        operation = (
            "image.text_to_image"
            if shot.media_type == Shot.MediaType.IMAGE
            else "video.text_to_video"
        )
        if operation not in set(route.get("media_operations") or []):
            raise ValidationError({
                "workflow_selection": f"route {route_index} 不支持镜头所需的 {operation}",
            })
        reference_bindings = (
            shot.reference_bindings if isinstance(shot.reference_bindings, dict) else {}
        )
        has_references = any((
            reference_bindings.get("first_frame_asset_id"),
            reference_bindings.get("last_frame_asset_id"),
            reference_bindings.get("first_frame_reference_asset_id"),
            reference_bindings.get("scene"),
            reference_bindings.get("characters"),
            reference_bindings.get("props"),
            reference_bindings.get("videos"),
            reference_bindings.get("audios"),
        ))
        if shot.media_type == Shot.MediaType.VIDEO and (
            (mode_contract or {}).get("input_kind") != "text_only" or has_references
        ):
            raise ValidationError({
                "workflow_selection": (
                    f"route {route_index} 的当前 async REST adapter 只支持 text_only 且无参考素材的视频"
                ),
            })
        if fresh_smoke_snapshot(deployment, operation) is None:
            raise ValidationError({
                "workflow_selection": f"route {route_index} 缺少 {operation} 的当前有效 smoke 证据",
            })
        return
    if workflow is None:
        raise ValidationError({
            "workflow_selection": f"route {route_index} 缺少当前可用的 ComfyUI 工作流",
        })


def _effective_route_resolution(
    route: dict[str, Any],
    *,
    requested_width: int,
    requested_height: int,
) -> tuple[int, int]:
    """Resolve a frozen workflow's executable dimensions without mutating project preference.

    Project/config resolution is a preference.  A selected immutable workflow can be more
    restrictive (for example, an exact 352x608 low-VRAM contract).  Keep the requested pair
    whenever both dimensions satisfy the workflow parameters; otherwise use the workflow's
    validated defaults as one atomic pair.  Async REST routes and workflows without explicit
    width/height parameters keep the requested dimensions.
    """

    snapshot = route.get("workflow_snapshot")
    contract = snapshot.get("input_contract") if isinstance(snapshot, dict) else None
    parameters = contract.get("parameters") if isinstance(contract, dict) else None
    if not isinstance(parameters, dict):
        return int(requested_width), int(requested_height)
    width_definition = parameters.get("image.width")
    height_definition = parameters.get("image.height")
    if not isinstance(width_definition, dict) or not isinstance(height_definition, dict):
        return int(requested_width), int(requested_height)

    def valid(value: Any, definition: dict[str, Any]) -> bool:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return False
        minimum = definition.get("min")
        maximum = definition.get("max")
        multiple = definition.get("multiple_of")
        if isinstance(minimum, int) and value < minimum:
            return False
        if isinstance(maximum, int) and value > maximum:
            return False
        if isinstance(multiple, int) and multiple > 0 and value % multiple:
            return False
        return True

    requested = (int(requested_width), int(requested_height))
    if valid(requested[0], width_definition) and valid(requested[1], height_definition):
        return requested
    defaults = (width_definition.get("default"), height_definition.get("default"))
    if valid(defaults[0], width_definition) and valid(defaults[1], height_definition):
        return int(defaults[0]), int(defaults[1])
    return requested
def _resolve_workflow_selection(
    *,
    project,
    stages: list[dict[str, Any]],
    selected_episode_ids: list[str],
    requested: dict[str, Any] | None,
    default_conditioning_mode: str,
    default_shot_media_type: str,
) -> dict[str, Any] | None:
    """Freeze exact route and PromptProfile binding ids for every media unit."""

    media_stage = next((item for item in stages if item["key"] == "media_generation"), None)
    if media_stage is None:
        if requested is None:
            return None
        raise ValidationError({
            "workflow_selection": "所选配方没有 media_generation 阶段",
        })
    routing = media_stage["routing"]
    routes = routing.get("routes") if isinstance(routing, dict) else None
    if not isinstance(routes, list) or not routes:
        raise ValidationError({
            "workflow_selection": "所选配方没有可选择的已冻结媒体 route",
        })
    if requested is None:
        code = (
            "prompt_profile_selection_required"
            if routing.get("mode") == "fixed" and len(routes) == 1
            else "route_selection_required"
        )
        raise ProductionRunCreateRejected(
            {"workflow_selection": "必须显式选择媒体 route 与 PromptProfile binding"},
            code=code,
        )
    if requested.get("schema_version") != 2:
        raise ProductionRunCreateRejected(
            {"workflow_selection": "仅支持 workflow_selection schema_version=2"},
            code="prompt_profile_selection_required",
        )
    route_by_index = {
        int(item["index"]): item
        for item in routes
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    }

    def selected_route(raw_index: Any) -> dict[str, Any]:
        try:
            route_index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValidationError({
                "workflow_selection": "route_index 必须是配方中的整数索引",
            }) from exc
        route = route_by_index.get(route_index)
        if route is None:
            raise ValidationError({
                "workflow_selection": f"route {route_index} 不属于所选 published recipe",
            })
        return route

    route_catalog: dict[
        int,
        tuple[ModelDeployment, WorkflowTemplateVersion | None],
    ] = {}
    validated_compatibility: set[tuple[int, str, str, bool]] = set()

    def route_catalog_facts(
        route: dict[str, Any],
    ) -> tuple[ModelDeployment, WorkflowTemplateVersion | None]:
        route_index = int(route["index"])
        if route_index not in route_catalog:
            route_catalog[route_index] = _validate_selected_media_route(
                route,
                project=project,
                shot=None,
            )
        return route_catalog[route_index]

    def validate_shot_route(route: dict[str, Any], shot: Shot) -> None:
        bindings = shot.reference_bindings if isinstance(shot.reference_bindings, dict) else {}
        has_references = any((
            bindings.get("first_frame_asset_id"),
            bindings.get("last_frame_asset_id"),
            bindings.get("first_frame_reference_asset_id"),
            bindings.get("scene"),
            bindings.get("characters"),
            bindings.get("props"),
            bindings.get("videos"),
            bindings.get("audios"),
        ))
        contract_key = (
            int(route["index"]),
            shot.media_type,
            shot.conditioning_mode,
            has_references,
        )
        if contract_key in validated_compatibility:
            return
        deployment, workflow = route_catalog_facts(route)
        _validate_selected_media_route_for_shot(
            route,
            project=project,
            shot=shot,
            deployment=deployment,
            workflow=workflow,
        )
        validated_compatibility.add(contract_key)

    def contract_for_shot(shot: Shot) -> tuple[str, str]:
        return (
            "image.generate" if shot.media_type == Shot.MediaType.IMAGE else "video.generate",
            shot.conditioning_mode if shot.media_type == Shot.MediaType.VIDEO else "",
        )

    def freeze_binding(
        route: dict[str, Any],
        *,
        operation: str,
        mode: str,
        binding_id: Any,
    ) -> dict[str, Any]:
        deployment, _workflow = route_catalog_facts(route)
        try:
            resolved = require_explicit_prompt_profile_binding(
                binding_id,
                operation=operation,
                model_family=model_family_for_profile(deployment.profile_version),
                mode=mode,
                model_profile_version=deployment.profile_version,
                lock=True,
            )
        except PromptProfileResolutionError as exc:
            raise ProductionRunCreateRejected(
                {"workflow_selection": str(exc)},
                code=exc.code,
            ) from exc
        return frozen_profile_reference(
            resolved,
            model_profile_version=deployment.profile_version,
            operation=operation,
            mode=mode,
        )

    mode = str(requested.get("mode") or "")
    shots = list(
        Shot.objects.select_related("episode")
        .filter(episode__project=project, episode_id__in=selected_episode_ids)
        .order_by("episode__number", "number", "id")
    )
    if mode == "uniform":
        route = selected_route(requested.get("route_index"))
        raw_selections = requested.get("prompt_profile_selections")
        if not isinstance(raw_selections, list) or not raw_selections:
            raise ProductionRunCreateRejected(
                {"workflow_selection": "整集 route 必须显式选择 PromptProfile binding"},
                code="prompt_profile_selection_required",
            )
        requested_by_contract: dict[tuple[str, str], Any] = {}
        for row in raw_selections:
            if not isinstance(row, dict):
                raise ValidationError({"workflow_selection": "prompt_profile_selections 每项必须是对象"})
            contract = (str(row.get("operation") or ""), str(row.get("mode") or ""))
            if contract in requested_by_contract:
                raise ValidationError({"workflow_selection": "同一 operation/mode 不能重复选择 PromptProfile"})
            requested_by_contract[contract] = row.get("prompt_profile_binding_id")
        if shots:
            route_catalog_facts(route)
            for shot in shots:
                validate_shot_route(route, shot)
            required_contracts = {contract_for_shot(shot) for shot in shots}
        else:
            # AI/import runs can select a route before storyboard materializes
            # any Shot rows. Validate against the same defaults that the
            # deterministic breakdown will persist, otherwise an image-only,
            # reference-only, or incompatible video workflow can be frozen into a
            # run that is guaranteed to block after storyboard creation.
            if default_shot_media_type == Shot.MediaType.VIDEO:
                try:
                    future_conditioning_mode = normalize_default_conditioning_mode(
                        default_conditioning_mode,
                    )
                except ValueError as exc:
                    raise ProductionRunCreateRejected(
                        {
                            "project_id": (
                                "自动 storyboard 运行要求项目配置有效的 "
                                "default_conditioning_mode"
                            ),
                        },
                        code="project_default_conditioning_mode_invalid",
                    ) from exc
            else:
                future_conditioning_mode = ""
            future_shot = Shot(
                media_type=default_shot_media_type,
                conditioning_mode=future_conditioning_mode,
                reference_bindings={},
                include_audio=False,
            )
            validate_shot_route(route, future_shot)
            required_contracts = {contract_for_shot(future_shot)}
        if set(requested_by_contract) != required_contracts:
            raise ProductionRunCreateRejected(
                {
                    "workflow_selection": (
                        "prompt_profile_selections 必须精确覆盖实际镜头所需的 operation/mode"
                    ),
                },
                code="prompt_profile_selection_incomplete",
            )
        frozen_profiles = [{
            "operation": operation,
            "mode": contract_mode,
            "prompt_profile": freeze_binding(
                route,
                operation=operation,
                mode=contract_mode,
                binding_id=requested_by_contract[(operation, contract_mode)],
            ),
        } for operation, contract_mode in sorted(required_contracts)]
        return _freeze_media_prompt_profile_routing(media_stage, {
            "schema_version": 2,
            "mode": "uniform",
            "selected_episode_ids": list(selected_episode_ids),
            "route": _safe_media_route_projection(route),
            "shot_routes": [],
            "prompt_profile_selections": frozen_profiles,
        })
    if mode != "per_shot":
        raise ValidationError({
            "workflow_selection": "mode 必须是 uniform 或 per_shot",
        })
    if not shots:
        raise ValidationError({
            "workflow_selection": (
                "逐镜头工作流只允许用于已物化 Shot 的项目；AI/导入首版必须选择整集统一工作流"
            ),
        })
    assignments = requested.get("shot_routes")
    if not isinstance(assignments, list):
        raise ValidationError({"workflow_selection": "shot_routes 必须是数组"})
    by_shot: dict[str, dict[str, Any]] = {}
    for item in assignments:
        if not isinstance(item, dict):
            raise ValidationError({"workflow_selection": "shot_routes 每项必须是对象"})
        shot_id = str(item.get("shot_id") or "")
        if shot_id in by_shot:
            raise ValidationError({"workflow_selection": "同一镜头不能重复选择 route"})
        by_shot[shot_id] = item
    expected = {str(item.id): item for item in shots}
    missing = sorted(set(expected) - set(by_shot))
    extra = sorted(set(by_shot) - set(expected))
    if extra:
        raise ValidationError({
            "workflow_selection": "shot_routes 包含跨项目、未选 Episode 或已删除的 Shot",
        })
    if missing:
        raise ValidationError({
            "workflow_selection": f"shot_routes 缺少 {len(missing)} 个已选镜头",
        })
    resolved_rows = []
    for shot in shots:
        requested_row = by_shot[str(shot.id)]
        route = selected_route(requested_row.get("route_index"))
        validate_shot_route(route, shot)
        operation, contract_mode = contract_for_shot(shot)
        resolved_rows.append({
            "episode_id": str(shot.episode_id),
            "episode_number": shot.episode.number,
            "shot_id": str(shot.id),
            "shot_number": shot.number,
            "media_type": shot.media_type,
            "conditioning_mode": shot.conditioning_mode,
            "route": _safe_media_route_projection(route),
            "prompt_profile": freeze_binding(
                route,
                operation=operation,
                mode=contract_mode,
                binding_id=requested_row.get("prompt_profile_binding_id"),
            ),
        })
    return _freeze_media_prompt_profile_routing(media_stage, {
        "schema_version": 2,
        "mode": "per_shot",
        "selected_episode_ids": list(selected_episode_ids),
        "route": None,
        "shot_routes": resolved_rows,
        "prompt_profile_selections": [],
    })


def _freeze_bible_prompt_profile_selection(
    *,
    stages: list[dict[str, Any]],
    binding_id: Any,
) -> dict[str, Any] | None:
    stage = next((
        item for item in stages
        if item["key"] == BIBLE_IMAGE_GENERATION_STAGE_KEY
    ), None)
    selected_id = str(binding_id or "").strip()
    if stage is None:
        if selected_id:
            raise ValidationError({
                "bible_image_prompt_profile_binding_id": (
                    "所选配方没有 Bible 参考图生成阶段"
                ),
            })
        return None
    if not selected_id:
        raise ProductionRunCreateRejected(
            {
                "bible_image_prompt_profile_binding_id": (
                    "Bible 参考图生成必须显式选择 PromptProfile binding"
                ),
            },
            code="bible_prompt_profile_selection_required",
        )
    routing = stage.get("routing")
    routes = routing.get("routes") if isinstance(routing, dict) else None
    if not isinstance(routes, list) or not routes:
        raise ValidationError({
            "bible_image_prompt_profile_binding_id": "Bible 阶段没有可冻结 route",
        })
    frozen_rows = []
    for route in routes:
        try:
            model_profile = ModelProfileVersion.objects.select_related("provider").get(
                pk=route["model_profile_version_id"],
                status=ModelProfileVersion.Status.PUBLISHED,
            )
            resolved = require_explicit_prompt_profile_binding(
                selected_id,
                operation="image.generate",
                model_family=model_family_for_profile(model_profile),
                mode="",
                model_profile_version=model_profile,
                bible_image=True,
                lock=True,
            )
        except (ModelProfileVersion.DoesNotExist, KeyError) as exc:
            raise ValidationError({
                "bible_image_prompt_profile_binding_id": "Bible route 模型版本不可用",
            }) from exc
        except PromptProfileResolutionError as exc:
            raise ProductionRunCreateRejected(
                {"bible_image_prompt_profile_binding_id": str(exc)},
                code=exc.code,
            ) from exc
        reference = frozen_profile_reference(
            resolved,
            model_profile_version=model_profile,
            operation="image.generate",
            mode="",
        )
        route["prompt_profile"] = reference
        frozen_rows.append({
            "route_index": int(route["index"]),
            "prompt_profile": deepcopy(reference),
        })
    return {
        "schema_version": 2,
        "prompt_profile_binding_id": selected_id,
        "routes": frozen_rows,
    }


def _route_mode_requires_entity_reference_bible(
    route: object,
    mode: object,
) -> bool:
    contract = conditioning_contract_from_frozen_route(route)
    row = conditioning_mode_contract(contract, mode, required=False)
    return bool(
        isinstance(row, dict)
        and row.get("requires_entity_reference_bible") is True
    )


def _routing_requires_entity_reference_bible(routing: object) -> bool:
    if not isinstance(routing, dict):
        return False
    for route in routing.get("routes") or []:
        if not isinstance(route, dict):
            continue
        contract = conditioning_contract_from_frozen_route(route)
        if any(
            isinstance(item, dict)
            and item.get("requires_entity_reference_bible") is True
            for item in (contract.get("modes") or [])
        ):
            return True
    return False


def _run_requires_entity_reference_bible(run: ProductionRun) -> bool:
    snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    selection = snapshot.get("workflow_selection")
    if not isinstance(selection, dict):
        return False
    if selection.get("mode") == "uniform":
        route = selection.get("route")
        return any(
            isinstance(item, dict)
            and str(item.get("operation") or "") == "video.generate"
            and _route_mode_requires_entity_reference_bible(
                route,
                item.get("mode"),
            )
            for item in (selection.get("prompt_profile_selections") or [])
        )
    return any(
        isinstance(item, dict)
        and str(item.get("media_type") or "") == Shot.MediaType.VIDEO
        and _route_mode_requires_entity_reference_bible(
            item.get("route"),
            item.get("conditioning_mode"),
        )
        for item in (selection.get("shot_routes") or [])
    )


def _validate_bible_source_contract(
    by_key: dict[str, dict[str, Any]],
    *,
    required_review_approval: str,
) -> str:
    """Validate the configured source of formal text Bible entities.

    Human-operated recipes may maintain Character/Scene/Prop Bible rows directly
    and confirm them at ``bible_review/manual``.  Generated or normalized script
    recipes instead freeze a structured candidate block at
    ``bible_materialize/deterministic`` and approve it at
    ``bible_review/review``.  Keeping these as two explicit, mutually-exclusive
    contracts avoids forcing a hidden SCRIPT_BIBLE_V1 preamble on ordinary
    pasted scripts while preserving provenance for model-generated candidates.
    """

    materialize = by_key.get("bible_materialize")
    review = by_key.get("bible_review")
    if materialize is None:
        if (
            isinstance(review, dict)
            and review.get("mode") == "manual"
            and review.get("approval") == "required"
        ):
            return "manual"
        raise ValidationError(
            "Bible 来源必须配置为 bible_review/manual(required)，或 "
            "bible_materialize/deterministic + bible_review/review。",
        )
    if (
        materialize.get("mode") == "deterministic"
        and isinstance(review, dict)
        and review.get("mode") == "review"
        and review.get("approval") == required_review_approval
    ):
        return "materialized"
    raise ValidationError(
        "Bible 候选物化必须使用 bible_materialize/deterministic，并由匹配的 "
        "bible_review/review 审核；纯人工 Bible 不得同时声明 bible_materialize。",
    )


def _validate_delivery_recipe_closure(stages: list[dict[str, Any]]) -> None:
    """Require the complete, formally-gated production-to-delivery chain."""

    by_key = {item["key"]: item for item in stages}
    policy_review = any(item.get("approval") == "policy" for item in stages)
    required_review_approval = "policy" if policy_review else "required"
    required = (
        "storyboard", "prompt_compile", "media_generation",
        "artifact_selection", "assembly", "delivery",
    )
    missing = [key for key in required if key not in by_key]
    if missing:
        raise ValidationError(
            "published recipe 缺少统一交付闭包阶段: " + ", ".join(missing),
        )
    content_inputs = [
        item for item in stages
        if (item["key"], item["mode"]) in {
            ("script_input", "confirmed_revision"),
            ("script_input", "manual"),
            ("script_generate", "ai"),
        }
    ]
    if len(content_inputs) != 1:
        raise ValidationError(
            "published recipe 必须且只能包含一个正式 content 输入门："
            "script_input/confirmed_revision、script_input/manual 或 script_generate/ai",
        )
    if content_inputs[0]["key"] == "script_generate" and not (
        by_key.get("script_review", {}).get("mode") == "review"
    ):
        raise ValidationError("script_generate/ai 后必须有 script_review/review 正式确认门")
    structure = by_key.get("script_structure")
    if structure:
        if (
            (content_inputs[0]["key"], content_inputs[0]["mode"])
            != ("script_input", "confirmed_revision")
        ):
            raise ValidationError("script_structure/ai 只能跟在 script_input/confirmed_revision 后")
        review = by_key.get("script_review")
        if (
            structure.get("mode") != "ai"
            or not isinstance(review, dict)
            or review.get("mode") != "review"
            or review.get("approval") != required_review_approval
        ):
            raise ValidationError(
                "script_structure 必须使用 ai，并由 approval=required 的 script_review 确认",
            )
    normalize = by_key.get("script_normalize")
    normalize_review = by_key.get("script_normalize_review")
    if bool(normalize) != bool(normalize_review):
        raise ValidationError(
            "script_normalize 与 script_normalize_review/review 必须成对出现",
        )
    if normalize and (
        normalize.get("mode") not in {"ai", "manual"}
        or normalize_review.get("mode") != "review"
        or normalize_review.get("approval") != required_review_approval
    ):
        raise ValidationError(
            "script_normalize 必须使用 ai/manual，并由 approval=required 的 review 阶段确认",
        )
    visual_rules_generate = by_key.get("visual_rules_generate")
    visual_rules_review = by_key.get("visual_rules_review")
    if bool(visual_rules_generate) != bool(visual_rules_review):
        raise ValidationError(
            "visual_rules_generate/ai 与 visual_rules_review/review 必须成对出现",
        )
    if visual_rules_generate:
        if (
            visual_rules_generate.get("mode") != "ai"
            or visual_rules_review.get("mode") != "review"
            or visual_rules_review.get("approval") != required_review_approval
        ):
            raise ValidationError(
                "全剧影视规则必须在已确认的 Bible 后生成，再由匹配的 review 门确认",
            )
        _validate_bible_source_contract(
            by_key,
            required_review_approval=required_review_approval,
        )
    bible_image_generation = by_key.get(BIBLE_IMAGE_GENERATION_STAGE_KEY)
    bible_image_review = by_key.get(BIBLE_IMAGE_REVIEW_STAGE_KEY)
    if bool(bible_image_generation) != bool(bible_image_review):
        raise ValidationError(
            "bible_image_generation 与 bible_image_review 必须成对出现",
        )
    if (
        _routing_requires_entity_reference_bible(
            by_key.get("media_generation", {}).get("routing")
        )
        and not bible_image_generation
    ):
        raise ValidationError(
            "声明 requires_entity_reference_bible 的媒体 route 必须包含 "
            "bible_image_generation/bible_image_review；"
            "仅绑定动态参考视频不能作为人物、场景和道具的跨镜头一致性锚点",
        )
    if bible_image_generation:
        storyboard_review = by_key.get("storyboard_review")
        _validate_bible_source_contract(
            by_key,
            required_review_approval=required_review_approval,
        )
        if not normalize:
            raise ValidationError(
                "Bible 参考图阶段必须先经过 script_normalize 与 required review，"
                "确保中文 Bible 字段已形成人工确认的执行语言视图",
            )
        if (
            bible_image_generation.get("mode") != "existing_pipeline"
            or bible_image_review.get("mode") != "review"
            or bible_image_review.get("approval") != required_review_approval
        ):
            raise ValidationError(
                "Bible 参考图必须由 existing_pipeline 生成并经 required review 审批",
            )
        if (
            not isinstance(storyboard_review, dict)
            or storyboard_review.get("mode") != "review"
            or storyboard_review.get("approval") != required_review_approval
        ):
            raise ValidationError(
                "Bible 参考图生产必须在 bible_image_review 后、prompt_compile 前经过 "
                "required storyboard_review，逐镜冻结精确素材绑定",
            )
    expected_modes = {
        "storyboard": {"deterministic", "manual"},
        **({
            **({"bible_materialize": "deterministic"} if "bible_materialize" in by_key else {}),
            "bible_review": {"review", "manual"},
            BIBLE_IMAGE_GENERATION_STAGE_KEY: "existing_pipeline",
            BIBLE_IMAGE_REVIEW_STAGE_KEY: "review",
            "storyboard_review": "review",
        } if bible_image_generation else ({
            **({"bible_materialize": "deterministic"} if "bible_materialize" in by_key else {}),
            "bible_review": {"review", "manual"},
        } if visual_rules_generate else {})),
        **({
            "visual_rules_generate": "ai",
            "visual_rules_review": "review",
        } if visual_rules_generate else {}),
        **({
            "script_normalize": {"ai", "manual"},
            "script_normalize_review": "review",
        } if normalize else {}),
        "prompt_compile": "existing_pipeline",
        "media_generation": "existing_pipeline",
        "artifact_selection": "review",
        "assembly": "existing_pipeline",
        "delivery": "existing_pipeline",
    }
    for key, expected in expected_modes.items():
        actual = by_key[key]["mode"]
        allowed = expected if isinstance(expected, set) else {expected}
        if actual not in allowed:
            raise ValidationError(
                f"published recipe 阶段 {key} 必须使用 " + "/".join(sorted(allowed)),
            )
    positions = {item["key"]: index for index, item in enumerate(stages)}
    content_key = content_inputs[0]["key"]
    ordered = [content_key]
    if content_key == "script_generate":
        ordered.append("script_review")
    elif structure:
        ordered.extend(("script_structure", "script_review"))
    if bible_image_generation or visual_rules_generate:
        if "bible_materialize" in by_key:
            ordered.append("bible_materialize")
        ordered.append("bible_review")
    if visual_rules_generate:
        ordered.extend(("visual_rules_generate", "visual_rules_review"))
    ordered.append("storyboard")
    if normalize:
        ordered.extend(("script_normalize", "script_normalize_review"))
    if bible_image_generation:
        ordered.extend((
            BIBLE_IMAGE_GENERATION_STAGE_KEY,
            BIBLE_IMAGE_REVIEW_STAGE_KEY,
            "storyboard_review",
        ))
    elif "storyboard_review" in by_key:
        ordered.append("storyboard_review")
    ordered.extend((
        "prompt_compile", "media_generation", "artifact_selection", "assembly", "delivery",
    ))
    if [positions[key] for key in ordered] != sorted(positions[key] for key in ordered):
        raise ValidationError("published recipe 的 content/storyboard/prompt/media/selection/assembly/delivery 顺序无效")


def _assert_frozen_bible_run_closure(stages: list[StageRun]) -> None:
    """Fail closed for pre-fix runs whose frozen stages bypass exact Shot binding."""

    has_bible_images = any(
        item.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY for item in stages
    )
    run = stages[0].production_run if stages else None
    if (
        not has_bible_images
        and run is not None
        and _run_requires_entity_reference_bible(run)
    ):
        raise ProductionRunConflict(
            {
                "detail": (
                    "该 ProductionRun 的冻结条件模式要求实体参考 Bible，却没有冻结 "
                    "Bible 参考图生成/审核阶段；"
                    "动态参考视频会作为完整视频条件被模型读取；purpose 仅表达希望侧重的"
                    "动作/镜头意图，不提供模型级内容隔离，也不能替代实体一致性锚点。"
                    "请使用包含 Bible 图片闭包的新配方创建新运行。"
                ),
            },
            code="entity_reference_bible_image_closure_missing",
        )
    if not has_bible_images:
        return
    frozen = [{
        "key": item.stage_key,
        "mode": item.mode,
        "approval": (
            "policy" if item.decision_policy
            else "required" if item.approval_required else "auto"
        ),
        "routing": deepcopy(item.routing),
    } for item in stages]
    try:
        _validate_delivery_recipe_closure(frozen)
    except ValidationError as exc:
        raise ProductionRunConflict(
            {
                "detail": (
                    "该 ProductionRun 使用已退役的 Bible 阶段顺序，缺少在图片审核后"
                    "逐镜冻结 reference_bindings 的正式 storyboard_review；请新建运行。"
                ),
            },
            code="retired_bible_run_stage_contract",
        ) from exc


def validate_production_recipe_executability(
    recipe: ProductionRecipeVersion,
    *,
    require_delivery_closure: bool = False,
) -> list[dict[str, Any]]:
    """Reject production publication when a stage has no real executor/materializer."""

    stages = _recipe_stages(recipe)
    unattended = (
        isinstance(recipe.definition, dict)
        and recipe.definition.get("schema_version") == 2
        and recipe.definition.get("interaction_mode") == UNATTENDED_INTERACTION_MODE
    )
    script_entries = [
        item for item in stages
        if (item["key"], item["mode"]) in {
            ("script_input", "confirmed_revision"), ("script_generate", "ai"),
        }
    ]
    if len(script_entries) > 1:
        raise ValidationError(
            "recipe 不能同时包含 script_input/confirmed_revision 与 script_generate/ai"
        )
    by_key = {item["key"]: item for item in stages}
    if "script_structure" in by_key:
        if not any(
            (item["key"], item["mode"]) == ("script_input", "confirmed_revision")
            for item in stages
        ):
            raise ValidationError("script_structure/ai 缺少 script_input/confirmed_revision")
        review = by_key.get("script_review")
        if not review or review["mode"] != "review" or review["approval"] != "required":
            raise ValidationError("script_structure/ai 后必须有 required script_review/review")
    visual_rules_generate = by_key.get("visual_rules_generate")
    visual_rules_review = by_key.get("visual_rules_review")
    if bool(visual_rules_generate) != bool(visual_rules_review):
        raise ValidationError("visual_rules_generate 与 visual_rules_review 必须成对出现")
    if visual_rules_generate:
        expected_approval = "policy" if unattended else "required"
        if (
            visual_rules_generate["mode"] != "ai"
            or visual_rules_review["mode"] != "review"
            or visual_rules_review["approval"] != expected_approval
        ):
            raise ValidationError(
                "全剧影视规则生成必须位于已确认 Bible 之后，并由匹配的 review 阶段确认",
            )
        _validate_bible_source_contract(
            by_key,
            required_review_approval=expected_approval,
        )
    for stage in stages:
        key = stage["key"]
        mode = stage["mode"]
        if mode == "skip":
            continue
        if (key, mode) not in EXECUTABLE_STAGE_MODES:
            raise ValidationError(
                f"阶段 {key}/{mode} 尚无正式 executor/materializer；只能保留 draft，不能发布生产配方"
            )
        if key in {"media_generation", BIBLE_IMAGE_GENERATION_STAGE_KEY}:
            routing = stage["routing"]
            if routing.get("mode") == "unconfigured" or not routing.get("routes"):
                raise ValidationError(f"{key} 必须冻结至少一个已发布 workflow/deployment route")
            for route in routing["routes"]:
                deployment = ModelDeployment.objects.select_related(
                    "profile_version__provider",
                ).get(pk=route["model_deployment_id"])
                if deployment.provider.driver_type == ProviderProfile.DriverType.COMFYUI:
                    if not route.get("workflow_version_id"):
                        raise ValidationError("ComfyUI media route 必须绑定 workflow version")
                    if (
                        key == BIBLE_IMAGE_GENERATION_STAGE_KEY
                        and route.get("workflow_output_kind") != Shot.MediaType.IMAGE
                    ):
                        raise ValidationError("Bible 参考图 ComfyUI route 必须输出 image")
                    if key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
                        _validate_bible_text_to_image_workflow_snapshot(
                            route.get("workflow_snapshot"),
                        )
                    continue
                if deployment.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
                    raise ValidationError("media_generation 仅支持 ComfyUI 或 async_rest route")
                if route.get("workflow_version_id") or route.get("workflow_snapshot"):
                    raise ValidationError("async_rest route 禁止绑定 ComfyUI workflow")
                operations = validate_async_model_profile(deployment.profile_version)
                if (
                    key == BIBLE_IMAGE_GENERATION_STAGE_KEY
                    and "image.text_to_image" not in operations
                ):
                    raise ValidationError("Bible 参考图 async route 缺少 image.text_to_image")
                if not operations:
                    raise ValidationError("async_rest route 没有首版可执行媒体能力")
                missing_smoke = _missing_fresh_async_media_smoke_operations(
                    deployment,
                    operations,
                )
                if missing_smoke:
                    raise ValidationError(
                        "async_rest route 缺少未过期的真实烟测证据: " + ", ".join(missing_smoke)
                    )
        if key == "script_generate":
            routing = stage["routing"]
            if routing.get("mode") == "unconfigured" or not routing.get("routes"):
                raise ValidationError("script_generate 必须冻结至少一个 ModelDeployment route")
            if routing["variant_count"] != 1:
                raise ValidationError("script_generate 首版只支持 variant_count=1")
            for route in routing["routes"]:
                if route.get("workflow_version_id") or route.get("workflow_snapshot"):
                    raise ValidationError("script_generate 文本 route 不能绑定媒体 workflow")
                deployment = ModelDeployment.objects.select_related(
                    "profile_version__provider",
                ).get(pk=route["model_deployment_id"])
                if validate_script_generation_model_profile(
                    deployment.profile_version,
                ) is None:
                    raise ValidationError("script_generate 的模型版本未声明 script.generate 能力")
                if not deployment_supports_script_generation(deployment):
                    raise ValidationError(
                        "script_generate deployment 没有显式受控 chat endpoint"
                    )
                try:
                    _series_policy(deployment)
                except StageBlocked as exc:
                    raise ValidationError(exc.reason) from exc
                try:
                    require_prompt_profile_binding(
                        operation=TEXT_OPERATION,
                        model_family=model_family_for_profile(deployment.profile_version),
                        model_profile_version=deployment.profile_version,
                    )
                except ValidationError as exc:
                    raise ValidationError(
                        "script_generate route 缺少唯一 active PromptProfile binding",
                    ) from exc
        if key == "script_structure":
            routing = stage["routing"]
            if routing.get("mode") == "unconfigured" or not routing.get("routes"):
                # Exact canonical input is a deterministic no-submission path
                # and remains executable without an external route or smoke.
                # Non-canonical input will stop at the stage with an explicit
                # routing_not_configured gate instead of fabricating output.
                continue
            if routing["variant_count"] != 1:
                raise ValidationError("script_structure 首版只支持 variant_count=1")
            for route in routing["routes"]:
                if route.get("workflow_version_id") or route.get("workflow_snapshot"):
                    raise ValidationError("script_structure 文本 route 不能绑定媒体 workflow")
                deployment = ModelDeployment.objects.select_related(
                    "profile_version__provider",
                ).get(pk=route["model_deployment_id"])
                if validate_script_structure_model_profile(deployment.profile_version) is None:
                    raise ValidationError(
                        "script_structure 的模型版本未声明 script.structure 能力",
                    )
                if not deployment_supports_script_generation(deployment):
                    raise ValidationError(
                        "script_structure deployment 没有显式受控 chat endpoint",
                    )
                try:
                    require_prompt_profile_binding(
                        operation=SCRIPT_STRUCTURE_OPERATION,
                        model_family=model_family_for_profile(deployment.profile_version),
                        model_profile_version=deployment.profile_version,
                    )
                except ValidationError as exc:
                    raise ValidationError(
                        "script_structure route 缺少唯一 active PromptProfile binding",
                    ) from exc
        if key == "visual_rules_generate":
            routing = stage["routing"]
            if routing.get("mode") == "unconfigured" or not routing.get("routes"):
                raise ValidationError(
                    "visual_rules_generate 必须冻结至少一个 ModelDeployment route",
                )
            if routing["variant_count"] != 1:
                raise ValidationError("visual_rules_generate 首版只支持 variant_count=1")
            for route in routing["routes"]:
                if route.get("workflow_version_id") or route.get("workflow_snapshot"):
                    raise ValidationError("visual_rules_generate 文本 route 不能绑定媒体 workflow")
                deployment = ModelDeployment.objects.select_related(
                    "profile_version__provider",
                ).get(pk=route["model_deployment_id"])
                if validate_visual_rules_model_profile(deployment.profile_version) is None:
                    raise ValidationError(
                        "visual_rules_generate 的模型版本未声明 visual_rules.generate 能力",
                    )
                if not deployment_supports_script_generation(deployment):
                    raise ValidationError(
                        "visual_rules_generate deployment 没有受控 chat endpoint",
                    )
                try:
                    verify_visual_rules_smoke(deployment, route)
                except ValidationError as exc:
                    raise ValidationError(
                        "visual_rules_generate route 缺少当前专用真实 smoke 证据",
                    ) from exc
                try:
                    require_prompt_profile_binding(
                        operation=VISUAL_RULES_OPERATION,
                        model_family=model_family_for_profile(deployment.profile_version),
                        model_profile_version=deployment.profile_version,
                    )
                except ValidationError as exc:
                    raise ValidationError(
                        "visual_rules_generate route 缺少唯一 active PromptProfile binding",
                    ) from exc
        if key == "script_normalize":
            routing = stage["routing"]
            if mode == "manual":
                if routing.get("mode") != "unconfigured" or routing.get("routes"):
                    raise ValidationError(
                        "script_normalize/manual 不得绑定 Provider route；人工字段仍由后续 review 严格确认",
                    )
                continue
            if routing.get("mode") == "unconfigured" or not routing.get("routes"):
                raise ValidationError("script_normalize 必须冻结至少一个 ModelDeployment route")
            if routing["variant_count"] != 1:
                raise ValidationError("script_normalize 首版只支持 variant_count=1")
            for route in routing["routes"]:
                if route.get("workflow_version_id") or route.get("workflow_snapshot"):
                    raise ValidationError("script_normalize 文本 route 不能绑定媒体 workflow")
                deployment = ModelDeployment.objects.select_related(
                    "profile_version__provider",
                ).get(pk=route["model_deployment_id"])
                if not validate_prompt_normalization_model_profile(
                    deployment.profile_version,
                ):
                    raise ValidationError(
                        "script_normalize 模型版本未在 capabilities 精确声明 prompt.normalize",
                    )
                state, reason, normalization_route = prompt_normalization_route_state(
                    deployment,
                    execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
                )
                if state != "ready" or normalization_route is None:
                    raise ValidationError(
                        f"script_normalize route 未通过 durable/fresh-smoke 门禁: {reason}",
                    )
                frozen_smoke = (
                    route.get("smoke_evidence", {}).get(PROMPT_NORMALIZE_OPERATION)
                    if isinstance(route.get("smoke_evidence"), dict) else None
                )
                if (
                    not isinstance(frozen_smoke, dict)
                    or frozen_smoke.get("snapshot_id") != str(normalization_route.smoke.id)
                    or frozen_smoke.get("target_sha256") != normalization_route.smoke.target_sha256
                ):
                    raise ValidationError("script_normalize route 缺少当前真实烟测冻结证据")
                try:
                    require_prompt_profile_binding(
                        operation=PROMPT_NORMALIZE_OPERATION,
                        model_family=model_family_for_profile(deployment.profile_version),
                        model_profile_version=deployment.profile_version,
                    )
                except ValidationError as exc:
                    raise ValidationError(
                        "script_normalize route 缺少唯一 active PromptProfile binding",
                    ) from exc
    if unattended:
        media_stage = by_key["media_generation"]
        media_routes = media_stage["routing"].get("routes") or []
        media_route = media_routes[0] if len(media_routes) == 1 else {}
        if (
            media_route.get("driver_type") != ProviderProfile.DriverType.COMFYUI
            or media_route.get("workflow_output_kind") != Shot.MediaType.VIDEO
            or not _routing_requires_entity_reference_bible({
                "routes": [media_route],
            })
        ):
            raise ValidationError(
                "unattended recipe 的唯一媒体 route 必须是明确声明 "
                "requires_entity_reference_bible 的已发布 ComfyUI 视频工作流",
            )
        _validate_delivery_recipe_closure(stages)
    elif require_delivery_closure:
        _validate_delivery_recipe_closure(stages)
    return stages


def _verify_run_snapshots(run: ProductionRun) -> None:
    verify_json_contract(
        run.config_snapshot,
        schema_version=run.config_snapshot_schema_version,
        size_bytes=run.config_snapshot_size_bytes,
        sha256=run.config_snapshot_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    verify_json_contract(
        run.input_snapshot,
        schema_version=run.input_snapshot_schema_version,
        size_bytes=run.input_snapshot_size_bytes,
        sha256=run.input_snapshot_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    if (
        "ai_shot_duration_contract" in snapshot
        or "ai_shot_duration_contract_sha256" in snapshot
    ):
        raise ValidationError(
            "ProductionRun 使用已淘汰的 AI 专用镜头时长合同；请按当前配方新建运行"
        )
    execution_contract = snapshot.get("project_execution_contract")
    execution_contract_sha256 = snapshot.get("project_execution_contract_sha256")
    if execution_contract is not None or execution_contract_sha256:
        if (
            not isinstance(execution_contract, dict)
            or execution_contract.get("schema_version") != 1
            or execution_contract_sha256 != _sha256(execution_contract)
        ):
            raise ValidationError("ProductionRun 项目执行合同快照无效")
    duration_contract = snapshot.get("structured_shot_duration_contract")
    duration_contract_sha256 = snapshot.get("structured_shot_duration_contract_sha256")
    if duration_contract is not None or duration_contract_sha256:
        expected_contract = {
            "ai": {
                "source": "structured_ai_plan",
                "route_stage_key": "script_generate",
                "model_policy_key": "series_concept",
            },
            "confirmed_revision": {
                "source": "confirmed_revision_structure_plan",
                "route_stage_key": "script_structure",
                "model_policy_key": "script_structure",
            },
        }.get(snapshot.get("script_mode"))
        if (
            not isinstance(duration_contract, dict)
            or duration_contract.get("schema_version") != 1
            or expected_contract is None
            or any(
                duration_contract.get(key) != value
                for key, value in expected_contract.items()
            )
            or duration_contract_sha256 != _sha256(duration_contract)
        ):
            raise ValidationError("ProductionRun 结构化镜头时长合同快照无效")
        route_policies = duration_contract.get("route_policies")
        if (
            not isinstance(route_policies, list)
            or not route_policies
            or len(route_policies) > _routing_routes_max_items()
            or any(
                not isinstance(item, dict)
                or set(item) != {
                    "route_index",
                    "minimum_duration_ms",
                    "maximum_duration_ms",
                }
                or type(item.get("route_index")) is not int
                or type(item.get("minimum_duration_ms")) is not int
                or type(item.get("maximum_duration_ms")) is not int
                or item["route_index"] < 0
                or item["minimum_duration_ms"] < 1
                or item["maximum_duration_ms"] < item["minimum_duration_ms"]
                for item in route_policies
            )
            or len({item["route_index"] for item in route_policies})
            != len(route_policies)
        ):
            raise ValidationError("ProductionRun 结构化镜头 route 时长策略快照无效")
        common_minimum = max(
            item["minimum_duration_ms"] for item in route_policies
        )
        common_maximum = min(
            item["maximum_duration_ms"] for item in route_policies
        )
        frozen_minimum = duration_contract.get("minimum_duration_ms")
        frozen_default = duration_contract.get("default_duration_ms")
        frozen_maximum = duration_contract.get("maximum_duration_ms")
        if (
            type(frozen_minimum) is not int
            or type(frozen_default) is not int
            or type(frozen_maximum) is not int
            or common_minimum > common_maximum
            or not common_minimum <= frozen_minimum <= frozen_default
            or not frozen_default <= frozen_maximum <= common_maximum
        ):
            raise ValidationError("ProductionRun 结构化镜头时长范围快照无效")
        expected_input_values = {
            "episode_count": snapshot.get("episode_count"),
            "shots_per_episode": snapshot.get("shots_per_episode"),
            "episode_duration_ms": (
                int(snapshot.get("episode_duration_seconds") or 0) * 1_000
            ),
        }
        if any(
            duration_contract.get(key) != value
            for key, value in expected_input_values.items()
        ):
            raise ValidationError("ProductionRun 镜头时长合同与结构化输入不一致")
        config_values = (
            run.config_snapshot.get("values")
            if isinstance(run.config_snapshot, dict) else None
        )
        expected_config_values = {
            "production.video_shot_min_duration_ms": duration_contract.get(
                "minimum_duration_ms"
            ),
            "production.default_shot_duration_ms": duration_contract.get(
                "default_duration_ms"
            ),
            "production.video_shot_max_duration_ms": duration_contract.get(
                "maximum_duration_ms"
            ),
        }
        if not isinstance(config_values, dict) or any(
            config_values.get(key) != value
            for key, value in expected_config_values.items()
        ):
            raise ValidationError("ProductionRun 镜头时长合同与冻结运行配置不一致")
    recipe_definition_sha256 = snapshot.get("recipe_definition_sha256")
    if recipe_definition_sha256 not in (None, "") and re.fullmatch(
        r"[0-9a-f]{64}",
        str(recipe_definition_sha256).strip().casefold(),
    ) is None:
        raise ValidationError("ProductionRun 配方定义 hash 快照无效")


def _verify_stage_routing(stage: StageRun) -> dict[str, Any]:
    verify_json_contract(
        stage.routing,
        schema_version=stage.routing_schema_version,
        size_bytes=stage.routing_size_bytes,
        sha256=stage.routing_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    verify_json_contract(
        stage.routing_decision,
        schema_version=stage.routing_decision_schema_version,
        size_bytes=stage.routing_decision_size_bytes,
        sha256=stage.routing_decision_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    if not isinstance(stage.routing, dict) or stage.routing.get("schema_version") != 1:
        raise StageBlocked("routing_snapshot_invalid", "阶段 routing 快照无效")
    variant_count = stage.routing.get("variant_count")
    if type(variant_count) is not int or not 1 <= variant_count <= _variant_max_items():
        raise StageBlocked(
            "routing_snapshot_invalid",
            "阶段 routing 快照缺少有效 variant_count；不会回退代码默认值。",
        )
    return stage.routing


def _selected_episodes(run: ProductionRun) -> list[Episode]:
    requested = run.input_snapshot.get("episode_ids") if isinstance(run.input_snapshot, dict) else None
    ids = [str(item) for item in requested] if isinstance(requested, list) else []
    if not ids:
        storyboard_stage = run.stages.filter(
            status=StageRun.Status.SUCCEEDED,
            output_snapshot__formal_object_type="storyboard",
        ).order_by("-ordinal").first()
        if storyboard_stage is not None:
            canonical = storyboard_stage.output_snapshot.get("canonical") or {}
            ids = [
                str(item.get("id"))
                for item in canonical.get("episodes", [])
                if isinstance(item, dict) and item.get("id")
            ]
    queryset = Episode.objects.filter(project=run.project)
    if ids:
        episodes = list(queryset.filter(pk__in=ids).order_by("number", "id"))
        if {str(item.id) for item in episodes} != set(ids):
            raise StageBlocked("frozen_episode_selection_drift", "冻结选集中的 Episode 已缺失或不再属于项目")
        return episodes
    return list(queryset.order_by("number", "id"))


def _canonical_script(run: ProductionRun) -> dict[str, Any]:
    revision = run.script_revision
    if revision is None:
        return {"revision": None}
    return {
        "id": str(revision.id),
        "source_id": str(revision.source_id),
        "revision": revision.revision,
        "status": revision.status,
        "content_sha256": revision.content_sha256,
        "content_size_bytes": revision.content_size_bytes,
    }


def _canonical_existing_episode_selection(run: ProductionRun) -> dict[str, Any]:
    """Freeze the approved identities used by an existing-project run.

    Shot content remains editable until the explicit storyboard/manual gate;
    that later gate freezes the complete creative storyboard contract.
    """

    return {
        "project_id": str(run.project_id),
        "episodes": [
            {"id": str(item.id), "number": item.number}
            for item in _selected_episodes(run)
        ],
    }


def _canonical_bible(run: ProductionRun) -> dict[str, Any]:
    common = (
        "id", "slug", "name", "description", "visual_identity",
        "negative_constraints", "anchor_asset_id", "metadata", "is_locked",
    )
    typed = {
        "characters": (CharacterBible, common + (
            "stable_speaker_id", "role", "age_appearance", "face_identity",
            "wardrobe", "voice_profile", "personality",
        )),
        "scenes": (SceneBible, common + (
            "location", "time_of_day", "lighting", "color_palette", "ambient_sound",
        )),
        "props": (PropBible, common + ("material", "distinctive_marks", "continuity_state")),
    }
    result: dict[str, Any] = {}
    for key, (model, fields) in typed.items():
        rows = []
        for obj in model.objects.filter(project=run.project).prefetch_related("reference_assets").order_by("slug", "id"):
            row = {}
            for field in fields:
                value = getattr(obj, field)
                row[field] = str(value) if field in {"id", "anchor_asset_id"} and value else value
            row["reference_asset_ids"] = sorted(str(item) for item in obj.reference_assets.values_list("id", flat=True))
            rows.append(row)
        result[key] = rows
    return result


def _bible_binding_asset_fact(
    asset: Asset,
    *,
    project: SeriesProject,
    entity_type: str,
    entity,
) -> dict[str, Any]:
    """Return verified byte/provenance facts for one approved Bible binding."""

    source_value = asset.file.path if asset.file else asset.local_path
    if not source_value:
        raise StageBlocked(
            "bible_image_binding_asset_integrity",
            f"Bible 绑定素材 {asset.name} 缺少可执行本地文件。",
        )
    try:
        source = Path(source_value).resolve()
        observed_size = source.stat().st_size
        facts = verified_local_media_facts(
            source,
            expected_kind="image",
            max_bytes=observed_size,
        )
    except (OSError, ValueError, ProvenanceError) as exc:
        raise StageBlocked(
            "bible_image_binding_asset_integrity",
            f"Bible 绑定素材 {asset.name} 的真实图片字节不可验证。",
        ) from exc
    if (
        not asset.is_active
        or facts["sha256"] != str(asset.checksum or "")
        or facts["mime_type"] != str(asset.mime_type or "")
    ):
        raise StageBlocked(
            "bible_image_binding_asset_integrity",
            f"Bible 绑定素材 {asset.name} 的启用状态、MIME 或 checksum 已漂移。",
        )
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    result = {
        "asset_id": str(asset.id),
        "kind": asset.kind,
        "mime_type": facts["mime_type"],
        "size_bytes": facts["size_bytes"],
        "checksum": facts["sha256"],
        "generated_artifact_id": None,
        "prompt_artifact_id": None,
        "source_snapshot_sha256": "",
        "source_artifact_sha256": "",
        "source_manifest_sha256": "",
    }
    if metadata.get("generation_purpose") != BIBLE_IMAGE_GENERATION_PURPOSE:
        return result
    if (
        str(metadata.get("entity_type") or "") != entity_type
        or str(metadata.get("entity_id") or "") != str(entity.id)
        or str(metadata.get("source_sha256") or "") != facts["sha256"]
    ):
        raise StageBlocked(
            "bible_image_binding_asset_provenance",
            f"Bible 绑定素材 {asset.name} 的实体或来源 provenance 无效。",
        )
    artifact = GeneratedArtifact.objects.select_related(
        "generation_attempt",
        "generation_task__generated_workflow__prompt_artifact",
    ).filter(
        pk=metadata.get("generated_artifact_id"),
        asset=asset,
        production_run__project=project,
        stage_run__stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
        kind=GeneratedArtifact.Kind.IMAGE,
        role="bible_reference_candidate",
    ).first()
    if (
        artifact is None
        or artifact.sha256 != facts["sha256"]
        or artifact.manifest_metadata_sha256
        != str(metadata.get("source_manifest_sha256") or "")
    ):
        raise StageBlocked(
            "bible_image_binding_asset_provenance",
            f"Bible 绑定素材 {asset.name} 不再对应已审核 GeneratedArtifact。",
        )
    artifact_metadata = (
        artifact.manifest_metadata
        if isinstance(artifact.manifest_metadata, dict) else {}
    )
    prompt = None
    if artifact.generation_task_id:
        prompt = getattr(
            getattr(artifact.generation_task, "generated_workflow", None),
            "prompt_artifact", None,
        )
    if prompt is None:
        prompt = PromptArtifact.objects.select_related("prompt_profile_version").filter(
            pk=artifact_metadata.get("prompt_artifact_id"),
        ).first()
    if prompt is None:
        raise StageBlocked(
            "bible_image_binding_asset_provenance",
            f"Bible 绑定素材 {asset.name} 缺少冻结 PromptArtifact。",
        )
    _assert_bible_prompt_source_current(
        prompt=prompt,
        entity_type=entity_type,
        entity=entity,
        project=project,
    )
    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    result.update({
        "generated_artifact_id": str(artifact.id),
        "prompt_artifact_id": str(prompt.id),
        "source_snapshot_sha256": str(
            options.get("source_snapshot_sha256") or ""
        ),
        "source_artifact_sha256": artifact.sha256,
        "source_manifest_sha256": artifact.manifest_metadata_sha256,
    })
    return result


def _canonical_bible_image_bindings(run: ProductionRun) -> dict[str, Any]:
    bindings = []
    for entity_type, entity in _bible_entities(run.project):
        anchor = (
            Asset.objects.get(pk=entity.anchor_asset_id)
            if entity.anchor_asset_id else None
        )
        references = list(entity.reference_assets.all().order_by("id"))
        bindings.append({
            "entity_type": entity_type,
            "entity_id": str(entity.id),
            "anchor_asset_id": str(anchor.id) if anchor else None,
            "reference_asset_ids": [str(item.id) for item in references],
            "anchor_asset": (
                _bible_binding_asset_fact(
                    anchor,
                    project=run.project,
                    entity_type=entity_type,
                    entity=entity,
                ) if anchor else None
            ),
            "reference_assets": [
                _bible_binding_asset_fact(
                    asset,
                    project=run.project,
                    entity_type=entity_type,
                    entity=entity,
                )
                for asset in references
            ],
        })
    return {"bindings": sorted(bindings, key=lambda item: (
        item["entity_type"], item["entity_id"],
    ))}


def _canonical_storyboard(run: ProductionRun) -> dict[str, Any]:
    episodes = _selected_episodes(run)
    episode_ids = [item.id for item in episodes]
    fetched = {
        item.id: item
        for item in Episode.objects.filter(pk__in=episode_ids).prefetch_related(
            "shots__characters", "shots__props", "shots__reference_assets",
        )
    }
    rows = []
    for episode_stub in episodes:
        episode = fetched[episode_stub.id]
        shots = []
        for shot in episode.shots.all().order_by("number", "id"):
            shot_characters = list(shot.characters.all())
            conditioning_mode_capability = (
                _selected_conditioning_mode_contract_for_shot(
                    run,
                    shot=shot,
                )
            )
            shots.append({
                "id": str(shot.id),
                "number": shot.number,
                "title": shot.title,
                "narrative": shot.narrative,
                "detailed_description": shot.detailed_description,
                "camera": shot.camera,
                "action": shot.action,
                "dialogue": shot.dialogue,
                "sound_effects": shot.sound_effects,
                "music": shot.music,
                "duration_ms": shot.duration_ms,
                "media_type": shot.media_type,
                "conditioning_mode": shot.conditioning_mode,
                "conditioning_mode_capability": conditioning_mode_capability,
                "include_audio": shot.include_audio,
                "scene_id": str(shot.scene_id) if shot.scene_id else None,
                # ``character_ids`` keeps the complete speaker/story relation.
                # Visual conditioning is a separate explicit projection so a
                # voice-only speaker remains available to dialogue without
                # acquiring a Picture/Subject binding.
                "character_ids": sorted(str(item.id) for item in shot_characters),
                "visual_reference_character_ids": sorted(
                    str(item.id)
                    for item in visual_reference_characters(shot_characters)
                ),
                "prop_ids": sorted(str(item) for item in shot.props.values_list("id", flat=True)),
                "reference_asset_ids": sorted(str(item) for item in shot.reference_assets.values_list("id", flat=True)),
                "reference_bindings": shot.reference_bindings,
                "requires_first_frame_continuity": shot.requires_first_frame_continuity,
                "requires_last_frame_continuity": shot.requires_last_frame_continuity,
                "continuity_notes": shot.continuity_notes,
                "metadata": shot.metadata,
            })
        rows.append({
            "id": str(episode.id),
            "number": episode.number,
            "title": episode.title,
            "synopsis": episode.synopsis,
            "script": episode.script,
            "status": episode.status,
            "continuity_from_previous": episode.continuity_from_previous,
            "continuity_to_next": episode.continuity_to_next,
            "prompt_settings": episode.prompt_settings,
            "shots": shots,
        })
    return {"episodes": rows}


def _canonical_prompts(run: ProductionRun) -> dict[str, Any]:
    return {"prompts": [
        {
            "id": str(item.id),
            "episode_id": str(item.episode_id) if item.episode_id else None,
            "shot_id": str(item.shot_id) if item.shot_id else None,
            "prompt_type": item.prompt_type,
            "conditioning_mode": item.conditioning_mode,
            "content_hash": item.content_hash,
        }
        for item in _prompt_rows_for_run(run)
    ]}


_PROMPT_NORMALIZATION_CANDIDATE_IDENTITY_KEYS = (
    "request_id",
    "scope_type",
    "scope_id",
    "field_key",
    "source_hash",
    "context_hash",
)
_PROMPT_PROFILE_TARGET_IDENTITY_KEYS = (
    "target_kind",
    "shot_id",
    "profile_selection_sha256",
    "prompt_profile_version_id",
    "profile_field_key",
    "profile_source_selector",
    "generation_mode",
    "generation_output_semantics",
    "generation_source_selector",
    "generation_source_scope_type",
    "generation_source_scope_id",
    "generation_source_field_key",
)


def _verified_manual_prompt_normalization_targets(
    run: ProductionRun,
    review_payload: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return server-frozen target identities for a manual review snapshot.

    Candidate text is an editable presentation object. Target identity comes
    only from the checksum-verified frozen input, and every Profile identity
    field must be present and match exactly.
    """

    review_origin = str(review_payload.get("review_origin") or "")
    if review_origin != "human_only_recipe":
        return {}
    if (
        review_payload.get("attempt_id") is not None
        or review_payload.get("submission_semantics")
        != "human_only_no_submission"
        or str(review_payload.get("provider") or "")
        or str(review_payload.get("model") or "")
    ):
        raise ValidationError("script_normalize 人工候选的无提交身份无效")
    frozen_input = review_payload.get("frozen_input")
    if not isinstance(frozen_input, dict):
        raise ValidationError("script_normalize 人工候选缺少冻结输入")
    try:
        frozen_contract = verify_json_contract(
            frozen_input,
            schema_version=review_payload.get("frozen_input_schema_version"),
            size_bytes=review_payload.get("frozen_input_size_bytes"),
            sha256=review_payload.get("frozen_input_sha256"),
            max_bytes=_text_snapshot_max_bytes(),
        )
    except ValidationError as exc:
        raise ValidationError("script_normalize 人工候选冻结输入 checksum 无效") from exc
    payload = frozen_input.get("payload")
    targets = frozen_input.get("targets")
    if not isinstance(payload, dict) or not isinstance(targets, list):
        raise ValidationError("script_normalize 人工候选冻结输入结构无效")
    try:
        validate_prompt_normalization_run_contract(payload)
    except PromptNormalizationContractError as exc:
        raise ValidationError("script_normalize 人工候选冻结字段合同无效") from exc

    frozen_by_request_id: dict[str, dict[str, Any]] = {}
    for target in targets:
        request_id = str(target.get("request_id") or "") if isinstance(target, dict) else ""
        if not request_id or request_id in frozen_by_request_id:
            raise ValidationError("script_normalize 冻结目标 request_id 缺失或重复")
        frozen_by_request_id[request_id] = target
    candidate_request_ids = [
        str(candidate.get("request_id") or "")
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    if (
        len(candidate_request_ids) != len(candidates)
        or any(not request_id for request_id in candidate_request_ids)
        or len(set(candidate_request_ids)) != len(candidate_request_ids)
        or set(candidate_request_ids) != set(frozen_by_request_id)
    ):
        raise ValidationError("script_normalize 人工候选与冻结目标集合不一致")

    for candidate in candidates:
        request_id = str(candidate.get("request_id") or "")
        frozen_target = frozen_by_request_id[request_id]
        if any(
            str(candidate.get(key) or "") != str(frozen_target.get(key) or "")
            for key in _PROMPT_NORMALIZATION_CANDIDATE_IDENTITY_KEYS
        ):
            raise ValidationError("script_normalize 人工候选基础身份与冻结目标不一致")
        try:
            generation_rule = target_generation_rule_for_request(
                payload,
                request_id=request_id,
            )
        except PromptNormalizationContractError as exc:
            raise ValidationError(
                "script_normalize 人工候选无法解析冻结字段规则",
            ) from exc
        if generation_rule is None:
            if str(candidate.get("target_kind") or ""):
                raise ValidationError("script_normalize 普通候选伪造了 Profile 目标身份")
            continue
        if frozen_target.get("target_kind") != "prompt_profile_field":
            raise ValidationError("script_normalize Profile 冻结目标类型无效")
        rule_pairs = {
            "shot_id": "shot_id",
            "profile_selection_sha256": "profile_selection_sha256",
            "profile_field_key": "profile_field_key",
            "profile_source_selector": "profile_source_selector",
            "generation_mode": "generation_mode",
            "generation_output_semantics": "generation_output_semantics",
            "generation_source_selector": "generation_source_selector",
        }
        if any(
            str(frozen_target.get(target_key) or "")
            != str(generation_rule.get(rule_key) or "")
            for target_key, rule_key in rule_pairs.items()
        ) or not str(frozen_target.get("prompt_profile_version_id") or ""):
            raise ValidationError("script_normalize Profile 冻结目标与字段生成合同不一致")
        missing_candidate_metadata = [
            key for key in _PROMPT_PROFILE_TARGET_IDENTITY_KEYS
            if key not in candidate
        ]
        if missing_candidate_metadata:
            raise ValidationError("script_normalize 人工候选缺少冻结的 Profile 目标元数据")
        if any(
            key in candidate
            and str(candidate.get(key) or "") != str(frozen_target.get(key) or "")
            for key in _PROMPT_PROFILE_TARGET_IDENTITY_KEYS
        ):
            raise ValidationError("script_normalize 人工候选 Profile 身份与冻结目标不一致")

    evidence = {
        "schema_version": 1,
        "review_origin": review_origin,
        "frozen_input_sha256": frozen_contract.sha256,
        "candidates": candidates,
    }
    if review_payload.get("result_sha256") != _sha256(evidence):
        raise ValidationError("script_normalize 人工候选证据 checksum 无效")
    return frozen_by_request_id


def _canonical_prompt_translations(run: ProductionRun) -> dict[str, Any]:
    normalize_stage = run.stages.filter(
        stage_key="script_normalize",
        status=StageRun.Status.SUCCEEDED,
    ).first()
    candidates: list[dict[str, Any]] = []
    review_payload: dict[str, Any] | None = None
    if normalize_stage is not None:
        snapshot = _verify_stage_snapshot(normalize_stage, "output_snapshot")
        review_payload = snapshot.get("review_payload")
        if isinstance(review_payload, dict) and isinstance(review_payload.get("candidates"), list):
            candidates = review_payload["candidates"]
    rows = []
    profile_field_values: list[dict[str, Any]] = []
    attempt_id = (
        str(review_payload.get("attempt_id") or "")
        if isinstance(review_payload, dict)
        else ""
    )
    manual_targets = (
        _verified_manual_prompt_normalization_targets(run, review_payload, candidates)
        if isinstance(review_payload, dict)
        else {}
    )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_attempt_id = str(
            candidate.get("generation_attempt_id") or attempt_id or "",
        )
        entry = PromptFieldTranslation.objects.filter(
            project=run.project,
            scope_type=candidate.get("scope_type"),
            scope_id=candidate.get("scope_id"),
            field_key=candidate.get("field_key"),
            status=PromptFieldTranslation.Status.CONFIRMED,
            **(
                {"origin_generation_attempt_id": candidate_attempt_id}
                if candidate_attempt_id else {}
            ),
        ).first()
        rows.append({
            "scope_type": str(candidate.get("scope_type") or ""),
            "scope_id": str(candidate.get("scope_id") or ""),
            "field_key": str(candidate.get("field_key") or ""),
            "entry_id": str(entry.id) if entry else None,
            "revision": entry.revision if entry else None,
            "source_hash": entry.source_hash if entry else "",
            "translated_text_sha256": (
                source_text_hash(entry.translated_text) if entry else ""
            ),
            "translation_context_hash": str(
                (entry.runtime_metadata or {}).get("translation_context_hash") or ""
            ) if entry else "",
            "origin_generation_attempt_id": (
                str(entry.origin_generation_attempt_id)
                if entry and entry.origin_generation_attempt_id else None
            ),
        })
        target_identity = manual_targets.get(
            str(candidate.get("request_id") or ""),
            candidate,
        )
        if target_identity.get("target_kind") == "prompt_profile_field":
            metadata = entry.runtime_metadata if entry and isinstance(
                entry.runtime_metadata,
                dict,
            ) else {}
            expected = {
                "shot_id": str(target_identity.get("shot_id") or ""),
                "profile_selection_sha256": str(
                    target_identity.get("profile_selection_sha256") or ""
                ),
                "prompt_profile_version_id": str(
                    target_identity.get("prompt_profile_version_id") or ""
                ),
                "profile_field_key": str(
                    target_identity.get("profile_field_key") or ""
                ),
                "source_selector": str(
                    target_identity.get("profile_source_selector") or ""
                ),
                "generation_mode": str(
                    target_identity.get("generation_mode") or ""
                ),
                "generation_source_selector": str(
                    target_identity.get("generation_source_selector") or ""
                ),
                "output_semantics": str(
                    target_identity.get("generation_output_semantics") or ""
                ),
            }
            metadata_expected = {
                "shot_id": expected["shot_id"],
                "profile_selection_sha256": expected["profile_selection_sha256"],
                "prompt_profile_version_id": expected["prompt_profile_version_id"],
                "profile_field_key": expected["profile_field_key"],
                "profile_source_selector": expected["source_selector"],
                "generation_mode": expected["generation_mode"],
                "generation_source_selector": expected[
                    "generation_source_selector"
                ],
                "generation_output_semantics": expected["output_semantics"],
            }
            if entry is None or any(
                str(metadata.get(key) or "") != value
                for key, value in metadata_expected.items()
            ):
                raise ValidationError(
                    "已审核 PromptProfile 字段覆盖与冻结候选身份不一致",
                )
            profile_field_values.append({
                **expected,
                "value": entry.translated_text,
                "value_sha256": source_text_hash(entry.translated_text),
                "request_id": str(target_identity.get("request_id") or ""),
            })
    return {
        "translations": rows,
        "profile_field_values": profile_field_values,
    }


def _reviewed_profile_fields_by_shot(
    run: ProductionRun,
) -> dict[str, list[dict[str, Any]]] | None:
    normalize_stage_exists = run.stages.filter(stage_key="script_normalize").exists()
    if not normalize_stage_exists:
        return None
    review_stage = run.stages.filter(
        stage_key="script_normalize_review",
        status=StageRun.Status.SUCCEEDED,
    ).first()
    if review_stage is None:
        raise StageBlocked(
            "prompt_profile_field_review_missing",
            "字段生成结果尚未完成审核，不能进入提示词编译。",
        )
    snapshot = _verify_stage_snapshot(review_stage, "output_snapshot")
    if snapshot.get("formal_object_type") != "prompt_translation_manifest":
        raise StageBlocked(
            "prompt_profile_field_review_invalid",
            "字段生成审核快照类型无效。",
        )
    canonical = snapshot.get("canonical")
    rows = canonical.get("profile_field_values") if isinstance(canonical, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise StageBlocked(
            "prompt_profile_field_review_invalid",
            "字段生成审核快照缺少可验证的 Profile 字段值。",
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        shot_id = str(row.get("shot_id") or "")
        if not shot_id:
            raise StageBlocked(
                "prompt_profile_field_review_invalid",
                "字段生成审核快照缺少 shot_id。",
            )
        grouped.setdefault(shot_id, []).append(deepcopy(row))
    return grouped


def _canonical_visual_rules(run: ProductionRun) -> dict[str, Any]:
    project = SeriesProject.objects.get(pk=run.project_id)
    snapshot = current_rules_snapshot(project)
    return {
        "project_id": str(run.project_id),
        "script_revision": _canonical_script(run),
        "rules": snapshot["rules"],
        "rules_sha256": snapshot["rules_sha256"],
    }


def _canonical_media(run: ProductionRun) -> dict[str, Any]:
    return {"artifacts": [
        {
            "id": str(item.id),
            "variant_group": item.variant_group,
            "variant_index": item.variant_index,
            "status": item.status,
            "kind": item.kind,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "object_key": item.object_key,
        }
        for item in GeneratedArtifact.objects.filter(
            production_run=run,
            stage_run__stage_key="media_generation",
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        ).order_by("variant_group", "variant_index", "id")
    ]}


def _canonical_bible_image_candidates(
    run: ProductionRun,
    stage: StageRun,
) -> dict[str, Any]:
    current_artifacts = list(GeneratedArtifact.objects.filter(
        production_run=run,
        stage_run=stage,
        kind=GeneratedArtifact.Kind.IMAGE,
        role="bible_reference_candidate",
    ).select_related(
        "generation_attempt__prompt_profile_version",
        "generation_task__generated_workflow__prompt_artifact",
        "asset",
    ).order_by("variant_group", "variant_index", "id"))
    historical_artifacts = GeneratedArtifact.objects.filter(
        production_run__project=run.project,
        stage_run__stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
        created_at__lt=stage.created_at,
        kind=GeneratedArtifact.Kind.IMAGE,
        role="bible_reference_candidate",
        status__in=(
            GeneratedArtifact.Status.AVAILABLE,
            GeneratedArtifact.Status.SELECTED,
        ),
    ).exclude(pk__in=[item.id for item in current_artifacts]).select_related(
        "generation_attempt__prompt_profile_version",
        "generation_task__generated_workflow__prompt_artifact",
        "asset",
    ).order_by("created_at", "id")
    entity_by_key = {
        (entity_type, str(entity.id)): entity
        for entity_type, entity in _bible_entities(run.project)
    }
    artifacts: list[tuple[GeneratedArtifact, bool]] = [
        (item, False) for item in current_artifacts
    ]
    for artifact in current_artifacts:
        try:
            _managed_generated_artifact_facts(artifact)
        except ReleaseBundleError as exc:
            raise ProductionRunConflict(
                "bible_image_candidate_media_integrity_invalid",
                "本 Run Bible 候选真实文件与冻结清单不一致，禁止进入审核。",
            ) from exc
    for artifact in historical_artifacts:
        metadata = (
            artifact.manifest_metadata
            if isinstance(artifact.manifest_metadata, dict) else {}
        )
        try:
            verify_json_contract(
                metadata,
                schema_version=artifact.manifest_schema_version,
                size_bytes=artifact.manifest_metadata_size_bytes,
                sha256=artifact.manifest_metadata_sha256,
                max_bytes=_stage_snapshot_max_bytes(),
            )
            target = validate_bible_prompt_target(metadata.get("target"))
            identity = parse_bible_candidate_unit_key(
                artifact.generation_attempt.unit_key
                if artifact.generation_attempt else "",
            )
            if (
                target["entity_type"] != identity["entity_type"]
                or target["entity_id"] != identity["entity_id"]
                or artifact.variant_group != identity["variant_group"]
            ):
                raise ValidationError("历史 Bible 候选 target 与 attempt identity 不一致")
            entity = entity_by_key.get((target["entity_type"], target["entity_id"]))
            prompt = None
            if artifact.generation_task_id:
                prompt = getattr(
                    getattr(artifact.generation_task, "generated_workflow", None),
                    "prompt_artifact", None,
                )
            if prompt is None:
                prompt = PromptArtifact.objects.select_related(
                    "prompt_profile_version",
                ).filter(pk=metadata.get("prompt_artifact_id")).first()
            if entity is None or prompt is None:
                raise ValidationError("历史 Bible 候选缺少当前实体或 PromptArtifact")
            _assert_bible_prompt_source_current(
                prompt=prompt,
                entity_type=target["entity_type"],
                entity=entity,
                project=run.project,
            )
            _assert_bible_candidate_provenance(
                artifact=artifact,
                attempt=artifact.generation_attempt,
                prompt=prompt,
                project=run.project,
            )
            _managed_generated_artifact_facts(artifact)
        except (ValidationError, ProductionRunConflict, ReleaseBundleError):
            # Invalid/stale history remains visible through the read-only
            # candidate endpoint, but never enters a selectable review snapshot.
            continue
        artifacts.append((artifact, True))
    entities: dict[tuple[str, str], dict[str, Any]] = {}
    for artifact, is_historical in artifacts:
        metadata = artifact.manifest_metadata if isinstance(artifact.manifest_metadata, dict) else {}
        target = validate_bible_prompt_target(metadata.get("target"))
        entity = entity_by_key.get((target["entity_type"], target["entity_id"]))
        attempt = artifact.generation_attempt
        prompt = None
        if artifact.generation_task_id:
            prompt = getattr(
                getattr(artifact.generation_task, "generated_workflow", None),
                "prompt_artifact", None,
            )
        if prompt is None:
            prompt = PromptArtifact.objects.select_related(
                "prompt_profile_version",
            ).filter(pk=metadata.get("prompt_artifact_id")).first()
        if entity is None or attempt is None or prompt is None:
            raise ValidationError("Bible 候选缺少实体、Attempt 或 Prompt provenance")
        _assert_bible_candidate_provenance(
            artifact=artifact,
            attempt=attempt,
            prompt=prompt,
            project=run.project,
        )
        key = (target["entity_type"], target["entity_id"])
        row = entities.setdefault(key, {
            "entity_type": target["entity_type"],
            "entity_id": target["entity_id"],
            "entity_name": target["entity_name"],
            "source_snapshot_sha256": target["source_snapshot_sha256"],
            "candidates": [],
        })
        row["candidates"].append({
            "artifact_id": str(artifact.id),
            "variant_group": artifact.variant_group,
            "variant_index": artifact.variant_index,
            # Eligibility is frozen by inclusion; the mutable selected flag is
            # intentionally normalized so another Run reusing the same history
            # cannot invalidate this immutable candidate manifest.
            "status": GeneratedArtifact.Status.AVAILABLE,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "mime_type": artifact.mime_type,
            "attempt_id": str(attempt.id) if attempt else None,
            "attempt_number": attempt.attempt_number if attempt else None,
            "generation_task_id": (
                str(artifact.generation_task_id) if artifact.generation_task_id else None
            ),
            "prompt_artifact_id": metadata.get("prompt_artifact_id"),
            "prompt_profile_version_id": metadata.get("prompt_profile_version_id"),
            "prompt_profile_binding_key": metadata.get("prompt_profile_binding_key", ""),
            "prompt_profile_contract_sha256": metadata.get(
                "prompt_profile_contract_sha256", "",
            ),
            "source_snapshot_sha256": target["source_snapshot_sha256"],
            "origin_production_run_id": str(artifact.production_run_id),
            "origin_stage_run_id": str(artifact.stage_run_id),
            "historical": is_historical,
        })
    return {
        "generation_stage_id": str(stage.id),
        "generation_stage_attempt_count": stage.attempt_count,
        "entities": [entities[key] for key in sorted(entities)],
    }


def _formal_snapshot(run: ProductionRun, stage: StageRun, output_reference: str) -> dict[str, Any]:
    formal_type = ""
    canonical: dict[str, Any] = {}
    if stage.stage_key == "script_input" and stage.mode == "manual":
        formal_type, canonical = (
            "existing_episode_selection",
            _canonical_existing_episode_selection(run),
        )
    elif stage.stage_key in {"script_input", "script_review", "script_generate", "script_structure"}:
        formal_type, canonical = "script", _canonical_script(run)
    elif stage.stage_key == "script_normalize_review":
        formal_type, canonical = "prompt_translation_manifest", _canonical_prompt_translations(run)
    elif stage.stage_key == "visual_rules_review":
        formal_type, canonical = "visual_rules", _canonical_visual_rules(run)
    elif stage.stage_key in {"bible", "bible_generation", "bible_review"}:
        formal_type, canonical = "bible", _canonical_bible(run)
    elif stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
        formal_type, canonical = (
            "bible_image_candidate_manifest",
            _canonical_bible_image_candidates(run, stage),
        )
    elif stage.stage_key == BIBLE_IMAGE_REVIEW_STAGE_KEY:
        existing = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
        if existing.get("kind") != "bible_image_bindings":
            raise ValidationError("Bible 参考图审核缺少冻结绑定结果")
        formal_type, canonical = "bible_image_bindings", deepcopy(existing)
    elif stage.stage_key in {"storyboard", "storyboard_review"}:
        formal_type, canonical = "storyboard", _canonical_storyboard(run)
    elif stage.stage_key == "prompt_compile":
        formal_type, canonical = "prompt_manifest", _canonical_prompts(run)
    elif stage.stage_key in {"media_generation", "media_review", "artifact_selection"}:
        formal_type, canonical = "media_manifest", _canonical_media(run)
    payload = {
        "schema_version": 1,
        "stage_key": stage.stage_key,
        "output_reference": str(output_reference),
        "formal_object_type": formal_type,
        "canonical": canonical,
        "canonical_sha256": _sha256(canonical),
    }
    existing = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
    if existing.get("kind") in {
        "generated_script_preview",
        "structured_script_preview",
        "script_review_decision",
        "script_bible_candidates",
        "prompt_normalization_candidates",
        "visual_rules_candidates",
        "visual_rules_review_decision",
        "bible_image_bindings",
    }:
        payload["review_payload"] = existing
    return payload


def _save_stage_snapshot(stage: StageRun, field: str, value: dict[str, Any]) -> str:
    contract = json_contract(value, max_bytes=_stage_snapshot_max_bytes())
    setattr(stage, field, contract.value)
    setattr(stage, f"{field}_schema_version", contract.schema_version)
    setattr(stage, f"{field}_size_bytes", contract.size_bytes)
    setattr(stage, f"{field}_sha256", contract.sha256)
    stage.save(update_fields=(
        field, f"{field}_schema_version", f"{field}_size_bytes",
        f"{field}_sha256", "updated_at",
    ))
    return contract.sha256


def _verify_stage_snapshot(stage: StageRun, field: str) -> dict[str, Any]:
    value = getattr(stage, field)
    verify_json_contract(
        value,
        schema_version=getattr(stage, f"{field}_schema_version"),
        size_bytes=getattr(stage, f"{field}_size_bytes"),
        sha256=getattr(stage, f"{field}_sha256"),
        max_bytes=_stage_snapshot_max_bytes(),
    )
    return value


def _storyboard_drift_projection(value: dict[str, Any]) -> dict[str, Any]:
    episodes = value.get("episodes")
    if not isinstance(episodes, list):
        return value
    return {
        **value,
        "episodes": [
            {key: child for key, child in episode.items() if key != "status"}
            if isinstance(episode, dict) else episode
            for episode in episodes
        ],
    }


def _bible_creative_drift_projection(value: dict[str, Any]) -> dict[str, Any]:
    """Exclude the separately approved Asset binding contract from creative Bible drift."""

    projected: dict[str, Any] = {}
    for section in ("characters", "scenes", "props"):
        rows = value.get(section)
        if not isinstance(rows, list):
            return value
        projected[section] = [
            {
                key: child
                for key, child in row.items()
                if key not in {"anchor_asset_id", "reference_asset_ids"}
            }
            if isinstance(row, dict) else row
            for row in rows
        ]
    return projected


def _bible_binding_drift_projection(value: dict[str, Any]) -> dict[str, Any]:
    rows = value.get("bindings")
    if not isinstance(rows, list):
        return value
    return {
        "bindings": sorted(({
            "entity_type": str(row.get("entity_type") or ""),
            "entity_id": str(row.get("entity_id") or ""),
            "anchor_asset": row.get("anchor_asset"),
            "reference_assets": sorted(
                (item for item in (row.get("reference_assets") or []) if isinstance(item, dict)),
                key=lambda item: str(item.get("asset_id") or ""),
            ),
        } for row in rows if isinstance(row, dict)), key=lambda item: (
            item["entity_type"], item["entity_id"],
        )),
    }


def _assert_frozen_formal_objects(run: ProductionRun) -> None:
    factories = {
        "script": _canonical_script,
        "existing_episode_selection": _canonical_existing_episode_selection,
        "bible": _canonical_bible,
        "storyboard": _canonical_storyboard,
        "prompt_manifest": _canonical_prompts,
        "prompt_translation_manifest": _canonical_prompt_translations,
        "visual_rules": _canonical_visual_rules,
        "media_manifest": _canonical_media,
        "bible_image_bindings": _canonical_bible_image_bindings,
    }
    latest: dict[str, StageRun] = {}
    for item in run.stages.filter(status=StageRun.Status.SUCCEEDED).order_by("ordinal"):
        snapshot = _verify_stage_snapshot(item, "output_snapshot")
        formal_type = str(snapshot.get("formal_object_type") or "")
        if formal_type in factories:
            latest[formal_type] = item
    for formal_type, item in latest.items():
        snapshot = item.output_snapshot
        frozen = snapshot.get("canonical")
        if not isinstance(frozen, dict) or _sha256(frozen) != snapshot.get("canonical_sha256"):
            raise StageBlocked(
                f"{formal_type}_snapshot_invalid",
                f"已审批/完成的 {formal_type} 正式对象快照无效。",
            )
        current = factories[formal_type](run)
        if formal_type == "storyboard":
            # Episode.status is a downstream lifecycle projection. Assembly
            # completion legitimately changes it from draft to completed, so it
            # must not participate in the immutable creative-input comparison.
            # Project both the historical canonical payload and current facts so
            # snapshots written before this rule remain auditable and resumable.
            frozen_for_drift = _storyboard_drift_projection(frozen)
            current_for_drift = _storyboard_drift_projection(current)
        elif formal_type == "bible":
            frozen_for_drift = _bible_creative_drift_projection(frozen)
            current_for_drift = _bible_creative_drift_projection(current)
        elif formal_type == "bible_image_bindings":
            frozen_for_drift = _bible_binding_drift_projection(frozen)
            current_for_drift = _bible_binding_drift_projection(current)
        else:
            frozen_for_drift = frozen
            current_for_drift = current
        if _sha256(current_for_drift) != _sha256(frozen_for_drift):
            raise StageBlocked(
                f"{formal_type}_snapshot_drift",
                f"已审批/完成的 {formal_type} 正式对象发生漂移；请新建 ProductionRun。",
            )


def _assert_storyboard_review_source(
    run: ProductionRun,
    *,
    expected_source_snapshot_sha256: str,
) -> None:
    source_stage = run.stages.select_for_update().filter(
        stage_key="storyboard",
        status=StageRun.Status.SUCCEEDED,
    ).order_by("-ordinal").first()
    if source_stage is None:
        raise ProductionRunConflict(
            {"detail": "缺少已成功的 storyboard 来源快照，不能批准分镜。"},
            code="storyboard_snapshot_invalid",
        )
    snapshot = _verify_stage_snapshot(source_stage, "output_snapshot")
    public_snapshot_sha256 = json_contract(
        redact_sensitive_bindings(snapshot),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    if expected_source_snapshot_sha256 != public_snapshot_sha256:
        raise ProductionRunConflict(
            {"detail": "页面展示的分镜来源快照已过期，请刷新后重新核对。"},
            code="storyboard_snapshot_stale",
        )
    frozen = snapshot.get("canonical")
    if (
        snapshot.get("formal_object_type") != "storyboard"
        or not isinstance(frozen, dict)
        or _sha256(frozen) != snapshot.get("canonical_sha256")
    ):
        raise ProductionRunConflict(
            {"detail": "冻结的 storyboard 正式对象快照无效。"},
            code="storyboard_snapshot_invalid",
        )
    current = _canonical_storyboard(run)
    if _sha256(_storyboard_drift_projection(current)) != _sha256(_storyboard_drift_projection(frozen)):
        raise ProductionRunConflict(
            {"detail": "分镜正式对象已在审核证据生成后变化；请重新生成审核证据后再批准。"},
            code="storyboard_snapshot_drift",
        )


def _reference_binding_asset_ids(bindings: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return every bound Asset plus the image subset governed by Bible review."""

    all_ids: set[str] = set()
    bible_image_ids: set[str] = set()
    for key in (
        "first_frame_asset_id", "last_frame_asset_id",
        "first_frame_reference_asset_id",
    ):
        if bindings.get(key):
            value = str(bindings[key])
            all_ids.add(value)
            bible_image_ids.add(value)
    for key in ("characters", "props"):
        for item in bindings.get(key) or []:
            if isinstance(item, dict) and item.get("asset_id"):
                value = str(item["asset_id"])
                all_ids.add(value)
                bible_image_ids.add(value)
    scene = bindings.get("scene")
    if isinstance(scene, dict) and scene.get("asset_id"):
        value = str(scene["asset_id"])
        all_ids.add(value)
        bible_image_ids.add(value)
    for item in bindings.get("videos") or []:
        if not isinstance(item, dict):
            continue
        for key in ("asset_id", "audio_asset_id"):
            if item.get(key):
                all_ids.add(str(item[key]))
    for item in bindings.get("audios") or []:
        if isinstance(item, dict) and item.get("asset_id"):
            all_ids.add(str(item["asset_id"]))
    return all_ids, bible_image_ids


def _lock_relation_rows(manager) -> None:
    list(manager.through.objects.select_for_update().filter(**{
        f"{manager.source_field_name}_id": manager.instance.pk,
    }).values_list("pk", flat=True))


def _approve_storyboard_reference_bindings(
    run: ProductionRun,
    *,
    user,
    selections: list[dict[str, Any]],
    expected_source_snapshot_sha256: str,
) -> None:
    """Persist the explicit post-Bible per-Shot reference contract."""

    has_formal_bible_images = run.stages.filter(
        stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
    ).exists()
    selected_episode_ids = [item.id for item in _selected_episodes(run)]
    if (
        not has_formal_bible_images
        and _run_requires_entity_reference_bible(run)
    ):
        raise ValidationError(
            "当前条件模式的 storyboard_review 必须先完成 Bible 参考图生成/审核；"
            "仅绑定动态参考视频不能形成实体一致性闭包",
        )
    if not has_formal_bible_images and not selections:
        # The ordinary storyboard review still protects the creative snapshot.
        # Reference bindings are optional on runs without the formal Bible image
        # pair, but an empty binding payload must never bypass drift detection.
        _assert_storyboard_review_source(
            run,
            expected_source_snapshot_sha256=expected_source_snapshot_sha256,
        )
        return
    locked_project = SeriesProject.objects.select_for_update().select_related(
        "owner", "style",
    ).get(pk=run.project_id)
    run.project = locked_project
    episode_ids = selected_episode_ids
    list(Episode.objects.select_for_update().filter(
        pk__in=episode_ids,
    ).values_list("id", flat=True))
    shots = list(
        Shot.objects.select_for_update().select_related("episode", "scene")
        .filter(episode_id__in=episode_ids)
        .order_by("episode__number", "number", "id")
    )
    expected_ids = {str(shot.id) for shot in shots}
    provided_ids = [str(item.get("shot_id") or "") for item in selections]
    if (
        len(provided_ids) != len(set(provided_ids))
        or (has_formal_bible_images and set(provided_ids) != expected_ids)
        or set(provided_ids) - expected_ids
    ):
        raise ValidationError(
            "Bible 生产的 storyboard_review 必须且只能逐 Shot 完整提交 reference_bindings",
        )

    for shot in shots:
        for manager in (shot.characters, shot.props, shot.reference_assets):
            _lock_relation_rows(manager)
    bible_entities: list[Any] = []
    for model in (CharacterBible, SceneBible, PropBible):
        bible_entities.extend(model.objects.select_for_update().filter(
            project=locked_project,
        ).order_by("id"))
    bible_asset_ids: set[str] = set()
    for entity in bible_entities:
        _lock_bible_reference_rows(entity)
        bible_asset_ids.update(
            str(value) for value in entity.reference_assets.values_list("id", flat=True)
        )
        if entity.anchor_asset_id:
            bible_asset_ids.add(str(entity.anchor_asset_id))

    raw_by_shot = {
        str(item["shot_id"]): dict(item.get("reference_bindings") or {})
        for item in selections
    }
    requested_asset_ids: set[str] = set()
    requested_bible_image_ids: set[str] = set()
    for bindings in raw_by_shot.values():
        all_ids, image_ids = _reference_binding_asset_ids(bindings)
        requested_asset_ids.update(all_ids)
        requested_bible_image_ids.update(image_ids)
    locked_assets = {
        str(asset.id): asset
        for asset in Asset.objects.select_for_update().filter(
            pk__in=(bible_asset_ids | requested_asset_ids),
        )
    }
    if set(locked_assets) != bible_asset_ids | requested_asset_ids:
        raise ValidationError("逐镜 reference_bindings 引用了不存在的 Asset")

    _assert_storyboard_review_source(
        run,
        expected_source_snapshot_sha256=expected_source_snapshot_sha256,
    )

    if has_formal_bible_images:
        review_stage = run.stages.select_for_update().filter(
            stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if review_stage is None:
            raise ValidationError("storyboard_review 前缺少已批准的 bible_image_review")
        try:
            _assert_frozen_formal_objects(run)
        except StageBlocked as exc:
            raise ProductionRunConflict(
                {"detail": exc.reason},
                code=exc.code,
            ) from exc
        review_snapshot = _verify_stage_snapshot(review_stage, "output_snapshot")
        review_canonical = review_snapshot.get("canonical")
        generation_stage = run.stages.select_for_update().filter(
            stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if (
            review_snapshot.get("formal_object_type") != "bible_image_bindings"
            or not isinstance(review_canonical, dict)
            or _sha256(review_canonical) != review_snapshot.get("canonical_sha256")
            or review_canonical.get("kind") != "bible_image_bindings"
            or not isinstance(review_canonical.get("bindings"), list)
            or generation_stage is None
            or review_canonical.get("generation_stage_id") != str(generation_stage.id)
            or review_canonical.get("generation_snapshot_sha256")
            != generation_stage.output_snapshot_sha256
        ):
            raise ValidationError("已批准 bible_image_review 的冻结绑定快照无效")
        approved_by_entity = {
            (str(row.get("entity_type") or ""), str(row.get("entity_id") or "")): {
                str(value) for value in row.get("selected_asset_ids") or []
            }
            for row in review_canonical["bindings"]
            if isinstance(row, dict)
        }
        approved_binding_ids = {
            asset_id for ids in approved_by_entity.values() for asset_id in ids
        }
        if requested_bible_image_ids - approved_binding_ids:
            raise ValidationError(
                "逐镜图片引用必须来自本次已批准 Bible anchor/reference_assets",
            )

    for shot in shots:
        if str(shot.id) not in raw_by_shot:
            continue
        raw_bindings = raw_by_shot[str(shot.id)]
        all_ids, _image_ids = _reference_binding_asset_ids(raw_bindings)
        if shot.media_type == Shot.MediaType.IMAGE and raw_bindings:
            raise ValidationError("图片 Shot 不接受视频条件参考绑定")
        characters = list(shot.characters.all())
        reference_characters = visual_reference_characters(characters)
        props = list(shot.props.all())
        if (
            has_formal_bible_images
            and _shot_requires_entity_reference_bible(run, shot)
        ):
            expected_character_ids = {
                str(item.id) for item in reference_characters
            }
            expected_prop_ids = {str(item.id) for item in props}
            actual_character_ids = {
                str(item.get("entity_id") or "")
                for item in raw_bindings.get("characters") or []
                if isinstance(item, dict)
            }
            actual_prop_ids = {
                str(item.get("entity_id") or "")
                for item in raw_bindings.get("props") or []
                if isinstance(item, dict)
            }
            expected_scene_id = str(shot.scene_id) if shot.scene_id else ""
            actual_scene = raw_bindings.get("scene")
            actual_scene_id = (
                str(actual_scene.get("entity_id") or "")
                if isinstance(actual_scene, dict) else ""
            )
            if (
                actual_character_ids != expected_character_ids
                or actual_prop_ids != expected_prop_ids
                or actual_scene_id != expected_scene_id
            ):
                raise ValidationError(
                    "当前条件模式要求为 Shot 的每个 character/scene/prop 显式选择精确 Asset",
                )
            for entity_type, items in (
                ("character", raw_bindings.get("characters") or []),
                ("prop", raw_bindings.get("props") or []),
            ):
                for item in items:
                    key = (entity_type, str(item.get("entity_id") or ""))
                    if str(item.get("asset_id") or "") not in approved_by_entity.get(key, set()):
                        raise ValidationError(
                            "实体图片必须来自本 Run 对该实体明确批准的候选",
                        )
            if isinstance(actual_scene, dict):
                scene_key = ("scene", str(actual_scene.get("entity_id") or ""))
                if str(actual_scene.get("asset_id") or "") not in approved_by_entity.get(scene_key, set()):
                    raise ValidationError(
                        "场景图片必须来自本 Run 对该场景明确批准的候选",
                    )
        try:
            normalized = normalize_shot_reference_bindings(
                mode=shot.conditioning_mode,
                bindings=raw_bindings,
                project=locked_project,
                characters=characters,
                scene=shot.scene,
                props=props,
                shot_reference_assets=[locked_assets[item] for item in sorted(all_ids)],
                user=locked_project.owner,
            )
        except ReferenceBindingError as exc:
            raise ValidationError({
                "shot_reference_bindings": f"Shot {shot.id}: {exc}",
            }) from exc
        mode_contract = _selected_conditioning_mode_contract_for_shot(run, shot=shot)
        if mode_contract is not None:
            try:
                validate_reference_bindings_against_mode_contract(
                    normalized,
                    mode_contract,
                )
            except ReferenceBindingError as exc:
                raise ValidationError({
                    "shot_reference_bindings": f"Shot {shot.id}: {exc}",
                }) from exc
        shot.reference_bindings = normalized
        shot.save(update_fields=("reference_bindings", "updated_at"))
        shot.reference_assets.set([locked_assets[item] for item in sorted(all_ids)])
    sync_consistency_locks(locked_project)


def _freeze_stage_input(run: ProductionRun, stage: StageRun) -> None:
    if stage.input_snapshot_sha256:
        existing = _verify_stage_snapshot(stage, "input_snapshot")
        if existing.get("stage_key"):
            return
    upstream = [
        {"stage_key": item.stage_key, "output_snapshot_sha256": item.output_snapshot_sha256}
        for item in run.stages.filter(status=StageRun.Status.SUCCEEDED, ordinal__lt=stage.ordinal).order_by("ordinal")
    ]
    payload = {
        "schema_version": 1,
        "stage_key": stage.stage_key,
        "run_input_snapshot_sha256": run.input_snapshot_sha256,
        "config_snapshot_sha256": stage.config_snapshot_sha256,
        "episode_ids": [str(item.id) for item in _selected_episodes(run)],
        "upstream": upstream,
    }
    if stage.stage_key in {
        "prompt_compile", "media_generation", BIBLE_IMAGE_GENERATION_STAGE_KEY,
    }:
        selection = _frozen_workflow_selection(stage)
        if selection is not None:
            payload["workflow_selection"] = deepcopy(selection)
            payload["workflow_selection_sha256"] = _sha256(selection)
        if stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
            run_snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
            bible_selection = run_snapshot.get("bible_image_prompt_profile")
            expected = str(run_snapshot.get("bible_image_prompt_profile_sha256") or "")
            if (
                not isinstance(bible_selection, dict)
                or bible_selection.get("schema_version") != 2
                or not expected
                or _sha256(bible_selection) != expected
            ):
                raise StageBlocked(
                    "prompt_profile_selection_snapshot_missing",
                    "Bible PromptProfile 显式选择快照缺失；必须新建 ProductionRun。",
                )
            payload["bible_image_prompt_profile"] = deepcopy(bible_selection)
            payload["bible_image_prompt_profile_sha256"] = expected
    _save_stage_snapshot(stage, "input_snapshot", payload)


def _freeze_stage_config(run: ProductionRun, stage: StageRun) -> None:
    if stage.config_snapshot_sha256:
        try:
            verify_json_contract(
                stage.config_snapshot,
                schema_version=stage.config_snapshot_schema_version,
                size_bytes=stage.config_snapshot_size_bytes,
                sha256=stage.config_snapshot_sha256,
                max_bytes=_stage_snapshot_max_bytes(),
            )
        except ValidationError as exc:
            raise StageBlocked("stage_config_snapshot_invalid", "阶段配置快照校验失败") from exc
        if isinstance(stage.config_snapshot, dict) and stage.config_snapshot.get("schema_version") == 1:
            return
    routing = _verify_stage_routing(stage)
    route_snapshots: dict[str, Any] = {}
    try:
        base = resolve_configuration_from_snapshot(run.config_snapshot)
        for route in routing.get("routes") or []:
            model = ModelDeployment.objects.select_related("profile_version").get(
                pk=route["model_deployment_id"],
            ).profile_version
            workflow = None
            if route.get("workflow_version_id"):
                workflow = WorkflowTemplateVersion.objects.get(pk=route["workflow_version_id"])
            resolved = resolve_configuration_from_snapshot(
                run.config_snapshot,
                model_profile_version=model,
                workflow_version=workflow,
            )
            route_snapshots[str(route["index"])] = resolved.snapshot()
    except (
        ConfigurationResolutionError,
        ModelDeployment.DoesNotExist,
        WorkflowTemplateVersion.DoesNotExist,
        KeyError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", "stage_config_source_missing")
        raise StageBlocked(code, f"阶段模型/工作流默认配置无法冻结: {exc}") from exc
    contract = json_contract({
        "schema_version": 1,
        "base": base.snapshot(),
        "routes": route_snapshots,
    }, max_bytes=_stage_snapshot_max_bytes())
    stage.config_snapshot = contract.value
    stage.config_snapshot_schema_version = contract.schema_version
    stage.config_snapshot_size_bytes = contract.size_bytes
    stage.config_snapshot_sha256 = contract.sha256
    stage.save(update_fields=(
        "config_snapshot", "config_snapshot_schema_version",
        "config_snapshot_size_bytes", "config_snapshot_sha256", "updated_at",
    ))


def _stage_config_values(stage: StageRun, *, route_index: int | None = None) -> dict[str, Any]:
    try:
        verify_json_contract(
            stage.config_snapshot,
            schema_version=stage.config_snapshot_schema_version,
            size_bytes=stage.config_snapshot_size_bytes,
            sha256=stage.config_snapshot_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        )
    except ValidationError as exc:
        raise StageBlocked("stage_config_snapshot_invalid", "阶段配置快照校验失败") from exc
    snapshot = stage.config_snapshot if isinstance(stage.config_snapshot, dict) else {}
    selected = route_index if route_index is not None else stage.selected_route_index
    effective = (snapshot.get("routes") or {}).get(str(selected)) if selected is not None else None
    if not isinstance(effective, dict):
        effective = snapshot.get("base")
    values = effective.get("values") if isinstance(effective, dict) else None
    if not isinstance(values, dict):
        raise StageBlocked("stage_config_snapshot_invalid", "阶段配置快照缺少 values")
    return values


def _save_routing_decision(stage: StageRun, decision: dict[str, Any]) -> None:
    contract = json_contract(decision, max_bytes=_stage_snapshot_max_bytes())
    stage.routing_decision = contract.value
    stage.routing_decision_schema_version = contract.schema_version
    stage.routing_decision_size_bytes = contract.size_bytes
    stage.routing_decision_sha256 = contract.sha256
    stage.save(update_fields=(
        "routing_decision", "routing_decision_schema_version",
        "routing_decision_size_bytes", "routing_decision_sha256", "updated_at",
    ))


def _append_route_event(
    stage: StageRun,
    *,
    unit_key: str,
    event_type: str,
    route_index: int | None = None,
    code: str = "",
) -> StageRouteEvent:
    sequence = (stage.route_events.aggregate(value=Max("sequence"))["value"] or 0) + 1
    return StageRouteEvent.objects.create(
        stage_run=stage,
        sequence=sequence,
        unit_key=unit_key[:191],
        route_index=route_index,
        event_type=event_type[:64],
        code=code[:100],
    )


def _frozen_workflow_selection(stage: StageRun) -> dict[str, Any] | None:
    if stage.stage_key not in {"prompt_compile", "media_generation"}:
        return None
    run = stage.production_run
    snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    selection = snapshot.get("workflow_selection")
    expected_sha256 = str(snapshot.get("workflow_selection_sha256") or "")
    if selection is None and not expected_sha256:
        return None
    if (
        not isinstance(selection, dict)
        or selection.get("schema_version") != 2
        or not expected_sha256
        or _sha256(selection) != expected_sha256
    ):
        raise StageBlocked(
            "prompt_profile_selection_snapshot_missing",
            "运行缺少 schema v2 工作流或当前 PromptProfile 合同的显式选择；必须新建 ProductionRun。",
        )
    media_stage = (
        stage if stage.stage_key == "media_generation"
        else run.stages.filter(stage_key="media_generation").first()
    )
    if media_stage is None:
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "运行缺少媒体阶段 route 快照；必须新建 ProductionRun。",
        )
    routing = _verify_stage_routing(media_stage)
    if selection.get("media_stage_routing_sha256") != _sha256(routing):
        raise StageBlocked(
            "workflow_selection_routing_drift",
            "运行冻结的工作流选择与媒体 route 快照不一致；必须新建 ProductionRun。",
        )
    if routing.get("prompt_profile_selection") != _media_prompt_profile_routing_projection(
        selection,
    ):
        raise StageBlocked(
            "prompt_profile_selection_snapshot_missing",
            "媒体 Stage routing 缺少与 Run 一致的显式 PromptProfile 选择；必须新建 ProductionRun。",
        )
    return selection


def _frozen_route_for_shot(
    selection: dict[str, Any],
    *,
    shot_id,
) -> dict[str, Any]:
    if selection.get("mode") == "uniform":
        route = selection.get("route")
    elif selection.get("mode") == "per_shot":
        matches = [
            item
            for item in (selection.get("shot_routes") or [])
            if isinstance(item, dict)
            and str(item.get("shot_id") or "") == str(shot_id)
        ]
        route = matches[0].get("route") if len(matches) == 1 else None
    else:
        route = None
    if not isinstance(route, dict):
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "当前 Shot 缺少唯一冻结媒体 route；必须新建 ProductionRun。",
        )
    return route


def _shot_requires_entity_reference_bible(
    run: ProductionRun,
    shot: Shot,
) -> bool:
    media_stage = run.stages.filter(stage_key="media_generation").first()
    if media_stage is None:
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "运行缺少媒体阶段 route 快照；必须新建 ProductionRun。",
        )
    selection = _frozen_workflow_selection(media_stage)
    if selection is None:
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "运行缺少冻结工作流选择；必须新建 ProductionRun。",
        )
    route = _frozen_route_for_shot(selection, shot_id=shot.id)
    return _route_mode_requires_entity_reference_bible(route, shot.conditioning_mode)


def _selected_workflow_route_index(
    stage: StageRun,
    *,
    prompt: PromptArtifact | None,
) -> int | None:
    selection = _frozen_workflow_selection(stage)
    if selection is not None and selection.get("mode") == "per_shot" and (
        prompt is None or prompt.shot_id is None
    ):
        raise StageBlocked(
            "workflow_selection_prompt_missing",
            "逐镜头工作流选择要求 PromptArtifact 绑定 Shot。",
        )
    return _selected_workflow_route_index_for_shot(
        stage,
        shot_id=(prompt.shot_id if prompt is not None else None),
    )


def _selected_workflow_route_index_for_shot(
    stage: StageRun,
    *,
    shot_id,
) -> int | None:
    selection = _frozen_workflow_selection(stage)
    if selection is None:
        return None
    if selection.get("mode") == "uniform":
        route = selection.get("route")
        if not isinstance(route, dict) or not isinstance(route.get("route_index"), int):
            raise StageBlocked(
                "workflow_selection_snapshot_invalid",
                "整集工作流选择缺少 route_index；必须新建 ProductionRun。",
            )
        return int(route["route_index"])
    if selection.get("mode") != "per_shot":
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "工作流选择 mode 无效；必须新建 ProductionRun。",
        )
    if shot_id is None:
        raise StageBlocked(
            "workflow_selection_prompt_missing",
            "逐镜头工作流选择要求正式 Shot 标识。",
        )
    shot_id = str(shot_id)
    rows = selection.get("shot_routes")
    matches = [
        item for item in rows
        if isinstance(item, dict) and str(item.get("shot_id") or "") == shot_id
    ] if isinstance(rows, list) else []
    if len(matches) != 1:
        raise StageBlocked(
            "workflow_selection_incomplete",
            "逐镜头工作流选择没有且仅有一个当前 Shot 映射；必须新建 ProductionRun。",
        )
    route = matches[0].get("route")
    if not isinstance(route, dict) or not isinstance(route.get("route_index"), int):
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "逐镜头工作流映射缺少 route_index；必须新建 ProductionRun。",
        )
    return int(route["route_index"])


def _frozen_media_prompt_profile_for_shot(
    stage: StageRun,
    *,
    shot: Shot,
) -> dict[str, Any]:
    stage_input = _verify_stage_snapshot(stage, "input_snapshot")
    selection = stage_input.get("workflow_selection")
    expected = str(stage_input.get("workflow_selection_sha256") or "")
    if (
        not isinstance(selection, dict)
        or selection.get("schema_version") != 2
        or not expected
        or _sha256(selection) != expected
        or selection != _frozen_workflow_selection(stage)
    ):
        raise StageBlocked(
            "prompt_profile_selection_snapshot_missing",
            "阶段缺少哈希校验通过的 PromptProfile 显式选择；必须新建 ProductionRun。",
        )
    operation = (
        "image.generate" if shot.media_type == Shot.MediaType.IMAGE else "video.generate"
    )
    mode = shot.conditioning_mode if shot.media_type == Shot.MediaType.VIDEO else ""
    reference = None
    if selection.get("mode") == "uniform":
        matches = [
            item for item in (selection.get("prompt_profile_selections") or [])
            if isinstance(item, dict)
            and item.get("operation") == operation
            and item.get("mode") == mode
        ]
        if len(matches) == 1:
            reference = matches[0].get("prompt_profile")
    elif selection.get("mode") == "per_shot":
        matches = [
            item for item in (selection.get("shot_routes") or [])
            if isinstance(item, dict) and str(item.get("shot_id") or "") == str(shot.id)
        ]
        if len(matches) == 1:
            reference = matches[0].get("prompt_profile")
    if not isinstance(reference, dict) or reference.get("schema_version") != 2:
        raise StageBlocked(
            "prompt_profile_selection_snapshot_missing",
            "当前镜头缺少唯一冻结 PromptProfile binding；必须新建 ProductionRun。",
        )
    return deepcopy(reference)


def _frozen_media_conditioning_contract_for_shot(
    run: ProductionRun,
    *,
    shot: Shot,
) -> tuple[dict[str, Any] | None, str]:
    """Return the exact route contract used to compile this shot's prompt."""

    if shot.media_type != Shot.MediaType.VIDEO:
        return None, ""
    media_stage = run.stages.filter(stage_key="media_generation").first()
    if media_stage is None:
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "运行缺少媒体阶段 route 快照；必须新建 ProductionRun。",
        )
    selection = _frozen_workflow_selection(media_stage)
    if selection is None:
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "运行缺少冻结工作流选择；必须新建 ProductionRun。",
        )
    route = _frozen_route_for_shot(selection, shot_id=shot.id)
    contract = route.get("conditioning_contract")
    contract_sha256 = str(route.get("conditioning_contract_sha256") or "")
    if (
        not isinstance(contract, dict)
        or not contract_sha256
        or _sha256(contract) != contract_sha256
    ):
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            f"镜头 {shot.id} 的冻结 conditioning contract 无效。",
        )
    return deepcopy(contract), contract_sha256


def _frozen_media_frame_policy_for_shot(
    run: ProductionRun,
    *,
    shot: Shot,
) -> dict[str, Any] | None:
    """Read frame alignment only from the selected immutable Workflow version."""

    if shot.media_type != Shot.MediaType.VIDEO:
        return None
    media_stage = run.stages.filter(stage_key="media_generation").first()
    if media_stage is None:
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "运行缺少媒体阶段 route 快照；必须新建 ProductionRun。",
        )
    selection = _frozen_workflow_selection(media_stage)
    if selection is None:
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "运行缺少冻结工作流选择；必须新建 ProductionRun。",
        )
    route = _frozen_route_for_shot(selection, shot_id=shot.id)
    workflow_snapshot = route.get("workflow_snapshot")
    input_contract = (
        workflow_snapshot.get("input_contract")
        if isinstance(workflow_snapshot, dict)
        else None
    )
    if not isinstance(input_contract, dict):
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            f"镜头 {shot.id} 的冻结 workflow input contract 无效。",
        )
    frame_policy = input_contract.get("frame_policy")
    return deepcopy(frame_policy) if isinstance(frame_policy, dict) else None


def _selected_media_model_profile(
    run: ProductionRun,
    *,
    shot: Shot,
) -> tuple[ModelProfileVersion | None, int | None, str]:
    """Resolve the exact frozen per-shot model used to assemble its PromptProfile."""

    media_stage = run.stages.filter(stage_key="media_generation").first()
    if media_stage is None:
        return None, None, ""
    routing = _verify_stage_routing(media_stage)
    route_index = _selected_workflow_route_index_for_shot(
        media_stage,
        shot_id=shot.id,
    )
    if route_index is None:
        if routing.get("mode") == "fixed":
            route_index = 0
        else:
            raise StageBlocked(
                "prompt_route_selection_required",
                "媒体提示词必须先冻结整集统一或逐镜头 route；请新建 ProductionRun 并完成工作流选择。",
                awaiting_approval=True,
            )
    routes = routing.get("routes")
    if (
        not isinstance(routes, list)
        or route_index < 0
        or route_index >= len(routes)
        or not isinstance(routes[route_index], dict)
        or routes[route_index].get("index") != route_index
    ):
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "工作流选择指向的媒体 route 快照无效；必须新建 ProductionRun。",
        )
    route = routes[route_index]
    try:
        deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).get(
            pk=route.get("model_deployment_id"),
            status=ModelDeployment.Status.ACTIVE,
        )
    except (ModelDeployment.DoesNotExist, ValueError) as exc:
        raise StageBlocked(
            "frozen_route_unavailable",
            "工作流选择对应的模型部署已不可用；必须新建 ProductionRun。",
        ) from exc
    profile = deployment.profile_version
    provider = profile.provider
    try:
        verified_frozen_model_profile_specification(profile, route)
    except ValidationError as exc:
        raise StageBlocked(
            "frozen_model_profile_contract_invalid",
            "工作流选择对应的模型合同完整性或冻结来源证明无效；必须新建 ProductionRun。",
        ) from exc
    if (
        str(profile.id) != str(route.get("model_profile_version_id") or "")
        or profile.model_identifier != str(route.get("model_identifier") or "")
        or profile.specification_sha256 != str(route.get("model_specification_sha256") or "")
        or str(provider.id) != str(route.get("provider_id") or "")
        or provider.driver_type != route.get("driver_type")
    ):
        raise StageBlocked(
            "frozen_deployment_changed",
            "工作流选择对应的模型/Provider 绑定已漂移；必须新建 ProductionRun。",
        )
    if not provider.is_active or profile.status != ModelProfileVersion.Status.PUBLISHED:
        raise StageBlocked(
            "frozen_model_profile_retired",
            "工作流选择对应的模型版本或 Provider 已停用。",
        )
    selection_sha256 = str(
        (run.input_snapshot or {}).get("workflow_selection_sha256") or ""
    )
    return profile, route_index, selection_sha256


def _verify_existing_prompt_media_profile(
    run: ProductionRun,
    *,
    stage: StageRun,
    prompt: PromptArtifact,
    shot: Shot,
) -> None:
    """Fail closed when a resumable prompt no longer proves its exact media route/profile."""

    model_profile, route_index, selection_sha256 = _selected_media_model_profile(
        run,
        shot=shot,
    )
    if model_profile is None:
        return
    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    frozen_reference = _frozen_media_prompt_profile_for_shot(stage, shot=shot)
    structured = (
        prompt.structured_payload if isinstance(prompt.structured_payload, dict) else {}
    )
    frozen_conditioning, frozen_conditioning_sha256 = (
        _frozen_media_conditioning_contract_for_shot(run, shot=shot)
    )
    target_model = structured.get("target_model_profile")
    if (
        prompt.model != model_profile.model_identifier
        or options.get("model_profile_version_id") != str(model_profile.id)
        or options.get("selected_media_route_index") != route_index
        or not isinstance(target_model, dict)
        or target_model.get("model_profile_version_id") != str(model_profile.id)
        or target_model.get("specification_sha256") != model_profile.specification_sha256
        or (
            selection_sha256
            and options.get("workflow_selection_sha256") != selection_sha256
        )
        or options.get("prompt_profile_selection") != frozen_reference
        or options.get("prompt_profile_selection_sha256") != _sha256(frozen_reference)
        or options.get("prompt_profile_binding_id")
        != frozen_reference.get("prompt_profile_binding_id")
        or (
            shot.media_type == Shot.MediaType.VIDEO
            and (
                structured.get("conditioning_contract") != frozen_conditioning
                or structured.get("conditioning_contract_sha256")
                != frozen_conditioning_sha256
            )
        )
    ):
        raise StageBlocked(
            "prompt_media_route_snapshot_mismatch",
            "已有 PromptArtifact 未冻结当前 ProductionRun 的实际媒体 route/model；必须新建运行。",
        )
    if prompt.prompt_profile_version_id is None:
        raise StageBlocked(
            "prompt_profile_binding_missing",
            "已有媒体 PromptArtifact 缺少冻结的 PromptProfile binding；必须新建运行。",
        )
    try:
        verify_frozen_profile_reference(
            prompt.prompt_profile_version,
            binding_key=prompt.prompt_profile_binding_key,
            contract_sha256=prompt.prompt_profile_contract_sha256,
        )
        verify_frozen_profile_assembly(
            prompt.prompt_profile_version,
            structured.get("prompt_profile"),
        )
    except PromptProfileResolutionError as exc:
        raise StageBlocked(exc.code, str(exc)) from exc


def _route_candidates(
    stage: StageRun,
    *,
    unit_key: str,
    prompt: PromptArtifact | None = None,
    after_route_index: int | None = None,
) -> list[dict[str, Any]]:
    routing = _verify_stage_routing(stage)
    mode = routing.get("mode")
    routes = routing.get("routes")
    if mode == "unconfigured" or not isinstance(routes, list) or not routes:
        raise StageBlocked(
            "routing_not_configured",
            f"阶段 {stage.stage_key} 没有冻结可执行 routing；请发布新 recipe 后重新创建运行。",
            awaiting_approval=True,
        )
    selected_workflow_route = _selected_workflow_route_index(stage, prompt=prompt)
    if selected_workflow_route is not None:
        # Explicit run-scoped workflow choices are exact. A retry may reuse the
        # same route, but must never drift to a different recipe fallback.
        indexes = [selected_workflow_route]
    elif mode == "manual_choice":
        if stage.selected_route_index is None:
            raise StageBlocked(
                "route_selection_required",
                f"阶段 {stage.stage_key} 使用 manual_choice，必须先显式选择 route。",
                awaiting_approval=True,
            )
        indexes = [stage.selected_route_index]
    elif mode == "fixed":
        indexes = [0]
    elif mode == "ordered_fallback":
        first_index = int(after_route_index) + 1 if after_route_index is not None else 0
        indexes = list(range(first_index, len(routes)))
    else:
        raise StageBlocked("routing_snapshot_invalid", "阶段 routing.mode 无效")
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index in indexes:
        if index >= len(routes):
            failures.append({"route_index": index, "code": "route_index_out_of_range"})
            continue
        route = routes[index]
        if not isinstance(route, dict) or route.get("index") != index:
            failures.append({"route_index": index, "code": "route_snapshot_invalid"})
            continue
        if prompt is not None:
            mode_contract = None
            if prompt.prompt_type == "video":
                try:
                    mode_contract = conditioning_mode_contract(
                        conditioning_contract_from_frozen_route(route),
                        prompt.conditioning_mode,
                    )
                except ValidationError:
                    failures.append({
                        "route_index": index,
                        "code": "conditioning_mode_mismatch",
                    })
                    continue
            if route.get("driver_type") == ProviderProfile.DriverType.ASYNC_REST:
                operation = (
                    "image.text_to_image"
                    if prompt.prompt_type == "image"
                    else "video.text_to_video"
                )
                references = (
                    prompt.structured_payload.get("reference_bindings")
                    if isinstance(prompt.structured_payload, dict) else None
                )
                if prompt.prompt_type == "video" and (
                    (mode_contract or {}).get("input_kind") != "text_only"
                    or references
                ):
                    failures.append({
                        "route_index": index,
                        "code": "provider_upload_contract_missing",
                    })
                    continue
                if operation not in set(route.get("media_operations") or []):
                    failures.append({"route_index": index, "code": "operation_mismatch"})
                    continue
            else:
                output_kind = str(route.get("workflow_output_kind") or "")
                if output_kind != prompt.prompt_type:
                    failures.append({"route_index": index, "code": "output_kind_mismatch"})
                    continue
        candidates.append(route)
    for failure in failures:
        _append_route_event(
            stage,
            unit_key=unit_key,
            route_index=failure.get("route_index"),
            event_type="preflight_failed",
            code=str(failure.get("code") or ""),
        )
    _append_route_event(stage, unit_key=unit_key, event_type="candidates_resolved")
    decision = dict(stage.routing_decision or {})
    decision.update({
        "schema_version": 1,
        "last_unit_key": unit_key,
        "last_candidate_route_indexes": [item["index"] for item in candidates],
        "event_count": stage.route_events.count(),
    })
    decision.pop("units", None)
    decision.pop("failures", None)
    _save_routing_decision(stage, decision)
    if not candidates:
        _append_route_event(
            stage,
            unit_key=unit_key,
            event_type="routes_exhausted",
            code="routing_candidates_exhausted",
        )
        if failures and all(
            item.get("code") == "provider_upload_contract_missing" for item in failures
        ):
            raise StageBlocked(
                "provider_upload_contract_missing",
                "当前 async_rest route 没有受控素材上传契约；首版仅支持文生图和纯文生视频。",
            )
        raise StageBlocked(
            "routing_candidates_exhausted",
            f"阶段 {stage.stage_key} 声明的 route 均不兼容；失败原因已记录。",
        )
    return candidates


def _mark_route_selected(stage: StageRun, *, unit_key: str, route_index: int, reason: str) -> None:
    _append_route_event(
        stage,
        unit_key=unit_key,
        route_index=route_index,
        event_type="route_selected",
        code=reason,
    )
    decision = dict(stage.routing_decision or {})
    decision.update({"schema_version": 1, "state": "selected"})
    decision["last_unit_key"] = unit_key
    decision["selected_route_index"] = route_index
    decision["event_count"] = stage.route_events.count()
    _save_routing_decision(stage, decision)


def _record_route_failure(
    stage: StageRun,
    *,
    unit_key: str,
    route_index: int,
    code: str,
) -> None:
    _append_route_event(
        stage,
        unit_key=unit_key,
        route_index=route_index,
        event_type="route_failed",
        code=code,
    )
    decision = dict(stage.routing_decision or {})
    decision.update({
        "schema_version": 1,
        "last_unit_key": unit_key,
        "last_failure": {"route_index": route_index, "code": code},
        "event_count": stage.route_events.count(),
    })
    _save_routing_decision(stage, decision)


def _decimal_amount(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise StageBlocked("cost_estimate_invalid", "driver 返回的费用估算不是有效数值") from exc
    if not amount.is_finite() or amount < 0:
        raise StageBlocked("cost_estimate_invalid", "driver 返回的费用估算必须是非负有限数")
    return amount.quantize(Decimal("0.000001"))


_USE_STAGE_APPROVED_COST = object()


def _allocated_stage_unknown_cost(
    total: Decimal | None,
    *,
    unit_index: int,
    unit_count: int,
) -> Decimal | None:
    """Deterministically split one approved stage total across initial units."""

    if total is None:
        return None
    if unit_count < 1 or not 1 <= unit_index <= unit_count:
        raise StageBlocked("cost_allocation_invalid", "阶段费用分摊单元无效")
    normalized = Decimal(total).quantize(Decimal("0.000001"))
    share = (normalized / unit_count).quantize(
        Decimal("0.000001"),
        rounding=ROUND_DOWN,
    )
    if unit_index == unit_count:
        return normalized - (share * (unit_count - 1))
    return share


def _estimate_and_reserve(
    run: ProductionRun,
    stage: StageRun,
    attempt: GenerationAttempt,
    *,
    deployment: ModelDeployment,
    operation: str,
    payload: dict[str, Any],
    is_external: bool,
    approved_unknown_cost: Decimal | None | object = _USE_STAGE_APPROVED_COST,
    approved_unknown_currency: str = "",
) -> None:
    if attempt.committed_cost is not None:
        return
    try:
        estimate = driver_for(deployment.profile_version.provider).estimate(
            deployment,
            operation,
            payload,
        )
    except ProviderDriverError as exc:
        attempt.error_code = exc.code
        attempt.error_message = str(exc)[:2000]
        attempt.save(update_fields=("error_code", "error_message", "updated_at"))
        raise StageBlocked("cost_estimate_failed", "provider driver 无法完成费用估算") from exc
    except ValidationError as exc:
        attempt.error_code = "deployment_execution_contract_invalid"
        attempt.error_message = "Deployment 执行合同无效；未提交远端任务。"
        attempt.save(update_fields=("error_code", "error_message", "updated_at"))
        raise StageBlocked(
            "deployment_execution_contract_invalid",
            "Deployment 执行合同无效；未提交远端任务。",
        ) from exc
    if not isinstance(estimate, dict) or estimate.get("schema_version") != 1:
        raise StageBlocked("cost_estimate_invalid", "provider driver 费用估算契约无效")
    amount = _decimal_amount(estimate.get("amount"))
    currency = str(estimate.get("currency") or "").strip().upper()
    if amount is None:
        if approved_unknown_cost is _USE_STAGE_APPROVED_COST:
            amount = stage.approved_estimated_cost
            currency = stage.approved_cost_currency
        else:
            amount = approved_unknown_cost
            currency = str(approved_unknown_currency or "").strip().upper()
    if amount is None:
        attempt.error_code = "unknown_cost"
        attempt.error_message = "provider 未能给出费用估算；未提交远端任务。"
        attempt.save(update_fields=("error_code", "error_message", "updated_at"))
        raise StageBlocked(
            "unknown_cost",
            "provider 费用未知；必须通过费用授权门禁给出本次上限后才能提交。",
            awaiting_approval=True,
        )
    if amount > 0 and not currency:
        raise StageBlocked("cost_currency_missing", "非零费用估算缺少 currency")
    if is_external and not run.allow_external_calls:
        attempt.error_code = "external_call_not_authorized"
        attempt.error_message = "本次运行尚未授权外发；未提交远端任务。"
        attempt.save(update_fields=("error_code", "error_message", "updated_at"))
        raise StageBlocked(
            "external_call_not_authorized",
            "选中 route 会外发数据；必须先通过外发授权门禁。",
            awaiting_approval=True,
        )
    locked = ProductionRun.objects.select_for_update().get(pk=run.pk)
    if locked.cost_currency and currency and locked.cost_currency != currency:
        raise StageBlocked("cost_currency_conflict", "同一运行不能混用多种费用币种")
    # Budget exposure is the sum of irreversible spend, outstanding
    # reservations, and the new submission.  Neither actual spend nor a live
    # reservation may be hidden by comparing only the larger subtotal.
    projected = locked.actual_cost_total + locked.committed_cost_total + amount
    if locked.budget_limit is not None and projected > locked.budget_limit:
        attempt.estimated_cost = amount
        attempt.cost_currency = currency
        attempt.error_code = "budget_limit_exceeded"
        attempt.error_message = (
            f"预计累计费用 {projected} {currency} 超过预算 {locked.budget_limit}；未提交。"
        )
        attempt.save(update_fields=(
            "estimated_cost", "cost_currency", "error_code", "error_message", "updated_at",
        ))
        raise StageBlocked(
            "budget_limit_exceeded",
            attempt.error_message,
            awaiting_approval=True,
        )
    if is_external and locked.budget_limit is None:
        raise StageBlocked(
            "external_budget_missing",
            "外发 route 必须先设置运行预算；未提交远端任务。",
            awaiting_approval=True,
        )
    attempt.estimated_cost = amount
    attempt.committed_cost = amount
    attempt.cost_currency = currency
    attempt.is_external = is_external
    attempt.operation = operation
    attempt.error_code = ""
    attempt.error_message = ""
    attempt.save(update_fields=(
        "estimated_cost", "committed_cost", "cost_currency", "is_external",
        "operation", "error_code", "error_message", "updated_at",
    ))
    locked.estimated_cost_total += amount
    locked.committed_cost_total += amount
    if currency and not locked.cost_currency:
        locked.cost_currency = currency
    locked.save(update_fields=(
        "estimated_cost_total", "committed_cost_total", "cost_currency", "updated_at",
    ))
    run.estimated_cost_total = locked.estimated_cost_total
    run.committed_cost_total = locked.committed_cost_total
    run.cost_currency = locked.cost_currency


def _record_actual_cost(attempt: GenerationAttempt, value: object, currency: str = "") -> None:
    amount = _decimal_amount(value)
    if amount is None:
        amount = attempt.committed_cost
    if amount is None:
        return
    normalized_currency = str(currency or attempt.cost_currency or "").strip().upper()
    if attempt.actual_cost is not None:
        return
    run = ProductionRun.objects.select_for_update().get(pk=attempt.stage_run.production_run_id)
    if run.cost_currency and normalized_currency and run.cost_currency != normalized_currency:
        raise StageBlocked("actual_cost_currency_conflict", "provider 实际费用币种与运行币种不一致")
    reserved = attempt.committed_cost or Decimal("0")
    attempt.actual_cost = amount
    attempt.committed_cost = Decimal("0")
    attempt.cost_currency = normalized_currency
    attempt.save(update_fields=("actual_cost", "committed_cost", "cost_currency", "updated_at"))
    run.actual_cost_total += amount
    run.committed_cost_total = max(Decimal("0"), run.committed_cost_total - reserved)
    if normalized_currency and not run.cost_currency:
        run.cost_currency = normalized_currency
    run.save(update_fields=(
        "actual_cost_total", "committed_cost_total", "cost_currency", "updated_at",
    ))


def _assert_initialized_production_run(
    run: ProductionRun,
    expected_stages: list[dict[str, Any]],
) -> None:
    try:
        _verify_run_snapshots(run)
        actual = list(run.stages.order_by("ordinal").values_list(
            "ordinal", "stage_key", "routing_sha256",
            "decision_policy", "decision_policy_sha256",
        ))
    except (ValidationError, ValueError, TypeError) as exc:
        raise ProductionRunConflict({
            "idempotency_key": "已存在的 ProductionRun 初始化不完整；请使用新的幂等键",
        }) from exc
    expected = [
        (
            ordinal,
            item["key"],
            item.get("decision_policy") or "",
            item.get("decision_policy_sha256") or "",
        )
        for ordinal, item in enumerate(expected_stages, start=1)
    ]
    if (
        len(actual) != len(expected)
        or [
            (ordinal, key, policy, policy_sha256)
            for ordinal, key, _digest, policy, policy_sha256 in actual
        ] != expected
        or any(not digest for _ordinal, _key, digest, _policy, _policy_sha256 in actual)
    ):
        raise ProductionRunConflict({
            "idempotency_key": "已存在的 ProductionRun 初始化不完整；请使用新的幂等键",
        })


def _assert_replayable_production_run(run: ProductionRun) -> None:
    try:
        _verify_run_snapshots(run)
        verify_json_contract(
            run.recipe_version.definition,
            schema_version=run.recipe_version.definition_schema_version,
            size_bytes=run.recipe_version.definition_size_bytes,
            sha256=run.recipe_version.definition_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        definition = run.recipe_version.definition
        raw_stages = definition.get("stages") if isinstance(definition, dict) else None
        expected_keys = [
            str(item.get("key") or "")
            for item in (raw_stages or [])
            if isinstance(item, dict)
        ]
        stages = list(run.stages.order_by("ordinal").values_list(
            "ordinal", "stage_key", "routing_sha256",
            "decision_policy", "decision_policy_sha256",
        ))
        expected_policies = []
        for raw in raw_stages or []:
            stage_key = str(raw.get("key") or "") if isinstance(raw, dict) else ""
            policy = str(raw.get("decision_policy") or "") if isinstance(raw, dict) else ""
            expected_policies.append((
                policy,
                _sha256({
                    "schema_version": 1,
                    "interaction_mode": UNATTENDED_INTERACTION_MODE,
                    "stage_key": stage_key,
                    "decision_policy": policy,
                    "recipe_definition_sha256": run.recipe_version.definition_sha256,
                }) if policy else "",
            ))
    except (ValidationError, ValueError, TypeError) as exc:
        raise ProductionRunConflict({
            "idempotency_key": "已存在的 ProductionRun 初始化不完整；请使用新的幂等键",
        }) from exc
    if (
        not stages
        or len(expected_keys) != len(stages)
        or [item[1] for item in stages] != expected_keys
        or [(item[3], item[4]) for item in stages] != expected_policies
        or any(
        ordinal != index or not stage_key or not digest
        for index, (
            ordinal, stage_key, digest, _policy, _policy_sha256,
        ) in enumerate(stages, start=1)
        )
    ):
        raise ProductionRunConflict({
            "idempotency_key": "已存在的 ProductionRun 初始化不完整；请使用新的幂等键",
        })


def _normalize_unattended_reference_input_intent(
    reference_inputs: object,
) -> list[dict[str, Any]]:
    if reference_inputs in (None, []):
        return []
    if (
        not isinstance(reference_inputs, list)
        or len(reference_inputs) > max_structural_media_bindings()
    ):
        raise ValidationError({
            "reference_inputs": (
                f"动态参考视频结构上限为 {max_structural_media_bindings()}；"
                "实际数量由已选模式合同决定"
            ),
        })
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(reference_inputs):
        if not isinstance(raw, dict) or set(raw) != {
            "asset_id", "purpose", "use_embedded_audio",
        }:
            raise ValidationError({
                "reference_inputs": (
                    f"第 {index + 1} 项必须且只能包含 "
                    "asset_id、purpose、use_embedded_audio"
                ),
            })
        try:
            asset_id = str(uuid.UUID(str(raw.get("asset_id") or "")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError({
                "reference_inputs": f"第 {index + 1} 项 asset_id 必须是有效 UUID",
            }) from exc
        try:
            purpose = normalize_reference_identifier(
                raw.get("purpose"),
                f"reference_inputs[{index}].purpose",
            )
        except ReferenceBindingError as exc:
            raise ValidationError({"reference_inputs": str(exc)}) from exc
        use_embedded_audio = raw.get("use_embedded_audio")
        if type(use_embedded_audio) is not bool:
            raise ValidationError({
                "reference_inputs": (
                    f"第 {index + 1} 项 use_embedded_audio 必须是布尔值"
                ),
            })
        normalized.append({
            "asset_id": asset_id,
            "purpose": purpose,
            "use_embedded_audio": use_embedded_audio,
        })
    asset_ids = [item["asset_id"] for item in normalized]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValidationError({"reference_inputs": "不能重复绑定同一动态参考视频"})
    return normalized


def _freeze_unattended_reference_inputs(
    *,
    project: SeriesProject,
    reference_inputs: object,
    conditioning_mode_contract: object,
) -> list[dict[str, Any]]:
    requested = _normalize_unattended_reference_input_intent(reference_inputs)
    try:
        validate_dynamic_reference_bindings_against_mode_contract(
            {"videos": requested} if requested else {},
            conditioning_mode_contract,
        )
    except ReferenceBindingError as exc:
        raise ProductionRunCreateRejected(
            {"reference_inputs": f"动态参考素材不满足冻结条件模式合同: {exc}"},
            code="unattended_reference_binding_contract_invalid",
        ) from exc
    if not requested:
        return []
    assets = {
        str(asset.id): asset
        for asset in Asset.objects.select_for_update().filter(
            pk__in=[item["asset_id"] for item in requested],
        )
    }
    frozen_inputs: list[dict[str, Any]] = []
    for index, intent in enumerate(requested):
        asset = assets.get(intent["asset_id"])
        if asset is None:
            raise ProductionRunCreateRejected(
                {"reference_inputs": f"第 {index + 1} 条动态参考视频不存在"},
                code="unattended_reference_video_invalid",
            )
        if asset.owner_id not in (None, project.owner_id):
            raise ProductionRunCreateRejected(
                {
                    "reference_inputs": (
                        f"第 {index + 1} 条动态参考视频不属于当前项目 "
                        "owner 或全局素材库"
                    ),
                },
                code="unattended_reference_video_forbidden",
            )
        if is_workflow_smoke_fixture_asset(asset):
            raise ProductionRunCreateRejected(
                {
                    "reference_inputs": (
                        f"第 {index + 1} 条动态参考视频是 Workflow Smoke 专用素材；"
                        "请改选与本剧人物、场景和道具不冲突的生产素材"
                    ),
                },
                code="unattended_reference_video_forbidden",
            )
        if not asset.is_active or asset.kind != Asset.Kind.REFERENCE_VIDEO:
            raise ProductionRunCreateRejected(
                {
                    "reference_inputs": (
                        f"第 {index + 1} 条动态参考视频必须是已启用的 "
                        "reference_video 素材"
                    ),
                },
                code="unattended_reference_video_invalid",
            )
        try:
            if asset.owner_id is None:
                if asset.file or not asset.local_path:
                    raise ValueError("全局素材必须来自只读本地素材库")
                source = Path(asset.local_path).resolve()
                allowed_root = Path(settings.ASSET_LIBRARY_ROOT).resolve()
                source_kind = "global_asset_library"
            else:
                if not asset.file:
                    raise ValueError("项目素材必须由上传接口保存")
                source = Path(asset.file.path).resolve()
                allowed_root = (
                    Path(settings.MEDIA_ROOT) / "assets" / str(asset.owner_id)
                ).resolve()
                source_kind = "owner_upload"
            if not source.is_relative_to(allowed_root) or not source.is_file():
                raise ValueError("素材文件越出授权目录或不存在")
            observed_size = source.stat().st_size
            facts = verified_local_media_facts(
                source,
                expected_kind="video",
                max_bytes=observed_size,
                include_observed_media=True,
            )
        except (OSError, ValueError, NotImplementedError, ProvenanceError) as exc:
            raise ProductionRunCreateRejected(
                {
                    "reference_inputs": (
                        f"第 {index + 1} 条动态参考视频真实文件不可执行或无法验证"
                    ),
                },
                code="unattended_reference_video_invalid",
            ) from exc
        recorded_checksum = str(asset.checksum or "").strip().casefold()
        recorded_mime = str(asset.mime_type or "").strip().casefold()
        if (
            len(recorded_checksum) != 64
            or facts["sha256"].casefold() != recorded_checksum
            or not recorded_mime.startswith("video/")
            or facts["mime_type"].casefold() != recorded_mime
        ):
            raise ProductionRunCreateRejected(
                {
                    "reference_inputs": (
                        f"第 {index + 1} 条动态参考视频 MIME 或 checksum "
                        "与真实文件不一致"
                    ),
                },
                code="unattended_reference_video_integrity_failed",
            )
        frozen = {
            "schema_version": 1,
            **intent,
            "asset_owner_id": str(asset.owner_id) if asset.owner_id else None,
            "asset_kind": asset.kind,
            "source_kind": source_kind,
            "mime_type": facts["mime_type"],
            "size_bytes": facts["size_bytes"],
            "checksum": facts["sha256"],
            "observed_media": facts.get("observed_media"),
        }
        frozen["facts_sha256"] = _sha256(frozen)
        frozen_inputs.append(frozen)
    return frozen_inputs


def _is_unattended_run(run: ProductionRun) -> bool:
    snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    return (
        snapshot.get("interaction_mode") == UNATTENDED_INTERACTION_MODE
        and snapshot.get("unattended_confirmed") is True
    )


def _freeze_project_execution_contract(project: SeriesProject) -> dict[str, Any]:
    settings_value = (
        project.production_settings
        if isinstance(project.production_settings, dict)
        else {}
    )
    raw_mode = str(settings_value.get("default_conditioning_mode") or "").strip()
    return {
        "schema_version": 1,
        "default_conditioning_mode": (
            normalize_default_conditioning_mode(raw_mode) if raw_mode else ""
        ),
    }


def _frozen_project_default_conditioning_mode(run: ProductionRun) -> str:
    """Read the run-scoped mode without consulting mutable project settings."""

    snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    contract = snapshot.get("project_execution_contract")
    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != 1
        or snapshot.get("project_execution_contract_sha256") != _sha256(contract)
    ):
        raise StageBlocked(
            "project_execution_contract_invalid",
            "ProductionRun 缺少有效的项目执行合同；请使用当前配置新建运行。",
        )
    try:
        return normalize_default_conditioning_mode(
            contract.get("default_conditioning_mode"),
        )
    except ValueError as exc:
        raise StageBlocked(
            "project_default_conditioning_mode_invalid",
            "ProductionRun 冻结的 default_conditioning_mode 无效；请新建运行。",
        ) from exc


def _frozen_recipe_definition_sha256(run: ProductionRun) -> str:
    snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    frozen = snapshot.get("recipe_definition_sha256")
    if frozen in (None, ""):
        # Runs created before the execution-contract snapshot keep their stage
        # routing hashes as the authority. New runs always persist this field.
        return str(run.recipe_version.definition_sha256 or "")
    normalized = str(frozen).strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise StageBlocked(
            "recipe_definition_snapshot_invalid",
            "ProductionRun 冻结的配方定义 hash 无效；请新建运行。",
        )
    return normalized


def _verified_unattended_reference_inputs(
    run: ProductionRun,
) -> list[dict[str, Any]]:
    snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    selection = snapshot.get("workflow_selection")
    if (
        snapshot.get("workflow_selection_sha256") != _sha256(selection)
        if isinstance(selection, dict) else True
    ):
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "unattended 运行的冻结 workflow selection 无效。",
        )
    try:
        mode_contract = _selected_unattended_conditioning_mode_contract(
            selection,
            mode=_frozen_project_default_conditioning_mode(run),
        )
    except ProductionRunCreateRejected as exc:
        raise StageBlocked(
            "unattended_conditioning_contract_snapshot_invalid",
            "unattended 运行的冻结条件模式合同无效。",
        ) from exc
    frozen = snapshot.get("reference_inputs")
    if (
        not isinstance(frozen, list)
        or len(frozen) > max_structural_media_bindings()
        or snapshot.get("reference_inputs_sha256") != _sha256(frozen)
    ):
        raise StageBlocked(
            "unattended_reference_video_snapshot_invalid",
            "unattended 动态参考视频冻结快照无效。",
        )
    raw = [{
        "asset_id": item.get("asset_id"),
        "purpose": item.get("purpose"),
        "use_embedded_audio": item.get("use_embedded_audio"),
    } for item in frozen if isinstance(item, dict)]
    if len(raw) != len(frozen):
        raise StageBlocked(
            "unattended_reference_video_snapshot_invalid",
            "unattended 动态参考视频冻结快照无效。",
        )
    try:
        current = _freeze_unattended_reference_inputs(
            project=run.project,
            reference_inputs=raw,
            conditioning_mode_contract=mode_contract,
        )
    except (ProductionRunCreateRejected, ValidationError) as exc:
        raise StageBlocked(
            "unattended_reference_video_drift",
            "unattended 动态参考视频当前不可执行或已漂移。",
        ) from exc
    if current != frozen:
        raise StageBlocked(
            "unattended_reference_video_drift",
            "unattended 动态参考视频事实与运行冻结快照不一致。",
        )
    return frozen


def _structured_script_route_duration_policy(
    stages: list[dict[str, Any]],
    *,
    stage_key: str,
    stage_mode: str,
    model_policy_key: str,
) -> tuple[int, int, list[dict[str, int]]]:
    """Return the common duration range supported by every frozen text route.

    Ordered fallback routes share one immutable ProductionRun configuration.
    Their structured-script duration policies therefore need a non-empty
    intersection; otherwise a later fallback could create a script that the
    same run's storyboard contract rejects.
    """

    script_stage = next((
        item for item in stages
        if item.get("key") == stage_key and item.get("mode") == stage_mode
    ), None)
    routing = script_stage.get("routing") if isinstance(script_stage, dict) else None
    routes = routing.get("routes") if isinstance(routing, dict) else None
    if not isinstance(routes, list) or not routes:
        raise ValidationError({
            "recipe_version_id": f"配方缺少已冻结的 {stage_key} route",
        })

    policies: list[dict[str, int]] = []
    for fallback_index, route in enumerate(routes):
        specification = (
            route.get("model_specification") if isinstance(route, dict) else None
        )
        policy = (
            specification.get(model_policy_key)
            if isinstance(specification, dict) else None
        )
        minimum = policy.get("minimum_shot_duration_ms") if isinstance(policy, dict) else None
        maximum = policy.get("maximum_shot_duration_ms") if isinstance(policy, dict) else None
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or minimum < 1
            or maximum < minimum
        ):
            raise ValidationError({
                "recipe_version_id": (
                    f"{stage_key} route 缺少有效的 {model_policy_key} 镜头时长策略"
                ),
            })
        route_index = route.get("index")
        policies.append({
            "route_index": (
                int(route_index)
                if type(route_index) is int
                else fallback_index
            ),
            "minimum_duration_ms": int(minimum),
            "maximum_duration_ms": int(maximum),
        })

    common_minimum = max(item["minimum_duration_ms"] for item in policies)
    common_maximum = min(item["maximum_duration_ms"] for item in policies)
    if common_minimum > common_maximum:
        raise ValidationError({
            "recipe_version_id": (
                f"{stage_key} fallback routes 的镜头时长策略没有共同可执行区间"
            ),
        })
    return common_minimum, common_maximum, policies


def _apply_structured_shot_duration_contract(
    *,
    stages: list[dict[str, Any]],
    source: str,
    stage_key: str,
    stage_mode: str,
    model_policy_key: str,
    episode_count: int,
    shots_per_episode: int,
    episode_duration_seconds: int,
    merged_overrides: dict[str, Any],
    validated_overrides: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze a run-scoped shot-duration contract from structured inputs.

    ``episode_duration_seconds`` is an episode budget, not a request to make
    every shot equal.  The selected stage's frozen model-policy range is
    intersected with that budget.  Explicit run overrides may narrow this
    interval, but cannot broaden it or make the structured plan infeasible.
    """

    if source not in {"structured_ai_plan", "confirmed_revision_structure_plan"}:
        raise ValidationError({"recipe_version_id": "结构化镜头时长合同来源无效"})
    max_episodes = _episode_max_items()
    max_shots_per_episode = _shots_per_episode_max_items()
    max_total_shots = _total_shot_max_items()
    if type(episode_count) is not int or not 1 <= episode_count <= max_episodes:
        raise ValidationError({
            "episode_count": f"必须在 1 到 {max_episodes} 之间",
        })
    if (
        type(shots_per_episode) is not int
        or not 1 <= shots_per_episode <= max_shots_per_episode
    ):
        raise ValidationError({
            "shots_per_episode": f"必须在 1 到 {max_shots_per_episode} 之间",
        })
    if episode_count * shots_per_episode > max_total_shots:
        raise ValidationError({
            "shots_per_episode": f"总镜头数不能超过安全边界 {max_total_shots}",
        })
    if type(episode_duration_seconds) is not int or episode_duration_seconds < 1:
        raise ValidationError({"episode_duration_seconds": "必须是正整数"})

    episode_duration_ms = episode_duration_seconds * 1_000
    model_minimum, model_maximum, route_policies = (
        _structured_script_route_duration_policy(
            stages,
            stage_key=stage_key,
            stage_mode=stage_mode,
            model_policy_key=model_policy_key,
        )
    )
    if shots_per_episode * model_minimum > episode_duration_ms:
        raise ValidationError({
            "episode_duration_seconds": (
                "单集时长不足以容纳每集镜数与冻结文本模型的最短镜头时长"
            ),
        })

    minimum_key = "production.video_shot_min_duration_ms"
    maximum_key = "production.video_shot_max_duration_ms"
    default_key = "production.default_shot_duration_ms"
    explicit_keys = {
        key for key in STRUCTURED_SHOT_DURATION_CONFIG_KEYS if key in merged_overrides
    }

    explicit_minimum = validated_overrides.get(minimum_key)
    if explicit_minimum is not None and int(explicit_minimum) < model_minimum:
        raise ValidationError({
            "config_overrides": (
                f"{minimum_key} 不能小于冻结 {stage_key} route 的共同最小值 "
                f"{model_minimum}ms"
            ),
        })
    effective_minimum = (
        int(explicit_minimum)
        if explicit_minimum is not None else model_minimum
    )
    if shots_per_episode * effective_minimum > episode_duration_ms:
        raise ValidationError({
            "config_overrides": (
                f"{minimum_key} 与单集 {episode_duration_ms}ms / "
                f"{shots_per_episode} 镜的总时长预算冲突"
            ),
        })

    budget_maximum = (
        episode_duration_ms
        - (shots_per_episode - 1) * effective_minimum
    )
    allowed_maximum = min(model_maximum, budget_maximum)
    if effective_minimum > allowed_maximum:
        raise ValidationError({
            "config_overrides": (
                f"{minimum_key} 超出冻结文本模型与单集总时长预算的共同可执行区间"
            ),
        })
    explicit_maximum = validated_overrides.get(maximum_key)
    if explicit_maximum is not None and not (
        effective_minimum <= int(explicit_maximum) <= allowed_maximum
    ):
        raise ValidationError({
            "config_overrides": (
                f"{maximum_key} 必须位于 {effective_minimum}–{allowed_maximum}ms；"
                "该范围由冻结文本模型与单集总时长预算共同决定"
            ),
        })
    effective_maximum = (
        int(explicit_maximum)
        if explicit_maximum is not None else allowed_maximum
    )

    average_duration_ms = (
        episode_duration_ms + shots_per_episode // 2
    ) // shots_per_episode
    planned_default = min(
        effective_maximum,
        max(effective_minimum, average_duration_ms),
    )
    explicit_default = validated_overrides.get(default_key)
    if explicit_default is not None and not (
        effective_minimum <= int(explicit_default) <= effective_maximum
    ):
        raise ValidationError({
            "config_overrides": (
                f"{default_key} 必须位于本次结构化计划的 "
                f"{effective_minimum}–{effective_maximum}ms 合同内"
            ),
        })
    effective_default = (
        int(explicit_default)
        if explicit_default is not None else planned_default
    )

    derived = {
        minimum_key: effective_minimum,
        default_key: effective_default,
        maximum_key: effective_maximum,
    }
    for key, value in derived.items():
        if key not in merged_overrides:
            merged_overrides[key] = value

    contract = {
        "schema_version": 1,
        "source": source,
        "episode_count": episode_count,
        "shots_per_episode": shots_per_episode,
        "episode_duration_ms": episode_duration_ms,
        "average_duration_ms": average_duration_ms,
        "minimum_duration_ms": effective_minimum,
        "default_duration_ms": effective_default,
        "maximum_duration_ms": effective_maximum,
        "explicit_override_keys": sorted(explicit_keys),
        "route_stage_key": stage_key,
        "model_policy_key": model_policy_key,
        "route_policies": route_policies,
    }
    return merged_overrides, contract


def _production_run_request_intent(
    *,
    project,
    script_revision,
    recipe,
    allow_external_calls,
    budget_limit,
    max_generation_tasks,
    config_overrides,
    idea,
    style_hint,
    episode_count,
    shots_per_episode,
    episode_duration_seconds,
    episode_ids,
    workflow_selection,
    bible_image_prompt_profile_binding_id,
    unattended_confirmed,
    reference_inputs,
) -> dict[str, Any]:
    selection = workflow_selection if isinstance(workflow_selection, dict) else None
    normalized_selection = None
    if selection is not None:
        normalized_selection = {
            "schema_version": selection.get("schema_version"),
            "mode": str(selection.get("mode") or ""),
            "route_index": selection.get("route_index"),
            "prompt_profile_selections": sorted(({
                "operation": str(item.get("operation") or ""),
                "mode": str(item.get("mode") or ""),
                "prompt_profile_binding_id": str(
                    item.get("prompt_profile_binding_id") or ""
                ),
            } for item in (selection.get("prompt_profile_selections") or [])
                if isinstance(item, dict)), key=lambda item: (
                    item["operation"], item["mode"], item["prompt_profile_binding_id"],
                )),
            "shot_routes": sorted(({
                "shot_id": str(item.get("shot_id") or ""),
                "route_index": item.get("route_index"),
                "prompt_profile_binding_id": str(
                    item.get("prompt_profile_binding_id") or ""
                ),
            } for item in (selection.get("shot_routes") or [])
                if isinstance(item, dict)), key=lambda item: item["shot_id"]),
        }
    intent = {
        "schema_version": 1,
        "project_id": str(project.id),
        "script_revision_id": str(script_revision.id) if script_revision else None,
        "recipe_version_id": str(recipe.id),
        "allow_external_calls": allow_external_calls,
        "budget_limit": str(budget_limit) if budget_limit is not None else None,
        "max_generation_tasks": max_generation_tasks,
        "config_overrides": _snapshot_config_overrides(dict(config_overrides or {})),
        "idea": str(idea or "").strip(),
        "style_hint": str(style_hint or "").strip(),
        "episode_count": int(episode_count),
        "shots_per_episode": int(shots_per_episode) if shots_per_episode is not None else None,
        "episode_duration_seconds": (
            int(episode_duration_seconds) if episode_duration_seconds is not None else None
        ),
        "episode_ids": sorted(str(item) for item in (episode_ids or [])),
        "workflow_selection": normalized_selection,
        "bible_image_prompt_profile_binding_id": str(
            bible_image_prompt_profile_binding_id or ""
        ),
    }
    if (
        isinstance(recipe.definition, dict)
        and recipe.definition.get("schema_version") == 2
    ) or unattended_confirmed or reference_inputs:
        intent.update({
            "unattended_confirmed": bool(unattended_confirmed),
            "reference_inputs": _normalize_unattended_reference_input_intent(
                reference_inputs,
            ),
        })
    return intent


@registry_reference_writer
@security_boundary_resolution_cache()
def create_production_run(
    *,
    user,
    project,
    script_revision: ScriptRevision | None,
    recipe: ProductionRecipeVersion,
    idempotency_key: str,
    allow_external_calls: bool | None,
    budget_limit: Decimal | None,
    max_generation_tasks: int | None,
    config_overrides: dict[str, Any] | None = None,
    idea: str = "",
    style_hint: str = "",
    episode_count: int | None = None,
    shots_per_episode: int | None = None,
    episode_duration_seconds: int | None = None,
    episode_ids: list[Any] | None = None,
    workflow_selection: dict[str, Any] | None = None,
    bible_image_prompt_profile_binding_id: Any = None,
    unattended_confirmed: bool = False,
    reference_inputs: list[dict[str, Any]] | None = None,
) -> tuple[ProductionRun, bool]:
    key = str(idempotency_key or "").strip()
    if not 1 <= len(key) <= 120:
        raise ValidationError({"idempotency_key": "长度必须为 1 到 120"})
    base_config = resolve_configuration(
        project=project,
        required_keys=("production.project_creation_policy",),
    )
    if episode_count is None:
        episode_count = _project_creation_default_episode_count(base_config.values)
    # Serialize creation per owner so two first-time requests cannot both pass
    # the pre-insert lookup. The unique constraint remains the final guard.
    type(user).objects.select_for_update().only("pk").get(pk=user.pk)
    request_intent = _production_run_request_intent(
        project=project,
        script_revision=script_revision,
        recipe=recipe,
        allow_external_calls=allow_external_calls,
        budget_limit=budget_limit,
        max_generation_tasks=max_generation_tasks,
        config_overrides=config_overrides,
        idea=idea,
        style_hint=style_hint,
        episode_count=episode_count,
        shots_per_episode=shots_per_episode,
        episode_duration_seconds=episode_duration_seconds,
        episode_ids=episode_ids,
        workflow_selection=workflow_selection,
        bible_image_prompt_profile_binding_id=bible_image_prompt_profile_binding_id,
        unattended_confirmed=unattended_confirmed,
        reference_inputs=reference_inputs,
    )
    request_intent_sha256 = _sha256(request_intent)
    replay = ProductionRun.objects.select_for_update().filter(
        owner=user,
        idempotency_key=key,
    ).first()
    replay_snapshot = (
        replay.input_snapshot if replay is not None
        and isinstance(replay.input_snapshot, dict) else {}
    )
    if replay is not None and replay_snapshot.get("schema_version") == 3:
        if replay_snapshot.get("request_intent_sha256") != request_intent_sha256:
            raise ProductionRunConflict({
                "idempotency_key": "该键已用于不同 ProductionRun 请求",
            })
        _assert_replayable_production_run(replay)
        return replay, False
    if recipe.status != ProductionRecipeVersion.Status.PUBLISHED:
        raise ValidationError("只能使用 published recipe")
    merged_overrides = dict(config_overrides or {})
    explicit_pairs = (
        ("production.max_generation_tasks", max_generation_tasks),
        ("production.allow_external_calls", allow_external_calls),
    )
    for config_key, explicit_value in explicit_pairs:
        if explicit_value is None:
            continue
        normalized_explicit = (
            int(explicit_value)
            if config_key == "production.max_generation_tasks"
            else bool(explicit_value)
        )
        if config_key in merged_overrides and merged_overrides[config_key] != normalized_explicit:
            raise ValidationError({
                "config_overrides": f"{config_key} 与同名运行参数冲突",
            })
        merged_overrides[config_key] = normalized_explicit
    validated_overrides = validate_config_override_values(
        ConfigRevision.ScopeType.RUN,
        merged_overrides,
    )
    effective_values = {**base_config.values, **validated_overrides}
    configured_task_cap = int(base_config.values["production.max_generation_tasks"])
    effective_task_cap = int(effective_values["production.max_generation_tasks"])
    if not 1 <= effective_task_cap <= configured_task_cap:
        raise ValidationError({
            "max_generation_tasks": f"必须在 1 到系统上限 {configured_task_cap} 之间",
        })
    effective_external = bool(effective_values["production.allow_external_calls"])
    if effective_external and budget_limit is None:
        raise ValidationError({"budget_limit": "允许外发时必须设置本次预算上限"})
    if budget_limit is not None and budget_limit < 0:
        raise ValidationError({"budget_limit": "不能为负数"})

    stages = validate_production_recipe_executability(recipe)
    ai_script = any(
        item["key"] == "script_generate" and item["mode"] == "ai"
        for item in stages
    )
    imported_script = any(
        item["key"] == "script_input" and item["mode"] == "confirmed_revision"
        for item in stages
    )
    script_structure_stage = next((
        item for item in stages
        if item["key"] == "script_structure" and item["mode"] == "ai"
    ), None)
    structured_import_plan_requested = imported_script and (
        shots_per_episode is not None or episode_duration_seconds is not None
    )
    downstream_only = not ai_script and not imported_script
    unattended = (
        isinstance(recipe.definition, dict)
        and recipe.definition.get("schema_version") == 2
        and recipe.definition.get("interaction_mode") == UNATTENDED_INTERACTION_MODE
    )
    if unattended:
        if unattended_confirmed is not True:
            raise ProductionRunCreateRejected(
                {
                    "unattended_confirmed": (
                        "真正全自动运行会按冻结策略自动批准创意候选；必须显式确认"
                    ),
                },
                code="unattended_confirmation_required",
            )
        if ai_script and any(
            relation.exists()
            for relation in (project.characters, project.scenes, project.props)
        ):
            raise ProductionRunCreateRejected(
                {
                    "project_id": (
                        "真正全自动的新剧本运行要求项目的角色、场景和道具 Bible 为空；"
                        "系统会从本次剧本生成并审核新的 Bible，不会混入历史或无关实体。"
                        "请新建空项目，或改用人工/半自动配方处理现有 Bible。"
                    ),
                },
                code="unattended_existing_bible_requires_new_project",
            )
    else:
        if unattended_confirmed or reference_inputs:
            raise ValidationError({
                "unattended_confirmed": (
                    "unattended_confirmed/reference_inputs 只允许用于 schema_version=2 unattended recipe"
                ),
            })
    if unattended or any(
        item["key"] == BIBLE_IMAGE_GENERATION_STAGE_KEY for item in stages
    ):
        _validate_delivery_recipe_closure(stages)
    if ai_script and imported_script:
        raise ValidationError("recipe 不能同时选择 script_generate/ai 与 script_input/confirmed_revision")
    storyboard_stage = next(
        (item for item in stages if item["key"] == "storyboard"),
        None,
    )
    if (
        imported_script
        and project.episodes.exists()
        and storyboard_stage is not None
        and storyboard_stage["mode"] not in {"manual", "skip"}
    ):
        raise ProductionRunCreateRejected(
            {
                "recipe_version_id": (
                    "项目已存在 Episode，confirmed_revision 配方不能再执行自动 storyboard；"
                    "请使用显式 storyboard/manual、storyboard/skip 的合法配方，或新建项目"
                ),
            },
            code="existing_storyboard_requires_manual_resolution",
        )
    normalized_idea = str(idea or "").strip()
    normalized_style_hint = str(style_hint or "").strip()
    if ai_script:
        if script_revision is not None:
            raise ValidationError("AI 剧本配方不能同时传入 script_revision")
        if not normalized_idea:
            raise ValidationError({"idea": "AI 剧本配方必须提供故事想法"})
        if shots_per_episode is None:
            raise ValidationError({"shots_per_episode": "AI 剧本配方必须提供每集镜数"})
        if episode_duration_seconds is None:
            raise ValidationError({"episode_duration_seconds": "AI 剧本配方必须提供单集时长"})
        if project.episodes.exists():
            raise ValidationError("已有 Episode 的项目不能启动自动剧本配方；不会覆盖现有生产事实")
    elif imported_script:
        if normalized_idea or normalized_style_hint:
            raise ValidationError("导入剧本配方不能携带 AI 剧本创作输入")
        if structured_import_plan_requested:
            if script_structure_stage is None:
                raise ValidationError({
                    "recipe_version_id": (
                        "只有包含 script_structure/ai 的导入剧本配方才能携带"
                        "计划镜数与单集时长"
                    ),
                })
            if shots_per_episode is None:
                raise ValidationError({
                    "shots_per_episode": "导入剧本结构化计划必须提供每集镜数",
                })
            if episode_duration_seconds is None:
                raise ValidationError({
                    "episode_duration_seconds": "导入剧本结构化计划必须提供单集时长",
                })
    elif downstream_only:
        if script_revision is not None or normalized_idea or normalized_style_hint:
            raise ValidationError("下游生产配方不能携带剧本输入")
        if not project.episodes.exists():
            raise ValidationError("下游生产配方要求项目已存在 Episode")
    if script_revision is not None:
        if script_revision.source.project_id != project.id:
            raise ValidationError("script revision 不属于当前项目")
        if script_revision.status != ScriptRevision.Status.CONFIRMED:
            raise ValidationError("script revision 尚未确认")
    elif imported_script:
        raise ValidationError("该 recipe 需要 confirmed script revision")
    max_episodes = _episode_max_items()
    if not 1 <= int(episode_count) <= max_episodes:
        raise ValidationError({
            "episode_count": f"必须在 1 到 {max_episodes} 之间",
        })
    structured_shot_duration_contract = None
    structured_contract_route = None
    if ai_script:
        structured_contract_route = {
            "source": "structured_ai_plan",
            "stage_key": "script_generate",
            "stage_mode": "ai",
            "model_policy_key": "series_concept",
        }
    elif structured_import_plan_requested:
        structured_contract_route = {
            "source": "confirmed_revision_structure_plan",
            "stage_key": "script_structure",
            "stage_mode": "ai",
            "model_policy_key": "script_structure",
        }
    if structured_contract_route is not None:
        merged_overrides, structured_shot_duration_contract = (
            _apply_structured_shot_duration_contract(
                stages=stages,
                episode_count=int(episode_count),
                shots_per_episode=int(shots_per_episode),
                episode_duration_seconds=int(episode_duration_seconds),
                merged_overrides=merged_overrides,
                validated_overrides=validated_overrides,
                **structured_contract_route,
            )
        )
        validated_overrides = validate_config_override_values(
            ConfigRevision.ScopeType.RUN,
            merged_overrides,
        )
        effective_values = {**base_config.values, **validated_overrides}
        try:
            frozen_duration_policy = video_shot_duration_policy(
                values=effective_values,
            )
        except ConfigurationResolutionError as exc:
            raise ValidationError({
                "config_overrides": str(exc),
            }) from exc
        expected_duration_policy = {
            "default_duration_ms": structured_shot_duration_contract["default_duration_ms"],
            "minimum_duration_ms": structured_shot_duration_contract["minimum_duration_ms"],
            "maximum_duration_ms": structured_shot_duration_contract["maximum_duration_ms"],
        }
        if frozen_duration_policy != expected_duration_policy:
            raise ValidationError({
                "config_overrides": (
                    "结构化脚本计划的镜头时长合同与最终运行配置不一致"
                ),
            })
    requested_episode_ids = sorted({str(item) for item in (episode_ids or [])})
    if requested_episode_ids:
        owned_episode_ids = {
            str(item)
            for item in Episode.objects.filter(
                project=project,
                pk__in=requested_episode_ids,
            ).values_list("id", flat=True)
        }
        invalid = sorted(set(requested_episode_ids) - owned_episode_ids)
        if invalid:
            raise ValidationError({"episode_ids": "包含不属于当前项目的 Episode"})
    else:
        requested_episode_ids = sorted(
            str(item)
            for item in Episode.objects.filter(project=project).values_list("id", flat=True)
        )
    project_execution_contract = _freeze_project_execution_contract(project)
    project_execution_contract_sha256 = _sha256(project_execution_contract)
    resolved_workflow_selection = _resolve_workflow_selection(
        project=project,
        stages=stages,
        selected_episode_ids=requested_episode_ids,
        requested=workflow_selection,
        default_conditioning_mode=project_execution_contract[
            "default_conditioning_mode"
        ],
        default_shot_media_type=str(
            project_creation_policy(values=effective_values)[
                "default_shot_media_type"
            ]
        ),
    )
    if unattended:
        selection_route = (
            resolved_workflow_selection.get("route")
            if isinstance(resolved_workflow_selection, dict) else None
        )
        try:
            default_mode = normalize_default_conditioning_mode(
                project_execution_contract["default_conditioning_mode"],
            )
        except ValueError as exc:
            raise ProductionRunCreateRejected(
                {
                    "project_id": (
                        "unattended 运行要求项目配置有效的 default_conditioning_mode"
                    ),
                },
                code="project_default_conditioning_mode_invalid",
            ) from exc
        if not _route_mode_requires_entity_reference_bible(
            selection_route,
            default_mode,
        ):
            raise ProductionRunCreateRejected(
                {
                    "project_id": (
                        "unattended 动态参考生产要求项目默认条件模式在冻结 route 中明确声明 "
                        "requires_entity_reference_bible=true"
                    ),
                },
                code="unattended_entity_reference_mode_required",
            )
        frozen_reference_inputs = _freeze_unattended_reference_inputs(
            project=project,
            reference_inputs=reference_inputs,
            conditioning_mode_contract=_selected_unattended_conditioning_mode_contract(
                resolved_workflow_selection,
                mode=default_mode,
            ),
        )
    else:
        frozen_reference_inputs = []
    workflow_selection_sha256 = (
        _sha256(resolved_workflow_selection) if resolved_workflow_selection is not None else ""
    )
    bible_prompt_profile_selection = _freeze_bible_prompt_profile_selection(
        stages=stages,
        binding_id=bible_image_prompt_profile_binding_id,
    )
    bible_prompt_profile_selection_sha256 = (
        _sha256(bible_prompt_profile_selection)
        if bible_prompt_profile_selection is not None else ""
    )

    request_payload = {
        "schema_version": 3,
        "request_intent": request_intent,
        "request_intent_sha256": request_intent_sha256,
        "project_id": str(project.id),
        # Freeze the human-owned metadata and current project master at the
        # idempotency boundary. script_review uses these exact values for a
        # compare-with-frozen-base update, so an edit made while AI is running
        # is preserved instead of being overwritten by the generated draft.
        "project_base_title": str(project.title),
        "project_base_logline": str(project.logline),
        "project_base_master_script_sha256": hashlib.sha256(
            str(project.master_script or "").encode("utf-8")
        ).hexdigest(),
        "script_revision_id": str(script_revision.id) if script_revision else None,
        "script_sha256": script_revision.content_sha256 if script_revision else "",
        "recipe_version_id": str(recipe.id),
        "recipe_definition_sha256": recipe.definition_sha256,
        "project_execution_contract": project_execution_contract,
        "project_execution_contract_sha256": project_execution_contract_sha256,
        "allow_external_calls": effective_external,
        "budget_limit": str(budget_limit) if budget_limit is not None else None,
        "max_generation_tasks": effective_task_cap,
        "config_overrides": _snapshot_config_overrides(merged_overrides),
        "script_mode": (
            "ai" if ai_script else "confirmed_revision" if imported_script else "existing_project"
        ),
        "idea": normalized_idea,
        "style_hint": normalized_style_hint,
        "episode_count": int(episode_count),
        "shots_per_episode": int(shots_per_episode) if shots_per_episode is not None else None,
        "episode_duration_seconds": (
            int(episode_duration_seconds) if episode_duration_seconds is not None else None
        ),
        "structured_shot_duration_contract": structured_shot_duration_contract,
        "structured_shot_duration_contract_sha256": (
            _sha256(structured_shot_duration_contract)
            if structured_shot_duration_contract is not None else ""
        ),
        "episode_ids": requested_episode_ids,
        "workflow_selection": resolved_workflow_selection,
        "workflow_selection_sha256": workflow_selection_sha256,
        "bible_image_prompt_profile": bible_prompt_profile_selection,
        "bible_image_prompt_profile_sha256": bible_prompt_profile_selection_sha256,
        "interaction_mode": (
            UNATTENDED_INTERACTION_MODE if unattended else "assisted"
        ),
        "unattended_confirmed": bool(unattended_confirmed),
        "reference_inputs": frozen_reference_inputs,
        "reference_inputs_sha256": (
            _sha256(frozen_reference_inputs) if unattended else ""
        ),
    }
    request_sha = request_intent_sha256
    existing = ProductionRun.objects.select_for_update().filter(
        owner=user,
        idempotency_key=key,
    ).first()
    if existing is not None:
        if existing.request_sha256 != request_sha:
            raise ProductionRunConflict({
                "idempotency_key": "该键已用于不同 ProductionRun 请求",
            })
        _assert_initialized_production_run(existing, stages)
        return existing, False

    empty_contract = json_contract(
        {"schema_version": 1},
        max_bytes=_stage_snapshot_max_bytes(),
    )
    input_contract = json_contract(
        request_payload,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    try:
        with transaction.atomic():
            run = ProductionRun.objects.create(
                owner=user,
                project=project,
                script_revision=script_revision,
                recipe_version=recipe,
                idempotency_key=key,
                request_sha256=request_sha,
                allow_external_calls=effective_external,
                budget_limit=budget_limit,
                max_generation_tasks=effective_task_cap,
                config_snapshot=empty_contract.value,
                config_snapshot_schema_version=empty_contract.schema_version,
                config_snapshot_size_bytes=empty_contract.size_bytes,
                config_snapshot_sha256=empty_contract.sha256,
                input_snapshot=request_payload,
                input_snapshot_schema_version=input_contract.schema_version,
                input_snapshot_size_bytes=input_contract.size_bytes,
                input_snapshot_sha256=input_contract.sha256,
            )
    except IntegrityError:
        existing = ProductionRun.objects.filter(owner=user, idempotency_key=key).first()
        if existing is None:
            raise
        if existing.request_sha256 != request_sha:
            raise ProductionRunConflict({
                "idempotency_key": "该键已用于不同 ProductionRun 请求",
            })
        _assert_initialized_production_run(existing, stages)
        return existing, False
    if merged_overrides:
        revision = create_config_revision(
            scope_type="run",
            run=run,
            project=None,
            values=merged_overrides,
            created_by=user,
            change_summary=f"ProductionRun {run.id} 创建时覆盖",
            clone_published=False,
        )
        revision = publish_config_revision(revision, published_by=user)
        run.config_revision = revision
    resolved = resolve_configuration(run=run)
    if (
        bool(resolved.values["production.allow_external_calls"]) != effective_external
        or int(resolved.values["production.max_generation_tasks"]) != effective_task_cap
    ):
        raise ValidationError("运行配置解析结果与运行字段不一致")
    config_snapshot = resolved.snapshot()
    config_contract = json_contract(
        config_snapshot,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    run.config_snapshot = config_snapshot
    run.config_snapshot_schema_version = config_contract.schema_version
    run.config_snapshot_size_bytes = config_contract.size_bytes
    run.config_snapshot_sha256 = config_contract.sha256
    if run.config_revision_id is None and resolved.revision_ids:
        run.config_revision = ConfigRevision.objects.filter(pk=resolved.revision_ids[-1]).first()
    run.save(update_fields=(
        "config_revision", "config_snapshot", "config_snapshot_schema_version",
        "config_snapshot_size_bytes", "config_snapshot_sha256", "updated_at",
    ))
    stage_rows = []
    empty_stage_snapshot = json_contract({"schema_version": 1}, max_bytes=_stage_snapshot_max_bytes())
    for ordinal, item in enumerate(stages, start=1):
        routing_contract = json_contract(item["routing"], max_bytes=_stage_snapshot_max_bytes())
        stage_workflow_selection = (
            resolved_workflow_selection
            if item["key"] == "media_generation"
            else None
        )
        selected_route_index = (
            int(stage_workflow_selection["route"]["route_index"])
            if stage_workflow_selection is not None
            and stage_workflow_selection.get("mode") == "uniform"
            else 0 if item["routing"].get("mode") == "fixed" else None
        )
        initial_decision = {
            "schema_version": 1,
            "state": (
                "selected_by_unit"
                if stage_workflow_selection is not None
                and stage_workflow_selection.get("mode") == "per_shot"
                else "selected" if selected_route_index is not None else "not_selected"
            ),
            "selected_route_index": selected_route_index,
            "workflow_selection_mode": (
                stage_workflow_selection.get("mode")
                if stage_workflow_selection is not None else ""
            ),
            "workflow_selection_sha256": (
                workflow_selection_sha256 if stage_workflow_selection is not None else ""
            ),
            "failures": [],
            "units": (
                {
                    f"shot:{row['shot_id']}": {
                        "route_index": int(row["route"]["route_index"]),
                    }
                    for row in stage_workflow_selection.get("shot_routes", [])
                }
                if stage_workflow_selection is not None
                and stage_workflow_selection.get("mode") == "per_shot"
                else {}
            ),
        }
        decision_contract = json_contract(initial_decision, max_bytes=_stage_snapshot_max_bytes())
        stage_rows.append(StageRun(
            production_run=run,
            stage_key=item["key"],
            ordinal=ordinal,
            mode=item["mode"],
            idempotency_key=f"{run.id}:{ordinal}:{item['key']}",
            approval_required=item["approval"] == "required",
            decision_policy=item.get("decision_policy") or "",
            decision_policy_sha256=item.get("decision_policy_sha256") or "",
            routing=routing_contract.value,
            routing_schema_version=routing_contract.schema_version,
            routing_size_bytes=routing_contract.size_bytes,
            routing_sha256=routing_contract.sha256,
            routing_decision=decision_contract.value,
            routing_decision_schema_version=decision_contract.schema_version,
            routing_decision_size_bytes=decision_contract.size_bytes,
            routing_decision_sha256=decision_contract.sha256,
            input_snapshot=empty_stage_snapshot.value,
            input_snapshot_schema_version=empty_stage_snapshot.schema_version,
            input_snapshot_size_bytes=empty_stage_snapshot.size_bytes,
            input_snapshot_sha256=empty_stage_snapshot.sha256,
            output_snapshot=empty_stage_snapshot.value,
            output_snapshot_schema_version=empty_stage_snapshot.schema_version,
            output_snapshot_size_bytes=empty_stage_snapshot.size_bytes,
            output_snapshot_sha256=empty_stage_snapshot.sha256,
            selected_route_index=selected_route_index,
        ))
    StageRun.objects.bulk_create(stage_rows)
    return advance_production_run(run), True


def _script_artifact(run: ProductionRun, stage: StageRun) -> GeneratedArtifact:
    revision = run.script_revision
    if revision is None or revision.status != ScriptRevision.Status.CONFIRMED:
        raise StageBlocked("script_revision_missing", "没有已确认的剧本 revision")
    metadata = {
        "schema_version": 1,
        "script_source_id": str(revision.source_id),
        "script_revision_id": str(revision.id),
        "revision": revision.revision,
    }
    contract = json_contract(metadata, max_bytes=_stage_snapshot_max_bytes())
    artifact, _ = GeneratedArtifact.objects.get_or_create(
        manifest_key=f"run:{run.id}:script:{revision.id}",
        defaults={
            "production_run": run,
            "stage_run": stage,
            "kind": GeneratedArtifact.Kind.SCRIPT,
            "role": "confirmed_script",
            "variant_group": "script",
            "variant_index": 1,
            "status": GeneratedArtifact.Status.SELECTED,
            "storage_backend": "database",
            "object_key": f"script-revisions/{revision.id}",
            "mime_type": "text/plain; charset=utf-8",
            "size_bytes": revision.content_size_bytes,
            "sha256": revision.content_sha256,
            "manifest_metadata": metadata,
            "manifest_schema_version": contract.schema_version,
            "manifest_metadata_size_bytes": contract.size_bytes,
            "manifest_metadata_sha256": contract.sha256,
        },
    )
    return artifact


def _validate_storyboard_duration_policy(
    script: str,
    *,
    minimum_duration_ms: int,
    maximum_duration_ms: int,
) -> None:
    """Reject canonical duration drift before deterministic materialization.

    Canonical AI scripts already contain reviewed, exact shot durations.  A
    later configuration snapshot must never silently replace those values with
    its default duration; doing so also corrupts action and dialogue timing.
    Non-canonical/manual scripts continue through the ordinary breakdown
    parser, which can surface its existing warnings to the operator.
    """

    try:
        parsed = parse_canonical_script(script)
    except (ValidationError, ValueError):
        return
    mismatches: list[str] = []
    for episode_index, episode in enumerate(parsed.get("episodes") or [], start=1):
        for shot_index, shot in enumerate(episode.get("shots") or [], start=1):
            duration_ms = int(shot.get("duration_ms") or 0)
            if not minimum_duration_ms <= duration_ms <= maximum_duration_ms:
                mismatches.append(
                    f"第{episode_index}集镜头{shot_index}={duration_ms}ms"
                )
    if mismatches:
        raise StageBlocked(
            "script_duration_policy_mismatch",
            "已确认规范剧本的镜头时长与本次运行冻结的视频时长范围冲突："
            + "、".join(mismatches)
            + f"；允许范围为 {minimum_duration_ms}–{maximum_duration_ms}ms。"
            "系统未改写剧本或创建分镜；请纠正配置后新建 ProductionRun。",
        )


def _execute_storyboard(run: ProductionRun, stage: StageRun) -> str:
    if run.script_revision is None:
        raise StageBlocked("script_revision_missing", "确定性拆稿需要已确认剧本")
    if run.project.episodes.exists():
        raise StageBlocked(
            "existing_storyboard_requires_manual_resolution",
            "项目已存在剧集/分镜；新主路径不会自动删除旧生产事实，请人工清理或新建项目后重试。",
        )
    values = _stage_config_values(stage)
    required = (
        "production.default_shot_duration_ms",
        "production.video_shot_min_duration_ms",
        "production.video_shot_max_duration_ms",
        "production.script_auto_split_trigger_chars",
        "production.script_auto_split_target_chars",
        "production.default_multi_shot",
        "production.default_include_audio",
        "production.default_first_frame_continuity",
        "production.default_last_frame_continuity",
        "production.script_prop_alias_policy",
    )
    missing = [key for key in required if key not in values]
    if missing:
        raise StageBlocked("config_snapshot_missing", f"配置快照缺少: {', '.join(missing)}")
    try:
        creation_policy = project_creation_policy(values=values)
    except ConfigurationResolutionError as exc:
        raise StageBlocked(exc.code, str(exc)) from exc
    _validate_storyboard_duration_policy(
        run.script_revision.content,
        minimum_duration_ms=int(values["production.video_shot_min_duration_ms"]),
        maximum_duration_ms=int(values["production.video_shot_max_duration_ms"]),
    )
    project = run.project
    project.master_script = run.script_revision.content
    project.save(update_fields=("master_script", "updated_at"))
    result = breakdown_script(
        project,
        run.script_revision.content,
        episode_count_hint=_frozen_input_episode_count(run.input_snapshot),
        default_shot_duration_ms=int(values["production.default_shot_duration_ms"]),
        default_first_frame_continuity=bool(values["production.default_first_frame_continuity"]),
        default_last_frame_continuity=bool(values["production.default_last_frame_continuity"]),
        video_shot_min_duration_ms=int(values["production.video_shot_min_duration_ms"]),
        video_shot_max_duration_ms=int(values["production.video_shot_max_duration_ms"]),
        auto_split_trigger_chars=int(values["production.script_auto_split_trigger_chars"]),
        auto_split_target_chars=int(values["production.script_auto_split_target_chars"]),
        multi_shot=bool(values["production.default_multi_shot"]),
        include_audio=bool(values["production.default_include_audio"]),
        default_shot_media_type=str(
            creation_policy["default_shot_media_type"]
        ),
        default_conditioning_mode=_frozen_project_default_conditioning_mode(run),
        prop_alias_policy=deepcopy(
            values["production.script_prop_alias_policy"]
        ),
    )
    sync_consistency_locks(project)
    return f"episodes:{len(result.episodes)}"


def _create_prompt_manifest(run, stage, prompt, *, shot_id: str) -> GeneratedArtifact:
    manifest_key = f"run:{run.id}:prompt:{prompt.id}"
    existing = GeneratedArtifact.objects.select_for_update().filter(
        manifest_key=manifest_key,
    ).first()
    if existing is not None:
        return existing
    variant_group = f"prompt:{shot_id}"
    last_variant_index = GeneratedArtifact.objects.select_for_update().filter(
        production_run=run,
        variant_group=variant_group,
    ).order_by("-variant_index").values_list("variant_index", flat=True).first()
    variant_index = (last_variant_index or 0) + 1
    metadata = {
        "schema_version": 1,
        "prompt_artifact_id": str(prompt.id),
        "content_sha256": prompt.content_hash,
        "shot_id": shot_id,
        "prompt_type": prompt.prompt_type,
    }
    contract = json_contract(metadata, max_bytes=_stage_snapshot_max_bytes())
    return GeneratedArtifact.objects.create(
        manifest_key=manifest_key,
        production_run=run,
        stage_run=stage,
        kind=GeneratedArtifact.Kind.PROMPT,
        role="compiled_generation_prompt",
        variant_group=variant_group,
        variant_index=variant_index,
        status=GeneratedArtifact.Status.SELECTED,
        storage_backend="database",
        object_key=f"prompt-artifacts/{prompt.id}",
        mime_type="text/plain; charset=utf-8",
        size_bytes=len(prompt.prompt_text.encode("utf-8")),
        sha256=prompt.content_hash,
        manifest_metadata=metadata,
        manifest_schema_version=contract.schema_version,
        manifest_metadata_size_bytes=contract.size_bytes,
        manifest_metadata_sha256=contract.sha256,
    )


def _create_bible_prompt_manifest(
    run: ProductionRun,
    stage: StageRun,
    prompt: PromptArtifact,
    *,
    target: dict[str, Any],
) -> GeneratedArtifact:
    target = validate_bible_prompt_target(target)
    metadata = {
        "schema_version": 1,
        "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
        "prompt_artifact_id": str(prompt.id),
        "content_sha256": prompt.content_hash,
        "target": target,
        "source_snapshot_sha256": target["source_snapshot_sha256"],
        "prompt_profile_version_id": (
            str(prompt.prompt_profile_version_id)
            if prompt.prompt_profile_version_id else None
        ),
        "prompt_profile_binding_key": prompt.prompt_profile_binding_key,
        "prompt_profile_binding_id": str(
            (prompt.request_options or {}).get("prompt_profile_binding_id")
            if isinstance(prompt.request_options, dict) else ""
        ),
        "prompt_profile_contract_sha256": prompt.prompt_profile_contract_sha256,
    }
    contract = json_contract(metadata, max_bytes=_stage_snapshot_max_bytes())
    return GeneratedArtifact.objects.create(
        manifest_key=f"run:{run.id}:bible-reference-prompt:{prompt.id}",
        production_run=run,
        stage_run=stage,
        kind=GeneratedArtifact.Kind.PROMPT,
        role="bible_reference_prompt",
        variant_group=(
            f"bible-prompt:{target['entity_type']}:{target['entity_id']}"
        ),
        variant_index=1,
        status=GeneratedArtifact.Status.SELECTED,
        storage_backend="database",
        object_key=f"prompt-artifacts/{prompt.id}",
        mime_type="text/plain; charset=utf-8",
        size_bytes=len(prompt.prompt_text.encode("utf-8")),
        sha256=prompt.content_hash,
        manifest_metadata=metadata,
        manifest_schema_version=contract.schema_version,
        manifest_metadata_size_bytes=contract.size_bytes,
        manifest_metadata_sha256=contract.sha256,
    )


def _bible_entities(project) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    characters = list(
        project.characters.select_related("project").order_by("slug", "id")
    )
    rows.extend(
        ("character", item)
        for item in visual_reference_characters(characters)
    )
    for entity_type, relation in (
        ("scene", project.scenes),
        ("prop", project.props),
    ):
        rows.extend(
            (entity_type, item)
            for item in relation.select_related("project").order_by("slug", "id")
        )
    return rows


def _bible_prompt_rows_for_stage(stage: StageRun) -> list[PromptArtifact]:
    manifests = list(GeneratedArtifact.objects.filter(
        production_run=stage.production_run,
        stage_run=stage,
        kind=GeneratedArtifact.Kind.PROMPT,
        role="bible_reference_prompt",
        status=GeneratedArtifact.Status.SELECTED,
    ).order_by("variant_group", "created_at"))
    ids: list[str] = []
    for artifact in manifests:
        metadata = artifact.manifest_metadata if isinstance(artifact.manifest_metadata, dict) else {}
        target = validate_bible_prompt_target(metadata.get("target"))
        prompt_id = str(metadata.get("prompt_artifact_id") or "")
        if not prompt_id:
            raise StageBlocked("bible_prompt_manifest_stale", "Bible prompt manifest 缺少 PromptArtifact")
        ids.append(prompt_id)
        if artifact.variant_group != f"bible-prompt:{target['entity_type']}:{target['entity_id']}":
            raise StageBlocked("bible_prompt_manifest_stale", "Bible prompt manifest 目标分组已漂移")
    prompts = {
        str(item.id): item
        for item in PromptArtifact.objects.select_related(
            "prompt_profile_version",
        ).filter(pk__in=ids)
    }
    if len(prompts) != len(ids) or len(ids) != len(set(ids)):
        raise StageBlocked("bible_prompt_manifest_stale", "Bible prompt manifest 缺失或重复")
    result = [prompts[item] for item in ids]
    for prompt in result:
        options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
        if options.get("generation_purpose") != BIBLE_IMAGE_GENERATION_PURPOSE:
            raise StageBlocked("bible_prompt_manifest_stale", "Bible PromptArtifact 用途无效")
        validate_bible_prompt_target(options.get("target"))
    return result


def _ensure_bible_image_prompts(
    run: ProductionRun,
    stage: StageRun,
) -> list[PromptArtifact]:
    existing = _bible_prompt_rows_for_stage(stage)
    if existing:
        return existing
    entities = _bible_entities(run.project)
    if not entities:
        raise StageBlocked("bible_entities_missing", "项目没有可生成参考图的 Bible 实体")
    routing = _verify_stage_routing(stage)
    routes = list(routing.get("routes") or [])
    if not routes:
        raise StageBlocked("routing_unconfigured", "Bible 参考图阶段没有冻结 route")
    route_index = stage.selected_route_index
    if route_index is None:
        if routing.get("mode") in {"fixed", "ordered_fallback"}:
            route_index = 0
        else:
            raise StageBlocked(
                "manual_route_selection_required",
                "Bible 参考图阶段必须先选择冻结 route",
                awaiting_approval=True,
            )
    try:
        route = routes[int(route_index)]
        stage_input = _verify_stage_snapshot(stage, "input_snapshot")
        bible_selection = stage_input.get("bible_image_prompt_profile")
        bible_selection_sha256 = str(
            stage_input.get("bible_image_prompt_profile_sha256") or ""
        )
        frozen_rows = (
            bible_selection.get("routes") if isinstance(bible_selection, dict) else None
        )
        matches = [
            item for item in (frozen_rows or [])
            if isinstance(item, dict) and item.get("route_index") == int(route_index)
        ]
        if (
            not isinstance(bible_selection, dict)
            or bible_selection.get("schema_version") != 2
            or not bible_selection_sha256
            or _sha256(bible_selection) != bible_selection_sha256
            or len(matches) != 1
            or matches[0].get("prompt_profile") != route.get("prompt_profile")
        ):
            raise StageBlocked(
                "prompt_profile_selection_snapshot_missing",
                "Bible 阶段缺少哈希校验通过的显式 PromptProfile 选择；必须新建运行。",
            )
        model_profile = ModelProfileVersion.objects.select_related("provider").get(
            pk=route["model_profile_version_id"],
        )
    except (IndexError, KeyError, TypeError, ValueError, ModelProfileVersion.DoesNotExist) as exc:
        raise StageBlocked("frozen_route_unavailable", "Bible 参考图冻结模型版本不可用") from exc
    compiled_rows: list[tuple[str, Any, Any]] = []
    for entity_type, entity in entities:
        try:
            compiled = compile_bible_image_prompt_input(
                project=run.project,
                entity_type=entity_type,
                entity=entity,
                model_profile_version=model_profile,
                frozen_prompt_profile=route.get("prompt_profile"),
            )
        except PromptProfileResolutionError as exc:
            raise StageBlocked(exc.code, str(exc)) from exc
        except (ValidationError, PromptBuildError) as exc:
            raise StageBlocked("bible_prompt_contract_invalid", str(exc)) from exc
        compiled_rows.append((entity_type, entity, compiled))
    candidates = [
        PromptArtifact(
            project=run.project,
            created_by=run.owner,
            prompt_type=PromptArtifact.PromptType.IMAGE,
            model=str(compiled.structured_payload.get("model") or ""),
            conditioning_mode=compiled.requested_conditioning_mode,
            is_multi_shot=compiled.is_multi_shot,
            include_audio=compiled.include_audio,
            total_duration_ms=compiled.total_duration_ms,
            structured_payload=compiled.structured_payload,
            prompt_text=compiled.prompt_text,
            negative_prompt=compiled.negative_prompt,
            content_hash=compiled.content_hash,
        )
        for _entity_type, _entity, compiled in compiled_rows
    ]
    _enforce_prompt_language_gate(candidates)
    created: list[PromptArtifact] = []
    with transaction.atomic():
        for entity_type, entity, compiled in compiled_rows:
            prompt = materialize_compiled_prompt(
                compiled=compiled,
                created_by=run.owner,
                request_options_additions={
                    "production_run_id": str(run.id),
                    "stage_run_id": str(stage.id),
                    "route_index": int(route_index),
                    "routing_sha256": stage.routing_sha256,
                    "config_snapshot_sha256": stage.config_snapshot_sha256,
                },
            )
            options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
            target = validate_bible_prompt_target(options.get("target"))
            if target["entity_type"] != entity_type or target["entity_id"] != str(entity.id):
                raise StageBlocked("bible_prompt_target_mismatch", "Bible Prompt target 与实体不一致")
            _create_bible_prompt_manifest(run, stage, prompt, target=target)
            created.append(prompt)
    return created


def _prompt_execution_conditioning_signature(prompt) -> str:
    """Hash only media facts that can actually distinguish one shot request.

    ``input_fingerprint`` intentionally covers every source field, including
    fields omitted by the selected PromptProfile.  It therefore cannot detect
    the dangerous case where two different storyboards collapse onto the same
    executable conditioning.  Compare the emitted channels and normalized
    reference bindings instead.
    """

    payload = (
        prompt.structured_payload
        if isinstance(getattr(prompt, "structured_payload", None), dict)
        else {}
    )
    mode = str(
        getattr(prompt, "conditioning_mode", "")
        or getattr(prompt, "requested_conditioning_mode", "")
        or ""
    )
    prompt_type = str(getattr(prompt, "prompt_type", "") or "")
    if prompt_type == PromptArtifact.PromptType.VIDEO and not mode:
        raise StageBlocked(
            "prompt_conditioning_mode_missing",
            "视频 PromptArtifact 缺少显式 conditioning_mode。",
        )
    return _sha256({
        "schema_version": 1,
        "prompt_type": prompt_type,
        "mode": mode,
        "positive_prompt": str(getattr(prompt, "prompt_text", "") or ""),
        "negative_prompt": str(getattr(prompt, "negative_prompt", "") or ""),
        "reference_bindings": deepcopy(payload.get("reference_bindings") or []),
    })


def _enforce_distinct_shot_conditioning(prompts: list) -> None:
    """Fail closed when distinct shots would submit the same model request.

    PromptProfile remains the only semantic assembler; this guard never
    silently appends storyboard fields around it.  Operators must fix the
    published strategy (normally by selecting shot.narrative, shot.action or
    shot.detailed_description) so the immutable prompt contract stays honest.
    """

    first_by_signature: dict[str, str] = {}
    duplicate_pairs: list[tuple[str, str]] = []
    for prompt in prompts:
        shot = getattr(prompt, "shot", None)
        shot_id = str(
            getattr(prompt, "shot_id", None)
            or getattr(shot, "id", None)
            or ""
        )
        if not shot_id:
            continue
        signature = _prompt_execution_conditioning_signature(prompt)
        first_shot_id = first_by_signature.setdefault(signature, shot_id)
        if first_shot_id != shot_id:
            duplicate_pairs.append((first_shot_id, shot_id))
    if duplicate_pairs:
        pairs = "; ".join(
            f"{first_id}={second_id}" for first_id, second_id in duplicate_pairs[:20]
        )
        raise StageBlocked(
            "duplicate_shot_conditioning",
            "不同分镜被 PromptProfile 装配成完全相同的可执行提示词/参考输入："
            f"{pairs}。请在已审核策略中加入 shot.narrative、shot.action 或 "
            "shot.detailed_description 等逐镜字段后新建运行。",
        )


def _assert_entity_reference_bible_bindings_ready(
    run: ProductionRun,
    shots: list[Shot],
) -> None:
    required_shots = [
        shot for shot in shots
        if shot.media_type == Shot.MediaType.VIDEO
        and _shot_requires_entity_reference_bible(run, shot)
    ]
    if not required_shots:
        return
    generation_stage = run.stages.filter(
        stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
        status=StageRun.Status.SUCCEEDED,
    ).first()
    review_stage = run.stages.filter(
        stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
        status=StageRun.Status.SUCCEEDED,
    ).first()
    if generation_stage is None or review_stage is None:
        raise StageBlocked(
            "entity_reference_bible_images_required",
            "当前条件模式在 prompt_compile 前必须完成 Bible 参考图生成与审核；"
            "动态参考视频不能替代人物、场景和道具的一致性图片锚点。",
        )
    snapshot = _verify_stage_snapshot(review_stage, "output_snapshot")
    canonical = snapshot.get("canonical")
    if (
        snapshot.get("formal_object_type") != "bible_image_bindings"
        or not isinstance(canonical, dict)
        or _sha256(canonical) != snapshot.get("canonical_sha256")
        or canonical.get("kind") != "bible_image_bindings"
        or canonical.get("generation_stage_id") != str(generation_stage.id)
        or canonical.get("generation_snapshot_sha256")
        != generation_stage.output_snapshot_sha256
    ):
        raise StageBlocked(
            "entity_reference_bible_binding_snapshot_invalid",
            "条件模式所需的 Bible 图片审核快照缺失或已漂移。",
        )
    approved_by_entity = {
        (str(row.get("entity_type") or ""), str(row.get("entity_id") or "")): {
            str(value) for value in (row.get("selected_asset_ids") or [])
        }
        for row in (canonical.get("bindings") or [])
        if isinstance(row, dict)
    }
    for shot in required_shots:
        bindings = (
            shot.reference_bindings
            if isinstance(shot.reference_bindings, dict) else {}
        )
        expected_characters = {
            str(item.id)
            for item in visual_reference_characters(list(shot.characters.all()))
        }
        expected_props = {str(item.id) for item in shot.props.all()}
        expected_scene = str(shot.scene_id) if shot.scene_id else ""
        actual_characters = {
            str(item.get("entity_id") or ""): str(item.get("asset_id") or "")
            for item in (bindings.get("characters") or [])
            if isinstance(item, dict)
        }
        actual_props = {
            str(item.get("entity_id") or ""): str(item.get("asset_id") or "")
            for item in (bindings.get("props") or [])
            if isinstance(item, dict)
        }
        scene_binding = bindings.get("scene")
        actual_scene = (
            str(scene_binding.get("entity_id") or "")
            if isinstance(scene_binding, dict) else ""
        )
        if (
            set(actual_characters) != expected_characters
            or set(actual_props) != expected_props
            or actual_scene != expected_scene
        ):
            raise StageBlocked(
                "entity_reference_image_bindings_incomplete",
                f"Shot {shot.id} 没有逐实体完整绑定人物、场景和道具图片。",
            )
        selected_pairs = [
            *(("character", entity_id, asset_id) for entity_id, asset_id in actual_characters.items()),
            *(("prop", entity_id, asset_id) for entity_id, asset_id in actual_props.items()),
        ]
        if expected_scene:
            selected_pairs.append((
                "scene",
                expected_scene,
                str(scene_binding.get("asset_id") or ""),
            ))
        if not selected_pairs:
            raise StageBlocked(
                "entity_reference_shot_bible_image_missing",
                f"Shot {shot.id} 没有关联任何人物、场景或道具，"
                "因此没有可进入工作流的 Bible 实体参考图。",
            )
        if any(
            not asset_id
            or asset_id not in approved_by_entity.get((entity_type, entity_id), set())
            for entity_type, entity_id, asset_id in selected_pairs
        ):
            raise StageBlocked(
                "entity_reference_image_not_approved",
                f"Shot {shot.id} 使用了未在本 Run Bible 审核中批准的实体图片。",
            )


def _execute_prompt_compile(run: ProductionRun, stage: StageRun) -> str:
    values = _stage_config_values(stage)
    language_gate_mode = str(
        values.get("production.prompt_language_gate") or ""
    ).strip().lower()
    if language_gate_mode not in {"block", "warn"}:
        raise StageBlocked(
            "config_snapshot_missing",
            "阶段配置缺少有效 production.prompt_language_gate",
        )
    selected_ids = [item.id for item in _selected_episodes(run)]
    shots = list(
        run.project.episodes.filter(pk__in=selected_ids).order_by("number")
        .prefetch_related("shots__characters", "shots__props", "shots__reference_assets")
    )
    ordered_shots = [shot for episode in shots for shot in episode.shots.all()]
    if not ordered_shots:
        raise StageBlocked("storyboard_missing", "项目没有可编译的分镜")
    _assert_entity_reference_bible_bindings_ready(run, ordered_shots)
    reviewed_profile_fields_by_shot = _reviewed_profile_fields_by_shot(run)
    generation_task_limit = _effective_generation_task_limit(run)
    if len(ordered_shots) > generation_task_limit:
        raise StageBlocked(
            "generation_task_budget_exceeded",
            f"预计 {len(ordered_shots)} 个生成任务，超过运行预算 {generation_task_limit}。",
        )
    existing_prompts = _prompt_rows_for_run(run)
    if existing_prompts:
        existing_shot_ids = {str(item.shot_id) for item in existing_prompts if item.shot_id}
        expected_shot_ids = {str(item.id) for item in ordered_shots}
        if existing_shot_ids != expected_shot_ids or len(existing_prompts) != len(ordered_shots):
            raise StageBlocked(
                "prompt_manifest_stale",
                "本次运行已有 PromptArtifact 与当前正式分镜不一致；必须新建运行。",
            )
        shots_by_id = {str(item.id): item for item in ordered_shots}
        for prompt in existing_prompts:
            _verify_existing_prompt_media_profile(
                run,
                stage=stage,
                prompt=prompt,
                shot=shots_by_id[str(prompt.shot_id)],
            )
        _enforce_distinct_shot_conditioning(existing_prompts)
        _enforce_prompt_language_gate(existing_prompts)
        return f"prompt-artifacts:{len(existing_prompts)}"
    compiled_rows = []
    for shot in ordered_shots:
        model_profile, selected_media_route_index, selection_sha256 = (
            _selected_media_model_profile(run, shot=shot)
        )
        frozen_conditioning, frozen_conditioning_sha256 = (
            _frozen_media_conditioning_contract_for_shot(run, shot=shot)
        )
        frozen_prompt_profile = _frozen_media_prompt_profile_for_shot(
            stage,
            shot=shot,
        )
        try:
            compiled = compile_prompt_input(
                project=run.project,
                shots=[shot],
                prompt_type=shot.media_type,
                mode=shot.conditioning_mode,
                include_audio=shot.include_audio,
                language_gate_mode=language_gate_mode,
                model_profile_version=model_profile,
                frozen_conditioning_contract=frozen_conditioning,
                frozen_conditioning_contract_sha256=(
                    frozen_conditioning_sha256
                ),
                frozen_frame_policy=_frozen_media_frame_policy_for_shot(
                    run,
                    shot=shot,
                ),
                frozen_prompt_profile=frozen_prompt_profile,
                reviewed_profile_fields=(
                    reviewed_profile_fields_by_shot.get(str(shot.id), [])
                    if reviewed_profile_fields_by_shot is not None
                    else None
                ),
            )
        except PromptProfileResolutionError as exc:
            raise StageBlocked(exc.code, str(exc)) from exc
        except ValidationError as exc:
            raise StageBlocked("prompt_profile_assembly_invalid", str(exc)) from exc
        except PromptBuildError as exc:
            raise StageBlocked("media_prompt_contract_invalid", str(exc)) from exc
        selection_facts = {}
        if selected_media_route_index is not None:
            selection_facts = {
                "selected_media_route_index": selected_media_route_index,
                "workflow_selection_sha256": selection_sha256,
            }
        compiled_rows.append((shot, compiled, selection_facts))

    # A blocked gate is caught by ``advance_production_run`` so writes made
    # before it would otherwise commit.  Evaluate the complete batch against
    # unsaved PromptArtifact-shaped rows before publishing any authoritative
    # PromptArtifact or selected manifest.
    candidate_prompts = [
        PromptArtifact(
            project=compiled.project,
            episode=compiled.episode,
            shot=compiled.shot,
            created_by=run.owner,
            prompt_type=compiled.prompt_type,
            model=str(compiled.structured_payload.get("model") or ""),
            conditioning_mode=compiled.requested_conditioning_mode,
            is_multi_shot=compiled.is_multi_shot,
            include_audio=compiled.include_audio,
            total_duration_ms=compiled.total_duration_ms,
            structured_payload=compiled.structured_payload,
            prompt_text=compiled.prompt_text,
            negative_prompt=compiled.negative_prompt,
            content_hash=compiled.content_hash,
        )
        for _shot, compiled, _selection_facts in compiled_rows
    ]
    _enforce_distinct_shot_conditioning(candidate_prompts)
    _enforce_prompt_language_gate(candidate_prompts)
    # Keep the durable rows and their selected manifests all-or-nothing even
    # though the surrounding stage dispatcher converts exceptions into a
    # persisted blocked/failed stage state.
    with transaction.atomic():
        for shot, compiled, selection_facts in compiled_rows:
            prompt = materialize_compiled_prompt(
                compiled=compiled,
                created_by=run.owner,
                request_options_additions={
                    "production_run_id": str(run.id),
                    "stage_run_id": str(stage.id),
                    "config_snapshot_sha256": stage.config_snapshot_sha256,
                    **selection_facts,
                },
            )
            _create_prompt_manifest(run, stage, prompt, shot_id=str(shot.id))
    return f"prompt-artifacts:{len(ordered_shots)}"


def _enforce_prompt_language_gate(prompts: list[PromptArtifact]) -> None:
    blocked = []
    for prompt in prompts:
        report = evaluate_prompt_language_gate(prompt)
        if report.blocks_enqueue:
            blocked.append({
                "prompt_id": str(prompt.id),
                "shot_id": str(prompt.shot_id or ""),
                "fields": list(report.fields_requiring_english_rewrite),
            })
    if blocked:
        summary = "; ".join(
            f"shot={item['shot_id'] or '-'} fields={','.join(item['fields']) or '-'}"
            for item in blocked[:20]
        )
        raise StageBlocked(
            "prompt_language_gate_blocked",
            f"生产提示词语言门禁阻断 {len(blocked)} 项：{summary}",
        )


def _prompt_rows_for_run(run: ProductionRun) -> list[PromptArtifact]:
    artifacts = GeneratedArtifact.objects.filter(
        production_run=run,
        kind=GeneratedArtifact.Kind.PROMPT,
        role="compiled_generation_prompt",
        status=GeneratedArtifact.Status.SELECTED,
    ).order_by("created_at")
    ids = []
    for artifact in artifacts:
        metadata = artifact.manifest_metadata if isinstance(artifact.manifest_metadata, dict) else {}
        prompt_id = metadata.get("prompt_artifact_id")
        if prompt_id:
            ids.append(prompt_id)
    prompts = {
        str(item.id): item
        for item in PromptArtifact.objects.select_related("shot", "episode").filter(pk__in=ids)
    }
    if len(prompts) != len(set(ids)):
        raise StageBlocked("prompt_manifest_stale", "Prompt manifest 指向的正式记录缺失")
    if len(ids) != len(set(ids)):
        raise StageBlocked("prompt_manifest_stale", "selected Prompt manifest 存在重复正式记录")
    return [prompts[str(item)] for item in ids]


def _load_frozen_media_route(
    route: dict[str, Any],
    *,
    prompt: PromptArtifact,
) -> tuple[WorkflowTemplateVersion | None, ModelDeployment]:
    try:
        deployment = ModelDeployment.objects.select_related(
            "profile_version__provider", "runtime_host",
        ).get(
            pk=route.get("model_deployment_id"),
            status=ModelDeployment.Status.ACTIVE,
        )
    except (ModelDeployment.DoesNotExist, ValueError) as exc:
        raise StageBlocked("frozen_route_unavailable", "冻结 route 的 workflow/deployment 已不可用") from exc
    prompt_options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    if prompt_options.get("generation_purpose") == BIBLE_IMAGE_GENERATION_PURPOSE:
        try:
            frozen_profile = load_frozen_prompt_profile_selection(
                route.get("prompt_profile"),
                model_profile_version=deployment.profile_version,
                operation="image.generate",
                mode="",
                bible_image=True,
            )
        except PromptProfileResolutionError as exc:
            raise StageBlocked(exc.code, str(exc)) from exc
        frozen_reference = route.get("prompt_profile") or {}
        if (
            prompt.prompt_profile_version_id != frozen_profile.id
            or prompt.prompt_profile_binding_key
            != frozen_reference.get("prompt_profile_binding_key")
            or prompt.prompt_profile_contract_sha256
            != frozen_reference.get("prompt_profile_contract_sha256")
            or prompt_options.get("prompt_profile_binding_id")
            != frozen_reference.get("prompt_profile_binding_id")
        ):
            raise StageBlocked(
                "prompt_profile_frozen_selection_invalid",
                "Bible PromptArtifact 与冻结 route 的 PromptProfile 选择不一致；必须新建运行。",
            )
    frozen_runtime_host_id = route.get("runtime_host_id")
    current_runtime_host_id = (
        str(deployment.runtime_host_id) if deployment.runtime_host_id else None
    )
    if current_runtime_host_id != frozen_runtime_host_id:
        raise StageBlocked(
            "frozen_deployment_changed",
            "冻结 deployment 的运行主机绑定已变化；必须新建运行。",
        )
    if deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
        if route.get("workflow_version_id") or route.get("workflow_snapshot"):
            raise StageBlocked("frozen_route_incompatible", "async_rest route 不能绑定 ComfyUI workflow")
        try:
            frozen_async_deployment(route)
        except ValidationError as exc:
            raise StageBlocked("frozen_route_unavailable", str(exc)) from exc
        operation = _async_media_operation(prompt)
        smoke_evidence = route.get("smoke_evidence")
        if not isinstance(smoke_evidence, dict) or not isinstance(smoke_evidence.get(operation), dict):
            raise StageBlocked(
                "async_smoke_evidence_missing",
                f"冻结 async route 缺少 {operation} 的真实烟测证据；必须新建运行。",
            )
        return None, deployment
    try:
        version = WorkflowTemplateVersion.objects.select_related(
            "template", "model_profile_version", "validated_deployment",
        ).get(pk=route.get("workflow_version_id"))
    except (WorkflowTemplateVersion.DoesNotExist, ValueError) as exc:
        raise StageBlocked("frozen_route_unavailable", "冻结 route 的 workflow 已不可用") from exc
    workflow_snapshot = route.get("workflow_snapshot")
    if not isinstance(workflow_snapshot, dict):
        raise StageBlocked("frozen_workflow_snapshot_missing", "冻结 route 缺少可执行 workflow 快照")
    if (
        version.validated_deployment_id != deployment.id
        or route.get("workflow_validated_deployment_id") != str(deployment.id)
    ):
        raise StageBlocked(
            "frozen_workflow_deployment_mismatch",
            "冻结 workflow 的真实 smoke deployment 与所选 deployment 不一致；必须发布新配方。",
        )
    try:
        verify_json_contract(
            workflow_snapshot.get("graph"),
            schema_version=int(workflow_snapshot.get("graph_schema_version") or 0),
            size_bytes=int(workflow_snapshot.get("graph_size_bytes") or -1),
            sha256=str(workflow_snapshot.get("graph_sha256") or ""),
            max_bytes=_workflow_graph_max_bytes(),
        )
        verify_json_contract(
            workflow_snapshot.get("input_contract"),
            schema_version=int(workflow_snapshot.get("input_contract_schema_version") or 0),
            size_bytes=int(workflow_snapshot.get("input_contract_size_bytes") or -1),
            sha256=str(workflow_snapshot.get("input_contract_sha256") or ""),
            max_bytes=_workflow_graph_max_bytes(),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise StageBlocked("frozen_workflow_snapshot_invalid", "冻结 workflow graph/input contract 校验失败") from exc
    template_snapshot = workflow_snapshot.get("template") or {}
    if template_snapshot.get("output_kind") != prompt.prompt_type:
        raise StageBlocked("frozen_route_incompatible", "冻结 route 输出类型与 PromptArtifact 不一致")
    if str(deployment.profile_version_id) != route.get("model_profile_version_id"):
        raise StageBlocked("frozen_deployment_changed", "冻结 deployment 模型绑定已变化")
    if deployment.profile_version.provider.driver_type != route.get("driver_type"):
        raise StageBlocked("frozen_deployment_changed", "冻结 deployment driver 已变化")
    if not deployment.profile_version.provider.is_active:
        raise StageBlocked("frozen_provider_disabled", "冻结 route 的 provider 已停用")
    if deployment.profile_version.status == deployment.profile_version.Status.RETIRED:
        raise StageBlocked("frozen_model_profile_retired", "冻结 route 的 model profile 已被安全停用")
    if credential_binding_sha256(deployment) != route.get("credential_binding_sha256"):
        raise StageBlocked(
            "frozen_credential_binding_changed",
            "冻结 deployment 的凭据绑定已变化；必须新建运行。",
        )
    frozen_config = {
        **(route.get("provider_config") or {}),
        **(route.get("deployment_config") or {}),
    }
    try:
        frozen_endpoint = validate_endpoint(
            str(frozen_config.get("base_url") or ""),
            resolve_dns=False,
        )
    except ValidationError as exc:
        raise StageBlocked(
            "frozen_deployment_security_drift",
            "冻结 deployment endpoint 不再满足当前 SSRF/主机白名单策略。",
        ) from exc
    frozen_hostname = (urlparse(frozen_endpoint).hostname or "").casefold()
    frozen_is_external = frozen_hostname not in {"localhost", "127.0.0.1", "::1"}
    if frozen_is_external is not bool(route.get("is_external")):
        raise StageBlocked(
            "frozen_deployment_security_drift",
            "冻结 deployment 的 endpoint/egress 分类与授权快照不一致。",
        )
    if deployment.profile_version.provider.driver_type != ProviderProfile.DriverType.COMFYUI:
        raise StageBlocked(
            "driver_contract_missing",
            "该媒体 route 尚未接通正式归档适配器；未提交远端任务。",
            awaiting_approval=True,
        )
    return version, deployment


def _async_media_operation(prompt: PromptArtifact) -> str:
    return (
        "image.text_to_image"
        if prompt.prompt_type == PromptArtifact.PromptType.IMAGE
        else "video.text_to_video"
    )


# Comfy workflow contracts and the browser/API boundary use JSON-interoperable
# integers.  Keep generated seeds inside the exact IEEE-754 integer range so a
# frozen ``sampling.seed`` contract can always compile without precision loss.
_MAX_MEDIA_EXECUTION_SEED = (1 << 53) - 1


def _media_execution_identity(
    *,
    run: ProductionRun,
    prompt: PromptArtifact,
    variant_number: int,
) -> dict[str, Any]:
    """Freeze a collision-resistant stochastic/output identity per media unit."""

    seed_material = {
        "schema_version": 1,
        "purpose": "media_execution_seed",
        "production_run_id": str(run.id),
        "project_id": str(prompt.project_id),
        "project_continuity_seed": int(prompt.project.continuity_seed),
        "prompt_artifact_id": str(prompt.id),
        "shot_id": str(prompt.shot_id or ""),
        "variant_number": int(variant_number),
    }
    seed = int(_sha256(seed_material)[:16], 16) % (
        _MAX_MEDIA_EXECUTION_SEED + 1
    )
    if seed == 0:
        seed = 1
    output_prefix = (
        f"autoprompt/{prompt.project_id}/{run.id}/{prompt.id}/"
        f"variant-{variant_number}"
    )
    return {
        "schema_version": 1,
        "prompt_artifact_id": str(prompt.id),
        "shot_id": str(prompt.shot_id or ""),
        "variant_number": int(variant_number),
        "seed": seed,
        "output_prefix": output_prefix,
    }


def _async_media_payload(
    *,
    run: ProductionRun,
    prompt: PromptArtifact,
    variant_number: int,
    deployment: ModelDeployment,
    contract: dict[str, Any],
    attempt_id,
    width: int,
    height: int,
    execution_seed: int | None = None,
) -> dict[str, Any]:
    properties = contract["request_schema"]["properties"]
    standard = {
        "model": deployment.profile_version.model_identifier,
        "prompt": prompt.prompt_text,
        "negative_prompt": prompt.negative_prompt,
        "width": width,
        "height": height,
        "duration_seconds": prompt.total_duration_ms / 1000,
        "fps": prompt.project.fps,
        "seed": (
            int(execution_seed)
            if execution_seed is not None
            else int(prompt.project.continuity_seed) + variant_number - 1
        ),
        "audio": bool(prompt.include_audio),
    }
    payload = {
        key: value for key, value in standard.items()
        if key == "model" or key in properties
    }
    # The key is persisted before the POST and may only be emitted through the
    # configured idempotency header by RestrictedAsyncRestDriver.
    payload.update({
        "schema_version": 1,
        "idempotency_key": f"production-attempt:{attempt_id}",
    })
    validate_standard_payload(contract, payload)
    return payload


def _freeze_media_execution_capacity(
    *,
    deployment: ModelDeployment,
    workflow_snapshot: dict[str, Any],
    values: dict[str, Any],
    config_snapshot_sha256: str,
) -> dict[str, Any]:
    configured_concurrency = int(values["production.default_concurrency"])
    hard_concurrency_ceiling = min(
        int(settings.TASK_MAX_CONCURRENT),
        boundary_int("security.production.max_concurrency"),
    )
    configured_task_timeout = float(values["production.task_timeout_seconds"])
    configured_idle_timeout = float(values["runtime.task_idle_timeout_seconds"])
    frozen_at = timezone.now()

    host_snapshot = None
    host_compatible_concurrency = None
    if deployment.runtime_host_id is not None:
        host = deployment.runtime_host
        if host.status != host.Status.ACTIVE:
            raise StageBlocked(
                "runtime_host_unavailable",
                "本地媒体 deployment 的运行主机未处于 active 状态",
            )
        host_snapshot = HostCapabilitySnapshot.objects.filter(
            host_id=deployment.runtime_host_id,
            probe_status__in=(
                HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
                HostCapabilitySnapshot.ProbeStatus.PARTIAL,
            ),
            expires_at__gt=frozen_at,
        ).order_by("-revision").first()
        if host_snapshot is None:
            raise StageBlocked(
                "host_capability_snapshot_missing",
                "本地媒体 deployment 缺少新鲜的主机能力快照",
            )
        try:
            verify_host_snapshot(host_snapshot)
            requirements = (
                workflow_snapshot.get("model_requirements")
                if isinstance(workflow_snapshot, dict) else None
            )
            if not isinstance(requirements, list):
                raise ValidationError("冻结 workflow 缺少 model requirements")
            required_rows = [
                item for item in requirements
                if isinstance(item, dict) and bool(item.get("is_required", True))
            ]
            artifact_ids = {
                str(item.get("artifact_id") or "") for item in required_rows
            }
            if not required_rows or "" in artifact_ids:
                raise ValidationError("冻结 workflow required artifact 未完整绑定")
            artifacts = {
                str(item.id): item
                for item in deployment.profile_version.artifacts.filter(pk__in=artifact_ids)
            }
            if set(artifacts) != artifact_ids:
                raise ValidationError("冻结 workflow required artifact 不属于 route model profile")
            for requirement in required_rows:
                artifact = artifacts[str(requirement["artifact_id"])]
                if (
                    artifact.filename != str(requirement.get("expected_filename") or "")
                    or not artifact.sha256
                    or artifact.sha256 != str(requirement.get("expected_sha256") or "")
                    or artifact.role != str(requirement.get("role") or "")
                ):
                    raise ValidationError("冻结 workflow required artifact 声明已漂移")
            for artifact in artifacts.values():
                if artifact.status != artifact.Status.AVAILABLE:
                    raise ValidationError("本地媒体模型 artifact 尚未核验")
                verified_artifact_availability(
                    snapshot=host_snapshot,
                    deployment=deployment,
                    artifact=artifact,
                )
            tiers = values["recommendation.vram_tiers"]
            _short_edge, host_compatible_concurrency = host_resolution_and_concurrency(
                host_snapshot,
                tiers,
            )
            host_compatible_concurrency = int(host_compatible_concurrency)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise StageBlocked(
                "host_capability_snapshot_invalid",
                "主机能力快照或冻结的显存并发规则无效",
            ) from exc

    concurrency_limits = [configured_concurrency, hard_concurrency_ceiling]
    if host_compatible_concurrency is not None:
        concurrency_limits.append(host_compatible_concurrency)
    effective_concurrency = min(concurrency_limits)
    effective_task_timeout = configured_task_timeout
    if (
        effective_concurrency < 1
        or effective_task_timeout <= 0
        or configured_idle_timeout <= 0
        or configured_idle_timeout > effective_task_timeout
    ):
        raise StageBlocked(
            "production_config_invalid",
            "冻结的并发或任务超时配置无效",
        )

    return {
        "schema_version": 3,
        "frozen_at": frozen_at.isoformat(),
        "configured_concurrency": configured_concurrency,
        "host_compatible_concurrency": host_compatible_concurrency,
        "hard_concurrency_ceiling_at_freeze": hard_concurrency_ceiling,
        "effective_concurrency": effective_concurrency,
        "runtime_host_id": (
            str(deployment.runtime_host_id) if deployment.runtime_host_id else None
        ),
        "host_capability_snapshot_id": (
            str(host_snapshot.id) if host_snapshot is not None else None
        ),
        "host_capability_snapshot_revision": (
            host_snapshot.revision if host_snapshot is not None else None
        ),
        "host_capability_snapshot_probe_status": (
            host_snapshot.probe_status if host_snapshot is not None else None
        ),
        "host_capability_snapshot_sha256": (
            host_snapshot.details_sha256 if host_snapshot is not None else ""
        ),
        "host_capability_snapshot_captured_at": (
            host_snapshot.captured_at.isoformat() if host_snapshot is not None else None
        ),
        "host_capability_snapshot_expires_at": (
            host_snapshot.expires_at.isoformat()
            if host_snapshot is not None and host_snapshot.expires_at is not None
            else None
        ),
        "configured_task_timeout_seconds": configured_task_timeout,
        "effective_task_timeout_seconds": effective_task_timeout,
        "task_idle_timeout_seconds": configured_idle_timeout,
        "config_snapshot_sha256": config_snapshot_sha256,
    }


@transaction.atomic
def _create_media_tasks(
    run: ProductionRun,
    stage: StageRun,
    *,
    unit_after_route: dict[str, int] | None = None,
    bible_variant_numbers: dict[tuple[str, str], list[int]] | None = None,
    generation_intent: dict[str, Any] | None = None,
    approved_unknown_cost_total: Decimal | None | object = _USE_STAGE_APPROVED_COST,
    approved_unknown_cost_currency: str = "",
) -> list[GenerationAttempt]:
    # A pricing/egress gate may be raised after some units have been prepared.
    # Keep materialization behind a savepoint so a caught StageBlocked cannot
    # commit taskless attempts or a partially reserved batch.
    is_bible_generation = stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY
    if bible_variant_numbers is not None and (
        not is_bible_generation or unit_after_route is not None
    ):
        raise ValidationError(
            "显式 Bible variant 只允许用于 Bible 候选追加生成，且不能与 fallback 混用",
        )
    if generation_intent is not None and bible_variant_numbers is None:
        raise ValidationError("generation_intent 必须绑定显式 Bible variant")
    prompts = (
        _ensure_bible_image_prompts(run, stage)
        if is_bible_generation else _prompt_rows_for_run(run)
    )
    if not prompts:
        raise StageBlocked("prompt_artifacts_missing", "没有本次运行的正式 PromptArtifact")
    if not is_bible_generation:
        _enforce_distinct_shot_conditioning(prompts)
    _enforce_prompt_language_gate(prompts)
    routing = _verify_stage_routing(stage)
    variant_count = routing["variant_count"]
    bible_targets_by_prompt_id: dict[str, dict[str, Any]] = {}
    if is_bible_generation:
        for prompt in prompts:
            options = (
                prompt.request_options
                if isinstance(prompt.request_options, dict) else {}
            )
            bible_targets_by_prompt_id[str(prompt.id)] = validate_bible_prompt_target(
                options.get("target"),
            )
    if bible_variant_numbers is not None:
        available_keys = {
            (target["entity_type"], target["entity_id"])
            for target in bible_targets_by_prompt_id.values()
        }
        if set(bible_variant_numbers) - available_keys:
            raise ValidationError("追加生成目标不属于冻结的 Bible Prompt 清单")
        for key, variants in bible_variant_numbers.items():
            if (
                not variants
                or len(variants) != len(set(variants))
                or any(
                    type(item) is not int
                    or not 1 <= item <= _bible_candidate_max_items()
                    for item in variants
                )
            ):
                raise ValidationError(f"Bible 实体 {key[0]}:{key[1]} 的追加 variant 无效")
    requested_tasks = (
        len(unit_after_route)
        if unit_after_route is not None
        else sum(len(items) for items in bible_variant_numbers.values())
        if bible_variant_numbers is not None
        else len(prompts) * variant_count
    )
    if requested_tasks <= 0:
        raise ValidationError("没有需要物化的生成任务")
    if run.generation_task_count + requested_tasks > _effective_generation_task_limit(run):
        raise StageBlocked("generation_task_budget_exceeded", "物化任务会超过本次运行任务预算")
    queue_guidance = capture_queue_capacity_advisory(
        run.project.owner,
        additional=requested_tasks,
        project=run.project,
    )
    attempts = []
    planned_unit_index = 0
    for prompt in prompts:
        bible_target = bible_targets_by_prompt_id.get(str(prompt.id))
        variants = (
            bible_variant_numbers.get(
                (bible_target["entity_type"], bible_target["entity_id"]),
                [],
            )
            if bible_variant_numbers is not None and bible_target is not None
            else range(1, variant_count + 1)
        )
        for variant_number in variants:
            unit_key = (
                bible_candidate_unit_key(
                    bible_target["entity_type"],
                    bible_target["entity_id"],
                    variant_number,
                )
                if bible_target is not None
                else f"prompt:{prompt.id}:variant:{variant_number}"
            )
            if unit_after_route is not None and unit_key not in unit_after_route:
                continue
            planned_unit_index += 1
            approved_unknown_cost = (
                _allocated_stage_unknown_cost(
                    (
                        stage.approved_estimated_cost
                        if approved_unknown_cost_total is _USE_STAGE_APPROVED_COST
                        else approved_unknown_cost_total
                    ),
                    unit_index=planned_unit_index,
                    unit_count=requested_tasks,
                )
                if unit_after_route is None else None
            )
            approved_unknown_currency = (
                stage.approved_cost_currency
                if approved_unknown_cost_total is _USE_STAGE_APPROVED_COST
                else str(approved_unknown_cost_currency or "")
            )
            after_route_index = (
                unit_after_route[unit_key] if unit_after_route is not None else None
            )
            candidates = _route_candidates(
                stage,
                unit_key=unit_key,
                prompt=prompt,
                after_route_index=after_route_index,
            )
            attempt_number = (
                stage.generation_attempts.filter(unit_key=unit_key)
                .aggregate(value=Max("attempt_number"))["value"]
                or 0
            ) + 1
            selected = None
            last_error = None
            for route in candidates:
                try:
                    version, deployment = _load_frozen_media_route(route, prompt=prompt)
                except StageBlocked as exc:
                    _record_route_failure(
                        stage,
                        unit_key=unit_key,
                        route_index=int(route["index"]),
                        code=exc.code,
                    )
                    last_error = exc
                    if routing.get("mode") != "ordered_fallback":
                        raise
                    continue
                selected = (route, version, deployment)
                break
            if selected is None:
                raise last_error or StageBlocked(
                    "routing_candidates_exhausted",
                    "ordered_fallback 的所有声明 route 均不可用。",
                )
            route, version, deployment = selected
            values = _stage_config_values(stage, route_index=int(route["index"]))
            required_config = (
                "production.task_max_attempts",
                "production.default_concurrency",
                "production.task_timeout_seconds",
                "runtime.task_idle_timeout_seconds",
                "production.default_resolution_width",
                "production.default_resolution_height",
            )
            if deployment.runtime_host_id is not None:
                required_config += ("recommendation.vram_tiers",)
            missing_config = [key for key in required_config if key not in values]
            if missing_config:
                raise StageBlocked(
                    "config_snapshot_missing",
                    "路由配置快照缺少: " + ", ".join(missing_config),
                )
            requested_width = int(values["production.default_resolution_width"])
            requested_height = int(values["production.default_resolution_height"])
            if requested_width <= 0 or requested_height <= 0:
                raise StageBlocked(
                    "production_config_invalid",
                    "冻结的分辨率配置无效",
                )
            width, height = _effective_route_resolution(
                route,
                requested_width=requested_width,
                requested_height=requested_height,
            )
            try:
                validate_media_execution_settings(
                    constraints=route.get("media_constraints"),
                    width=width,
                    height=height,
                    fps=int(prompt.project.fps),
                    duration_ms=int(prompt.total_duration_ms or 0),
                    media_type=prompt.prompt_type,
                )
            except ValidationError as exc:
                raise StageBlocked(
                    "media_execution_settings_invalid",
                    f"冻结媒体路由与实际执行参数不兼容: {exc}",
                ) from exc
            execution_identity = _media_execution_identity(
                run=run,
                prompt=prompt,
                variant_number=variant_number,
            )
            unit_generation_intent = (
                {
                    **deepcopy(generation_intent),
                    "entity_type": bible_target["entity_type"],
                    "entity_id": bible_target["entity_id"],
                    "variant_number": variant_number,
                }
                if generation_intent is not None and bible_target is not None
                else None
            )
            if deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
                operation = _async_media_operation(prompt)
                frozen_deployment = frozen_async_deployment(route)
                contract = async_operation_contract(frozen_deployment.profile_version, operation)
                attempt_id = uuid.uuid4()
                payload = _async_media_payload(
                    run=run,
                    prompt=prompt,
                    variant_number=variant_number,
                    deployment=frozen_deployment,
                    contract=contract,
                    attempt_id=attempt_id,
                    width=width,
                    height=height,
                    execution_seed=int(execution_identity["seed"]),
                )
                request_contract = json_contract({
                    "schema_version": 1,
                    "transport": "restricted_async_rest",
                    "operation": operation,
                    "output_kind": contract["output_kind"],
                    "prompt_artifact_id": str(prompt.id),
                    "prompt_content_hash": prompt.content_hash,
                    "prompt_profile_selection": deepcopy(
                        (prompt.request_options or {}).get("prompt_profile_selection")
                    ),
                    "prompt_profile_selection_sha256": str(
                        (prompt.request_options or {}).get(
                            "prompt_profile_selection_sha256",
                        ) or ""
                    ),
                    **({
                        "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
                        "target": deepcopy(bible_target),
                        "source_snapshot_sha256": bible_target["source_snapshot_sha256"],
                    } if bible_target is not None else {}),
                    **({
                        "generation_intent": unit_generation_intent,
                    } if unit_generation_intent is not None else {}),
                    "variant_number": variant_number,
                    "execution_identity": deepcopy(execution_identity),
                    "route": deepcopy(route),
                    "payload": payload,
                    "execution_deadlines": {
                        "schema_version": 1,
                        "hard_timeout_seconds": float(values["production.task_timeout_seconds"]),
                        "idle_timeout_seconds": float(values["runtime.task_idle_timeout_seconds"]),
                    },
                }, max_bytes=_stage_snapshot_max_bytes())
                attempt = _create_generation_attempt(
                    id=attempt_id,
                    stage_run=stage,
                    unit_key=unit_key,
                    attempt_number=attempt_number,
                    model_deployment=deployment,
                    prompt_profile_version=prompt.prompt_profile_version,
                    prompt_profile_binding_key=prompt.prompt_profile_binding_key,
                    prompt_profile_contract_sha256=prompt.prompt_profile_contract_sha256,
                    route_index=int(route["index"]),
                    operation=operation,
                    is_external=bool(route.get("is_external")),
                    status=GenerationAttempt.Status.QUEUED,
                    request_sha256=request_contract.sha256,
                    request_snapshot=request_contract.value,
                    request_snapshot_schema_version=request_contract.schema_version,
                    request_snapshot_size_bytes=request_contract.size_bytes,
                    request_snapshot_sha256=request_contract.sha256,
                )
                _estimate_and_reserve(
                    run,
                    stage,
                    attempt,
                    deployment=frozen_deployment,
                    operation=operation,
                    payload=payload,
                    is_external=bool(route.get("is_external")),
                    approved_unknown_cost=approved_unknown_cost,
                    approved_unknown_currency=approved_unknown_currency,
                )
                _mark_route_selected(
                    stage,
                    unit_key=unit_key,
                    route_index=int(route["index"]),
                    reason=(
                        "manual_choice" if routing.get("mode") == "manual_choice"
                        else "fixed_route" if routing.get("mode") == "fixed"
                        else "first_declared_compatible_route"
                    ),
                )
                attempts.append(attempt)
                continue
            execution_capacity = _freeze_media_execution_capacity(
                deployment=deployment,
                workflow_snapshot=route.get("workflow_snapshot") or {},
                values=values,
                config_snapshot_sha256=stage.config_snapshot_sha256,
            )
            workflow_snapshot = route.get("workflow_snapshot") or {}
            try:
                compiler_overrides = build_frozen_workflow_runtime_overrides(
                    workflow_snapshot=workflow_snapshot,
                    prompt=prompt,
                    width=width,
                    height=height,
                    seed=int(execution_identity["seed"]),
                    output_prefix=str(execution_identity["output_prefix"]),
                )
                compiler_duration_policy = (
                    video_shot_duration_policy(values=values)
                    if prompt.prompt_type == PromptArtifact.PromptType.VIDEO
                    else None
                )
                compiled_workflow = compile_frozen_workflow(
                    workflow_snapshot=route["workflow_snapshot"],
                    prompt=prompt,
                    project=prompt.project,
                    overrides=compiler_overrides,
                    video_duration_policy=compiler_duration_policy,
                )
            except ConfigurationResolutionError as exc:
                raise StageBlocked(
                    exc.code,
                    f"冻结的视频分镜时长配置无效: {exc}",
                ) from exc
            except WorkflowCompilerError as exc:
                raise StageBlocked(
                    "workflow_semantic_compile_failed",
                    f"冻结 workflow semantic 编译失败: {exc}",
                ) from exc
            execution_monitoring = _execution_monitoring_from_frozen_route(route)
            execution_monitoring_sha256 = execution_monitoring_contract_sha256(
                execution_monitoring,
            )
            route_snapshot_sha256 = _sha256(route)
            request_contract = json_contract({
                "schema_version": 1,
                "operation": "media.generate",
                "prompt_artifact_id": str(prompt.id),
                "prompt_content_hash": prompt.content_hash,
                "prompt_profile_selection": deepcopy(
                    (prompt.request_options or {}).get("prompt_profile_selection")
                ),
                "prompt_profile_selection_sha256": str(
                    (prompt.request_options or {}).get(
                        "prompt_profile_selection_sha256",
                    ) or ""
                ),
                **({
                    "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
                    "target": deepcopy(bible_target),
                    "source_snapshot_sha256": bible_target["source_snapshot_sha256"],
                } if bible_target is not None else {}),
                **({
                    "generation_intent": unit_generation_intent,
                } if unit_generation_intent is not None else {}),
                "compiled_workflow_sha256": compiled_workflow.graph_sha256,
                "workflow_contract_sha256": compiled_workflow.workflow_contract_sha256,
                "resolved_semantics_sha256": compiled_workflow.resolved_semantics_sha256,
                "asset_bindings_sha256": compiled_workflow.asset_bindings_sha256,
                "model_requirements_sha256": compiled_workflow.model_requirements_sha256,
                "route_snapshot_sha256": route_snapshot_sha256,
                "model_deployment_id": str(deployment.id),
                "model_profile_version_id": str(deployment.profile_version_id),
                "provider_profile_id": str(deployment.profile_version.provider_id),
                "config_snapshot_sha256": stage.config_snapshot_sha256,
                "variant_number": variant_number,
                "execution_identity": deepcopy(execution_identity),
                "execution_monitoring": deepcopy(execution_monitoring),
                "execution_monitoring_sha256": execution_monitoring_sha256,
                "output_contract": compiled_workflow.output_contract,
            }, max_bytes=_stage_snapshot_max_bytes())
            deployment_summary = {
                "schema_version": 1,
                "route_index": int(route["index"]),
                "model_deployment_id": str(deployment.id),
                "model_profile_version_id": str(deployment.profile_version_id),
                "provider_profile_id": str(deployment.profile_version.provider_id),
                "provider_config_sha256": route["provider_config_sha256"],
                "deployment_config_sha256": route["deployment_config_sha256"],
                "credential_binding_sha256": route["credential_binding_sha256"],
                "endpoint_is_external": bool(route["is_external"]),
                "effective_timeout_seconds": route["effective_timeout_seconds"],
                "execution_monitoring_sha256": execution_monitoring_sha256,
            }
            attempt = _create_generation_attempt(
                stage_run=stage,
                unit_key=unit_key,
                attempt_number=attempt_number,
                model_deployment=deployment,
                prompt_profile_version=prompt.prompt_profile_version,
                prompt_profile_binding_key=prompt.prompt_profile_binding_key,
                prompt_profile_contract_sha256=prompt.prompt_profile_contract_sha256,
                route_index=int(route["index"]),
                operation="media.generate",
                is_external=bool(route.get("is_external")),
                status=GenerationAttempt.Status.QUEUED,
                request_sha256=request_contract.sha256,
                request_snapshot=request_contract.value,
                request_snapshot_schema_version=request_contract.schema_version,
                request_snapshot_size_bytes=request_contract.size_bytes,
                request_snapshot_sha256=request_contract.sha256,
            )
            _estimate_and_reserve(
                run,
                stage,
                attempt,
                deployment=deployment,
                operation="media.generate",
                payload={
                    "schema_version": 1,
                    "prompt_sha256": prompt.content_hash,
                    "output_kind": prompt.prompt_type,
                    "variant_number": variant_number,
                },
                is_external=bool(route.get("is_external")),
                approved_unknown_cost=approved_unknown_cost,
                approved_unknown_currency=approved_unknown_currency,
            )
            workflow = materialize_compiled_workflow_version(
                user=run.project.owner,
                workflow_version=version,
                prompt=prompt,
                compiled=compiled_workflow,
                route_snapshot_sha256=route_snapshot_sha256,
                workflow_snapshot=route["workflow_snapshot"],
            )
            execution_metadata = (
                (workflow.injected_values or {}).get("execution_metadata")
            )
            if (
                not isinstance(execution_metadata, dict)
                or execution_metadata.get("execution_monitoring")
                != execution_monitoring
                or execution_metadata.get("execution_monitoring_sha256")
                != execution_monitoring_sha256
            ):
                raise StageBlocked(
                    "execution_monitoring_snapshot_drift",
                    "生成任务的执行监控合同与冻结媒体路由不一致",
                )
            task = materialize_generation_task(
                user=run.project.owner,
                workflow=workflow,
                priority=0,
                max_attempts=int(values["production.task_max_attempts"]),
                capability_snapshot_overrides={
                    "queue_guidance": deepcopy(queue_guidance),
                    "production_run_id": str(run.id),
                    "stage_run_id": str(stage.id),
                    "model_deployment_id": str(deployment.id),
                    "model_profile_version_id": str(deployment.profile_version_id),
                    "provider_profile_id": str(deployment.profile_version.provider_id),
                    "provider_config_sha256": route["provider_config_sha256"],
                    "deployment_config_sha256": route["deployment_config_sha256"],
                    "provider_config": route["provider_config"],
                    "deployment_config": route["deployment_config"],
                    "frozen_execution_config_sha256": _sha256({
                        "provider_config": route["provider_config"],
                        "deployment_config": route["deployment_config"],
                    }),
                    "credential_binding_sha256": route["credential_binding_sha256"],
                    "endpoint_is_external": bool(route["is_external"]),
                    "effective_timeout_seconds": route["effective_timeout_seconds"],
                    "execution_capacity": execution_capacity,
                    "execution_capacity_sha256": _sha256(execution_capacity),
                    "generation_attempt_id": str(attempt.id),
                    "execution_identity": deepcopy(execution_identity),
                    "execution_identity_sha256": _sha256(execution_identity),
                    "route_index": int(route["index"]),
                    "config_snapshot_sha256": stage.config_snapshot_sha256,
                    "deployment_summary": deployment_summary,
                    "deployment_summary_sha256": _sha256(deployment_summary),
                },
            )
            attempt.generation_task = task
            attempt.save(update_fields=("generation_task", "updated_at"))
            _mark_route_selected(
                stage,
                unit_key=unit_key,
                route_index=int(route["index"]),
                reason=(
                    "run_workflow_selection"
                    if _frozen_workflow_selection(stage) is not None
                    else "manual_choice"
                    if routing.get("mode") == "manual_choice"
                    else "fixed_route"
                    if routing.get("mode") == "fixed"
                    else "first_declared_compatible_route"
                ),
            )
            attempts.append(attempt)
    run.generation_task_count += len(attempts)
    run.save(update_fields=("generation_task_count", "updated_at"))
    return attempts


def _route_failure_once(stage: StageRun, attempt: GenerationAttempt, code: str) -> None:
    if not stage.route_events.filter(
        unit_key=attempt.unit_key,
        route_index=attempt.route_index,
        event_type="route_failed",
        code=code[:100],
    ).exists():
        _record_route_failure(
            stage,
            unit_key=attempt.unit_key,
            route_index=int(attempt.route_index),
            code=code,
        )


def _definitive_media_failure(attempt: GenerationAttempt) -> tuple[bool, str]:
    if attempt.status != GenerationAttempt.Status.FAILED:
        return False, attempt.error_code or attempt.status
    if attempt.operation in ASYNC_MEDIA_OPERATIONS:
        code = attempt.error_code or "async_generation_failed"
        return code in {
            "provider_request_rejected",
            "remote_generation_failed",
            "remote_absence_confirmed",
        }, code
    task = attempt.generation_task
    if task is None:
        return False, "generation_task_missing"
    if task.status != GenerationTask.Status.FAILED:
        return False, "generation_task_not_definitive_failure"
    if task.outputs or attempt.artifacts.exists() or task.generated_artifacts.exists():
        return False, "generation_result_or_archive_present"
    if (
        task.submission_state == GenerationTask.SubmissionState.UNPREPARED
        and not task.submission_token
        and not task.comfy_prompt_id
    ):
        return True, "comfy_pre_submit_failed"
    if task.submission_state == GenerationTask.SubmissionState.TERMINAL_FAILED:
        return True, "comfy_remote_terminal_failed"
    return False, "comfy_submission_state_uncertain"


def _queue_ordered_media_fallback(
    run: ProductionRun,
    stage: StageRun,
    failures: list[tuple[GenerationAttempt, str]],
) -> bool:
    routing = _verify_stage_routing(stage)
    if (
        routing.get("mode") != "ordered_fallback"
        or not failures
        or _frozen_workflow_selection(stage) is not None
    ):
        return False
    eligible: dict[str, int] = {}
    failure_codes: dict[str, str] = {}
    for attempt, _reason in failures:
        definitive, code = _definitive_media_failure(attempt)
        if not definitive or attempt.route_index is None:
            return False
        eligible[attempt.unit_key] = int(attempt.route_index)
        failure_codes[attempt.unit_key] = code
    for attempt, _reason in failures:
        _route_failure_once(stage, attempt, failure_codes[attempt.unit_key])
    try:
        created = _create_media_tasks(
            run,
            stage,
            unit_after_route=eligible,
        )
    except StageBlocked as exc:
        if exc.code == "routing_candidates_exhausted":
            for attempt, _reason in failures:
                if not stage.route_events.filter(
                    unit_key=attempt.unit_key,
                    event_type="routes_exhausted",
                ).exists():
                    _append_route_event(
                        stage,
                        unit_key=attempt.unit_key,
                        event_type="routes_exhausted",
                        code=exc.code,
                    )
        raise
    return bool(created)


def _poll_media_stage(run: ProductionRun, stage: StageRun) -> str | None:
    run.refresh_from_db(fields=("actual_cost_total", "budget_limit", "cost_currency"))
    if run.budget_limit is not None and run.actual_cost_total > run.budget_limit:
        raise StageBlocked(
            "actual_budget_exceeded",
            (
                f"实际累计费用 {run.actual_cost_total} {run.cost_currency} "
                f"超过预算 {run.budget_limit}；已保留真实产物，发布前必须提高预算。"
            ),
            awaiting_approval=True,
        )
    attempts = list(stage.generation_attempts.select_related("generation_task").order_by(
        "unit_key", "-attempt_number",
    ))
    latest: dict[str, GenerationAttempt] = {}
    for attempt in attempts:
        latest.setdefault(attempt.unit_key, attempt)
    if not latest:
        attempts = _create_media_tasks(run, stage)
        latest = {item.unit_key: item for item in attempts}
    active = False
    failures = []
    for attempt in latest.values():
        if attempt.operation in ASYNC_MEDIA_OPERATIONS:
            if attempt.generation_task_id is not None:
                failures.append((attempt, "async_attempt_has_generation_task"))
                continue
            if attempt.status in {
                GenerationAttempt.Status.QUEUED,
                GenerationAttempt.Status.SUBMITTING,
                GenerationAttempt.Status.SUBMITTED,
                GenerationAttempt.Status.CANCEL_REQUESTED,
            }:
                active = True
                continue
            if attempt.status == GenerationAttempt.Status.SUCCEEDED:
                if not attempt.artifacts.filter(
                    production_run=run,
                    status__in=(
                        GeneratedArtifact.Status.AVAILABLE,
                        GeneratedArtifact.Status.SELECTED,
                    ),
                ).exists():
                    failures.append((attempt, "async_artifact_archive_missing"))
                continue
            failures.append((attempt, attempt.error_code or attempt.status))
            continue
        task = attempt.generation_task
        if task is None:
            failures.append((attempt, "generation_task_missing"))
            continue
        if task.status in {
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        }:
            active = True
            attempt.status = (
                GenerationAttempt.Status.RUNNING
                if task.status != GenerationTask.Status.QUEUED
                else GenerationAttempt.Status.QUEUED
            )
            attempt.provider_request_id = task.comfy_prompt_id or task.submission_token
            attempt.save(update_fields=("status", "provider_request_id", "updated_at"))
        elif task.status == GenerationTask.Status.SUCCEEDED:
            if not task.generated_artifacts.filter(production_run=run).exists():
                record_generation_task_artifacts(
                    task,
                    production_run=run,
                    stage_run=stage,
                    generation_attempt=attempt,
                )
            _record_actual_cost(attempt, attempt.committed_cost, attempt.cost_currency)
        else:
            attempt.status = (
                GenerationAttempt.Status.CANCELLED
                if task.status == GenerationTask.Status.CANCELLED
                else GenerationAttempt.Status.FAILED
            )
            attempt.error_code = "generation_task_terminal_failure"
            attempt.error_message = str(task.error_message or task.remote_error or task.status)[:2000]
            attempt.finished_at = task.finished_at or timezone.now()
            attempt.save(update_fields=(
                "status", "error_code", "error_message", "finished_at", "updated_at",
            ))
            failures.append((attempt, attempt.error_message))
    if failures:
        if _queue_ordered_media_fallback(run, stage, failures):
            return None
        succeeded = sum(
            1 for item in latest.values()
            if (
                item.operation in ASYNC_MEDIA_OPERATIONS
                and item.status == GenerationAttempt.Status.SUCCEEDED
                and item.artifacts.filter(
                    production_run=run,
                    status__in=(
                        GeneratedArtifact.Status.AVAILABLE,
                        GeneratedArtifact.Status.SELECTED,
                    ),
                ).exists()
            ) or (
                item.generation_task is not None
                and item.generation_task.status == GenerationTask.Status.SUCCEEDED
            )
        )
        raise StageBlocked(
            "generation_partial_failure" if succeeded else "generation_tasks_failed",
            (
                f"{succeeded} 个生成单元成功、{len(failures)} 个失败；"
                "已保留成功产物，修复原因后调用 retry。"
            ),
        )
    if active:
        return None
    return (
        "generated-artifacts:"
        f"{GeneratedArtifact.objects.filter(production_run=run, stage_run=stage, kind__in=('image', 'video')).count()}"
    )


def _selected_assembly_sources(run: ProductionRun) -> dict[str, dict[str, str]]:
    """Return the selected, run-scoped media source for each formal Shot."""

    selected_sources: dict[str, dict[str, str]] = {}
    for artifact in GeneratedArtifact.objects.filter(
        production_run=run,
        stage_run__stage_key="media_generation",
        kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        status=GeneratedArtifact.Status.SELECTED,
    ).select_related(
        "generation_task__generated_workflow__prompt_artifact",
        "generation_attempt",
    ):
        task = artifact.generation_task
        prompt = getattr(getattr(task, "generated_workflow", None), "prompt_artifact", None)
        if prompt is None and artifact.generation_attempt_id:
            snapshot = artifact.generation_attempt.request_snapshot
            prompt_id = snapshot.get("prompt_artifact_id") if isinstance(snapshot, dict) else None
            prompt = PromptArtifact.objects.filter(pk=prompt_id).first()
        if prompt is not None and prompt.shot_id:
            selected_sources[str(prompt.shot_id)] = (
                {"generation_task_id": str(task.id)}
                if task is not None
                else {"generated_artifact_id": str(artifact.id)}
            )
    return selected_sources


def _execute_assembly(run: ProductionRun, stage: StageRun) -> str | None:
    from studio.models import AssemblyJob

    episodes = _selected_episodes(run)
    if not episodes:
        raise StageBlocked("assembly_episodes_missing", "逐集组装前没有正式 Episode")
    selected_sources = _selected_assembly_sources(run)
    for episode in episodes:
        if not episode.shots.exists():
            raise StageBlocked(
                "episode_master_source_missing",
                f"Episode {episode.number} 没有 Shot，不能创建母版。",
            )
        link = ProductionAssemblyLink.objects.filter(
            production_run=run,
            episode=episode,
        ).select_related("assembly_job").first()
        # A terminal worker failure is a durable operator-visible fact. Never
        # manufacture a new FFmpeg job while merely refreshing orchestration;
        # the explicit ProductionRun retry action performs that new enqueue.
        if link is not None:
            continue
        try:
            job = create_assembly(
                owner=run.project.owner,
                project=run.project,
                episode=episode,
                dry_run=False,
                source_refs_by_shot=selected_sources,
            )
        except AssemblyError as exc:
            raise StageBlocked("assembly_enqueue_failed", str(exc)) from exc
        ProductionAssemblyLink.objects.create(
            production_run=run,
            stage_run=stage,
            episode=episode,
            assembly_job=job,
        )
    links = list(ProductionAssemblyLink.objects.filter(
        production_run=run,
        stage_run=stage,
    ).select_related("episode", "assembly_job").order_by("episode__number"))
    if len(links) != len(episodes):
        raise StageBlocked("assembly_links_incomplete", "逐集 AssemblyJob 绑定不完整")
    active = [
        link for link in links
        if link.assembly_job.status in {
            AssemblyJob.Status.PLANNED,
            AssemblyJob.Status.QUEUED,
            AssemblyJob.Status.RUNNING,
        }
    ]
    failures = [link for link in links if link.assembly_job.status == AssemblyJob.Status.FAILED]
    succeeded = [link for link in links if link.assembly_job.status == AssemblyJob.Status.SUCCEEDED]
    for link in succeeded:
        record_assembly_artifact(
            link.assembly_job,
            production_run=run,
            stage_run=stage,
        )
    if failures:
        raise StageBlocked(
            "assembly_partial_failure" if succeeded else "assembly_failed",
            f"逐集组装 {len(succeeded)} 集成功、{len(failures)} 集失败；成功母版已归档。",
        )
    if active:
        stage.output_reference = f"episode-assembly-jobs:{len(links)}"
        stage.save(update_fields=("output_reference", "updated_at"))
        return None
    masters = GeneratedArtifact.objects.filter(
        production_run=run,
        kind=GeneratedArtifact.Kind.ASSEMBLY,
        role="episode_master",
        status=GeneratedArtifact.Status.SELECTED,
    )
    if masters.count() != len(episodes):
        raise StageBlocked("episode_master_manifest_incomplete", "逐集母版 artifact 清单不完整")
    return f"episode-masters:{masters.count()}"


def _execute_delivery(run: ProductionRun, stage: StageRun) -> str:
    links = list(ProductionAssemblyLink.objects.filter(
        production_run=run,
    ).select_related("episode", "assembly_job").order_by("episode__number"))
    episodes = _selected_episodes(run)
    if len(links) != len(episodes) or any(
        link.assembly_job.status != "succeeded" for link in links
    ):
        raise StageBlocked("episode_master_incomplete", "逐集母版任务未全部成功，不能交付")
    bundle = _seal_run_release_bundle(run, stage, links)
    return f"release-bundle:{bundle.id}"


def _seal_run_release_bundle(
    run: ProductionRun,
    stage: StageRun,
    links: list[ProductionAssemblyLink],
) -> ReleaseBundle:
    run.refresh_from_db(fields=("actual_cost_total", "budget_limit", "cost_currency"))
    if run.budget_limit is not None and run.actual_cost_total > run.budget_limit:
        raise StageBlocked(
            "actual_budget_exceeded",
            (
                f"实际累计费用 {run.actual_cost_total} {run.cost_currency} "
                f"超过预算 {run.budget_limit}；禁止封存发布包。"
            ),
            awaiting_approval=True,
        )
    existing = ReleaseBundle.objects.filter(production_run=run).first()
    if existing is not None and existing.status == ReleaseBundle.Status.SEALED:
        return existing
    selected_ids = [item.id for item in _selected_episodes(run)]
    shots = list(run.project.episodes.filter(pk__in=selected_ids).order_by("number").prefetch_related("shots"))
    ordered_shots = [shot for episode in shots for shot in episode.shots.all()]
    if not ordered_shots:
        raise StageBlocked("delivery_shots_missing", "交付清单没有正式 Shot")
    selected = list(GeneratedArtifact.objects.filter(
        production_run=run,
        stage_run__stage_key="media_generation",
        kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        status=GeneratedArtifact.Status.SELECTED,
    ).select_related(
        "generation_attempt", "generation_task", "production_run",
    ))
    by_shot: dict[str, GeneratedArtifact] = {}
    for shot in ordered_shots:
        matches = []
        for artifact in selected:
            if artifact.kind != shot.media_type:
                continue
            try:
                _artifact_binds_shot(artifact, shot)
                _managed_generated_artifact_facts(artifact)
            except (
                ReleaseBundleError, ProvenanceError, ValidationError,
                OSError, ValueError, TypeError, IndexError,
            ):
                continue
            matches.append(artifact)
        if len(matches) == 1:
            by_shot[str(shot.id)] = matches[0]
        elif len(matches) > 1:
            raise StageBlocked(
                "delivery_artifact_selection_ambiguous",
                f"Shot {shot.id} 存在多个 selected 正式产物，不能隐式选择。",
            )
    missing = [str(shot.id) for shot in ordered_shots if str(shot.id) not in by_shot]
    if missing:
        raise StageBlocked(
            "delivery_artifact_selection_incomplete",
            f"以下 Shot 缺少 selected 正式产物: {', '.join(missing[:20])}",
        )
    shot_selection = []
    for shot in ordered_shots:
        artifact = by_shot[str(shot.id)]
        if artifact.generation_task_id:
            try:
                _, output_index = _artifact_binds_shot(artifact, shot)
                prepare_task_distribution(artifact.generation_task, output_index)
            except (DistributionError, ReleaseBundleError, OSError, ValidationError) as exc:
                raise StageBlocked(
                    "distribution_archive_failed",
                    f"Shot {shot.id} 的公开发行副本归档失败: {exc}",
                ) from exc
        shot_selection.append({
            "shot_id": str(shot.id),
            "artifact_id": str(artifact.id),
        })
    episode_selection = []
    for link in links:
        master = GeneratedArtifact.objects.filter(
            production_run=run,
            manifest_key=f"assembly:{link.assembly_job_id}",
            kind=GeneratedArtifact.Kind.ASSEMBLY,
            status=GeneratedArtifact.Status.SELECTED,
        ).first()
        if master is None:
            raise StageBlocked(
                "delivery_episode_master_missing",
                f"Episode {link.episode_id} 缺少已归档的 selected 母版产物。",
            )
        episode_selection.append({
            "episode_id": str(link.episode_id),
            "artifact_id": str(master.id),
        })
    bundle = existing or create_release_draft(
        project=run.project,
        owner=run.project.owner,
        created_by=run.owner,
        title=f"{run.project.title} · ProductionRun 逐集交付",
        notes=f"由 ProductionRun {run.id} 生成；不包含默认整季长视频。",
    )
    if bundle.status != ReleaseBundle.Status.DRAFT:
        raise StageBlocked("release_bundle_state_invalid", "ProductionRun ReleaseBundle 状态无效")
    bundle.production_run = run
    bundle.selection = {
        "shots": shot_selection,
        "episodes": episode_selection,
        "series_artifact_id": None,
    }
    bundle.save(update_fields=("production_run", "selection", "updated_at"))
    try:
        return seal_release_bundle(bundle)
    except ReleaseBundleError as exc:
        raise StageBlocked("release_bundle_seal_failed", str(exc)) from exc


def _series_policy(deployment: ModelDeployment) -> dict[str, Any]:
    try:
        policy = validate_script_generation_model_profile(deployment.profile_version)
    except ValidationError as exc:
        raise StageBlocked("script_model_policy_invalid", str(exc)) from exc
    if policy is None:
        raise StageBlocked(
            "script_model_policy_missing",
            "模型版本未精确声明 script.generate 能力。",
        )
    return policy


def _prompt_normalization_source_rows(run: ProductionRun) -> list[dict[str, Any]]:
    """Collect the authoritative prompt-impacting fields in stable order."""

    rows: list[dict[str, Any]] = []

    def add(scope_type: str, scope_id, field_key: str, label: str) -> None:
        source = resolve_current_source(
            project=run.project,
            scope_type=scope_type,
            scope_id=scope_id,
            field_key=field_key,
        )
        if source.strip():
            rows.append({
                "scope_type": scope_type,
                "scope_id": str(scope_id),
                "field_key": field_key,
                "label": label,
                "source_text": source,
            })

    for field_key in sorted(ALLOWED_FIELD_KEYS[PromptFieldTranslation.ScopeType.PROJECT]):
        add(
            PromptFieldTranslation.ScopeType.PROJECT,
            run.project_id,
            field_key,
            f"project:{field_key}",
        )
    bible_specs = (
        (PromptFieldTranslation.ScopeType.CHARACTER, CharacterBible, "characters"),
        (PromptFieldTranslation.ScopeType.SCENE, SceneBible, "scenes"),
        (PromptFieldTranslation.ScopeType.PROP, PropBible, "props"),
    )
    for scope_type, model, label_prefix in bible_specs:
        for entity in model.objects.filter(project=run.project).order_by("slug", "id"):
            for field_key in sorted(ALLOWED_FIELD_KEYS[scope_type]):
                add(scope_type, entity.id, field_key, f"{label_prefix}:{entity.slug}:{field_key}")
    for episode in _selected_episodes(run):
        for shot in Shot.objects.filter(episode=episode).order_by("number", "id"):
            for field_key in sorted(ALLOWED_FIELD_KEYS[PromptFieldTranslation.ScopeType.SHOT]):
                add(
                    PromptFieldTranslation.ScopeType.SHOT,
                    shot.id,
                    field_key,
                    f"E{episode.number}S{shot.number}:{field_key}",
                )
            dialogue = shot.dialogue if isinstance(shot.dialogue, list) else []
            for index, line in enumerate(dialogue):
                if isinstance(line, dict) and str(line.get("delivery") or "").strip():
                    field_key = f"shot.dialogue.{index}.delivery"
                    add(
                        PromptFieldTranslation.ScopeType.SHOT,
                        shot.id,
                        field_key,
                        f"E{episode.number}S{shot.number}:{field_key}",
                    )
    return rows


def _prompt_normalization_generation_context(
    run: ProductionRun,
    *,
    shots: list[Shot],
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Freeze first-layer facts without encoding a model-specific prompt shape."""

    style = run.project.style
    def character_fact(item) -> dict[str, Any]:
        return {
            "entity_type": "character",
            "entity_id": str(item.id),
            "name": str(item.name or ""),
            "description": str(item.description or ""),
            "visual_identity": str(item.visual_identity or ""),
            "negative_constraints": str(item.negative_constraints or ""),
            "stable_speaker_id": str(item.stable_speaker_id or ""),
            "role": str(item.role or ""),
            "age_appearance": str(item.age_appearance or ""),
            "face_identity": str(item.face_identity or ""),
            "wardrobe": str(item.wardrobe or ""),
            "voice_profile": str(item.voice_profile or ""),
            "personality": str(item.personality or ""),
        }

    def scene_fact(item) -> dict[str, Any]:
        return {
            "entity_type": "scene",
            "entity_id": str(item.id),
            "name": str(item.name or ""),
            "description": str(item.description or ""),
            "visual_identity": str(item.visual_identity or ""),
            "negative_constraints": str(item.negative_constraints or ""),
            "location": str(item.location or ""),
            "time_of_day": str(item.time_of_day or ""),
            "lighting": str(item.lighting or ""),
            "color_palette": str(item.color_palette or ""),
            "ambient_sound": str(item.ambient_sound or ""),
        }

    def prop_fact(item) -> dict[str, Any]:
        return {
            "entity_type": "prop",
            "entity_id": str(item.id),
            "name": str(item.name or ""),
            "description": str(item.description or ""),
            "visual_identity": str(item.visual_identity or ""),
            "negative_constraints": str(item.negative_constraints or ""),
            "material": str(item.material or ""),
            "distinctive_marks": str(item.distinctive_marks or ""),
            "continuity_state": str(item.continuity_state or ""),
        }

    return {
        "schema_version": 2,
        "project": {
            "project_id": str(run.project_id),
            "title": str(run.project.title or ""),
            "logline": str(run.project.logline or ""),
            "language": str(run.project.language or ""),
            "aspect_ratio": str(run.project.aspect_ratio or ""),
            "continuity_summary": str(run.project.continuity_summary or ""),
            "style": ({
                "name": str(style.name or ""),
                "description": str(style.description or ""),
                "visual_prompt": str(style.visual_prompt or ""),
                "negative_prompt": str(style.negative_prompt or ""),
            } if style is not None else None),
        },
        "fields": [{
            "scope_type": str(target.get("scope_type") or ""),
            "scope_id": str(target.get("scope_id") or ""),
            "field_key": str(target.get("field_key") or ""),
            "value": str(target.get("source_text") or ""),
            "source_sha256": str(target.get("source_hash") or ""),
            "target_kind": str(target.get("target_kind") or "source_field"),
        } for target in targets
            if str(target.get("target_kind") or "") != "prompt_profile_field"
        ],
        "shots": [{
            "shot_id": str(shot.id),
            "episode_id": str(shot.episode_id),
            "episode_number": int(shot.episode.number),
            "shot_number": int(shot.number),
            "title": str(shot.title or ""),
            "narrative": str(shot.narrative or ""),
            "detailed_description": str(shot.detailed_description or ""),
            "camera": str(shot.camera or ""),
            "action": str(shot.action or ""),
            "sound_effects": str(shot.sound_effects or ""),
            "music": str(shot.music or ""),
            "duration_ms": int(shot.duration_ms),
            "media_type": str(shot.media_type or ""),
            "mode": str(shot.conditioning_mode or ""),
            "scene_id": str(shot.scene_id) if shot.scene_id else None,
            "character_ids": [
                str(item.id) for item in shot.characters.all().order_by("id")
            ],
            "prop_ids": [str(item.id) for item in shot.props.all().order_by("id")],
            "dialogue": deepcopy(shot.dialogue) if isinstance(shot.dialogue, list) else [],
            "linked_bible": [
                *[
                    character_fact(item)
                    for item in shot.characters.all().order_by("id")
                ],
                *([scene_fact(shot.scene)] if shot.scene is not None else []),
                *[prop_fact(item) for item in shot.props.all().order_by("id")],
            ],
        } for shot in shots],
    }


def _prompt_normalization_target_contract(
    run: ProductionRun,
    *,
    items: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Freeze model-neutral Layer-2 rules from the selected current PromptProfile contract."""

    selected_episode_ids = [item.id for item in _selected_episodes(run)]
    shots = list(
        Shot.objects.select_related("episode", "scene")
        .filter(episode_id__in=selected_episode_ids)
        .prefetch_related("characters", "props")
        .order_by("episode__number", "number", "id")
    )
    if not shots:
        return None

    media_stage = run.stages.filter(stage_key="media_generation").first()
    if media_stage is None:
        # A standalone prompt.normalize recipe has no downstream media
        # PromptProfile to govern. Its items keep the ordinary translation-only
        # contract rather than inventing an implicit media policy.
        return None
    selection = _frozen_workflow_selection(media_stage)
    workflow_selection_sha256 = str(
        (run.input_snapshot or {}).get("workflow_selection_sha256") or ""
    )
    if selection is None or not workflow_selection_sha256:
        raise StageBlocked(
            "prompt_field_generation_contract_missing",
            "字段生成缺少冻结工作流与 PromptProfile 选择。",
        )

    targets_by_field: dict[tuple[str, str, str], dict[str, Any]] = {}
    items_by_request_id = {
        str(item.get("request_id") or ""): item
        for item in items
        if isinstance(item, dict) and item.get("request_id")
    }
    for target in targets:
        if not isinstance(target, dict):
            raise StageBlocked(
                "prompt_normalize_input_invalid",
                "prompt.normalize 字段来源快照无效。",
            )
        key = (
            str(target.get("scope_type") or ""),
            str(target.get("scope_id") or ""),
            str(target.get("field_key") or ""),
        )
        if key in targets_by_field:
            raise StageBlocked(
                "prompt_normalize_input_invalid",
                "prompt.normalize 字段来源快照包含重复目标。",
            )
        targets_by_field[key] = target

    profiles_by_selection: dict[str, dict[str, Any]] = {}
    item_generation_rules: list[dict[str, Any]] = []
    destination_rules: dict[tuple[str, str, str], dict[str, Any]] = {}

    def reference_for_shot(shot: Shot) -> dict[str, Any]:
        operation = (
            "image.generate"
            if shot.media_type == Shot.MediaType.IMAGE
            else "video.generate"
        )
        mode = shot.conditioning_mode if shot.media_type == Shot.MediaType.VIDEO else ""
        if selection.get("mode") == "uniform":
            matches = [
                row for row in (selection.get("prompt_profile_selections") or [])
                if isinstance(row, dict)
                and row.get("operation") == operation
                and row.get("mode") == mode
            ]
            reference = matches[0].get("prompt_profile") if len(matches) == 1 else None
        elif selection.get("mode") == "per_shot":
            matches = [
                row for row in (selection.get("shot_routes") or [])
                if isinstance(row, dict) and str(row.get("shot_id") or "") == str(shot.id)
            ]
            reference = matches[0].get("prompt_profile") if len(matches) == 1 else None
        else:
            reference = None
        if not isinstance(reference, dict) or reference.get("schema_version") != 2:
            raise StageBlocked(
                "prompt_field_generation_contract_missing",
                f"镜头 {shot.id} 缺少唯一冻结 PromptProfile 选择。",
            )
        return deepcopy(reference)

    for shot in shots:
        model_profile, _route_index, selected_sha256 = _selected_media_model_profile(
            run,
            shot=shot,
        )
        if model_profile is None or selected_sha256 != workflow_selection_sha256:
            raise StageBlocked(
                "prompt_field_generation_contract_snapshot_drift",
                "字段生成的媒体模型选择快照已漂移。",
            )
        frozen_reference = reference_for_shot(shot)
        operation = (
            "image.generate"
            if shot.media_type == Shot.MediaType.IMAGE
            else "video.generate"
        )
        mode = shot.conditioning_mode if shot.media_type == Shot.MediaType.VIDEO else ""
        try:
            profile = load_frozen_prompt_profile_selection(
                frozen_reference,
                model_profile_version=model_profile,
                operation=operation,
                mode=mode,
            )
        except (PromptProfileResolutionError, ValidationError) as exc:
            raise StageBlocked(
                "prompt_field_generation_contract_invalid",
                "冻结 PromptProfile 身份、范围或合同校验无效。",
            ) from exc
        if profile.contract_schema_version != PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION:
            raise StageBlocked(
                "prompt_field_generation_contract_legacy",
                (
                    f"镜头 {shot.id} 仍选择历史 PromptProfile schema；"
                    "旧合同只可审计，新运行必须在系统设置选择当前已发布合同。"
                ),
            )

        frozen_selection_sha256 = _sha256(frozen_reference)
        if frozen_selection_sha256 not in profiles_by_selection:
            profile_contract = prompt_profile_contract_payload(profile)
            profiles_by_selection[frozen_selection_sha256] = {
                "prompt_profile_version_id": str(profile.id),
                "binding_key": str(
                    frozen_reference.get("prompt_profile_binding_key") or ""
                ),
                "frozen_selection_sha256": frozen_selection_sha256,
                "contract_schema_version": int(profile.contract_schema_version),
                "contract_size_bytes": int(profile.contract_size_bytes),
                "contract_sha256": profile.contract_sha256,
                "contract": profile_contract,
            }
        scope_ids = {
            PromptFieldTranslation.ScopeType.PROJECT: {str(run.project_id)},
            PromptFieldTranslation.ScopeType.SHOT: {str(shot.id)},
            PromptFieldTranslation.ScopeType.SCENE: (
                {str(shot.scene_id)} if shot.scene_id else set()
            ),
            PromptFieldTranslation.ScopeType.CHARACTER: {
                str(item.id) for item in shot.characters.all()
            },
            PromptFieldTranslation.ScopeType.PROP: {
                str(item.id) for item in shot.props.all()
            },
        }
        contract_rules = profiles_by_selection[
            frozen_selection_sha256
        ]["contract"]["rules"]
        for contract_rule in contract_rules:
            if contract_rule["generation_mode"] not in MODEL_PROCESSING_MODES:
                continue
            generation_source = str(contract_rule["generation_source_selector"])
            root = generation_source.split(".", 1)[0]
            scope_type = (
                PromptFieldTranslation.ScopeType.PROJECT
                if root == "style"
                else root
            )
            candidate_ids = scope_ids.get(scope_type, set())
            matched_targets = [
                targets_by_field[(scope_type, scope_id, generation_source)]
                for scope_id in sorted(candidate_ids)
                if (scope_type, scope_id, generation_source) in targets_by_field
            ]
            if not matched_targets:
                if contract_rule["generation_required"]:
                    raise StageBlocked(
                        "prompt_field_generation_source_missing",
                        (
                            f"PromptProfile 字段 {contract_rule['field_key']} 的冻结来源 "
                            f"{generation_source} 不存在或不属于镜头 {shot.id}。"
                        ),
                    )
                continue
            for target in matched_targets:
                source_request_id = str(target.get("request_id") or "")
                source_item = items_by_request_id.get(source_request_id)
                if source_item is None:
                    raise StageBlocked(
                        "prompt_normalize_input_invalid",
                        "字段生成来源缺少对应的 provider item。",
                    )
                destination_identity = (
                    str(shot.id),
                    frozen_selection_sha256,
                    str(contract_rule["field_key"]),
                )
                request_id = "profile-" + _sha256({
                    "schema_version": 1,
                    "shot_id": str(shot.id),
                    "profile_selection_sha256": frozen_selection_sha256,
                    "profile_field_key": str(contract_rule["field_key"]),
                    "generation_source_selector": generation_source,
                    "source_request_id": source_request_id,
                })[:48]
                candidate_rule = {
                    "request_id": request_id,
                    "shot_id": str(shot.id),
                    "scope_type": str(target.get("scope_type") or ""),
                    "scope_id": str(target.get("scope_id") or ""),
                    "profile_selection_sha256": frozen_selection_sha256,
                    "profile_field_key": str(contract_rule["field_key"]),
                    "profile_source_selector": str(contract_rule["source_selector"]),
                    "profile_rule_position": int(contract_rule["position"]),
                    "generation_mode": str(contract_rule["generation_mode"]),
                    "generation_source_selector": generation_source,
                    "generation_instruction": str(
                        contract_rule["generation_instruction"]
                    ),
                    "generation_output_semantics": str(
                        contract_rule["generation_output_semantics"]
                    ),
                    "generation_required": bool(contract_rule["generation_required"]),
                    "language_policy": str(contract_rule["language_policy"]),
                    "length_unit": str(contract_rule["length_unit"]),
                    "generation_min": contract_rule["generation_min"],
                    "generation_max": contract_rule["generation_max"],
                    "generation_validator_keys": list(
                        contract_rule["generation_validator_keys"],
                    ),
                    "generation_context_selectors": list(
                        contract_rule["generation_context_selectors"],
                    ),
                    "source_sha256": hashlib.sha256(
                        str(source_item.get("text") or "").encode("utf-8"),
                    ).hexdigest(),
                }
                existing_rule = destination_rules.get(destination_identity)
                if existing_rule == candidate_rule:
                    continue
                if existing_rule is not None:
                    raise StageBlocked(
                        "prompt_field_generation_rule_ambiguous",
                        (
                            f"镜头 {shot.id} 的 Profile 字段 "
                            f"{contract_rule['field_key']} 被多个生成规则同时声明。"
                        ),
                    )
                if request_id in items_by_request_id:
                    raise StageBlocked(
                        "prompt_field_generation_rule_ambiguous",
                        "字段生成 request_id 发生不可接受的摘要冲突。",
                    )
                generation_item = {
                    **source_item,
                    "request_id": request_id,
                    "source_lang": str(run.project.language or "")[:40] or "Chinese",
                    "field_key": str(contract_rule["field_key"]),
                    "label": (
                        f"{target.get('label') or generation_source}"
                        f" -> {contract_rule['field_key']}"
                    )[:240],
                }
                generation_target = {
                    **target,
                    "request_id": request_id,
                    "target_kind": "prompt_profile_field",
                    "scope_type": PromptFieldTranslation.ScopeType.SHOT,
                    "scope_id": str(shot.id),
                    "field_key": str(contract_rule["field_key"]),
                    "shot_id": str(shot.id),
                    "profile_selection_sha256": frozen_selection_sha256,
                    "prompt_profile_version_id": str(profile.id),
                    "profile_field_key": str(contract_rule["field_key"]),
                    "profile_source_selector": str(contract_rule["source_selector"]),
                    "generation_mode": str(contract_rule["generation_mode"]),
                    "generation_output_semantics": str(
                        contract_rule["generation_output_semantics"]
                    ),
                    "generation_source_selector": generation_source,
                    "generation_source_scope_type": str(
                        target.get("scope_type") or ""
                    ),
                    "generation_source_scope_id": str(target.get("scope_id") or ""),
                    "generation_source_field_key": str(target.get("field_key") or ""),
                }
                items.append(generation_item)
                targets.append(generation_target)
                items_by_request_id[request_id] = generation_item
                destination_rules[destination_identity] = candidate_rule
                item_generation_rules.append(candidate_rule)

    if not item_generation_rules:
        return None
    frozen_context = _prompt_normalization_generation_context(
        run,
        shots=shots,
        targets=targets,
    )
    return {
        "schema_version": FIELD_GENERATION_TARGET_CONTRACT_SCHEMA_VERSION,
        "kind": FIELD_GENERATION_TARGET_CONTRACT_KIND,
        "workflow_selection_sha256": workflow_selection_sha256,
        "profiles": list(profiles_by_selection.values()),
        "frozen_context": frozen_context,
        "frozen_context_sha256": _sha256(frozen_context),
        "item_generation_rules": item_generation_rules,
    }


def _build_prompt_normalization_input(run: ProductionRun) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    for index, row in enumerate(_prompt_normalization_source_rows(run), start=1):
        request_id = f"{index:03d}-{source_text_hash(row['source_text'])[:20]}"
        item, target = prepare_prompt_normalization_item(
            project=run.project,
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            field_key=row["field_key"],
            source_text=row["source_text"],
            label=row["label"],
            request_id=request_id,
        )
        items.append(item)
        targets.append(target)
    payload = {"schema_version": 1, "items": items}
    target_prompt_contract = _prompt_normalization_target_contract(
        run,
        items=items,
        targets=targets,
    )
    if target_prompt_contract is not None:
        payload["target_prompt_contract"] = target_prompt_contract
    return {
        "payload": payload,
        "targets": targets,
    }


def _prompt_normalization_batch_unit_key(
    batch_index: int,
    generation_mode: str,
) -> str:
    if type(batch_index) is not int or batch_index < 0:
        raise PromptNormalizationContractError(
            "prompt.normalize 批次序号无效",
            code="prompt_normalization_batch_contract_invalid",
        )
    if generation_mode not in MODEL_PROCESSING_MODES:
        raise PromptNormalizationContractError(
            "prompt.normalize 批次生成模式无效",
            code="prompt_normalization_batch_contract_invalid",
        )
    return f"{PROMPT_NORMALIZE_UNIT_KEY}:{batch_index + 1:03d}:{generation_mode}"


def _prompt_normalization_item_mode(
    payload: dict[str, Any],
    *,
    request_id: str,
) -> str:
    rule = target_generation_rule_for_request(payload, request_id=request_id)
    mode = str((rule or {}).get("generation_mode") or "translate_only")
    if mode not in MODEL_PROCESSING_MODES:
        raise PromptNormalizationContractError(
            f"{request_id} 的模型处理模式无效",
            code="prompt_normalization_batch_contract_invalid",
        )
    return mode


def _prompt_normalization_item_execution(
    payload: dict[str, Any],
    *,
    item: dict[str, Any],
    target: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Derive execution from frozen content, never from a caller flag."""

    try:
        materialization = deterministic_prompt_normalization_materialization(
            item=item,
            target=target,
            request_payload=payload,
        )
    except TranslationValidationError as exc:
        raise PromptNormalizationContractError(
            str(exc),
            code="prompt_normalization_batch_contract_invalid",
        ) from exc
    if materialization is not None:
        return PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC, materialization
    return PROMPT_NORMALIZATION_EXECUTION_PROVIDER, None


def _assemble_prompt_normalization_batch_payload(
    run: ProductionRun,
    *,
    base_payload: dict[str, Any],
    prompt_profile_version,
) -> dict[str, Any]:
    payload = deepcopy(base_payload)
    payload["prompt_profile_assembly"] = assemble_prompt_profile(
        prompt_profile_version,
        sources=text_profile_sources(
            project=run.project,
            inputs={
                "schema_version": 1,
                "items": deepcopy(payload.get("items") or []),
            },
            operation=PROMPT_NORMALIZE_OPERATION,
        ),
    )
    validate_prompt_normalization_request_contract(payload)
    return payload


def _prompt_normalization_chat_input_bytes(
    payload: dict[str, Any],
    *,
    policy: dict[str, Any],
) -> int:
    provider_payload = prompt_normalization_chat_payload(payload, policy=policy)
    return prompt_normalization_chat_messages_input_bytes(
        provider_payload["messages"],
    )


@security_boundary_resolution_cache()
def _build_prompt_normalization_batch_plan(
    run: ProductionRun,
    *,
    frozen_input: dict[str, Any],
    policy: dict[str, Any],
    prompt_profile_version,
) -> dict[str, Any]:
    """Freeze deterministic mode/capacity batches before the first submit."""

    full_payload = deepcopy(frozen_input.get("payload"))
    targets = deepcopy(frozen_input.get("targets"))
    if not isinstance(full_payload, dict) or not isinstance(targets, list):
        raise PromptNormalizationContractError(
            "prompt.normalize 完整冻结输入无效",
            code="prompt_normalization_batch_contract_invalid",
        )
    full_contract = json_contract(
        {"payload": full_payload, "targets": targets},
        max_bytes=_text_snapshot_max_bytes(),
    )
    validate_prompt_normalization_run_contract(full_payload)
    items = full_payload.get("items")
    if not isinstance(items, list) or len(items) != len(targets) or not items:
        raise PromptNormalizationContractError(
            "prompt.normalize 完整冻结 items/targets 无效",
            code="prompt_normalization_batch_contract_invalid",
        )
    targets_by_request_id = {
        str(target.get("request_id") or ""): target
        for target in targets
        if isinstance(target, dict) and target.get("request_id")
    }
    if len(targets_by_request_id) != len(targets):
        raise PromptNormalizationContractError(
            "prompt.normalize 完整冻结 targets request_id 无效",
            code="prompt_normalization_batch_contract_invalid",
        )
    mode_buckets: dict[tuple[str, str], list[str]] = {}
    original_request_ids: list[str] = []
    for item in items:
        request_id = str(item.get("request_id") or "") if isinstance(item, dict) else ""
        if not request_id or request_id not in targets_by_request_id:
            raise PromptNormalizationContractError(
                "prompt.normalize 完整冻结 request_id 不一致",
                code="prompt_normalization_batch_contract_invalid",
            )
        original_request_ids.append(request_id)
        mode = _prompt_normalization_item_mode(full_payload, request_id=request_id)
        execution_kind, _materialization = _prompt_normalization_item_execution(
            full_payload,
            item=item,
            target=targets_by_request_id[request_id],
        )
        mode_buckets.setdefault((mode, execution_kind), []).append(request_id)

    max_items = int(policy["max_items_per_batch"])
    batches: list[dict[str, Any]] = []

    items_by_request_id = {
        str(item.get("request_id") or ""): item
        for item in items
        if isinstance(item, dict) and item.get("request_id")
    }

    def freeze_batch(
        mode: str,
        execution_kind: str,
        request_ids: list[str],
    ) -> dict[str, Any]:
        base_payload = project_prompt_normalization_request_contract(
            full_payload,
            request_ids=request_ids,
        )
        assembled_payload = _assemble_prompt_normalization_batch_payload(
            run,
            base_payload=base_payload,
            prompt_profile_version=prompt_profile_version,
        )
        input_bytes = _prompt_normalization_chat_input_bytes(
            assembled_payload,
            policy=policy,
        )
        batch_index = len(batches)
        batch_targets = [deepcopy(targets_by_request_id[item]) for item in request_ids]
        materialization_proofs: list[dict[str, Any]] = []
        for request_id in request_ids:
            derived_kind, materialization = _prompt_normalization_item_execution(
                full_payload,
                item=items_by_request_id[request_id],
                target=targets_by_request_id[request_id],
            )
            if derived_kind != execution_kind:
                raise PromptNormalizationContractError(
                    "prompt.normalize 批次执行种类不一致",
                    code="prompt_normalization_batch_contract_invalid",
                )
            if materialization is not None:
                materialization_proofs.append(deepcopy(materialization["proof"]))
        return {
            "batch_index": batch_index,
            "unit_key": _prompt_normalization_batch_unit_key(batch_index, mode),
            "generation_mode": mode,
            "execution_kind": execution_kind,
            "request_ids": list(request_ids),
            "base_payload": base_payload,
            "base_payload_sha256": _sha256(base_payload),
            "targets": batch_targets,
            "targets_sha256": _sha256(batch_targets),
            "materialization_proofs": materialization_proofs,
            "materialization_proofs_sha256": _sha256(materialization_proofs),
            "initial_chat_input_bytes": input_bytes,
            "initial_assembled_payload_sha256": _sha256(assembled_payload),
            "initial_prompt_profile_version_id": str(prompt_profile_version.id),
        }

    for (mode, execution_kind), request_ids in mode_buckets.items():
        pending: list[str] = []
        for request_id in request_ids:
            candidate = [*pending, request_id]
            if len(candidate) > max_items:
                batches.append(freeze_batch(mode, execution_kind, pending))
                pending = [request_id]
                continue
            try:
                freeze_batch(mode, execution_kind, candidate)
            except PromptNormalizationContractError as exc:
                if exc.code != "prompt_normalization_batch_input_exceeded":
                    raise
                if not pending:
                    raise PromptNormalizationContractError(
                        (
                            f"{request_id} 单项装配后已超过模型 max_input_bytes；"
                            "请提高模型容量合同或缩减上游字段合同"
                        ),
                        code="prompt_normalization_item_exceeds_capacity",
                    ) from exc
                batches.append(freeze_batch(mode, execution_kind, pending))
                pending = [request_id]
                try:
                    freeze_batch(mode, execution_kind, pending)
                except PromptNormalizationContractError as single_exc:
                    if single_exc.code == "prompt_normalization_batch_input_exceeded":
                        raise PromptNormalizationContractError(
                            (
                                f"{request_id} 单项装配后已超过模型 max_input_bytes；"
                                "请提高模型容量合同或缩减上游字段合同"
                            ),
                            code="prompt_normalization_item_exceeds_capacity",
                        ) from single_exc
                    raise
            else:
                pending = candidate
        if pending:
            batches.append(freeze_batch(mode, execution_kind, pending))

    generation_task_limit = _effective_generation_task_limit(run)
    if run.generation_task_count + len(batches) > generation_task_limit:
        raise StageBlocked(
            "generation_task_budget_exhausted",
            (
                f"prompt.normalize 需要 {len(batches)} 个冻结批次，"
                f"会超过运行任务上限 {generation_task_limit}"
            ),
        )

    # Re-number after probe-only freeze_batch calls.  The helper reads the
    # current persisted list length, so every appended batch is already stable.
    plan_body = {
        "schema_version": PROMPT_NORMALIZATION_BATCH_PLAN_SCHEMA_VERSION,
        "full_frozen_input": full_contract.value,
        "full_frozen_input_schema_version": full_contract.schema_version,
        "full_frozen_input_size_bytes": full_contract.size_bytes,
        "full_frozen_input_sha256": full_contract.sha256,
        "original_request_ids": original_request_ids,
        "capacity_policy": deepcopy(policy),
        "capacity_policy_sha256": _sha256(policy),
        "batches": batches,
    }
    return {**plan_body, "batch_plan_sha256": _sha256(plan_body)}


@security_boundary_resolution_cache()
def _verify_prompt_normalization_batch_plan(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StageBlocked(
            "prompt_normalization_batch_plan_invalid",
            "prompt.normalize 缺少冻结批次计划",
        )
    body = {key: deepcopy(value) for key, value in raw.items() if key != "batch_plan_sha256"}
    if (
        body.get("schema_version") != PROMPT_NORMALIZATION_BATCH_PLAN_SCHEMA_VERSION
        or raw.get("batch_plan_sha256") != _sha256(body)
        or body.get("capacity_policy_sha256") != _sha256(body.get("capacity_policy"))
    ):
        raise StageBlocked(
            "prompt_normalization_batch_plan_invalid",
            "prompt.normalize 冻结批次计划 checksum 无效",
        )
    full_input = body.get("full_frozen_input")
    try:
        verify_json_contract(
            full_input,
            schema_version=body.get("full_frozen_input_schema_version"),
            size_bytes=body.get("full_frozen_input_size_bytes"),
            sha256=body.get("full_frozen_input_sha256"),
            max_bytes=_text_snapshot_max_bytes(),
        )
    except ValidationError as exc:
        raise StageBlocked(
            "prompt_normalization_batch_plan_invalid",
            "prompt.normalize 完整冻结输入 checksum 无效",
        ) from exc
    full_payload = full_input.get("payload") if isinstance(full_input, dict) else None
    full_targets = full_input.get("targets") if isinstance(full_input, dict) else None
    try:
        if not isinstance(full_payload, dict) or not isinstance(full_targets, list):
            raise PromptNormalizationContractError(
                "prompt.normalize 完整冻结输入结构无效",
                code="prompt_normalization_batch_contract_invalid",
            )
        validate_prompt_normalization_run_contract(full_payload)
        full_items = full_payload.get("items")
        if not isinstance(full_items, list) or len(full_items) != len(full_targets):
            raise PromptNormalizationContractError(
                "prompt.normalize 完整冻结 items/targets 无效",
                code="prompt_normalization_batch_contract_invalid",
            )
        items_by_request_id = {
            str(item.get("request_id") or ""): item
            for item in full_items
            if isinstance(item, dict) and item.get("request_id")
        }
        targets_by_request_id = {
            str(target.get("request_id") or ""): target
            for target in full_targets
            if isinstance(target, dict) and target.get("request_id")
        }
        if (
            len(items_by_request_id) != len(full_items)
            or len(targets_by_request_id) != len(full_targets)
            or set(items_by_request_id) != set(targets_by_request_id)
        ):
            raise PromptNormalizationContractError(
                "prompt.normalize 完整冻结 request_id 无效",
                code="prompt_normalization_batch_contract_invalid",
            )
        frozen_original_ids = [
            str(item.get("request_id") or "") for item in full_items
        ]
    except (PromptNormalizationContractError, ValidationError) as exc:
        raise StageBlocked(
            "prompt_normalization_batch_plan_invalid",
            "prompt.normalize 完整冻结输入结构无效",
        ) from exc
    batches = body.get("batches")
    original_ids = body.get("original_request_ids")
    if (
        not isinstance(batches, list)
        or not batches
        or not isinstance(original_ids, list)
        or original_ids != frozen_original_ids
    ):
        raise StageBlocked(
            "prompt_normalization_batch_plan_invalid",
            "prompt.normalize 冻结批次列表无效",
        )
    seen: set[str] = set()
    for index, batch in enumerate(batches):
        request_ids = batch.get("request_ids") if isinstance(batch, dict) else None
        mode = str(batch.get("generation_mode") or "") if isinstance(batch, dict) else ""
        execution_kind = str(batch.get("execution_kind") or "") if isinstance(batch, dict) else ""
        expected_targets = (
            [deepcopy(targets_by_request_id.get(item)) for item in request_ids]
            if isinstance(request_ids, list)
            else None
        )
        expected_base_payload = None
        expected_proofs: list[dict[str, Any]] | None = None
        derived_execution_kinds: set[str] = set()
        try:
            if isinstance(request_ids, list) and request_ids:
                expected_base_payload = project_prompt_normalization_request_contract(
                    full_payload,
                    request_ids=request_ids,
                )
                expected_proofs = []
                for request_id in request_ids:
                    if request_id not in items_by_request_id or request_id not in targets_by_request_id:
                        raise PromptNormalizationContractError(
                            "prompt.normalize 批次 request_id 不属于完整冻结输入",
                            code="prompt_normalization_batch_contract_invalid",
                        )
                    if _prompt_normalization_item_mode(
                        full_payload,
                        request_id=request_id,
                    ) != mode:
                        raise PromptNormalizationContractError(
                            "prompt.normalize 批次 generation_mode 已漂移",
                            code="prompt_normalization_batch_contract_invalid",
                        )
                    derived_kind, materialization = _prompt_normalization_item_execution(
                        full_payload,
                        item=items_by_request_id[request_id],
                        target=targets_by_request_id[request_id],
                    )
                    derived_execution_kinds.add(derived_kind)
                    if materialization is not None:
                        expected_proofs.append(deepcopy(materialization["proof"]))
        except (PromptNormalizationContractError, ValidationError) as exc:
            raise StageBlocked(
                "prompt_normalization_batch_plan_invalid",
                "prompt.normalize 冻结批次派生合同无效",
            ) from exc
        if (
            not isinstance(batch, dict)
            or batch.get("batch_index") != index
            or batch.get("unit_key") != _prompt_normalization_batch_unit_key(index, mode)
            or not isinstance(request_ids, list)
            or not request_ids
            or any(not isinstance(item, str) or not item or item in seen for item in request_ids)
            or execution_kind not in {
                PROMPT_NORMALIZATION_EXECUTION_PROVIDER,
                PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC,
            }
            or derived_execution_kinds != {execution_kind}
            or batch.get("base_payload") != expected_base_payload
            or batch.get("base_payload_sha256") != _sha256(batch.get("base_payload"))
            or batch.get("targets") != expected_targets
            or batch.get("targets_sha256") != _sha256(batch.get("targets"))
            or batch.get("materialization_proofs") != expected_proofs
            or batch.get("materialization_proofs_sha256")
            != _sha256(batch.get("materialization_proofs"))
        ):
            raise StageBlocked(
                "prompt_normalization_batch_plan_invalid",
                "prompt.normalize 冻结批次身份或 checksum 无效",
            )
        seen.update(request_ids)
    if set(original_ids) != seen or len(original_ids) != len(seen):
        raise StageBlocked(
            "prompt_normalization_batch_plan_invalid",
            "prompt.normalize 冻结批次未完整覆盖原 request_id",
        )
    return deepcopy(raw)


def _verified_prompt_normalization_review_batches(
    stage: StageRun,
    *,
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
    manual_validation_fallback: bool,
) -> tuple[
    GenerationAttempt,
    dict[str, GenerationAttempt],
    dict[str, Any],
]:
    raw_attempt_ids = payload.get("attempt_ids")
    if (
        not isinstance(raw_attempt_ids, list)
        or not raw_attempt_ids
        or len(raw_attempt_ids) != len(set(raw_attempt_ids))
        or any(not isinstance(value, str) or not value for value in raw_attempt_ids)
    ):
        raise ValidationError("script_normalize 批次 attempt_ids 无效")
    attempts = list(
        GenerationAttempt.objects.select_for_update().filter(
            pk__in=raw_attempt_ids,
            stage_run=stage,
            operation=PROMPT_NORMALIZE_OPERATION,
        )
    )
    attempts_by_id = {str(item.id): item for item in attempts}
    if set(attempts_by_id) != set(raw_attempt_ids):
        raise ValidationError("script_normalize 批次 attempt 缺失")
    latest = attempts_by_id.get(str(payload.get("attempt_id") or ""))
    if latest is None:
        raise ValidationError("script_normalize 最新批次 attempt 缺失")
    batch_plan = _verify_prompt_normalization_batch_plan(
        latest.request_snapshot.get("batch_plan"),
    )
    for attempt in attempts:
        verify_json_contract(
            attempt.request_snapshot,
            schema_version=attempt.request_snapshot_schema_version,
            size_bytes=attempt.request_snapshot_size_bytes,
            sha256=attempt.request_snapshot_sha256,
            max_bytes=_text_snapshot_max_bytes(),
        )
        if (
            attempt.request_snapshot.get("batch_plan", {}).get("batch_plan_sha256")
            != batch_plan["batch_plan_sha256"]
        ):
            raise ValidationError("script_normalize 批次 attempt 计划不一致")
    frozen_input = payload.get("frozen_input")
    if not isinstance(frozen_input, dict):
        raise ValidationError("script_normalize 批次审核缺少完整冻结输入")
    verify_json_contract(
        frozen_input,
        schema_version=payload.get("frozen_input_schema_version"),
        size_bytes=payload.get("frozen_input_size_bytes"),
        sha256=payload.get("frozen_input_sha256"),
        max_bytes=_text_snapshot_max_bytes(),
    )
    if (
        payload.get("batch_plan_sha256") != batch_plan["batch_plan_sha256"]
        or payload.get("frozen_input_sha256")
        != batch_plan["full_frozen_input_sha256"]
        or frozen_input != batch_plan["full_frozen_input"]
    ):
        raise ValidationError("script_normalize 批次审核与冻结计划不一致")

    batch_results = payload.get("batch_results")
    if not isinstance(batch_results, list):
        raise ValidationError("script_normalize batch_results 无效")
    candidates_by_request_id: dict[str, dict[str, Any]] = {}
    successful_attempt_ids: set[str] = set()
    batch_evidence: list[dict[str, Any]] = []
    for expected_index, completed in enumerate(batch_results):
        result = completed.get("result") if isinstance(completed, dict) else None
        attempt_id = str(completed.get("attempt_id") or "") if isinstance(completed, dict) else ""
        result_sha256 = str(completed.get("result_sha256") or "") if isinstance(completed, dict) else ""
        attempt = attempts_by_id.get(attempt_id)
        if (
            not isinstance(result, dict)
            or completed.get("batch_index") != expected_index
            or attempt is None
            or attempt.status != GenerationAttempt.Status.SUCCEEDED
            or not attempt.result_sha256
            or attempt.result_sha256 != result_sha256
            or json_contract(result, max_bytes=_text_snapshot_max_bytes()).sha256
            != result_sha256
            or result.get("attempt_id") != attempt_id
            or result.get("batch_index") != expected_index
            or result.get("batch_plan_sha256") != batch_plan["batch_plan_sha256"]
            or result.get("execution_kind")
            != batch_plan["batches"][expected_index].get("execution_kind")
        ):
            raise ValidationError("script_normalize 已完成批次结果证据无效")
        successful_attempt_ids.add(attempt_id)
        result_candidates = result.get("candidates")
        if not isinstance(result_candidates, list):
            raise ValidationError("script_normalize 已完成批次 candidates 无效")
        for candidate in result_candidates:
            request_id = str(candidate.get("request_id") or "") if isinstance(candidate, dict) else ""
            if (
                not request_id
                or request_id in candidates_by_request_id
                or candidate.get("generation_attempt_id") != attempt_id
            ):
                raise ValidationError("script_normalize 已完成批次候选 provenance 无效")
            candidates_by_request_id[request_id] = candidate
        batch_evidence.append({
            "batch_index": expected_index,
            "attempt_id": attempt_id,
            "result_sha256": result_sha256,
            "execution_kind": str(result.get("execution_kind") or ""),
        })

    original_request_ids = batch_plan["original_request_ids"]
    if manual_validation_fallback:
        if (
            latest.status != GenerationAttempt.Status.FAILED
            or latest.error_code not in PROMPT_VALIDATION_RETRYABLE_CODES
            or successful_attempt_ids | {str(latest.id)} != set(raw_attempt_ids)
        ):
            raise ValidationError("script_normalize 人工接管批次 attempt 状态无效")
    else:
        merged_candidates = [
            candidates_by_request_id.get(request_id)
            for request_id in original_request_ids
        ]
        aggregate_evidence = {
            "schema_version": 1,
            "batch_plan_sha256": batch_plan["batch_plan_sha256"],
            "batch_results": batch_evidence,
            "candidates": merged_candidates,
        }
        if (
            len(batch_results) != len(batch_plan["batches"])
            or any(item is None for item in merged_candidates)
            or merged_candidates != candidates
            or successful_attempt_ids != set(raw_attempt_ids)
            or payload.get("result_sha256") != _sha256(aggregate_evidence)
        ):
            raise ValidationError("script_normalize 批次聚合结果 checksum 无效")

    attempt_by_request_id: dict[str, GenerationAttempt] = {}
    for candidate in candidates:
        request_id = str(candidate.get("request_id") or "") if isinstance(candidate, dict) else ""
        origin_id = str(candidate.get("generation_attempt_id") or "") if isinstance(candidate, dict) else ""
        origin = attempts_by_id.get(origin_id)
        if origin is None and manual_validation_fallback:
            origin = latest
        if not request_id or origin is None:
            raise ValidationError("script_normalize 候选缺少批次 attempt provenance")
        attempt_by_request_id[request_id] = origin
    if set(attempt_by_request_id) != set(original_request_ids):
        raise ValidationError("script_normalize 批次候选未覆盖完整冻结输入")
    return latest, attempt_by_request_id, frozen_input


def _prompt_field_retry_policy(
    stage: StageRun,
    *,
    route_index: int | None,
) -> dict[str, int]:
    """Read the immutable per-stage retry policy without historical defaults."""

    values = _stage_config_values(stage, route_index=route_index)
    if PROMPT_FIELD_RETRY_LIMIT_CONFIG_KEY not in values:
        raise StageBlocked(
            "config_snapshot_missing",
            f"冻结配置缺少 {PROMPT_FIELD_RETRY_LIMIT_CONFIG_KEY}",
        )
    raw_limit = values[PROMPT_FIELD_RETRY_LIMIT_CONFIG_KEY]
    retry_limit_max = _automatic_retry_max_items()
    if type(raw_limit) is not int or not 0 <= raw_limit <= retry_limit_max:
        raise StageBlocked(
            "prompt_validation_retry_policy_invalid",
            (
                f"冻结配置 {PROMPT_FIELD_RETRY_LIMIT_CONFIG_KEY} 必须是 "
                f"0..{retry_limit_max} 的整数"
            ),
        )
    return {
        "retry_limit": raw_limit,
        "max_attempts": raw_limit + 1,
    }


def _text_contract_retry_policy(
    stage: StageRun,
    *,
    route_index: int | None,
) -> dict[str, int]:
    """Read the frozen finite-retry budget for known text response rejection."""

    values = _stage_config_values(stage, route_index=route_index)
    if TEXT_CONTRACT_RETRY_LIMIT_CONFIG_KEY not in values:
        raise StageBlocked(
            "config_snapshot_missing",
            f"冻结配置缺少 {TEXT_CONTRACT_RETRY_LIMIT_CONFIG_KEY}",
        )
    raw_limit = values[TEXT_CONTRACT_RETRY_LIMIT_CONFIG_KEY]
    retry_limit_max = _automatic_retry_max_items()
    if type(raw_limit) is not int or not 0 <= raw_limit <= retry_limit_max:
        raise StageBlocked(
            "text_contract_retry_policy_invalid",
            (
                f"冻结配置 {TEXT_CONTRACT_RETRY_LIMIT_CONFIG_KEY} 必须是 "
                f"0..{retry_limit_max} 的整数"
            ),
        )
    return {"retry_limit": raw_limit, "max_attempts": raw_limit + 1}


def _verified_text_contract_rejection(
    attempt: GenerationAttempt,
) -> dict[str, Any]:
    """Return one safe immutable rejection record; never expose model output."""

    if (
        attempt.status != GenerationAttempt.Status.FAILED
        or attempt.error_code != "provider_response_invalid"
        or attempt.operation not in {
            TEXT_OPERATION,
            SCRIPT_STRUCTURE_OPERATION,
            VISUAL_RULES_OPERATION,
        }
    ):
        raise StageBlocked(
            "text_contract_retry_audit_incomplete",
            "文本合同有限重试只接受已知且明确失败的 Provider 响应。",
        )
    resolutions = list(attempt.resolutions.filter(
        outcome=GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED,
    ).order_by("revision", "id"))
    if len(resolutions) != 1:
        raise StageBlocked(
            "text_contract_retry_audit_incomplete",
            "文本合同失败缺少唯一的已知响应拒绝审计；禁止自动重放。",
        )
    resolution = resolutions[0]
    try:
        verify_json_contract(
            resolution.evidence,
            schema_version=resolution.evidence_schema_version,
            size_bytes=resolution.evidence_size_bytes,
            sha256=resolution.evidence_sha256,
            max_bytes=_text_snapshot_max_bytes(),
        )
    except ValidationError as exc:
        raise StageBlocked(
            "text_contract_retry_audit_incomplete",
            "文本合同失败的已知响应拒绝审计校验和无效。",
        ) from exc
    evidence = resolution.evidence
    classifier = str(
        evidence.get("contract_error_code") or ""
    ) if isinstance(evidence, dict) else ""
    if (
        not isinstance(evidence, dict)
        or evidence.get("attempt_id") != str(attempt.id)
        or evidence.get("request_sha256") != attempt.request_sha256
        or evidence.get("failure_code") != attempt.error_code
        or classifier not in SAFE_PROMPT_RESPONSE_CLASSIFIERS
    ):
        raise StageBlocked(
            "text_contract_retry_audit_incomplete",
            "文本合同失败与安全拒绝分类或原请求身份不一致；禁止自动重放。",
        )
    return {
        "attempt_id": str(attempt.id),
        "attempt_number": int(attempt.attempt_number),
        "contract_error_code": classifier,
        "resolution_id": str(resolution.id),
        "resolution_evidence_sha256": resolution.evidence_sha256,
        **(
            {"finish_reason": str(evidence.get("finish_reason") or "")}
            if evidence.get("finish_reason") else {}
        ),
    }


def _queue_text_contract_retry_if_allowed(
    run: ProductionRun,
    stage: StageRun,
    *,
    latest: GenerationAttempt,
) -> bool:
    """Queue one safe new request or stop for explicit human confirmation."""

    if (
        latest.status != GenerationAttempt.Status.FAILED
        or latest.error_code != "provider_response_invalid"
    ):
        return False
    current = _verified_text_contract_rejection(latest)
    classifier = current["contract_error_code"]
    if classifier not in TEXT_CONTRACT_AUTO_RETRY_CLASSIFIERS:
        detail = (
            "Provider 明示内容过滤；平台不会自动换模型或重复提交。"
            if classifier == "response_content_filtered"
            else "该已知响应拒绝类型没有注册安全自动重放策略。"
        )
        raise StageBlocked(
            TEXT_CONTRACT_HUMAN_CONFIRMATION_REQUIRED,
            f"{detail} 请人工核对来源与模型策略，并明确确认是否重试。",
        )
    policy = _text_contract_retry_policy(
        stage,
        route_index=latest.route_index,
    )
    failures: list[dict[str, Any]] = []
    for attempt in stage.generation_attempts.filter(
        unit_key=latest.unit_key,
        status=GenerationAttempt.Status.FAILED,
        error_code="provider_response_invalid",
    ).order_by("attempt_number", "id"):
        failures.append(_verified_text_contract_rejection(attempt))
    if not failures or failures[-1]["attempt_id"] != str(latest.id):
        raise StageBlocked(
            "text_contract_retry_audit_incomplete",
            "文本合同失败次数与当前 attempt 不一致；禁止自动重放。",
        )
    if len(failures) >= int(policy["max_attempts"]):
        raise StageBlocked(
            TEXT_CONTRACT_HUMAN_CONFIRMATION_REQUIRED,
            (
                f"文本模型响应已连续 {len(failures)} 次未通过硬合同，达到本次运行"
                f"冻结上限（首次调用 + {policy['retry_limit']} 次自动重试）。"
                "失败输出未成为候选；请人工确认是否再次付费重试或调整模型策略。"
            ),
        )
    route = (
        latest.request_snapshot.get("route")
        if isinstance(latest.request_snapshot, dict) else None
    )
    if not isinstance(route, dict) or route.get("index") != latest.route_index:
        raise StageBlocked(
            "text_contract_retry_audit_incomplete",
            "文本合同有限重试缺少与失败 attempt 一致的冻结 route。",
        )
    retry_contract = {
        "schema_version": 1,
        "config_key": TEXT_CONTRACT_RETRY_LIMIT_CONFIG_KEY,
        "retry_limit": int(policy["retry_limit"]),
        "max_attempts": int(policy["max_attempts"]),
        "attempts_used": len(failures),
        "repair_of_attempt_id": str(latest.id),
        "contract_error_code": classifier,
        "resolution_id": current["resolution_id"],
        "resolution_evidence_sha256": current["resolution_evidence_sha256"],
    }
    next_attempt_number = int(latest.attempt_number) + 1
    if latest.operation == TEXT_OPERATION:
        _queue_text_attempt_for_route(
            run,
            stage,
            route=route,
            attempt_number=next_attempt_number,
            reason="known_text_contract_retry",
            retry_contract=retry_contract,
        )
    elif latest.operation == SCRIPT_STRUCTURE_OPERATION:
        _queue_script_structure_attempt_for_route(
            run,
            stage,
            route=route,
            attempt_number=next_attempt_number,
            reason="known_text_contract_retry",
            retry_contract=retry_contract,
        )
    elif latest.operation == VISUAL_RULES_OPERATION:
        frozen_plan = latest.request_snapshot.get("plan")
        if not isinstance(frozen_plan, dict):
            raise StageBlocked(
                "text_contract_retry_audit_incomplete",
                "影视规则有限重试缺少冻结 plan。",
            )
        _queue_visual_rules_attempt_for_route(
            run,
            stage,
            route=route,
            attempt_number=next_attempt_number,
            reason="known_text_contract_retry",
            frozen_plan=frozen_plan,
            retry_contract=retry_contract,
        )
    else:  # pragma: no cover - guarded by _verified_text_contract_rejection
        raise StageBlocked(
            "text_contract_retry_operation_unregistered",
            "当前文本 operation 没有注册有限重试适配器。",
        )
    _append_route_event(
        stage,
        unit_key=latest.unit_key,
        route_index=latest.route_index,
        event_type="known_text_contract_retry_queued",
        code=classifier,
    )
    return True


def _prompt_normalization_validation_failures(
    stage: StageRun,
    *,
    unit_key: str,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for attempt in stage.generation_attempts.filter(
        unit_key=unit_key,
        status=GenerationAttempt.Status.FAILED,
        error_code__in=PROMPT_VALIDATION_RETRYABLE_CODES,
    ).order_by("attempt_number", "id"):
        resolutions = list(attempt.resolutions.filter(
            outcome=GenerationAttemptResolution.Outcome.KNOWN_RESPONSE_REJECTED,
        ).order_by("revision", "id"))
        if len(resolutions) != 1:
            raise StageBlocked(
                "prompt_validation_retry_audit_incomplete",
                "提示词校验失败缺少唯一的已知响应拒绝审计",
            )
        resolution = resolutions[0]
        try:
            verify_json_contract(
                resolution.evidence,
                schema_version=resolution.evidence_schema_version,
                size_bytes=resolution.evidence_size_bytes,
                sha256=resolution.evidence_sha256,
                max_bytes=_text_snapshot_max_bytes(),
            )
        except ValidationError as exc:
            raise StageBlocked(
                "prompt_validation_retry_audit_incomplete",
                "提示词校验失败的已知响应拒绝审计校验和无效",
            ) from exc
        resolution_evidence = resolution.evidence
        failure_code = str(
            resolution_evidence.get("failure_code") or ""
        ) if isinstance(resolution_evidence, dict) else ""
        contract_error_code = str(
            resolution_evidence.get("contract_error_code") or ""
        ) if isinstance(resolution_evidence, dict) else ""
        reason_code = safe_prompt_field_validation_reason_code(
            resolution_evidence.get("reason_code")
            if isinstance(resolution_evidence, dict)
            else ""
        )
        validation_context = safe_prompt_field_validation_context(
            reason_code,
            resolution_evidence.get("validation_context")
            if isinstance(resolution_evidence, dict)
            else None,
            request_payload=(
                attempt.request_snapshot.get("payload")
                if isinstance(attempt.request_snapshot, dict)
                else None
            ),
        )
        if (
            not isinstance(resolution_evidence, dict)
            or resolution_evidence.get("attempt_id") != str(attempt.id)
            or resolution_evidence.get("request_sha256") != attempt.request_sha256
            or failure_code != attempt.error_code
        ):
            raise StageBlocked(
                "prompt_validation_retry_audit_incomplete",
                "提示词校验失败与已知响应拒绝审计不一致",
            )
        failures.append({
            "attempt_id": str(attempt.id),
            "attempt_number": int(attempt.attempt_number),
            "route_index": attempt.route_index,
            "unit_key": attempt.unit_key,
            "batch_index": attempt.request_snapshot.get("batch_index"),
            "error_code": attempt.error_code,
            "error_message": (
                "Provider 返回的响应未通过严格合同校验。"
                if attempt.error_code == "provider_response_invalid"
                else "提示词规范化结果未通过严格结构或语义校验。"
            ),
            "resolution_id": str(resolution.id),
            "resolution_evidence_sha256": resolution.evidence_sha256,
            "contract_error_code": contract_error_code,
            **({"reason_code": reason_code} if reason_code else {}),
            **(
                {"validation_context": validation_context}
                if validation_context else {}
            ),
            **(
                {"finish_reason": str(resolution_evidence.get("finish_reason") or "")}
                if resolution_evidence.get("finish_reason") else {}
            ),
            **(
                {"usage": deepcopy(resolution_evidence.get("usage"))}
                if isinstance(resolution_evidence.get("usage"), dict) else {}
            ),
            **(
                {
                    "response_id_sha256": str(
                        resolution_evidence.get("response_id_sha256") or "",
                    ),
                }
                if resolution_evidence.get("response_id_sha256") else {}
            ),
        })
    return failures


def _prompt_normalization_retry_feedback(
    attempt: GenerationAttempt,
    failure: dict[str, Any],
) -> dict[str, Any] | None:
    """Derive one bounded repair hint from verified immutable rejection evidence."""

    if (
        not isinstance(failure, dict)
        or failure.get("attempt_id") != str(attempt.id)
        or attempt.status != GenerationAttempt.Status.FAILED
        or attempt.operation != PROMPT_NORMALIZE_OPERATION
        or failure.get("contract_error_code") not in {
            "prompt_field_generation_contract_invalid",
            "prompt_normalize_contract_invalid",
        }
        or (
            failure.get("contract_error_code") == "prompt_normalize_contract_invalid"
            and failure.get("reason_code") != "protected_token_mismatch"
        )
    ):
        return None
    try:
        verify_json_contract(
            attempt.request_snapshot,
            schema_version=attempt.request_snapshot_schema_version,
            size_bytes=attempt.request_snapshot_size_bytes,
            sha256=attempt.request_snapshot_sha256,
            max_bytes=_text_snapshot_max_bytes(),
        )
    except ValidationError as exc:
        raise StageBlocked(
            "prompt_validation_retry_audit_incomplete",
            "提示词安全修复重试的冻结请求校验和无效",
        ) from exc
    payload = (
        attempt.request_snapshot.get("payload")
        if isinstance(attempt.request_snapshot, dict)
        else None
    )
    if not isinstance(payload, dict):
        raise StageBlocked(
            "prompt_validation_retry_audit_incomplete",
            "提示词安全修复重试缺少冻结请求",
        )
    payload_contract = json_contract(payload, max_bytes=_text_snapshot_max_bytes())
    if (
        attempt.request_snapshot.get("payload_sha256") != payload_contract.sha256
        or attempt.request_sha256 != payload_contract.sha256
    ):
        raise StageBlocked(
            "prompt_validation_retry_audit_incomplete",
            "提示词安全修复重试的请求身份已漂移",
        )
    candidate = {
        "schema_version": 1,
        "reason_code": failure.get("reason_code"),
        "validation_context": failure.get("validation_context"),
    }
    normalized = safe_prompt_normalization_retry_feedback(
        candidate,
        request_payload=payload,
    )
    if not normalized or normalized != candidate:
        return None
    return normalized


def _materialize_prompt_normalization_human_fallback(
    run: ProductionRun,
    stage: StageRun,
    *,
    latest: GenerationAttempt,
    policy: dict[str, int],
    stop_reason: str,
) -> str:
    """Freeze complete editable candidates after bounded automatic repair stops."""

    batch_plan = _verify_prompt_normalization_batch_plan(
        latest.request_snapshot.get("batch_plan"),
    )
    frozen_input = batch_plan["full_frozen_input"]
    targets = frozen_input.get("targets") if isinstance(frozen_input, dict) else None
    candidates = _blank_prompt_normalization_candidates(
        targets,
        invalid_message="prompt.normalize 人工接管字段来源无效",
    )
    progress = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
    completed_batches = (
        deepcopy(progress.get("completed_batches") or [])
        if progress.get("kind") == "prompt_normalization_batch_progress"
        else []
    )
    completed_by_request_id: dict[str, dict[str, Any]] = {}
    for completed in completed_batches:
        result = completed.get("result") if isinstance(completed, dict) else None
        for candidate in result.get("candidates") or [] if isinstance(result, dict) else []:
            request_id = str(candidate.get("request_id") or "") if isinstance(candidate, dict) else ""
            if not request_id or request_id in completed_by_request_id:
                raise StageBlocked(
                    "prompt_normalization_batch_progress_invalid",
                    "prompt.normalize 已完成批次候选身份无效",
                )
            completed_by_request_id[request_id] = deepcopy(candidate)
    candidates = [
        completed_by_request_id.get(str(candidate["request_id"]), candidate)
        for candidate in candidates
    ]

    failures = _prompt_normalization_validation_failures(
        stage,
        unit_key=latest.unit_key,
    )
    if (
        stop_reason not in PROMPT_VALIDATION_STOP_REASONS
        or not failures
        or failures[-1]["attempt_id"] != str(latest.id)
    ):
        raise StageBlocked(
            "prompt_validation_retry_audit_incomplete",
            "提示词校验重试审计与冻结策略不一致",
        )
    retry_limit_reached = len(failures) >= int(policy["max_attempts"])
    if stop_reason == PROMPT_VALIDATION_STOP_RETRY_LIMIT:
        if not retry_limit_reached:
            raise StageBlocked(
                "prompt_validation_retry_audit_incomplete",
                "提示词校验尚未达到冻结的重试上限",
            )
    elif (
        retry_limit_reached
        or _prompt_normalization_retry_feedback(latest, failures[-1]) is not None
    ):
        raise StageBlocked(
            "prompt_validation_retry_audit_incomplete",
            "提示词校验仍存在可安全冻结的修复反馈",
        )
    retry_policy = {
        "config_key": PROMPT_FIELD_RETRY_LIMIT_CONFIG_KEY,
        "retry_limit": int(policy["retry_limit"]),
        "max_attempts": int(policy["max_attempts"]),
        "attempts_used": len(failures),
    }
    evidence = {
        "schema_version": 1,
        "review_origin": "human_after_validation_retry_exhausted",
        "stop_reason": stop_reason,
        "batch_plan_sha256": batch_plan["batch_plan_sha256"],
        "latest_attempt_id": str(latest.id),
        "retry_policy": retry_policy,
        "validation_failures": failures,
        "candidates": candidates,
    }
    evidence_sha256 = _sha256(evidence)
    deployment = latest.model_deployment
    profile = deployment.profile_version if deployment is not None else None
    provider = profile.provider if profile is not None else None
    payload = {
        "schema_version": 1,
        "kind": "prompt_normalization_candidates",
        "review_origin": "human_after_validation_retry_exhausted",
        "stop_reason": stop_reason,
        "attempt_id": str(latest.id),
        "attempt_ids": [
            *[str(item.get("attempt_id") or "") for item in completed_batches],
            str(latest.id),
        ],
        "result_sha256": evidence_sha256,
        "provider": str(getattr(provider, "key", "") or "")[:80],
        "model": str(getattr(profile, "model_identifier", "") or "")[:200],
        "submission_semantics": "batched_durable_attempt",
        "batch_plan_sha256": batch_plan["batch_plan_sha256"],
        "batch_count": len(batch_plan["batches"]),
        "batch_results": completed_batches,
        "frozen_input": deepcopy(frozen_input),
        "frozen_input_schema_version": batch_plan["full_frozen_input_schema_version"],
        "frozen_input_size_bytes": batch_plan["full_frozen_input_size_bytes"],
        "frozen_input_sha256": batch_plan["full_frozen_input_sha256"],
        "retry_policy": retry_policy,
        "validation_failures": failures,
        "candidates": candidates,
    }
    _save_stage_snapshot(stage, "output_snapshot", payload)
    _append_route_event(
        stage,
        unit_key=latest.unit_key,
        route_index=latest.route_index,
        event_type="validation_review_required",
        code=PROMPT_VALIDATION_REVIEW_REQUIRED,
    )
    return f"prompt-normalization-human-review:{latest.id}"


def _blank_prompt_normalization_candidates(
    targets: object,
    *,
    invalid_message: str,
) -> list[dict[str, Any]]:
    if not isinstance(targets, list) or not targets:
        raise StageBlocked("prompt_normalize_input_invalid", invalid_message)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        request_id = str(target.get("request_id") or "") if isinstance(target, dict) else ""
        if (
            not request_id
            or request_id in seen
            or not isinstance(target, dict)
            or not target.get("scope_type")
            or not target.get("scope_id")
            or not target.get("field_key")
            or not target.get("source_hash")
            or not target.get("context_hash")
        ):
            raise StageBlocked(
                "prompt_normalize_input_invalid",
                invalid_message,
            )
        seen.add(request_id)
        candidate = {
            "request_id": request_id,
            "scope_type": str(target["scope_type"]),
            "scope_id": str(target["scope_id"]),
            "field_key": str(target["field_key"]),
            "label": str(target.get("label") or "")[:240],
            "source_text": str(target.get("source_text") or ""),
            "source_hash": str(target["source_hash"]),
            "context_hash": str(target["context_hash"]),
            # Invalid model output is deliberately not exposed as an
            # approvable candidate.  The operator must provide every value.
            "translated_text": "",
        }
        if target.get("target_kind") == "prompt_profile_field":
            for key in _PROMPT_PROFILE_TARGET_IDENTITY_KEYS:
                if key in target:
                    candidate[key] = deepcopy(target[key])
        candidates.append(candidate)
    return candidates


def _materialize_prompt_normalization_manual_review(
    run: ProductionRun,
    stage: StageRun,
    *,
    frozen_input: dict[str, Any],
    route_index: int | None,
    review_origin: str,
) -> str:
    """Create audited editable candidates without submitting to a Provider."""

    if review_origin != "human_only_recipe":
        raise StageBlocked(
            "prompt_normalize_manual_path_invalid",
            "纯人工提示词处理缺少受支持的冻结来源。",
        )
    payload = deepcopy(frozen_input.get("payload"))
    targets = deepcopy(frozen_input.get("targets"))
    if not isinstance(payload, dict):
        raise StageBlocked(
            "prompt_normalize_input_invalid",
            "纯人工提示词处理缺少冻结字段合同。",
        )
    try:
        validate_prompt_normalization_run_contract(payload)
    except PromptNormalizationContractError as exc:
        raise StageBlocked(
            "prompt_normalize_input_invalid",
            "纯人工提示词字段不满足冻结 PromptProfile 合同。",
        ) from exc
    candidates = _blank_prompt_normalization_candidates(
        targets,
        invalid_message="纯人工提示词处理的冻结字段来源无效。",
    )
    frozen_contract = json_contract(
        {"payload": payload, "targets": targets},
        max_bytes=_text_snapshot_max_bytes(),
    )
    evidence = {
        "schema_version": 1,
        "review_origin": review_origin,
        "frozen_input_sha256": frozen_contract.sha256,
        "candidates": candidates,
    }
    payload_snapshot = {
        "schema_version": 1,
        "kind": "prompt_normalization_candidates",
        "review_origin": review_origin,
        "attempt_id": None,
        "result_sha256": _sha256(evidence),
        "provider": "",
        "model": "",
        "submission_semantics": "human_only_no_submission",
        "frozen_input": frozen_contract.value,
        "frozen_input_schema_version": frozen_contract.schema_version,
        "frozen_input_size_bytes": frozen_contract.size_bytes,
        "frozen_input_sha256": frozen_contract.sha256,
        "candidates": candidates,
    }
    _save_stage_snapshot(stage, "output_snapshot", payload_snapshot)
    if route_index is not None:
        _mark_route_selected(
            stage,
            unit_key=PROMPT_NORMALIZE_UNIT_KEY,
            route_index=route_index,
            reason=review_origin,
        )
    _append_route_event(
        stage,
        unit_key=PROMPT_NORMALIZE_UNIT_KEY,
        route_index=route_index,
        event_type="human_review_selected",
        code=review_origin,
    )
    return f"prompt-normalization-human-only:{review_origin}"


def _text_smoke_projection(snapshot: ModelDeploymentCapabilitySnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": str(snapshot.id),
        "revision": int(snapshot.revision),
        "target_sha256": snapshot.target_sha256,
        "provider_config_sha256": snapshot.provider_config_sha256,
        "deployment_config_sha256": snapshot.deployment_config_sha256,
        "credential_binding_sha256": snapshot.credential_binding_sha256,
        "evidence_sha256": snapshot.evidence_sha256,
        "captured_at": snapshot.captured_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
    }


def _route_with_revalidated_text_smoke(
    stage: StageRun,
    *,
    route: dict[str, Any],
    deployment: ModelDeployment,
    operation: str,
    unit_key: str,
    current: ModelDeploymentCapabilitySnapshot | None = None,
    invalid_code: str,
    missing_code: str,
    drift_code: str,
) -> dict[str, Any]:
    """Admit a newer smoke only when it proves the exact frozen target.

    The immutable StageRun routing remains the run's original routing decision.
    A newer same-target smoke is instead frozen into the new GenerationAttempt
    route and its checksum is appended to StageRouteEvent/routing_decision.  This
    lets a long review pause recover from TTL expiry without silently changing
    deployment, model profile, provider/deployment config or credentials.
    """

    if current is None:
        try:
            current = fresh_smoke_snapshot(deployment, operation)
        except ValidationError as exc:
            raise StageBlocked(invalid_code, f"{operation} 当前 smoke 证据合同无效") from exc
    if current is None:
        raise StageBlocked(missing_code, f"{operation} route 缺少当前有效 smoke 证据")

    smoke_evidence = route.get("smoke_evidence")
    frozen = smoke_evidence.get(operation) if isinstance(smoke_evidence, dict) else None
    if not isinstance(frozen, dict):
        raise StageBlocked(drift_code, f"冻结 {operation} route 缺少原始 smoke 证据")
    frozen_snapshot_id = str(frozen.get("snapshot_id") or "")
    frozen_target_sha256 = str(frozen.get("target_sha256") or "")
    frozen_evidence_sha256 = str(frozen.get("evidence_sha256") or "")
    try:
        original = ModelDeploymentCapabilitySnapshot.objects.get(
            pk=frozen_snapshot_id,
            deployment_id=route.get("model_deployment_id"),
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
        )
        verify_json_contract(
            original.operations,
            schema_version=original.operations_schema_version,
            size_bytes=original.operations_size_bytes,
            sha256=original.operations_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        verify_json_contract(
            original.evidence,
            schema_version=original.evidence_schema_version,
            size_bytes=original.evidence_size_bytes,
            sha256=original.evidence_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        )
    except (ModelDeploymentCapabilitySnapshot.DoesNotExist, ValidationError, TypeError, ValueError) as exc:
        raise StageBlocked(drift_code, f"冻结 {operation} 原始 smoke 证据不可验证") from exc
    if (
        operation not in original.operations
        or original.evidence.get("execution_submitted") is not True
        or original.target_sha256 != frozen_target_sha256
        or original.evidence_sha256 != frozen_evidence_sha256
        or original.provider_config_sha256 != route.get("provider_config_record_sha256")
        or original.deployment_config_sha256 != route.get("deployment_config_record_sha256")
        or original.credential_binding_sha256 != route.get("credential_binding_sha256")
    ):
        raise StageBlocked(drift_code, f"冻结 {operation} 原始 smoke 与 route 指纹不一致")

    if (
        current.deployment_id != deployment.id
        or current.target_sha256 != frozen_target_sha256
        or current.provider_config_sha256 != route.get("provider_config_record_sha256")
        or current.deployment_config_sha256 != route.get("deployment_config_record_sha256")
        or current.credential_binding_sha256 != route.get("credential_binding_sha256")
    ):
        raise StageBlocked(
            drift_code,
            f"{operation} 新 smoke 与冻结 deployment/profile/target/config 不一致",
        )
    if str(current.id) == frozen_snapshot_id:
        if current.evidence_sha256 != frozen_evidence_sha256:
            raise StageBlocked(drift_code, f"冻结 {operation} smoke evidence hash 已漂移")
        return route

    current_projection = _text_smoke_projection(current)
    audit_payload = {
        "schema_version": 1,
        "operation": operation,
        "unit_key": unit_key,
        "route_index": int(route["index"]),
        "model_deployment_id": str(route.get("model_deployment_id") or ""),
        "model_profile_version_id": str(route.get("model_profile_version_id") or ""),
        "stage_routing_sha256": stage.routing_sha256,
        "previous_smoke": {
            "snapshot_id": frozen_snapshot_id,
            "target_sha256": frozen_target_sha256,
            "evidence_sha256": frozen_evidence_sha256,
        },
        "current_smoke": current_projection,
    }
    audit_payload["revalidation_sha256"] = _sha256(audit_payload)

    decision = deepcopy(stage.routing_decision) if isinstance(stage.routing_decision, dict) else {}
    previous_audits = decision.get("smoke_revalidations")
    if previous_audits is None:
        previous_audits = []
    if not isinstance(previous_audits, list):
        raise StageBlocked("routing_decision_snapshot_invalid", "阶段 smoke 重验证审计结构无效")
    if not any(
        isinstance(item, dict)
        and item.get("revalidation_sha256") == audit_payload["revalidation_sha256"]
        for item in previous_audits
    ):
        _append_route_event(
            stage,
            unit_key=unit_key,
            route_index=int(route["index"]),
            event_type="smoke_revalidated",
            code=audit_payload["revalidation_sha256"],
        )
        previous_audits = [*previous_audits, audit_payload]
        decision.update({
            "schema_version": 1,
            "last_unit_key": unit_key,
            "smoke_revalidations": previous_audits,
            "event_count": stage.route_events.count(),
        })
        _save_routing_decision(stage, decision)

    effective_route = deepcopy(route)
    effective_smoke_evidence = deepcopy(smoke_evidence)
    effective_smoke_evidence[operation] = {
        **current_projection,
        "revalidated_from_snapshot_id": frozen_snapshot_id,
        "revalidation_sha256": audit_payload["revalidation_sha256"],
    }
    effective_route["smoke_evidence"] = effective_smoke_evidence
    return effective_route


def _queue_prompt_normalization_attempt_for_route(
    run: ProductionRun,
    stage: StageRun,
    *,
    route: dict[str, Any],
    attempt_number: int,
    reason: str,
    frozen_input: dict[str, Any] | None = None,
    frozen_batch_plan: dict[str, Any] | None = None,
    batch_index: int = 0,
    frozen_prompt_profile_version=None,
    frozen_prompt_profile_binding_key: str = "",
    frozen_prompt_profile_contract_sha256: str = "",
    repair_of_attempt: GenerationAttempt | None = None,
    retry_feedback: dict[str, Any] | None = None,
) -> GenerationAttempt:
    if (repair_of_attempt is None) != (retry_feedback is None):
        raise StageBlocked(
            "prompt_normalization_retry_feedback_invalid",
            "prompt.normalize 安全修复 attempt 与反馈必须成对冻结",
        )
    try:
        live_deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).get(pk=route["model_deployment_id"], status=ModelDeployment.Status.ACTIVE)
        deployment = frozen_text_deployment({"route": route})
    except (ModelDeployment.DoesNotExist, ValidationError, KeyError, ValueError) as exc:
        raise StageBlocked("frozen_route_unavailable", "冻结 prompt.normalize deployment 已不可用") from exc
    capacity_policy = validate_prompt_normalization_model_profile(
        deployment.profile_version,
    )
    if capacity_policy is None:
        raise StageBlocked("prompt_normalize_capability_missing", "冻结模型未声明 prompt.normalize")
    state, state_reason, normalization_route = prompt_normalization_route_state(
        live_deployment,
        execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
    )
    if state != "ready" or normalization_route is None:
        raise StageBlocked(state_reason, "prompt.normalize route 未通过 durable/fresh-smoke 门禁")
    route = _route_with_revalidated_text_smoke(
        stage,
        route=route,
        deployment=live_deployment,
        operation=PROMPT_NORMALIZE_OPERATION,
        unit_key=(
            str(
                (frozen_batch_plan or {}).get("batches", [{}])[batch_index].get(
                    "unit_key",
                    PROMPT_NORMALIZE_UNIT_KEY,
                ),
            )
            if isinstance((frozen_batch_plan or {}).get("batches"), list)
            and 0 <= batch_index < len((frozen_batch_plan or {}).get("batches", []))
            else PROMPT_NORMALIZE_UNIT_KEY
        ),
        current=normalization_route.smoke,
        invalid_code="prompt_normalize_smoke_invalid",
        missing_code="prompt_normalize_smoke_required",
        drift_code="prompt_normalize_smoke_drift",
    )
    resolved_profile = None
    selected_profile_version = frozen_prompt_profile_version
    selected_binding_key = frozen_prompt_profile_binding_key
    selected_contract_sha256 = frozen_prompt_profile_contract_sha256
    try:
        if frozen_prompt_profile_version is not None:
            verify_frozen_profile_reference(
                frozen_prompt_profile_version,
                binding_key=frozen_prompt_profile_binding_key,
                contract_sha256=frozen_prompt_profile_contract_sha256,
            )
        else:
            resolved_profile = require_prompt_profile_binding(
                operation=PROMPT_NORMALIZE_OPERATION,
                model_family=model_family_for_profile(deployment.profile_version),
                model_profile_version=live_deployment.profile_version,
            )
            selected_profile_version = resolved_profile.profile_version
            selected_binding_key = resolved_profile.binding.key
            selected_contract_sha256 = resolved_profile.profile_version.contract_sha256
    except (ValidationError, PromptProfileResolutionError) as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "prompt_profile_invalid"),
            str(exc),
        ) from exc
    try:
        if frozen_batch_plan is None:
            source_input = frozen_input or _build_prompt_normalization_input(run)
            batch_plan = _build_prompt_normalization_batch_plan(
                run,
                frozen_input=source_input,
                policy=capacity_policy,
                prompt_profile_version=selected_profile_version,
            )
        else:
            batch_plan = _verify_prompt_normalization_batch_plan(frozen_batch_plan)
        batches = batch_plan["batches"]
        if type(batch_index) is not int or not 0 <= batch_index < len(batches):
            raise PromptNormalizationContractError(
                "prompt.normalize 批次序号超出冻结计划",
                code="prompt_normalization_batch_contract_invalid",
            )
        batch = batches[batch_index]
        execution_kind = str(batch.get("execution_kind") or "")
        if (
            execution_kind == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
            and repair_of_attempt is not None
        ):
            raise PromptNormalizationContractError(
                "服务端确定性物化批次不允许创建 Provider 修复请求",
                code="prompt_normalization_retry_feedback_invalid",
            )
        payload = _assemble_prompt_normalization_batch_payload(
            run,
            base_payload=batch["base_payload"],
            prompt_profile_version=selected_profile_version,
        )
        repair_feedback_sha256 = ""
        if repair_of_attempt is not None and retry_feedback is not None:
            previous_snapshot = repair_of_attempt.request_snapshot
            previous_payload = (
                previous_snapshot.get("payload")
                if isinstance(previous_snapshot, dict)
                else None
            )
            previous_batch_plan = (
                previous_snapshot.get("batch_plan")
                if isinstance(previous_snapshot, dict)
                and isinstance(previous_snapshot.get("batch_plan"), dict)
                else {}
            )
            if (
                repair_of_attempt.stage_run_id != stage.id
                or repair_of_attempt.operation != PROMPT_NORMALIZE_OPERATION
                or repair_of_attempt.status != GenerationAttempt.Status.FAILED
                or repair_of_attempt.unit_key != batch.get("unit_key")
                or repair_of_attempt.attempt_number + 1 != attempt_number
                or not isinstance(previous_payload, dict)
                or previous_snapshot.get("batch_index") != batch_index
                or previous_batch_plan.get(
                    "batch_plan_sha256",
                ) != batch_plan["batch_plan_sha256"]
            ):
                raise PromptNormalizationContractError(
                    "prompt.normalize 安全修复 attempt 身份无效",
                    code="prompt_normalization_retry_feedback_invalid",
                )
            previous_base_payload = deepcopy(previous_payload)
            previous_base_payload.pop("retry_feedback", None)
            if previous_base_payload != payload:
                raise PromptNormalizationContractError(
                    "prompt.normalize 安全修复改变了冻结字段或 Profile",
                    code="prompt_normalization_retry_feedback_invalid",
                )
            normalized_feedback = safe_prompt_normalization_retry_feedback(
                retry_feedback,
                request_payload=previous_payload,
            )
            if not normalized_feedback or normalized_feedback != retry_feedback:
                raise PromptNormalizationContractError(
                    "prompt.normalize 安全修复反馈未通过冻结字段反校验",
                    code="prompt_normalization_retry_feedback_invalid",
                )
            payload["retry_feedback"] = deepcopy(normalized_feedback)
            repair_feedback_sha256 = _sha256(normalized_feedback)
        targets = deepcopy(batch["targets"])
        verify_frozen_profile_assembly(
            selected_profile_version,
            payload.get("prompt_profile_assembly"),
        )
        validate_prompt_normalization_request_contract(payload)
        chat_input_bytes = _prompt_normalization_chat_input_bytes(
            payload,
            policy=capacity_policy,
        )
    except (PromptNormalizationContractError, ValidationError) as exc:
        raise StageBlocked(
            str(
                getattr(exc, "code", None)
                or "prompt_normalize_input_invalid"
            ),
            "prompt.normalize 冻结字段、容量或 PromptProfile 字段生成合同无效。",
        ) from exc
    unit_key = str(batch["unit_key"])
    payload_contract = json_contract(payload, max_bytes=_text_snapshot_max_bytes())
    if (
        repair_of_attempt is not None
        and payload_contract.sha256 == repair_of_attempt.request_sha256
    ):
        raise StageBlocked(
            "prompt_normalization_retry_feedback_invalid",
            "prompt.normalize 安全修复请求未产生新的冻结身份",
        )
    snapshot = {
        "schema_version": 1,
        "payload": payload_contract.value,
        "payload_sha256": payload_contract.sha256,
        "targets": targets,
        "batch_plan": batch_plan,
        "batch_index": batch_index,
        "batch_count": len(batches),
        "generation_mode": str(batch["generation_mode"]),
        "execution_kind": execution_kind,
        "materialization_proofs_sha256": str(
            batch.get("materialization_proofs_sha256") or ""
        ),
        "capacity_policy": capacity_policy,
        "capacity_policy_sha256": _sha256(capacity_policy),
        "chat_input_bytes": chat_input_bytes,
        "route": route,
        "submission_semantics": (
            PROMPT_NORMALIZATION_LOCAL_SUBMISSION_SEMANTICS
            if execution_kind == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
            else route.get("submission_semantics") or (
                PROMPT_NORMALIZATION_SUBMISSION_SEMANTICS
                if route.get("is_external") else "local_synchronous"
            )
        ),
        **(
            {
                "repair_of_attempt_id": str(repair_of_attempt.id),
                "retry_feedback_sha256": repair_feedback_sha256,
            }
            if repair_of_attempt is not None else {}
        ),
    }
    snapshot_contract = json_contract(snapshot, max_bytes=_text_snapshot_max_bytes())
    if run.generation_task_count + 1 > _effective_generation_task_limit(run):
        raise StageBlocked("generation_task_budget_exhausted", "prompt.normalize 会超过运行任务上限")
    attempt = _create_generation_attempt(
        stage_run=stage,
        unit_key=unit_key,
        attempt_number=attempt_number,
        model_deployment=live_deployment,
        prompt_profile_version=selected_profile_version,
        prompt_profile_binding_key=selected_binding_key,
        prompt_profile_contract_sha256=selected_contract_sha256,
        route_index=int(route["index"]),
        operation=PROMPT_NORMALIZE_OPERATION,
        is_external=(
            False
            if execution_kind == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
            else bool(route.get("is_external"))
        ),
        status=GenerationAttempt.Status.QUEUED,
        request_sha256=payload_contract.sha256,
        request_snapshot=snapshot_contract.value,
        request_snapshot_schema_version=snapshot_contract.schema_version,
        request_snapshot_size_bytes=snapshot_contract.size_bytes,
        request_snapshot_sha256=snapshot_contract.sha256,
        estimated_cost=(
            Decimal("0")
            if execution_kind == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
            else None
        ),
        committed_cost=(
            Decimal("0")
            if execution_kind == PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC
            else None
        ),
        cost_currency="",
    )
    run.generation_task_count += 1
    run.save(update_fields=("generation_task_count", "updated_at"))
    if execution_kind != PROMPT_NORMALIZATION_EXECUTION_DETERMINISTIC:
        _estimate_and_reserve(
            run,
            stage,
            attempt,
            deployment=deployment,
            operation=PROMPT_NORMALIZE_OPERATION,
            payload=payload,
            is_external=attempt.is_external,
        )
    _mark_route_selected(
        stage,
        unit_key=unit_key,
        route_index=int(route["index"]),
        reason=reason,
    )
    return attempt


def _prepare_prompt_normalization(run: ProductionRun, stage: StageRun) -> str | None:
    latest = stage.generation_attempts.filter(
        operation=PROMPT_NORMALIZE_OPERATION,
    ).order_by("-created_at", "-id").first()
    if latest is not None:
        if latest.status == GenerationAttempt.Status.SUCCEEDED:
            snapshot = _verify_stage_snapshot(stage, "output_snapshot")
            batch_plan = _verify_prompt_normalization_batch_plan(
                latest.request_snapshot.get("batch_plan"),
            )
            batch_index = latest.request_snapshot.get("batch_index")
            if type(batch_index) is not int:
                raise StageBlocked(
                    "prompt_normalization_materialization_missing",
                    "成功 attempt 缺少冻结 batch_index",
                )
            batch_results = (
                snapshot.get("completed_batches")
                if snapshot.get("kind") == "prompt_normalization_batch_progress"
                else snapshot.get("batch_results")
                if snapshot.get("kind") == "prompt_normalization_candidates"
                else None
            )
            if not isinstance(batch_results, list):
                raise StageBlocked(
                    "prompt_normalization_materialization_missing",
                    "成功 attempt 缺少对应的批次候选快照",
                )
            matching = [
                item for item in batch_results
                if isinstance(item, dict)
                and item.get("attempt_id") == str(latest.id)
                and item.get("result_sha256") == latest.result_sha256
                and item.get("batch_index") == batch_index
            ]
            if len(matching) != 1:
                raise StageBlocked(
                    "prompt_normalization_materialization_missing",
                    "成功 attempt 的批次结果 checksum 或身份不一致",
                )
            if snapshot.get("kind") == "prompt_normalization_batch_progress":
                next_index = batch_index + 1
                if next_index >= len(batch_plan["batches"]):
                    raise StageBlocked(
                        "prompt_normalization_materialization_missing",
                        "最后批次成功后未生成完整审批候选",
                    )
                route = latest.request_snapshot.get("route")
                if not isinstance(route, dict):
                    raise StageBlocked(
                        "frozen_route_unavailable",
                        "prompt.normalize 下一批次缺少冻结 route",
                    )
                _queue_prompt_normalization_attempt_for_route(
                    run,
                    stage,
                    route=route,
                    attempt_number=1,
                    reason="prompt_normalization_next_batch_queued",
                    frozen_batch_plan=batch_plan,
                    batch_index=next_index,
                    frozen_prompt_profile_version=latest.prompt_profile_version,
                    frozen_prompt_profile_binding_key=latest.prompt_profile_binding_key,
                    frozen_prompt_profile_contract_sha256=(
                        latest.prompt_profile_contract_sha256
                    ),
                )
                return None
            if (
                snapshot.get("batch_plan_sha256") != batch_plan["batch_plan_sha256"]
                or len(batch_results) != len(batch_plan["batches"])
            ):
                raise StageBlocked(
                    "prompt_normalization_materialization_missing",
                    "完整审批候选与冻结批次计划不一致",
                )
            return f"prompt-normalization-batches:{batch_plan['batch_plan_sha256']}"
        if latest.status in {
            GenerationAttempt.Status.QUEUED,
            GenerationAttempt.Status.RUNNING,
            GenerationAttempt.Status.SUBMITTED,
        }:
            if latest.status == GenerationAttempt.Status.QUEUED and latest.committed_cost is None:
                _estimate_and_reserve(
                    run,
                    stage,
                    latest,
                    deployment=frozen_text_deployment(latest.request_snapshot),
                    operation=PROMPT_NORMALIZE_OPERATION,
                    payload=latest.request_snapshot["payload"],
                    is_external=latest.is_external,
                )
            return None
        routing = _verify_stage_routing(stage)
        if (
            latest.status == GenerationAttempt.Status.FAILED
            and latest.error_code in PROMPT_VALIDATION_RETRYABLE_CODES
        ):
            policy = _prompt_field_retry_policy(
                stage,
                route_index=latest.route_index,
            )
            _route_failure_once(stage, latest, latest.error_code)
            validation_failures = _prompt_normalization_validation_failures(
                stage,
                unit_key=latest.unit_key,
            )
            if (
                not validation_failures
                or validation_failures[-1]["attempt_id"] != str(latest.id)
            ):
                raise StageBlocked(
                    "prompt_validation_retry_audit_incomplete",
                    "提示词校验重试缺少当前失败 attempt 的不可变审计",
                )
            if len(validation_failures) >= policy["max_attempts"]:
                return _materialize_prompt_normalization_human_fallback(
                    run,
                    stage,
                    latest=latest,
                    policy=policy,
                    stop_reason=PROMPT_VALIDATION_STOP_RETRY_LIMIT,
                )

            retry_feedback = _prompt_normalization_retry_feedback(
                latest,
                validation_failures[-1],
            )
            if retry_feedback is None:
                return _materialize_prompt_normalization_human_fallback(
                    run,
                    stage,
                    latest=latest,
                    policy=policy,
                    stop_reason=PROMPT_VALIDATION_STOP_SAFE_FEEDBACK_UNAVAILABLE,
                )

            batch_plan = _verify_prompt_normalization_batch_plan(
                latest.request_snapshot.get("batch_plan"),
            )
            batch_index = latest.request_snapshot.get("batch_index")
            if type(batch_index) is not int:
                raise StageBlocked(
                    "prompt_normalization_batch_plan_invalid",
                    "prompt.normalize 失败 attempt 缺少 batch_index",
                )
            # Ordered fallback prefers the next frozen route, but every real
            # provider submission consumes the same run-wide field-attempt
            # allowance.  If no later route is usable, retry the exact current
            # route instead of escaping the configured total bound.
            if routing.get("mode") == "ordered_fallback" and latest.route_index is not None:
                for route in _route_candidates(
                    stage,
                    unit_key=latest.unit_key,
                    after_route_index=int(latest.route_index),
                ):
                    try:
                        _queue_prompt_normalization_attempt_for_route(
                            run,
                            stage,
                            route=route,
                            attempt_number=latest.attempt_number + 1,
                            reason="validation_retry_fallback",
                            frozen_batch_plan=batch_plan,
                            batch_index=batch_index,
                            frozen_prompt_profile_version=latest.prompt_profile_version,
                            frozen_prompt_profile_binding_key=(
                                latest.prompt_profile_binding_key
                            ),
                            frozen_prompt_profile_contract_sha256=(
                                latest.prompt_profile_contract_sha256
                            ),
                            repair_of_attempt=latest,
                            retry_feedback=retry_feedback,
                        )
                        _append_route_event(
                            stage,
                            unit_key=latest.unit_key,
                            route_index=int(route["index"]),
                            event_type="validation_retry_queued",
                            code=latest.error_code,
                        )
                        return None
                    except StageBlocked as exc:
                        if exc.code not in {
                            "frozen_route_unavailable",
                            "prompt_normalize_capability_missing",
                            "prompt_normalize_smoke_drift",
                        }:
                            raise
                        _append_route_event(
                            stage,
                            unit_key=latest.unit_key,
                            route_index=int(route["index"]),
                            event_type="preflight_failed",
                            code=exc.code,
                        )

            current_route = latest.request_snapshot.get("route")
            if not isinstance(current_route, dict):
                raise StageBlocked(
                    "frozen_route_unavailable",
                    "prompt.normalize 校验重试缺少冻结 route",
                )
            _queue_prompt_normalization_attempt_for_route(
                run,
                stage,
                route=current_route,
                attempt_number=latest.attempt_number + 1,
                reason="validation_retry_same_route",
                frozen_batch_plan=batch_plan,
                batch_index=batch_index,
                frozen_prompt_profile_version=latest.prompt_profile_version,
                frozen_prompt_profile_binding_key=latest.prompt_profile_binding_key,
                frozen_prompt_profile_contract_sha256=(
                    latest.prompt_profile_contract_sha256
                ),
                repair_of_attempt=latest,
                retry_feedback=retry_feedback,
            )
            _append_route_event(
                stage,
                unit_key=latest.unit_key,
                route_index=latest.route_index,
                event_type="validation_retry_queued",
                code=latest.error_code,
            )
            return None

        safe_codes = {
            "text_attempt_pre_submit_validation_failed",
            "provider_request_rejected",
            "remote_absence_confirmed",
        }
        if (
            routing.get("mode") == "ordered_fallback"
            and latest.status == GenerationAttempt.Status.FAILED
            and latest.error_code in safe_codes
            and latest.route_index is not None
        ):
            _route_failure_once(stage, latest, latest.error_code)
            batch_plan = _verify_prompt_normalization_batch_plan(
                latest.request_snapshot.get("batch_plan"),
            )
            batch_index = latest.request_snapshot.get("batch_index")
            if type(batch_index) is not int:
                raise StageBlocked(
                    "prompt_normalization_batch_plan_invalid",
                    "prompt.normalize 失败 attempt 缺少 batch_index",
                )
            last_error = None
            for route in _route_candidates(
                stage,
                unit_key=latest.unit_key,
                after_route_index=int(latest.route_index),
            ):
                try:
                    _queue_prompt_normalization_attempt_for_route(
                        run,
                        stage,
                        route=route,
                        attempt_number=latest.attempt_number + 1,
                        reason="ordered_fallback",
                        frozen_batch_plan=batch_plan,
                        batch_index=batch_index,
                    )
                    return None
                except StageBlocked as exc:
                    last_error = exc
                    if exc.code not in {
                        "frozen_route_unavailable",
                        "prompt_normalize_capability_missing",
                        "prompt_normalize_smoke_drift",
                    }:
                        raise
                    _append_route_event(
                        stage,
                        unit_key=latest.unit_key,
                        route_index=int(route["index"]),
                        event_type="preflight_failed",
                        code=exc.code,
                    )
            raise last_error or StageBlocked(
                "routing_candidates_exhausted",
                "ordered_fallback 的 prompt.normalize routes 已耗尽。",
            )
        raise StageBlocked(
            latest.error_code or "prompt_normalization_failed",
            latest.error_message or "prompt.normalize 失败；当前 route 不允许自动切换。",
        )

    frozen_input = _build_prompt_normalization_input(run)
    if not frozen_input["payload"]["items"]:
        _save_stage_snapshot(stage, "output_snapshot", {
            "schema_version": 1,
            "kind": "prompt_normalization_candidates",
            "attempt_id": None,
            "result_sha256": _sha256([]),
            "provider": "",
            "model": "",
            "submission_semantics": "no_submission",
            "candidates": [],
        })
        return "prompt-normalization:no-op"
    route = _route_candidates(stage, unit_key=PROMPT_NORMALIZE_UNIT_KEY)[0]
    _queue_prompt_normalization_attempt_for_route(
        run,
        stage,
        route=route,
        attempt_number=1,
        reason="prompt_normalization_attempt_queued",
        frozen_input=frozen_input,
    )
    return None


def _prepare_manual_prompt_normalization(
    run: ProductionRun,
    stage: StageRun,
) -> str:
    if stage.generation_attempts.exists():
        raise StageBlocked(
            "prompt_normalize_manual_path_invalid",
            "script_normalize/manual 不得包含模型生成尝试。",
        )
    frozen_input = _build_prompt_normalization_input(run)
    if not frozen_input["payload"]["items"]:
        _save_stage_snapshot(stage, "output_snapshot", {
            "schema_version": 1,
            "kind": "prompt_normalization_candidates",
            "attempt_id": None,
            "result_sha256": _sha256([]),
            "provider": "",
            "model": "",
            "submission_semantics": "no_submission",
            "candidates": [],
        })
        return "prompt-normalization:no-op"
    return _materialize_prompt_normalization_manual_review(
        run,
        stage,
        frozen_input=frozen_input,
        route_index=None,
        review_origin="human_only_recipe",
    )


def _queue_text_attempt_for_route(
    run: ProductionRun,
    stage: StageRun,
    *,
    route: dict[str, Any],
    attempt_number: int,
    reason: str,
    retry_contract: dict[str, Any] | None = None,
) -> GenerationAttempt:
    try:
        live_deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).get(pk=route["model_deployment_id"], status=ModelDeployment.Status.ACTIVE)
        deployment = frozen_text_deployment({"route": route})
    except (ModelDeployment.DoesNotExist, ValidationError, KeyError, ValueError) as exc:
        raise StageBlocked("frozen_route_unavailable", "冻结文本 deployment 已不可用") from exc
    if not deployment_supports_script_generation(deployment):
        raise StageBlocked("script_driver_unavailable", "所选 deployment 没有安全文本生成 transport")
    inputs = run.input_snapshot
    if inputs.get("episode_duration_seconds") is None:
        raise StageBlocked("script_input_invalid", "AI 剧本必须显式提供 episode_duration_seconds")
    model_policy = _series_policy(deployment)
    stage_config_values = _stage_config_values(stage)
    try:
        production_policy = video_shot_duration_policy(
            values=stage_config_values,
        )
    except ConfigurationResolutionError as exc:
        raise StageBlocked(
            "script_duration_policy_invalid",
            f"冻结运行的镜头时长合同无效：{exc}",
        ) from exc
    minimum_duration_ms = max(
        int(model_policy["minimum_shot_duration_ms"]),
        int(production_policy["minimum_duration_ms"]),
    )
    maximum_duration_ms = min(
        int(model_policy["maximum_shot_duration_ms"]),
        int(production_policy["maximum_duration_ms"]),
    )
    if minimum_duration_ms > maximum_duration_ms:
        raise StageBlocked(
            "script_duration_policy_invalid",
            "冻结文本模型与 ProductionRun 的镜头时长合同没有共同可执行区间。",
        )
    try:
        plan = build_series_concept_plan(
            idea=str(inputs.get("idea") or ""),
            style_hint=str(inputs.get("style_hint") or ""),
            episode_count=int(inputs.get("episode_count") or 0),
            shots_per_episode=int(inputs.get("shots_per_episode") or 0),
            episode_duration_seconds=int(inputs["episode_duration_seconds"]),
            minimum_shot_duration_ms=minimum_duration_ms,
            maximum_shot_duration_ms=maximum_duration_ms,
            model_identifier=deployment.profile_version.model_identifier,
            timeout_seconds=float(model_policy["timeout_seconds"]),
            chat_request_options=deepcopy(model_policy["chat_request_options"]),
            configured_output_token_hint=int(model_policy["max_output_tokens"]),
            field_contract=deepcopy(model_policy["field_contract"]),
            resource_advisory=deepcopy(
                stage_config_values[
                    "production.series_concept_resource_advisory"
                ]
            ),
            attempt=attempt_number,
            require_all_bible_entities_used=(
                run.input_snapshot.get("interaction_mode")
                == UNATTENDED_INTERACTION_MODE
            ),
        )
    except Exception as exc:
        raise StageBlocked("script_input_invalid", str(exc)) from exc
    try:
        resolved_profile = require_prompt_profile_binding(
            operation=TEXT_OPERATION,
            model_family=model_family_for_profile(deployment.profile_version),
            model_profile_version=live_deployment.profile_version,
        )
        plan["prompt_profile_assembly"] = assemble_prompt_profile(
            resolved_profile.profile_version,
            sources=text_profile_sources(
                project=run.project,
                inputs=inputs,
                operation=TEXT_OPERATION,
            ),
        )
    except ValidationError as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "prompt_profile_invalid"),
            str(exc),
        ) from exc
    snapshot = {"schema_version": 1, "plan": plan, "route": route}
    if retry_contract is not None:
        snapshot["retry_contract"] = deepcopy(retry_contract)
    contract = json_contract(snapshot, max_bytes=_text_snapshot_max_bytes())
    if run.generation_task_count + 1 > _effective_generation_task_limit(run):
        raise StageBlocked("generation_task_budget_exhausted", "AI 剧本调用会超过运行任务上限")
    attempt = _create_generation_attempt(
        stage_run=stage,
        unit_key=TEXT_UNIT_KEY,
        attempt_number=attempt_number,
        model_deployment=live_deployment,
        prompt_profile_version=(
            resolved_profile.profile_version if resolved_profile is not None else None
        ),
        prompt_profile_binding_key=(
            resolved_profile.binding.key if resolved_profile is not None else ""
        ),
        prompt_profile_contract_sha256=(
            resolved_profile.profile_version.contract_sha256
            if resolved_profile is not None else ""
        ),
        route_index=int(route["index"]),
        operation=TEXT_OPERATION,
        is_external=bool(route.get("is_external")),
        status=GenerationAttempt.Status.QUEUED,
        request_sha256=contract.sha256,
        request_snapshot=contract.value,
        request_snapshot_schema_version=contract.schema_version,
        request_snapshot_size_bytes=contract.size_bytes,
        request_snapshot_sha256=contract.sha256,
    )
    run.generation_task_count += 1
    run.save(update_fields=("generation_task_count", "updated_at"))
    _estimate_and_reserve(
        run,
        stage,
        attempt,
        deployment=deployment,
        operation=TEXT_OPERATION,
        payload=text_provider_payload(plan),
        is_external=attempt.is_external,
    )
    _mark_route_selected(
        stage,
        unit_key=TEXT_UNIT_KEY,
        route_index=int(route["index"]),
        reason=reason,
    )
    return attempt


def _prepare_script_generation(run: ProductionRun, stage: StageRun) -> str | None:
    latest = stage.generation_attempts.filter(unit_key=TEXT_UNIT_KEY).order_by("-attempt_number").first()
    if latest is not None:
        if latest.status == GenerationAttempt.Status.SUCCEEDED:
            revision = run.script_revision
            if revision is None or revision.origin_generation_attempt_id != latest.id:
                raise StageBlocked("generated_script_materialization_missing", "成功 attempt 缺少绑定的 preview revision")
            return f"script-revision:{revision.id}"
        if latest.status in {
            GenerationAttempt.Status.QUEUED,
            GenerationAttempt.Status.RUNNING,
            GenerationAttempt.Status.SUBMITTED,
        }:
            if latest.status == GenerationAttempt.Status.QUEUED and latest.committed_cost is None:
                if latest.model_deployment is None:
                    raise StageBlocked("script_route_missing", "文本 attempt 缺少模型部署")
                deployment = frozen_text_deployment(latest.request_snapshot)
                _estimate_and_reserve(
                    run, stage, latest,
                    deployment=deployment,
                    operation=TEXT_OPERATION,
                    payload=text_provider_payload(latest.request_snapshot["plan"]),
                    is_external=latest.is_external,
                )
            return None
        if _queue_text_contract_retry_if_allowed(
            run,
            stage,
            latest=latest,
        ):
            return None
        routing = _verify_stage_routing(stage)
        safe_codes = {
            "text_attempt_pre_submit_validation_failed",
            "series_concept_invalid_response",
            "provider_request_rejected",
            "provider_response_invalid",
            "remote_absence_confirmed",
        }
        if (
            routing.get("mode") == "ordered_fallback"
            and latest.status == GenerationAttempt.Status.FAILED
            and latest.error_code in safe_codes
            and latest.route_index is not None
        ):
            _route_failure_once(stage, latest, latest.error_code)
            candidates = _route_candidates(
                stage,
                unit_key=TEXT_UNIT_KEY,
                after_route_index=int(latest.route_index),
            )
            last_error = None
            for route in candidates:
                try:
                    _queue_text_attempt_for_route(
                        run,
                        stage,
                        route=route,
                        attempt_number=latest.attempt_number + 1,
                        reason="ordered_fallback",
                    )
                    return None
                except StageBlocked as exc:
                    last_error = exc
                    if exc.code not in {
                        "frozen_route_unavailable", "script_driver_unavailable",
                        "script_model_policy_missing", "script_model_policy_invalid",
                    }:
                        raise
                    _append_route_event(
                        stage,
                        unit_key=TEXT_UNIT_KEY,
                        route_index=int(route["index"]),
                        event_type="preflight_failed",
                        code=exc.code,
                    )
            raise last_error or StageBlocked(
                "routing_candidates_exhausted",
                "ordered_fallback 的文本 routes 已耗尽。",
            )
        raise StageBlocked(
            latest.error_code or "script_generation_failed",
            latest.error_message or "文本生成失败；当前 route 不允许自动切换。",
        )

    route = _route_candidates(stage, unit_key=TEXT_UNIT_KEY)[0]
    _queue_text_attempt_for_route(
        run,
        stage,
        route=route,
        attempt_number=1,
        reason="text_attempt_queued",
    )
    return None


def _queue_script_structure_attempt_for_route(
    run: ProductionRun,
    stage: StageRun,
    *,
    route: dict[str, Any],
    attempt_number: int,
    reason: str,
    retry_contract: dict[str, Any] | None = None,
) -> GenerationAttempt:
    try:
        live_deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).get(pk=route["model_deployment_id"], status=ModelDeployment.Status.ACTIVE)
        deployment = frozen_text_deployment({"route": route})
    except (ModelDeployment.DoesNotExist, ValidationError, KeyError, ValueError) as exc:
        raise StageBlocked("frozen_route_unavailable", "冻结结构化 deployment 已不可用") from exc
    if not deployment_supports_script_generation(deployment):
        raise StageBlocked("script_driver_unavailable", "所选 deployment 没有安全文本生成 transport")
    try:
        route = _route_with_revalidated_text_smoke(
            stage,
            route=route,
            deployment=live_deployment,
            operation=SCRIPT_STRUCTURE_OPERATION,
            unit_key=SCRIPT_STRUCTURE_UNIT_KEY,
            invalid_code="script_structure_smoke_invalid",
            missing_code="script_structure_smoke_missing_or_stale",
            drift_code="script_structure_smoke_drift",
        )
        verify_script_structure_smoke(live_deployment, route)
    except ValidationError as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "script_structure_smoke_invalid"),
            str(exc),
        ) from exc
    revision = run.script_revision
    if revision is None or revision.status != ScriptRevision.Status.CONFIRMED:
        raise StageBlocked("script_revision_missing", "script.structure 需要已确认的导入剧本")
    try:
        policy = validate_script_structure_model_profile(deployment.profile_version)
        if policy is None:
            raise ValidationError("冻结模型未声明 script.structure 能力")
        production_duration_policy = video_shot_duration_policy(
            values=_stage_config_values(stage),
        )
        policy = intersect_script_structure_duration_policy(
            policy,
            production_minimum_duration_ms=production_duration_policy["minimum_duration_ms"],
            production_maximum_duration_ms=production_duration_policy["maximum_duration_ms"],
        )
        plan = build_script_structure_plan(
            revision=revision,
            model_identifier=deployment.profile_version.model_identifier,
            policy=policy,
        )
    except Exception as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "script_structure_input_invalid"),
            str(exc),
        ) from exc
    try:
        resolved_profile = require_prompt_profile_binding(
            operation=SCRIPT_STRUCTURE_OPERATION,
            model_family=model_family_for_profile(deployment.profile_version),
            model_profile_version=live_deployment.profile_version,
        )
        plan["prompt_profile_assembly"] = assemble_prompt_profile(
            resolved_profile.profile_version,
            sources=text_profile_sources(
                project=run.project,
                inputs=run.input_snapshot,
                operation=SCRIPT_STRUCTURE_OPERATION,
            ),
        )
    except ValidationError as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "prompt_profile_invalid"),
            str(exc),
        ) from exc
    snapshot = {"schema_version": 1, "plan": plan, "route": route}
    if retry_contract is not None:
        snapshot["retry_contract"] = deepcopy(retry_contract)
    try:
        contract = json_contract(snapshot, max_bytes=_text_snapshot_max_bytes())
        provider_request = structure_provider_payload(plan)
        json_contract(provider_request, max_bytes=_text_provider_request_max_bytes())
    except ValidationError as exc:
        raise StageBlocked(
            "script_structure_input_too_large",
            "完整剧本或装配策略超过受控请求上限；不会截断后提交",
        ) from exc
    if run.generation_task_count + 1 > _effective_generation_task_limit(run):
        raise StageBlocked(
            "generation_task_budget_exhausted",
            "剧本结构化调用会超过运行任务上限",
        )
    attempt = _create_generation_attempt(
        stage_run=stage,
        unit_key=SCRIPT_STRUCTURE_UNIT_KEY,
        attempt_number=attempt_number,
        model_deployment=live_deployment,
        prompt_profile_version=(
            resolved_profile.profile_version if resolved_profile is not None else None
        ),
        prompt_profile_binding_key=(
            resolved_profile.binding.key if resolved_profile is not None else ""
        ),
        prompt_profile_contract_sha256=(
            resolved_profile.profile_version.contract_sha256
            if resolved_profile is not None else ""
        ),
        route_index=int(route["index"]),
        operation=SCRIPT_STRUCTURE_OPERATION,
        is_external=bool(route.get("is_external")),
        status=GenerationAttempt.Status.QUEUED,
        request_sha256=contract.sha256,
        request_snapshot=contract.value,
        request_snapshot_schema_version=contract.schema_version,
        request_snapshot_size_bytes=contract.size_bytes,
        request_snapshot_sha256=contract.sha256,
    )
    run.generation_task_count += 1
    run.save(update_fields=("generation_task_count", "updated_at"))
    _estimate_and_reserve(
        run,
        stage,
        attempt,
        deployment=deployment,
        operation=SCRIPT_STRUCTURE_OPERATION,
        payload=provider_request,
        is_external=attempt.is_external,
    )
    _mark_route_selected(
        stage,
        unit_key=SCRIPT_STRUCTURE_UNIT_KEY,
        route_index=int(route["index"]),
        reason=reason,
    )
    return attempt


def _prepare_script_structure(run: ProductionRun, stage: StageRun) -> str | None:
    latest = stage.generation_attempts.filter(
        unit_key=SCRIPT_STRUCTURE_UNIT_KEY,
    ).order_by("-attempt_number").first()
    if latest is None:
        revision = run.script_revision
        if revision is None or revision.status != ScriptRevision.Status.CONFIRMED:
            raise StageBlocked("script_revision_missing", "script.structure 需要已确认的导入剧本")
        if is_canonical_script(revision.content):
            artifact = _script_artifact(run, stage)
            _save_stage_snapshot(
                stage,
                "output_snapshot",
                canonical_no_submission_review_payload(
                    revision,
                    title=run.project.title,
                    logline=run.project.logline,
                    artifact_id=str(artifact.id),
                ),
            )
            return f"script-revision:{revision.id}"
        routing = _verify_stage_routing(stage)
        candidates = _route_candidates(stage, unit_key=SCRIPT_STRUCTURE_UNIT_KEY)
        last_error = None
        preflight_codes = {
            "frozen_route_unavailable",
            "script_driver_unavailable",
            "script_structure_smoke_invalid",
            "script_structure_smoke_missing_or_stale",
            "script_structure_smoke_drift",
            "script_structure_input_invalid",
            "script_structure_input_too_large",
            "script_structure_duration_policy_incompatible",
            "prompt_profile_invalid",
        }
        for route in candidates:
            try:
                _queue_script_structure_attempt_for_route(
                    run,
                    stage,
                    route=route,
                    attempt_number=1,
                    reason="script_structure_attempt_queued",
                )
                return None
            except StageBlocked as exc:
                last_error = exc
                if routing.get("mode") != "ordered_fallback" or exc.code not in preflight_codes:
                    raise
                _append_route_event(
                    stage,
                    unit_key=SCRIPT_STRUCTURE_UNIT_KEY,
                    route_index=int(route["index"]),
                    event_type="preflight_failed",
                    code=exc.code,
                )
        raise last_error or StageBlocked(
            "routing_candidates_exhausted",
            "ordered_fallback 的 script.structure routes 已耗尽。",
        )
    if latest.status == GenerationAttempt.Status.SUCCEEDED:
        revision = run.script_revision
        if revision is None or revision.origin_generation_attempt_id != latest.id:
            raise StageBlocked(
                "structured_script_materialization_missing",
                "成功的 script.structure attempt 缺少绑定的 preview revision",
            )
        return f"script-revision:{revision.id}"
    if latest.status in {
        GenerationAttempt.Status.QUEUED,
        GenerationAttempt.Status.RUNNING,
        GenerationAttempt.Status.SUBMITTED,
    }:
        if latest.status == GenerationAttempt.Status.QUEUED and latest.committed_cost is None:
            deployment = frozen_text_deployment(latest.request_snapshot)
            _estimate_and_reserve(
                run,
                stage,
                latest,
                deployment=deployment,
                operation=SCRIPT_STRUCTURE_OPERATION,
                payload=structure_provider_payload(latest.request_snapshot["plan"]),
                is_external=latest.is_external,
            )
        return None
    if _queue_text_contract_retry_if_allowed(
        run,
        stage,
        latest=latest,
    ):
        return None
    routing = _verify_stage_routing(stage)
    safe_codes = {
        "text_attempt_pre_submit_validation_failed",
        "script_structure_invalid_response",
        "script_structure_ir_invalid",
        "script_structure_source_mismatch",
        "script_structure_source_coverage_invalid",
        "script_structure_smoke_invalid",
        "script_structure_smoke_missing_or_stale",
        "script_structure_smoke_drift",
        "provider_request_rejected",
        "provider_response_invalid",
        "remote_absence_confirmed",
    }
    if (
        routing.get("mode") == "ordered_fallback"
        and latest.status == GenerationAttempt.Status.FAILED
        and latest.error_code in safe_codes
        and latest.route_index is not None
    ):
        _route_failure_once(stage, latest, latest.error_code)
        candidates = _route_candidates(
            stage,
            unit_key=SCRIPT_STRUCTURE_UNIT_KEY,
            after_route_index=int(latest.route_index),
        )
        last_error = None
        for route in candidates:
            try:
                _queue_script_structure_attempt_for_route(
                    run,
                    stage,
                    route=route,
                    attempt_number=latest.attempt_number + 1,
                    reason="ordered_fallback",
                )
                return None
            except StageBlocked as exc:
                last_error = exc
                if exc.code not in {
                    "frozen_route_unavailable",
                    "script_driver_unavailable",
                    "script_structure_smoke_invalid",
                    "script_structure_smoke_missing_or_stale",
                    "script_structure_smoke_drift",
                    "script_structure_input_invalid",
                    "script_structure_input_too_large",
                    "script_structure_duration_policy_incompatible",
                    "prompt_profile_invalid",
                }:
                    raise
                _append_route_event(
                    stage,
                    unit_key=SCRIPT_STRUCTURE_UNIT_KEY,
                    route_index=int(route["index"]),
                    event_type="preflight_failed",
                    code=exc.code,
                )
        raise last_error or StageBlocked(
            "routing_candidates_exhausted",
            "ordered_fallback 的 script.structure routes 已耗尽。",
        )
    raise StageBlocked(
        latest.error_code or "script_structure_failed",
        latest.error_message or "剧本结构化失败；当前 route 不允许自动切换。",
    )


def _queue_visual_rules_attempt_for_route(
    run: ProductionRun,
    stage: StageRun,
    *,
    route: dict[str, Any],
    attempt_number: int,
    reason: str,
    frozen_plan: dict[str, Any] | None = None,
    retry_contract: dict[str, Any] | None = None,
) -> GenerationAttempt:
    try:
        live_deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).get(pk=route["model_deployment_id"], status=ModelDeployment.Status.ACTIVE)
        deployment = frozen_text_deployment({"route": route})
    except (ModelDeployment.DoesNotExist, ValidationError, KeyError, ValueError) as exc:
        raise StageBlocked("frozen_route_unavailable", "冻结影视规则 deployment 已不可用") from exc
    if not deployment_supports_script_generation(deployment):
        raise StageBlocked("script_driver_unavailable", "所选 deployment 没有安全文本生成 transport")
    try:
        route = _route_with_revalidated_text_smoke(
            stage,
            route=route,
            deployment=live_deployment,
            operation=VISUAL_RULES_OPERATION,
            unit_key=VISUAL_RULES_UNIT_KEY,
            invalid_code="visual_rules_smoke_invalid",
            missing_code="visual_rules_smoke_missing_or_stale",
            drift_code="visual_rules_smoke_drift",
        )
        verify_visual_rules_smoke(live_deployment, route)
    except ValidationError as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "visual_rules_smoke_invalid"),
            str(exc),
        ) from exc
    revision = run.script_revision
    if revision is None or revision.status != ScriptRevision.Status.CONFIRMED:
        raise StageBlocked("script_revision_missing", "全剧影视规则生成需要已确认的正式剧本")
    bible_review = run.stages.filter(
        stage_key="bible_review",
        status=StageRun.Status.SUCCEEDED,
    ).first()
    if bible_review is None:
        raise StageBlocked("visual_rules_bible_unconfirmed", "请先完成 Bible 生成与审核")
    try:
        policy = validate_visual_rules_model_profile(deployment.profile_version)
        if policy is None:
            raise ValidationError("冻结模型未声明 visual_rules.generate 能力")
        project = SeriesProject.objects.select_related("style").get(pk=run.project_id)
        if frozen_plan is None:
            plan = build_visual_rules_plan(
                revision=revision,
                project=project,
                model_identifier=deployment.profile_version.model_identifier,
                policy=policy,
            )
        else:
            plan = deepcopy(frozen_plan)
            source = plan.get("source_revision")
            project_context = plan.get("project_context")
            request = plan.get("request")
            current_context = current_visual_context_snapshot(project)
            if (
                not isinstance(source, dict)
                or not isinstance(project_context, dict)
                or not isinstance(request, dict)
                or source.get("id") != str(revision.id)
                or source.get("content_sha256") != revision.content_sha256
                or project_context.get("current_rules_sha256")
                != current_context["rules_sha256"]
                or project_context.get("confirmed_bible_sha256")
                != current_context["bible_sha256"]
            ):
                raise ValidationError(
                    "同一影视规则阶段的剧本、Bible 或正式规则基线已漂移；禁止 fallback 重建来源",
                    code="visual_rules_frozen_context_drift",
                )
            source_size = len(str(request.get("source_text") or "").encode("utf-8"))
            if source_size > int(policy["max_input_bytes"]):
                raise ValidationError(
                    "冻结完整剧本超过 fallback 模型输入上限；不会截断后提交",
                    code="visual_rules_input_too_large",
                )
            plan["model_identifier"] = deployment.profile_version.model_identifier
            request["max_output_tokens"] = int(policy["max_output_tokens"])
            request["timeout_seconds"] = float(policy["timeout_seconds"])
            request["chat_request_options"] = deepcopy(
                policy["chat_request_options"],
            )
            request.pop("chat_completion_options", None)
            plan["resource_estimate"] = visual_rules_resource_estimate(
                source_size_bytes=source_size,
                policy=policy,
            )
            plan.pop("prompt_profile_assembly", None)
    except Exception as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "visual_rules_input_invalid"),
            str(exc),
        ) from exc
    try:
        resolved_profile = require_prompt_profile_binding(
            operation=VISUAL_RULES_OPERATION,
            model_family=model_family_for_profile(deployment.profile_version),
            model_profile_version=live_deployment.profile_version,
        )
        plan["prompt_profile_assembly"] = assemble_prompt_profile(
            resolved_profile.profile_version,
            sources=text_profile_sources(
                project=project,
                inputs=run.input_snapshot,
                operation=VISUAL_RULES_OPERATION,
            ),
        )
    except ValidationError as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "prompt_profile_invalid"),
            str(exc),
        ) from exc
    snapshot = {"schema_version": 1, "plan": plan, "route": route}
    if retry_contract is not None:
        snapshot["retry_contract"] = deepcopy(retry_contract)
    try:
        contract = json_contract(snapshot, max_bytes=_text_snapshot_max_bytes())
        provider_request = visual_rules_provider_payload(plan)
        json_contract(provider_request, max_bytes=_text_provider_request_max_bytes())
    except ValidationError as exc:
        raise StageBlocked(
            "visual_rules_input_too_large",
            "完整剧本、Bible 或装配策略超过受控请求上限；不会截断后提交",
        ) from exc
    if run.generation_task_count + 1 > _effective_generation_task_limit(run):
        raise StageBlocked("generation_task_budget_exhausted", "影视规则生成会超过运行任务上限")
    attempt = _create_generation_attempt(
        stage_run=stage,
        unit_key=VISUAL_RULES_UNIT_KEY,
        attempt_number=attempt_number,
        model_deployment=live_deployment,
        prompt_profile_version=resolved_profile.profile_version,
        prompt_profile_binding_key=resolved_profile.binding.key,
        prompt_profile_contract_sha256=resolved_profile.profile_version.contract_sha256,
        route_index=int(route["index"]),
        operation=VISUAL_RULES_OPERATION,
        is_external=bool(route.get("is_external")),
        status=GenerationAttempt.Status.QUEUED,
        request_sha256=contract.sha256,
        request_snapshot=contract.value,
        request_snapshot_schema_version=contract.schema_version,
        request_snapshot_size_bytes=contract.size_bytes,
        request_snapshot_sha256=contract.sha256,
    )
    run.generation_task_count += 1
    run.save(update_fields=("generation_task_count", "updated_at"))
    _estimate_and_reserve(
        run,
        stage,
        attempt,
        deployment=deployment,
        operation=VISUAL_RULES_OPERATION,
        payload=provider_request,
        is_external=attempt.is_external,
    )
    _mark_route_selected(
        stage,
        unit_key=VISUAL_RULES_UNIT_KEY,
        route_index=int(route["index"]),
        reason=reason,
    )
    return attempt


def _prepare_visual_rules(run: ProductionRun, stage: StageRun) -> str | None:
    latest = stage.generation_attempts.filter(
        unit_key=VISUAL_RULES_UNIT_KEY,
    ).order_by("-attempt_number").first()
    if latest is None:
        routing = _verify_stage_routing(stage)
        candidates = _route_candidates(stage, unit_key=VISUAL_RULES_UNIT_KEY)
        last_error = None
        preflight_codes = {
            "frozen_route_unavailable", "script_driver_unavailable",
            "visual_rules_smoke_invalid", "visual_rules_smoke_missing_or_stale",
            "visual_rules_smoke_drift", "visual_rules_input_invalid",
            "visual_rules_input_too_large", "visual_rules_bible_unconfirmed",
            "visual_rules_bible_missing", "prompt_profile_invalid",
        }
        for route in candidates:
            try:
                _queue_visual_rules_attempt_for_route(
                    run,
                    stage,
                    route=route,
                    attempt_number=1,
                    reason="visual_rules_attempt_queued",
                )
                return None
            except StageBlocked as exc:
                last_error = exc
                if routing.get("mode") != "ordered_fallback" or exc.code not in preflight_codes:
                    raise
                _append_route_event(
                    stage,
                    unit_key=VISUAL_RULES_UNIT_KEY,
                    route_index=int(route["index"]),
                    event_type="preflight_failed",
                    code=exc.code,
                )
        raise last_error or StageBlocked(
            "routing_candidates_exhausted",
            "ordered_fallback 的 visual_rules.generate routes 已耗尽。",
        )
    if latest.status == GenerationAttempt.Status.SUCCEEDED:
        snapshot = _verify_stage_snapshot(stage, "output_snapshot")
        payload = snapshot.get("review_payload") if snapshot.get("formal_object_type") == "" else None
        if not isinstance(payload, dict):
            payload = snapshot
        if (
            payload.get("kind") != "visual_rules_candidates"
            or payload.get("attempt_id") != str(latest.id)
            or not latest.result_sha256
            or payload.get("result_sha256") != latest.result_sha256
            or latest.operation != VISUAL_RULES_OPERATION
        ):
            raise StageBlocked("visual_rules_materialization_missing", "成功 attempt 缺少冻结影视规则候选")
        return f"visual-rules-candidate:{payload.get('candidate_sha256', '')}"
    if latest.status in {
        GenerationAttempt.Status.QUEUED,
        GenerationAttempt.Status.RUNNING,
        GenerationAttempt.Status.SUBMITTED,
    }:
        if latest.status == GenerationAttempt.Status.QUEUED and latest.committed_cost is None:
            deployment = frozen_text_deployment(latest.request_snapshot)
            _estimate_and_reserve(
                run,
                stage,
                latest,
                deployment=deployment,
                operation=VISUAL_RULES_OPERATION,
                payload=visual_rules_provider_payload(latest.request_snapshot["plan"]),
                is_external=latest.is_external,
            )
        return None
    if _queue_text_contract_retry_if_allowed(
        run,
        stage,
        latest=latest,
    ):
        return None
    routing = _verify_stage_routing(stage)
    safe_codes = {
        "text_attempt_pre_submit_validation_failed", "visual_rules_response_invalid",
        "visual_rules_source_mismatch", "visual_rules_smoke_invalid",
        "visual_rules_smoke_missing_or_stale", "visual_rules_smoke_drift",
        "provider_request_rejected", "provider_response_invalid", "remote_absence_confirmed",
    }
    if (
        routing.get("mode") == "ordered_fallback"
        and latest.status == GenerationAttempt.Status.FAILED
        and latest.error_code in safe_codes
        and latest.route_index is not None
    ):
        _route_failure_once(stage, latest, latest.error_code)
        candidates = _route_candidates(
            stage,
            unit_key=VISUAL_RULES_UNIT_KEY,
            after_route_index=int(latest.route_index),
        )
        last_error = None
        for route in candidates:
            try:
                _queue_visual_rules_attempt_for_route(
                    run,
                    stage,
                    route=route,
                    attempt_number=latest.attempt_number + 1,
                    reason="ordered_fallback",
                    frozen_plan=(
                        latest.request_snapshot.get("plan")
                        if isinstance(latest.request_snapshot, dict) else None
                    ),
                )
                return None
            except StageBlocked as exc:
                last_error = exc
                if exc.code not in {
                    "frozen_route_unavailable", "script_driver_unavailable",
                    "visual_rules_smoke_invalid", "visual_rules_smoke_missing_or_stale",
                    "visual_rules_smoke_drift", "visual_rules_input_invalid",
                    "visual_rules_input_too_large", "visual_rules_bible_unconfirmed",
                    "visual_rules_bible_missing", "prompt_profile_invalid",
                }:
                    raise
                _append_route_event(
                    stage,
                    unit_key=VISUAL_RULES_UNIT_KEY,
                    route_index=int(route["index"]),
                    event_type="preflight_failed",
                    code=exc.code,
                )
        raise last_error or StageBlocked(
            "routing_candidates_exhausted",
            "ordered_fallback 的 visual_rules.generate routes 已耗尽。",
        )
    raise StageBlocked(
        latest.error_code or "visual_rules_failed",
        latest.error_message or "全剧影视规则生成失败；当前 route 不允许自动切换。",
    )


def _execute_bible_materialize(run: ProductionRun, stage: StageRun) -> str:
    revision = run.script_revision
    if revision is None or revision.status != ScriptRevision.Status.CONFIRMED:
        raise StageBlocked("script_revision_missing", "Bible 候选只能从 confirmed ScriptRevision 解析")
    try:
        frozen = freeze_script_bible_candidates(revision.content)
    except Exception as exc:
        raise StageBlocked("script_bible_invalid", str(exc)) from exc
    frozen.update({
        "script_revision_id": str(revision.id),
        "script_content_sha256": revision.content_sha256,
    })
    _save_stage_snapshot(stage, "output_snapshot", frozen)
    return f"script-bible-candidates:{frozen['candidate_sha256']}"


def _approve_generated_script(
    run: ProductionRun,
    stage: StageRun,
    user,
    *,
    adopt_generated_metadata: bool,
) -> None:
    evidence = approve_frozen_script_candidate(
        run=run,
        review_stage=stage,
        user=user,
        adopt_generated_metadata=adopt_generated_metadata,
    )
    _save_stage_snapshot(stage, "output_snapshot", evidence)


def _approve_bible_candidates(run: ProductionRun) -> None:
    materialize = run.stages.select_for_update().filter(
        stage_key="bible_materialize",
        status=StageRun.Status.SUCCEEDED,
    ).first()
    if materialize is None:
        raise ValidationError("缺少已完成的 bible_materialize 阶段")
    snapshot = _verify_stage_snapshot(materialize, "output_snapshot")
    frozen = snapshot.get("review_payload") if snapshot.get("formal_object_type") == "" else None
    if not isinstance(frozen, dict):
        frozen = snapshot
    if frozen.get("kind") != "script_bible_candidates":
        raise ValidationError("bible_materialize 审核快照无效")
    revision = run.script_revision
    if revision is None or revision.content_sha256 != frozen.get("script_content_sha256"):
        raise ValidationError("Bible 候选对应的剧本 revision 已漂移")
    apply_frozen_script_bible(
        project=run.project,
        source_content=revision.content,
        frozen=frozen,
    )


class _StoredAssetFileGuard:
    """Delete newly written Asset files when the surrounding DB commit fails."""

    def __init__(self) -> None:
        self._stored: list[tuple[Any, str]] = []

    def track(self, field_file) -> None:
        name = str(getattr(field_file, "name", "") or "")
        if name:
            self._stored.append((field_file.storage, name))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc_type is not None:
            for storage, name in reversed(self._stored):
                try:
                    storage.delete(name)
                except Exception:
                    logger.exception("Failed to rollback Bible reference Asset file")
        return False


def _locked_bible_entity(run: ProductionRun, entity_type: str, entity_id: str):
    model = {
        "character": CharacterBible,
        "scene": SceneBible,
        "prop": PropBible,
    }.get(entity_type)
    if model is None:
        raise ValidationError("Bible 参考图实体类型无效")
    try:
        return model.objects.select_for_update().prefetch_related(
            "reference_assets",
        ).get(pk=entity_id, project=run.project)
    except model.DoesNotExist as exc:
        raise ValidationError("Bible 参考图实体不存在或不属于当前项目") from exc


def _lock_bible_reference_rows(entity) -> None:
    manager = entity.reference_assets
    list(manager.through.objects.select_for_update().filter(**{
        f"{manager.source_field_name}_id": entity.pk,
    }).values_list("pk", flat=True))


def _assert_bible_asset_eligible_for_entity(
    run: ProductionRun,
    *,
    asset: Asset,
    entity_type: str,
) -> None:
    expected_kind = {
        "character": Asset.Kind.CHARACTER,
        "scene": Asset.Kind.SCENE,
        "prop": Asset.Kind.PROP,
    }[entity_type]
    if asset.owner_id not in (None, run.project.owner_id):
        raise ValidationError(
            f"Bible 素材 {asset.name} 不属于项目所有者或全局素材库",
        )
    if not asset.is_active:
        raise ValidationError(f"Bible 素材 {asset.name} 已停用，不能选择或保留")
    if str((asset.metadata or {}).get("usage") or "") == "project_cover":
        raise ValidationError(f"Bible 素材 {asset.name} 是项目封面，不能用作实体参考图")
    if not str(asset.mime_type or "").casefold().startswith("image/"):
        raise ValidationError(f"Bible 素材 {asset.name} 不是已验证图片")
    if asset.kind == Asset.Kind.REFERENCE:
        category = str((asset.metadata or {}).get("reference_category") or "").casefold()
        if category != expected_kind:
            raise ValidationError(
                f"Bible 通用素材 {asset.name} 未声明 reference_category={expected_kind}",
            )
    elif asset.kind != expected_kind:
        raise ValidationError(
            f"Bible 素材 {asset.name} 类别 {asset.kind} 与 {expected_kind} 不一致",
        )


def _locked_selected_bible_library_assets(
    run: ProductionRun,
    *,
    entity,
    entity_type: str,
    asset_selections: list[dict[str, Any]],
) -> dict[str, Asset]:
    if not asset_selections:
        return {}
    expected_by_id = {
        str(item["asset_id"]): str(item["expected_sha256"])
        for item in asset_selections
    }
    asset_ids = list(expected_by_id)
    assets = {
        str(item.id): item
        for item in Asset.objects.select_for_update().filter(pk__in=asset_ids)
    }
    if set(assets) != set(asset_ids):
        raise ValidationError("所选素材库参考图不存在或已删除")
    for asset in assets.values():
        _assert_bible_asset_eligible_for_entity(
            run,
            asset=asset,
            entity_type=entity_type,
        )
        fact = _bible_binding_asset_fact(
            asset,
            project=run.project,
            entity_type=entity_type,
            entity=entity,
        )
        if fact["checksum"] != expected_by_id[str(asset.id)]:
            raise ProductionRunConflict(
                {"detail": f"素材库图片 {asset.name} 已变化，请刷新后重新核对。"},
                code="bible_library_asset_snapshot_stale",
            )
    return assets


def _locked_preserved_bible_assets(
    run: ProductionRun,
    *,
    entity,
    entity_type: str,
) -> dict[str, Asset]:
    _lock_bible_reference_rows(entity)
    ids = {
        str(item) for item in entity.reference_assets.values_list("id", flat=True)
    }
    if entity.anchor_asset_id:
        ids.add(str(entity.anchor_asset_id))
    if not ids:
        return {}
    assets = {
        str(item.id): item
        for item in Asset.objects.select_for_update().filter(pk__in=ids)
    }
    if set(assets) != ids:
        raise ValidationError("Bible 现有 anchor/reference_assets 包含已删除素材")
    for asset in assets.values():
        _assert_bible_asset_eligible_for_entity(
            run,
            asset=asset,
            entity_type=entity_type,
        )
    return assets


def _partition_current_preserved_bible_assets(
    run: ProductionRun,
    *,
    entity,
    entity_type: str,
    assets: dict[str, Asset],
) -> tuple[dict[str, Asset], list[str]]:
    """Keep only bindings whose formal prompt source still matches the entity.

    Stale generated images remain immutable Assets and historical artifacts, but
    must not silently continue as current semantic references after a Bible or
    style edit.  Integrity/provenance failures remain hard errors.
    """

    current: dict[str, Asset] = {}
    removed_stale: list[str] = []
    for asset_id, asset in sorted(assets.items()):
        try:
            _bible_binding_asset_fact(
                asset,
                project=run.project,
                entity_type=entity_type,
                entity=entity,
            )
        except ProductionRunConflict as exc:
            if exc.code != "bible_image_source_stale":
                raise
            removed_stale.append(asset_id)
        else:
            current[asset_id] = asset
    return current, removed_stale


def _assert_bible_prompt_source_current(
    *,
    prompt: PromptArtifact,
    entity_type: str,
    entity,
    project: SeriesProject | None = None,
) -> dict[str, Any]:
    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    if options.get("generation_purpose") != BIBLE_IMAGE_GENERATION_PURPOSE:
        raise ValidationError("Bible 参考图 PromptArtifact 用途无效")
    target = validate_bible_prompt_target(options.get("target"))
    if target["entity_type"] != entity_type or target["entity_id"] != str(entity.id):
        raise ValidationError("Bible 参考图 Prompt target 与实体不一致")
    source_snapshot = options.get("source_snapshot")
    try:
        verify_json_contract(
            source_snapshot,
            schema_version=int(options.get("source_snapshot_schema_version") or 0),
            size_bytes=int(options.get("source_snapshot_size_bytes") or -1),
            sha256=str(options.get("source_snapshot_sha256") or ""),
            max_bytes=_stage_snapshot_max_bytes(),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise ValidationError("Bible 参考图 Prompt 来源快照缺失或已漂移") from exc
    if target["source_snapshot_sha256"] != options.get("source_snapshot_sha256"):
        raise ValidationError("Bible 参考图 target 与来源快照摘要不一致")
    verify_frozen_profile_reference(
        prompt.prompt_profile_version,
        binding_key=prompt.prompt_profile_binding_key,
        contract_sha256=prompt.prompt_profile_contract_sha256,
    )
    if prompt.prompt_profile_version is None:
        raise ValidationError("Bible 参考图 Prompt 缺少 PromptProfile")
    frozen_profile = source_snapshot.get("prompt_profile") if isinstance(source_snapshot, dict) else None
    if not isinstance(frozen_profile, dict):
        raise ValidationError("Bible 参考图来源快照缺少 PromptProfile")
    frozen_selection = frozen_profile.get("frozen_selection")
    try:
        model_profile = ModelProfileVersion.objects.get(
            pk=options.get("model_profile_version_id"),
        )
        selected_profile = load_frozen_prompt_profile_selection(
            frozen_selection,
            model_profile_version=model_profile,
            operation="image.generate",
            mode="",
            bible_image=True,
        )
    except (ModelProfileVersion.DoesNotExist, ValueError, PromptProfileResolutionError) as exc:
        raise ValidationError("Bible 参考图来源快照的 PromptProfile 冻结选择无效") from exc
    if (
        selected_profile.id != prompt.prompt_profile_version_id
        or frozen_selection.get("prompt_profile_binding_key")
        != prompt.prompt_profile_binding_key
        or frozen_selection.get("prompt_profile_contract_sha256")
        != prompt.prompt_profile_contract_sha256
        or frozen_selection.get("prompt_profile_binding_id")
        != options.get("prompt_profile_binding_id")
    ):
        raise ValidationError("Bible PromptArtifact 与来源快照的 PromptProfile 选择不一致")
    verify_frozen_profile_assembly(
        prompt.prompt_profile_version,
        frozen_profile.get("assembly"),
    )
    effective_project = project or prompt.project
    if prompt.project_id != effective_project.id:
        raise ValidationError("Bible PromptArtifact 与冻结项目不一致")
    available_sources = bible_image_profile_sources(
        project=effective_project,
        entity_type=entity_type,
        entity=entity,
        extra_direction=str(source_snapshot.get("extra_direction") or ""),
    )
    selected_sources = {
        rule.source_selector
        for rule in prompt.prompt_profile_version.field_rules.all()
        if rule.source_selector != "definition.default"
    }
    current_sources = {
        key: value for key, value in available_sources.items()
        if key in selected_sources
    }
    current_assembly = assemble_prompt_profile(
        prompt.prompt_profile_version,
        sources=current_sources,
        projection_context={
            "purpose": "static_reference",
            "target_kind": target["entity_type"],
        },
    )
    current_snapshot = deepcopy(source_snapshot)
    current_snapshot["sources"] = current_sources
    current_snapshot["prompt_profile"] = {
        **frozen_profile,
        "assembly": current_assembly,
    }
    current_sha = json_contract(
        current_snapshot,
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    if current_sha != target["source_snapshot_sha256"]:
        raise ProductionRunConflict(
            {"detail": f"Bible 实体 {entity.name} 的语义来源已变化，请重新生成参考图。"},
            code="bible_image_source_stale",
        )
    structured = prompt.structured_payload if isinstance(prompt.structured_payload, dict) else {}
    frozen_assembly = verify_frozen_profile_assembly(
        prompt.prompt_profile_version,
        structured.get("prompt_profile"),
    )
    if frozen_assembly != current_assembly:
        raise ValidationError("Bible PromptArtifact 装配结果与当前冻结来源不一致")
    if prompt.content_hash != hashlib.sha256(prompt.prompt_text.encode("utf-8")).hexdigest():
        raise ValidationError("Bible PromptArtifact content hash 无效")
    return target


def _assert_bible_candidate_provenance(
    *,
    artifact: GeneratedArtifact,
    attempt: GenerationAttempt,
    prompt: PromptArtifact,
    project: SeriesProject,
) -> None:
    """Re-verify the complete immutable execution chain before human approval."""

    origin_stage = attempt.stage_run
    origin_run = origin_stage.production_run
    metadata = artifact.manifest_metadata if isinstance(
        artifact.manifest_metadata, dict,
    ) else {}
    request = attempt.request_snapshot if isinstance(
        attempt.request_snapshot, dict,
    ) else {}
    try:
        verify_json_contract(
            request,
            schema_version=attempt.request_snapshot_schema_version,
            size_bytes=attempt.request_snapshot_size_bytes,
            sha256=attempt.request_snapshot_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise ValidationError("Bible 候选 attempt request contract 无效") from exc
    if attempt.request_sha256 != attempt.request_snapshot_sha256:
        raise ValidationError("Bible 候选 attempt request identity 已漂移")
    if (
        artifact.production_run_id != origin_run.id
        or artifact.stage_run_id != origin_stage.id
        or origin_run.project_id != project.id
        or origin_stage.stage_key != BIBLE_IMAGE_GENERATION_STAGE_KEY
        or attempt.model_deployment_id is None
    ):
        raise ValidationError("Bible 候选 artifact/attempt/stage/run 归属链不一致")
    if any(str(metadata.get(key) or "") != expected for key, expected in {
        "generation_attempt_id": str(attempt.id),
        "production_run_id": str(origin_run.id),
        "stage_run_id": str(origin_stage.id),
        "prompt_artifact_id": str(prompt.id),
    }.items()):
        raise ValidationError("Bible 候选 manifest execution identity 不一致")
    if str(metadata.get("request_sha256") or "") != attempt.request_sha256:
        raise ValidationError("Bible 候选 manifest/request hash 不一致")
    if (
        str(request.get("prompt_artifact_id") or "") != str(prompt.id)
        or str(request.get("prompt_content_hash") or "") != prompt.content_hash
        or str(metadata.get("prompt_content_hash") or "") != prompt.content_hash
        or str(metadata.get("prompt_profile_version_id") or "")
        != str(prompt.prompt_profile_version_id or "")
        or str(metadata.get("prompt_profile_binding_key") or "")
        != prompt.prompt_profile_binding_key
        or str(metadata.get("prompt_profile_binding_id") or "")
        != str(
            (prompt.request_options or {}).get("prompt_profile_binding_id")
            if isinstance(prompt.request_options, dict) else ""
        )
        or str(metadata.get("prompt_profile_contract_sha256") or "")
        != prompt.prompt_profile_contract_sha256
        or attempt.prompt_profile_version_id != prompt.prompt_profile_version_id
        or attempt.prompt_profile_binding_key != prompt.prompt_profile_binding_key
        or attempt.prompt_profile_contract_sha256 != prompt.prompt_profile_contract_sha256
    ):
        raise ValidationError("Bible 候选 Prompt/PromptProfile provenance 不一致")
    target = validate_bible_prompt_target(metadata.get("target"))
    request_target = validate_bible_prompt_target(request.get("target"))
    if target != request_target:
        raise ValidationError("Bible 候选 request/manifest target 不一致")

    deployment = attempt.model_deployment
    driver_type = deployment.profile_version.provider.driver_type
    if driver_type == ProviderProfile.DriverType.COMFYUI:
        task = artifact.generation_task
        if (
            task is None
            or attempt.generation_task_id != task.id
            or artifact.generation_task_id != task.id
            or task.status != GenerationTask.Status.SUCCEEDED
            or task.owner_id != project.owner_id
            or task.project_id != project.id
            or task.generated_workflow.prompt_artifact_id != prompt.id
        ):
            raise ValidationError("Bible Comfy 候选 task/attempt/artifact 归属链不一致")
        capability = task.capability_snapshot if isinstance(
            task.capability_snapshot, dict,
        ) else {}
        expected_capability = {
            "production_run_id": str(origin_run.id),
            "stage_run_id": str(origin_stage.id),
            "generation_attempt_id": str(attempt.id),
            "prompt_artifact_id": str(prompt.id),
            "model_deployment_id": str(attempt.model_deployment_id),
            "model_profile_version_id": str(attempt.model_deployment.profile_version_id),
        }
        if any(capability.get(key) != value for key, value in expected_capability.items()):
            raise ValidationError("Bible Comfy task capability execution identity 已漂移")
        if str(metadata.get("task_id") or "") != str(task.id):
            raise ValidationError("Bible Comfy manifest task identity 不一致")
        output_index = metadata.get("output_index")
        if isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
            raise ValidationError("Bible Comfy manifest output_index 无效")
        try:
            document = verified_task_manifest(task, output_index)
        except (ProvenanceError, OSError, ValueError) as exc:
            raise ValidationError("Bible Comfy 候选签名来源清单无效") from exc
        subject = document.get("subject") if isinstance(document, dict) else {}
        generation = document.get("generation") if isinstance(document, dict) else {}
        media = document.get("artifact") if isinstance(document, dict) else {}
        integrity = document.get("integrity") if isinstance(document, dict) else {}
        if (
            not isinstance(subject, dict)
            or subject.get("id") != str(task.id)
            or not isinstance(generation, dict)
            or generation.get("task_id") != str(task.id)
            or generation.get("prompt_artifact_id") != str(prompt.id)
            or not isinstance(media, dict)
            or media.get("media_kind") != artifact.kind
            or media.get("output_index") != output_index
            or media.get("sha256") != artifact.sha256
            or media.get("byte_size") != artifact.size_bytes
            or generation.get("project_id") != str(project.id)
            or not isinstance(integrity, dict)
            or integrity.get("payload_sha256")
            != str(metadata.get("provenance_manifest_sha256") or "")
        ):
            raise ValidationError("Bible Comfy 候选 signed manifest 与 artifact 不一致")
        return

    if driver_type != ProviderProfile.DriverType.ASYNC_REST:
        raise ValidationError("Bible 候选使用了未注册的媒体 driver")
    if attempt.generation_task_id is not None or artifact.generation_task_id is not None:
        raise ValidationError("Bible async 候选不得伪造 Comfy GenerationTask")
    if (
        request.get("transport") != "restricted_async_rest"
        or attempt.operation != "image.text_to_image"
        or metadata.get("source") != "restricted_async_rest"
        or str(metadata.get("tenant_owner_id") or "") != str(project.owner_id)
    ):
        raise ValidationError("Bible async 候选 transport/tenant provenance 不一致")


def _copy_bible_candidate_to_asset(
    *,
    run: ProductionRun,
    review_stage: StageRun,
    artifact: GeneratedArtifact,
    entity_type: str,
    entity,
    binding_role: str,
    asset_guard: _StoredAssetFileGuard,
) -> Asset:
    authored_fields = {
        "名称": str(entity.name or "").strip(),
        "基础描述": str(entity.description or "").strip(),
        "视觉锚点": str(entity.visual_identity or "").strip(),
    }
    missing_fields = [label for label, value in authored_fields.items() if not value]
    if missing_fields:
        raise ValidationError(
            "Bible 参考图入库前必须补全源实体的"
            f"{'、'.join(missing_fields)}；不能用来源说明或空值代替素材信息。"
        )
    try:
        facts = _managed_generated_artifact_facts(artifact)
    except ReleaseBundleError as exc:
        raise ValidationError("Bible 参考图候选真实字节或 manifest 无效") from exc
    root = Path(settings.MEDIA_ROOT).resolve()
    source = (root / artifact.object_key).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValidationError("Bible 参考图候选文件不存在或路径越界")
    suffix = source.suffix.lower()[:16]
    asset = Asset(
        owner=run.project.owner,
        style=run.project.style,
        kind=entity_type,
        name=authored_fields["名称"],
        description=authored_fields["基础描述"],
        prompt_hint=authored_fields["视觉锚点"],
        source_info=(
            f"ProductionRun {run.id} 审核绑定的 Bible 参考图"
            f" · 候选 {artifact.variant_index}"
        ),
        mime_type=facts["mime_type"],
        checksum=facts["sha256"],
        metadata={
            "schema_version": 1,
            "generation_purpose": BIBLE_IMAGE_GENERATION_PURPOSE,
            "production_run_id": str(run.id),
            "review_stage_id": str(review_stage.id),
            "generated_artifact_id": str(artifact.id),
            "generation_attempt_id": (
                str(artifact.generation_attempt_id)
                if artifact.generation_attempt_id else None
            ),
            "generation_task_id": (
                str(artifact.generation_task_id) if artifact.generation_task_id else None
            ),
            "entity_type": entity_type,
            "entity_id": str(entity.id),
            "binding_role": binding_role,
            "source_sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "mime_type": artifact.mime_type,
            "source_manifest_sha256": artifact.manifest_metadata_sha256,
        },
    )
    try:
        with source.open("rb") as handle:
            asset.file.save(
                f"{artifact.id}{suffix or '.media'}",
                File(handle),
                save=False,
            )
        asset_guard.track(asset.file)
        asset.save()
        destination = Path(asset.file.path).resolve()
        copied = verified_local_media_facts(
            destination,
            expected_kind="image",
            max_bytes=int(artifact.size_bytes or 0),
        )
    except Exception:
        asset_guard.track(asset.file)
        raise
    if (
        copied["sha256"] != artifact.sha256
        or copied["size_bytes"] != artifact.size_bytes
        or copied["mime_type"] != artifact.mime_type
    ):
        raise ValidationError("Bible 参考图复制为 Asset 后二次校验失败")
    return asset


def _approve_bible_image_bindings(
    run: ProductionRun,
    review_stage: StageRun,
    user,
    *,
    bindings: list[dict[str, Any]],
    expected_source_snapshot_sha256: str,
    asset_guard: _StoredAssetFileGuard,
) -> None:
    locked_project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    if locked_project.style_id:
        locked_project.style = Style.objects.select_for_update().get(
            pk=locked_project.style_id,
        )
    list(PromptFieldTranslation.objects.select_for_update().filter(
        project=locked_project,
    ).values_list("id", flat=True))
    run.project = locked_project
    generation_stage = run.stages.select_for_update().filter(
        stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
        status=StageRun.Status.SUCCEEDED,
    ).first()
    if generation_stage is None:
        raise ValidationError("缺少已完成的 bible_image_generation 阶段")
    generation_snapshot = _verify_stage_snapshot(generation_stage, "output_snapshot")
    public_sha = json_contract(
        redact_sensitive_bindings(generation_snapshot),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    if expected_source_snapshot_sha256 != public_sha:
        raise ProductionRunConflict(
            {"detail": "页面展示的 Bible 参考图候选快照已过期，请刷新后重新核对。"},
            code="bible_image_snapshot_stale",
        )
    canonical = generation_snapshot.get("canonical")
    if (
        generation_snapshot.get("formal_object_type") != "bible_image_candidate_manifest"
        or not isinstance(canonical, dict)
        or canonical.get("generation_stage_id") != str(generation_stage.id)
    ):
        raise ValidationError("Bible 参考图生成快照无效")
    current_canonical = _canonical_bible_image_candidates(run, generation_stage)
    if _sha256(current_canonical) != generation_snapshot.get("canonical_sha256"):
        raise ProductionRunConflict(
            {"detail": "Bible 参考图候选清单已变化，请刷新后重新核对。"},
            code="bible_image_candidate_manifest_stale",
        )
    expected_entities = {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
        for item in canonical.get("entities", [])
        if isinstance(item, dict)
    }
    provided_entities = {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
        for item in bindings
        if isinstance(item, dict)
    }
    if (
        len(provided_entities) != len(bindings)
        or provided_entities != expected_entities
        or expected_entities != {
            (entity_type, str(entity.id))
            for entity_type, entity in _bible_entities(run.project)
        }
    ):
        raise ValidationError("Bible 参考图审核必须且只能逐实体提交完整选择")
    allowed_by_entity: dict[tuple[str, str], set[str]] = {}
    for item in canonical.get("entities", []):
        key = (str(item["entity_type"]), str(item["entity_id"]))
        allowed_by_entity[key] = {
            str(candidate.get("artifact_id") or "")
            for candidate in item.get("candidates", [])
            if isinstance(candidate, dict)
        }
    locked_artifacts = {
        str(item.id): item
        for item in GeneratedArtifact.objects.select_for_update().select_related(
            "generation_attempt__prompt_profile_version",
            "generation_task__generated_workflow__prompt_artifact",
            "asset",
        ).filter(
            pk__in={
                artifact_id
                for artifact_ids in allowed_by_entity.values()
                for artifact_id in artifact_ids
            },
            production_run__project=run.project,
            stage_run__stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
            kind=GeneratedArtifact.Kind.IMAGE,
            role="bible_reference_candidate",
        )
    }
    result_rows: list[dict[str, Any]] = []
    claimed_final_asset_ids: set[str] = set()
    for selection in sorted(bindings, key=lambda item: (item["entity_type"], str(item["entity_id"]))):
        entity_type = str(selection["entity_type"])
        entity_id = str(selection["entity_id"])
        selected_ids = [str(item) for item in (selection.get("selected_artifact_ids") or [])]
        selected_library_rows = list(selection.get("selected_library_assets") or [])
        selected_library_ids = [str(item["asset_id"]) for item in selected_library_rows]
        anchor_artifact_id = str(selection.get("anchor_artifact_id") or "")
        anchor_library_asset_id = str(
            selection.get("anchor_library_asset_id") or ""
        )
        if (
            not (selected_ids or selected_library_ids)
            or len(selected_ids) != len(set(selected_ids))
            or len(selected_library_ids) != len(set(selected_library_ids))
            or len(selected_ids) + len(selected_library_ids)
            > _bible_reference_asset_max_items()
            or bool(anchor_artifact_id) == bool(anchor_library_asset_id)
            or (anchor_artifact_id and anchor_artifact_id not in selected_ids)
            or (
                anchor_library_asset_id
                and anchor_library_asset_id not in selected_library_ids
            )
            or set(selected_ids) - allowed_by_entity[(entity_type, entity_id)]
        ):
            raise ValidationError(
                "每个 Bible 实体必须选择 1..N 个模型候选和/或素材库图片，"
                "并从所选来源中指定唯一 anchor",
            )
        entity = _locked_bible_entity(run, entity_type, entity_id)
        selected_library_assets = _locked_selected_bible_library_assets(
            run,
            entity=entity,
            entity_type=entity_type,
            asset_selections=selected_library_rows,
        )
        preserved_assets = _locked_preserved_bible_assets(
            run,
            entity=entity,
            entity_type=entity_type,
        )
        preserved_assets, removed_stale_asset_ids = (
            _partition_current_preserved_bible_assets(
                run,
                entity=entity,
                entity_type=entity_type,
                assets=preserved_assets,
            )
        )
        created_assets: dict[str, Asset] = {}
        for artifact_id in selected_ids:
            artifact = locked_artifacts.get(artifact_id)
            if artifact is None:
                raise ValidationError("所选 Bible 参考图 artifact 不属于冻结生成阶段")
            identity = parse_bible_candidate_unit_key(
                artifact.generation_attempt.unit_key if artifact.generation_attempt else "",
            )
            if identity["entity_type"] != entity_type or identity["entity_id"] != entity_id:
                raise ValidationError("Bible 参考图 artifact 与实体目标不一致")
            if artifact.variant_group != identity["variant_group"]:
                raise ValidationError("Bible 参考图 artifact variant_group 无效")
            if artifact.status not in {
                GeneratedArtifact.Status.AVAILABLE,
                GeneratedArtifact.Status.SELECTED,
            }:
                raise ValidationError("所选 Bible 参考图 artifact 当前不可用")
            attempt = artifact.generation_attempt
            if attempt is None or attempt.status != GenerationAttempt.Status.SUCCEEDED:
                raise ValidationError("所选 Bible 参考图没有成功 GenerationAttempt")
            verify_json_contract(
                attempt.request_snapshot,
                schema_version=attempt.request_snapshot_schema_version,
                size_bytes=attempt.request_snapshot_size_bytes,
                sha256=attempt.request_snapshot_sha256,
                max_bytes=_stage_snapshot_max_bytes(),
            )
            if attempt.request_sha256 != attempt.request_snapshot_sha256:
                raise ValidationError("Bible 参考图 attempt 请求 identity 已漂移")
            metadata = artifact.manifest_metadata if isinstance(artifact.manifest_metadata, dict) else {}
            verify_json_contract(
                metadata,
                schema_version=artifact.manifest_schema_version,
                size_bytes=artifact.manifest_metadata_size_bytes,
                sha256=artifact.manifest_metadata_sha256,
                max_bytes=_stage_snapshot_max_bytes(),
            )
            prompt = None
            if artifact.generation_task_id:
                prompt = getattr(
                    getattr(artifact.generation_task, "generated_workflow", None),
                    "prompt_artifact", None,
                )
            if prompt is None:
                prompt = PromptArtifact.objects.select_related(
                    "prompt_profile_version",
                ).filter(pk=metadata.get("prompt_artifact_id")).first()
            if prompt is None or str(prompt.id) != str(attempt.request_snapshot.get("prompt_artifact_id") or ""):
                raise ValidationError("Bible 参考图 attempt/manifest 缺少同一 PromptArtifact")
            _assert_bible_candidate_provenance(
                artifact=artifact,
                attempt=attempt,
                prompt=prompt,
                project=locked_project,
            )
            _assert_bible_prompt_source_current(
                prompt=prompt,
                entity_type=entity_type,
                entity=entity,
                project=locked_project,
            )
            if artifact.asset_id:
                asset = Asset.objects.select_for_update().get(pk=artifact.asset_id)
                fact = _bible_binding_asset_fact(
                    asset,
                    project=run.project,
                    entity_type=entity_type,
                    entity=entity,
                )
                if fact["generated_artifact_id"] != str(artifact.id):
                    raise ValidationError(
                        "历史 Bible 参考图 Asset 与 GeneratedArtifact provenance 不一致",
                    )
            else:
                asset = _copy_bible_candidate_to_asset(
                    run=run,
                    review_stage=review_stage,
                    artifact=artifact,
                    entity_type=entity_type,
                    entity=entity,
                    binding_role=("anchor" if artifact_id == anchor_artifact_id else "reference"),
                    asset_guard=asset_guard,
                )
                artifact.asset = asset
            artifact.status = GeneratedArtifact.Status.SELECTED
            artifact.save(update_fields=("asset", "status", "updated_at"))
            selection_group = f"bible-candidate:{artifact.id}"
            revision = (
                ArtifactSelectionDecision.objects.filter(
                    stage_run=review_stage,
                    variant_group=selection_group,
                ).aggregate(value=Max("revision"))["value"] or 0
            ) + 1
            ArtifactSelectionDecision.objects.create(
                stage_run=review_stage,
                variant_group=selection_group,
                revision=revision,
                artifact=artifact,
                selected_by=user,
            )
            created_assets[artifact_id] = asset
        generated_asset_ids = {str(asset.id) for asset in created_assets.values()}
        if generated_asset_ids.intersection(selected_library_assets):
            raise ValidationError(
                "同一参考图不能同时作为模型候选和素材库图片重复提交",
            )
        old_anchor_id = str(entity.anchor_asset_id) if entity.anchor_asset_id else ""
        new_anchor = (
            created_assets[anchor_artifact_id]
            if anchor_artifact_id
            else selected_library_assets[anchor_library_asset_id]
        )
        selected_resolved_assets = [
            *[created_assets[item] for item in selected_ids],
            *[selected_library_assets[item] for item in selected_library_ids],
        ]
        selected_resolved_ids = {str(item.id) for item in selected_resolved_assets}
        if claimed_final_asset_ids.intersection(selected_resolved_ids):
            raise ValidationError(
                "同一最终 Asset 不能绑定到多个 Bible 实体",
            )
        claimed_final_asset_ids.update(selected_resolved_ids)
        reference_ids = {
            str(item) for item in preserved_assets
            if str(item) != old_anchor_id
        }
        if (
            old_anchor_id
            and old_anchor_id in preserved_assets
            and old_anchor_id != str(new_anchor.id)
        ):
            reference_ids.add(old_anchor_id)
        reference_ids.update(
            str(asset.id)
            for asset in selected_resolved_assets
            if str(asset.id) != str(new_anchor.id)
        )
        reference_ids.discard(str(new_anchor.id))
        entity.anchor_asset = new_anchor
        entity.save(update_fields=("anchor_asset", "updated_at"))
        binding_assets = {
            **preserved_assets,
            **{str(asset.id): asset for asset in created_assets.values()},
            **selected_library_assets,
        }
        entity.reference_assets.set([
            binding_assets[item]
            for item in sorted(reference_ids)
        ])
        actual_reference_ids = sorted(
            str(item) for item in entity.reference_assets.values_list("id", flat=True)
        )
        if actual_reference_ids != sorted(reference_ids):
            raise ValidationError("Bible reference_assets 提交事实与审核结果不一致")
        actual_reference_assets = list(
            entity.reference_assets.all().order_by("id")
        )
        result_rows.append({
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity.name,
            "selected_artifact_ids": selected_ids,
            "selected_library_asset_ids": selected_library_ids,
            "selected_library_assets": [
                {
                    "asset_id": str(item["asset_id"]),
                    "expected_sha256": str(item["expected_sha256"]),
                }
                for item in selected_library_rows
            ],
            "anchor_artifact_id": anchor_artifact_id or None,
            "anchor_library_asset_id": anchor_library_asset_id or None,
            "selected_asset_ids": [str(item.id) for item in selected_resolved_assets],
            "anchor_asset_id": str(new_anchor.id),
            "selection_sources": [
                *[
                    {
                        "source_type": "generated_artifact",
                        "source_id": artifact_id,
                        "asset_id": str(created_assets[artifact_id].id),
                    }
                    for artifact_id in selected_ids
                ],
                *[
                    {
                        "source_type": "asset_library",
                        "source_id": asset_id,
                        "asset_id": asset_id,
                    }
                    for asset_id in selected_library_ids
                ],
            ],
            "reference_asset_ids": actual_reference_ids,
            "removed_stale_asset_ids": removed_stale_asset_ids,
            "anchor_asset": _bible_binding_asset_fact(
                new_anchor,
                project=run.project,
                entity_type=entity_type,
                entity=entity,
            ),
            "reference_assets": [
                _bible_binding_asset_fact(
                    asset,
                    project=run.project,
                    entity_type=entity_type,
                    entity=entity,
                )
                for asset in actual_reference_assets
            ],
        })
    sync_consistency_locks(run.project)
    _save_stage_snapshot(review_stage, "output_snapshot", {
        "schema_version": 1,
        "kind": "bible_image_bindings",
        "generation_stage_id": str(generation_stage.id),
        "generation_snapshot_sha256": generation_stage.output_snapshot_sha256,
        "public_generation_snapshot_sha256": public_sha,
        "bindings": result_rows,
    })


def _validated_prompt_normalization_candidate_overrides(
    candidate_overrides: dict[str, str] | None,
) -> dict[str, str]:
    if candidate_overrides is None:
        return {}
    if not isinstance(candidate_overrides, dict):
        raise ValidationError({"candidate_overrides": "必须是对象"})
    maximum_items = normalization_batch_max_items()
    maximum_characters = normalization_text_max_characters()
    if len(candidate_overrides) > maximum_items:
        raise ValidationError({
            "candidate_overrides": f"最多包含 {maximum_items} 项",
        })
    normalized: dict[str, str] = {}
    for request_id, text in candidate_overrides.items():
        if not isinstance(request_id, str) or not request_id or len(request_id) > 191:
            raise ValidationError({"candidate_overrides": "request_id 无效"})
        if not isinstance(text, str) or not text or len(text) > maximum_characters:
            raise ValidationError({
                "candidate_overrides": (
                    f"文本必须为非空字符串且不超过 {maximum_characters} 个字符"
                ),
            })
        normalized[request_id] = text
    return normalized


def _approve_prompt_normalization(
    run: ProductionRun,
    user,
    *,
    candidate_overrides: dict[str, str] | None = None,
    expected_source_snapshot_sha256: str,
) -> None:
    normalize_stage = run.stages.select_for_update().filter(
        stage_key="script_normalize",
        status=StageRun.Status.SUCCEEDED,
    ).first()
    if normalize_stage is None:
        raise ValidationError("缺少已完成的 script_normalize 阶段")
    snapshot = _verify_stage_snapshot(normalize_stage, "output_snapshot")
    if expected_source_snapshot_sha256 != json_contract(
        redact_sensitive_bindings(snapshot),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256:
        raise ProductionRunConflict(
            {"detail": "页面展示的提示词规范化候选快照已过期，请刷新后重新核对。"},
            code="prompt_normalization_snapshot_stale",
        )
    payload = snapshot.get("review_payload")
    if not isinstance(payload, dict) or payload.get("kind") != "prompt_normalization_candidates":
        raise ValidationError("script_normalize 审批候选快照无效")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValidationError("script_normalize candidates 无效")
    overrides = _validated_prompt_normalization_candidate_overrides(
        candidate_overrides,
    )
    request_ids = {
        str(candidate.get("request_id") or "")
        for candidate in candidates
        if isinstance(candidate, dict)
    }
    if "" in request_ids or len(request_ids) != len(candidates):
        raise ValidationError("script_normalize candidate request_id 缺失或重复")
    unknown = sorted(set(overrides) - request_ids)
    if unknown:
        raise ValidationError({
            "candidate_overrides": (
                "包含不属于冻结候选的 request_id: " + ", ".join(unknown)
            ),
        })
    missing = sorted(request_ids - set(overrides))
    if missing:
        raise ValidationError({
            "candidate_overrides": (
                "必须逐项审核并提交全部冻结候选 request_id: " + ", ".join(missing)
            ),
        })
    reviewed_candidates = []
    for candidate in candidates:
        reviewed = dict(candidate)
        request_id = str(reviewed["request_id"])
        if request_id in overrides:
            reviewed["translated_text"] = overrides[request_id]
        reviewed_candidates.append(reviewed)
    attempt_id = payload.get("attempt_id")
    manual_review_origin = str(payload.get("review_origin") or "")
    manual_validation_fallback = (
        manual_review_origin == "human_after_validation_retry_exhausted"
    )
    batched_submission = isinstance(payload.get("attempt_ids"), list)
    manual_no_submission = (
        attempt_id is None
        and manual_review_origin == "human_only_recipe"
        and payload.get("submission_semantics") == "human_only_no_submission"
    )
    frozen_input: dict[str, Any] | None = None
    attempts_by_request_id: dict[str, GenerationAttempt] | None = None
    if attempt_id is None:
        if not candidates:
            return
        if not manual_no_submission:
            raise ValidationError("无提交的 script_normalize 快照不能包含候选")
        if str(payload.get("provider") or "") or str(payload.get("model") or ""):
            raise ValidationError("人工无提交候选不能声明 Provider 或模型")
        frozen_input = payload.get("frozen_input")
        if not isinstance(frozen_input, dict):
            raise ValidationError("script_normalize 人工无提交缺少冻结合同")
        try:
            verify_json_contract(
                frozen_input,
                schema_version=payload.get("frozen_input_schema_version"),
                size_bytes=payload.get("frozen_input_size_bytes"),
                sha256=payload.get("frozen_input_sha256"),
                max_bytes=_text_snapshot_max_bytes(),
            )
        except ValidationError as exc:
            raise ValidationError("script_normalize 人工无提交冻结合同无效") from exc
        evidence = {
            "schema_version": 1,
            "review_origin": manual_review_origin,
            "frozen_input_sha256": str(payload.get("frozen_input_sha256") or ""),
            "candidates": candidates,
        }
        if payload.get("result_sha256") != _sha256(evidence):
            raise ValidationError("script_normalize 人工无提交证据 checksum 无效")
        attempt = None
    elif batched_submission:
        try:
            attempt, attempts_by_request_id, frozen_input = (
                _verified_prompt_normalization_review_batches(
                    normalize_stage,
                    payload=payload,
                    candidates=candidates,
                    manual_validation_fallback=manual_validation_fallback,
                )
            )
        except (StageBlocked, ValidationError) as exc:
            raise ValidationError("script_normalize 批次审核证据无效") from exc
    else:
        attempt = GenerationAttempt.objects.select_for_update().filter(
            pk=attempt_id,
            stage_run=normalize_stage,
            operation=PROMPT_NORMALIZE_OPERATION,
        ).first()
    if attempt is None and not manual_no_submission:
        raise ValidationError("script_normalize attempt 缺失")
    try:
        if batched_submission:
            frozen_request_payload = frozen_input.get("payload")
        elif attempt is not None:
            verify_json_contract(
                attempt.request_snapshot,
                schema_version=attempt.request_snapshot_schema_version,
                size_bytes=attempt.request_snapshot_size_bytes,
                sha256=attempt.request_snapshot_sha256,
                max_bytes=_text_snapshot_max_bytes(),
            )
            frozen_request_payload = attempt.request_snapshot.get("payload")
        else:
            frozen_request_payload = frozen_input.get("payload")
        if not isinstance(frozen_request_payload, dict):
            raise PromptNormalizationContractError("prompt.normalize 冻结请求缺少 payload")
        for candidate in reviewed_candidates:
            validate_target_contribution_text(
                frozen_request_payload,
                request_id=str(candidate.get("request_id") or ""),
                director_text=str(candidate.get("translated_text") or ""),
            )
    except (ValidationError, PromptNormalizationContractError) as exc:
        raise ValidationError(
            "script_normalize 人工候选不满足冻结 PromptProfile 字段生成合同。",
        ) from exc
    if manual_no_submission:
        pass
    elif manual_validation_fallback:
        retry_policy = payload.get("retry_policy")
        failures = payload.get("validation_failures")
        stop_reason = str(payload.get("stop_reason") or "")
        frozen_policy = _prompt_field_retry_policy(
            normalize_stage,
            route_index=attempt.route_index,
        )
        try:
            actual_failures = _prompt_normalization_validation_failures(
                normalize_stage,
                unit_key=attempt.unit_key,
            )
        except StageBlocked as exc:
            raise ValidationError("script_normalize 人工接管审计证据无效") from exc
        retry_limit_reached = len(actual_failures) >= frozen_policy["max_attempts"]
        safe_feedback_unavailable = False
        if (
            stop_reason == PROMPT_VALIDATION_STOP_SAFE_FEEDBACK_UNAVAILABLE
            and actual_failures
            and not retry_limit_reached
        ):
            try:
                safe_feedback_unavailable = (
                    _prompt_normalization_retry_feedback(
                        attempt,
                        actual_failures[-1],
                    )
                    is None
                )
            except StageBlocked as exc:
                raise ValidationError(
                    "script_normalize 人工接管审计证据无效",
                ) from exc
        evidence = {
            "schema_version": 1,
            "review_origin": "human_after_validation_retry_exhausted",
            "stop_reason": stop_reason,
            **(
                {"batch_plan_sha256": str(payload.get("batch_plan_sha256") or "")}
                if batched_submission else {}
            ),
            "latest_attempt_id": str(attempt.id),
            "retry_policy": retry_policy,
            "validation_failures": failures,
            "candidates": candidates,
        }
        if (
            attempt.status != GenerationAttempt.Status.FAILED
            or attempt.error_code not in PROMPT_VALIDATION_RETRYABLE_CODES
            or not isinstance(retry_policy, dict)
            or retry_policy.get("config_key") != PROMPT_FIELD_RETRY_LIMIT_CONFIG_KEY
            or retry_policy.get("retry_limit") != frozen_policy["retry_limit"]
            or retry_policy.get("max_attempts") != frozen_policy["max_attempts"]
            or retry_policy.get("attempts_used") != len(actual_failures)
            or stop_reason not in PROMPT_VALIDATION_STOP_REASONS
            or (
                stop_reason == PROMPT_VALIDATION_STOP_RETRY_LIMIT
                and not retry_limit_reached
            )
            or (
                stop_reason == PROMPT_VALIDATION_STOP_SAFE_FEEDBACK_UNAVAILABLE
                and not safe_feedback_unavailable
            )
            or not isinstance(failures, list)
            or failures != actual_failures
            or not isinstance(failures[-1], dict)
            or failures[-1].get("attempt_id") != str(attempt.id)
            or failures[-1].get("error_code") != attempt.error_code
            or payload.get("result_sha256") != _sha256(evidence)
        ):
            raise ValidationError("script_normalize 人工接管审计证据无效")
    elif not batched_submission and (
        attempt.status != GenerationAttempt.Status.SUCCEEDED
        or not attempt.result_sha256
        or attempt.result_sha256 != payload.get("result_sha256")
    ):
        raise ValidationError("script_normalize attempt/result checksum 无效")
    materialized = confirm_prompt_normalization_candidates(
        project=run.project,
        attempt=attempt,
        candidates=reviewed_candidates,
        user=user,
        provider=str(payload.get("provider") or ""),
        model=str(payload.get("model") or ""),
        result_sha256=str(payload.get("result_sha256") or ""),
        submission_semantics=str(payload.get("submission_semantics") or ""),
        manual_validation_fallback=manual_validation_fallback,
        manual_no_submission=manual_no_submission,
        frozen_input=frozen_input,
        manual_review_origin=manual_review_origin,
        attempts_by_request_id=attempts_by_request_id,
    )
    if len(materialized) != len(reviewed_candidates):
        raise ValidationError("script_normalize 审批物化数量不一致")


def _approve_visual_rules(
    run: ProductionRun,
    review_stage: StageRun,
    *,
    candidate_overrides: dict[str, str] | None,
    expected_source_snapshot_sha256: str,
) -> None:
    generation_stage = run.stages.select_for_update().filter(
        stage_key="visual_rules_generate",
        status=StageRun.Status.SUCCEEDED,
    ).first()
    if generation_stage is None:
        raise ValidationError("缺少已完成的 visual_rules_generate 阶段")
    snapshot = _verify_stage_snapshot(generation_stage, "output_snapshot")
    public_sha256 = json_contract(
        redact_sensitive_bindings(snapshot),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    if expected_source_snapshot_sha256 != public_sha256:
        raise ProductionRunConflict(
            {"detail": "页面展示的全剧影视规则候选已过期，请刷新后重新核对。"},
            code="visual_rules_snapshot_stale",
        )
    payload = snapshot.get("review_payload")
    if not isinstance(payload, dict) or payload.get("kind") != "visual_rules_candidates":
        raise ValidationError("visual_rules_generate 审批候选快照无效")
    attempt = GenerationAttempt.objects.select_for_update().filter(
        pk=payload.get("attempt_id"),
        stage_run=generation_stage,
        operation=VISUAL_RULES_OPERATION,
        status=GenerationAttempt.Status.SUCCEEDED,
    ).first()
    if (
        attempt is None
        or not attempt.result_sha256
        or attempt.result_sha256 != payload.get("result_sha256")
    ):
        raise ValidationError("visual_rules.generate attempt/result checksum 无效")
    overrides = dict(candidate_overrides or {})
    expected_fields = {"negative_prompt", "color_script", "continuity_summary"}
    if set(overrides) != expected_fields:
        missing = sorted(expected_fields - set(overrides))
        unknown = sorted(set(overrides) - expected_fields)
        messages = []
        if missing:
            messages.append("缺少逐项审核字段: " + ", ".join(missing))
        if unknown:
            messages.append("包含未知字段: " + ", ".join(unknown))
        raise ValidationError({"candidate_overrides": "；".join(messages)})
    revision = run.script_revision
    if revision is None:
        raise ValidationError("全剧影视规则审核缺少正式剧本版本")
    try:
        evidence = apply_visual_rules_candidate(
            project=run.project,
            revision=revision,
            frozen=payload,
            reviewed_rules=overrides,
        )
    except VisualRulesConflict as exc:
        raise ProductionRunConflict(
            {"detail": str(exc)},
            code=str(getattr(exc, "code", None) or "visual_rules_source_drift"),
        ) from exc
    run.project.refresh_from_db(fields=("production_settings", "continuity_summary"))
    evidence.update({
        "generation_stage_id": str(generation_stage.id),
        "generation_snapshot_sha256": generation_stage.output_snapshot_sha256,
        "public_generation_snapshot_sha256": public_sha256,
        "attempt_id": str(attempt.id),
        "result_sha256": attempt.result_sha256,
    })
    _save_stage_snapshot(review_stage, "output_snapshot", evidence)


def _verified_unattended_decision_policy(
    run: ProductionRun,
    stage: StageRun,
) -> tuple[str, str]:
    if not _is_unattended_run(run) or stage.mode != "review":
        raise StageBlocked(
            "unattended_policy_context_invalid",
            "decision_policy 只能在已显式确认的 unattended review stage 执行。",
        )
    policy = UNATTENDED_DECISION_POLICY_REGISTRY.get(stage.stage_key)
    if not policy or stage.decision_policy != policy:
        raise StageBlocked(
            "unattended_policy_unregistered",
            f"阶段 {stage.stage_key} 没有匹配的已注册 decision_policy。",
        )
    policy_contract = {
        "schema_version": 1,
        "interaction_mode": UNATTENDED_INTERACTION_MODE,
        "stage_key": stage.stage_key,
        "decision_policy": policy,
        "recipe_definition_sha256": _frozen_recipe_definition_sha256(run),
    }
    policy_sha256 = _sha256(policy_contract)
    if stage.decision_policy_sha256 != policy_sha256:
        raise StageBlocked(
            "unattended_policy_snapshot_drift",
            f"阶段 {stage.stage_key} 的 decision_policy hash 已漂移。",
        )
    return policy, policy_sha256


def _is_unattended_provisional_identity(scope_type: str, value: str) -> bool:
    candidate = str(value or "").strip()
    if scope_type == PromptFieldTranslation.ScopeType.CHARACTER:
        return re.fullmatch(r"S[1-9][0-9]*", candidate) is not None
    if scope_type == PromptFieldTranslation.ScopeType.SCENE:
        return re.fullmatch(
            r"scene:[A-Za-z0-9][A-Za-z0-9_-]*",
            candidate,
        ) is not None
    if scope_type == PromptFieldTranslation.ScopeType.PROP:
        return re.fullmatch(
            r"prop:[A-Za-z0-9][A-Za-z0-9_-]*",
            candidate,
        ) is not None
    return False


def _looks_like_unattended_provisional_identity(
    scope_type: str,
    value: str,
) -> bool:
    candidate = str(value or "").strip()
    if scope_type == PromptFieldTranslation.ScopeType.CHARACTER:
        pattern = r"S[1-9][0-9]*"
    elif scope_type == PromptFieldTranslation.ScopeType.SCENE:
        pattern = r"scene:[A-Za-z0-9][A-Za-z0-9_-]*"
    elif scope_type == PromptFieldTranslation.ScopeType.PROP:
        pattern = r"prop:[A-Za-z0-9][A-Za-z0-9_-]*"
    else:
        return False
    return re.fullmatch(pattern, candidate, flags=re.IGNORECASE) is not None


def _unattended_exact_identity_pattern(tokens: list[str]) -> re.Pattern | None:
    values = sorted({str(token) for token in tokens if str(token)}, key=len, reverse=True)
    if not values:
        return None
    return re.compile(
        r"(?<![A-Za-z0-9_])(?:"
        + "|".join(re.escape(token) for token in values)
        + r")(?![A-Za-z0-9_-])",
    )


def _replace_unattended_identity_tokens(
    value: str,
    replacements: dict[str, str],
) -> str:
    original = str(value or "")
    pattern = _unattended_exact_identity_pattern(list(replacements))
    if pattern is None:
        return original
    # A single case-sensitive pass prevents a generated canonical from being
    # interpreted as another provisional token and preserves all whitespace.
    return pattern.sub(lambda match: replacements[match.group(0)], original)


def _unattended_authoritative_identity_tokens(
    project: SeriesProject,
    keys: set[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    grouped = {
        scope_type: {scope_id for item_scope, scope_id in keys if item_scope == scope_type}
        for scope_type in _UNATTENDED_IDENTITY_FIELDS
    }
    entities: dict[str, dict[str, Any]] = {
        PromptFieldTranslation.ScopeType.CHARACTER: {
            str(item.id): item
            for item in CharacterBible.objects.filter(
                project=project,
                pk__in=grouped[PromptFieldTranslation.ScopeType.CHARACTER],
            ).only("id", "stable_speaker_id")
        },
        PromptFieldTranslation.ScopeType.SCENE: {
            str(item.id): item
            for item in SceneBible.objects.filter(
                project=project,
                pk__in=grouped[PromptFieldTranslation.ScopeType.SCENE],
            ).only("id", "slug")
        },
        PromptFieldTranslation.ScopeType.PROP: {
            str(item.id): item
            for item in PropBible.objects.filter(
                project=project,
                pk__in=grouped[PromptFieldTranslation.ScopeType.PROP],
            ).only("id", "slug")
        },
    }
    result: dict[tuple[str, str], str] = {}
    for key in keys:
        scope_type, scope_id = key
        entity = entities.get(scope_type, {}).get(scope_id)
        if entity is None:
            raise StageBlocked(
                "unattended_prompt_identity_scope_invalid",
                "unattended 提示词标准化候选未绑定当前项目的权威 Bible 实体。",
            )
        if scope_type == PromptFieldTranslation.ScopeType.CHARACTER:
            token = str(entity.stable_speaker_id or "").strip()
        elif scope_type == PromptFieldTranslation.ScopeType.SCENE:
            token = f"scene:{entity.slug}"
        else:
            token = f"prop:{entity.slug}"
        if not _is_unattended_provisional_identity(scope_type, token):
            raise StageBlocked(
                "unattended_prompt_identity_authority_invalid",
                "Bible 实体的权威 provisional identity 无效，策略拒绝猜测。",
            )
        result[key] = token
    return result


def _unattended_visual_canonical_base(
    visual_identity: str,
    *,
    provisional_identities: list[str],
    canonical_policy: dict[str, int],
) -> str:
    from studio.services.canonical_glossary import validate_canonical_english
    from studio.services.prompt_translation import TranslationValidationError

    without_ids = _replace_unattended_identity_tokens(
        visual_identity,
        {value: " " for value in provisional_identities},
    )
    if _UNATTENDED_CJK_RE.search(without_ids):
        return ""
    words = []
    for raw in _UNATTENDED_CANONICAL_WORD_RE.findall(without_ids):
        folded = raw.casefold()
        if (
            folded in _UNATTENDED_CANONICAL_STOP_WORDS
            or re.fullmatch(r"s[1-9][0-9]*", folded)
        ):
            continue
        words.append(raw.title())
        if len(words) == 4:
            break
    for count in range(len(words), 0, -1):
        candidate = " ".join(words[:count])
        try:
            return validate_canonical_english(candidate, policy=canonical_policy)
        except TranslationValidationError:
            continue
    return ""


def _unattended_prompt_normalization_overrides(
    project: SeriesProject,
    candidates: list[dict[str, Any]],
    *,
    raw_overrides: dict[str, str],
    canonical_policy: dict[str, int],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Replace provisional Bible IDs with deterministic, auditable canonicals."""

    from studio.services.canonical_glossary import validate_canonical_english
    from studio.services.prompt_translation import TranslationValidationError

    name_rows: dict[tuple[str, str], dict[str, Any]] = {}
    visual_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        scope_type = str(candidate.get("scope_type") or "")
        scope_id = str(candidate.get("scope_id") or "")
        fields = _UNATTENDED_IDENTITY_FIELDS.get(scope_type)
        if not fields or not scope_id:
            continue
        key = (scope_type, scope_id)
        field_key = str(candidate.get("field_key") or "")
        target = name_rows if field_key == fields[0] else visual_rows if field_key == fields[1] else None
        if target is None:
            continue
        if key in target:
            raise StageBlocked(
                "unattended_prompt_identity_candidate_duplicate",
                "unattended 提示词标准化发现重复的 Bible identity 候选。",
            )
        target[key] = candidate

    configured_glossary = (project.production_settings or {}).get(
        "translation_glossary", {},
    )
    if not isinstance(configured_glossary, dict):
        raise StageBlocked(
            "unattended_prompt_identity_glossary_invalid",
            "项目 translation_glossary 结构无效，策略拒绝自动修复。",
        )
    configured_glossary = {
        str(source).strip(): str(target).strip()
        for source, target in configured_glossary.items()
        if str(source).strip() and str(target).strip()
    }

    scope_order = {
        PromptFieldTranslation.ScopeType.CHARACTER: 0,
        PromptFieldTranslation.ScopeType.SCENE: 1,
        PromptFieldTranslation.ScopeType.PROP: 2,
    }
    identity_keys = set(name_rows)
    authoritative_tokens = _unattended_authoritative_identity_tokens(
        project,
        identity_keys,
    )
    token_owners: dict[str, tuple[str, str]] = {}
    for key, token in authoritative_tokens.items():
        if token in token_owners and token_owners[token] != key:
            raise StageBlocked(
                "unattended_prompt_identity_collision",
                "多个 Bible 实体共享同一权威 provisional identity，策略拒绝自动决定。",
            )
        token_owners[token] = key

    source_owners: dict[str, tuple[str, str]] = {}
    for key, row in name_rows.items():
        source = str(row.get("source_text") or "").strip()
        if not source:
            continue
        if source in source_owners and source_owners[source] != key:
            raise StageBlocked(
                "unattended_prompt_identity_collision",
                "多个 Bible 实体共享同一源名称，策略拒绝自动决定 canonical。",
            )
        source_owners[source] = key

    configured_canonicals: dict[str, str] = {}
    used: dict[str, tuple[str, str]] = {}
    for source, configured in configured_glossary.items():
        try:
            canonical = validate_canonical_english(configured, policy=canonical_policy)
        except TranslationValidationError as exc:
            raise StageBlocked(
                "unattended_prompt_identity_glossary_invalid",
                "项目 translation_glossary 含无效 canonical English。",
            ) from exc
        owner = source_owners.get(source, ("glossary", source))
        folded = canonical.casefold()
        if folded in used and used[folded] != owner:
            raise StageBlocked(
                "unattended_prompt_identity_collision",
                "项目 glossary 的 canonical English 已存在跨实体冲突。",
            )
        configured_canonicals[source] = canonical
        used[folded] = owner

    rewrites: dict[str, str] = {}
    evidence: list[dict[str, Any]] = []
    fallback_counters = {scope_type: 0 for scope_type in _UNATTENDED_IDENTITY_FIELDS}
    ordered_keys = sorted(identity_keys, key=lambda key: (scope_order[key[0]], key[1]))
    authoritative_values = [authoritative_tokens[key] for key in ordered_keys]
    for key in ordered_keys:
        name_row = name_rows[key]
        scope_type, scope_id = key
        _name_field, _visual_field, fallback_label = _UNATTENDED_IDENTITY_FIELDS[scope_type]
        source_name = str(name_row.get("source_text") or "").strip()
        expected_provisional = authoritative_tokens[key]
        translated_name = raw_overrides.get(str(name_row.get("request_id") or ""), "")
        configured = configured_canonicals.get(source_name, "")
        visual_row = visual_rows.get(key)
        visual_request_id = str((visual_row or {}).get("request_id") or "")
        if configured:
            if translated_name not in {expected_provisional, configured}:
                raise StageBlocked(
                    "unattended_prompt_identity_glossary_mismatch",
                    "冻结名称候选与项目已配置 canonical glossary 不一致。",
                )
            canonical = configured
            derivation = "project_glossary"
        elif translated_name == expected_provisional:
            base = _unattended_visual_canonical_base(
                raw_overrides.get(visual_request_id, ""),
                provisional_identities=authoritative_values,
                canonical_policy=canonical_policy,
            )
            derivation = "visual_identity" if base else "deterministic_label"
            if base:
                canonical = base
                suffix = 2
                while canonical.casefold() in used and used[canonical.casefold()] != key:
                    canonical = validate_canonical_english(
                        f"{base} {suffix}", policy=canonical_policy,
                    )
                    suffix += 1
            else:
                while True:
                    fallback_counters[scope_type] += 1
                    canonical = validate_canonical_english(
                        f"{fallback_label} {fallback_counters[scope_type]}",
                        policy=canonical_policy,
                    )
                    if canonical.casefold() not in used or used[canonical.casefold()] == key:
                        break
        elif _looks_like_unattended_provisional_identity(scope_type, translated_name):
            raise StageBlocked(
                "unattended_prompt_identity_authority_mismatch",
                "冻结名称候选的 provisional identity 与当前 Bible 权威值不一致。",
            )
        else:
            try:
                canonical = validate_canonical_english(
                    translated_name, policy=canonical_policy,
                )
            except TranslationValidationError as exc:
                raise StageBlocked(
                    "unattended_prompt_identity_candidate_invalid",
                    "冻结名称候选既不是权威 provisional identity，也不是合法 canonical English。",
                ) from exc
            derivation = "frozen_name_candidate"

        folded = canonical.casefold()
        if folded in used and used[folded] != key:
            raise StageBlocked(
                "unattended_prompt_identity_collision",
                "多个 Bible 实体解析为同一 canonical English，策略拒绝自动决定。",
            )
        used[folded] = key
        rewrites[expected_provisional] = canonical
        evidence.append({
            "scope_type": scope_type,
            "scope_id": scope_id,
            "name_request_id": str(name_row.get("request_id") or ""),
            "visual_identity_request_id": visual_request_id or None,
            "provisional_identity": expected_provisional,
            "canonical_identity": canonical,
            "derivation": derivation,
        })

    replacement_counts = {token: 0 for token in rewrites}
    exact_pattern = _unattended_exact_identity_pattern(list(rewrites))
    for translated in raw_overrides.values():
        observed = [
            match.group(0)
            for match in _UNATTENDED_PROVISIONAL_TOKEN_RE.finditer(translated)
        ]
        unknown = sorted({token for token in observed if token not in rewrites})
        if unknown:
            raise StageBlocked(
                "unattended_prompt_identity_unknown_provisional",
                "冻结候选含未知、大小写漂移或扩展后的 provisional identity。",
            )
        if exact_pattern is not None:
            for match in exact_pattern.finditer(translated):
                replacement_counts[match.group(0)] += 1

    rewritten_overrides = {
        request_id: _replace_unattended_identity_tokens(translated, rewrites)
        for request_id, translated in raw_overrides.items()
    }
    if any(
        _UNATTENDED_PROVISIONAL_TOKEN_RE.search(translated)
        for translated in rewritten_overrides.values()
    ):
        raise StageBlocked(
            "unattended_prompt_identity_rewrite_incomplete",
            "冻结候选仍残留 provisional identity，策略拒绝确认。",
        )
    for item in evidence:
        item["replacement_count"] = replacement_counts[item["provisional_identity"]]
    return rewritten_overrides, evidence


@transaction.atomic
def _execute_unattended_policy_review(
    run: ProductionRun,
    stage: StageRun,
) -> str:
    policy, policy_sha256 = _verified_unattended_decision_policy(run, stage)
    policy_evidence: dict[str, Any] | None = None
    if stage.stage_key == "script_review":
        script_candidates = GeneratedArtifact.objects.select_for_update().filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.SCRIPT,
            status__in=(
                GeneratedArtifact.Status.AVAILABLE,
                GeneratedArtifact.Status.SELECTED,
            ),
        )
        if script_candidates.count() != 1:
            raise StageBlocked(
                "unattended_script_candidate_count_invalid",
                "unattended 剧本策略要求且只允许一个冻结候选。",
            )
        _approve_generated_script(
            run,
            stage,
            None,
            adopt_generated_metadata=False,
        )
    elif stage.stage_key == "bible_review":
        _approve_bible_candidates(run)
    elif stage.stage_key == BIBLE_IMAGE_REVIEW_STAGE_KEY:
        generation_stage = run.stages.select_for_update().filter(
            stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if generation_stage is None:
            raise StageBlocked(
                "unattended_bible_image_generation_missing",
                "unattended Bible 图片策略缺少已成功的生成阶段。",
            )
        source_snapshot = _verify_stage_snapshot(
            generation_stage,
            "output_snapshot",
        )
        canonical = source_snapshot.get("canonical")
        if (
            source_snapshot.get("formal_object_type")
            != "bible_image_candidate_manifest"
            or not isinstance(canonical, dict)
            or _sha256(canonical) != source_snapshot.get("canonical_sha256")
        ):
            raise StageBlocked(
                "unattended_bible_image_snapshot_invalid",
                "unattended Bible 图片策略缺少有效的冻结候选清单。",
            )
        selections: list[dict[str, Any]] = []
        selected_artifact_ids: list[str] = []
        for entity in canonical.get("entities") or []:
            if not isinstance(entity, dict):
                raise StageBlocked(
                    "unattended_bible_image_snapshot_invalid",
                    "unattended Bible 图片候选实体结构无效。",
                )
            current_candidates = [
                item for item in (entity.get("candidates") or [])
                if isinstance(item, dict)
                and not item.get("historical")
                and str(item.get("origin_production_run_id") or "") == str(run.id)
                and str(item.get("origin_stage_run_id") or "")
                == str(generation_stage.id)
            ]
            if len(current_candidates) != 1:
                raise StageBlocked(
                    "unattended_bible_image_candidate_count_invalid",
                    "全自动 Bible 图片策略要求每个实体恰好一个本 Run 冻结候选。",
                )
            artifact_id = str(current_candidates[0].get("artifact_id") or "")
            if not artifact_id:
                raise StageBlocked(
                    "unattended_bible_image_candidate_invalid",
                    "全自动 Bible 图片候选缺少 GeneratedArtifact identity。",
                )
            selected_artifact_ids.append(artifact_id)
            selections.append({
                "entity_type": str(entity.get("entity_type") or ""),
                "entity_id": str(entity.get("entity_id") or ""),
                "selected_artifact_ids": [artifact_id],
                "selected_library_assets": [],
                "anchor_artifact_id": artifact_id,
                "anchor_library_asset_id": None,
            })
        if not selections:
            raise StageBlocked(
                "unattended_bible_image_candidates_missing",
                "全自动 Bible 图片策略没有可绑定的实体候选。",
            )
        expected_source_snapshot_sha256 = json_contract(
            redact_sensitive_bindings(source_snapshot),
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        try:
            with _StoredAssetFileGuard() as asset_guard:
                with transaction.atomic():
                    _approve_bible_image_bindings(
                        run,
                        stage,
                        run.project.owner,
                        bindings=selections,
                        expected_source_snapshot_sha256=(
                            expected_source_snapshot_sha256
                        ),
                        asset_guard=asset_guard,
                    )
                    _assert_stage_output_integrity(run, stage)
        except (ValidationError, ProductionRunConflict) as exc:
            raise StageBlocked(
                "unattended_bible_image_binding_failed",
                "全自动 Bible 图片唯一候选未通过来源、字节或实体绑定校验。",
            ) from exc
        policy_evidence = {
            "schema_version": 1,
            "kind": "unattended_bible_image_policy",
            "source_snapshot_sha256": expected_source_snapshot_sha256,
            "selected_artifact_ids": sorted(selected_artifact_ids),
        }
    elif stage.stage_key == "visual_rules_review":
        generation_stage = run.stages.select_for_update().filter(
            stage_key="visual_rules_generate",
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if generation_stage is None:
            raise StageBlocked(
                "unattended_visual_rules_missing",
                "unattended 全剧影视规则策略缺少已成功的生成阶段。",
            )
        source_snapshot = _verify_stage_snapshot(generation_stage, "output_snapshot")
        payload = source_snapshot.get("review_payload")
        candidate = payload.get("candidate") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != "visual_rules_candidates"
            or not isinstance(candidate, dict)
        ):
            raise StageBlocked(
                "unattended_visual_rules_snapshot_invalid",
                "unattended 全剧影视规则策略缺少唯一冻结候选。",
            )
        expected_source_snapshot_sha256 = json_contract(
            redact_sensitive_bindings(source_snapshot),
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        try:
            _approve_visual_rules(
                run,
                stage,
                candidate_overrides={
                    key: str(candidate.get(key) or "")
                    for key in ("negative_prompt", "color_script", "continuity_summary")
                },
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
            )
        except ValidationError as exc:
            raise StageBlocked(
                "unattended_visual_rules_validation_failed",
                "unattended 全剧影视规则候选未通过来源、Bible 或规则合同校验。",
            ) from exc
        policy_evidence = {
            "schema_version": 1,
            "kind": "unattended_visual_rules_policy",
            "source_snapshot_sha256": expected_source_snapshot_sha256,
            "candidate_sha256": str(payload.get("candidate_sha256") or ""),
        }
    elif stage.stage_key == "script_normalize_review":
        normalize_stage = run.stages.select_for_update().filter(
            stage_key="script_normalize",
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if normalize_stage is None:
            raise StageBlocked(
                "unattended_prompt_normalization_missing",
                "unattended 提示词标准化策略缺少已成功的 script_normalize 阶段。",
            )
        source_snapshot = _verify_stage_snapshot(
            normalize_stage,
            "output_snapshot",
        )
        review_payload = source_snapshot.get("review_payload")
        candidates = (
            review_payload.get("candidates")
            if isinstance(review_payload, dict)
            and review_payload.get("kind") == "prompt_normalization_candidates"
            else None
        )
        if not isinstance(candidates, list):
            raise StageBlocked(
                "unattended_prompt_normalization_snapshot_invalid",
                "unattended 提示词标准化策略缺少冻结候选清单。",
            )
        raw_overrides: dict[str, str] = {}
        for candidate in candidates:
            request_id = str(
                (candidate.get("request_id") or "") if isinstance(candidate, dict) else ""
            ).strip()
            translated_text = str(
                (candidate.get("translated_text") or "") if isinstance(candidate, dict) else ""
            ).strip()
            if not request_id or request_id in raw_overrides or not translated_text:
                raise StageBlocked(
                    "unattended_prompt_normalization_candidate_invalid",
                    "unattended 提示词标准化策略发现缺失、重复或空白的冻结候选。",
                )
            raw_overrides[request_id] = translated_text
        candidate_overrides, identity_rewrites = (
            _unattended_prompt_normalization_overrides(
                run.project,
                candidates,
                raw_overrides=raw_overrides,
                canonical_policy=canonical_name_policy(
                    values=resolve_configuration_from_snapshot(run.config_snapshot).values,
                ),
            )
        )
        expected_source_snapshot_sha256 = json_contract(
            redact_sensitive_bindings(source_snapshot),
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        try:
            _approve_prompt_normalization(
                run,
                None,
                candidate_overrides=candidate_overrides,
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
            )
        except ValidationError as exc:
            raise StageBlocked(
                "unattended_prompt_normalization_validation_failed",
                (
                    "unattended 提示词标准化冻结候选未通过来源、上下文、"
                    "英文执行文本或 canonical identity 校验。"
                ),
            ) from exc
        policy_evidence = {
            "schema_version": 1,
            "kind": "unattended_prompt_normalization_policy",
            "source_snapshot_sha256": expected_source_snapshot_sha256,
            "candidate_count": len(candidates),
            "candidate_overrides_sha256": _sha256(candidate_overrides),
            "canonical_identity_rewrites": identity_rewrites,
        }
    elif stage.stage_key == "storyboard_review":
        reference_inputs = _verified_unattended_reference_inputs(run)
        storyboard_stage = run.stages.select_for_update().filter(
            stage_key="storyboard",
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if storyboard_stage is None:
            raise StageBlocked(
                "unattended_storyboard_missing",
                "unattended storyboard_review 缺少已成功的确定性 storyboard。",
            )
        source_snapshot = _verify_stage_snapshot(
            storyboard_stage,
            "output_snapshot",
        )
        expected_source_snapshot_sha256 = json_contract(
            redact_sensitive_bindings(source_snapshot),
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        shots = list(
            Shot.objects.select_for_update().filter(
                episode__in=_selected_episodes(run),
            ).order_by("episode__number", "number", "id")
        )
        if not shots or any(
            shot.media_type != Shot.MediaType.VIDEO
            or not _shot_requires_entity_reference_bible(run, shot)
            for shot in shots
        ):
            raise StageBlocked(
                "unattended_dynamic_storyboard_invalid",
                "unattended 动态参考链只允许冻结条件模式明确要求实体参考 Bible 的视频 Shot。",
            )
        used_bible_entities = {
            "character": {
                str(entity_id)
                for shot in shots
                for entity_id in shot.characters.values_list("id", flat=True)
            },
            "scene": {str(shot.scene_id) for shot in shots if shot.scene_id},
            "prop": {
                str(entity_id)
                for shot in shots
                for entity_id in shot.props.values_list("id", flat=True)
            },
        }
        expected_bible_entities = {
            "character": {
                str(item.id)
                for item in visual_reference_characters(
                    list(run.project.characters.all())
                )
            },
            "scene": {
                str(value) for value in run.project.scenes.values_list("id", flat=True)
            },
            "prop": {
                str(value) for value in run.project.props.values_list("id", flat=True)
            },
        }
        unused = {
            entity_type: sorted(expected_bible_entities[entity_type] - used_ids)
            for entity_type, used_ids in used_bible_entities.items()
            if expected_bible_entities[entity_type] - used_ids
        }
        if unused:
            raise StageBlocked(
                "unattended_storyboard_bible_entities_unused",
                "全自动分镜没有使用全部已物化 Bible 实体，不能把未出现在 Shot 关系中的"
                f"人物/场景/道具伪装为已建立一致性：{unused}",
            )
        video_bindings = [{
            "asset_id": item["asset_id"],
            "purpose": item["purpose"],
            "use_embedded_audio": item["use_embedded_audio"],
        } for item in reference_inputs]
        approved_anchors: dict[tuple[str, str], str] = {}
        if run.stages.filter(
            stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
        ).exists():
            bible_review_stage = run.stages.select_for_update().filter(
                stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
                status=StageRun.Status.SUCCEEDED,
            ).first()
            if bible_review_stage is None:
                raise StageBlocked(
                    "unattended_bible_image_review_missing",
                    "全自动实体参考分镜策略缺少已批准的 Bible 图片绑定。",
                )
            bible_snapshot = _verify_stage_snapshot(
                bible_review_stage,
                "output_snapshot",
            )
            bible_canonical = bible_snapshot.get("canonical")
            if (
                bible_snapshot.get("formal_object_type")
                != "bible_image_bindings"
                or not isinstance(bible_canonical, dict)
                or _sha256(bible_canonical)
                != bible_snapshot.get("canonical_sha256")
            ):
                raise StageBlocked(
                    "unattended_bible_image_binding_snapshot_invalid",
                    "全自动实体参考分镜策略的 Bible 图片绑定快照无效。",
                )
            approved_anchors = {
                (
                    str(item.get("entity_type") or ""),
                    str(item.get("entity_id") or ""),
                ): str(item.get("anchor_asset_id") or "")
                for item in (bible_canonical.get("bindings") or [])
                if isinstance(item, dict) and item.get("anchor_asset_id")
            }
        selections = []
        for shot in shots:
            bindings = deepcopy(
                shot.reference_bindings
                if isinstance(shot.reference_bindings, dict) else {}
            )
            if approved_anchors:
                bindings["characters"] = [
                    {
                        "entity_id": str(entity.id),
                        "asset_id": approved_anchors.get(
                            ("character", str(entity.id)),
                            "",
                        ),
                    }
                    for entity in visual_reference_characters(
                        list(shot.characters.all().order_by("id"))
                    )
                ]
                bindings["props"] = [
                    {
                        "entity_id": str(entity.id),
                        "asset_id": approved_anchors.get(
                            ("prop", str(entity.id)),
                            "",
                        ),
                    }
                    for entity in shot.props.all().order_by("id")
                ]
                if shot.scene_id:
                    bindings["scene"] = {
                        "entity_id": str(shot.scene_id),
                        "asset_id": approved_anchors.get(
                            ("scene", str(shot.scene_id)),
                            "",
                        ),
                    }
            bindings["videos"] = deepcopy(video_bindings)
            selections.append({
                "shot_id": str(shot.id),
                "reference_bindings": bindings,
            })
        _approve_storyboard_reference_bindings(
            run,
            user=run.project.owner,
            selections=selections,
            expected_source_snapshot_sha256=expected_source_snapshot_sha256,
        )
        policy_evidence = {
            "schema_version": 1,
            "kind": "unattended_storyboard_reference_policy",
            "source_snapshot_sha256": expected_source_snapshot_sha256,
            "reference_input_count": len(reference_inputs),
            "reference_inputs_sha256": _sha256(reference_inputs),
            "shot_count": len(shots),
        }
    elif stage.stage_key == "artifact_selection":
        media_stage = run.stages.select_for_update().filter(
            stage_key="media_generation",
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if media_stage is None:
            raise StageBlocked(
                "unattended_media_missing",
                "unattended 单产物策略缺少已成功的 media_generation。",
            )
        artifacts = GeneratedArtifact.objects.select_for_update().filter(
            production_run=run,
            stage_run=media_stage,
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        )
        groups = list(artifacts.values_list("variant_group", flat=True).distinct())
        if not groups:
            raise StageBlocked(
                "unattended_artifact_missing",
                "unattended 单产物策略没有可审核的媒体产物。",
            )
        for group in groups:
            candidates = list(artifacts.filter(variant_group=group))
            if (
                len(candidates) != 1
                or candidates[0].status != GeneratedArtifact.Status.SELECTED
            ):
                raise StageBlocked(
                    "unattended_multiple_artifacts",
                    f"产物组 {group} 不是唯一且已选择的正式产物，策略拒绝自动决定。",
                )
    else:
        raise StageBlocked(
            "unattended_policy_unregistered",
            f"阶段 {stage.stage_key} 没有 unattended policy executor。",
        )
    _assert_stage_output_integrity(run, stage)
    formal_snapshot = _formal_snapshot(run, stage, f"recipe-policy:{policy}")
    if policy_evidence is not None:
        formal_snapshot["policy_evidence"] = policy_evidence
    output_snapshot_sha256 = _save_stage_snapshot(
        stage,
        "output_snapshot",
        formal_snapshot,
    )
    revision = (
        stage.approval_decisions.aggregate(value=Max("revision"))["value"]
        or 0
    ) + 1
    approval = ApprovalDecision.objects.create(
        stage_run=stage,
        revision=revision,
        decision=ApprovalDecision.Decision.APPROVE,
        comment=f"由已冻结配方策略 {policy} 自动决定。",
        output_snapshot_sha256=output_snapshot_sha256,
        actor=ApprovalDecision.Actor.RECIPE_POLICY,
        decision_policy=policy,
        decision_policy_sha256=policy_sha256,
        decided_by=None,
    )
    return f"approval:{approval.id}"


def _execute_stage(run: ProductionRun, stage: StageRun) -> str | None:
    if stage.mode == "skip":
        return "skipped"
    if stage.stage_key == "script_normalize" and stage.mode == "manual":
        return _prepare_manual_prompt_normalization(run, stage)
    if stage.mode == "manual":
        raise StageBlocked(
            "manual_stage_confirmation_required",
            "该阶段配置为人工处理；完成页面操作后审批继续。",
            awaiting_approval=True,
        )
    if stage.stage_key == "script_generate" and stage.mode == "ai":
        return _prepare_script_generation(run, stage)
    if stage.stage_key == "script_structure" and stage.mode == "ai":
        return _prepare_script_structure(run, stage)
    if stage.stage_key == "visual_rules_generate" and stage.mode == "ai":
        return _prepare_visual_rules(run, stage)
    if stage.stage_key == "script_normalize" and stage.mode == "ai":
        return _prepare_prompt_normalization(run, stage)
    if stage.mode == "ai":
        _route_candidates(stage, unit_key=f"stage:{stage.stage_key}")
        raise StageBlocked(
            "driver_contract_missing",
            (
                f"阶段 {stage.stage_key} 已冻结 route，但正式文本结果 schema/对象物化适配器"
                "尚未注册；不会调用模型或伪造完成。"
            ),
            awaiting_approval=True,
        )
    if (
        stage.stage_key == "script_normalize_review"
        and stage.mode == "review"
        and stage.decision_policy
    ):
        normalize_stage = run.stages.filter(
            stage_key="script_normalize",
            status=StageRun.Status.SUCCEEDED,
        ).first()
        if normalize_stage is not None:
            source_snapshot = _verify_stage_snapshot(normalize_stage, "output_snapshot")
            review_payload = source_snapshot.get("review_payload")
            if (
                isinstance(review_payload, dict)
                and review_payload.get("review_origin")
                == "human_after_validation_retry_exhausted"
            ):
                stop_reason = str(review_payload.get("stop_reason") or "")
                raise StageBlocked(
                    PROMPT_VALIDATION_REVIEW_REQUIRED,
                    (
                        "提示词字段生成未产生可安全绑定冻结字段的修复反馈；"
                        if stop_reason
                        == PROMPT_VALIDATION_STOP_SAFE_FEEDBACK_UNAVAILABLE
                        else "提示词字段生成已达到冻结的校验重试上限；"
                    )
                    + (
                        "请逐项填写并确认英文执行字段后继续。"
                    ),
                    awaiting_approval=True,
                )
    if stage.mode == "review" and stage.decision_policy:
        return _execute_unattended_policy_review(run, stage)
    if stage.approval_required or stage.mode == "review":
        raise StageBlocked(
            "stage_approval_required",
            f"阶段 {stage.stage_key} 需要人工审批。",
            awaiting_approval=True,
        )
    if stage.stage_key == "script_input":
        return f"generated-artifact:{_script_artifact(run, stage).id}"
    if stage.stage_key == "bible_materialize" and stage.mode == "deterministic":
        return _execute_bible_materialize(run, stage)
    if stage.stage_key == "storyboard" and stage.mode == "deterministic":
        return _execute_storyboard(run, stage)
    if stage.stage_key == "prompt_compile" and stage.mode == "existing_pipeline":
        return _execute_prompt_compile(run, stage)
    if stage.stage_key == "media_generation" and stage.mode == "existing_pipeline":
        return _poll_media_stage(run, stage)
    if (
        stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY
        and stage.mode == "existing_pipeline"
    ):
        return _poll_media_stage(run, stage)
    if stage.stage_key == "assembly" and stage.mode == "existing_pipeline":
        return _execute_assembly(run, stage)
    if stage.stage_key == "delivery" and stage.mode == "existing_pipeline":
        return _execute_delivery(run, stage)
    raise StageBlocked(
        "stage_executor_unavailable",
        f"阶段 {stage.stage_key}/{stage.mode} 尚未接入受控执行器。",
    )


def _assert_stage_output_integrity(run: ProductionRun, stage: StageRun) -> None:
    if stage.reason_code in {
        "routing_not_configured", "route_selection_required", "unknown_cost",
        "budget_limit_exceeded", "external_budget_missing",
        "external_call_not_authorized", "driver_contract_missing",
    }:
        raise ValidationError("该门禁必须通过对应的 route/费用/外发操作解决，不能用审批跳过")
    if stage.stage_key == "script_input" and stage.mode == "manual":
        episodes = _selected_episodes(run)
        if not episodes:
            raise ValidationError("沿用现有分集前必须选择至少一个正式 Episode")
        return
    if stage.stage_key in {"script_input", "script_review", "script_generate", "script_structure"}:
        revision = run.script_revision
        if revision is None or revision.status != ScriptRevision.Status.CONFIRMED:
            raise ValidationError("剧本审批前必须存在已确认的正式 ScriptRevision")
        if not GeneratedArtifact.objects.filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.SCRIPT,
            status=GeneratedArtifact.Status.SELECTED,
        ).exists():
            _script_artifact(run, stage)
        return
    if stage.stage_key == "visual_rules_generate":
        snapshot = _verify_stage_snapshot(stage, "output_snapshot")
        payload = snapshot.get("review_payload") if snapshot.get("formal_object_type") else snapshot
        if not isinstance(payload, dict) or payload.get("kind") != "visual_rules_candidates":
            raise ValidationError("全剧影视规则生成阶段缺少冻结候选")
        attempt = stage.generation_attempts.filter(
            pk=payload.get("attempt_id"),
            operation=VISUAL_RULES_OPERATION,
            status=GenerationAttempt.Status.SUCCEEDED,
        ).first()
        if attempt is None or attempt.result_sha256 != payload.get("result_sha256"):
            raise ValidationError("全剧影视规则生成候选缺少有效 attempt/result 证据")
        return
    if stage.stage_key == "visual_rules_review":
        existing = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
        if existing.get("kind") != "visual_rules_review_decision":
            raise ValidationError("全剧影视规则审核尚未原子写入正式项目字段")
        canonical = _canonical_visual_rules(run)
        if canonical["rules_sha256"] != existing.get("after_rules_sha256"):
            raise ValidationError("全剧影视规则审核结果与当前项目正式字段不一致")
        return
    if stage.stage_key == "script_normalize_review":
        canonical = _canonical_prompt_translations(run)
        rows = canonical.get("translations")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or not row.get("entry_id")
            or not row.get("source_hash")
            or not row.get("translated_text_sha256")
            for row in rows
        ):
            raise ValidationError("prompt.normalize 审批物化不完整")
        return
    if stage.stage_key in {"bible", "bible_generation", "bible_review"}:
        missing = []
        for label, model in (
            ("CharacterBible", CharacterBible),
            ("SceneBible", SceneBible),
            ("PropBible", PropBible),
        ):
            if not model.objects.filter(project=run.project).exclude(name="").exists():
                missing.append(label)
        if missing:
            raise ValidationError(f"Bible 审批前缺少正式文本实体: {', '.join(missing)}")
        return
    if stage.stage_key in {"storyboard", "storyboard_review"}:
        episodes = _selected_episodes(run)
        if not episodes:
            raise ValidationError("分镜审批前必须存在正式 Episode")
        missing_shots = [str(item.id) for item in episodes if not item.shots.exists()]
        if missing_shots:
            raise ValidationError(f"以下 Episode 没有正式 Shot: {', '.join(missing_shots)}")
        return
    if stage.stage_key in {"media_generation", "media_review"}:
        artifacts = GeneratedArtifact.objects.filter(
            production_run=run,
            stage_run__stage_key="media_generation",
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        )
        if not artifacts.exists():
            raise ValidationError("媒体审批前没有正式 GeneratedArtifact")
        return
    if stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
        candidates = _canonical_bible_image_candidates(run, stage)
        expected = {
            (entity_type, str(entity.id))
            for entity_type, entity in _bible_entities(run.project)
        }
        actual = {
            (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
            for item in candidates.get("entities", [])
            if isinstance(item, dict) and item.get("candidates")
        }
        if actual != expected:
            raise ValidationError("Bible 参考图候选未完整覆盖当前实体")
        return
    if stage.stage_key == BIBLE_IMAGE_REVIEW_STAGE_KEY:
        snapshot = stage.output_snapshot if isinstance(stage.output_snapshot, dict) else {}
        if snapshot.get("kind") != "bible_image_bindings":
            raise ValidationError("Bible 参考图审核尚未物化绑定结果")
        bindings = snapshot.get("bindings")
        if not isinstance(bindings, list) or len(bindings) != len(_bible_entities(run.project)):
            raise ValidationError("Bible 参考图审核绑定覆盖不完整")
        for item in bindings:
            if (
                not isinstance(item, dict)
                or not item.get("anchor_asset_id")
                or not isinstance(item.get("reference_asset_ids"), list)
                or not item.get("selected_asset_ids")
                or not (
                    item.get("selected_artifact_ids")
                    or item.get("selected_library_asset_ids")
                )
            ):
                raise ValidationError("Bible 参考图审核绑定结果无效")
        return
    if stage.stage_key == "artifact_selection":
        media_stage = run.stages.filter(stage_key="media_generation").first()
        if media_stage is None or media_stage.status != StageRun.Status.SUCCEEDED:
            raise ValidationError("选片前 media_generation 尚未成功")
        artifacts = GeneratedArtifact.objects.filter(
            production_run=run,
            stage_run=media_stage,
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        )
        if not artifacts.exists():
            raise ValidationError("选片前没有正式 GeneratedArtifact")
        groups = list(artifacts.values_list("variant_group", flat=True).distinct())
        for group in groups:
            if not artifacts.filter(
                variant_group=group,
                status=GeneratedArtifact.Status.SELECTED,
            ).exists():
                raise ValidationError(f"产物组 {group} 尚未选择正式版本")
        routing = _verify_stage_routing(media_stage)
        if routing["variant_count"] > 1 and not routing.get("evaluator"):
            decided_groups = set(media_stage.artifact_selection_decisions.values_list(
                "variant_group", flat=True,
            ))
            if set(groups) - decided_groups:
                raise ValidationError("多版本产物未配置 evaluator，每个产物组都必须执行显式选片")
        return
    if stage.stage_key == "assembly":
        episodes = _selected_episodes(run)
        masters = GeneratedArtifact.objects.filter(
            production_run=run,
            kind=GeneratedArtifact.Kind.ASSEMBLY,
            role="episode_master",
            status=GeneratedArtifact.Status.SELECTED,
        )
        if masters.count() != len(episodes):
            raise ValidationError("逐集母版 artifact 清单不完整")
        return
    if stage.stage_key in {"delivery", "release"}:
        bundle = ReleaseBundle.objects.filter(production_run=run).first()
        if bundle is None or bundle.status != ReleaseBundle.Status.SEALED:
            raise ValidationError("交付审批前必须存在已封存 ReleaseBundle")
        return
    if stage.mode in {"manual", "review"}:
        raise ValidationError(f"阶段 {stage.stage_key} 尚未注册正式对象完整性断言")


def _assert_delivery_complete(run: ProductionRun) -> ReleaseBundle:
    bundle = ReleaseBundle.objects.filter(
        production_run=run,
        status=ReleaseBundle.Status.SEALED,
    ).first()
    if bundle is None:
        raise StageBlocked("delivery_manifest_missing", "运行没有已封存的 ReleaseBundle")
    try:
        document = verified_release_bundle(bundle)
    except ReleaseBundleError as exc:
        raise StageBlocked("delivery_manifest_invalid", str(exc)) from exc
    episodes = _selected_episodes(run)
    links = list(ProductionAssemblyLink.objects.filter(
        production_run=run,
    ).select_related("assembly_job"))
    if len(links) != len(episodes) or any(
        link.assembly_job.status != "succeeded"
        for link in links
    ):
        raise StageBlocked("episode_master_incomplete", "逐集母版任务未全部成功")
    masters = GeneratedArtifact.objects.filter(
        production_run=run,
        kind=GeneratedArtifact.Kind.ASSEMBLY,
        role="episode_master",
        status=GeneratedArtifact.Status.SELECTED,
    )
    if masters.count() != len(episodes):
        raise StageBlocked("episode_master_manifest_incomplete", "逐集母版 artifact 清单不完整")
    selected_episode_ids = {
        str(item.get("episode_id") or "")
        for item in (document.get("selection", {}).get("episodes") or [])
        if isinstance(item, dict)
    }
    if selected_episode_ids != {str(item.id) for item in episodes}:
        raise StageBlocked("release_episode_manifest_incomplete", "ReleaseBundle 未覆盖全部 Episode")
    return bundle


def _blocked_text_stage_has_same_target_fresh_smoke(stage: StageRun) -> bool:
    """Read-only gate used by ``continue`` before re-entering a blocked preflight."""

    stage_contracts = {
        "script_structure": (
            SCRIPT_STRUCTURE_OPERATION,
            {
                "script_structure_smoke_invalid",
                "script_structure_smoke_missing_or_stale",
                "script_structure_smoke_drift",
            },
        ),
        "visual_rules_generate": (
            VISUAL_RULES_OPERATION,
            {
                "visual_rules_smoke_invalid",
                "visual_rules_smoke_missing_or_stale",
                "visual_rules_smoke_drift",
            },
        ),
        "script_normalize": (
            PROMPT_NORMALIZE_OPERATION,
            {
                "prompt_normalize_smoke_invalid",
                "prompt_normalize_smoke_required",
                "prompt_normalize_smoke_drift",
            },
        ),
    }
    contract = stage_contracts.get(stage.stage_key)
    if (
        stage.status != StageRun.Status.BLOCKED
        or contract is None
        or stage.reason_code not in contract[1]
        or stage.generation_attempts.exists()
    ):
        return False
    operation = contract[0]
    try:
        routing = _verify_stage_routing(stage)
    except (StageBlocked, ValidationError):
        return False
    for route in routing.get("routes") or []:
        if not isinstance(route, dict):
            continue
        try:
            live = ModelDeployment.objects.select_related(
                "profile_version__provider",
            ).get(pk=route.get("model_deployment_id"), status=ModelDeployment.Status.ACTIVE)
            frozen_text_deployment({"route": route})
            current = fresh_smoke_snapshot(live, operation)
        except (ModelDeployment.DoesNotExist, ProviderDriverError, ValidationError, TypeError, ValueError):
            continue
        frozen_smoke = (
            route.get("smoke_evidence", {}).get(operation)
            if isinstance(route.get("smoke_evidence"), dict) else None
        )
        if (
            current is not None
            and isinstance(frozen_smoke, dict)
            and current.target_sha256 == frozen_smoke.get("target_sha256")
            and current.provider_config_sha256 == route.get("provider_config_record_sha256")
            and current.deployment_config_sha256 == route.get("deployment_config_record_sha256")
            and current.credential_binding_sha256 == route.get("credential_binding_sha256")
        ):
            return True
    return False


@transaction.atomic
@security_boundary_resolution_cache()
def advance_production_run(run: ProductionRun) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().select_related(
        "project", "owner", "script_revision__source", "recipe_version",
    ).get(pk=run.pk)
    if run.status in TERMINAL_RUN_STATUSES:
        return run
    _verify_run_snapshots(run)
    stages = list(run.stages.select_for_update().order_by("ordinal"))
    if not stages:
        run.status = ProductionRun.Status.BLOCKED
        run.blocked_code = "recipe_stages_missing"
        run.blocked_reason = "ProductionRun 没有持久化 StageRun。"
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
        return run
    try:
        _assert_frozen_bible_run_closure(stages)
    except ProductionRunConflict as exc:
        run.status = ProductionRun.Status.BLOCKED
        run.blocked_code = exc.code
        run.blocked_reason = str(exc.messages[0] if exc.messages else exc)
        run.save(update_fields=(
            "status", "blocked_code", "blocked_reason", "updated_at",
        ))
        return run
    if run.started_at is None:
        run.started_at = timezone.now()
    for stage in stages:
        if stage.status == StageRun.Status.SUCCEEDED:
            continue
        run.current_stage_key = stage.stage_key
        if _blocked_text_stage_has_same_target_fresh_smoke(stage):
            # ``continue`` may safely re-enter only this no-submission preflight.
            # The queue helper performs the authoritative original-evidence
            # verification and appends the new smoke audit before any attempt.
            stage.status = StageRun.Status.PENDING
            stage.reason_code = ""
            stage.reason = ""
            stage.finished_at = None
            stage.save(update_fields=(
                "status", "reason_code", "reason", "finished_at", "updated_at",
            ))
        if stage.status in {StageRun.Status.AWAITING_APPROVAL, StageRun.Status.BLOCKED, StageRun.Status.FAILED}:
            run.status = (
                ProductionRun.Status.AWAITING_APPROVAL
                if stage.status == StageRun.Status.AWAITING_APPROVAL
                else ProductionRun.Status.BLOCKED
                if stage.status == StageRun.Status.BLOCKED
                else ProductionRun.Status.FAILED
            )
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            break
        if stage.status == StageRun.Status.CANCELLED:
            run.status = ProductionRun.Status.CANCELLED
            run.finished_at = timezone.now()
            break
        if stage.status == StageRun.Status.PENDING:
            stage.status = StageRun.Status.RUNNING
            stage.attempt_count += 1
            stage.started_at = stage.started_at or timezone.now()
            stage.reason_code = ""
            stage.reason = ""
            stage.save(update_fields=(
                "status", "attempt_count", "started_at", "reason_code", "reason", "updated_at",
            ))
        run.status = ProductionRun.Status.RUNNING
        run.blocked_code = ""
        run.blocked_reason = ""
        run.save(update_fields=(
            "status", "current_stage_key", "blocked_code", "blocked_reason",
            "started_at", "updated_at",
        ))
        try:
            if stage.stage_key in {
                BIBLE_IMAGE_GENERATION_STAGE_KEY, BIBLE_IMAGE_REVIEW_STAGE_KEY,
                "visual_rules_generate", "visual_rules_review", "storyboard",
                "script_normalize", "script_normalize_review",
                "prompt_compile", "media_generation", "artifact_selection",
                "assembly", "delivery", "release",
            }:
                _assert_frozen_formal_objects(run)
            _freeze_stage_config(run, stage)
            _freeze_stage_input(run, stage)
            output_reference = _execute_stage(run, stage)
        except StageBlocked as exc:
            unattended_human_escalation = (
                _is_unattended_run(run)
                and stage.stage_key == "script_normalize_review"
                and exc.awaiting_approval
                and exc.code == PROMPT_VALIDATION_REVIEW_REQUIRED
            )
            policy_fail_closed = _is_unattended_run(run) and not unattended_human_escalation
            stage.status = (
                StageRun.Status.AWAITING_APPROVAL
                if exc.awaiting_approval and not policy_fail_closed
                else StageRun.Status.BLOCKED
            )
            stage.reason_code = exc.code
            stage.reason = exc.reason
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status = (
                ProductionRun.Status.AWAITING_APPROVAL
                if exc.awaiting_approval and not policy_fail_closed
                else ProductionRun.Status.BLOCKED
            )
            run.blocked_code = exc.code
            run.blocked_reason = exc.reason
            break
        except ValidationError as exc:
            stage.status = StageRun.Status.BLOCKED
            stage.reason_code = "stage_validation_failed"
            stage.reason = str(exc)[:2000]
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status = ProductionRun.Status.BLOCKED
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            break
        except Exception as exc:
            logger.exception("ProductionRun stage failed", extra={
                "production_run_id": str(run.id),
                "stage_key": stage.stage_key,
            })
            stage.status = StageRun.Status.FAILED
            stage.reason_code = "stage_execution_failed"
            stage.reason = "阶段执行发生未分类错误；请查看服务器日志。"
            stage.finished_at = timezone.now()
            stage.save(update_fields=(
                "status", "reason_code", "reason", "finished_at", "updated_at",
            ))
            run.status = ProductionRun.Status.FAILED
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            run.finished_at = timezone.now()
            break
        if output_reference is None:
            stage.status = StageRun.Status.RUNNING
            stage.reason_code = "waiting_external_task"
            stage.reason = "已创建持久化任务，等待 worker 完成；调用 continue 刷新状态。"
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status = ProductionRun.Status.RUNNING
            run.blocked_code = stage.reason_code
            run.blocked_reason = stage.reason
            break
        if not (stage.mode == "review" and stage.decision_policy):
            _save_stage_snapshot(
                stage,
                "output_snapshot",
                _formal_snapshot(run, stage, str(output_reference)),
            )
        stage.status = StageRun.Status.SUCCEEDED
        stage.output_reference = str(output_reference)[:255]
        stage.reason_code = ""
        stage.reason = ""
        stage.finished_at = timezone.now()
        stage.save(update_fields=(
            "status", "output_reference", "reason_code", "reason", "finished_at", "updated_at",
        ))
    else:
        try:
            _assert_delivery_complete(run)
        except StageBlocked as exc:
            run.status = ProductionRun.Status.BLOCKED
            run.blocked_code = exc.code
            run.blocked_reason = exc.reason
        else:
            run.status = ProductionRun.Status.SUCCEEDED
            run.current_stage_key = ""
            run.blocked_code = ""
            run.blocked_reason = ""
            run.finished_at = timezone.now()
    run.save(update_fields=(
        "status", "current_stage_key", "blocked_code", "blocked_reason",
        "started_at", "finished_at", "updated_at",
    ))
    return run


@security_boundary_resolution_cache()
def decide_stage(
    run: ProductionRun,
    *,
    stage_key: str,
    decision: str,
    comment: str,
    user,
    candidate_overrides: dict[str, str] | None = None,
    expected_source_snapshot_sha256: str = "",
    adopt_generated_metadata: bool = False,
    bible_image_bindings: list[dict[str, Any]] | None = None,
    shot_reference_bindings: list[dict[str, Any]] | None = None,
) -> ProductionRun:
    # The file guard encloses the database commit itself.  A late constraint or
    # storage-backed transaction failure therefore removes every newly copied
    # Asset file instead of leaving owner directories out of sync with DB facts.
    try:
        with _StoredAssetFileGuard() as asset_guard:
            with transaction.atomic():
                return _decide_stage_locked(
                    run,
                    stage_key=stage_key,
                    decision=decision,
                    comment=comment,
                    user=user,
                    candidate_overrides=candidate_overrides,
                    expected_source_snapshot_sha256=expected_source_snapshot_sha256,
                    adopt_generated_metadata=adopt_generated_metadata,
                    bible_image_bindings=bible_image_bindings,
                    shot_reference_bindings=shot_reference_bindings,
                    asset_guard=asset_guard,
                )
    except StageBlocked as exc:
        raise ProductionRunConflict(
            {"detail": exc.reason},
            code=exc.code,
        ) from exc


def _decide_stage_locked(
    run: ProductionRun,
    *,
    stage_key: str,
    decision: str,
    comment: str,
    user,
    candidate_overrides: dict[str, str] | None,
    expected_source_snapshot_sha256: str,
    adopt_generated_metadata: bool,
    bible_image_bindings: list[dict[str, Any]] | None,
    shot_reference_bindings: list[dict[str, Any]] | None,
    asset_guard: _StoredAssetFileGuard,
) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    _assert_frozen_bible_run_closure(
        list(run.stages.select_for_update().order_by("ordinal")),
    )
    stage = StageRun.objects.select_for_update().filter(
        production_run=run,
        stage_key=stage_key,
    ).first()
    if stage is None:
        raise ValidationError({"stage_key": "ProductionRun 不包含该阶段"})
    unattended_prompt_validation_review = (
        _is_unattended_run(run)
        and run.status == ProductionRun.Status.AWAITING_APPROVAL
        and run.current_stage_key == "script_normalize_review"
        and stage.stage_key == "script_normalize_review"
        and stage.status == StageRun.Status.AWAITING_APPROVAL
        and stage.reason_code == PROMPT_VALIDATION_REVIEW_REQUIRED
    )
    if _is_unattended_run(run) and not unattended_prompt_validation_review:
        raise ValidationError(
            "unattended ProductionRun 只允许在提示词校验重试耗尽后写入人工 ApprovalDecision。",
        )
    if stage.status != StageRun.Status.AWAITING_APPROVAL:
        raise ValidationError("该阶段当前不处于 awaiting_approval")
    if decision not in ApprovalDecision.Decision.values:
        raise ValidationError("decision 必须为 approve/reject")
    comment = normalize_optional_audit_comment(comment)
    if decision == ApprovalDecision.Decision.APPROVE:
        if stage.stage_key == "storyboard_review":
            _approve_storyboard_reference_bindings(
                run,
                user=user,
                selections=list(shot_reference_bindings or []),
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
            )
        elif expected_source_snapshot_sha256 and stage.stage_key not in {
            "script_normalize_review", "visual_rules_review", BIBLE_IMAGE_REVIEW_STAGE_KEY,
        }:
            raise ValidationError(
                "expected_source_snapshot_sha256 只允许用于 storyboard_review、"
                "script_normalize_review、visual_rules_review 或 bible_image_review"
            )
        if stage.stage_key == "storyboard_review":
            if candidate_overrides or adopt_generated_metadata or bible_image_bindings:
                raise ValidationError(
                    "storyboard_review 只接受逐镜 reference_bindings 与来源快照 hash",
                )
        elif stage.stage_key == "script_review":
            if candidate_overrides:
                raise ValidationError(
                    "只有 script_normalize_review/visual_rules_review 支持 candidate_overrides",
                )
            _approve_generated_script(
                run,
                stage,
                user,
                adopt_generated_metadata=adopt_generated_metadata,
            )
        elif stage.stage_key == "script_normalize_review":
            if adopt_generated_metadata:
                raise ValidationError("adopt_generated_metadata 只允许用于 script_review")
            _approve_prompt_normalization(
                run,
                user,
                candidate_overrides=candidate_overrides,
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
            )
        elif stage.stage_key == "visual_rules_review":
            if adopt_generated_metadata or bible_image_bindings or shot_reference_bindings:
                raise ValidationError(
                    "visual_rules_review 只接受三项文本候选与来源快照 hash",
                )
            _approve_visual_rules(
                run,
                stage,
                candidate_overrides=candidate_overrides,
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
            )
        elif stage.stage_key == "bible_review" and run.stages.filter(
            stage_key="bible_materialize",
        ).exists():
            if candidate_overrides:
                raise ValidationError(
                    "只有 script_normalize_review/visual_rules_review 支持 candidate_overrides",
                )
            if adopt_generated_metadata:
                raise ValidationError("adopt_generated_metadata 只允许用于 script_review")
            _approve_bible_candidates(run)
        elif stage.stage_key == BIBLE_IMAGE_REVIEW_STAGE_KEY:
            if candidate_overrides or adopt_generated_metadata:
                raise ValidationError("Bible 参考图审核不接受文本候选覆盖或项目元数据采用")
            _approve_bible_image_bindings(
                run,
                stage,
                user,
                bindings=list(bible_image_bindings or []),
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
                asset_guard=asset_guard,
            )
        elif (
            candidate_overrides
            or adopt_generated_metadata
            or bible_image_bindings
            or shot_reference_bindings
        ):
            raise ValidationError(
                "candidate_overrides 只允许用于 script_normalize_review/visual_rules_review；"
                "adopt_generated_metadata 只允许用于 script_review；"
                "bible_image_bindings 只允许用于 bible_image_review；"
                "shot_reference_bindings 只允许用于 storyboard_review",
            )
        _assert_stage_output_integrity(run, stage)
        output_snapshot_sha256 = _save_stage_snapshot(
            stage,
            "output_snapshot",
            _formal_snapshot(run, stage, "approval"),
        )
    else:
        if candidate_overrides:
            raise ValidationError("拒绝阶段时不能提交 candidate_overrides")
        if expected_source_snapshot_sha256:
            raise ValidationError("拒绝阶段时不能提交 expected_source_snapshot_sha256")
        if adopt_generated_metadata:
            raise ValidationError("拒绝阶段时不能采用生成的项目元数据")
        if bible_image_bindings:
            raise ValidationError("拒绝阶段时不能提交 bible_image_bindings")
        if shot_reference_bindings:
            raise ValidationError("拒绝阶段时不能提交 shot_reference_bindings")
        output_snapshot_sha256 = ""
    revision = (
        stage.approval_decisions.aggregate(value=Max("revision"))["value"]
        or 0
    ) + 1
    approval = ApprovalDecision.objects.create(
        stage_run=stage,
        revision=revision,
        decision=decision,
        comment=comment,
        actor=ApprovalDecision.Actor.HUMAN,
        decision_policy="",
        decision_policy_sha256="",
        decided_by=user,
        output_snapshot_sha256=output_snapshot_sha256,
    )
    stage.finished_at = timezone.now()
    if decision == ApprovalDecision.Decision.REJECT:
        if stage.stage_key == "script_review" and run.script_revision_id:
            GeneratedArtifact.objects.filter(
                production_run=run,
                generation_attempt_id=run.script_revision.origin_generation_attempt_id,
                kind=GeneratedArtifact.Kind.SCRIPT,
                status=GeneratedArtifact.Status.AVAILABLE,
            ).update(status=GeneratedArtifact.Status.REJECTED, updated_at=timezone.now())
        stage.status = StageRun.Status.FAILED
        stage.reason_code = "approval_rejected"
        stage.reason = comment or "阶段被拒绝"
        run.status = ProductionRun.Status.FAILED
        run.finished_at = stage.finished_at
        run.blocked_code = stage.reason_code
        run.blocked_reason = stage.reason
    else:
        stage.status = StageRun.Status.SUCCEEDED
        stage.output_reference = f"approval:{approval.id}"
        stage.reason_code = ""
        stage.reason = ""
        run.status = ProductionRun.Status.QUEUED
        run.blocked_code = ""
        run.blocked_reason = ""
    stage.save(update_fields=(
        "status", "output_reference", "reason_code", "reason", "finished_at", "updated_at",
    ))
    run.save(update_fields=(
        "status", "blocked_code", "blocked_reason", "finished_at", "updated_at",
    ))
    if decision == ApprovalDecision.Decision.REJECT:
        return run
    return advance_production_run(run)


@transaction.atomic
def resolve_uncertain_attempt(
    run: ProductionRun,
    *,
    attempt_id,
    outcome: str,
    note: str,
    user,
    provider_request_id: str = "",
    actual_cost: Decimal | None = None,
    cost_currency: str = "",
) -> ProductionRun:
    if not getattr(user, "is_staff", False):
        raise ValidationError("只有管理员可以核验不确定的远端提交")
    allowed = {
        GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED,
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED,
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED,
        GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED,
    }
    if outcome not in allowed:
        raise ValidationError({"outcome": "不支持的 attempt 核验结果"})
    normalized_note = str(note or "").strip()
    if not normalized_note:
        raise ValidationError({"note": "必须填写远端控制台或账单核验证据说明"})
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    attempt = GenerationAttempt.objects.select_for_update().select_related("stage_run").filter(
        pk=attempt_id,
        stage_run__production_run=run,
    ).first()
    if attempt is None:
        raise ValidationError({"attempt_id": "该运行不存在此 attempt"})
    if attempt.operation not in (
        {
            TEXT_OPERATION, SCRIPT_STRUCTURE_OPERATION, VISUAL_RULES_OPERATION,
            PROMPT_NORMALIZE_OPERATION,
        }
        | ASYNC_MEDIA_OPERATIONS
    ):
        raise ValidationError({"attempt_id": "该 attempt transport 不支持人工核验"})
    is_cost_resolution = outcome == GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED
    if run.status in TERMINAL_RUN_STATUSES and outcome in {
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED,
        GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED,
    }:
        raise ProductionRunRetryConflict("终态 ProductionRun 不能恢复执行或确认新费用")
    if is_cost_resolution:
        if (
            attempt.operation not in ASYNC_MEDIA_OPERATIONS
            or attempt.status != GenerationAttempt.Status.FAILED
            or attempt.error_code != "actual_cost_reconciliation_required"
            or not attempt.artifacts.exists()
        ):
            raise ProductionRunRetryConflict("该 attempt 当前不处于实际费用待对账状态")
        if actual_cost is None or Decimal(str(actual_cost)) < 0:
            raise ValidationError({"actual_cost": "必须提供非负实际费用"})
        normalized_currency = str(cost_currency or attempt.cost_currency or "").strip().upper()
        if Decimal(str(actual_cost)) > 0 and not normalized_currency:
            raise ValidationError({"cost_currency": "非零实际费用必须提供币种"})
    elif (
        attempt.status != GenerationAttempt.Status.FAILED
        or attempt.error_code != "uncertain_remote_response"
    ):
        raise ProductionRunRetryConflict("该 attempt 当前不处于待核验的不确定提交状态")
    normalized_request_id = str(provider_request_id or "").strip()
    if len(normalized_request_id) > 191:
        raise ValidationError({"provider_request_id": "provider request id 过长"})
    if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED:
        if attempt.operation not in ASYNC_MEDIA_OPERATIONS:
            raise ValidationError({"outcome": "只有 async media attempt 可恢复远端轮询"})
        if not normalized_request_id or len(normalized_request_id) > 191:
            raise ValidationError({"provider_request_id": "确认远端已提交时必须绑定 request_id"})
    if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
        if normalized_request_id:
            raise ValidationError({"provider_request_id": "确认未提交时不能绑定 request_id"})
        if actual_cost is not None and Decimal(str(actual_cost)) != 0:
            raise ValidationError({"actual_cost": "确认未提交时实际费用必须为空或 0"})
    resolution_cost: Decimal | None = None
    resolution_currency = str(cost_currency or attempt.cost_currency or "").strip().upper()
    if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED:
        if actual_cost is None:
            raise ValidationError({"actual_cost": "确认远端失败时必须核对实际费用（可填 0）"})
        try:
            resolution_cost = Decimal(str(actual_cost)).quantize(Decimal("0.000001"))
        except Exception as exc:
            raise ValidationError({"actual_cost": "实际费用必须是有限非负数"}) from exc
        if not resolution_cost.is_finite() or resolution_cost < 0:
            raise ValidationError({"actual_cost": "实际费用必须是有限非负数"})
        if resolution_cost > 0 and not resolution_currency:
            raise ValidationError({"cost_currency": "非零实际费用必须提供币种"})
        if run.cost_currency and resolution_currency and run.cost_currency != resolution_currency:
            raise ValidationError({"cost_currency": "实际费用币种与运行币种冲突"})
    provider_request_hash = (
        hashlib.sha256(
            (normalized_request_id or attempt.provider_request_id).encode("utf-8"),
        ).hexdigest()
        if (normalized_request_id or attempt.provider_request_id) else ""
    )
    evidence = {
        "schema_version": 1,
        "outcome": outcome,
        "attempt_id": str(attempt.id),
        "request_sha256": attempt.request_sha256,
        "request_snapshot_sha256": attempt.request_snapshot_sha256,
        "provider_request_id_sha256": provider_request_hash,
        "previous_error_code": attempt.error_code,
        "operator_note": normalized_note,
    }
    if is_cost_resolution:
        evidence.update({
            "actual_cost": str(Decimal(str(actual_cost)).quantize(Decimal("0.000001"))),
            "cost_currency": normalized_currency,
            "artifact_sha256": list(attempt.artifacts.order_by("manifest_key").values_list("sha256", flat=True)),
        })
    elif resolution_cost is not None:
        evidence.update({
            "actual_cost": str(resolution_cost),
            "cost_currency": resolution_currency,
        })
    if normalized_request_id:
        attempt.provider_request_id = normalized_request_id
    append_attempt_resolution(
        attempt,
        outcome=outcome,
        evidence=evidence,
        note=normalized_note,
        decided_by=user,
    )
    if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED:
        attempt.status = GenerationAttempt.Status.SUBMITTED
        attempt.remote_status = "operator_confirmed_submitted"
        attempt.submitted_at = attempt.submitted_at or timezone.now()
        attempt.next_poll_at = timezone.now()
        attempt.finished_at = None
        attempt.error_code = ""
        attempt.error_message = ""
    elif is_cost_resolution:
        amount = Decimal(str(actual_cost)).quantize(Decimal("0.000001"))
        if run.cost_currency and normalized_currency and run.cost_currency != normalized_currency:
            raise ValidationError({"cost_currency": "实际费用币种与运行币种冲突"})
        if attempt.actual_cost is None:
            attempt.actual_cost = amount
            attempt.cost_currency = normalized_currency
            run.actual_cost_total += amount
            if normalized_currency and not run.cost_currency:
                run.cost_currency = normalized_currency
        attempt.status = GenerationAttempt.Status.SUCCEEDED
        attempt.finished_at = timezone.now()
        attempt.error_code = ""
        attempt.error_message = ""
        if attempt.stage_run.stage_key != BIBLE_IMAGE_GENERATION_STAGE_KEY:
            first = attempt.artifacts.order_by("variant_index", "created_at").first()
            if first and not GeneratedArtifact.objects.filter(
                production_run=run,
                variant_group=first.variant_group,
                status=GeneratedArtifact.Status.SELECTED,
            ).exists():
                first.status = GeneratedArtifact.Status.SELECTED
                first.save(update_fields=("status", "updated_at"))
    else:
        attempt.error_code = "remote_absence_confirmed"
        attempt.error_message = "管理员已核验远端未形成可用结果；允许显式创建新的 attempt。"
        if outcome == GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED:
            released = attempt.committed_cost or Decimal("0")
            run.committed_cost_total = max(Decimal("0"), run.committed_cost_total - released)
            attempt.committed_cost = Decimal("0")
        elif outcome == GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED:
            reserved = attempt.committed_cost or Decimal("0")
            run.committed_cost_total = max(
                Decimal("0"), run.committed_cost_total - reserved,
            )
            attempt.committed_cost = Decimal("0")
            attempt.actual_cost = resolution_cost
            attempt.cost_currency = resolution_currency
            run.actual_cost_total += resolution_cost or Decimal("0")
            if resolution_currency and not run.cost_currency:
                run.cost_currency = resolution_currency
    attempt.save(update_fields=(
        "status", "provider_request_id", "remote_status", "submitted_at", "next_poll_at",
        "finished_at", "error_code", "error_message", "committed_cost", "actual_cost",
        "cost_currency", "updated_at",
    ))
    run.save(update_fields=("committed_cost_total", "actual_cost_total", "cost_currency", "updated_at"))
    stage = attempt.stage_run
    if run.status not in TERMINAL_RUN_STATUSES:
        resumable = outcome in {
            GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED,
            GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED,
        }
        stage.status = StageRun.Status.RUNNING if resumable else StageRun.Status.BLOCKED
        stage.reason_code = "" if resumable else attempt.error_code
        stage.reason = "" if resumable else attempt.error_message
        stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
        run.status = ProductionRun.Status.RUNNING if resumable else ProductionRun.Status.BLOCKED
        run.blocked_code = "" if resumable else attempt.error_code
        run.blocked_reason = "" if resumable else attempt.error_message
        run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
        if is_cost_resolution:
            return advance_production_run(run)
    return run


def _persist_media_retry_cost_gate(
    run: ProductionRun,
    stage: StageRun,
    exc: StageBlocked,
    *,
    retry_units: list[dict[str, str]],
) -> ProductionRun:
    stage.refresh_from_db()
    run.refresh_from_db()
    decision = dict(stage.routing_decision or {})
    decision.pop("active_media_retry_cost_authorization", None)
    decision["pending_media_retry"] = {
        "schema_version": 1,
        "units": retry_units,
        "gate_code": exc.code,
    }
    _save_routing_decision(stage, decision)
    stage.status = (
        StageRun.Status.AWAITING_APPROVAL
        if exc.awaiting_approval else StageRun.Status.BLOCKED
    )
    stage.reason_code = exc.code
    stage.reason = exc.reason
    stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
    run.status = (
        ProductionRun.Status.AWAITING_APPROVAL
        if exc.awaiting_approval else ProductionRun.Status.BLOCKED
    )
    run.blocked_code = exc.code
    run.blocked_reason = exc.reason
    run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
    return run


_BIBLE_REGENERATION_COST_GATE_CODES = {
    "unknown_cost",
    "budget_limit_exceeded",
    "external_budget_missing",
    "external_call_not_authorized",
    "cost_currency_missing",
}


def _normalized_bible_regeneration_entities(
    entities: list[dict[str, Any]],
) -> list[dict[str, str]]:
    maximum_entities = _bible_regeneration_entity_max_items()
    if not isinstance(entities, list) or len(entities) > maximum_entities:
        raise ValidationError({
            "entities": f"单次最多重新生成 {maximum_entities} 个 Bible 实体",
        })
    normalized: list[dict[str, str]] = []
    for item in entities:
        entity_type = str(item.get("entity_type") or "").strip().lower()
        if entity_type not in {"character", "scene", "prop"}:
            raise ValidationError({"entities": "Bible 重新生成实体类型无效"})
        try:
            entity_id = str(uuid.UUID(str(item.get("entity_id") or "")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError({"entities": "Bible 重新生成实体 ID 无效"}) from exc
        normalized.append({"entity_type": entity_type, "entity_id": entity_id})
    keys = [(item["entity_type"], item["entity_id"]) for item in normalized]
    if not keys or len(keys) != len(set(keys)):
        raise ValidationError({"entities": "必须提交至少一个且不能重复的 Bible 实体"})
    return sorted(normalized, key=lambda item: (item["entity_type"], item["entity_id"]))


def _bible_regeneration_request_contract(
    *,
    request_id: Any,
    expected_source_snapshot_sha256: str,
    entities: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    try:
        normalized_request_id = str(uuid.UUID(str(request_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError({"request_id": "重新生成 request_id 无效"}) from exc
    expected_sha256 = str(expected_source_snapshot_sha256 or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValidationError({
            "expected_source_snapshot_sha256": "页面候选快照 hash 无效",
        })
    payload = {
        "schema_version": 1,
        "request_id": normalized_request_id,
        "expected_source_snapshot_sha256": expected_sha256,
        "entities": _normalized_bible_regeneration_entities(entities),
    }
    return payload, _sha256(payload)


def _verify_bible_regeneration_source(
    run: ProductionRun,
    generation_stage: StageRun,
    *,
    expected_source_snapshot_sha256: str,
    entities: list[dict[str, str]],
) -> None:
    try:
        _assert_frozen_formal_objects(run)
    except StageBlocked as exc:
        raise ProductionRunConflict(
            {"detail": exc.reason},
            code=exc.code,
        ) from exc
    snapshot = _verify_stage_snapshot(generation_stage, "output_snapshot")
    public_sha256 = json_contract(
        redact_sensitive_bindings(snapshot),
        max_bytes=_stage_snapshot_max_bytes(),
    ).sha256
    if expected_source_snapshot_sha256 != public_sha256:
        raise ProductionRunConflict(
            {"detail": "页面展示的 Bible 参考图候选快照已过期，请刷新后重试。"},
            code="bible_image_snapshot_stale",
        )
    canonical = snapshot.get("canonical")
    if (
        snapshot.get("formal_object_type") != "bible_image_candidate_manifest"
        or not isinstance(canonical, dict)
        or canonical.get("generation_stage_id") != str(generation_stage.id)
        or _sha256(canonical) != snapshot.get("canonical_sha256")
    ):
        raise ProductionRunConflict(
            {"detail": "Bible 参考图生成快照无效，不能追加候选。"},
            code="bible_image_snapshot_invalid",
        )
    current = _canonical_bible_image_candidates(run, generation_stage)
    if _sha256(current) != snapshot.get("canonical_sha256"):
        raise ProductionRunConflict(
            {"detail": "Bible 候选或其来源已变化，请刷新后重试。"},
            code="bible_image_candidate_manifest_stale",
        )
    available = {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
        for item in canonical.get("entities", [])
        if isinstance(item, dict)
    }
    requested = {(item["entity_type"], item["entity_id"]) for item in entities}
    if not requested.issubset(available):
        raise ValidationError({"entities": "重新生成目标不属于当前冻结候选清单"})


def _next_bible_regeneration_variants(
    generation_stage: StageRun,
    *,
    entities: list[dict[str, str]],
) -> dict[tuple[str, str], list[int]]:
    used: dict[tuple[str, str], set[int]] = {}
    active_statuses = {
        GenerationAttempt.Status.QUEUED,
        GenerationAttempt.Status.RUNNING,
        GenerationAttempt.Status.SUBMITTING,
        GenerationAttempt.Status.SUBMITTED,
        GenerationAttempt.Status.CANCEL_REQUESTED,
    }
    for attempt in generation_stage.generation_attempts.select_for_update().order_by(
        "unit_key", "attempt_number",
    ):
        try:
            identity = parse_bible_candidate_unit_key(attempt.unit_key)
        except ValidationError as exc:
            raise ProductionRunConflict(
                {"detail": "Bible 生成阶段包含无效的候选单元 identity。"},
                code="bible_regeneration_attempt_identity_invalid",
            ) from exc
        if attempt.status in active_statuses:
            raise ProductionRunConflict(
                {"detail": "Bible 生成阶段仍有执行中的任务，不能重复追加候选。"},
                code="bible_regeneration_already_running",
            )
        key = (identity["entity_type"], identity["entity_id"])
        used.setdefault(key, set()).add(identity["variant_number"])
    result: dict[tuple[str, str], list[int]] = {}
    for item in entities:
        key = (item["entity_type"], item["entity_id"])
        previous = used.get(key, set())
        next_variant = (max(previous) if previous else 0) + 1
        max_candidates = _bible_candidate_max_items()
        if next_variant > max_candidates:
            raise ProductionRunConflict(
                {"detail": (
                    f"Bible 实体 {key[0]}:{key[1]} 已达到每次运行最多 "
                    f"{max_candidates} 个候选；"
                    "如需更换模型或 PromptProfile，请新建 ProductionRun。"
                )},
                code="bible_regeneration_variant_limit_reached",
            )
        result[key] = [next_variant]
    return result


def _clear_bible_review_for_regeneration(review_stage: StageRun) -> None:
    _save_stage_snapshot(review_stage, "input_snapshot", {})
    _save_stage_snapshot(review_stage, "output_snapshot", {})
    review_stage.status = StageRun.Status.PENDING
    review_stage.output_reference = ""
    review_stage.reason_code = ""
    review_stage.reason = ""
    review_stage.finished_at = None
    review_stage.save(update_fields=(
        "status", "output_reference", "reason_code", "reason", "finished_at",
        "updated_at",
    ))


def _persist_bible_regeneration_cost_gate(
    run: ProductionRun,
    generation_stage: StageRun,
    review_stage: StageRun,
    exc: StageBlocked,
    *,
    intent: dict[str, Any],
) -> ProductionRun:
    generation_stage.refresh_from_db()
    review_stage.refresh_from_db()
    run.refresh_from_db()
    decision = dict(generation_stage.routing_decision or {})
    decision["pending_bible_regeneration"] = {
        **deepcopy(intent),
        "gate_code": exc.code,
    }
    _save_routing_decision(generation_stage, decision)
    _append_route_event(
        generation_stage,
        unit_key=f"bible-regenerate:{intent['request_id']}",
        event_type="bible_regeneration_pending",
        code=intent["request_sha256"],
    )
    _clear_bible_review_for_regeneration(review_stage)
    generation_stage.status = StageRun.Status.AWAITING_APPROVAL
    generation_stage.output_reference = ""
    generation_stage.reason_code = exc.code
    generation_stage.reason = exc.reason
    generation_stage.finished_at = None
    generation_stage.save(update_fields=(
        "status", "output_reference", "reason_code", "reason", "finished_at",
        "updated_at",
    ))
    run.status = ProductionRun.Status.AWAITING_APPROVAL
    run.current_stage_key = BIBLE_IMAGE_GENERATION_STAGE_KEY
    run.blocked_code = exc.code
    run.blocked_reason = exc.reason
    run.finished_at = None
    run.save(update_fields=(
        "status", "current_stage_key", "blocked_code", "blocked_reason",
        "finished_at", "updated_at",
    ))
    return run


def _materialize_bible_regeneration(
    run: ProductionRun,
    generation_stage: StageRun,
    review_stage: StageRun,
    *,
    intent: dict[str, Any],
    approved_unknown_cost_total: Decimal | None,
    approved_unknown_cost_currency: str,
) -> ProductionRun:
    variant_numbers = {
        (str(item["entity_type"]), str(item["entity_id"])): [
            int(item["variant_number"]),
        ]
        for item in intent["targets"]
    }
    savepoint = transaction.savepoint()
    try:
        attempts = _create_media_tasks(
            run,
            generation_stage,
            bible_variant_numbers=variant_numbers,
            generation_intent={
                "schema_version": 1,
                "kind": "human_regenerate",
                "request_id": intent["request_id"],
                "requested_by_id": intent["requested_by_id"],
                "previous_generation_snapshot_sha256": intent[
                    "expected_source_snapshot_sha256"
                ],
                "request_sha256": intent["request_sha256"],
            },
            approved_unknown_cost_total=approved_unknown_cost_total,
            approved_unknown_cost_currency=approved_unknown_cost_currency,
        )
    except StageBlocked as exc:
        transaction.savepoint_rollback(savepoint)
        if exc.code in _BIBLE_REGENERATION_COST_GATE_CODES:
            return _persist_bible_regeneration_cost_gate(
                run,
                generation_stage,
                review_stage,
                exc,
                intent=intent,
            )
        raise ProductionRunConflict(
            {"detail": exc.reason},
            code=exc.code,
        ) from exc
    except Exception:
        transaction.savepoint_rollback(savepoint)
        raise
    transaction.savepoint_commit(savepoint)
    if len(attempts) != len(variant_numbers):
        raise ProductionRunConflict(
            {"detail": "Bible 追加候选没有完整物化，事务已回滚。"},
            code="bible_regeneration_materialization_incomplete",
        )
    decision = dict(generation_stage.routing_decision or {})
    decision.pop("pending_bible_regeneration", None)
    decision["last_bible_regeneration"] = deepcopy(intent)
    _save_routing_decision(generation_stage, decision)
    _append_route_event(
        generation_stage,
        unit_key=f"bible-regenerate:{intent['request_id']}",
        event_type="bible_regeneration_queued",
        code=intent["request_sha256"],
    )
    _clear_bible_review_for_regeneration(review_stage)
    generation_stage.status = StageRun.Status.PENDING
    generation_stage.output_reference = ""
    generation_stage.reason_code = ""
    generation_stage.reason = ""
    generation_stage.finished_at = None
    generation_stage.save(update_fields=(
        "status", "output_reference", "reason_code", "reason", "finished_at",
        "updated_at",
    ))
    run.status = ProductionRun.Status.QUEUED
    run.current_stage_key = BIBLE_IMAGE_GENERATION_STAGE_KEY
    run.blocked_code = ""
    run.blocked_reason = ""
    run.finished_at = None
    run.save(update_fields=(
        "status", "current_stage_key", "blocked_code", "blocked_reason",
        "finished_at", "updated_at",
    ))
    return advance_production_run(run)


@transaction.atomic
def regenerate_bible_image_candidates(
    run: ProductionRun,
    *,
    request_id: Any,
    expected_source_snapshot_sha256: str,
    entities: list[dict[str, Any]],
    user,
) -> ProductionRun:
    request, request_sha256 = _bible_regeneration_request_contract(
        request_id=request_id,
        expected_source_snapshot_sha256=expected_source_snapshot_sha256,
        entities=entities,
    )
    run = ProductionRun.objects.select_for_update().select_related(
        "project", "project__owner",
    ).get(pk=run.pk)
    if not getattr(user, "is_staff", False) and user.pk != run.project.owner_id:
        raise ValidationError("无权为该项目重新生成 Bible 候选")
    generation_stage = run.stages.select_for_update().filter(
        stage_key=BIBLE_IMAGE_GENERATION_STAGE_KEY,
    ).first()
    review_stage = run.stages.select_for_update().filter(
        stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
    ).first()
    if generation_stage is None or review_stage is None:
        raise ValidationError("ProductionRun 不包含完整 Bible 参考图生成/审核阶段")
    event_unit_key = f"bible-regenerate:{request['request_id']}"
    previous_events = list(generation_stage.route_events.select_for_update().filter(
        unit_key=event_unit_key,
        event_type__in=(
            "bible_regeneration_pending",
            "bible_regeneration_queued",
        ),
    ).order_by("sequence"))
    if previous_events:
        if any(item.code != request_sha256 for item in previous_events):
            raise ProductionRunConflict(
                {"detail": "同一 request_id 已绑定另一份 Bible 重新生成请求。"},
                code="bible_regeneration_idempotency_conflict",
            )
        return run
    if _is_unattended_run(run):
        raise ValidationError("全自动运行不能注入人工 Bible 候选重新生成请求")
    if (
        run.status != ProductionRun.Status.AWAITING_APPROVAL
        or run.current_stage_key != BIBLE_IMAGE_REVIEW_STAGE_KEY
        or generation_stage.status != StageRun.Status.SUCCEEDED
        or review_stage.status != StageRun.Status.AWAITING_APPROVAL
    ):
        raise ProductionRunConflict(
            {"detail": "只有 bible_image_review 待人工审核时才能追加模型候选。"},
            code="bible_regeneration_state_conflict",
        )
    if review_stage.approval_decisions.exists():
        raise ProductionRunConflict(
            {"detail": "Bible 审核已产生决定，不能再向该审核快照追加候选。"},
            code="bible_regeneration_review_already_decided",
        )
    _verify_bible_regeneration_source(
        run,
        generation_stage,
        expected_source_snapshot_sha256=request[
            "expected_source_snapshot_sha256"
        ],
        entities=request["entities"],
    )
    variants = _next_bible_regeneration_variants(
        generation_stage,
        entities=request["entities"],
    )
    intent = {
        **request,
        "request_sha256": request_sha256,
        "requested_by_id": str(user.pk),
        "targets": [
            {
                **item,
                "variant_number": variants[(item["entity_type"], item["entity_id"])][0],
            }
            for item in request["entities"]
        ],
    }
    return _materialize_bible_regeneration(
        run,
        generation_stage,
        review_stage,
        intent=intent,
        # A prior initial-generation unknown-cost approval is not permission
        # for extra spend.  Unknown-cost regeneration always opens a fresh gate.
        approved_unknown_cost_total=None,
        approved_unknown_cost_currency="",
    )


@transaction.atomic
def retry_production_run(run: ProductionRun, *, user) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    if run.status not in {ProductionRun.Status.BLOCKED, ProductionRun.Status.FAILED}:
        raise ValidationError("只有 blocked/failed ProductionRun 可以 retry")
    stage = run.stages.select_for_update().filter(stage_key=run.current_stage_key).first()
    if stage is None or stage.status not in {StageRun.Status.BLOCKED, StageRun.Status.FAILED}:
        raise ValidationError("当前阶段不可 retry")
    if stage.stage_key in {
        "script_generate", "script_structure", "visual_rules_generate", "script_normalize",
    }:
        unit_key = {
            "script_generate": TEXT_UNIT_KEY,
            "script_structure": SCRIPT_STRUCTURE_UNIT_KEY,
            "visual_rules_generate": VISUAL_RULES_UNIT_KEY,
            "script_normalize": PROMPT_NORMALIZE_UNIT_KEY,
        }[stage.stage_key]
        latest_query = (
            stage.generation_attempts.filter(operation=PROMPT_NORMALIZE_OPERATION)
            if stage.stage_key == "script_normalize"
            else stage.generation_attempts.filter(unit_key=unit_key)
        )
        latest = latest_query.order_by("-created_at", "-id").first()
        if latest is None:
            refreshable_preflight_codes = {
                "script_structure": {
                    "script_structure_smoke_invalid",
                    "script_structure_smoke_missing_or_stale",
                    "script_structure_smoke_drift",
                },
                "visual_rules_generate": {
                    "visual_rules_smoke_invalid",
                    "visual_rules_smoke_missing_or_stale",
                    "visual_rules_smoke_drift",
                },
                "script_normalize": {
                    "prompt_normalize_smoke_invalid",
                    "prompt_normalize_smoke_required",
                    "prompt_normalize_smoke_drift",
                    # The provider boundary was never crossed, so an explicit
                    # operator retry may safely rebuild deterministic input
                    # after a schema/configuration repair.
                    "prompt_normalize_input_invalid",
                },
            }
            if stage.reason_code not in refreshable_preflight_codes.get(stage.stage_key, set()):
                raise ValidationError("当前文本阶段没有可安全重跑的未提交 attempt")
            # No GenerationAttempt means the provider submission boundary was
            # never crossed.  Re-enter preflight; the queue helper still admits
            # only a fresh smoke whose full target/config fingerprint equals the
            # immutable route and records any newer evidence in the stage audit.
            stage.status = StageRun.Status.PENDING
            stage.reason_code = ""
            stage.reason = ""
            stage.finished_at = None
            stage.save(update_fields=(
                "status", "reason_code", "reason", "finished_at", "updated_at",
            ))
            run.status = ProductionRun.Status.QUEUED
            run.blocked_code = ""
            run.blocked_reason = ""
            run.finished_at = None
            run.save(update_fields=(
                "status", "blocked_code", "blocked_reason", "finished_at", "updated_at",
            ))
            return advance_production_run(run)
        if latest.status != GenerationAttempt.Status.FAILED:
            raise ValidationError("当前文本 attempt 不可 retry")
        if latest.error_code == "uncertain_remote_response":
            raise ProductionRunRetryConflict(
                "远端提交状态不确定，普通 retry 被禁止；必须由管理员先完成远端缺失核验。"
            )
        if (
            stage.stage_key == "script_normalize"
            and latest.error_code in PROMPT_VALIDATION_RETRYABLE_CODES
        ):
            # Validation failures re-enter the bounded policy engine. Do not
            # let the generic retry endpoint exceed the frozen retry limit.
            stage.status = StageRun.Status.RUNNING
            stage.reason_code = ""
            stage.reason = ""
            run.status = ProductionRun.Status.RUNNING
            run.blocked_code = ""
            run.blocked_reason = ""
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
            return advance_production_run(run)
        if stage.stage_key == "script_generate" and run.script_revision_id is not None:
            raise ValidationError("已存在 preview revision，不能再次提交文本模型")
        if _verify_stage_routing(stage).get("mode") == "ordered_fallback":
            stage.status = StageRun.Status.RUNNING
            stage.reason_code = ""
            stage.reason = ""
            run.status = ProductionRun.Status.RUNNING
            run.blocked_code = ""
            run.blocked_reason = ""
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
            return advance_production_run(run)
        if run.generation_task_count + 1 > _effective_generation_task_limit(run):
            raise ValidationError("retry 会超过 ProductionRun 任务预算")
        snapshot = deepcopy(latest.request_snapshot)
        next_number = latest.attempt_number + 1
        if stage.stage_key == "script_normalize":
            route = snapshot.get("route")
            if not isinstance(route, dict):
                raise ValidationError("prompt.normalize retry 缺少冻结 route")
            _queue_prompt_normalization_attempt_for_route(
                run,
                stage,
                route=route,
                attempt_number=next_number,
                reason="explicit_retry_after_resolution",
                frozen_batch_plan=_verify_prompt_normalization_batch_plan(
                    snapshot.get("batch_plan"),
                ),
                batch_index=snapshot.get("batch_index"),
                frozen_prompt_profile_version=latest.prompt_profile_version,
                frozen_prompt_profile_binding_key=latest.prompt_profile_binding_key,
                frozen_prompt_profile_contract_sha256=latest.prompt_profile_contract_sha256,
            )
        else:
            request = snapshot["plan"]["request"]
            if stage.stage_key == "script_generate":
                request["attempt"] = next_number
                request["request_id"] = f"{snapshot['plan']['source_fingerprint'][:20]}-{next_number}"
                request["seed"] = int(snapshot["plan"]["source_fingerprint"][:8], 16) + next_number
                retry_unit_key = TEXT_UNIT_KEY
                retry_operation = TEXT_OPERATION
                retry_payload = text_provider_payload(snapshot["plan"])
            elif stage.stage_key == "script_structure":
                request["seed"] = int(snapshot["plan"]["source_fingerprint"][:8], 16) + next_number
                retry_unit_key = SCRIPT_STRUCTURE_UNIT_KEY
                retry_operation = SCRIPT_STRUCTURE_OPERATION
                retry_payload = structure_provider_payload(snapshot["plan"])
            else:
                source_sha256 = str(
                    snapshot["plan"]["source_revision"]["content_sha256"]
                )
                request["seed"] = int(source_sha256[:8], 16) + next_number
                retry_unit_key = VISUAL_RULES_UNIT_KEY
                retry_operation = VISUAL_RULES_OPERATION
                retry_payload = visual_rules_provider_payload(snapshot["plan"])
            contract = json_contract(snapshot, max_bytes=_text_snapshot_max_bytes())
            new_attempt = _create_generation_attempt(
                stage_run=stage,
                unit_key=retry_unit_key,
                attempt_number=next_number,
                model_deployment=latest.model_deployment,
                prompt_profile_version=latest.prompt_profile_version,
                prompt_profile_binding_key=latest.prompt_profile_binding_key,
                prompt_profile_contract_sha256=latest.prompt_profile_contract_sha256,
                route_index=latest.route_index,
                operation=retry_operation,
                is_external=latest.is_external,
                status=GenerationAttempt.Status.QUEUED,
                request_sha256=contract.sha256,
                request_snapshot=contract.value,
                request_snapshot_schema_version=contract.schema_version,
                request_snapshot_size_bytes=contract.size_bytes,
                request_snapshot_sha256=contract.sha256,
            )
            _estimate_and_reserve(
                run, stage, new_attempt,
                deployment=frozen_text_deployment(snapshot),
                operation=retry_operation,
                payload=retry_payload,
                is_external=latest.is_external,
            )
            run.generation_task_count += 1
        stage.status = StageRun.Status.PENDING
        stage.reason_code = ""
        stage.reason = ""
        run.status = ProductionRun.Status.QUEUED
        run.blocked_code = ""
        run.blocked_reason = ""
        stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
        run.save(update_fields=(
            "generation_task_count", "status", "blocked_code", "blocked_reason", "updated_at",
        ))
        return advance_production_run(run)
    if stage.stage_key in {"media_generation", BIBLE_IMAGE_GENERATION_STAGE_KEY}:
        # Retry is a new submission decision. Re-evaluate the frozen run prompts
        # so a historical run cannot bypass the same executable-conditioning and
        # language gates used for the initial materialization path.
        try:
            prompts = (
                _bible_prompt_rows_for_stage(stage)
                if stage.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY
                else _prompt_rows_for_run(run)
            )
            if stage.stage_key != BIBLE_IMAGE_GENERATION_STAGE_KEY:
                _enforce_distinct_shot_conditioning(prompts)
            _enforce_prompt_language_gate(prompts)
        except StageBlocked as exc:
            stage.status = (
                StageRun.Status.AWAITING_APPROVAL
                if exc.awaiting_approval else StageRun.Status.BLOCKED
            )
            stage.reason_code = exc.code
            stage.reason = exc.reason
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.status = (
                ProductionRun.Status.AWAITING_APPROVAL
                if exc.awaiting_approval else ProductionRun.Status.BLOCKED
            )
            run.blocked_code = exc.code
            run.blocked_reason = exc.reason
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
            return run
        latest: dict[str, GenerationAttempt] = {}
        for attempt in stage.generation_attempts.select_related("generation_task").order_by(
            "unit_key", "-attempt_number",
        ):
            latest.setdefault(attempt.unit_key, attempt)
        if not latest:
            # A media preflight can fail before the first durable attempt is
            # materialized (for example, while resolving a fresh runtime-host
            # capability snapshot).  With no GenerationAttempt there is a
            # definitive no-submission boundary, so an explicit retry may
            # safely rerun the stage preflight instead of requiring a failed
            # remote unit that cannot exist.  Returning through advance keeps
            # the retry visible in StageRun.attempt_count.
            stage.status = StageRun.Status.PENDING
            stage.reason_code = ""
            stage.reason = ""
            stage.finished_at = None
            stage.save(update_fields=(
                "status", "reason_code", "reason", "finished_at", "updated_at",
            ))
            run.status = ProductionRun.Status.QUEUED
            run.blocked_code = ""
            run.blocked_reason = ""
            run.finished_at = None
            run.save(update_fields=(
                "status", "blocked_code", "blocked_reason", "finished_at", "updated_at",
            ))
            return advance_production_run(run)
        ordered_failures = [
            attempt for attempt in latest.values()
            if attempt.status == GenerationAttempt.Status.FAILED
            or (
                attempt.generation_task is not None
                and attempt.generation_task.status == GenerationTask.Status.FAILED
            )
        ]
        if _verify_stage_routing(stage).get("mode") == "ordered_fallback" and ordered_failures:
            for attempt in ordered_failures:
                task = attempt.generation_task
                if (
                    attempt.status != GenerationAttempt.Status.FAILED
                    and task is not None
                    and task.status == GenerationTask.Status.FAILED
                ):
                    attempt.status = GenerationAttempt.Status.FAILED
                    attempt.error_code = "generation_task_terminal_failure"
                    attempt.error_message = str(
                        task.error_message or task.remote_error or task.status,
                    )[:2000]
                    attempt.finished_at = task.finished_at or timezone.now()
                    attempt.save(update_fields=(
                        "status", "error_code", "error_message", "finished_at", "updated_at",
                    ))
                definitive, code = _definitive_media_failure(attempt)
                if not definitive:
                    raise ProductionRunRetryConflict(
                        f"ordered_fallback 的失败状态不具备安全换路由边界: {code}",
                    )
            stage.status = StageRun.Status.RUNNING
            stage.reason_code = ""
            stage.reason = ""
            run.status = ProductionRun.Status.RUNNING
            run.blocked_code = ""
            run.blocked_reason = ""
            stage.save(update_fields=("status", "reason_code", "reason", "updated_at"))
            run.save(update_fields=("status", "blocked_code", "blocked_reason", "updated_at"))
            return advance_production_run(run)
        retry_units = [
            {
                "unit_key": unit_key,
                "attempt_id": str(attempt.id),
                "transport": (
                    "async_rest"
                    if attempt.operation in ASYNC_MEDIA_OPERATIONS else "comfyui"
                ),
            }
            for unit_key, attempt in latest.items()
            if (
                attempt.status == GenerationAttempt.Status.FAILED
                if attempt.operation in ASYNC_MEDIA_OPERATIONS
                else attempt.generation_task is not None
                and attempt.generation_task.status in {
                    GenerationTask.Status.FAILED,
                    GenerationTask.Status.CANCELLED,
                }
            )
        ]
        retry_decision = dict(stage.routing_decision or {})
        retry_authorization = retry_decision.get(
            "active_media_retry_cost_authorization",
        )
        if not isinstance(retry_authorization, dict):
            retry_authorization = {}
        if retry_authorization and retry_authorization.get("units_sha256") != _sha256(retry_units):
            raise ProductionRunRetryConflict("retry 费用授权的冻结单元已变化，请重新授权。")
        retry_authorized_total = _decimal_amount(
            retry_authorization.get("total_amount"),
        ) if retry_authorization else None
        retry_authorized_currency = str(
            retry_authorization.get("currency") or "",
        )
        retry_cost_unit_index = 0
        retry_savepoint = transaction.savepoint()
        retried = 0
        new_attempt_count = 0
        for unit_key, attempt in latest.items():
            if attempt.operation in ASYNC_MEDIA_OPERATIONS:
                if attempt.status != GenerationAttempt.Status.FAILED:
                    continue
                if attempt.error_code == "uncertain_remote_response":
                    raise ProductionRunRetryConflict(
                        "远端提交状态不确定；必须先确认未提交、远端失败或绑定 request_id 恢复轮询。"
                    )
                if attempt.error_code == "actual_cost_reconciliation_required":
                    raise ProductionRunRetryConflict("产物已归档但实际费用未知；必须先完成费用对账。")
                verify_json_contract(
                    attempt.request_snapshot,
                    schema_version=attempt.request_snapshot_schema_version,
                    size_bytes=attempt.request_snapshot_size_bytes,
                    sha256=attempt.request_snapshot_sha256,
                    max_bytes=_stage_snapshot_max_bytes(),
                )
                if attempt.request_sha256 != attempt.request_snapshot_sha256:
                    raise ValidationError("原 async 请求 identity 与冻结快照不一致")
                if attempt.provider_request_id and attempt.error_code in {
                    "artifact_download_failed", "artifact_archive_failed", "artifact_invalid",
                    "provider_collect_failed", "artifact_response_invalid",
                }:
                    attempt.status = GenerationAttempt.Status.SUBMITTED
                    attempt.next_poll_at = timezone.now()
                    attempt.finished_at = None
                    attempt.error_code = ""
                    attempt.error_message = ""
                    attempt.save(update_fields=(
                        "status", "next_poll_at", "finished_at", "error_code", "error_message", "updated_at",
                    ))
                    retried += 1
                    continue
                if attempt.error_code not in {
                    "remote_absence_confirmed", "remote_generation_failed", "remote_cancelled",
                    "provider_request_rejected",
                }:
                    raise ProductionRunRetryConflict(
                        "该 async attempt 尚无可证明安全的新提交边界；禁止自动再次 POST。"
                    )
                if (
                    run.generation_task_count + new_attempt_count + 1
                    > _effective_generation_task_limit(run)
                ):
                    raise ValidationError("retry 会超过 ProductionRun 任务预算")
                snapshot = deepcopy(attempt.request_snapshot)
                next_attempt_number = attempt.attempt_number + 1
                new_id = uuid.uuid4()
                snapshot["payload"]["idempotency_key"] = f"production-attempt:{new_id}"
                snapshot["variant_number"] = snapshot.get("variant_number") or next_attempt_number
                request_contract = json_contract(snapshot, max_bytes=_stage_snapshot_max_bytes())
                new_attempt = _create_generation_attempt(
                    id=new_id,
                    stage_run=stage,
                    unit_key=unit_key,
                    attempt_number=next_attempt_number,
                    model_deployment=attempt.model_deployment,
                    prompt_profile_version=attempt.prompt_profile_version,
                    prompt_profile_binding_key=attempt.prompt_profile_binding_key,
                    prompt_profile_contract_sha256=attempt.prompt_profile_contract_sha256,
                    route_index=attempt.route_index,
                    operation=attempt.operation,
                    is_external=attempt.is_external,
                    status=GenerationAttempt.Status.QUEUED,
                    request_sha256=request_contract.sha256,
                    request_snapshot=request_contract.value,
                    request_snapshot_schema_version=request_contract.schema_version,
                    request_snapshot_size_bytes=request_contract.size_bytes,
                    request_snapshot_sha256=request_contract.sha256,
                )
                retry_cost_unit_index += 1
                try:
                    _estimate_and_reserve(
                        run, stage, new_attempt,
                        deployment=frozen_async_deployment(snapshot["route"]),
                        operation=attempt.operation,
                        payload=snapshot["payload"],
                        is_external=attempt.is_external,
                        approved_unknown_cost=_allocated_stage_unknown_cost(
                            retry_authorized_total,
                            unit_index=retry_cost_unit_index,
                            unit_count=max(1, len(retry_units)),
                        ),
                        approved_unknown_currency=retry_authorized_currency,
                    )
                except StageBlocked as exc:
                    transaction.savepoint_rollback(retry_savepoint)
                    return _persist_media_retry_cost_gate(
                        run,
                        stage,
                        exc,
                        retry_units=retry_units,
                    )
                retried += 1
                new_attempt_count += 1
                continue
            task = attempt.generation_task
            if task is None or task.status not in {
                GenerationTask.Status.FAILED,
                GenerationTask.Status.CANCELLED,
            }:
                continue
            if task.outputs:
                raise ValidationError(
                    "失败任务已保留远端 outputs，不能自动创建可能重复的 GPU retry；请人工核验。"
                )
            safe_comfy_retry = (
                (
                    task.submission_state == GenerationTask.SubmissionState.UNPREPARED
                    and not task.submission_token
                    and not task.comfy_prompt_id
                )
                or task.submission_state == GenerationTask.SubmissionState.TERMINAL_FAILED
            )
            if not safe_comfy_retry:
                raise ProductionRunRetryConflict(
                    "ComfyUI 提交状态未证明为未提交或明确终态失败；禁止创建新远端任务。"
                )
            next_attempt = attempt.attempt_number + 1
            if (
                run.generation_task_count + retried + 1
                > _effective_generation_task_limit(run)
            ):
                raise ValidationError("retry 会超过 ProductionRun 任务预算")
            try:
                verify_json_contract(
                    attempt.request_snapshot,
                    schema_version=attempt.request_snapshot_schema_version,
                    size_bytes=attempt.request_snapshot_size_bytes,
                    sha256=attempt.request_snapshot_sha256,
                    max_bytes=_stage_snapshot_max_bytes(),
                )
            except (ValidationError, TypeError, ValueError) as exc:
                raise ValidationError("原媒体请求快照缺失或已漂移，不能安全 retry") from exc
            if attempt.request_sha256 != attempt.request_snapshot_sha256:
                raise ValidationError("原媒体请求 identity 与冻结快照不一致")
            new_attempt = _create_generation_attempt(
                stage_run=stage,
                unit_key=unit_key,
                attempt_number=next_attempt,
                model_deployment=attempt.model_deployment,
                prompt_profile_version=attempt.prompt_profile_version,
                prompt_profile_binding_key=attempt.prompt_profile_binding_key,
                prompt_profile_contract_sha256=attempt.prompt_profile_contract_sha256,
                route_index=attempt.route_index,
                operation=attempt.operation,
                is_external=attempt.is_external,
                status=GenerationAttempt.Status.QUEUED,
                request_sha256=attempt.request_sha256,
                request_snapshot=deepcopy(attempt.request_snapshot),
                request_snapshot_schema_version=attempt.request_snapshot_schema_version,
                request_snapshot_size_bytes=attempt.request_snapshot_size_bytes,
                request_snapshot_sha256=attempt.request_snapshot_sha256,
            )
            retry_cost_unit_index += 1
            try:
                _estimate_and_reserve(
                    run,
                    stage,
                    new_attempt,
                    deployment=attempt.model_deployment,
                    operation=attempt.operation or "media.generate",
                    payload={
                        "schema_version": 1,
                        "retry_of": str(task.id),
                        "attempt_number": next_attempt,
                    },
                    is_external=attempt.is_external,
                    approved_unknown_cost=_allocated_stage_unknown_cost(
                        retry_authorized_total,
                        unit_index=retry_cost_unit_index,
                        unit_count=max(1, len(retry_units)),
                    ),
                    approved_unknown_currency=retry_authorized_currency,
                )
            except StageBlocked as exc:
                transaction.savepoint_rollback(retry_savepoint)
                return _persist_media_retry_cost_gate(
                    run,
                    stage,
                    exc,
                    retry_units=retry_units,
                )
            new_task = clone_generation_task_for_retry(
                task,
                user=run.project.owner,
                generation_attempt_id=new_attempt.id,
                extended_generation_confirmed=True,
            )
            new_attempt.generation_task = new_task
            new_attempt.save(update_fields=("generation_task", "updated_at"))
            retried += 1
            new_attempt_count += 1
        if not retried:
            transaction.savepoint_rollback(retry_savepoint)
            raise ValidationError("没有可安全自动 retry 的生成任务")
        transaction.savepoint_commit(retry_savepoint)
        if retry_authorization:
            refreshed_decision = dict(stage.routing_decision or {})
            refreshed_decision.pop("active_media_retry_cost_authorization", None)
            _save_routing_decision(stage, refreshed_decision)
        run.generation_task_count += new_attempt_count
        run.save(update_fields=("generation_task_count", "updated_at"))
        stage.status = StageRun.Status.RUNNING
    elif stage.stage_key == "assembly":
        from studio.models import AssemblyJob

        failed_links = list(
            ProductionAssemblyLink.objects.select_for_update()
            .select_related("episode", "assembly_job")
            .filter(
                production_run=run,
                stage_run=stage,
                assembly_job__status=AssemblyJob.Status.FAILED,
            )
            .order_by("episode__number")
        )
        if not failed_links:
            raise ValidationError("没有可显式 retry 的失败 AssemblyJob")
        selected_sources = _selected_assembly_sources(run)
        for link in failed_links:
            try:
                job = create_assembly(
                    owner=run.project.owner,
                    project=run.project,
                    episode=link.episode,
                    dry_run=False,
                    source_refs_by_shot=selected_sources,
                )
            except AssemblyError as exc:
                stage.status = StageRun.Status.BLOCKED
                stage.reason_code = "assembly_enqueue_failed"
                stage.reason = str(exc)[:2000]
                stage.save(update_fields=(
                    "status", "reason_code", "reason", "updated_at",
                ))
                run.status = ProductionRun.Status.BLOCKED
                run.blocked_code = stage.reason_code
                run.blocked_reason = stage.reason
                run.save(update_fields=(
                    "status", "blocked_code", "blocked_reason", "updated_at",
                ))
                return run
            link.assembly_job = job
            link.save(update_fields=("assembly_job", "updated_at"))
        stage.status = StageRun.Status.PENDING
    else:
        stage.status = StageRun.Status.PENDING
    stage.reason_code = ""
    stage.reason = ""
    stage.finished_at = None
    stage.save(update_fields=("status", "reason_code", "reason", "finished_at", "updated_at"))
    run.status = ProductionRun.Status.QUEUED
    run.blocked_code = ""
    run.blocked_reason = ""
    run.finished_at = None
    run.save(update_fields=(
        "status", "blocked_code", "blocked_reason", "finished_at", "updated_at",
    ))
    return advance_production_run(run)


@transaction.atomic
def cancel_production_run(run: ProductionRun, *, user=None) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    if run.status in TERMINAL_RUN_STATUSES:
        raise ValidationError("ProductionRun 已终态，不能取消")
    now = timezone.now()
    async_attempts = list(GenerationAttempt.objects.select_for_update().filter(
        stage_run__production_run=run,
        operation__in=ASYNC_MEDIA_OPERATIONS,
        status__in=(
            GenerationAttempt.Status.QUEUED,
            GenerationAttempt.Status.SUBMITTING,
            GenerationAttempt.Status.SUBMITTED,
            GenerationAttempt.Status.CANCEL_REQUESTED,
        ),
    ))
    for attempt in async_attempts:
        attempt.ignore_late_result = True
        attempt.cancel_requested_at = attempt.cancel_requested_at or now
        if attempt.status == GenerationAttempt.Status.QUEUED:
            released = attempt.committed_cost or Decimal("0")
            run.committed_cost_total = max(
                Decimal("0"), run.committed_cost_total - released,
            )
            attempt.committed_cost = Decimal("0")
            attempt.status = GenerationAttempt.Status.CANCELLED
            attempt.finished_at = now
            attempt.error_code = "production_run_cancelled_before_submit"
            attempt.error_message = "ProductionRun 在远端提交前取消。"
        elif attempt.status == GenerationAttempt.Status.SUBMITTED:
            attempt.status = GenerationAttempt.Status.CANCEL_REQUESTED
            attempt.next_poll_at = now
            attempt.error_code = "cancel_requested"
            attempt.error_message = "已请求 async provider 取消远端任务。"
        else:
            # Do not change SUBMITTING across the POST boundary. The worker will
            # atomically persist the returned request_id and transition to
            # CANCEL_REQUESTED; a stale submission remains explicitly uncertain.
            attempt.error_code = "cancel_requested_ignore_late"
            attempt.error_message = "提交边界处理中；远端响应将被取消或作为晚到结果忽略。"
        append_attempt_resolution(
            attempt,
            outcome=GenerationAttemptResolution.Outcome.CANCEL_REQUESTED_IGNORE_LATE,
            evidence={
                "schema_version": 1,
                "attempt_id": str(attempt.id),
                "request_sha256": attempt.request_sha256,
                "provider_request_id": attempt.provider_request_id,
                "policy": "remote_cancel_then_ignore_late",
            },
            note="ProductionRun cancelled for async media attempt",
            decided_by=user,
        )
        attempt.save(update_fields=(
            "status", "ignore_late_result", "cancel_requested_at", "next_poll_at",
            "error_code", "error_message", "finished_at", "committed_cost", "updated_at",
        ))
    text_attempts = list(GenerationAttempt.objects.select_for_update().filter(
        stage_run__production_run=run,
        operation__in=TEXT_OPERATIONS,
        status__in=(
            GenerationAttempt.Status.QUEUED,
            GenerationAttempt.Status.RUNNING,
            GenerationAttempt.Status.SUBMITTED,
        ),
    ))
    for attempt in text_attempts:
        attempt.ignore_late_result = True
        attempt.finished_at = now
        if attempt.status == GenerationAttempt.Status.SUBMITTED:
            attempt.error_code = "cancel_requested_ignore_late"
            attempt.error_message = "远端提交边界已经持久化；运行已取消，任何晚到结果都只保留审计证据。"
            append_attempt_resolution(
                attempt,
                outcome=GenerationAttemptResolution.Outcome.CANCEL_REQUESTED_IGNORE_LATE,
                evidence={
                    "schema_version": 1,
                    "attempt_id": str(attempt.id),
                    "request_sha256": attempt.request_sha256,
                    "provider_request_id": attempt.provider_request_id,
                    "policy": "ignore_late_result",
                },
                note="ProductionRun cancelled after submit boundary",
                decided_by=user,
            )
        else:
            attempt.status = GenerationAttempt.Status.CANCELLED
            attempt.error_code = "production_run_cancelled_before_submit"
            attempt.error_message = "ProductionRun 在远端提交前取消。"
        attempt.save(update_fields=(
            "status", "ignore_late_result", "error_code", "error_message",
            "finished_at", "updated_at",
        ))
    tasks = GenerationTask.objects.filter(
        platform_attempts__stage_run__production_run=run,
        status__in=(
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        ),
    ).distinct()
    tasks.filter(status=GenerationTask.Status.QUEUED).update(
        status=GenerationTask.Status.CANCELLED,
        finished_at=now,
        heartbeat_at=now,
        remote_state=GenerationTask.RemoteState.CANCELLED,
        updated_at=now,
    )
    tasks.filter(status=GenerationTask.Status.RUNNING).update(
        status=GenerationTask.Status.CANCEL_REQUESTED,
        heartbeat_at=now,
        remote_state=GenerationTask.RemoteState.CANCELLING,
        updated_at=now,
    )
    run.stages.exclude(status=StageRun.Status.SUCCEEDED).update(
        status=StageRun.Status.CANCELLED,
        reason_code="production_run_cancelled",
        reason="ProductionRun 已取消",
        finished_at=now,
        updated_at=now,
    )
    run.status = ProductionRun.Status.CANCELLED
    run.blocked_code = "production_run_cancelled"
    run.blocked_reason = "ProductionRun 已取消；运行中的远端任务已请求取消。"
    run.finished_at = now
    run.save(update_fields=(
        "status", "blocked_code", "blocked_reason", "finished_at",
        "committed_cost_total", "updated_at",
    ))
    return run


def select_run_artifact(run: ProductionRun, artifact: GeneratedArtifact, *, user):
    if artifact.production_run_id != run.id:
        raise ValidationError("artifact 不属于该 ProductionRun")
    if artifact.stage_run_id and artifact.stage_run.stage_key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
        raise ValidationError("Bible 参考图候选只能在 bible_image_review 批量选择并绑定")
    selected = select_generated_artifact(artifact, user=user)
    return selected


@transaction.atomic
def select_stage_route(
    run: ProductionRun,
    *,
    stage_key: str,
    route_index: int,
    user,
) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    try:
        stage = StageRun.objects.select_for_update().get(
            production_run=run,
            stage_key=stage_key,
        )
    except StageRun.DoesNotExist as exc:
        raise ValidationError({"stage_key": "ProductionRun 不包含该阶段"}) from exc
    routing = _verify_stage_routing(stage)
    if routing.get("mode") != "manual_choice":
        raise ValidationError("只有 manual_choice 阶段可以显式选择 route")
    routes = routing.get("routes") or []
    if type(route_index) is not int or route_index < 0 or route_index >= len(routes):
        raise ValidationError({"route_index": "不在冻结 routes 范围内"})
    if stage.status not in {StageRun.Status.AWAITING_APPROVAL, StageRun.Status.PENDING}:
        raise ValidationError("当前阶段状态不能选择 route")
    stage.selected_route_index = route_index
    stage.status = StageRun.Status.PENDING
    stage.reason_code = ""
    stage.reason = ""
    stage.finished_at = None
    stage.save(update_fields=(
        "selected_route_index", "status", "reason_code", "reason", "finished_at", "updated_at",
    ))
    _mark_route_selected(
        stage,
        unit_key=f"stage:{stage.stage_key}",
        route_index=route_index,
        reason=f"manual_choice_by:{user.id}",
    )
    run.status = ProductionRun.Status.QUEUED
    run.blocked_code = ""
    run.blocked_reason = ""
    run.finished_at = None
    run.save(update_fields=(
        "status", "blocked_code", "blocked_reason", "finished_at", "updated_at",
    ))
    return advance_production_run(run)


@transaction.atomic
def authorize_stage_cost(
    run: ProductionRun,
    *,
    stage_key: str,
    allow_external_calls: bool | None,
    budget_limit: Decimal | None,
    approved_estimated_cost: Decimal | None,
    currency: str,
    comment: str,
    user,
) -> ProductionRun:
    comment = normalize_optional_audit_comment(comment)
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    try:
        stage = StageRun.objects.select_for_update().get(
            production_run=run,
            stage_key=stage_key,
        )
    except StageRun.DoesNotExist as exc:
        raise ValidationError({"stage_key": "ProductionRun 不包含该阶段"}) from exc
    allowed_codes = {
        "unknown_cost", "budget_limit_exceeded", "external_budget_missing",
        "external_call_not_authorized", "cost_currency_missing", "actual_budget_exceeded",
    }
    if stage.status != StageRun.Status.AWAITING_APPROVAL or stage.reason_code not in allowed_codes:
        raise ValidationError("当前阶段不处于费用或外发授权门禁")
    normalized_currency = str(currency or run.cost_currency or "").strip().upper()
    if approved_estimated_cost is not None:
        if approved_estimated_cost < 0:
            raise ValidationError({"approved_estimated_cost": "不能为负数"})
        if approved_estimated_cost > 0 and not normalized_currency:
            raise ValidationError({"currency": "非零费用授权必须声明币种"})
        if run.cost_currency and normalized_currency and run.cost_currency != normalized_currency:
            raise ValidationError({"currency": "与运行已有费用币种不一致"})
        stage.approved_estimated_cost = approved_estimated_cost
        stage.approved_cost_currency = normalized_currency
    if budget_limit is not None:
        total_exposure = run.actual_cost_total + run.committed_cost_total
        if budget_limit < total_exposure:
            raise ValidationError({
                "budget_limit": f"不能低于已发生费用与未结承诺之和 {total_exposure}",
            })
        run.budget_limit = budget_limit
    if allow_external_calls is not None:
        run.allow_external_calls = bool(allow_external_calls)
    if run.allow_external_calls and run.budget_limit is None:
        raise ValidationError({"budget_limit": "授权外发时必须设置预算"})
    revision = (stage.approval_decisions.aggregate(value=Max("revision"))["value"] or 0) + 1
    ApprovalDecision.objects.create(
        stage_run=stage,
        revision=revision,
        decision=ApprovalDecision.Decision.APPROVE,
        comment=normalize_optional_audit_comment(
            f"费用/外发授权: allow_external_calls={run.allow_external_calls}, "
            f"budget_limit={run.budget_limit}, approved_estimated_cost="
            f"{stage.approved_estimated_cost} {stage.approved_cost_currency}; {comment or ''}"
        ),
        decided_by=user,
    )
    decision = dict(stage.routing_decision or {})
    pending_bible_regeneration = decision.get("pending_bible_regeneration")
    if isinstance(pending_bible_regeneration, dict):
        if stage.stage_key != BIBLE_IMAGE_GENERATION_STAGE_KEY:
            raise ValidationError("Bible 重新生成费用 intent 绑定到了错误阶段")
        if (
            pending_bible_regeneration.get("gate_code") == "unknown_cost"
            and approved_estimated_cost is None
        ):
            raise ValidationError({
                "approved_estimated_cost": (
                    "本次 Bible 追加生成费用未知，必须为这一次重新生成明确授权上限"
                ),
            })
        request, request_sha256 = _bible_regeneration_request_contract(
            request_id=pending_bible_regeneration.get("request_id"),
            expected_source_snapshot_sha256=pending_bible_regeneration.get(
                "expected_source_snapshot_sha256",
            ),
            entities=list(pending_bible_regeneration.get("entities") or []),
        )
        targets = list(pending_bible_regeneration.get("targets") or [])
        if (
            request_sha256 != pending_bible_regeneration.get("request_sha256")
            or len(targets) != len(request["entities"])
        ):
            raise ValidationError("Bible 重新生成费用 intent 已漂移")
        review_stage = StageRun.objects.select_for_update().filter(
            production_run=run,
            stage_key=BIBLE_IMAGE_REVIEW_STAGE_KEY,
        ).first()
        if review_stage is None or review_stage.status != StageRun.Status.PENDING:
            raise ValidationError("Bible 重新生成审核阶段状态已漂移")
        _verify_bible_regeneration_source(
            run,
            stage,
            expected_source_snapshot_sha256=request[
                "expected_source_snapshot_sha256"
            ],
            entities=request["entities"],
        )
        expected_variants = _next_bible_regeneration_variants(
            stage,
            entities=request["entities"],
        )
        actual_variants = {
            (str(item.get("entity_type") or ""), str(item.get("entity_id") or "")): [
                int(item.get("variant_number") or 0),
            ]
            for item in targets
            if isinstance(item, dict)
        }
        if actual_variants != expected_variants:
            raise ValidationError("Bible 重新生成待授权 variant 已漂移")
        stage.save(update_fields=(
            "approved_estimated_cost", "approved_cost_currency", "updated_at",
        ))
        run.save(update_fields=(
            "allow_external_calls", "budget_limit", "updated_at",
        ))
        return _materialize_bible_regeneration(
            run,
            stage,
            review_stage,
            intent=pending_bible_regeneration,
            approved_unknown_cost_total=(
                stage.approved_estimated_cost
                if approved_estimated_cost is not None else None
            ),
            approved_unknown_cost_currency=stage.approved_cost_currency,
        )
    pending_retry = decision.pop("pending_media_retry", None)
    if isinstance(pending_retry, dict):
        decision["active_media_retry_cost_authorization"] = {
            "schema_version": 1,
            "total_amount": (
                str(stage.approved_estimated_cost)
                if stage.approved_estimated_cost is not None else None
            ),
            "currency": stage.approved_cost_currency,
            "units_sha256": _sha256(pending_retry.get("units") or []),
        }
        _save_routing_decision(stage, decision)
        stage.status = StageRun.Status.BLOCKED
        stage.reason_code = "retry_authorized_resume"
        stage.reason = "费用/外发门禁已授权，继续原显式 retry intent。"
        stage.finished_at = None
        stage.save(update_fields=(
            "approved_estimated_cost", "approved_cost_currency", "status",
            "reason_code", "reason", "finished_at", "updated_at",
        ))
        run.status = ProductionRun.Status.BLOCKED
        run.blocked_code = stage.reason_code
        run.blocked_reason = stage.reason
        run.finished_at = None
        run.save(update_fields=(
            "allow_external_calls", "budget_limit", "status", "blocked_code",
            "blocked_reason", "finished_at", "updated_at",
        ))
        return retry_production_run(run, user=user)
    stage.status = StageRun.Status.PENDING
    stage.reason_code = ""
    stage.reason = ""
    stage.finished_at = None
    stage.save(update_fields=(
        "approved_estimated_cost", "approved_cost_currency", "status",
        "reason_code", "reason", "finished_at", "updated_at",
    ))
    run.status = ProductionRun.Status.QUEUED
    run.blocked_code = ""
    run.blocked_reason = ""
    run.finished_at = None
    run.save(update_fields=(
        "allow_external_calls", "budget_limit", "status", "blocked_code",
        "blocked_reason", "finished_at", "updated_at",
    ))
    return advance_production_run(run)
