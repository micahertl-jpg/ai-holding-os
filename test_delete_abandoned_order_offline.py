"""
test_delete_abandoned_order_offline.py — calls api.py's real
delete_abandoned_order() function directly (FastAPI's @app.delete
decorator leaves the underlying function callable, same technique
test_api_request_models_offline.py uses for the request models) rather
than mirroring its logic by hand, since this endpoint's guard
conditions (status check, age check) are exactly the kind of thing a
hand-mirrored test could get subtly wrong in a way that still passes.
Requires fastapi/pydantic installed (`.venv/bin/python3`).

This endpoint exists for one specific, narrow case: an owner manually
verified against Stripe's own record (never this system's) that a
pending_payment order never actually collected payment, and wants it
gone from the dashboard. Every test here is really testing a refusal
path except the one success case, because the refusals are what keep
this from ever being pointed at a real, paid order.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

import api
from db import Database
from registry import BusinessRegistry

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_delete_abandoned_order.db")


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    api.state["db"] = db
    api.state["businesses"] = BusinessRegistry(db)
    biz_id = api.state["businesses"].create("Storefront Co", "research_store", "test", 0.0)
    return db, biz_id


def _insert_order(db, biz_id, order_id, status, hours_old, stripe_session_id="cs_test_abc"):
    created_at = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_old))
    db.execute(
        "INSERT INTO orders (id, product_type, topic, customer_email, price_usd_cents, "
        "business_id, status, stripe_session_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (order_id, "research_opportunity", "a topic", "a@b.com", 1900, biz_id, status,
         stripe_session_id, created_at.strftime("%Y-%m-%d %H:%M:%S")),
    )


def test_deletes_a_pending_order_stuck_past_the_threshold():
    db, biz_id = _setup()
    _insert_order(db, biz_id, "ord_stuck", "pending_payment", hours_old=48)

    result = api.delete_abandoned_order(biz_id, "ord_stuck")
    assert result == {"status": "deleted"}
    assert db.query_one("SELECT id FROM orders WHERE id=?", ("ord_stuck",)) is None

    audit_row = db.query_one(
        "SELECT * FROM audit_log WHERE action='delete_abandoned_order' AND target_id=?", ("ord_stuck",)
    )
    assert audit_row is not None, "the deletion must be recorded in the audit trail"
    print("PASS: a pending_payment order stuck past the threshold is deleted and audited")
    db.close()
    os.remove(TEST_DB_PATH)


def test_refuses_a_pending_order_that_is_not_stuck_yet():
    db, biz_id = _setup()
    _insert_order(db, biz_id, "ord_fresh", "pending_payment", hours_old=1)

    try:
        api.delete_abandoned_order(biz_id, "ord_fresh")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 400
        assert "1.0h" in e.detail or "1h" in e.detail
    assert db.query_one("SELECT id FROM orders WHERE id=?", ("ord_fresh",)) is not None, \
        "a too-recent order must NOT be deleted -- its Stripe session could still be live"
    print("PASS: a pending_payment order that isn't stuck yet is refused, not deleted")
    db.close()
    os.remove(TEST_DB_PATH)


def test_refuses_a_paid_order_regardless_of_age():
    db, biz_id = _setup()
    _insert_order(db, biz_id, "ord_paid", "paid", hours_old=999)

    try:
        api.delete_abandoned_order(biz_id, "ord_paid")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 400
        assert "paid" in e.detail
    assert db.query_one("SELECT id FROM orders WHERE id=?", ("ord_paid",)) is not None, \
        "a paid order must never be deletable here, no matter how old"
    print("PASS: a paid order is refused regardless of age -- real money never gets silently deleted")
    db.close()
    os.remove(TEST_DB_PATH)


def test_refuses_a_failed_order_since_it_may_still_have_been_paid():
    db, biz_id = _setup()
    _insert_order(db, biz_id, "ord_failed", "failed", hours_old=999)

    try:
        api.delete_abandoned_order(biz_id, "ord_failed")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 400
    print("PASS: a failed order is refused -- 'failed' can mean the research task failed "
          "after a real payment succeeded")
    db.close()
    os.remove(TEST_DB_PATH)


def test_404s_on_an_unknown_order_or_business():
    db, biz_id = _setup()
    try:
        api.delete_abandoned_order(biz_id, "ord_does_not_exist")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404

    try:
        api.delete_abandoned_order("biz_does_not_exist", "ord_whatever")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404
    print("PASS: an unknown order or business 404s rather than deleting anything")
    db.close()
    os.remove(TEST_DB_PATH)


def test_an_orders_business_scoping_is_respected():
    db, biz_id = _setup()
    other_biz_id = api.state["businesses"].create("Other Co", "test", "test", 0.0)
    _insert_order(db, biz_id, "ord_scoped", "pending_payment", hours_old=48)

    try:
        api.delete_abandoned_order(other_biz_id, "ord_scoped")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404
    assert db.query_one("SELECT id FROM orders WHERE id=?", ("ord_scoped",)) is not None
    print("PASS: an order can only be deleted through its own business_id, not any other")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_deletes_a_pending_order_stuck_past_the_threshold()
    test_refuses_a_pending_order_that_is_not_stuck_yet()
    test_refuses_a_paid_order_regardless_of_age()
    test_refuses_a_failed_order_since_it_may_still_have_been_paid()
    test_404s_on_an_unknown_order_or_business()
    test_an_orders_business_scoping_is_respected()
    print("\nAll delete_abandoned_order offline tests passed.")
