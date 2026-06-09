"""
Production Settings Update for Project Eagle
Add these settings to config/settings/production.py

This file contains:
    1. Cloudflare middleware configuration
    2. Production security settings
    3. Rate limiting configuration
    4. Environment variables to add to .env
"""

# ════════════════════════════════════════════════════════════════
# ADD TO: config/settings/production.py
# ════════════════════════════════════════════════════════════════

import os

# ── Cloudflare Settings ───────────────────────────────────────
CLOUDFLARE_ZONE_ID            = os.getenv("CLOUDFLARE_ZONE_ID", "")
CLOUDFLARE_API_TOKEN          = os.getenv("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_TURNSTILE_SECRET   = os.getenv("CLOUDFLARE_TURNSTILE_SECRET", "")
CLOUDFLARE_TURNSTILE_SITE_KEY = os.getenv("CLOUDFLARE_TURNSTILE_SITE_KEY", "")

# Set to True in production to block non-Cloudflare direct access
CLOUDFLARE_ALLOWED_IPS_ONLY   = os.getenv("CLOUDFLARE_ALLOWED_IPS_ONLY", "False") == "True"

# Block requests with Cloudflare threat score above this value (0-100)
CLOUDFLARE_BLOCK_THREAT_SCORE = int(os.getenv("CLOUDFLARE_BLOCK_THREAT_SCORE", "50"))

# ── MIDDLEWARE ORDER (update your existing MIDDLEWARE list) ────
# Add CloudflareSecurityMiddleware as the FIRST middleware
# Your MIDDLEWARE list should look like this:
MIDDLEWARE = [
    "cloudflare.middleware.CloudflareSecurityMiddleware",   # ← ADD FIRST
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "middleware.request_logger.RequestLoggerMiddleware",
]

# ── Security Settings (production only) ──────────────────────
DEBUG                            = False
SECURE_SSL_REDIRECT              = True
SECURE_PROXY_SSL_HEADER          = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS              = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS   = True
SECURE_HSTS_PRELOAD              = True
SECURE_CONTENT_TYPE_NOSNIFF      = True
SECURE_BROWSER_XSS_FILTER        = True
X_FRAME_OPTIONS                  = "DENY"
SESSION_COOKIE_SECURE            = True
SESSION_COOKIE_HTTPONLY          = True
SESSION_COOKIE_SAMESITE          = "Strict"
CSRF_COOKIE_SECURE               = True
CSRF_COOKIE_HTTPONLY             = True
CSRF_COOKIE_SAMESITE             = "Strict"
CSRF_TRUSTED_ORIGINS             = [
    "https://yourdomain.com",
    "https://api.yourdomain.com",
]

# Trust Cloudflare proxy headers
USE_X_FORWARDED_HOST    = True
USE_X_FORWARDED_PORT    = True

# ── CORS — restrict to your frontend domains in production ────
CORS_ALLOWED_ORIGINS = [
    "https://yourdomain.com",
    "https://app.yourdomain.com",
]
CORS_ALLOW_CREDENTIALS = True

# ── Rate limiting (django-ratelimit) ─────────────────────────
# Used in addition to Cloudflare rate limiting for defence-in-depth
RATELIMIT_USE_CACHE = "default"

# ── Caching (Redis) ───────────────────────────────────────────
CACHES = {
    "default": {
        "BACKEND":  "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "TIMEOUT": 300,
    }
}

# ── Allowed Hosts (add your production domain) ────────────────
ALLOWED_HOSTS = [
    "yourdomain.com",
    "www.yourdomain.com",
    "api.yourdomain.com",
]

# ════════════════════════════════════════════════════════════════
# ADD THESE TO YOUR .env FILE FOR PRODUCTION
# ════════════════════════════════════════════════════════════════
"""
# Cloudflare
CLOUDFLARE_ZONE_ID=your_zone_id_from_cloudflare_dashboard
CLOUDFLARE_API_TOKEN=your_api_token_with_zone_edit_permissions
CLOUDFLARE_TURNSTILE_SECRET=your_turnstile_secret_key
CLOUDFLARE_TURNSTILE_SITE_KEY=your_turnstile_site_key
CLOUDFLARE_ALLOWED_IPS_ONLY=True
CLOUDFLARE_BLOCK_THREAT_SCORE=50

# Security
DJANGO_ENV=production
DEBUG=False
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com,api.yourdomain.com
SECRET_KEY=generate-with-python-secrets-token-hex-50
"""
