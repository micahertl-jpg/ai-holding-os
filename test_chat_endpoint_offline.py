"""
test_chat_endpoint_offline.py — calls api.py's real chat() function
directly (same technique test_set_job_interval_offline.py and
test_delete_abandoned_order_offline.py use) rather than mirroring its
logic by hand. Patches api.get_default_client so the test controls
exactly what the classifier "sees" without needing a real
ANTHROPIC_API_KEY, same technique test_executor_offline.py uses for
OWNER_EMAIL/send_email.

Requires fastapi/pydantic installed (`.venv/bin/python3`).
"""

import json
import os
from unittest.mock import patch

from fastapi import HTTPException

import api
from db import Database
from llm_client import MockClient

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_chat_endpoint.db")


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    api.state["db"] = db
    return db


def test_chat_endpoint_returns_a_real_answer_end_to_end():
    db = _setup()
    canned = MockClient(canned_response=json.dumps({"intent": "overview", "query": ""}))
    with patch.object(api, "get_default_client", return_value=canned):
        result = api.chat(api.ChatRequest(message="how's everything going?"))
    assert "answer" in result
    assert "0 business(es)" in result["answer"]
    print("PASS: POST /chat returns a real answer computed from the real (empty) database")
    db.close()
    os.remove(TEST_DB_PATH)


def test_chat_endpoint_rejects_an_empty_message():
    _setup()
    try:
        api.chat(api.ChatRequest(message="   "))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 400
    print("PASS: POST /chat rejects a blank message before ever calling the model")
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_chat_endpoint_returns_a_real_answer_end_to_end()
    test_chat_endpoint_rejects_an_empty_message()
    print("\nAll chat endpoint offline tests passed.")
