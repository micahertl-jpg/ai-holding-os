"""
test_executor_offline.py — real tests for executor.py's run_once(),
using the actual SQLite Database/Orchestrator (not mocks for those) and
MockClient/patched fetches for the parts that need real internet or a
real API key. Proves the executor correctly dispatches by task_type,
correctly leaves 'manual' tasks and non-'assigned' tasks untouched, and
correctly fails a task loudly on a real handler error rather than
hiding it.
"""

import os
import json
from unittest.mock import patch

from db import Database, new_id
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator
from llm_client import MockClient
from tasks.trading_common import DEFAULT_STRATEGY_PARAMS
import executor


class _FakeMarketClient:
    """Real (non-mock=True) quotes for executor.py's trading handlers to
    consume in these DB-integration tests -- market_data.MockMarketDataClient
    itself is deliberately refused by tasks/trading_cycle.py (see
    test_trading_cycle_offline.py), so these tests inject a client that
    looks like a real one, without any real network call."""

    def __init__(self, prices):
        self.prices = prices

    def get_quote(self, symbol):
        return {"symbol": symbol, "price": self.prices[symbol], "as_of": "2026-01-01",
                "mock": False}


class _FakeCostClient:
    """A minimal stand-in with a fixed, controllable last_usage, so this
    test can assert an exact expected cost_arc without depending on
    real network access or AnthropicClient's HTTP parsing (that's
    covered separately in test_llm_client_offline.py). Duck-types the
    same interface CostTrackingClient wraps."""

    def __init__(self, canned_response, cost_usd_per_call):
        self.canned_response = canned_response
        self.cost_usd_per_call = cost_usd_per_call
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        self.calls = 0

    def complete(self, messages, system=None, max_tokens=1000):
        self.calls += 1
        self.last_usage = {"input_tokens": 100, "output_tokens": 50,
                            "cost_usd": self.cost_usd_per_call}
        return self.canned_response

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_executor.db")

VALID_ASSESSMENT_JSON = json.dumps({
    "market_size": "Small but real, per the reference text.",
    "competition": "Fragmented.",
    "startup_cost": "Low.",
    "revenue_potential": "Modest but plausible.",
    "time_to_market": "A few weeks.",
    "operational_complexity": "Low.",
    "legal_regulatory_risk": "Minimal.",
    "capital_requirements": "Under $5k.",
    "downside_risk": "Low.",
    "confidence_level": "medium",
    "summary": "Worth a small validation effort.",
})

VALID_ROBLOX_ASSESSMENT_JSON = json.dumps({
    "player_demand_signals": "Moderate interest per the reference text.",
    "competition_level": "Fragmented.",
    "build_complexity": "Moderate.",
    "target_audience": "Kids/teens who like obby-style games.",
    "monetization_fit": "Game passes plausible.",
    "estimated_dev_time": "A few weeks.",
    "similar_successful_games": "A couple of comparable experiences exist.",
    "risk_factors": "Genre is moderately saturated.",
    "confidence_level": "medium",
    "summary": "Worth a small prototype effort.",
})

VALID_APP_FEASIBILITY_ASSESSMENT_JSON = json.dumps({
    "platform_recommendation": "Cross-platform mobile via React Native.",
    "suggested_tech_stack": "React Native, FastAPI, Postgres.",
    "complexity_tier": "moderate",
    "estimated_timeline": "8-12 weeks for an MVP",
    "estimated_cost_range": "Roughly $15k-$40k, a rough estimate.",
    "mvp_feature_scope": "Account creation and one core interaction loop.",
    "key_technical_risks": "Push notification reliability.",
    "similar_existing_apps": "A few comparable apps exist.",
    "confidence_level": "medium",
    "summary": "Worth a small MVP validation effort.",
})


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)
    biz_id = businesses.create("Executor Test Co", "opportunity_discovery", "test")
    agent_id = agents.create(biz_id, "Researcher", role="Research Analyst",
                              department="research", permission_level=2)
    agents.set_status(agent_id, "idle")
    return db, orch, biz_id, agent_id


def test_summarize_urls_task_gets_executed_and_completed():
    db, orch, biz_id, agent_id = _setup()
    with patch("tasks.summarize_urls.fetch_url_text", return_value="Some page text."):
        task_id = orch.create_task(
            biz_id, "Summarize a page", department="research",
            permission_level_required=2, task_type="summarize_urls",
            task_input={"urls": ["https://example.com/x"]},
        )
        client = MockClient(canned_response="A short factual summary.")
        outcomes = executor.run_once(db, orch, client=client)

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"
    assert "A short factual summary." in task["result"]
    print("PASS: a summarize_urls task gets picked up, executed, and completed by run_once()")
    db.close()
    os.remove(TEST_DB_PATH)


def test_research_opportunity_task_gets_executed_and_saved():
    db, orch, biz_id, agent_id = _setup()
    with patch("tasks.research_opportunity.fetch_url_text", return_value="Some reference text."):
        task_id = orch.create_task(
            biz_id, "Research a niche", department="research",
            permission_level_required=2, task_type="research_opportunity",
            task_input={"topic": "AI recipe apps", "reference_urls": ["https://example.com/y"]},
        )
        client = MockClient(canned_response=VALID_ASSESSMENT_JSON)
        outcomes = executor.run_once(db, orch, client=client)

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"
    assert "Opportunity assessment saved" in task["result"]

    opp = db.query_one("SELECT * FROM opportunities WHERE task_id=?", (task_id,))
    assert opp is not None, "expected a row in opportunities"
    assert opp["topic"] == "AI recipe apps"
    assert opp["confidence_level"] == "medium"
    assert json.loads(opp["reference_urls_used"]) == ["https://example.com/y"]
    print("PASS: a research_opportunity task gets executed and saves a real opportunities row")
    db.close()
    os.remove(TEST_DB_PATH)


def test_research_opportunity_charges_real_cost_and_rewards_by_confidence():
    """The core proof that ARC accounting is now real, not hardcoded
    0.0/0.0: a task run with a client that reports a known, nonzero
    LLM cost should charge exactly that (converted at
    executor.ARC_PER_USD) and reward exactly the confidence-tier amount
    — and both should show up as real rows in arc_ledger, not just in
    the task's own cost_arc column."""
    db, orch, biz_id, agent_id = _setup()
    banker = Banker(db)
    banker.allocate(biz_id, agent_id, 1000.0, reason="test funding")

    with patch("tasks.research_opportunity.fetch_url_text", return_value="Some reference text."):
        task_id = orch.create_task(
            biz_id, "Research a niche", department="research",
            permission_level_required=2, task_type="research_opportunity",
            task_input={"topic": "AI recipe apps", "reference_urls": ["https://example.com/y"]},
        )
        client = _FakeCostClient(VALID_ASSESSMENT_JSON, cost_usd_per_call=0.02)
        outcomes = executor.run_once(db, orch, client=client)

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    expected_cost_arc = 0.02 * executor.ARC_PER_USD
    expected_reward_arc = executor.CONFIDENCE_REWARD_ARC["medium"]
    assert abs(task["cost_arc"] - expected_cost_arc) < 1e-9, (
        f"expected cost_arc={expected_cost_arc}, got {task['cost_arc']}"
    )

    agent_after = db.query_one("SELECT * FROM agents WHERE id=?", (agent_id,))
    expected_balance = 1000.0 - expected_cost_arc + expected_reward_arc
    assert abs(agent_after["arc_balance"] - expected_balance) < 1e-9, (
        f"expected balance={expected_balance}, got {agent_after['arc_balance']}"
    )

    ledger = db.query("SELECT * FROM arc_ledger WHERE task_id=? ORDER BY id", (task_id,))
    entry_types = [r["entry_type"] for r in ledger]
    assert "spend" in entry_types and "earn" in entry_types, (
        f"expected both a real charge and a real reward ledger entry, got: {entry_types}"
    )
    print("PASS: a completed research_opportunity task charges its real estimated LLM cost "
          "and rewards by confidence tier — both traceable in the real arc_ledger, not 0.0/0.0")
    db.close()
    os.remove(TEST_DB_PATH)


def test_insufficient_arc_balance_fails_the_task_not_silently_skips_the_charge():
    """An agent with no/low ARC balance hitting a real cost should fail
    loudly (InsufficientArcError surfaces as a normal task failure via
    executor's existing try/except), never silently complete without
    actually charging — that would make the ledger lie."""
    db, orch, biz_id, agent_id = _setup()
    # Deliberately do NOT fund this agent — starts at arc_balance=0.

    with patch("tasks.research_opportunity.fetch_url_text", return_value="Some reference text."):
        task_id = orch.create_task(
            biz_id, "Research a niche", department="research",
            permission_level_required=2, task_type="research_opportunity",
            task_input={"topic": "AI recipe apps"},
        )
        client = _FakeCostClient(VALID_ASSESSMENT_JSON, cost_usd_per_call=1.0)  # expensive
        outcomes = executor.run_once(db, orch, client=client)

    assert outcomes == [(task_id, outcomes[0][1])]
    assert outcomes[0][1].startswith("failed:"), outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "failed"
    opp_count = db.query_one("SELECT COUNT(*) as c FROM opportunities")["c"]
    assert opp_count == 0, "no opportunity row should be saved when the task ultimately failed"
    print("PASS: an agent without enough ARC balance to cover the real cost fails the task "
          "loudly instead of completing for free")
    db.close()
    os.remove(TEST_DB_PATH)


def test_research_roblox_trend_task_gets_executed_and_saved():
    db, orch, biz_id, agent_id = _setup()
    with patch("tasks.research_roblox_trend.fetch_url_text", return_value="Some reference text."):
        task_id = orch.create_task(
            biz_id, "Research a Roblox concept", department="research",
            permission_level_required=2, task_type="research_roblox_trend",
            task_input={"concept": "obby with a twist", "reference_urls": ["https://example.com/z"]},
        )
        client = MockClient(canned_response=VALID_ROBLOX_ASSESSMENT_JSON)
        outcomes = executor.run_once(db, orch, client=client)

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"
    assert "Roblox trend assessment saved" in task["result"]

    trend = db.query_one("SELECT * FROM roblox_trends WHERE task_id=?", (task_id,))
    assert trend is not None, "expected a row in roblox_trends"
    assert trend["concept"] == "obby with a twist"
    assert trend["confidence_level"] == "medium"
    assert json.loads(trend["reference_urls_used"]) == ["https://example.com/z"]
    print("PASS: a research_roblox_trend task gets executed and saves a real roblox_trends row")
    db.close()
    os.remove(TEST_DB_PATH)


def test_research_app_feasibility_task_gets_executed_and_saved():
    db, orch, biz_id, agent_id = _setup()
    with patch("tasks.research_app_feasibility.fetch_url_text", return_value="Some reference text."):
        task_id = orch.create_task(
            biz_id, "Assess app feasibility", department="research",
            permission_level_required=2, task_type="research_app_feasibility",
            task_input={"concept": "a habit tracker app", "reference_urls": ["https://example.com/w"]},
        )
        client = MockClient(canned_response=VALID_APP_FEASIBILITY_ASSESSMENT_JSON)
        outcomes = executor.run_once(db, orch, client=client)

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"
    assert "App feasibility assessment saved" in task["result"]

    assessment = db.query_one("SELECT * FROM app_feasibility_assessments WHERE task_id=?", (task_id,))
    assert assessment is not None, "expected a row in app_feasibility_assessments"
    assert assessment["concept"] == "a habit tracker app"
    assert assessment["confidence_level"] == "medium"
    assert assessment["complexity_tier"] == "moderate"
    assert json.loads(assessment["reference_urls_used"]) == ["https://example.com/w"]
    print("PASS: a research_app_feasibility task gets executed and saves a real app_feasibility_assessments row")
    db.close()
    os.remove(TEST_DB_PATH)


def test_bad_model_json_fails_the_task_loudly():
    db, orch, biz_id, agent_id = _setup()
    task_id = orch.create_task(
        biz_id, "Research a niche", department="research",
        permission_level_required=2, task_type="research_opportunity",
        task_input={"topic": "Some topic"},
    )
    client = MockClient(canned_response="not valid json")
    outcomes = executor.run_once(db, orch, client=client)

    assert outcomes == [(task_id, outcomes[0][1])]
    assert outcomes[0][1].startswith("failed:")
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "failed"
    assert "did not return valid JSON" in task["result"]
    opp_count = db.query_one("SELECT COUNT(*) as c FROM opportunities")["c"]
    assert opp_count == 0, "no opportunity row should be saved for a failed assessment"
    print("PASS: invalid model JSON fails the task with the real error, saves nothing fabricated")
    db.close()
    os.remove(TEST_DB_PATH)


def test_manual_tasks_are_never_auto_executed():
    db, orch, biz_id, agent_id = _setup()
    task_id = orch.create_task(
        biz_id, "A manual task nobody should auto-run", department="research",
        permission_level_required=2,  # task_type defaults to 'manual'
    )
    task_before = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task_before["status"] == "assigned"  # auto-assigned, but NOT auto-executed

    outcomes = executor.run_once(db, orch, client=MockClient())
    assert outcomes == [], f"manual task should never be picked up by the executor: {outcomes}"

    task_after = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task_after["status"] == "assigned", "manual task's status must be untouched"
    print("PASS: task_type='manual' tasks are never auto-executed, exactly as before")
    db.close()
    os.remove(TEST_DB_PATH)


def test_queued_typed_tasks_are_not_touched_until_assigned():
    db, orch, biz_id, agent_id = _setup()
    # No agent in this department -> task stays 'queued', not 'assigned'
    task_id = orch.create_task(
        biz_id, "Research something", department="nonexistent-department",
        permission_level_required=2, task_type="research_opportunity",
        task_input={"topic": "x"},
    )
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "queued"

    outcomes = executor.run_once(db, orch, client=MockClient(canned_response=VALID_ASSESSMENT_JSON))
    assert outcomes == [], "a queued (not yet assigned) task must not be executed"
    print("PASS: a queued (unassigned) typed task is left alone until it's actually assigned")
    db.close()
    os.remove(TEST_DB_PATH)


def test_queued_task_gets_retried_and_executed_once_an_agent_becomes_available():
    """This is the exact real-world scenario the owner hit: a task is
    created while no eligible agent exists (stays 'queued'), and an
    eligible agent only shows up afterward. Before retry_queued_tasks()
    existed, that task was orphaned forever."""
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)

    biz_id = businesses.create("Retry Test Co", "opportunity_discovery", "test")
    # No agent exists yet — this task must stay queued.
    task_id = orch.create_task(
        biz_id, "Research a niche", department="research",
        permission_level_required=2, task_type="research_opportunity",
        task_input={"topic": "Some topic"},
    )
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "queued"

    # Now an eligible agent shows up.
    agent_id = agents.create(biz_id, "Late Researcher", role="Research Analyst",
                              department="research", permission_level=2)
    agents.set_status(agent_id, "idle")

    with patch("tasks.research_opportunity.fetch_url_text", return_value=""):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response=VALID_ASSESSMENT_JSON))

    assert outcomes == [(task_id, "completed")], (
        f"expected the previously-queued task to be retried, assigned, and executed "
        f"in the same pass, got: {outcomes}"
    )
    task_after = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task_after["status"] == "completed"
    print("PASS: a task queued before any eligible agent existed is retried, assigned, "
          "and executed once one becomes available — the exact bug found via real usage")
    db.close()
    os.remove(TEST_DB_PATH)


def test_reconcile_stuck_agents_resets_an_agent_with_no_live_task():
    """Crash-recovery scenario: an agent shows status='working' but has
    no task of its own in ('assigned','in_progress') -- the signature of
    a process restart landing between complete_task()'s separate,
    individually-committed writes (task status, then agent status).
    Without reconcile_stuck_agents(), this agent would be permanently
    unassignable (_try_assign only picks 'created'/'idle'/'active')."""
    db, orch, biz_id, agent_id = _setup()
    # Simulate the crash gap directly: force 'working' with no live task,
    # exactly what complete_task() could leave behind if killed between
    # its task-status write and its agent-status write.
    db.execute("UPDATE agents SET status='working' WHERE id=?", (agent_id,))

    reset_ids = orch.reconcile_stuck_agents()

    assert reset_ids == [agent_id], reset_ids
    agent = db.query_one("SELECT * FROM agents WHERE id=?", (agent_id,))
    assert agent["status"] == "idle"
    print("PASS: an agent stuck in 'working' with no live task is reset to 'idle'")
    db.close()
    os.remove(TEST_DB_PATH)


def test_reconcile_stuck_agents_leaves_a_genuinely_busy_agent_alone():
    """The other half of the same guarantee: an agent legitimately
    working on an in-flight task must NOT be touched -- reconcile_stuck_
    agents() is a crash-recovery sweep, not a way to interrupt real
    work."""
    db, orch, biz_id, agent_id = _setup()
    orch.create_task(biz_id, "Research a niche", department="research",
                      permission_level_required=2, task_type="research_opportunity",
                      task_input={"topic": "x"})
    agent = db.query_one("SELECT * FROM agents WHERE id=?", (agent_id,))
    assert agent["status"] == "working", "sanity check: the task should have been assigned"

    reset_ids = orch.reconcile_stuck_agents()

    assert reset_ids == []
    agent_after = db.query_one("SELECT * FROM agents WHERE id=?", (agent_id,))
    assert agent_after["status"] == "working", "a genuinely busy agent must not be reset"
    print("PASS: an agent with a live assigned task is left alone by the reconciliation sweep")
    db.close()
    os.remove(TEST_DB_PATH)


def test_run_once_heals_a_stuck_agent_and_makes_it_assignable_again_same_pass():
    """Integration-level proof: the sweep is actually wired into
    executor.run_once() (not just unit-tested in isolation), and healing
    happens early enough in the pass that the now-idle agent can pick up
    a brand-new task in that SAME pass -- proving this isn't just a
    cosmetic status flip."""
    db, orch, biz_id, agent_id = _setup()
    db.execute("UPDATE agents SET status='working' WHERE id=?", (agent_id,))

    task_id = orch.create_task(biz_id, "Research a niche", department="research",
                                permission_level_required=2, task_type="research_opportunity",
                                task_input={"topic": "x"})
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "queued", (
        "sanity check: with the agent stuck 'working', creation should find no eligible agent"
    )

    with patch("tasks.research_opportunity.fetch_url_text", return_value=""):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response=VALID_ASSESSMENT_JSON))

    assert outcomes == [(task_id, "completed")], outcomes
    print("PASS: run_once() heals a crash-stuck agent and it becomes assignable again "
          "within that same pass")
    db.close()
    os.remove(TEST_DB_PATH)


def _setup_trading(db, biz_id, drawdown_halt_pct=None):
    """Creates a permission_level=3 trading agent plus a fresh paper
    portfolio + active v1 strategy for biz_id. Returns (trading_agent_id,
    portfolio_id, params)."""
    agents = AgentRegistry(db)
    trading_agent_id = agents.create(biz_id, "Trading Agent", role="Paper Trading Analyst",
                                      permission_level=3)
    agents.set_status(trading_agent_id, "idle")

    portfolio_id = new_id("port")
    db.execute(
        "INSERT INTO paper_portfolios (id, business_id, starting_cash_usd, cash_usd) "
        "VALUES (?, ?, ?, ?)", (portfolio_id, biz_id, 10000.0, 10000.0),
    )
    params = dict(DEFAULT_STRATEGY_PARAMS)
    if drawdown_halt_pct is not None:
        params["drawdown_halt_pct"] = drawdown_halt_pct
    db.execute(
        "INSERT INTO trading_strategy_versions (id, business_id, version, parameters, "
        "rationale, source, active) VALUES (?, ?, 1, ?, ?, 'system', 1)",
        (new_id("strat"), biz_id, json.dumps(params), "Initial default strategy parameters."),
    )
    return trading_agent_id, portfolio_id, params


def test_trading_cycle_task_executes_a_paper_trade_and_records_a_snapshot():
    db, orch, biz_id, agent_id = _setup()
    trading_agent_id, portfolio_id, params = _setup_trading(db, biz_id)

    task_id = orch.create_task(biz_id, "Run a paper-trading cycle",
                                permission_level_required=3, task_type="trading_cycle")

    canned = json.dumps({"decisions": [
        {"symbol": s, "action": "buy" if s == "AAPL" else "hold",
         "confidence_level": "high", "size_pct": 0.05, "rationale": "test rationale"}
        for s in params["watchlist"]
    ]})
    fake_market = _FakeMarketClient({s: 100.0 for s in params["watchlist"]})

    with patch("executor.market_data.get_default_client", return_value=fake_market):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response=canned))

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"

    trades = db.query("SELECT * FROM paper_trades WHERE portfolio_id=?", (portfolio_id,))
    assert len(trades) == 1 and trades[0]["symbol"] == "AAPL" and trades[0]["side"] == "buy"
    assert trades[0]["rationale"] == "test rationale"

    portfolio = db.query_one("SELECT * FROM paper_portfolios WHERE id=?", (portfolio_id,))
    assert portfolio["cash_usd"] < 10000.0, "cash should have decreased after a real buy"

    snapshots = db.query("SELECT * FROM trading_snapshots WHERE portfolio_id=?", (portfolio_id,))
    assert len(snapshots) == 1

    print("PASS: a trading_cycle task executes a real paper trade, updates cash/positions, "
          "and records an equity snapshot")
    db.close()
    os.remove(TEST_DB_PATH)


def test_trading_cycle_drawdown_halt_pauses_the_trading_agent():
    db, orch, biz_id, agent_id = _setup()
    trading_agent_id, portfolio_id, params = _setup_trading(db, biz_id, drawdown_halt_pct=0.05)

    # Seed a much higher prior peak equity so this cycle's flat equity
    # already represents a >5% drawdown from that peak.
    db.execute(
        "INSERT INTO trading_snapshots (id, portfolio_id, strategy_version, equity_usd, "
        "cash_usd, open_positions) VALUES (?, ?, 1, 20000.0, 20000.0, 0)",
        (new_id("snap"), portfolio_id),
    )

    task_id = orch.create_task(biz_id, "Run a paper-trading cycle",
                                permission_level_required=3, task_type="trading_cycle")
    canned = json.dumps({"decisions": [
        {"symbol": s, "action": "hold", "confidence_level": "high", "size_pct": 0.0, "rationale": "x"}
        for s in params["watchlist"]
    ]})
    fake_market = _FakeMarketClient({s: 100.0 for s in params["watchlist"]})

    with patch("executor.market_data.get_default_client", return_value=fake_market):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response=canned))

    assert outcomes == [(task_id, "completed")], outcomes
    agent = db.query_one("SELECT * FROM agents WHERE id=?", (trading_agent_id,))
    assert agent["status"] == "paused", agent["status"]
    print("PASS: a drawdown past the active strategy's halt threshold automatically pauses "
          "the trading agent -- the circuit breaker is real, not just documented")
    db.close()
    os.remove(TEST_DB_PATH)


def test_trading_cycle_without_a_portfolio_fails_loudly():
    db, orch, biz_id, agent_id = _setup()
    agents = AgentRegistry(db)
    trading_agent_id = agents.create(biz_id, "Trading Agent", role="x", permission_level=3)
    agents.set_status(trading_agent_id, "idle")

    task_id = orch.create_task(biz_id, "Run a paper-trading cycle",
                                permission_level_required=3, task_type="trading_cycle")
    outcomes = executor.run_once(db, orch, client=MockClient(canned_response="{}"))

    assert len(outcomes) == 1 and outcomes[0][0] == task_id
    assert outcomes[0][1].startswith("failed:") and "portfolio" in outcomes[0][1]
    print("PASS: a trading_cycle task with no portfolio set up fails loudly with a clear "
          "error, never silently no-ops")
    db.close()
    os.remove(TEST_DB_PATH)


def test_trading_strategy_review_task_promotes_a_new_validated_version():
    db, orch, biz_id, agent_id = _setup()
    trading_agent_id, portfolio_id, params = _setup_trading(db, biz_id)

    task_id = orch.create_task(biz_id, "Review paper-trading strategy performance",
                                permission_level_required=3, task_type="trading_strategy_review")

    proposal = json.dumps({
        "parameters": {**params, "max_position_pct": 0.10},
        "rationale": "Tightening after a thin trade history.",
        "confidence_level": "medium",
    })
    outcomes = executor.run_once(db, orch, client=MockClient(canned_response=proposal))

    assert outcomes == [(task_id, "completed")], outcomes
    versions = db.query(
        "SELECT * FROM trading_strategy_versions WHERE business_id=? ORDER BY version",
        (biz_id,),
    )
    assert len(versions) == 2
    assert versions[0]["active"] == 0
    assert versions[1]["active"] == 1
    assert json.loads(versions[1]["parameters"])["max_position_pct"] == 0.10
    print("PASS: a trading_strategy_review task promotes a new, validated strategy version "
          "and deactivates the previous one -- full version history preserved, no code touched")
    db.close()
    os.remove(TEST_DB_PATH)


def test_trading_strategy_review_rejects_an_out_of_bounds_proposal():
    db, orch, biz_id, agent_id = _setup()
    trading_agent_id, portfolio_id, params = _setup_trading(db, biz_id)

    task_id = orch.create_task(biz_id, "Review paper-trading strategy performance",
                                permission_level_required=3, task_type="trading_strategy_review")
    proposal = json.dumps({
        "parameters": {**params, "max_position_pct": 0.95},  # past the absolute ceiling
        "rationale": "x", "confidence_level": "high",
    })
    outcomes = executor.run_once(db, orch, client=MockClient(canned_response=proposal))

    assert len(outcomes) == 1 and outcomes[0][1].startswith("failed:")
    versions = db.query("SELECT * FROM trading_strategy_versions WHERE business_id=?", (biz_id,))
    assert len(versions) == 1, "an out-of-bounds proposal must never be saved as a new version"
    print("PASS: an out-of-bounds strategy proposal fails the task loudly and is never saved "
          "as a new active version")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_summarize_urls_task_gets_executed_and_completed()
    test_research_opportunity_task_gets_executed_and_saved()
    test_research_opportunity_charges_real_cost_and_rewards_by_confidence()
    test_insufficient_arc_balance_fails_the_task_not_silently_skips_the_charge()
    test_research_roblox_trend_task_gets_executed_and_saved()
    test_research_app_feasibility_task_gets_executed_and_saved()
    test_bad_model_json_fails_the_task_loudly()
    test_manual_tasks_are_never_auto_executed()
    test_queued_typed_tasks_are_not_touched_until_assigned()
    test_queued_task_gets_retried_and_executed_once_an_agent_becomes_available()
    test_reconcile_stuck_agents_resets_an_agent_with_no_live_task()
    test_reconcile_stuck_agents_leaves_a_genuinely_busy_agent_alone()
    test_run_once_heals_a_stuck_agent_and_makes_it_assignable_again_same_pass()
    test_trading_cycle_task_executes_a_paper_trade_and_records_a_snapshot()
    test_trading_cycle_drawdown_halt_pauses_the_trading_agent()
    test_trading_cycle_without_a_portfolio_fails_loudly()
    test_trading_strategy_review_task_promotes_a_new_validated_version()
    test_trading_strategy_review_rejects_an_out_of_bounds_proposal()
    print("\nAll executor.py offline tests passed.")
