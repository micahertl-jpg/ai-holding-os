"""
test_fulfillment_offline.py — real tests for fulfillment.py's
run_once(), using the actual SQLite Database (not mocks) and a mocked
emailer.send_email (this sandbox has no internet/Resend API key).
Proves a completed order actually gets emailed and marked fulfilled,
a failed underlying task gets marked failed (not silently stuck), a
still-running order is left alone, and a report can never be "sent"
without send_email() actually being called and succeeding.
"""

import os
import json
from unittest.mock import patch

from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator
from db import new_id
import fulfillment

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_fulfillment.db")


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)
    biz_id = businesses.create("Store Test Co", "storefront", "test")
    agent_id = agents.create(biz_id, "Researcher", role="Research Analyst",
                              department="research", permission_level=2)
    agents.set_status(agent_id, "idle")
    return db, orch, biz_id


def _make_order(db, biz_id, product_type="research_opportunity", topic="AI recipe apps",
                 status="paid", task_id=None):
    order_id = new_id("ord")
    db.execute(
        "INSERT INTO orders (id, product_type, topic, customer_email, price_usd_cents, "
        "currency, business_id, status, task_id) VALUES (?, ?, ?, ?, ?, 'usd', ?, ?, ?)",
        (order_id, product_type, topic, "customer@example.com", 1900, biz_id, status, task_id),
    )
    return order_id


def test_completed_order_gets_emailed_and_marked_fulfilled():
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "AI recipe apps"})
    orch.complete_task(task_id, result="ok")
    opp_id = new_id("opp")
    db.execute(
        "INSERT INTO opportunities (id, business_id, task_id, topic, market_size, "
        "confidence_level, summary, reference_urls_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (opp_id, biz_id, task_id, "AI recipe apps", "Moderate", "medium",
         "Worth trying.", json.dumps([])),
    )
    order_id = _make_order(db, biz_id, task_id=task_id)

    with patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "fulfilled")], outcomes
    assert mock_send.call_count == 1
    call_args = mock_send.call_args[0]
    assert call_args[0] == "customer@example.com"
    assert "AI recipe apps" in call_args[1]  # subject

    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "fulfilled"
    assert order["fulfilled_at"] is not None
    print("PASS: a completed order with a real assessment gets emailed and marked fulfilled")
    db.close()
    os.remove(TEST_DB_PATH)


def test_order_never_marked_fulfilled_if_send_email_raises():
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "x"})
    orch.complete_task(task_id, result="ok")
    opp_id = new_id("opp")
    db.execute(
        "INSERT INTO opportunities (id, business_id, task_id, topic, confidence_level, "
        "summary, reference_urls_used) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (opp_id, biz_id, task_id, "x", "low", "Meh.", json.dumps([])),
    )
    order_id = _make_order(db, biz_id, task_id=task_id)

    from emailer import EmailError
    with patch("fulfillment.send_email", side_effect=EmailError("Resend is down")):
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "email_failed: Resend is down")], outcomes
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "paid", "must stay 'paid' (retryable), never silently 'fulfilled'"
    print("PASS: a failed email send leaves the order retryable, never falsely marked fulfilled")
    db.close()
    os.remove(TEST_DB_PATH)


def test_failed_task_marks_order_failed():
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "x"})
    orch.fail_task(task_id, reason="model error")
    order_id = _make_order(db, biz_id, task_id=task_id)

    with patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "failed")], outcomes
    assert mock_send.call_count == 0, "must never email a report for a task that failed"
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "failed"
    print("PASS: an order whose research task failed is marked failed, no email ever sent")
    db.close()
    os.remove(TEST_DB_PATH)


def test_still_running_order_is_left_alone():
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "x"})
    # deliberately do NOT complete/fail the task — stays 'assigned'
    order_id = _make_order(db, biz_id, task_id=task_id)

    with patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == [], outcomes
    assert mock_send.call_count == 0
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "paid", "must not touch an order whose task hasn't finished yet"
    print("PASS: an order whose task is still running is left alone, checked again next pass")
    db.close()
    os.remove(TEST_DB_PATH)


def test_unpaid_orders_are_never_touched():
    db, orch, biz_id = _setup()
    order_id = _make_order(db, biz_id, status="pending_payment", task_id=None)

    with patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == []
    assert mock_send.call_count == 0
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "pending_payment"
    print("PASS: an order that was never actually paid is never processed or emailed")
    db.close()
    os.remove(TEST_DB_PATH)


def test_missing_assessment_row_fails_loudly_not_silently():
    """A completed task with NO opportunities row (shouldn't normally
    happen, but must never be papered over) must never be emailed as
    if it had real content."""
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "x"})
    orch.complete_task(task_id, result="ok")
    # deliberately do NOT insert an opportunities row
    order_id = _make_order(db, biz_id, task_id=task_id)

    with patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes[0][0] == order_id
    assert outcomes[0][1].startswith("build_failed:")
    assert mock_send.call_count == 0
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "paid"
    print("PASS: a completed task with no real assessment row fails loudly, never fabricates an email")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_completed_order_gets_emailed_and_marked_fulfilled()
    test_order_never_marked_fulfilled_if_send_email_raises()
    test_failed_task_marks_order_failed()
    test_still_running_order_is_left_alone()
    test_unpaid_orders_are_never_touched()
    test_missing_assessment_row_fails_loudly_not_silently()
    print("\nAll fulfillment.py offline tests passed.")
