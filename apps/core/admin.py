"""
Django Admin — Core models
Register ClientCompany in the admin panel for easy management.
"""
from django.contrib import admin
from apps.core.models import ClientCompany


@admin.register(ClientCompany)
class ClientCompanyAdmin(admin.ModelAdmin):
    list_display  = ["name", "tin", "mssql_server", "mssql_database", "is_active"]
    list_filter   = ["is_active"]
    search_fields = ["name", "tin", "mssql_server"]
    readonly_fields = ["id", "created_at", "updated_at", "sage_access_token", "sage_token_expires"]
    fieldsets = (
        ("Company Info", {
            "fields": ("id", "name", "tin", "address", "email", "phone", "is_active")
        }),
        ("Sage 200 MSSQL (Client .bak Database)", {
            "fields": ("mssql_server", "mssql_database", "mssql_driver", "mssql_trusted",
                       "mssql_username", "mssql_password")
        }),
        ("Sage 200 REST API (for IRN write-back)", {
            "classes": ("collapse",),
            "fields": ("sage_client_id", "sage_client_secret", "sage_site_id",
                       "sage_company_id", "sage_api_base_url",
                       "sage_access_token", "sage_refresh_token", "sage_token_expires")
        }),
        ("FIRS MBS Credentials", {
            "classes": ("collapse",),
            "fields": ("firs_api_key", "firs_secret_key")
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at")
        }),
    )
    fieldsets = (
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
    )
