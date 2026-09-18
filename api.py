"""
api.py — thin FastAPI layer over the core OS modules (registry, banker,
approval, orchestrator). This is a real, deployable REST API — not a
mock — but it has NOT been executed in the sandbox that built it,
because `fastapi` isn't installable there (no PyPI access). See
README.md "API — status" for exactly what has and hasn't been verified,
and run `python3 -m py_compile api.py` yourself as a first sanity check
before installing anything.

Run locally (defaults to a local SQLite file, no Postgres account
needed yet):
    pip install -r requirements.txt
    uvicorn api:app --reload

Point at real Postgres:
    export DATABASE_URL=postgresql://user:pass@host:port/dbname
    uvicorn api:app --reload

This process holds ONE shared Database connection for the process
lifetime (see `lifespan` below) — fine for an MVP at low concurrency;
revisit with a real connection pool before this sees production traffic.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List
import os
import threading

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import get_database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker, InsufficientArcError
from approval import ApprovalQueue
from orchestrator import Orchestrator
from scheduler import JobRegistry, run_forever as scheduler_run_forever
import executor as executor_module

STATIC_DIR = Path(__file__).parent / "static"
SCHEDULER_POLL_INTERVAL_SECONDS = float(os.environ.get("SCHEDULER_POLL_INTERVAL_SECONDS", "15"))
EXECUTOR_POLL_INTERVAL_SECONDS = float(os.environ.get("EXECUTOR_POLL_INTERVAL_SECONDS", "10"))

# ---------------------------------------------------------------------
# Wiring — one shared Database + one instance of each module for the
# life of the process, created at startup and closed at shutdown.
# ---------------------------------------------------------------------

state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_database()
    state["db"] = db
    state["businesses"] = BusinessRegistry(db)
    state["agents"] = AgentRegistry(db)
    state["banker"] = Banker(db)
    state["approvals"] = ApprovalQueue(db)
    state["orchestrator"] = Orchestrator(db, state["banker"], state["approvals"])
    state["jobs"] = JobRegistry(db)

    stop_event = threading.Event()
    scheduler_thread = threading.Thread(
        target=scheduler_run_forever,
        args=(db, state["orchestrator"], state["jobs"], SCHEDULER_POLL_INTERVAL_SECONDS,
              stop_event),
        daemon=True,
        name="scheduler-thread",
    )
    scheduler_thread.start()
    state["scheduler_thread"] = scheduler_thread

    executor_stop_event = threading.Event()
    executor_thread = threading.Thread(
        target=executor_module.run_forever,
        args=(db, state["orchestrator"], EXECUTOR_POLL_INTERVAL_SECONDS, executor_stop_event),
        daemon=True,
        name="executor-thread",
    )
    executor_thread.start()
    state["executor_thread"] = executor_thread

    yield

    stop_event.set()
    executor_stop_event.set()
    scheduler_thread.join(timeout=5)
    executor_thread.join(timeout=5)
    db.close()


app = FastAPI(title="AI Holding Company OS — Core API", version="0.1.0", lifespan=lifespan)

# Serves dashboard.css/dashboard.js/dashboard-render.js at /static/... ,
# same-origin as the JSON API below, so the dashboard's fetch() calls
# need no CORS configuration.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/dashboard")
def dashboard_ui():
    """The owner-facing single-page dashboard. Plain HTML/CSS/JS, no
    build step, no Node/npm required — see static/dashboard.html."""
    return FileResponse(str(STATIC_DIR / "dashboard.html"))


def row_to_dict(row):
    """sqlite3.Row and psycopg2 RealDictRow both support dict(row)."""
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------

class CreateBusinessRequest(BaseModel):
    name: str
    type: Optional[str] = None
    objective: Optional[str] = None
    budget_usd: float = 0.0


class CreateAgentRequest(BaseModel):
    name: str
    role: Optional[str] = None
    department: Optional[str] = None
    manager_id: Optional[str] = None
    model: str = "unassigned"
    permission_level: int = 1


class CreateTaskRequest(BaseModel):
    objective: str
    department: Optional[str] = None
    priority: int = 3
    budget_arc: float = 0.0
    permission_level_required: int = 1


class CompleteTaskRequest(BaseModel):
    result: str
    cost_arc: float = 0.0
    reward_arc: float = 0.0
    reason: str = ""


class FailTaskRequest(BaseModel):
    reason: str


class AllocateArcRequest(BaseModel):
    agent_id: str
    amount: float
    reason: str = "budget allocation"


class DecisionRequest(BaseModel):
    notes: str = ""


class CreateScheduledJobRequest(BaseModel):
    name: str
    objective: str
    interval_seconds: int
    department: Optional[str] = None
    permission_level_required: int = 1
    budget_arc: float = 0.0
    enabled: bool = True


class SetJobEnabledRequest(BaseModel):
    enabled: bool


class ResearchOpportunityRequest(BaseModel):
    topic: str
    reference_urls: List[str] = []
    department: Optional[str] = None
    permission_level_required: int = 2
    priority: int = 3
    budget_arc: float = 0.0


class ResearchRobloxTrendRequest(BaseModel):
    concept: str
    reference_urls: List[str] = []
    department: Optional[str] = None
    permission_level_required: int = 2
    priority: int = 3
    budget_arc: float = 0.0


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------

@app.get("/health")
def health(response: Response):
    """Real health check, not a static 'ok'. Verifies the database is
    actually reachable (not just that the process is running) and that
    both background threads (scheduler, executor) are still alive.

    Returns 200 with status="ok" when everything checks out, or 503
    with status="degraded" and a `checks` breakdown otherwise — so an
    external uptime monitor (UptimeRobot, Railway's own alerts pointed
    at this route, etc.) can actually catch "the app is up but broken"
    instead of only "the app crashed"."""
    checks = {}

    try:
        state["db"].query_one("SELECT 1 AS ok")
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    sched = state.get("scheduler_thread")
    checks["scheduler_thread"] = "ok" if (sched and sched.is_alive()) else "not running"

    execu = state.get("executor_thread")
    checks["executor_thread"] = "ok" if (execu and execu.is_alive()) else "not running"

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}


# ---------------------------------------------------------------------
# Businesses
# ---------------------------------------------------------------------

@app.post("/businesses")
def create_business(req: CreateBusinessRequest):
    biz_id = state["businesses"].create(req.name, req.type, req.objective, req.budget_usd)
    return {"id": biz_id}


@app.get("/businesses")
def list_businesses():
    return [row_to_dict(r) for r in state["businesses"].list()]


@app.get("/businesses/{business_id}")
def get_business(business_id: str):
    biz = state["businesses"].get(business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="business not found")
    return row_to_dict(biz)


@app.get("/businesses/{business_id}/dashboard")
def business_dashboard(business_id: str):
    """One JSON view of everything demo.py's dashboard() function
    printed to a terminal — the real basis for a future owner UI."""
    db = state["db"]
    biz = state["businesses"].get(business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="business not found")
    agents = [row_to_dict(r) for r in state["agents"].list_by_business(business_id)]
    tasks = [row_to_dict(r) for r in
             db.query("SELECT * FROM tasks WHERE business_id=?", (business_id,))]
    pending_approvals = [row_to_dict(r) for r in
                          db.query("SELECT * FROM approvals WHERE business_id=? "
                                   "AND status='pending'", (business_id,))]
    arc_summary = state["banker"].business_summary(business_id)
    scheduled_jobs = [row_to_dict(r) for r in state["jobs"].list(business_id)]
    opportunities = [row_to_dict(r) for r in
                      db.query("SELECT * FROM opportunities WHERE business_id=? "
                               "ORDER BY created_at DESC", (business_id,))]
    roblox_trends = [row_to_dict(r) for r in
                      db.query("SELECT * FROM roblox_trends WHERE business_id=? "
                               "ORDER BY created_at DESC", (business_id,))]
    return {
        "business": row_to_dict(biz),
        "agents": agents,
        "tasks": tasks,
        "pending_approvals": pending_approvals,
        "arc_summary": arc_summary,
        "scheduled_jobs": scheduled_jobs,
        "opportunities": opportunities,
        "roblox_trends": roblox_trends,
    }


# ---------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------

@app.post("/businesses/{business_id}/agents")
def create_agent(business_id: str, req: CreateAgentRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    agent_id = state["agents"].create(
        business_id, req.name, req.role, req.department, req.manager_id,
        req.model, req.permission_level,
    )
    state["agents"].set_status(agent_id, "idle")
    return {"id": agent_id}


@app.get("/businesses/{business_id}/agents")
def list_agents(business_id: str):
    return [row_to_dict(r) for r in state["agents"].list_by_business(business_id)]


@app.post("/agents/{agent_id}/pause")
def pause_agent(agent_id: str, req: DecisionRequest):
    if not state["agents"].get(agent_id):
        raise HTTPException(status_code=404, detail="agent not found")
    state["agents"].pause(agent_id, reason=req.notes)
    return {"status": "paused"}


@app.post("/agents/{agent_id}/retire")
def retire_agent(agent_id: str, req: DecisionRequest):
    if not state["agents"].get(agent_id):
        raise HTTPException(status_code=404, detail="agent not found")
    state["agents"].retire(agent_id, reason=req.notes)
    return {"status": "retired"}


@app.get("/agents/{agent_id}/balance")
def agent_balance(agent_id: str):
    if not state["agents"].get(agent_id):
        raise HTTPException(status_code=404, detail="agent not found")
    return {"agent_id": agent_id, "arc_balance": state["banker"].balance(agent_id)}


# ---------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------

@app.post("/businesses/{business_id}/tasks")
def create_task(business_id: str, req: CreateTaskRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    task_id = state["orchestrator"].create_task(
        business_id, req.objective, department=req.department, priority=req.priority,
        budget_arc=req.budget_arc, permission_level_required=req.permission_level_required,
    )
    return {"id": task_id}


@app.get("/businesses/{business_id}/tasks")
def list_tasks(business_id: str):
    return [row_to_dict(r) for r in
            state["db"].query("SELECT * FROM tasks WHERE business_id=?", (business_id,))]


@app.post("/tasks/{task_id}/complete")
def complete_task(task_id: str, req: CompleteTaskRequest):
    try:
        state["orchestrator"].complete_task(
            task_id, req.result, cost_arc=req.cost_arc, reward_arc=req.reward_arc,
            reason=req.reason,
        )
    except InsufficientArcError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "completed"}


@app.post("/tasks/{task_id}/fail")
def fail_task(task_id: str, req: FailTaskRequest):
    try:
        state["orchestrator"].fail_task(task_id, reason=req.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "failed"}


# ---------------------------------------------------------------------
# Banker (ARC)
# ---------------------------------------------------------------------

@app.post("/businesses/{business_id}/banker/allocate")
def allocate_arc(business_id: str, req: AllocateArcRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    try:
        state["banker"].allocate(business_id, req.agent_id, req.amount, reason=req.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "allocated"}


@app.get("/businesses/{business_id}/banker/summary")
def banker_summary(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    return state["banker"].business_summary(business_id)


# ---------------------------------------------------------------------
# Scheduled jobs — recurring tasks created automatically by the
# background scheduler thread (see scheduler.py). A scheduled job
# creates a real task via the SAME orchestrator.create_task() path as
# any manual task, so it goes through identical auto-assignment and
# permission gating — a schedule is not a way to bypass the approval
# queue.
# ---------------------------------------------------------------------

@app.post("/businesses/{business_id}/scheduled-jobs")
def create_scheduled_job(business_id: str, req: CreateScheduledJobRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    try:
        job_id = state["jobs"].create(
            business_id, req.name, req.objective, req.interval_seconds,
            department=req.department, permission_level_required=req.permission_level_required,
            budget_arc=req.budget_arc, enabled=req.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": job_id}


@app.get("/businesses/{business_id}/scheduled-jobs")
def list_scheduled_jobs(business_id: str):
    return [row_to_dict(r) for r in state["jobs"].list(business_id)]


@app.post("/scheduled-jobs/{job_id}/set-enabled")
def set_job_enabled(job_id: str, req: SetJobEnabledRequest):
    if state["jobs"].get(job_id) is None:
        raise HTTPException(status_code=404, detail="scheduled job not found")
    state["jobs"].set_enabled(job_id, req.enabled)
    return {"status": "updated"}


# ---------------------------------------------------------------------
# Opportunity Discovery — the first real business vertical. Creates a
# task_type='research_opportunity' task; the executor background thread
# (executor.py) picks it up once auto-assigned and runs the actual LLM
# assessment, saving a row to `opportunities`. This endpoint itself
# does no LLM work and returns immediately — check back via
# GET .../opportunities or the dashboard once it completes (typically
# within EXECUTOR_POLL_INTERVAL_SECONDS, 10s by default).
# ---------------------------------------------------------------------

@app.post("/businesses/{business_id}/opportunities/research")
def request_opportunity_research(business_id: str, req: ResearchOpportunityRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    if len(req.reference_urls) > 3:
        raise HTTPException(status_code=400, detail="reference_urls is capped at 3")
    task_id = state["orchestrator"].create_task(
        business_id, f"Research opportunity: {req.topic}", department=req.department,
        priority=req.priority, budget_arc=req.budget_arc,
        permission_level_required=req.permission_level_required,
        task_type="research_opportunity",
        task_input={"topic": req.topic, "reference_urls": req.reference_urls},
    )
    return {"task_id": task_id}


@app.get("/businesses/{business_id}/opportunities")
def list_opportunities(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    return [row_to_dict(r) for r in
            state["db"].query("SELECT * FROM opportunities WHERE business_id=? "
                               "ORDER BY created_at DESC", (business_id,))]


# ---------------------------------------------------------------------
# Roblox Game Development — the second business vertical. Same pattern
# as Opportunity Discovery above: creates a task_type='research_roblox_
# trend' task, the executor thread picks it up and runs the real LLM
# assessment, saving a row to `roblox_trends`. This endpoint does no
# LLM work itself and returns immediately.
# ---------------------------------------------------------------------

@app.post("/businesses/{business_id}/roblox-trends/research")
def request_roblox_trend_research(business_id: str, req: ResearchRobloxTrendRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    if len(req.reference_urls) > 3:
        raise HTTPException(status_code=400, detail="reference_urls is capped at 3")
    task_id = state["orchestrator"].create_task(
        business_id, f"Research Roblox trend: {req.concept}", department=req.department,
        priority=req.priority, budget_arc=req.budget_arc,
        permission_level_required=req.permission_level_required,
        task_type="research_roblox_trend",
        task_input={"concept": req.concept, "reference_urls": req.reference_urls},
    )
    return {"task_id": task_id}


@app.get("/businesses/{business_id}/roblox-trends")
def list_roblox_trends(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    return [row_to_dict(r) for r in
            state["db"].query("SELECT * FROM roblox_trends WHERE business_id=? "
                               "ORDER BY created_at DESC", (business_id,))]


# ---------------------------------------------------------------------
# Approvals — the human approval queue. Nothing in this API executes a
# real-world-money action; approve/reject only record the owner's
# decision, per the project's hard real-money-safety requirement.
# ---------------------------------------------------------------------

@app.get("/approvals/pending")
def pending_approvals():
    return [row_to_dict(r) for r in state["approvals"].pending()]


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, req: DecisionRequest):
    if state["approvals"].status(approval_id) is None:
        raise HTTPException(status_code=404, detail="approval not found")
    state["approvals"].approve(approval_id, notes=req.notes)
    state["orchestrator"].promote_approved_task(approval_id)
    return {"status": "approved"}


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, req: DecisionRequest):
    if state["approvals"].status(approval_id) is None:
        raise HTTPException(status_code=404, detail="approval not found")
    state["approvals"].reject(approval_id, notes=req.notes)
    state["orchestrator"].cancel_rejected_task(approval_id)
    return {"status": "rejected"}
