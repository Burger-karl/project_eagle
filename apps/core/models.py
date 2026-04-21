"""
Core Models
Base abstract models used across the entire middleware.
Every model in Project Eagle inherits from TimeStampedModel.
"""
import uuid
from django.db import models


class TimeStampedModel(models.Model):
    """Base model with created/updated timestamps on every record."""
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ClientCompany(TimeStampedModel):
    """
    Represents one of Link Options' accounting clients.
    Each client has their own Sage 200 database and FIRS credentials.
    Supports the multi-tenant structure from the Eagle proposal.
    """
    name           = models.CharField(max_length=255, unique=True)
    tin            = models.CharField(max_length=50, unique=True, help_text="FIRS Tax Identification Number")
    address        = models.TextField()
    email          = models.EmailField()
    phone          = models.CharField(max_length=50, blank=True)
    is_active      = models.BooleanField(default=True)

    # Sage 200 MSSQL connection details for THIS client
    mssql_server   = models.CharField(max_length=255, help_text="SQL Server instance name e.g. SERVER\\SQLEXPRESS")
    mssql_database = models.CharField(max_length=255, help_text="Sage 200 database name from .bak restore")
    mssql_driver   = models.CharField(max_length=100, default="ODBC Driver 17 for SQL Server")
    mssql_trusted  = models.BooleanField(default=True, help_text="True = Windows Authentication")
    mssql_username = models.CharField(max_length=100, blank=True)
    mssql_password = models.CharField(max_length=255, blank=True)  # Encrypted in production

    # FIRS credentials for this client
    firs_api_key    = models.CharField(max_length=255, blank=True)
    firs_secret_key = models.CharField(max_length=255, blank=True)

    # Sage 200 API credentials (for IRN write-back)
    sage_client_id     = models.CharField(max_length=255, blank=True)
    sage_client_secret = models.CharField(max_length=255, blank=True)
    sage_site_id       = models.CharField(max_length=100, blank=True)
    sage_company_id    = models.CharField(max_length=100, blank=True)
    sage_api_base_url  = models.URLField(blank=True)

    # Token storage (refreshed automatically by Celery)
    sage_access_token   = models.TextField(blank=True)
    sage_refresh_token  = models.TextField(blank=True)
    sage_token_expires  = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Client Company"
        verbose_name_plural = "Client Companies"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.tin})"

    @property
    def odbc_connection_string(self):
        """Build ODBC connection string for Sage 200 Evolution database."""
        db_name = self.mssql_database.strip()
        server  = self.mssql_server.strip()
        driver  = self.mssql_driver.strip()

        if self.mssql_trusted:
            return (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={db_name};"
                f"Trusted_Connection=yes;"
                f"Connection Timeout=10;"
            )
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={server};"
            f"DATABASE={db_name};"
            f"UID={self.mssql_username};"
            f"PWD={self.mssql_password};"
            f"Connection Timeout=10;"
        )
