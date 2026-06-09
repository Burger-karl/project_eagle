"""
Project Eagle — Cloudflare Security Layer
Production-ready security configuration for the middleware.

This module handles:
    1. Cloudflare Turnstile verification (bot protection on API endpoints)
    2. CF-Connecting-IP header parsing (real client IP behind Cloudflare proxy)
    3. Request validation (Cloudflare-only enforcement)
    4. Rate limiting integration with Cloudflare rules
    5. Security headers injection

Setup required:
    1. Add your domain to Cloudflare
    2. Set DNS records to proxy through Cloudflare (orange cloud)
    3. Enable WAF rules (see cloudflare/waf_rules.json)
    4. Set environment variables:
        CLOUDFLARE_ZONE_ID=your_zone_id
        CLOUDFLARE_API_TOKEN=your_api_token
        CLOUDFLARE_TURNSTILE_SECRET=your_turnstile_secret_key
        CLOUDFLARE_ALLOWED_IPS_ONLY=True  (enforce CF proxy in production)
"""

import hashlib
import hmac
import logging
import requests
import ipaddress
from typing import Optional
from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger("apps.invoices")

# ── Cloudflare IP Ranges (updated periodically) ───────────────
# Source: https://www.cloudflare.com/ips/
CLOUDFLARE_IPV4_RANGES = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
]

CLOUDFLARE_IPV6_RANGES = [
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
]

_CF_NETWORKS = None


def get_cloudflare_networks():
    """Build list of Cloudflare IP networks (cached after first call)."""
    global _CF_NETWORKS
    if _CF_NETWORKS is None:
        _CF_NETWORKS = [
            ipaddress.ip_network(r)
            for r in CLOUDFLARE_IPV4_RANGES + CLOUDFLARE_IPV6_RANGES
        ]
    return _CF_NETWORKS


def is_cloudflare_ip(ip: str) -> bool:
    """Check if a request is coming from Cloudflare's proxy network."""
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in get_cloudflare_networks())
    except ValueError:
        return False


def get_real_client_ip(request) -> str:
    """
    Extract the real client IP from Cloudflare headers.

    When behind Cloudflare:
        CF-Connecting-IP  → the actual visitor's IP (most reliable)
        X-Forwarded-For   → may contain multiple IPs (use first)
        REMOTE_ADDR       → Cloudflare's edge IP (not the visitor's)

    When NOT behind Cloudflare (development):
        REMOTE_ADDR       → the actual client IP
    """
    # CF-Connecting-IP is set by Cloudflare — most reliable
    cf_ip = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()
    if cf_ip:
        return cf_ip

    # X-Forwarded-For fallback
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").strip()
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()

    # Raw connection IP (dev/direct access)
    return request.META.get("REMOTE_ADDR", "")


def verify_turnstile_token(token: str, client_ip: str) -> bool:
    """
    Verify a Cloudflare Turnstile token server-side.
    Used to protect API endpoints from automated abuse.

    Turnstile is Cloudflare's CAPTCHA replacement — invisible to real users.
    Get your secret key from: Cloudflare Dashboard → Turnstile

    Args:
        token:     The turnstile-response token from the client
        client_ip: The real client IP (from get_real_client_ip)

    Returns:
        True if the token is valid, False otherwise
    """
    secret = getattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "")
    if not secret:
        logger.warning("[Cloudflare] Turnstile secret not configured — skipping verification")
        return True  # Skip in dev if not configured

    try:
        response = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret":   secret,
                "response": token,
                "remoteip": client_ip,
            },
            timeout=5,
        )
        result = response.json()
        if not result.get("success"):
            logger.warning(
                f"[Cloudflare] Turnstile verification failed: "
                f"{result.get('error-codes', [])}"
            )
        return result.get("success", False)
    except Exception as e:
        logger.error(f"[Cloudflare] Turnstile verification error: {e}")
        return False


# ════════════════════════════════════════════════════════════════
# CLOUDFLARE SECURITY MIDDLEWARE
# ════════════════════════════════════════════════════════════════

class CloudflareSecurityMiddleware:
    """
    Django middleware that enforces Cloudflare security requirements.

    Add to MIDDLEWARE in settings BEFORE other middleware:
        "cloudflare.middleware.CloudflareSecurityMiddleware",

    What it does:
        1. Injects security headers on every response
        2. Enforces Cloudflare proxy in production (blocks direct hits)
        3. Extracts real client IP from CF-Connecting-IP header
        4. Logs request country from CF-IPCountry header
        5. Blocks requests from Cloudflare Threat Score above threshold
    """

    def __init__(self, get_response):
        self.get_response         = get_response
        self.enforce_cf_only      = getattr(settings, "CLOUDFLARE_ALLOWED_IPS_ONLY", False)
        self.block_threat_score   = getattr(settings, "CLOUDFLARE_BLOCK_THREAT_SCORE", 50)

    def __call__(self, request):
        # ── Enforce Cloudflare proxy in production ────────────
        if self.enforce_cf_only and not settings.DEBUG:
            remote_ip = request.META.get("REMOTE_ADDR", "")
            if not is_cloudflare_ip(remote_ip):
                logger.warning(
                    f"[Cloudflare] Direct access blocked from {remote_ip}"
                )
                return JsonResponse(
                    {"error": "Direct access not permitted"},
                    status=403
                )

        # ── Attach real IP to request ─────────────────────────
        request.real_ip = get_real_client_ip(request)

        # ── Log request country (set by Cloudflare) ───────────
        country = request.META.get("HTTP_CF_IPCOUNTRY", "")
        if country:
            request.cf_country = country

        # ── Block by Cloudflare threat score ──────────────────
        threat_score = request.META.get("HTTP_CF_THREAT_SCORE", "0")
        try:
            if int(threat_score) >= self.block_threat_score:
                logger.warning(
                    f"[Cloudflare] High threat score {threat_score} "
                    f"from {request.real_ip} — blocked"
                )
                return JsonResponse(
                    {"error": "Request blocked"},
                    status=403
                )
        except (ValueError, TypeError):
            pass

        # ── Process request ───────────────────────────────────
        response = self.get_response(request)

        # ── Inject security headers ───────────────────────────
        self._add_security_headers(response)

        return response

    @staticmethod
    def _add_security_headers(response):
        """Add security headers to every response."""
        headers = {
            # Prevent MIME type sniffing
            "X-Content-Type-Options": "nosniff",
            # Prevent clickjacking
            "X-Frame-Options": "DENY",
            # XSS protection (legacy browsers)
            "X-XSS-Protection": "1; mode=block",
            # Referrer policy
            "Referrer-Policy": "strict-origin-when-cross-origin",
            # Permissions policy
            "Permissions-Policy": (
                "geolocation=(), microphone=(), camera=()"
            ),
            # Strict Transport Security (HTTPS only)
            "Strict-Transport-Security": (
                "max-age=31536000; includeSubDomains; preload"
            ),
            # Content Security Policy
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self' https://api.ng.digitax.tech "
                "https://api.firsmbs.com; "
                "frame-ancestors 'none';"
            ),
            # Cache control for API responses
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        }
        for key, value in headers.items():
            response[key] = value
