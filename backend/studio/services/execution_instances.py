"""Execution-instance authoring and explicit configuration lifecycle.

Only the publish action freezes the complete definition. Resource identity
validation alone cannot grant publication, readiness or task dispatch.
Runtime preparation/admission remain separate from configuration lifecycle.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import re

from django.core.exceptions import ValidationError
from django.db.models import Max

from studio.models import (
    ExecutionAuxiliaryBinding, ExecutionInputSpec, ExecutionInputTarget, ExecutionParameterSpec, ExecutionVersionContract,
    ModelDeployment, ModelExecutionInstance, ModelExecutionInstanceVersion,
    ModelProfileVersion,
    PromptProfileVersion,
    ProviderProfile,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache
from studio.services.media_capabilities import (
    conditioning_mode_contract,
    resolve_conditioning_contract,
)
from studio.services.model_profile_contracts import verified_model_profile_specification
from studio.services.platform_lifecycle_audit import lifecycle_summary, record_platform_lifecycle_event
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.prompt_profiles import (
    model_family_for_profile,
    validate_prompt_profile_source_projection,
    verify_prompt_profile_contract,
)
from studio.services.workflow_parameter_ranges import is_multiple
from studio.services.workflow_versions import (
    declared_workflow_operation,
    verify_workflow_version_integrity,
)


_SELECTION_FIELDS = frozenset({
    "deployment", "operation", "mode", "prompt_mode", "prompt_profile_version", "workflow_version", "output_version",
})
_PARAMETER_FIELDS = (
    "key", "value_type", "minimum", "maximum", "enum_values", "step", "remainder",
    "policy", "default_value", "locked_value", "seed_policy",
)


@dataclass(frozen=True)
class ExecutionResourceSelection:
    """Actual resources, without an instance or a fabricated version record.

    Instance authoring and administrative resource Smoke must project the same
    capability/channel rules. This selection is not a prepared request,
    publication record, or permission to execute either kind of call.
    """
    deployment: ModelDeployment
    operation: str
    mode: str
    prompt_mode: str
    prompt_profile_version: PromptProfileVersion | None
    workflow_version: WorkflowTemplateVersion | None
    output_version: object = None

    @classmethod
    def from_version(cls, version):
        return cls(**{key: getattr(version, key) for key in _SELECTION_FIELDS})

    @property
    def deployment_id(self):
        return self.deployment.pk

    @property
    def prompt_profile_version_id(self):
        return self.prompt_profile_version.pk if self.prompt_profile_version is not None else None

    @property
    def workflow_version_id(self):
        return self.workflow_version.pk if self.workflow_version is not None else None

    @property
    def output_version_id(self):
        return self.output_version.pk if self.output_version is not None else None


def execution_configuration_max_bytes() -> int:
    """Runtime policy may tighten, never enlarge, the new schema's storage cap."""
    return min(DATABASE_JSON_FIELD_MAX_BYTES, boundary_int("security.snapshot.stage_max_bytes"))


def execution_resolved_inputs_max_bytes() -> int:
    """Runtime facts follow the request policy, not configuration-column caps."""
    from studio.services.execution_request_storage import execution_request_max_bytes
    return execution_request_max_bytes()


def _validate_expected_revision(expected: object) -> None:
    if type(expected) is not int or expected < 1:
        raise ValidationError({"expected_revision": "必须提供明确的正整数修订号"})


def _require_revision(actual: int, expected: object) -> None:
    _validate_expected_revision(expected)
    if actual != expected:
        raise ValidationError("配置已被修改，请重新读取后再保存", code="execution_definition_conflict")


@registry_reference_writer
@security_boundary_resolution_cache()
def create_execution_instance(*, key: str, name: str, description: str, actor):
    instance = ModelExecutionInstance(key=key, name=name, description=description)
    json_contract({"description": description}, max_bytes=execution_configuration_max_bytes())
    instance.full_clean()
    instance.save()
    record_platform_lifecycle_event(
        instance, actor=actor, action="create", reason_code="api.lifecycle",
        after_summary=lifecycle_summary(instance),
    )
    return instance


@registry_reference_writer
@security_boundary_resolution_cache()
def update_execution_instance(*, instance_id, expected_revision, changes, actor):
    if not isinstance(changes, dict) or not changes or set(changes) - {"name", "description", "is_active"}:
        raise ValidationError({"changes": "只能修改名称、说明和启用状态；稳定 key 不可修改"})
    if "is_active" in changes and type(changes["is_active"]) is not bool:
        raise ValidationError({"is_active": "必须是布尔值"})
    instance = ModelExecutionInstance.objects.select_for_update().get(pk=instance_id)
    _require_revision(instance.revision, expected_revision)
    before = lifecycle_summary(instance)
    for key, value in changes.items():
        setattr(instance, key, value)
    json_contract({"description": instance.description}, max_bytes=execution_configuration_max_bytes())
    instance.revision += 1
    instance.full_clean()
    instance.save()
    record_platform_lifecycle_event(
        instance, actor=actor, action="update", reason_code="api.lifecycle",
        before_summary=before, after_summary=lifecycle_summary(instance),
    )
    return instance


@registry_reference_writer
@security_boundary_resolution_cache()
def create_execution_draft(*, instance_id, expected_revision: int, selection: dict, actor, configuration=None):
    """Allocate a draft under the stable parent lock; no implicit publication."""
    if isinstance(selection, dict):
        selection = {"output_version": None, **selection}
    if not isinstance(selection, dict) or set(selection) != _SELECTION_FIELDS:
        raise ValidationError({"selection": "必须明确提供执行目标和提示词方式的全部选择字段"})
    instance = ModelExecutionInstance.objects.select_for_update().get(pk=instance_id)
    _require_revision(instance.revision, expected_revision)
    latest = instance.versions.aggregate(number=Max("version"))["number"]
    version = ModelExecutionInstanceVersion(instance=instance, version=(latest or 0) + 1, **selection)
    version.full_clean()
    resolve_execution_resources(version)
    version.save()
    _replace_draft_inputs(version, [])
    instance.revision += 1
    instance.save(update_fields=("revision", "updated_at"))
    record_platform_lifecycle_event(
        version, actor=actor, action="create", reason_code="api.lifecycle",
        after_summary=lifecycle_summary(version),
    )
    if configuration is not None:
        if not isinstance(configuration, dict) or set(configuration) - {"inputs", "parameters", "auxiliaries"}:
            raise ValidationError({"configuration": "初始配置只能包含输入、参数和辅助配置"})
        if configuration:
            version = update_execution_draft(
                version_id=version.pk, expected_revision=version.revision, changes=configuration, actor=actor,
            )
    return version


def _replace_draft_inputs(version, rows):
    from studio.services.execution_profile_inputs import reconcile_profile_inputs
    rows = reconcile_profile_inputs(version, rows)
    if not isinstance(rows, list):
        raise ValidationError({"inputs": "输入配置必须是数组"})
    json_contract(rows, max_bytes=execution_configuration_max_bytes())
    fields = {"key", "position", "kind", "required", "min_items", "max_items", "semantic_contract", "targets"}
    version.inputs.all().delete()
    for row in rows:
        if (not isinstance(row, dict) or not fields <= row.keys()
                or row.keys() - fields - {"data_structure_version_id", "media_source_policy"} or not isinstance(row["targets"], list)):
            raise ValidationError({"inputs": "必须明确提供每项输入的完整定义与目标列表"})
        if type(row["required"]) is not bool or any(type(row[key]) is not int for key in ("position", "min_items", "max_items")):
            raise ValidationError({"inputs": "必填标志必须为布尔值；顺序和数量必须为整数"})
        spec = ExecutionInputSpec(version=version, **{key: value for key, value in row.items() if key != "targets"})
        spec.full_clean()
        spec.save()
        for target in row["targets"]:
            if not isinstance(target, dict) or set(target) != {"target_kind", "target_key", "is_primary"} or type(target["is_primary"]) is not bool:
                raise ValidationError({"inputs": "目标必须明确提供类型、key与布尔主槽位标志"})
            item = ExecutionInputTarget(input_spec=spec, **target)
            item.full_clean()
            item.save()
    validate_execution_input_mapping(version.inputs.prefetch_related("targets"))


def _replace_draft_parameters(version, rows):
    if not isinstance(rows, list):
        raise ValidationError({"parameters": "参数配置必须是数组"})
    json_contract(rows, max_bytes=execution_configuration_max_bytes())
    fields = set(_PARAMETER_FIELDS)
    version.parameters.all().delete()
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValidationError({"parameters": "必须明确提供参数定义；不使用的范围或值填写空值"})
        spec = ExecutionParameterSpec(version=version, **row)
        spec.full_clean()
        spec.save()


def _replace_draft_auxiliaries(version, rows):
    if not isinstance(rows, list):
        raise ValidationError({"auxiliaries": "辅助配置必须是数组"})
    json_contract(rows, max_bytes=execution_configuration_max_bytes())
    version.auxiliaries.all().delete()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"purpose", "target_version_id"}:
            raise ValidationError({"auxiliaries": "辅助配置必须明确用途和精确实例版本 ID"})
        item = ExecutionAuxiliaryBinding(version=version, **row)
        item.full_clean()
        item.save()


def validate_execution_auxiliary_references(version, *, require_complete=False):
    """Check purpose, exact capability and leaf shape, not combination Smoke.

    The full definition verifier must also verify each leaf's frozen contract
    and target fingerprint. This reference check deliberately does not claim
    that a published row alone proves those derived facts.
    """
    from studio.services.execution_prompts import AUXILIARY_OPERATIONS, profile_auxiliary_purposes

    required = set(profile_auxiliary_purposes(version.prompt_profile_version)) if version.prompt_mode == "profile" else set()
    rows = list(version.auxiliaries.select_related(
        "target_version__deployment__profile_version__provider", "target_version__workflow_version",
        "target_version__prompt_profile_version",
    ).prefetch_related("target_version__auxiliaries"))
    seen = set()
    identities = []
    for row in rows:
        target = row.target_version
        if row.purpose not in required or row.purpose not in AUXILIARY_OPERATIONS or row.purpose in seen:
            raise ValidationError({"auxiliaries": "辅助用途未在当前策略中使用或重复配置"})
        if target.pk == version.pk or target.auxiliaries.exists():
            raise ValidationError({"auxiliaries": "辅助实例必须是无进一步辅助依赖的叶子，不能自环或递归"})
        if target.status != ModelExecutionInstanceVersion.Status.PUBLISHED:
            raise ValidationError({"auxiliaries": "必须选择已发布的辅助实例版本"})
        if target.operation != AUXILIARY_OPERATIONS[row.purpose]:
            raise ValidationError({"auxiliaries": f"{row.purpose} 必须引用执行能力 {AUXILIARY_OPERATIONS[row.purpose]}"})
        resources = resolve_execution_resources(target)
        output_kind = target.workflow_version.template.output_kind if target.workflow_version_id else target.deployment.profile_version.modality
        if output_kind not in {"text", "multimodal"}:
            raise ValidationError({"auxiliaries": "字段辅助操作需要文本输出能力"})
        # A leaf with model-generated fields but missing its own bindings is
        # not a leaf: do not let incomplete imported records evade the check.
        if target.prompt_mode == "profile" and profile_auxiliary_purposes(target.prompt_profile_version):
            raise ValidationError({"auxiliaries": "辅助叶子的策略仍需要模型生成字段，不能形成隐式递归"})
        identities.append({"purpose": row.purpose, "version_id": str(target.pk), "resources": resources})
        seen.add(row.purpose)
    if require_complete and required != seen:
        raise ValidationError({"auxiliaries": "尚未配置策略所需的辅助实例：" + ", ".join(sorted(required - seen))})
    return identities


def execution_version_configuration(version):
    """Canonical authored facts for cloning/audit and the definition builder.

    Child row UUIDs are storage identities, not author choices. Do not include
    them in a configuration digest or accidentally copy them into successors.
    """
    inputs = [{
        "key": spec.key, "position": spec.position, "kind": spec.kind, "required": spec.required,
        "min_items": spec.min_items, "max_items": spec.max_items,
        "semantic_contract": deepcopy(spec.semantic_contract),
        **({"media_source_policy": deepcopy(spec.media_source_policy)} if spec.media_source_policy is not None else {}),
        **({"data_structure_version_id": str(spec.data_structure_version_id)} if spec.data_structure_version_id else {}),
        "targets": sorted([
            {"target_kind": target.target_kind, "target_key": target.target_key, "is_primary": target.is_primary}
            for target in spec.targets.all()
        ], key=lambda row: (row["target_kind"], row["target_key"])),
    } for spec in version.inputs.prefetch_related("targets").order_by("position")]
    return {
        "deployment_id": str(version.deployment_id), "operation": version.operation, "mode": version.mode,
        "prompt_mode": version.prompt_mode,
        "prompt_profile_version_id": str(version.prompt_profile_version_id) if version.prompt_profile_version_id else None,
        "workflow_version_id": str(version.workflow_version_id) if version.workflow_version_id else None,
        **({"output_version_id": str(version.output_version_id)} if version.output_version_id else {}),
        "inputs": inputs,
        "parameters": list(version.parameters.order_by("key").values(*_PARAMETER_FIELDS)),
        "auxiliaries": [{"purpose": row.purpose, "target_version_id": str(row.target_version_id)} for row in version.auxiliaries.order_by("purpose")],
    }


@registry_reference_writer
@security_boundary_resolution_cache()
def clone_execution_draft(*, source_id, expected_revision: int, actor, expected_definition_sha256=None):
    """Clone exact author choices only; never inherit publication or evidence."""
    from studio.services.execution_definitions import stored_execution_contract, verify_stored_execution_definition
    instance_id = ModelExecutionInstanceVersion.objects.values_list("instance_id", flat=True).get(pk=source_id)
    parent = ModelExecutionInstance.objects.select_for_update().get(pk=instance_id)
    source = ModelExecutionInstanceVersion.objects.select_for_update().get(pk=source_id)
    _require_revision(source.revision, expected_revision)
    if source.status == ModelExecutionInstanceVersion.Status.DRAFT:
        if expected_definition_sha256 is not None:
            raise ValidationError({"expected_definition_sha256": "草稿尚未发布冻结合同，应提交空值"})
    else:
        frozen = stored_execution_contract(source.pk)
        verify_stored_execution_definition(frozen)
        if expected_definition_sha256 != frozen.sha256:
            raise ValidationError("源版本合同与所查看的版本不一致，请重新读取", code="execution_definition_conflict")
    config = execution_version_configuration(source)
    from studio.services.execution_profile_inputs import reconcile_profile_inputs
    config["inputs"] = reconcile_profile_inputs(source, config["inputs"], cloning=True)
    json_contract(config, max_bytes=execution_configuration_max_bytes())
    # Nested registry writers share the transaction/lock. If any child fails
    # validation, allocation, parent revision and both audit events roll back.
    draft = create_execution_draft(
        instance_id=parent.pk, expected_revision=parent.revision,
        selection={key: getattr(source, key) for key in _SELECTION_FIELDS}, actor=actor,
    )
    return update_execution_draft(
        version_id=draft.pk, expected_revision=draft.revision,
        changes={key: config[key] for key in ("inputs", "parameters", "auxiliaries")}, actor=actor,
    )


@registry_reference_writer
@security_boundary_resolution_cache()
def update_execution_draft(*, version_id, expected_revision: int, changes: dict, actor):
    """Atomically edit selection/inputs/parameters of a draft, never a published row.

    Full derived definitions and lifecycle endpoints are separate actions.
    An edited draft deliberately has no publishable snapshot
    until the complete contract builder has validated all layers.
    """
    if not isinstance(changes, dict) or not changes or set(changes) - (_SELECTION_FIELDS | {"inputs", "parameters", "auxiliaries"}):
        raise ValidationError({"changes": "只能修改执行目标、输入、参数和辅助配置，不能指定生命周期或派生合同"})
    # Lock family before version, matching creation and later publish/clone.
    instance_id = ModelExecutionInstanceVersion.objects.values_list("instance_id", flat=True).get(pk=version_id)
    ModelExecutionInstance.objects.select_for_update().get(pk=instance_id)
    version = ModelExecutionInstanceVersion.objects.select_for_update().get(pk=version_id)
    _require_revision(version.revision, expected_revision)
    if version.status != ModelExecutionInstanceVersion.Status.DRAFT:
        raise ValidationError("已发布或退役版本不能修改，请创建后继草稿", code="execution_definition_conflict")
    before = lifecycle_summary(version)
    for key in _SELECTION_FIELDS & set(changes):
        setattr(version, key, changes[key])
    version.full_clean()
    resolve_execution_resources(version)
    if "inputs" in changes or _SELECTION_FIELDS & set(changes):
        rows = changes.get("inputs", execution_version_configuration(version)["inputs"])
        _replace_draft_inputs(version, rows)
    if "parameters" in changes:
        _replace_draft_parameters(version, changes["parameters"])
    if "auxiliaries" in changes:
        _replace_draft_auxiliaries(version, changes["auxiliaries"])
    if version.auxiliaries.exists():
        validate_execution_auxiliary_references(version)
    if version.workflow_version_id is not None and version.inputs.exists():
        from studio.services.execution_inputs import validate_workflow_input_targets

        validate_workflow_input_targets(version)
    if version.parameters.exists():
        if version.workflow_version_id is not None:
            from studio.services.execution_parameters import validate_workflow_execution_parameters

            validate_workflow_execution_parameters(version)
        elif version.deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
            from studio.services.execution_api_contracts import validate_async_execution_parameters

            validate_async_execution_parameters(version)
        else:
            from studio.services.execution_text_contracts import text_execution_interface
            from studio.services.execution_parameters import restrict_execution_parameters

            interface = text_execution_interface(version)
            restrict_execution_parameters(interface["parameters"], version.parameters.all(), source=f"instance.{version.pk}.parameters")
    json_contract(execution_version_configuration(version), max_bytes=execution_configuration_max_bytes())
    ExecutionVersionContract.objects.filter(version=version).delete()
    version.revision += 1
    version.save()
    record_platform_lifecycle_event(
        version, actor=actor, action="update", reason_code="api.lifecycle",
        before_summary=before, after_summary=lifecycle_summary(version),
    )
    return version


def validate_input_semantic_contract(value: object) -> None:
    """Check the storage shape, not membership in a target's purpose catalog."""
    from studio.services.execution_input_catalog import EXECUTION_SCOPE_KINDS, EXECUTION_SUBJECT_KINDS

    keys = {
        "schema_version", "allowed_purposes", "allowed_subject_kinds",
        "allowed_scope_kinds", "paired_with_input_key", "allow_repeated_asset",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError({"semantic_contract": "必须明确提供 schema1 的全部语义字段"})
    json_contract(value, max_bytes=execution_configuration_max_bytes())
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValidationError({"semantic_contract": "只支持 schema1"})
    for key in ("allowed_purposes", "allowed_subject_kinds", "allowed_scope_kinds"):
        items = value[key]
        if (
            not isinstance(items, list)
            or any(not isinstance(item, str) or re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", item) is None for item in items)
            or len(set(items)) != len(items)
        ):
            raise ValidationError({"semantic_contract": f"{key} 必须是无重复的语义 key 数组"})
    for key, catalog in (("allowed_subject_kinds", EXECUTION_SUBJECT_KINDS), ("allowed_scope_kinds", EXECUTION_SCOPE_KINDS)):
        unknown = sorted(set(value[key]) - catalog.keys())
        if unknown:
            raise ValidationError({f"semantic_contract.{key}": "未注册的事实类型：" + ", ".join(unknown)})
    paired = value["paired_with_input_key"]
    if paired is not None and (
        not isinstance(paired, str) or re.fullmatch(r"[a-z][a-z0-9_.-]{1,99}", paired) is None
    ):
        raise ValidationError({"semantic_contract": "paired_with_input_key 必须为空或精确输入 key"})
    if type(value["allow_repeated_asset"]) is not bool:
        raise ValidationError({"semantic_contract": "allow_repeated_asset 必须是布尔值"})


def _locked_execution_version(version_id):
    instance_id = ModelExecutionInstanceVersion.objects.values_list("instance_id", flat=True).get(pk=version_id)
    parent = ModelExecutionInstance.objects.select_for_update().get(pk=instance_id)
    version = ModelExecutionInstanceVersion.objects.select_for_update().get(pk=version_id)
    return parent, version


def _expected_definition(actual, expected):
    if not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
        raise ValidationError({"expected_definition_sha256": "必须提供刚核对的完整定义摘要"})
    if actual != expected:
        raise ValidationError("所查看的执行定义已变化，请重新读取", code="execution_definition_conflict")


@registry_reference_writer
@security_boundary_resolution_cache()
def publish_execution_version(*, version_id, expected_revision, expected_definition_sha256, actor):
    from django.utils import timezone
    from studio.services.execution_definitions import build_execution_definition, stored_execution_contract, verify_current_execution_definition

    _validate_expected_revision(expected_revision)
    parent, version = _locked_execution_version(version_id)
    if version.status == ModelExecutionInstanceVersion.Status.PUBLISHED:
        # Same successful action replays before checking its former revision;
        # it cannot rewrite a version or certify a changed execution target.
        contract = stored_execution_contract(version.pk)
        _expected_definition(contract.sha256, expected_definition_sha256)
        verify_current_execution_definition(contract)
        return version
    _require_revision(version.revision, expected_revision)
    if version.status != ModelExecutionInstanceVersion.Status.DRAFT:
        raise ValidationError("退役版本不能重新发布，请创建后继", code="execution_definition_conflict")
    definition = build_execution_definition(version.pk)
    _expected_definition(definition.sha256, expected_definition_sha256)
    before = lifecycle_summary(version)
    contract, _created = ExecutionVersionContract.objects.update_or_create(version=version, defaults={
        "definition": definition.value, "schema_version": definition.schema_version,
        "size_bytes": definition.size_bytes, "sha256": definition.sha256,
        "target_fingerprint": definition.value["target_fingerprint"],
    })
    contract.full_clean()
    version.status = ModelExecutionInstanceVersion.Status.PUBLISHED
    version.published_at = timezone.now()
    version.published_by = actor
    version.revision += 1
    version.full_clean()
    version.save()
    parent.revision += 1
    parent.save(update_fields=("revision", "updated_at"))
    record_platform_lifecycle_event(version, actor=actor, action="publish", reason_code="api.lifecycle",
                                    before_summary=before, after_summary=lifecycle_summary(version))
    return version


@registry_reference_writer
@security_boundary_resolution_cache()
def retire_execution_version(*, version_id, expected_revision, expected_definition_sha256, reason, actor):
    from studio.models import PlatformLifecycleAuditEvent
    from studio.services.execution_definitions import stored_execution_contract, verify_stored_execution_definition

    if not isinstance(reason, str) or not reason.strip() or len(reason) > 191:
        raise ValidationError({"reason": "请提供不超过191字符的退役原因"})
    _validate_expected_revision(expected_revision)
    parent, version = _locked_execution_version(version_id)
    if version.status not in {ModelExecutionInstanceVersion.Status.PUBLISHED, ModelExecutionInstanceVersion.Status.RETIRED}:
        raise ValidationError("草稿尚未发布，不能退役", code="execution_definition_conflict")
    contract = stored_execution_contract(version.pk)
    verify_stored_execution_definition(contract)
    _expected_definition(contract.sha256, expected_definition_sha256)
    if version.status == ModelExecutionInstanceVersion.Status.RETIRED:
        event = PlatformLifecycleAuditEvent.objects.filter(
            object_type="model_execution_instance_version", object_id=version.pk, action="retire",
        ).order_by("-created_at", "-id").first()
        if event is None or event.after_summary.get("retirement_reason") != reason:
            raise ValidationError("退役请求与已完成动作不一致，请重新读取", code="execution_definition_conflict")
        return version
    _require_revision(version.revision, expected_revision)
    before = lifecycle_summary(version)
    version.status = ModelExecutionInstanceVersion.Status.RETIRED
    version.revision += 1
    version.full_clean()
    version.save()
    parent.revision += 1
    parent.save(update_fields=("revision", "updated_at"))
    # A broken/unavailable live target must not prevent retiring its frozen
    # configuration. Preserve the bounded reason in the existing audit detail.
    record_platform_lifecycle_event(version, actor=actor, action="retire", reason_code="api.lifecycle",
                                    before_summary=before, after_summary={**lifecycle_summary(version), "retirement_reason": reason})
    return version


def _valid_scalar(value: object, value_type: str) -> bool:
    if value_type == "boolean":
        return type(value) is bool
    if value_type == "integer":
        return type(value) is int
    if value_type == "number":
        return type(value) is int or (type(value) is float and math.isfinite(value))
    return value_type == "string" and isinstance(value, str)


def validate_execution_parameter_spec(spec) -> None:
    """Validate one explicit local restriction; never infer underlying bounds."""
    if spec.value_type not in {"boolean", "integer", "number", "string"}:
        raise ValidationError({"value_type": "未支持的参数类型"})
    if spec.policy not in {"overridable", "locked"}:
        raise ValidationError({"policy": "必须明确选择运行时配置或固定值"})
    if spec.policy == "locked":
        if spec.locked_value is None or spec.default_value is not None:
            raise ValidationError({"locked_value": "固定参数必须有固定值，且不能同时配置默认值"})
    elif spec.locked_value is not None:
        raise ValidationError({"locked_value": "可配置参数不能同时设置固定值"})
    if spec.seed_policy is not None:
        if spec.seed_policy not in ExecutionParameterSpec.SeedPolicy.values or spec.value_type != "integer":
            raise ValidationError({"seed_policy": "种子策略只适用于明确的整数参数"})
        if spec.seed_policy == "generate_once" and (
            spec.policy != "overridable" or spec.default_value is not None or spec.locked_value is not None
        ):
            raise ValidationError({"seed_policy": "每次新请求生成种子不能同时设置固定值或实例默认值"})
    numeric = spec.value_type in {"integer", "number"}
    for name in ("minimum", "maximum", "step", "remainder"):
        value = getattr(spec, name)
        if value is not None and (not numeric or not _valid_scalar(value, spec.value_type)):
            raise ValidationError({name: "范围和步长必须符合数值参数类型，布尔值不是数字"})
    if spec.minimum is not None and spec.maximum is not None and spec.minimum > spec.maximum:
        raise ValidationError({"minimum": "最小值不能大于最大值"})
    if spec.step is not None and spec.step <= 0:
        raise ValidationError({"step": "步长必须大于0"})
    if spec.remainder is not None and (
        spec.value_type != "integer" or spec.step is None or not 0 <= spec.remainder < spec.step
    ):
        raise ValidationError({"remainder": "整数对齐必须明确步长，且满足0 <= 余数 < 步长"})
    if not isinstance(spec.enum_values, list):
        raise ValidationError({"enum_values": "枚举必须是数组；空数组表示不限制枚举"})
    checked = []
    values = [("enum_values", value) for value in spec.enum_values]
    values += [(name, getattr(spec, name)) for name in ("default_value", "locked_value") if getattr(spec, name) is not None]
    for name, value in values:
        if not _valid_scalar(value, spec.value_type):
            raise ValidationError({name: f"值必须符合 {spec.value_type} 类型"})
        if name == "enum_values":
            if value in checked:
                raise ValidationError({name: "枚举值不能重复"})
            checked.append(value)
        elif spec.enum_values and value not in spec.enum_values:
            raise ValidationError({name: "值不在枚举范围内"})
        if numeric:
            if spec.minimum is not None and value < spec.minimum:
                raise ValidationError({name: "值小于最小值"})
            if spec.maximum is not None and value > spec.maximum:
                raise ValidationError({name: "值大于最大值"})
            if spec.step is not None and not is_multiple(value - (spec.remainder or 0), spec.step):
                raise ValidationError({name: "值不满足步长或余数对齐"})
    json_contract({name: getattr(spec, name) for name in (
        "minimum", "maximum", "step", "remainder", "enum_values", "default_value", "locked_value", "seed_policy",
    )}, max_bytes=execution_configuration_max_bytes())


def validate_execution_input_mapping(inputs) -> None:
    """Validate one complete logical-to-target configuration, without resolving media.

    Callers can prefetch ``targets``. Target existence/type and permitted
    purposes still have to be checked against the selected resource contract.
    """
    rows = list(inputs)
    keys = [row.key for row in rows]
    positions = [row.position for row in rows]
    if len(set(keys)) != len(keys) or len(set(positions)) != len(positions):
        raise ValidationError({"inputs": "输入key和显示顺序必须各自唯一"})
    physical_targets = {}
    pairings = {}
    for row in rows:
        row.full_clean()
        targets = list(row.targets.all())
        if not targets:
            raise ValidationError({"inputs": f"{row.key} 未配置目标输入"})
        direct_targets = []
        for target in targets:
            target.full_clean()
            if target.target_kind == "profile_input":
                continue
            direct_targets.append(target)
            identity = (target.target_kind, target.target_key)
            previous = physical_targets.get(identity)
            if previous is not None:
                raise ValidationError({"inputs": f"{row.key} 与 {previous} 不能写入同一个执行槽位"})
            physical_targets[identity] = row.key
        if direct_targets and sum(target.is_primary for target in direct_targets) != 1:
            raise ValidationError({"inputs": f"{row.key} 必须明确且仅指定一个主执行槽位"})
        paired = row.semantic_contract["paired_with_input_key"]
        if paired is not None:
            if paired not in keys or paired == row.key:
                raise ValidationError({"inputs": f"{row.key} 的配对输入不存在或指向自身"})
            pairings[row.key] = paired
    for key in pairings:
        seen = set()
        current = key
        while current in pairings:
            if current in seen:
                raise ValidationError({"inputs": "配对输入不能形成循环"})
            seen.add(current)
            current = pairings[current]


def _execution_model_contract(deployment):
    model = deployment.profile_version
    if model.status != ModelProfileVersion.Status.PUBLISHED:
        raise ValidationError({"deployment": "模型部署必须引用已发布模型版本"})
    specification = verified_model_profile_specification(model)
    capabilities = specification.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(item, str) or not item or item.strip() != item for item in capabilities)
        or len(set(capabilities)) != len(capabilities)
    ):
        raise ValidationError({"deployment": "模型合同必须明确声明无重复的精确 capabilities"})
    return specification, capabilities


def _execution_workflow_identity(deployment, workflow, *, resource_smoke=False):
    model = deployment.profile_version
    if workflow is not None:
        allowed = {WorkflowTemplateVersion.Status.DRAFT, WorkflowTemplateVersion.Status.VALIDATED,
            WorkflowTemplateVersion.Status.PUBLISHED} if resource_smoke else {WorkflowTemplateVersion.Status.PUBLISHED}
        if workflow.status not in allowed:
            raise ValidationError({"workflow_version": "必须引用已发布工作流"})
        if workflow.model_profile_version_id != model.pk:
            raise ValidationError({"workflow_version": "工作流与模型部署必须引用同一精确模型版本"})
        if workflow.provider_id != model.provider_id:
            raise ValidationError({"workflow_version": "工作流与模型的Provider不一致"})
        if workflow.validated_deployment_id != deployment.pk:
            raise ValidationError({"workflow_version": "工作流的校验部署与所选模型部署不一致"})
        verify_workflow_version_integrity(workflow)
        return {
            "id": str(workflow.pk),
            "execution_contract_sha256": workflow.execution_contract_sha256,
            "projector": deepcopy(workflow.input_contract["contract_projector"]),
        }


@security_boundary_resolution_cache()
def execution_selection_options(*, deployment=None, workflow=None, resource_smoke=False):
    """Read authoring choices before creating a draft; never imply admission."""
    from studio.services.execution_input_catalog import execution_input_identity_options
    from studio.services.model_profile_options import MODEL_CAPABILITY_CATALOG

    def choices(values):
        return [{"value": key, "label": label} for key, label in values]

    result = {
        "prompt_modes": choices(ModelExecutionInstanceVersion.PromptMode.choices),
        "version_statuses": choices(ModelExecutionInstanceVersion.Status.choices),
        "input_kinds": choices(ExecutionInputSpec.Kind.choices),
        "target_kinds": choices(ExecutionInputTarget.Kind.choices),
        "parameter_types": choices(ExecutionParameterSpec.ValueType.choices),
        "parameter_policies": choices(ExecutionParameterSpec.Policy.choices),
        "seed_policies": choices(ExecutionParameterSpec.SeedPolicy.choices),
        "identity_options": execution_input_identity_options(), "target": None,
    }
    if deployment is None:
        if workflow is not None:
            raise ValidationError({"deployment_id": "请先选择模型部署"})
        return result
    model = deployment.profile_version
    specification, capabilities = _execution_model_contract(deployment)
    uses_workflow = model.provider.driver_type == ProviderProfile.DriverType.COMFYUI
    if workflow is not None and not uses_workflow:
        raise ValidationError({"workflow_version_id": "直接API不能选择工作流"})
    workflow_identity = _execution_workflow_identity(deployment, workflow, resource_smoke=resource_smoke)
    if workflow is not None:
        operation = declared_workflow_operation(workflow)
        if operation not in capabilities:
            raise ValidationError({"workflow_version_id": "工作流能力未被模型合同声明"})
        capabilities = [operation]
    conditioning = resolve_conditioning_contract(
        workflow_input_contract=workflow.input_contract if workflow else None,
        model_specification=specification,
        output_kind=workflow.template.output_kind if workflow else model.modality,
    )
    labels = {row["value"]: row["label"] for row in MODEL_CAPABILITY_CATALOG}
    from studio.services.execution_output_contracts import output_parser_keys
    result["target"] = {
        "deployment_id": str(deployment.pk), "model_profile_version_id": str(model.pk),
        # Family is required by Profile matching, not by manual/none execution.
        # Report absence; never infer it from a filename or block unrelated ports.
        "model_family": model_family_for_profile(model) if "model_family" in specification else None,
        "driver": model.provider.driver_type,
        "workflow_required": uses_workflow, "workflow": workflow_identity,
        "operations": [{"value": key, "label": labels.get(key, key), "output_parser_keys": output_parser_keys(key)} for key in capabilities],
        "modes": [{"value": row["key"], "label": row["label"]} for row in conditioning["modes"]],
    }
    return json_contract(result, max_bytes=execution_configuration_max_bytes()).value


def resolve_execution_resources(version: ModelExecutionInstanceVersion | ExecutionResourceSelection, *, resource_smoke=False) -> dict:
    """Reference consistency only, not publication, admission or Smoke."""
    if resource_smoke and not isinstance(version, ExecutionResourceSelection):
        raise ValidationError("基础资源验收必须使用资源选择，不能放宽执行实例的发布条件")
    deployment = version.deployment
    from studio.services.execution_output_contracts import execution_output_reference
    execution_output_reference(version)
    model = deployment.profile_version
    specification, capabilities = _execution_model_contract(deployment)
    if version.operation not in capabilities:
        raise ValidationError({"operation": "模型合同未声明此执行能力"})
    workflow = version.workflow_version
    uses_workflow = model.provider.driver_type == ProviderProfile.DriverType.COMFYUI
    if uses_workflow != (workflow is not None):
        raise ValidationError({"workflow_version": "ComfyUI必须选择一个工作流；直接API不能选择工作流"})
    workflow_identity = _execution_workflow_identity(deployment, workflow, resource_smoke=resource_smoke)
    if workflow is not None and declared_workflow_operation(workflow) != version.operation:
        raise ValidationError({"operation": "执行能力与工作流冻结的capability不一致"})
    conditioning = resolve_conditioning_contract(
        workflow_input_contract=workflow.input_contract if workflow else None,
        model_specification=specification,
        output_kind=workflow.template.output_kind if workflow else model.modality,
    )
    selected_mode = None
    if version.mode or conditioning["modes"]:
        selected_mode = conditioning_mode_contract(conditioning, version.mode)
    profile = version.prompt_profile_version
    profile_identity = None
    if version.prompt_mode == ModelExecutionInstanceVersion.PromptMode.PROFILE:
        if profile is None or profile.status != PromptProfileVersion.Status.PUBLISHED:
            raise ValidationError({"prompt_profile_version": "策略组装必须引用一个已发布策略版本"})
        verify_prompt_profile_contract(profile)
        validate_prompt_profile_source_projection(profile)
        if model_family_for_profile(model) != profile.model_family:
            raise ValidationError({"prompt_profile_version": "提示词策略与精确模型的模型族不一致"})
        if profile.mode and profile.mode != version.mode:
            raise ValidationError({"mode": "所选策略的非空模式与执行实例不一致"})
        profile_identity = {
            "id": str(profile.pk), "contract_sha256": profile.contract_sha256,
            "operation": profile.operation, "mode": profile.mode,
        }
    elif version.prompt_mode in {
        ModelExecutionInstanceVersion.PromptMode.MANUAL, ModelExecutionInstanceVersion.PromptMode.NONE,
    }:
        if profile is not None:
            raise ValidationError({"prompt_profile_version": "人工或无提示词模式不能关联策略"})
    else:
        raise ValidationError({"prompt_mode": "未支持的提示词方式"})
    return {
        "deployment_id": str(deployment.pk),
        "model_profile_version_id": str(model.pk),
        "model_specification_sha256": model.specification_sha256,
        "execution": {"operation": version.operation, "mode": version.mode},
        "conditioning_mode": selected_mode,
        "prompt_mode": version.prompt_mode,
        "prompt_profile": profile_identity,
        "workflow": workflow_identity,
    }
def selectable_execution_versions(actor):
    """Published consumer choices, not a claim of current Smoke/admission."""
    from django.db.models import Q
    from studio.models import WorkflowTemplate
    from studio.services.production_pipeline import executable_templates

    query = ModelExecutionInstanceVersion.objects.filter(status="published", instance__is_active=True)
    if not actor or not actor.is_authenticated or not actor.is_active:
        return query.none()
    templates = executable_templates(WorkflowTemplate.objects.all(), actor).values("pk")
    return query.filter(Q(workflow_version__isnull=True) | Q(workflow_version__template_id__in=templates))
