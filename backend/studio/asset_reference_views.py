from django.shortcuts import get_object_or_404
from django.db.models import Exists, OuterRef, Q
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from studio.asset_reference_serializers import (
    AssetReferenceSeriesCreate, AssetReferenceSeriesInput, AssetReferenceSeriesQuery,
    AssetReferenceSeriesSummary, AssetReferenceVersionCreate, AssetReferenceVersionQuery,
    AssetReferenceVersionSummary,
    AssetReferenceBibleQuery, AssetReferenceBibleOption, AssetReferenceRemovalImpact,
)
from studio.pagination import StandardPageNumberPagination
from studio.platform_serializers import ConfigurationErrorResponseSerializer
from studio.services.asset_references import (
    attach_asset, create_series, remove_series, remove_version, series_rows,
    series_summaries, update_series, version_summaries,
    bible_options, removal_impact,
)


ERRORS = {400: ConfigurationErrorResponseSerializer, 404: ConfigurationErrorResponseSerializer,
    409: ConfigurationErrorResponseSerializer}
UUID_PATH = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _input(cls, data, **kwargs):
    value = cls(data=data, **kwargs)
    value.is_valid(raise_exception=True)
    return value.validated_data


class AssetReferenceSeriesViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    lookup_value_regex = UUID_PATH
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    serializer_class = AssetReferenceSeriesSummary
    http_method_names = ("get", "post", "patch", "delete", "head", "options")

    def get_queryset(self):
        return series_summaries(self.request.user)

    @extend_schema(parameters=[AssetReferenceSeriesQuery], responses={200: AssetReferenceSeriesSummary(many=True), **ERRORS})
    def list(self, request):
        # Query booleans are optional filters, not unchecked HTML form checkboxes.
        query = _input(AssetReferenceSeriesQuery, request.query_params.dict())
        rows = self.get_queryset()
        for key in ("role", "primary_series_id", "bible_variant_id"):
            if key in query:
                rows = rows.filter(**{key: query[key]})
        if "search" in query:
            rows = rows.filter(name__icontains=query["search"])
        if "bible_linked" in query:
            rows = rows.filter(bible_variant_id__isnull=not query["bible_linked"])
        if "media_type" in query or "asset_id" in query:
            versions = version_summaries(request.user).filter(series_id=OuterRef("pk"))
            if "media_type" in query:
                versions = versions.filter(asset__mime_type__startswith=query["media_type"] + "/")
            if "asset_id" in query:
                versions = versions.filter(asset_id=query["asset_id"])
            rows = rows.filter(Exists(versions))
        return self.get_paginated_response(self.get_serializer(self.paginate_queryset(rows), many=True).data)

    def _response(self, row, status=200):
        return Response(self.get_serializer(self.get_queryset().get(pk=row.pk)).data, status=status)

    @extend_schema(parameters=[AssetReferenceBibleQuery], responses={200: AssetReferenceBibleOption(many=True), **ERRORS})
    @action(detail=True, methods=("get",), url_path="bible-options")
    def bible_options(self, request, pk=None):
        query = _input(AssetReferenceBibleQuery, request.query_params.dict())
        rows = bible_options(self.get_object())
        if "id" in query:
            rows = rows.filter(pk=query["id"])
        if query.get("search"):
            word = query["search"]
            rows = rows.filter(Q(reference_name__icontains=word) | Q(name__icontains=word)
                | Q(project__title__icontains=word) | Q(character__name__icontains=word)
                | Q(scene__name__icontains=word) | Q(prop__name__icontains=word))
        return self.get_paginated_response(AssetReferenceBibleOption(self.paginate_queryset(rows), many=True).data)

    @extend_schema(responses={200: AssetReferenceRemovalImpact, **ERRORS})
    @action(detail=True, methods=("get",), url_path="removal-impact", pagination_class=None)
    def removal_impact(self, request, pk=None):
        return Response(AssetReferenceRemovalImpact(removal_impact(self.get_object())).data)

    @extend_schema(request=AssetReferenceSeriesCreate, responses={201: AssetReferenceSeriesSummary, **ERRORS})
    def create(self, request):
        return self._response(create_series(actor=request.user,
            values=_input(AssetReferenceSeriesCreate, request.data)), 201)

    @extend_schema(request=AssetReferenceSeriesInput, responses={200: AssetReferenceSeriesSummary, **ERRORS})
    def partial_update(self, request, pk=None):
        return self._response(update_series(actor=request.user, series_id=pk,
            values=_input(AssetReferenceSeriesInput, request.data, partial=True)))

    @extend_schema(responses={204: None, **ERRORS})
    def destroy(self, request, pk=None):
        remove_series(actor=request.user, series_id=pk)
        return Response(status=204)


class AssetReferenceVersionViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    lookup_value_regex = UUID_PATH
    permission_classes = (IsAuthenticated,)
    pagination_class = StandardPageNumberPagination
    serializer_class = AssetReferenceVersionSummary
    http_method_names = ("get", "post", "delete", "head", "options")

    def get_queryset(self):
        return version_summaries(self.request.user)

    @extend_schema(responses={200: AssetReferenceRemovalImpact, **ERRORS})
    @action(detail=True, methods=("get",), url_path="removal-impact", pagination_class=None)
    def removal_impact(self, request, pk=None):
        return Response(AssetReferenceRemovalImpact(removal_impact(self.get_object())).data)

    @extend_schema(parameters=[AssetReferenceVersionQuery], responses={200: AssetReferenceVersionSummary(many=True), **ERRORS})
    def list(self, request):
        query = _input(AssetReferenceVersionQuery, request.query_params)
        series = get_object_or_404(series_rows(request.user), pk=query["series_id"])
        rows = self.get_queryset().filter(series=series).order_by("-version", "id")
        return self.get_paginated_response(self.get_serializer(self.paginate_queryset(rows), many=True).data)

    @extend_schema(request=AssetReferenceVersionCreate,
        responses={200: AssetReferenceVersionSummary, 201: AssetReferenceVersionSummary, **ERRORS})
    def create(self, request):
        row, created = attach_asset(actor=request.user, **_input(AssetReferenceVersionCreate, request.data))
        return Response(self.get_serializer(self.get_queryset().get(pk=row.pk)).data, status=201 if created else 200)

    @extend_schema(responses={204: None, **ERRORS})
    def destroy(self, request, pk=None):
        remove_version(actor=request.user, version_id=pk)
        return Response(status=204)
