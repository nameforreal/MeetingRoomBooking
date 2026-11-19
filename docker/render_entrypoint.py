"""Cloud startup checks schema but NEVER applies migrations or seeds data."""

import os
import sys
import tempfile


def main():
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings_render"
    os.environ["DJANGO_READ_DOT_ENV"] = "false"
    if len(sys.argv) > 1:
        os.execvp(sys.argv[1], sys.argv[1:])
        return
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix="mrb-render-metrics-")
    try:
        import django
        from django.core.management import call_command
        from django.db import connection

        django.setup()
        call_command("check", verbosity=0)
        call_command("migrate", check_unapplied=True, verbosity=0)
        call_command("collectstatic", interactive=False, verbosity=0)
        connection.close()
    except Exception, SystemExit:
        # Database/driver exceptions can contain endpoint/user data. Never echo them.
        raise SystemExit(
            "Render startup refused: verify configuration, database availability, migrations and static files."
        ) from None
    os.execvp(
        "gunicorn", ["gunicorn", "config.wsgi:application", "--config=docker/render_gunicorn.py"]
    )


if __name__ == "__main__":
    main()
