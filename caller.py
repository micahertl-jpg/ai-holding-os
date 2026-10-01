"""
caller.py — places a real phone call via Twilio's Voice API
(https://www.twilio.com/docs/voice/api/call-resource), using only the
Python standard library (urllib), same pattern as emailer.py,
llm_client.py, and stripe_client.py.

This is the Overseer agent's one real-world side effect: an urgent,
already-pending approval gets escalated from "sits in the dashboard" to
"rings the owner's phone." Like every other real-integration module in
this codebase, it never fabricates success: missing Twilio credentials
is a hard failure, not a silently-skipped call. tasks/overseer_review.py
relies on that -- a call is only ever recorded as placed after
place_call() actually returns a real Twilio call sid.
"""

import base64
import json
import os
import urllib.request
import urllib.parse
import urllib.error

TWILIO_API_URL = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"


class CallError(Exception):
    pass


def _twiml_for_message(message: str) -> str:
    # <Say> has no attribute injection risk from message content since
    # it's plain element text, but XML-escape it anyway -- a message
    # containing "&", "<", or ">" (e.g. from a description field) must
    # not corrupt the TwiML document.
    escaped = (
        message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f'<Response><Say voice="alice">{escaped}</Say></Response>'


def place_call(to_number: str, message: str) -> dict:
    """Places one real voice call via Twilio, which speaks `message`
    aloud via inline TwiML (no hosted webhook needed). Raises CallError
    on any failure (missing credential config, network error, or a
    non-2xx response from Twilio) -- callers must not treat a raised
    CallError as "called anyway". Returns Twilio's call resource dict
    (includes a real "sid") on success."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    if not account_sid or not auth_token:
        raise CallError(
            "No TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN found. Refusing to proceed -- this "
            "system never pretends a call was placed when it wasn't."
        )
    if not from_number:
        raise CallError(
            "No TWILIO_FROM_NUMBER configured. Set this to a Twilio phone number on your "
            "account (or trial number) before placing real calls."
        )

    body = urllib.parse.urlencode({
        "To": to_number,
        "From": from_number,
        "Twiml": _twiml_for_message(message),
    }).encode("utf-8")

    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        TWILIO_API_URL.format(account_sid=account_sid),
        data=body,
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "authorization": f"Basic {credentials}",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise CallError(f"Twilio API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise CallError(f"Network error calling Twilio API: {e}") from e

    if "sid" not in data:
        raise CallError(f"Unexpected Twilio response, no call sid: {data}")
    return data
