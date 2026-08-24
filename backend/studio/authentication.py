from __future__ import annotations

from django.contrib.auth import get_user_model
from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme
from rest_framework.exceptions import APIException, AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer

from studio.services.access_control import security_profile_for


User = get_user_model()
PASSWORD_CHANGE_ALLOWED_URL_NAMES = {"me", "password-change"}


class PasswordChangeRequired(APIException):
    status_code = 403
    default_code = "password_change_required"
    default_detail = {
        "code": "password_change_required",
        "detail": "首次登录必须先修改临时密码",
    }


class StudioTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["auth_version"] = security_profile_for(user).auth_version
        return token


class StudioTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])
        user_id = refresh.get("user_id")
        try:
            user = User.objects.get(pk=user_id, is_active=True)
        except User.DoesNotExist as exc:
            raise AuthenticationFailed("账号已停用或不存在", code="user_inactive") from exc
        expected = security_profile_for(user).auth_version
        if refresh.get("auth_version") != expected:
            raise AuthenticationFailed("登录状态已失效，请重新登录", code="token_auth_version_mismatch")
        return super().validate(attrs)


class StudioJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        expected = security_profile_for(user).auth_version
        if validated_token.get("auth_version") != expected:
            raise AuthenticationFailed("登录状态已失效，请重新登录", code="token_auth_version_mismatch")
        return user

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        profile = security_profile_for(user)
        resolver_name = getattr(getattr(request, "resolver_match", None), "url_name", "")
        if profile.must_change_password and resolver_name not in PASSWORD_CHANGE_ALLOWED_URL_NAMES:
            raise PasswordChangeRequired()
        return user, token


class StudioJWTScheme(SimpleJWTScheme):
    """Document the hardened JWT subclass with the canonical bearer scheme."""

    target_class = "studio.authentication.StudioJWTAuthentication"
