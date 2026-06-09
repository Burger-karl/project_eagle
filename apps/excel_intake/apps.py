from django.apps import AppConfig


class ExcelIntakeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name  = "apps.excel_intake"
    label = "excel_intake"

    def ready(self):
        import apps.excel_intake.tasks  # noqa
