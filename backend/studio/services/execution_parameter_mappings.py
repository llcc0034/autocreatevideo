"""Read-only physical parameter destinations, proved against one definition.

This is not a compiler, readiness check or historical wire-request replay.
Never expose transport configuration, secrets, URLs, or graph widget values.
"""
from django.core.exceptions import ObjectDoesNotExist, ValidationError

from studio.models import ModelDeployment, WorkflowTemplateVersion
from studio.services.execution_definitions import read_execution_input_contract
from studio.services.provider_registry import deployment_execution_config_contract, ProviderDriverError
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_versions import verify_workflow_version_integrity


class ApiMappingUnavailable(ValidationError):
    """Safe, bounded public explanation; never carries private validation text."""


@security_boundary_resolution_cache()
def read_async_field_map(target):
    """Read one exact deployment only if its transport digest still matches.

    Shared by instance configuration and task history. This proves a declaration,
    not historical submission or provider receipt. No secret is resolved.
    """
    try:
        deployment = ModelDeployment.objects.select_related("profile_version__provider").only(
            "id", "profile_version_id", "runtime_config", "runtime_config_schema_version",
            "runtime_config_size_bytes", "runtime_config_sha256", "profile_version__provider_id",
            "profile_version__provider__driver_type", "profile_version__provider__driver_config",
            "profile_version__provider__driver_config_schema_version",
            "profile_version__provider__driver_config_size_bytes",
            "profile_version__provider__driver_config_sha256",
        ).get(pk=target["deployment_id"])
        configuration = deployment_execution_config_contract(deployment)
    except ObjectDoesNotExist:
        raise ApiMappingUnavailable("映射所依赖的精确部署已不存在，不能按名称选择替代资源。", code="mapping_resource_missing") from None
    except (ValidationError, ProviderDriverError):
        raise ApiMappingUnavailable("映射依赖的资源完整性或配置校验未通过，请到原资源管理入口核对。", code="mapping_evidence_invalid") from None
    if (target["driver_type"] != "async_rest" or deployment.provider.driver_type != "async_rest"
            or configuration.sha256 != target["configuration_sha256"]):
        raise ApiMappingUnavailable("部署传输配置已变化，未保存旧映射正文；不能用当前 API 字段补造该版本的映射。", code="deployment_mapping_changed")
    return configuration.value["request_field_map"]


@security_boundary_resolution_cache()
def execution_parameter_mappings(version_id, *, expected_definition_sha256):
    contract = read_execution_input_contract(version_id)
    if contract["sha256"] != expected_definition_sha256:
        raise ValidationError("实例合同已变化，请重新复核后查看映射", code="execution_definition_conflict")
    definition = contract["definition"]
    interface, target = definition["interface"], definition["target"]
    result = {
        "version_id": contract["version_id"], "revision": contract["revision"],
        "definition_sha256": contract["sha256"], "status": "not_recorded",
        "code": "adapter_mapping_not_recorded",
        "message": "此适配器未记录可独立核验的物理参数映射；请在任务中心核对该次实际请求。不能按参数同名推断。",
        "basis_kind": "none", "basis_id": None, "basis_sha256": None, "rows": [],
    }
    keys = sorted(interface["parameters"])
    workflow_ref = target.get("workflow")
    try:
        if workflow_ref is not None:
            result.update(basis_kind="workflow", basis_id=workflow_ref["id"],
                          basis_sha256=workflow_ref["execution_contract_sha256"])
            workflow = WorkflowTemplateVersion.objects.select_related(
                "template", "model_profile_version", "provider",
            ).prefetch_related("semantic_bindings", "model_requirements").get(pk=workflow_ref["id"])
            verify_workflow_version_integrity(workflow)
            if workflow.execution_contract_sha256 != result["basis_sha256"]:
                return _unavailable(result, "workflow_mapping_changed", "工作流合同与实例记录不一致，不能用变化后的接线解释该版本。")
            bindings = list(workflow.semantic_bindings.all())
            for key in keys:
                destinations = [{"node_id": row.node_id, "field": row.input_name}
                                for row in bindings if row.semantic_key == key]
                result["rows"].append({"key": key, "destinations": sorted(destinations, key=lambda row: (row["node_id"], row["field"]))})
            result.update(status="verified", code="workflow_bindings_verified",
                message="已核对精确工作流及接线摘要。以下为编译前的声明节点输入；尺寸/帧策略和动态节点展开仍由冻结编译器处理，最终发送值请看任务中心实际请求。维护位置：工作流版本的语义接线；实例只引用该版本。")
        elif target["driver_type"] == "async_rest":
            result.update(basis_kind="deployment", basis_id=target["deployment_id"],
                          basis_sha256=target["configuration_sha256"])
            mapping = read_async_field_map(target)
            result["rows"] = [{"key": key, "destinations": [{"node_id": None, "field": mapping[key]}] if key in mapping else []} for key in keys]
            result.update(status="verified", code="api_fields_verified",
                message="当前部署传输配置摘要与该实例记录一致。以下为 request_field_map 声明的 JSON 顶层字段；未提供或值为 null 的字段不会发送。维护位置：模型部署的传输配置（覆盖服务连接配置）；本页不复制一份配置。")
    except ApiMappingUnavailable as exc:
        return _unavailable(result, exc.code, exc.messages[0])
    except ObjectDoesNotExist:
        return _unavailable(result, "mapping_resource_missing", "映射所依赖的精确资源已不存在，不能按名称选择替代资源。")
    except ValidationError:
        # Provider/graph validation may contain private paths or endpoint text.
        return _unavailable(result, "mapping_evidence_invalid", "映射依赖的资源完整性或配置校验未通过，请到原资源管理入口核对；未展示未经核验的映射。")
    return result


def _unavailable(result, code, message):
    return {**result, "status": "unavailable", "code": code, "message": message, "rows": []}
