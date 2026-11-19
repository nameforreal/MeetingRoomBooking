"""Explicit, non-destructive initialization from an approved five-room manifest."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from rooms.models import Room

NAMES = [f"Переговорная №{number}" for number in range(1, 6)]
FIELDS = {"name", "capacity", "is_vip", "is_active", "description", "photo"}


def validate_manifest(data):
    if not isinstance(data, list) or len(data) != 5:
        raise CommandError("Provide exactly five explicitly approved room definitions.")
    for row in data:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise CommandError(
                "Each room requires name, capacity, is_vip, is_active, description and photo."
            )
        if (
            row["name"] not in NAMES
            or type(row["capacity"]) is not int
            or not 1 <= row["capacity"] <= 200
            or type(row["is_vip"]) is not bool
            or type(row["is_active"]) is not bool
            or not isinstance(row["description"], str)
            or row["photo"] is not None
        ):
            raise CommandError(
                "Invalid room parameters; photo must explicitly be null until persistent media is integrated."
            )
    if {row["name"] for row in data} != set(NAMES):
        raise CommandError("Provide each of the five required names exactly once.")
    return sorted(data, key=lambda row: row["name"])


def create_five(data, *, apply=False):
    rows = validate_manifest(data)
    if connection.vendor != "postgresql":
        raise CommandError("This initialization requires PostgreSQL.")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("LOCK TABLE rooms_room IN SHARE ROW EXCLUSIVE MODE")
        missing = []
        for row in rows:
            matches = list(Room.objects.filter(name=row["name"]))
            if len(matches) > 1:
                raise CommandError("Duplicate room names exist; no records were changed.")
            if matches:
                room = matches[0]
                if (
                    any(getattr(room, key) != value for key, value in row.items() if key != "photo")
                    or room.photo
                ):
                    raise CommandError(
                        "Existing room differs from the approved manifest; no records were changed."
                    )
            else:
                missing.append(row)
        if apply:
            for row in missing:
                Room.objects.create(**row)
        return len(missing)


class Command(BaseCommand):
    help = "Plan creation of five approved rooms; --apply explicitly performs the inserts."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            data = json.loads(Path(options["manifest"]).read_text(encoding="utf-8"))
        except OSError, ValueError:
            raise CommandError("Cannot read the approved JSON manifest.") from None
        count = create_five(data, apply=options["apply"])
        self.stdout.write(
            f"{'Created' if options['apply'] else 'Would create'}: {count}. Existing rooms unchanged."
        )
