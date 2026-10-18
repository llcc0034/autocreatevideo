from django.db.models import OuterRef, Subquery
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from studio.bible_variant_serializers import (
    BibleVariantCreateInput, BibleVariantDetail, BibleVariantPageQuery, BibleVariantQuery, BibleVariantReviseInput,
    BibleVariantReviewInput, BibleVariantRevisionSummary, BibleVariantSummary,
    BibleVariantSourceQuery, BibleSettingSourceOutput,
)
from studio.models import BibleVariant, BibleVariantReview
from studio.pagination import StandardPageNumberPagination
from studio.platform_serializers import ConfigurationErrorResponseSerializer
from studio.services.bible_variants import (
    create_variant, project_for_actor, review_variant, revise_variant, variant_rows, verify_setting,
)
from studio.services.bible_setting_evidence import setting_revision_rows, source_reference


class BibleVariantPagination(StandardPageNumberPagination):
    def get_page_size(self, request):
        return min(super().get_page_size(request), 50)


class BibleVariantViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = (IsAuthenticated,)
    serializer_class = BibleVariantSummary
    pagination_class = BibleVariantPagination
    http_method_names = ("get", "post", "head", "options")
    queryset = BibleVariant.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        rows = variant_rows(self.request.user).annotate(current_decision=Subquery(
            BibleVariantReview.objects.filter(setting__variant_id=OuterRef("id"),
                setting__revision=OuterRef("revision")).values("decision")[:1]))
        if self.action == "list":
            query = BibleVariantQuery(data=self.request.query_params)
            query.is_valid(raise_exception=True)
            values = query.validated_data
            project_for_actor(values["project_id"], self.request.user)
            rows = rows.filter(project_id=values["project_id"], **{f"{values['entity_kind']}_id": values["entity_id"]})
        return rows.select_related("source_binding").order_by("created_at", "id")

    def _detail(self, variant, *, revision=None):
        setting = setting_revision_rows().filter(variant_id=variant.pk, revision=revision or variant.revision).first()
        if setting is None:
            raise ValidationError("所选设定修订不存在")
        variant.setting = verify_setting(setting)
        variant.source = source_reference(variant)
        variant.primary_setting = setting.primary_setting_revision
        if revision is None or revision == variant.revision:
            variant.current_decision = getattr(getattr(setting, "review", None), "decision", None)
        return BibleVariantDetail(variant).data

    @extend_schema(parameters=[BibleVariantQuery], responses={200: BibleVariantSummary(many=True),
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer})
    def list(self, request, *args, **kwargs):
        rows = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        for row in rows:
            row.source = source_reference(row)
        return self.get_paginated_response(BibleVariantSummary(rows, many=True).data)

    @extend_schema(parameters=[OpenApiParameter("revision", int)], responses={200: BibleVariantDetail,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    def retrieve(self, request, *args, **kwargs):
        revision = request.query_params.get("revision")
        if revision is not None and (len(revision) > 10 or not revision.isascii() or not revision.isdigit() or int(revision) < 1):
            raise ValidationError("revision 必须是正整数")
        return Response(self._detail(self.get_object(), revision=int(revision) if revision else None))

    @extend_schema(parameters=[BibleVariantSourceQuery], responses={200: BibleSettingSourceOutput,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",))
    def source(self, request, pk=None):
        from studio.services.bible_setting_sources import read_setting_source
        query = BibleVariantSourceQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        variant = self.get_object()
        setting = setting_revision_rows().filter(variant_id=variant.pk,
            revision=query.validated_data["revision"]).first()
        if setting is None:
            raise ValidationError("所选设定修订不存在")
        return Response(BibleSettingSourceOutput(read_setting_source(setting)).data)

    @extend_schema(request=BibleVariantCreateInput, responses={201: BibleVariantDetail,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    def create(self, request):
        serializer = BibleVariantCreateInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        value = dict(serializer.validated_data)
        variant = create_variant(actor=request.user, project_id=value.pop("project_id"),
            entity_kind=value.pop("entity_kind"), entity_id=value.pop("entity_id"),
            appearance_role=value.pop("appearance_role"),
            primary_setting_revision_id=value.pop("primary_setting_revision_id"),
            primary_setting_sha256=value.pop("primary_setting_sha256"), value=value)
        return Response(self._detail(variant), status=201)

    @extend_schema(request=BibleVariantReviseInput, responses={200: BibleVariantDetail,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",))
    def revise(self, request, pk=None):
        serializer = BibleVariantReviseInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        value = dict(serializer.validated_data)
        variant = revise_variant(actor=request.user, variant=self.get_object(),
            expected_revision=value.pop("expected_revision"), value=value)
        return Response(self._detail(variant))

    @extend_schema(request=BibleVariantReviewInput, responses={200: BibleVariantDetail,
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer, 409: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("post",))
    def review(self, request, pk=None):
        serializer = BibleVariantReviewInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        variant = review_variant(actor=request.user, variant=self.get_object(), **serializer.validated_data)
        return Response(self._detail(variant))

    @extend_schema(parameters=[BibleVariantPageQuery], responses={200: BibleVariantRevisionSummary(many=True),
        400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer})
    @action(detail=True, methods=("get",))
    def revisions(self, request, pk=None):
        query = BibleVariantPageQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = self.get_object().revisions.select_related("review").defer("identity_anchor", "appearance_anchor").order_by("-revision")
        return self.get_paginated_response(BibleVariantRevisionSummary(self.paginate_queryset(rows), many=True).data)
