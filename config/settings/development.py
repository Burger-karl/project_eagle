# """Development settings — debug on, local database."""
# from .base import *  # noqa

# DEBUG = True
# CORS_ALLOW_ALL_ORIGINS = True

# INSTALLED_APPS += ["debug_toolbar"]
# MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]

# INTERNAL_IPS = ["127.0.0.1"]

# # Use console email backend in dev — emails print to terminal
# EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


"""Development settings."""
from .base import *  # noqa

DEBUG = True
CORS_ALLOW_ALL_ORIGINS = True

# Remove debug toolbar completely for now — causes djdt namespace error
# We don't need it to test the middleware
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "debug_toolbar"]
MIDDLEWARE = [m for m in MIDDLEWARE if "debug_toolbar" not in m]

# Use console email backend in dev
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"