"""Read exact parent-issued field batches; never accept a client-made batch."""
from django.core.exceptions import ValidationError

from studio.services.execution_definitions import stored_execution_contract, verify_stored_execution_definition
from studio.services.execution_field_contracts import verify_execution_field_request
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.execution_prompts import AUXILIARY_OPERATIONS
from studio.services.json_contracts import JsonContract


def read_preparation_field_batch(parent, batch_sha256):
    detail = read_execution_preparation_detail(parent)
    batches = detail.resolved_inputs.get("field_requests")
    if not isinstance(batches, list):
        raise ValidationError("父准备缺少明确的字段批次")
    matching = [row for row in batches if isinstance(row, dict) and row.get("sha256") == batch_sha256]
    if len(matching) != 1 or set(matching[0]) != {"value", "schema_version", "size_bytes", "sha256"}:
        raise ValidationError("字段批次必须来自父准备的精确冻结请求")
    batch = JsonContract(**matching[0])
    request = verify_execution_field_request(batch)
    definition = verify_stored_execution_definition(stored_execution_contract(parent.execution_instance_version_id))
    if request["profile"]["version_id"] != definition.value["configuration"]["prompt_profile_version_id"]:
        raise ValidationError("字段批次与父准备的精确策略不一致")
    purpose = next((key for key, operation in AUXILIARY_OPERATIONS.items() if operation == request["operation"]), None)
    binding = definition.value["interface"]["auxiliaries"].get(purpose)
    if binding is None:
        raise ValidationError("父实例未明确绑定该字段批次需要的辅助叶子")
    return batch, purpose, binding


def read_asset_field_batch(job):
    from studio.services.execution_task_links import validate_execution_task_links

    validate_execution_task_links(job)
    if not job.parent_preparation_id or job.job_type != "field":
        raise ValidationError("只有真实字段子任务可以读取父准备批次")
    batch, purpose, binding = read_preparation_field_batch(job.parent_preparation, job.field_batch_key)
    if purpose != job.auxiliary_purpose or binding["version_id"] != str(job.execution_instance_version_id):
        raise ValidationError("字段子任务与父实例冻结的用途、叶子不一致")
    leaf = verify_stored_execution_definition(stored_execution_contract(job.execution_instance_version_id))
    if binding["definition_sha256"] != leaf.sha256 or binding["target_fingerprint"] != leaf.value["target_fingerprint"]:
        raise ValidationError("字段子任务不属于父实例冻结的叶子定义")
    return batch


def production_field_unit(parent_id, batch_key):
    return f"field:{parent_id}:{batch_key}"


def read_production_field_context(preparation, detail=None):
    """Verify the trusted producer's frozen lineage before an Attempt exists.

    The indexed Stage/unit/request identity locates the child; this immutable
    intent is evidence, not a second task relation or a client-authored rule.
    Dispatch materializes the verified parent FK on the actual Attempt.
    """
    detail = detail or read_execution_preparation_detail(preparation)
    identity = detail.intent["request"]["requested"].get("production_field")
    if identity is None:
        return None
    return validate_production_field_identity(stage_run_id=preparation.stage_run_id,
        version_id=preparation.execution_instance_version_id, unit_key=preparation.unit_key,
        request_key=preparation.request_key, identity=identity, preparation_id=preparation.pk)


def validate_production_field_identity(*, stage_run_id, version_id, unit_key, request_key, identity, preparation_id=None):
    """One lineage check for both allocation and later reads of the child."""
    from studio.models import ExecutionPreparation
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES

    if (not stage_run_id or not isinstance(identity, dict)
            or set(identity) != {"parent_preparation_id", "parent_intent_sha256", "field_batch_key", "purpose", "attempt_number", "leaf"}
            or type(identity["attempt_number"]) is not int or identity["attempt_number"] < 1):
        raise ValidationError("生产字段准备的来源标识不完整")
    parent = ExecutionPreparation.objects.get(pk=identity["parent_preparation_id"])
    if (parent.pk == preparation_id or parent.stage_run_id != stage_run_id
            or parent.intent_sha256 != identity["parent_intent_sha256"]
            or parent.execution_instance_version.operation in FIELD_OPERATION_MODES
            or "production_field" in read_execution_preparation_detail(parent).intent["request"]["requested"]):
        raise ValidationError("辅助准备必须指向同一阶段的真实主准备，不能自指或递归")
    batch, purpose, binding = read_preparation_field_batch(parent, identity["field_batch_key"])
    leaf = verify_stored_execution_definition(stored_execution_contract(version_id))
    if (purpose != identity["purpose"] or binding != identity["leaf"]
            or binding["version_id"] != str(version_id)
            or binding["definition_sha256"] != leaf.sha256 or binding["target_fingerprint"] != leaf.value["target_fingerprint"]
            or leaf.value["configuration"]["prompt_mode"] != "none" or leaf.value["interface"]["auxiliaries"]
            or unit_key != production_field_unit(parent.pk, batch.sha256)
            or request_key != f"attempt:{identity['attempt_number']}"):
        raise ValidationError("辅助准备与主准备冻结的用途、批次或叶子实例不一致")
    return parent, batch, identity


def read_attempt_field_batch(attempt):
    from studio.services.execution_task_links import validate_execution_task_links
    validate_execution_task_links(attempt)
    if attempt.role != "auxiliary":
        raise ValidationError("只有真实生产辅助任务可以读取字段批次")
    return read_production_field_context(attempt.preparation)[1]


def read_preparation_operation_field_request(preparation, definition, detail):
    """Resolve a field child or explicit administrator Smoke fixture.

    Normal roots cannot author internal field rules. Smoke fixture rules must
    exactly match their actual published Profile contract, never caller JSON.
    """
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES

    operation = definition.value["configuration"]["operation"]
    if operation not in FIELD_OPERATION_MODES:
        return None
    if preparation.asset_job_id and preparation.asset_job.parent_preparation_id:
        batch = read_asset_field_batch(preparation.asset_job)
    elif preparation.stage_run_id:
        context = read_production_field_context(preparation, detail)
        if context is None:
            raise ValidationError("生产字段操作必须来自明确的主准备批次")
        batch = context[1]
    elif preparation.purpose in {"instance_smoke", "resource_smoke"} and (preparation.deployment_smoke_id or preparation.workflow_smoke_id):
        from studio.services.execution_task_links import validate_smoke_preparation_link
        from studio.services.execution_field_smoke import verify_instance_field_fixture

        attempt = preparation.deployment_smoke if preparation.deployment_smoke_id else preparation.workflow_smoke
        validate_smoke_preparation_link(attempt, preparation.execution_instance_version_id)
        batch = verify_instance_field_fixture(definition, detail.resolved_inputs["inputs"], allow_retired=True)
    else:
        raise ValidationError("字段操作只能来自真实辅助子任务或明确的管理验收夹具")
    request = verify_execution_field_request(batch)
    if request["operation"] != operation or detail.resolved_inputs["inputs"].get("operation_inputs") != {"field_request": batch.value}:
        raise ValidationError("字段叶子输入必须是本次精确批次，不能替换正文或执行能力")
    return batch
