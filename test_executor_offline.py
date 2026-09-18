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
    test_bad_model_json_fails_the_task_loudly()
    test_manual_tasks_are_never_auto_executed()
    test_queued_typed_tasks_are_not_touched_until_assigned()
    test_queued_task_gets_retried_and_executed_once_an_agent_becomes_available()
    print("\nAll executor.py offline tests passed.")
