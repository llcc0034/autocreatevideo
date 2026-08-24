from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.models import Episode, SeriesProject, Shot, ShotDetailDraft
from studio.serializers import ShotDetailDraftSerializer


def _owned_project(user, project_id) -> SeriesProject:
    queryset = SeriesProject.objects.select_related("style")
    if not user.is_staff:
        queryset = queryset.filter(owner=user)
    return get_object_or_404(queryset, pk=project_id)


def _owned_shot(project: SeriesProject, shot_id) -> Shot:
    return get_object_or_404(
        Shot.objects.select_related("episode", "episode__project", "scene"),
        pk=shot_id,
        episode__project=project,
    )


class ProjectShotDetailDraftListView(APIView):
    """Read-only access to historical high-detail proposals."""

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    @extend_schema(responses=ShotDetailDraftSerializer(many=True))
    def get(self, request, project_id):
        project = _owned_project(request.user, project_id)
        queryset = project.shot_detail_drafts.select_related(
            "project",
            "project__style",
            "episode",
            "shot",
            "parent_draft",
            "confirmed_by",
            "applied_by",
        )
        shot_id = request.query_params.get("shot")
        episode_id = request.query_params.get("episode")
        if shot_id:
            queryset = queryset.filter(shot=_owned_shot(project, shot_id))
        if episode_id:
            episode = get_object_or_404(Episode, pk=episode_id, project=project)
            queryset = queryset.filter(episode=episode)
        return Response(ShotDetailDraftSerializer(queryset, many=True).data)
