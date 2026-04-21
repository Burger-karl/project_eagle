"""
Celery Application Configuration
Handles all async invoice processing tasks.
"""
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("project_eagle")
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in all installed apps
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    """Health check task."""
    print(f"Request: {self.request!r}")
