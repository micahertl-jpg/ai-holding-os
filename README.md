# AI Holding Company OS

## What this is
An AI-native autonomous holding company, built around the core
described in the project spec: a Business registry, Agent registry,
Task orchestrator with permission gating, the Banker (ARC ledger — an
internal virtual economy, never real money), the Human Approval Queue,
and a full audit trail. Deployed 24/7 on Railway with real Postgres —
see `DEPLOY.md`.

All six of the project spec's planned business verticals are built on
that core: **Opportunity Discovery**, **Roblox Game Development**,
**Automated Stock Trading** (paper trading by default, with an
explicitly opt-in real-money Live Trading mode — see below), **App
Development Feasibility**, **Ops/Maintenance** (watches this system's
own health — see below), and **Real Estate** (investment research
only — see below).

Four of those six verticals (everything except trading and
Ops/Maintenance, which has no customer at all) are also **monetized
for real**: a public storefront takes real Stripe payments and emails
a real, AI-generated report to the customer. This is the only place in
the whole system real money changes hands — see "Storefront — real
payments" below.

## What's real vs. simulated
- **REAL:** the data model, permission gating, ARC accounting, the
  approval-queue gate, the audit log, every LLM call (real Anthropic
  API), the FastAPI backend + dashboard (deployed 24/7, real Postgres),
  the scheduler/executor background threads, all six verticals'
  research/assessment logic, and the storefront's real Stripe payments
  + Resend emails for three of those four monetized verticals —
  personally verified live by the owner with real purchases (Real
  Estate's storefront integration is built and offline/live-verified
  the same way, but not yet purchased for real — see its section below).
- **SIMULATION BY DEFAULT** (per the project spec's safety
  requirements): Automated Stock Trading defaults to paper trading —
  no brokerage integration is reachable unless the owner deliberately
  opts a business into **Live Trading** (real money, real orders via
  Alpaca — see "Live Trading — REAL MONEY" below). ARC (the internal
  agent economy) never represents real money, even where agents "earn"
  or "spend" it. Ops/Maintenance only ever recommends — it never acts
  on its own findings. Real Estate is investment research only — never
  an appraisal, never a brokered transaction, structurally incapable
  of either (no code path exists that lists, offers, or negotiates a
  property).
- **NOT YET BUILT:** none — all six of the project spec's planned
  verticals are built. See "Next real steps" for what's left overall.

## Why SQLite + stdlib-only Python
Built inside a sandboxed environment with no internet access, so no
`pip install` was possible. The schema and module boundaries
(`Database` class in `db.py`) are written so swapping SQLite for
Postgres later is a driver change, not a rewrite — every other module
talks to `Database`, never to `sqlite3` directly. Production now runs
on real Postgres (Railway); SQLite remains the zero-setup default for
local development and every offline test.

## Run it
```
python3 demo.py
```
Creates `holding_os.db`, runs a full business → agents → ARC allocation →
task → approval-gated task → owner approval cycle, and prints a
dashboard-style summary plus the audit trail.

## File map
This is the original core, from before any business vertical or the
storefront existed — `schema.sql`/`schema_postgres.sql` now hold many
more tables (`opportunities`, `roblox_trends`,
`app_feasibility_assessments`, the `paper_*`/`trading_*` trading
tables, `orders`, `real_transactions`, `scheduled_jobs`, ...) than just
the six listed below. See each feature's own section above for its
specific files; this list is the foundation everything else sits on.
- `schema.sql` — the six original core tables (businesses, agents,
  tasks, arc_ledger, approvals, audit_log)
- `db.py` — connection + audit-log helper; also the `Database`/
  `PostgresDatabase` abstraction every other module talks through
- `registry.py` — BusinessRegistry, AgentRegistry (create/list/pause/retire)
- `banker.py` — Banker: allocate / reward / charge / penalize ARC, with
  a hard floor against negative balances
- `approval.py` — ApprovalQueue: the only gate through which any
  permission_level >= 6 action can proceed
- `orchestrator.py` — Task lifecycle + naive assignment + automatic
  routing to approval for consequential tasks
- `api.py` — the real FastAPI app; every HTTP endpoint, the dashboard-
  auth middleware, and the storefront's checkout/webhook routes live here
- `scheduler.py` / `executor.py` / `fulfillment.py` — the three
  background threads (recurring jobs, running research/trading tasks,
  emailing finished storefront orders), all started in `api.py`'s lifespan
- `dashboard_auth.py` / `rate_limiter.py` — HTTP Basic Auth for the
  internal dashboard/API, and the in-memory rate limiter backing both
  it and `/store/checkout`
- `stripe_client.py` / `emailer.py` — real Stripe Checkout + webhook
  verification, real transactional email via Resend
- `demo.py` — proves the original core works together end to end

## Real LLM call — status
`llm_client.py` + `tasks/summarize_urls.py` + `demo_real_llm.py` wire an
actual (non-simulated) Claude API call into `orchestrator.complete_task`
for one narrow task type: fetch up to 3 URLs, summarize each.

What was actually verified in the sandbox that built this (no API key,
no general internet access there):
- `test_summarize_urls_offline.py` — 3 passing tests proving the
  fetch->strip->summarize->error-handling logic is correct, using
  mocked HTTP responses (no real network needed).
- `demo_real_llm.py` run live — the fetch calls correctly went out,
  correctly got blocked (403, from that sandbox's own restrictions),
  and the failures were correctly recorded per-URL in the task result
  and the database — nothing was faked or silently swallowed.
- **UPDATE: verified live by the owner.** Run with a real key and real
  internet on the owner's machine: real fetches, real Anthropic API
  calls, real model-generated summaries, stored in the real SQLite DB.
  One real bug was found and fixed in that process: `python.org`
  returned gzip-compressed content that `urllib` didn't auto-decompress,
  producing garbled binary text instead of a clean fetch error. Fixed by
  requesting `Accept-Encoding: identity` and treating any still-
  undecodable response as a fetch failure rather than feeding garbage
  to the model.

**To do the first real live test yourself:**
```
export ANTHROPIC_API_KEY=sk-ant-...
python3 demo_real_llm.py
```
If the key/network are good, you'll see real fetched page text
genuinely summarized by Claude, and `demo_real_llm.py` will print
`>> Real ANTHROPIC_API_KEY detected <<` instead of the mock warning.
If it fails, it will fail loudly (the task is marked `failed` in the DB
with the real error message) — it will never print a fabricated
success.

## API layer — status (Postgres + FastAPI)
`api.py` is a real FastAPI app over the same registry/banker/approval/
orchestrator modules the demos use, with `db.py` extended to support
either SQLite (default, zero accounts needed) or real Postgres via
`DATABASE_URL`.

**What was actually verified in the sandbox that built this** (no PyPI
access there — `pip install fastapi` fails outright):
- `python3 -m py_compile api.py db.py` — both are syntactically valid.
- `test_api_logic_offline.py` — 8 passing checks that exercise the
  *exact same call sequence* every api.py endpoint makes (create
  business/agent, allocate ARC, create+auto-assign a task, complete it,
  aggregate ARC summary, route a permission_level>=6 task to approval,
  approve it) — using the real SQLite backend, not a mock.
- `demo.py` and `test_summarize_urls_offline.py` still pass after the
  `db.py` refactor — the shared code path api.py now also uses didn't
  regress anything already working.
- **A real bug was found and fixed by that testing**, not hidden: the
  original approval-routing logic only escalated a permission_level>=6
  task to the human ApprovalQueue if an agent with that much clearance
  already existed in the business. A business with no such agent yet
  would have that task silently sit in `queued` forever — a real gap
  against the project's "consequential actions always reach human
  review" requirement. Fixed in `orchestrator.py`: the approval gate is
  now checked first and unconditionally, independent of whether any
  agent happens to be cleared for it.

**What was NOT verified there, because it requires things this sandbox
doesn't have:**
- `fastapi`/`uvicorn` actually installed and the HTTP server actually
  running (routing, request validation, status codes, OpenAPI docs).
- `psycopg2-binary` installed and a real Postgres connection
  (`PostgresDatabase` — the `?`→`%s` and `datetime('now')`→`now()`
  translation logic is written but has never executed against a real
  Postgres server).

**To do that first real verification yourself:**
```
pip install -r requirements.txt
uvicorn api:app --reload
```
Then open `http://127.0.0.1:8000/docs` — FastAPI auto-generates an
interactive API explorer there. Try `POST /businesses`, then
`POST /businesses/{id}/agents`, then `POST /businesses/{id}/tasks`, and
watch it behave like `demo.py` did, but over real HTTP. This uses the
local SQLite file by default — no Postgres account needed for this
first check.

To test against real Postgres (once you have a Supabase/Railway
instance):
```
export DATABASE_URL=postgresql://user:pass@host:5432/dbname
uvicorn api:app --reload
```
If this fails, paste the exact error — connection string format issues
are the most likely first failure, and are easy to fix once visible.

## Dashboard — status
`/dashboard` (served by the same FastAPI process) is a plain HTML/CSS/JS
single-page dashboard — no Node, no npm, no build step, no CDN
dependency. Deliberately built this way instead of a separate Next.js
app: the API already runs locally on your machine, so a same-origin
static page avoids a second server and all the CORS configuration that
would otherwise come with it.

The JS is split into two files on purpose:
- `static/dashboard-render.js` — pure functions that turn API JSON into
  HTML strings. No `document`/`window`/`fetch` — this is why it's
  testable without a browser.
- `static/dashboard.js` — the DOM wiring (fetch calls, event listeners)
  that calls those functions. This part genuinely cannot be verified
  without a real browser hitting a real running server.

**What was actually verified in the sandbox that built this:**
- `node test_dashboard_render.js` — 11 passing tests of the render
  functions, using sample data shaped exactly like the real JSON your
  live API returned earlier in this project (the `Test Co` / `Rex`
  business and agent are literally the same shapes, not invented ones),
  including an XSS-escaping check and both populated and empty-state
  cases for every panel.
- `node --check dashboard.js` / `node --check dashboard-render.js` —
  both syntactically valid.
- The full Python test suite (offline API-logic tests, `demo.py`,
  the summarize_urls tests) still passes after wiring `/dashboard` and
  `/static` into `api.py`.

**What was NOT verified there** (needs a real browser against your
running server — this sandbox has neither):
- That the page actually renders correctly, that the forms submit
  correctly, that clicking Approve/Reject/Pause/Retire actually calls
  the right endpoint and refreshes the view.

**To verify it yourself:**
```
python -m uvicorn api:app --reload
```
then open `http://127.0.0.1:8000/dashboard` in your browser. Create a
business, add an agent, add a task with `permission_level_required: 6`
(so you get something to approve), and click through Approve/Reject.
Tell me what you see — screenshots are great if something looks broken,
since I can't see your browser directly.

## Real bugs found via live testing (fixed)
Bugs surfaced by actually running this, not by inspection — all fixed,
all now covered by tests:
1. `python.org` served gzip-compressed content that `urllib` didn't
   auto-decompress, producing garbled binary text instead of a clean
   fetch error (found running `demo_real_llm.py` live). Fixed by
   requesting `Accept-Encoding: identity`.
2. After clicking Approve in the real dashboard, the owner noticed the
   linked task still showed `awaiting_approval`. Turned out there was
   no code path that ever moved a task out of `awaiting_approval` at
   all — `complete_task` only accepts `assigned`/`in_progress`, so an
   approved task was permanently stuck, approved or not. Fixed by
   adding a `task_id` column linking approvals to the task they gate,
   plus `Orchestrator.promote_approved_task()` (approve -> `assigned`,
   now completable) and `Orchestrator.cancel_rejected_task()` (reject ->
   `cancelled`, not left stuck either), covered by 3 new passing checks
   in `test_api_logic_offline.py` (11 total).
3. Clicking Disable on a scheduled job threw a 404 "agent not found."
   The job-toggle button reused the `.btn-pause`/`.btn-approve` CSS
   classes for styling, and `dashboard.js`'s click handler dispatched
   by CSS class — so the class check matched first and misrouted the
   click to the agent-pause endpoint with no agent id. Fixed properly,
   not just patched: refactored the entire click handler to dispatch on
   an explicit `data-action` attribute instead of CSS classes, so
   styling and behavior can never collide again structurally. Covered
   by new regression assertions in `test_dashboard_render.js` (14
   total).
4. New tasks the scheduler created server-side didn't appear on the
   dashboard until some unrelated click (like the Disable button above)
   happened to trigger a refresh. There was no polling loop at all —
   the page only ever refreshed in direct response to a user action.
   Fixed by adding a 5-second `setInterval` auto-refresh (with a
   re-entrancy guard so a slow request doesn't pile up overlapping
   fetches).
5. A research task stayed on `queued` forever even after an eligible
   agent became available. Root cause: task assignment was only ever
   attempted once, synchronously, at the moment a task was created — if
   the only agent was busy (or didn't exist yet), the task was orphaned
   permanently, with nothing ever giving it a second look. Fixed by
   persisting each task's originally-requested `department` (previously
   discarded after creation) and adding
   `Orchestrator.retry_queued_tasks()`, called at the start of every
   executor pass (`executor.py`) — so a stuck task gets re-attempted
   roughly every `EXECUTOR_POLL_INTERVAL_SECONDS` (10s by default)
   without needing a second background thread. Covered by 2 new tests
   (one at the orchestrator level, one proving the full retry ->
   execute path end to end).

## Scheduler — status
`scheduler.py` adds recurring tasks, running as a background thread
inside the same process as the API — no Celery, no Redis, no
APScheduler. `api.py`'s `lifespan` starts this thread on startup and
stops it cleanly on shutdown.

A scheduled job creates a real task through the exact same
`orchestrator.create_task()` path as a manually-created one, so it goes
through identical auto-assignment and permission gating — a schedule is
not a way to bypass the approval queue. A job below 30-second intervals
is rejected outright rather than silently busy-looping.

**Real concurrency issue found and fixed while building this:** adding
a background thread that shares one SQLite connection with the API's
request-handling threads exposed a genuine race condition — SQLite
connections aren't safe for concurrent use across threads without
serialization, even with `check_same_thread=False`. This wasn't a new
problem the scheduler introduced so much as a latent one in `api.py`
from the start (FastAPI already runs sync endpoints across a thread
pool), just now guaranteed to actually happen. Fixed by adding a
`threading.Lock` around every `Database`/`PostgresDatabase` operation.

**What was actually verified in the sandbox that built this:**
- `test_scheduler_offline.py` — 7 passing checks on `tick()` with
  explicit, controlled time (no sleeping, fully deterministic): jobs
  under 30s rejected, a due job fires exactly once, doesn't double-fire
  before its next run, does fire again once due, and disabled/
  re-enabled jobs behave correctly.
- `test_db_concurrency.py` — a **real** concurrency test: 20 actual OS
  threads x 10 writes each hitting one shared `Database` instance
  simultaneously. Zero errors, exactly 200 ARC awarded (not less), exactly
  200 ledger rows (not more or fewer) — no lost updates, no corruption.
- I also reproduced the race **without** the lock in a 10-second throwaway
  script to confirm the test isn't vacuous: without serialization, the
  same kind of concurrent read-modify-write silently lost 2 out of 500
  increments — no error raised, just a wrong number. That's the failure
  mode the lock prevents.
- 14 dashboard-render.js tests now (added `renderJobsTable`, covering
  empty/enabled/disabled states), `node --check` on both JS files,
  and the full existing test suite (offline API-logic tests, `demo.py`,
  summarize_urls tests) all still pass with the scheduler wired in.

**What was NOT verified there** (needs a real running process):
- That the background thread actually starts, actually polls every
  `SCHEDULER_POLL_INTERVAL_SECONDS` (default 15s), and actually creates
  tasks over real wall-clock time while the server runs.
- The new dashboard "Scheduled Jobs" panel/form in a real browser.

**To verify it yourself:**
```
python -m uvicorn api:app --reload
```
Open `http://127.0.0.1:8000/dashboard`, add a scheduled job with a
short interval (30s, the minimum), and watch the Tasks panel — a new
task should appear roughly every 30 seconds without you doing anything,
proving the background thread is actually running. Try disabling the
job and confirm it stops.

## Opportunity Discovery — status (first real business vertical)
The first real business vertical, per the project spec (Sec 9.B). This
also closes a real architectural gap that existed until now: nothing in
the system ever automatically *executed* a task — `create_task()`
would create and auto-assign a row, but turning that into actual AI
work only ever happened by hand, in `demo_real_llm.py`. A scheduled job
could have fired every hour forever and just piled up idle "assigned"
tasks doing nothing.

**What's new:**
- `tasks/webfetch.py` — the URL-fetching logic, extracted out of
  `summarize_urls.py` so `research_opportunity.py` doesn't duplicate it
  (and so the gzip-encoding bug fix from earlier stays fixed in one
  place, not two).
- `tasks/research_opportunity.py` — takes a topic and up to 3 optional
  reference URLs, asks the model for a structured JSON assessment
  (market size, competition, startup cost, revenue potential,
  time-to-market, operational complexity, legal/regulatory risk,
  capital requirements, downside risk, and a mandatory
  `confidence_level`). Every field is framed as an estimate, never a
  guarantee — per Sec 26 "No Magic." Invalid or incomplete JSON from
  the model is a hard failure, not a fallback to a fabricated result.
- `tasks.task_type` / `tasks.task_input` columns (both schemas) — a
  task can now carry a handler name and structured input, instead of
  only a free-text objective.
- `executor.py` — **the piece that closes the execution gap.** A
  background thread (started in `api.py`'s `lifespan`, alongside the
  scheduler's) polls for tasks with status `assigned` and a
  `task_type` it recognizes, runs the matching handler with a real LLM
  client, and calls `complete_task`/`fail_task` with the outcome.
  `task_type='manual'` (the default for every task created before this
  existed, and every one you create by hand) is never touched — nothing
  about existing behavior changed.
- New endpoints: `POST /businesses/{id}/opportunities/research` (topic
  + optional reference URLs -> creates a task, returns immediately) and
  `GET /businesses/{id}/opportunities` (list saved assessments). A new
  dashboard panel shows researched opportunities as cards and lets you
  kick off new research directly.
- `POST /businesses/{id}/opportunities/{opportunity_id}/launch` closes
  the loop research alone never did: turns a researched opportunity
  into a real, standalone business, seeded with that opportunity's
  topic/summary as its founding objective. Owner-initiated only (a
  "Launch Business" button on each unlaunched opportunity card) — it
  never moves money, never auto-adds agents, and never auto-activates
  anything; it's the exact same `POST /businesses` a blank "Create a
  New Business" form hits, just pre-filled from real research instead
  of typed by hand. `opportunities.launched_business_id` (new column,
  retrofitted via `_COLUMN_MIGRATIONS` in `db.py` for existing
  deployments) means an opportunity can only ever be launched once —
  the card shows a "launched" badge instead of the button afterward,
  and a second attempt is refused with a pointer to the business that
  already exists. The same launch action exists on every other
  research vertical too — Roblox Game Development, App Development
  Feasibility, and Real Estate all have their own `.../{id}/launch`
  endpoint and "Launch Business" button, sharing one
  `_launch_business_from_research()` helper and one
  `LaunchBusinessRequest` model in `api.py` rather than four copies of
  the same logic.

**What was actually verified in the sandbox that built this** (still no
PyPI/API-key access there):
- `test_research_opportunity_offline.py` — 7 passing checks: success
  with and without reference URLs, a failed reference fetch shrinking
  evidence without failing the task, the 3-URL cap, and three separate
  validation-failure paths (invalid JSON, a missing required field, an
  invalid `confidence_level` value) all correctly raising rather than
  fabricating a result.
- `test_executor_offline.py` — 5 passing checks: a `summarize_urls` task
  and a `research_opportunity` task both get picked up and completed
  correctly; a bad model response fails the task with the real error
  and saves nothing to `opportunities`; a `task_type='manual'` task is
  never touched; a `queued` (not yet assigned) typed task is left
  alone. This is the executor's core contract, and all of it holds.
- 4 new dashboard-render.js tests (18 total) for the opportunities
  panel, including malformed-JSON safety and the empty/no-references
  states.
- The full existing suite (API logic, scheduler, concurrency,
  summarize_urls, `demo.py`, `demo_real_llm.py`) still passes unchanged
  — this was a genuinely additive change, not a risky refactor.

**What was NOT verified there** (needs your machine, same as every LLM
task type so far):
- An actual `research_opportunity` task running against the real
  Anthropic API and producing a real assessment. **Important:** without
  a real `ANTHROPIC_API_KEY` set, these tasks will *fail* by design —
  `MockClient`'s canned response isn't valid JSON, so the strict
  validation in `research_opportunity.py` correctly rejects it. That's
  the intended behavior (no fabricated assessments), not a bug, but it
  means you need your API key set for this feature to do anything
  visible.
- The new dashboard panel and form in a real browser.

**UPDATE from the owner's first live test:** a task got created but
stayed on `queued` forever — this was the real bug described above (fix
#5 in "Real bugs found via live testing"): the only agent was busy when
the task was created, and nothing ever retried it. Fixed via
`Orchestrator.retry_queued_tasks()`, verified offline; **not yet
re-verified live** — this needs a fresh test to confirm the fix actually
resolves what you saw.

**To verify it yourself:**
```
export ANTHROPIC_API_KEY=sk-ant-...
python -m uvicorn api:app --reload
```
Open the dashboard, use the new "Research This Opportunity" form with
a real topic (e.g. "AI-powered meal planning apps") and optionally 1-3
real URLs, submit, and wait ~10-15 seconds (the executor polls every 10s
by default) — a card should appear with a real, model-generated
assessment and an honest confidence level. Make sure at least one agent
in the business is `idle` (not `working` on something else) when you
submit, or give it ~10s for the retry to kick in once one frees up.

## Roblox Game Development — status (second business vertical)

The second real business vertical, same pattern as Opportunity
Discovery: `tasks/research_roblox_trend.py` takes a game genre/mechanic/
concept and up to 3 optional reference URLs, and asks the model for a
structured JSON assessment — player demand signals, competition level,
build complexity, target audience, monetization fit, estimated dev
time, similar successful games, risk factors, and a mandatory
`confidence_level`. The model is explicitly instructed to never suggest
fake engagement, bots, or any other tactic that violates Roblox's Terms
of Service — asked to flag that plainly in `risk_factors` instead of
working around it.

- `test_research_roblox_trend_offline.py` — 7 passing checks covering
  the same success/validation-failure shape as Opportunity Discovery's
  tests (invalid JSON, missing fields, an invalid `confidence_level`,
  the reference-URL cap, all hard failures rather than fabricated
  results).
- Schema: `roblox_trends` (both `schema.sql` and `schema_postgres.sql`).
  `executor.py`'s `research_roblox_trend` handler follows the same
  real-cost-charged/confidence-rewarded ARC accounting as every other
  research task type.
- Endpoints and a dashboard panel mirror Opportunity Discovery's
  exactly: `POST /businesses/{id}/roblox-trends/research`,
  `GET /businesses/{id}/roblox-trends`, result cards on `/dashboard`.
- **Confirmed live by the owner** as one of the storefront's two
  originally-launched products (see "Storefront — real payments"
  below) — a real customer purchase, real Stripe payment, and a real
  emailed report were personally verified working end to end.

## Automated Stock Trading — status (paper by default; Live Trading is separate, see below)

Per the project spec's Trading Safety section: this is simulation only.
There is no brokerage integration anywhere in this codebase — that's a
structural fact (no code path exists that could place a real order), not
a config flag someone could flip on. Real trading stays out of scope
until explicitly, separately authorized and built.

**What's new:**
- `market_data.py` — a real Alpha Vantage quote client (`GLOBAL_QUOTE`),
  stdlib-only (`urllib`, same pattern as `llm_client.py`), plus an
  explicitly-labeled `MockMarketDataClient` for when no
  `ALPHAVANTAGE_API_KEY` is set. Every quote is tagged `mock: True/False`
  so downstream code can refuse to trade on fake prices.
- `tasks/trading_common.py` — the strategy-parameter schema (watchlist,
  position/trade/exposure limits, the drawdown circuit breaker, a
  minimum-confidence floor) and `validate_parameters()`, which enforces
  hard *absolute* ceilings no proposal — including the self-improvement
  loop's own output — can ever exceed.
- `tasks/trading_cycle.py` — the recurring task type. The model
  *proposes* a decision per watchlist symbol (buy/sell/hold, a mandatory
  confidence level, a rationale); `apply_risk_limits()` is a separate,
  pure, deterministic function that clamps or skips every proposal
  against the strategy's limits, independent of what the model asked
  for. This is unit-tested with no LLM involved at all — the safety
  guarantee doesn't depend on model behavior.
- `tasks/trading_strategy_review.py` — the "self-improving" half, run
  far less often. It computes real statistics (win rate, realized P&L,
  drawdown) in code — never asks the model to recall or invent numbers —
  and asks for a revised parameter proposal with a rationale, framed
  explicitly as an untested hypothesis. Per the project spec's warning
  against uncontrolled self-modification: this **never touches code**,
  only ever produces a new, versioned row of parameters that must pass
  the same `validate_parameters()` bounds check as anything else. Every
  prior version is kept (not overwritten), so the owner can always see
  the full history and what changed why.
- Schema: `paper_portfolios`, `paper_positions`, `paper_trades`,
  `trading_strategy_versions`, `trading_snapshots` (both `schema.sql` and
  `schema_postgres.sql`).
- `executor.py`: `trading_cycle` and `trading_strategy_review` handlers,
  registered in `HANDLERS`. The drawdown circuit breaker pauses the
  trading agent when triggered — see "Real bugs found" below for a real
  bug this surfaced and fixed.
- New endpoints under `/businesses/{id}/trading/...`: create a paper
  portfolio, read it (with positions + latest equity snapshot), read
  trade history, read/override the strategy version history, manually
  trigger a cycle or review, and `POST .../enable-auto-trading` — a
  single call that creates the portfolio (if missing), a dedicated
  Trading Agent (`permission_level=3`/SIMULATE) if none exists yet, and
  two scheduled jobs (`trading_cycle`, `trading_strategy_review`) — this
  is what makes it actually *automatic*, since nothing runs on its own
  without a scheduled job.
- A new dashboard panel: portfolio summary (cash/equity/P&L), open
  positions, trade history (with the model's stated rationale and
  confidence shown on every row, never hidden), and the full strategy
  version history.

**What was actually verified in THIS build** (unlike the sections above,
this session had real PyPI access and could actually install and run
`fastapi`/`uvicorn`/`psycopg2-binary`):
- `python3 -m py_compile` on every new/changed `.py` file, `node --check`
  on both dashboard JS files.
- The full offline suite: `test_market_data_offline.py` (6 checks),
  `test_trading_cycle_offline.py` (21 checks, including every risk-limit
  scenario with no LLM involved), `test_trading_strategy_review_offline.py`
  (8 checks), new `test_executor_offline.py` cases (5 more, DB-integration
  level: a real paper trade executing and updating cash/positions/a
  snapshot, the drawdown circuit breaker actually pausing the agent, a
  missing-portfolio task failing loudly, a strategy review promoting a
  validated version, an out-of-bounds proposal being rejected outright),
  and 13 new `test_dashboard_render.js` checks. Every pre-existing test
  in this repo still passes unchanged.
- **The real thing this sandbox's predecessor couldn't do:** installed
  the actual dependencies, stood up a real local Postgres database, ran
  `uvicorn api:app` for real, and exercised the new endpoints over real
  HTTP — created a business, created a paper portfolio, called
  `enable-auto-trading` (which really did create the agent + both
  scheduled jobs), watched the real scheduler and executor background
  threads fire the jobs over real wall-clock time, confirmed
  `/businesses/{id}/dashboard` returns the new trading fields, and loaded
  `/dashboard` in a real headless browser (screenshot taken) — the new
  panel renders correctly and matches the existing visual style.
- With no `ANTHROPIC_API_KEY`/`ALPHAVANTAGE_API_KEY` set (neither was
  available in this build environment either), a triggered
  `trading_cycle` task correctly **failed loudly** with
  `"refusing to trade — mock market data for [...] (ALPHAVANTAGE_API_KEY
  not configured...)"`, and a `trading_strategy_review` task correctly
  failed on the mock LLM client's non-JSON response — exactly the
  intended behavior, never a fabricated trade or a silently-accepted bad
  proposal.
- **A real bug found and fixed via this live testing, not by
  inspection:** `Orchestrator.complete_task()` unconditionally resets the
  completing agent's status to `'idle'` on success. The drawdown-halt
  logic was originally written to pause the agent *inside* the
  `trading_cycle` handler, before `complete_task()` ran — which silently
  got overwritten back to `'idle'` the moment the task completed,
  defeating the circuit breaker entirely. Fixed by adding a small,
  backward-compatible extension point: a handler may optionally return a
  4th element (the agent status to force *after* `complete_task()`
  runs) instead of the usual 3-tuple; every other handler is completely
  unaffected. Caught by this feature's own offline test
  (`test_trading_cycle_drawdown_halt_pauses_the_trading_agent`), not by
  inspection — proof the circuit breaker actually works, not just that
  it's documented.

**What was NOT verified** (needs a real Anthropic + Alpha Vantage key,
same as every other LLM-driven task type in this project):
- An actual `trading_cycle` task running against real market prices and
  a real model decision, executing a real (paper) trade end to end.
- An actual `trading_strategy_review` proposing a real parameter change
  from real accumulated trade history.
- The new dashboard panel's forms (Create Paper Portfolio, Enable
  Auto-Trading, Run Cycle/Review Now) clicked through in a real browser
  by a human — only automated/headless verification happened here.

**To verify it yourself:**
```
export ANTHROPIC_API_KEY=sk-ant-...
export ALPHAVANTAGE_API_KEY=...   # free key at alphavantage.co
python -m uvicorn api:app --reload
```
Open the dashboard, pick or create a business, click **Enable
Auto-Trading** (creates a $10,000 paper portfolio, a Trading Agent, and
both scheduled jobs in one step), then click **Run Trading Cycle Now** —
within a few seconds you should see a real decision with a stated
confidence and rationale, and (if it decided to buy/sell) a real paper
trade in the Trade History table. Try **Run Strategy Review Now** too,
though it's more informative after a handful of real trades exist.

## Live Trading — status (REAL MONEY, opt-in per business — see DEPLOY.md)

A separate, deliberately harder-to-reach feature on top of everything
above: it places real orders with real cash through
[Alpaca](https://alpaca.markets), reusing the paper system's strategy
(watchlist, position/trade/exposure limits) and its two safety
guarantees ("model proposes, code disposes"; every limit is enforced in
plain deterministic code, never trusted to the model) completely
unchanged. See **DEPLOY.md's "Live Trading — REAL MONEY" section** for
the full env var / enable checklist — this section only covers what was
built and what was and wasn't verified.

**What's new:**
- `alpaca_client.py` — a real Alpaca brokerage client, stdlib-only
  (`urllib`, same pattern as `market_data.py`). The core safety
  property: `AlpacaClient` defaults to Alpaca's own PAPER endpoint even
  when real credentials are configured — reaching the live endpoint
  requires a separate, explicit `ALPACA_BASE_URL` env var, so
  credentials alone can never place a real order. `MockAlpacaClient`
  (always `is_paper=True`) is the explicit offline-test stand-in;
  `get_default_client()` raises rather than silently falling back to
  it when credentials are missing — unlike `market_data.py`'s mock
  fallback, a live-trading task must fail loudly, never quietly no-op.
- `tasks/live_trading_safety.py` — a SECOND, independent safety layer,
  applied on top of (never instead of) the existing percentage-based
  limits: `LIVE_TRADING_MAX_TRADE_USD` (absolute per-trade dollar cap,
  default $15), `LIVE_TRADING_MAX_DAILY_LOSS_USD` (realized-loss
  circuit breaker, default $7), and `LIVE_TRADING_KILL_SWITCH` (an env
  var checked fresh every cycle — an instant, redeploy-free emergency
  halt). Defaults are sized for a small ~$100 starting live account.
- Schema: `paper_portfolios.live_trading_enabled` (owner-only switch,
  off by default) plus two new tables, `live_trades`/`live_snapshots` —
  deliberately separate from `paper_trades`/`trading_snapshots` so a
  report that forgets a `WHERE` clause fails loudly instead of silently
  blending real and simulated activity (both `schema.sql` and
  `schema_postgres.sql`).
- `executor.py`: `_handle_live_trading_cycle`, registered under task
  type `live_trading_cycle` (`permission_level_required=4`, one tier
  above paper's 3 — see `permission_levels.py`). Fetches cash/positions
  fresh from Alpaca every cycle (never a locally-summed ledger), runs
  the *exact same* `decide_trades()`/`apply_risk_limits()` from
  `tasks/trading_cycle.py`, applies `apply_live_safety_caps()`, places
  real orders, polls for a real fill (never assumes a placed order
  filled), and records the REAL fill price/quantity — never the
  pre-trade quoted price. A drawdown or daily-loss breach pauses the
  live trading agent, same pattern as paper's circuit breaker.
- New endpoints under `/businesses/{id}/trading/live/...`: `enable`
  (requires `confirm_real_money: true` and an existing paper
  portfolio/strategy; creates a Live Trading Agent and a recurring
  scheduled job), `disable` (always safe, no confirmation needed — also
  disables the scheduled job), a status/history read, and a manual
  "run one cycle now" trigger.
- A new dashboard panel, visually distinct (red accent) from the paper
  panel: enable/disable controls (enabling requires an explicit
  confirmation checkbox plus a JS `confirm()` prompt), real cash/
  equity/open-positions, a real equity sparkline, and real trade
  history (fill price/quantity, realized P&L, and whether the per-trade
  cap was applied to each row).

**What was actually verified in THIS build:**
- `python3 -m py_compile` on every new/changed `.py` file; `node -e
  "require(...)"` on the changed dashboard JS files.
- The full offline suite: `test_alpaca_client_offline.py` (15 checks —
  including the paper-by-default safety property, that a rejected
  order never returns a fabricated success, and that `get_default_
  client()` never silently falls back to a mock), `test_live_trading_
  safety_offline.py` (14 checks — every clamp boundary, the daily-loss
  halt, the kill switch, pinning the exact $15/$7/$2 default values),
  7 new `test_executor_offline.py` cases (DB-integration level: a real
  order executed through a fake broker client and recorded with its
  REAL fill data, refusing to run when live trading isn't enabled,
  respecting the kill switch, refusing when the broker client is still
  pointed at paper even with live trading enabled, a broker-rejected
  order never being recorded as a trade, a realized daily loss pausing
  the live agent, and a missing-portfolio task failing loudly), and 8
  new `test_dashboard_render.js` checks. Every pre-existing test in
  this repo still passes unchanged.
- Schema changes applied cleanly and verified on both SQLite (direct
  `Database()` instantiation) and a real local Postgres database
  (`psql -f schema_postgres.sql`, confirmed table structure).

**What was NOT verified** (this sandbox has no general internet access
at all, so there is no route to Alpaca's real API from here, unlike
Anthropic/Stripe/Resend which at least had network access attempted
elsewhere in this project):
- Any actual network call to Alpaca — order placement, fill polling,
  account/position fetching — has only ever been exercised against
  `MockAlpacaClient` (or a thin same-shaped test double reporting
  `is_paper=False`), never a real Alpaca account, not even their own
  paper-trading endpoint.
- The new dashboard panel's forms (Enable/Disable Live Trading, Run
  Live Cycle Now) clicked through in a real browser by a human.
- A real `live_trading_cycle` task running against real market prices,
  a real model decision, and an order actually filling at Alpaca.

**Before risking real capital**, follow DEPLOY.md's checklist: verify
the whole loop against Alpaca's own paper endpoint first (the default —
real network behavior, zero real-money risk), and only then set
`ALPACA_BASE_URL` to the live one.

## Strategy Backtesting — status (tests against real history, no real/paper money)

A separate feature for evaluating a strategy — or searching for a
better one — against REAL historical daily prices, before ever
spending real/paper wall-clock time waiting for live cycles to
accumulate, and before ever considering Live Trading above. Full
walkthrough and the reasoning behind it in DEPLOY.md's "Strategy
Backtesting" section.

**What's new:**
- `market_data.py` — `get_daily_history(symbol, start_date, end_date)`
  on both `AlphaVantageClient` (real, via Alpha Vantage's
  `TIME_SERIES_DAILY`) and `MockMarketDataClient` (deterministic
  synthetic bars, always tagged `mock=True`, for offline tests).
- `tasks/backtest.py` — `run_backtest()` steps day-by-day through real
  historical bars, calling the *exact same*
  `decide_trades()`/`apply_risk_limits()` pair from
  `tasks/trading_cycle.py`, completely unchanged — a backtest is "what
  this strategy would have really decided," not a separate simulation
  with its own rules. Refuses to run against any mock historical data.
  `compute_backtest_stats()` extends
  `trading_strategy_review.compute_stats()` with `profit_factor` (gross
  wins ÷ gross losses — the real tell for whether a high win rate is
  actually making money), real `max_drawdown_pct` over the whole run,
  and a `sample_size_ok` flag for when there simply aren't enough
  trades yet to trust the numbers.
- `tasks/strategy_backtest_search.py` — a BOUNDED, train/validation-
  checked search, built specifically to avoid "keep tweaking
  parameters until some target is hit" (a direct path to overfitting a
  strategy to noise in whatever window it was tuned against).
  `meets_bar()` is the actual pass/fail check, evaluated only against
  held-out VALIDATION stats, and never uses win rate alone: it requires
  a real sample size, positive net P&L, a profit_factor with real
  margin above break-even, and a bounded max drawdown.
  `run_strategy_search()` tries at most `max_candidates` strategies
  (reusing `trading_strategy_review.propose_strategy_update()`
  unchanged for each proposal) and stops the moment one clears the bar
  — "none of these were good enough" is reported plainly, never hidden
  or routed around. Never activates anything automatically.
- Schema: `backtest_runs` (both `schema.sql` and `schema_postgres.sql`)
  — every candidate tried, with its full train/validation stats, kept
  deliberately separate from `paper_trades`/`live_trades`/
  `trading_snapshots` (a backtest never writes to any of them).
- `executor.py`: `_handle_strategy_backtest_search`, registered under
  task type `strategy_backtest_search` (`permission_level_required=2`,
  recommend-only, same tier as `ops_maintenance_review`).
- New endpoints: `POST /businesses/{id}/trading/backtest` (triggers a
  bounded search over a given train/validation date range) and
  `GET /businesses/{id}/trading/backtest-runs`.
- A new dashboard panel: a date-range form and a list of past runs,
  each candidate shown with its validation P&L/profit factor/win
  rate/drawdown/trade count, a PASS/no badge, and the best candidate
  starred — never an auto-activate button, since promoting a strategy
  is always the owner's own explicit call via the existing
  strategy-override endpoint.

**What was actually verified in THIS build:**
- `python3 -m py_compile` on every new/changed `.py` file; `node -e
  "require(...)"` on the changed dashboard JS files.
- The full offline suite: 4 new `test_market_data_offline.py` cases (real-
  shaped history parsing/date-filtering, rate-limit handling, mock
  history tagging), 8 new `test_backtest_offline.py` cases (an exact
  buy-then-sell sequence across real historical days with the exact
  realized P&L asserted, refusing mock data, every
  `compute_backtest_stats` edge case including zero-loss/zero-trade),
  6 new `test_strategy_backtest_search_offline.py` cases (every
  `meets_bar` boundary, stopping early on a passing candidate, never
  exceeding `max_candidates`, ranking by real validation P&L rather
  than train performance or win rate), and 5 new DB-integration
  `test_executor_offline.py` cases (a real backtest run saved as a
  report, confirming it never touches `paper_trades`/`live_trades`/
  `trading_snapshots`, missing task input, a real historical-data fetch
  failure, no active strategy). Every pre-existing test in this repo
  still passes unchanged.
- Schema applied cleanly and verified on both SQLite and a real local
  Postgres database.

**Update, from the owner's real deployment:** the very first real call
to Alpha Vantage's historical-data endpoint (this sandbox still can't
reach it directly) surfaced a real bug this code's own comments got
wrong: `outputsize=full` — assumed free-tier-accessible when this was
written — is actually a premium-only parameter now; a free key only
ever gets `outputsize=compact` (~100 most recent trading days, roughly
4-5 calendar months). Fixed to request `compact`; see DEPLOY.md's
"Strategy Backtesting" section for the resulting date-range guidance.
Also confirmed live: `run_backtest()`'s own guard correctly failed the
task loudly with Alpha Vantage's real error text rather than silently
proceeding on partial/fabricated data — the safety property held even
though the underlying assumption about free-tier limits didn't.

**Still not verified:** an actual backtest search completing
successfully end to end against real historical prices and a real
model (the `outputsize` fix above hasn't yet been confirmed live), and
the new dashboard panel's form/results table clicked through in a real
browser by a human.

**To verify it yourself**, with `ANTHROPIC_API_KEY` and
`ALPHAVANTAGE_API_KEY` both set: open the dashboard, pick a business
with a paper trading portfolio, fill in a train start date, a
validation split date, and a validation end date in the Backtest panel
(a few months total is plenty to start), and click **Run Backtest
Search**. Within a few minutes you should see a real run appear with
one or more candidates and their real train/validation stats.

### Shared Alpha Vantage request budget (trading_cycle, live_trading_cycle, strategy_backtest_search)

Found for real, from an owner's actual usage: a backtest search
retried several times (each attempt burns real Alpha Vantage quota
even when it's rejected with a rate-limit response, not a network
failure) exhausted that day's free-tier 25-requests/day quota right
before a scheduled paper trading cycle needed it — the two features
were competing for the same real resource with neither aware of the
other's usage, so the trading cycle's own longer interval (see the
"Owner Digest" section's provisioning fix, a separate earlier issue)
didn't help.

- `market_data.py`'s `reserve_budget(db, market_client, count, purpose)`
  is called by all three handlers (`_handle_trading_cycle`,
  `_handle_live_trading_cycle`, `_handle_strategy_backtest_search`) in
  `executor.py`, right before they're about to make `count` real
  requests (one per watchlist/held symbol). It checks today's real
  usage (summed from the new `market_data_usage` table, real rows only
  — nothing here is ever estimated) against `DAILY_REQUEST_LIMIT`
  (default 25, override via `ALPHAVANTAGE_DAILY_REQUEST_LIMIT` if
  you've upgraded your Alpha Vantage plan) and refuses loudly, **before
  any real fetch happens**, if the request would exceed what's left —
  a caller that would fail partway through a batch never gets to burn
  through the remainder of the day's quota first.
- A complete no-op (no check, no record) when
  `market_client.is_mock` is `True` (both `AlphaVantageClient` and
  `MockMarketDataClient` now carry this marker) — there's no real
  quota at stake using the mock client, and this keeps every existing
  offline test that passes a mock/fake client working unmodified.

**What was actually verified in this session:**
- 6 new checks in `test_market_data_offline.py`: `used_today()` sums
  only today's (UTC) reservations across every purpose; `reserve_budget()`
  is a true no-op for a mock client even wildly over budget; it records
  a real reservation for a real client within budget; it refuses loudly
  (and records nothing) once a reservation would exceed what's left,
  with the exact remaining/used counts in the message; and it respects
  a raised `ALPHAVANTAGE_DAILY_REQUEST_LIMIT` for an upgraded plan.
- 2 new `test_executor_offline.py` checks: a `trading_cycle` task
  refuses loudly, before any real fetch, once an earlier
  `strategy_backtest_search` already spent the day's shared quota
  (and vice versa) — each confirming the refused reservation never
  partially applies.
- Live-verified directly against a real Postgres database (not just
  SQLite): reserved budget across two different purposes, confirmed a
  request that would exceed what's left is refused with the exact
  right numbers, confirmed the refusal records nothing, and confirmed
  a mock client is always a true no-op — including the cross-backend
  timestamp comparison (`market_data_usage.created_at` is
  `TIMESTAMPTZ` in Postgres vs. `TEXT` in SQLite) working correctly on
  both.

## App Development — status

The fourth business vertical: app-idea feasibility and technical
planning. Research/planning only (`permission_level` 1-2, same tier as
Opportunity Discovery and Roblox Game Development) — no code gets
written, no contracts get signed, nothing here spends real money. Chosen
over Real Estate (also named in the project spec) specifically because
it carries no jurisdiction/legal-exposure questions that would need a
dedicated scoping conversation first.

**What's new:**
- `tasks/research_app_feasibility.py` — same pattern as
  `research_opportunity.py`/`research_roblox_trend.py`: strict-JSON
  output, mandatory `confidence_level`, hard failure (never a fabricated
  fallback) on invalid or incomplete model output. `complexity_tier`
  is a second mandatory, validated enum (`simple`/`moderate`/`complex`/
  `very_complex`). The model is explicitly instructed to *flag* — never
  resolve — any concept touching a regulated domain (payments, health
  data, minors, biometric data, government IDs) as a risk requiring
  dedicated legal/compliance review.
- Schema: `app_feasibility_assessments` (both `schema.sql` and
  `schema_postgres.sql`).
- `executor.py`: `research_app_feasibility` handler, registered in
  `HANDLERS`, using the same real-cost-charged/confidence-rewarded ARC
  accounting as every other research task type.
- New endpoints: `POST /businesses/{id}/app-feasibility/research`
  (creates the task; returns immediately) and
  `GET /businesses/{id}/app-feasibility` (lists saved assessments).
  Wired into `GET /businesses/{id}/dashboard` alongside `opportunities`
  and `roblox_trends`.
- A new dashboard panel (concept + reference-URL form, result cards
  showing platform recommendation, tech stack, complexity tier,
  timeline/cost estimate, MVP scope, technical risks, and similar
  existing apps), mirroring the Opportunity Discovery/Roblox panels
  exactly.

**What was actually verified in this session:**
- `python3 -m py_compile` on every new/changed `.py` file; `node --check`
  passed implicitly via the full `node test_dashboard_render.js` run
  below.
- Full offline suite: 8 new checks in
  `test_research_app_feasibility_offline.py` (success with/without
  reference URLs, a dead reference URL shrinking evidence without
  failing the task, >3 URLs rejected, invalid JSON rejected, a missing
  required field rejected, an invalid `confidence_level` rejected, an
  invalid `complexity_tier` rejected), 1 new `test_executor_offline.py`
  case (a `research_app_feasibility` task gets picked up, executed, and
  saves a real row), and 4 new `test_dashboard_render.js` checks
  (empty state, real-shape rendering, no-references note, malformed-JSON
  safety). Every pre-existing test in the repo still passes unchanged.
- Stood up a real local Postgres database (fresh, schema applied from
  `schema_postgres.sql`), ran `uvicorn api:app` for real against it, and
  exercised the new endpoints over real HTTP: created a business and an
  agent, submitted an app-feasibility research request, confirmed the
  `>3 reference_urls` request is rejected with `400` and an unknown
  `business_id` returns `404` on both endpoints, and confirmed
  `GET /businesses/{id}/dashboard` returns the new
  `app_feasibility_assessments` field correctly (empty list, then
  reflecting the real task outcome below).
- With no `ANTHROPIC_API_KEY` set (not available in this build
  environment), the submitted task correctly **failed loudly** —
  `"model did not return valid JSON"` — and nothing was written to
  `app_feasibility_assessments`. This is the intended behavior (no
  fabricated assessments), not a bug, and it was confirmed as a real
  outcome via the live task result, the live `GET .../app-feasibility`
  endpoint (empty list), and the live dashboard JSON — not just by
  reading the code.
- Loaded `/dashboard` in a real headless browser (screenshot taken):
  the new "App Development — Feasibility Assessments" panel renders
  correctly, in the same visual style as the other research panels, and
  the failed task is visible in the Tasks table exactly as it should be.

**UPDATE — verified live in production after deploy:** the owner opened
the real Railway deployment, submitted "ai meal planning app" through
the new "Assess Feasibility" form with a real `ANTHROPIC_API_KEY` set,
and got back a complete, well-formed card (`confidence: medium`,
`complexity_tier: moderate`) with a real platform recommendation, tech
stack, timeline/cost range, MVP scope, and similar-apps list. It also
correctly flagged the regulated-domain risk on its own — health/
biometric data (weight, medical conditions, allergies) — as needing
dedicated legal/compliance review, exactly as the system prompt
requires, without attempting to resolve that itself. This closes the
only gap this vertical had: the full pipeline (task creation → executor
→ real LLM call → strict JSON validation → saved row → dashboard render)
is now confirmed working end to end against the real Anthropic API, not
just against a controlled `MockClient` in tests.

**To verify it yourself:**
```
export ANTHROPIC_API_KEY=sk-ant-...
python -m uvicorn api:app --reload
```
Open the dashboard, use the "Assess Feasibility" form with a real app
idea and optionally 1-3 real URLs, submit, and wait ~10-15 seconds — a
card should appear with a real, model-generated assessment and an
honest confidence level. Make sure at least one agent in the business is
idle (not `working` on something else) when you submit.

## Dashboard — visual redesign

The dashboard went from a plain functional page to a "futuristic
holographic command center" look (owner request), in several rounds,
each verified live via a real local Postgres + `uvicorn` + a headless
browser (screenshots + DOM/animation assertions), never just by reading
the code:

- A holographic gradient theme, radial gauge/status-bar/sparkline
  visualizations, and a "System Core" hero panel.
- A hand-rolled Canvas 2D 3D wireframe globe centerpiece
  (`static/dashboard-globe.js`) and a whole-page ambient particle-network
  background (`static/dashboard-network-bg.js`) — deliberately **not**
  WebGL/Three.js/any CDN library, consistent with this project's
  zero-external-JS-dependency convention. A hover-tilt effect on panels
  (`static/dashboard-tilt.js`).
- The System Core panel rebuilt as a hub-and-spoke radial layout:
  system-wide stats arranged around the globe, connected by animated
  SVG lines (`renderOrbitalRing()` in `dashboard-render.js`).
- **Real bugs found and fixed via live testing, not inspection:** the
  globe appeared frozen under `prefers-reduced-motion: reduce` (it was
  drawing one static frame and never starting its rotation loop — fixed
  to just rotate slower, not stop); the globe was visibly off-center
  (a flex layout centered the *group*, not the globe itself — fixed
  with a proper 3-column CSS grid); panel hover-lift effects had
  silently stopped working project-wide since the original holographic
  theme shipped (`animation-fill-mode: both` permanently pins
  `transform` after the animation ends, overriding `:hover` — fixed by
  changing to `backwards` everywhere); and the entire dashboard
  flickered on every 5-second auto-refresh because `innerHTML` was
  reassigned unconditionally even when the data hadn't changed,
  destroying and recreating every DOM node and replaying every entrance
  animation (fixed with a `setHtmlIfChanged()` helper that skips the
  write when the rendered HTML is byte-identical).
- Remove buttons (with new `DELETE` endpoints, audit-logged) on the
  Opportunity/Roblox/App-Feasibility research cards, so old research
  doesn't accumulate forever with no way to clear it.
- **Full palette/typography/shape match to a separately-designed
  "Command Core" concept (owner request).** Every panel switched from
  rounded corners + L-shaped corner brackets to an angular cut-corner
  glass bezel (`clip-path` + `backdrop-filter`, with a thin animated
  cyan/iris sheen along the top edge in place of the old shifting
  4-color holographic border gradient); every color token updated to
  Command Core's exact hex values; Orbitron (headers)/Inter (body)/
  JetBrains Mono (all numeric/data readouts) loaded from Google Fonts;
  a uniform click-ripple added to every button on the page via one
  delegated listener in `dashboard.js`, rather than wiring it into each
  handler individually. **The System Core globe is now real Three.js**
  (`static/dashboard-globe.js`), not the hand-rolled Canvas 2D globe
  described two bullets up — a deliberate, explicit exception to this
  project's usual zero-external-JS-dependency convention, made only
  for this one visual centerpiece, loaded from a CDN `<script>` tag in
  `dashboard.html`. Every other dashboard script (network background,
  tilt, the render layer, `dashboard.js` itself) stays dependency-free.

## Storefront — real payments (three of the four verticals)

The system's first path to real-world revenue: `static/store.html` (a
public, unauthenticated page, deliberately separate in look from the
owner's internal `/dashboard`) lets a customer pay **$9** via a real
Stripe Checkout Session for one of three products — a research report
from Opportunity Discovery, Roblox Game Development, or App Development
Feasibility — and get it emailed to them, usually within a minute.

- An order is only ever marked `paid`, and only a real
  `real_transactions` row is only ever written, in direct response to a
  Stripe webhook (`/store/webhook`) that `stripe_client.py` has
  cryptographically signature-verified — nothing here ever trusts a
  client redirect alone. Idempotent via a unique constraint on the
  Stripe event id, so a Stripe webhook retry can never double-fulfill
  an order.
- `fulfillment.py` — a background thread (same pattern as the scheduler/
  executor) polls for paid orders whose linked task has finished, and
  either emails the customer their real report (task completed — the
  email is built from the actual saved assessment row, never
  re-derived, and every customer-submitted/model-generated field is
  HTML-escaped before going into the email) or marks the order `failed`
  (task failed). Two failure modes are retried for a bounded number of
  passes and then also marked `failed`, rather than being retried and
  re-logged identically forever with no terminal state: a completed
  task whose expected result row never actually shows up (a data bug,
  not a normal outcome) — `FULFILLMENT_BUILD_FAILURE_RETRY_LIMIT`,
  default 5 — and a report email that never sends (Resend outage, or
  `RESEND_API_KEY`/`RESEND_FROM_EMAIL` never configured) —
  `FULFILLMENT_EMAIL_FAILURE_RETRY_LIMIT`, default 20.
- **Refunds are never automated, on purpose** — per the project spec's
  bias toward human judgment on consequential/irreversible actions, a
  `failed` order (already charged in Stripe) needs a manual refund via
  the Stripe dashboard. Since that step can't be automated, an optional
  `OWNER_EMAIL` env var gets a real-time alert email the moment an
  order needs one, and the dashboard's new **Store Orders** panel
  (`GET /businesses/{id}/dashboard`'s `orders` field) shows the
  selected business's most recent orders and their status at a glance,
  so this is never something the owner has to go find in Stripe or the
  database to notice.
- All 6 required env vars (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `STORE_BUSINESS_ID`, `PUBLIC_BASE_URL`, `RESEND_API_KEY`,
  `RESEND_FROM_EMAIL`) plus the optional ones (`OWNER_EMAIL`,
  per-product price overrides) are documented in `DEPLOY.md`'s
  Storefront section — they previously weren't documented anywhere,
  meaning the store was very likely live-but-silently-broken until that
  gap was found and closed.
- **Confirmed live by the owner** with real purchases, real Stripe
  payments, and real emailed reports for all three products (Opportunity
  Discovery and Roblox Game Development originally; App Development
  Feasibility added later and verified the same way).
- The storefront also got basic customer-facing/traffic infrastructure:
  the bare deployed URL (`GET /`) now serves the store directly instead
  of 404ing, `<meta description>`/Open Graph/Twitter Card tags so a
  shared link actually shows a title and description, a favicon
  (`static/favicon.svg`), and a `robots.txt` pointing crawlers at the
  store and away from the internal dashboard/API.

## Security hardening

Two gaps found and closed after real money started flowing through the
storefront:

- **`/store/checkout` rate limiting** — the one public, unauthenticated
  endpoint that does real work on every call (creates a real Stripe
  Checkout Session, writes an order row) had no protection against
  being spammed. Now capped per client IP (`CHECKOUT_RATE_LIMIT_MAX` /
  `CHECKOUT_RATE_LIMIT_WINDOW_SECONDS`, default 10 per 60s).
- **Dashboard login brute-force protection** — the entire internal
  dashboard/API (every business's data, the ARC ledger, agent controls)
  sits behind one static HTTP Basic Auth password
  (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`, previously undocumented
  anywhere — now covered in `DEPLOY.md`) with no other protection.
  Failed login attempts are now rate-limited per client IP
  (`DASHBOARD_LOGIN_RATE_LIMIT_MAX` /
  `DASHBOARD_LOGIN_RATE_LIMIT_WINDOW_SECONDS`, default 10 per 5
  minutes) — live-verified that legitimate, repeated *correct*-password
  traffic (the dashboard's own polling) is never affected, only
  repeated *wrong* passwords trip it.
- Both built on a small stdlib-only in-memory sliding-window
  `rate_limiter.py` — no new dependency, since this runs as a single
  `uvicorn` process with no distributed state to coordinate.

**Static asset caching** — found for real when a storefront redesign
didn't visually show up for a returning visitor until they
hard-refreshed: Starlette's plain `StaticFiles` sets no `Cache-Control`
at all, so a browser's own (much longer, inconsistent across browsers)
heuristic caching was the only thing controlling how long it held onto
e.g. `store.css`. `api.py`'s `CachedStaticFiles` (wraps the `/static`
mount) now sets `Cache-Control: public, max-age=300` on every static
asset, so a future deploy that changes a CSS/JS/image file reaches an
already-visited browser's next request within 5 minutes, without
forcing a re-fetch on every single page load either.

## Ops/Maintenance — status (fifth business vertical, no customer)

The project spec's sixth planned vertical, scoped after clarifying with
the owner what it was actually meant to do: not a customer-facing
product, but an agent that watches THIS system's own infrastructure and
produces maintenance recommendations for the owner. Same safety posture
as every other vertical: it only ever recommends — it never restarts
anything, deletes stale data, or changes configuration on its own.

**What's new:**
- `tasks/ops_maintenance_review.py` — `collect_system_metrics()`
  computes REAL numbers from the database (stuck tasks/approvals/
  orders past a configurable age threshold, scheduled jobs overdue past
  their own next_run_at by more than their own grace multiplier,
  row counts for the tables most worth watching for growth, a tally of
  recent `*_failed`/`*_error` audit log entries, and which known
  optional env vars are unset) — never asked of the model, never
  invented by it. `analyze_system_health()` sends those real numbers to
  the model and asks it to synthesize a prioritized, severity-ranked
  report (`ok`/`info`/`warning`/`critical`) from them — same
  hard-failure-never-fabricate posture as every other research task
  type: invalid or incomplete JSON is a hard failure.
- Schema: `ops_maintenance_reports` (both `schema.sql` and
  `schema_postgres.sql`), storing the model's synthesis alongside the
  real `metrics_snapshot` it was given, so a report can always be
  checked against the raw numbers behind it.
- `executor.py`'s `ops_maintenance_review` handler saves the report and,
  for a `warning`/`critical` severity, sends the same kind of
  best-effort `OWNER_EMAIL` alert as a failed storefront order — a
  broken/missing alert never affects the saved report or fails the task.
- **No owner setup step**, unlike every other vertical: a "System
  Operations" business, an Ops Monitor agent (permission_level 2), and
  a recurring `ops_maintenance_review` scheduled job (default every 24h,
  `OPS_REVIEW_INTERVAL_SECONDS`) are created automatically the first
  time the app starts — idempotent, so every later restart/redeploy is
  a no-op. Watching the system's own health needs nothing beyond the
  `ANTHROPIC_API_KEY` every other vertical already requires.
- New endpoints: `GET /ops/reports` (recent reports) and
  `POST /ops/review` (trigger one now). Wired into `GET /overview` as
  `latest_ops_report` — system-wide, not tied to whichever business the
  owner has selected, same as the approvals rollup above it.
- A new dashboard panel ("System Health — Ops/Maintenance"), with a
  severity badge, the model's summary, each finding with its own
  severity/description/recommendation, and a "Run Ops Review Now"
  button — placed next to the Owner Approvals panel since both are
  global, business-independent panels.

**What was actually verified in this session:**
- 15 new checks in `test_ops_maintenance_review_offline.py`:
  `collect_system_metrics()` against a real SQLite database with
  hand-seeded fixtures (a genuinely stuck task is flagged, a fresh one
  isn't; a stale approval; a scheduled job overdue past its own grace
  threshold, but not a disabled one; a stuck order; recent errors
  counted only within the window; a configured optional var not
  flagged, an unset one is; real table row counts), plus
  `analyze_system_health()`'s full validation surface (invalid JSON,
  missing fields, invalid severity/confidence values, findings that
  isn't a list, a malformed finding, and that an empty findings list is
  valid, not an error).
- 3 new `test_executor_offline.py` checks: an `ops_maintenance_review`
  task gets executed and saves a real report row; a `warning`-severity
  report alerts the configured `OWNER_EMAIL`; a broken owner-alert email
  never fails the review task itself.
- 5 new `test_dashboard_render.js` checks: the no-report-yet empty
  state, real-shape rendering with findings, the "everything healthy"
  empty-findings case, malformed findings JSON never throwing, and
  XSS-escaping of model-generated finding text.
- Live-verified against a real local Postgres + `uvicorn`: confirmed
  the "System Operations" business/Ops Monitor agent/scheduled job are
  created automatically on first startup, confirmed a restart doesn't
  create duplicates (idempotency), confirmed the scheduled job fires
  immediately (its `next_run_at` is set to "now" at creation) and the
  executor picks it up, and — with no real `ANTHROPIC_API_KEY` in this
  sandbox (same as every other vertical under identical conditions) —
  confirmed the task **correctly fails loudly** on the mock client's
  non-JSON response rather than fabricating a report. Also verified
  `POST /ops/review`'s manual trigger, and loaded `/dashboard` in a
  real browser (screenshot taken, zero console errors) in both the
  empty state and — after seeding one real report row directly — the
  populated state with a warning-severity finding.

**What was NOT verified** (needs a real `ANTHROPIC_API_KEY`, same as
every other LLM-driven task type in this project):
- An actual `ops_maintenance_review` task running against the real
  Anthropic API and producing a real, model-synthesized report.

**To verify it yourself:** nothing to configure — open `/dashboard`
with a real `ANTHROPIC_API_KEY` set, and within `OPS_REVIEW_INTERVAL_SECONDS`
of the app's first startup (or immediately, by clicking "Run Ops Review
Now") a real report should appear in the System Health panel.

## Real Estate — status (sixth business vertical, investment research only)

The project spec's last remaining vertical, scoped in a dedicated
conversation with the owner before any code was written: unlike a
literal brokerage/listing product (which would need real broker/agent
licensing and a legal review before any of this could be built),
this is investment RESEARCH only — same permission tier (1-2) and same
safety posture as Opportunity Discovery/Roblox Game Development/App
Development Feasibility. No listing, no offer, no contract, nothing
resembling acting as a real estate agent or broker; never a licensed
appraisal.

**What's new:**
- `tasks/research_real_estate.py` — same pattern as the other three
  research verticals: strict-JSON output, mandatory `confidence_level`,
  hard failure (never a fabricated fallback) on invalid or incomplete
  model output. Because real estate is uniquely jurisdiction-sensitive
  (zoning, disclosure law, rent control, broker/appraiser licensing all
  vary by state/country) and a $9 report here could plausibly
  influence a much larger financial decision than any other vertical's
  report, the model is explicitly instructed to flag — never resolve —
  anything jurisdiction-specific as needing a licensed real estate
  agent, appraiser, or attorney, and to never state a specific dollar
  valuation as a guaranteed figure.
- Schema: `real_estate_assessments` (both `schema.sql` and
  `schema_postgres.sql`).
- `executor.py`: `research_real_estate` handler, registered in
  `HANDLERS`, using the same real-cost-charged/confidence-rewarded ARC
  accounting as every other research task type.
- New endpoints: `POST /businesses/{id}/real-estate/research` (creates
  the task; returns immediately), `GET /businesses/{id}/real-estate`
  (lists saved assessments), and `DELETE .../real-estate/{id}`. Wired
  into `GET /businesses/{id}/dashboard` alongside the other verticals'
  assessment lists.
- A new dashboard panel (property/market + reference-URL form, result
  cards showing market trend, comparable properties, estimated rental
  yield, price trend assessment, and risk factors, with a Remove
  button), mirroring the other research panels exactly.
- **Storefront**: a fourth product, "Real Estate Investment Research
  Report" ($9, same as the other three — `STORE_PRICE_REAL_ESTATE_CENTS`
  to override), added to `PRODUCT_CATALOG`, `fulfillment.py`'s report-email builder, and
  `store-legal.html`'s disclaimer (a dedicated paragraph stating this
  is not an appraisal and not the advice of a licensed professional).
- **A real, separate bug found and fixed while wiring this up**: the
  Stripe webhook's task-creation code (`/store/webhook` in `api.py`)
  used to pick the task's input field with a binary check — `"topic"`
  for Opportunity Discovery, `"concept"` for everything else. Real
  Estate needed a third key, `"property_or_market"`, so that binary
  check would have silently created every real estate order's task
  with the wrong input key, causing `research_real_estate`'s handler to
  correctly reject it as missing a required field — a real customer
  charged real money for a report that could never be produced,
  discovered here before any real order could hit it. Fixed with an
  explicit per-product_type mapping instead of a binary guess; the
  fix is purely additive for the three existing products (verified by
  reading through the old vs. new logic side by side — same result for
  `research_opportunity`/`research_roblox_trend`/`research_app_feasibility`,
  a new correct branch for `research_real_estate`).

**What was actually verified in this session:**
- 7 new checks in `test_research_real_estate_offline.py` (success
  with/without reference URLs, a dead reference URL shrinking evidence
  without failing the task, >3 URLs rejected, invalid JSON rejected, a
  missing required field rejected, an invalid `confidence_level`
  rejected), 1 new `test_executor_offline.py` case (a
  `research_real_estate` task gets picked up, executed, and saves a
  real row), 2 new `test_fulfillment_offline.py` cases (a completed
  order gets emailed and marked fulfilled; a missing assessment row
  fails loudly rather than fabricating an email), and 5 new
  `test_dashboard_render.js` checks (empty state, real-shape rendering,
  no-references note, malformed-JSON safety, XSS-escaping). Every
  pre-existing test in the repo still passes unchanged.
- Stood up a real local Postgres database (fresh, schema applied from
  `schema_postgres.sql`), ran `uvicorn api:app` for real against it,
  and exercised the new endpoints over real HTTP: created a business
  and an agent, submitted a real-estate research request (auto-assigned
  and executed by the real background threads, not simulated), listed
  and deleted a directly-seeded assessment row over real HTTP, and
  confirmed `GET /businesses/{id}/dashboard` returns the new
  `real_estate_assessments` field correctly. Confirmed the new product
  appears correctly in `GET /store/products`.
- With no `ANTHROPIC_API_KEY` set (not available in this build
  environment), the submitted task correctly **failed loudly** —
  `"model did not return valid JSON"` — and nothing was written to
  `real_estate_assessments`. This is the intended behavior (no
  fabricated assessments), not a bug, confirmed via the live task
  result, not just by reading the code.
- Loaded `/dashboard` in a real headless browser (screenshot taken):
  the new "Real Estate — Investment Research" panel renders correctly,
  in the same visual style as the other research panels. Also
  interactively clicked the Remove button in the real browser and
  confirmed the card actually disappears and the underlying row is
  actually deleted, not just visually hidden.

**What was NOT verified** (needs a real `ANTHROPIC_API_KEY` and, for
the storefront path specifically, the owner's own real Stripe test
purchase — same as App Feasibility before the owner personally
verified it):
- An actual `research_real_estate` task running against the real
  Anthropic API and producing a real assessment.
- A real Stripe test-mode (or live) purchase of the Real Estate product
  through the actual storefront checkout → webhook → fulfillment →
  email path. The webhook `task_input`-mapping bug above was caught and
  fixed by reading the code, not by an actual test purchase — a real
  purchase is the one verification step only the owner can perform
  (same limitation as every other storefront product originally).

**To verify it yourself:**
```
export ANTHROPIC_API_KEY=sk-ant-...
python -m uvicorn api:app --reload
```
Open the dashboard, use the "Research This Property/Market" form with
a real address or market and optionally 1-3 real URLs, submit, and
wait ~10-15 seconds — a card should appear with a real, model-generated
assessment and an honest confidence level. Make sure at least one agent
in the business is idle when you submit. To verify the storefront path,
buy the "Real Estate Investment Research Report" through
`/static/store.html` with a real (or Stripe test-mode) card and confirm
the emailed report arrives with real content.

## Owner Digest — status ("personal assistant" feature, no customer)

A daily email summarizing what needs the owner's attention across
every business, so a decision doesn't sit unnoticed just because
nobody opened the dashboard that day. Deliberately code-only, no model
call: every line in the email is either a real query result or a
direct restatement of the latest ops review's already-synthesized
report — composing a digest is not a judgment call a model needs to
make, so this task type always has `cost_arc=0.0`.

**What's new:**
- `tasks/owner_digest.py` — `collect_owner_digest()` computes REAL
  numbers from the database: pending approvals (with business name,
  description, risk level, age), the latest ops-maintenance report's
  severity/summary, real revenue collected since the last digest, new
  research completed per vertical since the last digest, and each
  paper trading portfolio's P&L from its latest recorded snapshot
  (never a live quote fetch — reuses `trading_snapshots`, which every
  `trading_cycle` already records, so composing a digest never spends
  a market-data provider's request quota). `find_window_start()`
  anchors each digest to the most recently *completed* digest task, so
  "since your last digest" is exact, not a guess — falling back to a
  24h lookback only for the very first digest ever sent.
  `format_digest_email()` is pure string formatting (HTML-escaped),
  no I/O.
- `executor.py`'s `owner_digest` handler sends the email via the same
  `emailer.py`/`OWNER_EMAIL` this codebase already uses for a failed
  storefront order or a warning/critical ops report — but unlike those
  best-effort alerts, sending IS this task's entire purpose, so a
  missing `OWNER_EMAIL` or a real Resend send failure **fails the task
  loudly**, never silently "completes" having sent nothing.
- **No owner setup step**, same as Ops/Maintenance: a recurring
  `owner_digest` scheduled job (default every 24h,
  `OWNER_DIGEST_INTERVAL_SECONDS`) is created automatically under the
  same internal "System Operations" business the first time the app
  starts.
- Fixed a real gap this surfaced: `ensure_ops_business_provisioned()`
  used to return immediately once the System Operations business
  already existed — meaning a job type added later (like this one)
  would never get scheduled on an already-running deployment, only a
  brand-new install would ever see it. Job provisioning is now
  idempotent **per job type** (`_ensure_scheduled_job()`), independent
  of whether the business itself is new, so this correctly backfills
  onto a deployment that was already running before this feature
  existed.
- `POST /owner-digest/send` + a "Send Owner Digest Now" button
  (System Health panel, next to "Run Ops Review Now") — same
  on-demand pattern as `trigger_ops_review()`. Added after a real
  deployment's digest failed on a Resend "domain not verified" error:
  once the owner fixed their Resend/DNS setup, there was no way to
  confirm it actually worked without waiting up to
  `OWNER_DIGEST_INTERVAL_SECONDS` for the next scheduled run.

**What was actually verified in this session:**
- 12 new checks in `test_owner_digest_offline.py`: an empty database
  produces honest zeros (never fabricated); pending approvals carry
  their real business name/description/age and exclude decided ones;
  the digest surfaces the *latest* ops report, not an older one;
  revenue and new-research counts are scoped correctly to the window;
  trading P&L uses the latest snapshot (a portfolio with no cycle yet
  is skipped, not fabricated); `find_window_start()` anchors to the
  latest *completed* digest, ignoring a more recent failed one; the
  email subject reflects the real approval count; HTML-escaping of
  business names/descriptions.
- 3 new `test_executor_offline.py` checks: a real digest email sends
  and the task completes with zero cost; a missing `OWNER_EMAIL` fails
  the task loudly; a real `EmailError` from `send_email` fails the
  task (unlike the ops review's best-effort alert).
- 3 new `test_ops_business_provisioning_offline.py` checks calling
  `ensure_ops_business_provisioned()` directly: the first call creates
  the business/agent/both jobs; a second call (a restart) is a
  complete no-op; and — the actual regression this was built to catch
  — a database seeded to look like a deployment from *before* this
  feature existed gets the `owner_digest` job backfilled without
  touching its existing business/agent/ops-review job.
- 3 new `test_manual_triggers_offline.py` checks calling
  `trigger_ops_review()`/`trigger_owner_digest()` directly: each
  creates a real task of the right `task_type` under the System
  Operations business, and both reuse the same business rather than
  creating a duplicate.
- Full offline suite (`test_*.py` + `test_dashboard_render.js`) still
  passes.
- Live-verified against a real Postgres DB + running server + a real
  browser via Playwright: clicked "Send Owner Digest Now", confirmed a
  real `owner_digest` task was created and ran through the real
  executor pipeline (failing with the expected `OWNER_EMAIL not
  configured` message, since this sandbox has no real email
  credentials), with zero console errors.

**What was NOT verified** (needs a real `OWNER_EMAIL` + `RESEND_API_KEY`,
same limitation as every other real-email-sending path in this project):
- An actual digest email landing in a real inbox with real content.

**To verify it yourself:** set `OWNER_EMAIL`/`RESEND_API_KEY`/
`RESEND_FROM_EMAIL`, then click "Send Owner Digest Now" on the
dashboard's System Health panel (or wait for
`OWNER_DIGEST_INTERVAL_SECONDS` after startup) — a real email should
arrive summarizing pending approvals, system health, revenue, new research,
and trading P&L across every business.

## Owner Chat — status (the conversational layer, "personal assistant" feature)

Free-text Q&A: the owner types a question into a new "Ask" panel on
the dashboard and gets a real answer, without navigating any other
panel. Deliberately v1-scoped to READ-ONLY questions — see
`owner_chat.py`'s module docstring for the full reasoning, but in
short: turning "launch the pet grooming idea" into a real action needs
its own resolve-the-exact-record-then-confirm safety design, which is
a deliberate, separate, later decision, not something to fold into a
first version whose whole point is proving the read-only plumbing
works safely.

**What's new:**
- `owner_chat.py` — `classify_intent()` is the ONLY LLM call in this
  feature, and it never sees real business data and never composes
  the actual answer: it just classifies the owner's message into one
  of a fixed set of intents (`overview`, `pending_approvals`,
  `ops_health`, `trading_status`, `research_search`, `unclear`) via
  strict JSON — same hard-failure-never-guess discipline as every
  other classifier in this codebase. Its system prompt explicitly
  tells the model it has NO ability to act; an action-shaped request
  (e.g. "launch X", "approve Y", "delete Z") is routed to `unclear`
  along with anything off-topic or ambiguous. Once classified, CODE
  (never the model) runs the real query and composes the
  natural-language answer from the real result — reusing
  `tasks/owner_digest.py`'s `collect_owner_digest()` for the
  approvals/ops-health/trading-P&L pieces, plus a new
  `search_research()` that does a real (never fuzzy/semantic) SQL
  `LIKE` search across all four research verticals' title/summary
  fields, so a match is always something the owner's own words
  actually appear in.
- A classification failure (invalid JSON, or the model inventing an
  intent outside the fixed set) degrades to the same friendly "I can't
  do that yet, here's what I can help with" reply a genuinely unclear
  message gets — a chat turn never surfaces a raw error the way a
  failed scheduled task legitimately can.
- `POST /chat` — synchronous, not a scheduled/queued task: a chat turn
  should feel like asking a question, not submitting a form and
  refreshing later. Never touches the agent/task/ARC economy, same
  "synchronous, read-only, no task involved" shape as `/overview`.
- A new "Ask" panel on the dashboard (placed first, above Owner
  Approvals) — a scrollable log of the conversation plus a text input,
  business-independent since answers span every business.

**What was actually verified in this session:**
- 17 new checks in `test_owner_chat_offline.py`: `classify_intent()`
  accepts every valid intent and raises on invalid JSON or a
  model-invented intent outside the fixed set; `collect_chat_overview()`
  and `search_research()` against a real SQLite database (matches by
  title OR summary across every vertical, correctly reports launched
  status, returns nothing for an empty query); every `format_*()`
  function's empty/populated cases; and `answer_message()`'s full
  orchestration, including both ways a message degrades to the safe
  "I can't do that yet" reply (a classification failure, and an
  action-shaped request the model itself routes to `unclear`).
- 2 new `test_chat_endpoint_offline.py` checks calling `api.chat()`
  directly: a real end-to-end answer computed from the real (empty)
  database, and a blank message rejected with 400 before ever calling
  the model.
- Full offline suite (`test_*.py` + `test_dashboard_render.js`) still
  passes.
- Live-verified against a real Postgres DB + running server + a real
  browser via Playwright: the "Ask" panel renders correctly, a
  question and an action-shaped request ("launch the pet grooming
  idea") both round-trip through the real `/chat` endpoint and render
  in the log with zero console errors, the input clears and
  re-enables after each answer, and — with no real `ANTHROPIC_API_KEY`
  in this sandbox (same limitation as every other LLM-driven feature
  here) — confirmed the graceful "I can't do that yet" degradation
  path end-to-end rather than a raw error.

**What was NOT verified** (needs a real `ANTHROPIC_API_KEY`, same as
every other LLM-driven task type in this project):
- An actual question correctly classified and answered against real
  business data via the real Anthropic API.

**To verify it yourself:** open `/dashboard` with a real
`ANTHROPIC_API_KEY` set, and ask the "Ask" panel something like "what
needs my approval?" or "how's my trading doing?" — a real,
data-grounded answer should appear within a few seconds. Try an
action-shaped message too (e.g. "launch the pet grooming idea") and
confirm it gets a clear "I can't do that yet" reply rather than
anything actually happening.

## Next real steps, in order
1. ~~Wire one real LLM call~~ — done, verified live.
2. ~~Stand up Postgres + a thin REST API~~ — done, verified live against
   real Postgres.
3. ~~Build the dashboard~~ — verified live, including finding and fixing
   several real bugs through actual use.
4. ~~Add a scheduler~~ — verified live, including firing over real wall-clock time.
5. ~~Start the first business vertical (Opportunity Discovery)~~ —
   verified live end to end, including a real retry-logic bug found
   and fixed via actual use.
6. ~~24/7 hosting~~ — deployed to Railway with a managed Postgres add-on;
   confirmed live (`/health`, `/dashboard`, real Postgres persistence).
7. ~~Roblox Game Development vertical~~ — built, and confirmed live by
   the owner as one of the storefront's original two products.
8. ~~Automated Stock Trading vertical (paper trading only)~~ — built and
   verified against a real local Postgres + real HTTP; still needs a
   real Anthropic + Alpha Vantage key to see a real decision/trade end
   to end (see above).
9. ~~App Development Feasibility vertical~~ — built, verified against a
   real local Postgres + real HTTP, and confirmed end to end in
   production with a real Anthropic key producing a real assessment
   (see above).
10. ~~Dashboard visual redesign~~ (holographic theme, 3D globe, radial
    layout) — done, verified live via headless browser at every step
    (see above).
11. ~~Storefront — real Stripe payments for three of the four
    verticals~~ — done, confirmed live by the owner with real purchases
    for all three products (see above).
12. ~~Store Orders dashboard panel + owner failed-order email alerts +
    bounded fulfillment retries~~ — done, live-verified (see above).
13. ~~Security hardening — checkout + dashboard-login rate limiting~~ —
    done, live-verified (see above).
14. ~~Storefront traffic/discoverability basics~~ (root URL, meta tags,
    favicon, robots.txt) — done, live-verified (see above).
15. ~~Ops/Maintenance vertical~~ (scoped with the owner as
    self-maintenance for this system, not a customer product) — built,
    live-verified, no owner setup step (see above).
16. ~~Real Estate vertical~~ (scoped with the owner as investment
    research only, never a brokered transaction or appraisal, after a
    dedicated conversation about its jurisdiction/legal-exposure
    questions) — built, verified against a real local Postgres + real
    HTTP + a real browser; a genuine storefront webhook bug for this
    product was found and fixed before any real order could hit it
    (see above). Still needs a real Anthropic key to see a real
    assessment end to end, and the owner's own real (or Stripe
    test-mode) purchase to confirm the storefront path — same as every
    other vertical's first real verification.

All six of the project spec's planned business verticals are now
built. What's left is verification the owner has to do personally
(real API keys, real purchases — see each vertical's "What was NOT
verified" above) and whatever new direction the owner wants to take
next.

## ACTION REQUIRED FROM OWNER
- **Already done:** deployed on Railway with a managed Postgres add-on,
  confirmed live. `DEPLOY.md` still has the full steps/alternatives
  (Render/Fly) if you ever need to redeploy elsewhere or stand up a
  second environment.
- **For the Automated Stock Trading vertical:** set `ALPHAVANTAGE_API_KEY`
  as a Railway variable (free key at alphavantage.co) alongside the
  existing `ANTHROPIC_API_KEY`. Without it, `trading_cycle` tasks fail
  loudly with a clear error instead of trading on fabricated prices —
  this is intentional, not a bug, but it means the feature does nothing
  visible until the key is set.
  **Free-tier quota:** Alpha Vantage's free key allows 25 requests/day.
  Each trading cycle uses one request per watchlist/held symbol, so
  watchlist size x cycles/day must stay under 25 (the default —
  6-hour cycles, a 5-symbol watchlist — uses 20/day, leaving headroom
  for a manual trigger or backtest search the same day). Exceeding the
  quota surfaces as `TradingCycleError: missing live quotes for
  currently-held symbols [...]` with Alpha Vantage's own rate-limit text
  in the message. Fix an already-running job's cadence without
  disabling/recreating it via the "Edit Interval" button on its row in
  the Scheduled Jobs panel (or `POST /scheduled-jobs/{job_id}/set-interval`).
- **For Strategy Backtesting:** nothing beyond the `ALPHAVANTAGE_API_KEY`
  you already set above — it reuses the same key for historical data.
  Recommended before ever touching Live Trading below: see DEPLOY.md's
  "Strategy Backtesting" section and try it against a few months of
  real history first.
- **For Live Trading (REAL MONEY, optional, off by default):** nothing
  to do unless you want this — paper trading above needs none of it.
  See `DEPLOY.md`'s "Live Trading — REAL MONEY" section for the full
  checklist: `ALPACA_API_KEY`/`ALPACA_API_SECRET`/`ALPACA_BASE_URL`
  (the live endpoint is never the default — see that section), then an
  explicit per-business opt-in from the dashboard's Live Trading panel.
  `LIVE_TRADING_KILL_SWITCH` halts it instantly if you ever need to.
- **For dashboard access:** set `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`
  as Railway variables — without both set, every internal route
  (including `/dashboard` itself) fails closed with a 503, on purpose
  (never silently open). Pick a real random password, not something
  guessable — see `DEPLOY.md` and "Security hardening" above.
- **For the storefront (real payments):** set all 6 required env vars
  (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STORE_BUSINESS_ID`,
  `PUBLIC_BASE_URL`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL`) — see
  `DEPLOY.md`'s Storefront section for the full checklist. Already done
  and confirmed working if you've made a real purchase; also consider
  setting the optional `OWNER_EMAIL` so a failed order (needs a manual
  Stripe refund), a warning/critical Ops/Maintenance finding, and the
  daily Owner Digest all reach you by email, not just the dashboard.
  Without `OWNER_EMAIL` set, the Owner Digest task fails loudly (there's
  no address to send it to) every time it's due — harmless, but visible
  as a repeatedly-failed task until it's set.
- **Nothing needed for Ops/Maintenance or the Owner Digest** — both
  provision themselves and start running automatically on first deploy.
- **For the Real Estate product specifically:** once you've made a real
  (or Stripe test-mode) purchase of it through the storefront and
  confirmed the emailed report arrives with real content, it's fully
  verified end to end, same as the other three products already are.
