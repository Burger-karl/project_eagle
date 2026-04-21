import os

env = os.getenv("DJANGO_ENV", "development")

if env == "production":
    from .production import *   # noqa
else:
    from .development import *  # noqa
