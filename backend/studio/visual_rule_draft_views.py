from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.models import SeriesProject
from studio.services.visual_rule_drafts import (
    VisualRuleDraftConflict,
    VisualRuleDraftError,
    VisualRuleDraftResourceBusy,
    VisualRuleDraftRuntimeUnavailable,
    VisualRuleDraftValidationError,
    apply_visual_rule_draft,
    generate_visual_rule_draft,
)
from studio.visual_rule_draft_serializers import (
    VisualRuleDraftApplyResponseSerializer,
    VisualRuleDraftApplySerializer,
    VisualRuleDraftGenerateSerializer,
    VisualRuleDraftResponseSerializer,
)


class VisualRulesDraftBusy(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "visual_rules_resource_busy"


class VisualRulesDraftConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "visual_rules_source_conflict"


class VisualRulesDraftUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "visual_rules_runtime_unavailable"


def _owned_project(request, project_id) -> SeriesProject:
    queryset = SeriesProject.objects.select_related("owner", "style")
    if not request.user.is_staff:
        queryset = queryset.filter(owner=request.user)
    try:
        return queryset.get(pk=project_id)
    except SeriesProject.DoesNotExist as exc:
        raise NotFound("项目不存在") from exc


class ProjectVisualRuleDraftGenerateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=VisualRuleDraftGenerateSerializer,
        responses={200: VisualRuleDraftResponseSerializer},
        description=(
            "Generate an isolated, English-only project visual-rule candidate from persisted "
            "project, episode-summary, style, Bible and glossary text. No project field is changed."
        ),
    )
    def post(self, request, project_id):
        project = _owned_project(request, project_id)
        serializer = VisualRuleDraftGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = generate_visual_rule_draft(
                project=project,
                user=request.user,
                attempt=data["attempt"],
                direction_note=data["direction_note"],
                variation_note=data["variation_note"],
                expected_context_fingerprint=data.get("expected_context_fingerprint", ""),
            )
        except VisualRuleDraftValidationError as exc:
            raise ValidationError(str(exc)) from exc
        except VisualRuleDraftResourceBusy as exc:
            raise VisualRulesDraftBusy(str(exc)) from exc
        except VisualRuleDraftConflict as exc:
            raise VisualRulesDraftConflict(str(exc)) from exc
        except (VisualRuleDraftRuntimeUnavailable, VisualRuleDraftError) as exc:
            raise VisualRulesDraftUnavailable(str(exc)) from exc
        return Response(VisualRuleDraftResponseSerializer(result).data)


class ProjectVisualRuleDraftApplyView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=VisualRuleDraftApplySerializer,
        responses={200: VisualRuleDraftApplyResponseSerializer},
        description=(
            "Explicitly apply a reviewed visual-rule candidate. The project is locked and its "
            "authoritative source fingerprint is rechecked before the three existing fields are updated."
        ),
    )
    def post(self, request, project_id):
        project = _owned_project(request, project_id)
        serializer = VisualRuleDraftApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = apply_visual_rule_draft(
                project=project,
                user=request.user,
                context_fingerprint=data["context_fingerprint"],
                color_design=data["color_design"],
                attempt=data["attempt"],
                direction_note=data["direction_note"],
                variation_note=data["variation_note"],
                generation_fingerprint=data["generation_fingerprint"],
            )
        except VisualRuleDraftValidationError as exc:
            raise ValidationError(str(exc)) from exc
        except VisualRuleDraftConflict as exc:
            raise VisualRulesDraftConflict(str(exc)) from exc
        except VisualRuleDraftError as exc:
            raise VisualRulesDraftUnavailable(str(exc)) from exc
        return Response(VisualRuleDraftApplyResponseSerializer(result).data)
