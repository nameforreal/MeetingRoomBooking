"""Verify local Prometheus, Grafana provisioning, PostgreSQL metrics and alerts."""

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
from time import monotonic, sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def compose_command(project):
    return [
        "docker",
        "compose",
        "--env-file",
        "docker/test.env",
        "-p",
        project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.test.yaml",
        "-f",
        "compose.monitoring.yaml",
    ]


def get(url, headers=None):
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def wait_for(check, timeout=60, interval=2):
    deadline = monotonic() + timeout
    last_error = None
    while monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except Exception as exc:
            last_error = exc
        sleep(interval)
    raise AssertionError(f"Monitoring verification timed out: {last_error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="mrb-infra-test")
    args = parser.parse_args()
    compose = compose_command(args.project)

    def healthy_targets():
        data = get("http://127.0.0.1:18090/api/v1/targets")
        targets = data["data"]["activeTargets"]
        return (
            targets
            if targets and all(target["health"] == "up" for target in targets)
            else None
        )

    targets = wait_for(healthy_targets)
    assert targets, "Prometheus has no healthy application target"

    def database_metric_present():
        url = "http://127.0.0.1:18090/api/v1/query?" + urlencode(
            {"query": "mrb_database_connections"}
        )
        data = get(url)
        return data["data"]["result"] if data["status"] == "success" else None

    wait_for(database_metric_present)

    def grafana_ready():
        data = get("http://127.0.0.1:18091/api/health")
        return data if data.get("database") == "ok" else None

    grafana_health = wait_for(grafana_ready)
    password = os.environ.get("GRAFANA_ADMIN_PASSWORD", "isolated-grafana-only")
    credentials = base64.b64encode(f"admin:{password}".encode()).decode()

    def dashboard_ready():
        data = get(
            "http://127.0.0.1:18091/api/dashboards/uid/meetingroom-ops",
            {"Authorization": f"Basic {credentials}"},
        )
        return data if data.get("dashboard", {}).get("uid") == "meetingroom-ops" else None

    dashboard = wait_for(dashboard_ready)

    observed = False
    try:
        subprocess.run([*compose, "stop", "redis"], check=True)
        deadline = monotonic() + 60
        while monotonic() < deadline:
            alerts = get("http://127.0.0.1:18093/api/v2/alerts")
            if any(
                alert["labels"].get("alertname") == "RedisUnavailable"
                for alert in alerts
            ):
                observed = True
                break
            sleep(3)
        assert observed, "RedisUnavailable alert did not reach Alertmanager within 60 seconds"
    finally:
        subprocess.run([*compose, "start", "redis"], check=True)

    result = {
        "prometheus_target": "up",
        "postgres_connection_metric": "present",
        "grafana": grafana_health,
        "dashboard_uid": dashboard["dashboard"]["uid"],
        "redis_alert_received_by_alertmanager": observed,
        "external_notifications": "not configured",
    }
    Path("reports").mkdir(exist_ok=True)
    Path("reports/monitoring.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
