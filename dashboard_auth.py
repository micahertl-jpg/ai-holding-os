"""
dashboard_auth.py — HTTP Basic Auth check for the internal (owner-only)
dashboard and its JSON API. Kept in its own stdlib-only module, with
zero FastAPI dependency, so it's directly testable offline — same
pattern as stripe_client.py/emailer.py/llm_client.py in this codebase.

Why this exists: /dashboard and every internal endpoint (/businesses,
/agents, /tasks, /approvals, /banker/...) had ZERO authentication until
this was added. Anyone who found the Railway URL could see and control
the entire internal system — read every business's data, pause/retire
agents, approve/reject pending approvals, allocate ARC. That gap became
more consequential once the storefront started handling real customer
payments and emails.

The public storefront (/store/*, and the specific static store pages)
and /health are deliberately NOT covered by this — they're meant to be
reachable by anyone (customers, uptime monitors). /store/webhook has
its own, stronger protection: Stripe's HMAC signature verification in
stripe_client.py, which HTTP Basic Auth can't replace anyway (Stripe
doesn't send basic-auth credentials).

Fails CLOSED, not open: if DASHBOARD_USERNAME/DASHBOARD_PASSWORD aren't
configured, every protected route refuses access (503) rather than
silently staying open. Consistent with how this codebase treats every
other missing credential (ANTHROPIC_API_KEY, STRIPE_SECRET_KEY, etc.).

This module only checks whether a given set of credentials is correct
-- it has no concept of a client IP or a request, on purpose, to stay
stdlib-only and directly testable. Rate-limiting failed login attempts
(so this one static password can't be brute-forced) is api.py's
require_dashboard_auth() middleware's job, using rate_limiter.py.
"""

import base64
import os
import secrets

# Anything else (/dashboard, /static/dashboard.*, /businesses, /agents,
# /tasks, /approvals, /banker/..., /scheduled-jobs, ...) requires auth.
# "/" itself is the public storefront's landing page (see api.py's root
# route) -- the only customer-facing surface in the whole system, so it
# gets the clean root URL rather than living under /static/store.html.
PUBLIC_PATH_PREFIXES = ("/health", "/store", "/robots.txt", "/sitemap.xml", "/")
PUBLIC_STATIC_PATHS = {
    "/static/store.html", "/static/store.css", "/static/store.js",
    "/static/store-success.html", "/static/store-success.js",
    "/static/store-legal.html", "/static/favicon.svg",
}


def is_public_path(path: str) -> bool:
    """True for anything that should be reachable without credentials:
    the public storefront, its static assets, and /health."""
    if path in PUBLIC_STATIC_PATHS:
        return True
    for prefix in PUBLIC_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def credentials_configured() -> bool:
    return bool(os.environ.get("DASHBOARD_USERNAME")) and bool(os.environ.get("DASHBOARD_PASSWORD"))


def parse_basic_auth_header(auth_header):
    """Returns (username, password) or None if the header is missing,
    not a Basic-auth header, or malformed. Never raises — a broken
    header should fail the auth check, not crash the request."""
    if not auth_header or not auth_header.startswith("Basic "):
        return None
    encoded = auth_header[len("Basic "):]
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def check_credentials(auth_header, expected_username=None, expected_password=None) -> bool:
    """True only if auth_header supplies exactly the configured
    username AND password, compared with secrets.compare_digest
    (constant-time, to avoid leaking how much of the password matched
    via response-timing). Reads DASHBOARD_USERNAME/DASHBOARD_PASSWORD
    from the environment unless expected_* are passed explicitly
    (tests pass them explicitly for determinism, never touching the
    real environment)."""
    if expected_username is None:
        expected_username = os.environ.get("DASHBOARD_USERNAME")
    if expected_password is None:
        expected_password = os.environ.get("DASHBOARD_PASSWORD")
    if not expected_username or not expected_password:
        return False

    parsed = parse_basic_auth_header(auth_header)
    if not parsed:
        return False
    supplied_username, supplied_password = parsed

    return (secrets.compare_digest(supplied_username, expected_username) and
            secrets.compare_digest(supplied_password, expected_password))
