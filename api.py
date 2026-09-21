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
from datetime import date
from pathlib import Path
from typing import Optional, List
import json
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
from rate_limiter import RateLimiter
from tasks.trading_common import DEFAULT_STRATEGY_PARAMS, validate_parameters, StrategyParameterError
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

# Ordering here is the ordering the storefront displays products in
# (see /store/products and store.js) — leads with the lowest price so a
# cold, skeptical visitor has an easy, low-risk first "yes" before
# seeing the pricier reports.
PRODUCT_CATALOG = {
    "research_real_estate": {
        "name": "Real Estate Investment Research Report",
        "description": (
            "A structured investment research assessment for a property or market — "
            "market trend, comparable properties, estimated rental yield, price trend "
            "assessment, and risk factors, each framed as an estimate with an explicit "
            "confidence level. This is research only, never a licensed appraisal or a "
            "brokered transaction; any jurisdiction-specific issue (zoning, disclosure "
            "law, rent control, licensing) is flagged for a licensed real estate agent, "
            "appraiser, or attorney, not resolved here. Delivered by email, usually "
            "within a minute of payment."
        ),
        "price_usd_cents": int(os.environ.get("STORE_PRICE_REAL_ESTATE_CENTS", "500")),
    },
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
    "research_app_feasibility": {
        "name": "App Feasibility & Planning Report",
        "description": (
            "A structured feasibility assessment for an app idea — platform "
            "recommendation, suggested tech stack, complexity tier, rough timeline "
            "and cost range, MVP feature scope, and key technical risks, each framed "
            "as an estimate with an explicit confidence level. Any concept touching a "
            "regulated domain (payments, health data, minors, etc.) is flagged for "
            "dedicated legal/compliance review, not resolved here. Delivered by "
            "email, usually within a minute of payment."
        ),
        "price_usd_cents": int(os.environ.get("STORE_PRICE_APP_FEASIBILITY_CENTS", "1900")),
    },
}

# /store/checkout is the only public, unauthenticated endpoint that also
# does real work on every call (creates a real Stripe Checkout Session,
# writes a pending_payment order row) -- rate-limited per client IP so
# spamming it can't flood the orders table or burn through Stripe API
# calls. Generous enough that a real customer retrying a declined card
# is never the one who gets blocked. See rate_limiter.py.
CHECKOUT_RATE_LIMIT_MAX = int(os.environ.get("CHECKOUT_RATE_LIMIT_MAX", "10"))
CHECKOUT_RATE_LIMIT_WINDOW_SECONDS = float(
    os.environ.get("CHECKOUT_RATE_LIMIT_WINDOW_SECONDS", "60"))
_checkout_rate_limiter = RateLimiter(CHECKOUT_RATE_LIMIT_MAX, CHECKOUT_RATE_LIMIT_WINDOW_SECONDS)


def _client_ip(request: Request) -> str:
    """Railway (and any platform fronting this with a reverse proxy)
    means request.client.host is the proxy's own address, not the real
    caller's -- X-Forwarded-For (set by the proxy, not the client) is
    what actually identifies the caller. Falls back to request.client.host
    for local/direct runs where no proxy sets that header."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# The entire internal dashboard/API sits behind one static HTTP Basic
# Auth password (dashboard_auth.py) with no other protection -- without
# this, it could be brute-forced indefinitely. Only FAILED attempts
# count toward the limit (via blocked()/allow(), not the simpler
# allow()-gates-everything pattern used for checkout above): the
# dashboard's own browser session re-sends valid credentials on every
# request while polling every few seconds, and that legitimate traffic
# must never itself trip the limiter.
DASHBOARD_LOGIN_RATE_LIMIT_MAX = int(os.environ.get("DASHBOARD_LOGIN_RATE_LIMIT_MAX", "10"))
DASHBOARD_LOGIN_RATE_LIMIT_WINDOW_SECONDS = float(
    os.environ.get("DASHBOARD_LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300"))
_dashboard_login_rate_limiter = RateLimiter(
    DASHBOARD_LOGIN_RATE_LIMIT_MAX, DASHBOARD_LOGIN_RATE_LIMIT_WINDOW_SECONDS)

# ---------------------------------------------------------------------
# Ops/Maintenance — the sixth business vertical, and the only one with
# no owner setup step: unlike the storefront (needs Stripe/Resend keys)
# or Automated Stock Trading (needs an Alpha Vantage key + clicking
# "Enable Auto-Trading"), watching this system's own health needs
# nothing beyond the ANTHROPIC_API_KEY every other vertical already
# requires -- so it's provisioned automatically at startup, not behind
# a button. See tasks/ops_maintenance_review.py.
# ---------------------------------------------------------------------
OPS_BUSINESS_NAME = "System Operations"
OPS_BUSINESS_TYPE = "ops_maintenance"
OPS_REVIEW_INTERVAL_SECONDS = int(os.environ.get("OPS_REVIEW_INTERVAL_SECONDS", "86400"))  # 24h


def ensure_ops_business_provisioned(db, businesses, agents, banker, jobs):
    """Idempotent: creates the internal 'System Operations' business, an
    Ops Monitor agent, and a recurring ops_maintenance_review scheduled
    job the first time this runs; a no-op on every later call (every
    startup after the first, and /ops/review's own defensive re-check).
    Returns the business_id either way, so callers never need a second
    lookup."""
    existing = db.query_one("SELECT id FROM businesses WHERE type=?", (OPS_BUSINESS_TYPE,))
    if existing:
        return existing["id"]

    business_id = businesses.create(
        OPS_BUSINESS_NAME, OPS_BUSINESS_TYPE,
        "Monitor this system's own infrastructure and produce maintenance "
        "recommendations for the owner -- never acts on its own findings.",
    )
    agent_id = agents.create(
        business_id, "Ops Monitor", role="Systems Maintenance Analyst",
        department="ops", model="unassigned", permission_level=2,
    )
    agents.set_status(agent_id, "idle")
    banker.allocate(business_id, agent_id, 100.0,
                     reason="starting ARC runway for automated ops/maintenance reviews")
    jobs.create(
        business_id, "System health review", "Review this system's own infrastructure health",
        interval_seconds=OPS_REVIEW_INTERVAL_SECONDS, permission_level_required=2,
        task_type="ops_maintenance_review",
    )
    db.audit("system", "ops_business_provisioned", "business", business_id, {})
    return business_id


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

    ensure_ops_business_provisioned(db, state["businesses"], state["agents"],
                                     state["banker"], state["jobs"])

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
    etc.) — refuse loudly, never degrade silently.

    Also rate-limits failed login attempts per client IP (see
    DASHBOARD_LOGIN_RATE_LIMIT_MAX above) — this one static password is
    otherwise the only thing standing between anyone who finds the URL
    and every business's data, so it must not be brute-forceable."""
    path = request.url.path

    if dashboard_auth.is_public_path(path):
        return await call_next(request)

    if not dashboard_auth.credentials_configured():
        return JSONResponse(
            status_code=503,
            content={"detail": "Dashboard auth is not configured. Set DASHBOARD_USERNAME "
                                "and DASHBOARD_PASSWORD to enable access."},
        )

    client_ip = _client_ip(request)
    if _dashboard_login_rate_limiter.blocked(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many failed login attempts — please wait and try again."},
        )

    auth_header = request.headers.get("authorization")
    if not dashboard_auth.check_credentials(auth_header):
        _dashboard_login_rate_limiter.allow(client_ip)
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required."},
            headers={"WWW-Authenticate": 'Basic realm="AI Holding Company OS"'},
        )

    return await call_next(request)


@app.get("/")
def storefront_landing():
    """The public storefront's real entry point. This is the only
    customer-facing surface in the whole system, so it gets the clean
    root URL -- "https://your-app.up.railway.app/" -- rather than
    making anyone sharing/visiting the link append /static/store.html."""
    return FileResponse(str(STATIC_DIR / "store.html"))


@app.get("/robots.txt")
def robots_txt():
    """Points crawlers at the one page actually worth indexing and away
    from the internal dashboard/API (which 401s for an anonymous crawler
    anyway -- this is a courtesy/hint, not the security boundary)."""
    return Response(
        content=(
            "User-agent: *\n"
            "Allow: /\n"
            "Allow: /static/store.html\n"
            "Allow: /static/store-legal.html\n"
            "Disallow: /dashboard\n"
            "Disallow: /businesses\n"
            "Disallow: /agents\n"
            "Disallow: /tasks\n"
            "Disallow: /approvals\n"
            "Disallow: /banker\n"
            "Disallow: /scheduled-jobs\n"
            "Disallow: /static/store-success.html\n"
        ),
        media_type="text/plain",
    )


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


class ResearchAppFeasibilityRequest(BaseModel):
    concept: str
    reference_urls: List[str] = []
    department: Optional[str] = None
    permission_level_required: int = Field(2, ge=MIN_LEVEL, le=MAX_LEVEL)
    priority: int = 3
    budget_arc: float = 0.0


class ResearchRealEstateRequest(BaseModel):
    property_or_market: str
    reference_urls: List[str] = []
    department: Optional[str] = None
    permission_level_required: int = Field(2, ge=MIN_LEVEL, le=MAX_LEVEL)
    priority: int = 3
    budget_arc: float = 0.0


class CheckoutRequest(BaseModel):
    product_type: str
    topic: str
    customer_email: str


class CreateTradingPortfolioRequest(BaseModel):
    starting_cash_usd: float = 10000.0
    watchlist: List[str] = []  # empty -> tasks.trading_common.DEFAULT_STRATEGY_PARAMS watchlist


class TradingStrategyOverrideRequest(BaseModel):
    parameters: dict
    rationale: str = "Owner manual override"


class EnableAutoTradingRequest(BaseModel):
    starting_cash_usd: float = 10000.0
    watchlist: List[str] = []
    cycle_interval_seconds: int = 14400    # 4 hours
    review_interval_seconds: int = 86400   # 24 hours


class EnableLiveTradingRequest(BaseModel):
    # Deliberately no default of True -- the owner must actively set
    # this exact field on every call to turn on real-money trading.
    confirm_real_money: bool = False
    cycle_interval_seconds: int = 14400    # 4 hours, same default as paper


class TriggerBacktestSearchRequest(BaseModel):
    train_start_date: str          # "YYYY-MM-DD" -- start of the TRAIN window
    validation_split_date: str     # bars before this are TRAIN, on/after are VALIDATION
    validation_end_date: str       # "YYYY-MM-DD" -- end of the VALIDATION window
    starting_cash_usd: float = 10000.0
    max_candidates: int = 5


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
        # /health is public/unauthenticated -- the raw exception text can
        # include internal connection details (host, DSN fragments), so
        # log it server-side and keep the public response generic.
        print(f"[health] database check failed: {e}", flush=True)
        checks["database"] = "error"

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


@app.get("/overview")
def overview():
    """Cross-business rollup -- the actual 'Command Center' view: every
    business at a glance, without needing to select one from the
    dashboard's dropdown first. Read-only aggregation over existing
    tables/modules; adds no new state.

    Closes a real gap: the dashboard previously only ever showed pending
    approvals scoped to whichever business happened to be selected
    (business_dashboard()'s pending_approvals field) -- an approval on a
    DIFFERENT business could sit unnoticed indefinitely if the owner
    wasn't currently looking at that one, in the single most safety-
    critical part of this whole system. /approvals/pending already
    existed as a global, unscoped endpoint; this wires that -- and the
    equivalent global rollups for businesses/agents/tasks/ARC/revenue --
    into the dashboard as an always-visible top section, independent of
    which business (if any) is selected."""
    db = state["db"]

    businesses_overview = []
    for b in state["businesses"].list():
        agent_count = db.query_one(
            "SELECT COUNT(*) as c FROM agents WHERE business_id=?", (b["id"],)
        )["c"]
        open_task_count = db.query_one(
            "SELECT COUNT(*) as c FROM tasks WHERE business_id=? AND status NOT IN "
            "('completed','failed','cancelled')", (b["id"],)
        )["c"]
        pending_approval_count = db.query_one(
            "SELECT COUNT(*) as c FROM approvals WHERE business_id=? AND status='pending'",
            (b["id"],),
        )["c"]
        businesses_overview.append({
            **row_to_dict(b),
            "agent_count": agent_count,
            "open_task_count": open_task_count,
            "pending_approval_count": pending_approval_count,
            "arc_summary": state["banker"].business_summary(b["id"]),
        })

    agents_by_status = {
        r["status"]: r["c"]
        for r in db.query("SELECT status, COUNT(*) as c FROM agents GROUP BY status")
    }
    tasks_by_status = {
        r["status"]: r["c"]
        for r in db.query("SELECT status, COUNT(*) as c FROM tasks GROUP BY status")
    }
    real_revenue_usd_cents = db.query_one(
        "SELECT COALESCE(SUM(amount_usd_cents),0) as total FROM real_transactions "
        "WHERE direction='in'"
    )["total"]
    global_arc = {
        r["entry_type"]: (r["total"] or 0)
        for r in db.query("SELECT entry_type, SUM(amount) as total FROM arc_ledger GROUP BY entry_type")
    }

    latest_ops_report = row_to_dict(db.query_one(
        "SELECT * FROM ops_maintenance_reports ORDER BY created_at DESC LIMIT 1"
    ))

    return {
        "businesses": businesses_overview,
        "pending_approvals": [row_to_dict(r) for r in state["approvals"].pending()],
        "agents_by_status": agents_by_status,
        "tasks_by_status": tasks_by_status,
        "real_revenue_usd_cents": real_revenue_usd_cents,
        "global_arc": global_arc,
        "latest_ops_report": latest_ops_report,
    }


# ---------------------------------------------------------------------
# Ops/Maintenance — the sixth business vertical, and the only one with
# no customer/storefront product (see tasks/ops_maintenance_review.py
# and executor.py's _handle_ops_maintenance_review). System-wide, not
# scoped to whichever business the owner happens to have selected —
# same reasoning as /overview's global rollups above.
# ---------------------------------------------------------------------

@app.get("/ops/reports")
def list_ops_reports():
    return [row_to_dict(r) for r in state["db"].query(
        "SELECT * FROM ops_maintenance_reports ORDER BY created_at DESC LIMIT 20"
    )]


@app.post("/ops/review")
def trigger_ops_review():
    """Manually trigger one ops_maintenance_review task right now,
    outside its scheduled job -- for testing or an on-demand check.
    Re-provisions the ops business/agent/job if somehow missing (a
    no-op the vast majority of the time — see ensure_ops_business_provisioned,
    already called once at startup)."""
    business_id = ensure_ops_business_provisioned(
        state["db"], state["businesses"], state["agents"], state["banker"], state["jobs"],
    )
    task_id = state["orchestrator"].create_task(
        business_id, "Review this system's own infrastructure health",
        permission_level_required=2, task_type="ops_maintenance_review",
    )
    return {"task_id": task_id}


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
    app_feasibility_assessments = [row_to_dict(r) for r in
                                    db.query("SELECT * FROM app_feasibility_assessments WHERE "
                                             "business_id=? ORDER BY created_at DESC", (business_id,))]
    real_estate_assessments = [row_to_dict(r) for r in
                                db.query("SELECT * FROM real_estate_assessments WHERE "
                                         "business_id=? ORDER BY created_at DESC", (business_id,))]
    # Storefront orders -- the only place an owner can see real-money
    # order activity without going into Stripe or the database
    # directly. Most businesses will never have any (STORE_BUSINESS_ID
    # points at exactly one), so this is cheap for everyone else.
    orders = [row_to_dict(r) for r in
              db.query("SELECT * FROM orders WHERE business_id=? "
                       "ORDER BY created_at DESC LIMIT 50", (business_id,))]
    trading_portfolio = _trading_portfolio_view(business_id)
    trading_trades = []
    if trading_portfolio:
        trading_trades = [row_to_dict(r) for r in db.query(
            "SELECT * FROM paper_trades WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 20",
            (trading_portfolio["portfolio"]["id"],))]
    trading_strategy_versions = [row_to_dict(r) for r in db.query(
        "SELECT * FROM trading_strategy_versions WHERE business_id=? ORDER BY version DESC",
        (business_id,))]
    live_trading = _live_trading_view(business_id)
    backtest_runs_rows = db.query(
        "SELECT * FROM backtest_runs WHERE business_id=? ORDER BY created_at DESC LIMIT 5",
        (business_id,),
    )
    backtest_runs = []
    for r in backtest_runs_rows:
        d = row_to_dict(r)
        d["candidates"] = json.loads(d.pop("candidates_json"))
        backtest_runs.append(d)
    return {
        "business": row_to_dict(biz),
        "agents": agents,
        "tasks": tasks,
        "pending_approvals": pending_approvals,
        "arc_summary": arc_summary,
        "scheduled_jobs": scheduled_jobs,
        "opportunities": opportunities,
        "roblox_trends": roblox_trends,
        "app_feasibility_assessments": app_feasibility_assessments,
        "real_estate_assessments": real_estate_assessments,
        "orders": orders,
        "trading_portfolio": trading_portfolio,
        "trading_trades": trading_trades,
        "trading_strategy_versions": trading_strategy_versions,
        "live_trading": live_trading,
        "backtest_runs": backtest_runs,
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


@app.delete("/businesses/{business_id}/opportunities/{opportunity_id}")
def delete_opportunity(business_id: str, opportunity_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    if not db.query_one("SELECT id FROM opportunities WHERE id=? AND business_id=?",
                         (opportunity_id, business_id)):
        raise HTTPException(status_code=404, detail="opportunity not found")
    db.execute("DELETE FROM opportunities WHERE id=?", (opportunity_id,))
    db.audit("owner", "delete_opportunity", "opportunity", opportunity_id, {"business_id": business_id})
    return {"status": "deleted"}


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


@app.delete("/businesses/{business_id}/roblox-trends/{trend_id}")
def delete_roblox_trend(business_id: str, trend_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    if not db.query_one("SELECT id FROM roblox_trends WHERE id=? AND business_id=?",
                         (trend_id, business_id)):
        raise HTTPException(status_code=404, detail="roblox trend not found")
    db.execute("DELETE FROM roblox_trends WHERE id=?", (trend_id,))
    db.audit("owner", "delete_roblox_trend", "roblox_trend", trend_id, {"business_id": business_id})
    return {"status": "deleted"}


# ---------------------------------------------------------------------
# App Development — the third business vertical. Same pattern as
# Opportunity Discovery/Roblox Game Development above: creates a
# task_type='research_app_feasibility' task, the executor thread picks
# it up and runs the real LLM assessment, saving a row to
# `app_feasibility_assessments`. This endpoint does no LLM work itself
# and returns immediately. Research/planning only -- see
# tasks/research_app_feasibility.py: it never promises a delivery date
# or dollar figure as fact, and explicitly flags (never resolves) any
# regulated-domain risk (payments, health data, etc.) for dedicated
# legal/compliance review.
# ---------------------------------------------------------------------

@app.post("/businesses/{business_id}/app-feasibility/research")
def request_app_feasibility_research(business_id: str, req: ResearchAppFeasibilityRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    if len(req.reference_urls) > 3:
        raise HTTPException(status_code=400, detail="reference_urls is capped at 3")
    task_id = state["orchestrator"].create_task(
        business_id, f"Assess app feasibility: {req.concept}", department=req.department,
        priority=req.priority, budget_arc=req.budget_arc,
        permission_level_required=req.permission_level_required,
        task_type="research_app_feasibility",
        task_input={"concept": req.concept, "reference_urls": req.reference_urls},
    )
    return {"task_id": task_id}


@app.get("/businesses/{business_id}/app-feasibility")
def list_app_feasibility_assessments(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    return [row_to_dict(r) for r in
            state["db"].query("SELECT * FROM app_feasibility_assessments WHERE business_id=? "
                               "ORDER BY created_at DESC", (business_id,))]


@app.delete("/businesses/{business_id}/app-feasibility/{assessment_id}")
def delete_app_feasibility_assessment(business_id: str, assessment_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    if not db.query_one("SELECT id FROM app_feasibility_assessments WHERE id=? AND business_id=?",
                         (assessment_id, business_id)):
        raise HTTPException(status_code=404, detail="app feasibility assessment not found")
    db.execute("DELETE FROM app_feasibility_assessments WHERE id=?", (assessment_id,))
    db.audit("owner", "delete_app_feasibility_assessment", "app_feasibility_assessment", assessment_id,
              {"business_id": business_id})
    return {"status": "deleted"}


# ---------------------------------------------------------------------
# Real Estate — the fifth business vertical. Same pattern as the other
# storefront-monetized verticals above: creates a
# task_type='research_real_estate' task, the executor thread picks it
# up and runs the real LLM assessment, saving a row to
# `real_estate_assessments`. This endpoint does no LLM work itself and
# returns immediately. Investment research only -- see
# tasks/research_real_estate.py: it is never an appraisal, never
# brokers/facilitates an actual transaction, and explicitly flags
# (never resolves) any jurisdiction-specific issue (zoning, disclosure
# law, rent control, licensing) for a licensed real estate agent,
# appraiser, or attorney.
# ---------------------------------------------------------------------

@app.post("/businesses/{business_id}/real-estate/research")
def request_real_estate_research(business_id: str, req: ResearchRealEstateRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    if len(req.reference_urls) > 3:
        raise HTTPException(status_code=400, detail="reference_urls is capped at 3")
    task_id = state["orchestrator"].create_task(
        business_id, f"Research real estate investment: {req.property_or_market}",
        department=req.department, priority=req.priority, budget_arc=req.budget_arc,
        permission_level_required=req.permission_level_required,
        task_type="research_real_estate",
        task_input={"property_or_market": req.property_or_market,
                    "reference_urls": req.reference_urls},
    )
    return {"task_id": task_id}


@app.get("/businesses/{business_id}/real-estate")
def list_real_estate_assessments(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    return [row_to_dict(r) for r in
            state["db"].query("SELECT * FROM real_estate_assessments WHERE business_id=? "
                               "ORDER BY created_at DESC", (business_id,))]


@app.delete("/businesses/{business_id}/real-estate/{assessment_id}")
def delete_real_estate_assessment(business_id: str, assessment_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    if not db.query_one("SELECT id FROM real_estate_assessments WHERE id=? AND business_id=?",
                         (assessment_id, business_id)):
        raise HTTPException(status_code=404, detail="real estate assessment not found")
    db.execute("DELETE FROM real_estate_assessments WHERE id=?", (assessment_id,))
    db.audit("owner", "delete_real_estate_assessment", "real_estate_assessment", assessment_id,
              {"business_id": business_id})
    return {"status": "deleted"}


# ---------------------------------------------------------------------
# Automated Stock Trading — PAPER TRADING ONLY. See tasks/trading_cycle.py
# for the full safety explanation: there is no brokerage integration
# anywhere in this codebase, so nothing reachable through these endpoints
# can ever place a real order, regardless of any setting here. One
# paper_portfolios row per business. task_type='trading_cycle' and
# 'trading_strategy_review' tasks are picked up and run by the same
# executor background thread as every other typed task.
# ---------------------------------------------------------------------

def _trading_portfolio_view(business_id: str):
    db = state["db"]
    portfolio = db.query_one("SELECT * FROM paper_portfolios WHERE business_id=?", (business_id,))
    if not portfolio:
        return None
    positions = [row_to_dict(r) for r in db.query(
        "SELECT * FROM paper_positions WHERE portfolio_id=? AND quantity > 0", (portfolio["id"],)
    )]
    latest_snapshot = db.query_one(
        "SELECT * FROM trading_snapshots WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 1",
        (portfolio["id"],),
    )
    # Oldest-first, capped at 30 points -- exactly what a sparkline needs
    # and no more; this is real recorded equity, never synthesized.
    equity_history = [row_to_dict(r) for r in db.query(
        "SELECT equity_usd, created_at FROM trading_snapshots WHERE portfolio_id=? "
        "ORDER BY created_at DESC LIMIT 30", (portfolio["id"],),
    )][::-1]
    return {
        "portfolio": row_to_dict(portfolio),
        "positions": positions,
        "latest_snapshot": row_to_dict(latest_snapshot),
        "equity_history": equity_history,
    }


@app.post("/businesses/{business_id}/trading/portfolio")
def create_trading_portfolio(business_id: str, req: CreateTradingPortfolioRequest):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    if db.query_one("SELECT id FROM paper_portfolios WHERE business_id=?", (business_id,)):
        raise HTTPException(status_code=409,
                             detail="this business already has a paper trading portfolio")

    params = dict(DEFAULT_STRATEGY_PARAMS)
    if req.watchlist:
        params["watchlist"] = req.watchlist
    try:
        params = validate_parameters(params)
    except StrategyParameterError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.starting_cash_usd <= 0:
        raise HTTPException(status_code=400, detail="starting_cash_usd must be positive")

    portfolio_id = new_id("port")
    db.execute(
        "INSERT INTO paper_portfolios (id, business_id, starting_cash_usd, cash_usd) "
        "VALUES (?, ?, ?, ?)",
        (portfolio_id, business_id, req.starting_cash_usd, req.starting_cash_usd),
    )
    db.execute(
        "INSERT INTO trading_strategy_versions (id, business_id, version, parameters, "
        "rationale, source, active) VALUES (?, ?, 1, ?, ?, 'system', 1)",
        (new_id("strat"), business_id, json.dumps(params), "Initial default strategy parameters."),
    )
    db.audit("owner", "create_trading_portfolio", "paper_portfolio", portfolio_id,
              {"starting_cash_usd": req.starting_cash_usd, "watchlist": params["watchlist"]})
    return _trading_portfolio_view(business_id)


@app.get("/businesses/{business_id}/trading/portfolio")
def get_trading_portfolio(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    view = _trading_portfolio_view(business_id)
    if not view:
        raise HTTPException(status_code=404, detail="no paper trading portfolio for this business yet")
    return view


@app.get("/businesses/{business_id}/trading/trades")
def list_trading_trades(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    portfolio = db.query_one("SELECT id FROM paper_portfolios WHERE business_id=?", (business_id,))
    if not portfolio:
        return []
    return [row_to_dict(r) for r in db.query(
        "SELECT * FROM paper_trades WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 100",
        (portfolio["id"],),
    )]


@app.get("/businesses/{business_id}/trading/strategy-versions")
def list_trading_strategy_versions(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    return [row_to_dict(r) for r in state["db"].query(
        "SELECT * FROM trading_strategy_versions WHERE business_id=? ORDER BY version DESC",
        (business_id,),
    )]


@app.post("/businesses/{business_id}/trading/strategy-override")
def override_trading_strategy(business_id: str, req: TradingStrategyOverrideRequest):
    """Owner-only manual override — the owner can always directly set the
    active strategy (tighten OR loosen, up to the absolute ceilings in
    tasks/trading_common.py), independent of the self-improvement loop.
    Still versioned and still validated by the exact same
    validate_parameters() the automatic review path uses — there is no
    'trusted' path that skips the bounds check, owner included."""
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    current = db.query_one(
        "SELECT * FROM trading_strategy_versions WHERE business_id=? AND active=1 "
        "ORDER BY version DESC LIMIT 1", (business_id,),
    )
    if not current:
        raise HTTPException(status_code=404,
                             detail="no trading portfolio/strategy for this business yet")
    try:
        params = validate_parameters(req.parameters)
    except StrategyParameterError as e:
        raise HTTPException(status_code=400, detail=str(e))

    new_version = current["version"] + 1
    db.execute("UPDATE trading_strategy_versions SET active=0 WHERE business_id=? AND active=1",
               (business_id,))
    db.execute(
        "INSERT INTO trading_strategy_versions (id, business_id, version, parameters, "
        "rationale, source, active) VALUES (?, ?, ?, ?, ?, 'owner_override', 1)",
        (new_id("strat"), business_id, new_version, json.dumps(params), req.rationale),
    )
    db.audit("owner", "trading_strategy_overridden", "trading_strategy_version", None,
              {"business_id": business_id, "new_version": new_version})
    return {"version": new_version, "parameters": params}


@app.post("/businesses/{business_id}/trading/cycle")
def trigger_trading_cycle(business_id: str, department: Optional[str] = None):
    """Manually trigger one trading_cycle task right now, outside any
    scheduled job — for testing or an on-demand check. Creates a real
    task through the same orchestrator path (auto-assignment, permission
    gating) as anything else; returns immediately — the executor thread
    picks it up within EXECUTOR_POLL_INTERVAL_SECONDS."""
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    if not state["db"].query_one("SELECT id FROM paper_portfolios WHERE business_id=?", (business_id,)):
        raise HTTPException(status_code=404, detail="create a paper trading portfolio first")
    task_id = state["orchestrator"].create_task(
        business_id, "Run a paper-trading cycle", department=department,
        permission_level_required=3, task_type="trading_cycle",
    )
    return {"task_id": task_id}


@app.post("/businesses/{business_id}/trading/strategy-review")
def trigger_trading_strategy_review(business_id: str, department: Optional[str] = None):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    if not state["db"].query_one("SELECT id FROM paper_portfolios WHERE business_id=?", (business_id,)):
        raise HTTPException(status_code=404, detail="create a paper trading portfolio first")
    task_id = state["orchestrator"].create_task(
        business_id, "Review paper-trading strategy performance", department=department,
        permission_level_required=3, task_type="trading_strategy_review",
    )
    return {"task_id": task_id}


@app.post("/businesses/{business_id}/trading/enable-auto-trading")
def enable_auto_trading(business_id: str, req: EnableAutoTradingRequest):
    """Convenience endpoint: creates the paper portfolio (if it doesn't
    exist yet), a dedicated Trading Agent (permission_level=3/SIMULATE —
    if the business has no agent cleared for it already), and two
    scheduled jobs — trading_cycle on cycle_interval_seconds,
    trading_strategy_review on review_interval_seconds — in one call.
    This is what makes trading actually 'automatic': without a scheduled
    job, nothing ever runs a cycle on its own. Scheduled jobs are created
    with department=None (not restricted to a specific department) so
    assignment isn't accidentally narrower than the agent this endpoint
    itself creates."""
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]

    if not db.query_one("SELECT id FROM paper_portfolios WHERE business_id=?", (business_id,)):
        create_trading_portfolio(business_id, CreateTradingPortfolioRequest(
            starting_cash_usd=req.starting_cash_usd, watchlist=req.watchlist,
        ))

    trading_agent = db.query_one(
        "SELECT id FROM agents WHERE business_id=? AND permission_level >= 3 "
        "AND status != 'retired' ORDER BY created_at ASC LIMIT 1", (business_id,),
    )
    if not trading_agent:
        agent_id = state["agents"].create(
            business_id, "Trading Agent", role="Paper Trading Analyst",
            department="Trading", model="unassigned", permission_level=3,
        )
        state["agents"].set_status(agent_id, "idle")
        state["banker"].allocate(business_id, agent_id, 500.0,
                                  reason="starting ARC runway for automated trading")

    cycle_job_id = state["jobs"].create(
        business_id, "Paper trading cycle", "Run a paper-trading cycle",
        interval_seconds=req.cycle_interval_seconds, permission_level_required=3,
        task_type="trading_cycle",
    )
    review_job_id = state["jobs"].create(
        business_id, "Paper trading strategy review", "Review paper-trading strategy performance",
        interval_seconds=req.review_interval_seconds, permission_level_required=3,
        task_type="trading_strategy_review",
    )
    db.audit("owner", "enable_auto_trading", "business", business_id,
              {"cycle_job_id": cycle_job_id, "review_job_id": review_job_id})
    return {
        "cycle_job_id": cycle_job_id,
        "review_job_id": review_job_id,
        "portfolio": _trading_portfolio_view(business_id),
    }


# ---------------------------------------------------------------------
# Automated Stock Trading — LIVE (REAL MONEY). Everything below this
# point can place real orders against real cash once
# live_trading_enabled=1 on a business's paper_portfolios row AND
# ALPACA_API_KEY/ALPACA_API_SECRET/ALPACA_BASE_URL are all configured
# (see alpaca_client.py's paper-by-default safety default, and
# executor.py's _handle_live_trading_cycle for the two independent
# safety layers applied to every trade before it reaches the broker).
# live_trading_cycle tasks reuse the SAME active trading_strategy_
# versions row as paper trading -- there is deliberately no separate
# "live strategy"; only execution differs.
# ---------------------------------------------------------------------

def _live_trading_view(business_id: str):
    db = state["db"]
    portfolio = db.query_one("SELECT * FROM paper_portfolios WHERE business_id=?", (business_id,))
    if not portfolio:
        return None
    trades = [row_to_dict(r) for r in db.query(
        "SELECT * FROM live_trades WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 100",
        (portfolio["id"],),
    )]
    latest_snapshot = db.query_one(
        "SELECT * FROM live_snapshots WHERE portfolio_id=? ORDER BY created_at DESC LIMIT 1",
        (portfolio["id"],),
    )
    # Real recorded equity only, oldest-first, same 30-point cap as the
    # paper sparkline -- never synthesized or backfilled.
    equity_history = [row_to_dict(r) for r in db.query(
        "SELECT equity_usd, created_at FROM live_snapshots WHERE portfolio_id=? "
        "ORDER BY created_at DESC LIMIT 30", (portfolio["id"],),
    )][::-1]
    return {
        "live_trading_enabled": bool(portfolio["live_trading_enabled"]),
        "portfolio_id": portfolio["id"],
        "trades": trades,
        "latest_snapshot": row_to_dict(latest_snapshot),
        "equity_history": equity_history,
    }


@app.get("/businesses/{business_id}/trading/live")
def get_live_trading(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    view = _live_trading_view(business_id)
    if not view:
        raise HTTPException(status_code=404, detail="create a paper trading portfolio first")
    return view


@app.post("/businesses/{business_id}/trading/live/enable")
def enable_live_trading(business_id: str, req: EnableLiveTradingRequest):
    """Owner-only switch that turns REAL MONEY trading on for this
    business. Requires a paper trading portfolio to already exist (its
    active trading_strategy_versions row is reused as-is) and an
    explicit confirm_real_money=true on every call -- there is no
    default that enables this by accident. Also fails fast if Alpaca
    credentials aren't configured at all, rather than silently
    scheduling a job that will only ever fail. Creates a dedicated
    live-trading agent (permission_level=4, one tier above paper's 3 —
    see permission_levels.py) if this business doesn't already have
    one, and a recurring live_trading_cycle scheduled job, same shape
    as enable_auto_trading's paper job."""
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    if not req.confirm_real_money:
        raise HTTPException(
            status_code=400,
            detail="confirm_real_money must be set to true -- this switches on real-money "
                   "trading with real cash; it is never enabled by a default value",
        )
    db = state["db"]
    portfolio = db.query_one("SELECT * FROM paper_portfolios WHERE business_id=?", (business_id,))
    if not portfolio:
        raise HTTPException(
            status_code=404,
            detail="create a paper trading portfolio first (POST /businesses/{id}/trading/portfolio) "
                   "-- live trading reuses its active strategy",
        )
    if not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_API_SECRET")):
        raise HTTPException(
            status_code=400,
            detail="ALPACA_API_KEY / ALPACA_API_SECRET are not configured -- set both (and "
                   "ALPACA_BASE_URL=https://api.alpaca.markets to actually reach the live "
                   "endpoint, not Alpaca's paper simulator) before enabling live trading",
        )

    db.execute("UPDATE paper_portfolios SET live_trading_enabled=1, updated_at=datetime('now') "
               "WHERE id=?", (portfolio["id"],))

    live_agent = db.query_one(
        "SELECT id FROM agents WHERE business_id=? AND permission_level >= 4 "
        "AND status != 'retired' ORDER BY created_at ASC LIMIT 1", (business_id,),
    )
    if not live_agent:
        agent_id = state["agents"].create(
            business_id, "Live Trading Agent", role="Real-Money Trading Executor",
            department="Trading", model="unassigned", permission_level=4,
        )
        state["agents"].set_status(agent_id, "idle")
        state["banker"].allocate(business_id, agent_id, 200.0,
                                  reason="starting ARC runway for live trading")

    cycle_job_id = state["jobs"].create(
        business_id, "Live trading cycle", "Run a live (real-money) trading cycle",
        interval_seconds=req.cycle_interval_seconds, permission_level_required=4,
        task_type="live_trading_cycle",
    )
    db.audit("owner", "enable_live_trading", "paper_portfolio", portfolio["id"],
              {"business_id": business_id, "cycle_job_id": cycle_job_id})
    return {"cycle_job_id": cycle_job_id, "live_trading": _live_trading_view(business_id)}


@app.post("/businesses/{business_id}/trading/live/disable")
def disable_live_trading(business_id: str):
    """Always safe, no confirmation required: flips live_trading_enabled
    off (the next executor pass refuses any already-assigned
    live_trading_cycle task -- see _handle_live_trading_cycle) and
    disables every scheduled live_trading_cycle job for this business,
    so nothing keeps recreating the task after this call returns."""
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    portfolio = db.query_one("SELECT * FROM paper_portfolios WHERE business_id=?", (business_id,))
    if not portfolio:
        raise HTTPException(status_code=404, detail="no trading portfolio for this business")

    db.execute("UPDATE paper_portfolios SET live_trading_enabled=0, updated_at=datetime('now') "
               "WHERE id=?", (portfolio["id"],))
    live_jobs = db.query(
        "SELECT id FROM scheduled_jobs WHERE business_id=? AND task_type='live_trading_cycle' "
        "AND enabled=1", (business_id,),
    )
    for job in live_jobs:
        state["jobs"].set_enabled(job["id"], False)
    db.audit("owner", "disable_live_trading", "paper_portfolio", portfolio["id"],
              {"business_id": business_id, "disabled_job_ids": [j["id"] for j in live_jobs]})
    return {"live_trading": _live_trading_view(business_id)}


@app.post("/businesses/{business_id}/trading/live/cycle")
def trigger_live_trading_cycle(business_id: str, department: Optional[str] = None):
    """Manually trigger one live_trading_cycle task right now, outside
    any scheduled job -- same on-demand pattern as trigger_trading_cycle.
    Refuses if live trading isn't enabled, rather than creating a task
    that _handle_live_trading_cycle will just reject."""
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    portfolio = state["db"].query_one(
        "SELECT * FROM paper_portfolios WHERE business_id=?", (business_id,)
    )
    if not portfolio:
        raise HTTPException(status_code=404, detail="create a paper trading portfolio first")
    if not portfolio["live_trading_enabled"]:
        raise HTTPException(status_code=400,
                             detail="live trading is not enabled for this business yet "
                                    "(POST /businesses/{id}/trading/live/enable)")
    task_id = state["orchestrator"].create_task(
        business_id, "Run a live (real-money) trading cycle", department=department,
        permission_level_required=4, task_type="live_trading_cycle",
    )
    return {"task_id": task_id}


# ---------------------------------------------------------------------
# Backtesting & bounded strategy search against REAL historical price
# data (see tasks/backtest.py, tasks/strategy_backtest_search.py,
# executor.py's _handle_strategy_backtest_search). Recommend-only, no
# real or paper money touched -- see that handler's docstring. Requires
# ALPHAVANTAGE_API_KEY to be configured; without it, market_data.py
# returns mock historical data and the search refuses to run rather
# than backtest against fabricated prices.
# ---------------------------------------------------------------------

def _estimate_backtest_calendar_days(train_start_date: str, validation_end_date: str) -> int:
    """A deliberately loose upper bound (calendar days, not trading
    days -- weekends/holidays make the real count somewhat lower) used
    only to size a starting ARC allocation generously. Never used for
    anything that needs to be exact; tasks/backtest.py's own date
    handling is what actually matters for correctness."""
    d1 = date.fromisoformat(train_start_date)
    d2 = date.fromisoformat(validation_end_date)
    return max((d2 - d1).days, 1)


# Conservative real-cost-per-simulated-day estimate, in ARC (see
# llm_client.PRICING_USD_PER_TOKEN and executor.ARC_PER_USD) -- rounded
# up generously, since a real call's exact token count varies with
# watchlist size and trade history length. Better to over-fund an
# agent by a little than have a real, already-spent backtest run get
# discarded at the very end for insufficient ARC (see
# executor._require_affordable -- the LLM cost is incurred either way,
# this only controls whether the result is kept).
ESTIMATED_ARC_PER_SIMULATED_DAY = 15.0
MIN_BACKTEST_AGENT_STARTING_ARC = 500.0


@app.post("/businesses/{business_id}/trading/backtest")
def trigger_strategy_backtest_search(business_id: str, req: TriggerBacktestSearchRequest,
                                      department: Optional[str] = None):
    """Triggers one bounded backtest/strategy-search task right now.
    Each simulated day is a real LLM call (same cost as one live/paper
    cycle), so the total cost scales with (train+validation days) x
    max_candidates -- real USD either way, same as every other task
    type. Never auto-promotes anything; review results via GET
    .../trading/backtest-runs and promote a candidate (if any) through
    the existing strategy-override endpoint yourself.

    Auto-creates a permission_level=2 agent (same pattern as
    enable_live_trading) if this business doesn't already have one
    eligible, and tops up its ARC balance if it's under-funded for
    THIS specific request -- a backtest can call the model hundreds of
    times in one task, far more than the generic Add Agent form's
    100 ARC default covers, so without this a real run could get all
    the way through (spending real USD on every call) only to have its
    result discarded at the very end for insufficient ARC."""
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    db = state["db"]
    if not db.query_one("SELECT id FROM paper_portfolios WHERE business_id=?", (business_id,)):
        raise HTTPException(status_code=404, detail="create a paper trading portfolio first -- "
                                                      "a backtest reuses its active strategy")
    if not (1 <= req.max_candidates <= 10):
        raise HTTPException(status_code=400, detail="max_candidates must be between 1 and 10")
    if req.train_start_date >= req.validation_split_date:
        raise HTTPException(status_code=400,
                             detail="train_start_date must be before validation_split_date")
    if req.validation_split_date >= req.validation_end_date:
        raise HTTPException(status_code=400,
                             detail="validation_split_date must be before validation_end_date")

    estimated_days = _estimate_backtest_calendar_days(req.train_start_date, req.validation_end_date)
    estimated_arc_needed = max(
        estimated_days * req.max_candidates * ESTIMATED_ARC_PER_SIMULATED_DAY,
        MIN_BACKTEST_AGENT_STARTING_ARC,
    )

    backtest_agent = db.query_one(
        "SELECT id, arc_balance FROM agents WHERE business_id=? AND permission_level >= 2 "
        "AND status != 'retired' ORDER BY created_at ASC LIMIT 1", (business_id,),
    )
    if not backtest_agent:
        agent_id = state["agents"].create(
            business_id, "Backtest Agent", role="Strategy Researcher",
            department="Trading", model="unassigned", permission_level=2,
        )
        state["agents"].set_status(agent_id, "idle")
        state["banker"].allocate(business_id, agent_id, estimated_arc_needed,
                                  reason="starting ARC runway for this backtest search")
    elif backtest_agent["arc_balance"] < estimated_arc_needed:
        state["banker"].allocate(
            business_id, backtest_agent["id"], estimated_arc_needed - backtest_agent["arc_balance"],
            reason="topping up ARC runway for this backtest search",
        )

    task_id = state["orchestrator"].create_task(
        business_id, "Backtest and search for a better trading strategy against real "
                     "historical price data", department=department,
        permission_level_required=2, task_type="strategy_backtest_search",
        task_input={
            "train_start_date": req.train_start_date,
            "validation_split_date": req.validation_split_date,
            "validation_end_date": req.validation_end_date,
            "starting_cash_usd": req.starting_cash_usd,
            "max_candidates": req.max_candidates,
        },
    )
    return {"task_id": task_id}


@app.get("/businesses/{business_id}/trading/backtest-runs")
def list_backtest_runs(business_id: str):
    if not state["businesses"].get(business_id):
        raise HTTPException(status_code=404, detail="business not found")
    rows = state["db"].query(
        "SELECT * FROM backtest_runs WHERE business_id=? ORDER BY created_at DESC LIMIT 20",
        (business_id,),
    )
    runs = []
    for r in rows:
        d = row_to_dict(r)
        d["candidates"] = json.loads(d.pop("candidates_json"))
        runs.append(d)
    return runs


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
def create_checkout(req: CheckoutRequest, request: Request):
    if not _checkout_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="Too many checkout attempts — please wait a minute and try again.",
        )
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
        # Stripe's own error text can echo back request details (e.g. a
        # malformed/invalid API key fragment) -- log it server-side only,
        # never hand raw Stripe error text to an unauthenticated customer.
        print(f"[store] Stripe checkout failed for order {order_id}: {e}", flush=True)
        raise HTTPException(status_code=502, detail="Could not start checkout — please try again shortly.")

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
    # would hit that constraint on a second INSERT. Checked explicitly
    # up front, rather than relying on catching the resulting exception,
    # so the common case needs no exception handling at all.
    if db.query_one("SELECT id FROM real_transactions WHERE stripe_event_id=?", (stripe_event_id,)):
        return {"status": "already_processed", "order_id": order_id}

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
        # Only reachable via a genuine race: another delivery of this
        # same event landed between the SELECT above and this INSERT,
        # and the UNIQUE constraint caught it. Confirm that's actually
        # what happened before treating this as a harmless duplicate —
        # a previous version of this code caught ANY exception here
        # (a real DB error included) and silently told Stripe "success,
        # don't retry" regardless of whether the transaction was ever
        # actually recorded, which could lose a paid order's fulfillment
        # entirely with no trace. If the row still doesn't exist, this
        # is a real failure and must surface as one.
        if not db.query_one("SELECT id FROM real_transactions WHERE stripe_event_id=?",
                             (stripe_event_id,)):
            raise
        return {"status": "already_processed", "order_id": order_id}

    if order["status"] == "pending_payment":
        db.execute(
            "UPDATE orders SET status='paid', paid_at=datetime('now'), "
            "stripe_payment_intent_id=? WHERE id=?",
            (payment_intent_id, order_id),
        )
        task_type = order["product_type"]
        # Each research task type expects its "what to research" value
        # under a different task_input key (see each tasks/research_*.py
        # module) -- orders.topic holds the customer's raw input
        # regardless of product, so it's remapped to the right key here.
        # A product_type missing from this map would otherwise silently
        # create a task with the wrong input key, which the executor's
        # handler would reject as missing its required field, failing an
        # already-paid order's fulfillment with no report ever produced.
        input_key = {
            "research_opportunity": "topic",
            "research_roblox_trend": "concept",
            "research_app_feasibility": "concept",
            "research_real_estate": "property_or_market",
        }.get(task_type, "topic")
        task_input = {input_key: order["topic"], "reference_urls": []}
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
