"""
tasks/owner_digest.py — the "personal assistant" vertical: a scheduled
job that compiles and emails the owner a digest across every business,
so they get a "here's what needs you" briefing without opening the
dashboard.

Deliberately code-only, no model call: every number here is either a
real query result or a direct restatement of an already-synthesized
report (the latest ops_maintenance_reports row, itself produced with a
model call in tasks/ops_maintenance_review.py) -- there is no judgment
call in composing a digest that would benefit from asking a model to
re-decide it, so this task type is registered in executor.py's
HANDLERS with cost_arc always 0.0.

Same split as tasks/ops_maintenance_review.py: collect_owner_digest()
is pure enough to unit-test directly against a real (SQLite) Database
with hand-seeded fixtures, and format_digest_email() is pure string
formatting with no I/O of its own.
"""

import html
from datetime import datetime, timedelta, timezone

from scheduler import TIMESTAMP_FORMAT
from tasks.ops_maintenance_review import age_hours, parse_db_timestamp

# How far back a digest looks when there is no previous completed
# owner_digest task to anchor the window to (i.e. the very first
# digest ever sent) -- 24h matches the job's own default cadence.
DEFAULT_LOOKBACK_HOURS = 24.0

# table -> human-readable vertical name, for the "new research
# completed" section. Deliberately the same four research verticals
# api.py's LaunchBusinessRequest-based launch endpoints know about.
RESEARCH_TABLES = {
    "opportunities": "Opportunity Discovery",
    "roblox_trends": "Roblox Game Development",
    "app_feasibility_assessments": "App Development Feasibility",
    "real_estate_assessments": "Real Estate Investment Research",
}


def find_window_start(db, now: datetime = None):
    """Returns the created_at of the most recently COMPLETED
    owner_digest task, so each digest covers exactly the period since
    the last one actually sent -- or None (collect_owner_digest()
    then falls back to DEFAULT_LOOKBACK_HOURS) if this is the first
    digest ever sent, or every previous attempt failed (e.g.
    OWNER_EMAIL wasn't configured yet)."""
    last = db.query_one(
        "SELECT created_at FROM tasks WHERE task_type='owner_digest' AND status='completed' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    return parse_db_timestamp(last["created_at"]) if last else None


def collect_owner_digest(db, now: datetime = None, window_start: datetime = None) -> dict:
    """Real, code-computed digest content -- every field comes from an
    actual query against the actual database, never from a model."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    window_start = window_start or (now - timedelta(hours=DEFAULT_LOOKBACK_HOURS))
    window_start_str = window_start.strftime(TIMESTAMP_FORMAT)

    pending_approvals = []
    for row in db.query(
        "SELECT a.id, a.action_type, a.description, a.risk_level, a.created_at, "
        "b.name as business_name FROM approvals a LEFT JOIN businesses b ON b.id = a.business_id "
        "WHERE a.status='pending' ORDER BY a.created_at ASC"
    ):
        pending_approvals.append({
            "id": row["id"], "action_type": row["action_type"], "description": row["description"],
            "business_name": row["business_name"] or "(system)", "risk_level": row["risk_level"],
            "age_hours": round(age_hours(now, row["created_at"]), 1),
        })

    latest_ops_report = db.query_one(
        "SELECT overall_severity, summary, created_at FROM ops_maintenance_reports "
        "ORDER BY created_at DESC LIMIT 1"
    )
    ops_health = None
    if latest_ops_report:
        ops_health = {
            "severity": latest_ops_report["overall_severity"],
            "summary": latest_ops_report["summary"],
            "age_hours": round(age_hours(now, latest_ops_report["created_at"]), 1),
        }

    revenue_usd_cents = db.query_one(
        "SELECT COALESCE(SUM(amount_usd_cents),0) as total FROM real_transactions "
        "WHERE direction='in' AND occurred_at >= ?", (window_start_str,),
    )["total"]

    new_research = {}
    for table, label in RESEARCH_TABLES.items():
        count = db.query_one(
            f"SELECT COUNT(*) as c FROM {table} WHERE created_at >= ?", (window_start_str,),
        )["c"]
        if count:
            new_research[label] = count

    trading_portfolios = []
    for portfolio in db.query(
        "SELECT p.id, p.starting_cash_usd, b.name as business_name FROM paper_portfolios p "
        "LEFT JOIN businesses b ON b.id = p.business_id"
    ):
        latest_snapshot = db.query_one(
            "SELECT equity_usd, created_at FROM trading_snapshots WHERE portfolio_id=? "
            "ORDER BY created_at DESC LIMIT 1", (portfolio["id"],),
        )
        if not latest_snapshot:
            continue  # no cycle has run yet -- nothing real to report
        trading_portfolios.append({
            "business_name": portfolio["business_name"] or "(unknown business)",
            "starting_cash_usd": portfolio["starting_cash_usd"],
            "equity_usd": latest_snapshot["equity_usd"],
            "pnl_usd": round(latest_snapshot["equity_usd"] - portfolio["starting_cash_usd"], 2),
        })

    return {
        "generated_at": now.strftime(TIMESTAMP_FORMAT),
        "window_start": window_start_str,
        "pending_approvals": pending_approvals,
        "ops_health": ops_health,
        "revenue_usd_cents": revenue_usd_cents,
        "new_research": new_research,
        "trading_portfolios": trading_portfolios,
    }


def format_digest_email(digest: dict) -> tuple:
    """Returns (subject, html_body). Pure string formatting, no I/O --
    trivially unit-testable against a hand-built digest dict."""
    approvals = digest["pending_approvals"]
    subject = (
        f"Daily digest: {len(approvals)} approval(s) awaiting your decision"
        if approvals else "Daily digest: nothing awaiting your decision"
    )

    parts = ['<div style="font-family:sans-serif;max-width:600px;">', "<h2>Owner Daily Digest</h2>"]

    if approvals:
        parts.append(f"<h3>{len(approvals)} approval(s) awaiting your decision</h3><ul>")
        for a in approvals:
            parts.append(
                f'<li>[{html.escape(a["risk_level"])}] {html.escape(a["business_name"])}: '
                f'{html.escape(a["description"])} ({a["age_hours"]}h old)</li>'
            )
        parts.append("</ul>")
    else:
        parts.append("<p>No approvals awaiting your decision.</p>")

    if digest["ops_health"]:
        oh = digest["ops_health"]
        parts.append(
            f'<h3>System health: {html.escape(oh["severity"])}</h3>'
            f'<p>{html.escape(oh["summary"])} (as of {oh["age_hours"]}h ago)</p>'
        )
    else:
        parts.append("<h3>System health: no ops review has run yet</h3>")

    revenue_usd = digest["revenue_usd_cents"] / 100.0
    parts.append(f"<h3>Revenue collected since last digest: ${revenue_usd:.2f}</h3>")

    if digest["new_research"]:
        parts.append("<h3>New research completed since last digest</h3><ul>")
        for label, count in digest["new_research"].items():
            parts.append(f"<li>{html.escape(label)}: {count}</li>")
        parts.append("</ul>")

    if digest["trading_portfolios"]:
        parts.append("<h3>Trading portfolios</h3><ul>")
        for p in digest["trading_portfolios"]:
            sign = "+" if p["pnl_usd"] >= 0 else ""
            parts.append(
                f'<li>{html.escape(p["business_name"])}: ${p["equity_usd"]:.2f} equity '
                f'({sign}${p["pnl_usd"]:.2f} vs ${p["starting_cash_usd"]:.2f} starting cash, '
                f"as of its last cycle)</li>"
            )
        parts.append("</ul>")

    parts.append(
        '<p style="color:#666;font-size:12px;">See the dashboard for full detail. This digest '
        "never acts on your behalf -- everything above is a real, already-recorded number or "
        "report.</p></div>"
    )
    return subject, "".join(parts)
