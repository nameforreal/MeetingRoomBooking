# Generated for MeetingRoomBooking role metadata.

from django.db import migrations, models
import django.db.models.deletion
from django.db.migrations.exceptions import IrreversibleError
from django.utils.text import slugify
from rooms.migration_safety import protect_booking_history


def populate_role_metadata(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Role = apps.get_model("rooms", "Role")
    using = schema_editor.connection.alias

    for group in Group.objects.using(using).order_by("pk").iterator():
        base = (slugify(group.name) or f"group-{group.pk}")[:90]
        code = base
        suffix = 2
        while Role.objects.using(using).filter(code=code).exists():
            tail = f"-{suffix}"
            code = f"{base[:100 - len(tail)]}{tail}"
            suffix += 1
        Role.objects.using(using).create(
            group_id=group.pk,
            code=code,
            description="",
        )


def protect_role_metadata(apps, schema_editor):
    Role = apps.get_model("rooms", "Role")
    # Preflight lower migration guards before 0010 can be reversed.
    protect_booking_history(apps, schema_editor)
    using = schema_editor.connection.alias
    if schema_editor.connection.vendor == "postgresql":
        table = schema_editor.quote_name(Role._meta.db_table)
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    if Role.objects.using(using).exists():
        raise IrreversibleError(
            "Role metadata is populated; preserve role records instead of reversing 0010_role_model."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("rooms", "0009_user_activity_guard"),
    ]

    operations = [
        migrations.CreateModel(
            name="Role",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("group", models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="meetingroom_role",
                    to="auth.group",
                    verbose_name="Группа разрешений",
                )),
                ("code", models.SlugField(max_length=100, unique=True, verbose_name="Код роли")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Дата изменения")),
            ],
            options={
                "verbose_name": "Роль",
                "verbose_name_plural": "Роли",
                "ordering": ("group__name",),
            },
        ),
        migrations.RunPython(populate_role_metadata, protect_role_metadata),
    ]
