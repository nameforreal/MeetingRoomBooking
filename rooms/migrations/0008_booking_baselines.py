"""Known state at migration time, never a reconstruction of past activity."""
from datetime import timezone as datetime_timezone
from uuid import uuid4

from django.db import migrations
from django.utils import timezone
from rooms.migration_safety import protect_booking_history


def snapshot_existing(apps, schema_editor):
    Booking = apps.get_model("rooms", "Booking")
    Event = apps.get_model("rooms", "AuditEvent")
    alias = schema_editor.connection.alias
    captured_at, correlation = timezone.now(), uuid4()
    fields = ("room_id", "user_id", "start_time", "end_time", "status", "revision")
    batch = []

    def write_batch(rows):
        existing = set(Event.objects.using(alias).filter(action="baseline", booking_id__in=[r.pk for r in rows])
                       .values_list("booking_id", flat=True))
        events = []
        for booking in rows:
            if booking.pk in existing:
                continue
            state = {field: getattr(booking, field) for field in fields}
            for field in ("start_time", "end_time"):
                state[field] = state[field].astimezone(datetime_timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            events.append(Event(action="baseline", source="system", actor_id=None,
                                object_type="booking", object_id=booking.pk, booking_id=booking.pk,
                                new_room_id=booking.room_id, new_owner_id=booking.user_id,
                                before={}, after=state, changed_fields=[], revision_before=None,
                                revision_after=booking.revision, occurred_at=captured_at, correlation_id=correlation))
        Event.objects.using(alias).bulk_create(events, batch_size=500)

    for booking in Booking.objects.using(alias).order_by("pk").iterator(chunk_size=500):
        batch.append(booking)
        if len(batch) == 500:
            write_batch(batch)
            batch = []
    if batch:
        write_batch(batch)


class Migration(migrations.Migration):
    dependencies = [("rooms", "0007_audit_history")]
    # Captured history cannot be reversed; empty installations can roll back.
    operations = [migrations.RunPython(snapshot_existing, protect_booking_history)]
