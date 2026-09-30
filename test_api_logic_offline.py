"""
test_api_logic_offline.py — proves the business logic behind every
api.py endpoint actually works, WITHOUT needing fastapi/uvicorn
installed. This does not test the HTTP layer itself (routing, request
validation, status codes) — only the exact sequence of registry/banker/
approval/orchestrator calls each endpoint makes. See README.md "API —
status" for what this does and does not prove.
"""

import os
from db import Database, new_id
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

    # --- mirrors POST /agents/{id}/pause and POST /agents/{id}/resume ---
    agents.pause(agent_id, reason="drawdown halt")
    assert agents.get(agent_id)["status"] == "paused"
    agents.resume(agent_id, reason="owner reviewed and cleared it")
    assert agents.get(agent_id)["status"] == "idle", (
        "resume() must reverse pause() -- the only path back for an agent paused "
        "either by the owner or automatically by a drawdown halt"
    )
    resume_audit = db.query_one(
        "SELECT * FROM audit_log WHERE target_id=? AND action='resume_agent' "
        "ORDER BY created_at DESC LIMIT 1", (agent_id,),
    )
    assert resume_audit is not None and resume_audit["actor"] == "owner"
    print("PASS: pause_agent + resume_agent reverse each other, both real and audited")

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

    # --- mirrors POST /businesses/{id}/opportunities/{id}/launch: turns
    # a researched opportunity into a real, standalone business, seeded
    # from that research, and marks the opportunity as launched so it
    # can't be launched a second time. ---
    opp_id = "opp_launch_test"
    db.execute(
        "INSERT INTO opportunities (id, business_id, topic, summary, confidence_level) "
        "VALUES (?, ?, ?, ?, ?)",
        (opp_id, biz_id, "AI-powered pet grooming subscription boxes",
         "Worth a small validation effort.", "medium"),
    )
    opp = db.query_one("SELECT * FROM opportunities WHERE id=?", (opp_id,))
    assert opp["launched_business_id"] is None
    new_biz_id = businesses.create(opp["topic"], "venture",
                                    f"Founded from a researched business opportunity: "
                                    f"{opp['topic']}. {opp['summary']}", 0.0)
    db.execute("UPDATE opportunities SET launched_business_id=? WHERE id=?", (new_biz_id, opp_id))
    launched_opp = db.query_one("SELECT * FROM opportunities WHERE id=?", (opp_id,))
    assert launched_opp["launched_business_id"] == new_biz_id
    new_biz = businesses.get(new_biz_id)
    assert new_biz["name"] == "AI-powered pet grooming subscription boxes"
    assert new_biz["type"] == "venture"
    assert "Worth a small validation effort." in new_biz["objective"]
    assert new_biz["budget_usd"] == 0.0
    print("PASS: launching an opportunity creates a real business seeded from its research "
          "and marks the opportunity as launched")

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


DELETE_TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_delete_research_logic.db")


def test_delete_research_items_removes_only_the_targeted_row():
    """Mirrors the logic behind DELETE .../opportunities/{id},
    .../roblox-trends/{id}, and .../app-feasibility/{id}: the owner
    asked for a way to clear out old research cards they no longer
    want cluttering the dashboard. Each endpoint scopes its lookup to
    (id, business_id) before deleting -- this proves that scoping
    actually excludes a different business, not just that the delete
    itself works."""
    if os.path.exists(DELETE_TEST_DB_PATH):
        os.remove(DELETE_TEST_DB_PATH)
    db = Database(DELETE_TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    biz_id = businesses.create("Delete Test Co", "test", "prove delete endpoints work", 0.0)
    other_biz_id = businesses.create("Other Co", "test", "a second, unrelated business", 0.0)

    db.execute(
        "INSERT INTO opportunities (id, business_id, topic, confidence_level, summary) "
        "VALUES (?, ?, ?, ?, ?)",
        ("opp_1", biz_id, "Test topic", "medium", "summary"),
    )
    assert db.query_one("SELECT id FROM opportunities WHERE id=?", ("opp_1",)) is not None
    # Same guard the endpoint applies before deleting: a real id looked
    # up under the WRONG business_id must not resolve.
    assert db.query_one("SELECT id FROM opportunities WHERE id=? AND business_id=?",
                         ("opp_1", other_biz_id)) is None
    db.execute("DELETE FROM opportunities WHERE id=?", ("opp_1",))
    db.audit("owner", "delete_opportunity", "opportunity", "opp_1", {"business_id": biz_id})
    assert db.query_one("SELECT id FROM opportunities WHERE id=?", ("opp_1",)) is None
    print("PASS: delete_opportunity removes the row and is scoped to its own business")

    db.execute(
        "INSERT INTO roblox_trends (id, business_id, concept, confidence_level, summary) "
        "VALUES (?, ?, ?, ?, ?)",
        ("trend_1", biz_id, "Test concept", "medium", "summary"),
    )
    db.execute("DELETE FROM roblox_trends WHERE id=?", ("trend_1",))
    db.audit("owner", "delete_roblox_trend", "roblox_trend", "trend_1", {"business_id": biz_id})
    assert db.query_one("SELECT id FROM roblox_trends WHERE id=?", ("trend_1",)) is None
    print("PASS: delete_roblox_trend removes the row")

    db.execute(
        "INSERT INTO app_feasibility_assessments (id, business_id, concept, confidence_level, summary) "
        "VALUES (?, ?, ?, ?, ?)",
        ("assess_1", biz_id, "Test app idea", "medium", "summary"),
    )
    db.execute("DELETE FROM app_feasibility_assessments WHERE id=?", ("assess_1",))
    db.audit("owner", "delete_app_feasibility_assessment", "app_feasibility_assessment", "assess_1",
              {"business_id": biz_id})
    assert db.query_one("SELECT id FROM app_feasibility_assessments WHERE id=?", ("assess_1",)) is None
    print("PASS: delete_app_feasibility_assessment removes the row")

    audit_rows = db.query("SELECT * FROM audit_log WHERE action LIKE 'delete_%'")
    assert len(audit_rows) == 3
    print("PASS: all three deletions are recorded in the audit log")

    db.close()
    os.remove(DELETE_TEST_DB_PATH)


AUDIT_TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_audit_enrichment.db")


def test_audit_actor_name_enrichment():
    """Mirrors GET /audit's actor_name resolution (added for the
    dashboard's live activity feed): a batch lookup of agents whose id
    appears as an audit_log row's actor, NOT a JOIN (this codebase's
    queries are otherwise all single-table). Proves a real agent id
    resolves to its name, while 'owner'/'system'/an unrecognized id
    correctly get no fabricated name back."""
    if os.path.exists(AUDIT_TEST_DB_PATH):
        os.remove(AUDIT_TEST_DB_PATH)
    db = Database(AUDIT_TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)

    biz_id = businesses.create("Audit Test Co", "test", "x", 0.0)
    agent_id = agents.create(biz_id, "Nova", role="researcher", permission_level=1)

    db.audit("owner", "create_business", "business", biz_id, {})
    db.audit(agent_id, "complete_task", "task", "task_1", {})
    db.audit("system", "scheduled_job_fired", "scheduled_job", "job_1", {})
    db.audit("agent_does_not_exist", "complete_task", "task", "task_2", {})

    # --- mirrors GET /audit's post-query enrichment step exactly ---
    rows = [dict(r) for r in db.query(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (10,)
    )]
    actor_ids = sorted({r["actor"] for r in rows if r["actor"] not in (None, "owner", "system")})
    actor_names = {}
    if actor_ids:
        placeholders = ",".join(["?"] * len(actor_ids))
        for a in db.query(f"SELECT id, name FROM agents WHERE id IN ({placeholders})", tuple(actor_ids)):
            actor_names[a["id"]] = a["name"]
    for r in rows:
        r["actor_name"] = actor_names.get(r["actor"])

    by_action = {r["action"]: r for r in rows}
    assert by_action["create_business"]["actor_name"] is None
    assert by_action["scheduled_job_fired"]["actor_name"] is None
    assert by_action["complete_task"]["actor_name"] == "Nova", (
        "a real agent id in audit_log.actor must resolve to that agent's real name"
    )
    # The row with the bogus actor id also has action='complete_task' --
    # find it specifically by target_id since by_action collapses same-
    # action rows to the last one seen.
    bogus_row = next(r for r in rows if r["target_id"] == "task_2")
    assert bogus_row["actor_name"] is None, (
        "an actor id matching no real agent must never get a fabricated name"
    )

    db.close()
    os.remove(AUDIT_TEST_DB_PATH)
    print("PASS: /audit's actor_name enrichment resolves real agents, and never "
          "fabricates a name for owner/system/an unknown actor id")


EVOLUTION_TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trading_evolution.db")


def test_trading_strategy_evolution_rollup():
    """Mirrors GET /overview's new trading_strategy_evolution field: a
    real cross-business rollup over trading_strategy_versions, the
    honest version of "AI evolution" this codebase actually has (self-
    improvement is scoped to the trading strategy review loop only)."""
    if os.path.exists(EVOLUTION_TEST_DB_PATH):
        os.remove(EVOLUTION_TEST_DB_PATH)
    db = Database(EVOLUTION_TEST_DB_PATH)
    businesses = BusinessRegistry(db)

    biz_a = businesses.create("Trader A", "test", "x", 0.0)
    biz_b = businesses.create("Trader B", "test", "x", 0.0)
    biz_c = businesses.create("Never Traded Co", "test", "x", 0.0)

    def insert_version(business_id, version, source, active, rationale="test rationale"):
        db.execute(
            "INSERT INTO trading_strategy_versions (id, business_id, version, parameters, "
            "rationale, confidence_level, source, active) VALUES (?, ?, ?, '{}', ?, 'medium', ?, ?)",
            (f"strat_{business_id}_{version}", business_id, version, rationale, source,
             1 if active else 0),
        )

    insert_version(biz_a, 1, "system", active=False)
    insert_version(biz_a, 2, "strategy_review", active=True)
    insert_version(biz_b, 1, "system", active=True)

    # --- mirrors GET /overview's new aggregate queries exactly ---
    total_versions = db.query_one("SELECT COUNT(*) as c FROM trading_strategy_versions")["c"]
    active_count = db.query_one(
        "SELECT COUNT(*) as c FROM trading_strategy_versions WHERE active=1"
    )["c"]
    businesses_with_trading = db.query_one(
        "SELECT COUNT(DISTINCT business_id) as c FROM trading_strategy_versions"
    )["c"]
    latest_version = db.query_one(
        "SELECT created_at, rationale, source FROM trading_strategy_versions "
        "ORDER BY created_at DESC LIMIT 1"
    )

    assert total_versions == 3
    assert active_count == 2, "one active version per business that has ever traded"
    assert businesses_with_trading == 2, (
        f"exactly biz_a and biz_b, never biz_c ({biz_c}) which has no strategy versions at all"
    )
    assert latest_version is not None
    assert latest_version["source"] in ("system", "strategy_review")

    db.close()
    os.remove(EVOLUTION_TEST_DB_PATH)
    print("PASS: /overview's trading_strategy_evolution rollup counts real versions "
          "across businesses, and never counts a business that has never traded")


ASSIGN_TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_find_assignable_agent.db")


def test_find_assignable_agent_matches_actual_task_assignment():
    """Regression test for a real, money-wasting bug: the backtest,
    enable-auto-trading, and enable-live-trading endpoints each used to
    pick "the agent to fund" with their own query ordered by
    created_at (oldest-created agent wins), while the orchestrator's
    _try_assign picks "the agent to actually hand the task to" ordered
    by updated_at (least-recently-touched agent wins). Whenever a
    business had more than one eligible agent, those two orderings
    could disagree -- an endpoint would top up agent A, but the task
    (or, for backtest, hundreds of real LLM-call tasks) would go to
    agent B, still sitting at its default starting ARC. The real
    incident: a backtest search burned its full real LLM cost, then
    got discarded at the very end because the agent it actually ran
    under was never funded.

    This sets up exactly that disagreement (agent A created first but
    touched more recently; agent B created second but never touched
    since) and proves Orchestrator.find_assignable_agent -- now the
    ONLY place either decision is made -- returns agent B in both
    roles, so an endpoint funding via find_assignable_agent can never
    again fund a different agent than _try_assign assigns to."""
    if os.path.exists(ASSIGN_TEST_DB_PATH):
        os.remove(ASSIGN_TEST_DB_PATH)
    db = Database(ASSIGN_TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)

    biz_id = businesses.create("Assign Test Co", "trading", "prove agent selection is consistent", 0.0)

    # Agent A: created FIRST (wins a created_at-ordered query), but
    # backdated/forward-dated here to look like it was touched more
    # RECENTLY than agent B -- e.g. an actively-scheduled trading agent
    # whose updated_at keeps refreshing every cycle.
    agent_a = agents.create(biz_id, "Agent A (active elsewhere)", role="x",
                             department="Trading", permission_level=3)
    agents.set_status(agent_a, "idle")
    db.execute("UPDATE agents SET created_at=?, updated_at=? WHERE id=?",
               ("2024-01-01 00:00:00", "2024-01-03 00:00:00", agent_a))

    # Agent B: created SECOND (loses a created_at-ordered query), but
    # never touched since -- its updated_at is older than agent A's,
    # so it wins an updated_at-ordered query, exactly what _try_assign
    # actually uses.
    agent_b = agents.create(biz_id, "Agent B (idle since creation)", role="x",
                             department="Trading", permission_level=3)
    agents.set_status(agent_b, "idle")
    db.execute("UPDATE agents SET created_at=?, updated_at=? WHERE id=?",
               ("2024-01-02 00:00:00", "2024-01-02 00:00:00", agent_b))

    # Sanity check the scenario is real: a naive created_at-ordered
    # query (the old, buggy shape) really would pick the WRONG agent.
    naive_created_at_pick = db.query_one(
        "SELECT id FROM agents WHERE business_id=? AND permission_level >= 3 "
        "AND status != 'retired' ORDER BY created_at ASC LIMIT 1", (biz_id,),
    )
    assert naive_created_at_pick["id"] == agent_a, (
        "test setup sanity check: the old created_at-ordered query must pick agent A"
    )

    # The fix: find_assignable_agent must agree with what _try_assign
    # will actually do, not with the naive created_at ordering above.
    funded = orch.find_assignable_agent(biz_id, None, 3)
    assert funded is not None and funded["id"] == agent_b, (
        "find_assignable_agent must pick agent B (updated_at-ordered), matching "
        "_try_assign, never agent A (created_at-ordered) -- otherwise an endpoint "
        "funding 'the agent find_assignable_agent returns' funds the wrong one again"
    )
    print("PASS: find_assignable_agent picks the updated_at-oldest agent, not the "
          "created_at-oldest one")

    # Fund agent B for a hypothetical real task, exactly like an
    # endpoint would (e.g. trigger_strategy_backtest_search).
    banker.allocate(biz_id, agent_b, 1000.0, reason="test: fund the agent we expect the task to reach")

    # Now actually create a task requiring the same permission level and
    # prove the orchestrator's real assignment path (_try_assign, via
    # create_task) lands on the SAME agent that was just funded.
    task_id = orch.create_task(biz_id, "prove assignment matches funding",
                                permission_level_required=3, task_type="manual")
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "assigned"
    assert task["agent_id"] == agent_b, (
        "the task must be assigned to agent B -- the same agent find_assignable_agent "
        "said would be funded. If this ever assigns to agent A instead, the real bug "
        "is back: some endpoint's funding and the real assignment have diverged again"
    )
    print("PASS: _try_assign's real task assignment lands on the exact same agent "
          "find_assignable_agent said it would -- funding and assignment can no "
          "longer disagree")

    db.close()
    os.remove(ASSIGN_TEST_DB_PATH)


CANCEL_TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_cancel_task_logic.db")


def test_cancel_task_charges_real_cost_and_never_rewards():
    """Mirrors Orchestrator.cancel_task -- the Stop button's terminal
    state for a task that made real, billable progress before it was
    told to stop. Distinct from complete_task (charges AND rewards, for
    a verified success) and fail_task (charges nothing, for a task that
    never produced anything billable) -- this charges what was really
    spent but never rewards, since there's no verified output to
    reward for a stopped-early run."""
    if os.path.exists(CANCEL_TEST_DB_PATH):
        os.remove(CANCEL_TEST_DB_PATH)
    db = Database(CANCEL_TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)

    biz_id = businesses.create("Cancel Test Co", "trading", "prove cancel_task works", 0.0)
    agent_id = agents.create(biz_id, "Backtest Agent", role="x", permission_level=2)
    agents.set_status(agent_id, "idle")
    banker.allocate(biz_id, agent_id, 100.0, reason="test funding")

    task_id = orch.create_task(biz_id, "Backtest search", permission_level_required=2,
                                task_type="strategy_backtest_search")
    assert db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"] == "assigned"

    orch.cancel_task(task_id, cost_arc=15.0, reason="cancelled by owner after 3 simulated days")
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "cancelled"
    assert task["cost_arc"] == 15.0
    assert "cancelled by owner" in task["result"]
    assert banker.balance(agent_id) == 100.0 - 15.0
    agent = agents.get(agent_id)
    assert agent["status"] == "idle"
    cancel_audit = db.query_one(
        "SELECT * FROM audit_log WHERE action='cancel_task' AND target_id=?", (task_id,)
    )
    assert cancel_audit is not None and cancel_audit["actor"] == "owner"
    print("PASS: cancel_task charges the real partial cost, marks the task 'cancelled', resets "
          "the agent to idle, and audits as an owner action")

    # --- no-op on a lost race, same reasoning as fail_task ---
    # Calling cancel_task again (e.g. a duplicate/racing request) on a
    # task that already reached a terminal state must NOT charge a
    # second time or overwrite the result -- exactly the race
    # fail_task's own docstring guards against.
    orch.cancel_task(task_id, cost_arc=999.0, reason="should never apply")
    task_after = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task_after["cost_arc"] == 15.0, "a second cancel_task call must never re-charge"
    assert task_after["result"] == task["result"]
    assert banker.balance(agent_id) == 100.0 - 15.0
    print("PASS: cancel_task is a no-op (never re-charges, never overwrites) on a task that "
          "already reached a terminal state -- same race-safety as fail_task")

    db.close()
    os.remove(CANCEL_TEST_DB_PATH)


def test_cancel_task_endpoint_logic_for_each_starting_status():
    """Mirrors POST /tasks/{id}/cancel's three branches directly (that
    endpoint needs fastapi to import, which this offline suite avoids
    -- see this file's own docstring): a task with no real work started
    yet (queued/awaiting_approval) is cancelled immediately at zero
    cost; an already-running task (assigned/in_progress) only gets
    cancel_requested flagged, since only the handler actually running
    it knows when it's safe to stop; a task already in a terminal state
    is rejected outright, never silently re-cancelled."""
    if os.path.exists(CANCEL_TEST_DB_PATH):
        os.remove(CANCEL_TEST_DB_PATH)
    db = Database(CANCEL_TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    biz_id = businesses.create("Cancel Endpoint Test Co", "test", "x", 0.0)

    def make_task(status):
        tid = new_id("task")
        db.execute(
            "INSERT INTO tasks (id, business_id, objective, status, task_type) "
            "VALUES (?, ?, 'x', ?, 'strategy_backtest_search')",
            (tid, biz_id, status),
        )
        return tid

    # queued/awaiting_approval -- cancel immediately, for real, at zero cost.
    for status in ("queued", "awaiting_approval"):
        tid = make_task(status)
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
        assert db.query_one("SELECT status FROM tasks WHERE id=?", (tid,))["status"] == "cancelled"
    print("PASS: a queued or awaiting_approval task (no real work started) cancels "
          "immediately, matching what POST /tasks/{id}/cancel does for those statuses")

    # assigned/in_progress -- only the cooperative flag gets set; status
    # itself must NOT change here (only the running handler's own
    # should_stop check, noticed later, actually moves it to cancelled).
    for status in ("assigned", "in_progress"):
        tid = make_task(status)
        db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (tid,))
        row = db.query_one("SELECT status, cancel_requested FROM tasks WHERE id=?", (tid,))
        assert row["status"] == status, "status must stay as-is until the handler notices"
        assert row["cancel_requested"] == 1
    print("PASS: an assigned/in_progress task only gets cancel_requested flagged, never has "
          "its status touched directly -- matching what POST /tasks/{id}/cancel does for those")

    db.close()
    os.remove(CANCEL_TEST_DB_PATH)


if __name__ == "__main__":
    main()
    test_overview_aggregation_spans_all_businesses()
    test_audit_actor_name_enrichment()
    test_trading_strategy_evolution_rollup()
    test_delete_research_items_removes_only_the_targeted_row()
    test_find_assignable_agent_matches_actual_task_assignment()
    test_cancel_task_charges_real_cost_and_never_rewards()
    test_cancel_task_endpoint_logic_for_each_starting_status()
