"""Request logging middleware — logs every API call."""

import time
import logging

logger = logging.getLogger("apps.invoices")


class RequestLoggerMiddleware:
    """Log every incoming HTTP request with duration."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.time()
        response = self.get_response(request)
        duration_ms = int((time.time() - start) * 1000)

        if request.path.startswith("/api/"):
            logger.info(
                f"{request.method} {request.path} → {response.status_code} ({duration_ms}ms)"
            )

        return response
