"""Explicit recipe result reuse; never dispatch, approve, or upload a bypass file."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from studio.models import ArtifactSelectionDecision, AssetReferenceSeries, AssetReferenceVersion, StageRun
from studio.platform_serializers import ConfigurationErrorResponseSerializer
from studio.serializers import StrictSerializer
from studio.services.production_recipe_material_fulfillment import (
    material_fulfillment_receipt, recipe_material_fulfillment_context, select_recipe_material_asset,
)
from studio.services.security_boundaries import security_boundary_resolution_cache

ERRORS = {400: ConfigurationErrorResponseSerializer, 403: ConfigurationErrorResponseSerializer,
    404: ConfigurationErrorResponseSerializer}


class RecipeMaterialFulfillmentQuerySerializer(StrictSerializer):
    stage_run_id = serializers.UUIDField()
    unit_key = serializers.CharField(max_length=191, trim_whitespace=False)


class RecipeMaterialFulfillmentInputSerializer(RecipeMaterialFulfillmentQuerySerializer):
    expected_run_plan_sha256 = serializers.RegexField(r"^[a-f0-9]{64}$", trim_whitespace=False)
    requirement_key = serializers.CharField(max_length=191, trim_whitespace=False)
    reference_version_id = serializers.UUIDField(required=False,
        help_text="复用确切原版本；与series_id、asset_id、source三字段互斥，来源由原版本读取。")
    series_id = serializers.UUIDField(required=False)
    asset_id = serializers.UUIDField(required=False, help_text="新关联的正式Asset；上传先走原assets multipart入口。与reference_version_id互斥。")
    source = serializers.ChoiceField(required=False,
        choices=(AssetReferenceVersion.Source.UPLOAD, AssetReferenceVersion.Source.EXISTING))

    def validate(self, attrs):
        association = {"series_id", "asset_id", "source"} & attrs.keys()
        if ("reference_version_id" in attrs and association
                or "reference_version_id" not in attrs and association != {"series_id", "asset_id", "source"}):
            raise ValidationError("请选择确切版本，或完整提供系列、Asset和关联来源；两种方式不能混合。")
        return attrs


class RecipeMaterialFulfillmentReceiptSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    reference_version_id = serializers.UUIDField()
    series_id = serializers.UUIDField()
    asset_id = serializers.UUIDField()
    source = serializers.ChoiceField(choices=AssetReferenceVersion.Source.choices,
        help_text="所选确切版本的原始来源；复用已生成版本仍为generated，不代表本轮调用模型。")
    target_sha256 = serializers.CharField()
    selection_mode = serializers.ChoiceField(choices=ArtifactSelectionDecision.SelectionMode.choices)
    selected_by_id = serializers.IntegerField(min_value=1, allow_null=True)
    created_at = serializers.DateTimeField()


class RecipeMaterialFulfillmentContextSerializer(serializers.Serializer):
    source_mode = serializers.ChoiceField(choices=("generate", "existing", "upload"))
    stage_run_id = serializers.UUIDField()
    unit_key = serializers.CharField()
    run_plan_sha256 = serializers.CharField()
    requirement_key = serializers.CharField()
    appearance_id = serializers.UUIDField()
    setting_revision_id = serializers.UUIDField()
    appearance_role = serializers.ChoiceField(choices=AssetReferenceSeries.Role.choices)
    reference_name = serializers.CharField()
    primary_appearance_id = serializers.UUIDField(allow_null=True)
    can_select = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)
    selection = RecipeMaterialFulfillmentReceiptSerializer(allow_null=True)


def _input(serializer, data):
    result = serializer(data=data)
    result.is_valid(raise_exception=True)
    return result.validated_data


class ProductionMaterialFulfillmentActions:
    def _material_fulfillment_stage(self, stage_run_id):
        # Check BOTH path Run and submitted Stage before entering the writer.
        # Staff visibility of two Runs must not let a URL mutate a different one.
        return get_object_or_404(StageRun.objects.only("id"), pk=stage_run_id, production_run=self.get_object())

    @extend_schema(description="只读核对确切需求；无准备、未冻结且主辅均无调用的受阻准备，或全部调用明确失败无产物时可人工选材。提交时重新核对，不预留资格。",
        parameters=[RecipeMaterialFulfillmentQuerySerializer],
        responses={200: RecipeMaterialFulfillmentContextSerializer, **ERRORS})
    @action(detail=True, methods=("get",), url_path="material-fulfillment-context")
    @security_boundary_resolution_cache()
    def material_fulfillment_context(self, request, pk=None):
        values = _input(RecipeMaterialFulfillmentQuerySerializer, request.query_params.dict())
        stage = self._material_fulfillment_stage(values["stage_run_id"])
        try:
            result = recipe_material_fulfillment_context(stage_run_id=stage.pk,
                unit_key=values["unit_key"], actor=request.user)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or {"detail": exc.messages}) from exc
        return Response(RecipeMaterialFulfillmentContextSerializer(result).data,
            headers={"Cache-Control": "private, no-store"})

    @extend_schema(description="明确选择本轮成图；未提交受阻准备及辅助准备在同一事务内沿原取消writer停止，原输入与阻断证据保留。拒绝在途、未知、成功及独立取消请求；不提交模型或批准图片审核。",
        request=RecipeMaterialFulfillmentInputSerializer,
        responses={200: RecipeMaterialFulfillmentReceiptSerializer, 201: RecipeMaterialFulfillmentReceiptSerializer, **ERRORS})
    @action(detail=True, methods=("post",), url_path="fulfill-material")
    @security_boundary_resolution_cache()
    def fulfill_material(self, request, pk=None):
        values = _input(RecipeMaterialFulfillmentInputSerializer, request.data)
        self._material_fulfillment_stage(values["stage_run_id"])
        try:
            row, created = select_recipe_material_asset(actor=request.user, **values)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or {"detail": exc.messages}) from exc
        # No post-commit read that could misreport a committed choice as failure;
        # no advancement, scheduling, synthetic candidate, or approval action.
        return Response(RecipeMaterialFulfillmentReceiptSerializer(material_fulfillment_receipt(row)).data,
            status=201 if created else 200, headers={"Cache-Control": "private, no-store"})
