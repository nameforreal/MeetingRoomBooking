"""PostgreSQL-only, fail-closed guard for the standard auth.User.is_active."""
from django.conf import settings
from django.db import migrations
from rooms.migration_safety import protect_booking_history


def install_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    table = schema_editor.quote_name(apps.get_model(app_label, model_name)._meta.db_table)
    schema_editor.execute("""
        CREATE FUNCTION meetingroom_guard_user_activity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF current_setting('meetingroom.user_activity', true)
               IS DISTINCT FROM format('%s:%s:%s', txid_current(), NEW.id, NEW.is_active::text) THEN
                RAISE EXCEPTION 'Use rooms.account_operations to change is_active'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END;
        $$
    """, params=None)
    schema_editor.execute(f"""
        CREATE TRIGGER meetingroom_user_activity_guard
        BEFORE UPDATE OF is_active ON {table}
        FOR EACH ROW WHEN (OLD.is_active IS DISTINCT FROM NEW.is_active)
        EXECUTE FUNCTION meetingroom_guard_user_activity()
    """)


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    table = schema_editor.quote_name(apps.get_model(app_label, model_name)._meta.db_table)
    schema_editor.execute(f"DROP TRIGGER meetingroom_user_activity_guard ON {table}")
    schema_editor.execute("DROP FUNCTION meetingroom_guard_user_activity()")


class Migration(migrations.Migration):
    dependencies = [("rooms", "0008_booking_baselines")]
    operations = [migrations.RunPython(install_guard, remove_guard),
                  # Runs FIRST on reverse, before dropping the trigger/function.
                  migrations.RunPython(migrations.RunPython.noop, protect_booking_history)]
