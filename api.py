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

from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from permission_levels import LEVEL_NAMES, MIN_LEVEL, MAX_LEVEL

from db import get_database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker, InsufficientArcError
from approval import ApprovalQueue
from orchestrator import Orchestrator
from scheduler import JobRegistry, run_forever as scheduler_run_forever
import executor as executor_module
import fulfillment as fulfillment_module
import stripe_client
import dashboard_auth
from db import new_id

STATIC_DIR = Path(__file__).parent / "static"
SCHEDULER_POLL_INTERVAL_SECONDS = float(os.environ.get("SCHEDULER_POLL_INTERVAL_SECONDS", "15"))
EXECUTOR_POLL_INTERVAL_SECONDS = float(os.environ.get("EXECUTOR_POLL_INTERVAL_SECONDS", "10"))
FULFILLMENT_POLL_INTERVAL_SECONDS = float(os.environ.get("FULFILLMENT_POLL_INTERVAL_SECONDS", "10"))

# ---------------------------------------------------------------------
# The storefront — the first path this system has to real-world USD.
# STORE_BUSINESS_ID names which existing business (create one in the
# dashboard first) fulfills paid orders; the store refuses to accept
# any payment until it's configured, rather than accepting money with
# nowhere real to route the resulting work. Prices are configurable via
# env vars so changing them never requires a code change/redeploy of
# logic, only a variable.
# ---------------------------------------------------------------------
STORE_BUSINESS_ID = os.environ.get("STORE_BUSINESS_ID")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

PRODUCT_CATALOG = {
    "research_opportunity": {
        "name": "Business Opportunity Research Report",
        "description": (
            "A structured, evidence-based assessment of a business idea or niche — "
            "market size, competition, startup cost, revenue potential, and more, "
            "each framed as an estimate with an explicit confidence level. Delivered "
            "by email, usually within a minute of payment."
        ),
        "price_usd_cents": int(os.environ.get("STORE_PRICE_OPPORTUNITY_CENTS", "1900")),
    },
    "research_roblox_trend": {
        "name": "Roblox Concept Trend Research Report",
        "description": (
            "A structured assessment of a Roblox game genre/mechanic/concept — "
            "player demand signals, competition, build complexity, monetization fit, "
            "and more, each framed as an estimate with an explicit confidence level. "
            "Delivered by email, usually within a minute of payment."
        ),
        "price_usd_cents": int(os.environ.get("STORE_PRICE_ROBLOX_CENTS", "1900")),
    },
}

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

    fulfillment_stop_event = threading.Event()
    fulfillment_thread = threading.Thread(
        target=fulfillment_module.run_forever,
        args=(db, FULFILLMENT_POLL_INTERVAL_SECONDS, fulfillment_stop_event),
        daemon=True,
        name="fulfillment-thread",
    )
    fulfillment_thread.start()
    state["fulfillment_thread"] = fulfillment_thread

    yield

    stop_event.set()
    executor_stop_event.set()
    fulfillment_stop_event.set()
    scheduler_thread.join(timeout=5)
    executor_thread.join(timeout=5)
    fulfillment_thread.join(timeout=5)
    db.close()


app = FastAPI(title="AI Holding Company OS — Core API", version="0.1.0", lifespan=lifespan)

# Serves dashboard.css/dashboard.js/dashboard-render.js at /static/... ,
# same-origin as the JSON API below, so the dashboard's fetch() calls
# need no CORS configuration.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def require_dashboard_auth(request: Request, call_next):
    """Protects every internal route (dashboard UI + JSON API) with HTTP
    Basic Auth. /health and the public storefront (/store/*, plus its
    specific static assets) are left open on purpose — see
    dashboard_auth.py's module docstring for why.

    Fails CLOSED: if DASHBOARD_USERNAME/DASHBOARD_PASSWORD aren't set in
    the environment, every protected route returns 503 rather than
    silently staying open. This matches how the rest of this codebase
    treats a missing credential (ANTHROPIC_API_KEY, STRIPE_SECRET_KEY,
    etc.) — refuse loudly, never degrade silently."""
    path = request.url.path

    if dashboard_auth.is_public_path(path):
        return await call_next(request)

    if not dashboard_auth.credentials_configured():
        return JSONResponse(
            status_code=503,
            content={"detail": "Dashboard auth is not configured. Set DASHBOARD_USERNAME "
                                "and DASHBOARD_PASSWORD to enable access."},
        )

    auth_header = request.headers.get("authorization")
    if not dashboard_auth.check_credentials(auth_header):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required."},
            headers={"WWW-Authenticate": 'Basic realm="AI Holding Company OS"'},
        )

    return await call_next(request)


@app.get("/dashboard")
def dashboard_ui():
    """The owner-facing single-page dashboard. Plain HTML/CSS/JS, no
    build step, no Node/npm required — see static/dashboard.html."""
    return FileResponse(str(STATIC_DIR / "dashboard.html"))


@app.get("/store")
def store_ui():
    """The public, customer-facing storefront — deliberately a
    separate page/look from the owner's internal /dashboard, since
    real paying strangers land here, not just the owner."""
    return FileResponse(str(STATIC_DIR / "store.html"))


@app.get("/store/terms")
def store_terms():
    """Public terms of service / refund policy / privacy notice.
    Required before real payments should be taken — a checkout page
    with zero disclosure of what's being sold, how refunds work, or
    what happens to a customer's data is a real legal-exposure gap,
    not cosmetic. Linked from store.html's and store-success.html's
    footers."""
    return FileResponse(str(STATIC_DIR / "store-legal.html"))


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
    permission_level: int = Field(1, ge=MIN_LEVEL, le=MAX_LEVEL)
    # Since executor.py now charges agents real (estimated) ARC cost for
    # LLM usage instead of always 0.0, a brand-new agent needs some
    # starting balance or its very first task would fail with
    # InsufficientArcError before doing anything wrong. 100 ARC is an
    # arbitrary, easily-overridden starting runway, not a meaningful
    # number on its own — set to 0 to opt an agent out of auto-funding.
    starting_budget_arc: float = 100.0


class CreateTaskRequest(BaseModel):
    objective: str
    department: Optional[str] = None
    priority: int = 3
    budget_arc: float = 0.0
    permission_level_required: int = Field(1, ge=MIN_LEVEL, le=MAX_LEVEL)


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
    permission_level_required: int = Field(1, ge=MIN_LEVEL, le=MAX_LEVEL)
    budget_arc: float = 0.0
    enabled: bool = True


class SetJobEnabledRequest(BaseModel):
    enabled: bool


class ResearchOpportunityRequest(BaseModel):
    topic: str
    reference_urls: List[str] = []
    department: Optional[str] = None
    permission_level_required: int = Field(2, ge=MIN_LEVEL, le=MAX_LEVEL)
    priority: int = 3
    budget_arc: float = 0.0


class ResearchRobloxTrendRequest(BaseModel):
    concept: str
    reference_urls: List[str] = []
    department: Optional[str] = None
    permission_level_required: int = Field(2, ge=MIN_LEVEL, le=MAX_LEVEL)
    priority: int = 3
    budget_arc: float = 0.0


class CheckoutRequest(BaseModel):
    product_type: str
    topic: str
    customer_email: str


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

    fulfill = state.get("fulfillment_thread")
    checks["fulfillment_thread"] = "ok" if (fulfill and fulfill.is_alive()) else "not running"

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@app.get("/permission-levels")
def list_permission_levels():
    """The full 0-7 permission-level ladder (permission_levels.py), so
    the dashboard can show names instead of bare integers when setting
    an agent's permission_level or a task's permission_level_required."""
    return [{"level": lvl, "name": name} for lvl, name in sorted(LEVEL_NAMES.items())]


@app.get("/audit")
def list_audit_log(target_type: Optional[str] = None, target_id: Optional[str] = None,
                    action: Optional[str] = None, limit: int = 50):
    """Read-only view of audit_log — protected by the same dashboard
    auth as everything else outside /store and /health (this route
    isn't under either prefix, so require_dashboard_auth covers it).
    Added specifically so a stuck order/task can be diagnosed from the
    browser (e.g. GET /audit?target_type=order&target_id=ord_xxx) without
    needing direct database access. limit is capped at 200 to keep this
    a quick diagnostic view, not a full log export."""
    limit = max(1, min(limit, 200))
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    if target_type:
        query += " AND target_type=?"
        params.append(target_type)
    if target_id:
        query += " AND target_id=?"
        params.append(target_id)
    if action:
        query += " AND action=?"
        params.append(action)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = state["db"].query(query, tuple(params))
    return [row_to_dict(r) for r in rows]


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
    if req.starting_budget_arc > 0:
        state["banker"].allocate(business_id, agent_id, req.starting_budget_arc,
                                  reason="initial agent budget")
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
# Storefront — the first path this system has to real-world USD. This
# is the ONLY part of the API that touches real money, and it follows
# one hard rule throughout: an order is never marked 'paid', and a
# real_transactions row is never written, except in direct response to
# a Stripe webhook that stripe_client.verify_webhook_signature() has
# cryptographically verified. Nothing here ever just trusts a client
# request that a payment happened.
# ---------------------------------------------------------------------

@app.get("/store/products")
def list_store_products():
    """Public catalog — powers static/store.html. Returns 200 even if
    the store isn't configured yet (STORE_BUSINESS_ID unset); checkout
    is what actually enforces that, so browsing/pricing still works
    while you're setting things up."""
    return {
        "products": [
            {"product_type": key, **value} for key, value in PRODUCT_CATALOG.items()
        ],
        "store_configured": bool(STORE_BUSINESS_ID),
    }


@app.post("/store/checkout")
def create_checkout(req: CheckoutRequest):
    if not STORE_BUSINESS_ID:
        raise HTTPException(
            status_code=503,
            detail="Store is not configured yet — set STORE_BUSINESS_ID to an existing "
                   "business id before accepting payments.",
        )
    if not state["businesses"].get(STORE_BUSINESS_ID):
        raise HTTPException(
            status_code=503,
            detail=f"STORE_BUSINESS_ID={STORE_BUSINESS_ID!r} does not match any existing business.",
        )
    if req.product_type not in PRODUCT_CATALOG:
        raise HTTPException(status_code=400, detail=f"Unknown product_type: {req.product_type!r}")
    if not req.topic.strip():
        raise HTTPException(status_code=400, detail="topic is required")
    if "@" not in req.customer_email:
        raise HTTPException(status_code=400, detail="customer_email looks invalid")
    if not PUBLIC_BASE_URL:
        raise HTTPException(
            status_code=503,
            detail="Store is not configured yet — set PUBLIC_BASE_URL "
                   "(e.g. https://your-app.up.railway.app) so Stripe knows where to "
                   "send the customer back after payment.",
        )

    product = PRODUCT_CATALOG[req.product_type]
    order_id = new_id("ord")
    db = state["db"]
    db.execute(
        "INSERT INTO orders (id, product_type, topic, customer_email, price_usd_cents, "
        "currency, business_id, status) VALUES (?, ?, ?, ?, ?, 'usd', ?, 'pending_payment')",
        (order_id, req.product_type, req.topic.strip(), req.customer_email,
         product["price_usd_cents"], STORE_BUSINESS_ID),
    )

    try:
        session = stripe_client.create_checkout_session(
            product_name=product["name"],
            unit_amount_cents=product["price_usd_cents"],
            currency="usd",
            customer_email=req.customer_email,
            success_url=f"{PUBLIC_BASE_URL}/static/store-success.html?order_id={order_id}",
            cancel_url=f"{PUBLIC_BASE_URL}/static/store.html",
            metadata={"order_id": order_id},
        )
    except stripe_client.StripeError as e:
        db.execute("UPDATE orders SET status='failed' WHERE id=?", (order_id,))
        raise HTTPException(status_code=502, detail=f"Could not start checkout: {e}")

    db.execute("UPDATE orders SET stripe_session_id=? WHERE id=?", (session["id"], order_id))
    return {"order_id": order_id, "checkout_url": session["url"]}


@app.get("/store/orders/{order_id}")
def get_order_status(order_id: str):
    """Public, minimal status check for store-success.html to poll —
    deliberately returns only what a customer should see, not internal
    fields like business_id or the linked task's full detail."""
    order = state["db"].query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    return {
        "id": order["id"],
        "product_type": order["product_type"],
        "topic": order["topic"],
        "status": order["status"],
    }


@app.post("/store/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe_client.verify_webhook_signature(payload, sig_header)
    except stripe_client.WebhookVerificationError as e:
        # 400, not 500: this could be an attacker, a misconfigured
        # secret, or a stale/replayed request — never treat it as our
        # bug, and never act on the payload.
        raise HTTPException(status_code=400, detail=f"webhook verification failed: {e}")

    if event.get("type") != "checkout.session.completed":
        return {"status": "ignored", "type": event.get("type")}

    session_obj = event.get("data", {}).get("object", {})
    order_id = (session_obj.get("metadata") or {}).get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="webhook missing metadata.order_id")

    db = state["db"]
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not order:
        raise HTTPException(status_code=404, detail=f"webhook references unknown order {order_id!r}")

    stripe_event_id = event.get("id")
    amount_total = session_obj.get("amount_total", order["price_usd_cents"])
    currency = session_obj.get("currency", order["currency"])
    payment_intent_id = session_obj.get("payment_intent")

    # Idempotency: real_transactions.stripe_event_id is UNIQUE, so a
    # duplicate webhook delivery (Stripe retries on anything but a 2xx)
    # hitting this INSERT twice raises here — caught and treated as
    # "already processed", never as a second real payment.
    try:
        db.execute(
            "INSERT INTO real_transactions (id, order_id, direction, source, destination, "
            "amount_usd_cents, currency, business_id, purpose, stripe_event_id) "
            "VALUES (?, ?, 'in', ?, 'owner_stripe_account', ?, ?, ?, ?, ?)",
            (new_id("txn"), order_id, f"stripe_customer:{order['customer_email']}",
             amount_total, currency, order["business_id"],
             f"store order: {order['product_type']} — {order['topic']}", stripe_event_id),
        )
    except Exception:
        return {"status": "already_processed", "order_id": order_id}

    if order["status"] == "pending_payment":
        db.execute(
            "UPDATE orders SET status='paid', paid_at=datetime('now'), "
            "stripe_payment_intent_id=? WHERE id=?",
            (payment_intent_id, order_id),
        )
        task_type = order["product_type"]
        task_input = ({"topic": order["topic"], "reference_urls": []}
                      if task_type == "research_opportunity"
                      else {"concept": order["topic"], "reference_urls": []})
        task_id = state["orchestrator"].create_task(
            order["business_id"], f"[Paid order {order_id}] {order['topic']}",
            department="research", priority=2, permission_level_required=2,
            task_type=task_type, task_input=task_input,
        )
        db.execute("UPDATE orders SET task_id=? WHERE id=?", (task_id, order_id))
        db.audit("stripe_webhook", "order_paid", "order", order_id,
                  {"amount_usd_cents": amount_total, "task_id": task_id})

    return {"status": "processed", "order_id": order_id}


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
