"""Version-one reverse guards. Keep this historical-migration contract stable.

Use historical models only. PostgreSQL table locks close the gap between the
empty-table check and reverse DDL; stop application writers before migrations.
"""
from django.db.migrations.exceptions import IrreversibleError


def _models_and_locks(apps, schema_editor):
    found = {}
    # Same dependency direction as creation: receipt, room, booking, event.
    for name in ("BookingCreationRequest", "Room", "Booking", "AuditEvent"):
        try:
            found[name] = apps.get_model("rooms", name)
        except LookupError:
            continue  # This model does not exist at this historical stage.
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            for model in found.values():
                table = schema_editor.quote_name(model._meta.db_table)
                cursor.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    return found


def _refuse(model_name):
    raise IrreversibleError(
        f"Unsafe rollback: rooms.{model_name} contains data. "
        "Keep the current schema and protections. Back up and verify restoration; "
        "see MIGRATION_SAFETY.md. No automatic history deletion is permitted."
    )


def protect_booking_history(apps, schema_editor):
    models = _models_and_locks(apps, schema_editor)
    alias = schema_editor.connection.alias
    for name in ("BookingCreationRequest", "AuditEvent", "Booking"):
        if name in models and models[name]._base_manager.using(alias).exists():
            _refuse(name)


def protect_room_catalog(apps, schema_editor):
    models = _models_and_locks(apps, schema_editor)
    if models["Room"]._base_manager.using(schema_editor.connection.alias).exists():
        _refuse("Room")


def protect_photo_links(apps, schema_editor):
    models = _models_and_locks(apps, schema_editor)
    if models["Room"]._base_manager.using(schema_editor.connection.alias).exclude(
        photo__isnull=True,
    ).exclude(photo="").exists():
        _refuse("Room.photo")
