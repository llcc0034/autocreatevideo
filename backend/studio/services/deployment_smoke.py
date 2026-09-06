from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import ModelDeployment, ModelDeploymentCapabilitySnapshot, ProviderProfile
from studio.services.async_media_contract import (
    deployment_target_sha256,
    validate_async_model_profile,
)
from studio.services.json_contracts import (
    canonical_json_bytes,
    json_contract,
    verify_json_contract,
)
from studio.services.configuration import resolve_configuration
from studio.services.model_profile_contracts import CHAT_REQUEST_OPTIONS_KEY
from studio.services.provider_registry import (
    ProviderDriverError,
    credential_binding_sha256,
    deployment_supports_script_generation,
    driver_for,
)
from studio.services.deployment_smoke_contract import (
    DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
)
from studio.services.series_concept_drafts import (
    build_series_concept_plan,
    finalize_series_concept_draft,
)
from studio.services.script_generation_contract import (
    SCRIPT_GENERATE_OPERATION,
    validate_script_generation_model_profile,
)
from studio.services.script_structure import (
    SCRIPT_STRUCTURE_OPERATION,
    build_script_structure_plan,
    finalize_script_structure,
    structure_provider_payload,
    validate_script_structure_model_profile,
)
from studio.services.production_visual_rules import (
    VISUAL_RULES_OPERATION,
    VisualRulesError,
    build_visual_rules_smoke_plan,
    finalize_visual_rules,
    validate_visual_rules_model_profile,
    visual_rules_provider_payload,
)
from studio.services.text_generation_queue import TEXT_OPERATION, provider_payload
from studio.services.deployment_smoke_profiles import (
    select_smoke_profile, smoke_profile_reference, assemble_text_smoke_profile,
    profile_columns,
)
from studio.services.security_boundaries import boundary_int, boundary_number


assert SCRIPT_GENERATE_OPERATION == TEXT_OPERATION


_SAFE_CONTRACT_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _series_concept_resource_advisory() -> dict[str, Any]:
    value = resolve_configuration(required_keys=(
        "production.series_concept_resource_advisory",
    )).values["production.series_concept_resource_advisory"]
    if not isinstance(value, dict):
        raise ValidationError(
            "production.series_concept_resource_advisory 配置必须是对象"
        )
    return value


def _text_provider_request_max_bytes() -> int:
    return boundary_int("security.snapshot.text_provider_request_max_bytes")


def _provider_response_max_bytes() -> int:
    return boundary_int("security.provider.max_response_bytes")


def async_media_smoke_max_poll_seconds() -> int:
    return boundary_int("security.provider.smoke_max_poll_seconds")


def async_media_smoke_max_poll_interval_seconds() -> float:
    return boundary_number(
        "security.provider.smoke_max_poll_interval_seconds",
        minimum=0.001,
    )


def _smoke_artifact_max_bytes(output_kind: str) -> int:
    key = (
        "security.provider.smoke_image_max_bytes"
        if output_kind == "image"
        else "security.provider.smoke_video_max_bytes"
    )
    return boundary_int(key)


def smoke_artifact_max_bytes(output_kind: str) -> int:
    return _smoke_artifact_max_bytes(output_kind)


def _safe_contract_error_code(value: object, *, fallback: str) -> str:
    """Return only a bounded internal classifier, never provider response text."""

    candidate = str(value or "")
    if _SAFE_CONTRACT_ERROR_CODE_RE.fullmatch(candidate):
        return candidate
    return fallback


def _config(deployment: ModelDeployment) -> dict:
    return {
        **(deployment.provider.driver_config or {}),
        **(deployment.runtime_config or {}),
    }


@transaction.atomic
def record_deployment_capability_snapshot(
    deployment: ModelDeployment,
    *,
    probe_kind: str,
    status: str,
    operations: list[str],
    evidence: dict,
    expected_target_sha256: str | None = None,
) -> ModelDeploymentCapabilitySnapshot:
    locked = ModelDeployment.objects.select_for_update().get(pk=deployment.pk)
    current_target_sha256 = deployment_target_sha256(locked)
    if expected_target_sha256 and current_target_sha256 != expected_target_sha256:
        status = ModelDeploymentCapabilitySnapshot.Status.FAILED
        evidence = {
            **evidence,
            "schema_version": 1,
            "execution_submitted": bool(evidence.get("execution_submitted")),
            "error_code": "target_changed_during_probe",
            "probed_target_sha256": expected_target_sha256,
            "current_target_sha256": current_target_sha256,
        }
    revision = (
        locked.capability_snapshots.aggregate(value=Max("revision"))["value"] or 0
    ) + 1
    operations_contract = json_contract(
        operations,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    evidence_contract = json_contract(
        evidence,
        max_bytes=_stage_snapshot_max_bytes(),
    )
    raw_ttl = _config(locked).get("capability_ttl_seconds")
    ttl_ceiling = boundary_int("security.runtime.max_capability_ttl_seconds")
    if type(raw_ttl) is not int or not 1 <= raw_ttl <= ttl_ceiling:
        raise ValidationError({
            "runtime_config": (
                "capability_ttl_seconds 必须显式配置为 1 到 "
                f"{ttl_ceiling} 的整数"
            ),
        })
    ttl = raw_ttl
    captured = timezone.now()
    return ModelDeploymentCapabilitySnapshot.objects.create(
        **profile_columns(evidence.get("prompt_profile")),
        deployment=locked,
        revision=revision,
        probe_kind=probe_kind,
        status=status,
        target_sha256=current_target_sha256,
        provider_config_sha256=locked.provider.driver_config_sha256,
        deployment_config_sha256=locked.runtime_config_sha256,
        credential_binding_sha256=credential_binding_sha256(locked),
        operations=operations_contract.value,
        operations_schema_version=operations_contract.schema_version,
        operations_size_bytes=operations_contract.size_bytes,
        operations_sha256=operations_contract.sha256,
        evidence=evidence_contract.value,
        evidence_schema_version=evidence_contract.schema_version,
        evidence_size_bytes=evidence_contract.size_bytes,
        evidence_sha256=evidence_contract.sha256,
        captured_at=captured,
        expires_at=captured + timedelta(seconds=ttl),
    )


def probe_async_deployment(
    deployment: ModelDeployment,
) -> ModelDeploymentCapabilitySnapshot:
    deployment = ModelDeployment.objects.select_related("profile_version__provider").get(pk=deployment.pk)
    expected_target_sha256 = deployment_target_sha256(deployment)
    if deployment.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
        raise ValidationError("该 probe 仅支持 async_rest deployment")
    operations = validate_async_model_profile(deployment.profile_version)
    driver = driver_for(deployment.provider)
    try:
        health = driver.invoke(deployment, "health", {"schema_version": 1})
        evidence = {
            "schema_version": 1,
            "execution_submitted": False,
            "health_response_sha256": str(health.get("response_sha256") or ""),
        }
        status = ModelDeploymentCapabilitySnapshot.Status.PASSED
    except ProviderDriverError as exc:
        evidence = {
            "schema_version": 1,
            "execution_submitted": False,
            "error_code": exc.code,
        }
        status = ModelDeploymentCapabilitySnapshot.Status.FAILED
    return record_deployment_capability_snapshot(
        deployment,
        probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.HEALTH,
        status=status,
        operations=operations,
        evidence=evidence,
        expected_target_sha256=expected_target_sha256,
    )


def _script_generation_policy(deployment: ModelDeployment) -> dict[str, Any]:
    profile = deployment.profile_version
    if deployment.status != deployment.Status.ACTIVE:
        raise ValidationError("script.generate 烟测只接受 active deployment")
    if profile.status != profile.Status.PUBLISHED:
        raise ValidationError("script.generate 烟测只接受已发布 model profile")
    if profile.modality not in {profile.Modality.TEXT, profile.Modality.MULTIMODAL}:
        raise ValidationError("script.generate 只支持 text/multimodal model profile")
    policy = validate_script_generation_model_profile(profile)
    if policy is None:
        raise ValidationError({
            "specification": "model profile 未精确声明 script.generate 能力",
        })
    if not deployment.provider.is_active:
        raise ValidationError("script.generate provider 已停用")
    if not deployment_supports_script_generation(deployment):
        raise ValidationError("deployment 没有受控 chat_completion transport")
    return policy


def prepare_script_generation_smoke_request(
    deployment: ModelDeployment,
    *, prompt_profile_binding_id=None,
) -> dict:
    """Build and price the exact tiny request before the queue row is created."""

    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    policy = _script_generation_policy(deployment)
    selected = select_smoke_profile(deployment, SCRIPT_GENERATE_OPERATION, prompt_profile_binding_id)
    episode_duration_seconds = max(
        1,
        (int(policy["minimum_shot_duration_ms"]) + 999) // 1000,
    )
    plan = build_series_concept_plan(
        idea="旅人在灯塔中发现一封信。",
        style_hint="简洁悬疑",
        episode_count=1,
        shots_per_episode=1,
        episode_duration_seconds=episode_duration_seconds,
        minimum_shot_duration_ms=int(policy["minimum_shot_duration_ms"]),
        maximum_shot_duration_ms=int(policy["maximum_shot_duration_ms"]),
        model_identifier=deployment.profile_version.model_identifier,
        timeout_seconds=float(policy["timeout_seconds"]),
        chat_request_options=policy[CHAT_REQUEST_OPTIONS_KEY],
        # Exercise the same declared output budget used by production.  A
        # smaller smoke-only cap can exhaust reasoning-capable models before
        # they emit any JSON, producing a false negative for script.generate.
        configured_output_token_hint=int(policy["max_output_tokens"]),
        field_contract=policy["field_contract"],
        resource_advisory=_series_concept_resource_advisory(),
    )
    plan["prompt_profile_assembly"] = assemble_text_smoke_profile(selected, SCRIPT_GENERATE_OPERATION, plan["request"])
    payload = provider_payload(plan)
    estimate = driver_for(deployment.provider).estimate(
        deployment,
        SCRIPT_GENERATE_OPERATION,
        payload,
    )
    if not isinstance(estimate, dict) or estimate.get("amount") is None:
        raise ValidationError("真实烟测前必须配置可确定的低成本 pricing")
    snapshot = json_contract({
        "schema_version": 1,
        "operation": SCRIPT_GENERATE_OPERATION,
        "prompt_profile": smoke_profile_reference(deployment, SCRIPT_GENERATE_OPERATION, selected),
        "payload": payload,
        "plan": plan,
        "plan_sha256": json_contract(
            plan,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256,
    }, max_bytes=_stage_snapshot_max_bytes()).value
    return {
        "request_snapshot": snapshot,
        "request_sha256": json_contract(
            payload,
            max_bytes=_text_provider_request_max_bytes(),
        ).sha256,
        "target_sha256": deployment_target_sha256(deployment),
        "estimated_cost": estimate.get("amount"),
        "cost_currency": str(estimate.get("currency") or "")[:12],
    }


def execute_script_generation_smoke_request(
    deployment: ModelDeployment,
    request_snapshot: dict,
) -> dict:
    """Execute one already-persisted request; never writes attempt state itself."""

    payload = request_snapshot.get("payload") if isinstance(request_snapshot, dict) else None
    plan = request_snapshot.get("plan") if isinstance(request_snapshot, dict) else None
    if (
        not isinstance(payload, dict)
        or not isinstance(plan, dict)
        or request_snapshot.get("plan_sha256") != json_contract(
            plan,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
    ):
        raise ValidationError("script.generate smoke 请求快照无效")
    response_sha256 = ""
    request_id_sha256 = ""
    evidence = {
        "schema_version": 1,
        "execution_submitted": True,
        "submission_uncertain": False,
        "request_sha256": json_contract(
            payload,
            max_bytes=_text_provider_request_max_bytes(),
        ).sha256,
        "submission_semantics": DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
    }
    try:
        response = driver_for(deployment.provider).invoke(
            deployment,
            "chat_completion",
            payload,
        )
        if not isinstance(response, dict):
            error = ProviderDriverError(
                "chat_completion 响应必须是对象",
                code="provider_response_invalid",
                submission_uncertain=False,
            )
            error.metadata = {"contract_error_code": "response_root_not_object"}
            raise error
        json_contract(response, max_bytes=_provider_response_max_bytes())
        response_sha256 = hashlib.sha256(canonical_json_bytes(response)).hexdigest()
        request_id = str(response.get("request_id") or "")
        if request_id:
            request_id_sha256 = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        try:
            decoded = json.loads(str(response.get("text") or ""))
            result = finalize_series_concept_draft(
                plan=plan,
                runtime_payload={
                    "source_fingerprint": plan["source_fingerprint"],
                    "result": decoded,
                    "review_issues": [],
                },
            )
        except Exception as exc:
            error = ProviderDriverError(
                "script.generate 响应未通过严格合同校验",
                code="series_concept_invalid_response",
                submission_uncertain=False,
            )
            error.metadata = {
                "contract_error_code": _safe_contract_error_code(
                    getattr(exc, "code", None),
                    fallback=(
                        "response_not_strict_json"
                        if isinstance(exc, json.JSONDecodeError)
                        else "series_concept_contract_validation_failed"
                    ),
                ),
            }
            raise error from exc
        result_sha256 = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
        return {
            "status": "succeeded",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.PASSED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": "",
            "error_message": "",
            "evidence": {
                **evidence,
                "response_sha256": response_sha256,
                "result_sha256": result_sha256,
                "request_id_sha256": request_id_sha256,
            },
        }
    except ProviderDriverError as exc:
        uncertain = bool(exc.submission_uncertain)
        metadata = exc.metadata if isinstance(getattr(exc, "metadata", None), dict) else {}
        contract_error_code = _safe_contract_error_code(
            metadata.get("contract_error_code"),
            fallback="provider_contract_validation_failed",
        )
        return {
            "status": "uncertain" if uncertain else "failed",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.FAILED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": exc.code,
            "error_message": (
                "script.generate smoke 提交状态不确定，必须人工消歧。"
                if uncertain else "script.generate smoke 未通过严格合同。"
            ),
            "evidence": {
                **evidence,
                "submission_uncertain": uncertain,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": exc.code,
                "contract_error_code": contract_error_code,
            },
        }
    except Exception:
        return {
            "status": "uncertain",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.FAILED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": "provider_unclassified_failure",
            "error_message": "script.generate smoke 提交状态不确定，必须人工消歧。",
            "evidence": {
                **evidence,
                "submission_uncertain": True,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": "provider_unclassified_failure",
            },
        }


def _script_structure_policy(deployment: ModelDeployment) -> dict[str, Any]:
    profile = deployment.profile_version
    if deployment.status != deployment.Status.ACTIVE:
        raise ValidationError("script.structure 烟测只接受 active deployment")
    if profile.status != profile.Status.PUBLISHED:
        raise ValidationError("script.structure 烟测只接受已发布 model profile")
    if profile.modality not in {profile.Modality.TEXT, profile.Modality.MULTIMODAL}:
        raise ValidationError("script.structure 只支持 text/multimodal model profile")
    policy = validate_script_structure_model_profile(profile)
    if policy is None:
        raise ValidationError({
            "specification": "model profile 未精确声明 script.structure 能力",
        })
    if not deployment.provider.is_active:
        raise ValidationError("script.structure provider 已停用")
    if not deployment_supports_script_generation(deployment):
        raise ValidationError("script.structure deployment 没有受控 chat_completion transport")
    return policy


def prepare_script_structure_smoke_request(
    deployment: ModelDeployment,
    *, prompt_profile_binding_id=None,
) -> dict:
    """Build, freeze and price the dedicated complete-source structure probe."""

    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    policy = _script_structure_policy(deployment)
    selected = select_smoke_profile(deployment, SCRIPT_STRUCTURE_OPERATION, prompt_profile_binding_id)
    source_text = "第一集：灯塔来信\n旅人走进风雨中的灯塔。\n旅人发现一封密封的旧信。"
    source_bytes = source_text.encode("utf-8")
    smoke_revision = type("FrozenSmokeRevision", (), {
        "id": uuid.uuid4(),
        "source_id": uuid.uuid4(),
        "revision": 1,
        "content": source_text,
        "content_size_bytes": len(source_bytes),
        "content_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "status": "confirmed",
    })()
    plan = build_script_structure_plan(
        revision=smoke_revision,
        model_identifier=deployment.profile_version.model_identifier,
        policy=policy,
    )
    plan["prompt_profile_assembly"] = assemble_text_smoke_profile(selected, SCRIPT_STRUCTURE_OPERATION, plan["request"])
    payload = structure_provider_payload(plan)
    estimate = driver_for(deployment.provider).estimate(
        deployment,
        SCRIPT_STRUCTURE_OPERATION,
        payload,
    )
    if not isinstance(estimate, dict) or estimate.get("amount") is None:
        raise ValidationError("真实烟测前必须配置可确定的低成本 pricing")
    snapshot = json_contract({
        "schema_version": 1,
        "operation": SCRIPT_STRUCTURE_OPERATION,
        "prompt_profile": smoke_profile_reference(deployment, SCRIPT_STRUCTURE_OPERATION, selected),
        "payload": payload,
        "plan": plan,
        "plan_sha256": json_contract(
            plan,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256,
    }, max_bytes=_stage_snapshot_max_bytes()).value
    return {
        "request_snapshot": snapshot,
        "request_sha256": json_contract(
            payload,
            max_bytes=_text_provider_request_max_bytes(),
        ).sha256,
        "target_sha256": deployment_target_sha256(deployment),
        "estimated_cost": estimate.get("amount"),
        "cost_currency": str(estimate.get("currency") or "")[:12],
    }


def execute_script_structure_smoke_request(
    deployment: ModelDeployment,
    request_snapshot: dict,
) -> dict:
    """Execute one frozen structure request without owning attempt persistence."""

    payload = request_snapshot.get("payload") if isinstance(request_snapshot, dict) else None
    plan = request_snapshot.get("plan") if isinstance(request_snapshot, dict) else None
    if (
        not isinstance(payload, dict)
        or not isinstance(plan, dict)
        or request_snapshot.get("plan_sha256") != json_contract(
            plan,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
    ):
        raise ValidationError("script.structure smoke 请求快照无效")
    response_sha256 = ""
    request_id_sha256 = ""
    evidence = {
        "schema_version": 1,
        "execution_submitted": True,
        "submission_uncertain": False,
        "request_sha256": json_contract(
            payload,
            max_bytes=_text_provider_request_max_bytes(),
        ).sha256,
        "submission_semantics": DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
    }
    try:
        response = driver_for(deployment.provider).invoke(
            deployment,
            "chat_completion",
            payload,
        )
        if not isinstance(response, dict):
            raise ProviderDriverError(
                "chat_completion 响应必须是对象",
                code="provider_response_invalid",
                submission_uncertain=False,
            )
        json_contract(response, max_bytes=_provider_response_max_bytes())
        response_sha256 = hashlib.sha256(canonical_json_bytes(response)).hexdigest()
        request_id = str(response.get("request_id") or "")
        if request_id:
            request_id_sha256 = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        try:
            decoded = json.loads(str(response.get("text") or ""))
            result = finalize_script_structure(plan=plan, payload=decoded)
        except Exception as exc:
            error = ProviderDriverError(
                "script.structure 响应未通过严格合同校验",
                code="script_structure_invalid_response",
                submission_uncertain=False,
            )
            error.metadata = {
                "contract_error_code": _safe_contract_error_code(
                    getattr(exc, "code", ""),
                    fallback=(
                        "script_structure_invalid_json"
                        if isinstance(exc, json.JSONDecodeError)
                        else "script_structure_contract_validation_failed"
                    ),
                ),
            }
            raise error from exc
        result_sha256 = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
        return {
            "status": "succeeded",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.PASSED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": "",
            "error_message": "",
            "evidence": {
                **evidence,
                "response_sha256": response_sha256,
                "result_sha256": result_sha256,
                "request_id_sha256": request_id_sha256,
            },
        }
    except ProviderDriverError as exc:
        uncertain = bool(exc.submission_uncertain)
        metadata = exc.metadata if isinstance(getattr(exc, "metadata", None), dict) else {}
        contract_error_code = _safe_contract_error_code(
            metadata.get("contract_error_code"),
            fallback="provider_contract_validation_failed",
        )
        return {
            "status": "uncertain" if uncertain else "failed",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.FAILED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": exc.code,
            "error_message": (
                "script.structure smoke 提交状态不确定，必须人工消歧。"
                if uncertain else "script.structure smoke 未通过严格合同。"
            ),
            "evidence": {
                **evidence,
                "submission_uncertain": uncertain,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": exc.code,
                "contract_error_code": contract_error_code,
            },
        }
    except Exception:
        return {
            "status": "uncertain",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.FAILED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": "provider_unclassified_failure",
            "error_message": "script.structure smoke 提交状态不确定，必须人工消歧。",
            "evidence": {
                **evidence,
                "submission_uncertain": True,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": "provider_unclassified_failure",
            },
        }


def _visual_rules_policy(deployment: ModelDeployment) -> dict[str, Any]:
    profile = deployment.profile_version
    if deployment.status != deployment.Status.ACTIVE:
        raise ValidationError("visual_rules.generate 烟测只接受 active deployment")
    if profile.status != profile.Status.PUBLISHED:
        raise ValidationError("visual_rules.generate 烟测只接受已发布 model profile")
    if profile.modality not in {profile.Modality.TEXT, profile.Modality.MULTIMODAL}:
        raise ValidationError("visual_rules.generate 只支持 text/multimodal model profile")
    policy = validate_visual_rules_model_profile(profile)
    if policy is None:
        raise ValidationError({
            "specification": "model profile 未精确声明 visual_rules.generate 能力",
        })
    if not deployment.provider.is_active:
        raise ValidationError("visual_rules.generate provider 已停用")
    if not deployment_supports_script_generation(deployment):
        raise ValidationError(
            "visual_rules.generate deployment 没有受控 chat_completion transport",
        )
    return policy


def prepare_visual_rules_smoke_request(deployment: ModelDeployment, *, prompt_profile_binding_id=None) -> dict:
    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    policy = _visual_rules_policy(deployment)
    selected = select_smoke_profile(deployment, VISUAL_RULES_OPERATION, prompt_profile_binding_id)
    plan = build_visual_rules_smoke_plan(
        model_identifier=deployment.profile_version.model_identifier,
        policy=policy,
    )
    plan["prompt_profile_assembly"] = assemble_text_smoke_profile(selected, VISUAL_RULES_OPERATION, plan.get("request", {}))
    payload = visual_rules_provider_payload(plan)
    estimate = driver_for(deployment.provider).estimate(
        deployment,
        VISUAL_RULES_OPERATION,
        payload,
    )
    if not isinstance(estimate, dict) or estimate.get("amount") is None:
        raise ValidationError("真实烟测前必须配置可确定的低成本 pricing")
    snapshot = json_contract({
        "schema_version": 1,
        "operation": VISUAL_RULES_OPERATION,
        "prompt_profile": smoke_profile_reference(deployment, VISUAL_RULES_OPERATION, selected),
        "payload": payload,
        "plan": plan,
        "plan_sha256": json_contract(
            plan,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256,
    }, max_bytes=_stage_snapshot_max_bytes()).value
    return {
        "request_snapshot": snapshot,
        "request_sha256": json_contract(
            payload,
            max_bytes=_text_provider_request_max_bytes(),
        ).sha256,
        "target_sha256": deployment_target_sha256(deployment),
        "estimated_cost": estimate.get("amount"),
        "cost_currency": str(estimate.get("currency") or "")[:12],
    }


def execute_visual_rules_smoke_request(
    deployment: ModelDeployment,
    request_snapshot: dict,
) -> dict:
    payload = request_snapshot.get("payload") if isinstance(request_snapshot, dict) else None
    plan = request_snapshot.get("plan") if isinstance(request_snapshot, dict) else None
    if (
        not isinstance(payload, dict)
        or not isinstance(plan, dict)
        or request_snapshot.get("plan_sha256") != json_contract(
            plan,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
    ):
        raise ValidationError("visual_rules.generate smoke 请求快照无效")
    response_sha256 = ""
    request_id_sha256 = ""
    evidence = {
        "schema_version": 1,
        "execution_submitted": True,
        "submission_uncertain": False,
        "request_sha256": json_contract(
            payload,
            max_bytes=_text_provider_request_max_bytes(),
        ).sha256,
        "submission_semantics": DEPLOYMENT_SMOKE_SUBMISSION_SEMANTICS,
    }
    try:
        response = driver_for(deployment.provider).invoke(
            deployment,
            "chat_completion",
            payload,
        )
        if not isinstance(response, dict):
            error = ProviderDriverError(
                "chat_completion 响应必须是对象",
                code="provider_response_invalid",
                submission_uncertain=False,
            )
            error.metadata = {"contract_error_code": "response_root_not_object"}
            raise error
        try:
            json_contract(response, max_bytes=_provider_response_max_bytes())
        except ValidationError as exc:
            error = ProviderDriverError(
                "chat_completion 响应信封未通过受限 JSON 合同",
                code="provider_response_invalid",
                submission_uncertain=False,
            )
            error.metadata = {"contract_error_code": "response_envelope_invalid"}
            raise error from exc
        response_sha256 = hashlib.sha256(canonical_json_bytes(response)).hexdigest()
        request_id = str(response.get("request_id") or "")
        if request_id:
            request_id_sha256 = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        try:
            decoded = json.loads(str(response.get("text") or ""))
        except json.JSONDecodeError as exc:
            error = ProviderDriverError(
                "visual_rules.generate 响应不是严格 JSON 对象",
                code="visual_rules_response_invalid",
                submission_uncertain=False,
            )
            error.metadata = {"contract_error_code": "response_not_strict_json"}
            raise error from exc
        try:
            result = finalize_visual_rules(plan=plan, payload=decoded)
        except VisualRulesError as exc:
            error = ProviderDriverError(
                "visual_rules.generate 响应未通过严格合同校验",
                code="visual_rules_response_invalid",
                submission_uncertain=False,
            )
            error.metadata = {
                "contract_error_code": _safe_contract_error_code(
                    exc.code,
                    fallback="visual_rules_contract_validation_failed",
                ),
            }
            raise error from exc
        result_sha256 = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
        return {
            "status": "succeeded",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.PASSED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": "",
            "error_message": "",
            "evidence": {
                **evidence,
                "response_sha256": response_sha256,
                "result_sha256": result_sha256,
                "request_id_sha256": request_id_sha256,
            },
        }
    except ProviderDriverError as exc:
        uncertain = bool(exc.submission_uncertain)
        metadata = exc.metadata if isinstance(getattr(exc, "metadata", None), dict) else {}
        contract_error_code = _safe_contract_error_code(
            metadata.get("contract_error_code"),
            fallback="provider_contract_validation_failed",
        )
        return {
            "status": "uncertain" if uncertain else "failed",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.FAILED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": exc.code,
            "error_message": (
                "visual_rules.generate smoke 提交状态不确定，必须人工消歧。"
                if uncertain else "visual_rules.generate smoke 未通过严格合同。"
            ),
            "evidence": {
                **evidence,
                "submission_uncertain": uncertain,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": exc.code,
                "contract_error_code": contract_error_code,
            },
        }
    except Exception:
        return {
            "status": "uncertain",
            "snapshot_status": ModelDeploymentCapabilitySnapshot.Status.FAILED,
            "provider_request_id_sha256": request_id_sha256,
            "error_code": "provider_unclassified_failure",
            "error_message": "visual_rules.generate smoke 提交状态不确定，必须人工消歧。",
            "evidence": {
                **evidence,
                "submission_uncertain": True,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": "provider_unclassified_failure",
            },
        }
