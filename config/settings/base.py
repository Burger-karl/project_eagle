"""
Project Eagle — Base Settings
Shared configuration for all environments.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ── Security ──────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "insecure-dev-key-change-in-production")
DEBUG = os.getenv("DEBUG", "True") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# ── Apps ──────────────────────────────────────────────────────
DJANGO_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "django_filters",
    "django_celery_beat",
    "django_celery_results",
    "drf_spectacular",
]

LOCAL_APPS = [
    "apps.core",
    "apps.sage200",
    "apps.firs",
    "apps.invoices",
    "apps.reporting",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ── Middleware ────────────────────────────────────────────────
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "middleware.request_logger.RequestLoggerMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# ── Templates ─────────────────────────────────────────────────
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ],
    },
}]

# ── Primary Middleware Database ───────────────────────────────
# Uses SQLite for development (zero setup), PostgreSQL for production
import os

if os.getenv("USE_SQLITE", "False") == "True":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        },
        # ── Sage 200 MSSQL (read-only — client's restored .bak) ──
        "sage200": {
            "ENGINE": "mssql",
            "NAME": os.getenv("SAGE200_MSSQL_DATABASE", ""),
            "HOST": os.getenv("SAGE200_MSSQL_SERVER", ""),
            "PORT": "",
            "OPTIONS": {
                "driver": os.getenv("SAGE200_MSSQL_DRIVER", "ODBC Driver 17 for SQL Server"),
                "Trusted_Connection": os.getenv("SAGE200_MSSQL_TRUSTED_CONNECTION", "yes"),
                "user": os.getenv("SAGE200_MSSQL_USERNAME", ""),
                "password": os.getenv("SAGE200_MSSQL_PASSWORD", ""),
            },
            "TEST": {
                "NAME": None,
            },
        },
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "project_eagle"),
            "USER": os.getenv("DB_USER", "eagle_user"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
            "OPTIONS": {
                "connect_timeout": 10,
            },
            "CONN_MAX_AGE": 60,
        },
        # ── Sage 200 MSSQL (read-only — client's restored .bak) ──
        "sage200": {
            "ENGINE": "mssql",
            "NAME": os.getenv("SAGE200_MSSQL_DATABASE", ""),
            "HOST": os.getenv("SAGE200_MSSQL_SERVER", ""),
            "PORT": "",
            "OPTIONS": {
                "driver": os.getenv("SAGE200_MSSQL_DRIVER", "ODBC Driver 17 for SQL Server"),
                "Trusted_Connection": os.getenv("SAGE200_MSSQL_TRUSTED_CONNECTION", "yes"),
                "user": os.getenv("SAGE200_MSSQL_USERNAME", ""),
                "password": os.getenv("SAGE200_MSSQL_PASSWORD", ""),
            },
            "TEST": {
                "NAME": None,
            },
        },
    }


# ── Auth ──────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── REST Framework ────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "utils.exceptions.custom_exception_handler",
}

# ── Celery ────────────────────────────────────────────────────
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Africa/Lagos"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_SOFT_TIME_LIMIT = 300    # 5 min soft limit per task
CELERY_TASK_TIME_LIMIT = 600         # 10 min hard limit per task
CELERY_WORKER_MAX_TASKS_PER_CHILD = 1000  # Restart worker after 1000 tasks
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# ── Celery Beat Schedule (background polling) ─────────────────
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    # Poll Sage 200 every 5 minutes for new invoices
    "poll-sage200-invoices": {
        "task": "apps.invoices.tasks.poll_sage200_invoices",
        "schedule": 300,  # seconds
    },
    # Retry failed FIRS submissions every 15 minutes
    "retry-failed-submissions": {
        "task": "apps.invoices.tasks.retry_failed_submissions",
        "schedule": 900,
    },
    # Refresh Sage 200 API token before it expires (every 7 hours)
    "refresh-sage200-token": {
        "task": "apps.sage200.tasks.refresh_access_token",
        "schedule": 25200,
    },
}

# ── FIRS Configuration ────────────────────────────────────────
USE_FIRS_PRODUCTION = os.getenv("FIRS_USE_PRODUCTION", "False") == "True"
FIRS_BASE_URL = (
    os.getenv("FIRS_PRODUCTION_URL", "https://einvoice.firs.gov.ng/api/v1")
    if USE_FIRS_PRODUCTION
    else os.getenv("FIRS_SANDBOX_URL", "https://sandbox.einvoice.firs.gov.ng/api/v1")
)
FIRS_API_KEY    = os.getenv("FIRS_API_KEY", "")
FIRS_SECRET_KEY = os.getenv("FIRS_SECRET_KEY", "")
FIRS_TIN        = os.getenv("FIRS_TIN", "")

# ── Sage 200 API Configuration ────────────────────────────────
SAGE200_CLIENT_ID     = os.getenv("SAGE200_CLIENT_ID", "")
SAGE200_CLIENT_SECRET = os.getenv("SAGE200_CLIENT_SECRET", "")
SAGE200_SITE_ID       = os.getenv("SAGE200_SITE_ID", "")
SAGE200_COMPANY_ID    = os.getenv("SAGE200_COMPANY_ID", "")
SAGE200_API_BASE_URL  = os.getenv("SAGE200_API_BASE_URL", "")

# ── Client Company Info ───────────────────────────────────────
CLIENT_COMPANY_NAME    = os.getenv("CLIENT_COMPANY_NAME", "")
CLIENT_COMPANY_ADDRESS = os.getenv("CLIENT_COMPANY_ADDRESS", "Lagos, Nigeria")
CLIENT_COMPANY_TIN     = os.getenv("CLIENT_COMPANY_TIN", "")

# ── Email ─────────────────────────────────────────────────────
EMAIL_BACKEND    = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST       = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT       = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS    = True
EMAIL_HOST_USER  = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
ALERT_EMAIL      = os.getenv("ALERT_EMAIL", "")

# ── Static Files ──────────────────────────────────────────────
STATIC_URL  = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL   = "/media/"
MEDIA_ROOT  = BASE_DIR / "media"

# ── Internationalisation ──────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE     = "Africa/Lagos"
USE_I18N      = True
USE_TZ        = True

# ── DRF Spectacular (API Docs) ────────────────────────────────
SPECTACULAR_SETTINGS = {
    "TITLE": "Project Eagle — SAGE 200 → FIRS MBS Middleware API",
    "DESCRIPTION": "Middleware API for integrating SAGE 200 with the FIRS NRS e-Invoice platform.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ── Logging ───────────────────────────────────────────────────
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} [{levelname}] {name}: {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file_info": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOGS_DIR / "info.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
        },
        "file_error": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOGS_DIR / "error.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 10,
            "formatter": "verbose",
            "level": "ERROR",
        },
    },
    "loggers": {
        "apps.invoices": {"handlers": ["console", "file_info", "file_error"], "level": "DEBUG", "propagate": False},
        "apps.firs":     {"handlers": ["console", "file_info", "file_error"], "level": "DEBUG", "propagate": False},
        "apps.sage200":  {"handlers": ["console", "file_info", "file_error"], "level": "DEBUG", "propagate": False},
        "django":        {"handlers": ["console", "file_error"], "level": "WARNING", "propagate": False},
        "celery":        {"handlers": ["console", "file_info"], "level": "INFO", "propagate": False},
    },
}

