from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import re
import time
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    Asset,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    WorkflowModelRequirement,
    WorkflowSemanticBinding,
    WorkflowSmokeAttempt,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.comfyui_contract import ComfyUIContractError, validate_comfyui_api_contract
from studio.services.comfyui import ComfyUIClient
from studio.services.comfyui_artifacts import (
    artifact_inventory_spec,
    exact_object_info_model_option,
    object_info_inventory_names,
    safe_model_relative_filename,
)
from studio.services.configuration import (
    resolve_configuration,
    validate_version_configuration_defaults,
)
from studio.services.json_contracts import (
    canonical_json_bytes,
    find_forbidden_secret_fields,
    json_contract,
    verify_json_contract,
)
from studio.services.security_boundaries import boundary_int
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.media_capabilities import (
    conditioning_mode_contract,
    resolve_conditioning_contract,
)
from studio.services.execution_monitoring import (
    resolve_execution_monitoring_contract,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    _comfyui_failure_message,
    credential_binding_sha256,
    driver_for,
)
from studio.services.runtime_platform import verified_artifact_availability
from studio.services.provenance import classify_task_output, sha256_file
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.workflow import (
    WorkflowBuildError,
    validate_comfy_graph,
)
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    compile_frozen_workflow,
    compiler_contract_from_input,
    workflow_execution_contract,
)
from studio.services.workflow_semantics import (
    VALUE_TYPES,
    WorkflowSemanticError,
    canonical_binding_rows,
    canonical_requirement_rows,
    expected_value_type,
    validate_semantic_key,
    validate_resolution_strategy,
)
from studio.services.workflow_smoke_fixtures import (
    FIXTURE_SCHEMA_VERSION,
    workflow_execution_conditioning_contract,
)
from studio.services.workflow_projectors import (
    validate_frozen_workflow_projector_contract,
)


class WorkflowModelEvidenceUnavailable(ValidationError):
    """A Live Validate model-proof failure with a stable recovery reason."""

    def __init__(
        self,
        *,
        reason_code: str,
        requirement_alias: str,
        artifact_id: str,
    ):
        self.reason_code = str(reason_code or "artifact_observation_unverified")
        self.requirement_alias = str(requirement_alias or "模型文件")
        self.artifact_id = str(artifact_id or "")
        explanations = {
            "host_snapshot_stale": "验收主机的模型文件证明已过期",
            "host_snapshot_not_latest": "校验读取到的不是验收主机最新证明",
            "host_probe_failed": "验收主机最近一次探测失败",
            "artifact_host_mismatch": "模型文件证明与验收部署绑定的主机不一致",
            "artifact_profile_mismatch": "模型文件与验收部署绑定的模型版本不一致",
            "artifact_observation_unverified": "最新主机证明缺少与模型声明一致的文件大小或 SHA-256",
        }
        self.operator_message = (
            f"模型文件 {self.requirement_alias}："
            f"{explanations.get(self.reason_code, explanations['artifact_observation_unverified'])}。"
        )
        super().__init__(self.operator_message, code=self.reason_code)


def _validation_error_code(exc: ValidationError) -> str:
    for item in getattr(exc, "error_list", ()):
        code = str(getattr(item, "code", "") or "").strip()
        if code:
            return code
    for items in getattr(exc, "error_dict", {}).values():
        for item in items:
            code = str(getattr(item, "code", "") or "").strip()
            if code:
                return code
    return "artifact_observation_unverified"


def _workflow_graph_max_bytes() -> int:
    return boundary_int("security.workflow.max_graph_bytes")


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _workflow_smoke_fixture_max_bytes() -> int:
    return boundary_int("security.workflow_smoke.fixture_values_max_bytes")


def _evidence_digest(value: dict[str, Any]) -> str:
    return json_contract(value, max_bytes=_stage_snapshot_max_bytes()).sha256


def _execution_target_evidence(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
) -> dict[str, Any]:
    provider = deployment.profile_version.provider
    artifacts = []
    prefetched_requirements = getattr(
        version,
        "_prefetched_objects_cache",
        {},
    ).get("model_requirements")
    requirements = (
        sorted(
            prefetched_requirements,
            key=lambda row: (row.role, row.alias, str(row.id)),
        )
        if prefetched_requirements is not None
        else version.model_requirements.select_related("artifact").order_by(
            "role", "alias", "id",
        )
    )
    for requirement in requirements:
        artifact = requirement.artifact
        row = {
            "requirement_id": str(requirement.id),
            "required": requirement.is_required,
            "expected_filename": requirement.expected_filename,
            "expected_sha256": requirement.expected_sha256,
            "artifact_id": str(artifact.id) if artifact else None,
            "artifact_status": artifact.status if artifact else None,
            "artifact_filename": artifact.filename if artifact else None,
            "artifact_sha256": artifact.sha256 if artifact else None,
        }
        # Empty optional inventory selectors carry no execution semantics.
        # Omitting them keeps target evidence stable across the migration that
        # introduced these fields, while non-empty selectors remain frozen.
        if artifact and any((
            artifact.inventory_folder,
            artifact.inventory_node_type,
            artifact.inventory_input_name,
        )):
            row.update({
                "artifact_inventory_folder": artifact.inventory_folder,
                "artifact_inventory_node_type": artifact.inventory_node_type,
                "artifact_inventory_input_name": artifact.inventory_input_name,
            })
        artifacts.append(row)
    return {
        "schema_version": 1,
        "deployment_id": str(deployment.id),
        "deployment_status": deployment.status,
        "deployment_runtime_config_sha256": deployment.runtime_config_sha256,
        "credential_binding_sha256": credential_binding_sha256(deployment),
        "provider_id": str(provider.id),
        "provider_active": provider.is_active,
        "provider_driver_type": provider.driver_type,
        "provider_driver_config_sha256": provider.driver_config_sha256,
        "model_profile_version_id": str(deployment.profile_version_id),
        "model_profile_status": deployment.profile_version.status,
        "model_identifier": deployment.profile_version.model_identifier,
        "template_output_kind": version.template.output_kind,
        "required_artifacts": artifacts,
    }


def _execution_target_sha256(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
) -> str:
    return _evidence_digest(_execution_target_evidence(version, deployment))


def workflow_execution_target_sha256(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
) -> str:
    """Public immutable execution-target digest shared by queued consumers."""

    return _execution_target_sha256(version, deployment)


def verify_workflow_smoke_attempt_contract(
    attempt: WorkflowSmokeAttempt,
    *,
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
) -> None:
    """Fail before submit/publish when an attempt belongs to older evidence."""

    current_target_sha256 = _execution_target_sha256(version, deployment)
    if (
        attempt.workflow_version_id != version.id
        or attempt.deployment_id != deployment.id
        or version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED
        or version.validated_deployment_id != deployment.id
        or _evidence_digest(version.validation_evidence)
        != version.validation_evidence_sha256
        or version.validation_evidence.get("execution_target_sha256")
        != current_target_sha256
        or attempt.graph_sha256 != version.graph_sha256
        or attempt.execution_contract_sha256 != version.execution_contract_sha256
        or attempt.validation_evidence_sha256 != version.validation_evidence_sha256
        or attempt.target_sha256 != current_target_sha256
    ):
        raise ValidationError(
            "workflow smoke attempt 不属于当前 graph/validation/deployment 合同",
        )


def declared_workflow_operation(version: WorkflowTemplateVersion) -> str:
    """Return the explicit media operation frozen into the workflow contract.

    ``workflow_mode`` explains how inputs are interpreted while ``capability``
    is the operation admitted by routing/readiness.  They are intentionally not
    inferred from the template output kind: doing so would make a Ref2VA graph
    look like a text-to-video graph and would let an unrelated smoke proof pass.
    """

    contract = version.input_contract
    operation = contract.get("capability") if isinstance(contract, dict) else None
    if (
        not isinstance(operation, str)
        or not operation.strip()
        or operation != operation.strip()
    ):
        raise ValidationError(
            "workflow input_contract 缺少显式 capability operation",
            code="workflow_operation_missing",
        )
    output_kind = str(version.template.output_kind or "")
    if not operation.startswith(f"{output_kind}."):
        raise ValidationError(
            "workflow capability operation 与模板输出模态不一致",
            code="workflow_operation_mismatch",
        )
    return operation


def _verify_current_workflow_validation_evidence(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
    *,
    target_sha256: str,
) -> None:
    evidence = version.validation_evidence
    if not isinstance(evidence, dict) or not version.validation_evidence_sha256:
        raise ValidationError(
            "workflow live-validation 证据缺失",
            code="workflow_validation_evidence_invalid",
        )
    if _evidence_digest(evidence) != version.validation_evidence_sha256:
        raise ValidationError(
            "workflow live-validation 证据摘要无效",
            code="workflow_validation_evidence_invalid",
        )
    if (
        str(evidence.get("deployment_id") or "") != str(deployment.id)
        or evidence.get("graph_sha256") != version.graph_sha256
        or evidence.get("execution_contract_sha256")
        != version.execution_contract_sha256
    ):
        raise ValidationError(
            "workflow 定义已在 live-validation 后变化",
            code="workflow_definition_drifted",
        )
    if (
        evidence.get("execution_target_sha256") != target_sha256
        or not isinstance(evidence.get("execution_target"), dict)
        or _evidence_digest(evidence["execution_target"]) != target_sha256
    ):
        raise ValidationError(
            "workflow live-validation 未绑定当前 graph/target/config hash",
            code="workflow_target_drifted",
        )


def _workflow_smoke_attempt_for_evidence(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
    *,
    smoke_attempt: WorkflowSmokeAttempt | None = None,
    smoke_attempt_loaded: bool = False,
) -> WorkflowSmokeAttempt:
    attempt_id = (
        version.smoke_test_evidence.get("smoke_attempt_id")
        if isinstance(version.smoke_test_evidence, dict)
        else None
    )
    if not smoke_attempt_loaded:
        try:
            smoke_attempt = WorkflowSmokeAttempt.objects.get(
                pk=attempt_id,
                workflow_version=version,
                deployment=deployment,
                status=WorkflowSmokeAttempt.Status.SUCCEEDED,
            )
        except (WorkflowSmokeAttempt.DoesNotExist, TypeError, ValueError) as exc:
            raise ValidationError(
                "workflow smoke evidence 未精确绑定成功的持久 attempt",
                code="workflow_smoke_attempt_missing",
            ) from exc
    if (
        smoke_attempt is None
        or str(smoke_attempt.id) != str(attempt_id or "")
        or smoke_attempt.workflow_version_id != version.id
        or smoke_attempt.deployment_id != deployment.id
        or smoke_attempt.status != WorkflowSmokeAttempt.Status.SUCCEEDED
    ):
        raise ValidationError(
            "workflow smoke evidence 未精确绑定成功的持久 attempt",
            code="workflow_smoke_attempt_missing",
        )
    return smoke_attempt


def _verify_current_workflow_smoke_evidence(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
    *,
    target_sha256: str,
    smoke_attempt: WorkflowSmokeAttempt | None = None,
    smoke_attempt_loaded: bool = False,
) -> WorkflowSmokeAttempt:
    evidence = version.smoke_test_evidence
    if not isinstance(evidence, dict) or not version.smoke_test_evidence_sha256:
        raise ValidationError(
            "workflow smoke 证据缺失",
            code="workflow_smoke_evidence_invalid",
        )
    if _evidence_digest(evidence) != version.smoke_test_evidence_sha256:
        raise ValidationError(
            "workflow smoke 证据摘要无效",
            code="workflow_smoke_evidence_invalid",
        )
    if (
        evidence.get("execution_submitted") is not True
        or str(evidence.get("deployment_id") or "") != str(deployment.id)
        or evidence.get("graph_sha256") != version.graph_sha256
        or evidence.get("execution_contract_sha256")
        != version.execution_contract_sha256
    ):
        raise ValidationError(
            "workflow smoke 证据不属于当前定义",
            code="workflow_smoke_evidence_invalid",
        )
    if (
        evidence.get("execution_target_sha256") != target_sha256
        or not isinstance(evidence.get("execution_target"), dict)
        or _evidence_digest(evidence["execution_target"]) != target_sha256
    ):
        raise ValidationError(
            "workflow smoke 证据不属于当前执行目标",
            code="workflow_smoke_target_drifted",
        )
    if (
        evidence.get("output_kind") != version.template.output_kind
        or not isinstance(evidence.get("outputs"), list)
        or not evidence.get("outputs")
        or evidence.get("output_count") != len(evidence["outputs"])
    ):
        raise ValidationError(
            "workflow smoke 输出证据不完整",
            code="workflow_smoke_evidence_invalid",
        )

    attempt = _workflow_smoke_attempt_for_evidence(
        version,
        deployment,
        smoke_attempt=smoke_attempt,
        smoke_attempt_loaded=smoke_attempt_loaded,
    )
    try:
        verify_workflow_smoke_attempt_contract(
            attempt,
            version=version,
            deployment=deployment,
        )
        verify_json_contract(
            attempt.fixture_values,
            schema_version=attempt.fixture_values_schema_version,
            size_bytes=attempt.fixture_values_size_bytes,
            sha256=attempt.fixture_values_sha256,
            max_bytes=_workflow_smoke_fixture_max_bytes(),
        )
    except ValidationError as exc:
        raise ValidationError(
            "workflow smoke attempt 不属于当前完整性合同",
            code="workflow_smoke_attempt_invalid",
        ) from exc
    if attempt.fixture_values_schema_version != FIXTURE_SCHEMA_VERSION:
        raise ValidationError(
            "workflow smoke fixture schema 已过期",
            code="workflow_smoke_attempt_invalid",
        )

    fixture_values = attempt.fixture_values
    result = attempt.result if isinstance(attempt.result, dict) else {}
    reference_bindings = (
        fixture_values.get("reference_bindings")
        if isinstance(fixture_values, dict) else None
    )
    expected_reference_sha256 = (
        json_contract(
            reference_bindings,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        if isinstance(reference_bindings, list) else ""
    )
    expected = {
        "smoke_attempt_id": str(attempt.id),
        "fixture_sha256": attempt.fixture_sha256,
        "reference_bindings_sha256": expected_reference_sha256,
        "asset_bindings_sha256": str(
            evidence.get("asset_bindings_sha256") or "",
        ),
        "materialized_graph_sha256": str(
            fixture_values.get("materialized_graph_sha256") or "",
        ) if isinstance(fixture_values, dict) else "",
        "compiled_graph_sha256": str(
            evidence.get("compiled_graph_sha256") or "",
        ),
    }
    if any(
        not value
        or str(evidence.get(key) or "") != value
        or str(result.get(key) or "") != value
        for key, value in expected.items()
    ):
        raise ValidationError(
            "workflow smoke fixture/materialized/compiled 证据不完整",
            code="workflow_smoke_evidence_invalid",
        )
    return attempt


def inspect_workflow_acceptance(
    version: WorkflowTemplateVersion,
    *,
    smoke_attempt: WorkflowSmokeAttempt | None = None,
    smoke_attempt_loaded: bool = False,
    graph_limit: int | None = None,
) -> dict[str, Any]:
    """Project persisted acceptance facts against the current target, read-only.

    Historical states remain untouched.  ``fresh`` means the immutable evidence
    still matches the current workflow, deployment, provider and model facts.
    """

    validation = {
        "stored_state": version.validation_state,
        "state": version.validation_state,
        "fresh": False,
        "reason_code": "",
        "message": "",
        "evidence_sha256": version.validation_evidence_sha256,
        "stored_target_sha256": (
            str(version.validation_evidence.get("execution_target_sha256") or "")
            if isinstance(version.validation_evidence, dict) else ""
        ),
        "current_target_sha256": "",
    }
    smoke = {
        "stored_state": version.smoke_test_state,
        "state": version.smoke_test_state,
        "fresh": False,
        "reason_code": "",
        "message": "",
        "evidence_sha256": version.smoke_test_evidence_sha256,
    }

    if version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED:
        validation.update({
            "reason_code": (
                "live_validation_not_run"
                if version.validation_state == WorkflowTemplateVersion.ValidationState.NOT_RUN
                else "live_validation_failed"
            ),
            "message": (
                "尚未执行 Live Validate。"
                if version.validation_state == WorkflowTemplateVersion.ValidationState.NOT_RUN
                else "最近一次 Live Validate 未通过。"
            ),
        })
    elif not version.validated_deployment_id:
        validation.update({
            "state": "evidence_invalid",
            "reason_code": "validated_deployment_missing",
            "message": "历史通过状态缺少精确的模型实例关联。",
        })
    else:
        deployment = version.validated_deployment
        if (
            deployment.status != ModelDeployment.Status.ACTIVE
            or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
            or not deployment.profile_version.provider.is_active
        ):
            validation.update({
                "state": "target_unavailable",
                "reason_code": "workflow_target_unavailable",
                "message": "历史验收目标当前不可用。",
            })
        elif (
            version.provider_id != deployment.profile_version.provider_id
            or version.model_profile_version_id != deployment.profile_version_id
        ):
            validation.update({
                "state": "drifted",
                "reason_code": "workflow_target_drifted",
                "message": "工作流模型合同与历史验收实例已不一致。",
            })
        else:
            try:
                verify_workflow_version_integrity(
                    version,
                    graph_limit=graph_limit,
                )
                target_sha256 = _execution_target_sha256(version, deployment)
                validation["current_target_sha256"] = target_sha256
                _verify_current_workflow_validation_evidence(
                    version,
                    deployment,
                    target_sha256=target_sha256,
                )
            except (ValidationError, RuntimeError, TypeError, ValueError) as exc:
                code = (
                    _validation_error_code(exc)
                    if isinstance(exc, ValidationError)
                    else "workflow_acceptance_unavailable"
                )
                validation.update({
                    "state": (
                        "drifted"
                        if code in {
                            "workflow_definition_drifted",
                            "workflow_target_drifted",
                        }
                        else "evidence_invalid"
                    ),
                    "reason_code": code,
                    "message": (
                        "历史 Live Validate 已通过，但当前证据已漂移。"
                        if code in {
                            "workflow_definition_drifted",
                            "workflow_target_drifted",
                        }
                        else "Live Validate 证据不完整或无法核验。"
                    ),
                })
            else:
                validation.update({
                    "state": "current",
                    "fresh": True,
                    "reason_code": "current",
                    "message": "Live Validate 证据与当前目标一致。",
                })

    if version.smoke_test_state != WorkflowTemplateVersion.ValidationState.PASSED:
        smoke.update({
            "reason_code": (
                "workflow_smoke_not_run"
                if version.smoke_test_state == WorkflowTemplateVersion.ValidationState.NOT_RUN
                else "workflow_smoke_failed"
            ),
            "message": (
                "尚未执行 Workflow Smoke。"
                if version.smoke_test_state == WorkflowTemplateVersion.ValidationState.NOT_RUN
                else "最近一次 Workflow Smoke 未通过。"
            ),
        })
    elif not validation["fresh"]:
        smoke.update({
            "state": "drifted",
            "reason_code": "live_validation_stale",
            "message": "历史 Workflow Smoke 已通过，但 Live Validate 证据当前无效。",
        })
    else:
        deployment = version.validated_deployment
        try:
            _verify_current_workflow_smoke_evidence(
                version,
                deployment,
                target_sha256=validation["current_target_sha256"],
                smoke_attempt=smoke_attempt,
                smoke_attempt_loaded=smoke_attempt_loaded,
            )
        except (ValidationError, RuntimeError, TypeError, ValueError) as exc:
            code = (
                _validation_error_code(exc)
                if isinstance(exc, ValidationError)
                else "workflow_smoke_evidence_invalid"
            )
            smoke.update({
                "state": (
                    "drifted"
                    if code == "workflow_smoke_target_drifted"
                    else "evidence_invalid"
                ),
                "reason_code": code,
                "message": (
                    "历史 Workflow Smoke 已通过，但当前目标已漂移。"
                    if code == "workflow_smoke_target_drifted"
                    else "Workflow Smoke 证据不完整或无法核验。"
                ),
            })
        else:
            smoke.update({
                "state": "current",
                "fresh": True,
                "reason_code": "current",
                "message": "Workflow Smoke 证据与当前目标一致。",
            })

    return {"validation": validation, "smoke": smoke}


def verify_published_workflow_capability(
    version: WorkflowTemplateVersion,
    deployment: ModelDeployment,
    *,
    operation: str,
) -> tuple[WorkflowSmokeAttempt, str]:
    """Verify the current workflow, target and exact real-smoke fact.

    This is the read-side admission check for an already-published workflow;
    it never repairs, re-hashes or substitutes stale evidence.  A smoke state
    flag alone is not evidence: the successful durable attempt must bind the
    current graph, execution contract, validation digest and deployment target.
    The latter freezes provider/deployment configuration and model artifacts.
    """

    if (
        version.status != WorkflowTemplateVersion.Status.PUBLISHED
        or not version.is_current
        or not version.template.is_active
        or version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED
        or version.smoke_test_state != WorkflowTemplateVersion.ValidationState.PASSED
        or version.validated_deployment_id != deployment.id
    ):
        raise ValidationError(
            "workflow 不是当前已发布且已校验的能力事实",
            code="workflow_not_production_ready",
        )
    if (
        deployment.status != ModelDeployment.Status.ACTIVE
        or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
        or not deployment.profile_version.provider.is_active
        or version.provider_id != deployment.profile_version.provider_id
        or version.model_profile_version_id != deployment.profile_version_id
    ):
        raise ValidationError(
            "workflow 绑定的 deployment/provider/model 已不可用或已漂移",
            code="workflow_target_drifted",
        )
    try:
        verify_workflow_version_integrity(version)
    except ValidationError as exc:
        raise ValidationError(
            "workflow graph/input/execution contract 完整性校验失败",
            code="workflow_integrity_invalid",
        ) from exc
    declared_operation = declared_workflow_operation(version)
    if operation != declared_operation:
        raise ValidationError(
            "workflow capability operation 与当前能力请求不一致",
            code="workflow_operation_mismatch",
        )

    target_sha256 = _execution_target_sha256(version, deployment)
    try:
        _verify_current_workflow_validation_evidence(
            version,
            deployment,
            target_sha256=target_sha256,
        )
    except ValidationError as exc:
        raise ValidationError(
            "workflow live-validation 未绑定当前 graph/target/config hash",
            code="workflow_target_drifted",
        ) from exc
    try:
        attempt = _verify_current_workflow_smoke_evidence(
            version,
            deployment,
            target_sha256=target_sha256,
        )
    except ValidationError as exc:
        raise ValidationError(
            "workflow 缺少当前 graph/target/config 对应的真实烟测证据",
            code="workflow_smoke_missing_or_stale",
        ) from exc
    return attempt, target_sha256


def _validate_model_inventory(version, deployment, driver, object_info) -> dict[str, Any]:
    if version.model_profile_version_id is None:
        raise ValidationError("workflow version 未绑定 model profile version")
    if deployment.profile_version_id != version.model_profile_version_id:
        raise ValidationError("deployment model profile 与 workflow 精确绑定不一致")
    checked = []
    folder_cache: dict[str, set[str]] = {}
    host_snapshot = None
    model_bindings = {
        row.semantic_key: row
        for row in version.semantic_bindings.all()
        if row.value_type == "model_artifact"
    }
    for requirement in version.model_requirements.select_related("artifact").all():
        artifact = requirement.artifact
        if artifact is None:
            if requirement.is_required:
                raise ValidationError({"model_requirements": f"{requirement.alias} needs_review: 未绑定 artifact"})
            continue
        if artifact.profile_version_id != version.model_profile_version_id:
            raise ValidationError({"model_requirements": f"{requirement.alias} artifact 不属于绑定模型版本"})
        artifact_filename = safe_model_relative_filename(artifact.filename)
        expected_filename = safe_model_relative_filename(
            requirement.expected_filename,
        )
        if (
            artifact_filename is None
            or expected_filename is None
            or artifact_filename != expected_filename
        ):
            raise ValidationError({"model_requirements": f"{requirement.alias} 文件名与 artifact 不一致"})
        if requirement.is_required:
            expected_sha = str(requirement.expected_sha256 or "").strip()
            artifact_sha = str(artifact.sha256 or "").strip()
            if artifact.status != ModelArtifact.Status.AVAILABLE:
                raise ValidationError({"model_requirements": f"{requirement.alias} needs_review: artifact 未验证可用"})
            if not expected_sha or not artifact_sha or expected_sha != artifact_sha:
                raise ValidationError({"model_requirements": f"{requirement.alias} needs_review: 缺少一致的受信 hash"})
            if deployment.runtime_host_id is None:
                raise ValidationError({
                    "model_requirements": f"{requirement.alias} needs_review: deployment 未绑定受管运行主机",
                })
            if host_snapshot is None:
                host_snapshot = HostCapabilitySnapshot.objects.filter(
                    host_id=deployment.runtime_host_id,
                ).order_by("-revision").first()
            if host_snapshot is None:
                raise ValidationError({
                    "model_requirements": f"{requirement.alias} needs_review: 缺少主机 artifact 观测快照",
                })
            try:
                verified_artifact_availability(
                    snapshot=host_snapshot,
                    deployment=deployment,
                    artifact=artifact,
                )
            except ValidationError as exc:
                raise WorkflowModelEvidenceUnavailable(
                    reason_code=_validation_error_code(exc),
                    requirement_alias=requirement.alias,
                    artifact_id=str(artifact.id),
                ) from exc
        try:
            spec = artifact_inventory_spec(artifact)
        except ValueError as exc:
            if requirement.is_required:
                raise ValidationError({
                    "model_requirements": f"{requirement.alias} 缺少合法受控库存探测器",
                }) from exc
            continue
        if spec.source == "object_info":
            try:
                available_names = object_info_inventory_names(object_info, spec)
            except ValueError as exc:
                raise ValidationError({
                    "model_requirements": (
                        f"目标运行端没有返回 {spec.node_type}.{spec.input_name} 自定义库存"
                    ),
                }) from exc
            inventory_source = f"object_info:{spec.node_type}.{spec.input_name}"
        else:
            if spec.folder not in folder_cache:
                response = driver.invoke(deployment, "models", {"folder": spec.folder})
                items = response.get("items") if isinstance(response, dict) else None
                if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                    raise ValidationError({
                        "model_requirements": f"目标运行端没有返回 {spec.folder} 库存",
                    })
                folder_cache[spec.folder] = set(items)
            available_names = folder_cache[spec.folder]
            inventory_source = "models_api"
        expected_inventory_name = safe_model_relative_filename(
            requirement.expected_filename,
        )
        normalized_available_names = {
            normalized
            for name in available_names
            for normalized in [safe_model_relative_filename(name)]
            if normalized is not None
        }
        if (
            expected_inventory_name is None
            or expected_inventory_name not in normalized_available_names
        ):
            raise ValidationError({"model_requirements": f"目标运行端缺少 {requirement.expected_filename}"})
        semantic_key = f"model.{requirement.alias}.filename"
        binding = model_bindings.get(semantic_key)
        if binding is None:
            if requirement.is_required:
                raise ValidationError({
                    "model_requirements": f"{requirement.alias} 缺少模型 semantic binding",
                })
            continue
        node = version.graph.get(str(binding.node_id))
        node_type = str(node.get("class_type") or "") if isinstance(node, dict) else ""
        if not node_type:
            raise ValidationError({
                "model_requirements": f"{requirement.alias} 模型 semantic 目标节点无效",
            })
        try:
            submission_filename = exact_object_info_model_option(
                object_info,
                node_type=node_type,
                input_name=binding.input_name,
                expected_filename=requirement.expected_filename,
            )
        except ValueError as exc:
            raise ValidationError({
                "model_requirements": (
                    f"{requirement.alias} 无法解析目标 Deployment 的精确提交枚举值：{exc}"
                ),
            }) from exc
        graph_inputs = node.get("inputs") if isinstance(node, dict) else None
        graph_filename = (
            graph_inputs.get(binding.input_name)
            if isinstance(graph_inputs, dict)
            else None
        )
        # The immutable graph and managed artifact use a platform-neutral model
        # identity.  The target ComfyUI enum may spell that same safe relative
        # path with Windows backslashes.  Validate the canonical identity here;
        # the exact, uniquely resolved enum spelling is frozen below as
        # submission_filename and applied to the runtime graph before submit.
        if (
            not isinstance(graph_filename, str)
            or safe_model_relative_filename(graph_filename)
            != expected_inventory_name
        ):
            raise ValidationError({
                "model_requirements": (
                    f"{requirement.alias} 的冻结 graph model widget 与受管模型身份不一致；"
                    "请修正来源工作流后重新导入"
                ),
            })
        checked.append({
            "requirement_id": str(requirement.id),
            "artifact_id": str(artifact.id),
            "filename": requirement.expected_filename,
            "submission_filename": submission_filename,
            "sha256": artifact.sha256,
            "folder": spec.folder,
            "inventory_source": inventory_source,
            "semantic_key": semantic_key,
            "node_id": str(binding.node_id),
            "node_type": node_type,
            "input_name": binding.input_name,
        })
    return {
        "schema_version": 2,
        "checked_requirements": sorted(
            checked,
            key=lambda row: (row["semantic_key"], row["requirement_id"]),
        ),
    }


def _binding_rows(version: WorkflowTemplateVersion) -> list[dict[str, Any]]:
    return canonical_binding_rows([{
        "semantic_key": row.semantic_key,
        "node_id": row.node_id,
        "input_name": row.input_name,
        "value_type": row.value_type,
        "is_required": row.is_required,
    } for row in version.semantic_bindings.all()])


def _requirement_rows(version: WorkflowTemplateVersion) -> list[dict[str, Any]]:
    return canonical_requirement_rows([{
        "role": row.role,
        "alias": row.alias,
        "artifact_id": str(row.artifact_id) if row.artifact_id else None,
        "expected_filename": row.expected_filename,
        "expected_sha256": row.expected_sha256,
        "is_required": row.is_required,
    } for row in version.model_requirements.all()])


def workflow_version_execution_snapshot(version: WorkflowTemplateVersion) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "workflow_version_id": str(version.id),
        "workflow_version": version.version,
        "template": {
            "id": str(version.template_id),
            "slug": version.template.slug,
            "name": version.template.name,
            "output_kind": version.template.output_kind,
        },
        "model_identifier": (
            version.model_profile_version.model_identifier
            if version.model_profile_version_id else ""
        ),
        "graph": version.graph,
        "graph_schema_version": version.graph_schema_version,
        "graph_size_bytes": version.graph_size_bytes,
        "graph_sha256": version.graph_sha256,
        "input_contract": version.input_contract,
        "input_contract_schema_version": version.input_contract_schema_version,
        "input_contract_size_bytes": version.input_contract_size_bytes,
        "input_contract_sha256": version.input_contract_sha256,
        "compiler_id": version.compiler_id,
        "compiler_version": version.compiler_version,
        "semantic_bindings": _binding_rows(version),
        "semantic_bindings_sha256": version.semantic_bindings_sha256,
        "model_requirements": _requirement_rows(version),
        "model_requirements_sha256": version.model_requirements_sha256,
        "execution_contract_sha256": version.execution_contract_sha256,
    }


def _smoke_conditioning_mode(
    version: WorkflowTemplateVersion,
    values: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    if version.template.output_kind != "video":
        return "", None
    try:
        _conditioning, mode_contract = workflow_execution_conditioning_contract(
            version,
            values,
        )
    except ValidationError as exc:
        detail = "; ".join(exc.messages) if getattr(exc, "messages", None) else str(exc)
        raise ValidationError({"fixture": detail}) from exc
    mode = str((mode_contract or {}).get("key") or "")
    if not mode:
        raise ValidationError({"fixture": "conditioning mode 合同缺失"})
    return mode, mode_contract


def _smoke_audio_enabled(
    version: WorkflowTemplateVersion,
    values: dict[str, Any],
) -> bool:
    """Resolve smoke audio behavior from the frozen version contract only."""

    contract = version.input_contract if isinstance(version.input_contract, dict) else {}
    parameters = contract.get("parameters") if isinstance(contract, dict) else None
    static = contract.get("static_semantics", {})
    if "audio.enabled" in static:
        value = static["audio.enabled"]
        if type(value) is not bool or "audio.enabled" in values and values["audio.enabled"] != value:
            raise ValidationError({"fixture": "audio.enabled 不能覆盖冻结静态语义"})
        return value
    if not isinstance(parameters, dict) or "audio.enabled" not in parameters:
        # A version that does not bind audio.enabled cannot request audio
        # topology. The absence is recorded in its frozen contract.
        return False
    value = values.get("audio.enabled")
    if not isinstance(value, bool):
        raise ValidationError({
            "fixture": "audio.enabled 必须由冻结 workflow 参数合同明确提供布尔值",
        })
    return value


def _materialize_smoke_graph(
    version: WorkflowTemplateVersion,
    *,
    fixture_values: dict[str, Any],
    fixture_reference_bindings: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    fixture_asset_facts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    output_prefix: str | None = None,
    owner_id=None,
    require_smoke_video_timing: bool = True,
):
    contract = version.input_contract if isinstance(version.input_contract, dict) else {}
    values = fixture_values if isinstance(fixture_values, dict) else None
    if values is None:
        raise ValidationError("workflow smoke fixture values 无效")
    asset_facts = list(fixture_asset_facts or [])
    reference_bindings = list(fixture_reference_bindings or [])
    smoke_conditioning_mode, smoke_mode_contract = _smoke_conditioning_mode(
        version,
        values,
    )
    labels = [
        str(label)
        for item in reference_bindings
        for label in (item.get("input_label"), item.get("audio_input_label"))
        if label
    ]
    base_prompt = str(values.get("prompt.positive") or values.get("PROMPT") or "smoke test")
    smoke_prompt = (
        base_prompt + (" Reference inputs: " + " ".join(labels) if labels else "")
    )
    output_kind = str(version.template.output_kind or "")
    raw_fps = values.get("video.fps", values.get("FPS"))
    if require_smoke_video_timing and output_kind == WorkflowTemplate.OutputKind.VIDEO and (
        isinstance(raw_fps, bool)
        or not isinstance(raw_fps, (int, float))
        or float(raw_fps) <= 0
    ):
        raise ValidationError({"fixture": "video smoke 必须由参数合同明确提供正数 video.fps"})
    raw_duration_ms = values.get("video.duration_ms", values.get("DURATION_MS"))
    if raw_duration_ms is None and output_kind == WorkflowTemplate.OutputKind.VIDEO:
        frames = values.get("video.frames", values.get("FRAMES"))
        if (
            isinstance(frames, int)
            and not isinstance(frames, bool)
            and frames > 0
            and isinstance(raw_fps, (int, float))
            and not isinstance(raw_fps, bool)
            and float(raw_fps) > 0
        ):
            raw_duration_ms = round(frames / float(raw_fps) * 1000)
    if require_smoke_video_timing and output_kind == WorkflowTemplate.OutputKind.VIDEO and (
        isinstance(raw_duration_ms, bool)
        or not isinstance(raw_duration_ms, (int, float))
        or int(raw_duration_ms) <= 0
    ):
        raise ValidationError({
            "fixture": (
                "video smoke 必须明确提供 video.duration_ms，或同时提供 video.frames/video.fps"
            ),
        })
    is_dynamic_references = (version.compiler_id, version.compiler_version) == ("generic.comfy", 3)
    smoke_audio_enabled = _smoke_audio_enabled(
        version,
        values,
    )
    prompt = SimpleNamespace(
        id=version.id,
        prompt_text=smoke_prompt,
        negative_prompt=str(values.get("prompt.negative") or values.get("NEGATIVE_PROMPT") or ""),
        total_duration_ms=int(raw_duration_ms or 0),
        include_audio=smoke_audio_enabled,
        prompt_type=output_kind,
        conditioning_mode=smoke_conditioning_mode,
        model=(
            version.model_profile_version.model_identifier
            if version.model_profile_version_id else ""
        ),
        structured_payload={
            "reference_bindings": reference_bindings or asset_facts,
            "prompt_template_key": (contract.get("prompt_template_keys") or [""])[0] if is_dynamic_references else "",
            "effective_conditioning_mode": smoke_conditioning_mode,
        },
        project=SimpleNamespace(owner_id=owner_id),
    )
    width = values.get("image.width", values.get("WIDTH"))
    height = values.get("image.height", values.get("HEIGHT"))
    seed = values.get("sampling.seed", values.get("SEED"))
    aspect_ratio = values.get("image.aspect_ratio", values.get("ASPECT_RATIO"))
    project = SimpleNamespace(
        id=version.id,
        owner_id=owner_id,
        width=int(width) if width is not None else None,
        height=int(height) if height is not None else None,
        fps=int(raw_fps) if raw_fps is not None else None,
        continuity_seed=int(seed) if seed is not None else None,
        aspect_ratio=str(aspect_ratio) if aspect_ratio is not None else None,
    )
    try:
        parameters = contract.get("parameters") if isinstance(contract, dict) else {}
        generic_overrides = {
            key: value
            for key, value in values.items()
            if (
                isinstance(parameters, dict)
                and key in parameters
                and not key.startswith("asset.")
                and not key.startswith("model.")
            )
        }
        if is_dynamic_references and isinstance(parameters, dict) and "prompt.positive" in parameters:
            generic_overrides["prompt.positive"] = smoke_prompt
        if isinstance(parameters, dict) and "output.prefix" in parameters:
            generic_overrides["output.prefix"] = (
                str(output_prefix or "").strip()
                or f"autoprompt/smoke/{version.id}"
            )
        compiled = compile_frozen_workflow(
            workflow_snapshot=workflow_version_execution_snapshot(version),
            prompt=prompt, project=project, overrides=generic_overrides,
        )
    except (WorkflowCompilerError, WorkflowSemanticError) as exc:
        raise ValidationError({"fixture": str(exc)}) from exc
    # ModelRequirement filenames remain immutable, platform-neutral evidence.
    # Actual ComfyUI widget values must use the exact spelling frozen from the
    # validated Deployment's object_info enum (Windows backslashes included).
    # A draft may still be structurally compiled for preview, but every
    # validated execution fails closed without schema-v2 inventory evidence.
    runtime_graph = deepcopy(compiled.graph)
    runtime_values = deepcopy(compiled.resolved_semantics)
    bindings_by_key = {
        row.semantic_key: row
        for row in version.semantic_bindings.all()
    }
    model_requirements = list(
        version.model_requirements.select_related("artifact").all()
    )
    validated_execution = (
        version.validation_state
        == WorkflowTemplateVersion.ValidationState.PASSED
    )
    evidence_by_requirement: dict[str, dict[str, Any]] = {}
    if validated_execution and any(
        f"model.{requirement.alias}.filename" in bindings_by_key
        for requirement in model_requirements
    ):
        evidence = version.validation_evidence
        inventory = evidence.get("inventory") if isinstance(evidence, dict) else None
        rows = inventory.get("checked_requirements") if isinstance(inventory, dict) else None
        if (
            not isinstance(evidence, dict)
            or version.validation_evidence_sha256 != _evidence_digest(evidence)
            or not isinstance(inventory, dict)
            or inventory.get("schema_version") != 2
            or not isinstance(rows, list)
        ):
            remediation = (
                "已发布版本需新建版本后执行 live validate"
                if version.status in {
                    WorkflowTemplateVersion.Status.PUBLISHED,
                    WorkflowTemplateVersion.Status.RETIRED,
                }
                else "请重新执行 live validate"
            )
            raise ValidationError({
                "fixture": (
                    "validated workflow 缺少目标 Deployment 的精确模型提交枚举证据；"
                    + remediation
                ),
            })
        for row in rows:
            requirement_id = str(row.get("requirement_id") or "") if isinstance(row, dict) else ""
            if not requirement_id or requirement_id in evidence_by_requirement:
                raise ValidationError({
                    "fixture": "validated workflow 模型提交枚举证据重复或无效",
                })
            evidence_by_requirement[requirement_id] = row
    for requirement in model_requirements:
        artifact = requirement.artifact
        semantic_key = f"model.{requirement.alias}.filename"
        binding = bindings_by_key.get(semantic_key)
        if binding is None:
            continue
        if not validated_execution:
            continue
        if artifact is None:
            raise ValidationError({
                "fixture": f"{semantic_key} 缺少已证明的 ModelArtifact",
            })
        expected_filename = safe_model_relative_filename(requirement.expected_filename)
        artifact_filename = safe_model_relative_filename(artifact.filename)
        if (
            expected_filename is None
            or artifact_filename is None
            or expected_filename != artifact_filename
        ):
            raise ValidationError({
                "fixture": f"{semantic_key} 的 artifact/requirement 文件名已漂移",
            })
        evidence_row = evidence_by_requirement.get(str(requirement.id))
        submission_filename = (
            str(evidence_row.get("submission_filename") or "")
            if isinstance(evidence_row, dict) else ""
        )
        target_node = runtime_graph.get(str(binding.node_id))
        target_node_type = (
            str(target_node.get("class_type") or "")
            if isinstance(target_node, dict) else ""
        )
        if (
            not isinstance(evidence_row, dict)
            or str(evidence_row.get("artifact_id") or "") != str(artifact.id)
            or evidence_row.get("filename") != requirement.expected_filename
            or str(evidence_row.get("sha256") or "") != str(artifact.sha256 or "")
            or evidence_row.get("semantic_key") != semantic_key
            or str(evidence_row.get("node_id") or "") != str(binding.node_id)
            or evidence_row.get("node_type") != target_node_type
            or evidence_row.get("input_name") != binding.input_name
            or safe_model_relative_filename(submission_filename) != expected_filename
        ):
            raise ValidationError({
                "fixture": f"{semantic_key} 的精确模型提交枚举证据缺失或已漂移",
            })
        node = target_node
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict) or binding.input_name not in inputs:
            raise ValidationError({
                "fixture": f"模型 semantic 目标不存在: {binding.node_id}.{binding.input_name}",
            })
        inputs[binding.input_name] = submission_filename
        runtime_values[semantic_key] = submission_filename
    if runtime_graph != compiled.graph:
        compiled = replace(
            compiled,
            graph=runtime_graph,
            graph_sha256=json_contract(
                runtime_graph,
                max_bytes=_workflow_graph_max_bytes(),
            ).sha256,
            resolved_semantics=runtime_values,
            resolved_semantics_sha256=json_contract(
                runtime_values,
                max_bytes=_workflow_graph_max_bytes(),
            ).sha256,
        )
    expected_assets = sorted(
        (
            str(row.get("semantic_key") or ""),
            str(row.get("asset_id") or ""),
            str(row.get("checksum") or "").casefold(),
        )
        for row in asset_facts
    )
    compiled_assets = []
    for row in compiled.asset_bindings:
        semantic_key = str(row.get("semantic_key") or "")
        compiled_assets.append((
            semantic_key,
            str(row.get("asset_id") or ""),
            str(row.get("checksum") or "").casefold(),
        ))
        if row.get("audio_asset_id"):
            compiled_assets.append((
                f"{semantic_key}.audio",
                str(row.get("audio_asset_id") or ""),
                str(row.get("audio_checksum") or "").casefold(),
            ))
    compiled_assets.sort()
    if compiled_assets != expected_assets:
        raise ValidationError({
            "fixture": "具体素材未精确进入本次冻结执行图",
        })
    return compiled


def materialize_workflow_execution_graph(
    version: WorkflowTemplateVersion,
    *,
    values: dict[str, Any],
    reference_bindings: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    asset_facts: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    output_prefix: str,
    owner_id=None,
):
    """Compile one exact frozen WorkflowTemplateVersion for a durable execution."""

    return _materialize_smoke_graph(
        version,
        fixture_values=values,
        fixture_reference_bindings=reference_bindings,
        fixture_asset_facts=asset_facts,
        output_prefix=output_prefix,
        owner_id=owner_id,
        require_smoke_video_timing=False,
    )


def _stage_smoke_assets(*, compiled, version, deployment, driver) -> tuple[dict[str, Any], set[str]]:
    graph = compiled.graph
    if not compiled.asset_bindings:
        return graph, set()
    operations = set((driver.capabilities() or {}).get("operations") or [])
    if "upload" not in operations:
        raise ValidationError("i2v/media smoke-test 需要 provider upload 契约")
    asset_root = Path(settings.ASSET_LIBRARY_ROOT).resolve()
    replacements: dict[str, str] = {}
    uploaded_paths: set[str] = set()
    physical_bindings = []
    for binding in compiled.asset_bindings:
        physical_bindings.append({
            "asset_id": binding.get("asset_id"),
            "checksum": binding.get("checksum"),
            "token": binding.get("token"),
        })
        if binding.get("audio_asset_id"):
            audio_asset_id = str(binding.get("audio_asset_id") or "")
            physical_bindings.append({
                "asset_id": audio_asset_id,
                "checksum": binding.get("audio_checksum"),
                "token": f"__AUTOPROMPT_ASSET__:{audio_asset_id}",
            })
    seen_asset_ids: set[str] = set()
    for binding in physical_bindings:
        asset_id = str(binding.get("asset_id") or "")
        if not asset_id or asset_id in seen_asset_ids:
            raise ValidationError("compiled smoke graph 的素材 manifest 必须全局唯一")
        seen_asset_ids.add(asset_id)
        try:
            asset = Asset.objects.get(pk=asset_id, is_active=True, owner__isnull=True)
        except (Asset.DoesNotExist, ValueError) as exc:
            raise ValidationError("smoke asset 必须是启用的全局受管资产") from exc
        if not asset.local_path or asset.file:
            raise ValidationError("smoke asset 必须来自只读素材库 local_path")
        source = Path(asset.local_path).resolve()
        if not source.is_relative_to(asset_root) or not source.is_file():
            raise ValidationError("smoke asset 路径越过受控素材库或文件不存在")
        digest = sha256_file(source)
        if digest != str(binding.get("checksum") or "").casefold() or digest != str(asset.checksum or "").casefold():
            raise ValidationError("smoke asset checksum 与冻结 manifest 不一致")
        uploaded = driver.invoke(deployment, "upload", {
            "source_path": str(source),
            "subfolder": f"autoprompt/smoke/{version.id}",
            "filename": f"{asset.id}{source.suffix.lower() or '.bin'}",
        })
        name = str(uploaded.get("name") or "") if isinstance(uploaded, dict) else ""
        subfolder = str(uploaded.get("subfolder") or "") if isinstance(uploaded, dict) else ""
        relative_path = "/".join(filter(None, (subfolder, name)))
        if not name or not relative_path:
            raise ValidationError("provider upload 未返回可执行相对路径")
        replacements[str(binding["token"])] = relative_path
        uploaded_paths.add(relative_path)

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str) and value.startswith("__AUTOPROMPT_ASSET__:"):
            if value not in replacements:
                raise ValidationError("compiled smoke graph 含未授权 asset token")
            return replacements[value]
        return value

    return replace(graph), uploaded_paths


def _validate_semantic_bindings(
    graph: dict,
    input_contract: dict,
    bindings: list[dict[str, Any]],
    *,
    strict: bool,
    output_kind: str,
) -> None:
    seen: set[tuple[str, str]] = set()
    bound_keys: set[str] = set()
    parameters = input_contract.get("parameters") if isinstance(input_contract, dict) else None
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValidationError({"semantic_bindings": f"第 {index + 1} 项必须是对象"})
        semantic_key = str(binding.get("semantic_key") or "").strip()
        node_id = str(binding.get("node_id") or "").strip()
        input_name = str(binding.get("input_name") or "").strip()
        target = (node_id, input_name)
        if not semantic_key or target in seen:
            raise ValidationError({"semantic_bindings": "semantic_key 必须非空，node/input 目标必须唯一"})
        node = graph.get(node_id)
        if not isinstance(node, dict):
            raise ValidationError({"semantic_bindings": f"节点 {node_id} 不存在"})
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or input_name not in inputs:
            raise ValidationError({"semantic_bindings": f"节点 {node_id} 不包含输入 {input_name}"})
        current = inputs[input_name]
        if (
            isinstance(current, list) and len(current) == 2
            and isinstance(current[0], str) and isinstance(current[1], int)
        ):
            raise ValidationError({"semantic_bindings": "generic binding 禁止覆盖节点连接"})
        if strict:
            try:
                validate_semantic_key(semantic_key)
            except WorkflowSemanticError as exc:
                raise ValidationError({"semantic_bindings": str(exc)}) from exc
            value_type = str(binding.get("value_type") or "string")
            if value_type not in VALUE_TYPES:
                raise ValidationError({"semantic_bindings": f"{semantic_key} value_type 无效"})
            definition = parameters.get(semantic_key) if isinstance(parameters, dict) else None
            if not isinstance(definition, dict) or definition.get("type") != value_type:
                raise ValidationError({"semantic_bindings": f"{semantic_key} 缺少一致的参数 Schema"})
            expected = expected_value_type(semantic_key)
            if expected and value_type != expected and not (
                semantic_key == "video.fps" and value_type == "integer"
            ):
                raise ValidationError({"semantic_bindings": f"{semantic_key} 类型必须为 {expected}"})
        seen.add(target)
        bound_keys.add(semantic_key)
    if strict and isinstance(parameters, dict):
        missing_required = sorted(
            key for key, definition in parameters.items()
            if isinstance(definition, dict)
            and bool(definition.get("required", True))
            and key not in bound_keys
        )
        if missing_required:
            raise ValidationError({
                "semantic_bindings": (
                    "required parameter 缺少实际 node/input binding: "
                    + ", ".join(missing_required)
                ),
            })
    if strict:
        try:
            validate_resolution_strategy(
                graph=graph,
                contract=input_contract,
                bindings=bindings,
                output_kind=output_kind,
            )
        except WorkflowSemanticError as exc:
            raise ValidationError({"semantic_bindings": str(exc)}) from exc


def _validate_requirements(requirements: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    roles = set(ModelArtifact.Role.values)
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            raise ValidationError({"model_requirements": f"第 {index + 1} 项必须是对象"})
        role = str(requirement.get("role") or "")
        alias = str(requirement.get("alias") or "").strip()
        filename = str(requirement.get("expected_filename") or "").strip()
        if role not in roles or not alias or not filename:
            raise ValidationError({"model_requirements": "role/alias/expected_filename 无效"})
        if (role, alias) in seen:
            raise ValidationError({"model_requirements": "role+alias 必须唯一"})
        seen.add((role, alias))


def _validate_model_binding_coverage(
    input_contract: dict[str, Any],
    bindings: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> None:
    required_aliases = {
        str(row["alias"])
        for row in requirements
        if bool(row.get("is_required", True))
    }
    parameters = input_contract.get("parameters") or {}
    bound_aliases = set()
    for row in bindings:
        key = str(row.get("semantic_key") or "")
        if not key.startswith("model.") or not key.endswith(".filename"):
            continue
        definition = parameters.get(key) if isinstance(parameters, dict) else None
        fallback_alias = key[len("model."):-len(".filename")]
        bound_aliases.add(str((definition or {}).get("requirement_alias") or fallback_alias))
    missing = sorted(required_aliases - bound_aliases)
    if missing:
        raise ValidationError({
            "model_requirements": "required requirement 缺少 model semantic binding: " + ", ".join(missing),
        })


def _uses_strict_semantic_contract(compiler_id: str, compiler_version: int) -> bool:
    return compiler_id == "generic.comfy" and compiler_version in {1, 2, 3}


def _validate_classified_definition(graph, contract, model_specification, bindings, requirements):
    if contract.get("compiler") != {"id": "generic.comfy", "version": 3}:
        return
    from studio.services.workflow_classified_references import validate_frozen, ClassifiedReferenceError
    projector = validate_frozen_workflow_projector_contract(contract)
    try:
        from studio.services.workflow_projectors import _matches
        if not _matches(projector, graph):
            raise ClassifiedReferenceError("工作流图结构与冻结 Projector 规则不一致")
        capabilities = (model_specification or {}).get("capabilities")
        if capabilities is not None and contract.get("capability") not in capabilities:
            raise ClassifiedReferenceError("Projector 能力不在模型版本声明中")
        validate_frozen(contract, graph, projector.definition, model_specification, bindings, requirements)
    except ClassifiedReferenceError as exc:
        raise ValidationError({"input_contract": str(exc)}) from exc


def validate_workflow_mode_contract(input_contract: object) -> str:
    """Require one explicit runtime mode on every frozen workflow contract."""

    if not isinstance(input_contract, dict):
        raise ValidationError({
            "input_contract": "必须是包含非空 workflow_mode 的对象",
        })
    workflow_mode = input_contract.get("workflow_mode")
    if (
        not isinstance(workflow_mode, str)
        or not workflow_mode.strip()
        or workflow_mode != workflow_mode.strip()
    ):
        raise ValidationError({
            "input_contract": "workflow_mode 必须是无首尾空白的非空字符串",
        })
    return workflow_mode


@transaction.atomic
def import_workflow_version(
    *,
    template: WorkflowTemplate,
    graph: dict,
    input_contract: dict,
    semantic_bindings: list[dict[str, Any]],
    model_requirements: list[dict[str, Any]],
    provider: ProviderProfile | None,
    model_profile_version: ModelProfileVersion | None,
) -> WorkflowTemplateVersion:
    if not isinstance(graph, dict) or not graph:
        raise ValidationError({"graph": "ComfyUI API graph 必须是非空对象"})
    if not isinstance(input_contract, dict):
        raise ValidationError({"input_contract": "必须是对象"})
    for field, value in (("graph", graph), ("input_contract", input_contract)):
        sensitive = find_forbidden_secret_fields(value)
        if sensitive:
            raise ValidationError({
                field: f"工作流合同不能保存明文密钥字段: {', '.join(sensitive)}",
            })
    validate_workflow_mode_contract(input_contract)
    validate_frozen_workflow_projector_contract(input_contract)
    configuration = resolve_configuration(required_keys=("workflow.max_graph_bytes",))
    graph_limit = int(configuration.values["workflow.max_graph_bytes"])
    graph_contract = json_contract(graph, max_bytes=graph_limit)
    input_contract_meta = json_contract(input_contract, max_bytes=graph_limit)
    try:
        validate_comfy_graph(graph)
    except WorkflowBuildError as exc:
        raise ValidationError({"graph": str(exc)}) from exc
    try:
        compiler_id, compiler_version = compiler_contract_from_input(input_contract)
    except WorkflowCompilerError as exc:
        raise ValidationError({"input_contract": str(exc)}) from exc
    model_specification = (
        verified_model_profile_specification(model_profile_version)
        if model_profile_version is not None else None
    )
    resolve_conditioning_contract(
        workflow_input_contract=input_contract,
        workflow_compiler_id=compiler_id,
        model_specification=model_specification,
        output_kind=template.output_kind,
        required=False,
    )
    resolve_execution_monitoring_contract(
        model_specification=model_specification,
        workflow_input_contract=input_contract,
        workflow_graph=graph,
    )
    strict_semantics = _uses_strict_semantic_contract(compiler_id, compiler_version)
    _validate_semantic_bindings(
        graph,
        input_contract,
        semantic_bindings,
        strict=strict_semantics,
        output_kind=template.output_kind,
    )
    _validate_requirements(model_requirements)
    if strict_semantics:
        _validate_model_binding_coverage(input_contract, semantic_bindings, model_requirements)
    _validate_classified_definition(graph, input_contract, model_specification, semantic_bindings, model_requirements)

    WorkflowTemplate.objects.select_for_update().get(pk=template.pk)
    next_version = (
        WorkflowTemplateVersion.objects.filter(template=template)
        .aggregate(value=Max("version"))["value"]
        or 0
    ) + 1
    binding_rows = canonical_binding_rows(semantic_bindings)
    requirement_rows = canonical_requirement_rows([
        {
            **item,
            "artifact_id": str(item.get("artifact_id")) if item.get("artifact_id") else None,
        }
        for item in model_requirements
    ])
    _, execution_digest = workflow_execution_contract(
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        graph_sha256=graph_contract.sha256,
        input_contract_sha256=input_contract_meta.sha256,
        bindings=binding_rows,
        requirements=requirement_rows,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template,
        version=next_version,
        provider=provider,
        model_profile_version=model_profile_version,
        graph=graph,
        graph_schema_version=graph_contract.schema_version,
        graph_size_bytes=graph_contract.size_bytes,
        graph_sha256=graph_contract.sha256,
        input_contract=input_contract,
        input_contract_schema_version=input_contract_meta.schema_version,
        input_contract_size_bytes=input_contract_meta.size_bytes,
        input_contract_sha256=input_contract_meta.sha256,
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        semantic_bindings_sha256=json_contract(
            binding_rows,
            max_bytes=graph_limit,
        ).sha256,
        model_requirements_sha256=json_contract(
            requirement_rows,
            max_bytes=graph_limit,
        ).sha256,
        execution_contract_sha256=execution_digest,
        status=WorkflowTemplateVersion.Status.DRAFT,
        validation_message="结构校验已通过；尚未连接运行节点校验。",
    )
    WorkflowSemanticBinding.objects.bulk_create([
        WorkflowSemanticBinding(
            workflow_version=version,
            semantic_key=str(item["semantic_key"]).strip(),
            node_id=str(item["node_id"]).strip(),
            input_name=str(item["input_name"]).strip(),
            value_type=str(item.get("value_type") or "string")[:32],
            is_required=bool(item.get("is_required", True)),
        )
        for item in semantic_bindings
    ])
    artifact_ids = {
        str(item.get("artifact_id"))
        for item in model_requirements
        if item.get("artifact_id")
    }
    artifacts = {
        str(item.id): item
        for item in ModelArtifact.objects.filter(pk__in=artifact_ids)
    }
    if artifact_ids - set(artifacts):
        raise ValidationError({"model_requirements": "存在无效 artifact_id"})
    if model_profile_version is not None:
        foreign = sorted(
            item.logical_name
            for item in artifacts.values()
            if item.profile_version_id != model_profile_version.id
        )
        if foreign:
            raise ValidationError({
                "model_requirements": "artifact 必须属于 workflow 绑定的 model profile: "
                + ", ".join(foreign),
            })
    WorkflowModelRequirement.objects.bulk_create([
        WorkflowModelRequirement(
            workflow_version=version,
            role=str(item["role"]),
            alias=str(item["alias"]).strip(),
            artifact=artifacts.get(str(item.get("artifact_id"))),
            expected_filename=str(item["expected_filename"]).strip(),
            expected_sha256=str(item.get("expected_sha256") or "").strip(),
            is_required=bool(item.get("is_required", True)),
        )
        for item in model_requirements
    ])
    return version


def _verify_workflow_storage_integrity(
    version: WorkflowTemplateVersion,
    *,
    graph_limit: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Verify recorded bytes and relationships, without granting execution."""
    if graph_limit is None:
        configuration = resolve_configuration(
            required_keys=("workflow.max_graph_bytes",),
        )
        graph_limit = int(configuration.values["workflow.max_graph_bytes"])
    verify_json_contract(
        version.graph,
        schema_version=version.graph_schema_version,
        size_bytes=version.graph_size_bytes,
        sha256=version.graph_sha256,
        max_bytes=graph_limit,
    )
    verify_json_contract(
        version.input_contract,
        schema_version=version.input_contract_schema_version,
        size_bytes=version.input_contract_size_bytes,
        sha256=version.input_contract_sha256,
        max_bytes=graph_limit,
    )
    compiler = version.input_contract.get("compiler") if isinstance(version.input_contract, dict) else None
    if not isinstance(compiler, dict) or (compiler.get("id"), compiler.get("version")) != (version.compiler_id, version.compiler_version):
        raise ValidationError("workflow compiler 字段与 input_contract 不一致")
    bindings = _binding_rows(version)
    requirements = _requirement_rows(version)
    payload, digest = workflow_execution_contract(
        compiler_id=version.compiler_id,
        compiler_version=version.compiler_version,
        graph_sha256=version.graph_sha256,
        input_contract_sha256=version.input_contract_sha256,
        bindings=bindings,
        requirements=requirements,
    )
    if payload["semantic_bindings_sha256"] != version.semantic_bindings_sha256:
        raise ValidationError("semantic bindings 已在证据生成后漂移")
    if payload["model_requirements_sha256"] != version.model_requirements_sha256:
        raise ValidationError("model requirements 已在证据生成后漂移")
    if digest != version.execution_contract_sha256:
        raise ValidationError("workflow execution contract hash 不一致")
    return bindings, requirements


def verify_workflow_version_integrity(
    version: WorkflowTemplateVersion,
    *,
    graph_limit: int | None = None,
) -> None:
    bindings, requirements = _verify_workflow_storage_integrity(version, graph_limit=graph_limit)
    validate_workflow_mode_contract(version.input_contract)
    validate_frozen_workflow_projector_contract(version.input_contract)
    try:
        validate_comfy_graph(version.graph)
    except WorkflowBuildError as exc:
        raise ValidationError({"graph": str(exc)}) from exc
    try:
        compiler_id, compiler_version = compiler_contract_from_input(version.input_contract)
    except WorkflowCompilerError as exc:
        raise ValidationError({"input_contract": str(exc)}) from exc
    if (compiler_id, compiler_version) != (version.compiler_id, version.compiler_version):
        raise ValidationError("workflow compiler 字段与 input_contract 不一致")
    model_specification = (
        verified_model_profile_specification(version.model_profile_version)
        if version.model_profile_version_id else None
    )
    resolve_conditioning_contract(
        workflow_input_contract=version.input_contract,
        workflow_compiler_id=compiler_id,
        model_specification=model_specification,
        output_kind=version.template.output_kind,
        required=False,
    )
    resolve_execution_monitoring_contract(
        model_specification=model_specification,
        workflow_input_contract=version.input_contract,
        workflow_graph=version.graph,
    )
    strict_semantics = _uses_strict_semantic_contract(compiler_id, compiler_version)
    _validate_semantic_bindings(
        version.graph,
        version.input_contract,
        bindings,
        strict=strict_semantics,
        output_kind=version.template.output_kind,
    )
    _validate_requirements(requirements)
    if strict_semantics:
        _validate_model_binding_coverage(version.input_contract, bindings, requirements)
    _validate_classified_definition(version.graph, version.input_contract, model_specification, bindings, requirements)


def resolve_workflow_canvas_export_deployment(
    version: WorkflowTemplateVersion,
    *,
    requested_deployment_id=None,
) -> tuple[WorkflowTemplateVersion, ModelDeployment, str]:
    """Resolve the only deployment whose node catalog may shape a UI export.

    Validated lifecycle rows are pinned to their persisted evidence. A draft
    without evidence must name an active ComfyUI deployment explicitly; there
    is intentionally no process-global/default deployment fallback.
    """

    version = WorkflowTemplateVersion.objects.select_related(
        "template", "provider", "model_profile_version",
        "validated_deployment__profile_version__provider",
    ).prefetch_related("model_requirements__artifact").get(pk=version.pk)
    requested_id = str(requested_deployment_id or "").strip()
    evidence_bound = version.status in {
        WorkflowTemplateVersion.Status.VALIDATED,
        WorkflowTemplateVersion.Status.PUBLISHED,
        WorkflowTemplateVersion.Status.RETIRED,
    }

    if evidence_bound:
        if not version.validated_deployment_id:
            raise ValidationError({
                "deployment_id": "validated/published workflow 缺少持久化 validated deployment；禁止回退默认 ComfyUI",
            })
        if requested_id and requested_id != str(version.validated_deployment_id):
            raise ValidationError({
                "deployment_id": "该 workflow 已绑定 validated deployment，不能改用其他 ComfyUI 部署导出",
            })
        deployment = version.validated_deployment
        deployment_source = "validated"
    else:
        if version.status != WorkflowTemplateVersion.Status.DRAFT:
            raise ValidationError({"deployment_id": "当前 workflow 状态不能导出 ComfyUI 画布"})
        if version.validated_deployment_id:
            raise ValidationError({
                "deployment_id": "draft workflow 残留 validated deployment，状态与证据不一致；请重新校验",
            })
        if not requested_id:
            raise ValidationError({
                "deployment_id": "未校验草稿必须显式选择 active ComfyUI deployment；禁止回退系统默认值",
            })
        deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).filter(pk=requested_id).first()
        if deployment is None:
            raise ValidationError({"deployment_id": "所选 ModelDeployment 不存在"})
        deployment_source = "explicit_draft"

    provider = deployment.profile_version.provider
    if deployment.status != ModelDeployment.Status.ACTIVE:
        raise ValidationError({"deployment_id": "所选 ModelDeployment 已停用或异常"})
    if not provider.is_active:
        raise ValidationError({"deployment_id": "所选 ModelDeployment 的 Provider 已停用"})
    if deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED:
        raise ValidationError({"deployment_id": "所选 ModelDeployment 的模型版本不是 published"})
    if provider.driver_type != ProviderProfile.DriverType.COMFYUI:
        raise ValidationError({"deployment_id": "所选 ModelDeployment 不是 ComfyUI 驱动"})
    if version.provider_id and version.provider_id != provider.id:
        raise ValidationError({"deployment_id": "deployment provider 与 workflow 绑定已漂移"})
    if (
        version.model_profile_version_id
        and version.model_profile_version_id != deployment.profile_version_id
    ):
        raise ValidationError({"deployment_id": "deployment model profile 与 workflow 精确绑定已漂移"})

    verify_workflow_version_integrity(version)
    if evidence_bound:
        if (
            version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED
            or not version.provider_id
            or not version.model_profile_version_id
        ):
            raise ValidationError({
                "deployment_id": "validated/published workflow 的绑定或通过状态不完整",
            })
        evidence = version.validation_evidence
        if (
            not isinstance(evidence, dict)
            or _evidence_digest(evidence) != version.validation_evidence_sha256
            or str(evidence.get("deployment_id") or "") != str(deployment.id)
            or evidence.get("execution_target_sha256")
            != _execution_target_sha256(version, deployment)
        ):
            raise ValidationError({
                "deployment_id": "validated deployment/provider/model/artifact 配置或证据已漂移",
            })

    return version, deployment, deployment_source


def validate_workflow_version(
    version: WorkflowTemplateVersion,
    *,
    deployment: ModelDeployment,
) -> tuple[WorkflowTemplateVersion, dict[str, Any]]:
    # Invalidate previous evidence before doing network I/O.  This first,
    # committed transaction prevents a concurrent publish from reusing stale
    # PASSED evidence while a re-validation is in flight.
    with transaction.atomic():
        deployment = ModelDeployment.objects.select_for_update(of=("self",)).select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        version = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).select_related(
            "provider", "template", "model_profile_version",
        ).prefetch_related("model_requirements__artifact").get(pk=version.pk)
        if version.status in {
            WorkflowTemplateVersion.Status.PUBLISHED,
            WorkflowTemplateVersion.Status.RETIRED,
        }:
            raise ValidationError("已发布/停用版本不可重新写入校验状态，请导入新版本")
        if not version.provider_id:
            raise ValidationError("workflow version 未绑定 provider")
        if deployment.profile_version.provider_id != version.provider_id:
            raise ValidationError("deployment provider 与 workflow provider 不匹配")
        if deployment.profile_version_id != version.model_profile_version_id:
            raise ValidationError("deployment model profile 与 workflow 精确绑定不一致")
        if (
            deployment.status != ModelDeployment.Status.ACTIVE
            or not deployment.profile_version.provider.is_active
            or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
        ):
            raise ValidationError("deployment/provider/model profile 当前不可用于验证")
        verify_workflow_version_integrity(version)
        expected_graph_sha256 = version.graph_sha256
        expected_execution_sha256 = version.execution_contract_sha256
        expected_target_sha256 = _execution_target_sha256(version, deployment)
        validation_generation = uuid.uuid4().hex
        inflight_validation_evidence = {
            "schema_version": 1,
            "inflight_validation_generation": validation_generation,
        }
        version.status = WorkflowTemplateVersion.Status.DRAFT
        version.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
        version.validation_message = "live validation 执行中。"
        version.validated_deployment = None
        version.validation_evidence = inflight_validation_evidence
        version.validation_evidence_sha256 = _evidence_digest(
            inflight_validation_evidence,
        )
        version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
        version.smoke_test_message = "live validation 更新后必须重新执行真实 smoke-test。"
        version.smoke_test_evidence = {}
        version.smoke_test_evidence_sha256 = ""
        version.save(update_fields=(
            "status", "validation_state", "validation_message", "validated_deployment",
            "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
            "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
            "updated_at",
        ))
    driver = driver_for(version.provider)
    try:
        object_info = driver.invoke(deployment, "object_info", {})
        report = validate_comfyui_api_contract(
            version.graph,
            object_info,
            allow_managed_asset_tokens=True,
        )
        compiler_probes = []
        from studio.services.workflow_resolution import CONFIGURED_RESOLUTION_ADAPTER, materialize_resolution_graph
        strategy = version.input_contract.get("resolution_strategy") or {}
        if strategy.get("adapter") == CONFIGURED_RESOLUTION_ADAPTER:
            for size in strategy["sizes"]:
                probe = materialize_resolution_graph(version.graph, strategy, size["width"], size["height"])
                probe_report = validate_comfyui_api_contract(probe, object_info, allow_managed_asset_tokens=True)
                compiler_probes.append({
                    "kind": "configured_image_resolution", "size": size,
                    "graph_sha256": json_contract(probe, max_bytes=_workflow_graph_max_bytes()).sha256,
                    "contract": probe_report.as_dict(),
                })
        if (version.compiler_id, version.compiler_version) == ("generic.comfy", 2):
            from studio.services.workflow_asset_groups import materialize_group_graph
            for boundary in ("min_items", "max_items"):
                selected = {
                    key for group in version.input_contract["asset_groups"]
                    for key in group["semantic_keys"][:group[boundary]]
                }
                probe = materialize_group_graph(version.graph, version.input_contract, selected)
                probe_report = validate_comfyui_api_contract(probe, object_info, allow_managed_asset_tokens=True)
                compiler_probes.append({
                    "kind": f"generic_asset_groups_{boundary}",
                    "graph_sha256": json_contract(probe, max_bytes=_workflow_graph_max_bytes()).sha256,
                    "contract": probe_report.as_dict(),
                })
        if (version.compiler_id, version.compiler_version) == ("generic.comfy", 3):
            from studio.services.workflow_classified_references import materialize, probe_cases
            for kind, references in probe_cases(version.input_contract):
                probe_graph = materialize(version.graph, version.input_contract, references)
                probe_report = validate_comfyui_api_contract(probe_graph, object_info, allow_managed_asset_tokens=True)
                compiler_probes.append({
                    "kind": kind,
                    "graph_sha256": json_contract(probe_graph, max_bytes=_workflow_graph_max_bytes()).sha256,
                    "contract": probe_report.as_dict(),
                })
        inventory = _validate_model_inventory(version, deployment, driver, object_info)
    except Exception as exc:
        validation_superseded = False
        with transaction.atomic():
            failed = WorkflowTemplateVersion.objects.select_for_update().get(pk=version.pk)
            validation_superseded = (
                failed.validation_state
                != WorkflowTemplateVersion.ValidationState.NOT_RUN
                or failed.validation_evidence != inflight_validation_evidence
                or failed.validation_evidence_sha256
                != _evidence_digest(inflight_validation_evidence)
            )
            if not validation_superseded:
                failed.status = WorkflowTemplateVersion.Status.DRAFT
                failed.validation_state = WorkflowTemplateVersion.ValidationState.FAILED
                failed.validation_message = str(
                    getattr(exc, "operator_message", str(exc)),
                )[:2000]
                failed.validated_deployment = None
                failed.validation_evidence = {}
                failed.validation_evidence_sha256 = ""
                failed.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
                failed.smoke_test_message = "live validation 失败，smoke-test 证据已失效。"
                failed.smoke_test_evidence = {}
                failed.smoke_test_evidence_sha256 = ""
                failed.save(update_fields=(
                    "status", "validation_state", "validation_message", "validated_deployment",
                    "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
                    "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
                    "updated_at",
                ))
        if validation_superseded:
            raise ValidationError({
                "graph": "此次 live validation 已被较新的校验请求取代。",
            }) from exc
        if isinstance(exc, WorkflowModelEvidenceUnavailable):
            raise exc
        raise ValidationError({"graph": str(exc)}) from exc
    validation_drifted = False
    validation_superseded = False
    with transaction.atomic():
        current_deployment = ModelDeployment.objects.select_for_update(of=("self",)).select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        version = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).select_related(
            "template",
        ).get(pk=version.pk)
        if (
            version.validation_state
            != WorkflowTemplateVersion.ValidationState.NOT_RUN
            or version.validation_evidence != inflight_validation_evidence
            or version.validation_evidence_sha256
            != _evidence_digest(inflight_validation_evidence)
        ):
            validation_superseded = True
        elif (
            version.graph_sha256 != expected_graph_sha256
            or version.execution_contract_sha256 != expected_execution_sha256
            or _execution_target_sha256(version, current_deployment) != expected_target_sha256
        ):
            validation_drifted = True
            version.status = WorkflowTemplateVersion.Status.DRAFT
            version.validation_state = WorkflowTemplateVersion.ValidationState.FAILED
            version.validation_message = "workflow 在 live validation 期间已变更，请重新校验"
            version.validated_deployment = None
            version.validation_evidence = {}
            version.validation_evidence_sha256 = ""
        else:
            version.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
            version.status = WorkflowTemplateVersion.Status.VALIDATED
            version.validation_message = "已通过连接节点的 ComfyUI object_info 契约校验。"
            version.validated_deployment = deployment
            version.validation_evidence = {
                "schema_version": 1,
                "deployment_id": str(deployment.id),
                "model_profile_version_id": str(deployment.profile_version_id),
                "graph_sha256": version.graph_sha256,
                "execution_contract_sha256": version.execution_contract_sha256,
                "contract": report.as_dict(),
                "compiler_probes": compiler_probes,
                "inventory": inventory,
                "execution_target": _execution_target_evidence(version, current_deployment),
                "execution_target_sha256": expected_target_sha256,
            }
            version.validation_evidence_sha256 = _evidence_digest(version.validation_evidence)
        if not validation_superseded:
            version.save(update_fields=(
                "validation_state", "status", "validation_message", "validated_deployment",
                "validation_evidence", "validation_evidence_sha256", "updated_at",
            ))
    if validation_superseded:
        raise ValidationError("此次 live validation 已被较新的校验请求取代。")
    if validation_drifted:
        raise ValidationError("workflow 在 live validation 期间已变更，请重新校验")
    return version, report.as_dict()


def smoke_test_workflow_version(
    version: WorkflowTemplateVersion,
    *,
    deployment: ModelDeployment,
    fixture_values: dict[str, Any],
    fixture_reference_bindings: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    fixture_asset_facts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    materialized_graph_sha256: str = "",
    fixture_sha256: str,
    smoke_attempt_id: str,
    expected_claim_token=None,
    submission_token: str | None = None,
    submitted_before: bool = False,
    on_submission_started=None,
    on_submitted=None,
    poll_control=None,
) -> tuple[WorkflowTemplateVersion, dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{64}", str(fixture_sha256 or "").casefold()):
        raise ValidationError("smoke-test fixture hash 无效")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        str(materialized_graph_sha256 or "").casefold(),
    ):
        raise ValidationError("workflow smoke 缺少冻结执行图 hash")
    preflight_target_drifted = False
    with transaction.atomic():
        deployment = ModelDeployment.objects.select_for_update(of=("self",)).select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        version = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).select_related(
            "provider", "template", "model_profile_version", "validated_deployment",
        ).get(pk=version.pk)
        if version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED:
            raise ValidationError("必须先通过 live validate")
        if not version.provider_id:
            raise ValidationError("workflow version 未绑定 provider")
        if deployment.profile_version_id != version.model_profile_version_id:
            raise ValidationError("deployment model profile 与 workflow 精确绑定不一致")
        if version.validated_deployment_id != deployment.id:
            raise ValidationError("smoke-test 必须使用通过 live validation 的同一 deployment")
        verify_workflow_version_integrity(version)
        expected_target_sha256 = _execution_target_sha256(version, deployment)
        if (
            version.validation_evidence.get("execution_target_sha256")
            != expected_target_sha256
        ):
            preflight_target_drifted = True
            version.status = WorkflowTemplateVersion.Status.DRAFT
            version.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            version.validation_message = "validated deployment/provider/model/artifact 配置已漂移"
            version.validated_deployment = None
            version.validation_evidence = {}
            version.validation_evidence_sha256 = ""
            version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            version.smoke_test_message = "live validation 证据漂移，smoke-test 证据已失效。"
            version.smoke_test_evidence = {}
            version.smoke_test_evidence_sha256 = ""
            version.save(update_fields=(
                "status", "validation_state", "validation_message", "validated_deployment",
                "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
                "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
                "updated_at",
            ))
        expected_graph_sha256 = version.graph_sha256
        expected_execution_sha256 = version.execution_contract_sha256
        expected_validation_sha256 = version.validation_evidence_sha256
        if not preflight_target_drifted:
            version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            version.smoke_test_message = "真实 smoke-test 执行中。"
            version.smoke_test_evidence = {}
            version.smoke_test_evidence_sha256 = ""
            version.save(update_fields=(
                "smoke_test_state", "smoke_test_message", "smoke_test_evidence",
                "smoke_test_evidence_sha256", "updated_at",
            ))
    if preflight_target_drifted:
        raise ValidationError("validated deployment/provider/model/artifact 配置已漂移")
    driver = driver_for(version.provider)
    request_id = ""
    pre_submit_resource_release = None
    structural_contract_drifted = False
    try:
        operations = set((driver.capabilities() or {}).get("operations") or [])
        required_operations = {"submit", "poll", "cancel", "collect"}
        if not required_operations <= operations:
            raise ValidationError(
                "provider driver 缺少真实 smoke-test submit/poll/cancel/collect 契约；版本保持 experimental",
            )
        configuration = resolve_configuration(required_keys=(
            "workflow.smoke_hard_timeout_seconds", "artifact.max_bytes",
            "workflow.smoke_poll_interval_seconds",
        ))
        timeout = int(configuration.values["workflow.smoke_hard_timeout_seconds"])
        artifact_max_bytes = int(configuration.values["artifact.max_bytes"])
        poll_interval = float(configuration.values["workflow.smoke_poll_interval_seconds"])
        health = driver.invoke(deployment, "health", {})
        object_info = driver.invoke(deployment, "object_info", {})
        compiled = _materialize_smoke_graph(
            version,
            fixture_values=fixture_values,
            fixture_reference_bindings=fixture_reference_bindings,
            fixture_asset_facts=fixture_asset_facts,
        )
        if (
            materialized_graph_sha256
            and compiled.graph_sha256 != materialized_graph_sha256
        ):
            raise ProviderDriverError(
                "workflow smoke 重编图与 enqueue 冻结图不一致",
                code="workflow_smoke_contract_drifted",
            )
        graph, uploaded_paths = _stage_smoke_assets(
            compiled=compiled,
            version=version,
            deployment=deployment,
            driver=driver,
        )
        try:
            report = validate_comfyui_api_contract(
                graph,
                object_info,
                uploaded_paths=uploaded_paths,
            )
        except ComfyUIContractError:
            structural_contract_drifted = True
            raise
        submitted_graph_sha256 = json_contract(
            graph,
            max_bytes=_workflow_graph_max_bytes(),
        ).sha256
        request_id = str(submission_token or "") if submitted_before else ""
        if not request_id:
            if callable(on_submission_started):
                on_submission_started()
            submitted = driver.invoke(deployment, "submit", {
                "workflow": graph,
                "client_id": f"autoprompt-smoke-{version.id}",
                "submission_token": str(submission_token or ""),
            })
            request_id = str(submitted.get("request_id") or "") if isinstance(submitted, dict) else ""
            if isinstance(submitted, dict):
                release_audit = submitted.get("pre_submit_resource_release")
                if isinstance(release_audit, dict):
                    pre_submit_resource_release = release_audit
            if callable(on_submitted):
                on_submitted(request_id)
        if not request_id:
            raise ProviderDriverError("smoke submit 未返回 request_id", code="provider_response_invalid")
        deadline = time.monotonic() + timeout
        while True:
            poll = driver.invoke(deployment, "poll", {"request_id": request_id})
            remote_status = str(poll.get("status") or "") if isinstance(poll, dict) else ""
            control = poll_control(remote_status) if callable(poll_control) else ""
            if control:
                try:
                    driver.invoke(deployment, "cancel", {"request_id": request_id})
                except Exception:
                    pass
                raise ProviderDriverError(
                    f"真实 smoke-test {control} 并已请求取消",
                    code=f"provider_smoke_{control}",
                )
            if remote_status == "succeeded":
                break
            if remote_status == "failed":
                remote_error = (
                    _comfyui_failure_message(poll.get("failure"), graph=graph)
                    if isinstance(poll, dict)
                    else ""
                )
                raise ProviderDriverError(
                    remote_error or (
                        "真实 smoke-test 远端执行失败，但执行器未返回具体节点原因。"
                        "处理建议：在 ComfyUI History 中定位失败节点，修正后创建新的 Smoke 任务。"
                    ),
                    code="provider_smoke_failed",
                )
            if time.monotonic() >= deadline:
                try:
                    driver.invoke(deployment, "cancel", {"request_id": request_id})
                except Exception:
                    pass
                raise ProviderDriverError("真实 smoke-test 超时并已请求取消", code="provider_smoke_timeout")
            time.sleep(poll_interval)
        collected = driver.invoke(deployment, "collect", {"request_id": request_id})
        outputs = collected.get("outputs") if isinstance(collected, dict) else None
        if not isinstance(outputs, list) or not outputs:
            raise ProviderDriverError("真实 smoke-test 没有输出", code="provider_smoke_output_missing")
        primary_output = compiled.output_contract.get("primary")
        if not isinstance(primary_output, dict):
            raise ProviderDriverError(
                "真实 smoke-test 缺少冻结 primary output 契约",
                code="provider_smoke_output_invalid",
            )
        expected_kind = str(primary_output.get("kind") or "")
        expected_node_id = str(primary_output.get("node_id") or "")
        matching = [
            item for item in outputs
            if isinstance(item, dict)
            and (
                (expected_kind == WorkflowTemplate.OutputKind.TEXT and item.get("kind") == "text")
                or classify_task_output(item) == expected_kind
            )
            and str(item.get("node_id") or "") == expected_node_id
        ]
        if len(matching) != 1:
            raise ProviderDriverError(
                "真实 smoke-test 的声明 primary node 必须且只能产生一个匹配输出",
                code="provider_smoke_output_invalid",
            )
        verified_outputs = []
        if expected_kind == WorkflowTemplate.OutputKind.TEXT:
            item = matching[0]
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ProviderDriverError(
                    "smoke-test text 输出为空或合同无效",
                    code="provider_smoke_output_invalid",
                )
            payload = text.encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            if (
                len(payload) > ComfyUIClient.TEXT_OUTPUT_MAX_BYTES
                or item.get("size_bytes") != len(payload)
                or item.get("sha256") != digest
            ):
                raise ProviderDriverError(
                    "smoke-test text 输出 size/hash 无效",
                    code="provider_smoke_output_invalid",
                )
            verified_outputs.append({
                "node_id": expected_node_id,
                "kind": "text",
                "size_bytes": len(payload),
                "sha256": digest,
            })
        else:
            output_root_raw = str(getattr(settings, "COMFYUI_OUTPUT_DIR", "") or "").strip()
            if not output_root_raw:
                raise ProviderDriverError(
                    "production smoke-test 需要可访问的 ComfyUI 输出目录",
                    code="provider_smoke_output_unavailable",
                )
            output_root = Path(output_root_raw).resolve()
            for item in matching:
                filename = PurePosixPath(str(item.get("filename") or "").replace("\\", "/")).name
                subfolder = str(item.get("subfolder") or "").replace("\\", "/")
                candidate = (output_root / subfolder / filename).resolve()
                if not filename or not candidate.is_relative_to(output_root):
                    raise ProviderDriverError(
                        "smoke-test 输出路径越过受控目录",
                        code="provider_smoke_output_invalid",
                    )
                try:
                    facts = verified_local_media_facts(
                        candidate,
                        expected_kind=expected_kind,
                        max_bytes=artifact_max_bytes,
                        include_observed_media=(
                            expected_kind in {
                                WorkflowTemplate.OutputKind.VIDEO,
                                WorkflowTemplate.OutputKind.AUDIO,
                            }
                        ),
                    )
                except Exception as exc:
                    raise ProviderDriverError(
                        "smoke-test 输出真实字节校验失败",
                        code="provider_smoke_output_invalid",
                    ) from exc
                if expected_kind == WorkflowTemplate.OutputKind.VIDEO:
                    observed = facts.get("observed_media")
                    expected_semantics = compiled.resolved_semantics
                    mismatches: list[str] = []
                    if not isinstance(observed, dict):
                        mismatches.append("observed_media")
                    else:
                        for semantic_key, observed_key in (
                            ("image.width", "width"),
                            ("image.height", "height"),
                            ("video.frames", "frames"),
                        ):
                            expected_value = expected_semantics.get(semantic_key)
                            if (
                                isinstance(expected_value, int)
                                and not isinstance(expected_value, bool)
                                and expected_value > 0
                                and observed.get(observed_key) != expected_value
                            ):
                                mismatches.append(semantic_key)
                        expected_fps = expected_semantics.get("video.fps")
                        if (
                            isinstance(expected_fps, (int, float))
                            and not isinstance(expected_fps, bool)
                            and float(expected_fps) > 0
                            and float(observed.get("fps") or 0) != float(expected_fps)
                        ):
                            mismatches.append("video.fps")
                        explicit_audio = expected_semantics.get("audio.enabled")
                        if (
                            explicit_audio is True
                            and observed.get("has_audio") is not True
                        ):
                            mismatches.append("audio.enabled")
                    if mismatches:
                        raise ProviderDriverError(
                            "workflow smoke 输出未满足冻结语义: "
                            + ", ".join(sorted(set(mismatches))),
                            code="provider_smoke_output_invalid",
                        )
                if compiled.output_contract.get("schema_version") == 2:
                    from studio.services.workflow_resolution import validate_resolved_image_output
                    try:
                        validate_resolved_image_output(candidate, compiled.output_contract)
                    except ValidationError as exc:
                        # Remote collection has finished: this is a definitive
                        # output rejection, not an uncertain remote execution.
                        raise ProviderDriverError(
                            str(exc), code="provider_smoke_output_invalid",
                        ) from exc
                verified_outputs.append(facts)
    except Exception as exc:
        with transaction.atomic():
            ModelDeployment.objects.select_for_update(of=("self",)).get(
                pk=deployment.pk,
            )
            failed = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).get(
                pk=version.pk,
            )
            claim_is_current = True
            if expected_claim_token is not None:
                locked_attempt = WorkflowSmokeAttempt.objects.select_for_update(
                    of=("self",),
                ).get(pk=smoke_attempt_id)
                claim_is_current = (
                    locked_attempt.claim_token == expected_claim_token
                    and locked_attempt.status
                    not in {
                        WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
                        WorkflowSmokeAttempt.Status.CANCELLED,
                    }
                )
            owns_validation_generation = (
                claim_is_current
                and
                failed.validation_state
                == WorkflowTemplateVersion.ValidationState.PASSED
                and failed.validated_deployment_id == deployment.id
                and failed.validation_evidence_sha256 == expected_validation_sha256
            )
            if owns_validation_generation:
                if structural_contract_drifted:
                    failed.status = WorkflowTemplateVersion.Status.DRAFT
                    failed.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
                    failed.validation_message = "实时 ComfyUI 节点合同已漂移，请重新 validate。"
                    failed.validated_deployment = None
                    failed.validation_evidence = {}
                    failed.validation_evidence_sha256 = ""
                failed.smoke_test_state = WorkflowTemplateVersion.ValidationState.FAILED
                failed.smoke_test_message = str(exc)[:2000]
                failed.smoke_test_evidence = {}
                failed.smoke_test_evidence_sha256 = ""
                update_fields = [
                    "smoke_test_state", "smoke_test_message", "smoke_test_evidence",
                    "smoke_test_evidence_sha256", "updated_at",
                ]
                if structural_contract_drifted:
                    update_fields.extend((
                        "status", "validation_state", "validation_message",
                        "validated_deployment", "validation_evidence",
                        "validation_evidence_sha256",
                    ))
                failed.save(update_fields=update_fields)
        raise ValidationError({"detail": str(exc)}) from exc
    smoke_drifted = False
    smoke_superseded = False
    smoke_worker_superseded = False
    smoke_cancelled = False
    with transaction.atomic():
        current_deployment = ModelDeployment.objects.select_for_update(of=("self",)).select_related(
            "profile_version__provider",
        ).get(pk=deployment.pk)
        version = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).select_related(
            "template",
        ).get(pk=version.pk)
        if expected_claim_token is not None:
            locked_attempt = WorkflowSmokeAttempt.objects.select_for_update(
                of=("self",),
            ).get(pk=smoke_attempt_id)
            smoke_worker_superseded = (
                locked_attempt.claim_token != expected_claim_token
            )
            smoke_cancelled = locked_attempt.status in {
                WorkflowSmokeAttempt.Status.CANCEL_REQUESTED,
                WorkflowSmokeAttempt.Status.CANCELLED,
            }
        if smoke_worker_superseded:
            pass
        elif smoke_cancelled:
            pass
        elif (
            version.validation_evidence_sha256 != expected_validation_sha256
            or version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED
            or version.validated_deployment_id != current_deployment.id
        ):
            smoke_superseded = True
        elif (
            version.graph_sha256 != expected_graph_sha256
            or version.execution_contract_sha256 != expected_execution_sha256
            or _execution_target_sha256(version, current_deployment) != expected_target_sha256
        ):
            smoke_drifted = True
            version.status = WorkflowTemplateVersion.Status.DRAFT
            version.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
            version.validation_message = "workflow 在 smoke-test 期间已变更，请重新校验"
            version.validated_deployment = None
            version.validation_evidence = {}
            version.validation_evidence_sha256 = ""
            version.smoke_test_state = WorkflowTemplateVersion.ValidationState.FAILED
            version.smoke_test_message = "workflow 在 smoke-test 期间已变更，请重新校验"
            version.smoke_test_evidence = {}
            version.smoke_test_evidence_sha256 = ""
        else:
            version.smoke_test_state = WorkflowTemplateVersion.ValidationState.PASSED
            version.smoke_test_message = "真实受限执行已提交、完成并收集到声明类型输出。"
            version.smoke_test_evidence = {
                "schema_version": 3,
                "execution_submitted": True,
                "smoke_attempt_id": str(smoke_attempt_id),
                "fixture_sha256": str(fixture_sha256),
                "reference_bindings_sha256": json_contract(
                    list(fixture_reference_bindings or []),
                    max_bytes=_stage_snapshot_max_bytes(),
                ).sha256,
                "asset_bindings_sha256": compiled.asset_bindings_sha256,
                "materialized_graph_sha256": compiled.graph_sha256,
                **({"output_contract": compiled.output_contract} if compiled.output_contract.get("schema_version") == 2 else {}),
                "compiled_graph_sha256": submitted_graph_sha256,
                "request_id_sha256": json_contract(
                    request_id,
                    max_bytes=_stage_snapshot_max_bytes(),
                ).sha256,
                "deployment_id": str(deployment.id),
                "graph_sha256": version.graph_sha256,
                "execution_contract_sha256": version.execution_contract_sha256,
                "output_kind": expected_kind,
                "output_count": len(matching),
                "outputs": verified_outputs,
                "contract_schema_sha256": report.schema_sha256,
                "execution_target": _execution_target_evidence(version, current_deployment),
                "execution_target_sha256": expected_target_sha256,
            }
            if pre_submit_resource_release is not None:
                version.smoke_test_evidence["pre_submit_resource_release"] = (
                    pre_submit_resource_release
                )
            version.smoke_test_evidence_sha256 = _evidence_digest(version.smoke_test_evidence)
        if not smoke_superseded and not smoke_worker_superseded and not smoke_cancelled:
            version.save(update_fields=(
                "status", "validation_state", "validation_message", "validated_deployment",
                "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
                "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
                "updated_at",
            ))
    if smoke_worker_superseded:
        raise ProviderDriverError(
            "此次 smoke-test worker claim 已失效，结果未写入版本。",
            code="workflow_smoke_worker_superseded",
        )
    if smoke_cancelled:
        raise ProviderDriverError(
            "取消请求已生效；此次 smoke-test 结果未写入版本。",
            code="workflow_smoke_cancelled_result_discarded",
        )
    if smoke_superseded:
        raise ProviderDriverError(
            "此次 smoke-test 结果已被较新的 live validation 取代。",
            code="workflow_smoke_superseded",
        )
    if smoke_drifted:
        raise ProviderDriverError(
            "workflow 在 smoke-test 期间已变更，请重新校验",
            code="workflow_smoke_contract_drifted",
        )
    result = {
        "schema_version": 3,
        "execution_submitted": True,
        "smoke_attempt_id": str(smoke_attempt_id),
        "fixture_sha256": str(fixture_sha256),
        "reference_bindings_sha256": json_contract(
            list(fixture_reference_bindings or []),
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256,
        "asset_bindings_sha256": compiled.asset_bindings_sha256,
        "materialized_graph_sha256": compiled.graph_sha256,
        **({"output_contract": compiled.output_contract} if compiled.output_contract.get("schema_version") == 2 else {}),
        "compiled_graph_sha256": submitted_graph_sha256,
        "health_version": (
            (health.get("system") or {}).get("comfyui_version")
            if isinstance(health, dict) else ""
        ),
        "contract": report.as_dict(),
    }
    if pre_submit_resource_release is not None:
        result["pre_submit_resource_release"] = pre_submit_resource_release
    return version, result


@transaction.atomic
def _publish_workflow_version_atomic(
    version: WorkflowTemplateVersion,
    *,
    published_by,
) -> tuple[WorkflowTemplateVersion, bool]:
    deployment_id = WorkflowTemplateVersion.objects.values_list(
        "validated_deployment_id",
        flat=True,
    ).get(pk=version.pk)
    deployment = None
    if deployment_id is not None:
        # All workflow lifecycle writers use deployment -> version -> attempt.
        deployment = ModelDeployment.objects.select_for_update(
            of=("self",),
        ).select_related("profile_version__provider").get(pk=deployment_id)
    version = WorkflowTemplateVersion.objects.select_for_update(of=("self",)).select_related(
        "template", "provider", "model_profile_version",
    ).get(pk=version.pk)
    if version.validated_deployment_id != deployment_id:
        raise ValidationError("validated deployment 正在变化，请刷新后重试")
    target_drifted = False
    if deployment is not None and (
        version.validation_evidence.get("execution_target_sha256")
        or version.smoke_test_evidence.get("execution_target_sha256")
    ):
        current = _execution_target_sha256(version, deployment)
        target_drifted = (
            version.validation_evidence.get("execution_target_sha256") != current
            or version.smoke_test_evidence.get("execution_target_sha256") != current
        )
    if target_drifted:
        version.status = WorkflowTemplateVersion.Status.DRAFT
        version.validation_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
        version.validation_message = "validated deployment/provider/model/artifact 配置已漂移"
        version.validated_deployment = None
        version.validation_evidence = {}
        version.validation_evidence_sha256 = ""
        version.smoke_test_state = WorkflowTemplateVersion.ValidationState.NOT_RUN
        version.smoke_test_message = "发布前目标配置漂移，smoke-test 证据已失效。"
        version.smoke_test_evidence = {}
        version.smoke_test_evidence_sha256 = ""
        version.save(update_fields=(
            "status", "validation_state", "validation_message", "validated_deployment",
            "validation_evidence", "validation_evidence_sha256", "smoke_test_state",
            "smoke_test_message", "smoke_test_evidence", "smoke_test_evidence_sha256",
            "updated_at",
        ))
        return version, True
    if version.status != WorkflowTemplateVersion.Status.VALIDATED:
        raise ValidationError("只有 validated 版本可以发布")
    if version.validation_state != WorkflowTemplateVersion.ValidationState.PASSED:
        raise ValidationError("live validate 未通过")
    if version.smoke_test_state != WorkflowTemplateVersion.ValidationState.PASSED:
        raise ValidationError("smoke-test 未通过")
    if not version.validated_deployment_id:
        raise ValidationError("缺少 validated deployment 证据")
    if deployment is None:
        raise ValidationError("缺少 validated deployment 证据")
    if _evidence_digest(version.validation_evidence) != version.validation_evidence_sha256:
        raise ValidationError("validation evidence 校验和不一致")
    if _evidence_digest(version.smoke_test_evidence) != version.smoke_test_evidence_sha256:
        raise ValidationError("smoke evidence 校验和不一致")
    if version.smoke_test_evidence.get("execution_submitted") is not True:
        raise ValidationError("contract-only 检查不能发布 production workflow")
    attempt_id = version.smoke_test_evidence.get("smoke_attempt_id")
    fixture_sha256 = str(version.smoke_test_evidence.get("fixture_sha256") or "")
    compiled_graph_sha256 = str(
        version.smoke_test_evidence.get("compiled_graph_sha256") or "",
    )
    try:
        attempt = WorkflowSmokeAttempt.objects.select_for_update().get(
            pk=attempt_id,
            workflow_version=version,
            deployment_id=version.validated_deployment_id,
            status=WorkflowSmokeAttempt.Status.SUCCEEDED,
        )
    except (WorkflowSmokeAttempt.DoesNotExist, TypeError, ValueError) as exc:
        raise ValidationError("smoke-test 证据未绑定成功的持久 attempt") from exc
    verify_workflow_smoke_attempt_contract(
        attempt,
        version=version,
        deployment=deployment,
    )
    if (
        not fixture_sha256
        or fixture_sha256 != attempt.fixture_sha256
        or len(compiled_graph_sha256) != 64
        or str((attempt.result or {}).get("fixture_sha256") or "") != fixture_sha256
        or str((attempt.result or {}).get("compiled_graph_sha256") or "")
        != compiled_graph_sha256
    ):
        raise ValidationError("smoke-test fixture/compiled graph 证据不完整")
    if attempt.fixture_values_schema_version != FIXTURE_SCHEMA_VERSION:
        raise ValidationError("smoke-test fixture schema 已退役，必须重新执行当前烟测")
    persisted_fixture = attempt.fixture_values if isinstance(attempt.fixture_values, dict) else {}
    materialized_graph_sha256 = str(
        persisted_fixture.get("materialized_graph_sha256") or "",
    )
    reference_bindings = persisted_fixture.get("reference_bindings")
    reference_bindings_sha256 = (
        json_contract(
            reference_bindings,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        if isinstance(reference_bindings, list) else ""
    )
    expected = {
        "reference_bindings_sha256": reference_bindings_sha256,
        "asset_bindings_sha256": str(
            version.smoke_test_evidence.get("asset_bindings_sha256") or "",
        ),
        "materialized_graph_sha256": materialized_graph_sha256,
    }
    if any(
        len(value) != 64
        or str(version.smoke_test_evidence.get(key) or "") != value
        or str((attempt.result or {}).get(key) or "") != value
        for key, value in expected.items()
    ):
        raise ValidationError("smoke-test 动态引用或 materialized graph 证据不完整")
    if version.validation_evidence.get("execution_contract_sha256") != version.execution_contract_sha256:
        raise ValidationError("live validation 证据未绑定当前 execution contract")
    if version.smoke_test_evidence.get("execution_contract_sha256") != version.execution_contract_sha256:
        raise ValidationError("smoke-test 证据未绑定当前 execution contract")
    if (
        deployment.status != ModelDeployment.Status.ACTIVE
        or deployment.profile_version_id != version.model_profile_version_id
        or not deployment.profile_version.provider.is_active
        or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
    ):
        raise ValidationError("validated deployment 已停用或模型绑定发生变化")
    validate_version_configuration_defaults(workflow_version=version)
    current_target_sha256 = _execution_target_sha256(version, deployment)
    if (
        version.validation_evidence.get("execution_target_sha256") != current_target_sha256
        or version.smoke_test_evidence.get("execution_target_sha256") != current_target_sha256
    ):
        raise ValidationError("validated deployment/provider/model/artifact 配置已漂移")
    verify_workflow_version_integrity(version)
    WorkflowTemplateVersion.objects.filter(
        template=version.template,
        is_current=True,
    ).exclude(pk=version.pk).update(is_current=False, updated_at=timezone.now())
    version.status = WorkflowTemplateVersion.Status.PUBLISHED
    version.is_current = True
    version.published_at = timezone.now()
    version.published_by = published_by
    version.save(update_fields=(
        "status", "is_current", "published_at", "published_by", "updated_at",
    ))
    return version, False


def publish_workflow_version(
    version: WorkflowTemplateVersion,
    *,
    published_by,
) -> WorkflowTemplateVersion:
    published, target_drifted = _publish_workflow_version_atomic(
        version,
        published_by=published_by,
    )
    if target_drifted:
        raise ValidationError("validated deployment/provider/model/artifact 配置已漂移")
    return published


def node_metadata(
    *,
    deployment: ModelDeployment,
    search: str = "",
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValidationError({"offset": "offset 必须是非负整数"})
    if not isinstance(search, str):
        raise ValidationError({"search": "search 必须是字符串"})
    maximum_page_size = boundary_int("security.api.max_page_size")
    maximum_search_characters = boundary_int("security.api.max_search_chars")
    configuration = resolve_configuration(required_keys=(
        "workflow.node_metadata_default_page_size",
        "workflow.node_metadata_input_preview_limit",
    ))
    default_page_size = configuration.values["workflow.node_metadata_default_page_size"]
    input_preview_limit = configuration.values["workflow.node_metadata_input_preview_limit"]
    if (
        type(default_page_size) is not int
        or not 1 <= default_page_size <= maximum_page_size
    ):
        raise ValidationError({
            "workflow.node_metadata_default_page_size": (
                "必须是不超过 security.api.max_page_size 的正整数"
            ),
        })
    if type(input_preview_limit) is not int or input_preview_limit < 1:
        raise ValidationError({
            "workflow.node_metadata_input_preview_limit": "必须是正整数",
        })
    if len(search) > maximum_search_characters:
        raise ValidationError({
            "search": f"search 不能超过安全上限 {maximum_search_characters} 个字符",
        })
    if limit is None:
        limit = default_page_size
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= maximum_page_size
    ):
        raise ValidationError({
            "limit": f"limit 必须是 1 到 {maximum_page_size} 的整数",
        })
    provider = deployment.profile_version.provider
    driver = driver_for(provider)
    raw = driver.invoke(deployment, "object_info", {})
    if not isinstance(raw, dict):
        raise ValidationError("object_info 响应无效")
    query = search.strip().casefold()
    items = []
    for class_type in sorted(raw):
        definition = raw[class_type]
        if query and query not in class_type.casefold():
            continue
        if not isinstance(definition, dict):
            continue
        inputs = definition.get("input") if isinstance(definition.get("input"), dict) else {}
        required = inputs.get("required") if isinstance(inputs.get("required"), dict) else {}
        optional = inputs.get("optional") if isinstance(inputs.get("optional"), dict) else {}
        items.append({
            "class_type": class_type,
            "display_name": str(definition.get("display_name") or class_type),
            "category": str(definition.get("category") or ""),
            "required_inputs": sorted(required)[:input_preview_limit],
            "optional_inputs": sorted(optional)[:input_preview_limit],
            "output_count": len(definition.get("output") or []),
        })
    total_count = len(items)
    page = items[offset:offset + limit]
    consumed = offset + len(page)
    return {
        "schema_version": 2,
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "next_offset": consumed if consumed < total_count else None,
        "catalog_sha256": hashlib.sha256(canonical_json_bytes(items)).hexdigest(),
        "items": page,
    }
