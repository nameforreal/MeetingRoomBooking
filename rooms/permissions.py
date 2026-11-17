"""Shared authorization for administrative operations."""
from functools import wraps
from django.core.exceptions import PermissionDenied
from django.contrib.admin.views.decorators import staff_member_required
from rest_framework.permissions import BasePermission, SAFE_METHODS


def staff_has_perm(user, permission):
    return user.is_active and user.is_staff and user.has_perm(permission)


def staff_permission_required(permission):
    def decorate(view):
        @staff_member_required
        @wraps(view)
        def checked(request, *args, **kwargs):
            if not staff_has_perm(request.user, permission):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return checked
    return decorate


def can_edit_account(actor, target):
    return staff_has_perm(actor, "auth.change_user") and (
        actor.is_superuser or (
            target.pk != actor.pk and not target.is_staff and not target.is_superuser
        )
    )


class RoomPermissions(BasePermission):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True  # Active catalog remains public.
        codename = {"POST": "add_room", "PUT": "change_room",
                    "PATCH": "change_room", "DELETE": "change_room"}.get(request.method)
        return bool(codename and staff_has_perm(request.user, "rooms." + codename))
