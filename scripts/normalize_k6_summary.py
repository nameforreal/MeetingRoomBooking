"""Normalize and validate k6's legacy machine-readable summary export."""

import argparse
import json
import math
from pathlib import Path

HTTP_ERROR_RATE_LIMIT = 0.01
P95_LIMIT_MS = 1000.0
CHECK_PASS_RATE_LIMIT = 0.99


def _metric(metrics, name):
    metric = metrics.get(name)
    if not isinstance(metric, dict):
        raise ValueError(f"Required k6 metric is missing or invalid: {name}")
    return metric


def _number(metric, key, context):
    value = metric.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Required numeric value is missing: {context}.{key}")
    if not math.isfinite(value):
        raise ValueError(f"Non-finite numeric value: {context}.{key}")
    return float(value)


def _count(metric, key, context):
    value = _number(metric, key, context)
    if value < 0 or not value.is_integer():
        raise ValueError(f"Expected a non-negative integer: {context}.{key}")
    return int(value)


def _threshold_result(metric, expression, observed, limit, unit, passed, required):
    threshold_map = metric.get("thresholds", {})
    if not isinstance(threshold_map, dict):
        raise ValueError("k6 metric thresholds must be an object")
    exceeded = threshold_map.get(expression)
    if exceeded is None:
        if required:
            raise ValueError(f"Required k6 threshold is missing: {expression}")
    elif not isinstance(exceeded, bool):
        raise ValueError(f"k6 threshold flag must be boolean: {expression}")
    elif exceeded != (not passed):
        raise ValueError(f"k6 threshold flag disagrees with measured value: {expression}")

    return {
        "expression": expression,
        "observed": round(observed, 6),
        "limit": limit,
        "unit": unit,
        "passed": passed,
        "k6_raw_exceeded": exceeded,
    }


def normalize_summary(summary):
    """Return a clear summary and reject contradictory raw k6 threshold flags."""
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("The k6 summary has no metrics object")

    requests = _metric(metrics, "http_reqs")
    failed_rate_metric = _metric(metrics, "http_req_failed")
    duration = _metric(metrics, "http_req_duration")
    checks = _metric(metrics, "checks")
    max_vus_metric = _metric(metrics, "vus_max")
    iterations_metric = _metric(metrics, "iterations")

    request_count = _count(requests, "count", "http_reqs")
    if request_count == 0:
        raise ValueError("The k6 summary contains no HTTP requests")

    # Rate counters count non-zero samples as passes. For this metric, those
    # samples are failed requests; zero samples are successful requests.
    failed_requests = _count(failed_rate_metric, "passes", "http_req_failed")
    successful_requests = _count(failed_rate_metric, "fails", "http_req_failed")
    if failed_requests + successful_requests != request_count:
        raise ValueError("http_req_failed counters do not match http_reqs.count")

    error_rate = _number(failed_rate_metric, "value", "http_req_failed")
    calculated_error_rate = failed_requests / request_count
    if not math.isclose(error_rate, calculated_error_rate, abs_tol=1e-9):
        raise ValueError("http_req_failed.value disagrees with its sample counters")

    request_rate = _number(requests, "rate", "http_reqs")
    error_threshold = _threshold_result(
        failed_rate_metric,
        "rate<0.01",
        error_rate,
        HTTP_ERROR_RATE_LIMIT,
        "ratio",
        error_rate < HTTP_ERROR_RATE_LIMIT,
        required=True,
    )

    p95 = _number(duration, "p(95)", "http_req_duration")
    p95_threshold = _threshold_result(
        duration,
        "p(95)<1000",
        p95,
        P95_LIMIT_MS,
        "ms",
        p95 < P95_LIMIT_MS,
        required=True,
    )

    check_passes = _count(checks, "passes", "checks")
    check_failures = _count(checks, "fails", "checks")
    check_total = check_passes + check_failures
    if check_total == 0:
        raise ValueError("The k6 summary contains no checks")
    check_rate = check_passes / check_total
    check_value = _number(checks, "value", "checks")
    if not math.isclose(check_value, check_rate, abs_tol=1e-9):
        raise ValueError("checks.value disagrees with its sample counters")
    checks_threshold = _threshold_result(
        checks,
        "rate>0.99",
        check_rate,
        CHECK_PASS_RATE_LIMIT,
        "ratio",
        check_rate > CHECK_PASS_RATE_LIMIT,
        required=False,
    )

    thresholds = {
        "http_req_failed": error_threshold,
        "http_req_duration": p95_threshold,
        "checks": checks_threshold,
    }
    passed = all(item["passed"] for item in thresholds.values())

    return {
        "schema_version": 1,
        "source": "k6 --summary-export",
        "result": "passed" if passed else "failed",
        "virtual_users_max": _count(max_vus_metric, "value", "vus_max"),
        "requests": {
            "total": request_count,
            "failed": failed_requests,
            "successful": successful_requests,
            "error_rate": round(error_rate, 6),
            "average_rate_per_second": round(request_rate, 4),
        },
        "checks": {
            "total": check_total,
            "passed": check_passes,
            "failed": check_failures,
            "pass_rate": round(check_rate, 6),
        },
        "iterations": _count(iterations_metric, "count", "iterations"),
        "latency_ms": {
            "average": round(_number(duration, "avg", "http_req_duration"), 4),
            "p95": round(p95, 4),
            "p99": round(_number(duration, "p(99)", "http_req_duration"), 4),
            "maximum": round(_number(duration, "max", "http_req_duration"), 4),
        },
        "thresholds": thresholds,
        "request_scope": (
            "Read-only GET requests to /api/rooms/ and /rooms/, plus one "
            "pre-test GET /health/; no booking writes."
        ),
        "raw_k6_json_interpretation": {
            "threshold_flags": (
                "true means the threshold was exceeded (failed); false means "
                "the threshold was not exceeded (passed)."
            ),
            "http_req_failed_rate_counters": (
                "For this Rate metric, passes counts non-zero samples (failed "
                "requests), while fails counts zero samples (successful "
                "requests). The value field is the actual failure rate."
            ),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, default=Path("reports/k6-summary.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/k6-summary-readable.json")
    )
    args = parser.parse_args()

    try:
        summary = json.loads(args.input.read_text(encoding="utf-8"))
        normalized = normalize_summary(summary)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(2, f"ERROR: cannot normalize k6 summary: {exc}\n")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "k6 summary: "
        f"{normalized['result']}; "
        f"{normalized['requests']['failed']}/"
        f"{normalized['requests']['total']} HTTP errors; "
        f"{normalized['checks']['passed']}/"
        f"{normalized['checks']['total']} checks passed; "
        f"max VUs={normalized['virtual_users_max']}"
    )
    return 0 if normalized["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
