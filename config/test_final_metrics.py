from django.test import TestCase, override_settings


@override_settings(METRICS_TOKEN="final-metrics-token")
class MetricsAuthorizationTests(TestCase):
    def test_valid_token_still_works(self):
        response = self.client.get("/metrics/", HTTP_AUTHORIZATION="Bearer final-metrics-token")
        self.assertContains(response, "mrb_database_up 1")
        self.assertNotContains(response, "final-metrics-token")

    def test_invalid_headers_are_uniform_404(self):
        for header in (None, "", "Bearer incorrect", "Bearer é", "Bearer токен", "x" * 100000):
            with self.subTest(header=None if header is None else header[:30]):
                kwargs = {} if header is None else {"HTTP_AUTHORIZATION": header}
                response = self.client.get("/metrics/", **kwargs)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.content, b"")

    @override_settings(METRICS_TOKEN="")
    def test_unconfigured_token_denies_access(self):
        self.assertEqual(
            self.client.get("/metrics/", HTTP_AUTHORIZATION="Bearer ").status_code, 404
        )

    @override_settings(METRICS_TOKEN="токен")
    def test_invalid_token_configuration_fails_closed(self):
        self.assertEqual(
            self.client.get("/metrics/", HTTP_AUTHORIZATION="Bearer токен").status_code, 404
        )
