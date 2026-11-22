"""Unit tests for the k6 summary normalizer."""

import copy
import unittest

from scripts.normalize_k6_summary import normalize_summary


def sample_summary():
    return {
        "metrics": {
            "http_reqs": {"count": 100, "rate": 50.0},
            "http_req_failed": {
                "passes": 0,
                "fails": 100,
                "value": 0.0,
                "thresholds": {"rate<0.01": False},
            },
            "http_req_duration": {
                "avg": 12.0,
                "p(95)": 30.0,
                "p(99)": 45.0,
                "max": 60.0,
                "thresholds": {"p(95)<1000": False},
            },
            "checks": {
                "passes": 100,
                "fails": 0,
                "value": 1.0,
                "thresholds": {"rate>0.99": False},
            },
            "vus_max": {"value": 1000},
            "iterations": {"count": 100},
        }
    }


class K6SummaryNormalizerTests(unittest.TestCase):
    def test_passing_summary_has_unambiguous_counts_and_thresholds(self):
        normalized = normalize_summary(sample_summary())

        self.assertEqual(normalized["result"], "passed")
        self.assertEqual(normalized["requests"]["failed"], 0)
        self.assertEqual(normalized["requests"]["successful"], 100)
        self.assertEqual(normalized["checks"]["passed"], 100)
        self.assertEqual(normalized["checks"]["failed"], 0)
        self.assertTrue(
            all(item["passed"] for item in normalized["thresholds"].values())
        )
        self.assertFalse(
            normalized["thresholds"]["http_req_failed"]["k6_raw_exceeded"]
        )

    def test_http_req_failed_rate_counters_are_interpreted_correctly(self):
        summary = sample_summary()
        metric = summary["metrics"]["http_req_failed"]
        metric.update(
            {
                "passes": 2,
                "fails": 98,
                "value": 0.02,
                "thresholds": {"rate<0.01": True},
            }
        )

        normalized = normalize_summary(summary)

        self.assertEqual(normalized["requests"]["failed"], 2)
        self.assertEqual(normalized["requests"]["successful"], 98)
        self.assertEqual(normalized["requests"]["error_rate"], 0.02)
        self.assertEqual(normalized["result"], "failed")

    def test_raw_threshold_flag_must_match_measured_value(self):
        summary = sample_summary()
        summary["metrics"]["http_req_duration"]["thresholds"]["p(95)<1000"] = True

        with self.assertRaisesRegex(ValueError, "disagrees with measured value"):
            normalize_summary(summary)

    def test_failed_threshold_is_reported_as_failed(self):
        summary = sample_summary()
        summary["metrics"]["http_req_duration"]["p(95)"] = 1200.0
        summary["metrics"]["http_req_duration"]["thresholds"]["p(95)<1000"] = True
        summary["metrics"]["checks"].update(
            {
                "passes": 98,
                "fails": 2,
                "value": 0.98,
                "thresholds": {"rate>0.99": True},
            }
        )

        normalized = normalize_summary(summary)

        self.assertEqual(normalized["result"], "failed")
        self.assertFalse(normalized["thresholds"]["checks"]["passed"])

    def test_legacy_summary_without_checks_threshold_is_supported(self):
        summary = copy.deepcopy(sample_summary())
        summary["metrics"]["checks"].pop("thresholds")

        normalized = normalize_summary(summary)

        self.assertEqual(normalized["result"], "passed")
        self.assertIsNone(normalized["thresholds"]["checks"]["k6_raw_exceeded"])

    def test_inconsistent_request_counters_are_rejected(self):
        summary = sample_summary()
        summary["metrics"]["http_req_failed"]["fails"] = 99

        with self.assertRaisesRegex(ValueError, "do not match"):
            normalize_summary(summary)


if __name__ == "__main__":
    unittest.main()
