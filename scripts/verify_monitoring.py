"""Observe a real RedisUnavailable alert in the local Prometheus/Alertmanager."""

import json
from pathlib import Path
import subprocess
from time import monotonic, sleep
from urllib.request import urlopen

COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    "docker/test.env",
    "-p",
    "mrb-infra-test",
    "-f",
    "compose.yaml",
    "-f",
    "compose.test.yaml",
    "-f",
    "compose.monitoring.yaml",
]


def get(url):
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def main():
    targets = get("http://127.0.0.1:18090/api/v1/targets")["data"]["activeTargets"]
    assert targets and all(target["health"] == "up" for target in targets)
    try:
        subprocess.run([*COMPOSE, "stop", "redis"], check=True)
        deadline = monotonic() + 60
        observed = False
        while monotonic() < deadline:
            alerts = get("http://127.0.0.1:18093/api/v2/alerts")
            if any(alert["labels"].get("alertname") == "RedisUnavailable" for alert in alerts):
                observed = True
                break
            sleep(3)
        assert observed, "Alert did not reach Alertmanager within 60 seconds"
    finally:
        subprocess.run([*COMPOSE, "start", "redis"], check=True)
    result = {
        "scrape": "up",
        "RedisUnavailable_received_by_alertmanager": observed,
        "external_notifications": "not configured",
    }
    Path("reports").mkdir(exist_ok=True)
    Path("reports/monitoring.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
