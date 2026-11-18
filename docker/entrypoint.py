"""Never auto-migrate on web startup. exec preserves Gunicorn's signal handling."""

import os
import subprocess
import sys
import time
import tempfile


def main():
    command = sys.argv[1:]
    if not command:
        raise SystemExit("A command is required")
    if command[0] == "gunicorn":
        # New directory per master lifecycle; never reuse stale worker counters.
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix="mrb-metrics-")
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        from django.db import OperationalError, connection

        deadline = time.monotonic() + 60
        while True:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                break
            except OperationalError:
                connection.close()
                if time.monotonic() >= deadline:
                    raise SystemExit("Database readiness timed out") from None
                time.sleep(1)
        connection.close()
        subprocess.run([sys.executable, "manage.py", "migrate", "--check"], check=True)
        subprocess.run([sys.executable, "manage.py", "collectstatic", "--noinput"], check=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
