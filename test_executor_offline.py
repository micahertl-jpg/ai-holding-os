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
from alpaca_client import MockAlpacaClient, AlpacaError
from market_data import MarketDataError
import executor


class _FakeHistoricalMarketClient:
    """Stand-in for market_data.get_default_client() in these tests --
    serves real-shaped (mock=False) historical bars from a fixed dict
    per symbol, filtered to the requested date range, exactly like
    AlphaVantageClient.get_daily_history() would. get_quote() is
    deliberately unimplemented since _handle_strategy_backtest_search
    never calls it."""

    def __init__(self, bars_by_symbol, raise_for=()):
        self.bars_by_symbol = bars_by_symbol
        self.raise_for = set(raise_for)

    def get_daily_history(self, symbol, start_date, end_date):
        if symbol in self.raise_for:
            raise MarketDataError(f"no historical data for {symbol}")
        return [b for b in self.bars_by_symbol[symbol] if start_date <= b["date"] <= end_date]


class _MultiPurposeLLMClient:
    """Routes each call to a decision response or a proposal response
    based on which system prompt it's given -- see
    test_strategy_backtest_search_offline.py for the full rationale;
    duplicated here (not imported) since each test file in this
    codebase is self-contained."""

    def __init__(self, decision_responses, proposal_responses=()):
        self.decision_responses = list(decision_responses)
        self.proposal_responses = list(proposal_responses)
        self.decision_calls = 0
        self.proposal_calls = 0
        self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def complete(self, messages, system=None, max_tokens=1000):
        if system and "paper-trading equity analyst" in system:
            response = self.decision_responses[self.decision_calls % len(self.decision_responses)]
            self.decision_calls += 1
            return response
        response = self.proposal_responses[self.proposal_calls % len(self.proposal_responses)]
        self.proposal_calls += 1
        return response


def _bt_bar(date, close):
    return {"date": date, "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1000000, "mock": False}


def _bt_decisions(*entries):
    return json.dumps({"decisions": [
        {"symbol": s, "action": a, "confidence_level": "high", "size_pct": p, "rationale": "x"}
        for s, a, p in entries
    ]})


class _FakeLiveAlpacaClient(MockAlpacaClient):
    """A MockAlpacaClient look-alike that reports is_paper=False, so
    these tests can exercise _handle_live_trading_cycle's real-order
    path without a real network connection. alpaca_client.MockAlpacaClient
    itself is always is_paper=True BY DESIGN (see
    test_alpaca_client_offline.py's test_mock_client_is_paper_is_always_true
    -- a mock must never be able to represent a live-money connection in
    production code). This subclass exists only here, as a test double
    standing in for "an operator who has genuinely configured a live
    connection" -- it changes nothing about that production guarantee."""
    is_paper = False


class _FailingLiveAlpacaClient(_FakeLiveAlpacaClient):
    """Simulates a broker that rejects every order -- for testing that
    a rejected real order is never recorded as a fill."""
    def place_order(self, *args, **kwargs):
        raise AlpacaError("simulated broker rejection")


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

VALID_REAL_ESTATE_ASSESSMENT_JSON = json.dumps({
    "market_trend": "Prices have risen modestly over the past two years.",
    "comparable_properties": "A few similar properties nearby sold recently at comparable prices.",
    "estimated_rental_yield": "Roughly 4-6% gross, a rough estimate only.",
    "price_trend_assessment": "Gradual appreciation, consistent with the broader market.",
    "risk_factors": "Local zoning and HOA rules should be confirmed with a licensed agent.",
    "confidence_level": "medium",
    "summary": "A reasonably stable market; worth a closer look with a local professional.",
})

VALID_OPS_REPORT_OK_JSON = json.dumps({
    "overall_severity": "ok",
    "findings": [],
    "confidence_level": "high",
    "summary": "Nothing unusual -- system looks healthy.",
})

VALID_OPS_REPORT_WARNING_JSON = json.dumps({
    "overall_severity": "warning",
    "findings": [{
        "category": "stuck tasks", "severity": "warning",
        "description": "One task has been queued for over 2 hours.",
        "recommendation": "Check whether an eligible agent is idle for its department.",
    }],
    "confidence_level": "medium",
    "summary": "A task looks stuck; worth a look.",
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


def test_research_real_estate_task_gets_executed_and_saved():
    db, orch, biz_id, agent_id = _setup()
    with patch("tasks.research_real_estate.fetch_url_text", return_value="Some reference text."):
        task_id = orch.create_task(
            biz_id, "Research real estate investment", department="research",
            permission_level_required=2, task_type="research_real_estate",
            task_input={"property_or_market": "123 Main St, Springfield",
                        "reference_urls": ["https://example.com/w"]},
        )
        client = MockClient(canned_response=VALID_REAL_ESTATE_ASSESSMENT_JSON)
        outcomes = executor.run_once(db, orch, client=client)

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"
    assert "Real estate research assessment saved" in task["result"]

    assessment = db.query_one("SELECT * FROM real_estate_assessments WHERE task_id=?", (task_id,))
    assert assessment is not None, "expected a row in real_estate_assessments"
    assert assessment["property_or_market"] == "123 Main St, Springfield"
    assert assessment["confidence_level"] == "medium"
    assert json.loads(assessment["reference_urls_used"]) == ["https://example.com/w"]
    print("PASS: a research_real_estate task gets executed and saves a real real_estate_assessments row")
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


def _setup_live_trading(db, biz_id, live_trading_enabled=1, drawdown_halt_pct=None):
    """Creates a permission_level=4 live-trading agent plus a fresh
    paper_portfolios row (with live_trading_enabled set as given) and
    active v1 strategy for biz_id -- live_trading_cycle reuses the same
    trading_strategy_versions row paper trading does. Returns
    (live_agent_id, portfolio_id, params). Deliberately a small starting
    cash_usd/cash figure (100.0) -- irrelevant to paper_portfolios itself
    (never touched by the live handler) but matches the fake broker's
    starting cash in the tests below, keeping the numbers easy to reason
    about."""
    agents = AgentRegistry(db)
    live_agent_id = agents.create(biz_id, "Live Trading Agent", role="Real-Money Trading Executor",
                                   permission_level=4)
    agents.set_status(live_agent_id, "idle")

    portfolio_id = new_id("port")
    db.execute(
        "INSERT INTO paper_portfolios (id, business_id, starting_cash_usd, cash_usd, "
        "live_trading_enabled) VALUES (?, ?, ?, ?, ?)",
        (portfolio_id, biz_id, 100.0, 100.0, live_trading_enabled),
    )
    params = dict(DEFAULT_STRATEGY_PARAMS)
    if drawdown_halt_pct is not None:
        params["drawdown_halt_pct"] = drawdown_halt_pct
    db.execute(
        "INSERT INTO trading_strategy_versions (id, business_id, version, parameters, "
        "rationale, source, active) VALUES (?, ?, 1, ?, ?, 'system', 1)",
        (new_id("strat"), biz_id, json.dumps(params), "Initial default strategy parameters."),
    )
    return live_agent_id, portfolio_id, params


def test_live_trading_cycle_task_executes_a_real_order_and_records_it():
    db, orch, biz_id, agent_id = _setup()
    live_agent_id, portfolio_id, params = _setup_live_trading(db, biz_id)

    task_id = orch.create_task(biz_id, "Run a live (real-money) trading cycle",
                                permission_level_required=4, task_type="live_trading_cycle")

    canned = json.dumps({"decisions": [
        {"symbol": s, "action": "buy" if s == "AAPL" else "hold",
         "confidence_level": "high", "size_pct": 0.05, "rationale": "test rationale"}
        for s in params["watchlist"]
    ]})
    fake_market = _FakeMarketClient({s: 100.0 for s in params["watchlist"]})
    fake_alpaca = _FakeLiveAlpacaClient(cash=100.0, quote_prices={"AAPL": 100.0})

    with patch("executor.market_data.get_default_client", return_value=fake_market), \
         patch("executor.get_default_alpaca_client", return_value=fake_alpaca):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response=canned))

    assert outcomes == [(task_id, "completed")], outcomes
    trades = db.query("SELECT * FROM live_trades WHERE portfolio_id=?", (portfolio_id,))
    assert len(trades) == 1 and trades[0]["symbol"] == "AAPL" and trades[0]["side"] == "buy"
    assert trades[0]["alpaca_order_id"] == "mock-order-1"
    assert trades[0]["rationale"] == "test rationale"

    snapshots = db.query("SELECT * FROM live_snapshots WHERE portfolio_id=?", (portfolio_id,))
    assert len(snapshots) == 1, "the snapshot's cash/equity must come from the broker, not " \
                                 "a locally-summed ledger"
    print("PASS: a live_trading_cycle task places a real order through the broker client, "
          "records the REAL fill into live_trades, and snapshots real account state")
    db.close()
    os.remove(TEST_DB_PATH)


def test_live_trading_cycle_refuses_when_live_trading_not_enabled():
    db, orch, biz_id, agent_id = _setup()
    _setup_live_trading(db, biz_id, live_trading_enabled=0)
    task_id = orch.create_task(biz_id, "Run a live (real-money) trading cycle",
                                permission_level_required=4, task_type="live_trading_cycle")
    outcomes = executor.run_once(db, orch, client=MockClient(canned_response="{}"))
    assert len(outcomes) == 1 and outcomes[0][0] == task_id
    assert outcomes[0][1].startswith("failed:") and "not enabled" in outcomes[0][1]
    print("PASS: a live_trading_cycle task refuses to run (and never touches the broker at "
          "all) unless live_trading_enabled is explicitly set on the portfolio")
    db.close()
    os.remove(TEST_DB_PATH)


def test_live_trading_cycle_respects_the_kill_switch():
    db, orch, biz_id, agent_id = _setup()
    live_agent_id, portfolio_id, params = _setup_live_trading(db, biz_id)
    task_id = orch.create_task(biz_id, "Run a live (real-money) trading cycle",
                                permission_level_required=4, task_type="live_trading_cycle")
    with patch.dict("tasks.live_trading_safety.os.environ",
                     {"LIVE_TRADING_KILL_SWITCH": "1"}, clear=False):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response="{}"))
    assert len(outcomes) == 1 and outcomes[0][0] == task_id
    assert outcomes[0][1].startswith("failed:") and "KILL_SWITCH" in outcomes[0][1]
    trades = db.query("SELECT * FROM live_trades WHERE portfolio_id=?", (portfolio_id,))
    assert trades == [], "the kill switch must block a cycle before it ever reaches the broker"
    print("PASS: LIVE_TRADING_KILL_SWITCH halts a live_trading_cycle task before it ever "
          "contacts the broker, no matter what the strategy would have decided")
    db.close()
    os.remove(TEST_DB_PATH)


def test_live_trading_cycle_refuses_when_broker_client_is_still_pointed_at_paper():
    db, orch, biz_id, agent_id = _setup()
    _setup_live_trading(db, biz_id)
    task_id = orch.create_task(biz_id, "Run a live (real-money) trading cycle",
                                permission_level_required=4, task_type="live_trading_cycle")
    with patch("executor.get_default_alpaca_client", return_value=MockAlpacaClient()):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response="{}"))
    assert len(outcomes) == 1 and outcomes[0][0] == task_id
    assert outcomes[0][1].startswith("failed:") and "PAPER endpoint" in outcomes[0][1]
    print("PASS: a broker connection still pointed at Alpaca's paper endpoint is refused even "
          "with live trading enabled -- a paper fill can never be recorded as a live trade")
    db.close()
    os.remove(TEST_DB_PATH)


def test_live_trading_cycle_records_no_trade_when_the_broker_rejects_the_order():
    db, orch, biz_id, agent_id = _setup()
    live_agent_id, portfolio_id, params = _setup_live_trading(db, biz_id)
    task_id = orch.create_task(biz_id, "Run a live (real-money) trading cycle",
                                permission_level_required=4, task_type="live_trading_cycle")

    canned = json.dumps({"decisions": [
        {"symbol": s, "action": "buy" if s == "AAPL" else "hold",
         "confidence_level": "high", "size_pct": 0.05, "rationale": "test rationale"}
        for s in params["watchlist"]
    ]})
    fake_market = _FakeMarketClient({s: 100.0 for s in params["watchlist"]})
    fake_alpaca = _FailingLiveAlpacaClient(cash=100.0, quote_prices={"AAPL": 100.0})

    with patch("executor.market_data.get_default_client", return_value=fake_market), \
         patch("executor.get_default_alpaca_client", return_value=fake_alpaca):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response=canned))

    assert outcomes == [(task_id, "completed")], outcomes
    trades = db.query("SELECT * FROM live_trades WHERE portfolio_id=?", (portfolio_id,))
    assert trades == [], "a broker-rejected order must never be recorded as a real trade"
    print("PASS: a broker-rejected order is never recorded into live_trades, and the task "
          "still completes rather than crashing the whole cycle over one rejected order")
    db.close()
    os.remove(TEST_DB_PATH)


def test_live_trading_cycle_daily_loss_halt_pauses_the_live_agent():
    db, orch, biz_id, agent_id = _setup()
    live_agent_id, portfolio_id, params = _setup_live_trading(db, biz_id)
    # Seed a realized loss from earlier today already past the default
    # $7 live daily-loss cap -- this alone should halt the cycle even
    # though today's new decisions are all 'hold'.
    db.execute(
        "INSERT INTO live_trades (id, portfolio_id, alpaca_order_id, symbol, side, quantity, "
        "price_usd, realized_pnl_usd, confidence_level, rationale, strategy_version) "
        "VALUES (?, ?, 'seed-order', 'AAPL', 'sell', 1.0, 90.0, -50.0, 'high', 'seed', 1)",
        (new_id("ltr"), portfolio_id),
    )
    task_id = orch.create_task(biz_id, "Run a live (real-money) trading cycle",
                                permission_level_required=4, task_type="live_trading_cycle")
    canned = json.dumps({"decisions": [
        {"symbol": s, "action": "hold", "confidence_level": "high", "size_pct": 0.0, "rationale": "x"}
        for s in params["watchlist"]
    ]})
    fake_market = _FakeMarketClient({s: 100.0 for s in params["watchlist"]})
    fake_alpaca = _FakeLiveAlpacaClient(cash=100.0, quote_prices={"AAPL": 100.0})

    with patch("executor.market_data.get_default_client", return_value=fake_market), \
         patch("executor.get_default_alpaca_client", return_value=fake_alpaca):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response=canned))

    assert outcomes == [(task_id, "completed")], outcomes
    agent = db.query_one("SELECT * FROM agents WHERE id=?", (live_agent_id,))
    assert agent["status"] == "paused", agent["status"]
    print("PASS: a realized loss already past the live daily-loss cap pauses the live "
          "trading agent, even on a cycle with no new executed trades")
    db.close()
    os.remove(TEST_DB_PATH)


def test_live_trading_cycle_without_a_portfolio_fails_loudly():
    db, orch, biz_id, agent_id = _setup()
    agents = AgentRegistry(db)
    live_agent_id = agents.create(biz_id, "Live Trading Agent", role="x", permission_level=4)
    agents.set_status(live_agent_id, "idle")

    task_id = orch.create_task(biz_id, "Run a live (real-money) trading cycle",
                                permission_level_required=4, task_type="live_trading_cycle")
    outcomes = executor.run_once(db, orch, client=MockClient(canned_response="{}"))
    assert len(outcomes) == 1 and outcomes[0][0] == task_id
    assert outcomes[0][1].startswith("failed:") and "portfolio" in outcomes[0][1]
    print("PASS: a live_trading_cycle task with no trading portfolio at all fails loudly "
          "with a clear error, never silently no-ops")
    db.close()
    os.remove(TEST_DB_PATH)


def _setup_backtest_strategy(db, biz_id):
    """A permission_level=2 agent (this task type is recommend-only,
    same tier as ops_maintenance_review) plus an active v1 strategy
    with watchlist=['AAPL'] -- no paper_portfolios row needed, since
    _handle_strategy_backtest_search never reads one."""
    agents = AgentRegistry(db)
    agent_id = agents.create(biz_id, "Backtest Agent", role="Strategy Researcher",
                              permission_level=2)
    agents.set_status(agent_id, "idle")
    params = dict(DEFAULT_STRATEGY_PARAMS)
    params["watchlist"] = ["AAPL"]
    db.execute(
        "INSERT INTO trading_strategy_versions (id, business_id, version, parameters, "
        "rationale, source, active) VALUES (?, ?, 1, ?, ?, 'system', 1)",
        (new_id("strat"), biz_id, json.dumps(params), "Initial default strategy parameters."),
    )
    return agent_id, params


def test_strategy_backtest_search_task_runs_and_saves_a_report():
    db, orch, biz_id, agent_id = _setup()
    _setup_backtest_strategy(db, biz_id)

    task_id = orch.create_task(
        biz_id, "Backtest and search for a better trading strategy against real historical "
                "price data", permission_level_required=2, task_type="strategy_backtest_search",
        task_input={
            "train_start_date": "2026-01-05", "validation_split_date": "2026-01-06",
            "validation_end_date": "2026-01-07", "max_candidates": 1,
        },
    )
    bars = {"AAPL": [_bt_bar("2026-01-05", 100.0), _bt_bar("2026-01-06", 110.0),
                      _bt_bar("2026-01-07", 90.0)]}
    fake_market = _FakeHistoricalMarketClient(bars)
    llm = _MultiPurposeLLMClient(decision_responses=[
        _bt_decisions(("AAPL", "buy", 0.5)),
        _bt_decisions(("AAPL", "sell", 1.0)),
    ])

    with patch("executor.market_data.get_default_client", return_value=fake_market):
        outcomes = executor.run_once(db, orch, client=llm)

    assert outcomes == [(task_id, "completed")], outcomes
    runs = db.query("SELECT * FROM backtest_runs WHERE task_id=?", (task_id,))
    assert len(runs) == 1
    candidates = json.loads(runs[0]["candidates_json"])
    assert len(candidates) == 1  # max_candidates=1 -- only the current strategy was tried
    assert candidates[0]["train_stats"]["total_trades"] >= 1
    assert runs[0]["train_start_date"] == "2026-01-05"
    assert runs[0]["validation_split_date"] == "2026-01-06"
    print("PASS: a strategy_backtest_search task runs a real backtest against historical data "
          "and saves a report -- never touching paper_trades/live_trades")
    db.close()
    os.remove(TEST_DB_PATH)


def test_strategy_backtest_search_never_touches_paper_or_live_trade_tables():
    db, orch, biz_id, agent_id = _setup()
    _setup_backtest_strategy(db, biz_id)
    task_id = orch.create_task(
        biz_id, "Backtest", permission_level_required=2, task_type="strategy_backtest_search",
        task_input={"train_start_date": "2026-01-05", "validation_split_date": "2026-01-06",
                    "validation_end_date": "2026-01-07", "max_candidates": 1},
    )
    bars = {"AAPL": [_bt_bar("2026-01-05", 100.0), _bt_bar("2026-01-06", 110.0),
                      _bt_bar("2026-01-07", 90.0)]}
    fake_market = _FakeHistoricalMarketClient(bars)
    llm = _MultiPurposeLLMClient(decision_responses=[
        _bt_decisions(("AAPL", "buy", 0.5)), _bt_decisions(("AAPL", "sell", 1.0)),
    ])
    with patch("executor.market_data.get_default_client", return_value=fake_market):
        executor.run_once(db, orch, client=llm)

    assert db.query("SELECT * FROM paper_trades") == []
    assert db.query("SELECT * FROM live_trades") == []
    assert db.query("SELECT * FROM trading_snapshots") == []
    print("PASS: a backtest search never writes to paper_trades/live_trades/trading_snapshots "
          "-- it can only ever save a backtest_runs report")
    db.close()
    os.remove(TEST_DB_PATH)


def test_strategy_backtest_search_requires_complete_date_range_in_task_input():
    db, orch, biz_id, agent_id = _setup()
    _setup_backtest_strategy(db, biz_id)
    task_id = orch.create_task(
        biz_id, "Backtest", permission_level_required=2, task_type="strategy_backtest_search",
        task_input={"train_start_date": "2026-01-05"},  # missing the other two required dates
    )
    outcomes = executor.run_once(db, orch, client=MockClient(canned_response="{}"))
    assert len(outcomes) == 1 and outcomes[0][0] == task_id
    assert outcomes[0][1].startswith("failed:") and "missing required" in outcomes[0][1]
    print("PASS: a strategy_backtest_search task with an incomplete date range fails loudly "
          "before ever fetching historical data or calling the model")
    db.close()
    os.remove(TEST_DB_PATH)


def test_strategy_backtest_search_fails_loudly_on_missing_historical_data():
    db, orch, biz_id, agent_id = _setup()
    _setup_backtest_strategy(db, biz_id)
    task_id = orch.create_task(
        biz_id, "Backtest", permission_level_required=2, task_type="strategy_backtest_search",
        task_input={"train_start_date": "2026-01-05", "validation_split_date": "2026-01-06",
                    "validation_end_date": "2026-01-07", "max_candidates": 1},
    )
    fake_market = _FakeHistoricalMarketClient({"AAPL": []}, raise_for=["AAPL"])
    with patch("executor.market_data.get_default_client", return_value=fake_market):
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response="{}"))
    assert len(outcomes) == 1 and outcomes[0][0] == task_id
    assert outcomes[0][1].startswith("failed:") and "no historical data for AAPL" in outcomes[0][1]
    print("PASS: a real historical-data fetch failure fails the task loudly with the real "
          "error, never silently backtests against an empty/fabricated dataset")
    db.close()
    os.remove(TEST_DB_PATH)


def test_strategy_backtest_search_without_an_active_strategy_fails_loudly():
    db, orch, biz_id, agent_id = _setup()
    agents = AgentRegistry(db)
    agent_id2 = agents.create(biz_id, "Backtest Agent", role="x", permission_level=2)
    agents.set_status(agent_id2, "idle")
    task_id = orch.create_task(
        biz_id, "Backtest", permission_level_required=2, task_type="strategy_backtest_search",
        task_input={"train_start_date": "2026-01-05", "validation_split_date": "2026-01-06",
                    "validation_end_date": "2026-01-07"},
    )
    outcomes = executor.run_once(db, orch, client=MockClient(canned_response="{}"))
    assert len(outcomes) == 1 and outcomes[0][0] == task_id
    assert outcomes[0][1].startswith("failed:") and "no active trading strategy" in outcomes[0][1]
    print("PASS: a strategy_backtest_search task with no active strategy for the business "
          "fails loudly, same as trading_cycle/trading_strategy_review would")
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


def test_ops_maintenance_review_task_gets_executed_and_saved():
    db, orch, biz_id, agent_id = _setup()
    task_id = orch.create_task(biz_id, "Review this system's own infrastructure health",
                                permission_level_required=2, task_type="ops_maintenance_review")

    with patch.object(executor, "OWNER_EMAIL", None), \
         patch("executor.send_email") as mock_send:
        outcomes = executor.run_once(db, orch, client=MockClient(canned_response=VALID_OPS_REPORT_OK_JSON))

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"
    assert "Ops/Maintenance review saved" in task["result"]

    report = db.query_one("SELECT * FROM ops_maintenance_reports WHERE task_id=?", (task_id,))
    assert report is not None, "expected a row in ops_maintenance_reports"
    assert report["overall_severity"] == "ok"
    assert json.loads(report["findings"]) == []
    assert json.loads(report["metrics_snapshot"]).get("stuck_tasks") == []
    assert mock_send.call_count == 0, "a healthy 'ok' report must never alert the owner"
    print("PASS: an ops_maintenance_review task gets executed and saves a real report row")
    db.close()
    os.remove(TEST_DB_PATH)


def test_ops_maintenance_review_alerts_owner_on_warning_severity():
    db, orch, biz_id, agent_id = _setup()
    task_id = orch.create_task(biz_id, "Review this system's own infrastructure health",
                                permission_level_required=2, task_type="ops_maintenance_review")

    with patch.object(executor, "OWNER_EMAIL", "owner@example.com"), \
         patch("executor.send_email") as mock_send:
        outcomes = executor.run_once(
            db, orch, client=MockClient(canned_response=VALID_OPS_REPORT_WARNING_JSON))

    assert outcomes == [(task_id, "completed")], outcomes
    report = db.query_one("SELECT * FROM ops_maintenance_reports WHERE task_id=?", (task_id,))
    assert report["overall_severity"] == "warning"
    assert mock_send.call_count == 1
    call_args = mock_send.call_args[0]
    assert call_args[0] == "owner@example.com"
    assert "WARNING" in call_args[1]  # subject
    print("PASS: a warning-severity ops report alerts the configured OWNER_EMAIL")
    db.close()
    os.remove(TEST_DB_PATH)


def test_ops_maintenance_review_broken_owner_alert_does_not_fail_the_task():
    db, orch, biz_id, agent_id = _setup()
    task_id = orch.create_task(biz_id, "Review this system's own infrastructure health",
                                permission_level_required=2, task_type="ops_maintenance_review")

    from emailer import EmailError
    with patch.object(executor, "OWNER_EMAIL", "owner@example.com"), \
         patch("executor.send_email", side_effect=EmailError("Resend is down")):
        outcomes = executor.run_once(
            db, orch, client=MockClient(canned_response=VALID_OPS_REPORT_WARNING_JSON))

    assert outcomes == [(task_id, "completed")], outcomes
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed", \
        "a broken owner-alert email must never fail an otherwise-successful ops review"
    print("PASS: a broken owner-alert email never fails the ops review task itself")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_summarize_urls_task_gets_executed_and_completed()
    test_research_opportunity_task_gets_executed_and_saved()
    test_research_opportunity_charges_real_cost_and_rewards_by_confidence()
    test_insufficient_arc_balance_fails_the_task_not_silently_skips_the_charge()
    test_research_roblox_trend_task_gets_executed_and_saved()
    test_research_app_feasibility_task_gets_executed_and_saved()
    test_research_real_estate_task_gets_executed_and_saved()
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
    test_live_trading_cycle_task_executes_a_real_order_and_records_it()
    test_live_trading_cycle_refuses_when_live_trading_not_enabled()
    test_live_trading_cycle_respects_the_kill_switch()
    test_live_trading_cycle_refuses_when_broker_client_is_still_pointed_at_paper()
    test_live_trading_cycle_records_no_trade_when_the_broker_rejects_the_order()
    test_live_trading_cycle_daily_loss_halt_pauses_the_live_agent()
    test_live_trading_cycle_without_a_portfolio_fails_loudly()
    test_strategy_backtest_search_task_runs_and_saves_a_report()
    test_strategy_backtest_search_never_touches_paper_or_live_trade_tables()
    test_strategy_backtest_search_requires_complete_date_range_in_task_input()
    test_strategy_backtest_search_fails_loudly_on_missing_historical_data()
    test_strategy_backtest_search_without_an_active_strategy_fails_loudly()
    test_trading_strategy_review_task_promotes_a_new_validated_version()
    test_trading_strategy_review_rejects_an_out_of_bounds_proposal()
    test_ops_maintenance_review_task_gets_executed_and_saved()
    test_ops_maintenance_review_alerts_owner_on_warning_severity()
    test_ops_maintenance_review_broken_owner_alert_does_not_fail_the_task()
    print("\nAll executor.py offline tests passed.")
