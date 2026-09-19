"""
test_permission_levels_offline.py — real tests for the full 0-7
permission-level ladder (permission_levels.py) and its two enforced
behaviors: level 6 (REQUIRE_APPROVAL, pre-existing) always routes to
the human ApprovalQueue, and level 7 (HUMAN_ONLY, new) is never
auto-executed by executor.py even after the owner approves it — only
the owner can complete it by hand. Also proves an OBSERVE_ONLY (0)
agent is never auto-assigned real work, even for a task that itself
only requires level 0.
"""

import os
import json

from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator
import executor
from permission_levels import LEVEL_NAMES, APPROVAL_REQUIRED_LEVEL, HUMAN_ONLY_LEVEL

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_permission_levels.db")


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)
    biz_id = businesses.create("Perm Test Co", "test", "test")
    return db, orch, agents, biz_id


def test_all_eight_levels_are_named():
    assert set(LEVEL_NAMES.keys()) == set(range(8))
    for lvl in range(8):
        assert isinstance(LEVEL_NAMES[lvl], str) and LEVEL_NAMES[lvl]
    print("PASS: all 8 permission levels (0-7) have real names, not just bare integers")


def test_observe_only_agent_never_gets_real_work():
    db, orch, agents, biz_id = _setup()
    agent_id = agents.create(biz_id, "Watcher", role="Observer", department="research",
                              permission_level=0)
    agents.set_status(agent_id, "idle")

    # Even a task that ITSELF only requires level 0 must not be handed
    # to a level-0 agent — OBSERVE_ONLY means never assigned real work.
    task_id = orch.create_task(biz_id, "Just observe", department="research",
                                permission_level_required=0)
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "queued", (
        f"expected an OBSERVE_ONLY agent to never be assigned, got status={task['status']}"
    )
    assert task["agent_id"] is None
    print("PASS: an OBSERVE_ONLY (level 0) agent is never auto-assigned real work, "
          "even for a level-0 task")
    db.close()
    os.remove(TEST_DB_PATH)


def test_level_6_always_requires_approval_even_with_cleared_agent():
    db, orch, agents, biz_id = _setup()
    agent_id = agents.create(biz_id, "Trusted", role="Senior", department="research",
                              permission_level=7)
    agents.set_status(agent_id, "idle")

    task_id = orch.create_task(biz_id, "Needs sign-off", department="research",
                                permission_level_required=APPROVAL_REQUIRED_LEVEL)
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "awaiting_approval", (
        "a level-6 task must always go to human approval, regardless of agent clearance"
    )
    print("PASS: a level-6 (REQUIRE_APPROVAL) task always routes to human approval, "
          "even when a fully-cleared agent exists")
    db.close()
    os.remove(TEST_DB_PATH)


def test_level_7_task_never_auto_executed_even_after_approval():
    """The real bug this closes: before permission_level_required was
    checked in executor.py's query, a level-7 task using an executable
    task_type (e.g. research_opportunity) would get silently
    auto-executed by the executor the moment it reached 'assigned'
    status after approval — defeating "human-only" entirely."""
    db, orch, agents, biz_id = _setup()
    agent_id = agents.create(biz_id, "Trusted", role="Senior", department="research",
                              permission_level=7)
    agents.set_status(agent_id, "idle")

    task_id = orch.create_task(
        biz_id, "Human-only research", department="research",
        permission_level_required=HUMAN_ONLY_LEVEL,
        task_type="research_opportunity", task_input={"topic": "x", "reference_urls": []},
    )
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "awaiting_approval"

    # Owner approves it — this is the exact promote_approved_task() path
    # that moves the task to 'assigned', which is where the old bug lived.
    appr = db.query_one("SELECT * FROM approvals WHERE task_id=?", (task_id,))
    orch.approvals.approve(appr["id"], notes="looks fine")
    orch.promote_approved_task(appr["id"])
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "assigned"

    # Now run the executor — a level-7 task must NOT be picked up, even
    # though its task_type is in executor.HANDLERS and it's 'assigned'.
    outcomes = executor.run_once(db, orch, client=None)
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "assigned", (
        f"a HUMAN_ONLY task must never be auto-executed, got status={task['status']}"
    )
    assert all(o[0] != task_id for o in outcomes), (
        "executor must not have touched the human-only task at all"
    )
    print("PASS: a level-7 (HUMAN_ONLY) task is never auto-executed, even after approval "
          "and even with an executable task_type — only the owner can complete it by hand")

    # Confirm the owner CAN still complete it manually.
    orch.complete_task(task_id, result="done by hand", cost_arc=0.0, reward_arc=0.0)
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"
    print("PASS: the owner can still complete a human-only task by hand")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_all_eight_levels_are_named()
    test_observe_only_agent_never_gets_real_work()
    test_level_6_always_requires_approval_even_with_cleared_agent()
    test_level_7_task_never_auto_executed_even_after_approval()
    print("\nAll permission_levels.py offline tests passed.")
