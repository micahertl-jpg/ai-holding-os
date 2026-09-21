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

import html
import json
import os
import threading
import time

from llm_client import get_default_client, CostTrackingClient
from tasks.summarize_urls import summarize_urls
from tasks.research_opportunity import research_opportunity, OpportunityAssessmentError
from tasks.research_roblox_trend import research_roblox_trend, RobloxTrendAssessmentError
from tasks.research_app_feasibility import (
    research_app_feasibility, AppFeasibilityAssessmentError,
)
from tasks.research_real_estate import research_real_estate, RealEstateAssessmentError
from tasks.trading_cycle import run_trading_cycle, TradingCycleError
from tasks.trading_strategy_review import (
    compute_stats, propose_strategy_update, TradingStrategyReviewError,
)
from tasks.live_trading_safety import (
    apply_live_safety_caps, is_kill_switch_active, LIVE_MAX_DAILY_LOSS_USD,
)
from tasks.backtest import BacktestError
from tasks.strategy_backtest_search import (
    run_strategy_search, split_bars_by_date, StrategySearchError, DEFAULT_MAX_CANDIDATES,
)
from tasks.ops_maintenance_review import (
    collect_system_metrics, analyze_system_health, OpsMaintenanceReviewError,
)
import market_data
from alpaca_client import get_default_client as get_default_alpaca_client, AlpacaError
from db import new_id
from permission_levels import HUMAN_ONLY_LEVEL
from emailer import send_email, EmailError

# Same optional alert address fulfillment.py uses for a failed storefront
# order -- one "tell the owner something needs a look" address, reused
# here for a warning/critical ops report. Unset by default; a missing
# OWNER_EMAIL never blocks the review itself from running and saving.
OWNER_EMAIL = os.environ.get("OWNER_EMAIL")
OPS_ALERT_SEVERITIES = {"warning", "critical"}

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


def _handle_research_real_estate(task_row, client, db):
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    property_or_market = task_input.get("property_or_market")
    if not property_or_market:
        raise ValueError("research_real_estate task_input missing required 'property_or_market'")
    reference_urls = task_input.get("reference_urls", [])

    tracked_client = CostTrackingClient(client)
    assessment = research_real_estate(property_or_market, tracked_client,
                                       reference_urls=reference_urls)

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    assessment_id = new_id("re")
    db.execute(
        "INSERT INTO real_estate_assessments (id, business_id, task_id, property_or_market, "
        "market_trend, comparable_properties, estimated_rental_yield, price_trend_assessment, "
        "risk_factors, confidence_level, summary, reference_urls_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (assessment_id, task_row["business_id"], task_row["id"], assessment["property_or_market"],
         assessment["market_trend"], assessment["comparable_properties"],
         assessment["estimated_rental_yield"], assessment["price_trend_assessment"],
         assessment["risk_factors"], assessment["confidence_level"], assessment["summary"],
         json.dumps(assessment["reference_urls_used"])),
    )
    db.audit("executor", "real_estate_assessed", "real_estate_assessment", assessment_id,
              {"property_or_market": property_or_market,
               "confidence_level": assessment["confidence_level"]})

    result_text = (
        f"Real estate research assessment saved (id={assessment_id}, "
        f"confidence={assessment['confidence_level']}): {assessment['summary']}"
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


# ---------------------------------------------------------------------
# Automated Stock Trading — LIVE (REAL MONEY). Reuses the exact same
# decide_trades()/apply_risk_limits() pair from tasks/trading_cycle.py
# as paper trading, completely unchanged ("model proposes, code
# disposes" — see that module's docstring). Two differences from
# _handle_trading_cycle above: (1) cash/positions/equity come from
# Alpaca's own account every cycle, never from a locally-summed ledger
# (alpaca_client.py's "source of truth from the broker" contract —
# paper_portfolios.cash_usd is never touched by this handler), and (2)
# every executed trade passes through tasks/live_trading_safety.py's
# apply_live_safety_caps() — a second, independent absolute-dollar
# layer — AFTER apply_risk_limits() and BEFORE any real order is
# placed. Results are recorded into live_trades/live_snapshots, tables
# deliberately separate from paper_trades/trading_snapshots so a report
# that forgets a WHERE clause fails loudly rather than silently
# blending real and simulated activity.
# ---------------------------------------------------------------------

# A freshly-placed order often comes back "accepted"/"new", not yet
# filled. These are the statuses this handler treats as final — no
# more polling needed either because a real fill (or partial fill)
# happened, or because the order will clearly never fill.
TERMINAL_ORDER_STATUSES = {
    "filled", "partially_filled", "canceled", "expired", "rejected",
    "done_for_day", "stopped",
}
LIVE_ORDER_FILL_POLL_SECONDS = 1.0
LIVE_ORDER_FILL_TIMEOUT_SECONDS = 30.0


def _poll_order_until_filled(alpaca, order):
    """Polls get_order() until a terminal status is reached or a
    bounded timeout elapses. Never fabricates a fill -- returns
    whatever Alpaca's own get_order() last reported, filled or not, so
    the caller records exactly what really happened rather than
    assuming a placed order means a filled order."""
    if order.get("status") in TERMINAL_ORDER_STATUSES:
        return order
    order_id = order["id"]
    waited = 0.0
    while waited < LIVE_ORDER_FILL_TIMEOUT_SECONDS:
        time.sleep(LIVE_ORDER_FILL_POLL_SECONDS)
        waited += LIVE_ORDER_FILL_POLL_SECONDS
        order = alpaca.get_order(order_id)
        if order.get("status") in TERMINAL_ORDER_STATUSES:
            return order
    return order


def _handle_live_trading_cycle(task_row, client, db):
    business_id = task_row["business_id"]
    strategy_row, strategy_params = _get_active_trading_strategy(db, business_id)
    portfolio = _get_trading_portfolio(db, business_id)
    if not portfolio.get("live_trading_enabled"):
        raise ValueError(
            "live trading is not enabled for this business's portfolio -- the owner must "
            "explicitly enable it first (POST /businesses/{id}/trading/live/enable)"
        )
    if is_kill_switch_active():
        raise RuntimeError(
            "LIVE_TRADING_KILL_SWITCH is active -- all live trading is halted until it's cleared"
        )

    alpaca = get_default_alpaca_client()
    if alpaca.is_paper:
        # get_default_alpaca_client() only ever returns a real
        # AlpacaClient, but that client itself defaults to Alpaca's
        # PAPER endpoint unless ALPACA_BASE_URL is explicitly set to
        # the live one -- catching that here turns what would silently
        # be a no-op (a "live" task that only ever touches Alpaca's
        # simulator) into a loud, actionable error, and prevents a
        # paper fill from ever being recorded into live_trades.
        raise RuntimeError(
            "live trading is enabled but this client is talking to Alpaca's PAPER endpoint "
            "(ALPACA_BASE_URL is not set to the live one) -- set "
            "ALPACA_BASE_URL=https://api.alpaca.markets to actually place real orders"
        )

    account = alpaca.get_account()
    live_positions = alpaca.get_positions()
    positions = [{"symbol": p["symbol"], "quantity": p["quantity"],
                  "avg_cost_usd": p["avg_entry_price"]} for p in live_positions]
    positions_by_symbol = {p["symbol"]: p for p in positions}

    recent = [_row_to_dict(r) for r in db.query(
        "SELECT * FROM live_trades WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 10",
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
        account["cash"], positions, strategy_params, recent_trades_summary,
        market_data.get_default_client(), tracked_client,
    )

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    today_pnl_row = db.query_one(
        "SELECT COALESCE(SUM(realized_pnl_usd), 0) as pnl FROM live_trades "
        "WHERE portfolio_id=? AND side='sell' AND date(created_at) = date('now')",
        (portfolio["id"],),
    )
    todays_realized_pnl = today_pnl_row["pnl"] if today_pnl_row else 0.0

    capped_trades = apply_live_safety_caps(result["trades"], todays_realized_pnl)

    lines = [f"Live trading cycle starting. Cash ${account['cash']:.2f}, "
             f"equity ${account['equity']:.2f}, {len(positions)} open position(s)."]
    cycle_realized_pnl = 0.0
    for t in capped_trades:
        if not t["executed"]:
            lines.append(f"  skipped {t['action']} {t['symbol']}: {t['skip_reason']}")
            continue

        notional_usd = t["quantity"] * t["price"]
        try:
            order = alpaca.place_order(t["symbol"], t["side"], notional_usd)
            order = _poll_order_until_filled(alpaca, order)
        except AlpacaError as e:
            db.audit("executor", "live_order_failed", "agent", task_row["agent_id"], {
                "business_id": business_id, "symbol": t["symbol"], "side": t["side"],
                "notional_usd": notional_usd, "error": str(e),
            })
            lines.append(f"  ORDER FAILED {t['side'].upper()} ~${notional_usd:.2f} {t['symbol']}: {e}")
            continue

        filled_qty = float(order.get("filled_qty") or 0)
        filled_price = float(order.get("filled_avg_price") or 0)
        if filled_qty <= 0 or filled_price <= 0:
            # Placed but never confirmed filled within the poll window (or
            # rejected/canceled without a fill) -- never fabricate a trade
            # row for a real order that didn't actually execute.
            db.audit("executor", "live_order_unconfirmed", "agent", task_row["agent_id"], {
                "business_id": business_id, "symbol": t["symbol"], "side": t["side"],
                "order_id": order.get("id"), "status": order.get("status"),
            })
            lines.append(f"  ORDER UNCONFIRMED {t['side'].upper()} {t['symbol']} "
                         f"(id={order.get('id')}, status={order.get('status')}) -- no fill recorded")
            continue

        existing = positions_by_symbol.get(t["symbol"], {"quantity": 0.0, "avg_cost_usd": 0.0})
        realized_pnl = None
        if t["side"] == "sell":
            realized_pnl = (filled_price - existing["avg_cost_usd"]) * filled_qty
            cycle_realized_pnl += realized_pnl

        db.execute(
            "INSERT INTO live_trades (id, portfolio_id, task_id, alpaca_order_id, symbol, side, "
            "quantity, price_usd, realized_pnl_usd, confidence_level, rationale, "
            "strategy_version, live_cap_applied) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("ltr"), portfolio["id"], task_row["id"], order.get("id"), t["symbol"],
             t["side"], filled_qty, filled_price, realized_pnl, t["confidence_level"],
             t["rationale"], strategy_row["version"], 1 if t.get("live_cap_applied") else 0),
        )
        lines.append(f"  EXECUTED {t['side'].upper()} {filled_qty:.4f} {t['symbol']} "
                     f"@ ${filled_price:.2f} [{t['confidence_level']}]"
                     + (" (cap-clamped)" if t.get("live_cap_applied") else "")
                     + f" — {t['rationale']}")

    # Source of truth is the broker, always -- re-fetch after every order
    # this cycle so the snapshot reflects exactly what Alpaca itself now
    # holds, never a locally-summed guess.
    final_account = alpaca.get_account()
    final_positions = alpaca.get_positions()
    open_positions_count = sum(1 for p in final_positions if p["quantity"] > 0)

    db.execute(
        "INSERT INTO live_snapshots (id, portfolio_id, strategy_version, equity_usd, "
        "cash_usd, open_positions) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id("lsnap"), portfolio["id"], strategy_row["version"], final_account["equity"],
         final_account["cash"], open_positions_count),
    )

    peak_row = db.query_one(
        "SELECT MAX(equity_usd) as peak FROM live_snapshots WHERE portfolio_id=?",
        (portfolio["id"],),
    )
    peak_equity = (peak_row["peak"] if peak_row and peak_row["peak"] is not None
                   else final_account["equity"])
    drawdown_pct = ((peak_equity - final_account["equity"]) / peak_equity) if peak_equity > 0 else 0.0
    drawdown_halted = drawdown_pct >= strategy_params["drawdown_halt_pct"]

    todays_realized_pnl_after = todays_realized_pnl + cycle_realized_pnl
    daily_loss_halted = todays_realized_pnl_after <= -abs(LIVE_MAX_DAILY_LOSS_USD)

    halted = drawdown_halted or daily_loss_halted
    if halted:
        halt_reasons = []
        if drawdown_halted:
            halt_reasons.append(f"drawdown {drawdown_pct:.1%} >= threshold "
                                 f"{strategy_params['drawdown_halt_pct']:.1%}")
        if daily_loss_halted:
            halt_reasons.append(f"today's realized P&L ${todays_realized_pnl_after:.2f} <= "
                                 f"-${LIVE_MAX_DAILY_LOSS_USD:.2f} daily loss cap")
        db.audit("executor", "live_trading_halt", "agent", task_row["agent_id"], {
            "business_id": business_id, "reasons": halt_reasons,
            "equity_usd": final_account["equity"], "peak_equity_usd": peak_equity,
            "todays_realized_pnl_usd": todays_realized_pnl_after,
        })
        lines.append(f"*** LIVE TRADING HALTED: {'; '.join(halt_reasons)} -- agent paused, "
                     f"owner review required to resume. ***")

    result_text = "\n".join(lines)

    reward_arc = TRADING_CYCLE_BASE_REWARD_ARC
    if cycle_realized_pnl > 0:
        reward_arc += min(cycle_realized_pnl * TRADING_PNL_REWARD_ARC_PER_USD,
                           TRADING_PNL_REWARD_ARC_CAP)

    # Same optional 4th-element mechanism _handle_trading_cycle uses --
    # see run_once() below. complete_task() would otherwise silently
    # reset the agent to 'idle' right after a halt this handler just
    # applied.
    if halted:
        return result_text, cost_arc, reward_arc, "paused"
    return result_text, cost_arc, reward_arc


# ---------------------------------------------------------------------
# Backtesting & bounded strategy search against REAL historical data
# (see tasks/backtest.py, tasks/strategy_backtest_search.py).
# Recommend-only, like ops_maintenance_review: this NEVER touches
# paper_trades/trading_snapshots/live_trades/live_snapshots, and never
# auto-activates a candidate strategy as the business's active one --
# promoting a candidate is always a separate, explicit owner action via
# the existing POST /businesses/{id}/trading/strategy-override endpoint
# (already versioned, already bounds-checked). This only ever saves a
# report of what was tried and found.
# ---------------------------------------------------------------------

BACKTEST_SEARCH_BASE_REWARD_ARC = 5.0


def _fmt_stat(value):
    """Formats a stats value that may be None or +/-inf (profit_factor
    with zero losing trades) safely -- a plain f-string :.2f on an
    inf/None value would crash or misrepresent the result."""
    if value is None:
        return "n/a"
    if isinstance(value, float) and (value == float("inf") or value == float("-inf")):
        return str(value)
    return f"{value:.4f}"


def _handle_strategy_backtest_search(task_row, client, db):
    business_id = task_row["business_id"]
    task_input = json.loads(task_row["task_input"]) if task_row["task_input"] else {}
    train_start_date = task_input.get("train_start_date")
    validation_split_date = task_input.get("validation_split_date")
    validation_end_date = task_input.get("validation_end_date")
    if not (train_start_date and validation_split_date and validation_end_date):
        raise ValueError(
            "strategy_backtest_search task_input missing required 'train_start_date' / "
            "'validation_split_date' / 'validation_end_date' (each \"YYYY-MM-DD\")"
        )
    starting_cash_usd = float(task_input.get("starting_cash_usd", 10000.0))
    max_candidates = int(task_input.get("max_candidates", DEFAULT_MAX_CANDIDATES))

    strategy_row, strategy_params = _get_active_trading_strategy(db, business_id)
    market_client = market_data.get_default_client()

    # One real historical-data fetch per watchlist symbol, covering the
    # WHOLE train+validation range in one call each (not one call per
    # window) -- split_bars_by_date then cuts it at the boundary. Any
    # MarketDataError here (including get_default_client() falling back
    # to a mock with no ALPHAVANTAGE_API_KEY, which run_strategy_search
    # would then refuse via BacktestError anyway) propagates up through
    # run_once()'s normal try/except and fails this task loudly.
    historical_bars_by_symbol = {
        symbol: market_client.get_daily_history(symbol, train_start_date, validation_end_date)
        for symbol in strategy_params["watchlist"]
    }
    train_bars, validation_bars = split_bars_by_date(historical_bars_by_symbol, validation_split_date)

    tracked_client = CostTrackingClient(client)
    try:
        search_result = run_strategy_search(
            starting_cash_usd, strategy_params, train_bars, validation_bars,
            tracked_client, max_candidates=max_candidates,
        )
    except (BacktestError, StrategySearchError) as e:
        raise ValueError(str(e)) from e

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    candidates_summary = [
        {
            "parameters": c["parameters"],
            "rationale": c.get("rationale"),
            "confidence_level": c.get("confidence_level"),
            "train_stats": c["train_stats"],
            "validation_stats": c["validation_stats"],
            "train_meets_bar": c["train_meets_bar"],
            "validation_meets_bar": c["validation_meets_bar"],
        }
        for c in search_result["candidates"]
    ]

    run_id = new_id("bt")
    db.execute(
        "INSERT INTO backtest_runs (id, business_id, task_id, train_start_date, "
        "validation_split_date, validation_end_date, max_candidates, candidates_json, "
        "best_candidate_index, stopped_early) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, business_id, task_row["id"], train_start_date, validation_split_date,
         validation_end_date, max_candidates, json.dumps(candidates_summary),
         search_result["best_index"], 1 if search_result["stopped_early"] else 0),
    )
    db.audit("executor", "strategy_backtest_search_completed", "backtest_run", run_id, {
        "business_id": business_id, "candidates_tried": len(candidates_summary),
        "stopped_early": search_result["stopped_early"],
    })

    best = candidates_summary[search_result["best_index"]]
    vs = best["validation_stats"]
    result_text = (
        f"Backtest search complete: {len(candidates_summary)} candidate(s) tried against real "
        f"historical data, "
        + ("a strategy cleared the profitability bar on held-out validation data."
           if search_result["stopped_early"] else
           "none cleared the profitability bar on held-out validation data.")
        + f" Best by real validation net P&L: ${_fmt_stat(vs['net_pnl_usd'])} "
        f"(profit_factor={_fmt_stat(vs['profit_factor'])}, win_rate={_fmt_stat(vs['win_rate'])}, "
        f"max_drawdown={_fmt_stat(vs['max_drawdown_pct'])}, "
        f"sample_size_ok={vs['sample_size_ok']}, sell_trades={vs['sell_trades']}). "
        f"Review every candidate on the dashboard's Backtest panel (id={run_id}) and use the "
        f"existing strategy-override endpoint to promote one -- nothing is auto-activated."
    )
    reward_arc = BACKTEST_SEARCH_BASE_REWARD_ARC
    return result_text, cost_arc, reward_arc


# ---------------------------------------------------------------------
# Ops/Maintenance — the sixth business vertical, and the only one with
# no customer/storefront product: it watches this system's OWN
# infrastructure and produces a recommend-only report for the owner.
# See tasks/ops_maintenance_review.py.
# ---------------------------------------------------------------------

def _notify_owner_of_ops_report(report_id, severity, summary, db):
    """Best-effort alert for a warning/critical ops report -- same
    pattern as fulfillment.py's _notify_owner_of_failed_order: a missing
    OWNER_EMAIL or a broken Resend send must never affect the report
    that's already correctly saved, or crash the executor. Only ever
    logged via the audit trail either way."""
    if not OWNER_EMAIL:
        return
    subject = f"[{severity.upper()}] Ops/Maintenance review found something"
    body = f"""
    <div style="font-family:sans-serif;max-width:600px;">
      <h2>System health review: {html.escape(severity)}</h2>
      <p>{html.escape(summary)}</p>
      <p style="color:#666;font-size:12px;">Report id: {html.escape(report_id)} -- see the
        dashboard's System Health panel for the full findings list. This system only ever
        recommends; nothing was changed automatically.</p>
    </div>
    """
    try:
        send_email(OWNER_EMAIL, subject, body)
        db.audit("executor", "owner_ops_report_alert_sent", "ops_maintenance_report", report_id, {})
    except EmailError as e:
        db.audit("executor", "owner_ops_report_alert_failed", "ops_maintenance_report", report_id,
                  {"error": str(e)})


def _handle_ops_maintenance_review(task_row, client, db):
    metrics = collect_system_metrics(db)

    tracked_client = CostTrackingClient(client)
    report = analyze_system_health(metrics, tracked_client)

    cost_arc = tracked_client.total_cost_usd * ARC_PER_USD
    _require_affordable(task_row, db, cost_arc)

    report_id = new_id("ops")
    db.execute(
        "INSERT INTO ops_maintenance_reports (id, business_id, task_id, overall_severity, "
        "findings, confidence_level, summary, metrics_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (report_id, task_row["business_id"], task_row["id"], report["overall_severity"],
         json.dumps(report["findings"]), report["confidence_level"], report["summary"],
         json.dumps(metrics)),
    )
    db.audit("executor", "ops_maintenance_reviewed", "ops_maintenance_report", report_id,
              {"overall_severity": report["overall_severity"], "finding_count": len(report["findings"])})

    if report["overall_severity"] in OPS_ALERT_SEVERITIES:
        _notify_owner_of_ops_report(report_id, report["overall_severity"], report["summary"], db)

    result_text = (
        f"Ops/Maintenance review saved (id={report_id}, severity={report['overall_severity']}, "
        f"{len(report['findings'])} finding(s)): {report['summary']}"
    )
    reward_arc = CONFIDENCE_REWARD_ARC.get(report["confidence_level"], 0.0)
    return result_text, cost_arc, reward_arc


# Registry of task_type -> handler(task_row, client, db) -> (result_text, cost_arc, reward_arc).
# 'manual' is deliberately absent — those tasks are never auto-executed.
HANDLERS = {
    "summarize_urls": _handle_summarize_urls,
    "research_opportunity": _handle_research_opportunity,
    "research_roblox_trend": _handle_research_roblox_trend,
    "research_app_feasibility": _handle_research_app_feasibility,
    "research_real_estate": _handle_research_real_estate,
    "trading_cycle": _handle_trading_cycle,
    "trading_strategy_review": _handle_trading_strategy_review,
    "live_trading_cycle": _handle_live_trading_cycle,
    "strategy_backtest_search": _handle_strategy_backtest_search,
    "ops_maintenance_review": _handle_ops_maintenance_review,
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
