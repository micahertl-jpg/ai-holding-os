"""
test_ops_maintenance_review_offline.py — real tests for
tasks/ops_maintenance_review.py, using the actual SQLite Database (not
mocks) for collect_system_metrics() -- every metric it returns must
come from a real query against real seeded rows -- and a MockClient
returning controlled JSON for analyze_system_health(), mirroring
test_research_app_feasibility_offline.py's structure.
"""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

from db import Database, new_id
from registry import BusinessRegistry
from scheduler import TIMESTAMP_FORMAT
from llm_client import MockClient
from tasks.ops_maintenance_review import (
    collect_system_metrics, analyze_system_health, OpsMaintenanceReviewError,
    STUCK_TASK_THRESHOLD_HOURS, STALE_APPROVAL_THRESHOLD_HOURS, STUCK_ORDER_THRESHOLD_HOURS,
)

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_ops_maintenance_review.db")

NOW = datetime(2026, 1, 1, 12, 0, 0)


def _fmt(dt):
    return dt.strftime(TIMESTAMP_FORMAT)


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    biz_id = BusinessRegistry(db).create("Test Co", "opportunity_discovery", "test")
    return db, biz_id


def _mock_client_returning(obj):
    return MockClient(canned_response=json.dumps(obj))


VALID_REPORT = {
    "overall_severity": "warning",
    "findings": [{
        "category": "stuck tasks", "severity": "warning",
        "description": "2 tasks have been queued for over 2 hours.",
        "recommendation": "Check whether an eligible agent is idle for their department.",
    }],
    "confidence_level": "high",
    "summary": "A couple of tasks are stuck; nothing else looks unusual.",
}


def test_collect_system_metrics_flags_a_stuck_task_but_not_a_fresh_one():
    db, biz_id = _setup()
    old_hours = STUCK_TASK_THRESHOLD_HOURS + 1
    db.execute(
        "INSERT INTO tasks (id, business_id, objective, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
        (new_id("task"), biz_id, "Stuck task", _fmt(NOW - timedelta(hours=old_hours))),
    )
    db.execute(
        "INSERT INTO tasks (id, business_id, objective, status, created_at) VALUES (?, ?, ?, 'assigned', ?)",
        (new_id("task"), biz_id, "Fresh task", _fmt(NOW - timedelta(minutes=5))),
    )
    metrics = collect_system_metrics(db, now=NOW)
    assert len(metrics["stuck_tasks"]) == 1
    assert metrics["stuck_tasks"][0]["objective"] == "Stuck task"
    print("PASS: collect_system_metrics flags a genuinely stuck task, not a freshly-queued one")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_system_metrics_flags_a_stale_approval():
    db, biz_id = _setup()
    old_hours = STALE_APPROVAL_THRESHOLD_HOURS + 1
    db.execute(
        "INSERT INTO approvals (id, action_type, business_id, status, created_at) "
        "VALUES (?, 'spend_usd', ?, 'pending', ?)",
        (new_id("appr"), biz_id, _fmt(NOW - timedelta(hours=old_hours))),
    )
    db.execute(
        "INSERT INTO approvals (id, action_type, business_id, status, created_at) "
        "VALUES (?, 'spend_usd', ?, 'pending', ?)",
        (new_id("appr"), biz_id, _fmt(NOW - timedelta(hours=1))),
    )
    metrics = collect_system_metrics(db, now=NOW)
    assert len(metrics["stale_approvals"]) == 1
    print("PASS: collect_system_metrics flags an approval stale for longer than the threshold")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_system_metrics_flags_a_silent_scheduled_job():
    db, biz_id = _setup()
    # interval_seconds=3600 (1h) -> default grace threshold is 2x that = 2h.
    db.execute(
        "INSERT INTO scheduled_jobs (id, business_id, name, objective, interval_seconds, "
        "enabled, next_run_at) VALUES (?, ?, 'Silent job', 'x', 3600, 1, ?)",
        (new_id("job"), biz_id, _fmt(NOW - timedelta(hours=3))),
    )
    db.execute(
        "INSERT INTO scheduled_jobs (id, business_id, name, objective, interval_seconds, "
        "enabled, next_run_at) VALUES (?, ?, 'Healthy job', 'x', 3600, 1, ?)",
        (new_id("job"), biz_id, _fmt(NOW - timedelta(minutes=10))),
    )
    db.execute(
        "INSERT INTO scheduled_jobs (id, business_id, name, objective, interval_seconds, "
        "enabled, next_run_at) VALUES (?, ?, 'Disabled overdue job', 'x', 3600, 0, ?)",
        (new_id("job"), biz_id, _fmt(NOW - timedelta(hours=5))),
    )
    metrics = collect_system_metrics(db, now=NOW)
    names = [j["name"] for j in metrics["silent_scheduled_jobs"]]
    assert names == ["Silent job"], names
    print("PASS: only an ENABLED job overdue by more than its own grace threshold is flagged")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_system_metrics_flags_a_stuck_order():
    db, biz_id = _setup()
    old_hours = STUCK_ORDER_THRESHOLD_HOURS + 1
    db.execute(
        "INSERT INTO orders (id, product_type, topic, customer_email, price_usd_cents, "
        "business_id, status, created_at) VALUES (?, 'research_opportunity', 'x', "
        "'a@example.com', 1900, ?, 'pending_payment', ?)",
        (new_id("ord"), biz_id, _fmt(NOW - timedelta(hours=old_hours))),
    )
    db.execute(
        "INSERT INTO orders (id, product_type, topic, customer_email, price_usd_cents, "
        "business_id, status, created_at) VALUES (?, 'research_opportunity', 'x', "
        "'b@example.com', 1900, ?, 'fulfilled', ?)",
        (new_id("ord"), biz_id, _fmt(NOW - timedelta(hours=old_hours))),
    )
    metrics = collect_system_metrics(db, now=NOW)
    assert len(metrics["stuck_orders"]) == 1
    print("PASS: a stuck pending_payment/paid order is flagged, a fulfilled one is not")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_system_metrics_counts_recent_errors_only():
    db, biz_id = _setup()
    db.execute("INSERT INTO audit_log (actor, action, created_at) VALUES ('x', 'order_email_failed', ?)",
               (_fmt(NOW - timedelta(hours=1)),))
    db.execute("INSERT INTO audit_log (actor, action, created_at) VALUES ('x', 'fulfillment_pass_error', ?)",
               (_fmt(NOW - timedelta(hours=2)),))
    db.execute("INSERT INTO audit_log (actor, action, created_at) VALUES ('x', 'order_fulfilled', ?)",
               (_fmt(NOW - timedelta(hours=1)),))  # not an error
    db.execute("INSERT INTO audit_log (actor, action, created_at) VALUES ('x', 'order_email_failed', ?)",
               (_fmt(NOW - timedelta(hours=48)),))  # outside the 24h window
    metrics = collect_system_metrics(db, now=NOW)
    assert metrics["recent_error_count"] == 2, metrics["recent_error_count"]
    print("PASS: recent_error_count only counts *_failed/*_error actions within the recent window")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_system_metrics_detects_missing_optional_config():
    db, biz_id = _setup()
    with patch.dict(os.environ, {"OWNER_EMAIL": "owner@example.com"}, clear=False):
        os.environ.pop("ALPHAVANTAGE_API_KEY", None)
        metrics = collect_system_metrics(db, now=NOW)
    env_vars_flagged = {m["env_var"] for m in metrics["missing_optional_config"]}
    assert "OWNER_EMAIL" not in env_vars_flagged
    assert "ALPHAVANTAGE_API_KEY" in env_vars_flagged
    print("PASS: a configured optional var is not flagged, an unset one is")
    db.close()
    os.remove(TEST_DB_PATH)


def test_collect_system_metrics_table_row_counts():
    db, biz_id = _setup()
    db.execute(
        "INSERT INTO tasks (id, business_id, objective, status) VALUES (?, ?, 'x', 'queued')",
        (new_id("task"), biz_id),
    )
    metrics = collect_system_metrics(db, now=NOW)
    assert metrics["table_row_counts"]["tasks"] == 1
    assert metrics["table_row_counts"]["orders"] == 0
    print("PASS: table_row_counts reflects real row counts, not placeholders")
    db.close()
    os.remove(TEST_DB_PATH)


def test_analyze_system_health_success():
    client = _mock_client_returning(VALID_REPORT)
    report = analyze_system_health({"stuck_tasks": []}, client)
    assert report["overall_severity"] == "warning"
    assert report["confidence_level"] == "high"
    assert len(report["findings"]) == 1
    print("PASS: analyze_system_health returns a validated report from a well-formed response")


def test_analyze_system_health_rejects_invalid_json():
    client = MockClient(canned_response="not json")
    try:
        analyze_system_health({}, client)
        assert False, "expected OpsMaintenanceReviewError"
    except OpsMaintenanceReviewError:
        pass
    print("PASS: invalid JSON from the model is a hard failure, never a fabricated report")


def test_analyze_system_health_rejects_missing_fields():
    bad = dict(VALID_REPORT)
    del bad["summary"]
    client = _mock_client_returning(bad)
    try:
        analyze_system_health({}, client)
        assert False, "expected OpsMaintenanceReviewError"
    except OpsMaintenanceReviewError:
        pass
    print("PASS: a missing required top-level field is rejected")


def test_analyze_system_health_rejects_invalid_overall_severity():
    bad = dict(VALID_REPORT, overall_severity="catastrophic")
    client = _mock_client_returning(bad)
    try:
        analyze_system_health({}, client)
        assert False, "expected OpsMaintenanceReviewError"
    except OpsMaintenanceReviewError:
        pass
    print("PASS: an invalid overall_severity value is rejected")


def test_analyze_system_health_rejects_invalid_confidence_level():
    bad = dict(VALID_REPORT, confidence_level="certain")
    client = _mock_client_returning(bad)
    try:
        analyze_system_health({}, client)
        assert False, "expected OpsMaintenanceReviewError"
    except OpsMaintenanceReviewError:
        pass
    print("PASS: an invalid confidence_level value is rejected")


def test_analyze_system_health_rejects_findings_not_a_list():
    bad = dict(VALID_REPORT, findings="everything is fine")
    client = _mock_client_returning(bad)
    try:
        analyze_system_health({}, client)
        assert False, "expected OpsMaintenanceReviewError"
    except OpsMaintenanceReviewError:
        pass
    print("PASS: findings that isn't a JSON array is rejected")


def test_analyze_system_health_rejects_a_malformed_finding():
    bad = dict(VALID_REPORT, findings=[{"category": "x", "severity": "warning"}])  # missing fields
    client = _mock_client_returning(bad)
    try:
        analyze_system_health({}, client)
        assert False, "expected OpsMaintenanceReviewError"
    except OpsMaintenanceReviewError:
        pass
    print("PASS: a finding missing required fields is rejected")


def test_analyze_system_health_allows_empty_findings():
    healthy = dict(VALID_REPORT, overall_severity="ok", findings=[])
    client = _mock_client_returning(healthy)
    report = analyze_system_health({}, client)
    assert report["findings"] == []
    print("PASS: an empty findings list (everything healthy) is valid, not an error")


if __name__ == "__main__":
    test_collect_system_metrics_flags_a_stuck_task_but_not_a_fresh_one()
    test_collect_system_metrics_flags_a_stale_approval()
    test_collect_system_metrics_flags_a_silent_scheduled_job()
    test_collect_system_metrics_flags_a_stuck_order()
    test_collect_system_metrics_counts_recent_errors_only()
    test_collect_system_metrics_detects_missing_optional_config()
    test_collect_system_metrics_table_row_counts()
    test_analyze_system_health_success()
    test_analyze_system_health_rejects_invalid_json()
    test_analyze_system_health_rejects_missing_fields()
    test_analyze_system_health_rejects_invalid_overall_severity()
    test_analyze_system_health_rejects_invalid_confidence_level()
    test_analyze_system_health_rejects_findings_not_a_list()
    test_analyze_system_health_rejects_a_malformed_finding()
    test_analyze_system_health_allows_empty_findings()
    print("\nAll ops_maintenance_review.py offline tests passed.")
