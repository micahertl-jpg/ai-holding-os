"""
tasks/ops_maintenance_review.py — the Ops/Maintenance vertical, per the
project spec's sixth planned business vertical. Unlike the other four
(Opportunity Discovery, Roblox Game Development, Automated Stock
Trading, App Development Feasibility), this one has no customer and no
storefront product: it watches THIS system's own infrastructure and
produces maintenance recommendations for the owner.

Same safety posture as every other research task type in this
codebase: it only ever RECOMMENDS. It never restarts anything, never
deletes stale data, never changes configuration, never touches code —
strictly read-only introspection followed by a synthesized report. Per
the project spec's bias toward human judgment on consequential
actions, turning any of these findings into an actual fix is a
decision for the owner, not something this task does on its own.

Same pattern as tasks/trading_strategy_review.py: collect_system_metrics()
computes REAL numbers from the database (never asked of the model,
never invented by it), and analyze_system_health() sends those real
numbers to the model and asks it to synthesize a prioritized,
human-readable report from them. Invalid or incomplete JSON from the
model is a hard failure, never a fabricated fallback.
"""

import json
import os
from datetime import datetime, timezone

from scheduler import TIMESTAMP_FORMAT

# How overdue a task/approval/order needs to be before it's worth
# flagging. These are deliberately generous defaults (a task queued for
# a few minutes is normal; queued for hours usually means something is
# actually stuck) -- all overridable via env var for a deployment with
# different normal operating rhythms.
STUCK_TASK_THRESHOLD_HOURS = float(os.environ.get("OPS_STUCK_TASK_THRESHOLD_HOURS", "2"))
STALE_APPROVAL_THRESHOLD_HOURS = float(os.environ.get("OPS_STALE_APPROVAL_THRESHOLD_HOURS", "24"))
STUCK_ORDER_THRESHOLD_HOURS = float(os.environ.get("OPS_STUCK_ORDER_THRESHOLD_HOURS", "24"))

# A scheduled job is "silent" once it's this many of its OWN intervals
# overdue past its next_run_at -- relative to its own cadence, not a
# fixed number, since a daily job and a 30-second job have very
# different normal jitter. The floor keeps a very-short-interval job
# from being flagged after a trivially small delay.
SILENT_JOB_GRACE_MULTIPLIER = float(os.environ.get("OPS_SILENT_JOB_GRACE_MULTIPLIER", "2"))
SILENT_JOB_MIN_GRACE_HOURS = float(os.environ.get("OPS_SILENT_JOB_MIN_GRACE_HOURS", "1"))

RECENT_ERROR_WINDOW_HOURS = float(os.environ.get("OPS_RECENT_ERROR_WINDOW_HOURS", "24"))
# Bounds how many recent audit_log rows are scanned for the error tally
# -- avoids pulling the entire (unbounded-growth) audit log into memory
# on every review; id is a sequential AUTOINCREMENT, so "most recent N"
# is exact, not an approximation.
RECENT_AUDIT_LOG_SCAN_LIMIT = 500

# Tables worth watching for runaway growth -- a deliberately small,
# hand-picked list of tables that grow with real usage (append-only or
# near-append-only), not every table in the schema.
GROWTH_WATCH_TABLES = (
    "tasks", "audit_log", "arc_ledger", "orders", "approvals",
    "opportunities", "roblox_trends", "app_feasibility_assessments",
)

# Known optional integrations this codebase supports -- each entry is
# (env var name, one-line description of what's missing without it).
# Deliberately excludes anything REQUIRED (ANTHROPIC_API_KEY,
# DASHBOARD_USERNAME/PASSWORD, DATABASE_URL) -- those already fail
# loudly on their own everywhere they're used, so flagging them again
# here would be redundant noise, not a genuine finding.
KNOWN_OPTIONAL_CONFIG = [
    ("OWNER_EMAIL", "no email alert when a storefront order fails or an "
                     "ops review finds something serious -- only visible on the dashboard"),
    ("ALPHAVANTAGE_API_KEY", "Automated Stock Trading cycles fail loudly instead of trading"),
    ("STRIPE_SECRET_KEY", "the storefront cannot accept any real payments"),
    ("STRIPE_WEBHOOK_SECRET", "the storefront cannot accept any real payments"),
    ("STORE_BUSINESS_ID", "the storefront cannot accept any real payments"),
    ("PUBLIC_BASE_URL", "the storefront cannot accept any real payments"),
    ("RESEND_API_KEY", "no storefront order or owner alert email can ever be sent"),
    ("RESEND_FROM_EMAIL", "no storefront order or owner alert email can ever be sent"),
]

REQUIRED_FIELDS = ["overall_severity", "findings", "confidence_level", "summary"]
VALID_SEVERITIES = {"ok", "info", "warning", "critical"}
VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}

SYSTEM_PROMPT = """You are a systems-maintenance analyst reviewing an AI holding company's own \
internal infrastructure -- NOT a customer or a business the company runs. You will be given \
REAL, already-computed metrics about the system's own database and background jobs -- never \
recompute, second-guess, or invent a number that wasn't given to you.

Rules:
- You are producing RECOMMENDATIONS ONLY. Never claim to have fixed, changed, restarted, or \
deleted anything -- you have no ability to act, only to report and recommend what a human \
should look at.
- overall_severity must be exactly one of "ok" (nothing needs attention), "info" (worth knowing, \
not urgent), "warning" (should be looked at soon), or "critical" (needs prompt attention) -- \
choose the highest severity that applies to any individual finding.
- findings must be a JSON array (empty if truly nothing to report -- do not invent a finding just \
to have something to say). Each finding must have: "category" (a short label, e.g. "stuck tasks", \
"database growth", "missing config"), "severity" (same four values as overall_severity), \
"description" (what the real data shows, referencing the actual numbers given), and \
"recommendation" (a concrete, specific next step -- never vague advice like "monitor this").
- Missing optional configuration is at most "info" severity, never "warning"/"critical" -- an \
unconfigured optional integration is an expected, normal state for many deployments, not a fault.
- confidence_level (top-level, about this report itself) must be "low", "medium", or "high", \
reflecting how much the given metrics actually support your conclusions.
- summary must be 1-3 plain-language sentences a busy owner can read in five seconds.

Respond with ONLY a single JSON object (no markdown fences, no prose before or after): \
{"overall_severity": "ok"|"info"|"warning"|"critical", "findings": \
[{"category": "...", "severity": "ok"|"info"|"warning"|"critical", "description": "...", \
"recommendation": "..."}], "confidence_level": "low"|"medium"|"high", "summary": "..."}"""


class OpsMaintenanceReviewError(Exception):
    pass


def _parse_db_timestamp(value):
    """created_at/next_run_at/etc. come back as a naive UTC string from
    SQLite (TEXT columns) but a real timezone-aware datetime from
    Postgres (TIMESTAMPTZ columns) via psycopg2 -- this normalizes both
    to a naive UTC datetime so age-in-hours math works identically on
    either backend, the same cross-backend concern db.py's `?`->`%s`
    translation layer exists for elsewhere."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    return datetime.strptime(value, TIMESTAMP_FORMAT)


def _age_hours(now, value):
    return (now - _parse_db_timestamp(value)).total_seconds() / 3600.0


def collect_system_metrics(db, now: datetime = None) -> dict:
    """Real, code-computed metrics about this system's own health --
    every number here comes from an actual query against the actual
    database, never from the model. Pure enough to unit-test directly
    against a real (SQLite) Database with hand-seeded fixtures."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    stuck_tasks = []
    for row in db.query("SELECT id, objective, status, created_at FROM tasks "
                         "WHERE status IN ('queued','assigned')"):
        age = _age_hours(now, row["created_at"])
        if age >= STUCK_TASK_THRESHOLD_HOURS:
            stuck_tasks.append({"id": row["id"], "objective": row["objective"],
                                 "status": row["status"], "age_hours": round(age, 1)})

    stale_approvals = []
    for row in db.query("SELECT id, created_at FROM approvals WHERE status='pending'"):
        age = _age_hours(now, row["created_at"])
        if age >= STALE_APPROVAL_THRESHOLD_HOURS:
            stale_approvals.append({"id": row["id"], "age_hours": round(age, 1)})

    silent_scheduled_jobs = []
    for row in db.query("SELECT id, name, interval_seconds, next_run_at FROM scheduled_jobs "
                         "WHERE enabled=1"):
        overdue = _age_hours(now, row["next_run_at"])
        threshold = max(SILENT_JOB_MIN_GRACE_HOURS,
                         (row["interval_seconds"] / 3600.0) * SILENT_JOB_GRACE_MULTIPLIER)
        if overdue >= threshold:
            silent_scheduled_jobs.append({"id": row["id"], "name": row["name"],
                                           "hours_overdue": round(overdue, 1)})

    stuck_orders = []
    for row in db.query("SELECT id, status, created_at FROM orders "
                         "WHERE status IN ('pending_payment','paid')"):
        age = _age_hours(now, row["created_at"])
        if age >= STUCK_ORDER_THRESHOLD_HOURS:
            stuck_orders.append({"id": row["id"], "status": row["status"], "age_hours": round(age, 1)})

    table_row_counts = {
        table: db.query_one(f"SELECT COUNT(*) as c FROM {table}")["c"]
        for table in GROWTH_WATCH_TABLES
    }

    recent_rows = db.query(
        f"SELECT action, created_at FROM audit_log ORDER BY id DESC LIMIT {RECENT_AUDIT_LOG_SCAN_LIMIT}"
    )
    recent_error_count = sum(
        1 for row in recent_rows
        if _age_hours(now, row["created_at"]) <= RECENT_ERROR_WINDOW_HOURS
        and ("_failed" in row["action"] or "_error" in row["action"])
    )

    missing_optional_config = [
        {"env_var": name, "impact": impact}
        for name, impact in KNOWN_OPTIONAL_CONFIG
        if not os.environ.get(name)
    ]

    return {
        "collected_at": now.strftime(TIMESTAMP_FORMAT),
        "stuck_tasks": stuck_tasks,
        "stale_approvals": stale_approvals,
        "silent_scheduled_jobs": silent_scheduled_jobs,
        "stuck_orders": stuck_orders,
        "table_row_counts": table_row_counts,
        "recent_error_count": recent_error_count,
        "recent_error_window_hours": RECENT_ERROR_WINDOW_HOURS,
        "missing_optional_config": missing_optional_config,
    }


def analyze_system_health(metrics: dict, client) -> dict:
    """One real LLM call over the real metrics dict from
    collect_system_metrics(). Returns {"overall_severity": ..., \
    "findings": [...], "confidence_level": ..., "summary": ...}. Raises
    OpsMaintenanceReviewError on any structurally invalid response --
    same hard-failure-never-fabricate posture as every other research
    task type in this codebase."""
    user_content = (
        "Real system metrics (computed in code, not by you):\n\n"
        f"{json.dumps(metrics, indent=2)}"
    )
    raw_response = client.complete(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=1200,
    )

    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise OpsMaintenanceReviewError(
            f"model did not return valid JSON: {e}. Raw response: {raw_response[:300]}"
        ) from e

    missing = [f for f in REQUIRED_FIELDS if f not in parsed]
    if missing:
        raise OpsMaintenanceReviewError(
            f"model's JSON is missing required fields: {missing}. Got keys: {list(parsed.keys())}"
        )
    if parsed["overall_severity"] not in VALID_SEVERITIES:
        raise OpsMaintenanceReviewError(
            f"overall_severity must be one of {sorted(VALID_SEVERITIES)}, "
            f"got: {parsed['overall_severity']!r}"
        )
    if parsed["confidence_level"] not in VALID_CONFIDENCE_LEVELS:
        raise OpsMaintenanceReviewError(
            f"confidence_level must be one of {sorted(VALID_CONFIDENCE_LEVELS)}, "
            f"got: {parsed['confidence_level']!r}"
        )
    if not isinstance(parsed["findings"], list):
        raise OpsMaintenanceReviewError(
            f"findings must be a JSON array, got: {type(parsed['findings']).__name__}"
        )
    for i, finding in enumerate(parsed["findings"]):
        finding_missing = [f for f in ("category", "severity", "description", "recommendation")
                            if f not in finding]
        if finding_missing:
            raise OpsMaintenanceReviewError(
                f"finding[{i}] is missing required fields: {finding_missing}"
            )
        if finding["severity"] not in VALID_SEVERITIES:
            raise OpsMaintenanceReviewError(
                f"finding[{i}] severity must be one of {sorted(VALID_SEVERITIES)}, "
                f"got: {finding['severity']!r}"
            )

    return {
        "overall_severity": parsed["overall_severity"],
        "findings": parsed["findings"],
        "confidence_level": parsed["confidence_level"],
        "summary": str(parsed["summary"]),
    }
