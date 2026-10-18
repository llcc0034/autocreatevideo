from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied as DjangoPermissionDenied, ValidationError as DjangoValidationError
from drf_spectacular.types import OpenApiTypes
from django.db.models import Prefetch
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from studio.asset_generation_serializers import (
    AssetAppearanceOptionsQuerySerializer,
    AssetAppearanceOptionsResponseSerializer,
    AssetAppearanceOptionSerializer,
    AssetGenerationJobCreateSerializer,
    AssetGenerationJobSerializer,
    AssetGenerationOptionsQuerySerializer,
    AssetGenerationOptionsResponseSerializer,
    AssetGenerationOptionSerializer,
    AssetGenerationResolutionInputSerializer,
    AssetGenerationResolutionSerializer,
)
from studio.models import AssetGenerationJob
from studio.pagination import StandardPageNumberPagination
from studio.services.asset_generation import (
    asset_generation_options,
    create_asset_generation_job,
    request_asset_generation_cancel,
    resolve_uncertain_asset_generation_job,
    retry_asset_generation_job,
)
from studio.services.provider_registry import ProviderDriverError
from studio.services.media_generation_appearances import appearance_options


class AssetGenerationJobViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    filterset_fields = ("job_type", "media_type", "status", "style")
    ordering_fields = ("created_at", "updated_at", "finished_at")

    def get_queryset(self):
        queryset = AssetGenerationJob.objects.select_related(
            "owner", "style", "model_deployment", "workflow_template_version__template",
            "output_asset", "execution_instance_version__instance",
        ).prefetch_related("resolutions", "execution_preparations",
            Prefetch("retries", queryset=AssetGenerationJob.objects.only("id", "retry_of_id", "status").order_by("created_at", "id")))
        if getattr(self, "swagger_fake_view", False):
            return queryset.none()
        return queryset if self.request.user.is_staff else queryset.filter(owner=self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return AssetGenerationJobCreateSerializer
        return AssetGenerationJobSerializer

    @extend_schema(
        request=AssetGenerationJobCreateSerializer,
        parameters=[OpenApiParameter(
            name="Idempotency-Key",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            required=True,
            description="同一创建意图必须复用的稳定幂等键；响应丢失后重放不会重复计费。",
        )],
        responses={
            200: AssetGenerationJobSerializer,
            202: AssetGenerationJobSerializer,
        },
    )
    def create(self, request, *args, **kwargs):
        serializer = AssetGenerationJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        try:
            job, created = create_asset_generation_job(
                owner=request.user,
                client_idempotency_key=str(request.headers.get("Idempotency-Key") or ""),
                **values,
            )
        except ObjectDoesNotExist as exc:
            raise NotFound("执行实例或其关联对象不存在") from exc
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except (DjangoValidationError, ProviderDriverError) as exc:
            if isinstance(exc, ProviderDriverError):
                raise ValidationError({
                    "code": exc.code,
                    "detail": str(exc),
                }) from exc
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            if getattr(exc, "code", None) == "execution_preparation_conflict":
                return Response({"code": exc.code, "detail": detail}, status=status.HTTP_409_CONFLICT)
            raise ValidationError(detail) from exc
        output = AssetGenerationJobSerializer(job, context={"request": request})
        return Response(
            output.data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
            headers={"Location": self.reverse_action("detail", args=[job.pk])},
        )

    @extend_schema(
        parameters=[AssetGenerationOptionsQuerySerializer],
        responses=AssetGenerationOptionsResponseSerializer,
    )
    @action(detail=False, methods=("get",), url_path="options")
    def generation_options(self, request):
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise ValidationError("选项查询参数不能重复")
        query = AssetGenerationOptionsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        values = query.validated_data
        rows = asset_generation_options(user=request.user, job_type=values["job_type"], media_type=values["media_type"])
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(AssetGenerationOptionSerializer(page, many=True).data)

    @extend_schema(parameters=[AssetAppearanceOptionsQuerySerializer], responses=AssetAppearanceOptionsResponseSerializer)
    @action(detail=False, methods=("get",), url_path="appearance-options")
    def appearance_options(self, request):
        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise ValidationError("选项查询参数不能重复")
        query = AssetAppearanceOptionsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = appearance_options(owner=request.user, role=query.validated_data["role"], search=query.validated_data.get("search", ""))
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(AssetAppearanceOptionSerializer(page, many=True).data)

    @extend_schema(request=None, responses=AssetGenerationJobSerializer)
    @action(detail=True, methods=("post",))
    def cancel(self, request, pk=None):
        try:
            job = request_asset_generation_cancel(self.get_object())
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages) from exc
        return Response(AssetGenerationJobSerializer(job, context={"request": request}).data)

    @extend_schema(
        request=None,
        parameters=[OpenApiParameter(
            name="Idempotency-Key",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            required=False,
            description="同一重试意图的稳定幂等键；省略时按原任务生成服务端稳定键。原任务已有后继时仅允许原键重放，新键409；后继失败须明确重试该失败任务。",
        )],
        responses={200: AssetGenerationJobSerializer, 202: AssetGenerationJobSerializer},
    )
    @action(detail=True, methods=("post",))
    def retry(self, request, pk=None):
        if request.data:
            raise ValidationError("重试沿用原请求；修改输入或实例请新建任务")
        try:
            job, created = retry_asset_generation_job(
                self.get_object(),
                client_idempotency_key=str(request.headers.get("Idempotency-Key") or ""),
            )
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            if getattr(exc, "code", None) == "execution_preparation_conflict":
                return Response({"code": exc.code, "detail": detail}, status=status.HTTP_409_CONFLICT)
            raise ValidationError(detail) from exc
        return Response(
            AssetGenerationJobSerializer(job, context={"request": request}).data,
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
            headers={"Location": self.reverse_action("detail", args=[job.pk])},
        )

    @extend_schema(
        request=AssetGenerationResolutionInputSerializer,
        parameters=[OpenApiParameter(
            name="Idempotency-Key",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            required=False,
            description="同一管理员消歧意图的稳定幂等键；响应丢失后可安全重放。",
        )],
        responses={
            200: AssetGenerationResolutionSerializer,
            201: AssetGenerationResolutionSerializer,
        },
        description=(
            "Append immutable staff evidence for an uncertain asset generation. "
            "This never repeats the original submit; confirmed media success resumes poll/collect."
        ),
    )
    @action(
        detail=True,
        methods=("post",),
        url_path="resolve-uncertain",
        permission_classes=(IsAdminUser,),
    )
    def resolve_uncertain(self, request, pk=None):
        serializer = AssetGenerationResolutionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            resolution, created = resolve_uncertain_asset_generation_job(
                self.get_object(),
                user=request.user,
                client_idempotency_key=str(request.headers.get("Idempotency-Key") or ""),
                **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise ValidationError(detail) from exc
        return Response(
            AssetGenerationResolutionSerializer(resolution).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
