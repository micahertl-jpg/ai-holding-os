"""
fulfillment.py — closes the last gap in the storefront loop: a paid
order whose research task has finished sitting there until someone
notices. This background thread (started alongside the scheduler's and
executor's, in api.py's lifespan) polls for orders with status='paid'
whose linked task has reached a terminal state, and either emails the
customer their report (task completed) or marks the order failed
(task failed) — never silently retrying forever, never emailing a
fabricated result. A completed task whose expected result row never
shows up (a data bug elsewhere, not a normal outcome) is retried for
up to BUILD_FAILURE_RETRY_LIMIT passes and then also given up on and
marked 'failed', rather than being logged identically forever with no
terminal state.

Split the same way as scheduler.py/executor.py: `run_once()` is a
pure-enough, directly-testable pass; `run_forever()` is the thin
sleep-loop that can only really be verified by running the server.

NOT IMPLEMENTED: automatic refunds for a failed order. A task that
fails after real payment succeeded currently just marks the order
'failed' and leaves a real_transactions record showing the customer
paid — the owner needs to manually refund via the Stripe dashboard.
Building real refund automation is a deliberate later step, not an
oversight; issuing money back automatically is exactly the kind of
consequential action this project's spec says should default to
requiring human judgment, not be silently automated on day one.
Because that refund step can't be automated, a failed order sends a
best-effort alert email to OWNER_EMAIL (if configured) so it doesn't
just sit there waiting to be noticed in the dashboard's Store Orders
panel -- see _notify_owner_of_failed_order().
"""

import html
import os
import threading

from emailer import send_email, EmailError

TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled")

# Optional: where to alert the owner that an order needs a manual Stripe
# refund (see the module docstring -- refunds are never automated). Unset
# by default, matching every other optional integration in this codebase
# (e.g. STORE_BUSINESS_ID) -- fulfillment must keep working for every
# other order even if this is never configured.
OWNER_EMAIL = os.environ.get("OWNER_EMAIL")

# A completed task whose expected result row is missing (see
# _build_report_email) indicates a real data problem, not a transient
# one -- it will never fix itself by being retried. Without a cap, that
# order stays 'paid' and gets re-logged identically on every fulfillment
# pass forever, with no terminal state and no owner alert, ever. After
# this many passes the order is given up on and marked 'failed' like any
# other unfulfillable order.
BUILD_FAILURE_RETRY_LIMIT = int(os.environ.get("FULFILLMENT_BUILD_FAILURE_RETRY_LIMIT", "5"))


def _build_report_email(order, task, db):
    """Returns (subject, html_body) for a completed order, pulling the
    real saved assessment row (never re-deriving/re-summarizing it —
    the email must say exactly what was actually produced).

    Every value interpolated into html_body is passed through
    html.escape() first. `topic`/`concept` traces back to unauthenticated,
    customer-submitted input at checkout (api.py's /store/checkout takes
    it with no sanitization beyond .strip()) and the other fields are
    model output that could itself echo/quote that same input — without
    escaping, a customer could submit e.g. `<a href="...">` as their
    "topic" and have it rendered as live HTML in a real transactional
    email sent, from this business's verified sending domain, to
    whatever `customer_email` they also supplied (not necessarily their
    own address). The dashboard already escapes this same data
    (dashboard-render.js's escapeHtml) before rendering it — this brings
    the email path in line with that, closing a real gap between the
    two."""
    if order["product_type"] == "research_opportunity":
        row = db.query_one("SELECT * FROM opportunities WHERE task_id=?", (task["id"],))
        if not row:
            raise RuntimeError(
                f"task {task['id']} is completed but no opportunities row exists for it "
                f"— cannot build a real report from nothing"
            )
        subject = f"Your Opportunity Research Report: {row['topic']}"
        fields = [
            ("Market size", row["market_size"]),
            ("Competition", row["competition"]),
            ("Startup cost", row["startup_cost"]),
            ("Revenue potential", row["revenue_potential"]),
            ("Time to market", row["time_to_market"]),
            ("Operational complexity", row["operational_complexity"]),
            ("Legal/regulatory risk", row["legal_regulatory_risk"]),
            ("Capital requirements", row["capital_requirements"]),
            ("Downside risk", row["downside_risk"]),
        ]
        confidence = row["confidence_level"]
        summary = row["summary"]
        topic = row["topic"]
    elif order["product_type"] == "research_roblox_trend":
        row = db.query_one("SELECT * FROM roblox_trends WHERE task_id=?", (task["id"],))
        if not row:
            raise RuntimeError(
                f"task {task['id']} is completed but no roblox_trends row exists for it "
                f"— cannot build a real report from nothing"
            )
        subject = f"Your Roblox Concept Research Report: {row['concept']}"
        fields = [
            ("Player demand signals", row["player_demand_signals"]),
            ("Competition level", row["competition_level"]),
            ("Build complexity", row["build_complexity"]),
            ("Target audience", row["target_audience"]),
            ("Monetization fit", row["monetization_fit"]),
            ("Estimated dev time", row["estimated_dev_time"]),
            ("Similar successful games", row["similar_successful_games"]),
            ("Risk factors", row["risk_factors"]),
        ]
        confidence = row["confidence_level"]
        summary = row["summary"]
        topic = row["concept"]
    elif order["product_type"] == "research_app_feasibility":
        row = db.query_one("SELECT * FROM app_feasibility_assessments WHERE task_id=?", (task["id"],))
        if not row:
            raise RuntimeError(
                f"task {task['id']} is completed but no app_feasibility_assessments row "
                f"exists for it — cannot build a real report from nothing"
            )
        subject = f"Your App Feasibility Report: {row['concept']}"
        fields = [
            ("Platform recommendation", row["platform_recommendation"]),
            ("Suggested tech stack", row["suggested_tech_stack"]),
            ("Complexity tier", row["complexity_tier"]),
            ("Estimated timeline", row["estimated_timeline"]),
            ("Estimated cost range", row["estimated_cost_range"]),
            ("MVP feature scope", row["mvp_feature_scope"]),
            ("Key technical risks", row["key_technical_risks"]),
            ("Similar existing apps", row["similar_existing_apps"]),
        ]
        confidence = row["confidence_level"]
        summary = row["summary"]
        topic = row["concept"]
    elif order["product_type"] == "research_real_estate":
        row = db.query_one("SELECT * FROM real_estate_assessments WHERE task_id=?", (task["id"],))
        if not row:
            raise RuntimeError(
                f"task {task['id']} is completed but no real_estate_assessments row "
                f"exists for it — cannot build a real report from nothing"
            )
        subject = f"Your Real Estate Investment Research Report: {row['property_or_market']}"
        fields = [
            ("Market trend", row["market_trend"]),
            ("Comparable properties", row["comparable_properties"]),
            ("Estimated rental yield", row["estimated_rental_yield"]),
            ("Price trend assessment", row["price_trend_assessment"]),
            ("Risk factors", row["risk_factors"]),
        ]
        confidence = row["confidence_level"]
        summary = row["summary"]
        topic = row["property_or_market"]
    else:
        raise ValueError(f"unknown product_type: {order['product_type']!r}")

    rows_html = "".join(
        f"<tr><td style='padding:6px 12px;font-weight:bold;vertical-align:top;'>{html.escape(label)}</td>"
        f"<td style='padding:6px 12px;'>{html.escape(value) if value else ''}</td></tr>"
        for label, value in fields
    )
    html_body = f"""
    <div style="font-family:sans-serif;max-width:600px;">
      <h2>Research Report: {html.escape(topic)}</h2>
      <p><strong>Confidence level: {html.escape(confidence or '')}</strong></p>
      <p>{html.escape(summary) if summary else ''}</p>
      <table style="border-collapse:collapse;width:100%;">{rows_html}</table>
      <p style="color:#666;font-size:12px;margin-top:24px;">
        This report was produced by an AI research process. Every field above is an
        estimate, not a guarantee — nothing here is financial, legal, or professional
        advice. Treat it as a starting point for your own judgment, not a substitute
        for it.
      </p>
    </div>
    """
    return subject, html_body


def _notify_owner_of_failed_order(order, reason, db):
    """Best-effort alert that an order needs a manual Stripe refund. The
    order is already correctly marked 'failed' in the database by the
    time this runs, so a missing OWNER_EMAIL or a broken Resend send
    must never crash the fulfillment loop or affect that outcome for
    this order (or any other) -- it's only ever logged via the audit
    trail, same as every other failure mode in this module."""
    if not OWNER_EMAIL:
        return
    subject = f"[Action needed] Order failed after payment: {order['topic']}"
    body = f"""
    <div style="font-family:sans-serif;max-width:600px;">
      <h2>An order needs a manual refund</h2>
      <p>Order <strong>{html.escape(order['id'])}</strong>
        (topic: {html.escape(order['topic'])}, customer:
        {html.escape(order['customer_email'])}) was paid, but {html.escape(reason)}.</p>
      <p>This system does not automate refunds -- the customer was already
        charged and needs a manual refund via the Stripe dashboard.</p>
    </div>
    """
    try:
        send_email(OWNER_EMAIL, subject, body)
        db.audit("fulfillment", "owner_failed_order_alert_sent", "order", order["id"], {})
    except EmailError as e:
        db.audit("fulfillment", "owner_failed_order_alert_failed", "order", order["id"],
                  {"error": str(e)})


def run_once(db, client=None):
    """One fulfillment pass. `client` is unused currently (kept for
    signature symmetry with scheduler.run_once/executor.run_once and
    in case a future report format needs an LLM call to compose it);
    ignored for now. Returns the list of (order_id, outcome) processed."""
    paid_orders = db.query(
        "SELECT * FROM orders WHERE status='paid' AND task_id IS NOT NULL"
    )
    outcomes = []
    for order in paid_orders:
        task = db.query_one("SELECT * FROM tasks WHERE id=?", (order["task_id"],))
        if not task or task["status"] not in TERMINAL_TASK_STATUSES:
            continue  # still running — check again next pass

        if task["status"] != "completed":
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (order["id"],))
            db.audit("fulfillment", "order_fulfillment_failed", "order", order["id"],
                      {"reason": f"linked task ended in status={task['status']}"})
            _notify_owner_of_failed_order(
                order, f"its research task ended in status '{task['status']}' instead "
                       f"of completing", db)
            outcomes.append((order["id"], "failed"))
            continue

        try:
            subject, html_body = _build_report_email(order, task, db)
        except (RuntimeError, ValueError) as e:
            # A data problem (e.g. the assessment row is missing), not
            # a delivery problem — will keep failing identically every
            # pass. Logged every time either way, but only given up on
            # (marked 'failed' + owner alerted) once it's clearly not
            # going to resolve itself, rather than being retried forever
            # with no terminal state and no one ever told.
            db.audit("fulfillment", "order_report_build_failed", "order", order["id"],
                      {"error": str(e)})
            prior_failures = db.query_one(
                "SELECT COUNT(*) as c FROM audit_log WHERE action='order_report_build_failed' "
                "AND target_id=?", (order["id"],),
            )["c"]
            if prior_failures >= BUILD_FAILURE_RETRY_LIMIT:
                db.execute("UPDATE orders SET status='failed' WHERE id=?", (order["id"],))
                db.audit("fulfillment", "order_fulfillment_failed", "order", order["id"],
                          {"reason": f"report data never became available after "
                                     f"{prior_failures} attempts: {e}"})
                _notify_owner_of_failed_order(
                    order, f"its report could not be generated after {prior_failures} "
                           f"attempts ({e})", db)
                outcomes.append((order["id"], f"failed: build_failed_permanently: {e}"))
            else:
                outcomes.append((order["id"], f"build_failed: {e}"))
            continue

        try:
            send_email(order["customer_email"], subject, html_body)
        except EmailError as e:
            # Leave status='paid' so the next pass retries — a failed
            # SEND (network blip, Resend outage) shouldn't permanently
            # strand a customer who already paid.
            db.audit("fulfillment", "order_email_failed", "order", order["id"],
                      {"error": str(e)})
            outcomes.append((order["id"], f"email_failed: {e}"))
            continue

        db.execute(
            "UPDATE orders SET status='fulfilled', fulfilled_at=datetime('now') WHERE id=?",
            (order["id"],),
        )
        db.audit("fulfillment", "order_fulfilled", "order", order["id"], {})
        outcomes.append((order["id"], "fulfilled"))

    return outcomes


def run_forever(db, poll_interval_seconds: float, stop_event: threading.Event):
    """Thin wrapper around run_once(), same pattern as scheduler.py/
    executor.py's run_forever(): sleep, run a pass, repeat, until
    stop_event is set. A bad pass is logged and the loop continues."""
    while not stop_event.is_set():
        try:
            run_once(db)
        except Exception as e:
            try:
                db.audit("fulfillment", "fulfillment_pass_error", details={"error": str(e)})
            except Exception:
                pass
        stop_event.wait(poll_interval_seconds)
