"""Join saved preparation facts and explicit field choices into a request preview.

This is the input to finalization, not the finalization/admission decision.
It does not save prepared_request, mark ready, or create another task queue.
"""
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from studio.services.execution_preparation_fields import _locked_preparations, read_execution_field_selection_snapshot
from studio.services.execution_preparations import read_execution_preparation_detail, preparation_execution_definition
from studio.services.execution_profile_preparation import compile_preparation_profile
from studio.services.execution_request_compilation import compile_execution_transport_request
from studio.services.json_contracts import JsonContract, json_contract
from studio.services.execution_instances import execution_resolved_inputs_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.security_boundaries import security_boundary_resolution_cache


@dataclass(frozen=True)
class PreparedExecutionTransportPreview:
    preparation_id: str
    preparation_revision: int
    intent_sha256: str
    transport: JsonContract
    field_selections: JsonContract


def _revision(preparation, expected):
    if type(expected) is not int or expected < 1:
        raise ValidationError("必须提供明确的准备修订号")
    if preparation.revision != expected:
        raise ValidationError("准备已变化，请重新读取", code="execution_preparation_conflict")


@security_boundary_resolution_cache()
def compile_prepared_execution_transport(*, preparation_id, expected_preparation_revision, actor):
    """Compile from the original intent; never read new sources or defaults.

    Cross-preparation field reads acquire all source roots in stable order in
    the shared selector. Do not hold the target preparation lock while asking
    that selector to lock another root. The final revision check detects an
    intervening field write; finalization must recheck the returned revision.
    """
    with _locked_preparations([preparation_id], actor) as rows:
        preparation = rows[str(preparation_id)]
        _revision(preparation, expected_preparation_revision)
        detail = read_execution_preparation_detail(preparation)
        requested = detail.intent["request"].get("requested")
        parameters = detail.resolved_inputs.get("parameters")
        if not isinstance(requested, dict) or not isinstance(requested.get("parameters"), dict) or not isinstance(parameters, dict):
            raise ValidationError("准备缺少明确的原始参数请求与解析参数")
        if not isinstance(parameters.get("values"), dict) or not isinstance(parameters.get("sources"), dict):
            raise ValidationError("准备缺少参数值和来源")
        original = {key: value for key, value in parameters["values"].items() if parameters["sources"].get(key) == "request"}
        if original != requested["parameters"]:
            raise ValidationError("解析参数与当时提交的原始值不一致")
    # Do not nest a registry lock under a root/preparation lock. Published
    # identity is immutable; the finalizer rechecks this preview's revision.
    definition = preparation_execution_definition(preparation, detail=detail)
    if detail.intent["request"].get("definition_sha256") != definition.sha256:
        raise ValidationError("准备意图与精确实例定义不一致")
    mode = definition.value["configuration"]["prompt_mode"]
    # Both readers are side-effect free; sources and explicit selection facts
    # are immutable. A field write still invalidates the preview's revision.
    compilation = None
    if mode == "profile":
        if "manual_channels" in requested and requested["manual_channels"] != {}:
            raise ValidationError("策略准备不能混入另一份人工提示词")
        result = compile_preparation_profile(preparation_id=preparation_id,
            expected_preparation_revision=expected_preparation_revision, actor=actor)
        compilation, channels = result.compilation, {}
    else:
        if detail.resolved_inputs.get("field_requests") != []:
            raise ValidationError("人工或无提示词主准备不能隐含策略字段批次")
        channels = requested.get("manual_channels")
        if not isinstance(channels, dict):
            raise ValidationError("人工或无提示词准备必须明确保存通道对象，不读取工作流示例")
    selected = read_execution_field_selection_snapshot(preparation_id=preparation_id,
        expected_preparation_revision=expected_preparation_revision, actor=actor)
    if compilation is not None and result.field_selection_sha256 != selected.sha256:
        raise ValidationError("策略编译与本次字段采用事实不一致", code="execution_preparation_conflict")
    workflow_arguments = {}
    if definition.value["target"]["workflow"] is not None:
        from studio.models import WorkflowTemplateVersion
        from studio.services.workflow_versions import workflow_version_execution_snapshot
        workflow = WorkflowTemplateVersion.objects.select_related("template", "model_profile_version").get(
            pk=definition.value["target"]["workflow"]["id"])
        duration = parameters["values"].get("video.duration_ms")
        if "reference_context" in detail.resolved_inputs.get("profile_sources", {}):
            from studio.services.reference_context_facts import read_reference_context_facts
            context = read_reference_context_facts(JsonContract(**detail.resolved_inputs["profile_sources"]["reference_context"]),
                max_bytes=execution_request_max_bytes())
            duration = sum(unit.duration_ms for unit in context.ordered_units)
        if not isinstance(detail.resolved_inputs.get("inputs"), dict):
            raise ValidationError("工作流准备缺少原始解析输入，不能重读当前来源补齐")
        workflow_arguments = {"workflow_snapshot": workflow_version_execution_snapshot(workflow), "total_duration_ms": duration,
            "workflow_inputs": json_contract(detail.resolved_inputs["inputs"], max_bytes=execution_resolved_inputs_max_bytes())}
    from studio.services.execution_auxiliary_requests import read_preparation_operation_field_request
    field_request = read_preparation_operation_field_request(preparation, definition, detail)
    from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS, native_text_source_reference
    if (definition.value["configuration"]["operation"] in NATIVE_TEXT_OPERATIONS
            and definition.value["target"]["workflow"] is None):
        workflow_arguments["operation_inputs"] = detail.resolved_inputs["inputs"]["operation_inputs"]
        workflow_arguments["operation_source_reference"] = native_text_source_reference(definition.value, detail.resolved_inputs["inputs"])
    transport = compile_execution_transport_request(definition, prepared_parameters=parameters,
        manual_channels=channels, idempotency_key=str(preparation.pk), profile_compilation=compilation,
        field_request=field_request, **workflow_arguments)
    with _locked_preparations([preparation_id], actor) as rows:
        current = rows[str(preparation_id)]
        _revision(current, expected_preparation_revision)
        if current.intent_sha256 != preparation.intent_sha256:
            raise ValidationError("准备意图在读取期间变化", code="execution_preparation_conflict")
    return PreparedExecutionTransportPreview(str(preparation.pk), preparation.revision, preparation.intent_sha256, transport, selected)
