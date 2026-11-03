from django.db import models

from apps.core.models import ClientCompany


class SagePullState(models.Model):
    """
    Where a client's manual Sage 200 pulls have got to.

    `last_record_id` is a bookmark: the highest Sage record number (PostAR.AutoIdx)
    that has been read. The next pull continues after it, so pulling 50 and then
    50 more gives 100 different invoices.
    """

    client = models.OneToOneField(ClientCompany, on_delete=models.CASCADE, related_name="sage_pull_state")
    last_record_id = models.BigIntegerField(default=0, help_text="Highest Sage 200 record number already read.")
    last_pull_at = models.DateTimeField(null=True, blank=True)
    last_pull_summary = models.JSONField(default=dict, blank=True, help_text="Counts and errors from the last pull.")
    running_since = models.DateTimeField(null=True, blank=True, help_text="Set while a pull is in progress.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Sage pull state"
        verbose_name_plural = "Sage pull states"

    def __str__(self):
        return f"{self.client.name}: read up to Sage record {self.last_record_id}"
