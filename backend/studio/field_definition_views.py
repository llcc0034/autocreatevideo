"""Shared field-version authoring; never creates execution or business values."""
from collections.abc import Mapping

from django.core.exceptions import ValidationError as DjangoValidationError
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from studio.field_definition_serializers import (
    FieldDefinitionVersionOptionsSerializer,
    FieldDefinitionVersionSerializer,
    FieldDefinitionVersionSummarySerializer,
)
from studio.models import FieldDefinitionVersion
from studio.pagination import StandardPageNumberPagination
from studio.services.field_definitions import (
    publish_field_definition_version,
    retire_field_definition_version,
)


class FieldDefinitionVersionFilter(filters.FilterSet):
    # UUID filtering does not fetch the identity's full, unrelated definition body.
    field_definition = filters.UUIDFilter(field_name="field_definition_id")

    class Meta:
        model = FieldDefinitionVersion
        fields = ("field_definition", "status", "value_type", "category")


class FieldDefinitionVersionViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsAdminUser,)
    pagination_class = StandardPageNumberPagination
    serializer_class = FieldDefinitionVersionSerializer
    queryset = FieldDefinitionVersion.objects.select_related("field_definition")
    filterset_class = FieldDefinitionVersionFilter
    search_fields = ("field_definition__key", "label")

    def get_queryset(self):
        query = super().get_queryset().order_by("field_definition_id", "-version", "id")
        if self.action == "list":
            query = query.only(
                "id", "field_definition_id", "field_definition__key", "version", "status",
                "label", "category", "value_type", "unit", "created_at", "updated_at",
            )
        return query

    def get_serializer_class(self):
        if self.action == "list":
            return FieldDefinitionVersionSummarySerializer
        return super().get_serializer_class()

    def _transition(self, request, transition):
        version = self.get_object()
        if not isinstance(request.data, Mapping) or request.data:
            raise ValidationError("版本状态操作不接受字段修改；请先保存草稿")
        try:
            version = transition(version_id=version.pk)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, "message_dict", None) or exc.messages) from exc
        return Response(self.get_serializer(version).data)

    @extend_schema(request=None, responses=FieldDefinitionVersionSerializer)
    @action(detail=True, methods=("post",))
    def publish(self, request, pk=None):
        return self._transition(request, publish_field_definition_version)

    @extend_schema(request=None, responses=FieldDefinitionVersionSerializer)
    @action(detail=True, methods=("post",))
    def retire(self, request, pk=None):
        return self._transition(request, retire_field_definition_version)

    @extend_schema(request=None, responses=FieldDefinitionVersionOptionsSerializer)
    @action(detail=False, methods=("get",), url_path="options", pagination_class=None, filter_backends=[])
    def registry_options(self, request):
        return Response(FieldDefinitionVersionOptionsSerializer({
            "value_types": [{"value": value, "label": label} for value, label in FieldDefinitionVersion.ValueType.choices],
            "statuses": [{"value": value, "label": label} for value, label in FieldDefinitionVersion.Status.choices],
        }).data)
