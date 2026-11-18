"""Failure/restart checks ONLY for the named isolated Compose projects.

Preserves all test data, volumes and containers. Run after initial migrate/up.
"""

import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", choices=["mrb-infra-test", "mrb-ci"], default="mrb-infra-test")
    args = parser.parse_args()
    compose = [
        "docker",
        "compose",
        "--env-file",
        "docker/test.env",
        "-p",
        args.project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.test.yaml",
    ]
    run_id = uuid4().hex
    code = Path("scripts/infra_data_probe.py").read_text(encoding="utf-8")

    def command(*parts, **kwargs):
        return subprocess.run([*compose, *parts], check=True, text=True, **kwargs)

    def data(mode):
        result = command(
            "exec",
            "-T",
            "-e",
            "INFRA_TEST_RUN=" + run_id,
            "-e",
            "INFRA_TEST_MODE=" + mode,
            "app",
            "python",
            "-",
            input=code,
            capture_output=True,
        )
        return json.loads(result.stdout)

    def health(expected_status=200, expected_cache="ok"):
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            try:
                with urlopen("http://127.0.0.1:18063/health/", timeout=8) as response:
                    payload = json.load(response)
                    if (
                        response.status == expected_status
                        and payload.get("cache") == expected_cache
                    ):
                        return
            except HTTPError as exc:
                if exc.code == expected_status:
                    return
            except URLError, TimeoutError:
                pass
            time.sleep(1)
        raise AssertionError("Readiness did not reach expected state")

    health()
    first = data("seed")
    try:
        command("stop", "redis")
        health(expected_cache="unavailable")
        assert data("replay") == first
        second = data("outage")
        assert len(second["bookings"]) == len(second["receipts"]) == len(second["events"]) == 2
        assert data("replay") == second
        print("Redis outage: booking writes and idempotent replay preserved", flush=True)
    finally:
        command("start", "redis")
    health()
    try:
        command("stop", "db")
        health(expected_status=503)
    finally:
        command("start", "db")
    health()
    assert data("replay") == second
    print("PostgreSQL stop/start: data and application recovery OK", flush=True)
    command("stop")
    command("up", "-d", "--wait", "--wait-timeout", "120", "--no-recreate")
    health()
    assert data("replay") == second
    command("up", "-d", "--wait", "--no-recreate")
    assert data("replay") == second
    print("Graceful stop, repeat Compose start, persistent history: OK", flush=True)


if __name__ == "__main__":
    main()
