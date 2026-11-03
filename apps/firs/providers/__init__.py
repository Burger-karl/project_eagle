"""
Access Point Provider adapters.

    from apps.firs.providers import get_provider
    provider = get_provider(company)          # picks company.app_provider or APP_PROVIDER
    result   = provider.submit(mapped)        # SubmissionResult
"""

from apps.firs.providers.base import (  # noqa: F401
    AccessPointProvider, ProviderConfigError, ProviderStatus, SubmissionResult,
)
from apps.firs.providers.registry import (  # noqa: F401
    available_providers, get_provider, register_provider, resolve_provider_name,
)

# Importing the adapter modules registers them.
from apps.firs.providers import digitax, interswitch  # noqa: F401,E402
