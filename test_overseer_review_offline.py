"""
test_overseer_review_offline.py — real tests for
tasks/overseer_review.py: the Overseer's quiet-hours clock check, the
"one call per issue" dedup query, the spoken-message formatting, and
the orchestration in run_overseer_review(). Uses a real SQLite Database
with hand-seeded fixtures (same pattern as test_owner_digest's
collect_owner_digest tests) and a MockCaller test double in place of a
real Twilio call.
"""

import os
from datetime import datetime

from db import Database, new_id
from registry import BusinessRegistry
from approval import ApprovalQueue
from tasks.overseer_review import (
    is_quiet_hours, find_uncalled_pending_approvals, format_call_message,
    run_overseer_review,
)

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_overseer_review.db")


class MockCaller:
    def __init__(self, sid="CAtest"):
        self.sid = sid
        self.calls = []

    def place_call(self, to_number, message):
        self.calls.append((to_number, message))
        return {"sid": self.sid, "status": "queued"}


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    approvals = ApprovalQueue(db)
    biz_id = businesses.create("Overseer Test Co", "opportunity_discovery", "test")
    return db, businesses, approvals, biz_id


def test_is_quiet_hours_within_a_same_day_window():
    assert is_quiet_hours(datetime(2026, 1, 1, 13, 0), "12:00", "14:00", "UTC") is True
    assert is_quiet_hours(datetime(2026, 1, 1, 11, 59), "12:00", "14:00", "UTC") is False
    assert is_quiet_hours(datetime(2026, 1, 1, 14, 0), "12:00", "14:00", "UTC") is False, \
        "the window is half-open -- the end minute itself is NOT quiet"
    print("PASS: is_quiet_hours correctly bounds a same-day window")


def test_is_quiet_hours_crossing_midnight():
    # 22:00 -> 07:00: quiet late at night AND early morning, awake midday.
    assert is_quiet_hours(datetime(2026, 1, 1, 23, 0), "22:00", "07:00", "UTC") is True
    assert is_quiet_hours(datetime(2026, 1, 1, 3, 0), "22:00", "07:00", "UTC") is True
    assert is_quiet_hours(datetime(2026, 1, 1, 12, 0), "22:00", "07:00", "UTC") is False
    assert is_quiet_hours(datetime(2026, 1, 1, 22, 0), "22:00", "07:00", "UTC") is True
    assert is_quiet_hours(datetime(2026, 1, 1, 7, 0), "22:00", "07:00", "UTC") is False
    print("PASS: is_quiet_hours correctly handles a window that crosses midnight")


def test_is_quiet_hours_respects_timezone():
    # 18:00 UTC is 13:00 in America/New_York (UTC-5 in January) -- well
    # outside a 22:00-07:00 New York-local quiet window.
    assert is_quiet_hours(
        datetime(2026, 1, 1, 18, 0), "22:00", "07:00", "America/New_York") is False
    # 03:00 UTC is 22:00 the prior day in New York -- inside the window.
    assert is_quiet_hours(
        datetime(2026, 1, 1, 3, 0), "22:00", "07:00", "America/New_York") is True
    print("PASS: is_quiet_hours evaluates the window in the configured local timezone, not UTC")


def test_find_uncalled_pending_approvals_excludes_already_called_and_non_pending():
    db, businesses, approvals, biz_id = _setup()
    pending_uncalled = approvals.request("launch_business", "Launch idea A", business_id=biz_id)
    pending_called = approvals.request("launch_business", "Launch idea B", business_id=biz_id)
    approved = approvals.request("launch_business", "Launch idea C", business_id=biz_id)
    db.execute("UPDATE approvals SET status='approved' WHERE id=?", (approved,))
    db.execute("INSERT INTO overseer_calls (id, approval_id, call_sid) VALUES (?, ?, ?)",
               (new_id("ovc"), pending_called, "CAold"))

    found = find_uncalled_pending_approvals(db)
    found_ids = [row["id"] for row in found]
    assert found_ids == [pending_uncalled], found_ids
    print("PASS: find_uncalled_pending_approvals returns only still-pending approvals that "
          "have never triggered a call, excluding already-called and non-pending ones")
    db.close()
    os.remove(TEST_DB_PATH)


def test_format_call_message_single_vs_multiple():
    single = [{"business_name": "Acme", "risk_level": "high", "action_type": "spend_usd"}]
    msg = format_call_message(single)
    assert "Acme" in msg and "high" in msg and "spend_usd" in msg
    assert "dashboard" in msg

    multiple = [
        {"business_name": "Acme", "risk_level": "low", "action_type": "spend_usd"},
        {"business_name": "Widgets Co", "risk_level": "high", "action_type": "sign_contract"},
    ]
    msg2 = format_call_message(multiple)
    assert "2 approvals" in msg2
    assert "Widgets Co" in msg2 and "high" in msg2, "must name the highest-risk item, not just the first"
    print("PASS: format_call_message names the single approval directly, and for multiple "
          "leads with the count plus the single highest-risk item")


def test_run_overseer_review_skips_during_quiet_hours():
    db, businesses, approvals, biz_id = _setup()
    approvals.request("launch_business", "Launch idea A", business_id=biz_id)
    caller = MockCaller()

    result = run_overseer_review(
        db, caller, "+15551234567", quiet_start="00:00", quiet_end="23:59",
        tz_name="UTC", now=datetime(2026, 1, 1, 12, 0))

    assert result == {"called": False, "reason": "quiet_hours", "approval_count": 0, "call_sid": None}
    assert caller.calls == []
    print("PASS: run_overseer_review places no call during configured quiet hours, even with "
          "a pending approval waiting")
    db.close()
    os.remove(TEST_DB_PATH)


def test_run_overseer_review_skips_with_no_quiet_hours_configured():
    """Both quiet_start/quiet_end must be set for quiet hours to apply
    at all -- an unset pair is never a surprise default that silently
    blocks calls."""
    db, businesses, approvals, biz_id = _setup()
    approvals.request("launch_business", "Launch idea A", business_id=biz_id)
    caller = MockCaller()

    result = run_overseer_review(db, caller, "+15551234567", now=datetime(2026, 1, 1, 3, 0))

    assert result["called"] is True
    assert len(caller.calls) == 1
    print("PASS: with no quiet hours configured, run_overseer_review calls at any hour")
    db.close()
    os.remove(TEST_DB_PATH)


def test_run_overseer_review_no_op_when_nothing_pending():
    db, businesses, approvals, biz_id = _setup()
    caller = MockCaller()

    result = run_overseer_review(db, caller, "+15551234567")

    assert result == {"called": False, "reason": "no_new_approvals", "approval_count": 0,
                       "call_sid": None}
    assert caller.calls == []
    print("PASS: run_overseer_review is a real no-op (not an error) when there is nothing "
          "pending to call about")
    db.close()
    os.remove(TEST_DB_PATH)


def test_run_overseer_review_calls_once_and_records_every_covered_approval():
    db, businesses, approvals, biz_id = _setup()
    a1 = approvals.request("launch_business", "Launch idea A", business_id=biz_id,
                            risk_level="low")
    a2 = approvals.request("spend_usd", "Buy ad credits", business_id=biz_id, risk_level="high")
    caller = MockCaller(sid="CAreal")

    result = run_overseer_review(db, caller, "+15551234567")

    assert result == {"called": True, "reason": None, "approval_count": 2, "call_sid": "CAreal"}
    assert len(caller.calls) == 1, "one phone call covers every uncalled pending approval, not one call each"
    assert caller.calls[0][0] == "+15551234567"

    recorded = {row["approval_id"] for row in db.query("SELECT * FROM overseer_calls")}
    assert recorded == {a1, a2}
    print("PASS: run_overseer_review places exactly one call covering every uncalled pending "
          "approval and records each as called")
    db.close()
    os.remove(TEST_DB_PATH)


def test_run_overseer_review_never_calls_twice_about_the_same_approval():
    db, businesses, approvals, biz_id = _setup()
    approvals.request("launch_business", "Launch idea A", business_id=biz_id)
    caller = MockCaller()

    first = run_overseer_review(db, caller, "+15551234567")
    second = run_overseer_review(db, caller, "+15551234567")

    assert first["called"] is True
    assert second == {"called": False, "reason": "no_new_approvals", "approval_count": 0,
                       "call_sid": None}
    assert len(caller.calls) == 1, "a second review pass must never call again about the same still-pending approval"
    print("PASS: run_overseer_review never calls twice about the same still-pending approval")
    db.close()
    os.remove(TEST_DB_PATH)


def test_run_overseer_review_calls_again_for_a_genuinely_new_approval():
    db, businesses, approvals, biz_id = _setup()
    approvals.request("launch_business", "Launch idea A", business_id=biz_id)
    caller = MockCaller()
    run_overseer_review(db, caller, "+15551234567")

    approvals.request("spend_usd", "A brand new ask", business_id=biz_id)
    second = run_overseer_review(db, caller, "+15551234567")

    assert second["called"] is True
    assert second["approval_count"] == 1
    assert len(caller.calls) == 2, "a genuinely new approval must still trigger a fresh call"
    print("PASS: a new pending approval still triggers a fresh call even right after a "
          "previous one")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_is_quiet_hours_within_a_same_day_window()
    test_is_quiet_hours_crossing_midnight()
    test_is_quiet_hours_respects_timezone()
    test_find_uncalled_pending_approvals_excludes_already_called_and_non_pending()
    test_format_call_message_single_vs_multiple()
    test_run_overseer_review_skips_during_quiet_hours()
    test_run_overseer_review_skips_with_no_quiet_hours_configured()
    test_run_overseer_review_no_op_when_nothing_pending()
    test_run_overseer_review_calls_once_and_records_every_covered_approval()
    test_run_overseer_review_never_calls_twice_about_the_same_approval()
    test_run_overseer_review_calls_again_for_a_genuinely_new_approval()
    print("\nAll overseer_review.py offline tests passed.")
