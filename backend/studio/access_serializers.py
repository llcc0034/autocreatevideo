from __future__ import annotations

from datetime import datetime

from django.contrib.auth import get_user_model
from rest_framework import serializers

from studio.models import AccessControlAuditEvent
from studio.services.access_control import ROLES, role_for_user, security_profile_for


User = get_user_model()


class AccessUserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    must_change_password = serializers.SerializerMethodField()
    password_changed_at = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "username", "email", "first_name", "last_name", "role",
            "is_active", "must_change_password", "password_changed_at",
            "last_login", "date_joined",
        )
        read_only_fields = fields

    def get_role(self, obj) -> str:
        return role_for_user(obj)

    def get_must_change_password(self, obj) -> bool:
        return security_profile_for(obj).must_change_password

    def get_password_changed_at(self, obj) -> datetime | None:
        return security_profile_for(obj).password_changed_at


class AccessUserCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    role = serializers.ChoiceField(choices=ROLES, default="member")
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("账号已存在")
        return value


class AccessRoleChangeSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=ROLES)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class AccessStatusChangeSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class TemporaryPasswordResetSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class TemporaryPasswordResponseSerializer(serializers.Serializer):
    user = AccessUserSerializer()
    temporary_password = serializers.CharField(help_text="只在本次响应显示一次")


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "两次输入的新密码不一致"})
        return attrs


class AccessAuditEventSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True)
    target_username = serializers.CharField(source="target_user.username", read_only=True)

    class Meta:
        model = AccessControlAuditEvent
        fields = (
            "id", "actor", "actor_username", "target_user", "target_username",
            "action", "before_role", "after_role", "before_active", "after_active",
            "reason", "created_at",
        )
        read_only_fields = fields
