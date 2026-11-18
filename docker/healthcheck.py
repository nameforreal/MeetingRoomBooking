import http.client
import os

connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=4)
host = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip()
connection.request("GET", "/health/", headers={"Host": host})
response = connection.getresponse()
raise SystemExit(0 if response.status == 200 else 1)
