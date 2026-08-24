from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import ModelDeployment, ModelDeploymentCapabilitySnapshot, ProviderProfile
from studio.services.async_media_contract import (
    async_operation_contract,
    deployment_target_sha256,
    validate_async_model_profile,
    validate_standard_payload,
)
from studio.services.json_contracts import (
    canonical_json_bytes,
    json_contract,
    verify_json_contract,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    credential_binding_sha256,
    deployment_supports_script_generation,
    driver_for,
)
from studio.services.remote_artifacts import verify_remote_artifacts_for_smoke
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
from studio.services.text_generation_queue import TEXT_OPERATION, provider_payload


assert SCRIPT_GENERATE_OPERATION == TEXT_OPERATION


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
    operations_contract = json_contract(operations)
    evidence_contract = json_contract(evidence)
    ttl = int(_config(locked).get("capability_ttl_seconds", 3600))
    captured = timezone.now()
    return ModelDeploymentCapabilitySnapshot.objects.create(
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
    *,
    operation: str | None = None,
) -> ModelDeploymentCapabilitySnapshot:
    deployment = ModelDeployment.objects.select_related("profile_version__provider").get(pk=deployment.pk)
    expected_target_sha256 = deployment_target_sha256(deployment)
    if deployment.provider.driver_type != ProviderProfile.DriverType.ASYNC_REST:
        raise ValidationError("该 probe 仅支持 async_rest deployment")
    operations = validate_async_model_profile(deployment.profile_version)
    driver = driver_for(deployment.provider)
    if operation is None:
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
    if operation not in operations:
        raise ValidationError({"operation": "deployment 未声明该 async media 能力"})
    contract = async_operation_contract(deployment.profile_version, operation)
    payload = dict(contract["smoke_inputs"])
    payload["model"] = deployment.profile_version.model_identifier
    payload.update({
        "schema_version": 1,
        "idempotency_key": f"deployment-smoke:{deployment.id}:{uuid.uuid4()}",
    })
    validate_standard_payload(contract, payload)
    estimate = driver.estimate(deployment, operation, payload)
    if estimate.get("amount") is None:
        raise ValidationError("真实烟测前必须配置可确定的低成本 pricing")
    submitted = False
    request_id = ""
    try:
        response = driver.invoke(deployment, "submit", payload)
        submitted = True
        request_id = str(response.get("request_id") or "")
        config = _config(deployment)
        deadline = time.monotonic() + min(60, int(config.get("max_poll_seconds", 3600)))
        interval = min(2.0, max(0.25, float(config.get("poll_interval_seconds", 2))))
        while True:
            polled = driver.invoke(deployment, "poll", {"request_id": request_id})
            if polled.get("terminal_failure") or polled.get("terminal_cancelled"):
                raise ProviderDriverError("烟测远端任务未成功", code="smoke_remote_failed")
            if polled.get("terminal_success"):
                break
            if time.monotonic() >= deadline:
                try:
                    driver.invoke(deployment, "cancel", {"request_id": request_id})
                except ProviderDriverError:
                    pass
                raise ProviderDriverError("烟测在受控时限内未完成", code="smoke_timeout")
            time.sleep(interval)
        collected = driver.invoke(deployment, "collect", {"request_id": request_id})
        max_bytes = 64 * 1024 * 1024 if contract["output_kind"] == "image" else 512 * 1024 * 1024
        facts = verify_remote_artifacts_for_smoke(
            deployment_id=deployment.id,
            artifacts=collected.get("artifacts"),
            provider_config=config,
            expected_kind=contract["output_kind"],
            max_bytes=max_bytes,
        )
        evidence = {
            "schema_version": 1,
            "execution_submitted": True,
            "request_id_sha256": hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
            "request_sha256": json_contract(payload).sha256,
            "artifact_facts": facts,
            "estimated_cost": estimate.get("amount"),
            "estimated_currency": estimate.get("currency") or "",
            "actual_cost": collected.get("actual_cost"),
            "actual_currency": collected.get("currency") or "",
        }
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=[operation],
            evidence=evidence,
            expected_target_sha256=expected_target_sha256,
        )
    except ProviderDriverError as exc:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[operation],
            evidence={
                "schema_version": 1,
                "execution_submitted": submitted,
                "request_id_sha256": (
                    hashlib.sha256(request_id.encode("utf-8")).hexdigest() if request_id else ""
                ),
                "error_code": exc.code,
                "estimated_cost": estimate.get("amount"),
                "estimated_currency": estimate.get("currency") or "",
            },
            expected_target_sha256=expected_target_sha256,
        )


def _script_generation_policy(deployment: ModelDeployment) -> dict[str, int | float]:
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
) -> dict:
    """Build and price the exact tiny request before the queue row is created."""

    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    policy = _script_generation_policy(deployment)
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
        configured_output_token_hint=min(int(policy["max_output_tokens"]), 2048),
    )
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
        "payload": payload,
        "plan": plan,
        "plan_sha256": json_contract(plan).sha256,
    }).value
    return {
        "request_snapshot": snapshot,
        "request_sha256": json_contract(payload).sha256,
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
        or request_snapshot.get("plan_sha256") != json_contract(plan).sha256
    ):
        raise ValidationError("script.generate smoke 请求快照无效")
    response_sha256 = ""
    request_id_sha256 = ""
    evidence = {
        "schema_version": 1,
        "execution_submitted": True,
        "submission_uncertain": False,
        "request_sha256": json_contract(payload).sha256,
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
        json_contract(response, max_bytes=1024 * 1024)
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
            raise ProviderDriverError(
                "script.generate 响应未通过严格合同校验",
                code="series_concept_invalid_response",
                submission_uncertain=False,
            ) from exc
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


def smoke_script_generation_deployment(
    deployment: ModelDeployment,
) -> ModelDeploymentCapabilitySnapshot:
    """Execute one tiny, strictly-finalized script.generate request.

    Only hashes, cost metadata and failure classification are persisted.  The
    request prompt and provider response remain process-local.
    """

    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    expected_target_sha256 = deployment_target_sha256(deployment)
    policy = _script_generation_policy(deployment)
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
        configured_output_token_hint=min(int(policy["max_output_tokens"]), 2048),
    )
    payload = provider_payload(plan)
    request_sha256 = json_contract(payload).sha256
    driver = driver_for(deployment.provider)
    try:
        estimate = driver.estimate(deployment, SCRIPT_GENERATE_OPERATION, payload)
    except ProviderDriverError as exc:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[SCRIPT_GENERATE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": False,
                "request_sha256": request_sha256,
                "error_code": exc.code,
            },
            expected_target_sha256=expected_target_sha256,
        )
    if not isinstance(estimate, dict) or estimate.get("amount") is None:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[SCRIPT_GENERATE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": False,
                "request_sha256": request_sha256,
                "error_code": "provider_cost_unknown",
            },
            expected_target_sha256=expected_target_sha256,
        )

    cost_evidence = {
        "estimated_cost": str(estimate.get("amount")),
        "estimated_currency": str(estimate.get("currency") or ""),
    }
    response_sha256 = ""
    request_id_sha256 = ""
    try:
        response = driver.invoke(deployment, "chat_completion", payload)
        if not isinstance(response, dict):
            raise ProviderDriverError(
                "chat_completion 响应必须是对象",
                code="provider_response_invalid",
                submission_uncertain=False,
            )
        json_contract(response, max_bytes=1024 * 1024)
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
            raise ProviderDriverError(
                "script.generate 响应未通过严格合同校验",
                code="series_concept_invalid_response",
                submission_uncertain=False,
            ) from exc
        result_sha256 = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    except ProviderDriverError as exc:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[SCRIPT_GENERATE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "submission_uncertain": bool(exc.submission_uncertain),
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": exc.code,
                **cost_evidence,
            },
            expected_target_sha256=expected_target_sha256,
        )
    except Exception:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[SCRIPT_GENERATE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "submission_uncertain": True,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": "provider_unclassified_failure",
                **cost_evidence,
            },
            expected_target_sha256=expected_target_sha256,
        )
    return record_deployment_capability_snapshot(
        deployment,
        probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
        operations=[SCRIPT_GENERATE_OPERATION],
        evidence={
            "schema_version": 1,
            "execution_submitted": True,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "result_sha256": result_sha256,
            "request_id_sha256": request_id_sha256,
            **cost_evidence,
        },
        expected_target_sha256=expected_target_sha256,
    )


def _script_structure_policy(deployment: ModelDeployment) -> dict[str, int | float]:
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
) -> dict:
    """Build, freeze and price the dedicated complete-source structure probe."""

    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    policy = _script_structure_policy(deployment)
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
        "payload": payload,
        "plan": plan,
        "plan_sha256": json_contract(plan).sha256,
    }).value
    return {
        "request_snapshot": snapshot,
        "request_sha256": json_contract(payload).sha256,
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
        or request_snapshot.get("plan_sha256") != json_contract(plan).sha256
    ):
        raise ValidationError("script.structure smoke 请求快照无效")
    response_sha256 = ""
    request_id_sha256 = ""
    evidence = {
        "schema_version": 1,
        "execution_submitted": True,
        "submission_uncertain": False,
        "request_sha256": json_contract(payload).sha256,
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
        json_contract(response, max_bytes=1024 * 1024)
        response_sha256 = hashlib.sha256(canonical_json_bytes(response)).hexdigest()
        request_id = str(response.get("request_id") or "")
        if request_id:
            request_id_sha256 = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        try:
            decoded = json.loads(str(response.get("text") or ""))
            result = finalize_script_structure(plan=plan, payload=decoded)
        except Exception as exc:
            raise ProviderDriverError(
                "script.structure 响应未通过严格合同校验",
                code="script_structure_invalid_response",
                submission_uncertain=False,
            ) from exc
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


def smoke_script_structure_deployment(
    deployment: ModelDeployment,
) -> ModelDeploymentCapabilitySnapshot:
    """Submit and strictly finalize a dedicated ``script.structure`` probe.

    A successful ``script.generate`` probe is deliberately not reused: the
    structure operation sends a complete frozen source identity and requires
    exact line coverage, which is a materially different target contract.
    Only hashes and cost/failure classification are persisted.
    """

    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).get(pk=deployment.pk)
    expected_target_sha256 = deployment_target_sha256(deployment)
    policy = _script_structure_policy(deployment)
    source_text = "第一集：灯塔来信\n旅人走进风雨中的灯塔。\n旅人发现一封密封的旧信。"
    source_bytes = source_text.encode("utf-8")
    # build_script_structure_plan only needs these immutable revision facts;
    # the smoke probe must not create a project or any user-owned ScriptSource.
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
    payload = structure_provider_payload(plan)
    request_sha256 = json_contract(payload).sha256
    driver = driver_for(deployment.provider)
    try:
        estimate = driver.estimate(deployment, SCRIPT_STRUCTURE_OPERATION, payload)
    except ProviderDriverError as exc:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[SCRIPT_STRUCTURE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": False,
                "request_sha256": request_sha256,
                "error_code": exc.code,
            },
            expected_target_sha256=expected_target_sha256,
        )
    if not isinstance(estimate, dict) or estimate.get("amount") is None:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[SCRIPT_STRUCTURE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": False,
                "request_sha256": request_sha256,
                "error_code": "provider_cost_unknown",
            },
            expected_target_sha256=expected_target_sha256,
        )

    cost_evidence = {
        "estimated_cost": str(estimate.get("amount")),
        "estimated_currency": str(estimate.get("currency") or ""),
    }
    response_sha256 = ""
    request_id_sha256 = ""
    try:
        response = driver.invoke(deployment, "chat_completion", payload)
        if not isinstance(response, dict):
            raise ProviderDriverError(
                "chat_completion 响应必须是对象",
                code="provider_response_invalid",
                submission_uncertain=False,
            )
        json_contract(response, max_bytes=1024 * 1024)
        response_sha256 = hashlib.sha256(canonical_json_bytes(response)).hexdigest()
        request_id = str(response.get("request_id") or "")
        if request_id:
            request_id_sha256 = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        try:
            decoded = json.loads(str(response.get("text") or ""))
            result = finalize_script_structure(plan=plan, payload=decoded)
        except Exception as exc:
            raise ProviderDriverError(
                "script.structure 响应未通过严格合同校验",
                code="script_structure_invalid_response",
                submission_uncertain=False,
            ) from exc
        result_sha256 = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    except ProviderDriverError as exc:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[SCRIPT_STRUCTURE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "submission_uncertain": bool(exc.submission_uncertain),
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": exc.code,
                **cost_evidence,
            },
            expected_target_sha256=expected_target_sha256,
        )
    except Exception:
        return record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.FAILED,
            operations=[SCRIPT_STRUCTURE_OPERATION],
            evidence={
                "schema_version": 1,
                "execution_submitted": True,
                "submission_uncertain": True,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "request_id_sha256": request_id_sha256,
                "error_code": "provider_unclassified_failure",
                **cost_evidence,
            },
            expected_target_sha256=expected_target_sha256,
        )
    return record_deployment_capability_snapshot(
        deployment,
        probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
        operations=[SCRIPT_STRUCTURE_OPERATION],
        evidence={
            "schema_version": 1,
            "execution_submitted": True,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "result_sha256": result_sha256,
            "request_id_sha256": request_id_sha256,
            **cost_evidence,
        },
        expected_target_sha256=expected_target_sha256,
    )
