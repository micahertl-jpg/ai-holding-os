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

from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator
from llm_client import MockClient
import executor


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


if __name__ == "__main__":
    test_summarize_urls_task_gets_executed_and_completed()
    test_research_opportunity_task_gets_executed_and_saved()
    test_research_opportunity_charges_real_cost_and_rewards_by_confidence()
    test_insufficient_arc_balance_fails_the_task_not_silently_skips_the_charge()
    test_research_roblox_trend_task_gets_executed_and_saved()
    test_bad_model_json_fails_the_task_loudly()
    test_manual_tasks_are_never_auto_executed()
    test_queued_typed_tasks_are_not_touched_until_assigned()
    test_queued_task_gets_retried_and_executed_once_an_agent_becomes_available()
    print("\nAll executor.py offline tests passed.")
