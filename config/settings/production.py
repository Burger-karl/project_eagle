"""
Production settings — security hardened, Sentry enabled.
All secrets MUST come from environment variables.
"""
from .base import *  # noqa
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.celery import CeleryIntegration

DEBUG = False

# Security
SECURE_SSL_REDIRECT          = True
SECURE_HSTS_SECONDS          = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD          = True
SECURE_CONTENT_TYPE_NOSNIFF  = True
SECURE_BROWSER_XSS_FILTER    = True
X_FRAME_OPTIONS              = "DENY"
SESSION_COOKIE_SECURE        = True
CSRF_COOKIE_SECURE           = True

CORS_ALLOWED_ORIGINS = [
    "https://yourdomain.com",
]

# Sentry — error tracking in production
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN", ""),
    integrations=[DjangoIntegration(), CeleryIntegration()],
    traces_sample_rate=0.1,
    send_default_pii=False,
    environment="production",
)
