"""Administrative authoring and explicit freezing; no execution or admission."""
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import IntegrityError
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from studio.exceptions import api_error_response
from studio.execution_instance_serializers import (
    ExecutionCloneSerializer, ExecutionInstanceCreateSerializer, ExecutionInstanceSerializer,
    ExecutionInstanceUpdateSerializer, ExecutionVersionCreateSerializer,
    ExecutionVersionDetailSerializer, ExecutionVersionSerializer, ExecutionVersionUpdateSerializer,
    ExecutionInputContractSerializer, ExecutionPublishSerializer, ExecutionRetireSerializer, ExecutionAuthoringOptionsSerializer,
    ExecutionSelectionQuerySerializer, ExecutionSelectionOptionsSerializer,
    ExecutionInputSuggestionQuerySerializer, ExecutionInputSuggestionSerializer,
    ExecutionParameterMappingQuerySerializer, ExecutionParameterMappingsSerializer,
)
from studio.models import ModelExecutionInstance, ModelExecutionInstanceVersion
from studio.pagination import StandardPageNumberPagination, AcceptanceSmokePagination
from studio.platform_serializers import ConfigurationErrorResponseSerializer
from studio.services.execution_instances import (
    clone_execution_draft, create_execution_draft, create_execution_instance,
    update_execution_draft, update_execution_instance,
    publish_execution_version, retire_execution_version,
    execution_selection_options, selectable_execution_versions,
)
from studio.services.execution_definitions import read_execution_input_contract, execution_authoring_options
from studio.execution_smoke_serializers import (
    ExecutionSmokeCreateSerializer, ExecutionSmokeCreatedSerializer, ExecutionSmokeEvidenceQuerySerializer,
    ExecutionSmokeEvidenceSerializer,
    ExecutionFieldSmokeSourceQuerySerializer, ExecutionFieldSmokeSourceCreateSerializer, ExecutionFieldSmokeSourceSerializer,
    SmokeSourceContextQuerySerializer, SmokeSourceContextPageSerializer,
)
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_smoke_queries import instance_smoke_history, project_instance_smoke_history, instance_smoke_summary


class ExecutionRegistryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    http_method_names = ("get", "post", "patch", "head", "options")

    def handle_exception(self, exc):
        if isinstance(exc, DjangoValidationError):
            code = getattr(exc, "code", None) or "invalid"
            return api_error_response(
                status_code=409 if code in {"execution_definition_conflict", "execution_target_drift", "execution_preparation_conflict"} else 400,
                code=code, details=getattr(exc, "message_dict", None) or exc.messages,
            )
        if isinstance(exc, ObjectDoesNotExist):
            return api_error_response(status_code=404, code="not_found", details="配置对象不存在或已删除，请刷新后重试")
        if isinstance(exc, IntegrityError):
            # All authoring writes use one atomic registry service. Do not leak
            # database names/values and do not retry a possibly stale request.
            return api_error_response(status_code=409, code="execution_definition_conflict", details="配置发生并发冲突，请重新读取后再保存")
        return super().handle_exception(exc)


class ModelExecutionInstanceViewSet(ExecutionRegistryViewSet):
    serializer_class = ExecutionInstanceSerializer
    queryset = ModelExecutionInstance.objects.all().order_by("key")
    filterset_fields = ("is_active",)
    search_fields = ("key", "name", "description")

    @extend_schema(request=ExecutionInstanceCreateSerializer, responses={201: ExecutionInstanceSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    def create(self, request):
        serializer = ExecutionInstanceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = create_execution_instance(**serializer.validated_data, actor=request.user)
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ExecutionInstanceUpdateSerializer, responses={200: ExecutionInstanceSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    def partial_update(self, request, pk=None):
        instance = self.get_object()
        serializer = ExecutionInstanceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        updated = update_execution_instance(
            instance_id=instance.pk, expected_revision=data.pop("expected_revision"), changes=data, actor=request.user,
        )
        return Response(self.get_serializer(updated).data)


class ModelExecutionInstanceVersionViewSet(ExecutionRegistryViewSet):
    serializer_class = ExecutionVersionSerializer
    queryset = ModelExecutionInstanceVersion.objects.select_related(
        "instance", "prompt_profile_version",
    ).order_by("instance__key", "-version")
    filterset_fields = ("instance", "status", "operation", "deployment", "workflow_version", "prompt_profile_version")
    search_fields = ("instance__key", "instance__name", "operation")

    @extend_schema(parameters=[SmokeSourceContextQuerySerializer], responses=SmokeSourceContextPageSerializer)
    @action(detail=True, methods=("get",), url_path="smoke-source-contexts", filter_backends=(), pagination_class=None)
    def smoke_source_contexts(self, request, pk=None):
        self.get_object()
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise DjangoValidationError("来源上下文查询参数不能重复")
        query = SmokeSourceContextQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        from studio.services.execution_smoke_sources import smoke_source_context_rows
        pagination = AcceptanceSmokePagination()
        pagination.acceptance_max_page_size = 50
        rows = pagination.paginate_queryset(smoke_source_context_rows(request.user), request)
        data = {"count": pagination.page.paginator.count, "next": pagination.get_next_link(),
            "previous": pagination.get_previous_link(), "results": rows}
        return Response(SmokeSourceContextPageSerializer(data).data, headers={"Cache-Control": "private, no-store"})

    @extend_schema(parameters=[ExecutionSmokeEvidenceQuerySerializer],
        responses={200: ExecutionSmokeEvidenceSerializer, 400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",), pagination_class=None, filter_backends=())
    def evidence(self, request, pk=None):
        version = self.get_object()
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise DjangoValidationError("验收查询参数不能重复")
        query = ExecutionSmokeEvidenceQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        # Summary intentionally ignores the history status/page filter: a
        # success-only history must never conceal the latest failed evidence.
        summary = instance_smoke_summary(version)
        pagination = AcceptanceSmokePagination()
        rows = pagination.paginate_queryset(instance_smoke_history(version, status=query.validated_data.get("status")), request)
        data = {"summary": summary, "count": pagination.page.paginator.count,
            "next": pagination.get_next_link(), "previous": pagination.get_previous_link(),
            "results": project_instance_smoke_history(version, rows)}
        return Response(ExecutionSmokeEvidenceSerializer(data).data)

    @extend_schema(methods=["GET"], parameters=[ExecutionFieldSmokeSourceQuerySerializer], responses=ExecutionFieldSmokeSourceSerializer)
    @extend_schema(methods=["POST"], request=ExecutionFieldSmokeSourceCreateSerializer, responses=ExecutionFieldSmokeSourceSerializer)
    @action(detail=True, methods=("get", "post"), url_path="field-smoke-source")
    def field_smoke_source(self, request, pk=None):
        from studio.services.execution_field_smoke import field_smoke_source, build_field_smoke_source
        version = self.get_object()
        if request.method == "GET":
            if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
                raise DjangoValidationError("字段来源查询参数不能重复")
            query = ExecutionFieldSmokeSourceQuerySerializer(data=request.query_params.dict())
            query.is_valid(raise_exception=True)
            result = field_smoke_source(version_id=version.pk, actor=request.user, **query.validated_data)
        else:
            query = ExecutionFieldSmokeSourceCreateSerializer(data=request.data)
            query.is_valid(raise_exception=True)
            result = build_field_smoke_source(version_id=version.pk, actor=request.user, **query.validated_data)
        return Response(ExecutionFieldSmokeSourceSerializer(result).data)

    @extend_schema(request=ExecutionSmokeCreateSerializer,
        parameters=[OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True)],
        responses={202: ExecutionSmokeCreatedSerializer, 200: ExecutionSmokeCreatedSerializer,
            400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="smoke-test")
    def smoke_test(self, request, pk=None):
        version = self.get_object()
        serializer = ExecutionSmokeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attempt, prep, created = create_instance_smoke(version_id=version.pk, actor=request.user,
            client_idempotency_key=request.headers.get("Idempotency-Key", ""), **serializer.validated_data)
        kind = "workflow_smoke" if version.workflow_version_id else "deployment_smoke"
        response = ExecutionSmokeCreatedSerializer({"attempt_kind": kind, "attempt_id": attempt.pk, "status": attempt.status, "preparation": prep})
        from rest_framework.reverse import reverse
        location = reverse("execution-preparation-detail", args=[prep.pk], request=request)
        return Response(response.data, status=202 if created else 200, headers={"Location": location})

    @extend_schema(parameters=[ExecutionSelectionQuerySerializer], responses={200: ExecutionSelectionOptionsSerializer, 400: ConfigurationErrorResponseSerializer})
    @action(detail=False, methods=("get",), url_path="selection-options")
    def selection_options(self, request):
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise DjangoValidationError("选项目录参数不能重复")
        serializer = ExecutionSelectionQuerySerializer(data=request.query_params.dict())
        serializer.is_valid(raise_exception=True)
        return Response(ExecutionSelectionOptionsSerializer(execution_selection_options(**serializer.validated_data)).data)

    @extend_schema(responses={200: ExecutionAuthoringOptionsSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",), url_path="authoring-options")
    def authoring_options(self, request, pk=None):
        version = self.get_object()
        return Response(ExecutionAuthoringOptionsSerializer(execution_authoring_options(version.pk)).data)

    def get_serializer_class(self):
        return ExecutionVersionSerializer if self.action == "list" else ExecutionVersionDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in {"input_contract", "input_suggestion", "parameter_mappings"} and not self.request.user.is_staff:
            queryset = queryset.filter(pk__in=selectable_execution_versions(self.request.user).values("pk"))
        # Lists/details need only the strategy's purpose, never its frozen
        # rules, raw JSON or any Provider/Deployment runtime configuration.
        queryset = queryset.only(
            "id", "instance_id", "version", "status", "revision", "deployment_id", "operation", "mode",
            "prompt_mode", "prompt_profile_version_id", "workflow_version_id", "output_version_id", "published_at", "published_by_id",
            "created_at", "updated_at", "instance__id", "instance__name",
            "prompt_profile_version__id", "prompt_profile_version__operation", "prompt_profile_version__mode",
        )
        if self.action not in {"list", "evidence"}:
            queryset = queryset.prefetch_related("inputs__targets", "parameters", "auxiliaries")
        return queryset

    @extend_schema(request=ExecutionVersionCreateSerializer, responses={201: ExecutionVersionDetailSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    def create(self, request):
        serializer = ExecutionVersionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        parent = data.pop("instance")
        expected = data.pop("expected_revision")
        configuration = {key: data.pop(key) for key in ("inputs", "parameters", "auxiliaries") if key in data}
        version = create_execution_draft(
            instance_id=parent.pk, expected_revision=expected, selection=data, configuration=configuration, actor=request.user,
        )
        return Response(self.get_serializer(version).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ExecutionVersionUpdateSerializer, responses={200: ExecutionVersionDetailSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    def partial_update(self, request, pk=None):
        version = self.get_object()
        serializer = ExecutionVersionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        updated = update_execution_draft(
            version_id=version.pk, expected_revision=data.pop("expected_revision"), changes=data, actor=request.user,
        )
        return Response(self.get_serializer(updated).data)

    @extend_schema(request=ExecutionCloneSerializer, responses={201: ExecutionVersionDetailSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="clone-successor")
    def clone_successor(self, request, pk=None):
        source = self.get_object()
        serializer = ExecutionCloneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        version = clone_execution_draft(source_id=source.pk, actor=request.user, **serializer.validated_data)
        return Response(self.get_serializer(version).data, status=status.HTTP_201_CREATED)

    @extend_schema(description="返回精确实例版本的输入合同。参考图片策略仅在选用的原语、模板或逐单元生成依赖单元事实时声明 reference_context.units；只使用参考主体/保留声明的图片编辑不要求视频单元。已发布版本读取冻结合同，改变输入须发布后继版本。",
        responses={200: ExecutionInputContractSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",), url_path="input-contract", permission_classes=(IsAuthenticated,))
    def input_contract(self, request, pk=None):
        version = self.get_object()
        result = read_execution_input_contract(version.pk)
        return Response(ExecutionInputContractSerializer(result).data)

    @extend_schema(parameters=[ExecutionParameterMappingQuerySerializer], responses={
        200: ExecutionParameterMappingsSerializer, 400: ConfigurationErrorResponseSerializer,
        403: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer,
    })
    @action(detail=True, methods=("get",), url_path="parameter-mappings", permission_classes=(IsAuthenticated,),
            filter_backends=(), pagination_class=None)
    def parameter_mappings(self, request, pk=None):
        version = self.get_object()
        if set(request.query_params) != {"expected_definition_sha256"} or len(request.query_params.getlist("expected_definition_sha256")) != 1:
            raise DjangoValidationError("映射查询只接受一个 expected_definition_sha256")
        query = ExecutionParameterMappingQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        from studio.services.execution_parameter_mappings import execution_parameter_mappings
        result = execution_parameter_mappings(version.pk, **query.validated_data)
        return Response(ExecutionParameterMappingsSerializer(result).data, headers={"Cache-Control": "private, no-store"})

    @extend_schema(parameters=[ExecutionInputSuggestionQuerySerializer], responses={200: ExecutionInputSuggestionSerializer,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",), url_path="input-suggestion", permission_classes=(IsAuthenticated,))
    def input_suggestion(self, request, pk=None):
        version = self.get_object()
        query = ExecutionInputSuggestionQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        from studio.services.execution_input_suggestions import preview_execution_input_suggestion
        result = preview_execution_input_suggestion(read_execution_input_contract(version.pk), owner=request.user,
            project_id=query.validated_data.get("project_id"), input_key=query.validated_data["input_key"],
            suggestion_key=query.validated_data["suggestion_key"], production_run_id=query.validated_data.get("production_run_id"),
            production_scope={"kind": query.validated_data["entity_kind"], "id": str(query.validated_data["entity_id"])}
                if "entity_kind" in query.validated_data else None)
        return Response(ExecutionInputSuggestionSerializer(result).data, headers={"Cache-Control": "private, no-store"})

    @extend_schema(request=ExecutionPublishSerializer, responses={200: ExecutionVersionDetailSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        version = self.get_object()
        serializer = ExecutionPublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = publish_execution_version(version_id=version.pk, actor=request.user, **serializer.validated_data)
        return Response(self.get_serializer(result).data)

    @extend_schema(request=ExecutionRetireSerializer, responses={200: ExecutionVersionDetailSerializer, 400: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        version = self.get_object()
        serializer = ExecutionRetireSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = retire_execution_version(version_id=version.pk, actor=request.user, **serializer.validated_data)
        return Response(self.get_serializer(result).data)
