"""
stripe_client.py — the ONLY place real-world money touches this
codebase. A minimal, real Stripe integration using only the Python
standard library (urllib, hmac, hashlib), matching the same
zero-pip-dependency philosophy as llm_client.py.

Two things live here:
  - create_checkout_session(): makes a real HTTPS call to Stripe to
    start a real payment. Requires STRIPE_SECRET_KEY. Refuses to
    proceed without it — never fabricates a fake checkout URL.
  - verify_webhook_signature(): cryptographically verifies that a
    webhook request actually came from Stripe (HMAC-SHA256 over
    "timestamp.payload" using STRIPE_WEBHOOK_SECRET) before ANYTHING
    in this system treats a payment as real. This is the single most
    important safety check in the whole store pipeline — without it,
    anyone who finds the webhook URL could POST a fake "payment
    succeeded" event and get a free report (or worse, a false
    real_transactions ledger entry).

Per the project spec Sec 7 ("the system should never invent revenue...
it should never claim money was earned unless there is verifiable
financial evidence"): nothing in api.py or fulfillment.py should ever
write a real_transactions row, mark an order 'paid', or treat a
customer as having paid, except in direct response to a webhook that
passed verify_webhook_signature(). This module is what makes that
verification real rather than a comment.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

STRIPE_API_BASE = "https://api.stripe.com/v1"

# Stripe recommends rejecting a webhook whose timestamp is further than
# this from "now" — protects against a captured/replayed request being
# re-sent later. 5 minutes, same as Stripe's own client libraries.
WEBHOOK_TOLERANCE_SECONDS = 300


class StripeError(Exception):
    pass


class WebhookVerificationError(Exception):
    pass


def _get_secret_key() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise StripeError(
            "No STRIPE_SECRET_KEY found. Refusing to create a checkout session — "
            "this system never fabricates a payment flow."
        )
    return key


def create_checkout_session(product_name: str, unit_amount_cents: int, currency: str,
                             customer_email: str, success_url: str, cancel_url: str,
                             metadata: dict) -> dict:
    """Creates a real Stripe Checkout Session (hosted payment page).
    Returns {"id": ..., "url": ...} — redirect the customer to `url`.
    Raises StripeError on any failure, including a missing API key;
    there is no mock mode for this call because there is nothing
    meaningful to mock — either a real checkout exists or it doesn't."""
    if unit_amount_cents <= 0:
        raise ValueError("unit_amount_cents must be positive")
    secret_key = _get_secret_key()

    form = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "customer_email": customer_email,
        "payment_method_types[0]": "card",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency,
        "line_items[0][price_data][unit_amount]": str(unit_amount_cents),
        "line_items[0][price_data][product_data][name]": product_name,
    }
    for key, value in (metadata or {}).items():
        form[f"metadata[{key}]"] = str(value)

    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        f"{STRIPE_API_BASE}/checkout/sessions",
        data=body,
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "authorization": "Basic " + _basic_auth(secret_key),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise StripeError(f"Stripe API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise StripeError(f"Network error calling Stripe API: {e}") from e

    if "id" not in data or "url" not in data:
        raise StripeError(f"Unexpected Stripe response, missing id/url: {data}")
    return {"id": data["id"], "url": data["url"]}


def _basic_auth(secret_key: str) -> str:
    import base64
    return base64.b64encode(f"{secret_key}:".encode("utf-8")).decode("ascii")


def verify_webhook_signature(payload: bytes, sig_header: str, webhook_secret: str = None,
                              now: float = None) -> dict:
    """Verifies a Stripe webhook request is authentic and returns the
    parsed event dict. Raises WebhookVerificationError on ANY failure —
    missing secret, malformed header, signature mismatch, or a
    timestamp too old/skewed. Callers (api.py) MUST NOT act on the
    event unless this returns successfully.

    `payload` is the raw request body bytes, exactly as received —
    verifying against a re-serialized/re-parsed version would not
    match Stripe's own signature and defeats the point.
    `now` is injectable for deterministic tests; defaults to time.time()."""
    webhook_secret = webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise WebhookVerificationError(
            "No STRIPE_WEBHOOK_SECRET configured — refusing to trust any webhook "
            "without it, since that's the only thing proving a request actually "
            "came from Stripe and not an attacker."
        )
    if not sig_header:
        raise WebhookVerificationError("Missing Stripe-Signature header")

    parts = dict(
        item.split("=", 1) for item in sig_header.split(",") if "=" in item
    )
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        raise WebhookVerificationError(
            f"Malformed Stripe-Signature header (missing t= or v1=): {sig_header!r}"
        )

    now = time.time() if now is None else now
    try:
        ts_int = int(timestamp)
    except ValueError:
        raise WebhookVerificationError(f"Non-numeric timestamp in signature: {timestamp!r}")
    if abs(now - ts_int) > WEBHOOK_TOLERANCE_SECONDS:
        raise WebhookVerificationError(
            f"Webhook timestamp {ts_int} is outside the {WEBHOOK_TOLERANCE_SECONDS}s "
            f"tolerance window (now={now:.0f}) — possible replay, rejecting"
        )

    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected_sig = hmac.new(webhook_secret.encode("utf-8"), signed_payload,
                             hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, signature):
        raise WebhookVerificationError(
            "Signature mismatch — this request did not come from Stripe (or the "
            "webhook secret is wrong). Refusing to treat it as a real payment event."
        )

    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise WebhookVerificationError(f"Signature verified but body isn't valid JSON: {e}") from e
