from __future__ import annotations

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.bible_image_candidate_serializers import (
    BibleImageCandidateErrorResponseSerializer,
    BibleImageCandidatesResponseSerializer,
)
from studio.models import CharacterBible, PropBible, SceneBible, SeriesProject
from studio.services.bible_image_candidates import (
    project_bible_image_candidates,
)


def _project_queryset(user):
    queryset = SeriesProject.objects.select_related("owner", "style").prefetch_related(
        Prefetch(
            "characters",
            queryset=CharacterBible.objects.select_related("anchor_asset").prefetch_related(
                "reference_assets",
            ),
        ),
        Prefetch(
            "scenes",
            queryset=SceneBible.objects.select_related("anchor_asset").prefetch_related(
                "reference_assets",
            ),
        ),
        Prefetch(
            "props",
            queryset=PropBible.objects.select_related("anchor_asset").prefetch_related(
                "reference_assets",
            ),
        ),
    )
    if not getattr(user, "is_staff", False):
        queryset = queryset.filter(owner=user)
    return queryset


class ProjectBibleImageCandidatesView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    def _project(self, request, project_id):
        project = _project_queryset(request.user).filter(pk=project_id).first()
        if project is None:
            raise NotFound("连续剧项目不存在")
        return project

    @extend_schema(
        responses={
            200: BibleImageCandidatesResponseSerializer,
            404: BibleImageCandidateErrorResponseSerializer,
        },
        description=(
            "Read the current Bible entity projection and latest persisted "
            "PromptArtifact → GeneratedWorkflow → GenerationTask candidate chain. "
            "This request is read-only and never creates candidate rows."
        ),
    )
    def get(self, request, id):
        payload = project_bible_image_candidates(
            self._project(request, id), request=request,
        )
        response_serializer = BibleImageCandidatesResponseSerializer(data=payload)
        response_serializer.is_valid(raise_exception=True)
        return Response(response_serializer.validated_data)
