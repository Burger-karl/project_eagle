"""
Test settings — fully self-contained.

Forces BOTH database aliases to in-memory SQLite so the test run never
touches SQL Server (fixes "NotSupportedError: SQL Server v17 is not
supported" when pytest tries to create a test DB on the sage200 alias).
"""
from .base import *  # noqa

DEBUG = False
SECRET_KEY = "test-only-secret-key"

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    "sage200": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
}

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

APP_PROVIDER = "digitax"
FIRS_API_KEY = "test-key"
