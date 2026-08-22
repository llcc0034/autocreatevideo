from __future__ import annotations

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.bible_image_candidate_serializers import (
    BibleImageCandidateCreateResponseSerializer,
    BibleImageCandidateErrorResponseSerializer,
    BibleImageCandidateRequestSerializer,
    BibleImageCandidatesResponseSerializer,
)
from studio.models import CharacterBible, PropBible, SceneBible, SeriesProject
from studio.serializers import APIValidationErrorEnvelopeSerializer
from studio.services.bible_image_candidates import (
    create_bible_image_candidate,
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
    parser_classes = [JSONParser]

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

    @extend_schema(
        request=BibleImageCandidateRequestSerializer,
        responses={
            200: BibleImageCandidateCreateResponseSerializer,
            201: BibleImageCandidateCreateResponseSerializer,
            400: APIValidationErrorEnvelopeSerializer,
            404: BibleImageCandidateErrorResponseSerializer,
            409: BibleImageCandidateErrorResponseSerializer,
            503: BibleImageCandidateErrorResponseSerializer,
        },
        description=(
            "Create one review-only Bible image candidate through the fixed active global "
            "minimax-reference-image system template. The image is not an Asset until an "
            "authorized user promotes a successful task output."
        ),
    )
    def post(self, request, id):
        request_serializer = BibleImageCandidateRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        data = request_serializer.validated_data
        creation = create_bible_image_candidate(
            user=request.user,
            project_id=id,
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            idempotency_key=data["idempotency_key"],
            extra_direction=data["extra_direction"],
            prompt_overrides=data["prompt_overrides"],
            seed=data.get("seed"),
            priority=data["priority"],
            max_attempts=data["max_attempts"],
            extended_generation_confirmed=data["extended_generation_confirmed"],
        )
        project = self._project(request, creation.project.id)
        projection = project_bible_image_candidates(project, request=request)
        candidate = next(
            item for item in projection["candidates"]
            if item["entity_type"] == creation.entity_type
            and item["entity_id"] == str(creation.entity.id)
        )
        payload = {
            "project_id": str(project.id),
            "prompt_artifact_id": str(creation.prompt.id),
            "generated_workflow_id": str(creation.workflow.id),
            "generation_task_id": str(creation.task.id),
            "idempotent_replay": creation.idempotent_replay,
            "review_required": True,
            "persisted_as_asset": False,
            "candidate": candidate,
        }
        response_serializer = BibleImageCandidateCreateResponseSerializer(data=payload)
        response_serializer.is_valid(raise_exception=True)
        return Response(
            response_serializer.validated_data,
            status=(
                status.HTTP_200_OK
                if creation.idempotent_replay
                else status.HTTP_201_CREATED
            ),
        )
