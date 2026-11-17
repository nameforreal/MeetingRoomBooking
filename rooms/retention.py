"""Ordinary deletion is forbidden; empty objects have an explicit maintenance path."""
from contextvars import ContextVar

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import connections, models, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError

from . import audit

_deletion = ContextVar("controlled_empty_deletion", default=None)


def protect_user_delete(sender, instance, **kwargs):
    if _deletion.get() != (sender._meta.label_lower, instance.pk):
        raise ProtectedError("Deactivate users; empty deletion requires the controlled procedure.", [instance])


@audit.operation_context
def delete_empty(obj, *, actor):
    """Maintenance only, never an Admin bulk action. No history may be removed."""
    from .models import Room, Booking, AuditEvent, BookingCreationRequest
    if not actor.is_active or not actor.is_superuser or type(obj) not in (Room, get_user_model()):
        raise PermissionDenied
    if isinstance(obj, get_user_model()) and obj.pk == actor.pk:
        raise PermissionDenied
    alias = obj._state.db or "default"
    with transaction.atomic(using=alias):
        if isinstance(obj, Room):
            from .booking_operations import _lock_rooms
            current = _lock_rooms(alias, {obj.pk}).get(obj.pk)
            if current is None:
                raise Room.DoesNotExist
            history = (Booking.objects.using(alias).filter(room=current).exists()
                       or AuditEvent.objects.using(alias).filter(Q(room=current) | Q(previous_room=current) | Q(new_room=current)).exists()
                       or BookingCreationRequest.objects.using(alias).filter(canonical_payload__room=current.pk).exists())
            before = audit.room_state(current)
        else:
            lock_options = {"no_key": True} if connections[alias].vendor == "postgresql" else {}
            locked = {u.pk: u for u in get_user_model().objects.using(alias).filter(
                pk__in=[actor.pk, obj.pk]).order_by("pk").select_for_update(**lock_options)}
            actor, current = locked[actor.pk], locked[obj.pk]
            if not actor.is_active or not actor.is_superuser:
                raise PermissionDenied
            history = (Booking.objects.using(alias).filter(user=current).exists()
                       or AuditEvent.objects.using(alias).filter(Q(actor=current) | Q(subject_user=current) | Q(previous_owner=current) | Q(new_owner=current)).exists()
                       or BookingCreationRequest.objects.using(alias).filter(Q(actor=current) | Q(canonical_payload__user=current.pk)).exists()
                       or LogEntry.objects.using(alias).filter(user=current).exists())
            before = {"is_active": current.is_active}
        content_type = ContentType.objects.db_manager(alias).get_for_model(current)
        history = history or LogEntry.objects.using(alias).filter(content_type=content_type, object_id=str(current.pk)).exists()
        if history:
            raise ProtectedError("Object has history; archive or deactivate it.", [current])
        # A deletion event records an ID, not a new FK to the object being removed.
        audit.record(action=f"{current._meta.model_name}.deleted", obj=current,
                     before=before, after={}, actor=actor, deleted=True)
        token = _deletion.set((current._meta.label_lower, current.pk))
        try:
            models.Model.delete(current, using=alias)
        finally:
            _deletion.reset(token)
