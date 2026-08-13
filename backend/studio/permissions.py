from rest_framework.permissions import SAFE_METHODS, BasePermission


def resolve_owner(obj):
    if hasattr(obj, "owner"):
        return obj.owner
    if hasattr(obj, "project"):
        return obj.project.owner
    if hasattr(obj, "episode"):
        return obj.episode.project.owner
    return None


class IsOwnerOrStaff(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        return request.user.is_staff or resolve_owner(obj) == request.user


class IsOwnerOrStaffOrGlobalReadOnly(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        owner = resolve_owner(obj)
        if owner is None:
            return request.method in SAFE_METHODS or request.user.is_staff
        return request.user.is_staff or owner == request.user


class IsStaffOrReadOnly(BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.method in SAFE_METHODS or request.user.is_staff
