"""E2E fixture/client regressions; no network, database or .env access."""

from http.cookiejar import Cookie
from unittest.mock import patch
from uuid import UUID

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from scripts.e2e_http import Session, registration_payload


def add_session_cookie(session):
    session.cookies.set_cookie(
        Cookie(
            0,
            "sessionid",
            "private-cookie-value",
            None,
            False,
            "127.0.0.1",
            False,
            False,
            "/",
            True,
            False,
            None,
            True,
            None,
            None,
            {},
        )
    )


class E2EFixtureTests(SimpleTestCase):
    def test_original_uuid_password_can_fail_real_django_validators(self):
        run = "0123456789abcdef" * 2
        user = User(username="e2e-" + run, email="e2e@example.invalid")
        with self.assertRaises(ValidationError) as caught:
            validate_password("E2E-test-" + run[::-1], user)
        self.assertIn("password_too_similar", [e.code for e in caught.exception.error_list])

    def test_new_fixture_passes_real_validators_even_for_identical_uuids(self):
        for run in ("0123456789abcdef" * 2, "0" * 32, "f" * 32):
            with self.subTest(run=run), patch("scripts.e2e_http.uuid4", return_value=UUID(run)):
                payload = registration_payload(run)
                user = User(username=payload["username"], email=payload["email"])
                validate_password(payload["password1"], user)
                self.assertEqual(payload["password1"], payload["password2"])

    def test_registration_rejects_form_error_with_http_200(self):
        session = Session()
        session.last_path = "/register/"
        with patch.object(session, "request", return_value=(200, "form error", {})):
            with self.assertRaisesRegex(AssertionError, "registration"):
                session.register({})

    def test_registration_accepts_new_session_and_home_redirect(self):
        session = Session()

        def response(path, data=None):
            if data is not None:
                add_session_cookie(session)
                session.last_path = "/"
            return 200, "", {}

        with patch.object(session, "request", side_effect=response):
            session.register({"username": "synthetic-user"})

    def test_login_cannot_pass_using_registration_cookie(self):
        session = Session()
        add_session_cookie(session)
        session.last_path = "/login/"
        with patch.object(session, "request", return_value=(200, "invalid login", {})):
            with self.assertRaisesRegex(AssertionError, "login"):
                session.login("synthetic-user", "private-password")
        self.assertEqual(list(session.cookies), [])

    def test_login_checks_subsequent_authenticated_request(self):
        session = Session()
        add_session_cookie(session)

        def response(path, data=None):
            if path == "/login/" and data is None:
                self.assertEqual(list(session.cookies), [])
            if data is not None:
                add_session_cookie(session)
            session.last_path = "/" if data is not None else path
            return 200, "", {}

        with patch.object(session, "request", side_effect=response) as request:
            session.login("synthetic-user", "private-password")
        self.assertEqual(request.call_args.args, ("/my-bookings/",))

    def test_session_cookie_without_authenticated_access_is_rejected(self):
        session = Session()

        def response(path, data=None):
            if data is not None:
                add_session_cookie(session)
            session.last_path = "/" if data is not None else "/login/"
            return 200, "", {}

        with patch.object(session, "request", side_effect=response):
            with self.assertRaisesRegex(AssertionError, "session reuse"):
                session.login("synthetic-user", "private-password")

    def test_failed_status_is_rejected_without_logging_secrets(self):
        session = Session()
        add_session_cookie(session)
        session.last_path = "/"
        with self.assertRaises(AssertionError) as caught:
            session.assert_authenticated(403, "/", "login")
        diagnostic = str(caught.exception)
        self.assertIn("403", diagnostic)
        self.assertIn("sessionid", diagnostic)
        self.assertNotIn("private-cookie-value", diagnostic)
