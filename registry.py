"""
registry.py — Business and Agent registries.

Permission levels (from the project spec):
  0 OBSERVE ONLY        4 EXECUTE LOW-RISK ACTIONS
  1 RESEARCH            5 EXECUTE WITH BUDGET LIMIT
  2 RECOMMEND           6 REQUIRE HUMAN APPROVAL
  3 SIMULATE            7 HUMAN-ONLY ACTION

An agent's permission_level is the CEILING of what it may do without
triggering the approval queue. Anything at level 6+ always routes to
ApprovalQueue regardless of the agent's own level.
"""

from db import Database, new_id

AGENT_STATUSES = {
    "created", "initializing", "active", "working", "idle",
    "waiting", "improving", "paused", "failed", "quarantined", "retired",
}


class BusinessRegistry:
    def __init__(self, db: Database):
        self.db = db

    def create(self, name, type_, objective, budget_usd=0.0) -> str:
        bid = new_id("biz")
        self.db.execute(
            "INSERT INTO businesses (id, name, type, objective, budget_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (bid, name, type_, objective, budget_usd),
        )
        self.db.audit("owner", "create_business", "business", bid,
                       {"name": name, "budget_usd": budget_usd})
        return bid

    def get(self, business_id):
        return self.db.query_one("SELECT * FROM businesses WHERE id=?", (business_id,))

    def list(self, status=None):
        if status:
            return self.db.query("SELECT * FROM businesses WHERE status=?", (status,))
        return self.db.query("SELECT * FROM businesses")

    def set_status(self, business_id, status):
        assert status in {"active", "paused", "retired"}
        self.db.execute("UPDATE businesses SET status=? WHERE id=?", (status, business_id))
        self.db.audit("owner", "set_business_status", "business", business_id, {"status": status})


class AgentRegistry:
    def __init__(self, db: Database):
        self.db = db

    def create(self, business_id, name, role, department=None, manager_id=None,
               model="unassigned", permission_level=1) -> str:
        if not (0 <= permission_level <= 7):
            raise ValueError("permission_level must be 0-7")
        aid = new_id("agt")
        self.db.execute(
            "INSERT INTO agents (id, business_id, name, role, department, manager_id, "
            "status, model, permission_level) VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)",
            (aid, business_id, name, role, department, manager_id, model, permission_level),
        )
        self.db.audit("owner", "create_agent", "agent", aid,
                       {"name": name, "role": role, "permission_level": permission_level})
        return aid

    def get(self, agent_id):
        return self.db.query_one("SELECT * FROM agents WHERE id=?", (agent_id,))

    def list_by_business(self, business_id):
        return self.db.query("SELECT * FROM agents WHERE business_id=?", (business_id,))

    def set_status(self, agent_id, status, actor="system"):
        if status not in AGENT_STATUSES:
            raise ValueError(f"invalid status: {status}")
        self.db.execute("UPDATE agents SET status=?, updated_at=datetime('now') WHERE id=?",
                         (status, agent_id))
        self.db.audit(actor, "set_agent_status", "agent", agent_id, {"status": status})

    def pause(self, agent_id, reason=""):
        self.set_status(agent_id, "paused", actor="owner")
        self.db.audit("owner", "pause_agent", "agent", agent_id, {"reason": reason})

    def retire(self, agent_id, reason=""):
        self.set_status(agent_id, "retired", actor="owner")
        self.db.audit("owner", "retire_agent", "agent", agent_id, {"reason": reason})
