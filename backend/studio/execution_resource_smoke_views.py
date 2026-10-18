"""Resource Smoke at the original resource URLs and original attempt owners.

The URL owns Deployment/Workflow identity. Callers may select an operation and
explicit prompt source, never a purpose, instance, Binding or transport body.
"""
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import IntegrityError
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from studio.exceptions import api_error_response
from studio.execution_instance_serializers import ExecutionSelectionOptionsSerializer
from studio.execution_smoke_serializers import (
    ResourceSmokeSelectionSerializer, ResourceSmokeContractSerializer,
    ResourceFieldSmokeSourceQuerySerializer, ResourceFieldSmokeSourceSerializer,
    ResourceSmokeCreateSerializer, ExecutionSmokeCreatedSerializer,
)
from studio.models import BusinessOutputVersion, ModelDeployment, PromptProfileVersion, WorkflowTemplateVersion
from studio.platform_serializers import ConfigurationErrorResponseSerializer
from studio.services.execution_instances import ExecutionResourceSelection, execution_selection_options
from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
from studio.services.execution_input_catalog import execution_context_field_options, execution_input_form_options, execution_language_form_options, execution_subject_category_options
from studio.services.registry_reference_lock import registry_reference_writer


def resource_smoke_owner(owner):
    if isinstance(owner, ModelDeployment):
        deployment = ModelDeployment.objects.select_related("profile_version__provider", "runtime_host").get(pk=owner.pk)
        if deployment.provider.driver_type == "comfyui":
            raise DjangoValidationError("ComfyUI基础烟测请从具体工作流版本进入，不从部署猜测工作流")
        return deployment, None
    if isinstance(owner, WorkflowTemplateVersion):
        workflow = WorkflowTemplateVersion.objects.select_related("template").get(pk=owner.pk)
        if workflow.validated_deployment_id is None:
            raise DjangoValidationError("工作流尚未绑定在线校验部署，请先完成Live Validate")
        deployment = ModelDeployment.objects.select_related("profile_version__provider", "runtime_host").get(pk=workflow.validated_deployment_id)
        return deployment, workflow
    raise DjangoValidationError("基础烟测需要明确的模型部署或工作流版本")


def resource_smoke_selection(owner, data):
    deployment, workflow = resource_smoke_owner(owner)
    profile = PromptProfileVersion.objects.get(pk=data["prompt_profile_version_id"]) if data["prompt_profile_version_id"] else None
    output = BusinessOutputVersion.objects.select_related("data_structure_version").get(pk=data["output_version_id"]) if data.get("output_version_id") else None
    return ExecutionResourceSelection(deployment=deployment, workflow_version=workflow,
        operation=data["operation"], mode=data["mode"], prompt_mode=data["prompt_mode"], prompt_profile_version=profile, output_version=output)


class ResourceSmokeMixin:
    def _resource_smoke_response(self, reader):
        try:
            # Keep one registry read transaction while resolving the URL owner,
            # published Profile and full model/Workflow input definition.
            return registry_reference_writer(reader)()
        except ObjectDoesNotExist:
            return api_error_response(status_code=404, code="not_found", details="所选资源或策略版本已不存在")
        except IntegrityError:
            return api_error_response(status_code=409, code="execution_definition_conflict", details="烟测对象发生并发变化，请核对原任务再继续")
        except DjangoValidationError as exc:
            code = getattr(exc, "code", None) or "invalid"
            return api_error_response(status_code=409 if code in {"execution_definition_conflict", "execution_target_drift", "execution_preparation_conflict"} else 400,
                code=code, details=getattr(exc, "message_dict", None) or exc.messages)

    @extend_schema(responses=ExecutionSelectionOptionsSerializer)
    @action(detail=True, methods=("get",), url_path="smoke-options", permission_classes=(IsAdminUser,))
    def smoke_options(self, request, pk=None):
        def read():
            if request.query_params:
                raise DjangoValidationError("基础烟测选项从当前对象读取，不接受额外查询参数")
            deployment, workflow = resource_smoke_owner(self.get_object())
            choices = execution_selection_options(deployment=deployment, workflow=workflow, resource_smoke=True)
            if workflow is not None:
                choices["prompt_modes"] = [row for row in choices["prompt_modes"] if row["value"] != "profile"]
            return Response(ExecutionSelectionOptionsSerializer(choices).data)
        return self._resource_smoke_response(read)

    @extend_schema(request=ResourceSmokeSelectionSerializer, responses=ResourceSmokeContractSerializer)
    @action(detail=True, methods=("post",), url_path="smoke-contract", permission_classes=(IsAdminUser,))
    def smoke_contract(self, request, pk=None):
        def read():
            serializer = ResourceSmokeSelectionSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            selection = resource_smoke_selection(self.get_object(), serializer.validated_data)
            definition = build_resource_smoke_definition(selection)
            return Response(ResourceSmokeContractSerializer({"scope": "resource_smoke", "version_id": None,
                "schema_version": definition.schema_version, "size_bytes": definition.size_bytes, "sha256": definition.sha256,
                "target_fingerprint": definition.value["target_fingerprint"], "definition": definition.value,
                "context_fields": execution_context_field_options(), "input_forms": execution_input_form_options(definition.value),
                "language_form": execution_language_form_options(definition.value),
                "subject_categories": execution_subject_category_options()}).data)
        return self._resource_smoke_response(read)

    @extend_schema(request=ResourceFieldSmokeSourceQuerySerializer, responses=ResourceFieldSmokeSourceSerializer)
    @action(detail=True, methods=("post",), url_path="smoke-field-source", permission_classes=(IsAdminUser,))
    def smoke_field_source(self, request, pk=None):
        from studio.services.execution_field_smoke import resource_field_smoke_source, build_resource_field_smoke_source
        def read():
            serializer = ResourceFieldSmokeSourceQuerySerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = dict(serializer.validated_data)
            selection = resource_smoke_selection(self.get_object(), data.pop("selection"))
            reader = build_resource_field_smoke_source if "items" in data else resource_field_smoke_source
            result = reader(selection=selection, actor=request.user, **data)
            return Response(ResourceFieldSmokeSourceSerializer(result).data)
        return self._resource_smoke_response(read)

    @extend_schema(request=ResourceSmokeCreateSerializer,
        parameters=[OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True)],
        responses={200: ExecutionSmokeCreatedSerializer, 202: ExecutionSmokeCreatedSerializer,
            400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
            404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="smoke-test", permission_classes=(IsAdminUser,))
    def smoke_test(self, request, pk=None):
        from rest_framework.reverse import reverse
        from studio.services.execution_smoke import create_resource_smoke
        def create():
            serializer = ResourceSmokeCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = dict(serializer.validated_data)
            selection = resource_smoke_selection(self.get_object(), data.pop("selection"))
            attempt, preparation, created = create_resource_smoke(selection=selection, actor=request.user,
                client_idempotency_key=request.headers.get("Idempotency-Key", ""), **data)
            response = ExecutionSmokeCreatedSerializer({"attempt_kind": "workflow_smoke" if selection.workflow_version_id else "deployment_smoke",
                "attempt_id": attempt.pk, "status": attempt.status, "preparation": preparation})
            return Response(response.data, status=202 if created else 200,
                headers={"Location": reverse("execution-preparation-detail", args=[preparation.pk], request=request)})
        return self._resource_smoke_response(create)
