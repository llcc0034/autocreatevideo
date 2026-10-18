from collections.abc import Mapping

from django.core.exceptions import ValidationError as DjangoValidationError
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from studio.data_structure_serializers import (
    DataStructurePreviewSerializer, DataStructureSummarySerializer, DataStructureVersionSerializer, DataStructureAuthoringOptionsSerializer,
    StructureValueRequestSerializer, StructureValuePreviewSerializer,
    StructureMappingRequestSerializer, StructureMappingPreviewSerializer, StructurePathOptionsSerializer, StructureMappingAuthoringOptionsSerializer,
    InputSourceCatalogSerializer, MediaSourcePreviewRequestSerializer, MediaSourcePreviewSerializer,
)
from studio.models import DataStructureVersion
from studio.pagination import StandardPageNumberPagination
from studio.services.data_structures import compile_data_structure, describe_data_structure, transition_data_structure, preview_structure_value, structure_authoring_options
from studio.services.security_boundaries import boundary_int
from studio.services.json_contracts import JsonExpansionBudget
from studio.services.structure_mapping import mapping_authoring_options, preview_structure_mapping, structure_path_options


class DataStructureFilter(filters.FilterSet):
    structure_key = filters.UUIDFilter()

    class Meta:
        model = DataStructureVersion
        fields = ("structure_key", "status")


class DataStructureVersionViewSet(
    mixins.CreateModelMixin, mixins.UpdateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    queryset = DataStructureVersion.objects.all()
    serializer_class = DataStructureVersionSerializer
    filterset_class = DataStructureFilter
    search_fields = ("name",)

    @extend_schema(responses=DataStructureAuthoringOptionsSerializer)
    @action(detail=False, methods=("get",), url_path="authoring-options", pagination_class=None, filter_backends=[])
    def authoring_options(self, request):
        return Response(structure_authoring_options())

    @extend_schema(responses=StructureMappingAuthoringOptionsSerializer)
    @action(detail=False, methods=("get",), url_path="mapping-options", pagination_class=None, filter_backends=[])
    def mapping_options(self, request):
        return Response(mapping_authoring_options())

    @extend_schema(responses=InputSourceCatalogSerializer)
    @action(detail=False, methods=("get",), url_path="input-source-options", permission_classes=(IsAuthenticated,))
    def input_source_options(self, request):
        from studio.services.input_source_policies import input_source_options
        return Response(input_source_options())

    @extend_schema(request=MediaSourcePreviewRequestSerializer, responses=MediaSourcePreviewSerializer)
    @action(detail=False, methods=("post",), url_path="media-source-preview")
    def media_source_preview(self, request):
        from studio.services.input_source_policies import resolve_media_source_selection
        from studio.services.execution_instances import execution_configuration_max_bytes
        from studio.services.json_contracts import json_contract

        incoming = MediaSourcePreviewRequestSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        data = incoming.validated_data
        try:
            json_contract(data, max_bytes=execution_configuration_max_bytes())
            resolved = resolve_media_source_selection(data["policy"], data["selection"], owner=request.user,
                max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
                max_media_items=boundary_int("security.workflow_smoke.asset_max_items"),
                **({"override": data["override"]} if "override" in data else {}))
        except DjangoValidationError as exc:
            return Response({"preview_only": True, "valid": False, "resolved": None,
                "errors": getattr(exc, "message_dict", None) or {"selection": exc.messages}})
        return Response({"preview_only": True, "valid": True, "resolved": resolved, "errors": {}})

    def get_serializer_class(self):
        return DataStructureSummarySerializer if self.action == "list" else self.serializer_class

    def get_queryset(self):
        query = super().get_queryset().order_by("structure_key", "-version", "id")
        if self.action == "list":
            return query.only("id", "structure_key", "version", "name", "status", "created_at", "updated_at")
        return query

    def _transition(self, request, status):
        structure = self.get_object()
        if not isinstance(request.data, Mapping) or request.data:
            raise ValidationError("状态操作不接受内容修改，请先保存草稿")
        try:
            structure = transition_data_structure(version_id=structure.pk, status=status)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc
        return Response(self.get_serializer(structure).data)

    @extend_schema(request=None, responses=DataStructureVersionSerializer)
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        return self._transition(request, "published")

    @extend_schema(request=None, responses=DataStructureVersionSerializer)
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        return self._transition(request, "retired")

    @extend_schema(responses=DataStructurePreviewSerializer)
    @action(detail=True, methods=("get",), url_path="schema-preview")
    def schema_preview(self, request, pk=None):
        structure = self.get_object()
        try:
            schema = compile_data_structure(structure, require_published_fields=structure.status == "draft")
        except DjangoValidationError as exc:
            return Response({"valid": False, "json_schema": None, "errors": getattr(exc, "message_dict", None) or {"nodes": exc.messages}})
        return Response({"valid": True, "json_schema": schema, "errors": {}})

    @extend_schema(request=StructureValueRequestSerializer, responses=StructureValuePreviewSerializer)
    @action(detail=True, methods=("post",), url_path="value-preview")
    def value_preview(self, request, pk=None):
        structure = self.get_object()
        incoming = StructureValueRequestSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        value = incoming.validated_data["value"]
        try:
            budget = JsonExpansionBudget(boundary_int("security.provider.max_response_bytes"), path="value.preview_budget")
            result = preview_structure_value(structure, value, consume=budget.consume)
        except DjangoValidationError as exc:
            result = {"valid": False, "value": value, "defaults": [],
                "errors": getattr(exc, "message_dict", None) or {"nodes": exc.messages}}
        return Response({"preview_only": True, "structure_version_id": str(structure.pk), **result})

    @extend_schema(responses=StructurePathOptionsSerializer)
    @action(detail=True, methods=("get",), url_path="path-options")
    def path_options(self, request, pk=None):
        structure = self.get_object()
        try:
            options = structure_path_options(describe_data_structure(structure))
        except DjangoValidationError as exc:
            return Response({"structure_version_id": str(structure.pk), "valid": False, "options": [],
                "errors": getattr(exc, "message_dict", None) or {"nodes": exc.messages}})
        return Response({"structure_version_id": str(structure.pk), "valid": True, "options": options, "errors": {}})

    @extend_schema(request=StructureMappingRequestSerializer, responses=StructureMappingPreviewSerializer)
    @action(detail=True, methods=("post",), url_path="mapping-preview")
    def mapping_preview(self, request, pk=None):
        target = self.get_object()
        incoming = StructureMappingRequestSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        data = incoming.validated_data
        try:
            source_contract = describe_data_structure(data["source_structure_version"])
            target_contract = describe_data_structure(target)
            report = preview_structure_mapping(source_contract=source_contract, target_contract=target_contract,
                source_text=data["source_text"], mapping=data["mapping"],
                max_bytes=boundary_int("security.provider.max_response_bytes"))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc
        return Response(report)
