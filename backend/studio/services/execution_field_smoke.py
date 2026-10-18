"""Explicit admin test facts with rules copied from a real published Profile.

Preview is read-only: no synthetic parent, approval, seed, task or model call.
The formal Smoke action revalidates its exact frozen field rules on creation.
"""
from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError

from studio.models import PromptProfileVersion
from studio.services.execution_definitions import stored_execution_contract, verify_stored_execution_definition
from studio.services.execution_field_contracts import ExecutionFieldInput, FIELD_OPERATION_MODES, freeze_execution_field_request, verify_execution_field_request
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.json_contracts import json_contract
from studio.services.prompt_normalization_contract import PromptNormalizationContractError
from studio.services.prompt_profiles import (
    validate_current_generation_source, validate_field_source_selector,
    verify_prompt_profile_contract,
)
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.security_boundaries import security_boundary_resolution_cache


def _admin(actor):
    if not actor or not actor.is_active or not actor.is_staff:
        raise PermissionDenied("字段测试夹具仅供有效管理员复核")


def _input_key(definition):
    config = definition.value["configuration"]
    inputs = config["inputs"]
    if (config["operation"] not in FIELD_OPERATION_MODES or config["prompt_mode"] != "none"
            or len(inputs) != 1 or inputs[0]["kind"] != "json"
            or inputs[0]["targets"] != [{"target_kind": "operation_input", "target_key": "field_request", "is_primary": True}]):
        raise ValidationError("此入口只适用于具有唯一字段批次输入的字段生成/翻译叶子")
    return inputs[0]["key"]


@security_boundary_resolution_cache()
def field_smoke_source(*, version_id, profile_version_id, actor):
    _admin(actor)
    definition = verify_stored_execution_definition(stored_execution_contract(version_id))
    return _field_smoke_source(definition, profile_version_id)


@registry_reference_writer
@security_boundary_resolution_cache()
def resource_field_smoke_source(*, selection, profile_version_id, actor):
    _admin(actor)
    from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
    return _field_smoke_source(build_resource_smoke_definition(selection), profile_version_id)


def _field_smoke_source(definition, profile_version_id):
    key = _input_key(definition)
    profile = PromptProfileVersion.objects.get(pk=profile_version_id, status="published")
    contract = verify_prompt_profile_contract(profile)
    mode = FIELD_OPERATION_MODES[definition.value["configuration"]["operation"]]
    rules = [deepcopy(row) for row in contract["rules"] if row["generation_mode"] == mode]
    if not rules:
        raise ValidationError("此策略没有符合当前叶子操作的字段，请选择其他已发布版本")
    _validate_current_field_rules(profile, {row["field_key"] for row in rules})
    return {"execution_instance_version_id": definition.value["version_id"], "definition_sha256": definition.sha256,
        "input_key": key, "profile_version_id": str(profile.pk), "profile_contract_sha256": profile.contract_sha256,
        "rules": rules}


def _validate_current_field_rules(profile, field_keys):
    """New leaf requests use current sources; historical readers keep frozen facts."""
    for rule in profile.field_rules.filter(field_definition__key__in=field_keys).select_related(
            "profile_version", "field_definition", "field_definition_version"):
        validate_field_source_selector(rule.source_selector)
        validate_current_generation_source(rule)


@security_boundary_resolution_cache()
def build_field_smoke_source(*, version_id, profile_version_id, expected_definition_sha256,
        expected_profile_contract_sha256, items, actor):
    _admin(actor)
    definition = verify_stored_execution_definition(stored_execution_contract(version_id))
    return _build_field_smoke_source(definition, profile_version_id=profile_version_id, expected_definition_sha256=expected_definition_sha256,
        expected_profile_contract_sha256=expected_profile_contract_sha256, items=items)


@registry_reference_writer
@security_boundary_resolution_cache()
def build_resource_field_smoke_source(*, selection, profile_version_id, expected_definition_sha256,
        expected_profile_contract_sha256, items, actor):
    _admin(actor)
    from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
    return _build_field_smoke_source(build_resource_smoke_definition(selection), profile_version_id=profile_version_id,
        expected_definition_sha256=expected_definition_sha256, expected_profile_contract_sha256=expected_profile_contract_sha256, items=items)


def _build_field_smoke_source(definition, *, profile_version_id, expected_definition_sha256, expected_profile_contract_sha256, items):
    source = _field_smoke_source(definition, profile_version_id)
    if source["definition_sha256"] != expected_definition_sha256 or source["profile_contract_sha256"] != expected_profile_contract_sha256:
        raise ValidationError("执行定义或策略来源与本次核对不一致，请重新读取", code="execution_definition_conflict")
    if not isinstance(items, list) or not items:
        raise ValidationError("请选择至少一个字段并填写本次测试事实")
    entries = []
    allowed = {row["field_key"] for row in source["rules"]}
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"field_key", "source_text", "source_language", "target_language", "context_facts", "protected_tokens"}
                or not isinstance(item["field_key"], str) or item["field_key"] not in allowed):
            raise ValidationError("字段测试只接受已发布规则的字段键及明确测试事实，不接受自定义规则")
        entries.append(ExecutionFieldInput(request_id="smoke:" + item["field_key"], unit_key="smoke",
            field_key=item["field_key"], source_text=item["source_text"], source_language=item["source_language"],
            target_language=item["target_language"], context_facts=deepcopy(item["context_facts"]),
            reference_facts={"protected_tokens": deepcopy(item["protected_tokens"]), "logical_inputs": [], "subject_facts": []}))
    profile = PromptProfileVersion.objects.get(pk=profile_version_id)
    batch = freeze_execution_field_request(operation=definition.value["configuration"]["operation"], profile=profile, inputs=entries)
    return {**source, "source": {"kind": "literal", "value": batch.value}}


def verify_instance_field_fixture(definition, inputs, *, allow_retired=False):
    """Revalidate client transport against stored rules, never trust preview JSON."""
    operation = definition.value["configuration"]["operation"]
    if operation not in FIELD_OPERATION_MODES:
        return None
    _input_key(definition)
    operation_inputs = inputs.get("operation_inputs") if isinstance(inputs, dict) else None
    if not isinstance(operation_inputs, dict) or set(operation_inputs) != {"field_request"}:
        raise ValidationError("字段叶子验收必须明确提供字段测试夹具")
    batch = json_contract(operation_inputs["field_request"], max_bytes=execution_request_max_bytes())
    try:
        request = verify_execution_field_request(batch)
    except PromptNormalizationContractError as exc:
        # Shared protocol validation fails for both new fixtures and frozen
        # evidence. Preserve the error; never repair the saved rule or skip it.
        raise ValidationError(str(exc), code=exc.code) from exc
    profile = PromptProfileVersion.objects.get(pk=request["profile"]["version_id"])
    contract = verify_prompt_profile_contract(profile)
    rules = {row["field_key"]: row for row in contract["rules"]}
    if (request["operation"] != operation or profile.status not in ({"published", "retired"} if allow_retired else {"published"})
            or profile.contract_sha256 != request["profile"]["contract_sha256"]
            or list(rules) != request["profile_field_keys"]
            or any(rules.get(row["field_contract"]["field_key"]) != row["field_contract"] for row in request["items"])):
        raise ValidationError("字段验收夹具必须引用实际冻结策略规则，不能自编规则或补造版本")
    if not allow_retired:
        _validate_current_field_rules(profile, {row["field_contract"]["field_key"] for row in request["items"]})
    return batch
