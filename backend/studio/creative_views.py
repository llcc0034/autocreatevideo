from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.models import Episode, SeriesProject
from studio.serializers import EpisodeCreativeDraftSerializer


def _owned_project(user, project_id) -> SeriesProject:
    queryset = SeriesProject.objects.all()
    if not user.is_staff:
        queryset = queryset.filter(owner=user)
    return get_object_or_404(queryset, pk=project_id)


def _owned_episode(project: SeriesProject, episode_id) -> Episode:
    return get_object_or_404(Episode, pk=episode_id, project=project)


class ProjectCreativeDraftListView(APIView):
    """Read immutable facts from creative drafts created by the retired path."""

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    @extend_schema(responses=EpisodeCreativeDraftSerializer(many=True))
    def get(self, request, project_id):
        project = _owned_project(request.user, project_id)
        queryset = project.creative_drafts.select_related(
            "project", "episode", "parent_draft", "confirmed_by", "applied_by",
        )
        episode_id = request.query_params.get("episode")
        if episode_id:
            queryset = queryset.filter(episode=_owned_episode(project, episode_id))
        return Response(EpisodeCreativeDraftSerializer(queryset, many=True).data)
