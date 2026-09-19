"""
test_api_logic_offline.py — proves the business logic behind every
api.py endpoint actually works, WITHOUT needing fastapi/uvicorn
installed. This does not test the HTTP layer itself (routing, request
validation, status codes) — only the exact sequence of registry/banker/
approval/orchestrator calls each endpoint makes. See README.md "API —
status" for what this does and does not prove.
"""

import os
from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_api_logic.db")


def main():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)

    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)

    # --- mirrors POST /businesses ---
    biz_id = businesses.create("API Test Co", "test", "prove api.py's logic works", 0.0)
    assert businesses.get(biz_id) is not None
    print("PASS: create_business + get_business")

    # --- mirrors POST /businesses/{id}/agents ---
    agent_id = agents.create(biz_id, "Test Agent", role="Tester", department="qa",
                              permission_level=2)
    agents.set_status(agent_id, "idle")
    assert agents.get(agent_id)["status"] == "idle"
    print("PASS: create_agent + status transition")

    # --- mirrors POST /businesses/{id}/banker/allocate ---
    banker.allocate(biz_id, agent_id, 20, reason="test allocation")
    assert banker.balance(agent_id) == 20
    print("PASS: banker.allocate")

    # --- mirrors POST /businesses/{id}/tasks (auto-assign path) ---
    task_id = orch.create_task(biz_id, "Do a small thing", department="qa",
                                permission_level_required=2, budget_arc=5)
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "assigned", task["status"]
    print("PASS: create_task auto-assigns to a matching agent")

    # --- mirrors POST /tasks/{id}/complete ---
    orch.complete_task(task_id, result="did the thing", cost_arc=2, reward_arc=3,
                        reason="test")
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "completed"
    assert banker.balance(agent_id) == 20 - 2 + 3  # 21
    print("PASS: complete_task updates status + ARC balance correctly")

    # --- mirrors GET /businesses/{id}/dashboard's underlying queries ---
    arc_summary = banker.business_summary(biz_id)
    assert arc_summary.get("allocation") == 20
    print("PASS: business_summary aggregation")

    # --- mirrors the permission_level>=6 -> approval queue path ---
    high_risk_task = orch.create_task(biz_id, "Spend real money on something",
                                       department="qa", permission_level_required=6)
    t = db.query_one("SELECT * FROM tasks WHERE id=?", (high_risk_task,))
    assert t["status"] == "awaiting_approval"
    pending = approvals.pending()
    assert len(pending) == 1
    print("PASS: permission_level>=6 correctly routes to the approval queue, not auto-execution")

    # --- mirrors POST /approvals/{id}/approve ---
    approvals.approve(pending[0]["id"], notes="looks fine")
    assert approvals.status(pending[0]["id"]) == "approved"
    print("PASS: approve() records the decision")

    # --- mirrors what api.py's approve endpoint now ALSO does: advance the
    # linked task. This is the exact bug the owner found via the dashboard:
    # before promote_approved_task existed, an approved task had no path
    # forward at all (complete_task only accepts assigned/in_progress). ---
    orch.promote_approved_task(pending[0]["id"])
    promoted_task = db.query_one("SELECT * FROM tasks WHERE id=?", (high_risk_task,))
    assert promoted_task["status"] == "assigned", promoted_task["status"]
    print("PASS: promote_approved_task advances an approved task out of "
          "awaiting_approval so it becomes completable")

    # --- now prove it's actually completable, closing the loop end to end ---
    orch.complete_task(high_risk_task, result="ad test run", cost_arc=0, reward_arc=0)
    completed = db.query_one("SELECT * FROM tasks WHERE id=?", (high_risk_task,))
    assert completed["status"] == "completed"
    print("PASS: an approved+promoted task can be completed normally")

    # --- mirror the reject path on a second level-6 task ---
    second_high_risk = orch.create_task(biz_id, "Another consequential action",
                                         department="qa", permission_level_required=6)
    pending2 = approvals.pending()
    assert len(pending2) == 1
    approvals.reject(pending2[0]["id"], notes="not needed")
    orch.cancel_rejected_task(pending2[0]["id"])
    cancelled_task = db.query_one("SELECT * FROM tasks WHERE id=?", (second_high_risk,))
    assert cancelled_task["status"] == "cancelled", cancelled_task["status"]
    print("PASS: cancel_rejected_task moves a rejected task to 'cancelled', "
          "not left stuck in awaiting_approval")

    # --- retry_queued_tasks: a task created with no eligible agent stays
    # queued, then gets promoted once an eligible agent shows up. Real bug
    # found via the owner's live usage: without this, such a task was
    # orphaned forever. ---
    stuck_task = orch.create_task(biz_id, "Task with no agent yet",
                                   department="nobody-here-yet", permission_level_required=1)
    stuck_row = db.query_one("SELECT * FROM tasks WHERE id=?", (stuck_task,))
    assert stuck_row["status"] == "queued"

    late_agent_id = agents.create(biz_id, "Late Agent", role="Worker",
                                   department="nobody-here-yet", permission_level=1)
    agents.set_status(late_agent_id, "idle")

    moved = orch.retry_queued_tasks()
    assert stuck_task in moved, moved
    stuck_row_after = db.query_one("SELECT * FROM tasks WHERE id=?", (stuck_task,))
    assert stuck_row_after["status"] == "assigned", stuck_row_after["status"]
    print("PASS: retry_queued_tasks() promotes a previously-orphaned queued task "
          "once an eligible agent becomes available")

    db.close()
    os.remove(TEST_DB_PATH)
    print("\nAll API-logic offline checks passed — the code every api.py "
          "endpoint calls behaves correctly. The HTTP layer itself (FastAPI "
          "routing/validation) still needs to be run with fastapi installed.")


OVERVIEW_TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_overview_logic.db")


def test_overview_aggregation_spans_all_businesses():
    """Proves the exact logic behind GET /overview -- not by importing
    api.py (needs fastapi), but by running the same underlying calls the
    endpoint makes and checking the results, same convention as main()
    above. This is the fix for a real gap: the dashboard used to only
    ever show pending approvals scoped to whichever business happened to
    be selected -- an approval on a DIFFERENT business could sit
    unnoticed. approvals.pending() (used by /overview) is unscoped by
    business_id; this proves that actually holds across multiple
    businesses, not just within one."""
    if os.path.exists(OVERVIEW_TEST_DB_PATH):
        os.remove(OVERVIEW_TEST_DB_PATH)
    db = Database(OVERVIEW_TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)

    biz_a = businesses.create("Business A", "test", "x")
    biz_b = businesses.create("Business B", "test", "x")
    agent_a = agents.create(biz_a, "Agent A", role="x", permission_level=1)
    agents.set_status(agent_a, "idle")
    agent_b = agents.create(biz_b, "Agent B", role="x", permission_level=1)
    agents.set_status(agent_b, "working")  # a genuinely busy agent, not stuck

    # A level-6+ task on Business B routes to approval automatically --
    # this is the approval that would have been invisible while the
    # owner was looking at Business A.
    orch.create_task(biz_b, "spend real money on something", permission_level_required=6)

    all_pending = approvals.pending()
    assert len(all_pending) == 1
    assert all_pending[0]["business_id"] == biz_b, (
        "the approval must be visible via the GLOBAL, unscoped query even though "
        "the owner might currently have Business A selected in the dashboard"
    )

    # Mirror /overview's per-business rollup counts.
    agent_count_a = db.query_one(
        "SELECT COUNT(*) as c FROM agents WHERE business_id=?", (biz_a,)
    )["c"]
    agent_count_b = db.query_one(
        "SELECT COUNT(*) as c FROM agents WHERE business_id=?", (biz_b,)
    )["c"]
    assert agent_count_a == 1 and agent_count_b == 1

    pending_for_b = db.query_one(
        "SELECT COUNT(*) as c FROM approvals WHERE business_id=? AND status='pending'", (biz_b,)
    )["c"]
    pending_for_a = db.query_one(
        "SELECT COUNT(*) as c FROM approvals WHERE business_id=? AND status='pending'", (biz_a,)
    )["c"]
    assert pending_for_b == 1 and pending_for_a == 0

    # Mirror /overview's global agents_by_status rollup.
    agents_by_status = {
        r["status"]: r["c"]
        for r in db.query("SELECT status, COUNT(*) as c FROM agents GROUP BY status")
    }
    assert agents_by_status.get("idle") == 1
    assert agents_by_status.get("working") == 1

    db.close()
    os.remove(OVERVIEW_TEST_DB_PATH)
    print("PASS: /overview's aggregation logic correctly surfaces an approval on "
          "a business other than the one that would be selected -- the real gap "
          "this feature closes -- plus correct per-business and global rollup counts")


if __name__ == "__main__":
    main()
    test_overview_aggregation_spans_all_businesses()
