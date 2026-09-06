from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    GenerationAttempt,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelDeploymentSmokeAttemptResolution,
    ModelProfileVersion,
    ProviderProfile,
    SeriesProject,
)
from studio.services.async_media_contract import deployment_target_sha256, fresh_smoke_snapshot
from studio.services.configuration import resolve_configuration
from studio.services.deployment_smoke import record_deployment_capability_snapshot
from studio.services.deployment_smoke_contract import (
    DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
)
from studio.services.deployment_smoke_resolution import (
    resolve_deployment_smoke_attempt,
    unresolved_deployment_smoke_attempt,
)
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION
from studio.services.prompt_normalization_contract import (
    FinalizedPromptNormalizationResponse,
    PROMPT_NORMALIZATION_POLICY_KEY,
    PromptNormalizationContractError,
    prompt_normalization_chat_messages_input_bytes,
    prompt_normalization_chat_payload,
    prompt_normalization_input_warnings,
    prompt_normalization_policy_from_specification,
    prompt_normalization_usage_warnings,
    safe_prompt_normalization_usage,
    safe_prompt_field_validation_context,
    safe_prompt_field_validation_reason_code,
    validate_prompt_normalization_items,
)
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache
from studio.services.prompt_normalization_smoke import (
    representative_prompt_normalization_smoke_payload,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    deployment_supports_script_generation,
    driver_for,
    resolve_credential,
)


def _prompt_normalization_smoke_snapshot_max_bytes() -> int:
    return boundary_int(
        "security.prompt_normalization.smoke_snapshot_max_bytes",
    )


def _text_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.text_max_bytes")


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _text_provider_request_max_bytes() -> int:
    return boundary_int("security.snapshot.text_provider_request_max_bytes")


def prompt_normalization_smoke_snapshot_max_bytes() -> int:
    return _prompt_normalization_smoke_snapshot_max_bytes()


DEFAULT_DEPLOYMENT_CONFIG_KEY = "production.prompt_normalization_deployment_id"
ROUTE_CONTEXT_STANDALONE = "standalone"
ROUTE_CONTEXT_DURABLE_ATTEMPT = "durable_attempt"
ROUTE_CONTEXT_DEPLOYMENT_SMOKE = "deployment_smoke"
ROUTE_CONTEXTS = frozenset({
    ROUTE_CONTEXT_STANDALONE,
    ROUTE_CONTEXT_DURABLE_ATTEMPT,
    ROUTE_CONTEXT_DEPLOYMENT_SMOKE,
})
SUBMISSION_SEMANTICS = DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS


@dataclass(frozen=True)
class PromptNormalizationRoute:
    deployment: ModelDeployment
    is_external: bool
    estimate_amount: Decimal | None
    estimate_currency: str
    estimate_basis: str
    smoke: ModelDeploymentCapabilitySnapshot | None

    @property
    def transport(self) -> str:
        return "remote_api" if self.is_external else "local_endpoint"


def _declared_capabilities(profile: ModelProfileVersion) -> set[str]:
    specification = verified_model_profile_specification(profile)
    values = specification.get("capabilities")
    if not isinstance(values, list):
        return set()
    return {str(item).strip() for item in values if str(item).strip()}


def validate_prompt_normalization_model_profile(
    profile: ModelProfileVersion,
) -> dict[str, Any] | None:
    """Validate an exact prompt.normalize declaration before publication."""

    specification = verified_model_profile_specification(profile)
    declared = specification.get("capabilities")
    aliases = {
        field
        for field in ("operations", "protocols")
        if PROMPT_NORMALIZE_OPERATION in (specification.get(field) or [])
    }
    if aliases:
        raise ValidationError({
            "specification": (
                "prompt.normalize 只能在 specification.capabilities 中声明；"
                f"禁止兼容字段: {', '.join(sorted(aliases))}"
            ),
        })
    if not isinstance(declared, list) or PROMPT_NORMALIZE_OPERATION not in declared:
        if PROMPT_NORMALIZATION_POLICY_KEY in specification:
            raise ValidationError({
                "specification": (
                    "只有精确声明 prompt.normalize 能力时才能配置 "
                    "specification.prompt_normalization"
                ),
            })
        return None
    if any(not isinstance(item, str) or not item.strip() for item in declared):
        raise ValidationError({"specification": "capabilities 必须是非空字符串数组"})
    if len(declared) != len(set(declared)):
        raise ValidationError({"specification": "capabilities 不能重复"})
    if profile.modality not in {
        ModelProfileVersion.Modality.TEXT,
        ModelProfileVersion.Modality.MULTIMODAL,
    }:
        raise ValidationError({"modality": "prompt.normalize 只支持 text/multimodal 模型"})
    driver = driver_for(profile.provider)
    operations = set((driver.capabilities() or {}).get("operations") or [])
    if PROMPT_NORMALIZE_OPERATION not in operations:
        raise ValidationError({
            "specification": "provider driver 未实现 prompt.normalize",
        })
    try:
        policy = prompt_normalization_policy_from_specification(
            specification,
            required=True,
        )
    except PromptNormalizationContractError as exc:
        raise ValidationError({"specification": str(exc)}) from exc
    assert policy is not None
    return policy


def _combined_config(deployment: ModelDeployment) -> dict[str, Any]:
    provider_contract = verify_json_contract(
        deployment.provider.driver_config,
        schema_version=deployment.provider.driver_config_schema_version,
        size_bytes=deployment.provider.driver_config_size_bytes,
        sha256=deployment.provider.driver_config_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    deployment_contract = verify_json_contract(
        deployment.runtime_config,
        schema_version=deployment.runtime_config_schema_version,
        size_bytes=deployment.runtime_config_size_bytes,
        sha256=deployment.runtime_config_sha256,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    return {
        **(provider_contract.value or {}),
        **(deployment_contract.value or {}),
    }


def _is_external(deployment: ModelDeployment) -> bool:
    hostname = (
        urlparse(str(_combined_config(deployment).get("base_url") or "")).hostname or ""
    ).casefold()
    if not hostname:
        raise ValidationError("prompt.normalize deployment 缺少有效 base_url")
    return hostname not in {"localhost", "127.0.0.1", "::1"}


def _capability_external_flag(deployment: ModelDeployment) -> bool:
    """Fail closed without exposing invalid or unsigned deployment config."""

    try:
        return _is_external(deployment)
    except (ValidationError, TypeError, ValueError):
        return True


def _estimate(deployment: ModelDeployment) -> tuple[Decimal | None, str, str]:
    raw = driver_for(deployment.provider).estimate(
        deployment,
        PROMPT_NORMALIZE_OPERATION,
        {"schema_version": 1, "item_count": 1},
    )
    amount = raw.get("amount") if isinstance(raw, dict) else None
    if amount is None:
        parsed = None
    else:
        try:
            parsed = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ProviderDriverError(
                "prompt.normalize pricing 无效",
                code="provider_pricing_invalid",
            ) from exc
        if not parsed.is_finite() or parsed < 0:
            raise ProviderDriverError(
                "prompt.normalize pricing 无效",
                code="provider_pricing_invalid",
            )
    return parsed, str(raw.get("currency") or ""), str(raw.get("basis") or "unknown")


def _credential_ready(deployment: ModelDeployment, *, external: bool) -> bool:
    reference = deployment.credential or deployment.provider.credential
    if not reference:
        return bool(
            not external
            and deployment.provider.driver_type == ProviderProfile.DriverType.LOCAL_QWEN
        )
    try:
        resolve_credential(reference)
    except ProviderDriverError:
        return False
    return True


def unresolved_prompt_normalization_smoke_attempt(
    deployment: ModelDeployment,
) -> ModelDeploymentSmokeAttempt | None:
    """Return the newest smoke submission that still requires operator evidence."""

    return unresolved_deployment_smoke_attempt(
        deployment,
        operation=PROMPT_NORMALIZE_OPERATION,
    )


def route_state(
    deployment: ModelDeployment,
    *,
    execution_context: str = ROUTE_CONTEXT_STANDALONE,
) -> tuple[str, str, PromptNormalizationRoute | None]:
    if execution_context not in ROUTE_CONTEXTS:
        return "unavailable", "prompt_normalize_execution_context_invalid", None
    if deployment.status != ModelDeployment.Status.ACTIVE:
        return "unavailable", "deployment_inactive", None
    profile = deployment.profile_version
    if profile.status != ModelProfileVersion.Status.PUBLISHED:
        return "unavailable", "model_version_not_published", None
    if not deployment.provider.is_active:
        return "unavailable", "provider_inactive", None
    if profile.modality not in {
        ModelProfileVersion.Modality.TEXT,
        ModelProfileVersion.Modality.MULTIMODAL,
    }:
        return "unavailable", "text_modality_required", None
    try:
        declared_capabilities = _declared_capabilities(profile)
    except ValidationError:
        return "unavailable", "model_profile_contract_invalid", None
    if PROMPT_NORMALIZE_OPERATION not in declared_capabilities:
        return "unavailable", "capability_not_declared", None
    try:
        external = _is_external(deployment)
        if external and execution_context == ROUTE_CONTEXT_STANDALONE:
            return "unavailable", "external_durable_attempt_required", None
        driver = driver_for(deployment.provider)
        driver.validate_config(_combined_config(deployment))
        if PROMPT_NORMALIZE_OPERATION not in set(
            (driver.capabilities() or {}).get("operations") or [],
        ):
            return "unavailable", "driver_capability_missing", None
        if not deployment_supports_script_generation(deployment):
            return "unavailable", "controlled_chat_transport_missing", None
        if not _credential_ready(deployment, external=external):
            return "unavailable", "credential_unavailable", None
        amount, currency, basis = _estimate(deployment)
    except (ProviderDriverError, ValidationError, TypeError, ValueError):
        return "unavailable", "deployment_configuration_invalid", None
    smoke = fresh_smoke_snapshot(deployment, PROMPT_NORMALIZE_OPERATION)
    route = PromptNormalizationRoute(
        deployment=deployment,
        is_external=external,
        estimate_amount=amount,
        estimate_currency=currency,
        estimate_basis=basis,
        smoke=smoke,
    )
    if unresolved_prompt_normalization_smoke_attempt(deployment) is not None:
        return "unavailable", "unresolved_smoke_submission", route
    if smoke is None:
        return "smoke_test_required", "prompt_normalize_smoke_required", route
    return "ready", "ready", route


def prompt_normalization_capability(
    *,
    project: SeriesProject | None = None,
) -> dict[str, Any]:
    resolved = resolve_configuration(
        project=project,
        required_keys=(DEFAULT_DEPLOYMENT_CONFIG_KEY,),
    )
    configured_default = str(resolved.values[DEFAULT_DEPLOYMENT_CONFIG_KEY] or "")
    rows = []
    deployments = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).filter(
        profile_version__modality__in=(
            ModelProfileVersion.Modality.TEXT,
            ModelProfileVersion.Modality.MULTIMODAL,
        ),
    ).order_by("-is_default", "-priority", "key")
    for deployment in deployments:
        state, reason_code, route = route_state(deployment)
        external = route.is_external if route else _capability_external_flag(deployment)
        rows.append({
            "model_deployment_id": str(deployment.id),
            "deployment_key": deployment.key,
            "label": deployment.name,
            "provider_key": deployment.provider.key,
            "provider_label": deployment.provider.name,
            "provider_driver": deployment.provider.driver_type,
            "model_profile_version_id": str(deployment.profile_version_id),
            "model_identifier": deployment.profile_version.model_identifier,
            "capability": PROMPT_NORMALIZE_OPERATION,
            "state": state,
            "ready": state == "ready",
            "reason_code": reason_code,
            "transport": "remote_api" if external else "local_endpoint",
            "data_egress": external,
            "submission_semantics": (
                SUBMISSION_SEMANTICS if external else "local_synchronous"
            ),
            "data_egress_notice": (
                "该路由会把待翻译文本与必要上下文发送到外部 Provider。"
                if external else ""
            ),
            "estimated_cost": (
                str(route.estimate_amount) if route and route.estimate_amount is not None else None
            ),
            "cost_currency": route.estimate_currency if route else "",
            "cost_basis": route.estimate_basis if route else "unknown",
            "smoke_snapshot_id": (
                str(route.smoke.id) if route and route.smoke else None
            ),
            "smoke_expires_at": route.smoke.expires_at if route and route.smoke else None,
        })
    public_contract = json_contract({
        "schema_version": 1,
        "default_model_deployment_id": configured_default or None,
    }, max_bytes=_text_snapshot_max_bytes())
    return {
        "schema_version": 2,
        "capability_key": PROMPT_NORMALIZE_OPERATION,
        "review_required": True,
        "default_model_deployment_id": configured_default or None,
        "configuration_public_sha256": public_contract.sha256,
        "deployments": rows,
    }


def resolve_prompt_normalization_route(
    deployment_id,
    *,
    require_smoke: bool = True,
    execution_context: str = ROUTE_CONTEXT_STANDALONE,
) -> PromptNormalizationRoute:
    try:
        deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).get(pk=deployment_id)
    except (ModelDeployment.DoesNotExist, ValueError, TypeError) as exc:
        raise ProviderDriverError(
            "所选 prompt.normalize deployment 不存在",
            code="model_deployment_not_found",
        ) from exc
    state, reason, route = route_state(
        deployment,
        execution_context=execution_context,
    )
    if route is None or state == "unavailable":
        raise ProviderDriverError(
            f"所选 prompt.normalize deployment 不可用: {reason}",
            code=reason,
        )
    if require_smoke and state != "ready":
        raise ProviderDriverError(
            "所选 prompt.normalize deployment 缺少未过期真实烟测",
            code=reason,
        )
    return route


def invoke_prompt_normalization(
    *,
    deployment_id,
    payload: dict[str, Any],
    execution_context: str = ROUTE_CONTEXT_STANDALONE,
    generation_attempt: GenerationAttempt | None = None,
    smoke_attempt: ModelDeploymentSmokeAttempt | None = None,
    execution_deployment: ModelDeployment | None = None,
) -> dict[str, Any]:
    route = resolve_prompt_normalization_route(
        deployment_id,
        execution_context=execution_context,
    )
    payload_contract = json_contract(
        payload,
        max_bytes=_text_provider_request_max_bytes(),
    )
    if execution_context == ROUTE_CONTEXT_DURABLE_ATTEMPT:
        persisted_attempt = (
            GenerationAttempt.objects.filter(pk=getattr(generation_attempt, "pk", None)).first()
            if generation_attempt is not None else None
        )
        if (
            persisted_attempt is None
            or persisted_attempt.operation != PROMPT_NORMALIZE_OPERATION
            or persisted_attempt.model_deployment_id != route.deployment.id
            or persisted_attempt.status != GenerationAttempt.Status.SUBMITTED
            or persisted_attempt.request_sha256 != payload_contract.sha256
        ):
            raise ProviderDriverError(
                "外部 prompt.normalize 必须绑定已持久化且已进入提交边界的 GenerationAttempt",
                code="durable_generation_attempt_required",
            )
    elif execution_context == ROUTE_CONTEXT_DEPLOYMENT_SMOKE:
        persisted_smoke_attempt = (
            ModelDeploymentSmokeAttempt.objects.filter(
                pk=getattr(smoke_attempt, "pk", None),
            ).first()
            if smoke_attempt is not None else None
        )
        if (
            persisted_smoke_attempt is None
            or persisted_smoke_attempt.operation != PROMPT_NORMALIZE_OPERATION
            or persisted_smoke_attempt.deployment_id != route.deployment.id
            or persisted_smoke_attempt.status != ModelDeploymentSmokeAttempt.Status.SUBMITTING
            or persisted_smoke_attempt.request_sha256 != payload_contract.sha256
        ):
            raise ProviderDriverError(
                "外部 prompt.normalize smoke 必须绑定已持久化的单次提交记录",
                code="durable_smoke_attempt_required",
            )
    if execution_deployment is not None:
        if execution_context != ROUTE_CONTEXT_DURABLE_ATTEMPT:
            raise ProviderDriverError(
                "冻结 execution deployment 只允许用于 ProductionRun durable attempt",
                code="frozen_execution_context_invalid",
            )
        if (
            execution_deployment.id != route.deployment.id
            or deployment_target_sha256(execution_deployment)
            != deployment_target_sha256(route.deployment)
        ):
            raise ProviderDriverError(
                "冻结 prompt.normalize deployment 已与当前烟测目标漂移",
                code="frozen_deployment_drift",
            )
        route = PromptNormalizationRoute(
            deployment=execution_deployment,
            is_external=route.is_external,
            estimate_amount=route.estimate_amount,
            estimate_currency=route.estimate_currency,
            estimate_basis=route.estimate_basis,
            smoke=route.smoke,
        )
    return _invoke_resolved_prompt_normalization(route=route, payload=payload)


def _invoke_resolved_prompt_normalization(
    *,
    route: PromptNormalizationRoute,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke one already-admitted route exactly once.

    The caller owns the durable submission boundary.  Deployment smoke uses
    this helper after persisting its own ``submitting`` barrier, avoiding a
    second route check that would correctly see that barrier as unresolved.
    """

    route_metadata = {
        "model_deployment_id": str(route.deployment.id),
        "model_profile_version_id": str(route.deployment.profile_version_id),
        "model_identifier": route.deployment.profile_version.model_identifier,
        "specification_sha256": route.deployment.profile_version.specification_sha256,
        "provider_profile_id": str(route.deployment.profile_version.provider_id),
        "provider": route.deployment.provider.key,
        "provider_driver": route.deployment.provider.driver_type,
        "transport": route.transport,
        "data_egress": route.is_external,
        "smoke_snapshot_id": str(route.smoke.id) if route.smoke else None,
        "smoke_target_sha256": route.smoke.target_sha256 if route.smoke else "",
        "estimated_cost": (
            str(route.estimate_amount) if route.estimate_amount is not None else None
        ),
        "cost_currency": route.estimate_currency,
        "cost_basis": route.estimate_basis,
        "submission_semantics": (
            SUBMISSION_SEMANTICS if route.is_external else "local_synchronous"
        ),
    }
    capacity_policy = prompt_normalization_policy_from_specification(
        verified_model_profile_specification(route.deployment.profile_version),
        required=True,
    )
    assert capacity_policy is not None
    try:
        result = driver_for(route.deployment.provider).invoke(
            route.deployment,
            PROMPT_NORMALIZE_OPERATION,
            payload,
        )
        if not isinstance(result, dict):
            raise PromptNormalizationContractError("prompt.normalize 响应必须是对象")
        rows = result.get("items")
        if isinstance(result, FinalizedPromptNormalizationResponse):
            validated_items = result.finalized_items_for(payload)
            if validated_items is None:
                raise PromptNormalizationContractError(
                    "prompt.normalize 已完成结果的冻结请求或校验和已漂移",
                    code="prompt_normalization_finalization_proof_invalid",
                )
            # The registered text driver already applied the strict Provider
            # wire validator and restored server-owned literals. Treating
            # these finalized rows as wire input again would reject the exact
            # literals that the first pass was required to restore.
        else:
            # Plain driver rows (including deterministic test drivers) are
            # still Provider-wire candidates and retain the full strict check.
            validated_items = validate_prompt_normalization_items(payload, rows)
        if not isinstance(validated_items, list):
            raise PromptNormalizationContractError(
                "prompt.normalize 已完成结果缺少 items",
                code="prompt_normalization_finalization_proof_invalid",
            )
        result["items"] = validated_items
        incoming_metadata = (
            result.get("metadata")
            if isinstance(result.get("metadata"), dict)
            else {}
        )
        usage = safe_prompt_normalization_usage(incoming_metadata.get("usage"))
        field_warnings = [
            warning
            for item in validated_items
            for warning in item.get("warnings", [])
        ]
        warnings = [
            *field_warnings,
            *prompt_normalization_input_warnings(
                capacity_policy,
                prompt_normalization_chat_payload(
                    payload,
                    policy=capacity_policy,
                )["messages"],
            ),
            *prompt_normalization_usage_warnings(capacity_policy, usage),
        ]
        response_envelope = str(
            incoming_metadata.get("response_envelope") or "",
        )
        result["metadata"] = {
            **({"usage": usage} if usage else {}),
            **(
                {"response_envelope": response_envelope}
                if response_envelope in {"json", "json_code_fence"}
                else {}
            ),
            **({"warnings": warnings} if warnings else {}),
        }
    except PromptNormalizationContractError as exc:
        error = ProviderDriverError(
            "prompt.normalize 响应未通过共享契约校验",
            code="prompt_normalize_contract_failed",
        )
        reason_code = safe_prompt_field_validation_reason_code(
            getattr(exc, "reason_code", ""),
        )
        validation_context = safe_prompt_field_validation_context(
            reason_code,
            getattr(exc, "validation_context", None),
            request_payload=payload,
        )
        error.metadata = {
            **route_metadata,
            "contract_error_code": str(
                getattr(exc, "code", "prompt_normalize_contract_invalid")
            ),
            **({"reason_code": reason_code} if reason_code else {}),
            **(
                {"validation_context": validation_context}
                if validation_context else {}
            ),
        }
        raise error from exc
    except ProviderDriverError as exc:
        existing_metadata = (
            dict(exc.metadata)
            if isinstance(getattr(exc, "metadata", None), dict)
            else {}
        )
        reason_code = safe_prompt_field_validation_reason_code(
            existing_metadata.pop("reason_code", ""),
        )
        validation_context = safe_prompt_field_validation_context(
            reason_code,
            existing_metadata.pop("validation_context", None),
            request_payload=payload,
        )
        exc.metadata = {
            **existing_metadata,
            **({"reason_code": reason_code} if reason_code else {}),
            **(
                {"validation_context": validation_context}
                if validation_context else {}
            ),
            **route_metadata,
        }
        raise
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    result["metadata"] = {**metadata, **route_metadata}
    return result


def _provider_request_id_sha256(result: dict[str, Any] | None) -> str:
    request_id = str((result or {}).get("request_id") or "")
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest() if request_id else ""


def _prompt_normalization_smoke_execution_route(
    deployment: ModelDeployment,
) -> PromptNormalizationRoute:
    """Resolve eligibility without consulting the attempt created as a barrier."""

    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    profile = deployment.profile_version
    if deployment.status != ModelDeployment.Status.ACTIVE:
        raise ValidationError("prompt.normalize 烟测只接受 active deployment")
    if profile.status != ModelProfileVersion.Status.PUBLISHED:
        raise ValidationError("prompt.normalize 烟测只接受已发布 model profile")
    if not deployment.provider.is_active:
        raise ValidationError("prompt.normalize provider 已停用")
    if not validate_prompt_normalization_model_profile(profile):
        raise ValidationError({
            "specification": "model profile 未精确声明 prompt.normalize 能力",
        })
    external = _is_external(deployment)
    driver = driver_for(deployment.provider)
    driver.validate_config(_combined_config(deployment))
    if PROMPT_NORMALIZE_OPERATION not in set(
        (driver.capabilities() or {}).get("operations") or [],
    ):
        raise ValidationError("provider driver 未实现 prompt.normalize")
    if not deployment_supports_script_generation(deployment):
        raise ValidationError("deployment 没有受控 chat_completion transport")
    if not _credential_ready(deployment, external=external):
        raise ValidationError("prompt.normalize 受管凭据不可用")
    amount, currency, basis = _estimate(deployment)
    if external and amount is None:
        raise ValidationError("真实 smoke 前必须配置可确定 pricing")
    return PromptNormalizationRoute(
        deployment=deployment,
        is_external=external,
        estimate_amount=amount,
        estimate_currency=currency,
        estimate_basis=basis,
        smoke=fresh_smoke_snapshot(deployment, PROMPT_NORMALIZE_OPERATION),
    )


@security_boundary_resolution_cache()
def prepare_prompt_normalization_smoke_request(
    deployment: ModelDeployment,
    *, prompt_profile_binding_id=None,
) -> dict[str, Any]:
    """Freeze the exact normalization probe before any provider submission."""

    from studio.services.deployment_smoke_profiles import select_smoke_profile, smoke_profile_reference
    selected = select_smoke_profile(deployment, PROMPT_NORMALIZE_OPERATION, prompt_profile_binding_id)
    route = _prompt_normalization_smoke_execution_route(deployment)
    if unresolved_prompt_normalization_smoke_attempt(route.deployment) is not None:
        raise ValidationError({
            "code": "unresolved_smoke_submission",
            "detail": "已有 prompt.normalize smoke 提交状态不确定，必须先人工消歧",
        })
    policy = validate_prompt_normalization_model_profile(
        route.deployment.profile_version,
    )
    if policy is None:
        raise ValidationError("prompt.normalize smoke 缺少模型容量合同")
    try:
        payload, representative_contract = (
            representative_prompt_normalization_smoke_payload(
                route.deployment,
                policy=policy,
                text_binding=selected,
            )
        )
    except PromptNormalizationContractError as exc:
        capacity_exceeded = (
            exc.code == "prompt_normalization_batch_input_exceeded"
        )
        raise ValidationError({
            "code": (
                "prompt_normalization_smoke_capacity_exceeded"
                if capacity_exceeded
                else "prompt_normalization_smoke_contract_invalid"
            ),
            "detail": (
                "代表性 prompt.normalize smoke 输入超过冻结模型容量合同"
                if capacity_exceeded
                else "代表性 prompt.normalize smoke 未通过冻结请求合同"
            ),
        }) from exc
    snapshot = json_contract(
        {
            "schema_version": 1,
            "operation": PROMPT_NORMALIZE_OPERATION,
            "payload": payload,
            "representative_contract": representative_contract,
            "prompt_profile": smoke_profile_reference(deployment, PROMPT_NORMALIZE_OPERATION, selected),
        },
        max_bytes=_prompt_normalization_smoke_snapshot_max_bytes(),
    ).value
    return {
        "request_snapshot": snapshot,
        "request_sha256": json_contract(
            payload,
            max_bytes=_prompt_normalization_smoke_snapshot_max_bytes(),
        ).sha256,
        "target_sha256": deployment_target_sha256(route.deployment),
        "estimated_cost": route.estimate_amount,
        "cost_currency": route.estimate_currency[:12],
    }


def execute_prompt_normalization_smoke_request(
    deployment: ModelDeployment,
    request_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Execute exactly once after the queue row entered ``submitting``."""

    payload = request_snapshot.get("payload") if isinstance(request_snapshot, dict) else None
    if not isinstance(payload, dict):
        raise ValidationError("prompt.normalize smoke 请求快照无效")
    route = _prompt_normalization_smoke_execution_route(deployment)
    policy = validate_prompt_normalization_model_profile(
        route.deployment.profile_version,
    )
    representative = request_snapshot.get("representative_contract")
    target_contract = payload.get("target_prompt_contract")
    if (
        policy is None
        or not isinstance(representative, dict)
        or not isinstance(target_contract, dict)
    ):
        raise ValidationError("prompt.normalize smoke 代表性容量合同无效")
    provider_payload = prompt_normalization_chat_payload(payload, policy=policy)
    input_bytes = prompt_normalization_chat_messages_input_bytes(
        provider_payload["messages"],
    )
    if (
        representative.get("input_bytes") != input_bytes
        or representative.get("max_input_bytes") != policy["max_input_bytes"]
        or representative.get("item_count") != len(payload.get("items") or [])
        or representative.get("max_items_per_batch") != policy["max_items_per_batch"]
        or representative.get("target_contract_sha256")
        != json_contract(
            target_contract,
            max_bytes=_prompt_normalization_smoke_snapshot_max_bytes(),
        ).sha256
    ):
        raise ValidationError("prompt.normalize smoke 代表性容量合同 checksum 已漂移")
    result: dict[str, Any] | None = None
    evidence = {
        "schema_version": 1,
        "execution_submitted": True,
        "submission_uncertain": False,
        "request_sha256": json_contract(
            payload,
            max_bytes=_prompt_normalization_smoke_snapshot_max_bytes(),
        ).sha256,
        "submission_semantics": SUBMISSION_SEMANTICS,
        "representative_contract": representative,
    }
    try:
        result = _invoke_resolved_prompt_normalization(route=route, payload=payload)
        rows = result["items"]
        response_contract = json_contract({
            "items": [
                {
                    "request_id": row.get("request_id"),
                    "translation": row.get("translation"),
                }
                for row in rows
            ],
        }, max_bytes=_prompt_normalization_smoke_snapshot_max_bytes())
        request_id_sha256 = _provider_request_id_sha256(result)
        result_metadata = (
            result.get("metadata")
            if isinstance(result.get("metadata"), dict)
            else {}
        )
        result_warnings = (
            result_metadata.get("warnings")
            if isinstance(result_metadata.get("warnings"), list)
            else []
        )
        return {
            "status": "succeeded",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.PASSED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": "",
            "error_message": "",
            "evidence": {
                **evidence,
                "response_sha256": response_contract.sha256,
                "request_id_sha256": request_id_sha256,
                "review_status": (
                    "needs_attention" if result_warnings else "ready"
                ),
                **(
                    {"usage": result_metadata["usage"]}
                    if isinstance(result_metadata.get("usage"), dict)
                    else {}
                ),
                **(
                    {"warnings": result_warnings}
                    if result_warnings
                    else {}
                ),
            },
        }
    except ProviderDriverError as exc:
        uncertain = bool(exc.submission_uncertain)
        metadata = exc.metadata if isinstance(getattr(exc, "metadata", None), dict) else {}
        reason_code = safe_prompt_field_validation_reason_code(
            metadata.get("reason_code"),
        )
        validation_context = safe_prompt_field_validation_context(
            reason_code,
            metadata.get("validation_context"),
            request_payload=payload,
        )
        request_id_sha256 = _provider_request_id_sha256(result)
        return {
            "status": "uncertain" if uncertain else "failed",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.FAILED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": exc.code,
            "error_message": (
                "prompt.normalize smoke 提交状态不确定，必须人工消歧。"
                if uncertain else "prompt.normalize smoke 未通过共享合同。"
            ),
            "evidence": {
                **evidence,
                "submission_uncertain": uncertain,
                "request_id_sha256": request_id_sha256,
                "error_code": exc.code,
                "contract_error_code": str(metadata.get("contract_error_code") or ""),
                **({"reason_code": reason_code} if reason_code else {}),
                **(
                    {"validation_context": validation_context}
                    if validation_context else {}
                ),
                **(
                    {"finish_reason": str(metadata.get("finish_reason") or "")}
                    if metadata.get("finish_reason") else {}
                ),
                **(
                    {"usage": metadata.get("usage")}
                    if isinstance(metadata.get("usage"), dict) else {}
                ),
                **(
                    {"response_id_sha256": str(metadata.get("response_id_sha256") or "")}
                    if metadata.get("response_id_sha256") else {}
                ),
            },
        }
    except Exception:
        request_id_sha256 = _provider_request_id_sha256(result)
        return {
            "status": "uncertain",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.FAILED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": "provider_unclassified_failure",
            "error_message": "prompt.normalize smoke 提交状态不确定，必须人工消歧。",
            "evidence": {
                **evidence,
                "submission_uncertain": True,
                "request_id_sha256": request_id_sha256,
                "error_code": "provider_unclassified_failure",
            },
        }


@transaction.atomic
def resolve_prompt_normalization_smoke_attempt(
    deployment: ModelDeployment,
    *,
    smoke_attempt_id,
    outcome: str,
    note: str,
    user,
    provider_request_id: str = "",
    actual_cost: Decimal | None = None,
    cost_currency: str = "",
) -> ModelDeploymentSmokeAttemptResolution:
    """Compatibility wrapper for the operation-specific public service."""

    return resolve_deployment_smoke_attempt(
        deployment,
        smoke_attempt_id=smoke_attempt_id,
        outcome=outcome,
        note=note,
        user=user,
        provider_request_id=provider_request_id,
        actual_cost=actual_cost,
        cost_currency=cost_currency,
        expected_operation=PROMPT_NORMALIZE_OPERATION,
    )
