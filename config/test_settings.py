"""Isolated tests: no .env reads or production database connections."""
import ast
from pathlib import Path
_ALLOWED = {"INSTALLED_APPS", "MIDDLEWARE", "TEMPLATES", "ROOT_URLCONF", "AUTH_PASSWORD_VALIDATORS", "LANGUAGE_CODE", "TIME_ZONE", "USE_I18N", "USE_TZ", "STATIC_URL", "LOGIN_URL", "LOGIN_REDIRECT_URL", "LOGOUT_REDIRECT_URL", "REST_FRAMEWORK", "MEDIA_URL", "DEFAULT_AUTO_FIELD"}
for _node in ast.parse(Path(__file__).with_name("settings.py").read_text(encoding="utf-8-sig")).body:
    if isinstance(_node, ast.Assign):
        for _target in _node.targets:
            if isinstance(_target, ast.Name) and _target.id in _ALLOWED:
                globals()[_target.id] = ast.literal_eval(_node.value)
SECRET_KEY = "isolated-tests-only"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ALLOWED_HOSTS = ["testserver", "localhost"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
