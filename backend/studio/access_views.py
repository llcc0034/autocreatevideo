from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.access_serializers import (
    AccessAuditEventSerializer,
    AccessRoleChangeSerializer,
    AccessStatusChangeSerializer,
    AccessUserCreateSerializer,
    AccessUserSerializer,
    PasswordChangeSerializer,
    TemporaryPasswordResetSerializer,
    TemporaryPasswordResponseSerializer,
)
from studio.models import AccessControlAuditEvent
from studio.pagination import StandardPageNumberPagination
from studio.permissions import IsSuperUser
from studio.serializers import UserSerializer
from studio.services.access_control import (
    change_own_password,
    change_user_role,
    change_user_status,
    create_managed_user,
    reset_temporary_password,
)


User = get_user_model()


def _drf_error(exc: DjangoValidationError) -> ValidationError:
    return ValidationError(
        getattr(exc, "message_dict", None)
        or getattr(exc, "messages", None)
        or str(exc)
    )


class AccessUserViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsSuperUser,)
    pagination_class = StandardPageNumberPagination
    queryset = User.objects.select_related("security_profile").all().order_by("username")
    search_fields = ("username", "email", "first_name", "last_name")
    filterset_fields = ("is_active", "is_staff", "is_superuser")

    def get_serializer_class(self):
        return AccessUserCreateSerializer if self.action == "create" else AccessUserSerializer

    @extend_schema(request=AccessUserCreateSerializer, responses={201: TemporaryPasswordResponseSerializer})
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user, password = create_managed_user(actor=request.user, **serializer.validated_data)
        except DjangoValidationError as exc:
            raise _drf_error(exc) from exc
        response = Response(
            {"user": AccessUserSerializer(user).data, "temporary_password": password},
            status=status.HTTP_201_CREATED,
        )
        response["Cache-Control"] = "no-store"
        return response

    @extend_schema(request=AccessRoleChangeSerializer, responses=AccessUserSerializer)
    @action(detail=True, methods=("post",), url_path="role")
    def role(self, request, pk=None):
        serializer = AccessRoleChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = change_user_role(
                actor=request.user, target=self.get_object(), **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            raise _drf_error(exc) from exc
        return Response(AccessUserSerializer(user).data)

    @extend_schema(request=AccessStatusChangeSerializer, responses=AccessUserSerializer)
    @action(detail=True, methods=("post",), url_path="status")
    def status(self, request, pk=None):
        serializer = AccessStatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = change_user_status(
                actor=request.user, target=self.get_object(), **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            raise _drf_error(exc) from exc
        return Response(AccessUserSerializer(user).data)

    @extend_schema(request=TemporaryPasswordResetSerializer, responses=TemporaryPasswordResponseSerializer)
    @action(detail=True, methods=("post",), url_path="temporary-password")
    def temporary_password(self, request, pk=None):
        serializer = TemporaryPasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user, password = reset_temporary_password(
                actor=request.user, target=self.get_object(), **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            raise _drf_error(exc) from exc
        response = Response({
            "user": AccessUserSerializer(user).data,
            "temporary_password": password,
        })
        response["Cache-Control"] = "no-store"
        return response


class AccessAuditEventViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = (IsSuperUser,)
    pagination_class = StandardPageNumberPagination
    queryset = AccessControlAuditEvent.objects.select_related(
        "actor", "target_user",
    ).all()
    serializer_class = AccessAuditEventSerializer
    filterset_fields = ("action", "actor", "target_user")


class PasswordChangeView(APIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(request=PasswordChangeSerializer, responses=UserSerializer)
    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = change_own_password(
                user=request.user,
                current_password=serializer.validated_data["current_password"],
                new_password=serializer.validated_data["new_password"],
            )
        except DjangoValidationError as exc:
            raise _drf_error(exc) from exc
        response = Response(UserSerializer(user).data)
        response["Cache-Control"] = "no-store"
        return response
