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

from llm_client import get_default_client
from tasks.summarize_urls import summarize_urls
from tasks.research_opportunity import research_opportunity, OpportunityAssessmentError
from tasks.research_roblox_trend import research_roblox_trend, RobloxTrendAssessmentError
from db import new_id


def _handle_summarize_urls(task_row, client, db):
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    urls = task_input.get("urls", [])
    results = summarize_urls(urls, client)
    result_text = "\n".join(f"- {url}: {summary}" for url, summary in results.items())
    return result_text, 0.0, 0.0  # cost_arc/reward_arc: MVP leaves ARC accounting
                                    # for this to a later, deliberate policy decision
                                    # (see README) rather than an arbitrary number here


def _handle_research_opportunity(task_row, client, db):
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    topic = task_input.get("topic")
    if not topic:
        raise ValueError("research_opportunity task_input missing required 'topic'")
    reference_urls = task_input.get("reference_urls", [])

    assessment = research_opportunity(topic, client, reference_urls=reference_urls)

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
    return result_text, 0.0, 0.0


def _handle_research_roblox_trend(task_row, client, db):
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    concept = task_input.get("concept")
    if not concept:
        raise ValueError("research_roblox_trend task_input missing required 'concept'")
    reference_urls = task_input.get("reference_urls", [])

    assessment = research_roblox_trend(concept, client, reference_urls=reference_urls)

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
    return result_text, 0.0, 0.0


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
    assigned = db.query(
        f"SELECT * FROM tasks WHERE status='assigned' AND task_type IN ({placeholders})",
        tuple(HANDLERS.keys()),
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
