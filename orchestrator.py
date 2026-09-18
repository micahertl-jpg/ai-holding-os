"""
orchestrator.py — Task lifecycle: queued -> assigned -> in_progress ->
completed/failed, with automatic routing to the ApprovalQueue for any
task whose permission_level_required is >= 6, regardless of whether a
sufficiently-cleared agent exists (see _try_assign for why that
distinction matters).

Assignment below that threshold is deliberately simple in this MVP:
pick an active/idle agent in the right business+department with a
permission_level high enough for the task. This is a placeholder for a
real scheduler — it is NOT meant to be the final orchestration logic
once there are hundreds of agents.
"""

from db import Database, new_id
from banker import Banker
from approval import ApprovalQueue
import json


class Orchestrator:
    def __init__(self, db: Database, banker: Banker, approvals: ApprovalQueue):
        self.db = db
        self.banker = banker
        self.approvals = approvals

    def create_task(self, business_id, objective, department=None, priority=3,
                     budget_arc=0.0, permission_level_required=1, parent_task_id=None,
                     task_type="manual", task_input=None):
        """task_type/task_input are what let a task be auto-executed by
        the background worker (see executor.py) instead of only ever
        being completed manually. task_type='manual' (the default) is
        fully backward compatible — such tasks behave exactly as before
        and are never picked up by the executor."""
        tid = new_id("task")
        task_input_json = json.dumps(task_input) if task_input is not None else None
        self.db.execute(
            "INSERT INTO tasks (id, business_id, parent_task_id, objective, department, "
            "priority, budget_arc, permission_level_required, task_type, task_input) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, business_id, parent_task_id, objective, department, priority, budget_arc,
             permission_level_required, task_type, task_input_json),
        )
        self.db.audit("system", "create_task", "task", tid,
                       {"objective": objective, "task_type": task_type})
        self._try_assign(tid, business_id, department, permission_level_required)
        return tid

    def retry_queued_tasks(self):
        """Re-attempts assignment for every task still sitting in
        'queued' (i.e. it had no eligible agent when created). Without
        this, a queued task is orphaned forever — new agents joining or
        old ones freeing up never gets it a second look. Found via real
        usage: a research task created while the only agent was busy
        stayed queued even after a second, eligible agent was added.

        Called from executor.py's run loop before each execution pass,
        so 'newly assignable' and 'ready to execute' happen in the same
        heartbeat rather than needing two separate background threads
        to coincidentally line up. Returns the list of task ids that
        moved out of 'queued' this pass (to 'assigned' or
        'awaiting_approval'), for callers/tests to inspect."""
        queued = self.db.query("SELECT * FROM tasks WHERE status='queued'")
        moved = []
        for task in queued:
            before_status = task["status"]
            self._try_assign(task["id"], task["business_id"], task["department"],
                              task["permission_level_required"])
            after = self.db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
            if after["status"] != before_status:
                moved.append(task["id"])
        return moved

    def _try_assign(self, task_id, business_id, department, perm_required):
        # Consequential tasks (permission_level_required >= 6) MUST always
        # reach the human approval queue — this is checked FIRST and
        # unconditionally. A previous version of this method only routed
        # to approval if an agent with permission_level >= 6 already
        # existed; if a business had no such agent yet, the task silently
        # sat in 'queued' with no escalation at all. Found via
        # test_api_logic_offline.py, fixed here: the task's risk tier
        # decides the approval gate, independent of whether any agent
        # happens to be cleared for it.
        if perm_required >= 6:
            query = ("SELECT * FROM agents WHERE business_id=? AND status IN "
                     "('created','idle','active')")
            params = [business_id]
            if department:
                query += " AND department=?"
                params.append(department)
            query += " ORDER BY permission_level DESC, updated_at ASC LIMIT 1"
            candidate = self.db.query_one(query, tuple(params))
            candidate_id = candidate["id"] if candidate else None

            appr_id = self.approvals.request(
                action_type="execute_task",
                description=f"Task {task_id} requires human approval before execution",
                business_id=business_id, agent_id=candidate_id, risk_level="high",
                task_id=task_id,
            )
            self.db.execute("UPDATE tasks SET status='awaiting_approval', agent_id=? WHERE id=?",
                             (candidate_id, task_id))
            self.db.audit("system", "task_awaiting_approval", "task", task_id,
                           {"approval_id": appr_id, "candidate_agent": candidate_id})
            return candidate_id

        query = ("SELECT * FROM agents WHERE business_id=? AND status IN "
                 "('created','idle','active') AND permission_level >= ?")
        params = [business_id, perm_required]
        if department:
            query += " AND department=?"
            params.append(department)
        query += " ORDER BY updated_at ASC LIMIT 1"
        agent = self.db.query_one(query, tuple(params))

        if not agent:
            self.db.execute("UPDATE tasks SET status='queued' WHERE id=?", (task_id,))
            self.db.audit("system", "task_unassigned_no_agent", "task", task_id, None)
            return None

        self.db.execute("UPDATE tasks SET status='assigned', agent_id=? WHERE id=?",
                         (agent["id"], task_id))
        self.db.execute("UPDATE agents SET status='working', updated_at=datetime('now') "
                         "WHERE id=?", (agent["id"],))
        self.db.audit("system", "assign_task", "task", task_id, {"agent_id": agent["id"]})
        return agent["id"]

    def complete_task(self, task_id, result, cost_arc=0.0, reward_arc=0.0, reason=""):
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("unknown task")
        if task["status"] not in ("assigned", "in_progress"):
            raise ValueError(f"cannot complete task in status {task['status']}")

        if cost_arc > 0:
            self.banker.charge(task["agent_id"], task["business_id"], cost_arc,
                                reason=f"task cost: {task['objective']}", task_id=task_id)
        if reward_arc > 0:
            self.banker.reward(task["agent_id"], task["business_id"], reward_arc,
                                reason=reason or "verified task completion", task_id=task_id)

        self.db.execute(
            "UPDATE tasks SET status='completed', result=?, cost_arc=?, "
            "completed_at=datetime('now') WHERE id=?", (result, cost_arc, task_id),
        )
        self.db.execute("UPDATE agents SET status='idle', updated_at=datetime('now') "
                         "WHERE id=?", (task["agent_id"],))
        self.db.audit("system", "complete_task", "task", task_id,
                       {"cost_arc": cost_arc, "reward_arc": reward_arc})

    def fail_task(self, task_id, reason):
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("unknown task")
        self.db.execute("UPDATE tasks SET status='failed', result=? WHERE id=?",
                         (reason, task_id))
        if task["agent_id"]:
            self.db.execute("UPDATE agents SET status='idle', updated_at=datetime('now') "
                             "WHERE id=?", (task["agent_id"],))
        self.db.audit("system", "fail_task", "task", task_id, {"reason": reason})
        # Failed experiments/tasks are institutional memory, not automatically
        # an agent penalty — see project spec Sec. 16. Penalization is a
        # separate, deliberate call to banker.penalize() by whatever review
        # process eventually evaluates repeated failure patterns.

    def promote_approved_task(self, approval_id):
        """Called after the owner approves a request. Advances the linked
        task from 'awaiting_approval' to 'assigned', so it becomes
        completable via complete_task(). Before this existed, an approved
        task had literally no path forward — complete_task() only accepts
        'assigned'/'in_progress', so the task would stay stuck in
        'awaiting_approval' forever regardless of the owner's decision.
        Found via the owner's own click-through testing of the dashboard.

        A no-op if the approval isn't linked to a task (task_id is null —
        true for any approval requested outside the task-creation flow),
        if the approval isn't actually approved, or if the task has
        already moved past 'awaiting_approval' some other way."""
        appr = self.db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not appr or not appr["task_id"] or appr["status"] != "approved":
            return
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (appr["task_id"],))
        if not task or task["status"] != "awaiting_approval":
            return
        self.db.execute("UPDATE tasks SET status='assigned' WHERE id=?", (task["id"],))
        if task["agent_id"]:
            self.db.execute("UPDATE agents SET status='working', updated_at=datetime('now') "
                             "WHERE id=?", (task["agent_id"],))
        self.db.audit("owner", "task_approved_promoted", "task", task["id"],
                       {"approval_id": approval_id})

    def cancel_rejected_task(self, approval_id):
        """Mirror of promote_approved_task for the reject path: moves the
        linked task to 'cancelled' so it doesn't sit in 'awaiting_approval'
        indefinitely after the owner has explicitly said no."""
        appr = self.db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not appr or not appr["task_id"] or appr["status"] != "rejected":
            return
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (appr["task_id"],))
        if not task or task["status"] != "awaiting_approval":
            return
        self.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task["id"],))
        if task["agent_id"]:
            self.db.execute("UPDATE agents SET status='idle', updated_at=datetime('now') "
                             "WHERE id=?", (task["agent_id"],))
        self.db.audit("owner", "task_rejected_cancelled", "task", task["id"],
                       {"approval_id": approval_id})
