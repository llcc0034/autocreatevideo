from __future__ import annotations

from typing import Any

from studio.models import WorkflowTemplate


def can_execute_template(template: WorkflowTemplate, user) -> bool:
    """Only curated global system templates may execute for non-staff users."""

    if not getattr(user, "is_authenticated", False) or not template.is_active:
        return False
    return bool(
        getattr(user, "is_staff", False)
        or (template.owner_id is None and template.is_system)
    )


def executable_templates(queryset, user):
    queryset = queryset.filter(is_active=True)
    if getattr(user, "is_staff", False):
        return queryset
    return queryset.filter(owner__isnull=True, is_system=True)


def queue_capacity_advisories(
    *,
    active_task_count: int,
    user_active_task_count: int,
    requested_task_count: int,
    recommended_max_pending: int,
    recommended_max_active_per_user: int,
) -> list[dict[str, Any]]:
    """Describe recommendation overages without turning them into gates."""

    projected_active = active_task_count + requested_task_count
    projected_user_active = user_active_task_count + requested_task_count
    advisories: list[dict[str, Any]] = []
    if projected_active > recommended_max_pending:
        advisories.append({
            "schema_version": 1,
            "code": "recommended_max_pending_exceeded",
            "severity": "warning",
            "scope": "platform",
            "measured": projected_active,
            "recommended_maximum": recommended_max_pending,
        })
    if projected_user_active > recommended_max_active_per_user:
        advisories.append({
            "schema_version": 1,
            "code": "recommended_max_active_per_user_exceeded",
            "severity": "warning",
            "scope": "user",
            "measured": projected_user_active,
            "recommended_maximum": recommended_max_active_per_user,
        })
    return advisories
