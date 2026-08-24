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
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION
from studio.services.prompt_normalization_contract import (
    PromptNormalizationContractError,
    validate_prompt_normalization_items,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    deployment_supports_script_generation,
    driver_for,
    resolve_credential,
)


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
    specification = profile.specification if isinstance(profile.specification, dict) else {}
    values = specification.get("capabilities")
    if not isinstance(values, list):
        return set()
    return {str(item).strip() for item in values if str(item).strip()}


def validate_prompt_normalization_model_profile(profile: ModelProfileVersion) -> bool:
    """Validate an exact prompt.normalize declaration before publication."""

    specification = profile.specification if isinstance(profile.specification, dict) else {}
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
        return False
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
    return True


def _combined_config(deployment: ModelDeployment) -> dict[str, Any]:
    provider_contract = verify_json_contract(
        deployment.provider.driver_config,
        schema_version=deployment.provider.driver_config_schema_version,
        size_bytes=deployment.provider.driver_config_size_bytes,
        sha256=deployment.provider.driver_config_sha256,
    )
    deployment_contract = verify_json_contract(
        deployment.runtime_config,
        schema_version=deployment.runtime_config_schema_version,
        size_bytes=deployment.runtime_config_size_bytes,
        sha256=deployment.runtime_config_sha256,
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
    if PROMPT_NORMALIZE_OPERATION not in _declared_capabilities(profile):
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
    })
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
    payload_contract = json_contract(payload)
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
    try:
        result = driver_for(route.deployment.provider).invoke(
            route.deployment,
            PROMPT_NORMALIZE_OPERATION,
            payload,
        )
        if not isinstance(result, dict):
            raise PromptNormalizationContractError("prompt.normalize 响应必须是对象")
        rows = result.get("items")
        result["items"] = validate_prompt_normalization_items(payload, rows)
    except PromptNormalizationContractError as exc:
        error = ProviderDriverError(
            "prompt.normalize 响应未通过共享契约校验",
            code="prompt_normalize_contract_failed",
        )
        error.metadata = route_metadata
        raise error from exc
    except ProviderDriverError as exc:
        exc.metadata = {
            **(
                exc.metadata
                if isinstance(getattr(exc, "metadata", None), dict)
                else {}
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


def prepare_prompt_normalization_smoke_request(
    deployment: ModelDeployment,
) -> dict[str, Any]:
    """Freeze the exact normalization probe before any provider submission."""

    route = _prompt_normalization_smoke_execution_route(deployment)
    if unresolved_prompt_normalization_smoke_attempt(route.deployment) is not None:
        raise ValidationError({
            "code": "unresolved_smoke_submission",
            "detail": "已有 prompt.normalize smoke 提交状态不确定，必须先人工消歧",
        })
    payload = {
        "schema_version": 1,
        "items": [{
            "request_id": "prompt-normalize-smoke",
            "text": "缓慢推近 __AUTOPROMPT_LITERAL_SMOKE_0001__",
            "source_lang": "Chinese",
            "target_lang": "English",
            "field_key": "smoke.camera",
            "label": "smoke",
            "context": "{}",
            "protected_tokens": ["__AUTOPROMPT_LITERAL_SMOKE_0001__"],
        }],
    }
    snapshot = json_contract({
        "schema_version": 1,
        "operation": PROMPT_NORMALIZE_OPERATION,
        "payload": payload,
    }).value
    return {
        "request_snapshot": snapshot,
        "request_sha256": json_contract(payload).sha256,
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
    result: dict[str, Any] | None = None
    evidence = {
        "schema_version": 1,
        "execution_submitted": True,
        "submission_uncertain": False,
        "request_sha256": json_contract(payload).sha256,
        "submission_semantics": SUBMISSION_SEMANTICS,
    }
    try:
        result = _invoke_resolved_prompt_normalization(route=route, payload=payload)
        rows = result["items"]
        response_contract = json_contract({
            "request_id": rows[0].get("request_id"),
            "translation": rows[0].get("translation"),
        })
        request_id_sha256 = _provider_request_id_sha256(result)
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
            },
        }
    except ProviderDriverError as exc:
        uncertain = bool(exc.submission_uncertain)
        metadata = exc.metadata if isinstance(getattr(exc, "metadata", None), dict) else {}
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
def _create_prompt_normalization_smoke_attempt(
    *,
    route: PromptNormalizationRoute,
    request_sha256: str,
    target_sha256: str,
) -> ModelDeploymentSmokeAttempt:
    locked = ModelDeployment.objects.select_for_update().get(pk=route.deployment.pk)
    if unresolved_prompt_normalization_smoke_attempt(locked) is not None:
        raise ValidationError({
            "code": "unresolved_smoke_submission",
            "detail": "已有 prompt.normalize smoke 提交状态不确定，必须先人工消歧",
        })
    if deployment_target_sha256(locked) != target_sha256:
        raise ValidationError({
            "code": "deployment_target_changed",
            "detail": "deployment 在 smoke 提交前已变化，请重新发起",
        })
    return ModelDeploymentSmokeAttempt.objects.create(
        deployment=locked,
        operation=PROMPT_NORMALIZE_OPERATION,
        status=ModelDeploymentSmokeAttempt.Status.SUBMITTING,
        request_sha256=request_sha256,
        target_sha256=target_sha256,
        estimated_cost=route.estimate_amount,
        cost_currency=route.estimate_currency,
    )


@transaction.atomic
def _finalize_prompt_normalization_smoke_attempt(
    *,
    attempt: ModelDeploymentSmokeAttempt,
    status: str,
    snapshot_status: str,
    evidence: dict[str, Any],
    provider_request_id_sha256: str = "",
) -> ModelDeploymentCapabilitySnapshot:
    snapshot = record_deployment_capability_snapshot(
        attempt.deployment,
        probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        status=snapshot_status,
        operations=[PROMPT_NORMALIZE_OPERATION],
        evidence=evidence,
        expected_target_sha256=attempt.target_sha256,
    )
    locked = ModelDeploymentSmokeAttempt.objects.select_for_update().get(pk=attempt.pk)
    if (
        locked.status != ModelDeploymentSmokeAttempt.Status.SUBMITTING
        or locked.final_snapshot_id is not None
    ):
        raise ValidationError("prompt.normalize smoke attempt 已终结，不能重复写入")
    locked.status = status
    locked.provider_request_id_sha256 = provider_request_id_sha256
    locked.finished_at = timezone.now()
    locked.final_snapshot = snapshot
    locked.save(update_fields=(
        "status",
        "provider_request_id_sha256",
        "finished_at",
        "final_snapshot",
        "updated_at",
    ))
    return snapshot


def smoke_prompt_normalization_deployment(
    deployment: ModelDeployment,
) -> ModelDeploymentCapabilitySnapshot:
    request_payload = {
        "schema_version": 1,
        "items": [{
            "request_id": "prompt-normalize-smoke",
            "text": "缓慢推近 __AUTOPROMPT_LITERAL_SMOKE_0001__",
            "source_lang": "Chinese",
            "target_lang": "English",
            "field_key": "smoke.camera",
            "label": "smoke",
            "context": "{}",
            "protected_tokens": ["__AUTOPROMPT_LITERAL_SMOKE_0001__"],
        }],
    }
    request_contract = json_contract(request_payload)
    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    if unresolved_prompt_normalization_smoke_attempt(deployment) is not None:
        raise ValidationError({
            "code": "unresolved_smoke_submission",
            "detail": "已有 prompt.normalize smoke 提交状态不确定，必须先人工消歧",
        })
    expected_target_sha256 = deployment_target_sha256(deployment)
    try:
        route = resolve_prompt_normalization_route(
            deployment.id,
            require_smoke=False,
            execution_context=ROUTE_CONTEXT_DEPLOYMENT_SMOKE,
        )
        expected_target_sha256 = deployment_target_sha256(route.deployment)
        if route.is_external and route.estimate_amount is None:
            raise ProviderDriverError(
                "真实 smoke 前必须配置可确定 pricing",
                code="provider_cost_unknown",
            )
    except (ProviderDriverError, ValidationError, ValueError) as exc:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[PROMPT_NORMALIZE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": False,
                "submission_uncertain": False,
                "error_code": str(getattr(exc, "code", "prompt_normalize_smoke_failed")),
                "request_sha256": request_contract.sha256,
                "submission_semantics": SUBMISSION_SEMANTICS,
                "captured_at": timezone.now().isoformat(),
            },
            expected_target_sha256=expected_target_sha256,
        )

    attempt = _create_prompt_normalization_smoke_attempt(
        route=route,
        request_sha256=request_contract.sha256,
        target_sha256=expected_target_sha256,
    )
    cost_evidence = {
        "estimated_cost": (
            str(route.estimate_amount) if route.estimate_amount is not None else None
        ),
        "estimated_currency": route.estimate_currency,
    }
    result: dict[str, Any] | None = None
    try:
        result = _invoke_resolved_prompt_normalization(
            route=route,
            payload=request_payload,
        )
        rows = result["items"]
        response_contract = json_contract({
            "request_id": rows[0].get("request_id"),
            "translation": rows[0].get("translation"),
        })
        return _finalize_prompt_normalization_smoke_attempt(
            attempt=attempt,
            status=ModelDeploymentSmokeAttempt.Status.SUCCEEDED,
            snapshot_status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "submission_uncertain": False,
                "request_sha256": request_contract.sha256,
                "response_sha256": response_contract.sha256,
                "smoke_attempt_id": str(attempt.id),
                "submission_semantics": SUBMISSION_SEMANTICS,
                **cost_evidence,
            },
            provider_request_id_sha256=_provider_request_id_sha256(result),
        )
    except ProviderDriverError as exc:
        uncertain = bool(exc.submission_uncertain)
        error_metadata = (
            exc.metadata
            if isinstance(getattr(exc, "metadata", None), dict)
            else {}
        )
        return _finalize_prompt_normalization_smoke_attempt(
            attempt=attempt,
            status=(
                ModelDeploymentSmokeAttempt.Status.UNCERTAIN
                if uncertain else ModelDeploymentSmokeAttempt.Status.FAILED
            ),
            snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "submission_uncertain": uncertain,
                "error_code": exc.code,
                "contract_error_code": str(
                    error_metadata.get("contract_error_code") or ""
                ),
                "request_sha256": request_contract.sha256,
                "smoke_attempt_id": str(attempt.id),
                "submission_semantics": SUBMISSION_SEMANTICS,
                **cost_evidence,
                "captured_at": timezone.now().isoformat(),
            },
            provider_request_id_sha256=_provider_request_id_sha256(result),
        )
    except Exception:
        return _finalize_prompt_normalization_smoke_attempt(
            attempt=attempt,
            status=ModelDeploymentSmokeAttempt.Status.UNCERTAIN,
            snapshot_status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "submission_uncertain": True,
                "error_code": "provider_unclassified_failure",
                "request_sha256": request_contract.sha256,
                "smoke_attempt_id": str(attempt.id),
                "submission_semantics": SUBMISSION_SEMANTICS,
                **cost_evidence,
                "captured_at": timezone.now().isoformat(),
            },
            provider_request_id_sha256=_provider_request_id_sha256(result),
        )


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
