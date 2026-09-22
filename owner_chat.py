"""
owner_chat.py — the conversational layer: the owner types a free-text
question and gets a real answer, without opening the dashboard.

Deliberately v1-scoped to READ-ONLY questions. The one LLM call this
module makes never sees real business data and never decides what to
say to the owner -- it only classifies the message into one of a FIXED
set of intents (strict JSON, same discipline as every other task type
in this codebase: invalid/unrecognized output is a hard failure, never
guessed past). Once classified, CODE (never the model) runs the real
query and composes the natural-language answer from the real result --
same "never invent a number" rule as tasks/owner_digest.py and
tasks/ops_maintenance_review.py, just applied to a chat turn instead of
a scheduled report.

There is deliberately NO intent here that takes an action (launching a
business, approving something, moving money, deleting anything) --
classify_intent()'s system prompt explicitly tells the model it has no
ability to act, and an action-shaped request is routed to "unclear"
along with everything else the fixed intent set doesn't cover. Turning
a recognized intent into a real action (e.g. "launch the pet grooming
idea") is a deliberate, separate, later decision -- it needs its own
confirmation/safety design (at minimum: resolving "the pet grooming
idea" to an exact record via search_research() first, then a second,
explicit owner confirmation before anything is actually launched), not
something to fold into a first version whose whole point is proving
the read-only plumbing works.

Synchronous, not a scheduled/queued task: a chat turn should feel like
asking a question, not like submitting a form and refreshing later.
This never touches the agent/task/ARC economy (see api.py's /overview
for the same "synchronous, read-only, no task involved" precedent) --
it's a dashboard convenience, not a business action.
"""

import json

from tasks.owner_digest import collect_owner_digest

VALID_INTENTS = {
    "overview", "pending_approvals", "ops_health", "trading_status",
    "research_search", "unclear",
}

# table -> (the column holding this record's title, a human-readable
# vertical label) -- same mapping api.py's _launch_business_from_research
# uses, duplicated here rather than imported since api.py never gets
# imported BY a module lower in the dependency stack (api.py imports
# task/service modules, never the reverse -- avoids a circular import
# and keeps this module testable with no FastAPI/pydantic involved).
RESEARCH_TABLES = [
    {"table": "opportunities", "title_field": "topic", "label": "Opportunity Discovery"},
    {"table": "roblox_trends", "title_field": "concept", "label": "Roblox Game Development"},
    {"table": "app_feasibility_assessments", "title_field": "concept",
     "label": "App Development Feasibility"},
    {"table": "real_estate_assessments", "title_field": "property_or_market",
     "label": "Real Estate Investment Research"},
]

RESEARCH_SEARCH_LIMIT_PER_TABLE = 5

SYSTEM_PROMPT = """You are a routing classifier for an AI holding company's owner-facing chat \
assistant. You have NO ability to take any action -- you cannot launch a business, approve or \
reject anything, move money, delete anything, or change any data whatsoever. Your ONLY job is to \
read the owner's message and classify it into exactly one of a fixed set of intents, so that CODE \
(never you) can fetch the real, current data and compose the actual answer.

Valid intents:
- "overview": a general status/summary question (e.g. "how's everything going", "give me a summary")
- "pending_approvals": asking what needs their decision/approval
- "ops_health": asking about system health, errors, or maintenance status
- "trading_status": asking about paper trading P&L, performance, or portfolio value
- "research_search": asking about a SPECIFIC researched idea, concept, or property by name/topic \
-- extract the search text into "query"
- "unclear": the message asks for an ACTION (e.g. "launch X", "approve Y", "delete Z", "refund \
this order", "pause agent A") rather than a question, is off-topic, or doesn't clearly match any \
intent above. When in doubt, use "unclear" -- never guess at an intent that isn't a strong match.

Respond with ONLY a single JSON object (no markdown fences, no prose before or after): \
{"intent": "overview"|"pending_approvals"|"ops_health"|"trading_status"|"research_search"|"unclear", \
"query": "<search text, only meaningful when intent is research_search>"}"""


class ChatError(Exception):
    pass


def classify_intent(message: str, client) -> dict:
    """One real LLM call whose ENTIRE job is picking a fixed intent
    label (plus an optional search query string) -- never given real
    business data, never asked to compose the actual answer. Raises
    ChatError on invalid JSON or an intent outside VALID_INTENTS --
    same hard-failure-never-guess posture as every other classifier in
    this codebase; the caller (answer_message()) decides how to
    present that failure to the owner (see its docstring)."""
    raw_response = client.complete(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
        max_tokens=200,
    )
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise ChatError(f"model did not return valid JSON: {e}. Raw response: {raw_response[:300]}") from e

    intent = parsed.get("intent")
    if intent not in VALID_INTENTS:
        raise ChatError(f"intent must be one of {sorted(VALID_INTENTS)}, got: {intent!r}")

    return {"intent": intent, "query": str(parsed.get("query") or "")}


def collect_chat_overview(db) -> dict:
    """Real, code-computed cross-business snapshot -- the same shape of
    numbers api.py's /overview endpoint renders, recomputed directly
    against the database here rather than importing api.py (which
    imports FROM this module's sibling task modules, never the other
    way around)."""
    business_count = db.query_one("SELECT COUNT(*) as c FROM businesses")["c"]
    agent_count = db.query_one("SELECT COUNT(*) as c FROM agents")["c"]
    open_task_count = db.query_one(
        "SELECT COUNT(*) as c FROM tasks WHERE status NOT IN ('completed','failed','cancelled')"
    )["c"]
    pending_approval_count = db.query_one(
        "SELECT COUNT(*) as c FROM approvals WHERE status='pending'"
    )["c"]
    real_revenue_usd_cents = db.query_one(
        "SELECT COALESCE(SUM(amount_usd_cents),0) as total FROM real_transactions WHERE direction='in'"
    )["total"]
    latest_ops = db.query_one(
        "SELECT overall_severity FROM ops_maintenance_reports ORDER BY created_at DESC LIMIT 1"
    )
    return {
        "business_count": business_count,
        "agent_count": agent_count,
        "open_task_count": open_task_count,
        "pending_approval_count": pending_approval_count,
        "real_revenue_usd_cents": real_revenue_usd_cents,
        "ops_severity": latest_ops["overall_severity"] if latest_ops else None,
    }


def search_research(db, query: str, limit_per_table: int = RESEARCH_SEARCH_LIMIT_PER_TABLE) -> list:
    """Real substring search (case-insensitive via SQL LIKE) across all
    four research verticals' title field and summary -- never a fuzzy/
    semantic match, so a result is always something the owner's own
    words actually appear in, not a model's guess at what they meant."""
    query = (query or "").strip()
    if not query:
        return []
    like_pattern = f"%{query}%"
    results = []
    for spec in RESEARCH_TABLES:
        rows = db.query(
            f"SELECT id, business_id, {spec['title_field']} as title, summary, confidence_level, "
            f"launched_business_id, created_at FROM {spec['table']} "
            f"WHERE {spec['title_field']} LIKE ? OR summary LIKE ? "
            f"ORDER BY created_at DESC LIMIT ?",
            (like_pattern, like_pattern, limit_per_table),
        )
        for row in rows:
            results.append({
                "vertical": spec["label"], "id": row["id"], "business_id": row["business_id"],
                "title": row["title"], "summary": row["summary"],
                "confidence_level": row["confidence_level"],
                "launched": row["launched_business_id"] is not None,
                "created_at": row["created_at"],
            })
    return results


UNCLEAR_RESPONSE = (
    "I can answer questions about: your overall business overview, pending approvals, system "
    "health, trading P&L, or searching your researched ideas by name/topic. I can't take actions "
    "yet (like launching a business, approving something, or deleting anything) -- use the "
    "dashboard for those. Try asking something like \"what needs my approval?\" or \"how's my "
    "trading doing?\"."
)


def format_overview(overview: dict) -> str:
    revenue_usd = overview["real_revenue_usd_cents"] / 100.0
    ops_severity = overview["ops_severity"] or "not yet reviewed"
    return (
        f"You have {overview['business_count']} business(es), {overview['agent_count']} agent(s), "
        f"and {overview['open_task_count']} open task(s). "
        f"{overview['pending_approval_count']} approval(s) are awaiting your decision. "
        f"Total revenue collected: ${revenue_usd:.2f}. System health: {ops_severity}."
    )


def format_pending_approvals(approvals: list) -> str:
    if not approvals:
        return "Nothing is awaiting your decision right now."
    lines = [f"You have {len(approvals)} approval(s) awaiting your decision:"]
    for a in approvals:
        lines.append(f"- [{a['risk_level']}] {a['business_name']}: {a['description']} "
                      f"({a['age_hours']}h old)")
    return "\n".join(lines)


def format_ops_health(ops_health) -> str:
    if not ops_health:
        return "No system health review has run yet."
    return (f"System health: {ops_health['severity']} (as of {ops_health['age_hours']}h ago). "
            f"{ops_health['summary']}")


def format_trading_status(portfolios: list) -> str:
    if not portfolios:
        return "No paper trading portfolio has run a cycle yet."
    lines = []
    for p in portfolios:
        sign = "+" if p["pnl_usd"] >= 0 else ""
        lines.append(f"{p['business_name']}: ${p['equity_usd']:.2f} equity "
                      f"({sign}${p['pnl_usd']:.2f} vs ${p['starting_cash_usd']:.2f} starting cash)")
    return "\n".join(lines)


def format_research_search(results: list, query: str) -> str:
    if not results:
        return f"I couldn't find any researched idea matching \"{query}\"."
    lines = [f"Found {len(results)} result(s) matching \"{query}\":"]
    for r in results:
        status = "already launched" if r["launched"] else "not yet launched"
        lines.append(
            f"- [{r['vertical']}] {r['title']} (confidence: {r['confidence_level'] or 'n/a'}, "
            f"{status}) — {r['summary'] or 'no summary yet'}"
        )
    return "\n".join(lines)


def answer_message(db, message: str, client) -> str:
    """Orchestrates one chat turn: classify, then run the matching
    real query and compose the answer in code. Any classification
    failure (invalid JSON, an intent the model invented outside
    VALID_INTENTS) degrades to the same UNCLEAR_RESPONSE a genuinely
    unclear message gets -- a wobbly classification should read like
    "I didn't understand that", never surface a raw error in a chat
    box the way a failed scheduled task legitimately can."""
    try:
        classification = classify_intent(message, client)
    except ChatError:
        return UNCLEAR_RESPONSE

    intent = classification["intent"]
    if intent == "overview":
        return format_overview(collect_chat_overview(db))
    if intent == "pending_approvals":
        return format_pending_approvals(collect_owner_digest(db)["pending_approvals"])
    if intent == "ops_health":
        return format_ops_health(collect_owner_digest(db)["ops_health"])
    if intent == "trading_status":
        return format_trading_status(collect_owner_digest(db)["trading_portfolios"])
    if intent == "research_search":
        query = classification["query"]
        return format_research_search(search_research(db, query), query)
    return UNCLEAR_RESPONSE
