"""
tasks/overseer_review.py — the Overseer: a scheduled job that calls the
owner's real phone (via caller.py / Twilio) the moment ANY approval is
pending, so something that needs a decision doesn't just sit quietly in
the dashboard or an email digest.

Deliberately code-only, no model call: deciding "should I call" is a
database lookup (any pending approval not yet called about) plus a
clock check (quiet hours), not a judgment call a model would improve --
same reasoning as tasks/owner_digest.py, and this task type is
registered in executor.py's HANDLERS with cost_arc always 0.0.

Two things keep this from blowing up the owner's phone:
  - Dedup ("one call per issue"): once an approval has a row in
    overseer_calls, it is never called about again while still pending
    -- the owner already knows; a second call about the same unresolved
    approval is noise, not new information. A NEW approval still rings
    immediately, even seconds after a previous call.
  - Quiet hours: an optional [start, end) local-time window
    (OVERSEER_QUIET_HOURS_START/_END, read by executor.py and passed in
    here) during which no call is placed at all, even if approvals are
    waiting -- both env vars must be set for this to apply; unset means
    no quiet hours, never a surprise default.

Same split as owner_digest.py: find_uncalled_pending_approvals() and
is_quiet_hours() are pure enough to unit-test directly, and
format_call_message() is pure string formatting with no I/O. All three
are owner-notification channels (the digest emails a full report once a
day with real-world revenue/trading numbers; this page calls once per
NEW approval) so calling it its own vertical, not folding it into
owner_digest, keeps "something needs you right now" from waiting on the
digest's own daily/large interval.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from db import new_id

_RISK_RANK = {"low": 1, "medium": 2, "high": 3}


def is_quiet_hours(now: datetime, quiet_start: str, quiet_end: str, tz_name: str) -> bool:
    """Returns True if `now` falls inside the [quiet_start, quiet_end)
    window in tz_name's local time, where quiet_start/quiet_end are
    required "HH:MM" strings -- handles a window that crosses midnight
    (e.g. "22:00" -> "07:00") correctly. `now` may be naive (treated as
    UTC, matching every other timestamp in this codebase) or
    timezone-aware."""
    tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(tz)

    start_h, start_m = (int(x) for x in quiet_start.split(":"))
    end_h, end_m = (int(x) for x in quiet_end.split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    now_minutes = local_now.hour * 60 + local_now.minute

    if start_minutes == end_minutes:
        return False  # a zero-width window is never active
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes  # crosses midnight


def find_uncalled_pending_approvals(db):
    """Every pending approval that has never triggered a call yet --
    the dedup half of "one call per issue": an approval that already
    has a row in overseer_calls is skipped no matter how many more
    review cycles pass while it's still pending."""
    return db.query(
        "SELECT a.id, a.action_type, a.description, a.risk_level, a.created_at, "
        "b.name as business_name FROM approvals a LEFT JOIN businesses b ON b.id = a.business_id "
        "WHERE a.status='pending' AND NOT EXISTS "
        "(SELECT 1 FROM overseer_calls oc WHERE oc.approval_id = a.id) "
        "ORDER BY a.created_at ASC"
    )


def format_call_message(approvals) -> str:
    """Returns the spoken message text (Twilio <Say>). Pure string
    formatting, no I/O. The owner can't read on a phone call, so this
    names a count and the single highest-risk item rather than listing
    every approval -- full detail is always "check your dashboard"."""
    if len(approvals) == 1:
        a = approvals[0]
        business = a["business_name"] or "the system"
        return (
            f"This is your A I Holding Company Overseer. {business} has a {a['risk_level']} "
            f"risk approval pending: {a['action_type']}. Please check your dashboard to review it."
        )
    highest = max(approvals, key=lambda a: _RISK_RANK.get(a["risk_level"], 0))
    business = highest["business_name"] or "the system"
    return (
        f"This is your A I Holding Company Overseer. There are {len(approvals)} approvals "
        f"pending your decision, including a {highest['risk_level']} risk request from "
        f"{business}. Please check your dashboard to review them."
    )


def run_overseer_review(db, caller, to_number: str, quiet_start: str = None,
                         quiet_end: str = None, tz_name: str = None, now: datetime = None) -> dict:
    """Orchestrates one review pass: skip entirely during quiet hours
    (if both quiet_start/quiet_end are configured), skip if there's
    nothing new to call about, otherwise place one real call covering
    every uncalled pending approval and record each as called. Returns
    a dict describing what happened -- never raises for "nothing to do",
    only for a real failure placing the call (caller.CallError
    propagates unchanged, matching every other real-integration call
    site in this codebase)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    if quiet_start and quiet_end and is_quiet_hours(now, quiet_start, quiet_end, tz_name or "UTC"):
        return {"called": False, "reason": "quiet_hours", "approval_count": 0, "call_sid": None}

    approvals = find_uncalled_pending_approvals(db)
    if not approvals:
        return {"called": False, "reason": "no_new_approvals", "approval_count": 0, "call_sid": None}

    message = format_call_message(approvals)
    call = caller.place_call(to_number, message)
    call_sid = call.get("sid")

    for a in approvals:
        db.execute(
            "INSERT INTO overseer_calls (id, approval_id, call_sid) VALUES (?, ?, ?)",
            (new_id("ovc"), a["id"], call_sid),
        )

    return {"called": True, "reason": None, "approval_count": len(approvals), "call_sid": call_sid}
