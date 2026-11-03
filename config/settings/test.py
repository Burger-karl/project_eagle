"""
Settings for running the test suite.

    python -m pytest --ds=config.settings.test

Tests get one throw-away in-memory SQLite database. The "sage200" MSSQL alias is
dropped on purpose: no code reads Sage 200 through Django's ORM (it uses pyodbc
directly), and some pytest-django versions would otherwise try to build a test
database on the real SQL Server, which fails on versions mssql-django does not
know (for example SQL Server 2025: "SQL Server v17 is not supported").
"""
from .development import *  # noqa: F401,F403

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
}
CELERY_BROKER_URL = "memory://"  # tests must never need Redis
