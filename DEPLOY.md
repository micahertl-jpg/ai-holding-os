# Deploying for 24/7 operation

## Status: NOT YET DEPLOYED, NOT YET TESTED
Everything below is real, working configuration — but it has never
actually been deployed or run on Railway/Render/Fly.io. This sandbox
has no Docker and no access to any of these platforms. Treat this as a
carefully-written starting point, not a proven path — the first real
deployment attempt is likely to surface at least one thing that needs
fixing, the same way every other "first live run" has in this project
so far.

## Why Railway (recommended)
- Deploys straight from a git push, auto-detects this is a Python app
  (or uses the Dockerfile if present — it does here)
- Runs a normal long-running container, so the scheduler and executor
  background threads work exactly as they do on your machine
- Offers a one-click managed Postgres add-on on the same bill, which
  also resolves the still-open "set up real Postgres" item
- Predictable ~$5/mo floor (Hobby plan) once you're past free-trial credits

## CRITICAL: SQLite will NOT survive here
Railway (and Render, and Fly.io) run your app in a container with an
**ephemeral filesystem** — every redeploy wipes local files, including
`holding_os.db`. Deploying with SQLite as-is means losing all your
businesses/agents/tasks/opportunities on the next deploy. This is not
optional to fix — set up real Postgres as part of this same step, not
as a separate later task.

## Deploy steps (Railway)
1. Push this code to a GitHub repo (Railway deploys from a repo, not a
   zip upload).
2. At railway.app, create a new project, "Deploy from GitHub repo",
   pick your repo.
3. In the same Railway project, click "+ New" → "Database" → "Add
   PostgreSQL". Railway provisions it and gives you a `DATABASE_URL`
   automatically available as an env var to your other service.
4. On your app service, go to Variables and confirm `DATABASE_URL` is
   set (Railway usually references the Postgres service's variable
   automatically — check it's actually pointing at the Postgres
   service, not blank).
5. Also set `ANTHROPIC_API_KEY` as a variable (Settings → Variables).
   Never commit it to the repo. For the Automated Stock Trading vertical,
   also set `ALPHAVANTAGE_API_KEY` (free key at alphavantage.co) — without
   it, `trading_cycle` tasks fail loudly instead of trading on fabricated
   prices, which is correct behavior but means the feature is otherwise
   invisible.
6. Set **`DASHBOARD_USERNAME`** and **`DASHBOARD_PASSWORD`** — this is
   what actually protects `/dashboard` and everything under it (every
   business's data, the ARC ledger, agent controls) with HTTP Basic
   Auth (see `dashboard_auth.py`). It fails CLOSED: without both set,
   every internal route returns 503 rather than silently staying open,
   so skipping this step doesn't leave you exposed — it just means the
   dashboard itself won't load until you set them. Pick a real random
   password, not something guessable; failed attempts are rate-limited
   (`DASHBOARD_LOGIN_RATE_LIMIT_MAX` / `DASHBOARD_LOGIN_RATE_LIMIT_WINDOW_SECONDS`,
   default 10 per 5 minutes per IP) but that's a backstop, not a
   substitute for a strong password.
7. Railway should detect the `Dockerfile` and build/deploy from it. If
   it instead tries to auto-detect Python directly, that's fine too —
   it'll pick up `requirements.txt`. Either path should work; if one
   fails, try forcing Docker build in Railway's settings.
8. Once deployed, Railway gives you a public URL
   (`something.up.railway.app`). Open `https://that-url/dashboard` and
   log in with the username/password from step 6. The bare URL
   (`https://that-url/`) is the public storefront's landing page —
   that's the link to actually share with customers.

## If you'd rather use Render or Fly.io instead
The Dockerfile is portable — the app itself doesn't change.
- **Render**: "New Web Service" → connect repo → it detects the
  Dockerfile. You'll need the paid Starter tier ($7/mo) for a
  background-thread-capable, always-on service — the free tier sleeps
  on inactivity, which would silently kill the scheduler/executor
  threads. Add a Render Postgres instance the same way (New →
  PostgreSQL), and set `DATABASE_URL`/`ANTHROPIC_API_KEY` as
  environment variables on the web service.
- **Fly.io**: requires installing the `flyctl` CLI on your machine,
  then `fly launch` in this directory (it reads the Dockerfile
  automatically), `fly postgres create` for a database, `fly secrets
  set ANTHROPIC_API_KEY=...`. Fly's current free-allowance situation
  was genuinely unclear from what I could find — confirm actual costs
  on fly.io's own pricing page before committing.

## Storefront — turning on real payments (currently OFF)

The storefront (`/static/store.html`, `/store/checkout`, `/store/webhook`,
`fulfillment.py`) is fully built and offline-tested for four products —
Opportunity Discovery, Roblox Trend Research, App Feasibility, and Real
Estate Investment Research reports — but it is very likely **not
actually live** on your deployment right now:
none of the six environment variables it needs are set anywhere in this
repo or its deploy docs, and every one of them is required (there is no
"demo mode" — `/store/checkout` returns a clear 503 until they're set,
and `send_email()` raises rather than pretending an email went out).

1. **`STRIPE_SECRET_KEY`** — from your Stripe Dashboard → Developers →
   API keys. Use a `sk_test_...` key first and only switch to a live
   `sk_live_...` key once you've actually completed a real test purchase
   end to end (see the checklist below).
2. **`STRIPE_WEBHOOK_SECRET`** — from Stripe Dashboard → Developers →
   Webhooks → "Add endpoint". Point it at
   `https://your-url.up.railway.app/store/webhook`, subscribe it to the
   `checkout.session.completed` event, then copy the signing secret
   (`whsec_...`) it gives you. Every order is only ever marked paid in
   response to this webhook, cryptographically verified — never trusted
   from the client redirect alone.
3. **`STORE_BUSINESS_ID`** — the `id` of an existing business in this
   system (e.g. one you create first via the dashboard) that will own
   every storefront order/task/ARC entry. `GET /businesses` lists ids.
4. **`PUBLIC_BASE_URL`** — your real deployed URL, no trailing slash
   (e.g. `https://ai-holding-os-production.up.railway.app`). Stripe uses
   this to build the success/cancel redirect URLs.
5. **`RESEND_API_KEY`** — from resend.com (they have a free tier). This
   is what actually emails the finished report to the paying customer.
6. **`RESEND_FROM_EMAIL`** — a sender address on a domain you've verified
   with Resend, or their own test sender `onboarding@resend.dev` while
   you're still testing (that one can only send to your own verified
   Resend account email, not real customers — switch to a verified
   domain before accepting real orders).

Optional: `STORE_PRICE_OPPORTUNITY_CENTS` / `STORE_PRICE_ROBLOX_CENTS` /
`STORE_PRICE_APP_FEASIBILITY_CENTS` / `STORE_PRICE_REAL_ESTATE_CENTS`
override the default $19.00 price per report (each is a number of
cents, e.g. `2900` for $29.00).

Optional: **`OWNER_EMAIL`** — your own address. Refunds are never
automated (see `fulfillment.py`'s module docstring) — if a paid order's
research task fails, the customer has already been charged and someone
has to notice and refund them manually via Stripe. Without this set,
that's only visible by opening the dashboard's Store Orders panel; with
it set, you also get an email the moment it happens. This same alert
also fires if a completed task's report data never shows up (a bug,
not a normal outcome) after `FULFILLMENT_BUILD_FAILURE_RETRY_LIMIT`
fulfillment passes (default 5), or if the report email itself never
sends (e.g. Resend outage, or `RESEND_API_KEY`/`RESEND_FROM_EMAIL` never
configured at all) after `FULFILLMENT_EMAIL_FAILURE_RETRY_LIMIT` passes
(default 20) — either way, that order is then also given up on and
marked `failed` rather than being retried forever.

Optional: `CHECKOUT_RATE_LIMIT_MAX` / `CHECKOUT_RATE_LIMIT_WINDOW_SECONDS`
(default 10 requests per 60 seconds, per client IP) — `/store/checkout`
is the one public, unauthenticated endpoint that does real work on every
call (creates a real Stripe Checkout Session, writes an order row), so
it's rate-limited to stop it being spammed. Raise the limit if real
customers are ever legitimately hitting it (e.g. a payment provider
integration retrying on your behalf); a normal customer retrying a
declined card a couple of times will never come close to the default.

**Before accepting real money, verify the whole loop with Stripe's test
mode** (test-mode keys, and Stripe's published test card
`4242 4242 4242 4242`, any future expiry/CVC):
1. Open `/static/store.html`, buy a report with a real email address you
   can check.
2. Confirm the Stripe webhook actually fires and `GET /store/orders/{id}`
   moves from `pending_payment` → `paid` → `fulfilled`.
3. Confirm the report email actually arrives, with real content (not a
   blank/error report).
4. Only after that succeeds, switch `STRIPE_SECRET_KEY`/
   `STRIPE_WEBHOOK_SECRET` to live-mode values.

## Ops/Maintenance — watches this system's own health (no setup needed)

Unlike every other vertical, this one needs nothing beyond the
`ANTHROPIC_API_KEY` you already have set — a "System Operations"
business, an Ops Monitor agent, and a recurring review job are created
automatically the first time the app starts (idempotent — safe on
every later restart/redeploy too). It watches this system's own stuck
tasks/approvals/orders, silent scheduled jobs, database growth, recent
errors, and missing optional config, and produces a recommend-only
report on the dashboard's "System Health" panel — it never restarts,
deletes, or changes anything on its own.

Optional: `OPS_REVIEW_INTERVAL_SECONDS` (default 86400 = 24h) controls
how often it runs on its own; use the panel's "Run Ops Review Now"
button to trigger one immediately instead of waiting. A handful of
`OPS_*_THRESHOLD_HOURS` env vars (see `tasks/ops_maintenance_review.py`)
tune exactly how overdue something needs to be before it's flagged, if
the defaults don't fit your usage patterns.

## Live Trading — REAL MONEY (currently OFF, opt-in per business)

Everything under "Automated Stock Trading" above is paper (simulated)
trading — safe by construction, since there's no brokerage integration
anywhere in that path. Live trading is a *separate*, deliberately
harder-to-reach feature that places real orders with real cash through
[Alpaca](https://alpaca.markets) (a free brokerage API with its own
built-in paper-trading endpoint, which this codebase also uses for
testing). It stays fully OFF — structurally, not just by a flag not
being set — until every one of the following is true:

1. **`ALPACA_API_KEY`** / **`ALPACA_API_SECRET`** — from your Alpaca
   dashboard. Without both set, any `live_trading_cycle` task refuses
   to run at all (`alpaca_client.get_default_client()` raises rather
   than silently falling back to a mock — see `alpaca_client.py`).
2. **`ALPACA_BASE_URL`** — set this explicitly to
   `https://api.alpaca.markets` to actually reach Alpaca's LIVE
   endpoint. **This is the single most important line in this section.**
   Leave it unset (or point it at Alpaca's own paper endpoint,
   `https://paper-api.alpaca.markets`) and `AlpacaClient` defaults to
   paper — real credentials alone are never enough to place a real
   order. `_handle_live_trading_cycle` in `executor.py` also checks
   this itself at runtime and refuses to proceed if it's still pointed
   at paper, so a forgotten `ALPACA_BASE_URL` fails loudly instead of
   quietly trading Alpaca's own paper simulator under a "live" label.
3. **The owner enables it per business**, explicitly, from the
   dashboard's "Live Trading" panel (`POST
   /businesses/{id}/trading/live/enable` with
   `confirm_real_money: true`) — there is no default or bulk-enable
   path. This requires a paper trading portfolio (and its active
   strategy — watchlist, position/trade/exposure limits) to already
   exist for that business; live trading reuses it as-is rather than
   having a separate "live strategy."

Two independent safety layers apply to every trade, in order, before
it ever reaches the broker:
- The existing percentage-based limits in `tasks/trading_common.py` /
  `tasks/trading_cycle.py` (same code paper trading already uses,
  completely unchanged).
- Absolute-dollar hard caps in `tasks/live_trading_safety.py`:
  **`LIVE_TRADING_MAX_TRADE_USD`** (default `15.0`) caps any single
  trade; **`LIVE_TRADING_MAX_DAILY_LOSS_USD`** (default `7.0`) halts
  all further live trades — and pauses the live trading agent — once
  today's *realized* losses reach it. These defaults are sized for a
  small ~$100 starting live account; raise them deliberately as real
  capital is added (see that file's own comments for the reasoning).

**`LIVE_TRADING_KILL_SWITCH`** — set to `1`/`true`/`yes`/`on` to
instantly halt all live trading across every business, checked fresh
on every cycle. No redeploy, no dashboard access needed — this is the
fastest possible emergency stop, meant for exactly that use.

Real fills are recorded into `live_trades`/`live_snapshots` — tables
kept deliberately separate from `paper_trades`/`trading_snapshots`, so
a report or query that forgets a `WHERE` clause fails loudly (wrong or
empty table) instead of silently blending real and simulated activity.

**Before enabling this with real capital**: start with Alpaca's own
paper endpoint (the default) to confirm the whole loop — order
placement, fill polling, `live_trades` recording — behaves as expected
using Alpaca's simulator, exactly the same way you'd test Stripe in
test mode before going live above. Only then set `ALPACA_BASE_URL` to
the real live endpoint. This sandbox has no route to Alpaca's API at
all (no general internet access), so the actual network calls in
`alpaca_client.py` have only ever been tested with a mock broker
client (`MockAlpacaClient`) — see `test_alpaca_client_offline.py` and
`test_executor_offline.py`'s `live_trading_cycle` tests for exactly
what is and isn't covered. Treat the real Alpaca connection as
unverified until you've watched it place and fill one small order
yourself.

## Strategy Backtesting — test against real history before risking real cash

Before ever enabling Live Trading (above), use this to see how the
current strategy — or a bounded search for a better one — would have
performed against REAL past market data, without spending a single day
of real/paper time waiting for cycles to accumulate. From the
dashboard's Backtest panel (or `POST /businesses/{id}/trading/backtest`
directly), pick a **train window** (what the search tunes against) and
a separate, later **validation window** (what actually judges a
candidate — never shown to the model while it's proposing changes).

**Why this exists rather than just "keep tweaking until it looks
good":** a strategy tuned and judged on the exact same historical data
will often look great in that test and fall apart on anything new —
classic overfitting. Splitting train from validation, and only ever
judging a candidate's real quality on the validation window, is the
standard defense. The search itself is bounded too — it tries at most
`max_candidates` strategies (default 5, capped at 10) and stops the
moment one clears the bar; "none of these were good enough" is a
normal, expected result, not a bug to route around. And the bar itself
is never win rate alone: `tasks/strategy_backtest_search.py`'s
`meets_bar()` requires a real sample size, positive net P&L, a
profit_factor with real margin above break-even (not just "wins more
than it loses"), and a bounded max drawdown.

**Requires `ALPHAVANTAGE_API_KEY`** (same key the paper-trading
vertical uses) — without it, `market_data.py` returns clearly-labeled
mock historical data and the backtest refuses to run against it,
exactly like live/paper trading refuse to trade on a mock quote.

**Free-tier history is capped at ~100 trading days (roughly the last
4-5 calendar months), found live:** Alpha Vantage's `TIME_SERIES_DAILY`
endpoint has an `outputsize=full` option for full history, but it's a
premium-only parameter now — a free key only ever gets `outputsize=
compact` (the most recent ~100 points). Pick a `train_start_date` no
more than about 4-5 months back from today, or the earlier portion of
your train window will simply have fewer (or zero) real bars than
requested. A range entirely older than that fails loudly
("no daily bars found for SYMBOL in range...") rather than silently
running on a partial or fabricated dataset.

**Real cost, stated plainly:** each simulated trading day is one real
LLM call — the same cost as one real trading_cycle task. A backtest
search over N historical days with `max_candidates` tries costs
roughly `N × max_candidates` LLM calls. Keep date ranges reasonable
(a few months is usually plenty to start, and is close to the free-tier
ceiling anyway) rather than backtesting years of history in one call.

**Nothing here is ever auto-activated.** A backtest run only ever
saves a report (`backtest_runs` table) of every candidate tried, with
its full train/validation stats. If a candidate looks genuinely good,
promote it yourself via the existing
`POST /businesses/{id}/trading/strategy-override` endpoint — same
versioned, bounds-checked path as any other strategy change.

## After deploying, verify for real (don't just assume it works)
1. Open `https://your-url/dashboard` — does it load?
2. Create a business, an agent, a task — does the same flow that
   worked locally still work?
3. Create a scheduled job with a 30s interval — does a new task appear
   on its own after ~30-60 seconds, proving the background thread
   survived deployment (this is the part most likely to have platform-
   specific gotchas — some platforms are stricter about long-running
   background threads inside a request-serving process)?
4. Submit a real Opportunity Discovery research request — does it
   complete with a real assessment?
5. Restart/redeploy the service, then check that your businesses/
   agents/tasks are still there — this proves Postgres (not the
   ephemeral local disk) is actually being used.

Report back whatever happens at each step — especially anything that
fails, since that's exactly the kind of platform-specific issue no
amount of local testing can catch in advance.
