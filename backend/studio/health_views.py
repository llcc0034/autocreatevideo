from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE
from rest_framework.views import APIView

from studio.services.readiness import collect_readiness


class ReadinessView(APIView):
    """Dependency probe for traffic admission; unlike liveness it may return 503."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(responses=dict)
    def get(self, request):
        payload = collect_readiness()
        return Response(
            payload,
            status=HTTP_200_OK if payload["status"] == "ready" else HTTP_503_SERVICE_UNAVAILABLE,
        )
