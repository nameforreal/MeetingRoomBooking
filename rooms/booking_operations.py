"""Transactional booking writes. Lock order: rooms by PK, then booking.

No caller may acquire booking locks before entering this module.
Each operation changes one booking; batch callers invoke separate operations.
"""
import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .permissions import staff_has_perm

FIELDS = ("room_id", "user_id", "start_time", "end_time", "status", "revision")
MAX_REVISION = 2147483647
STALE = "Бронирование изменено другим запросом. Обновите страницу и повторите действие."


def parse_revision(value):
    """Accept an explicit integer or a decimal form value, never bool/float."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise ValidationError({"revision": "Передайте корректную версию бронирования (revision)."})
    return value


def next_revision(current):
    if current >= MAX_REVISION:
        raise ValidationError("Достигнут предел версии бронирования.")
    return current + 1


def _lock_rooms(alias, room_ids):
    from .models import Room
    return {room.pk: room for room in Room.objects.using(alias).filter(
        pk__in=room_ids,
    ).order_by("pk").select_for_update()}


def snapshot(booking):
    return tuple(getattr(booking, field) for field in FIELDS)


def authorize(actor, booking, operation):
    permission = "rooms.add_booking" if operation == "create" else "rooms.change_booking"
    if operation in {"confirm", "reject"}:
        allowed = staff_has_perm(actor, permission)
    else:
        allowed = actor.is_active and (
            actor.pk == booking.user_id or staff_has_perm(actor, permission)
        )
    if not allowed:
        raise PermissionDenied


def create(*, actor, room, start_time, end_time, user=None):
    from .models import Booking
    booking = Booking(room=room, user=user or actor, start_time=start_time, end_time=end_time)
    authorize(actor, booking, "create")
    return persist(booking)


def edit(booking, *, actor, revision=None, **changes):
    authorize(actor, booking, "edit")
    for field, value in changes.items():
        if field not in {"room", "start_time", "end_time"}:
            raise ValidationError("Недопустимое поле бронирования.")
        setattr(booking, field, value)
    return persist(booking, actor=actor, operation="edit", revision=revision)


def cancel(booking, *, actor, revision=None):
    authorize(actor, booking, "cancel")
    return persist(booking, actor=actor, operation="cancel", revision=revision)


def confirm(booking, *, actor, revision=None):
    authorize(actor, booking, "confirm")
    return persist(booking, actor=actor, operation="confirm", revision=revision)


def reject(booking, *, actor, revision=None):
    authorize(actor, booking, "reject")
    return persist(booking, actor=actor, operation="reject", revision=revision)


def save_room(room, *, force_insert=False, force_update=False, using=None, update_fields=None):
    """Room first, then all affected bookings by PK; no cross-room locks here."""
    from .models import Room, Booking
    alias = using or room._state.db or "default"
    if update_fields is not None and not update_fields:
        return
    with transaction.atomic(using=alias):
        current = None
        if not room._state.adding:
            current = _lock_rooms(alias, {room.pk}).get(room.pk)
            if current is None:
                raise ValidationError(STALE)
            baseline = getattr(room, "_room_flags", {})
            for field in ("is_vip", "is_active"):
                if ((update_fields is not None and field not in update_fields)
                        or getattr(room, field) == baseline.get(field, getattr(current, field))):
                    setattr(room, field, getattr(current, field))
                elif field in baseline and getattr(current, field) != baseline[field]:
                    raise ValidationError(STALE)
        models.Model.save(room, using=alias, force_insert=force_insert,
                          force_update=force_update, update_fields=update_fields)
        if current is not None and not current.is_vip and room.is_vip:
            # This changes only approval, not scheduling. Legacy time ranges may
            # be outside today's booking horizon; do not edit or discard them.
            affected = Booking.objects.using(alias).filter(
                room_id=room.pk, status="confirmed", end_time__gt=timezone.now(),
            ).order_by("pk").select_for_update()
            for booking in affected:
                booking.status = "pending"
                booking.revision = next_revision(booking.revision)
                models.Model.save(booking, using=alias, update_fields=["status", "revision"])
        room._room_flags = {field: getattr(room, field) for field in ("is_vip", "is_active")}


def persist(booking, *, actor=None, operation="save", revision=None, update_fields=None, using=None,
            force_insert=False, force_update=False):
    """Also used by Model.save(), so ordinary ORM writes obey the same locks.

    Actor-less saves are trusted Python calls, not exposed HTTP operations.
    Stale objects are rejected, including partial saves. Cancellation uses the
    latest row intentionally, and never copies stale scheduling fields.
    """
    from .models import Booking
    alias = using or booking._state.db or "default"
    if update_fields is not None and not update_fields:
        return booking
    baseline = getattr(booking, "_booking_snapshot", None)
    room_ids = {booking.room_id}
    if baseline:
        room_ids.add(baseline[0])
    try:
        with transaction.atomic(using=alias):
            rooms = _lock_rooms(alias, room_ids)
            current = None
            if not booking._state.adding:
                try:
                    current = Booking.objects.using(alias).select_for_update().get(pk=booking.pk)
                except Booking.DoesNotExist as exc:
                    raise ValidationError(STALE) from exc
                if current.room_id not in rooms:
                    raise ValidationError(STALE)
                if actor is not None:
                    authorize(actor, current, operation)
                expected_revision = parse_revision(revision)
                if operation == "cancel" and current.status == "cancelled":
                    if expected_revision > current.revision:
                        raise ValidationError(STALE)
                    current._booking_noop = True
                    return current
                if expected_revision != current.revision:
                    raise ValidationError(STALE)
                if operation in {"cancel", "reject"}:
                    if operation == "reject" and current.status != "pending":
                        raise ValidationError("Отклонить можно только ожидающую заявку.")
                    booking = current
                    booking.status = "cancelled"
                else:
                    if baseline is None or baseline != snapshot(current):
                        raise ValidationError(STALE)
                    if current.status == "cancelled":
                        raise ValidationError("Отменённое бронирование нельзя изменять.")
                    if current.end_time <= timezone.now() and booking.status != "cancelled":
                        raise ValidationError("Завершённое бронирование нельзя изменять.")
                    if update_fields is not None:
                        selected = set(update_fields)
                        for field in FIELDS:
                            if field not in selected and field.removesuffix("_id") not in selected:
                                setattr(booking, field, getattr(current, field))
                    if operation == "confirm":
                        if current.status != "pending":
                            raise ValidationError("Подтвердить можно только ожидающую заявку.")
                        booking.status = "confirmed"
            elif booking.pk is not None:
                # A reconstructed instance must not overwrite an existing PK.
                raise ValidationError("Создание бронирования с заданным ID запрещено.")
            if booking.room_id not in rooms:
                raise ValidationError("Комната недоступна.")
            booking.room = rooms[booking.room_id]
            if booking.status != "cancelled" and not booking.room.is_active:
                raise ValidationError("Комната недоступна для бронирования.")
            booking.full_clean()
            booking.revision = next_revision(current.revision) if current else 1
            write_fields = None if update_fields is None else set(update_fields) | {"status", "revision"}
            models.Model.save(booking, using=alias, force_insert=force_insert,
                              force_update=force_update, update_fields=write_fields)
            booking._booking_snapshot = snapshot(booking)
            return booking
    except IntegrityError as exc:
        if getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None) == "booking_no_active_overlap":
            raise ValidationError("Комната уже забронирована на это время.") from exc
        raise
