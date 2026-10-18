"""Frozen media request reader for the original production attempt queue.

No current route, PromptArtifact, field selection or random value is rebuilt.
The creator freezes runtime limits with input facts; the worker verifies the
same peer identity before any submit, poll, cancellation or collection.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.models import ProviderProfile
from studio.services.configuration import resolve_configuration_from_snapshot
from studio.services.execution_deadlines import validate_deadline_seconds
from studio.services.execution_definitions import (
    execution_definition_version, stored_execution_contract, verify_current_execution_definition,
)
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_task_links import validate_execution_task_links
from studio.services.json_contracts import verify_json_contract
from studio.services.provider_registry import restricted_async_rest_runtime_budget
from studio.services.security_boundaries import boundary_int


def freeze_production_media_runtime(*, run):
    base = verify_json_contract(run.config_snapshot, schema_version=run.config_snapshot_schema_version,
        size_bytes=run.config_snapshot_size_bytes, sha256=run.config_snapshot_sha256,
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    resolved = resolve_configuration_from_snapshot(base.value, required_keys=(
        "runtime.task_idle_timeout_seconds", "production.task_timeout_seconds"))
    idle, hard = validate_deadline_seconds(idle_seconds=resolved.values["runtime.task_idle_timeout_seconds"],
        hard_seconds=resolved.values["production.task_timeout_seconds"])
    return {"schema_version": 1, "idle_timeout_seconds": idle, "hard_timeout_seconds": hard,
        "configuration_sha256": base.sha256, "revision_ids": list(resolved.revision_ids)}


def read_production_media_deadlines(detail):
    deadlines = detail.resolved_inputs.get("execution_deadlines")
    if (not isinstance(deadlines, dict) or set(deadlines) != {"schema_version", "idle_timeout_seconds", "hard_timeout_seconds",
            "configuration_sha256", "revision_ids"} or type(deadlines["schema_version"]) is not int or deadlines["schema_version"] != 1
            or not isinstance(deadlines["configuration_sha256"], str) or len(deadlines["configuration_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in deadlines["configuration_sha256"])
            or not isinstance(deadlines["revision_ids"], list) or any(not isinstance(key, str) or not key for key in deadlines["revision_ids"])
            or any(isinstance(deadlines[key], bool) for key in ("idle_timeout_seconds", "hard_timeout_seconds"))):
        raise ValidationError("媒体准备缺少冻结的任务时限，不能用当前默认值补齐")
    validate_deadline_seconds(idle_seconds=deadlines["idle_timeout_seconds"], hard_seconds=deadlines["hard_timeout_seconds"])
    return deepcopy(deadlines)


def read_prepared_production_media(attempt):
    validate_execution_task_links(attempt)
    if attempt.role != "primary" or attempt.stage_run.stage_key not in {"media_generation", "bible_image_generation"}:
        raise ValidationError("媒体主调用必须归属实际生成阶段，不是字段辅助调用")
    preparation = attempt.preparation
    detail, definition, transport = read_frozen_execution_request(preparation)
    from studio.services.execution_request_storage import request_document
    snapshot = request_document(attempt.request_snapshot, schema_version=attempt.request_snapshot_schema_version)
    if snapshot.sha256 != preparation.request_sha256 or attempt.request_sha256 != snapshot.sha256:
        raise ValidationError("原媒体任务与本次冻结准备请求不一致")
    value = transport.value
    output_contract, output_prefix, monitoring = None, None, None
    if value["driver_type"] == ProviderProfile.DriverType.COMFYUI:
        # The common frozen-request reader has verified the compiled graph,
        # semantic/file/use hashes and original output contract already.
        compiled = value["payload"]["compiled_workflow"]
        output_contract = deepcopy(compiled["output_contract"])
        output_kind = output_contract["primary"]["kind"]
        from studio.services.execution_workflow_outputs import frozen_comfy_output_prefix
        output_prefix = frozen_comfy_output_prefix(compiled, preparation_id=preparation.pk)
        monitoring = deepcopy(definition.value["interface"]["execution_monitoring"])
    elif value["driver_type"] == ProviderProfile.DriverType.ASYNC_REST and definition.value["target"]["workflow"] is None:
        output_kind = value["output"].get("kind")
    else:
        raise ValidationError("媒体准备只能交给其声明的工作流或受限异步媒体执行器")
    if value["field_request_sha256"] is not None or output_kind not in {"image", "video", "audio"}:
        raise ValidationError("媒体输出合同或主辅调用身份无效")
    deadlines = read_production_media_deadlines(detail)
    from studio.services.production_execution_units import verify_production_unit
    unit = verify_production_unit(attempt.stage_run.production_run, detail.resolved_inputs.get("production_unit"))
    from studio.services.production_stage_catalog import STAGE_MEDIA_OUTPUT_KINDS, STAGE_MEDIA_SCOPE_KINDS
    if (unit["scope"]["kind"] not in STAGE_MEDIA_SCOPE_KINDS[attempt.stage_run.stage_key]
            or output_kind not in STAGE_MEDIA_OUTPUT_KINDS[attempt.stage_run.stage_key]):
        raise ValidationError("媒体目标范围或输出类型与实际生产步骤不一致")
    target = {}
    if attempt.stage_run.stage_key == "bible_image_generation":
        from studio.services.media_generation_targets import read_media_generation_target
        appearance = (read_media_generation_target(preparation, detail=detail).value.get("appearance")
            if attempt.stage_run.production_run.plan_source == "recipe" else None)
        if appearance and appearance["role"] == "variant":
            from studio.services.production_recipe_material_inputs import read_recipe_material_binding
            read_recipe_material_binding(preparation, detail=detail)
            if "bible_target" in detail.resolved_inputs:
                raise ValidationError("变体不能携带实体主图写回基线")
        else:
            from studio.services.production_bible_targets import verify_bible_target
            target["bible_target"] = deepcopy(verify_bible_target(detail.resolved_inputs.get("bible_target"), unit))
    elif "bible_target" in detail.resolved_inputs:
        raise ValidationError("普通媒体调用不能携带实体参考图写回基线")
    return {"payload": deepcopy(value["payload"]), "operation": value["operation"],
        "driver_type": value["driver_type"], "output_kind": output_kind, "output_contract": output_contract,
        "output_prefix": output_prefix, "execution_monitoring": monitoring, "execution_deadlines": deepcopy(deadlines),
        "production_unit": unit, **target,
        "preparation_id": str(preparation.pk), "definition_sha256": definition.sha256,
        "resolved_inputs_sha256": detail.resolved_inputs_sha256, "parameters": deepcopy(value["parameters"]["values"])}


def production_media_peer(attempt):
    """Same target or explicit failure; inactive instances may reconcile, not resubmit."""
    snapshot = read_prepared_production_media(attempt)
    if snapshot["driver_type"] != ProviderProfile.DriverType.ASYNC_REST:
        raise ValidationError("异步媒体队列不能接管 ComfyUI 原任务的提交或回收")
    current = verify_current_execution_definition(stored_execution_contract(attempt.execution_instance_version_id))
    if current.sha256 != snapshot["definition_sha256"]:
        raise ValidationError("媒体执行目标已变化，不能用新连接处理原远端请求")
    version = execution_definition_version(attempt.execution_instance_version_id)
    config = {**version.deployment.provider.driver_config, **version.deployment.runtime_config}
    restricted_async_rest_runtime_budget(config)
    return version.deployment


def production_submission_payload(attempt, snapshot):
    """Stamp only delivery identity; semantic payload stays Preparation-owned."""
    payload = deepcopy(snapshot["payload"])
    if payload.get("idempotency_key") != str(attempt.preparation_id):
        raise ValidationError("异步媒体请求的准备幂等身份不一致")
    # Stable within one attempt (including recovery), distinct for an explicit
    # new attempt. The durable Attempt ID is not a new business parameter.
    payload["idempotency_key"] = f"production-attempt:{attempt.pk}"
    return payload
