from __future__ import annotations

import hashlib
import logging
import uuid
from copy import deepcopy
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    ApprovalDecision,
    ArtifactSelectionDecision,
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
    ModelProfileVersion,
    ProviderProfile,
    ProductionAssemblyLink,
    ProductionRecipeVersion,
    ProductionRun,
    PromptArtifact,
    PromptFieldTranslation,
    PropBible,
    ReleaseBundle,
    SceneBible,
    ScriptRevision,
    Shot,
    StageRun,
    StageRouteEvent,
    WorkflowTemplateVersion,
)
from studio.services.assembly import AssemblyError, create_assembly
from studio.services.configuration import (
    ConfigurationResolutionError,
    create_config_revision,
    publish_config_revision,
    redact_sensitive_bindings,
    resolve_configuration,
    resolve_configuration_from_snapshot,
    validate_config_override_values,
    video_shot_duration_policy,
)
from studio.services.generated_artifacts import (
    record_assembly_artifact,
    record_generation_task_artifacts,
    select_artifact as select_generated_artifact,
)
from studio.services.json_contracts import canonical_json_bytes, json_contract, verify_json_contract
from studio.services.provenance import ProvenanceError
from studio.services.provider_registry import (
    ProviderDriverError,
    credential_binding_sha256,
    deployment_supports_script_generation,
    driver_for,
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
    clone_generation_task_for_retry,
    compile_prompt_input,
    materialize_compiled_prompt,
    materialize_compiled_workflow_version,
    materialize_generation_task,
)
from studio.services.workflow_compiler import WorkflowCompilerError, compile_frozen_workflow
from studio.services.workflow_versions import workflow_version_execution_snapshot
from studio.services.script_breakdown import breakdown_script, project_default_h3_mode
from studio.services.h3_prompts import PromptBuildError, sync_consistency_locks
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.services.prompt_profiles import (
    PromptProfileResolutionError,
    assemble_prompt_profile,
    model_family_for_profile,
    require_prompt_profile_binding,
    text_profile_sources,
    verify_frozen_profile_assembly,
    verify_frozen_profile_reference,
)
from studio.services.prompt_normalization import (
    ROUTE_CONTEXT_DURABLE_ATTEMPT,
    SUBMISSION_SEMANTICS as PROMPT_NORMALIZATION_SUBMISSION_SEMANTICS,
    route_state as prompt_normalization_route_state,
    validate_prompt_normalization_model_profile,
)
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION
from studio.services.prompt_translation import (
    ALLOWED_FIELD_KEYS,
    confirm_prompt_normalization_candidates,
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
from studio.services.script_structure import (
    SCRIPT_STRUCTURE_OPERATION,
    SCRIPT_STRUCTURE_UNIT_KEY,
    build_script_structure_plan,
    canonical_no_submission_review_payload,
    is_canonical_script,
    structure_provider_payload,
    validate_script_structure_model_profile,
    verify_script_structure_smoke,
)
from studio.services.series_concept_drafts import build_series_concept_plan
from studio.services.text_generation_queue import (
    MAX_TEXT_SNAPSHOT_BYTES,
    TEXT_OPERATION,
    TEXT_UNIT_KEY,
    PROMPT_NORMALIZE_UNIT_KEY,
    append_attempt_resolution,
    frozen_text_deployment,
    provider_payload as text_provider_payload,
)
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
    "script_generate",
    "script_structure",
    "script_normalize",
    "bible_generation",
})
ROUTING_MODES = frozenset({"fixed", "ordered_fallback", "manual_choice"})
MAX_ROUTING_ROUTES = 16
MAX_STAGE_SNAPSHOT_BYTES = 8 * 1024 * 1024
EXECUTABLE_STAGE_MODES = frozenset({
    ("script_input", "confirmed_revision"),
    ("script_generate", "ai"),
    ("script_structure", "ai"),
    ("script_review", "review"),
    ("bible_materialize", "deterministic"),
    ("bible_review", "review"),
    ("storyboard", "deterministic"),
    ("storyboard_review", "review"),
    ("script_normalize", "ai"),
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
    return {
        "schema_version": 1,
        "mode": "unconfigured",
        "routes": [],
        "variant_count": 1,
        "evaluator": None,
    }


def _freeze_stage_routing(raw: object, *, stage_key: str) -> dict[str, Any]:
    if raw is None:
        return _unconfigured_routing()
    if not isinstance(raw, dict):
        raise ValidationError({"routing": f"阶段 {stage_key} routing 必须是对象"})
    unknown = sorted(set(raw) - {"mode", "routes", "evaluator", "variant_count"})
    if unknown:
        raise ValidationError({"routing": f"阶段 {stage_key} routing 含未知字段: {', '.join(unknown)}"})
    mode = str(raw.get("mode") or "").strip()
    routes = raw.get("routes")
    evaluator = raw.get("evaluator")
    variant_count = raw.get("variant_count", 1)
    if mode not in ROUTING_MODES:
        raise ValidationError({"routing": f"阶段 {stage_key} routing.mode 无效"})
    if not isinstance(routes, list) or not 1 <= len(routes) <= MAX_ROUTING_ROUTES:
        raise ValidationError({"routing": f"阶段 {stage_key} routes 必须为 1 到 {MAX_ROUTING_ROUTES} 项"})
    if mode == "fixed" and len(routes) != 1:
        raise ValidationError({"routing": f"阶段 {stage_key} fixed 必须且只能声明一个 route"})
    if type(variant_count) is not int or not 1 <= variant_count <= 8:
        raise ValidationError({"routing": f"阶段 {stage_key} variant_count 必须为 1 到 8"})
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
                workflow = WorkflowTemplateVersion.objects.select_related("template").get(
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
        workflow_snapshot = None
        if workflow is not None:
            workflow_snapshot = workflow_version_execution_snapshot(workflow)
        provider_config = _non_secret_config(deployment.profile_version.provider.driver_config or {})
        deployment_config = _non_secret_config(deployment.runtime_config or {})
        model_specification = _non_secret_config(deployment.profile_version.specification or {})
        effective_timeout_seconds = float(
            deployment_config.get(
                "timeout_seconds",
                provider_config.get("timeout_seconds", 60),
            )
        )
        media_operations: list[str] = []
        smoke_evidence: dict[str, dict[str, Any]] = {}
        submission_semantics = ""
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
        if stage_key == "media_generation":
            output_kinds: list[str] = []
            h3_modes: list[str] = []
            if workflow is not None:
                output_kinds = [workflow.template.output_kind]
                h3_modes = [
                    str(value)
                    for value in ((workflow.input_contract or {}).get("h3_modes") or [])
                ]
            else:
                if "image.text_to_image" in media_operations:
                    output_kinds.append(Shot.MediaType.IMAGE)
                if "video.text_to_video" in media_operations:
                    output_kinds.append(Shot.MediaType.VIDEO)
                    h3_modes.append(Shot.H3Mode.T2VA)
            prompt_profile_reasons = _media_prompt_profile_dependency_reasons(
                deployment.profile_version,
                output_kinds=output_kinds,
                h3_modes=h3_modes,
            )
            if prompt_profile_reasons:
                raise ValidationError({
                    "routing": (
                        f"routes[{index}] 的提示词策略依赖未满足："
                        + "；".join(reason for _code, reason in prompt_profile_reasons)
                    ),
                })
        frozen_routes.append({
            "index": index,
            "workflow_version_id": str(workflow.id) if workflow else None,
            "workflow_sha256": workflow.graph_sha256 if workflow else "",
            "workflow_output_kind": workflow.template.output_kind if workflow else "",
            "workflow_h3_modes": (
                list((workflow.input_contract or {}).get("h3_modes") or []) if workflow else []
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
    )
    definition = recipe.definition
    if not isinstance(definition, dict) or definition.get("schema_version") != 1:
        raise ValidationError("recipe definition schema_version 不受支持")
    raw_stages = definition.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ValidationError("recipe 必须声明非空 stages")
    stages = []
    seen: set[str] = set()
    for ordinal, raw in enumerate(raw_stages, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"recipe stage {ordinal} 必须是对象")
        key = str(raw.get("key") or "").strip()
        mode = str(raw.get("mode") or "").strip()
        approval = str(raw.get("approval") or "auto").strip()
        if not key or key in seen or mode not in SUPPORTED_STAGE_MODES:
            raise ValidationError(f"recipe stage {ordinal} 的 key/mode 无效")
        if approval not in {"auto", "required", "on_warning"}:
            raise ValidationError(f"recipe stage {ordinal} 的 approval 无效")
        seen.add(key)
        stages.append({
            "key": key,
            "mode": mode,
            "approval": approval,
            "routing": _freeze_stage_routing(raw.get("routing"), stage_key=key),
        })
    required_order = [
        "script_input", "script_generate", "script_structure", "script_review", "bible_materialize", "bible_review", "storyboard",
        "storyboard_review", "script_normalize", "script_normalize_review",
        "prompt_compile", "media_generation",
        "artifact_selection", "assembly", "delivery",
    ]
    positions = {item["key"]: index for index, item in enumerate(stages)}
    present = [key for key in required_order if key in positions]
    if present != sorted(present, key=required_order.index):
        raise ValidationError("recipe 主阶段顺序无效")
    return stages


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

    def canonical_uuid(value: object) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            return ""

    deployment_ids = {
        identifier
        for item in raw_routes
        if isinstance(item, dict)
        if (identifier := canonical_uuid(item.get("model_deployment_id")))
    }
    workflow_ids = {
        identifier
        for item in raw_routes
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
        h3_modes: list[str] = []
        media_operations: list[str] = []

        if not isinstance(raw, dict):
            reasons.append(("route_contract_invalid", "route 合同不是对象"))
        if not deployment_id:
            reasons.append(("deployment_id_invalid", "模型部署 ID 缺失或无效"))
        elif deployment is None:
            reasons.append(("deployment_missing", "模型部署不存在"))
        else:
            profile = deployment.profile_version
            provider = profile.provider
            deployment_label = deployment.name or deployment.key
            model_identifier = profile.model_identifier
            driver_type = provider.driver_type
            if deployment.status != ModelDeployment.Status.ACTIVE:
                reasons.append(("deployment_not_active", "模型部署未启用"))
            if not provider.is_active:
                reasons.append(("provider_not_active", "Provider 已停用"))
            if profile.status != profile.Status.PUBLISHED:
                reasons.append(("model_not_published", "模型版本未发布"))

            if driver_type == ProviderProfile.DriverType.ASYNC_REST:
                if workflow_id:
                    reasons.append((
                        "async_workflow_forbidden",
                        "async REST route 不能绑定 ComfyUI 工作流",
                    ))
                try:
                    media_operations = validate_async_model_profile(profile)
                except (ValidationError, ProviderDriverError, RuntimeError):
                    reasons.append((
                        "async_contract_invalid",
                        "异步媒体模型合同或驱动能力不完整",
                    ))
                if "image.text_to_image" in media_operations:
                    output_kinds.append(Shot.MediaType.IMAGE)
                if "video.text_to_video" in media_operations:
                    output_kinds.append(Shot.MediaType.VIDEO)
                    h3_modes.append(Shot.H3Mode.T2VA)
            elif driver_type == ProviderProfile.DriverType.COMFYUI:
                if not workflow_id:
                    reasons.append(("workflow_required", "ComfyUI route 缺少工作流"))
                elif workflow is None:
                    reasons.append(("workflow_missing", "工作流版本不存在"))
                else:
                    workflow_label = f"{workflow.template.name} · v{workflow.version}"
                    output_kinds = [workflow.template.output_kind]
                    h3_modes = [
                        str(value)
                        for value in ((workflow.input_contract or {}).get("h3_modes") or [])
                    ]
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
            reasons.extend(_media_prompt_profile_dependency_reasons(
                profile,
                output_kinds=output_kinds,
                h3_modes=h3_modes,
            ))

        options.append({
            "route_index": route_index,
            "workflow_version_id": workflow_id or None,
            "workflow_label": workflow_label,
            "model_deployment_id": deployment_id or None,
            "deployment_label": deployment_label,
            "model_identifier": model_identifier,
            "driver_type": driver_type,
            "output_kinds": output_kinds,
            "h3_modes": h3_modes,
            "media_operations": media_operations,
            "is_selectable": not reasons,
            "reason_codes": [code for code, _reason in reasons],
            "reasons": [reason for _code, reason in reasons],
        })

    return {
        "schema_version": 1,
        "recipe_version_id": str(recipe.id),
        "recipe_definition_sha256": recipe.definition_sha256,
        "routes": options,
    }


def _media_prompt_profile_dependency_reasons(
    profile: ModelProfileVersion,
    *,
    output_kinds: list[str],
    h3_modes: list[str],
) -> list[tuple[str, str]]:
    """Return safe dependency gaps for every prompt contract a media route advertises."""

    requirements: list[tuple[str, str]] = []
    if Shot.MediaType.IMAGE in output_kinds:
        requirements.append(("image.generate", ""))
    if Shot.MediaType.VIDEO in output_kinds:
        requirements.extend(
            ("video.generate", mode) for mode in (h3_modes or [""])
        )
    family = model_family_for_profile(profile)
    reasons: list[tuple[str, str]] = []
    for operation, mode in requirements:
        label = f"{operation}/{mode}" if mode else operation
        try:
            require_prompt_profile_binding(
                operation=operation,
                model_family=family,
                mode=mode,
                model_profile_version=profile,
            )
        except PromptProfileResolutionError as exc:
            if exc.code == "prompt_profile_binding_missing":
                reason = f"{label} 缺少 active PromptProfile binding（必须唯一）"
            elif exc.code == "prompt_profile_binding_ambiguous":
                reason = f"{label} 存在多个同优先级 active PromptProfile binding"
            else:
                reason = f"{label} 的 PromptProfile binding 冲突或无效"
            reasons.append((exc.code, reason))
            continue
        except ValidationError:
            reasons.append((
                "prompt_profile_binding_invalid",
                f"{label} 的 PromptProfile binding 无效",
            ))
            continue
    return reasons


def _validate_media_prompt_profile_dependencies(
    route: dict[str, Any],
    *,
    profile: ModelProfileVersion,
    shot: Shot | None,
) -> None:
    if shot is not None:
        output_kinds = [shot.media_type]
        h3_modes = [shot.h3_mode] if shot.media_type == Shot.MediaType.VIDEO else []
    elif profile.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
        operations = set(route.get("media_operations") or [])
        output_kinds = []
        if "image.text_to_image" in operations:
            output_kinds.append(Shot.MediaType.IMAGE)
        if "video.text_to_video" in operations:
            output_kinds.append(Shot.MediaType.VIDEO)
        h3_modes = [Shot.H3Mode.T2VA] if Shot.MediaType.VIDEO in output_kinds else []
    else:
        output_kind = str(route.get("workflow_output_kind") or "")
        output_kinds = [output_kind] if output_kind in Shot.MediaType.values else []
        h3_modes = [str(value) for value in (route.get("workflow_h3_modes") or [])]
    reasons = _media_prompt_profile_dependency_reasons(
        profile,
        output_kinds=output_kinds,
        h3_modes=h3_modes,
    )
    if reasons:
        raise ValidationError({
            "workflow_selection": (
                f"route {int(route.get('index', -1))} 的提示词策略依赖未满足："
                + "；".join(reason for _code, reason in reasons)
            ),
        })


def _safe_media_route_projection(route: dict[str, Any]) -> dict[str, Any]:
    """Keep the selected immutable identities without exposing driver configuration."""

    smoke_evidence = route.get("smoke_evidence")
    return {
        "schema_version": 1,
        "route_index": int(route["index"]),
        "workflow_version_id": route.get("workflow_version_id"),
        "workflow_sha256": str(route.get("workflow_sha256") or ""),
        "workflow_output_kind": str(route.get("workflow_output_kind") or ""),
        "workflow_h3_modes": list(route.get("workflow_h3_modes") or []),
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
    }


def _validate_selected_media_route(
    route: dict[str, Any],
    *,
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
                shot=shot,
                deployment=deployment,
                workflow=None,
            )
        _validate_media_prompt_profile_dependencies(
            route,
            profile=profile,
            shot=shot,
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
            shot=shot,
            deployment=deployment,
            workflow=workflow,
        )
    _validate_media_prompt_profile_dependencies(
        route,
        profile=profile,
        shot=shot,
    )
    return deployment, workflow


def _validate_selected_media_route_for_shot(
    route: dict[str, Any],
    *,
    shot: Shot,
    deployment: ModelDeployment,
    workflow: WorkflowTemplateVersion | None,
) -> None:
    route_index = int(route.get("index", -1))
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
            shot.h3_mode != Shot.H3Mode.T2VA or has_references
        ):
            raise ValidationError({
                "workflow_selection": (
                    f"route {route_index} 的 async REST 首版只支持无参考素材的 T2VA 视频"
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
    if workflow.template.output_kind != shot.media_type:
        raise ValidationError({
            "workflow_selection": (
                f"route {route_index} 输出 {workflow.template.output_kind}，"
                f"不能用于 {shot.media_type} 镜头"
            ),
        })
    h3_modes = list((workflow.input_contract or {}).get("h3_modes") or [])
    if (
        shot.media_type == Shot.MediaType.VIDEO
        and h3_modes
        and shot.h3_mode not in h3_modes
    ):
        raise ValidationError({
            "workflow_selection": (
                f"route {route_index} 不支持镜头 H3 模式 {shot.h3_mode}"
            ),
        })


def _resolve_workflow_selection(
    *,
    project,
    stages: list[dict[str, Any]],
    selected_episode_ids: list[str],
    requested: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve a run-scoped uniform/per-shot choice only from frozen recipe routes."""

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
        if routing.get("mode") != "fixed" or len(routes) != 1:
            raise ProductionRunCreateRejected(
                {
                    "workflow_selection": (
                        "该配方包含非固定或多候选媒体 route；"
                        "必须显式选择整集统一或逐镜头工作流后才能创建运行"
                    ),
                },
                code="route_selection_required",
            )
        # A fixed single route is not a user choice. Freeze it through the same
        # validation path as an explicit uniform selection so catalog/shot
        # compatibility and the run snapshot remain identical.
        requested = {
            "mode": "uniform",
            "route_index": int(routes[0]["index"]),
        }
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
            shot.h3_mode,
            has_references,
        )
        if contract_key in validated_compatibility:
            return
        deployment, workflow = route_catalog_facts(route)
        _validate_selected_media_route_for_shot(
            route,
            shot=shot,
            deployment=deployment,
            workflow=workflow,
        )
        validated_compatibility.add(contract_key)

    mode = str(requested.get("mode") or "")
    shots = list(
        Shot.objects.select_related("episode")
        .filter(episode__project=project, episode_id__in=selected_episode_ids)
        .order_by("episode__number", "number", "id")
    )
    routing_sha256 = _sha256(routing)
    if mode == "uniform":
        route = selected_route(requested.get("route_index"))
        if shots:
            route_catalog_facts(route)
            for shot in shots:
                validate_shot_route(route, shot)
        else:
            # AI/import runs can select a route before storyboard materializes
            # any Shot rows. Validate against the same defaults that the
            # deterministic breakdown will persist, otherwise an image-only,
            # reference-only, or incompatible H3 workflow can be frozen into a
            # run that is guaranteed to block after storyboard creation.
            future_shot = Shot(
                media_type=Shot.MediaType.VIDEO,
                h3_mode=project_default_h3_mode(project),
                reference_bindings={},
            )
            validate_shot_route(route, future_shot)
        return {
            "schema_version": 1,
            "mode": "uniform",
            "media_stage_routing_sha256": routing_sha256,
            "selected_episode_ids": list(selected_episode_ids),
            "route": _safe_media_route_projection(route),
            "shot_routes": [],
        }
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
    by_shot: dict[str, Any] = {}
    for item in assignments:
        if not isinstance(item, dict):
            raise ValidationError({"workflow_selection": "shot_routes 每项必须是对象"})
        shot_id = str(item.get("shot_id") or "")
        if shot_id in by_shot:
            raise ValidationError({"workflow_selection": "同一镜头不能重复选择 route"})
        by_shot[shot_id] = item.get("route_index")
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
        route = selected_route(by_shot[str(shot.id)])
        validate_shot_route(route, shot)
        resolved_rows.append({
            "episode_id": str(shot.episode_id),
            "episode_number": shot.episode.number,
            "shot_id": str(shot.id),
            "shot_number": shot.number,
            "media_type": shot.media_type,
            "h3_mode": shot.h3_mode,
            "route": _safe_media_route_projection(route),
        })
    return {
        "schema_version": 1,
        "mode": "per_shot",
        "media_stage_routing_sha256": routing_sha256,
        "selected_episode_ids": list(selected_episode_ids),
        "route": None,
        "shot_routes": resolved_rows,
    }


def _validate_delivery_recipe_closure(stages: list[dict[str, Any]]) -> None:
    """Require the complete, formally-gated production-to-delivery chain."""

    by_key = {item["key"]: item for item in stages}
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
            or review.get("approval") != "required"
        ):
            raise ValidationError(
                "script_structure 必须使用 ai，并由 approval=required 的 script_review 确认",
            )
    normalize = by_key.get("script_normalize")
    normalize_review = by_key.get("script_normalize_review")
    if bool(normalize) != bool(normalize_review):
        raise ValidationError(
            "script_normalize/ai 与 script_normalize_review/review 必须成对出现",
        )
    if normalize and (
        normalize.get("mode") != "ai"
        or normalize_review.get("mode") != "review"
        or normalize_review.get("approval") != "required"
    ):
        raise ValidationError(
            "script_normalize 必须使用 ai，并由 approval=required 的 review 阶段确认",
        )
    expected_modes = {
        "storyboard": {"deterministic", "manual"},
        **({
            "script_normalize": "ai",
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
    ordered.extend(("storyboard",))
    if normalize:
        if "storyboard_review" in by_key:
            ordered.append("storyboard_review")
        ordered.extend(("script_normalize", "script_normalize_review"))
    ordered.extend((
        "prompt_compile", "media_generation", "artifact_selection", "assembly", "delivery",
    ))
    if [positions[key] for key in ordered] != sorted(positions[key] for key in ordered):
        raise ValidationError("published recipe 的 content/storyboard/prompt/media/selection/assembly/delivery 顺序无效")


def validate_production_recipe_executability(
    recipe: ProductionRecipeVersion,
    *,
    require_delivery_closure: bool = False,
) -> list[dict[str, Any]]:
    """Reject production publication when a stage has no real executor/materializer."""

    stages = _recipe_stages(recipe)
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
    for stage in stages:
        key = stage["key"]
        mode = stage["mode"]
        if mode == "skip":
            continue
        if (key, mode) not in EXECUTABLE_STAGE_MODES:
            raise ValidationError(
                f"阶段 {key}/{mode} 尚无正式 executor/materializer；只能保留 draft，不能发布生产配方"
            )
        if key == "media_generation":
            routing = stage["routing"]
            if routing.get("mode") == "unconfigured" or not routing.get("routes"):
                raise ValidationError("media_generation 必须冻结至少一个已发布 workflow/deployment route")
            for route in routing["routes"]:
                deployment = ModelDeployment.objects.select_related(
                    "profile_version__provider",
                ).get(pk=route["model_deployment_id"])
                if deployment.provider.driver_type == ProviderProfile.DriverType.COMFYUI:
                    if not route.get("workflow_version_id"):
                        raise ValidationError("ComfyUI media route 必须绑定 workflow version")
                    continue
                if deployment.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
                    raise ValidationError("media_generation 仅支持 ComfyUI 或 async_rest route")
                if route.get("workflow_version_id") or route.get("workflow_snapshot"):
                    raise ValidationError("async_rest route 禁止绑定 ComfyUI workflow")
                operations = validate_async_model_profile(deployment.profile_version)
                if not operations:
                    raise ValidationError("async_rest route 没有首版可执行媒体能力")
                missing_smoke = [
                    operation for operation in operations
                    if fresh_smoke_snapshot(deployment, operation) is None
                ]
                if missing_smoke:
                    raise ValidationError(
                        "async_rest route 缺少未过期的真实烟测证据: " + ", ".join(missing_smoke)
                    )
        if key == "script_generate":
            routing = stage["routing"]
            if routing.get("mode") == "unconfigured" or not routing.get("routes"):
                raise ValidationError("script_generate 必须冻结至少一个 ModelDeployment route")
            if int(routing.get("variant_count") or 1) != 1:
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
                        "script_generate deployment 没有受控 chat endpoint；禁止回退 legacy settings"
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
            if int(routing.get("variant_count") or 1) != 1:
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
                        "script_structure deployment 没有受控 chat endpoint；禁止回退 legacy settings",
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
        if key == "script_normalize":
            routing = stage["routing"]
            if routing.get("mode") == "unconfigured" or not routing.get("routes"):
                raise ValidationError("script_normalize 必须冻结至少一个 ModelDeployment route")
            if int(routing.get("variant_count") or 1) != 1:
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
    if require_delivery_closure:
        _validate_delivery_recipe_closure(stages)
    return stages


def _verify_run_snapshots(run: ProductionRun) -> None:
    verify_json_contract(
        run.config_snapshot,
        schema_version=run.config_snapshot_schema_version,
        size_bytes=run.config_snapshot_size_bytes,
        sha256=run.config_snapshot_sha256,
    )
    verify_json_contract(
        run.input_snapshot,
        schema_version=run.input_snapshot_schema_version,
        size_bytes=run.input_snapshot_size_bytes,
        sha256=run.input_snapshot_sha256,
    )


def _verify_stage_routing(stage: StageRun) -> dict[str, Any]:
    verify_json_contract(
        stage.routing,
        schema_version=stage.routing_schema_version,
        size_bytes=stage.routing_size_bytes,
        sha256=stage.routing_sha256,
        max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
    )
    verify_json_contract(
        stage.routing_decision,
        schema_version=stage.routing_decision_schema_version,
        size_bytes=stage.routing_decision_size_bytes,
        sha256=stage.routing_decision_sha256,
        max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
    )
    if not isinstance(stage.routing, dict) or stage.routing.get("schema_version") != 1:
        raise StageBlocked("routing_snapshot_invalid", "阶段 routing 快照无效")
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
                "h3_mode": shot.h3_mode,
                "include_audio": shot.include_audio,
                "scene_id": str(shot.scene_id) if shot.scene_id else None,
                "character_ids": sorted(str(item) for item in shot.characters.values_list("id", flat=True)),
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
            "h3_mode": item.h3_mode,
            "content_hash": item.content_hash,
        }
        for item in _prompt_rows_for_run(run)
    ]}


def _canonical_prompt_translations(run: ProductionRun) -> dict[str, Any]:
    normalize_stage = run.stages.filter(
        stage_key="script_normalize",
        status=StageRun.Status.SUCCEEDED,
    ).first()
    candidates: list[dict[str, Any]] = []
    if normalize_stage is not None:
        snapshot = _verify_stage_snapshot(normalize_stage, "output_snapshot")
        review_payload = snapshot.get("review_payload")
        if isinstance(review_payload, dict) and isinstance(review_payload.get("candidates"), list):
            candidates = review_payload["candidates"]
    rows = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        entry = PromptFieldTranslation.objects.filter(
            project=run.project,
            scope_type=candidate.get("scope_type"),
            scope_id=candidate.get("scope_id"),
            field_key=candidate.get("field_key"),
            status=PromptFieldTranslation.Status.CONFIRMED,
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
    return {"translations": rows}


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
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        ).order_by("variant_group", "variant_index", "id")
    ]}


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
    elif stage.stage_key in {"bible", "bible_generation", "bible_review"}:
        formal_type, canonical = "bible", _canonical_bible(run)
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
    }:
        payload["review_payload"] = existing
    return payload


def _save_stage_snapshot(stage: StageRun, field: str, value: dict[str, Any]) -> str:
    contract = json_contract(value, max_bytes=MAX_STAGE_SNAPSHOT_BYTES)
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
        max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
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


def _assert_frozen_formal_objects(run: ProductionRun) -> None:
    factories = {
        "script": _canonical_script,
        "existing_episode_selection": _canonical_existing_episode_selection,
        "bible": _canonical_bible,
        "storyboard": _canonical_storyboard,
        "prompt_manifest": _canonical_prompts,
        "prompt_translation_manifest": _canonical_prompt_translations,
        "media_manifest": _canonical_media,
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
    public_snapshot_sha256 = json_contract(redact_sensitive_bindings(snapshot)).sha256
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
    if stage.stage_key == "media_generation":
        selection = _frozen_workflow_selection(stage)
        if selection is not None:
            payload["workflow_selection"] = deepcopy(selection)
            payload["workflow_selection_sha256"] = _sha256(selection)
    _save_stage_snapshot(stage, "input_snapshot", payload)


def _freeze_stage_config(run: ProductionRun, stage: StageRun) -> None:
    if stage.config_snapshot_sha256:
        try:
            verify_json_contract(
                stage.config_snapshot,
                schema_version=stage.config_snapshot_schema_version,
                size_bytes=stage.config_snapshot_size_bytes,
                sha256=stage.config_snapshot_sha256,
                max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
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
    }, max_bytes=MAX_STAGE_SNAPSHOT_BYTES)
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
            max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
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
    contract = json_contract(decision, max_bytes=MAX_STAGE_SNAPSHOT_BYTES)
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
    if stage.stage_key != "media_generation":
        return None
    run = stage.production_run
    snapshot = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    selection = snapshot.get("workflow_selection")
    expected_sha256 = str(snapshot.get("workflow_selection_sha256") or "")
    if selection is None and not expected_sha256:
        return None
    if (
        not isinstance(selection, dict)
        or selection.get("schema_version") != 1
        or not expected_sha256
        or _sha256(selection) != expected_sha256
    ):
        raise StageBlocked(
            "workflow_selection_snapshot_invalid",
            "运行冻结的工作流选择快照无效；必须新建 ProductionRun。",
        )
    routing = _verify_stage_routing(stage)
    if selection.get("media_stage_routing_sha256") != _sha256(routing):
        raise StageBlocked(
            "workflow_selection_routing_drift",
            "运行冻结的工作流选择与媒体 route 快照不一致；必须新建 ProductionRun。",
        )
    return selection


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
    structured = (
        prompt.structured_payload if isinstance(prompt.structured_payload, dict) else {}
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
                    prompt.h3_mode != "T2VA" or references
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
                modes = route.get("workflow_h3_modes")
                if output_kind != prompt.prompt_type:
                    failures.append({"route_index": index, "code": "output_kind_mismatch"})
                    continue
                if (
                    prompt.prompt_type == "video"
                    and isinstance(modes, list)
                    and modes
                    and prompt.h3_mode not in modes
                ):
                    failures.append({"route_index": index, "code": "h3_mode_mismatch"})
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


def _estimate_and_reserve(
    run: ProductionRun,
    stage: StageRun,
    attempt: GenerationAttempt,
    *,
    deployment: ModelDeployment,
    operation: str,
    payload: dict[str, Any],
    is_external: bool,
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
    if not isinstance(estimate, dict) or estimate.get("schema_version") != 1:
        raise StageBlocked("cost_estimate_invalid", "provider driver 费用估算契约无效")
    amount = _decimal_amount(estimate.get("amount"))
    currency = str(estimate.get("currency") or "").strip().upper()
    if amount is None:
        amount = stage.approved_estimated_cost
        currency = stage.approved_cost_currency
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
        ))
    except (ValidationError, ValueError, TypeError) as exc:
        raise ProductionRunConflict({
            "idempotency_key": "已存在的 ProductionRun 初始化不完整；请使用新的幂等键",
        }) from exc
    expected = [
        (ordinal, item["key"])
        for ordinal, item in enumerate(expected_stages, start=1)
    ]
    if (
        len(actual) != len(expected)
        or [(ordinal, key) for ordinal, key, _digest in actual] != expected
        or any(not digest for _ordinal, _key, digest in actual)
    ):
        raise ProductionRunConflict({
            "idempotency_key": "已存在的 ProductionRun 初始化不完整；请使用新的幂等键",
        })


@transaction.atomic
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
    episode_count: int = 1,
    shots_per_episode: int | None = None,
    episode_duration_seconds: int | None = None,
    episode_ids: list[Any] | None = None,
    workflow_selection: dict[str, Any] | None = None,
) -> tuple[ProductionRun, bool]:
    if recipe.status != ProductionRecipeVersion.Status.PUBLISHED:
        raise ValidationError("只能使用 published recipe")
    stages = validate_production_recipe_executability(recipe)
    key = str(idempotency_key or "").strip()
    if not 1 <= len(key) <= 120:
        raise ValidationError({"idempotency_key": "长度必须为 1 到 120"})
    # Serialize creation per owner so two first-time requests cannot both pass
    # the pre-insert lookup. The unique constraint remains the final guard.
    type(user).objects.select_for_update().only("pk").get(pk=user.pk)
    ai_script = any(item["key"] == "script_generate" and item["mode"] == "ai" for item in stages)
    imported_script = any(
        item["key"] == "script_input" and item["mode"] == "confirmed_revision" for item in stages
    )
    downstream_only = not ai_script and not imported_script
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
    elif imported_script and (
        normalized_idea or normalized_style_hint
        or shots_per_episode is not None or episode_duration_seconds is not None
    ):
        raise ValidationError("导入剧本配方不能携带 AI 剧本输入")
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
    if not 1 <= int(episode_count) <= 100:
        raise ValidationError({"episode_count": "必须在 1 到 100 之间"})
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
    resolved_workflow_selection = _resolve_workflow_selection(
        project=project,
        stages=stages,
        selected_episode_ids=requested_episode_ids,
        requested=workflow_selection,
    )
    workflow_selection_sha256 = (
        _sha256(resolved_workflow_selection) if resolved_workflow_selection is not None else ""
    )

    base_config = resolve_configuration(project=project)
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
    request_payload = {
        "schema_version": 2,
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
        "episode_ids": requested_episode_ids,
        "workflow_selection": resolved_workflow_selection,
        "workflow_selection_sha256": workflow_selection_sha256,
    }
    request_sha = _sha256(request_payload)
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

    empty_contract = json_contract({"schema_version": 1})
    input_contract = json_contract(request_payload)
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
    config_contract = json_contract(config_snapshot)
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
    empty_stage_snapshot = json_contract({"schema_version": 1}, max_bytes=MAX_STAGE_SNAPSHOT_BYTES)
    for ordinal, item in enumerate(stages, start=1):
        routing_contract = json_contract(item["routing"], max_bytes=MAX_STAGE_SNAPSHOT_BYTES)
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
        decision_contract = json_contract(initial_decision, max_bytes=MAX_STAGE_SNAPSHOT_BYTES)
        stage_rows.append(StageRun(
            production_run=run,
            stage_key=item["key"],
            ordinal=ordinal,
            mode=item["mode"],
            idempotency_key=f"{run.id}:{ordinal}:{item['key']}",
            approval_required=item["approval"] == "required",
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
    contract = json_contract(metadata)
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
    )
    missing = [key for key in required if key not in values]
    if missing:
        raise StageBlocked("config_snapshot_missing", f"配置快照缺少: {', '.join(missing)}")
    project = run.project
    project.master_script = run.script_revision.content
    project.save(update_fields=("master_script", "updated_at"))
    result = breakdown_script(
        project,
        run.script_revision.content,
        episode_count_hint=int(run.input_snapshot.get("episode_count") or 1),
        default_shot_duration_ms=int(values["production.default_shot_duration_ms"]),
        default_first_frame_continuity=bool(values["production.default_first_frame_continuity"]),
        default_last_frame_continuity=bool(values["production.default_last_frame_continuity"]),
        video_shot_min_duration_ms=int(values["production.video_shot_min_duration_ms"]),
        video_shot_max_duration_ms=int(values["production.video_shot_max_duration_ms"]),
        auto_split_trigger_chars=int(values["production.script_auto_split_trigger_chars"]),
        auto_split_target_chars=int(values["production.script_auto_split_target_chars"]),
        multi_shot=bool(values["production.default_multi_shot"]),
        include_audio=bool(values["production.default_include_audio"]),
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
    contract = json_contract(metadata)
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
    return _sha256({
        "schema_version": 1,
        "prompt_type": str(getattr(prompt, "prompt_type", "") or ""),
        "mode": str(
            getattr(prompt, "h3_mode", "")
            or getattr(prompt, "requested_h3_mode", "")
            or payload.get("effective_conditioning_mode")
            or payload.get("mode")
            or ""
        ),
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


def _execute_prompt_compile(run: ProductionRun, stage: StageRun) -> str:
    values = _stage_config_values(stage)
    language_gate_mode = str(
        values.get("production.h3_prompt_language_gate") or ""
    ).strip().lower()
    if language_gate_mode not in {"block", "warn"}:
        raise StageBlocked(
            "config_snapshot_missing",
            "阶段配置缺少有效 production.h3_prompt_language_gate",
        )
    selected_ids = [item.id for item in _selected_episodes(run)]
    shots = list(
        run.project.episodes.filter(pk__in=selected_ids).order_by("number")
        .prefetch_related("shots__characters", "shots__props", "shots__reference_assets")
    )
    ordered_shots = [shot for episode in shots for shot in episode.shots.all()]
    if not ordered_shots:
        raise StageBlocked("storyboard_missing", "项目没有可编译的分镜")
    if len(ordered_shots) > run.max_generation_tasks:
        raise StageBlocked(
            "generation_task_budget_exceeded",
            f"预计 {len(ordered_shots)} 个生成任务，超过运行预算 {run.max_generation_tasks}。",
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
        try:
            compiled = compile_prompt_input(
                project=run.project,
                shots=[shot],
                prompt_type=shot.media_type,
                mode=shot.h3_mode,
                include_audio=shot.include_audio,
                language_gate_mode=language_gate_mode,
                model_profile_version=model_profile,
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


def _prepare_prompt_compile_language_gate_retry(
    run: ProductionRun,
    stage: StageRun,
) -> None:
    """Retire selected manifests produced by the historical pre-gate bug.

    PromptArtifact rows remain immutable project history.  Only the run-scoped
    manifest selection is changed, and only before any media attempt exists, so
    retry can compile a new authoritative revision without changing an already
    submitted request.
    """

    if stage.reason_code not in {
        "prompt_language_gate_blocked",
        "duplicate_shot_conditioning",
    }:
        return
    if GenerationAttempt.objects.select_for_update().filter(
        stage_run__production_run=run,
        stage_run__stage_key="media_generation",
    ).exists():
        raise ProductionRunRetryConflict(
            "prompt_compile 已存在媒体 attempt，不能替换其冻结 PromptArtifact。",
        )
    manifests = list(GeneratedArtifact.objects.select_for_update().filter(
        production_run=run,
        kind=GeneratedArtifact.Kind.PROMPT,
        role="compiled_generation_prompt",
        status=GeneratedArtifact.Status.SELECTED,
    ))
    if manifests:
        GeneratedArtifact.objects.filter(
            pk__in=[item.pk for item in manifests],
        ).update(
            status=GeneratedArtifact.Status.REJECTED,
            updated_at=timezone.now(),
        )


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
            "template", "model_profile_version",
        ).get(pk=route.get("workflow_version_id"))
    except (WorkflowTemplateVersion.DoesNotExist, ValueError) as exc:
        raise StageBlocked("frozen_route_unavailable", "冻结 route 的 workflow 已不可用") from exc
    workflow_snapshot = route.get("workflow_snapshot")
    if not isinstance(workflow_snapshot, dict):
        raise StageBlocked("frozen_workflow_snapshot_missing", "冻结 route 缺少可执行 workflow 快照")
    try:
        verify_json_contract(
            workflow_snapshot.get("graph"),
            schema_version=int(workflow_snapshot.get("graph_schema_version") or 0),
            size_bytes=int(workflow_snapshot.get("graph_size_bytes") or -1),
            sha256=str(workflow_snapshot.get("graph_sha256") or ""),
            max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
        )
        verify_json_contract(
            workflow_snapshot.get("input_contract"),
            schema_version=int(workflow_snapshot.get("input_contract_schema_version") or 0),
            size_bytes=int(workflow_snapshot.get("input_contract_size_bytes") or -1),
            sha256=str(workflow_snapshot.get("input_contract_sha256") or ""),
            max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
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


_MAX_MEDIA_EXECUTION_SEED = (1 << 63) - 1


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
    seed = int(_sha256(seed_material)[:16], 16) % _MAX_MEDIA_EXECUTION_SEED
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
    hard_concurrency_ceiling = int(settings.TASK_MAX_CONCURRENT)
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
) -> list[GenerationAttempt]:
    # A pricing/egress gate may be raised after some units have been prepared.
    # Keep materialization behind a savepoint so a caught StageBlocked cannot
    # commit taskless attempts or a partially reserved batch.
    prompts = _prompt_rows_for_run(run)
    if not prompts:
        raise StageBlocked("prompt_artifacts_missing", "没有本次运行的正式 PromptArtifact")
    _enforce_distinct_shot_conditioning(prompts)
    _enforce_prompt_language_gate(prompts)
    routing = _verify_stage_routing(stage)
    variant_count = int(routing.get("variant_count") or 1)
    requested_tasks = (
        len(unit_after_route)
        if unit_after_route is not None
        else len(prompts) * variant_count
    )
    if run.generation_task_count + requested_tasks > run.max_generation_tasks:
        raise StageBlocked("generation_task_budget_exceeded", "物化任务会超过本次运行任务预算")
    attempts = []
    for prompt in prompts:
        for variant_number in range(1, variant_count + 1):
            unit_key = f"prompt:{prompt.id}:variant:{variant_number}"
            if unit_after_route is not None and unit_key not in unit_after_route:
                continue
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
            width = int(values["production.default_resolution_width"])
            height = int(values["production.default_resolution_height"])
            if width <= 0 or height <= 0:
                raise StageBlocked(
                    "production_config_invalid",
                    "冻结的分辨率配置无效",
                )
            execution_identity = _media_execution_identity(
                run=run,
                prompt=prompt,
                variant_number=variant_number,
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
                    "variant_number": variant_number,
                    "execution_identity": deepcopy(execution_identity),
                    "route": deepcopy(route),
                    "payload": payload,
                    "execution_deadlines": {
                        "schema_version": 1,
                        "hard_timeout_seconds": float(values["production.task_timeout_seconds"]),
                        "idle_timeout_seconds": float(values["runtime.task_idle_timeout_seconds"]),
                    },
                }, max_bytes=MAX_STAGE_SNAPSHOT_BYTES)
                attempt = GenerationAttempt.objects.create(
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
            compiler_id = str((route.get("workflow_snapshot") or {}).get("compiler_id") or "")
            if compiler_id == "generic.comfy":
                binding_keys = {
                    str(item.get("semantic_key") or "")
                    for item in (
                        (route.get("workflow_snapshot") or {}).get("semantic_bindings")
                        or []
                    )
                    if isinstance(item, dict)
                }
                required_execution_keys = {
                    "image.width", "image.height", "sampling.seed", "output.prefix",
                }
                if not required_execution_keys.issubset(binding_keys):
                    raise StageBlocked(
                        "workflow_execution_identity_binding_missing",
                        "generic.comfy workflow 必须绑定 image.width、image.height、"
                        "sampling.seed 与 output.prefix",
                    )
                compiler_overrides = {
                    "image.width": width,
                    "image.height": height,
                    "sampling.seed": int(execution_identity["seed"]),
                    "output.prefix": str(execution_identity["output_prefix"]),
                }
                compiler_duration_policy = None
            elif compiler_id == "minimax_h3":
                compiler_overrides = {
                    "WIDTH": width,
                    "HEIGHT": height,
                    "SEED": int(execution_identity["seed"]),
                    "OUTPUT_PREFIX": str(execution_identity["output_prefix"]),
                }
                try:
                    compiler_duration_policy = video_shot_duration_policy(values=values)
                except ConfigurationResolutionError as exc:
                    raise StageBlocked(
                        exc.code,
                        f"冻结的视频分镜时长配置无效: {exc}",
                    ) from exc
            else:
                compiler_overrides = {}
                compiler_duration_policy = None
            try:
                compiled_workflow = compile_frozen_workflow(
                    workflow_snapshot=route["workflow_snapshot"],
                    prompt=prompt,
                    project=prompt.project,
                    overrides=compiler_overrides,
                    video_duration_policy=compiler_duration_policy,
                )
            except WorkflowCompilerError as exc:
                raise StageBlocked(
                    "workflow_semantic_compile_failed",
                    f"冻结 workflow semantic 编译失败: {exc}",
                ) from exc
            route_snapshot_sha256 = _sha256(route)
            request_contract = json_contract({
                "schema_version": 1,
                "operation": "media.generate",
                "prompt_artifact_id": str(prompt.id),
                "prompt_content_hash": prompt.content_hash,
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
                "output_contract": compiled_workflow.output_contract,
            })
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
            }
            attempt = GenerationAttempt.objects.create(
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
            )
            workflow = materialize_compiled_workflow_version(
                user=run.owner,
                workflow_version=version,
                prompt=prompt,
                compiled=compiled_workflow,
                route_snapshot_sha256=route_snapshot_sha256,
                workflow_snapshot=route["workflow_snapshot"],
            )
            task = materialize_generation_task(
                user=run.owner,
                workflow=workflow,
                priority=0,
                max_attempts=int(values["production.task_max_attempts"]),
                capability_snapshot_overrides={
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
    return f"generated-artifacts:{GeneratedArtifact.objects.filter(production_run=run, kind__in=('image', 'video')).count()}"


def _selected_assembly_sources(run: ProductionRun) -> dict[str, dict[str, str]]:
    """Return the selected, run-scoped media source for each formal Shot."""

    selected_sources: dict[str, dict[str, str]] = {}
    for artifact in GeneratedArtifact.objects.filter(
        production_run=run,
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
                owner=run.owner,
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
        owner=run.owner,
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


def _series_policy(deployment: ModelDeployment) -> dict[str, int | float]:
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
    if len(rows) > 100:
        raise StageBlocked(
            "prompt_normalization_batch_too_large",
            "本次 prompt.normalize 字段超过 100 项；首版禁止静默截断，请拆分 ProductionRun。",
        )
    return rows


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
    return {
        "payload": {"schema_version": 1, "items": items},
        "targets": targets,
    }


def _queue_prompt_normalization_attempt_for_route(
    run: ProductionRun,
    stage: StageRun,
    *,
    route: dict[str, Any],
    attempt_number: int,
    reason: str,
    frozen_input: dict[str, Any] | None = None,
    frozen_prompt_profile_version=None,
    frozen_prompt_profile_binding_key: str = "",
    frozen_prompt_profile_contract_sha256: str = "",
) -> GenerationAttempt:
    try:
        live_deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).get(pk=route["model_deployment_id"], status=ModelDeployment.Status.ACTIVE)
        deployment = frozen_text_deployment({"route": route})
    except (ModelDeployment.DoesNotExist, ValidationError, KeyError, ValueError) as exc:
        raise StageBlocked("frozen_route_unavailable", "冻结 prompt.normalize deployment 已不可用") from exc
    if not validate_prompt_normalization_model_profile(deployment.profile_version):
        raise StageBlocked("prompt_normalize_capability_missing", "冻结模型未声明 prompt.normalize")
    state, state_reason, normalization_route = prompt_normalization_route_state(
        live_deployment,
        execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
    )
    if state != "ready" or normalization_route is None:
        raise StageBlocked(state_reason, "prompt.normalize route 未通过 durable/fresh-smoke 门禁")
    smoke = route.get("smoke_evidence", {}).get(PROMPT_NORMALIZE_OPERATION)
    if (
        not isinstance(smoke, dict)
        or smoke.get("snapshot_id") != str(normalization_route.smoke.id)
        or smoke.get("target_sha256") != normalization_route.smoke.target_sha256
    ):
        raise StageBlocked("prompt_normalize_smoke_drift", "冻结 prompt.normalize smoke 已漂移")
    source_input = frozen_input or _build_prompt_normalization_input(run)
    payload = deepcopy(source_input.get("payload"))
    targets = source_input.get("targets")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("items"), list)
        or not payload["items"]
        or not isinstance(targets, list)
        or len(targets) != len(payload["items"])
    ):
        raise StageBlocked("prompt_normalize_input_invalid", "prompt.normalize 冻结输入无效")
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
            verify_frozen_profile_assembly(
                frozen_prompt_profile_version,
                payload.get("prompt_profile_assembly"),
            )
        else:
            resolved_profile = require_prompt_profile_binding(
                operation=PROMPT_NORMALIZE_OPERATION,
                model_family=model_family_for_profile(deployment.profile_version),
                model_profile_version=live_deployment.profile_version,
            )
            payload["prompt_profile_assembly"] = assemble_prompt_profile(
                resolved_profile.profile_version,
                sources=text_profile_sources(
                    project=run.project,
                    inputs=payload,
                    operation=PROMPT_NORMALIZE_OPERATION,
                ),
            )
            selected_profile_version = resolved_profile.profile_version
            selected_binding_key = resolved_profile.binding.key
            selected_contract_sha256 = resolved_profile.profile_version.contract_sha256
    except ValidationError as exc:
        raise StageBlocked(
            str(getattr(exc, "code", None) or "prompt_profile_invalid"),
            str(exc),
        ) from exc
    payload_contract = json_contract(payload, max_bytes=MAX_TEXT_SNAPSHOT_BYTES)
    snapshot = {
        "schema_version": 1,
        "payload": payload_contract.value,
        "payload_sha256": payload_contract.sha256,
        "targets": targets,
        "route": route,
        "submission_semantics": route.get("submission_semantics") or (
            PROMPT_NORMALIZATION_SUBMISSION_SEMANTICS
            if route.get("is_external") else "local_synchronous"
        ),
    }
    snapshot_contract = json_contract(snapshot, max_bytes=MAX_TEXT_SNAPSHOT_BYTES)
    if run.generation_task_count + 1 > run.max_generation_tasks:
        raise StageBlocked("generation_task_budget_exhausted", "prompt.normalize 会超过运行任务上限")
    attempt = GenerationAttempt.objects.create(
        stage_run=stage,
        unit_key=PROMPT_NORMALIZE_UNIT_KEY,
        attempt_number=attempt_number,
        model_deployment=live_deployment,
        prompt_profile_version=selected_profile_version,
        prompt_profile_binding_key=selected_binding_key,
        prompt_profile_contract_sha256=selected_contract_sha256,
        route_index=int(route["index"]),
        operation=PROMPT_NORMALIZE_OPERATION,
        is_external=bool(route.get("is_external")),
        status=GenerationAttempt.Status.QUEUED,
        request_sha256=payload_contract.sha256,
        request_snapshot=snapshot_contract.value,
        request_snapshot_schema_version=snapshot_contract.schema_version,
        request_snapshot_size_bytes=snapshot_contract.size_bytes,
        request_snapshot_sha256=snapshot_contract.sha256,
    )
    run.generation_task_count += 1
    run.save(update_fields=("generation_task_count", "updated_at"))
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
        unit_key=PROMPT_NORMALIZE_UNIT_KEY,
        route_index=int(route["index"]),
        reason=reason,
    )
    return attempt


def _prepare_prompt_normalization(run: ProductionRun, stage: StageRun) -> str | None:
    latest = stage.generation_attempts.filter(
        unit_key=PROMPT_NORMALIZE_UNIT_KEY,
    ).order_by("-attempt_number").first()
    if latest is not None:
        if latest.status == GenerationAttempt.Status.SUCCEEDED:
            snapshot = _verify_stage_snapshot(stage, "output_snapshot")
            if (
                snapshot.get("kind") != "prompt_normalization_candidates"
                or snapshot.get("attempt_id") != str(latest.id)
                or snapshot.get("result_sha256") != latest.result_sha256
            ):
                raise StageBlocked(
                    "prompt_normalization_materialization_missing",
                    "成功 attempt 缺少对应的审批候选快照",
                )
            return f"prompt-normalization-attempt:{latest.id}"
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
        safe_codes = {
            "text_attempt_pre_submit_validation_failed",
            "prompt_normalize_contract_failed",
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
            frozen_input = {
                "payload": deepcopy(latest.request_snapshot.get("payload")),
                "targets": deepcopy(latest.request_snapshot.get("targets")),
            }
            last_error = None
            for route in _route_candidates(
                stage,
                unit_key=PROMPT_NORMALIZE_UNIT_KEY,
                after_route_index=int(latest.route_index),
            ):
                try:
                    _queue_prompt_normalization_attempt_for_route(
                        run,
                        stage,
                        route=route,
                        attempt_number=latest.attempt_number + 1,
                        reason="ordered_fallback",
                        frozen_input=frozen_input,
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
                        unit_key=PROMPT_NORMALIZE_UNIT_KEY,
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


def _queue_text_attempt_for_route(
    run: ProductionRun,
    stage: StageRun,
    *,
    route: dict[str, Any],
    attempt_number: int,
    reason: str,
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
    policy = _series_policy(deployment)
    try:
        plan = build_series_concept_plan(
            idea=str(inputs.get("idea") or ""),
            style_hint=str(inputs.get("style_hint") or ""),
            episode_count=int(inputs.get("episode_count") or 0),
            shots_per_episode=int(inputs.get("shots_per_episode") or 0),
            episode_duration_seconds=int(inputs["episode_duration_seconds"]),
            minimum_shot_duration_ms=int(policy["minimum_shot_duration_ms"]),
            maximum_shot_duration_ms=int(policy["maximum_shot_duration_ms"]),
            model_identifier=deployment.profile_version.model_identifier,
            timeout_seconds=float(policy["timeout_seconds"]),
            configured_output_token_hint=int(policy["max_output_tokens"]),
            attempt=attempt_number,
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
    contract = json_contract(snapshot, max_bytes=MAX_TEXT_SNAPSHOT_BYTES)
    if run.generation_task_count + 1 > run.max_generation_tasks:
        raise StageBlocked("generation_task_budget_exhausted", "AI 剧本调用会超过运行任务上限")
    attempt = GenerationAttempt.objects.create(
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
    try:
        contract = json_contract(snapshot, max_bytes=MAX_TEXT_SNAPSHOT_BYTES)
        provider_request = structure_provider_payload(plan)
        json_contract(provider_request, max_bytes=MAX_TEXT_SNAPSHOT_BYTES * 2)
    except ValidationError as exc:
        raise StageBlocked(
            "script_structure_input_too_large",
            "完整剧本或装配策略超过受控请求上限；不会截断后提交",
        ) from exc
    if run.generation_task_count + 1 > run.max_generation_tasks:
        raise StageBlocked(
            "generation_task_budget_exhausted",
            "剧本结构化调用会超过运行任务上限",
        )
    attempt = GenerationAttempt.objects.create(
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
    apply_frozen_script_bible(project=run.project, frozen=frozen)


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
    if expected_source_snapshot_sha256 != json_contract(redact_sensitive_bindings(snapshot)).sha256:
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
    overrides = dict(candidate_overrides or {})
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
    if attempt_id is None:
        if candidates:
            raise ValidationError("无提交的 script_normalize 快照不能包含候选")
        return
    attempt = GenerationAttempt.objects.select_for_update().filter(
        pk=attempt_id,
        stage_run=normalize_stage,
        operation=PROMPT_NORMALIZE_OPERATION,
    ).first()
    if (
        attempt is None
        or attempt.status != GenerationAttempt.Status.SUCCEEDED
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
        result_sha256=attempt.result_sha256,
        submission_semantics=str(payload.get("submission_semantics") or ""),
    )
    if len(materialized) != len(reviewed_candidates):
        raise ValidationError("script_normalize 审批物化数量不一致")


def _execute_stage(run: ProductionRun, stage: StageRun) -> str | None:
    if stage.mode == "skip":
        return "skipped"
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
            kind__in=(GeneratedArtifact.Kind.IMAGE, GeneratedArtifact.Kind.VIDEO),
        )
        if not artifacts.exists():
            raise ValidationError("媒体审批前没有正式 GeneratedArtifact")
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
        if int(routing.get("variant_count") or 1) > 1 and not routing.get("evaluator"):
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


@transaction.atomic
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
    if run.started_at is None:
        run.started_at = timezone.now()
    for stage in stages:
        if stage.status == StageRun.Status.SUCCEEDED:
            continue
        run.current_stage_key = stage.stage_key
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
                "prompt_compile", "media_generation", "artifact_selection",
                "assembly", "delivery", "release",
            }:
                _assert_frozen_formal_objects(run)
            _freeze_stage_config(run, stage)
            _freeze_stage_input(run, stage)
            output_reference = _execute_stage(run, stage)
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


@transaction.atomic
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
) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    stage = StageRun.objects.select_for_update().filter(
        production_run=run,
        stage_key=stage_key,
    ).first()
    if stage is None:
        raise ValidationError({"stage_key": "ProductionRun 不包含该阶段"})
    if stage.status != StageRun.Status.AWAITING_APPROVAL:
        raise ValidationError("该阶段当前不处于 awaiting_approval")
    if decision not in ApprovalDecision.Decision.values:
        raise ValidationError("decision 必须为 approve/reject")
    if decision == ApprovalDecision.Decision.APPROVE:
        if stage.stage_key == "storyboard_review":
            _assert_storyboard_review_source(
                run,
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
            )
        elif expected_source_snapshot_sha256 and stage.stage_key != "script_normalize_review":
            raise ValidationError(
                "expected_source_snapshot_sha256 只允许用于 storyboard_review 或 script_normalize_review"
            )
        if stage.stage_key == "script_review":
            if candidate_overrides:
                raise ValidationError("只有 script_normalize_review 支持 candidate_overrides")
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
        elif stage.stage_key == "bible_review" and run.stages.filter(
            stage_key="bible_materialize",
        ).exists():
            if candidate_overrides:
                raise ValidationError("只有 script_normalize_review 支持 candidate_overrides")
            if adopt_generated_metadata:
                raise ValidationError("adopt_generated_metadata 只允许用于 script_review")
            _approve_bible_candidates(run)
        elif candidate_overrides or adopt_generated_metadata:
            raise ValidationError(
                "candidate_overrides 只允许用于 script_normalize_review；"
                "adopt_generated_metadata 只允许用于 script_review",
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
        output_snapshot_sha256 = ""
    revision = (
        stage.approval_decisions.aggregate(value=Max("revision"))["value"]
        or 0
    ) + 1
    approval = ApprovalDecision.objects.create(
        stage_run=stage,
        revision=revision,
        decision=decision,
        comment=str(comment or "")[:4000],
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
        stage.reason = str(comment or "阶段被拒绝")[:2000]
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
        {TEXT_OPERATION, SCRIPT_STRUCTURE_OPERATION, PROMPT_NORMALIZE_OPERATION}
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


# Kept as an internal import compatibility alias while the public endpoint has
# already become transport-neutral.
resolve_uncertain_text_attempt = resolve_uncertain_attempt


@transaction.atomic
def retry_production_run(run: ProductionRun, *, user) -> ProductionRun:
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    if run.status not in {ProductionRun.Status.BLOCKED, ProductionRun.Status.FAILED}:
        raise ValidationError("只有 blocked/failed ProductionRun 可以 retry")
    stage = run.stages.select_for_update().filter(stage_key=run.current_stage_key).first()
    if stage is None or stage.status not in {StageRun.Status.BLOCKED, StageRun.Status.FAILED}:
        raise ValidationError("当前阶段不可 retry")
    if stage.stage_key in {"script_generate", "script_structure", "script_normalize"}:
        unit_key = {
            "script_generate": TEXT_UNIT_KEY,
            "script_structure": SCRIPT_STRUCTURE_UNIT_KEY,
            "script_normalize": PROMPT_NORMALIZE_UNIT_KEY,
        }[stage.stage_key]
        latest = stage.generation_attempts.filter(unit_key=unit_key).order_by("-attempt_number").first()
        if latest is None or latest.status != GenerationAttempt.Status.FAILED:
            raise ValidationError("当前文本 attempt 不可 retry")
        if latest.error_code == "uncertain_remote_response":
            raise ProductionRunRetryConflict(
                "远端提交状态不确定，普通 retry 被禁止；必须由管理员先完成远端缺失核验。"
            )
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
        if run.generation_task_count + 1 > run.max_generation_tasks:
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
                frozen_input={
                    "payload": deepcopy(snapshot.get("payload")),
                    "targets": deepcopy(snapshot.get("targets")),
                },
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
            else:
                request["seed"] = int(snapshot["plan"]["source_fingerprint"][:8], 16) + next_number
                retry_unit_key = SCRIPT_STRUCTURE_UNIT_KEY
                retry_operation = SCRIPT_STRUCTURE_OPERATION
                retry_payload = structure_provider_payload(snapshot["plan"])
            contract = json_contract(snapshot, max_bytes=MAX_TEXT_SNAPSHOT_BYTES)
            new_attempt = GenerationAttempt.objects.create(
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
    if stage.stage_key == "prompt_compile":
        _prepare_prompt_compile_language_gate_retry(run, stage)
    if stage.stage_key == "media_generation":
        # Retry is a new submission decision. Re-evaluate the frozen run prompts
        # so a historical run cannot bypass the same executable-conditioning and
        # language gates used for the initial materialization path.
        try:
            prompts = _prompt_rows_for_run(run)
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
                    max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
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
                if run.generation_task_count + new_attempt_count + 1 > run.max_generation_tasks:
                    raise ValidationError("retry 会超过 ProductionRun 任务预算")
                snapshot = deepcopy(attempt.request_snapshot)
                next_attempt_number = attempt.attempt_number + 1
                new_id = uuid.uuid4()
                snapshot["payload"]["idempotency_key"] = f"production-attempt:{new_id}"
                snapshot["variant_number"] = snapshot.get("variant_number") or next_attempt_number
                request_contract = json_contract(snapshot, max_bytes=MAX_STAGE_SNAPSHOT_BYTES)
                new_attempt = GenerationAttempt.objects.create(
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
                _estimate_and_reserve(
                    run, stage, new_attempt,
                    deployment=frozen_async_deployment(snapshot["route"]),
                    operation=attempt.operation,
                    payload=snapshot["payload"],
                    is_external=attempt.is_external,
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
            if run.generation_task_count + retried + 1 > run.max_generation_tasks:
                raise ValidationError("retry 会超过 ProductionRun 任务预算")
            try:
                verify_json_contract(
                    attempt.request_snapshot,
                    schema_version=attempt.request_snapshot_schema_version,
                    size_bytes=attempt.request_snapshot_size_bytes,
                    sha256=attempt.request_snapshot_sha256,
                    max_bytes=MAX_STAGE_SNAPSHOT_BYTES,
                )
            except (ValidationError, TypeError, ValueError) as exc:
                raise ValidationError("原媒体请求快照缺失或已漂移，不能安全 retry") from exc
            if attempt.request_sha256 != attempt.request_snapshot_sha256:
                raise ValidationError("原媒体请求 identity 与冻结快照不一致")
            new_attempt = GenerationAttempt.objects.create(
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
                )
            except StageBlocked as exc:
                stage.status = (
                    StageRun.Status.AWAITING_APPROVAL if exc.awaiting_approval else StageRun.Status.BLOCKED
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
            new_task = clone_generation_task_for_retry(
                task,
                user=user,
                extended_generation_confirmed=True,
            )
            new_attempt.generation_task = new_task
            new_attempt.save(update_fields=("generation_task", "updated_at"))
            retried += 1
            new_attempt_count += 1
        if not retried:
            raise ValidationError("没有可安全自动 retry 的生成任务")
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
                    owner=run.owner,
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
        operation__in=(TEXT_OPERATION, SCRIPT_STRUCTURE_OPERATION),
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
        comment=(
            f"费用/外发授权: allow_external_calls={run.allow_external_calls}, "
            f"budget_limit={run.budget_limit}, approved_estimated_cost="
            f"{stage.approved_estimated_cost} {stage.approved_cost_currency}; {comment or ''}"
        )[:4000],
        decided_by=user,
    )
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
