"""
ClientCompany Model Update
Add these fields to apps/core/models.py in the ClientCompany class.
They go after the existing Sage 200 fields.

INSTRUCTIONS:
1. Open apps/core/models.py
2. Find the block that starts with:
   "# Sage 200 API credentials (for IRN write-back)"
3. Paste the SAGE 300 FIELDS block directly after the Sage 200 block
4. Run: python manage.py makemigrations core
5. Run: python manage.py migrate
"""

# ════════════════════════════════════════════════════════════════
# PASTE THIS INTO ClientCompany class in apps/core/models.py
# Place it after the existing Sage 200 fields block
# ════════════════════════════════════════════════════════════════

SAGE300_FIELDS = """
    # ── Sage 300 MSSQL Connection ─────────────────────────────
    sage300_mssql_server   = models.CharField(
        max_length=255, blank=True,
        help_text="Sage 300 SQL Server instance e.g. localhost or SERVER\\\\INSTANCE"
    )
    sage300_mssql_database = models.CharField(
        max_length=255, blank=True,
        help_text="Sage 300 company database code e.g. SAMLTD"
    )
    sage300_mssql_driver   = models.CharField(
        max_length=100, blank=True,
        default="ODBC Driver 17 for SQL Server"
    )
    sage300_mssql_trusted  = models.BooleanField(
        default=True,
        help_text="True = Windows Authentication (recommended)"
    )
    sage300_mssql_username = models.CharField(max_length=100, blank=True)
    sage300_mssql_password = models.CharField(max_length=255, blank=True)

    # ── Sage 300 Web API (for IRN write-back) ─────────────────
    sage300_api_server    = models.CharField(
        max_length=255, blank=True,
        help_text="Sage 300 Web API server e.g. myserver.local"
    )
    sage300_api_company   = models.CharField(
        max_length=20, blank=True,
        help_text="Sage 300 company code e.g. SAMLTD"
    )
    sage300_api_version   = models.CharField(
        max_length=10, blank=True, default="v1.0"
    )
    sage300_api_username  = models.CharField(
        max_length=100, blank=True, default="ADMIN"
    )
    sage300_api_password  = models.CharField(max_length=255, blank=True)
"""

# ════════════════════════════════════════════════════════════════
# ALSO ADD THIS PROPERTY to ClientCompany (after odbc_connection_string)
# ════════════════════════════════════════════════════════════════

SAGE300_PROPERTY = """
    @property
    def sage300_odbc_connection_string(self):
        db     = self.sage300_mssql_database or self.mssql_database
        server = self.sage300_mssql_server   or self.mssql_server
        driver = self.sage300_mssql_driver   or self.mssql_driver
        trusted = getattr(self, "sage300_mssql_trusted", True)

        if trusted:
            return (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={db};"
                f"Trusted_Connection=yes;"
                f"Connection Timeout=10;"
            )
        username = self.sage300_mssql_username or ""
        password = self.sage300_mssql_password or ""
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={server};"
            f"DATABASE={db};"
            f"UID={username};"
            f"PWD={password};"
            f"Connection Timeout=10;"
        )
"""

# ════════════════════════════════════════════════════════════════
# ADMIN UPDATE — add Sage 300 fieldset to ClientCompanyAdmin
# in apps/core/admin.py, add this inside the fieldsets tuple
# ════════════════════════════════════════════════════════════════

SAGE300_ADMIN_FIELDSET = """
        ("Sage 300 MSSQL (Client .bak Database)", {
            "classes": ("collapse",),
            "fields": (
                "sage300_mssql_server", "sage300_mssql_database",
                "sage300_mssql_driver", "sage300_mssql_trusted",
                "sage300_mssql_username", "sage300_mssql_password"
            )
        }),
        ("Sage 300 Web API (for IRN write-back)", {
            "classes": ("collapse",),
            "fields": (
                "sage300_api_server", "sage300_api_company",
                "sage300_api_version", "sage300_api_username",
                "sage300_api_password"
            )
        }),
"""

# ════════════════════════════════════════════════════════════════
# CELERY BEAT SCHEDULE UPDATE
# Add this to CELERY_BEAT_SCHEDULE in config/settings/base.py
# ════════════════════════════════════════════════════════════════

CELERY_BEAT_ADDITION = """
    # Poll Sage 300 every 5 minutes for new invoices
    "poll-sage300-invoices": {
        "task":     "apps.sage300.tasks.poll_sage300_invoices",
        "schedule": 300,
    },
"""
