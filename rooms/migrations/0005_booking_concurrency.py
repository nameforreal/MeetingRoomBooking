from django.db import migrations, models


def add_overlap_constraint(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    # Validate existing data before installing an extension or locking for DDL.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM rooms_booking WHERE start_time >= end_time LIMIT 1")
        if cursor.fetchone():
            raise RuntimeError("Invalid booking intervals; repair data before retrying migration.")
        cursor.execute("""
            SELECT 1 FROM rooms_booking a JOIN rooms_booking b
              ON a.id < b.id AND a.room_id = b.room_id
             AND a.start_time < b.end_time AND a.end_time > b.start_time
            WHERE a.status IN ('pending', 'confirmed')
              AND b.status IN ('pending', 'confirmed') LIMIT 1
        """)
        if cursor.fetchone():
            raise RuntimeError("Overlapping active bookings; resolve conflicts before retrying migration.")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        cursor.execute("""
            ALTER TABLE rooms_booking ADD CONSTRAINT booking_no_active_overlap
            EXCLUDE USING gist (
                room_id WITH =,
                tstzrange(start_time, end_time, '[)') WITH &&
            ) WHERE (status IN ('pending', 'confirmed'))
        """)


def remove_overlap_constraint(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("ALTER TABLE rooms_booking DROP CONSTRAINT booking_no_active_overlap")
    # btree_gist may be used by other tables; never drop it on rollback.


class Migration(migrations.Migration):
    dependencies = [("rooms", "0004_booking_export_permission")]
    operations = [
        migrations.AlterModelOptions(
            name="booking",
            options={
                "base_manager_name": "objects", "default_manager_name": "objects",
                "ordering": ["-start_time"], "verbose_name": "Бронирование",
                "verbose_name_plural": "Бронирования",
                "permissions": [("export_booking", "Can export bookings")],
            },
        ),
        migrations.AddField(
            model_name="booking", name="revision",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        # PostgreSQL-only DB constraint; SQLite supports the application checks.
        migrations.RunPython(add_overlap_constraint, remove_overlap_constraint),
    ]
