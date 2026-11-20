"""Verify local Prometheus, Grafana provisioning, PostgreSQL metrics and alerts."""
import base64
import json
import os
from pathlib import Path
import subprocess
from time import monotonic, sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen

COMPOSE = ["docker", "compose", "--env-file", "docker/test.env", "-p", "mrb-infra-test", "-f", "compose.yaml", "-f", "compose.test.yaml", "-f", "compose.monitoring.yaml"]


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
    targets = wait_for(
        lambda: (
            data["data"]["activeTargets"]
            if (data := get("http://127.0.0.1:18090/api/v1/targets"))["data"]["activeTargets"]
            and all(target["health"] == "up" for target in data["data"]["activeTargets"])
            else None
        )
    )
    assert targets, "Prometheus has no healthy application target"

    database_metric = wait_for(
        lambda: (
            data["data"]["result"]
            if (data := get("http://127.0.0.1:18090/api/v1/query?" + urlencode({"query": "mrb_database_connections"})))["status"] == "success"
            and data["data"]["result"]
            else None
        )
    )

    grafana_health = wait_for(lambda: get("http://127.0.0.1:18091/api/health").get("database") == "ok")
    password = os.environ.get("GRAFANA_ADMIN_PASSWORD", "isolated-grafana-only")
    credentials = base64.b64encode(f"admin:{password}".encode()).decode()
    dashboard = wait_for(
        lambda: get(
            "http://127.0.0.1:18091/api/dashboards/uid/meetingroom-ops",
            {"Authorization": f"Basic {credentials}"},
        )
    )
    assert dashboard["dashboard"]["uid"] == "meetingroom-ops"

    observed = False
    try:
        subprocess.run([*COMPOSE, "stop", "redis"], check=True)
        deadline = monotonic() + 60
        while monotonic() < deadline:
            alerts = get("http://127.0.0.1:18093/api/v2/alerts")
            if any(alert["labels"].get("alertname") == "RedisUnavailable" for alert in alerts):
                observed = True
                break
            sleep(3)
        assert observed, "RedisUnavailable alert did not reach Alertmanager within 60 seconds"
    finally:
        subprocess.run([*COMPOSE, "start", "redis"], check=True)

    result = {
        "prometheus_target": "up",
        "postgres_connection_metric": "present",
        "grafana": grafana_health,
        "dashboard_uid": dashboard["dashboard"]["uid"],
        "redis_alert_received_by_alertmanager": observed,
        "external_notifications": "not configured",
    }
    Path("reports").mkdir(exist_ok=True)
    Path("reports/monitoring.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
