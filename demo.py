"""
demo.py — end-to-end smoke test of the Core AI Holding Company OS.

Run: python3 demo.py

This is SIMULATION: no real money moves, no real AI model calls happen,
and nothing here talks to the internet. It proves the data model and
control logic (business/agent registry, ARC ledger, task lifecycle,
permission gating, human approval queue, audit trail) work together
correctly. Wiring real Claude API calls into `orchestrator.complete_task`
is the very next real step once you're ready to deploy this for real.
"""

import os
from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator

DB_PATH = os.path.join(os.path.dirname(__file__), "holding_os.db")


def dashboard(db, biz_id, banker):
    biz = db.query_one("SELECT * FROM businesses WHERE id=?", (biz_id,))
    agents = db.query("SELECT * FROM agents WHERE business_id=?", (biz_id,))
    tasks = db.query("SELECT * FROM tasks WHERE business_id=?", (biz_id,))
    pending_appr = db.query("SELECT * FROM approvals WHERE business_id=? AND status='pending'",
                             (biz_id,))
    arc_summary = banker.business_summary(biz_id)

    print("=" * 60)
    print(f"BUSINESS: {biz['name']}  [{biz['status']}]  (SIMULATION)")
    print(f"  Objective: {biz['objective']}")
    print(f"  Authorized real-USD budget: ${biz['budget_usd']:.2f} (NOT MOVED — no payment "
          f"rails connected)")
    print("-" * 60)
    print("AGENTS:")
    for a in agents:
        print(f"  [{a['status']:>8}] {a['name']:<22} role={a['role']:<20} "
              f"perm_lvl={a['permission_level']}  arc_balance={a['arc_balance']:.1f}")
    print("-" * 60)
    print("TASKS:")
    for t in tasks:
        print(f"  [{t['status']:>17}] {t['objective']:<40} cost_arc={t['cost_arc']:.1f}")
    print("-" * 60)
    print(f"ARC LEDGER SUMMARY: {arc_summary}")
    print("-" * 60)
    print(f"APPROVALS AWAITING OWNER: {len(pending_appr)}")
    for p in pending_appr:
        print(f"  -> [{p['risk_level']}] {p['description']} "
              f"(${p['amount_usd']}) id={p['id']}")
    print("=" * 60)


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)  # fresh run each time for this demo
    db = Database(DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)

    # 1. Owner creates a business
    biz_id = businesses.create(
        name="Demo Ventures",
        type_="opportunity_discovery",
        objective="Prototype the core OS before committing to a real business vertical",
        budget_usd=500.00,
    )

    # 2. Owner (via this script, standing in for the future dashboard) stands
    #    up an executive + two specialist agents
    ceo_id = agents.create(biz_id, "Ada (CEO agent)", role="CEO",
                            department="executive", permission_level=3)
    research_id = agents.create(biz_id, "Rex (Research agent)", role="Research Analyst",
                                 department="research", manager_id=ceo_id, permission_level=2)
    ops_id = agents.create(biz_id, "Otto (Ops agent)", role="Operations",
                            department="operations", manager_id=ceo_id, permission_level=6)
    for aid in (ceo_id, research_id, ops_id):
        agents.set_status(aid, "idle")

    # 3. Banker allocates ARC budget from the business to each agent
    banker.allocate(biz_id, ceo_id, 100, reason="initial ops budget")
    banker.allocate(biz_id, research_id, 50, reason="initial research budget")
    banker.allocate(biz_id, ops_id, 50, reason="initial ops budget")

    # 4. A normal (low-permission) task: gets auto-assigned, no approval needed
    t1 = orch.create_task(biz_id, "Scan 5 candidate niches for market size signal",
                           department="research", permission_level_required=2, budget_arc=10)
    orch.complete_task(t1, result="Found 5 niches; 2 show promising search-volume trend.",
                        cost_arc=4, reward_arc=6, reason="delivered verifiable market signal")

    # 5. A consequential task: requires level-6 -> auto-routes to human approval,
    #    does NOT execute on its own
    t2 = orch.create_task(biz_id, "Spend $50 on a paid ad test for the top niche",
                           department="operations", permission_level_required=6, budget_arc=0)

    dashboard(db, biz_id, banker)

    pending = approvals.pending()
    if pending:
        print("\n--- Simulating the owner reviewing the queue ---")
        approvals.approve(pending[0]["id"], notes="Approved for $50 test only, one-time.")
        orch.promote_approved_task(pending[0]["id"])
        print(f"Owner approved {pending[0]['id']}. The task has moved from "
              f"'awaiting_approval' to 'assigned' and could now be completed via "
              f"complete_task() — but actually spending $50 on ads still requires a "
              f"real ad-platform integration, which does not exist yet. Approval "
              f"unblocks the decision; it doesn't perform the action.")

        t2_row = db.query_one("SELECT status FROM tasks WHERE id=?", (t2,))
        print(f"Task {t2} status is now: {t2_row['status']}")

    print("\nAudit trail (most recent 8 entries):")
    for row in db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 8"):
        print(f"  {row['created_at']}  actor={row['actor']:<8} action={row['action']}")

    db.close()
    print(f"\nSQLite file written to: {DB_PATH}")


if __name__ == "__main__":
    main()
