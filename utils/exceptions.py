"""Custom exceptions and DRF exception handler."""

import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response

logger = logging.getLogger("apps.invoices")


def custom_exception_handler(exc, context):
    """
    Custom DRF exception handler.
    Adds error codes and logs all API errors.
    """
    response = exception_handler(exc, context)

    if response is not None:
        view = context.get("view", None)
        logger.error(
            f"API error: {exc}",
            extra={
                "view":   str(view.__class__.__name__) if view else "unknown",
                "status": response.status_code,
            }
        )
        response.data = {
            "error":  True,
            "detail": response.data,
            "status": response.status_code,
        }

    return response


class PipelineError(Exception):
    """Raised when the invoice pipeline encounters a fatal error."""
    pass


class ConfigurationError(Exception):
    """Raised when required credentials or settings are missing."""
    pass
