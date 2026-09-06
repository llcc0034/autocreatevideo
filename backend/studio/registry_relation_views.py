"""Staff-only, paginated relationship browser. No execution endpoints."""
from drf_spectacular.utils import extend_schema
from django.utils import timezone
from django.db.models import OuterRef, Q
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.views import APIView

from studio.pagination import AcceptanceSmokePagination
from studio.services.registry_relations import MODELS, catalog, fields, groups, row_summary, summary_queryset


class RegistryRelationQuerySerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=list(MODELS))
    ids = serializers.CharField(required=False, max_length=1850)
    source_kind = serializers.ChoiceField(choices=list(MODELS), required=False)
    source_id = serializers.UUIDField(required=False)
    relation = serializers.CharField(required=False, max_length=40)
    search = serializers.CharField(required=False, max_length=160)
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(required=False, min_value=1, max_value=50)

    def validate(self, value):
        source = [key in value for key in ("source_kind", "source_id", "relation")]
        if any(source) and not all(source):
            raise serializers.ValidationError("关联筛选必须同时提供 source_kind、source_id、relation。")
        if "ids" in value:
            parts = value["ids"].split(",")
            if not 1 <= len(parts) <= 50:
                raise serializers.ValidationError("每批最多读取 50 个精确对象。")
            value["id_list"] = [serializers.UUIDField().run_validation(part) for part in parts]
        return value


class RegistryRelationGroupSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    kind = serializers.CharField()
    count = serializers.IntegerField(min_value=0)
    url = serializers.CharField()


class RegistryRelationRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.CharField()
    label = serializers.CharField()
    status = serializers.CharField()
    config_url = serializers.CharField()
    overview_url = serializers.CharField()
    acceptance_url = serializers.CharField(allow_blank=True)
    groups = RegistryRelationGroupSerializer(many=True)
    notices = serializers.ListField(child=serializers.CharField())
    capabilities = serializers.ListField(child=serializers.CharField())


class RegistryRelationPageSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    generated_at = serializers.DateTimeField()
    context_label = serializers.CharField(allow_blank=True)
    results = RegistryRelationRowSerializer(many=True)


class RegistryRelationsView(APIView):
    permission_classes = (IsAdminUser,)

    @extend_schema(parameters=[RegistryRelationQuerySerializer], responses=RegistryRelationPageSerializer)
    def get(self, request):
        query = RegistryRelationQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        kind = data["kind"]
        queryset = summary_queryset(kind)
        context_label = ""
        if "source_id" in data:
            names = set()
            def collect(name):
                names.add(name)
                return OuterRef(name)
            groups(data["source_kind"], collect)
            source = catalog(data["source_kind"]).filter(pk=data["source_id"]).values(*set(names).union(fields(data["source_kind"]))).first()
            if source is None:
                raise NotFound("关联来源已删除或不存在，请返回目录重新选择。")
            selected = next((group for group in groups(data["source_kind"], source.__getitem__) if group.key == data["relation"]), None)
            if selected is None or selected.kind != kind:
                raise ValidationError("此对象不支持指定关联分组。")
            queryset = queryset.filter(pk__in=selected.queryset.values("pk"))
            name = source.get("profile_key") or source.get("name") or source.get("template__name") or source.get("projector__name")
            context_label = f"{name}" + (f" v{source['version']}" if "version" in source else "") + " → " + selected.label
        if "id_list" in data:
            queryset = queryset.filter(pk__in=data["id_list"])
        if data.get("search"):
            searchable = {"model": ["profile_key", "model_identifier"], "deployment": ["name", "key"],
                          "workflow": ["template__name", "template__slug"], "prompt": ["name", "profile_key"],
                          "binding": ["name", "key", "model_family", "operation"],
                          "projector": ["projector__name", "projector__key"], "recipe": ["name", "recipe_key"]}
            predicate = Q()
            for field in searchable[kind]: predicate |= Q(**{field + "__icontains": data["search"]})
            queryset = queryset.filter(predicate)
        relation_fields = ["relation_" + group.key for group in groups(kind, OuterRef)]
        paginator = AcceptanceSmokePagination()
        paginator.acceptance_max_page_size = 50
        page = paginator.paginate_queryset(queryset.order_by("id").values(*fields(kind), *relation_fields), request, view=self)
        rows = [row_summary(kind, row) for row in page]
        response = paginator.get_paginated_response(RegistryRelationRowSerializer(rows, many=True).data)
        response.data.update(generated_at=timezone.now().isoformat(), context_label=context_label)
        return response
