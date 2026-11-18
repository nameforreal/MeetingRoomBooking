"""Read-only HTTP smoke check for the isolated Compose project."""

import json
from urllib.request import urlopen


def main():
    base = "http://127.0.0.1:18063"
    with urlopen(base + "/health/", timeout=10) as response:
        assert json.load(response) == {"status": "ok", "cache": "ok"}
    for path in ["/login/", "/static/rooms/enterprise.css"]:
        with urlopen(base + path, timeout=10) as response:
            assert response.status == 200
    print("Django, PostgreSQL, Redis, static and login: OK")


if __name__ == "__main__":
    main()
