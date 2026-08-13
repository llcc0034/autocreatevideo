from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.asset_draft_serializers import (
    AssetBibleDraftRequestSerializer,
    AssetBibleDraftResponseSerializer,
)
from studio.models import SeriesProject
from studio.services.asset_bible_drafts import (
    AssetBibleDraftError,
    AssetBibleDraftConflict,
    AssetBibleDraftResourceBusy,
    AssetBibleDraftRuntimeUnavailable,
    AssetBibleDraftValidationError,
    generate_asset_bible_draft,
)


class AssetDraftBusy(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "asset_draft_resource_busy"


class AssetDraftConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "asset_draft_source_conflict"


class AssetDraftUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "asset_draft_runtime_unavailable"


class ProjectAssetBibleDraftGenerateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AssetBibleDraftRequestSerializer,
        responses={200: AssetBibleDraftResponseSerializer},
        description=(
            "Generate an editable Bible candidate from selected assets' saved textual metadata. "
            "No image pixels are read and no CharacterBible, SceneBible or PropBible row is created."
        ),
    )
    def post(self, request, project_id):
        queryset = SeriesProject.objects.select_related("owner")
        if not request.user.is_staff:
            queryset = queryset.filter(owner=request.user)
        try:
            project = queryset.get(pk=project_id)
        except SeriesProject.DoesNotExist:
            # Keep cross-tenant project identifiers indistinguishable from
            # genuinely missing projects.
            from rest_framework.exceptions import NotFound

            raise NotFound("项目不存在")

        serializer = AssetBibleDraftRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = generate_asset_bible_draft(
                project=project,
                user=request.user,
                entity_type=data["entity_type"],
                output_language=data["output_language"],
                asset_id=data["asset_id"],
                asset_checksum=data["asset_checksum"],
                attempt=data["attempt"],
            )
        except AssetBibleDraftValidationError as exc:
            raise ValidationError(str(exc)) from exc
        except AssetBibleDraftResourceBusy as exc:
            raise AssetDraftBusy(str(exc)) from exc
        except AssetBibleDraftConflict as exc:
            raise AssetDraftConflict(str(exc)) from exc
        except AssetBibleDraftRuntimeUnavailable as exc:
            raise AssetDraftUnavailable(str(exc)) from exc
        except AssetBibleDraftError as exc:
            raise AssetDraftUnavailable(str(exc)) from exc
        response = AssetBibleDraftResponseSerializer(result)
        return Response(response.data)
