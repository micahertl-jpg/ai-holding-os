"""
emailer.py — sends the finished report to a paying customer, via
Resend's HTTP API (https://resend.com), using only the Python standard
library (urllib), same pattern as llm_client.py and stripe_client.py.

This is customer-facing, real-world delivery — a paying customer is
waiting on this email. Like the other real-integration modules in this
codebase, it never fabricates success: no RESEND_API_KEY means a hard
failure, not a silently-skipped send. fulfillment.py relies on that —
an order is only ever marked 'fulfilled' after send_email() actually
returns success.
"""

import json
import os
import urllib.request
import urllib.error

RESEND_API_URL = "https://api.resend.com/emails"


class EmailError(Exception):
    pass


def send_email(to_email: str, subject: str, html_body: str, text_body: str = None) -> dict:
    """Sends one real email via Resend. Raises EmailError on any
    failure (missing API key/from-address config, network error, or a
    non-2xx response from Resend) — callers must not treat a raised
    EmailError as "sent anyway"."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise EmailError(
            "No RESEND_API_KEY found. Refusing to proceed — this system never "
            "pretends an email was sent when it wasn't."
        )
    from_email = os.environ.get("RESEND_FROM_EMAIL")
    if not from_email:
        raise EmailError(
            "No RESEND_FROM_EMAIL configured. Set this to a sender address on a "
            "domain you've verified with Resend (or Resend's own test sender, "
            "e.g. onboarding@resend.dev, while testing) before sending real email."
        )

    body = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        body["text"] = text_body

    req = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise EmailError(f"Resend API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise EmailError(f"Network error calling Resend API: {e}") from e

    if "id" not in data:
        raise EmailError(f"Unexpected Resend response, no message id: {data}")
    return data
