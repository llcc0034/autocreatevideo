from __future__ import annotations

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import APIException, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.bible_image_candidate_serializers import (
    BibleImageCandidateErrorResponseSerializer,
    BibleImageCandidatesResponseSerializer,
)
from studio.models import CharacterBible, PropBible, SceneBible, SeriesProject
from studio.services.bible_image_candidates import (
    BibleImageCandidateProjectionIntegrityError,
    project_bible_image_candidates,
)


class BibleImageCandidateIntegrityConflict(APIException):
    status_code = 409
    default_code = "bible_image_candidate_provenance_unattributable"
    default_detail = "Bible 参考图历史存在无法归属的损坏 provenance。"


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
            "Read the current Bible bindings and every formal ProductionRun "
            "bible_image_generation candidate version. This request is read-only "
            "and never creates, selects or promotes a candidate."
        ),
    )
    def get(self, request, id):
        try:
            payload = project_bible_image_candidates(
                self._project(request, id), request=request,
            )
        except BibleImageCandidateProjectionIntegrityError as exc:
            raise BibleImageCandidateIntegrityConflict() from exc
        response_serializer = BibleImageCandidatesResponseSerializer(data=payload)
        response_serializer.is_valid(raise_exception=True)
        return Response(response_serializer.validated_data)
