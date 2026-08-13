from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.services.h3_prompt_optimizer import get_h3_prompt_optimizer_capability
from studio.services.creative_drafts import get_creative_capability
from studio.services.prompt_translation import get_translation_capability


H3PromptOptimizerCapabilityResponse = inline_serializer(
    name="H3PromptOptimizerCapability",
    fields={
        "enabled": serializers.BooleanField(),
        "configured": serializers.BooleanField(),
        "transport": serializers.ChoiceField(
            choices=("local", "remote", "unconfigured"),
        ),
        "model": serializers.CharField(allow_blank=True),
        "failure_mode": serializers.ChoiceField(choices=("warn", "block")),
        "language_gate": serializers.ChoiceField(choices=("warn", "block")),
    },
)


class H3PromptOptimizerCapabilityView(APIView):
    """Expose only the authenticated user's safe server capability snapshot."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=H3PromptOptimizerCapabilityResponse)
    def get(self, request):
        return Response(get_h3_prompt_optimizer_capability())


class PromptTranslationCapabilityView(APIView):
    """Safe snapshot of the disposable local translation runtime."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(get_translation_capability())


class CreativeDraftCapabilityView(APIView):
    """Safe snapshot of the disposable local structured-creation runtime."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(get_creative_capability())
