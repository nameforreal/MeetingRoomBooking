"""Creation receipts, on the SAME connection/transaction as booking writes.

Lock order: creation key, rooms by PK, booking. Never call after room locks.
Only successful operations commit. Internal reservations roll back on any error.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timezone as datetime_timezone

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connections, models, transaction
from django.utils import timezone

from .models import Booking, BookingCreationRequest, Room


class IdempotencyError(ValidationError):
    def __init__(self, message, code, status=400):
        super().__init__(message, code=code)
        self.status = status


def validate_key(value):
    if value is None or value == "":
        raise IdempotencyError("Передайте обязательный ключ идемпотентности.", "idempotency_key_required")
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is None:
        raise IdempotencyError("Ключ должен содержать 1–128 символов A–Z, a–z, 0–9, . _ : -.", "idempotency_key_invalid")
    return value


def utc_string(value):
    if not hasattr(value, "utcoffset") or timezone.is_naive(value):
        raise ValidationError("Укажите дату и время с часовым поясом.")
    try:
        return value.astimezone(datetime_timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise ValidationError("Укажите допустимые дату и время.") from exc


def normalize(*, room_id, user_id, start_time, end_time):
    return {"room": int(room_id), "user": int(user_id),
            "start_time": utc_string(start_time), "end_time": utc_string(end_time)}


def creation_snapshot(booking):
    # Same minimal public fields as BookingSerializer, no email/name of user.
    def date(value):
        return value.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z")
    return {"id": booking.pk, "room": booking.room_id, "room_name": booking.room.name,
            "start_time": date(booking.start_time), "end_time": date(booking.end_time),
            "status": booking.status, "status_display": booking.get_status_display(),
            "created_at": date(booking.created_at), "revision": booking.revision}


@dataclass(frozen=True)
class CreationResult:
    booking: Booking
    payload: dict
    replayed: bool
    status: int = 201


def _claim(*, actor, key, payload, alias):
    """A failed INSERT uses its own savepoint, leaving the outer atomic usable."""
    connection = connections[alias]
    try:
        with transaction.atomic(using=alias):
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_setting('lock_timeout')")
                    previous_timeout = cursor.fetchone()[0]
                    cursor.execute("SELECT set_config('lock_timeout', '5000ms', true)")
            receipt = BookingCreationRequest(
                actor=actor, key=key, canonical_payload=payload,
                payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            )
            # Only this module may persist an uncommitted reservation.
            models.Model.save(receipt, using=alias, force_insert=True)
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous_timeout])
            return receipt, False
    except IntegrityError as exc:
        cause = exc.__cause__
        name = getattr(getattr(cause, "diag", None), "constraint_name", None)
        sqlite_unique = (connection.vendor == "sqlite" and
                         str(cause) == "UNIQUE constraint failed: rooms_bookingcreationrequest.actor_id, rooms_bookingcreationrequest.operation, rooms_bookingcreationrequest.key")
        if name != "booking_creation_key_unique" and not sqlite_unique:
            raise
        return BookingCreationRequest.objects.using(alias).get(
            actor=actor, operation="booking.create", key=key), True
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) == "55P03":
            raise IdempotencyError("Запрос с этим ключом ещё выполняется. Повторите его с тем же ключом.",
                                   "idempotency_in_progress", 503) from exc
        raise


def _complete(receipt, booking, payload, alias):
    receipt.booking = booking
    receipt.response_payload = payload
    receipt.completed_at = timezone.now()
    models.Model.save(receipt, using=alias, update_fields=["booking", "response_payload", "completed_at"])


def create(*, actor, key, room_id, start_time, end_time, user=None):
    from . import booking_operations as operations
    key = validate_key(key)
    alias = "default"
    if not actor or not actor.is_authenticated:
        raise PermissionDenied
    # Do not reuse cached permissions or is_active from an earlier request.
    actor = get_user_model().objects.using(alias).get(pk=actor.pk)
    owner = user or actor
    payload = normalize(room_id=room_id, user_id=owner.pk, start_time=start_time, end_time=end_time)
    with transaction.atomic(using=alias):
        operations.authorize(actor, Booking(user=owner), "create")
        receipt, replayed = _claim(actor=actor, key=key, payload=payload, alias=alias)
        if replayed:
            if receipt.normalization_version != 1:
                raise RuntimeError("Unsupported creation normalization version")
            if receipt.canonical_payload != payload:
                raise IdempotencyError("Этот ключ уже использован с другими параметрами бронирования.",
                                       "idempotency_key_reused", 409)
            if receipt.booking_id is None or receipt.completed_at is None or receipt.response_payload is None:
                raise RuntimeError("Incomplete committed booking creation receipt")
            booking = Booking.objects.using(alias).get(pk=receipt.booking_id)
            operations.authorize(actor, booking, "create")
            return CreationResult(booking, receipt.response_payload, True, receipt.response_status)
        try:
            room = Room.objects.using(alias).get(pk=room_id)
        except Room.DoesNotExist as exc:
            raise ValidationError("Комната недоступна для бронирования.") from exc
        booking = operations.create(actor=actor, room=room, user=owner, start_time=start_time, end_time=end_time)
        result = creation_snapshot(booking)
        _complete(receipt, booking, result, alias)
        return CreationResult(booking, result, False)
