"""Asset mutation guards read the same frozen logical inputs as execution.

This is not another input ledger or a recursive search through arbitrary prompt
JSON. Only declared media inputs can reference Assets. Caller holds the existing
registry reference guard, also used by Preparation allocation, before Asset locks.
"""
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Exists, OuterRef, Q

from studio.models import ExecutionPreparation, GenerationAttempt, GenerationTask
from studio.services.execution_definitions import verify_execution_definition_payload
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


def active_asset_preparations():
    """Include waiting review and unresolved remote work, not just queue rows."""
    terminal = ("succeeded", "failed", "cancelled")
    attempts = GenerationAttempt.objects.filter(preparation_id=OuterRef("pk"))
    pending_attempts = attempts.exclude(status__in=terminal)
    pending_tasks = attempts.filter(generation_task__status__in=("queued", "running", "cancel_requested"))
    uncertain_tasks = attempts.exclude(generation_task__isnull=True).exclude(
        generation_task__submission_token="",
    ).exclude(generation_task__submission_state__in=(
        GenerationTask.SubmissionState.TERMINAL_SUCCEEDED,
        GenerationTask.SubmissionState.TERMINAL_FAILED,
        GenerationTask.SubmissionState.TERMINAL_CANCELLED,
    )).exclude(generation_task__status="succeeded").exclude(generation_task__remote_state="succeeded")
    asset_root = Q(asset_job__isnull=False) & ~Q(asset_job__status__in=terminal)
    deployment_root = Q(deployment_smoke__isnull=False) & ~Q(deployment_smoke__status__in=terminal)
    workflow_root = Q(workflow_smoke__isnull=False) & ~Q(workflow_smoke__status__in=terminal)
    production_root = (Q(stage_run__isnull=False) & ~Q(stage_run__production_run__status__in=terminal)
        & ~Q(status="cancelled") & ~Q(stage_run__status__in=terminal))
    return ExecutionPreparation.objects.filter(asset_root | deployment_root | workflow_root | production_root
        | Exists(pending_attempts) | Exists(pending_tasks) | Exists(uncertain_tasks))


def _read_contract_field(detail, name):
    return verify_json_contract(getattr(detail, name), schema_version=getattr(detail, f"{name}_schema_version"),
        size_bytes=getattr(detail, f"{name}_size_bytes"), sha256=getattr(detail, f"{name}_sha256"),
        max_bytes=(execution_resolved_inputs_max_bytes()
            if name in {"intent", "resolved_inputs"} else execution_configuration_max_bytes())).value


@security_boundary_resolution_cache()
def asset_has_active_execution_reference(asset):
    query = active_asset_preparations()
    if asset.owner_id is not None:
        query = query.filter(Q(asset_job__owner_id=asset.owner_id)
            | Q(stage_run__production_run__owner_id=asset.owner_id)
            | Q(deployment_smoke__isnull=False, created_by_id=asset.owner_id)
            | Q(workflow_smoke__isnull=False, created_by_id=asset.owner_id))
    # Load immutable source facts together, not whole task snapshots or one
    # related query per candidate. Never load compiled prompts/evaluations here.
    contract_fields = [f"contract__{name}{suffix}" for name in ("intent", "resolved_inputs")
        for suffix in ("", "_schema_version", "_size_bytes", "_sha256")]
    query = query.select_related("contract", "execution_instance_version__contract").only(
        "id", "intent_sha256", "execution_instance_version_id", "contract__preparation_id", *contract_fields,
        "execution_instance_version__id", "execution_instance_version__contract__version_id",
        "execution_instance_version__contract__definition", "execution_instance_version__contract__schema_version",
        "execution_instance_version__contract__size_bytes", "execution_instance_version__contract__sha256",
        "execution_instance_version__contract__target_fingerprint",
    ).order_by("id")
    definitions = {}
    for preparation in query.iterator():
        try:
            detail = preparation.contract
            intent, resolved = _read_contract_field(detail, "intent"), _read_contract_field(detail, "resolved_inputs")
            if (detail.intent_sha256 != preparation.intent_sha256 or not isinstance(intent, dict)
                    or intent.get("resolved_inputs_sha256") != detail.resolved_inputs_sha256
                    or not isinstance(intent.get("request"), dict) or not isinstance(resolved, dict)):
                raise ValidationError("执行准备的来源摘要不一致")
            key = preparation.execution_instance_version_id
            if key is None:
                raise ValidationError("执行准备尚未接入当前实例来源合同")
            if key not in definitions:
                contract = preparation.execution_instance_version.contract
                definition = verify_execution_definition_payload(JsonContract(contract.definition, contract.schema_version,
                    contract.size_bytes, contract.sha256))
                if (definition.value["version_id"] != str(key)
                        or definition.value["target_fingerprint"] != contract.target_fingerprint):
                    raise ValidationError("执行定义的来源身份不一致")
                definitions[key] = definition
            definition = definitions[key]
            if intent["request"].get("definition_sha256") != definition.sha256:
                raise ValidationError("执行准备未引用对应冻结定义")
            inputs = resolved.get("inputs")
            if not isinstance(inputs, dict):
                raise ValidationError("执行准备缺少已解析输入")
            uses = read_execution_workflow_uses(definition, json_contract(inputs, max_bytes=execution_resolved_inputs_max_bytes()))
            if any(row.get("asset_id") == str(asset.pk) for row in uses.items.values()):
                return True
        except (ObjectDoesNotExist, ValidationError, KeyError, TypeError, ValueError) as exc:
            # Never convert broken frozen evidence to an empty reference list.
            raise ValidationError("有未结束执行的素材来源无法核对，请先检查执行准备后再修改类别或删除素材。",
                code="execution_asset_reference_invalid") from exc
    return False
