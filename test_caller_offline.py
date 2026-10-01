"""
test_caller_offline.py — real tests for caller.py, using unittest.mock
to stand in for the actual HTTPS call to Twilio (this sandbox has no
internet/API key). Proves it never fabricates a "called" result without
real credentials, and correctly surfaces a real call.
"""

import json
import os
from unittest.mock import patch, MagicMock

from caller import place_call, CallError


def test_refuses_without_account_sid_or_auth_token():
    saved = {k: os.environ.pop(k, None) for k in
             ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")}
    try:
        place_call("+15551234567", "Hello")
    except CallError as e:
        assert "TWILIO_ACCOUNT_SID" in str(e)
        print("PASS: place_call refuses to fabricate a call without real credentials")
        return
    finally:
        for k, v in saved.items():
            if v:
                os.environ[k] = v
    raise AssertionError("expected CallError with no Twilio credentials")


def test_refuses_without_from_number():
    os.environ["TWILIO_ACCOUNT_SID"] = "ACfake"
    os.environ["TWILIO_AUTH_TOKEN"] = "fake_token"
    saved_from = os.environ.pop("TWILIO_FROM_NUMBER", None)
    try:
        place_call("+15551234567", "Hello")
    except CallError as e:
        assert "TWILIO_FROM_NUMBER" in str(e)
        print("PASS: place_call refuses to call with no configured from-number")
        return
    finally:
        del os.environ["TWILIO_ACCOUNT_SID"]
        del os.environ["TWILIO_AUTH_TOKEN"]
        if saved_from:
            os.environ["TWILIO_FROM_NUMBER"] = saved_from
    raise AssertionError("expected CallError with no TWILIO_FROM_NUMBER")


def test_success_path_returns_real_response():
    os.environ["TWILIO_ACCOUNT_SID"] = "ACfake"
    os.environ["TWILIO_AUTH_TOKEN"] = "fake_token"
    os.environ["TWILIO_FROM_NUMBER"] = "+15559876543"
    try:
        body = json.dumps({"sid": "CAabc123", "status": "queued"}).encode()
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        with patch("urllib.request.urlopen", return_value=cm) as mock_urlopen:
            result = place_call("+15551234567", "This is a test message with an & in it")
        assert result == {"sid": "CAabc123", "status": "queued"}

        sent_req = mock_urlopen.call_args[0][0]
        import urllib.parse
        sent_body = dict(urllib.parse.parse_qsl(sent_req.data.decode("utf-8")))
        assert sent_body["To"] == "+15551234567"
        assert sent_body["From"] == "+15559876543"
        assert "<Say voice=\"alice\">" in sent_body["Twiml"]
        assert "&amp;" in sent_body["Twiml"], "must XML-escape the spoken message"

        sent_headers = {k.lower(): v for k, v in sent_req.headers.items()}
        assert "authorization" in sent_headers
        assert sent_headers["authorization"].startswith("Basic ")
        print("PASS: place_call sends the real To/From/Twiml and returns Twilio's response")
        print("PASS: place_call XML-escapes the spoken message and authenticates with Basic auth")
    finally:
        del os.environ["TWILIO_ACCOUNT_SID"]
        del os.environ["TWILIO_AUTH_TOKEN"]
        del os.environ["TWILIO_FROM_NUMBER"]


def test_http_error_surfaces_as_call_error_not_silent_success():
    import io
    import urllib.error
    os.environ["TWILIO_ACCOUNT_SID"] = "ACfake"
    os.environ["TWILIO_AUTH_TOKEN"] = "fake_token"
    os.environ["TWILIO_FROM_NUMBER"] = "+15559876543"
    try:
        fp = io.BytesIO(b'{"message":"not a valid phone number"}')
        error = urllib.error.HTTPError("url", 400, "Bad Request", {}, fp)
        with patch("urllib.request.urlopen", side_effect=error):
            try:
                place_call("not-a-number", "Hello")
            except CallError as e:
                assert "400" in str(e)
                print("PASS: a real Twilio API error surfaces as CallError, never a silent success")
                return
        raise AssertionError("expected CallError")
    finally:
        del os.environ["TWILIO_ACCOUNT_SID"]
        del os.environ["TWILIO_AUTH_TOKEN"]
        del os.environ["TWILIO_FROM_NUMBER"]


if __name__ == "__main__":
    test_refuses_without_account_sid_or_auth_token()
    test_refuses_without_from_number()
    test_success_path_returns_real_response()
    test_http_error_surfaces_as_call_error_not_silent_success()
    print("\nAll caller.py offline tests passed.")
