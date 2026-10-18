"""Derived execution definitions; publication is not admission or dispatch.

One builder composes already reviewed resource/interface validators. Unsupported
adapters fail explicitly: a relationship-only draft cannot acquire a complete
definition by omitting an input partition. No compatibility schema is accepted.
"""
from copy import deepcopy
import re

from django.core.exceptions import ValidationError

from studio.models import ExecutionVersionContract, ModelExecutionInstanceVersion, ProviderProfile
from studio.services.execution_api_contracts import async_execution_interface, validate_async_execution_wiring
from studio.services.execution_identity import execution_target_contract
from studio.services.execution_inputs import validate_workflow_input_targets
from studio.services.execution_input_catalog import execution_input_purpose_options, validate_execution_input_purposes
from studio.services.execution_instances import (
    ExecutionResourceSelection,
    execution_configuration_max_bytes, execution_version_configuration,
    resolve_execution_resources, validate_execution_input_mapping,
    validate_execution_auxiliary_references,
)
from studio.services.execution_parameters import restrict_execution_parameters
from studio.services.execution_prompts import validate_execution_prompt_channels
from studio.services.execution_profile_inputs import derived_profile_inputs, profile_input_targets, validate_profile_input_targets
from studio.services.execution_workflow_contracts import workflow_execution_interface
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_compiler import WorkflowCompilerError, workflow_primary_output_contract


DEFINITION_SCHEMA_VERSION = 1
_DEFINITION_KEYS = {"schema_version", "version_id", "configuration", "resources", "interface", "target", "target_fingerprint"}


def execution_definition_version(version_id):
    """Fresh exact references, never a caller's stale related-object cache."""
    return ModelExecutionInstanceVersion.objects.select_related(
        "instance", "deployment__profile_version__provider", "workflow_version__template",
        "workflow_version__model_profile_version", "workflow_version__provider", "prompt_profile_version", "output_version",
    ).prefetch_related("inputs__targets", "parameters", "auxiliaries").get(pk=version_id)


def _unsupported(path, message):
    error = ValidationError({path: message})
    error.code = "execution_interface_unavailable"
    raise error


def _verify_read_revision(version):
    if not ModelExecutionInstanceVersion.objects.filter(pk=version.pk, revision=version.revision).exists():
        raise ValidationError("实例配置在读取期间变化，请重新读取", code="execution_definition_conflict")


def _complete_auxiliaries(version):
    """Freeze exact leaf identities, never another recursively built graph.

    Check leaf shape before reading its definition. These are configuration
    proofs, not successful Smoke evidence or authorization to start a call.
    """
    identities = validate_execution_auxiliary_references(version, require_complete=True)
    result = {}
    for row in identities:
        contract = stored_execution_contract(row["version_id"])
        leaf = verify_current_execution_definition(contract)
        interface = leaf.value["interface"]
        if interface["prompt"]["auxiliary_purposes"] or interface.get("auxiliaries"):
            raise ValidationError({"auxiliaries": "辅助合同含有进一步依赖，不能递归组装"})
        if interface["output"].get("adapter") != "text.prompt_fields/v1":
            raise ValidationError({"auxiliaries": "辅助实例必须使用已登记的字段结果协议"})
        result[row["purpose"]] = {
            "version_id": row["version_id"], "definition_sha256": leaf.sha256,
            "target_fingerprint": contract.target_fingerprint,
            "operation": leaf.value["configuration"]["operation"],
        }
    return result


def stored_execution_contract(version_id):
    try:
        return ExecutionVersionContract.objects.get(version_id=version_id)
    except ExecutionVersionContract.DoesNotExist as exc:
        raise ValidationError("已发布或退役版本缺少冻结合同，不能按当前配置补造", code="execution_definition_conflict") from exc


def _complete_inputs(version, interface):
    inputs = list(version.inputs.all())
    validate_execution_input_mapping(inputs)
    if version.workflow_version_id:
        validate_workflow_input_targets(version, require_complete=True)
    else:
        assigned = set()
        for spec in inputs:
            for mapping in spec.targets.all():
                if mapping.target_kind == "profile_input":
                    continue
                target = interface["input_targets"].get(mapping.target_key)
                if mapping.target_kind != "operation_input" or target is None:
                    raise ValidationError({f"inputs.{spec.key}": "所选 API 未声明此逻辑输入"})
                if spec.kind != target["kind"] or spec.min_items < target["min_items"] or spec.max_items > target["max_items"] or (target["required"] and not spec.required):
                    raise ValidationError({f"inputs.{spec.key}": "操作输入的类型、数量或必填性不匹配"})
                source_kinds = set(target.get("source_kinds", ()))
                if source_kinds and source_kinds <= {"context_field", "selected_output"}:
                    from studio.services.execution_input_catalog import execution_context_field_options, execution_text_output_options
                    sources = [row for row in execution_context_field_options()
                        if "context_field" in source_kinds and row["selector_key"] in target.get("context_selectors", ())]
                    sources.extend(row for row in execution_text_output_options()
                        if "selected_output" in source_kinds and row["output_key"] in target.get("selected_output_keys", ()))
                    if not any(row["kind"] == spec.kind
                            and row["scope_kind"] in spec.semantic_contract["allowed_scope_kinds"]
                            for row in sources):
                        raise ValidationError({f"inputs.{spec.key}": "输入范围无法接入目标要求的已确认上下文"})
                assigned.add(mapping.target_key)
        missing = sorted(key for key, target in interface["input_targets"].items() if target["required"] and key not in assigned)
        if missing:
            raise ValidationError({"inputs": "必需操作输入尚未接线：" + ", ".join(missing)})
    validate_profile_input_targets(version, interface.get("profile_inputs", {}), require_complete=True)
    validate_execution_input_purposes(inputs, interface["input_purposes"])
    from studio.services.execution_source_readers import input_source_reader
    for spec in execution_version_configuration(version)["inputs"]:
        reader = input_source_reader(spec, interface)
        if reader:
            required_kind = "project_snapshot" if reader["protocol"] == "project_snapshot/v1" else "context_field"
            for mapping in spec["targets"]:
                target = interface["input_targets"].get(mapping["target_key"], {}) if mapping["target_kind"] != "profile_input" else {}
                if ("source_kinds" in target and required_kind not in target["source_kinds"]
                        or required_kind == "context_field" and "context_selectors" in target and reader["selector"] not in target["context_selectors"]):
                    raise ValidationError({f"inputs.{spec['key']}": "策略来源与物理目标要求的来源类型冲突"})
    for spec in inputs:
        mappings = list(spec.targets.all())
        for mapping in mappings:
            if mapping.target_kind != "workflow_slot":
                if spec.semantic_contract["paired_with_input_key"] is not None:
                    raise ValidationError({f"inputs.{spec.key}": "此输入不支持素材配对接线"})
                continue
            wiring = interface["input_targets"][mapping.target_key]["wiring"]
            parent_target = wiring.get("index_source_target_key")
            paired_key = spec.semantic_contract["paired_with_input_key"]
            if parent_target is None:
                if paired_key is not None:
                    raise ValidationError({f"inputs.{spec.key}": "冻结目标没有声明此配对接线"})
                continue
            parents = [row for row in inputs if row.key == paired_key]
            if len(parents) != 1 or not any(
                target.target_kind == "workflow_slot" and target.target_key == parent_target
                for target in parents[0].targets.all()
            ):
                raise ValidationError({f"inputs.{spec.key}": "配套输入必须明确关联接入父目标的逻辑输入"})


def execution_resource_interface(selection: ExecutionResourceSelection, resources):
    """One resource projection shared by draft forms and complete definitions."""
    if not isinstance(selection, ExecutionResourceSelection):
        raise ValidationError("资源接口需要明确选择的资源引用，不接受虚构的实例版本")
    version = selection
    driver = version.deployment.provider.driver_type
    if driver == ProviderProfile.DriverType.COMFYUI:
        from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
        if version.operation in NATIVE_TEXT_OPERATIONS:
            _unsupported("operation", "该原生业务输出尚无 ComfyUI 结构化适配；不能用普通文本回执冒充验收通过")
        interface = workflow_execution_interface(version)
        workflow = version.workflow_version
        try:
            output = workflow_primary_output_contract(workflow.input_contract, workflow.graph, workflow.template.output_kind)
        except WorkflowCompilerError as exc:
            raise ValidationError({"output_contract": str(exc)}) from exc
        interface["output"] = {"adapter": deepcopy(workflow.input_contract["contract_projector"]), "contract": output}
        from studio.services.execution_monitoring import resolve_execution_monitoring_contract
        from studio.services.model_profile_contracts import verified_model_profile_specification
        interface["execution_monitoring"] = resolve_execution_monitoring_contract(
            model_specification=verified_model_profile_specification(version.deployment.profile_version),
            workflow_input_contract=workflow.input_contract, workflow_graph=workflow.graph)
        source, label = f"workflow.{workflow.pk}", "工作流"
    elif driver == ProviderProfile.DriverType.ASYNC_REST:
        validate_async_execution_wiring(version)
        interface = async_execution_interface(version)
        interface["output"] = {"adapter": driver, "kind": interface["output_kind"]}
        source, label = f"model.{version.deployment.profile_version_id}.async_media", "API"
    else:
        from studio.services.execution_text_contracts import text_execution_interface

        interface = text_execution_interface(version)
        source, label = f"model.{version.deployment.profile_version_id}.text_execution", "文本协议"
    if version.prompt_mode == "profile":
        interface["profile_inputs"] = profile_input_targets(version.prompt_profile_version)
    interface["input_purposes"] = execution_input_purpose_options(resources, interface["input_targets"])
    return interface, source, label


@security_boundary_resolution_cache()
def execution_authoring_options(version_id):
    """Draft-only source catalog; not a partly valid published definition.

    A blank draft must be able to discover required ports before wiring them.
    Publication still passes through the complete builder and explicit SHA.
    """
    from studio.services.execution_input_catalog import execution_input_identity_options
    from studio.services.execution_prompts import AUXILIARY_OPERATIONS, profile_auxiliary_purposes

    version = execution_definition_version(version_id)
    if version.status != ModelExecutionInstanceVersion.Status.DRAFT:
        raise ValidationError("已发布/退役版本请查看冻结输入合同，不能读取当前作者目录替换历史配置", code="execution_definition_conflict")
    version.full_clean()
    resources = resolve_execution_resources(version)
    interface, _, _ = execution_resource_interface(ExecutionResourceSelection.from_version(version), resources)
    purposes = profile_auxiliary_purposes(version.prompt_profile_version) if version.prompt_mode == "profile" else []
    derived_keys = {row["key"] for row in derived_profile_inputs(version.prompt_profile_version)} if version.prompt_mode == "profile" else set()
    # A value explicitly reused by a physical port remains a combination choice.
    derived_keys -= {mapping.target_key for spec in version.inputs.all() for mapping in spec.targets.all()
        if mapping.target_kind == "profile_input" and any(target.target_kind != "profile_input" for target in spec.targets.all())}
    _verify_read_revision(version)
    return json_contract({
        "version_id": str(version.pk), "revision": version.revision, "resources": resources,
        "input_targets": interface["input_targets"], "profile_inputs": interface.get("profile_inputs", {}),
        "derived_input_keys": sorted(derived_keys),
        "input_purposes": interface["input_purposes"],
        "static_semantics": interface.get("static_semantics", {}),
        "parameters": interface["parameters"], "prompt_channels": interface["prompt_channels"],
        "prompt_not_required": interface.get("prompt_not_required", False),
        "auxiliary_purposes": [{"purpose": purpose, "operation": AUXILIARY_OPERATIONS[purpose]} for purpose in purposes],
        "identity_options": execution_input_identity_options(),
    }, max_bytes=execution_configuration_max_bytes()).value


@security_boundary_resolution_cache()
def build_execution_definition(version_id):
    """Complete configuration only; no writes, network, random seed or proof.

    Media Workflow, restricted REST and registered text protocols share exact
    source projection. Unsupported reference/purpose adapters remain explicit
    gaps rather than publishable partial contracts.

    Read-only callers must not compete for the registry write lock. Actual
    create/update/publish/dispatch writers hold that lock and revalidate the
    revision and digest before storing references.
    """
    version = execution_definition_version(version_id)
    version.full_clean()
    from studio.services.execution_output_contracts import execution_output_reference
    execution_output_reference(version, required=True)
    resources = resolve_execution_resources(version)
    target = execution_target_contract(deployment_id=version.deployment_id, workflow_version_id=version.workflow_version_id)
    auxiliaries = _complete_auxiliaries(version)
    interface, source, label = execution_resource_interface(ExecutionResourceSelection.from_version(version), resources)
    _complete_inputs(version, interface)
    from studio.services.execution_input_structures import freeze_input_structures
    input_structures = freeze_input_structures(version)
    from studio.services.execution_media_source_policy import execution_media_source_policy
    for spec in execution_version_configuration(version)["inputs"]:
        execution_media_source_policy(spec)
    if input_structures:
        interface["input_structures"] = input_structures
    from studio.services.execution_resource_parameters import has_system_output
    if has_system_output(interface) and version.parameters.filter(key="max_tokens").exists():
        raise ValidationError({"parameters": "输出长度只能在系统资源设置中配置，实例不能覆盖"})
    interface["parameters"] = restrict_execution_parameters(
        interface["parameters"], version.parameters.all(), source=f"instance.{version.pk}.parameters",
    )
    interface["prompt"] = validate_execution_prompt_channels(
        version, interface.pop("prompt_channels"), source=source, target_label=label,
        allowed_templates=version.workflow_version.input_contract.get("prompt_template_keys", []) if version.workflow_version_id else (),
        prompt_not_required=interface.pop("prompt_not_required", False),
    )
    interface["auxiliaries"] = auxiliaries
    # Resource/config changes between fresh reads cannot be silently frozen
    # together. Formal writers also hold the shared registry transaction lock.
    current = execution_target_contract(deployment_id=version.deployment_id, workflow_version_id=version.workflow_version_id)
    if current.sha256 != target.sha256:
        raise ValidationError("执行目标在合同核对期间变化，请重新读取", code="execution_target_drift")
    if resources["model_specification_sha256"] != target.value["model"]["specification_sha256"]:
        raise ValidationError("执行目标与模型资源快照不一致", code="execution_target_drift")
    definition = json_contract({
        "schema_version": DEFINITION_SCHEMA_VERSION, "version_id": str(version.pk),
        "configuration": execution_version_configuration(version), "resources": resources,
        "interface": interface, "target": target.value, "target_fingerprint": target.sha256,
    }, max_bytes=execution_configuration_max_bytes())
    _verify_read_revision(version)
    return definition


@security_boundary_resolution_cache()
def verify_stored_execution_definition(contract):
    """Check frozen integrity, not current target readiness or expiry."""
    frozen = verify_execution_definition_payload(JsonContract(value=contract.definition,
        schema_version=contract.schema_version, size_bytes=contract.size_bytes, sha256=contract.sha256))
    value = frozen.value
    if value["version_id"] != str(contract.version_id) or value["target_fingerprint"] != contract.target_fingerprint:
        raise ValidationError({"definition": "执行定义索引与冻结身份不一致"})
    version = execution_definition_version(contract.version_id)
    if value["configuration"] != execution_version_configuration(version):
        raise ValidationError("冻结定义与作者配置不一致", code="execution_definition_conflict")
    return frozen


@security_boundary_resolution_cache()
def verify_execution_definition_payload(contract):
    """Verify an immutable definition value without reading current resources.

    Registry/preparation adapters must additionally verify the exact stored
    version relationship before using a request as a submission authority.
    """
    if not isinstance(contract, JsonContract):
        raise ValidationError({"definition": "需要完整执行定义与摘要"})
    frozen = verify_json_contract(
        contract.value, schema_version=contract.schema_version, size_bytes=contract.size_bytes,
        sha256=contract.sha256, max_bytes=execution_configuration_max_bytes(),
    )
    value = frozen.value
    if contract.schema_version != DEFINITION_SCHEMA_VERSION or not isinstance(value, dict) or set(value) != _DEFINITION_KEYS or type(value.get("schema_version")) is not int or value["schema_version"] != DEFINITION_SCHEMA_VERSION:
        raise ValidationError({"definition": "执行定义结构不完整或版本不支持"})
    # Null is the explicit resource-only definition identity. The stored
    # instance verifier and preparation/root adapters still require an exact
    # matching instance FK for production/instance Smoke; this pure verifier
    # alone grants neither execution nor resource-purpose authorization.
    if (value["version_id"] is not None and (not isinstance(value["version_id"], str) or not value["version_id"])) or not all(isinstance(value[key], dict) for key in ("configuration", "resources", "interface", "target")):
        raise ValidationError({"definition": "执行定义对象或身份无效"})
    configuration, resources, interface, target = (value[key] for key in ("configuration", "resources", "interface", "target"))
    from studio.services.execution_input_structures import input_structure_schemas
    input_structure_schemas(value)
    from studio.services.execution_media_source_policy import execution_media_source_policy
    for spec in configuration["inputs"]:
        execution_media_source_policy(spec)
    if (configuration.get("output_version_id") is not None) != ("business_output" in interface):
        raise ValidationError("业务输出版本引用与冻结内容必须成对存在")
    if configuration.get("operation") in {"script.generate", "script.structure"} and not configuration.get("output_version_id"):
        raise ValidationError("剧本执行缺少确切输出版本，不读取旧固定协议")
    if configuration.get("output_version_id") is not None:
        from studio.services.execution_output_contracts import verify_operation_output
        output = verify_operation_output(configuration.get("operation"), interface.get("business_output"))
        if (output["id"] != configuration["output_version_id"]
                or (configuration.get("operation") == "visual_rules.generate" and (
                    output["parser"].get("operation") != configuration.get("operation")
                    or interface.get("native_policy", {}).get("quality_advisories", {}) != output["quality_advisories"]))):
            raise ValidationError("输出版本身份、能力或质量建议与冻结执行合同不一致")
    from studio.services.execution_output_contracts import STRUCTURED_TEXT_OPERATION, STRUCTURED_TEXT_ADAPTER
    if configuration.get("operation") == STRUCTURED_TEXT_OPERATION and (
            not configuration.get("output_version_id")
            or interface.get("output") != {"adapter": STRUCTURED_TEXT_ADAPTER, "kind": "text", "schema_version": 1}):
        raise ValidationError("通用结构化文本缺少确切输出合同或适配器不一致")
    if (
        not {"parameters", "input_targets", "input_purposes", "prompt", "output", "auxiliaries"} <= interface.keys()
        or not all(isinstance(interface[key], dict) for key in ("parameters", "input_targets", "input_purposes", "prompt", "output", "auxiliaries"))
        or not {"mode", "channels", "auxiliary_purposes"} <= interface["prompt"].keys()
        or not interface["output"]
        or not isinstance(target.get("model"), dict)
        or not isinstance(resources.get("execution"), dict)
        or resources.get("deployment_id") != configuration.get("deployment_id")
        or target.get("deployment_id") != configuration.get("deployment_id")
        or resources.get("model_profile_version_id") != target["model"].get("id")
        or resources.get("model_specification_sha256") != target["model"].get("specification_sha256")
        or resources["execution"] != {"operation": configuration.get("operation"), "mode": configuration.get("mode")}
        or interface["prompt"]["mode"] != configuration.get("prompt_mode")
    ):
        raise ValidationError({"definition": "执行定义缺少完整接口，或冻结资源身份相互矛盾"})
    if "conditioning_mode" not in resources or interface["input_purposes"] != execution_input_purpose_options(resources, interface["input_targets"]):
        raise ValidationError({"definition": "用途目录与冻结模型来源不一致"})
    if target.get("workflow") is not None:
        from studio.services.execution_monitoring import normalize_frozen_execution_monitoring_contract
        monitoring = interface.get("execution_monitoring")
        if not isinstance(monitoring, dict) or normalize_frozen_execution_monitoring_contract(monitoring) != monitoring:
            raise ValidationError({"definition": "工作流执行定义缺少规范化节点进度监测合同，不能从旧任务补齐"})
    from studio.services.execution_prompts import AUXILIARY_OPERATIONS

    auxiliary_refs = configuration.get("auxiliaries")
    if not isinstance(auxiliary_refs, list) or any(not isinstance(row, dict) or set(row) != {"purpose", "target_version_id"} for row in auxiliary_refs):
        raise ValidationError({"definition": "辅助作者配置无效"})
    if {row["purpose"] for row in auxiliary_refs} != set(interface["auxiliaries"]) or len(auxiliary_refs) != len(interface["auxiliaries"]):
        raise ValidationError({"definition": "辅助合同与作者用途不一致"})
    for row in auxiliary_refs:
        identity = interface["auxiliaries"][row["purpose"]]
        if (not isinstance(identity, dict) or set(identity) != {"version_id", "definition_sha256", "target_fingerprint", "operation"}
            or identity["version_id"] != row["target_version_id"]
            or identity["operation"] != AUXILIARY_OPERATIONS.get(row["purpose"])
            or any(not isinstance(identity[key], str) or re.fullmatch(r"[a-f0-9]{64}", identity[key]) is None for key in ("definition_sha256", "target_fingerprint"))):
            raise ValidationError({"definition": "辅助版本身份或摘要无效"})
    fingerprint = value["target_fingerprint"]
    if not isinstance(fingerprint, str) or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None or json_contract(value["target"], max_bytes=execution_configuration_max_bytes()).sha256 != fingerprint:
        raise ValidationError({"definition": "执行目标摘要与冻结正文不一致"})
    return frozen


@security_boundary_resolution_cache()
def read_execution_input_contract(version_id):
    """Draft projection or immutable published detail, never a readiness flag."""
    from studio.services.execution_input_catalog import execution_context_field_options, execution_input_form_options, execution_language_form_options, execution_subject_category_options
    from studio.services.media_appearance_input_catalog import media_appearance_input_options
    version = execution_definition_version(version_id)
    frozen = version.status != ModelExecutionInstanceVersion.Status.DRAFT
    if frozen:
        contract = stored_execution_contract(version.pk)
        definition = verify_stored_execution_definition(contract)
    else:
        definition = build_execution_definition(version_id)
    return {
        "version_id": str(version.pk), "revision": version.revision, "frozen": frozen,
        "schema_version": definition.schema_version, "size_bytes": definition.size_bytes,
        "sha256": definition.sha256, "target_fingerprint": definition.value["target_fingerprint"],
        "definition": definition.value, "context_fields": execution_context_field_options(),
        "input_forms": execution_input_form_options(definition.value),
        "appearance_inputs": media_appearance_input_options(definition.value),
        "language_form": execution_language_form_options(definition.value),
        "subject_categories": execution_subject_category_options(),
    }


@security_boundary_resolution_cache()
def verify_current_execution_definition(contract):
    """Detect drift without rewriting the immutable record or creating proof."""
    frozen = verify_stored_execution_definition(contract)
    current = build_execution_definition(contract.version_id)
    if current.value["target_fingerprint"] != contract.target_fingerprint:
        raise ValidationError("执行目标已变化，请创建后继版本", code="execution_target_drift")
    if current.sha256 != frozen.sha256:
        raise ValidationError("当前完整合同与发布定义不一致，请创建后继版本", code="execution_definition_conflict")
    return frozen
