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
from tasks.research_app_feasibility import (
    research_app_feasibility, AppFeasibilityAssessmentError,
)
from tasks.trading_cycle import run_trading_cycle, TradingCycleError
from tasks.trading_strategy_review import (
    compute_stats, propose_strategy_update, TradingStrategyReviewError,
)
import market_data
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

# trading_cycle: a small flat reward for completing a cycle (running the
# analysis and reporting honestly is worth something even on a hold-only
# cycle), plus a bonus scaled by THIS cycle's realized P&L if positive —
# capped so one favorable cycle can't mint an outsized amount of ARC.
# Same "weak simple proxy, not a claim of true economic value" caveat as
# every other reward constant in this file.
TRADING_CYCLE_BASE_REWARD_ARC = 5.0
TRADING_PNL_REWARD_ARC_PER_USD = 2.0
TRADING_PNL_REWARD_ARC_CAP = 50.0


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


def _handle_research_app_feasibility(task_row, client, db):
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    concept = task_input.get("concept")
    if not concept:
        raise ValueError("research_app_feasibility task_input missing required 'concept'")
    reference_urls = task_input.get("reference_urls", [])

    tracked_client = CostTrackingClient(client)
    assessment = research_app_feasibility(concept, tracked_client, reference_urls=reference_urls)

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    assessment_id = new_id("app")
    db.execute(
        "INSERT INTO app_feasibility_assessments (id, business_id, task_id, concept, "
        "platform_recommendation, suggested_tech_stack, complexity_tier, estimated_timeline, "
        "estimated_cost_range, mvp_feature_scope, key_technical_risks, similar_existing_apps, "
        "confidence_level, summary, reference_urls_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (assessment_id, task_row["business_id"], task_row["id"], assessment["concept"],
         assessment["platform_recommendation"], assessment["suggested_tech_stack"],
         assessment["complexity_tier"], assessment["estimated_timeline"],
         assessment["estimated_cost_range"], assessment["mvp_feature_scope"],
         assessment["key_technical_risks"], assessment["similar_existing_apps"],
         assessment["confidence_level"], assessment["summary"],
         json.dumps(assessment["reference_urls_used"])),
    )
    db.audit("executor", "app_feasibility_assessed", "app_feasibility_assessment", assessment_id,
              {"concept": concept, "confidence_level": assessment["confidence_level"],
               "complexity_tier": assessment["complexity_tier"]})

    result_text = (
        f"App feasibility assessment saved (id={assessment_id}, "
        f"confidence={assessment['confidence_level']}, complexity={assessment['complexity_tier']}): "
        f"{assessment['summary']}"
    )
    reward_arc = CONFIDENCE_REWARD_ARC.get(assessment["confidence_level"], 0.0)
    return result_text, cost_arc, reward_arc


# ---------------------------------------------------------------------
# Automated Stock Trading — PAPER TRADING ONLY (see tasks/trading_cycle.py
# for the full safety explanation: no brokerage integration exists
# anywhere in this codebase, so real order execution is structurally
# impossible here, not just disabled by config).
# ---------------------------------------------------------------------

def _row_to_dict(row):
    """Same one-liner as api.py's row_to_dict, duplicated here (not
    imported) to avoid executor.py depending on api.py — api.py already
    depends on executor.py, and this module needs to stay importable on
    its own (see test_trading_cycle_offline.py / test_executor_offline.py,
    neither of which touches api.py)."""
    return dict(row) if row is not None else None


def _get_active_trading_strategy(db, business_id):
    row = db.query_one(
        "SELECT * FROM trading_strategy_versions WHERE business_id=? AND active=1 "
        "ORDER BY version DESC LIMIT 1", (business_id,),
    )
    if not row:
        raise ValueError(
            "no active trading strategy for this business — create a paper trading "
            "portfolio first (POST /businesses/{id}/trading/portfolio)"
        )
    row = _row_to_dict(row)
    return row, json.loads(row["parameters"])


def _get_trading_portfolio(db, business_id):
    row = db.query_one("SELECT * FROM paper_portfolios WHERE business_id=?", (business_id,))
    if not row:
        raise ValueError(
            "no paper trading portfolio for this business — create one first "
            "(POST /businesses/{id}/trading/portfolio)"
        )
    return _row_to_dict(row)


def _upsert_paper_position(db, portfolio_id, symbol, quantity, avg_cost_usd):
    # Deliberately a SELECT-then-INSERT-or-UPDATE, not an SQL-level
    # upsert (ON CONFLICT / INSERT OR REPLACE) — those dialects differ
    # between SQLite and Postgres, and db.py's translation layer only
    # handles `?` and `datetime('now')`. Two extra round-trips per
    # touched symbol is a fine tradeoff for staying on one SQL string
    # that works unmodified on both backends.
    existing = db.query_one(
        "SELECT id FROM paper_positions WHERE portfolio_id=? AND symbol=?",
        (portfolio_id, symbol),
    )
    if existing:
        db.execute(
            "UPDATE paper_positions SET quantity=?, avg_cost_usd=?, updated_at=datetime('now') "
            "WHERE id=?", (quantity, avg_cost_usd, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO paper_positions (id, portfolio_id, symbol, quantity, avg_cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id("pos"), portfolio_id, symbol, quantity, avg_cost_usd),
        )


def _handle_trading_cycle(task_row, client, db):
    business_id = task_row["business_id"]
    strategy_row, strategy_params = _get_active_trading_strategy(db, business_id)
    portfolio = _get_trading_portfolio(db, business_id)
    positions = [_row_to_dict(r) for r in
                 db.query("SELECT * FROM paper_positions WHERE portfolio_id=?", (portfolio["id"],))]

    recent = [_row_to_dict(r) for r in db.query(
        "SELECT * FROM paper_trades WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 10",
        (portfolio["id"],),
    )]
    recent_trades_summary = "\n".join(
        f"- {r['side']} {r['quantity']:.4f} {r['symbol']} @ ${r['price_usd']:.2f}"
        + (f" (realized P&L ${r['realized_pnl_usd']:+.2f})"
           if r["side"] == "sell" and r["realized_pnl_usd"] is not None else "")
        + (f" — {r['rationale']}" if r.get("rationale") else "")
        for r in recent
    )

    tracked_client = CostTrackingClient(client)
    result = run_trading_cycle(
        portfolio["cash_usd"], positions, strategy_params, recent_trades_summary,
        market_data.get_default_client(), tracked_client,
    )

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    positions_by_symbol = {p["symbol"]: p for p in positions}
    cash = portfolio["cash_usd"]
    cycle_realized_pnl = 0.0
    touched_symbols = set()

    for t in result["trades"]:
        if not t["executed"]:
            continue
        existing = positions_by_symbol.get(t["symbol"], {"symbol": t["symbol"], "quantity": 0.0,
                                                           "avg_cost_usd": 0.0})
        realized_pnl = None
        if t["side"] == "buy":
            new_qty = existing["quantity"] + t["quantity"]
            existing["avg_cost_usd"] = (
                (existing["quantity"] * existing["avg_cost_usd"] + t["quantity"] * t["price"]) / new_qty
                if new_qty > 0 else 0.0
            )
            existing["quantity"] = new_qty
            cash -= t["quantity"] * t["price"]
        else:  # sell
            realized_pnl = (t["price"] - existing["avg_cost_usd"]) * t["quantity"]
            cycle_realized_pnl += realized_pnl
            existing["quantity"] -= t["quantity"]
            if existing["quantity"] <= 1e-9:
                existing["quantity"] = 0.0
                existing["avg_cost_usd"] = 0.0
            cash += t["quantity"] * t["price"]

        positions_by_symbol[t["symbol"]] = existing
        touched_symbols.add(t["symbol"])

        db.execute(
            "INSERT INTO paper_trades (id, portfolio_id, task_id, symbol, side, quantity, "
            "price_usd, realized_pnl_usd, confidence_level, rationale, strategy_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("ptr"), portfolio["id"], task_row["id"], t["symbol"], t["side"],
             t["quantity"], t["price"], realized_pnl, t["confidence_level"], t["rationale"],
             strategy_row["version"]),
        )

    for symbol in touched_symbols:
        p = positions_by_symbol[symbol]
        _upsert_paper_position(db, portfolio["id"], symbol, p["quantity"], p["avg_cost_usd"])

    db.execute("UPDATE paper_portfolios SET cash_usd=?, updated_at=datetime('now') WHERE id=?",
               (cash, portfolio["id"]))

    quotes = result["quotes"]
    equity = cash + sum(p["quantity"] * quotes[p["symbol"]]["price"]
                         for p in positions_by_symbol.values()
                         if p["quantity"] > 0 and p["symbol"] in quotes)
    open_positions_count = sum(1 for p in positions_by_symbol.values() if p["quantity"] > 0)

    db.execute(
        "INSERT INTO trading_snapshots (id, portfolio_id, strategy_version, equity_usd, "
        "cash_usd, open_positions) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id("snap"), portfolio["id"], strategy_row["version"], equity, cash, open_positions_count),
    )

    peak_row = db.query_one(
        "SELECT MAX(equity_usd) as peak FROM trading_snapshots WHERE portfolio_id=?",
        (portfolio["id"],),
    )
    peak_equity = peak_row["peak"] if peak_row and peak_row["peak"] is not None else equity
    drawdown_pct = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0

    halted = drawdown_pct >= strategy_params["drawdown_halt_pct"]
    if halted:
        db.audit("executor", "trading_drawdown_halt", "agent", task_row["agent_id"], {
            "business_id": business_id, "drawdown_pct": drawdown_pct,
            "halt_threshold": strategy_params["drawdown_halt_pct"],
            "equity_usd": equity, "peak_equity_usd": peak_equity,
        })

    lines = [f"Trading cycle complete. Equity ${equity:.2f} (cash ${cash:.2f}), "
             f"{open_positions_count} open position(s)."]
    for t in result["trades"]:
        if t["executed"]:
            lines.append(f"  EXECUTED {t['side'].upper()} {t['quantity']:.4f} {t['symbol']} "
                         f"@ ${t['price']:.2f} [{t['confidence_level']}] — {t['rationale']}")
        else:
            lines.append(f"  skipped {t['action']} {t['symbol']}: {t['skip_reason']}")
    if result["quote_errors"]:
        lines.append(f"Quote fetch errors this cycle: {result['quote_errors']}")
    if halted:
        lines.append(
            f"*** DRAWDOWN HALT TRIGGERED at {drawdown_pct:.1%} (threshold "
            f"{strategy_params['drawdown_halt_pct']:.1%} of peak equity ${peak_equity:.2f}) — "
            f"trading agent paused. Owner review required to resume. ***"
        )
    result_text = "\n".join(lines)

    reward_arc = TRADING_CYCLE_BASE_REWARD_ARC
    if cycle_realized_pnl > 0:
        reward_arc += min(cycle_realized_pnl * TRADING_PNL_REWARD_ARC_PER_USD,
                           TRADING_PNL_REWARD_ARC_CAP)

    # A 4th, optional return element: the agent status to force AFTER
    # orchestrator.complete_task() runs. Necessary because complete_task()
    # unconditionally resets the agent to 'idle' on success — a real bug
    # found via this feature's own offline tests: without this, pausing
    # the agent for a drawdown breach here got silently undone the moment
    # the task completed successfully. See run_once() below for how this
    # optional 4th element is consumed; every other handler still returns
    # a plain 3-tuple and is completely unaffected.
    if halted:
        return result_text, cost_arc, reward_arc, "paused"
    return result_text, cost_arc, reward_arc


def _handle_trading_strategy_review(task_row, client, db):
    business_id = task_row["business_id"]
    strategy_row, strategy_params = _get_active_trading_strategy(db, business_id)
    portfolio = _get_trading_portfolio(db, business_id)

    trades = [_row_to_dict(r) for r in db.query(
        "SELECT * FROM paper_trades WHERE portfolio_id=? ORDER BY created_at ASC", (portfolio["id"],),
    )]
    snapshots = [_row_to_dict(r) for r in db.query(
        "SELECT * FROM trading_snapshots WHERE portfolio_id=? ORDER BY created_at ASC", (portfolio["id"],),
    )]
    stats = compute_stats(trades, snapshots)

    recent = trades[-10:]
    recent_trades_summary = "\n".join(
        f"- {r['side']} {r['quantity']:.4f} {r['symbol']} @ ${r['price_usd']:.2f}"
        + (f" (realized P&L ${r['realized_pnl_usd']:+.2f})"
           if r["side"] == "sell" and r["realized_pnl_usd"] is not None else "")
        for r in recent
    )

    tracked_client = CostTrackingClient(client)
    proposal = propose_strategy_update(strategy_params, stats, recent_trades_summary, tracked_client)

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    new_version = strategy_row["version"] + 1
    db.execute("UPDATE trading_strategy_versions SET active=0 WHERE business_id=? AND active=1",
               (business_id,))
    db.execute(
        "INSERT INTO trading_strategy_versions (id, business_id, version, parameters, rationale, "
        "confidence_level, source, active) VALUES (?, ?, ?, ?, ?, ?, 'strategy_review', 1)",
        (new_id("strat"), business_id, new_version, json.dumps(proposal["parameters"]),
         proposal["rationale"], proposal["confidence_level"]),
    )
    db.audit("executor", "trading_strategy_updated", "trading_strategy_version", None, {
        "business_id": business_id, "new_version": new_version,
        "confidence_level": proposal["confidence_level"],
    })

    win_rate_str = f"{stats['win_rate']:.1%}" if stats["win_rate"] is not None else "n/a (no completed trades yet)"
    result_text = (
        f"Strategy reviewed: promoted version {strategy_row['version']} -> {new_version} "
        f"(proposal confidence: {proposal['confidence_level']}).\n"
        f"Stats at review time: {stats['sell_trades']} completed trades, win rate {win_rate_str}, "
        f"total realized P&L ${stats['total_realized_pnl_usd']:.2f}.\n"
        f"Rationale: {proposal['rationale']}"
    )
    reward_arc = CONFIDENCE_REWARD_ARC.get(proposal["confidence_level"], 0.0)
    return result_text, cost_arc, reward_arc


# Registry of task_type -> handler(task_row, client, db) -> (result_text, cost_arc, reward_arc).
# 'manual' is deliberately absent — those tasks are never auto-executed.
HANDLERS = {
    "summarize_urls": _handle_summarize_urls,
    "research_opportunity": _handle_research_opportunity,
    "research_roblox_trend": _handle_research_roblox_trend,
    "research_app_feasibility": _handle_research_app_feasibility,
    "trading_cycle": _handle_trading_cycle,
    "trading_strategy_review": _handle_trading_strategy_review,
}


def run_once(db, orchestrator, client=None):
    """One executor pass: first resets any agent crash-stuck in 'working'
    with no live task back to 'idle' (see Orchestrator.reconcile_stuck_
    agents — without this, a process restart at exactly the wrong moment
    inside complete_task() could strand an agent permanently
    unassignable), then retries any queued tasks that now have an
    eligible agent (see Orchestrator.retry_queued_tasks — without this,
    a task created before any agent was free would stay queued forever;
    doing this AFTER the reconciliation above means a just-healed agent
    is actually available for this same pass's retry, not just the next
    one), then finds every task with status='assigned' and a task_type
    in HANDLERS, runs its handler, and completes or fails it. Returns
    the list of (task_id, outcome) tuples processed, so callers/tests
    can see exactly what happened.

    A handler exception fails the task with the real error message —
    never silently retried forever, never papered over with a fake
    success. Retrying a genuinely transient failure (a dead network,
    say) is a deliberate future decision, not a default.

    A handler normally returns (result_text, cost_arc, reward_arc). It
    may optionally return a 4th element, the agent status to force AFTER
    orchestrator.complete_task() runs — needed because complete_task()
    unconditionally resets the agent to 'idle' on success, which would
    otherwise silently undo something a handler just did (e.g.
    _handle_trading_cycle pausing the agent on a drawdown-halt). Every
    handler that doesn't need this still returns a plain 3-tuple."""
    client = client or get_default_client()
    # Order matters: heal crash-stuck agents FIRST, so a newly-idle agent
    # is actually available for retry_queued_tasks()'s assignment attempt
    # in this same pass, rather than having to wait a full poll interval.
    orchestrator.reconcile_stuck_agents()
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
            outcome = handler(task, client, db)
            if len(outcome) == 4:
                result_text, cost_arc, reward_arc, forced_agent_status = outcome
            else:
                result_text, cost_arc, reward_arc = outcome
                forced_agent_status = None

            orchestrator.complete_task(task["id"], result=result_text, cost_arc=cost_arc,
                                        reward_arc=reward_arc)
            if forced_agent_status and task["agent_id"]:
                db.execute("UPDATE agents SET status=?, updated_at=datetime('now') WHERE id=?",
                           (forced_agent_status, task["agent_id"]))
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
