"""Resource-only definitions projected through the existing pure interfaces.

No instance/version allocation, Binding lookup, request defaults, field values,
admission or queue mutation. The registered Smoke action must later own and
freeze this definition; null version identity cannot authorize production.
"""
from copy import deepcopy

from django.core.exceptions import ObjectDoesNotExist, ValidationError

from studio.models import BusinessOutputVersion, ModelDeployment, ModelDeploymentSmokeAttempt, PromptProfileVersion, WorkflowSmokeAttempt, WorkflowTemplateVersion

from studio.services.execution_definitions import (
    DEFINITION_SCHEMA_VERSION, execution_resource_interface, verify_execution_definition_payload,
)
from studio.services.execution_identity import execution_target_contract
from studio.services.execution_input_catalog import EXECUTION_SUBJECT_KINDS, execution_context_field_options
from studio.services.execution_instances import ExecutionResourceSelection, execution_configuration_max_bytes, resolve_execution_resources
from studio.services.execution_prompts import validate_execution_prompt_channels
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache


def _input_key(kind, key):
    # Structural identity, not model semantics or a guessed source. Distinct
    # strategy/executor ports with equal labels remain different inputs.
    return "resource_" + json_contract({"kind": kind, "key": key}, max_bytes=execution_configuration_max_bytes()).sha256


def resource_smoke_input_specs(interface):
    """Expose each real resource port once, without an instance mapping layer."""
    result = []
    groups = [(False, interface["input_targets"]), (True, interface.get("profile_inputs", {}))]
    for profile, targets in groups:
        for key, target in sorted(targets.items()):
            kind = "profile_input" if profile else target["target_kind"]
            if (target["target_key"] != key or kind not in {"operation_input", "workflow_slot", "profile_input"}
                    or type(target["min_items"]) is not int or type(target["max_items"]) is not int
                    or not 0 <= target["min_items"] <= target["max_items"] or type(target["required"]) is not bool):
                raise ValidationError("资源端口缺少完整的身份、类型或数量合同")
            scopes = ["request"]
            if target.get("source_kinds") == ["context_field"] or (
                    target.get("source_kinds") and "literal" not in target["source_kinds"]):
                scopes = sorted({row["scope_kind"] for row in execution_context_field_options()
                    if row["selector_key"] in target.get("context_selectors", []) and row["kind"] == target["kind"]})
                if not scopes:
                    raise ValidationError("资源端口要求受控上下文但没有已登记来源，不以样例代替")
            parent = target.get("wiring", {}).get("index_source_target_key")
            if parent is not None and (kind != "workflow_slot" or parent not in interface["input_targets"]):
                raise ValidationError("资源素材端口缺少明确的配对父目标")
            media = target["kind"] in {"image", "video", "audio"}
            result.append({"key": _input_key(kind, key), "position": len(result), "kind": target["kind"],
                "required": target["required"], "min_items": target["min_items"], "max_items": target["max_items"],
                "semantic_contract": {"schema_version": 1,
                    "allowed_purposes": [row["value"] for row in interface["input_purposes"].get(target["kind"], [])] if media else [],
                    "allowed_subject_kinds": list(EXECUTION_SUBJECT_KINDS) if media else [],
                    "allowed_scope_kinds": scopes,
                    "paired_with_input_key": _input_key("workflow_slot", parent) if parent is not None else None,
                    "allow_repeated_asset": media},
                "targets": [{"target_kind": kind, "target_key": key, "is_primary": not profile}]})
    return result


@security_boundary_resolution_cache()
def build_resource_smoke_definition(selection: ExecutionResourceSelection):
    """Complete resource ports/channels; no fabricated production identity.

    Profile fields may be explicitly supplied/reviewed through the shared
    field store. Resource Smoke does not invent auxiliary instance bindings
    or call an AI to complete a fixture. Missing facts stay missing.

    This also runs on acceptance GETs: do not acquire a write transaction.
    Target fingerprints are checked again below; the actual Smoke/reference
    writers own the registry guard and revalidate before persisting.
    """
    if not isinstance(selection, ExecutionResourceSelection):
        raise ValidationError("基础资源定义需要明确的资源选择")
    if selection.workflow_version_id and selection.prompt_mode == "profile":
        raise ValidationError("基础图Smoke使用明确fixture，不隐含策略关联")
    resources = resolve_execution_resources(selection, resource_smoke=True)
    target = execution_target_contract(deployment_id=selection.deployment_id, workflow_version_id=selection.workflow_version_id)
    interface, source, label = execution_resource_interface(selection, resources)
    inputs = resource_smoke_input_specs(interface)
    interface["prompt"] = validate_execution_prompt_channels(selection, interface.pop("prompt_channels"),
        source=source, target_label=label,
        allowed_templates=selection.workflow_version.input_contract.get("prompt_template_keys", []) if selection.workflow_version_id else (),
        prompt_not_required=interface.pop("prompt_not_required", False))
    interface["auxiliaries"] = {}
    configuration = {"deployment_id": str(selection.deployment_id), "operation": selection.operation, "mode": selection.mode,
        **({"output_version_id": str(selection.output_version_id)} if selection.output_version_id else {}),
        "prompt_mode": selection.prompt_mode,
        "prompt_profile_version_id": str(selection.prompt_profile_version_id) if selection.prompt_profile_version_id else None,
        "workflow_version_id": str(selection.workflow_version_id) if selection.workflow_version_id else None,
        "inputs": inputs, "parameters": [], "auxiliaries": []}
    current = execution_target_contract(deployment_id=selection.deployment_id, workflow_version_id=selection.workflow_version_id)
    if current.sha256 != target.sha256 or resources["model_specification_sha256"] != target.value["model"]["specification_sha256"]:
        raise ValidationError("资源验收目标在读取期间变化，请重新读取", code="execution_target_drift")
    return verify_execution_definition_payload(json_contract({"schema_version": DEFINITION_SCHEMA_VERSION, "version_id": None,
        "configuration": configuration, "resources": deepcopy(resources), "interface": interface,
        "target": target.value, "target_fingerprint": target.sha256}, max_bytes=execution_configuration_max_bytes()))


def load_resource_smoke_selection(attempt, definition):
    """Load exact referenced rows after the original root definition was verified."""
    config = definition.value["configuration"]
    try:
        return ExecutionResourceSelection(
            deployment=ModelDeployment.objects.select_related("profile_version__provider", "runtime_host").get(pk=attempt.deployment_id),
            operation=config["operation"], mode=config["mode"], prompt_mode=config["prompt_mode"],
            prompt_profile_version=PromptProfileVersion.objects.get(pk=config["prompt_profile_version_id"]) if config["prompt_profile_version_id"] else None,
            output_version=BusinessOutputVersion.objects.select_related("data_structure_version").get(pk=config["output_version_id"])
                if config.get("output_version_id") else None,
            workflow_version=WorkflowTemplateVersion.objects.select_related("template").get(pk=attempt.workflow_version_id)
                if isinstance(attempt, WorkflowSmokeAttempt) else None)
    except ObjectDoesNotExist as exc:
        raise ValidationError("资源Smoke引用已不存在，不能更换为当前版本") from exc


def read_resource_smoke_definition(attempt, *, current=False):
    """Read only the exact original resource root; never interpret old Binding snapshots."""
    if (not isinstance(attempt, (ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt))
            or attempt.scope != "resource" or attempt.execution_instance_version_id is not None):
        raise ValidationError("基础资源定义只能归属原基础Smoke任务，不能授权实例组合或生产")
    if isinstance(attempt, ModelDeploymentSmokeAttempt):
        snapshot, schema_version = attempt.request_snapshot, 2
    else:
        if attempt.fixture_values_schema_version != 3:
            raise ValidationError("基础图Smoke需要当前资源准备快照版本")
        snapshot = verify_json_contract(attempt.fixture_values, schema_version=attempt.fixture_values_schema_version,
            size_bytes=attempt.fixture_values_size_bytes, sha256=attempt.fixture_values_sha256,
            max_bytes=execution_configuration_max_bytes()).value
        schema_version = 3
    if (not isinstance(snapshot, dict) or set(snapshot) != {"schema_version", "resource_definition"}
            or type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != schema_version):
        raise ValidationError("资源Smoke缺少当前精确资源定义，不读取旧绑定或补造快照")
    envelope = snapshot["resource_definition"]
    if not isinstance(envelope, dict) or set(envelope) != {"value", "schema_version", "size_bytes", "sha256"}:
        raise ValidationError("资源Smoke定义缺少完整正文与摘要")
    definition = verify_execution_definition_payload(JsonContract(**envelope))
    config, target = definition.value["configuration"], definition.value["target"]
    required = {"deployment_id", "operation", "mode", "prompt_mode", "prompt_profile_version_id", "workflow_version_id",
        "inputs", "parameters", "auxiliaries"}
    if "output_version_id" in config:
        required.add("output_version_id")
    profile = definition.value["resources"].get("prompt_profile")
    workflow = target.get("workflow")
    if (set(config) != required or not all(isinstance(config[key], str) for key in ("deployment_id", "operation", "mode", "prompt_mode"))
            or config["prompt_mode"] not in {"profile", "manual", "none"}
            or not isinstance(config["inputs"], list) or config["parameters"] != [] or config["auxiliaries"] != []
            or (workflow is not None and (not isinstance(workflow, dict) or not {"id", "execution_contract_sha256"} <= workflow.keys()))):
        raise ValidationError("基础资源定义缺少明确配置或混入了实例映射规则")
    if config["prompt_mode"] == "profile":
        if (not isinstance(profile, dict) or not {"id", "contract_sha256"} <= profile.keys()
                or not config["prompt_profile_version_id"] or profile["id"] != config["prompt_profile_version_id"]):
            raise ValidationError("基础资源策略身份与冻结配置不一致")
    elif config["prompt_profile_version_id"] is not None or profile is not None:
        raise ValidationError("人工或无提示词模式不能隐含策略关联")
    workflow_id = str(attempt.workflow_version_id) if isinstance(attempt, WorkflowSmokeAttempt) else None
    if (definition.value["version_id"] is not None or config["deployment_id"] != str(attempt.deployment_id)
            or definition.value["target_fingerprint"] != attempt.target_sha256
            or config["workflow_version_id"] != workflow_id
            or (workflow["id"] if workflow else None) != workflow_id):
        raise ValidationError("资源定义与Smoke的实际目标或范围不一致")
    if isinstance(attempt, ModelDeploymentSmokeAttempt):
        profile_id = str(attempt.prompt_profile_version_id) if attempt.prompt_profile_version_id else None
        if (config["operation"] != attempt.operation or config["prompt_profile_version_id"] != profile_id
                or (profile["contract_sha256"] if profile else "") != attempt.prompt_profile_contract_sha256):
            raise ValidationError("基础文本Smoke与冻结能力或精确策略不一致")
    elif config["prompt_mode"] == "profile" or config["prompt_profile_version_id"] is not None:
        raise ValidationError("基础图Smoke使用明确fixture，不隐含策略关联")
    elif workflow["execution_contract_sha256"] != attempt.execution_contract_sha256:
        raise ValidationError("基础图Smoke与冻结工作流执行合同不一致")
    if current:
        selection = load_resource_smoke_selection(attempt, definition)
        if workflow_id and (selection.workflow_version.graph_sha256 != attempt.graph_sha256
                or selection.workflow_version.validation_evidence_sha256 != attempt.validation_evidence_sha256):
            raise ValidationError("基础图Smoke的图或在线校验证据已变化，请重新验收", code="execution_target_drift")
        if build_resource_smoke_definition(selection).sha256 != definition.sha256:
            raise ValidationError("资源Smoke定义已漂移，不能替换原请求", code="execution_target_drift")
    return definition
