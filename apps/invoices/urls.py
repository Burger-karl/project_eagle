"""Invoice app URL patterns."""
from django.urls import path
from apps.invoices import views

urlpatterns = [
    # ── System ────────────────────────────────────────────────
    path("health/",          views.HealthCheckView.as_view(),          name="health"),
    path("stats/",           views.StatsView.as_view(),                name="stats"),
    path("poll/",            views.PollSage200View.as_view(),          name="poll-sage200"),

    # ── Invoice List & Detail ─────────────────────────────────
    path("",                 views.InvoiceListView.as_view(),          name="invoice-list"),
    path("<uuid:pk>/",       views.InvoiceDetailView.as_view(),        name="invoice-detail"),

    # ── Invoice Actions ───────────────────────────────────────
    path("<uuid:pk>/submit/", views.SubmitInvoiceView.as_view(),       name="invoice-submit"),
    path("<uuid:pk>/retry/",  views.RetryInvoiceView.as_view(),        name="invoice-retry"),

    # ── TIN ───────────────────────────────────────────────────
    path("validate-tin/",    views.ValidateTINView.as_view(),          name="validate-tin"),
    path("buyer-tins/",      views.BuyerTINMappingListCreateView.as_view(), name="buyer-tins"),
]
