"""
test_emailer_offline.py — real tests for emailer.py, using
unittest.mock to stand in for the actual HTTPS call to Resend (this
sandbox has no internet/API key). Proves it never fabricates a "sent"
result without a real API key/from-address, and correctly surfaces a
real send.
"""

import json
import os
from unittest.mock import patch, MagicMock

from emailer import send_email, EmailError


def test_refuses_without_api_key():
    saved_key = os.environ.pop("RESEND_API_KEY", None)
    saved_from = os.environ.pop("RESEND_FROM_EMAIL", None)
    try:
        send_email("customer@example.com", "Subject", "<p>Body</p>")
    except EmailError as e:
        assert "RESEND_API_KEY" in str(e)
        print("PASS: send_email refuses to fabricate a send without a real API key")
        return
    finally:
        if saved_key:
            os.environ["RESEND_API_KEY"] = saved_key
        if saved_from:
            os.environ["RESEND_FROM_EMAIL"] = saved_from
    raise AssertionError("expected EmailError with no RESEND_API_KEY")


def test_refuses_without_from_address():
    os.environ["RESEND_API_KEY"] = "re_fake_key"
    saved_from = os.environ.pop("RESEND_FROM_EMAIL", None)
    try:
        send_email("customer@example.com", "Subject", "<p>Body</p>")
    except EmailError as e:
        assert "RESEND_FROM_EMAIL" in str(e)
        print("PASS: send_email refuses to send with no configured from-address")
        return
    finally:
        del os.environ["RESEND_API_KEY"]
        if saved_from:
            os.environ["RESEND_FROM_EMAIL"] = saved_from
    raise AssertionError("expected EmailError with no RESEND_FROM_EMAIL")


def test_success_path_returns_real_response():
    os.environ["RESEND_API_KEY"] = "re_fake_key"
    os.environ["RESEND_FROM_EMAIL"] = "reports@example.com"
    try:
        body = json.dumps({"id": "email_abc123"}).encode()
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        with patch("urllib.request.urlopen", return_value=cm) as mock_urlopen:
            result = send_email("customer@example.com", "Your Report", "<p>Hi</p>", text_body="Hi")
        assert result == {"id": "email_abc123"}

        # Confirm the actual request sent matches what we asked for —
        # not just that SOME request happened.
        sent_req = mock_urlopen.call_args[0][0]
        sent_body = json.loads(sent_req.data.decode("utf-8"))
        assert sent_body["to"] == ["customer@example.com"]
        assert sent_body["from"] == "reports@example.com"
        assert sent_body["subject"] == "Your Report"
        assert sent_body["html"] == "<p>Hi</p>"
        assert sent_body["text"] == "Hi"
        print("PASS: send_email sends the real recipient/subject/body and returns Resend's response")
    finally:
        del os.environ["RESEND_API_KEY"]
        del os.environ["RESEND_FROM_EMAIL"]


def test_http_error_surfaces_as_email_error_not_silent_success():
    import io
    import urllib.error
    os.environ["RESEND_API_KEY"] = "re_fake_key"
    os.environ["RESEND_FROM_EMAIL"] = "reports@example.com"
    try:
        fp = io.BytesIO(b'{"error":"bad address"}')
        error = urllib.error.HTTPError("url", 422, "Unprocessable", {}, fp)
        with patch("urllib.request.urlopen", side_effect=error):
            try:
                send_email("not-an-email", "Subject", "<p>Body</p>")
            except EmailError as e:
                assert "422" in str(e)
                print("PASS: a real Resend API error surfaces as EmailError, never a silent success")
                return
        raise AssertionError("expected EmailError")
    finally:
        del os.environ["RESEND_API_KEY"]
        del os.environ["RESEND_FROM_EMAIL"]


if __name__ == "__main__":
    test_refuses_without_api_key()
    test_refuses_without_from_address()
    test_success_path_returns_real_response()
    test_http_error_surfaces_as_email_error_not_silent_success()
    print("\nAll emailer.py offline tests passed.")
