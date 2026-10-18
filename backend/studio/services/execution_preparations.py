"""Transactional preparation allocation, separate from dispatch and admission.

The existing root-job service must call this in its creation transaction. Its
internal resolver reads authorized sources, initializes parameters and returns
frozen facts; it must not invoke a provider or enqueue children. This is not a
public accept-arbitrary-snapshots API and is not wired into old consumers.
"""
from dataclasses import dataclass
from copy import deepcopy
import re

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.utils import timezone

from studio.models import ExecutionPreparation, ExecutionPreparationContract
from studio.services.execution_definitions import (
    execution_definition_version, stored_execution_contract, verify_current_execution_definition,
    verify_stored_execution_definition,
)
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_preparation_owners import locked_preparation_owners, PREPARATION_OWNER_MODELS
from studio.services.json_contracts import JsonContract, assign_json_contract, json_contract, verify_json_contract
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import security_boundary_resolution_cache


@dataclass(frozen=True)
class ResolvedExecutionPreparation:
    resolved_inputs: JsonContract
    review_contract: JsonContract


def _conflict(message):
    raise ValidationError(message, code="execution_preparation_conflict")


def _verify_object(contract, label):
    if not isinstance(contract, JsonContract) or contract.schema_version != 1 or not isinstance(contract.value, dict):
        raise ValidationError({label: "需要已核对的当前对象合同与摘要"})
    return verify_json_contract(contract.value, schema_version=contract.schema_version, size_bytes=contract.size_bytes,
        sha256=contract.sha256, max_bytes=(execution_request_max_bytes()
            if label in {"intent", "resolved_inputs"} else execution_configuration_max_bytes()))


def read_execution_preparation_detail(preparation, *, detail=None):
    """Verify immutable detail after owner authorization; accept a batched row."""
    try:
        if detail is None:
            detail = ExecutionPreparationContract.objects.get(preparation=preparation)
    except ObjectDoesNotExist as exc:
        raise ValidationError("准备缺少已冻结详情，不能重新生成来源或种子") from exc
    if detail.preparation_id != preparation.pk:
        _conflict("准备详情不属于当前准备")
    detail.clean()
    if detail.intent_sha256 != preparation.intent_sha256:
        _conflict("准备意图索引与详情不一致")
    if (set(detail.intent) != {"schema_version", "request", "resolved_inputs_sha256", "review_contract_sha256"}
            or type(detail.intent["schema_version"]) is not int or detail.intent["schema_version"] != 1
            or not isinstance(detail.intent["request"], dict)
            or detail.intent["resolved_inputs_sha256"] != detail.resolved_inputs_sha256
            or detail.intent["review_contract_sha256"] != detail.review_contract_sha256):
        _conflict("准备意图没有覆盖完整来源、实际种子及审核合同")
    if detail.prepared_request_sha256 != preparation.request_sha256:
        _conflict("最终请求索引与详情不一致")
    if (detail.prepared_request_key is not None) != (preparation.request_frozen_at is not None):
        _conflict("最终请求的冻结时间与正文不一致")
    if (detail.evaluation is not None) != (preparation.evaluation_revision is not None):
        _conflict("准备评估的详情与索引不一致")
    if detail.evaluation is not None and (
            detail.evaluation.get("preparation_revision") != preparation.evaluation_revision
            or preparation.evaluated_at is None
            or detail.evaluation.get("evaluated_at") != preparation.evaluated_at.isoformat()):
        _conflict("准备评估的修订或时间不一致")
    return detail


@security_boundary_resolution_cache()
def read_frozen_execution_request(preparation):
    """The shared saved-request reader for assets, production and Smoke."""
    detail = read_execution_preparation_detail(preparation)
    frozen = detail.prepared_request
    if frozen is None:
        raise ValidationError("请求尚未完成准备和准入，不能调用模型", code="execution_request_not_frozen")
    if (not isinstance(frozen, dict) or set(frozen) != {"schema_version", "preparation_id", "basis_preparation_revision", "intent_sha256", "field_selections", "transport"}
            or type(frozen["schema_version"]) is not int or frozen["schema_version"] != 1
            or frozen["preparation_id"] != str(preparation.pk) or frozen["intent_sha256"] != preparation.intent_sha256
            or type(frozen["basis_preparation_revision"]) is not int or not 1 <= frozen["basis_preparation_revision"] < preparation.revision
            or not isinstance(frozen["field_selections"], dict)):
        raise ValidationError("冻结请求的准备身份或来源修订不一致")
    definition = preparation_execution_definition(preparation, detail=detail)
    if detail.intent["request"].get("definition_sha256") != definition.sha256:
        raise ValidationError("准备与执行定义不一致")
    transport = json_contract(frozen["transport"], max_bytes=execution_request_max_bytes())
    value = transport.value
    keys = {"schema_version", "execution_instance_version_id", "definition_sha256", "target_fingerprint", "operation",
        "driver_type", "idempotency_key", "parameters", "prompt_channels", "profile_compilation", "field_request_sha256", "payload", "output"}
    from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
    from studio.services.execution_output_contracts import STRUCTURED_TEXT_OPERATION
    if (definition.value["configuration"]["operation"] in (*NATIVE_TEXT_OPERATIONS, STRUCTURED_TEXT_OPERATION)
            and definition.value["target"]["workflow"] is None):
        keys.add("operation_plan")
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
    from studio.services.chat_body import CHAT_DRIVERS, validate_chat_body_layout
    # Historical envelopes stay immutable; missing assembly evidence is not rebuilt.
    if isinstance(value, dict) and value.get("driver_type") in CHAT_DRIVERS and "chat_body_layout" in value:
        keys.add("chat_body_layout")
        validate_chat_body_layout(value["chat_body_layout"], value.get("payload"))
    # Optional original assembly evidence: old requests remain immutable and
    # explicitly lack provenance. Other protocols cannot accept this metadata.
    if (definition.value["configuration"]["operation"] in (*FIELD_OPERATION_MODES, STRUCTURED_TEXT_OPERATION)
            and definition.value["target"]["workflow"] is None
            and "system_message_layout" in value):
        keys.add("system_message_layout")
    if (not isinstance(value, dict) or set(value) != keys or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["execution_instance_version_id"] != (str(preparation.execution_instance_version_id) if preparation.execution_instance_version_id else None)
            or value["definition_sha256"] != definition.sha256
            or value["target_fingerprint"] != definition.value["target_fingerprint"]
            or value["operation"] != definition.value["configuration"]["operation"]
            or value["driver_type"] != definition.value["target"]["driver_type"]
            or value["idempotency_key"] != str(preparation.pk)
            or value["parameters"] != detail.resolved_inputs.get("parameters")
            or value["output"] != definition.value["interface"]["output"] or not isinstance(value["payload"], dict)):
        raise ValidationError("冻结提交请求与执行定义或原准备事实不一致")
    if value["operation"] == STRUCTURED_TEXT_OPERATION:
        from studio.services.execution_structured_text import verify_structured_text_plan
        verify_structured_text_plan(value["operation_plan"], expected_output=definition.value["interface"]["business_output"])
        from studio.services.chat_output_options import project_chat_output_options
        interface = definition.value["interface"]
        if value["payload"].get("chat_request_options") != project_chat_output_options(
                interface["chat_request_options"], interface["business_output"]):
            raise ValidationError("结构化文本请求的响应格式与冻结业务输出不一致")
    if value["operation"] in {"script.generate", "script.structure"} and "business_output" in definition.value["interface"]:
        from studio.services.script_semantic_requests import verify_script_semantic_execution
        verify_script_semantic_execution(definition.value, detail.resolved_inputs, value,
            max_bytes=execution_request_max_bytes())
    if value["driver_type"] == "comfyui":
        from studio.services.execution_workflow_requests import verify_execution_workflow_transport
        verify_execution_workflow_transport(definition, transport)
    return detail, definition, transport


@security_boundary_resolution_cache()
def read_production_attempt_request(attempt):
    """Read the original production request, never rebuild it from live options.

    This is identity verification, not submission or candidate acceptance.
    The caller still owns visibility, claim/state checks and its root locks.
    """
    from studio.services.execution_request_storage import request_document
    from studio.services.execution_task_links import validate_execution_task_links

    preparation = ExecutionPreparation.objects.filter(pk=attempt.preparation_id,
        stage_run_id=attempt.stage_run_id, purpose="production").first()
    if preparation is None:
        raise ValidationError("原调用缺少同一阶段的生产准备")
    detail, definition, transport = read_frozen_execution_request(preparation)
    snapshot = request_document(attempt.request_snapshot, schema_version=attempt.request_snapshot_schema_version)
    if (snapshot.sha256 != preparation.request_sha256 or attempt.request_sha256 != preparation.request_sha256
            or attempt.execution_instance_version_id != preparation.execution_instance_version_id
            or attempt.operation != transport.value["operation"]
            or attempt.request_snapshot_key != detail.prepared_request_key):
        raise ValidationError("原任务与冻结生产请求不一致")
    validate_execution_task_links(attempt)
    return detail, definition, transport


def preparation_execution_definition(preparation, *, detail=None, current=False):
    """One definition reader for real instance and resource-only root identities."""
    if preparation.execution_instance_version_id is not None:
        if preparation.purpose not in {"production", "instance_smoke"}:
            raise ValidationError("资源验收不能携带执行实例身份")
        contract = stored_execution_contract(preparation.execution_instance_version_id)
        definition = verify_current_execution_definition(contract) if current else verify_stored_execution_definition(contract)
    else:
        if preparation.purpose != "resource_smoke" or preparation.asset_job_id or preparation.stage_run_id:
            raise ValidationError("缺少执行实例的准备不能用于生产或组合验收")
        from studio.services.execution_preparation_owners import nonproduction_preparation_root
        from studio.services.execution_resource_smoke_definition import read_resource_smoke_definition
        definition = read_resource_smoke_definition(nonproduction_preparation_root(preparation), current=current)
    detail = read_execution_preparation_detail(preparation) if detail is None else detail
    if detail.intent["request"].get("definition_sha256") != definition.sha256:
        raise ValidationError("准备意图与精确冻结定义不一致")
    return definition


def _persist_preparation(*, lookup, version, purpose, unit_key, request_key, request, resolved, review, actor, supersedes=None):
    frozen_intent = json_contract({"schema_version": 1, "request": request.value,
        "resolved_inputs_sha256": resolved.sha256, "review_contract_sha256": review.sha256},
        max_bytes=execution_request_max_bytes())
    preparation = ExecutionPreparation(**lookup, execution_instance_version=version, purpose=purpose, supersedes=supersedes,
        **({"unit_key": unit_key, "request_key": request_key} if "stage_run_id" not in lookup else {}),
        status="preparing", created_by=actor, intent_sha256=frozen_intent.sha256, next_check_at=timezone.now())
    preparation.full_clean()
    preparation.save()
    detail = ExecutionPreparationContract(preparation=preparation)
    for name, value in (("intent", frozen_intent), ("resolved_inputs", resolved), ("review_contract", review)):
        assign_json_contract(detail, name, deepcopy(value.value), schema_version=value.schema_version,
            max_bytes=(execution_request_max_bytes() if name in {"intent", "resolved_inputs"} else execution_configuration_max_bytes()))
    detail.full_clean()
    detail.save()
    return preparation


def _verify_resolved(facts, definition, *, request):
    if not isinstance(facts, ResolvedExecutionPreparation):
        raise ValidationError("来源适配器必须返回核对过的准备事实")
    resolved = _verify_object(facts.resolved_inputs, "resolved_inputs")
    review = _verify_object(facts.review_contract, "review_contract")
    if type(resolved.value.get("schema_version")) is not int or resolved.value["schema_version"] != 1:
        raise ValidationError("解析事实缺少当前版本")
    requests = resolved.value.get("field_requests")
    if "media_generation_target" in resolved.value:
        from studio.services.media_generation_targets import verify_media_generation_target
        verify_media_generation_target(definition, resolved.value, request=request)
    if not isinstance(requests, list):
        raise ValidationError("必须明确记录字段请求集合，不能恢复时再生成")
    from studio.services.execution_field_contracts import verify_execution_field_request

    destinations = set()
    for envelope in requests:
        if not isinstance(envelope, dict) or set(envelope) != {"value", "schema_version", "size_bytes", "sha256"}:
            raise ValidationError("字段请求缺少完整冻结摘要")
        request = verify_execution_field_request(JsonContract(**envelope))
        if request["profile"]["version_id"] != definition.value["configuration"]["prompt_profile_version_id"]:
            raise ValidationError("字段请求不属于本次精确主策略")
        for item in request["items"]:
            identity = (item["unit_key"], item["field_contract"]["field_key"])
            if identity in destinations:
                raise ValidationError("准备中存在重复单元字段请求")
            destinations.add(identity)
    from studio.services.execution_field_review import verify_field_review_contract
    verify_field_review_contract(review.value)
    return resolved, review


@registry_reference_writer
@security_boundary_resolution_cache()
def create_execution_instance_preparation(*, owner_ref, version_id, expected_definition_sha256,
        unit_key, request_key, intent, resolve_facts, actor, supersedes=None):
    """Create once per root/unit/request; replay never re-resolves any input.

    ``intent`` is the root action's validated input, before generated seeds.
    ``resolve_facts`` is a trusted internal adapter, not a request parameter.
    It runs only after the root lock and exact published-definition checks.
    No state here means the main call is admitted or ready for remote dispatch.
    """
    if not isinstance(owner_ref, tuple) or len(owner_ref) != 2 or owner_ref[0] not in PREPARATION_OWNER_MODELS:
        raise ValidationError("必须明确指定一个正式任务来源")
    for key, value in (("unit_key", unit_key), ("request_key", request_key)):
        if not isinstance(value, str) or not value.strip() or len(value) > 191:
            raise ValidationError({key: "需要非空且不超过191字符的标识"})
    if not isinstance(expected_definition_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_definition_sha256):
        raise ValidationError({"expected_definition_sha256": "需要明确的实例冻结定义摘要"})
    requested = _verify_object(intent, "intent")
    request = json_contract({"schema_version": 1, "version_id": str(version_id),
        "definition_sha256": expected_definition_sha256, "unit_key": unit_key, "request_key": request_key,
        "requested": deepcopy(requested.value)}, max_bytes=execution_request_max_bytes())
    key, owner_id = owner_ref
    purpose = "production" if key in {"asset_job_id", "stage_run_id"} else "instance_smoke"
    with locked_preparation_owners([owner_ref], actor) as owners:
        owner = owners[(key, str(owner_id))]
        if key == "asset_job_id":
            from studio.services.execution_task_links import validate_asset_preparation_link
            validate_asset_preparation_link(owner, version_id)
        elif key in {"deployment_smoke_id", "workflow_smoke_id"}:
            from studio.services.execution_task_links import validate_smoke_preparation_link
            validate_smoke_preparation_link(owner, version_id)
        lookup = {key: owner.pk}
        if key == "stage_run_id":
            lookup.update(unit_key=unit_key, request_key=request_key)
            if owner.stage_key == "bible_image_generation":
                from studio.services.execution_production_attempts import require_unfulfilled_material_unit
                material_unit_key = unit_key
                if "production_field" in requested.value:
                    from studio.services.execution_auxiliary_requests import validate_production_field_identity
                    parent, _, _ = validate_production_field_identity(stage_run_id=owner.pk,
                        version_id=version_id, unit_key=unit_key, request_key=request_key,
                        identity=requested.value["production_field"])
                    material_unit_key = parent.unit_key
                require_unfulfilled_material_unit(stage_id=owner.pk, unit_key=material_unit_key)
                if owner.production_run.plan_source == "recipe":
                    from studio.services.production_recipe_preparations import recipe_stage_context
                    from studio.services.production_recipe_routing import recipe_unit_role
                    from studio.services.production_recipe_material_sources import recipe_material_source
                    plan, declaration = recipe_stage_context(owner, None)
                    role = recipe_unit_role(owner, plan, material_unit_key)
                    if recipe_material_source(declaration, role) != "generate":
                        raise ValidationError("此角色配置为已有/上传成图，不能绕过待来源状态创建生成准备")
        previous = ExecutionPreparation.objects.select_for_update().filter(**lookup).first()
        if previous is not None:
            detail = read_execution_preparation_detail(previous)
            if (detail.intent["request"] != request.value
                    or str(previous.execution_instance_version_id) != str(version_id) or previous.purpose != purpose
                    or previous.unit_key != unit_key or previous.request_key != request_key
                    or previous.supersedes_id != (supersedes.pk if supersedes else None)):
                _conflict("同一业务请求已存在不同实例、来源或参数，不能覆盖")
            # Reading a past preparation does not check current target/TTL or
            # invoke the resolver again. Submission will do its own admission.
            definition = verify_stored_execution_definition(stored_execution_contract(version_id))
            if definition.sha256 != expected_definition_sha256:
                _conflict("准备引用的冻结定义已不一致")
            _verify_resolved(ResolvedExecutionPreparation(
                JsonContract(detail.resolved_inputs, detail.resolved_inputs_schema_version,
                             detail.resolved_inputs_size_bytes, detail.resolved_inputs_sha256),
                JsonContract(detail.review_contract, detail.review_contract_schema_version,
                             detail.review_contract_size_bytes, detail.review_contract_sha256),
            ), definition, request=requested.value)
            return previous
        if supersedes is not None:
            from studio.services.production_recipe_material_repreparation import require_replacement_parent
            require_replacement_parent(supersedes, owner_ref=owner_ref, unit_key=unit_key,
                request_key=request_key, requested=requested.value)
        version = execution_definition_version(version_id)
        if version.status != "published" or not version.instance.is_active:
            raise ValidationError("新准备必须选择启用实例的已发布精确版本")
        definition = verify_current_execution_definition(stored_execution_contract(version_id))
        if definition.sha256 != expected_definition_sha256:
            _conflict("实例合同与本次确认的定义不一致，请重新读取")
        resolved, review = _verify_resolved(resolve_facts(definition, owner), definition, request=requested.value)
        from studio.services.execution_field_review import verify_review_owner
        verify_review_owner(review.value, key, owner)
        # Intent identity includes first-resolved facts and generated seeds, not
        # merely the client request. Replay compares the nested request without
        # re-evaluating those immutable facts.
        return _persist_preparation(lookup=lookup, version=version, purpose=purpose, unit_key=unit_key,
            request_key=request_key, request=request, resolved=resolved, review=review, actor=actor, supersedes=supersedes)


@registry_reference_writer
@security_boundary_resolution_cache()
def create_resource_smoke_preparation(*, owner_ref, expected_definition_sha256, unit_key, request_key, intent, resolve_facts, actor):
    """Internal adapter for an original resource root allocated by its formal action.

    It shares storage and input/field facts, but cannot allocate a production
    root or an instance identity. Replays never reread mutable fixture sources.
    """
    if not isinstance(owner_ref, tuple) or len(owner_ref) != 2 or owner_ref[0] not in {"deployment_smoke_id", "workflow_smoke_id"}:
        raise ValidationError("基础资源准备只能归属原Smoke任务")
    for value in (unit_key, request_key):
        if not isinstance(value, str) or not value.strip() or len(value) > 191:
            raise ValidationError("资源准备需要明确且不超过191字符的请求与单元标识")
    if not isinstance(expected_definition_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_definition_sha256):
        raise ValidationError("资源准备需要精确资源定义摘要")
    requested = _verify_object(intent, "intent")
    request = json_contract({"schema_version": 1, "version_id": None, "definition_sha256": expected_definition_sha256,
        "unit_key": unit_key, "request_key": request_key, "requested": deepcopy(requested.value)}, max_bytes=execution_request_max_bytes())
    key, identifier = owner_ref
    from studio.services.execution_resource_smoke_definition import read_resource_smoke_definition
    from studio.services.execution_field_review import verify_review_owner
    with locked_preparation_owners([owner_ref], actor) as owners:
        root = owners[(key, str(identifier))]
        lookup = {key: root.pk}
        previous = ExecutionPreparation.objects.select_for_update().filter(**lookup).first()
        if previous is not None:
            detail = read_execution_preparation_detail(previous)
            if (detail.intent["request"] != request.value or previous.purpose != "resource_smoke"
                    or previous.execution_instance_version_id is not None or previous.unit_key != unit_key or previous.request_key != request_key):
                _conflict("同一资源验收请求已存在不同来源或定义，不能覆盖")
            definition = preparation_execution_definition(previous, detail=detail)
            _verify_resolved(ResolvedExecutionPreparation(
                JsonContract(detail.resolved_inputs, detail.resolved_inputs_schema_version, detail.resolved_inputs_size_bytes, detail.resolved_inputs_sha256),
                JsonContract(detail.review_contract, detail.review_contract_schema_version, detail.review_contract_size_bytes, detail.review_contract_sha256),
            ), definition, request=requested.value)
            return previous
        definition = read_resource_smoke_definition(root, current=True)
        if definition.sha256 != expected_definition_sha256:
            _conflict("资源合同与本次确认的定义摘要不一致")
        if root.status != "preparing" or root.submission_started_at is not None or root.provider_request_id:
            raise ValidationError("只有尚未提交的资源Smoke可以创建准备")
        resolved, review = _verify_resolved(resolve_facts(definition, root), definition, request=requested.value)
        verify_review_owner(review.value, key, root)
        return _persist_preparation(lookup=lookup, version=None, purpose="resource_smoke", unit_key=unit_key,
            request_key=request_key, request=request, resolved=resolved, review=review, actor=actor)
