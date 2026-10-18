"""Authorized reads, explicit field actions and the shared continue action."""
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from studio.exceptions import api_error_response
from studio.execution_preparation_serializers import (
    ExecutionFieldActionResultSerializer, ExecutionFieldConfirmSerializer, ExecutionFieldCreateSerializer,
    ExecutionFieldDetailSerializer, ExecutionFieldReviewSerializer, ExecutionFieldSummarySerializer,
    ExecutionPreparationDetailSerializer, ExecutionPreparationContinueSerializer, ExecutionResourceConfirmSerializer, ExecutionTextResponseCompleteSerializer,
    ExecutionFieldBatchRetrySerializer, ExecutionPreparationSerializer, StagePreparationQuerySerializer, StageExecutionPreparationSerializer,
    ExecutionMediaArchiveRecoverSerializer, ExecutionMediaUncertainRetryRequestSerializer,
    StagePreparationGroupQuerySerializer, StagePreparationGroupSerializer,
)
from studio.models import ExecutionFieldValue, ExecutionPreparation, StageRun
from studio.pagination import StandardPageNumberPagination
from studio.platform_serializers import ConfigurationErrorResponseSerializer
from studio.services.execution_preparation_fields import confirm_execution_field, create_manual_execution_field, review_execution_field
from studio.services.execution_preparation_owners import locked_preparation_owners, preparation_owner_ref, visible_execution_preparations
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.prompt_normalization_contract import PromptNormalizationContractError
from studio.execution_smoke_serializers import ExecutionSmokeReuseFixtureSerializer, ExecutionSmokeReuseQuerySerializer
from studio.execution_prompt_serializers import ExecutionPromptQuerySerializer, ExecutionPromptDisplaySerializer
from studio.execution_storage_serializers import ExecutionStorageMetricsQuerySerializer, ExecutionStorageMetricsSerializer
from studio.execution_field_bulk_serializers import (
    ExecutionBulkFieldPreviewRequestSerializer, ExecutionBulkFieldConfirmRequestSerializer,
    ExecutionBulkFieldPreviewSerializer, ExecutionBulkFieldResultSerializer,
)
from studio.execution_configuration_replacement_serializers import (
    ConfigurationReplacementRequestSerializer, ConfigurationReplacementApplySerializer,
    ConfigurationReplacementOptionsSerializer, ConfigurationReplacementPreviewSerializer,
    ConfigurationReplacementResultSerializer,
)


class ExecutionPreparationApiViewSet(viewsets.GenericViewSet):
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    http_method_names = ("get", "post", "head", "options")

    def check_permissions(self, request):
        super().check_permissions(request)
        if not request.user.is_active:
            raise PermissionDenied("当前登录身份不可用")

    def handle_exception(self, exc):
        if isinstance(exc, PromptNormalizationContractError):
            return api_error_response(status_code=400, code="execution_field_contract_invalid", details=str(exc))
        if isinstance(exc, DjangoValidationError):
            code = getattr(exc, "code", None) or "invalid"
            return api_error_response(status_code=409 if code in {"execution_preparation_conflict", "field_review_stale", "field_selection_conflict"} else 400,
                code=code, details=getattr(exc, "message_dict", None) or exc.messages)
        if isinstance(exc, ObjectDoesNotExist):
            return api_error_response(status_code=404, code="not_found", details="执行准备、阶段或字段不存在，或无权查看")
        if isinstance(exc, IntegrityError):
            return api_error_response(status_code=409, code="execution_preparation_conflict", details="准备已发生变化，请重新读取后再操作")
        return super().handle_exception(exc)

    def visible_preparation(self, pk):
        return visible_execution_preparations(self.request.user).get(pk=pk)


class ExecutionPreparationViewSet(ExecutionPreparationApiViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin):
    serializer_class = ExecutionPreparationDetailSerializer

    @extend_schema(responses={200: ConfigurationReplacementOptionsSerializer, 400: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",), url_path="configuration-successors")
    def configuration_successors(self, request, pk=None):
        from studio.services.execution_configuration_replacement import replacement_options
        self.visible_preparation(pk)
        response = Response(ConfigurationReplacementOptionsSerializer(replacement_options(preparation_id=pk, actor=request.user)).data)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(request=ConfigurationReplacementRequestSerializer, responses={200: ConfigurationReplacementPreviewSerializer,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="configuration-replacement-preview")
    def configuration_replacement_preview(self, request, pk=None):
        from studio.services.execution_configuration_replacement import preview_configuration_replacement
        self.visible_preparation(pk)
        payload = ConfigurationReplacementRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        response = Response(ConfigurationReplacementPreviewSerializer(preview_configuration_replacement(
            preparation_id=pk, actor=request.user, **payload.validated_data)).data)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(request=ConfigurationReplacementApplySerializer, responses={200: ConfigurationReplacementResultSerializer,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="configuration-replacement")
    def configuration_replacement(self, request, pk=None):
        from studio.services.execution_configuration_replacement import apply_configuration_replacement
        self.visible_preparation(pk)
        payload = ConfigurationReplacementApplySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        replacement = apply_configuration_replacement(preparation_id=pk, actor=request.user, **payload.validated_data)
        return Response(ConfigurationReplacementResultSerializer({"preparation_id": pk,
            "replacement_id": replacement.pk, "version_id": replacement.execution_instance_version_id}).data)

    def visible_bulk_ids(self, ids):
        if visible_execution_preparations(self.request.user).filter(pk__in=ids).count() != len(set(ids)):
            raise ExecutionPreparation.DoesNotExist()

    @extend_schema(request=ExecutionBulkFieldPreviewRequestSerializer, responses={200: ExecutionBulkFieldPreviewSerializer,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=False, methods=("post",), url_path="bulk-field-preview")
    def bulk_field_preview(self, request):
        from studio.services.execution_field_bulk_review import preview_bulk_field_review
        payload = ExecutionBulkFieldPreviewRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = payload.validated_data["preparation_ids"]
        self.visible_bulk_ids(ids)
        result = preview_bulk_field_review(preparation_ids=ids, actor=request.user)
        response = Response(ExecutionBulkFieldPreviewSerializer(result).data)
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(request=ExecutionBulkFieldConfirmRequestSerializer, responses={200: ExecutionBulkFieldResultSerializer,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=False, methods=("post",), url_path="bulk-field-confirm")
    def bulk_field_confirm(self, request):
        from studio.services.execution_field_bulk_review import confirm_bulk_field_review
        payload = ExecutionBulkFieldConfirmRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        items = payload.validated_data["items"]
        self.visible_bulk_ids([item["preparation_id"] for item in items])
        return Response(ExecutionBulkFieldResultSerializer(confirm_bulk_field_review(items=items, actor=request.user)).data)

    def get_serializer_class(self):
        if self.action == "groups":
            return StagePreparationGroupSerializer
        return StageExecutionPreparationSerializer if self.action == "list" else ExecutionPreparationDetailSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ExecutionPreparation.objects.none()
        query = visible_execution_preparations(self.request.user)
        if self.action in {"list", "groups"}:
            if any(len(self.request.query_params.getlist(key)) != 1 for key in self.request.query_params):
                raise DjangoValidationError("不能重复提供查询参数")
            query_serializer = StagePreparationGroupQuerySerializer if self.action == "groups" else StagePreparationQuerySerializer
            serializer = query_serializer(data=self.request.query_params)
            serializer.is_valid(raise_exception=True)
            stages = StageRun.objects.all()
            if not self.request.user.is_staff:
                stages = stages.filter(production_run__owner=self.request.user)
            stage = stages.select_related("production_run").only("id", "production_run__project_id").get(pk=serializer.validated_data["stage_run_id"])
            self.presentation_stage = stage
            query = query.filter(stage_run_id=stage.pk).only(*ExecutionPreparationSerializer.Meta.fields)
            from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
            kind = serializer.validated_data.get("kind")
            if kind == "auxiliary":
                query = query.filter(execution_instance_version__operation__in=FIELD_OPERATION_MODES)
            elif kind == "primary":
                query = query.exclude(execution_instance_version__operation__in=FIELD_OPERATION_MODES)
            query = query.select_related("execution_instance_version").only(
                *ExecutionPreparationSerializer.Meta.fields, "execution_instance_version__operation")
            if self.action == "groups" or "unit_key" in serializer.validated_data:
                from studio.services.execution_preparation_presentation import preparation_unit_queryset, preparation_unit_groups
                query = preparation_unit_queryset(query, stage)
                if self.action == "groups":
                    return preparation_unit_groups(query)
                return query.filter(unit_group_key=serializer.validated_data["unit_key"]).order_by("-created_at", "-id")
        return query.order_by("created_at", "id")

    @extend_schema(parameters=[StagePreparationGroupQuerySerializer], responses={200: StagePreparationGroupSerializer(many=True),
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer})
    @action(detail=False, methods=["get"])
    def groups(self, request):
        from studio.services.execution_preparation_presentation import production_preparation_presentations
        page = self.paginate_queryset(self.get_queryset())
        rows = production_preparation_presentations(page, self.presentation_stage)
        groups = [{"unit_key": row.unit_group_key, "version_count": row.unit_version_count,
            "label": row.presentation["parent"]["label"] if row.presentation["parent"] else row.presentation["label"],
            "latest": row} for row in rows]
        return self.get_paginated_response(self.get_serializer(groups, many=True).data)

    @extend_schema(parameters=[StagePreparationQuerySerializer], responses={200: StageExecutionPreparationSerializer(many=True),
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer})
    def list(self, request, *args, **kwargs):
        from studio.services.execution_preparation_presentation import production_preparation_presentations
        page = self.paginate_queryset(self.get_queryset())
        rows = production_preparation_presentations(page, self.presentation_stage)
        return self.get_paginated_response(self.get_serializer(rows, many=True).data)

    @extend_schema(responses={200: ExecutionPreparationDetailSerializer, 400: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    def retrieve(self, request, pk=None):
        preparation = self.get_object()
        return self.detail_response(preparation)

    @extend_schema(parameters=[ExecutionPromptQuerySerializer], responses={200: ExecutionPromptDisplaySerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",), url_path="prompt-display")
    def prompt_display(self, request, pk=None):
        from studio.services.execution_prompt_display import read_preparation_prompt_display
        preparation = self.get_object()
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise DjangoValidationError("不能重复提供查询参数")
        query = ExecutionPromptQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        result = read_preparation_prompt_display(preparation_id=preparation.pk, actor=request.user,
            expected_preparation_revision=query.validated_data["expected_preparation_revision"])
        return Response(ExecutionPromptDisplaySerializer(result).data)

    @extend_schema(parameters=[ExecutionStorageMetricsQuerySerializer], responses={200: ExecutionStorageMetricsSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer},
        description="按需读取原准备合同与请求文件manifest的UTF-8字节元数据；不读正文/文件、不重算、不执行。JSON字节不是数据库物理占用，文件大小不是当前完整性核验；当前上限不是历史保存配置，不合计字段历史或重复Attempt文件。")
    @action(detail=True, methods=("get",), url_path="storage-metrics")
    def storage_metrics(self, request, pk=None):
        from studio.services.execution_storage_metrics import read_preparation_storage_metrics
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise DjangoValidationError("不能重复提供查询参数")
        query = ExecutionStorageMetricsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        result = read_preparation_storage_metrics(preparation_id=pk, actor=request.user,
            expected_preparation_revision=query.validated_data["expected_preparation_revision"])
        response = Response(ExecutionStorageMetricsSerializer(result).data)
        response["Cache-Control"] = "private, no-store"
        return response

    def detail_response(self, preparation):
        with locked_preparation_owners([preparation_owner_ref(preparation)], self.request.user):
            preparation = self.get_queryset().select_for_update(of=("self",)).get(pk=preparation.pk)
            detail = read_execution_preparation_detail(preparation)
            if "media_generation_target" in detail.resolved_inputs:
                from studio.services.media_generation_targets import read_media_generation_target
                read_media_generation_target(preparation, detail=detail)
            preparation.presentation = None
            if preparation.stage_run_id:
                from studio.services.execution_preparation_presentation import production_preparation_presentations
                from studio.services.execution_auxiliary_requests import read_production_field_context
                context = read_production_field_context(preparation, detail=detail)
                parents = {preparation.pk: context[0]} if context else {}
                production_preparation_presentations([preparation], preparation.stage_run, verified_parents=parents)
            else:
                from studio.services.execution_preparation_presentation import selected_preparation_outcome
                preparation.outcome = selected_preparation_outcome(preparation)
            # The fields exist only on this read representation, not the model
            # or its list queries. Do not repair missing snapshots here.
            for key in ("intent", "resolved_inputs", "review_contract", "prepared_request", "evaluation"):
                setattr(preparation, key, getattr(detail, key))
            from studio.services.execution_capacity_observations import read_preparation_capacity_observation
            from studio.services.execution_preparation_fields import read_preparation_field_slots
            from studio.services.execution_field_review import describe_field_review_contract
            preparation.review_summary = describe_field_review_contract(detail.review_contract)
            preparation.capacity_sample = read_preparation_capacity_observation(preparation)
            preparation.field_slots = read_preparation_field_slots(preparation)
            from studio.services.execution_auxiliary_actions import read_auxiliary_batches
            preparation.auxiliary_batches = read_auxiliary_batches(preparation)
            from studio.services.execution_response_diagnostics import read_preparation_response_diagnostic
            preparation.response_diagnostic = read_preparation_response_diagnostic(preparation)
            from studio.services.execution_production_cancellation import production_cancellation_state
            preparation.cancellation = production_cancellation_state(preparation, detail)
            from studio.services.execution_resource_policy import describe_resource_review
            preparation.resource_review = describe_resource_review(preparation, detail)
            from studio.services.execution_text_response_review import describe_text_response_review
            preparation.text_response_review = describe_text_response_review(preparation)
            from studio.services.execution_media_archive_recovery import describe_media_archive_recovery
            preparation.media_archive_recovery = describe_media_archive_recovery(preparation)
            from studio.services.execution_media_uncertain_retry import describe_media_uncertain_retry
            preparation.media_uncertain_retry = describe_media_uncertain_retry(preparation)
            return Response(self.get_serializer(preparation).data)

    @extend_schema(request=ExecutionPreparationContinueSerializer, responses={200: ExecutionPreparationDetailSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="continue")
    def continue_preparation(self, request, pk=None):
        preparation = self.get_object()
        serializer = ExecutionPreparationContinueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        advanced = advance_execution_preparation(preparation_id=preparation.pk,
            expected_preparation_revision=serializer.validated_data["expected_preparation_revision"], actor=request.user, human_initiated=True)
        return self.detail_response(advanced)

    @extend_schema(request=ExecutionResourceConfirmSerializer, responses={200: ExecutionPreparationDetailSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="confirm-resources")
    def confirm_resources(self, request, pk=None):
        preparation = self.get_object()
        serializer = ExecutionResourceConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from studio.services.execution_resource_policy import confirm_resource_review
        confirmed = confirm_resource_review(preparation_id=preparation.pk, actor=request.user, **serializer.validated_data)
        return self.detail_response(confirmed)

    @extend_schema(request=ExecutionMediaArchiveRecoverSerializer, responses={200: ExecutionPreparationDetailSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="recover-media-archive")
    def recover_media_archive(self, request, pk=None):
        preparation = self.get_object()
        serializer = ExecutionMediaArchiveRecoverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from studio.services.execution_media_archive_recovery import recover_media_archive
        recovered = recover_media_archive(preparation=preparation, actor=request.user, **serializer.validated_data)
        return self.detail_response(recovered)

    @extend_schema(request=ExecutionMediaUncertainRetryRequestSerializer, responses={200: ExecutionPreparationDetailSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="retry-uncertain-media")
    def retry_uncertain_media(self, request, pk=None):
        serializer = ExecutionMediaUncertainRetryRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from studio.services.execution_media_uncertain_retry import retry_uncertain_media
        result = retry_uncertain_media(preparation=self.get_object(), actor=request.user, **serializer.validated_data)
        return self.detail_response(result)

    @extend_schema(request=ExecutionTextResponseCompleteSerializer, responses={200: ExecutionPreparationDetailSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="complete-text-response")
    def complete_text_response(self, request, pk=None):
        preparation = self.get_object()
        serializer = ExecutionTextResponseCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from studio.services.execution_text_response_review import complete_text_response
        completed = complete_text_response(preparation_id=preparation.pk, actor=request.user, **serializer.validated_data)
        return self.detail_response(completed)

    @extend_schema(request=ExecutionPreparationContinueSerializer, responses={200: ExecutionPreparationDetailSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="cancel")
    def cancel_preparation(self, request, pk=None):
        preparation = self.get_object()
        serializer = ExecutionPreparationContinueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from studio.services.execution_production_cancellation import cancel_production_preparation
        cancelled = cancel_production_preparation(preparation_id=preparation.pk, actor=request.user,
            **serializer.validated_data)
        return self.detail_response(cancelled)

    @extend_schema(request=ExecutionFieldBatchRetrySerializer, responses={200: ExecutionPreparationDetailSerializer,
        400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
        404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",), url_path="retry-field-batch")
    def retry_field_batch(self, request, pk=None):
        preparation = self.get_object()
        serializer = ExecutionFieldBatchRetrySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from studio.services.execution_auxiliary_actions import retry_preparation_field_batch
        advanced = retry_preparation_field_batch(preparation_id=preparation.pk, actor=request.user, **serializer.validated_data)
        return self.detail_response(advanced)

    @extend_schema(parameters=[OpenApiParameter("expected_preparation_revision", int, required=True)],
        responses={200: ExecutionSmokeReuseFixtureSerializer, 400: ConfigurationErrorResponseSerializer,
            403: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",), url_path="smoke-fixture")
    def smoke_fixture(self, request, pk=None):
        if not request.user.is_staff:
            raise PermissionDenied("组合烟测需要管理员身份")
        preparation = self.get_object()
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise DjangoValidationError("不能重复提供查询参数")
        serializer = ExecutionSmokeReuseQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        from studio.services.execution_smoke import read_instance_smoke_reuse_fixture
        fixture = read_instance_smoke_reuse_fixture(preparation_id=preparation.pk, actor=request.user,
            expected_preparation_revision=serializer.validated_data["expected_preparation_revision"])
        return Response(ExecutionSmokeReuseFixtureSerializer(fixture).data)


class ExecutionFieldValueViewSet(ExecutionPreparationApiViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin):
    serializer_class = ExecutionFieldDetailSerializer

    def get_serializer_class(self):
        return ExecutionFieldSummarySerializer if self.action == "list" else ExecutionFieldDetailSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ExecutionFieldValue.objects.none()
        query = ExecutionFieldValue.objects.filter(preparation__in=visible_execution_preparations(self.request.user))
        if self.action == "list":
            raw_id = self.request.query_params.get("preparation_id")
            if not raw_id:
                raise DjangoValidationError({"preparation_id": "请选择需要查看的执行准备"})
            preparation = self.visible_preparation(raw_id)
            query = query.filter(preparation=preparation).defer("text")
        return query.order_by("unit_key", "field_key", "-revision", "id")

    @extend_schema(parameters=[OpenApiParameter("preparation_id", str, required=True)], responses=ExecutionFieldSummarySerializer(many=True))
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        value = self.get_object()
        value.clean()
        return Response(self.get_serializer(value).data)

    def result(self, value, selection=None, *, status=200):
        value.refresh_from_db()
        return Response(ExecutionFieldActionResultSerializer({"field": value,
            "preparation": self.visible_preparation(value.preparation_id), "selection": selection}).data, status=status)

    @extend_schema(request=ExecutionFieldCreateSerializer, responses={200: ExecutionFieldActionResultSerializer,
        201: ExecutionFieldActionResultSerializer, 400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer,
        409: ConfigurationErrorResponseSerializer})
    @transaction.atomic
    def create(self, request):
        serializer = ExecutionFieldCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        preparation = self.visible_preparation(values["preparation_id"])
        with locked_preparation_owners([preparation_owner_ref(preparation)], request.user):
            existed = preparation.field_values.filter(unit_key=values["unit_key"], field_key=values["field_key"], revision=values["revision"]).exists()
            value = create_manual_execution_field(**values, actor=request.user)
            return self.result(value, status=200 if existed else 201)

    @extend_schema(request=ExecutionFieldConfirmSerializer, responses={200: ExecutionFieldActionResultSerializer,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",))
    @transaction.atomic
    def confirm(self, request, pk=None):
        value = self.get_object()
        serializer = ExecutionFieldConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        value, selection = confirm_execution_field(preparation_id=value.preparation_id, field_value_id=value.pk,
            actor=request.user, **serializer.validated_data)
        return self.result(value, selection)

    @extend_schema(request=ExecutionFieldReviewSerializer, responses={200: ExecutionFieldActionResultSerializer,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",))
    @transaction.atomic
    def reject(self, request, pk=None):
        value = self.get_object()
        serializer = ExecutionFieldReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        value = review_execution_field(preparation_id=value.preparation_id, field_value_id=value.pk,
            approve=False, actor=request.user, **serializer.validated_data)
        return self.result(value)
