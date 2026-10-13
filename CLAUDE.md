# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AI-native autonomous holding company: a Business registry, Agent registry, Task
orchestrator with permission gating, the Banker (ARC ledger — an internal virtual
economy, never real money), a Human Approval Queue, and a full audit trail. Six
business verticals are built on that core (Opportunity Discovery, Roblox Game
Development, Automated Stock Trading, App Development Feasibility, Ops/Maintenance,
Real Estate research), plus a public storefront that takes real Stripe payments.
See `README.md` for what's real vs. simulated per feature and `DEPLOY.md` for
production deployment. There is no `package.json`/build step anywhere in this repo.

## Commands

Setup:
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Run the API + dashboard locally (SQLite by default, no `DATABASE_URL` needed):
```
.venv/bin/uvicorn api:app --reload
```
Set `DATABASE_URL=postgresql://...` to run against Postgres instead — `db.py`
switches drivers automatically. Set `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` to
unlock the dashboard/API (it fails closed with a 503 otherwise, by design).

Run the original zero-dependency core demo (no API keys needed):
```
python3 demo.py
```

Run the real-Claude-API demo (needs `ANTHROPIC_API_KEY`):
```
python3 demo_real_llm.py
```

Tests — there is no pytest/test runner. Every `test_*_offline.py` file is a
standalone script: run it directly, and it prints `PASS: ...` per check, exiting
non-zero on the first failed assertion.
```
.venv/bin/python3 test_trading_cycle_offline.py     # a single file
for f in test_*.py; do .venv/bin/python3 "$f" || echo "FAILED: $f"; done   # everything
node test_dashboard_render.js                        # the one JS test file (static/dashboard-render.js)
```
Always use `.venv/bin/python3`, not system `python3` — some test files import
`api`, which needs `fastapi`/`pydantic` installed. To run a single test function
within a file, call it directly, e.g. `.venv/bin/python3 -c "from test_trading_cycle_offline import test_hold_never_trades; test_hold_never_trades()"`.

There is no linter/formatter configured in this repo.

## Architecture

**Core data flow**: `orchestrator.py` is the center — it creates tasks, auto-assigns
them to an eligible agent (`Orchestrator.find_assignable_agent`, a single shared
query used by every endpoint that needs to answer "which agent" so that decision
can never diverge from reality), and completes/fails/cancels them, charging or
rewarding ARC via `banker.py` as a side effect. `registry.py` holds
Business/AgentRegistry. Every module talks to `db.py`'s `Database`/`PostgresDatabase`
abstraction, never to `sqlite3`/`psycopg2` directly — this is what let the project
move from SQLite to real Postgres as a driver swap, not a rewrite. The two schema
files (`schema.sql` for SQLite, `schema_postgres.sql` for Postgres) are maintained
by hand in parallel; a new table only needs `CREATE TABLE IF NOT EXISTS` in both,
but a new COLUMN on an existing table also needs an entry in `db.py`'s
`_COLUMN_MIGRATIONS` list, or an already-deployed database never picks it up.

**Permission levels** (`permission_levels.py`, 0-7): most levels are descriptive
tiers for the owner's own judgment about what to let an agent do, but two are
actually enforced in code — `permission_level_required >= 6` always routes through
the human `ApprovalQueue` first regardless of any agent's clearance, and `>= 7`
is never auto-executed by `executor.py` even after approval (owner must complete
it by hand). Don't add a new executable task type above level 6 without routing
it through the approval queue the same way.

**Three always-running background threads**, all started in `api.py`'s FastAPI
lifespan: `scheduler.py` (fires recurring `scheduled_jobs` rows into new tasks),
`executor.py` (polls for `status='assigned'` tasks whose `task_type` is in its
`HANDLERS` dict and actually runs them — a task type absent from `HANDLERS`, like
the default `'manual'`, is never auto-executed), and `fulfillment.py` (emails
finished storefront orders). `task_type -> handler` in `executor.py`'s `HANDLERS`
dict is the single dispatch point for every autonomous action in this system.

**"Model proposes, code disposes"** is the safety pattern repeated across every
autonomous decision: an LLM call produces a raw proposal (a trade, a budget
change, a strategy tweak), and a separate, pure, deterministic function — never
touching the network or DB — enforces the real hard limits regardless of what was
proposed. The clearest example is trading: `tasks/trading_cycle.py`'s
`decide_trades()` (the LLM call) vs. `apply_risk_limits()` (pure; clamps/skips
every proposed trade against `tasks/trading_common.py`'s `validate_parameters()`
bounds, and force-closes a position that's crossed a configured stop-loss/
take-profit, overriding the model's own decision for that symbol entirely).
`tasks/trading_strategy_review.py`'s self-improvement loop proposes new strategy
*parameters*, never new code, and every proposal — including the owner's own
manual override via the dashboard — passes through that same `validate_parameters()`
gate; there is no trusted path that skips it.

**Business verticals live under `tasks/`**, each usually a `decide_or_assess()` +
a `validate/apply` pair, registered as a `task_type` in `executor.py`. Trading in
particular spans paper (`trading_cycle.py`), real-money live trading (same
`apply_risk_limits()`, plus a second independent absolute-dollar safety layer in
`live_trading_safety.py`), and backtesting (`backtest.py`/`strategy_backtest_search.py`,
which replay the exact same `decide_trades()`/`apply_risk_limits()` pair against
real historical bars instead of live quotes) — a risk-parameter change can and
should be backtested before it's ever run live.

**Real external integrations are thin, stdlib-only clients** (`llm_client.py`,
`market_data.py`, `stripe_client.py`, `emailer.py`, `caller.py`, `alpaca_client.py`)
using only `urllib`, no `requests`/SDK dependency. Each raises its own `*Error` on
any failure (missing credentials, network error, bad response shape) and never
fabricates a successful result — callers rely on this (e.g. a storefront order is
only marked fulfilled after a real, verified email send). Each has a paired
explicitly-labeled `Mock*Client` for offline tests, and real market-data fetches
share one daily request budget tracked in `market_data.market_data_usage`
(`reserve_budget()` must be called, and can refuse, before any batch of real
quote/history fetches).

**Dashboard** (`static/dashboard.html` + `dashboard.js` + `dashboard-render.js`) is
plain HTML/CSS/JS with no build step or framework, gated by HTTP Basic Auth
(`dashboard_auth.py`, fails closed with 503 if credentials aren't configured).
`dashboard-render.js` exports pure `render*()` functions (string template -> HTML
string) that `test_dashboard_render.js` tests directly against hand-built data —
keep new UI in that same pure-function shape rather than inlining markup into
`dashboard.js`'s DOM-manipulation code.

**Test philosophy**: real logic, minimal mocking — mocks stand in only for things
that need real network/API keys this environment doesn't have (LLM calls, market
data, Stripe, email, Twilio). Each `test_*_offline.py` file is fully self-contained
(small helper classes like a fake LLM/market-data client are duplicated per file
rather than imported from a shared fixtures module) — follow that convention
rather than introducing shared test utilities.
