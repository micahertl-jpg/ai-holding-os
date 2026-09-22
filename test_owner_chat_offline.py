"""
test_owner_chat_offline.py — real tests for owner_chat.py, using the
actual SQLite Database (not mocks) for the data-fetch/format functions
-- same discipline as test_owner_digest_offline.py/
test_ops_maintenance_review_offline.py -- and a MockClient returning
controlled JSON for classify_intent(), mirroring
test_ops_maintenance_review_offline.py's structure.
"""

import json
import os
from datetime import datetime, timedelta

from db import Database, new_id
from registry import BusinessRegistry
from approval import ApprovalQueue
from scheduler import TIMESTAMP_FORMAT
from llm_client import MockClient
from owner_chat import (
    classify_intent, collect_chat_overview, search_research, answer_message,
    format_overview, format_pending_approvals, format_ops_health, format_trading_status,
    format_research_search, ChatError, UNCLEAR_RESPONSE, VALID_INTENTS,
)

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_owner_chat.db")

NOW = datetime(2026, 1, 2, 12, 0, 0)


def _fmt(dt):
    return dt.strftime(TIMESTAMP_FORMAT)


def _client(obj):
    return MockClient(canned_response=json.dumps(obj))


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    biz_id = BusinessRegistry(db).create("Test Co", "opportunity_discovery", "test")
    return db, biz_id


# --- classify_intent() ---

def test_classify_intent_accepts_every_valid_intent():
    for intent in VALID_INTENTS:
        result = classify_intent("some message", _client({"intent": intent, "query": "x"}))
        assert result["intent"] == intent
    print("PASS: classify_intent accepts every intent in VALID_INTENTS")


def test_classify_intent_raises_on_invalid_json():
    try:
        classify_intent("hi", MockClient(canned_response="not json"))
        assert False, "expected ChatError"
    except ChatError as e:
        assert "valid JSON" in str(e)
    print("PASS: classify_intent raises ChatError on invalid JSON, never guesses")


def test_classify_intent_raises_on_an_intent_outside_the_valid_set():
    try:
        classify_intent("launch the pet grooming idea",
                         _client({"intent": "launch_business", "query": ""}))
        assert False, "expected ChatError"
    except ChatError as e:
        assert "launch_business" in str(e)
    print("PASS: classify_intent raises ChatError on a model-invented intent outside "
          "VALID_INTENTS -- e.g. the model trying to invent an action intent")


def test_classify_intent_defaults_query_to_empty_string_when_absent():
    result = classify_intent("how's everything", _client({"intent": "overview"}))
    assert result["query"] == ""
    print("PASS: classify_intent defaults query to an empty string when the model omits it")


# --- collect_chat_overview() ---

def test_collect_chat_overview_reflects_real_counts():
    db, biz_id = _setup()
    from registry import AgentRegistry
    AgentRegistry(db).create(biz_id, "Agent A", role="R", department="d", permission_level=1)
    ApprovalQueue(db).request("launch_business", "Do a thing", business_id=biz_id)
    db.execute(
        "INSERT INTO real_transactions (id, direction, source, destination, amount_usd_cents, "
        "business_id, occurred_at) VALUES (?,?,?,?,?,?,?)",
        (new_id("txn"), "in", "stripe_customer:a@example.com", "owner_stripe_account", 5000,
         biz_id, _fmt(NOW)),
    )

    overview = collect_chat_overview(db)
    assert overview["business_count"] == 1
    assert overview["agent_count"] == 1
    assert overview["pending_approval_count"] == 1
    assert overview["real_revenue_usd_cents"] == 5000
    assert overview["ops_severity"] is None
    print("PASS: collect_chat_overview reflects real business/agent/approval/revenue counts")
    db.close()
    os.remove(TEST_DB_PATH)


# --- search_research() ---

def test_search_research_matches_across_all_four_verticals_by_title_or_summary():
    db, biz_id = _setup()
    db.execute("INSERT INTO opportunities (id, business_id, topic, summary) VALUES (?,?,?,?)",
               (new_id("opp"), biz_id, "AI-powered pet grooming subscription boxes",
                "Worth a small validation effort."))
    db.execute("INSERT INTO roblox_trends (id, business_id, concept, summary) VALUES (?,?,?,?)",
               (new_id("trend"), biz_id, "A cozy farming sim", "mentions pet grooming as a feature"))
    db.execute("INSERT INTO app_feasibility_assessments (id, business_id, concept, summary) "
               "VALUES (?,?,?,?)", (new_id("app"), biz_id, "A habit tracker app", "Unrelated."))

    results = search_research(db, "pet grooming")
    assert len(results) == 2
    verticals = {r["vertical"] for r in results}
    assert verticals == {"Opportunity Discovery", "Roblox Game Development"}
    print("PASS: search_research matches by title OR summary, across every research vertical")
    db.close()
    os.remove(TEST_DB_PATH)


def test_search_research_reports_launched_status_correctly():
    db, biz_id = _setup()
    launched_biz_id = BusinessRegistry(db).create("Launched Co", "venture", "test")
    db.execute("INSERT INTO opportunities (id, business_id, topic, launched_business_id) "
               "VALUES (?,?,?,?)", (new_id("opp"), biz_id, "Pet grooming boxes", launched_biz_id))

    results = search_research(db, "pet grooming")
    assert len(results) == 1
    assert results[0]["launched"] is True
    print("PASS: search_research correctly reports whether a matched idea was already launched")
    db.close()
    os.remove(TEST_DB_PATH)


def test_search_research_returns_nothing_for_an_empty_query():
    db, _biz_id = _setup()
    assert search_research(db, "") == []
    assert search_research(db, "   ") == []
    print("PASS: search_research returns nothing for an empty/blank query, never every row")
    db.close()
    os.remove(TEST_DB_PATH)


# --- format_*() pure functions ---

def test_format_pending_approvals_empty_and_populated():
    assert format_pending_approvals([]) == "Nothing is awaiting your decision right now."
    text = format_pending_approvals([
        {"risk_level": "high", "business_name": "Test Co", "description": "Launch it",
         "age_hours": 3.0},
    ])
    assert "Test Co" in text and "Launch it" in text and "high" in text
    print("PASS: format_pending_approvals handles both the empty and populated cases")


def test_format_ops_health_none_and_populated():
    assert format_ops_health(None) == "No system health review has run yet."
    text = format_ops_health({"severity": "warning", "summary": "Something's off.", "age_hours": 1.0})
    assert "warning" in text and "Something's off." in text
    print("PASS: format_ops_health handles both the no-report-yet and populated cases")


def test_format_trading_status_none_and_populated():
    assert format_trading_status([]) == "No paper trading portfolio has run a cycle yet."
    text = format_trading_status([
        {"business_name": "Trading Co", "starting_cash_usd": 10000.0, "equity_usd": 10250.0,
         "pnl_usd": 250.0},
    ])
    assert "Trading Co" in text and "+$250.00" in text
    print("PASS: format_trading_status handles both the no-cycle-yet and populated cases, "
          "with a correctly-signed P&L")


def test_format_research_search_none_and_populated():
    assert "couldn't find" in format_research_search([], "widgets")
    text = format_research_search([
        {"vertical": "Opportunity Discovery", "title": "Pet grooming boxes", "confidence_level": "high",
         "launched": False, "summary": "Worth validating."},
    ], "pet grooming")
    assert "Pet grooming boxes" in text and "not yet launched" in text
    print("PASS: format_research_search handles both the no-match and populated cases")


def test_format_overview_uses_real_numbers_verbatim():
    text = format_overview({
        "business_count": 3, "agent_count": 5, "open_task_count": 2,
        "pending_approval_count": 1, "real_revenue_usd_cents": 12345, "ops_severity": "ok",
    })
    assert "3 business(es)" in text and "5 agent(s)" in text and "$123.45" in text and "ok" in text
    print("PASS: format_overview reports the exact real numbers it was given, never rounded off")


# --- answer_message() end-to-end orchestration ---

def test_answer_message_routes_overview_intent_to_real_data():
    db, biz_id = _setup()
    client = _client({"intent": "overview", "query": ""})
    answer = answer_message(db, "how's everything going?", client)
    assert "1 business(es)" in answer
    print("PASS: answer_message routes 'overview' to real, freshly-computed data")
    db.close()
    os.remove(TEST_DB_PATH)


def test_answer_message_routes_research_search_intent_with_the_extracted_query():
    db, biz_id = _setup()
    db.execute("INSERT INTO opportunities (id, business_id, topic) VALUES (?,?,?)",
               (new_id("opp"), biz_id, "Pet grooming boxes"))
    client = _client({"intent": "research_search", "query": "pet grooming"})
    answer = answer_message(db, "what did we find on the pet grooming idea?", client)
    assert "Pet grooming boxes" in answer
    print("PASS: answer_message passes the classifier's extracted query into search_research")
    db.close()
    os.remove(TEST_DB_PATH)


def test_answer_message_degrades_to_unclear_response_on_a_classification_failure():
    db, _biz_id = _setup()
    answer = answer_message(db, "launch the pet grooming idea", MockClient(canned_response="not json"))
    assert answer == UNCLEAR_RESPONSE
    print("PASS: answer_message degrades gracefully to UNCLEAR_RESPONSE on a classification "
          "failure -- a chat turn never surfaces a raw error the way a failed scheduled task can")
    db.close()
    os.remove(TEST_DB_PATH)


def test_answer_message_degrades_to_unclear_response_for_an_action_shaped_intent():
    db, _biz_id = _setup()
    # Simulates the model correctly recognizing an action request isn't
    # one of the real intents and routing it to "unclear" itself, per
    # its system prompt.
    client = _client({"intent": "unclear", "query": ""})
    answer = answer_message(db, "launch the pet grooming idea", client)
    assert answer == UNCLEAR_RESPONSE
    print("PASS: an action-shaped request (e.g. 'launch X') gets the same UNCLEAR_RESPONSE, "
          "never an attempted action")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_classify_intent_accepts_every_valid_intent()
    test_classify_intent_raises_on_invalid_json()
    test_classify_intent_raises_on_an_intent_outside_the_valid_set()
    test_classify_intent_defaults_query_to_empty_string_when_absent()
    test_collect_chat_overview_reflects_real_counts()
    test_search_research_matches_across_all_four_verticals_by_title_or_summary()
    test_search_research_reports_launched_status_correctly()
    test_search_research_returns_nothing_for_an_empty_query()
    test_format_pending_approvals_empty_and_populated()
    test_format_ops_health_none_and_populated()
    test_format_trading_status_none_and_populated()
    test_format_research_search_none_and_populated()
    test_format_overview_uses_real_numbers_verbatim()
    test_answer_message_routes_overview_intent_to_real_data()
    test_answer_message_routes_research_search_intent_with_the_extracted_query()
    test_answer_message_degrades_to_unclear_response_on_a_classification_failure()
    test_answer_message_degrades_to_unclear_response_for_an_action_shaped_intent()
    print("\nAll owner_chat.py offline tests passed.")
