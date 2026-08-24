from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import AccessControlAuditEvent, UserSecurityProfile


User = get_user_model()

ROLE_MEMBER = "member"
ROLE_PLATFORM_ADMIN = "platform_admin"
ROLE_SUPER_ADMIN = "super_admin"
ROLES = (ROLE_MEMBER, ROLE_PLATFORM_ADMIN, ROLE_SUPER_ADMIN)


def role_for_user(user) -> str:
    if user.is_superuser:
        return ROLE_SUPER_ADMIN
    if user.is_staff:
        return ROLE_PLATFORM_ADMIN
    return ROLE_MEMBER


def security_profile_for(user, *, lock: bool = False) -> UserSecurityProfile:
    if not lock:
        try:
            return user.security_profile
        except ObjectDoesNotExist:
            pass
    queryset = UserSecurityProfile.objects
    if lock:
        queryset = queryset.select_for_update()
    profile, _ = queryset.get_or_create(user=user)
    return profile


def _temporary_password() -> str:
    # 32 URL-safe random bytes; never persisted outside Django's password hash.
    return secrets.token_urlsafe(32)


def _audit(*, actor, target, action, before_role="", after_role="", before_active=None,
           after_active=None, reason="") -> None:
    AccessControlAuditEvent.objects.create(
        actor=actor,
        target_user=target,
        action=action,
        before_role=before_role,
        after_role=after_role,
        before_active=before_active,
        after_active=after_active,
        reason=str(reason or "")[:500],
    )


@transaction.atomic
def create_managed_user(*, actor, username: str, email: str, first_name: str = "",
                        last_name: str = "", role: str = ROLE_MEMBER, reason: str = "",
                        audit_action: str = AccessControlAuditEvent.Action.CREATE_USER):
    if role not in ROLES:
        raise ValidationError({"role": "未知角色"})
    temporary_password = _temporary_password()
    candidate = User(username=username, email=email, first_name=first_name, last_name=last_name)
    validate_password(temporary_password, user=candidate)
    candidate.set_password(temporary_password)
    candidate.is_staff = role in {ROLE_PLATFORM_ADMIN, ROLE_SUPER_ADMIN}
    candidate.is_superuser = role == ROLE_SUPER_ADMIN
    candidate.is_active = True
    candidate.save()
    UserSecurityProfile.objects.create(
        user=candidate,
        must_change_password=True,
        temporary_password_issued_at=timezone.now(),
    )
    _audit(
        actor=actor, target=candidate,
        action=audit_action,
        after_role=role, after_active=True, reason=reason,
    )
    return candidate, temporary_password


@transaction.atomic
def change_user_role(*, actor, target, role: str, reason: str = ""):
    if role not in ROLES:
        raise ValidationError({"role": "未知角色"})
    target = User.objects.select_for_update().get(pk=target.pk)
    before = role_for_user(target)
    if target.pk == actor.pk and role != ROLE_SUPER_ADMIN:
        raise ValidationError("不能通过权限管理降低自己的超级管理员角色")
    if before == ROLE_SUPER_ADMIN and target.is_active and role != ROLE_SUPER_ADMIN:
        active_superusers = len(list(
            User.objects.select_for_update().filter(
                is_active=True, is_superuser=True,
            ).values_list("pk", flat=True)
        ))
        if active_superusers <= 1:
            raise ValidationError("不能降级最后一个活跃超级管理员")
    target.is_staff = role in {ROLE_PLATFORM_ADMIN, ROLE_SUPER_ADMIN}
    target.is_superuser = role == ROLE_SUPER_ADMIN
    target.save(update_fields=("is_staff", "is_superuser"))
    profile = security_profile_for(target, lock=True)
    profile.auth_version += 1
    profile.save(update_fields=("auth_version", "updated_at"))
    _audit(
        actor=actor, target=target,
        action=AccessControlAuditEvent.Action.CHANGE_ROLE,
        before_role=before, after_role=role,
        before_active=target.is_active, after_active=target.is_active, reason=reason,
    )
    return target


@transaction.atomic
def change_user_status(*, actor, target, is_active: bool, reason: str = ""):
    target = User.objects.select_for_update().get(pk=target.pk)
    before = target.is_active
    role = role_for_user(target)
    if target.pk == actor.pk and not is_active:
        raise ValidationError("不能通过权限管理停用自己的账号")
    if target.is_superuser and target.is_active and not is_active:
        active_superusers = len(list(
            User.objects.select_for_update().filter(
                is_active=True, is_superuser=True,
            ).values_list("pk", flat=True)
        ))
        if active_superusers <= 1:
            raise ValidationError("不能停用最后一个活跃超级管理员")
    target.is_active = is_active
    target.save(update_fields=("is_active",))
    profile = security_profile_for(target, lock=True)
    profile.auth_version += 1
    profile.save(update_fields=("auth_version", "updated_at"))
    _audit(
        actor=actor, target=target,
        action=AccessControlAuditEvent.Action.CHANGE_STATUS,
        before_role=role, after_role=role,
        before_active=before, after_active=is_active, reason=reason,
    )
    return target


@transaction.atomic
def reset_temporary_password(*, actor, target, reason: str = ""):
    target = User.objects.select_for_update().get(pk=target.pk)
    temporary_password = _temporary_password()
    validate_password(temporary_password, user=target)
    target.set_password(temporary_password)
    target.save(update_fields=("password",))
    profile = security_profile_for(target, lock=True)
    profile.must_change_password = True
    profile.temporary_password_issued_at = timezone.now()
    profile.auth_version += 1
    profile.save(update_fields=(
        "must_change_password", "temporary_password_issued_at",
        "auth_version", "updated_at",
    ))
    _audit(
        actor=actor, target=target,
        action=AccessControlAuditEvent.Action.RESET_PASSWORD,
        before_role=role_for_user(target), after_role=role_for_user(target),
        before_active=target.is_active, after_active=target.is_active, reason=reason,
    )
    return target, temporary_password


@transaction.atomic
def change_own_password(*, user, current_password: str, new_password: str):
    user = User.objects.select_for_update().get(pk=user.pk)
    if not user.check_password(current_password):
        raise ValidationError({"current_password": "当前密码不正确"})
    if current_password == new_password:
        raise ValidationError({"new_password": "新密码不能与当前密码相同"})
    validate_password(new_password, user=user)
    user.set_password(new_password)
    user.save(update_fields=("password",))
    profile = security_profile_for(user, lock=True)
    was_temporary = profile.must_change_password
    profile.must_change_password = False
    profile.password_changed_at = timezone.now()
    profile.temporary_password_issued_at = None
    profile.auth_version += 1
    profile.save(update_fields=(
        "must_change_password", "password_changed_at",
        "temporary_password_issued_at", "auth_version", "updated_at",
    ))
    _audit(
        actor=user, target=user,
        action=AccessControlAuditEvent.Action.CHANGE_PASSWORD,
        before_role=role_for_user(user), after_role=role_for_user(user),
        before_active=user.is_active, after_active=user.is_active,
        reason="首次登录改密" if was_temporary else "用户主动改密",
    )
    return user
