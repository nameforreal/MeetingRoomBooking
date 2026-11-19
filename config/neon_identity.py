"""Read-only Neon control-plane identity verification; fail closed, no secret diagnostics."""

import json
import os
import re
import ssl
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.core.management.base import CommandError

API = "https://console.neon.tech/api/v2"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CommandError("Neon identity verification refused a redirect.")


def api_get(path, token):
    try:
        opener = build_opener(
            ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
        )
        request = Request(
            API + path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}
        )
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError
            body = response.read(1048577)
            if len(body) > 1048576:
                raise ValueError
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
    except Exception:
        raise CommandError(
            "Cannot independently verify Neon identity; no migrations permitted."
        ) from None


def verify_endpoint(host):
    ids = [
        os.environ.get(name, "")
        for name in ("NEON_PROJECT_ID", "NEON_BRANCH_ID", "NEON_ENDPOINT_ID")
    ]
    token = os.environ.get("NEON_API_KEY", "")
    if (
        not all(re.fullmatch(r"[A-Za-z0-9-]{1,128}", value) for value in ids)
        or not token
        or not token.isascii()
        or any(ord(c) <= 32 for c in token)
    ):
        raise CommandError(
            "Configure Neon API credentials and project/branch/endpoint IDs in environment."
        )
    project_id, branch_id, endpoint_id = ids
    try:
        project = api_get(f"/projects/{project_id}", token)["project"]
        branch = api_get(f"/projects/{project_id}/branches/{branch_id}", token)["branch"]
        endpoint = api_get(f"/projects/{project_id}/endpoints/{endpoint_id}", token)["endpoint"]
        valid = (
            project["id"] == project_id
            and branch["id"] == branch_id
            and branch["project_id"] == project_id
            and branch["name"] == "hosting-test"
            and branch["default"] is False
            and branch.get("protected", False) is False
            and endpoint["id"] == endpoint_id
            and endpoint["project_id"] == project_id
            and endpoint["branch_id"] == branch_id
            and endpoint["host"] == host
            and endpoint["type"] == "read_write"
            and "-pooler." not in host
        )
        if not valid:
            raise ValueError
    except Exception:
        raise CommandError(
            "Neon API identity mismatch or unavailable evidence; no migrations permitted."
        ) from None
