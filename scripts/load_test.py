"""Short paced read-only load, hard-limited to the isolated HTTP port."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import BoundedSemaphore
from time import monotonic, sleep
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rps", type=int, choices=[100, 200], required=True)
    parser.add_argument("--seconds", type=int, default=15, choices=range(5, 61))
    args = parser.parse_args()
    base = "http://127.0.0.1:18063"
    with urlopen(base + "/health/", timeout=5) as response:
        assert json.load(response)["status"] == "ok"
    limit = BoundedSemaphore(200)
    started = monotonic()
    results, dropped = [], 0

    def request(scheduled, number):
        try:
            # Exercise Django + SQL and a real HTML page; exclude static-only traffic.
            path = "/api/rooms/" if number % 5 else "/rooms/"
            with urlopen(base + path, timeout=5) as response:
                response.read()
                return response.status < 400, monotonic() - scheduled
        except Exception:
            return False, monotonic() - scheduled
        finally:
            limit.release()

    with ThreadPoolExecutor(max_workers=100) as pool:
        for number in range(args.rps * args.seconds):
            scheduled = started + number / args.rps
            sleep(max(0, scheduled - monotonic()))
            if limit.acquire(blocking=False):
                results.append(pool.submit(request, scheduled, number))
            else:
                dropped += 1
        completed = [future.result() for future in results]
    elapsed = monotonic() - started
    latencies = sorted(seconds * 1000 for _, seconds in completed)
    failures = sum(not ok for ok, _ in completed)
    result = {
        "target_rps": args.rps,
        "duration_seconds": args.seconds,
        "elapsed_seconds": round(elapsed, 3),
        "requests": len(completed),
        "dropped": dropped,
        "http_errors": failures,
        "achieved_rps": round(len(completed) / elapsed, 2),
        "p50_ms": round(latencies[int(0.50 * (len(latencies) - 1))], 2),
        "p95_ms": round(latencies[int(0.95 * (len(latencies) - 1))], 2),
        "p99_ms": round(latencies[int(0.99 * (len(latencies) - 1))], 2),
        "scope": "80% public room API, 20% HTML catalog; read-only, no booking writes",
    }
    Path("reports").mkdir(exist_ok=True)
    Path(f"reports/load-{args.rps}.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result))
    # A failed performance target must not be reported as a successful load gate.
    raise SystemExit(
        0
        if failures == 0
        and dropped == 0
        and result["achieved_rps"] >= 0.95 * args.rps
        and result["p95_ms"] < 1000
        else 1
    )


if __name__ == "__main__":
    main()
