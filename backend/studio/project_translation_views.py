from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, OuterRef, Subquery
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from studio.models import ExecutionPreparation, ProjectTranslationRequest, ProjectTranslationSource
from studio.asset_generation_serializers import AssetGenerationOptionSerializer
from studio.services.asset_generation import asset_generation_options
from studio.services.content_contracts import translation_request_max_entries
from studio.project_translation_serializers import (
    ProjectTranslationAdoptSerializer, ProjectTranslationCandidateSerializer, ProjectTranslationCreateSerializer,
    ProjectTranslationRequestSerializer, ProjectTranslationSourceSerializer,
    ProjectTranslationOptionsSerializer,
)
from studio.services.project_translation_requests import (
    adopt_project_translation_candidate, create_project_translation_request, read_project_translation_candidate,
)
from studio.services.prompt_translation import PromptTranslationError


def _error(exc):
    messages = exc.messages if isinstance(exc, DjangoValidationError) else [str(exc)]
    if getattr(exc, "code", "") in {"translation_source_conflict", "translation_request_conflict"}:
        return Response({"code": exc.code, "detail": messages}, status=409)
    raise serializers.ValidationError(messages) from exc


def _private(response):
    response["Cache-Control"] = "private, no-store"
    return response


def _page_query(request):
    if set(request.query_params) - {"page", "page_size", "format"} or any(len(values) != 1 for _, values in request.query_params.lists()):
        raise serializers.ValidationError("只支持分页参数")


REQUEST_PATH = OpenApiParameter("translation_request_id", OpenApiTypes.UUID, OpenApiParameter.PATH)
SOURCE_PATH = OpenApiParameter("source_id", OpenApiTypes.UUID, OpenApiParameter.PATH)


class ProjectTranslationRequestsMixin:
    @extend_schema(responses=ProjectTranslationOptionsSerializer)
    @action(detail=True, methods=("get",), url_path="translations/options", filter_backends=[])
    def translation_options(self, request, pk=None):
        self.get_object()
        _page_query(request)
        rows = asset_generation_options(user=request.user, job_type="prompt", media_type="text").filter(
            operation="prompt.normalize", workflow_version__isnull=True)
        page = self.paginate_queryset(rows)
        response = self.get_paginated_response(AssetGenerationOptionSerializer(page, many=True).data)
        fields = ProjectTranslationCreateSerializer().fields
        response.data.update(source_lang=next(iter(fields["source_lang"].choices)),
            target_lang=next(iter(fields["target_lang"].choices)), max_entries=translation_request_max_entries())
        return _private(response)

    def _translation_requests(self, project):
        rows = ProjectTranslationRequest.objects.filter(project=project)
        if not self.request.user.is_staff:
            rows = rows.filter(job__owner=self.request.user)
        source_count = ProjectTranslationSource.objects.filter(request_id=OuterRef("pk")).order_by().values("request_id").annotate(total=Count("id")).values("total")
        return rows.select_related("job").only(
            "id", "job_id", "created_at", "job__status", "job__execution_instance_version_id").annotate(source_count=Subquery(source_count),
            preparation_id=Subquery(ExecutionPreparation.objects.filter(asset_job_id=OuterRef("job_id")).values("id")[:1])
        ).order_by("-created_at", "-id")

    def _translation_row(self, row):
        if row.preparation_id is None or row.source_count is None or row.source_count < 1:
            raise serializers.ValidationError("翻译任务的原执行准备或字段来源缺失，不能恢复")
        return {"id": row.pk, "job_id": row.job_id, "preparation_id": row.preparation_id,
            "execution_instance_version_id": row.job.execution_instance_version_id,
            "status": row.job.status, "created_at": row.created_at, "source_count": row.source_count}

    @extend_schema(responses=ProjectTranslationRequestSerializer(many=True))
    @action(detail=True, methods=("get",), url_path="translations/requests", filter_backends=[])
    def translation_requests(self, request, pk=None):
        _page_query(request)
        project = self.get_object()
        page = self.paginate_queryset(self._translation_requests(project))
        return _private(self.get_paginated_response(ProjectTranslationRequestSerializer(
            [self._translation_row(row) for row in page], many=True).data))

    @extend_schema(request=ProjectTranslationCreateSerializer, responses={200: ProjectTranslationRequestSerializer, 201: ProjectTranslationRequestSerializer},
        description="Create original asynchronous text-candidate Job and Preparation with exact project source/revision bindings. Requires Idempotency-Key; does not call a model or adopt translations.")
    @translation_requests.mapping.post
    def create_translation_request(self, request, pk=None):
        project = self.get_object()
        serializer = ProjectTranslationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            saved, created = create_project_translation_request(project_id=project.pk, actor=request.user,
                client_idempotency_key=request.headers.get("Idempotency-Key", ""), **serializer.validated_data)
        except (DjangoValidationError, PromptTranslationError) as exc:
            return _error(exc)
        row = self._translation_requests(project).get(pk=saved.pk)
        return _private(Response(ProjectTranslationRequestSerializer(self._translation_row(row)).data, status=201 if created else 200))

    @extend_schema(responses=ProjectTranslationSourceSerializer(many=True), parameters=[REQUEST_PATH])
    @action(detail=True, methods=("get",), url_path=r"translations/requests/(?P<translation_request_id>[^/.]+)/sources", filter_backends=[])
    def translation_sources(self, request, pk=None, translation_request_id=None):
        _page_query(request)
        saved = get_object_or_404(self._translation_requests(self.get_object()), pk=serializers.UUIDField().run_validation(translation_request_id))
        rows = saved.sources.only("id", "position", "scope_type", "scope_id", "field_key", "expected_revision",
            "adopted_entry_id", "adopted_revision", "adopted_at")
        page = self.paginate_queryset(rows)
        return _private(self.get_paginated_response(ProjectTranslationSourceSerializer(page, many=True).data))

    @extend_schema(responses=ProjectTranslationCandidateSerializer, parameters=[REQUEST_PATH, SOURCE_PATH])
    @action(detail=True, methods=("get",), url_path=r"translations/requests/(?P<translation_request_id>[^/.]+)/sources/(?P<source_id>[^/.]+)", filter_backends=[])
    def translation_candidate(self, request, pk=None, translation_request_id=None, source_id=None):
        saved = get_object_or_404(self._translation_requests(self.get_object()), pk=serializers.UUIDField().run_validation(translation_request_id))
        source_id = serializers.UUIDField().run_validation(source_id)
        get_object_or_404(saved.sources.only("id"), pk=source_id)
        try:
            value = read_project_translation_candidate(request_id=saved.pk, source_id=source_id, actor=request.user)
        except (DjangoValidationError, PromptTranslationError) as exc:
            return _error(exc)
        return _private(Response(ProjectTranslationCandidateSerializer({**value, "source_text": value["source"].source_text}).data))

    @extend_schema(request=ProjectTranslationAdoptSerializer, responses=ProjectTranslationSourceSerializer, parameters=[REQUEST_PATH, SOURCE_PATH],
        description="Explicit adoption as a draft translation only. Source/manual revision and relevant identity/context must still match; repeated adoption returns the original receipt. Does not confirm or promote project fields.")
    @translation_candidate.mapping.post
    def adopt_translation_candidate(self, request, pk=None, translation_request_id=None, source_id=None):
        saved = get_object_or_404(self._translation_requests(self.get_object()), pk=serializers.UUIDField().run_validation(translation_request_id))
        source_id = serializers.UUIDField().run_validation(source_id)
        get_object_or_404(saved.sources.only("id"), pk=source_id)
        serializer = ProjectTranslationAdoptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            source, _created = adopt_project_translation_candidate(request_id=saved.pk, source_id=source_id, actor=request.user)
        except (DjangoValidationError, PromptTranslationError) as exc:
            return _error(exc)
        return _private(Response(ProjectTranslationSourceSerializer(source).data))
