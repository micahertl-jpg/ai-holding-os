"""
test_stripe_client_offline.py — real tests for stripe_client.py. This
is the single most safety-critical module added for the storefront —
if webhook signature verification has a bug, an attacker could forge
"payment succeeded" events. These tests construct real HMAC-SHA256
signatures the same way Stripe does, then verify the module accepts a
genuine one and rejects every way it could be tampered with.
"""

import hashlib
import hmac
import json
import time
from unittest.mock import patch, MagicMock

from stripe_client import (
    create_checkout_session, verify_webhook_signature,
    StripeError, WebhookVerificationError,
)

WEBHOOK_SECRET = "whsec_test_secret_12345"


def _sign(payload: bytes, secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def test_valid_signature_is_accepted_and_event_parsed():
    payload = json.dumps({"id": "evt_1", "type": "checkout.session.completed",
                           "data": {"object": {"metadata": {"order_id": "ord_1"}}}}).encode()
    now = time.time()
    sig_header = _sign(payload, WEBHOOK_SECRET, int(now))
    event = verify_webhook_signature(payload, sig_header, webhook_secret=WEBHOOK_SECRET, now=now)
    assert event["id"] == "evt_1"
    assert event["type"] == "checkout.session.completed"
    print("PASS: a genuinely-signed webhook is accepted and correctly parsed")


def test_tampered_payload_is_rejected():
    """The exact attack this exists to stop: sign one payload, then
    submit a DIFFERENT payload with that signature (e.g. an attacker
    changing the amount or order_id after seeing a real signed event)."""
    original_payload = json.dumps({"id": "evt_1", "amount_total": 1900}).encode()
    now = time.time()
    sig_header = _sign(original_payload, WEBHOOK_SECRET, int(now))

    tampered_payload = json.dumps({"id": "evt_1", "amount_total": 99999}).encode()
    try:
        verify_webhook_signature(tampered_payload, sig_header, webhook_secret=WEBHOOK_SECRET, now=now)
    except WebhookVerificationError as e:
        assert "mismatch" in str(e).lower()
        print("PASS: a tampered payload with a mismatched signature is rejected")
        return
    raise AssertionError("expected WebhookVerificationError for tampered payload")


def test_wrong_secret_is_rejected():
    payload = json.dumps({"id": "evt_1"}).encode()
    now = time.time()
    sig_header = _sign(payload, "whsec_wrong_secret", int(now))
    try:
        verify_webhook_signature(payload, sig_header, webhook_secret=WEBHOOK_SECRET, now=now)
    except WebhookVerificationError as e:
        assert "mismatch" in str(e).lower()
        print("PASS: a signature made with the wrong secret is rejected")
        return
    raise AssertionError("expected WebhookVerificationError for wrong secret")


def test_expired_timestamp_is_rejected():
    payload = json.dumps({"id": "evt_1"}).encode()
    old_timestamp = int(time.time()) - 10000  # way outside the 300s tolerance
    sig_header = _sign(payload, WEBHOOK_SECRET, old_timestamp)
    try:
        verify_webhook_signature(payload, sig_header, webhook_secret=WEBHOOK_SECRET)
    except WebhookVerificationError as e:
        assert "tolerance" in str(e).lower() or "replay" in str(e).lower()
        print("PASS: an old/expired timestamp is rejected as a possible replay")
        return
    raise AssertionError("expected WebhookVerificationError for expired timestamp")


def test_missing_header_is_rejected():
    try:
        verify_webhook_signature(b"{}", None, webhook_secret=WEBHOOK_SECRET)
    except WebhookVerificationError as e:
        assert "missing" in str(e).lower()
        print("PASS: a missing Stripe-Signature header is rejected")
        return
    raise AssertionError("expected WebhookVerificationError for missing header")


def test_malformed_header_is_rejected():
    try:
        verify_webhook_signature(b"{}", "not-a-valid-header", webhook_secret=WEBHOOK_SECRET)
    except WebhookVerificationError as e:
        assert "malformed" in str(e).lower()
        print("PASS: a malformed Stripe-Signature header is rejected")
        return
    raise AssertionError("expected WebhookVerificationError for malformed header")


def test_missing_webhook_secret_is_rejected():
    payload = json.dumps({"id": "evt_1"}).encode()
    sig_header = _sign(payload, WEBHOOK_SECRET, int(time.time()))
    try:
        verify_webhook_signature(payload, sig_header, webhook_secret=None)
    except WebhookVerificationError as e:
        assert "STRIPE_WEBHOOK_SECRET" in str(e)
        print("PASS: no configured webhook secret means every webhook is rejected, "
              "never silently trusted")
        return
    raise AssertionError("expected WebhookVerificationError for no configured secret")


def test_create_checkout_session_refuses_without_api_key():
    import os
    saved = os.environ.pop("STRIPE_SECRET_KEY", None)
    try:
        create_checkout_session("Test Product", 1900, "usd", "a@example.com",
                                 "https://x.test/success", "https://x.test/cancel", {})
    except StripeError as e:
        assert "STRIPE_SECRET_KEY" in str(e)
        print("PASS: create_checkout_session refuses to fabricate a checkout without a real API key")
        return
    finally:
        if saved:
            os.environ["STRIPE_SECRET_KEY"] = saved
    raise AssertionError("expected StripeError with no STRIPE_SECRET_KEY")


def test_create_checkout_session_success_path():
    import os
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
    try:
        body = json.dumps({"id": "cs_test_123", "url": "https://checkout.stripe.com/pay/cs_test_123"}).encode()
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        with patch("urllib.request.urlopen", return_value=cm):
            result = create_checkout_session(
                "Test Product", 1900, "usd", "a@example.com",
                "https://x.test/success", "https://x.test/cancel",
                {"order_id": "ord_abc"},
            )
        assert result == {"id": "cs_test_123", "url": "https://checkout.stripe.com/pay/cs_test_123"}
        print("PASS: create_checkout_session returns the real session id/url on success")
    finally:
        del os.environ["STRIPE_SECRET_KEY"]


if __name__ == "__main__":
    test_valid_signature_is_accepted_and_event_parsed()
    test_tampered_payload_is_rejected()
    test_wrong_secret_is_rejected()
    test_expired_timestamp_is_rejected()
    test_missing_header_is_rejected()
    test_malformed_header_is_rejected()
    test_missing_webhook_secret_is_rejected()
    test_create_checkout_session_refuses_without_api_key()
    test_create_checkout_session_success_path()
    print("\nAll stripe_client.py offline tests passed.")
