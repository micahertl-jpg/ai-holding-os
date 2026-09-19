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
from emailer import EmailError
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


def test_report_email_escapes_html_in_customer_and_model_supplied_fields():
    """Regression test for a real bug: topic/concept traces back to
    unauthenticated, customer-submitted input at checkout with no
    sanitization beyond .strip() (api.py's /store/checkout), and the
    other fields are model output that could echo it back. Before the
    fix, all of this was interpolated into the email's HTML body with
    zero escaping -- a customer could submit '<script>...' or
    '<a href="...">' as their "topic" and have it rendered as live HTML
    in a real transactional email sent, from this business's verified
    domain, to whatever customer_email they also supplied (not
    necessarily their own address). The dashboard already escapes this
    same data; the email path must too."""
    db, orch, biz_id = _setup()
    evil_topic = '<script>alert("xss")</script><a href="https://evil.example/phish">click</a>'
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": evil_topic})
    orch.complete_task(task_id, result="ok")
    opp_id = new_id("opp")
    db.execute(
        "INSERT INTO opportunities (id, business_id, task_id, topic, market_size, "
        "confidence_level, summary, reference_urls_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (opp_id, biz_id, task_id, evil_topic, '<img src=x onerror=alert(1)>',
         "medium", "Worth trying.", json.dumps([])),
    )
    order_id = _make_order(db, biz_id, topic=evil_topic, task_id=task_id)

    with patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "fulfilled")], outcomes
    html_body = mock_send.call_args[0][2]
    assert "<script>" not in html_body, "raw <script> tag leaked into the email HTML body"
    assert "<img" not in html_body, "raw <img onerror=...> leaked into the email HTML body"
    assert "&lt;script&gt;" in html_body, "the topic should appear HTML-escaped, not stripped"
    print("PASS: customer-submitted HTML in topic/model fields is escaped, never rendered "
          "live, in the real report email")
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

    with patch.object(fulfillment, "OWNER_EMAIL", None), \
         patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "failed")], outcomes
    assert mock_send.call_count == 0, "must never email a report for a task that failed"
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "failed"
    print("PASS: an order whose research task failed is marked failed, no email ever sent")
    db.close()
    os.remove(TEST_DB_PATH)


def test_failed_order_alerts_owner_when_owner_email_is_configured():
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "a subscription box for plants"})
    orch.fail_task(task_id, reason="model error")
    order_id = _make_order(db, biz_id, topic="a subscription box for plants", task_id=task_id)

    with patch.object(fulfillment, "OWNER_EMAIL", "owner@example.com"), \
         patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "failed")], outcomes
    assert mock_send.call_count == 1
    call_args = mock_send.call_args[0]
    assert call_args[0] == "owner@example.com"
    assert "a subscription box for plants" in call_args[1]  # subject
    assert "refund" in call_args[2].lower()
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "failed"
    print("PASS: a failed order alerts the configured OWNER_EMAIL that a manual refund is needed")
    db.close()
    os.remove(TEST_DB_PATH)


def test_failed_order_alert_escapes_customer_supplied_fields():
    db, orch, biz_id = _setup()
    evil_topic = '<script>alert("xss")</script>'
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": evil_topic})
    orch.fail_task(task_id, reason="model error")
    order_id = _make_order(db, biz_id, topic=evil_topic, task_id=task_id)

    with patch.object(fulfillment, "OWNER_EMAIL", "owner@example.com"), \
         patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "failed")], outcomes
    html_body = mock_send.call_args[0][2]
    assert "<script>" not in html_body
    print("PASS: the owner failed-order alert escapes customer-submitted topic text")
    db.close()
    os.remove(TEST_DB_PATH)


def test_failed_order_stays_failed_even_if_owner_alert_email_errors():
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "x"})
    orch.fail_task(task_id, reason="model error")
    order_id = _make_order(db, biz_id, task_id=task_id)

    with patch.object(fulfillment, "OWNER_EMAIL", "owner@example.com"), \
         patch("fulfillment.send_email", side_effect=EmailError("Resend is down")):
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "failed")], outcomes
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "failed", \
        "a broken owner-alert send must never undo the order's own already-correct status"
    print("PASS: a broken owner-alert email never crashes the loop or reverts the order's status")
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


def test_app_feasibility_order_gets_emailed_and_marked_fulfilled():
    """Same success path as test_completed_order_gets_emailed_and_marked_fulfilled,
    but for the third storefront product (added alongside the App
    Development vertical) -- proves fulfillment.py's app_feasibility
    branch actually works, not just that it compiles."""
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Assess feasibility", department="research",
                                permission_level_required=2, task_type="research_app_feasibility",
                                task_input={"concept": "a habit tracker app"})
    orch.complete_task(task_id, result="ok")
    assessment_id = new_id("app")
    db.execute(
        "INSERT INTO app_feasibility_assessments (id, business_id, task_id, concept, "
        "platform_recommendation, suggested_tech_stack, complexity_tier, estimated_timeline, "
        "estimated_cost_range, mvp_feature_scope, key_technical_risks, similar_existing_apps, "
        "confidence_level, summary, reference_urls_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (assessment_id, biz_id, task_id, "a habit tracker app", "iOS + Android via React Native",
         "React Native, FastAPI, Postgres", "moderate", "8-12 weeks", "$15k-$40k",
         "Account creation, one core loop", "Push notification reliability",
         "A few comparable apps exist", "medium", "Worth a small MVP validation effort.",
         json.dumps([])),
    )
    order_id = _make_order(db, biz_id, product_type="research_app_feasibility",
                            topic="a habit tracker app", task_id=task_id)

    with patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes == [(order_id, "fulfilled")], outcomes
    assert mock_send.call_count == 1
    call_args = mock_send.call_args[0]
    assert call_args[0] == "customer@example.com"
    assert "a habit tracker app" in call_args[1]  # subject
    html_body = call_args[2]
    assert "React Native, FastAPI, Postgres" in html_body

    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "fulfilled"
    print("PASS: a completed research_app_feasibility order gets emailed and marked fulfilled")
    db.close()
    os.remove(TEST_DB_PATH)


def test_app_feasibility_missing_assessment_row_fails_loudly_not_silently():
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Assess feasibility", department="research",
                                permission_level_required=2, task_type="research_app_feasibility",
                                task_input={"concept": "x"})
    orch.complete_task(task_id, result="ok")
    # deliberately do NOT insert an app_feasibility_assessments row
    order_id = _make_order(db, biz_id, product_type="research_app_feasibility",
                            topic="x", task_id=task_id)

    with patch("fulfillment.send_email") as mock_send:
        outcomes = fulfillment.run_once(db)

    assert outcomes[0][0] == order_id
    assert outcomes[0][1].startswith("build_failed:")
    assert mock_send.call_count == 0
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "paid"
    print("PASS: a completed research_app_feasibility task with no real assessment row "
          "fails loudly, never fabricates an email")
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


def test_build_failure_exhausts_retries_and_marks_order_failed():
    """Regression test for a real bug: an order whose expected result
    row is permanently missing (a data bug elsewhere, not something
    that fixes itself) used to stay 'paid' forever, re-logging the
    identical error on every single fulfillment pass with no terminal
    state and no owner alert, ever."""
    db, orch, biz_id = _setup()
    task_id = orch.create_task(biz_id, "Research", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "x"})
    orch.complete_task(task_id, result="ok")
    # deliberately do NOT insert an opportunities row -- every pass fails identically
    order_id = _make_order(db, biz_id, task_id=task_id)

    with patch.object(fulfillment, "BUILD_FAILURE_RETRY_LIMIT", 3), \
         patch.object(fulfillment, "OWNER_EMAIL", "owner@example.com"), \
         patch("fulfillment.send_email") as mock_send:
        for _ in range(2):
            outcomes = fulfillment.run_once(db)
            assert outcomes[0][1].startswith("build_failed:"), outcomes
            order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
            assert order["status"] == "paid"
        assert mock_send.call_count == 0, "no owner alert until the retry limit is actually hit"

        outcomes = fulfillment.run_once(db)

    assert outcomes[0][0] == order_id
    assert outcomes[0][1].startswith("failed: build_failed_permanently:"), outcomes
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    assert order["status"] == "failed"
    assert mock_send.call_count == 1
    call_args = mock_send.call_args[0]
    assert call_args[0] == "owner@example.com"
    assert "3 attempts" in call_args[2]
    print("PASS: an order whose report data never appears is eventually marked failed "
          "and the owner alerted, not retried forever with no terminal state")
    db.close()
    os.remove(TEST_DB_PATH)


def test_build_failure_retry_count_is_scoped_to_its_own_order():
    """The retry-limit query counts audit_log rows by target_id -- a
    second order that starts failing must get its own fresh count, not
    inherit however many build-failure rows an unrelated older order
    has already piled up in the same audit log."""
    db, orch, biz_id = _setup()

    def _make_stuck_order():
        task_id = orch.create_task(biz_id, "Research", department="research",
                                    permission_level_required=2, task_type="research_opportunity",
                                    task_input={"topic": "x"})
        orch.complete_task(task_id, result="ok")
        return _make_order(db, biz_id, task_id=task_id)

    noisy_order_id = _make_stuck_order()

    with patch.object(fulfillment, "BUILD_FAILURE_RETRY_LIMIT", 2), \
         patch("fulfillment.send_email"):
        for _ in range(3):
            fulfillment.run_once(db)
        noisy_order = db.query_one("SELECT * FROM orders WHERE id=?", (noisy_order_id,))
        assert noisy_order["status"] == "failed"

        target_order_id = _make_stuck_order()
        outcomes = fulfillment.run_once(db)

    target_outcome = next(o for o in outcomes if o[0] == target_order_id)
    assert target_outcome[1].startswith("build_failed:"), target_outcome
    target_order = db.query_one("SELECT * FROM orders WHERE id=?", (target_order_id,))
    assert target_order["status"] == "paid", \
        "a fresh order must not inherit another order's build-failure count"
    print("PASS: each order's retry count is scoped to its own audit log entries")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_completed_order_gets_emailed_and_marked_fulfilled()
    test_report_email_escapes_html_in_customer_and_model_supplied_fields()
    test_order_never_marked_fulfilled_if_send_email_raises()
    test_failed_task_marks_order_failed()
    test_failed_order_alerts_owner_when_owner_email_is_configured()
    test_failed_order_alert_escapes_customer_supplied_fields()
    test_failed_order_stays_failed_even_if_owner_alert_email_errors()
    test_still_running_order_is_left_alone()
    test_unpaid_orders_are_never_touched()
    test_missing_assessment_row_fails_loudly_not_silently()
    test_build_failure_exhausts_retries_and_marks_order_failed()
    test_build_failure_retry_count_is_scoped_to_its_own_order()
    test_app_feasibility_order_gets_emailed_and_marked_fulfilled()
    test_app_feasibility_missing_assessment_row_fails_loudly_not_silently()
    print("\nAll fulfillment.py offline tests passed.")
