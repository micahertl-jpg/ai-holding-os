"""
test_owner_digest_offline.py — real tests for tasks/owner_digest.py,
using the actual SQLite Database (not mocks). Every field
collect_owner_digest() returns must come from a real query against
real seeded rows, same discipline test_ops_maintenance_review_offline.py
uses for collect_system_metrics(). format_digest_email() is pure string
formatting, tested directly against hand-built digest dicts.
"""

import os
from datetime import datetime, timedelta

from db import Database, new_id
from registry import BusinessRegistry
from approval import ApprovalQueue
from scheduler import TIMESTAMP_FORMAT
from tasks.owner_digest import (
    find_window_start, collect_owner_digest, format_digest_email, DEFAULT_LOOKBACK_HOURS,
)

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_owner_digest.db")

NOW = datetime(2026, 1, 2, 12, 0, 0)


def _fmt(dt):
    return dt.strftime(TIMESTAMP_FORMAT)


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    biz_id = BusinessRegistry(db).create("Test Co", "opportunity_discovery", "test")
    return db, biz_id


def test_collect_owner_digest_with_nothing_seeded_returns_all_empty_real_zeros():
    db, _biz_id = _setup()
    digest = collect_owner_digest(db, now=NOW)
    assert digest["pending_approvals"] == []
    assert digest["ops_health"] is None
    assert digest["revenue_usd_cents"] == 0
    assert digest["new_research"] == {}
    assert digest["trading_portfolios"] == []
    print("PASS: an empty database produces a digest of real, honest zeros -- never fabricated")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_owner_digest_includes_real_pending_approvals_with_business_name_and_age():
    db, biz_id = _setup()
    approvals = ApprovalQueue(db)
    created = NOW - timedelta(hours=5)
    approval_id = approvals.request("launch_business", "Launch the pet grooming idea",
                                     business_id=biz_id, risk_level="high")
    db.execute("UPDATE approvals SET created_at=? WHERE id=?", (_fmt(created), approval_id))

    digest = collect_owner_digest(db, now=NOW)
    assert len(digest["pending_approvals"]) == 1
    a = digest["pending_approvals"][0]
    assert a["business_name"] == "Test Co"
    assert a["description"] == "Launch the pet grooming idea"
    assert a["risk_level"] == "high"
    assert a["age_hours"] == 5.0
    print("PASS: pending approvals carry their real business name, description, and age")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_owner_digest_only_includes_pending_approvals_not_decided_ones():
    db, biz_id = _setup()
    approvals = ApprovalQueue(db)
    approval_id = approvals.request("launch_business", "Already decided", business_id=biz_id)
    approvals.approve(approval_id)

    digest = collect_owner_digest(db, now=NOW)
    assert digest["pending_approvals"] == []
    print("PASS: an already-decided approval never appears in the digest")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_owner_digest_reflects_the_latest_ops_report():
    db, biz_id = _setup()
    older = NOW - timedelta(hours=30)
    newer = NOW - timedelta(hours=2)
    db.execute(
        "INSERT INTO ops_maintenance_reports (id, business_id, overall_severity, findings, "
        "confidence_level, summary, created_at) VALUES (?,?,?,?,?,?,?)",
        (new_id("ops"), biz_id, "warning", "[]", "high", "An older report", _fmt(older)),
    )
    db.execute(
        "INSERT INTO ops_maintenance_reports (id, business_id, overall_severity, findings, "
        "confidence_level, summary, created_at) VALUES (?,?,?,?,?,?,?)",
        (new_id("ops"), biz_id, "critical", "[]", "high", "The real latest report", _fmt(newer)),
    )

    digest = collect_owner_digest(db, now=NOW)
    assert digest["ops_health"]["severity"] == "critical"
    assert digest["ops_health"]["summary"] == "The real latest report"
    assert digest["ops_health"]["age_hours"] == 2.0
    print("PASS: digest surfaces the LATEST ops report, not an older one")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_owner_digest_sums_real_revenue_only_inside_the_window():
    db, biz_id = _setup()
    window_start = NOW - timedelta(hours=10)
    inside = NOW - timedelta(hours=5)
    outside = NOW - timedelta(hours=20)
    db.execute(
        "INSERT INTO real_transactions (id, direction, source, destination, amount_usd_cents, "
        "business_id, occurred_at) VALUES (?,?,?,?,?,?,?)",
        (new_id("txn"), "in", "stripe_customer:a@example.com", "owner_stripe_account", 2500,
         biz_id, _fmt(inside)),
    )
    db.execute(
        "INSERT INTO real_transactions (id, direction, source, destination, amount_usd_cents, "
        "business_id, occurred_at) VALUES (?,?,?,?,?,?,?)",
        (new_id("txn"), "in", "stripe_customer:b@example.com", "owner_stripe_account", 9900,
         biz_id, _fmt(outside)),
    )

    digest = collect_owner_digest(db, now=NOW, window_start=window_start)
    assert digest["revenue_usd_cents"] == 2500
    print("PASS: revenue is summed only for transactions inside the digest's window")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_owner_digest_counts_new_research_per_vertical_inside_the_window():
    db, biz_id = _setup()
    window_start = NOW - timedelta(hours=10)
    inside = _fmt(NOW - timedelta(hours=1))
    outside = _fmt(NOW - timedelta(hours=20))
    db.execute("INSERT INTO opportunities (id, business_id, topic, created_at) VALUES (?,?,?,?)",
               (new_id("opp"), biz_id, "Inside window", inside))
    db.execute("INSERT INTO opportunities (id, business_id, topic, created_at) VALUES (?,?,?,?)",
               (new_id("opp"), biz_id, "Outside window", outside))
    db.execute("INSERT INTO roblox_trends (id, business_id, concept, created_at) VALUES (?,?,?,?)",
               (new_id("trend"), biz_id, "A game", inside))

    digest = collect_owner_digest(db, now=NOW, window_start=window_start)
    assert digest["new_research"] == {"Opportunity Discovery": 1, "Roblox Game Development": 1}
    print("PASS: new_research counts only rows created inside the window, per real vertical table")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_owner_digest_reports_real_trading_pnl_from_the_latest_snapshot():
    db, biz_id = _setup()
    portfolio_id = new_id("port")
    db.execute(
        "INSERT INTO paper_portfolios (id, business_id, starting_cash_usd, cash_usd) "
        "VALUES (?,?,?,?)", (portfolio_id, biz_id, 10000.0, 9500.0),
    )
    older = _fmt(NOW - timedelta(hours=8))
    newer = _fmt(NOW - timedelta(hours=1))
    db.execute(
        "INSERT INTO trading_snapshots (id, portfolio_id, equity_usd, cash_usd, open_positions, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (new_id("snap"), portfolio_id, 9800.0, 9700.0, 1, older),
    )
    db.execute(
        "INSERT INTO trading_snapshots (id, portfolio_id, equity_usd, cash_usd, open_positions, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (new_id("snap"), portfolio_id, 10250.0, 9500.0, 1, newer),
    )

    digest = collect_owner_digest(db, now=NOW)
    assert len(digest["trading_portfolios"]) == 1
    p = digest["trading_portfolios"][0]
    assert p["business_name"] == "Test Co"
    assert p["equity_usd"] == 10250.0
    assert p["pnl_usd"] == 250.0
    print("PASS: trading P&L uses the LATEST snapshot's real mark-to-market equity, no live "
          "quote fetch needed")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_owner_digest_skips_a_portfolio_with_no_snapshot_yet():
    db, biz_id = _setup()
    db.execute(
        "INSERT INTO paper_portfolios (id, business_id, starting_cash_usd, cash_usd) "
        "VALUES (?,?,?,?)", (new_id("port"), biz_id, 10000.0, 10000.0),
    )
    digest = collect_owner_digest(db, now=NOW)
    assert digest["trading_portfolios"] == []
    print("PASS: a portfolio with no cycle run yet is skipped, not reported with a fabricated P&L")
    db.close()
    os.remove(TEST_DB_PATH)


def test_find_window_start_uses_the_most_recent_completed_owner_digest_task():
    db, biz_id = _setup()
    older = _fmt(NOW - timedelta(hours=48))
    newer = _fmt(NOW - timedelta(hours=6))
    db.execute(
        "INSERT INTO tasks (id, business_id, objective, task_type, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (new_id("task"), biz_id, "old digest", "owner_digest", "completed", older),
    )
    db.execute(
        "INSERT INTO tasks (id, business_id, objective, task_type, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (new_id("task"), biz_id, "failed digest", "owner_digest", "failed", newer),
    )
    real_newer_completed = _fmt(NOW - timedelta(hours=3))
    db.execute(
        "INSERT INTO tasks (id, business_id, objective, task_type, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (new_id("task"), biz_id, "real latest digest", "owner_digest", "completed",
         real_newer_completed),
    )

    window_start = find_window_start(db)
    assert window_start.strftime(TIMESTAMP_FORMAT) == real_newer_completed
    print("PASS: find_window_start anchors to the latest COMPLETED digest task, ignoring a "
          "failed one even if it's more recent")
    db.close()
    os.remove(TEST_DB_PATH)


def test_find_window_start_returns_none_with_no_prior_completed_digest():
    db, _biz_id = _setup()
    assert find_window_start(db) is None
    print("PASS: find_window_start returns None (caller falls back to "
          f"{DEFAULT_LOOKBACK_HOURS}h) when no digest has ever completed")
    db.close()
    os.remove(TEST_DB_PATH)


def test_format_digest_email_subject_reflects_pending_approval_count():
    subject, _body = format_digest_email({
        "pending_approvals": [{"risk_level": "low", "business_name": "A", "description": "d",
                                "age_hours": 1.0}],
        "ops_health": None, "revenue_usd_cents": 0, "new_research": {}, "trading_portfolios": [],
    })
    assert "1 approval" in subject

    subject_empty, _body = format_digest_email({
        "pending_approvals": [], "ops_health": None, "revenue_usd_cents": 0,
        "new_research": {}, "trading_portfolios": [],
    })
    assert "nothing awaiting" in subject_empty
    print("PASS: the email subject reflects the real pending-approval count")


def test_format_digest_email_escapes_html_in_untrusted_text_fields():
    subject, body = format_digest_email({
        "pending_approvals": [{"risk_level": "low", "business_name": "<script>alert(1)</script>",
                                "description": "d", "age_hours": 1.0}],
        "ops_health": None, "revenue_usd_cents": 0, "new_research": {}, "trading_portfolios": [],
    })
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    print("PASS: format_digest_email escapes business names/descriptions, never injects raw HTML")


if __name__ == "__main__":
    test_collect_owner_digest_with_nothing_seeded_returns_all_empty_real_zeros()
    test_collect_owner_digest_includes_real_pending_approvals_with_business_name_and_age()
    test_collect_owner_digest_only_includes_pending_approvals_not_decided_ones()
    test_collect_owner_digest_reflects_the_latest_ops_report()
    test_collect_owner_digest_sums_real_revenue_only_inside_the_window()
    test_collect_owner_digest_counts_new_research_per_vertical_inside_the_window()
    test_collect_owner_digest_reports_real_trading_pnl_from_the_latest_snapshot()
    test_collect_owner_digest_skips_a_portfolio_with_no_snapshot_yet()
    test_find_window_start_uses_the_most_recent_completed_owner_digest_task()
    test_find_window_start_returns_none_with_no_prior_completed_digest()
    test_format_digest_email_subject_reflects_pending_approval_count()
    test_format_digest_email_escapes_html_in_untrusted_text_fields()
    print("\nAll owner_digest.py offline tests passed.")
