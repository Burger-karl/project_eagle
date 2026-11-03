"""
APP provider registry.

Resolution order for which provider a company uses:
    1. ClientCompany.app_provider            (per-client override)
    2. settings.APP_PROVIDER                 (global default, from .env)

Resolution order for credentials:
    1. ClientCompany.firs_api_key / firs_secret_key   (per-client)
    2. Provider-specific settings (DIGITAX_API_KEY, INTERSWITCH_CLIENT_ID, ...)
    3. Legacy FIRS_API_KEY / FIRS_SECRET_KEY

Adding a new APP:
    1. Create apps/firs/providers/<name>.py with a subclass of AccessPointProvider
    2. Decorate it with @register_provider("<name>")
    3. Import the module in apps/firs/providers/__init__.py
    4. Add its settings block in config/settings/base.py
    5. Set APP_PROVIDER=<name> (global) or ClientCompany.app_provider=<name>
"""

from __future__ import annotations

from typing import Dict, Optional, Type

from django.conf import settings

from apps.firs.providers.base import AccessPointProvider, ProviderConfigError

_REGISTRY: Dict[str, Type[AccessPointProvider]] = {}


def register_provider(name: str):
    def decorator(cls: Type[AccessPointProvider]):
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return decorator


def available_providers():
    return sorted(_REGISTRY)


def resolve_provider_name(company=None, name: Optional[str] = None) -> str:
    chosen = (
        name
        or (getattr(company, "app_provider", "") if company is not None else "")
        or getattr(settings, "APP_PROVIDER", "digitax")
    )
    return str(chosen).strip().lower()


def get_provider(company=None, name: Optional[str] = None) -> AccessPointProvider:
    """Return a configured adapter for the given company (or the global default)."""
    provider_name = resolve_provider_name(company, name)
    cls = _REGISTRY.get(provider_name)
    if cls is None:
        raise ProviderConfigError(
            f"Unknown APP provider '{provider_name}'. Registered: {available_providers()}"
        )
    return cls.from_settings(company)
