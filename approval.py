"""
approval.py — Human Approval Queue.

HARD RULE (per project spec, Phase 1): no agent may move real-world money,
sign a contract, or take any permission_level>=6 action without an
explicit owner decision recorded here. Nothing in this codebase currently
has the ability to execute a payment, trade, or signature — that's
intentional. This queue is the *only* place such actions could ever be
wired up, and even then only after `approve()` has been called by a
human.
"""

from db import Database, new_id

RISK_LEVELS = {"low", "medium", "high"}


class ApprovalQueue:
    def __init__(self, db: Database):
        self.db = db

    def request(self, action_type, description, business_id=None, agent_id=None,
                amount_usd=None, risk_level="medium", task_id=None):
        if risk_level not in RISK_LEVELS:
            raise ValueError("risk_level must be low|medium|high")
        req_id = new_id("appr")
        self.db.execute(
            "INSERT INTO approvals (id, action_type, description, business_id, agent_id, "
            "amount_usd, risk_level, task_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (req_id, action_type, description, business_id, agent_id, amount_usd,
             risk_level, task_id),
        )
        self.db.audit(agent_id or "system", "request_approval", "approval", req_id,
                       {"action_type": action_type, "amount_usd": amount_usd,
                        "risk_level": risk_level})
        return req_id

    def pending(self):
        return self.db.query("SELECT * FROM approvals WHERE status='pending' "
                              "ORDER BY created_at ASC")

    def approve(self, approval_id, notes=""):
        self.db.execute(
            "UPDATE approvals SET status='approved', decided_at=datetime('now'), "
            "decision_notes=? WHERE id=?", (notes, approval_id),
        )
        self.db.audit("owner", "approve_action", "approval", approval_id, {"notes": notes})

    def reject(self, approval_id, notes=""):
        self.db.execute(
            "UPDATE approvals SET status='rejected', decided_at=datetime('now'), "
            "decision_notes=? WHERE id=?", (notes, approval_id),
        )
        self.db.audit("owner", "reject_action", "approval", approval_id, {"notes": notes})

    def status(self, approval_id):
        row = self.db.query_one("SELECT status FROM approvals WHERE id=?", (approval_id,))
        return row["status"] if row else None
