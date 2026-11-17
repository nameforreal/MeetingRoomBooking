"""Server-owned audit context; never serialize a request or user-supplied metadata."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4
import logging

from django.core.exceptions import PermissionDenied
from django.db import connections, models

_context = ContextVar("booking_audit_context", default=None)
logger = logging.getLogger("rooms.operations")


@contextmanager
def context(*, source="system", actor=None, correlation_id=None):
    token = _context.set((source, actor, correlation_id or uuid4()))
    try:
        yield
    finally:
        _context.reset(token)


def identity(actor=None):
    source, contextual_actor, correlation = _context.get() or ("system", None, uuid4())
    if callable(contextual_actor):
        contextual_actor = contextual_actor()
    actor = actor if actor is not None else contextual_actor
    if actor is not None and not actor.is_authenticated:
        actor = None
    if source != "system" and actor is None:
        raise PermissionDenied("Audit actor required")
    return source, actor, correlation


def request_context(source):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            with context(source=source, actor=lambda: request.user):
                return view(request, *args, **kwargs)
        return wrapped
    return decorate


def operation_context(operation):
    @wraps(operation)
    def wrapped(*args, **kwargs):
        try:
            if _context.get() is not None:
                return operation(*args, **kwargs)
            with context(source="system"):
                return operation(*args, **kwargs)
        except Exception as error:
            # Do not log payloads, exception messages, secrets or idempotency keys.
            logger.warning("Operation rejected: operation=%s error=%s", operation.__name__, type(error).__name__)
            raise
    return wrapped


class APIContextMixin:
    def dispatch(self, request, *args, **kwargs):
        # DRF authenticates its Request inside dispatch, including test clients.
        with context(source="api", actor=lambda: self.request.user):
            return super().dispatch(request, *args, **kwargs)


def booking_state(booking):
    if booking is None:
        return {}
    from .booking_idempotency import utc_string
    return {"room_id": booking.room_id, "user_id": booking.user_id,
            "start_time": utc_string(booking.start_time), "end_time": utc_string(booking.end_time),
            "status": booking.status, "revision": booking.revision}


def room_state(room):
    return {} if room is None else {"is_active": room.is_active, "is_vip": room.is_vip}


def record(*, action, obj, before, after, actor=None, deleted=False):
    from .models import AuditEvent
    alias = obj._state.db or "default"
    if not connections[alias].in_atomic_block:
        raise RuntimeError("Audit event requires the business operation's transaction")
    source, actor, correlation = identity(actor)
    kind = "user" if obj._meta.label_lower == "auth.user" else obj._meta.model_name
    allowed = {"booking": {"room_id", "user_id", "start_time", "end_time", "status", "revision"},
               "room": {"is_active", "is_vip"}, "user": {"is_active"}}
    if kind not in allowed or (set(before) | set(after)) - allowed[kind]:
        raise ValueError("Unexpected audit fields")
    refs = {} if deleted else {"subject_user" if kind == "user" else kind: obj}
    if kind == "booking":
        refs.update(previous_room_id=before.get("room_id"), new_room_id=after.get("room_id"),
                    previous_owner_id=before.get("user_id"), new_owner_id=after.get("user_id"))
    event = AuditEvent(action=action, source=source, actor=actor, object_type=kind, object_id=obj.pk,
                       before=before, after=after, changed_fields=sorted(k for k in set(before) | set(after)
                                                                        if before.get(k) != after.get(k)),
                       revision_before=before.get("revision"), revision_after=after.get("revision"),
                       correlation_id=correlation, **refs)
    models.Model.save(event, using=alias, force_insert=True)
    return event


def booking_action(before, after, operation):
    if not before:
        return "booking.created"
    if operation == "reject":
        return "booking.vip_rejected"
    if after["status"] == "cancelled" and before["status"] != "cancelled":
        return "booking.cancelled"
    if before["status"] == "pending" and after["status"] == "confirmed":
        return "booking.confirmed"
    changes = {k for k in before if before[k] != after[k]} - {"revision"}
    if changes == {"user_id"}:
        return "booking.owner_changed"
    if changes and changes <= {"start_time", "end_time"}:
        return "booking.time_changed"
    if changes == {"room_id"}:
        return "booking.room_changed"
    return "booking.updated" if changes else "booking.saved"
