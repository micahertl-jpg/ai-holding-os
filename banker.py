"""
banker.py — The Banker: manages the internal ARC (Almost Real Currency)
economy. ARC is a virtual accounting/reward unit and NEVER represents
real-world money on its own — real USD lives only in businesses.budget_usd
and is only ever moved through the ApprovalQueue (see approval.py).

The Banker's job here (MVP scope):
  - allocate ARC budget from a business to an agent
  - reward ARC for verified, completed contribution
  - charge ARC for resources consumed (e.g. simulated model-cost)
  - refuse to let any agent go into negative balance beyond its allowance
  - keep a full ledger so every ARC unit is traceable to a reason
"""

from db import Database


class InsufficientArcError(Exception):
    pass


class Banker:
    def __init__(self, db: Database):
        self.db = db

    def allocate(self, business_id, agent_id, amount, reason="budget allocation"):
        if amount <= 0:
            raise ValueError("allocation must be positive")
        self.db.execute(
            "UPDATE agents SET budget_arc = budget_arc + ?, arc_balance = arc_balance + ? "
            "WHERE id=?",
            (amount, amount, agent_id),
        )
        self.db.execute(
            "INSERT INTO arc_ledger (agent_id, business_id, amount, entry_type, reason) "
            "VALUES (?, ?, ?, 'allocation', ?)",
            (agent_id, business_id, amount, reason),
        )
        self.db.audit("banker", "allocate_arc", "agent", agent_id,
                       {"amount": amount, "reason": reason})

    def reward(self, agent_id, business_id, amount, reason, task_id=None):
        """Reward ARC for measurable, verified contribution — never for
        raw activity. Callers should only invoke this after a task has
        actually been verified as completed/successful."""
        if amount <= 0:
            raise ValueError("reward must be positive")
        self.db.execute("UPDATE agents SET arc_balance = arc_balance + ? WHERE id=?",
                         (amount, agent_id))
        self.db.execute(
            "INSERT INTO arc_ledger (agent_id, business_id, amount, entry_type, reason, task_id) "
            "VALUES (?, ?, ?, 'earn', ?, ?)",
            (agent_id, business_id, amount, reason, task_id),
        )
        self.db.audit("banker", "reward_arc", "agent", agent_id,
                       {"amount": amount, "reason": reason, "task_id": task_id})

    def charge(self, agent_id, business_id, amount, reason, task_id=None):
        """Deducts `amount` from agent_id's ARC balance, refusing if that
        would take it negative.

        The balance check and the deduction happen as ONE atomic SQL
        statement (`UPDATE ... WHERE arc_balance >= amount`), not a
        separate SELECT-then-check-then-UPDATE — the latter has a real
        race window between the read and the write where two concurrent
        charge() calls (e.g. two /tasks/{id}/complete requests for the
        same agent) can each read the same starting balance, both decide
        they can afford it, and both deduct, together taking the balance
        below zero despite this method's whole purpose being to prevent
        exactly that. Confirmed via a forced-timing reproduction (20
        threads charging 10 against a balance of 100 with a race
        deliberately widened between read and write: all 20 "succeeded",
        final balance -100) before this fix — see
        test_db_concurrency.py's test_concurrent_charge_never_overdraws
        for the same proof without an artificial delay, run at high
        enough concurrency to hit the real window."""
        if amount <= 0:
            raise ValueError("charge must be positive")
        row = self.db.query_one("SELECT id FROM agents WHERE id=?", (agent_id,))
        if row is None:
            raise ValueError("unknown agent")

        result = self.db.execute(
            "UPDATE agents SET arc_balance = arc_balance - ? WHERE id=? AND arc_balance >= ?",
            (amount, agent_id, amount),
        )
        if result.rowcount == 0:
            raise InsufficientArcError(
                f"agent {agent_id} balance is insufficient to cover charge {amount} "
                f"(checked atomically at charge time)"
            )

        self.db.execute(
            "INSERT INTO arc_ledger (agent_id, business_id, amount, entry_type, reason, task_id) "
            "VALUES (?, ?, ?, 'spend', ?, ?)",
            (agent_id, business_id, -amount, reason, task_id),
        )
        self.db.audit("banker", "charge_arc", "agent", agent_id,
                       {"amount": amount, "reason": reason, "task_id": task_id})

    def penalize(self, agent_id, business_id, amount, reason, task_id=None):
        """Explicit penalty path, kept separate from `charge` so the ledger
        clearly distinguishes 'paid for a resource' from 'docked for waste
        or repeated failure'."""
        self.db.execute("UPDATE agents SET arc_balance = arc_balance - ? WHERE id=?",
                         (amount, agent_id))
        self.db.execute(
            "INSERT INTO arc_ledger (agent_id, business_id, amount, entry_type, reason, task_id) "
            "VALUES (?, ?, ?, 'penalty', ?, ?)",
            (agent_id, business_id, -amount, reason, task_id),
        )
        self.db.audit("banker", "penalize_arc", "agent", agent_id,
                       {"amount": amount, "reason": reason})

    def balance(self, agent_id):
        row = self.db.query_one("SELECT arc_balance FROM agents WHERE id=?", (agent_id,))
        return row["arc_balance"] if row else None

    def business_summary(self, business_id):
        rows = self.db.query(
            "SELECT entry_type, SUM(amount) as total FROM arc_ledger "
            "WHERE business_id=? GROUP BY entry_type", (business_id,)
        )
        return {r["entry_type"]: r["total"] for r in rows}
