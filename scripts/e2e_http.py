"""Real HTTP/Nginx/Gunicorn/PostgreSQL E2E; only the isolated Compose project.

No Django test client or mocked business calls. Leaves its own fixtures for review.
"""

import argparse
import csv
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from io import StringIO
import json
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

BASE = "http://127.0.0.1:18063"


def registration_payload(run):
    # A single UUID password can resemble the UUID username to Django's
    # UserAttributeSimilarityValidator (quick_ratio compares character counts).
    # 73 characters vs. a 36-character username bounds similarity below 0.7,
    # even if both random UUIDs happen to contain exactly the same characters.
    password = "E2E-test-" + uuid4().hex + uuid4().hex
    return {
        "username": "e2e-" + run,
        "email": "e2e@example.invalid",
        "password1": password,
        "password2": password,
    }


class Session:
    def __init__(self):
        self.cookies = CookieJar()
        self.http = build_opener(HTTPCookieProcessor(self.cookies))
        self.last_path = None

    def request(self, path, data=None, *, method=None, as_json=False, headers=None):
        headers = dict(headers or {})
        if data is not None:
            csrf = next((c.value for c in self.cookies if c.name == "csrftoken"), "")
            headers["X-CSRFToken"] = csrf
            headers["Referer"] = BASE + "/"
            headers["Content-Type"] = (
                "application/json" if as_json else "application/x-www-form-urlencoded"
            )
            data = (
                json.dumps(data).encode()
                if as_json
                else urlencode(data | {"csrfmiddlewaretoken": csrf}).encode()
            )
        try:
            response = self.http.open(
                Request(BASE + path, data=data, headers=headers, method=method), timeout=15
            )
        except HTTPError as exc:
            response = exc
        self.last_path = urlsplit(response.geturl()).path
        content = response.read()
        body = (
            json.loads(content)
            if "application/json" in response.headers.get("Content-Type", "")
            else content.decode("utf-8-sig")
        )
        return response.status, body, response.headers

    def assert_authenticated(self, status, path, stage):
        # Never include cookie values, submitted credentials or raw HTML in logs.
        diagnostic = {
            "stage": stage,
            "status": status,
            "final_path": self.last_path,
            "cookie_names": sorted(c.name for c in self.cookies),
        }
        assert status == 200 and any(c.name == "sessionid" for c in self.cookies), diagnostic
        assert self.last_path == path, diagnostic

    def register(self, payload):
        self.cookies.clear()
        assert self.request("/register/")[0] == 200
        status, _, _ = self.request("/register/", payload)
        self.assert_authenticated(status, "/", "registration")

    def login(self, username, password):
        # Registration logs the user in. A fresh jar proves login itself works.
        self.cookies.clear()
        assert self.request("/login/")[0] == 200
        status, _, _ = self.request("/login/", {"username": username, "password": password})
        self.assert_authenticated(status, "/", "login")
        status, _, _ = self.request("/my-bookings/")
        self.assert_authenticated(status, "/my-bookings/", "session reuse")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", choices=["mrb-infra-test", "mrb-ci"], default="mrb-infra-test")
    args = parser.parse_args()
    run = uuid4().hex
    registration = registration_payload(run)
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
    seed = """import os,json,django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rooms.models import Room
db=settings.DATABASES['default']
assert (db['HOST'],db['NAME'],db['USER'])==('db','booking_tests','booking_tests') and not settings.PRODUCTION
run=os.environ['E2E_RUN']; u=get_user_model().objects.create_user(username='staff-'+run,password='E2E-test-'+run,is_staff=True)
u.user_permissions.add(*Permission.objects.filter(content_type__app_label='rooms',codename__in=['view_booking','change_booking','export_booking']))
normal=Room.objects.create(name='E2E-'+run); vip=Room.objects.create(name='E2E-VIP-'+run,is_vip=True)
print(json.dumps({'room':normal.pk,'vip':vip.pk}))
"""
    seeded = subprocess.run(
        [*compose, "exec", "-T", "-e", "E2E_RUN=" + run, "app", "python", "-"],
        input=seed,
        text=True,
        capture_output=True,
        check=True,
    )
    rooms = json.loads(seeded.stdout)
    user, staff, stranger = Session(), Session(), Session()
    user.register(registration)
    user.login(registration["username"], registration["password1"])
    staff.login("staff-" + run, "E2E-test-" + run)
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    payload = {
        "room": rooms["room"],
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
    }
    headers = {"Idempotency-Key": run}
    status, booking, _ = user.request("/api/bookings/", payload, as_json=True, headers=headers)
    assert status == 201 and booking["status"] == "confirmed", (status, booking)
    status, replay, response_headers = user.request(
        "/api/bookings/", payload, as_json=True, headers=headers
    )
    assert (
        status == 201 and replay == booking and response_headers["Idempotency-Replayed"] == "true"
    )
    assert (
        user.request(
            "/api/bookings/", payload, as_json=True, headers={"Idempotency-Key": run + "conflict"}
        )[0]
        == 400
    )
    path = f"/api/bookings/{booking['id']}/"
    assert stranger.request(path)[0] == 403
    assert staff.request(path)[0] == 404  # Staff API is still owner-scoped.
    assert user.request(path, {"room": rooms["vip"]}, method="PATCH", as_json=True)[0] == 400
    status, pending, _ = user.request(
        path, {"room": rooms["vip"], "revision": booking["revision"]}, method="PATCH", as_json=True
    )
    assert status == 200 and pending["status"] == "pending"
    assert (
        user.request(
            path,
            {"room": rooms["room"], "revision": booking["revision"]},
            method="PATCH",
            as_json=True,
        )[0]
        == 400
    )
    approval = f"/management/bookings/{booking['id']}/status/"
    user.request(approval, {"revision": pending["revision"], "status": "confirmed"})
    assert user.request(path)[1]["status"] == "pending"
    staff.request(approval, {"revision": pending["revision"], "status": "confirmed"})
    confirmed = user.request(path)[1]
    assert confirmed["status"] == "confirmed"
    params = urlencode(
        {
            "room": rooms["vip"],
            "date_from": start.date().isoformat(),
            "date_to": start.date().isoformat(),
        }
    )
    status, content, _ = staff.request("/management/export.csv?" + params)
    rows = list(csv.reader(StringIO(content), delimiter=";"))
    assert status == 200 and len(rows) == 2 and int(rows[1][0]) == booking["id"]
    cancel = path + "cancel/"
    status, cancelled, _ = user.request(cancel, {"revision": confirmed["revision"]}, as_json=True)
    assert status == 200 and cancelled["status"] == "cancelled"
    assert user.request(cancel, {"revision": confirmed["revision"]}, as_json=True)[1] == cancelled
    assert user.request("/api/bookings/", payload, as_json=True, headers=headers)[1] == booking
    assert user.request("/my-bookings/")[0] == 200
    print(
        "HTTP E2E PASS: registration, sessions/CSRF, creation, conflict, replay, revision, VIP approval, access, date/room CSV, cancellation and history"
    )


if __name__ == "__main__":
    main()
