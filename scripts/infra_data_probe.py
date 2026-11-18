"""Executed on stdin inside the isolated app, never against an arbitrary DB."""

import json
import os
from datetime import timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from rooms.booking_operations import create_once
from rooms.models import AuditEvent, Booking, BookingCreationRequest, Room

db = settings.DATABASES["default"]
assert (db["HOST"], db["NAME"], db["USER"]) == ("db", "booking_tests", "booking_tests")
assert settings.PRODUCTION is False
run = os.environ["INFRA_TEST_RUN"]
assert len(run) == 32 and all(char in "0123456789abcdef" for char in run)
user, _ = get_user_model().objects.get_or_create(username="infra-" + run)
room, _ = Room.objects.get_or_create(name="infra-" + run)
mode = os.environ["INFRA_TEST_MODE"]
receipts = BookingCreationRequest.objects.filter(actor=user)
if mode in {"seed", "outage"}:
    start = (timezone.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    if mode == "outage":
        start += timedelta(hours=1)
    create_once(
        actor=user,
        key=run + mode,
        room_id=room.pk,
        start_time=start,
        end_time=start + timedelta(hours=1),
    )
elif mode == "replay":
    for receipt in receipts:
        data = receipt.canonical_payload
        result = create_once(
            actor=user,
            key=receipt.key,
            room_id=data["room"],
            start_time=parse_datetime(data["start_time"]),
            end_time=parse_datetime(data["end_time"]),
        )
        assert result.replayed
else:
    raise ValueError("Unknown probe mode")

bookings = Booking.objects.filter(user=user)
snapshot = {
    "bookings": list(
        bookings.order_by("pk").values("id", "revision", "status", "start_time", "end_time")
    ),
    "receipts": list(receipts.order_by("pk").values("id", "booking_id", "response_payload")),
    "events": list(
        AuditEvent.objects.filter(booking__in=bookings)
        .order_by("pk")
        .values("id", "action", "revision_after")
    ),
}
print(json.dumps(snapshot, default=str, sort_keys=True))
