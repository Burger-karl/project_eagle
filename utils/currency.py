"""
Currency Utilities
Fetches NGN exchange rates from the Central Bank of Nigeria (CBN)
for foreign currency invoice conversion.
"""

import logging
import requests
from datetime import date
from typing import Optional

logger = logging.getLogger("apps.invoices")

CBN_API_URL = "https://www.cbn.gov.ng/rates/ExchRates.asp"

# Fallback rates (use only when CBN API is unavailable)
FALLBACK_RATES = {
    "USD": 1580.0,
    "GBP": 2000.0,
    "EUR": 1720.0,
    "GHS": 100.0,
    "ZAR": 85.0,
}


def get_ngn_exchange_rate(currency: str, on_date: Optional[date] = None) -> float:
    """
    Get the NGN exchange rate for a foreign currency on a specific date.

    Args:
        currency: ISO 4217 currency code e.g. "USD", "GBP"
        on_date:  Date for the rate. Uses today if None.

    Returns:
        Exchange rate as float (NGN per 1 unit of foreign currency)
    """
    if currency == "NGN":
        return 1.0

    try:
        # CBN API call (simplified — real implementation would parse their XML)
        response = requests.get(CBN_API_URL, timeout=10)
        if response.status_code == 200:
            # Parse CBN response — implementation depends on their current API format
            rate = _parse_cbn_rate(response.text, currency)
            if rate:
                logger.info(f"CBN rate for {currency}/NGN: {rate}")
                return rate
    except Exception as e:
        logger.warning(f"CBN API unavailable: {e}. Using fallback rate for {currency}.")

    # Use fallback rate
    fallback = FALLBACK_RATES.get(currency.upper())
    if fallback:
        logger.warning(f"Using fallback rate for {currency}/NGN: {fallback}")
        return fallback

    raise ValueError(f"No exchange rate available for currency: {currency}")


def _parse_cbn_rate(html: str, currency: str) -> Optional[float]:
    """Parse the CBN response to extract a specific currency rate."""
    # CBN rate parsing implementation — adjust based on actual CBN API response format
    # This is a placeholder — real implementation parses their HTML/XML table
    return None
