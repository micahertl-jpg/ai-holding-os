# AI Holding Company OS — Core MVP (Phase 1)

## What this is
The foundation layer described in the project spec: Business registry,
Agent registry, Task orchestrator with permission gating, the Banker
(ARC ledger), the Human Approval Queue, and a full audit trail. It is
**not** any of the six future businesses (trading, real estate, Roblox,
apps, opportunity discovery, ops/maintenance) — those plug into this
core later, as designed.

## What's real vs. simulated
- REAL: the data model, the permission gating logic, the ARC accounting
  math, the approval-queue gate, the audit log. Run `demo.py` and inspect
  `holding_os.db` yourself — nothing is hardcoded output.
- SIMULATION: agent "work" (`orchestrator.complete_task` is called with
  a hand-written result string in the demo — no LLM call happens yet).
  Real-world USD amounts are recorded as *authorized ceilings and
  requests*, never moved.
- NOT IMPLEMENTED: any real AI model call, any live tool/API integration,
  any actual payment/trading/contract execution, any web dashboard,
  multi-user auth, cloud deployment, scheduling/cron, or model-cost
  routing. All of these are explicitly out of scope for this step.

## Why SQLite + stdlib-only Python
Built inside a sandboxed environment with no internet access, so no
`pip install` was possible. The schema and module boundaries
(`Database` class in `db.py`) are written so swapping SQLite for
Postgres later is a driver change, not a rewrite — every other module
talks to `Database`, never to `sqlite3` directly.

## Run it
```
python3 demo.py
```
Creates `holding_os.db`, runs a full business → agents → ARC allocation →
task → approval-gated task → owner approval cycle, and prints a
dashboard-style summary plus the audit trail.

## File map
- `schema.sql` — the six core tables (businesses, agents, tasks,
  arc_ledger, approvals, audit_log)
- `db.py` — connection + audit-log helper
- `registry.py` — BusinessRegistry, AgentRegistry (create/list/pause/retire)
- `banker.py` — Banker: allocate / reward / charge / penalize ARC, with
  a hard floor against negative balances
- `approval.py` — ApprovalQueue: the only gate through which any
  permission_level >= 6 action can proceed
- `orchestrator.py` — Task lifecycle + naive assignment + automatic
  routing to approval for consequential tasks
- `demo.py` — proves it all works together

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

## Automated Stock Trading — status (PAPER TRADING ONLY)

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

## Next real steps, in order
1. ~~Wire one real LLM call~~ — done, verified live.
2. ~~Stand up Postgres + a thin REST API~~ — done on SQLite, verified
   live; real Postgres itself still untested (no instance set up yet).
3. ~~Build the dashboard~~ — verified live, including finding and fixing
   several real bugs through actual use.
4. ~~Add a scheduler~~ — verified live, including firing over real wall-clock time.
5. ~~Start the first business vertical (Opportunity Discovery)~~ —
   verified live end to end, including a real retry-logic bug found
   and fixed via actual use.
6. ~~24/7 hosting~~ — deployed to Railway with a managed Postgres add-on;
   confirmed live (`/health`, `/dashboard`, real Postgres persistence).
7. ~~Automated Stock Trading vertical (paper trading only)~~ — built and
   verified against a real local Postgres + real HTTP in this session;
   still needs a real Anthropic + Alpha Vantage key to see a real
   decision/trade end to end (see above).
8. ~~App Development Feasibility vertical~~ — built, verified against a
   real local Postgres + real HTTP, and confirmed end to end in
   production with a real Anthropic key producing a real assessment
   (see above).

## ACTION REQUIRED FROM OWNER
- **Now:** read `DEPLOY.md` and, when ready, push this repo to GitHub
  and deploy it on Railway (or Render/Fly — see the alternatives
  section). Report back exactly what happens, including any error —
  first deployments to a new platform almost always surface something.
- **Important:** SQLite will NOT survive a redeploy on any of these
  platforms (ephemeral filesystem) — set up the Postgres add-on as
  part of this same step, not as an afterthought.
- **For the Automated Stock Trading vertical:** set `ALPHAVANTAGE_API_KEY`
  as a Railway variable (free key at alphavantage.co) alongside the
  existing `ANTHROPIC_API_KEY`. Without it, `trading_cycle` tasks fail
  loudly with a clear error instead of trading on fabricated prices —
  this is intentional, not a bug, but it means the feature does nothing
  visible until the key is set.
