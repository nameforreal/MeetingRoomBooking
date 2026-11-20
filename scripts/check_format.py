"""Format gate for infrastructure introduced in 5.6.3; old code is not rewritten."""

import subprocess
import sys

FILES = [
    "config/neon_identity.py",
    "config/test_neon_guards.py",
    "rooms/bootstrap_catalog.py",
    "rooms/management/commands/seed_hosting_rooms.py",
    "scripts/check_neon_guards.py",
    "config/render_config.py",
    "config/settings_render.py",
    "config/render_media.py",
    "config/test_render.py",
    "docker/render_entrypoint.py",
    "docker/render_gunicorn.py",
    "rooms/management/commands/neon_bootstrap.py",
    "config/health.py",
    "config/test_container_settings.py",
    "config/test_infrastructure.py",
    "docker/entrypoint.py",
    "docker/healthcheck.py",
    "scripts/check_format.py",
    "scripts/infra_probe.py",
    "scripts/infra_data_probe.py",
    "scripts/verify_infrastructure.py",
    "scripts/e2e_http.py",
    "scripts/load_test.py",
    "scripts/check_coverage.py",
    "config/metrics.py",
    "rooms/test_metrics.py",
    "config/openapi.py",
    "config/test_delivery.py",
    "docker/gunicorn.conf.py",
    "scripts/verify_monitoring.py",
]

if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "ruff", "format", "--check", *FILES], check=True)
