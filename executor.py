"""
executor.py — the piece that actually DOES the work a task represents.

Before this existed, nothing in the system ever automatically executed
a task: create_task() would create and auto-assign a row, but turning
that into real AI work only ever happened by hand, in demo_real_llm.py.
That's a real gap for an "autonomous" system — a scheduled job could
create a research_opportunity task every hour forever, and it would
just pile up in 'assigned' status doing nothing.

This module closes that loop: a background thread (started alongside
the scheduler's, in api.py's lifespan) polls for tasks with status
'assigned' and a task_type in HANDLERS, runs the matching handler with
a real LLM client, and calls orchestrator.complete_task() or
.fail_task() with the outcome. task_type='manual' (the default for
every task created without an explicit type) is NEVER touched here —
manual tasks are still completed by hand, exactly as before.

Split the same way as scheduler.py: `run_once()` is a pure-enough,
directly-testable pass; `run_forever()` is the thin sleep-loop that can
only really be verified by running the server.
"""

import json
import threading

from llm_client import get_default_client, CostTrackingClient
from tasks.summarize_urls import summarize_urls
from tasks.research_opportunity import research_opportunity, OpportunityAssessmentError
from tasks.research_roblox_trend import research_roblox_trend, RobloxTrendAssessmentError
from db import new_id
from permission_levels import HUMAN_ONLY_LEVEL

# ---------------------------------------------------------------------
# ARC accounting policy for the executor's task handlers.
#
# This is a deliberate, adjustable first-pass policy, not a claim of
# "true" economic value — see banker.py and the project spec Sec. 6/16
# ("Banker should NOT blindly reward agents for activity. Reward
# outcomes and meaningful contribution.") Both numbers below are meant
# to be tuned by the owner over time as real usage data comes in; they
# are not derived from anything external.
#
# cost_arc: charged 1:1 against the LLM call's estimated real USD cost
# (see llm_client.PRICING_USD_PER_TOKEN), converted to ARC at this
# fixed internal exchange rate. This makes "cost" in the ARC ledger
# actually track real spend for the first time, instead of always
# reading 0.0 regardless of how much a task really used.
ARC_PER_USD = 1000.0

# reward_arc: research tasks are rewarded more for a higher-confidence
# (more evidence-backed) assessment, since that's the closest measurable
# proxy this MVP has for "meaningful contribution" per a research task.
# This is intentionally a weak, simple proxy — not a claim that a
# high-confidence assessment is objectively more valuable to the
# business than a low-confidence one flagging a genuine unknown.
CONFIDENCE_REWARD_ARC = {"low": 5.0, "medium": 15.0, "high": 30.0}

# summarize_urls has no confidence_level, so it's rewarded flatly per
# URL that actually got a real summary (fetch+model call both
# succeeded) — a failed/empty URL earns nothing.
REWARD_ARC_PER_SUMMARIZED_URL = 3.0


def _require_affordable(task_row, db, cost_arc):
    """Raises loudly if the assigned agent can't cover cost_arc, BEFORE
    a handler saves any result row. Without this check, a handler that
    computes cost_arc only after doing (and saving) real work would let
    an unaffordable task still write its result, then fail later at
    orchestrator.complete_task()'s own charge() call — leaving a saved
    opportunities/roblox_trends row attached to a task that ultimately
    shows 'failed'. Checking first means a task that can't be charged
    never gets to save anything it can't pay for, matching this
    codebase's existing rule of never partially/silently succeeding.

    The real LLM cost is still incurred either way (the call already
    happened by the time cost_arc is known) — this only controls
    whether the RESULT is kept, not whether money was spent getting it.
    That's a deliberate simplicity tradeoff for the MVP, not something
    a real production system should accept long-term."""
    if cost_arc <= 0:
        return
    agent = db.query_one("SELECT arc_balance FROM agents WHERE id=?", (task_row["agent_id"],))
    balance = agent["arc_balance"] if agent else 0.0
    if balance < cost_arc:
        raise RuntimeError(
            f"agent's ARC balance ({balance:.4f}) is insufficient to cover this task's "
            f"real estimated cost ({cost_arc:.4f} ARC) — the result is being discarded "
            f"rather than saved against a task that can't be charged; allocate more ARC "
            f"to this agent (see the dashboard's 'Allocate ARC' form) and retry"
        )


def _handle_summarize_urls(task_row, client, db):
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    urls = task_input.get("urls", [])
    tracked_client = CostTrackingClient(client)
    results = summarize_urls(urls, tracked_client)
    result_text = "\n".join(f"- {url}: {summary}" for url, summary in results.items())

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)
    successful = sum(1 for summary in results.values() if not summary.startswith("["))
    reward_arc = successful * REWARD_ARC_PER_SUMMARIZED_URL
    return result_text, cost_arc, reward_arc


def _handle_research_opportunity(task_row, client, db):
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    topic = task_input.get("topic")
    if not topic:
        raise ValueError("research_opportunity task_input missing required 'topic'")
    reference_urls = task_input.get("reference_urls", [])

    tracked_client = CostTrackingClient(client)
    assessment = research_opportunity(topic, tracked_client, reference_urls=reference_urls)

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    opp_id = new_id("opp")
    db.execute(
        "INSERT INTO opportunities (id, business_id, task_id, topic, market_size, "
        "competition, startup_cost, revenue_potential, time_to_market, "
        "operational_complexity, legal_regulatory_risk, capital_requirements, "
        "downside_risk, confidence_level, summary, reference_urls_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (opp_id, task_row["business_id"], task_row["id"], assessment["topic"],
         assessment["market_size"], assessment["competition"], assessment["startup_cost"],
         assessment["revenue_potential"], assessment["time_to_market"],
         assessment["operational_complexity"], assessment["legal_regulatory_risk"],
         assessment["capital_requirements"], assessment["downside_risk"],
         assessment["confidence_level"], assessment["summary"],
         json.dumps(assessment["reference_urls_used"])),
    )
    db.audit("executor", "opportunity_assessed", "opportunity", opp_id,
              {"topic": topic, "confidence_level": assessment["confidence_level"]})

    result_text = (
        f"Opportunity assessment saved (id={opp_id}, confidence={assessment['confidence_level']}): "
        f"{assessment['summary']}"
    )
    reward_arc = CONFIDENCE_REWARD_ARC.get(assessment["confidence_level"], 0.0)
    return result_text, cost_arc, reward_arc


def _handle_research_roblox_trend(task_row, client, db):
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    concept = task_input.get("concept")
    if not concept:
        raise ValueError("research_roblox_trend task_input missing required 'concept'")
    reference_urls = task_input.get("reference_urls", [])

    tracked_client = CostTrackingClient(client)
    assessment = research_roblox_trend(concept, tracked_client, reference_urls=reference_urls)

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    trend_id = new_id("rbx")
    db.execute(
        "INSERT INTO roblox_trends (id, business_id, task_id, concept, "
        "player_demand_signals, competition_level, build_complexity, target_audience, "
        "monetization_fit, estimated_dev_time, similar_successful_games, risk_factors, "
        "confidence_level, summary, reference_urls_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (trend_id, task_row["business_id"], task_row["id"], assessment["concept"],
         assessment["player_demand_signals"], assessment["competition_level"],
         assessment["build_complexity"], assessment["target_audience"],
         assessment["monetization_fit"], assessment["estimated_dev_time"],
         assessment["similar_successful_games"], assessment["risk_factors"],
         assessment["confidence_level"], assessment["summary"],
         json.dumps(assessment["reference_urls_used"])),
    )
    db.audit("executor", "roblox_trend_assessed", "roblox_trend", trend_id,
              {"concept": concept, "confidence_level": assessment["confidence_level"]})

    result_text = (
        f"Roblox trend assessment saved (id={trend_id}, confidence={assessment['confidence_level']}): "
        f"{assessment['summary']}"
    )
    reward_arc = CONFIDENCE_REWARD_ARC.get(assessment["confidence_level"], 0.0)
    return result_text, cost_arc, reward_arc


# Registry of task_type -> handler(task_row, client, db) -> (result_text, cost_arc, reward_arc).
# 'manual' is deliberately absent — those tasks are never auto-executed.
HANDLERS = {
    "summarize_urls": _handle_summarize_urls,
    "research_opportunity": _handle_research_opportunity,
    "research_roblox_trend": _handle_research_roblox_trend,
}


def run_once(db, orchestrator, client=None):
    """One executor pass: first retries any queued tasks that now have
    an eligible agent (see Orchestrator.retry_queued_tasks — without
    this, a task created before any agent was free would stay queued
    forever), then finds every task with status='assigned' and a
    task_type in HANDLERS, runs its handler, and completes or fails it.
    Returns the list of (task_id, outcome) tuples processed, so
    callers/tests can see exactly what happened.

    A handler exception fails the task with the real error message —
    never silently retried forever, never papered over with a fake
    success. Retrying a genuinely transient failure (a dead network,
    say) is a deliberate future decision, not a default."""
    client = client or get_default_client()
    orchestrator.retry_queued_tasks()
    placeholders = ",".join("?" for _ in HANDLERS)
    # permission_level_required < HUMAN_ONLY_LEVEL excludes level-7
    # (HUMAN_ONLY) tasks even once they reach 'assigned' status after
    # owner approval — those must only ever be completed by the owner
    # calling complete_task()/fail_task() by hand, never auto-run here.
    # Before this filter existed, a level-7 task using an executable
    # task_type was silently auto-executed the moment it was approved,
    # defeating the whole point of "human-only".
    assigned = db.query(
        f"SELECT * FROM tasks WHERE status='assigned' AND permission_level_required < ? "
        f"AND task_type IN ({placeholders})",
        (HUMAN_ONLY_LEVEL,) + tuple(HANDLERS.keys()),
    )
    outcomes = []
    for task in assigned:
        handler = HANDLERS[task["task_type"]]
        try:
            result_text, cost_arc, reward_arc = handler(task, client, db)
            orchestrator.complete_task(task["id"], result=result_text, cost_arc=cost_arc,
                                        reward_arc=reward_arc)
            outcomes.append((task["id"], "completed"))
        except Exception as e:
            orchestrator.fail_task(task["id"], reason=f"executor error: {e}")
            outcomes.append((task["id"], f"failed: {e}"))
    return outcomes


def run_forever(db, orchestrator, poll_interval_seconds: float, stop_event: threading.Event,
                 client=None):
    """Thin wrapper around run_once(), same pattern as scheduler.py's
    run_forever(): sleep, run a pass, repeat, until stop_event is set.
    A bad pass is logged and the loop continues rather than dying."""
    while not stop_event.is_set():
        try:
            run_once(db, orchestrator, client=client)
        except Exception as e:
            try:
                db.audit("executor", "executor_pass_error", details={"error": str(e)})
            except Exception:
                pass
        stop_event.wait(poll_interval_seconds)
