from django.apps import AppConfig


class Sage300Config(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name  = "apps.sage300"
    label = "sage300"

    def ready(self):
        """Import tasks so Celery autodiscover finds them."""
        import apps.sage300.tasks  # noqa
