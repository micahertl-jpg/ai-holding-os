-- AI Holding Company OS — Core Schema (MVP)
-- SQLite for local prototyping; designed to port 1:1 to Postgres later
-- (swap AUTOINCREMENT -> SERIAL/IDENTITY, TEXT timestamps -> TIMESTAMPTZ).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS businesses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    objective TEXT,
    budget_usd REAL DEFAULT 0,       -- REAL money authorized ceiling (owner-set)
    status TEXT DEFAULT 'active',    -- active | paused | retired
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    name TEXT NOT NULL,
    role TEXT,
    department TEXT,
    manager_id TEXT REFERENCES agents(id),
    status TEXT DEFAULT 'created',   -- created|initializing|active|working|idle|
                                      -- waiting|improving|paused|failed|quarantined|retired
    version TEXT DEFAULT 'v1.0',
    model TEXT,                      -- e.g. 'claude-sonnet-5' — routing stub, not live-wired yet
    permission_level INTEGER DEFAULT 1,  -- 0..7, see approvals.py docstring
    budget_arc REAL DEFAULT 0,       -- ARC allowance set by the Banker
    arc_balance REAL DEFAULT 0,      -- current ARC balance (earned - spent)
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    agent_id TEXT REFERENCES agents(id),
    parent_task_id TEXT REFERENCES tasks(id),
    objective TEXT NOT NULL,
    department TEXT,                  -- persisted so a queued task can be retried later
                                       -- (see Orchestrator.retry_queued_tasks) without
                                       -- losing the department it was originally filtered by
    task_type TEXT DEFAULT 'manual',  -- 'manual' (no auto-execution — the default,
                                       -- backward-compatible with every existing task)
                                       -- or a registered handler name, e.g.
                                       -- 'summarize_urls', 'research_opportunity' —
                                       -- see executor.py's HANDLERS registry
    task_input TEXT,                  -- JSON blob of handler-specific input
                                       -- (e.g. {"topic": "...", "reference_urls": [...]})
                                       -- NULL/unused for task_type='manual'
    priority INTEGER DEFAULT 3,       -- 1 (highest) .. 5 (lowest)
    status TEXT DEFAULT 'queued',     -- queued|assigned|in_progress|blocked|
                                       -- awaiting_approval|completed|failed|cancelled
    budget_arc REAL DEFAULT 0,
    cost_arc REAL DEFAULT 0,
    permission_level_required INTEGER DEFAULT 1,
    result TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS arc_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT,
    business_id TEXT,
    amount REAL NOT NULL,             -- positive = earn/allocate, negative = spend
    entry_type TEXT NOT NULL,         -- allocation|earn|spend|penalty
    reason TEXT,
    task_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Real-world USD is tracked completely separately from ARC. Nothing here
-- moves money; it only records the *intent* and the owner's decision.
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,        -- e.g. 'spend_usd', 'sign_contract', 'trade_execute'
    description TEXT,
    business_id TEXT,
    agent_id TEXT,
    task_id TEXT REFERENCES tasks(id), -- the task this decision gates, if any
    amount_usd REAL,
    risk_level TEXT,                  -- low|medium|high
    status TEXT DEFAULT 'pending',    -- pending|approved|rejected
    created_at TEXT DEFAULT (datetime('now')),
    decided_at TEXT,
    decision_notes TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT,                       -- agent id, 'owner', or 'system'
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Recurring tasks. next_run_at/last_run_at are stored as the same
-- 'YYYY-MM-DD HH:MM:SS' TEXT format as every other timestamp in this
-- schema (not a SQLite-only datetime type) specifically so lexicographic
-- comparison (`next_run_at <= ?`) works identically on Postgres too —
-- see schema_postgres.sql, which mirrors this as TEXT for the same reason.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    task_type TEXT DEFAULT 'manual',  -- same meaning as tasks.task_type; propagated to
                                       -- every task this job creates
    task_input TEXT,                  -- same meaning as tasks.task_input
    department TEXT,
    permission_level_required INTEGER DEFAULT 1,
    budget_arc REAL DEFAULT 0,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER DEFAULT 1,        -- 0/1, kept as INTEGER (not BOOLEAN) for
                                       -- identical behavior across both backends
    last_run_at TEXT,
    next_run_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Structured, evidence-based opportunity assessments produced by the
-- research_opportunity task type (Opportunity Discovery business
-- vertical). Every field is the model's ESTIMATE, never a guaranteed
-- fact — see tasks/research_opportunity.py's system prompt, which
-- enforces this framing, and confidence_level, which is mandatory.
CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    topic TEXT NOT NULL,
    market_size TEXT,
    competition TEXT,
    startup_cost TEXT,
    revenue_potential TEXT,
    time_to_market TEXT,
    operational_complexity TEXT,
    legal_regulatory_risk TEXT,
    capital_requirements TEXT,
    downside_risk TEXT,
    confidence_level TEXT,            -- low|medium|high — see research_opportunity.py
    summary TEXT,
    reference_urls_used TEXT,         -- JSON array of URLs that actually fetched
    -- Set once, by POST .../opportunities/{id}/launch, when the owner
    -- turns this researched idea into a real business -- see api.py's
    -- launch_opportunity(). NULL means "not launched yet".
    launched_business_id TEXT REFERENCES businesses(id),
    created_at TEXT DEFAULT (datetime('now'))
);

-- Everything below this point is the customer-facing storefront (the
-- first path this system has to real-world USD, not just ARC). orders
-- and real_transactions are DELIBERATELY separate from arc_ledger —
-- ARC never represents real money, so a real payment must never be
-- recorded there. A real_transactions row is only ever written by
-- api.py's Stripe webhook handler after stripe_client.verify_webhook_
-- signature() succeeds — never speculatively, never before payment is
-- actually confirmed. See stripe_client.py and fulfillment.py.

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    product_type TEXT NOT NULL,       -- 'research_opportunity' | 'research_roblox_trend'
    topic TEXT NOT NULL,              -- the topic/concept the customer wants researched
    customer_email TEXT NOT NULL,
    price_usd_cents INTEGER NOT NULL,
    currency TEXT DEFAULT 'usd',
    stripe_session_id TEXT,
    stripe_payment_intent_id TEXT,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),   -- set once payment is confirmed and the
                                          -- research task is actually created
    status TEXT DEFAULT 'pending_payment',
        -- pending_payment | paid | fulfilled | failed | refunded
    created_at TEXT DEFAULT (datetime('now')),
    paid_at TEXT,
    fulfilled_at TEXT
);

CREATE TABLE IF NOT EXISTS real_transactions (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES orders(id),
    direction TEXT NOT NULL,          -- 'in' (customer paid) | 'out' (refund/payout, future)
    source TEXT NOT NULL,             -- e.g. "stripe_customer:<email>"
    destination TEXT NOT NULL,        -- e.g. "owner_stripe_account"
    amount_usd_cents INTEGER NOT NULL,
    currency TEXT DEFAULT 'usd',
    business_id TEXT REFERENCES businesses(id),
    purpose TEXT,
    stripe_event_id TEXT UNIQUE,      -- the verified webhook event id — UNIQUE so a
                                       -- retried/duplicate webhook delivery can never
                                       -- record the same real payment twice
    compliance_status TEXT DEFAULT 'unreviewed',
    occurred_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS roblox_trends (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    concept TEXT NOT NULL,
    player_demand_signals TEXT,
    competition_level TEXT,
    build_complexity TEXT,
    target_audience TEXT,
    monetization_fit TEXT,
    estimated_dev_time TEXT,
    similar_successful_games TEXT,
    risk_factors TEXT,
    confidence_level TEXT,            -- low|medium|high — see research_roblox_trend.py
    summary TEXT,
    reference_urls_used TEXT,         -- JSON array of URLs that actually fetched
    -- Set once, by POST .../roblox-trends/{id}/launch, when the owner
    -- turns this researched concept into a real business. NULL means
    -- "not launched yet". Same pattern as opportunities.launched_business_id.
    launched_business_id TEXT REFERENCES businesses(id),
    created_at TEXT DEFAULT (datetime('now'))
);

-- Automated Stock Trading — PAPER TRADING ONLY. Nothing in this schema
-- or anywhere it's read/written (tasks/trading_cycle.py, executor.py,
-- api.py) can move real money — there is no brokerage integration in
-- this codebase. One portfolio per business for this MVP.
CREATE TABLE IF NOT EXISTS paper_portfolios (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    agent_id TEXT REFERENCES agents(id),
    starting_cash_usd REAL NOT NULL,
    cash_usd REAL NOT NULL,
    -- Owner-controlled switch for THIS business's real-money trading —
    -- off by default, and staying off is what keeps every other
    -- account on this system paper-only even after live trading exists
    -- in the codebase. Only ever flipped by an explicit owner action on
    -- the dashboard (see api.py's set-live-trading-enabled endpoint),
    -- never by the model or by any default. See alpaca_client.py and
    -- tasks/live_trading_safety.py for the rest of the real-money
    -- safety design (paper-by-default broker endpoint, hard dollar
    -- caps, a separate kill switch).
    live_trading_enabled INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Current open (or previously-open, quantity may be 0) positions. One
-- row per (portfolio, symbol) — upserted by executor.py's trading_cycle
-- handler on every executed trade, never inserted more than once per
-- symbol per portfolio.
CREATE TABLE IF NOT EXISTS paper_positions (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    avg_cost_usd REAL NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(portfolio_id, symbol)
);

-- Append-only paper trade log — the full history behind every equity
-- number and every strategy-review statistic. realized_pnl_usd is set
-- only on 'sell' rows (NULL for 'buy'); strategy_version is the
-- trading_strategy_versions.version that was ACTIVE at the moment this
-- trade executed, so a version's real track record can always be
-- reconstructed later even after a newer version becomes active.
CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    task_id TEXT REFERENCES tasks(id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,               -- buy | sell
    quantity REAL NOT NULL,
    price_usd REAL NOT NULL,
    realized_pnl_usd REAL,            -- set for 'sell' only
    confidence_level TEXT,            -- the model's stated confidence — low|medium|high
    rationale TEXT,                   -- the model's stated reasoning, never hidden from the owner
    strategy_version INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Full version history of strategy parameters (see
-- tasks/trading_common.py's schema). Exactly one row per business should
-- have active=1 at a time (enforced in application code, not a DB
-- constraint, to keep this MVP's schema portable across both backends).
-- Rows are NEVER deleted or edited after creation — a "rollback" is
-- reactivating an older version's parameters as a new row, so the full
-- history (including what was tried and abandoned) is always visible.
CREATE TABLE IF NOT EXISTS trading_strategy_versions (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    version INTEGER NOT NULL,
    parameters TEXT NOT NULL,         -- JSON, validated by trading_common.validate_parameters
    rationale TEXT,                   -- why this version differs from the last
    confidence_level TEXT,            -- the model's stated confidence in this proposal (NULL for v1/owner-set)
    source TEXT NOT NULL DEFAULT 'system',  -- 'system' (initial default) | 'strategy_review' | 'owner_override'
    active INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- One row per trading_cycle task, recording mark-to-market equity at
-- that moment. This is what powers the equity curve and drawdown circuit
-- breaker (see executor.py's _handle_trading_cycle) without re-fetching
-- live quotes just to render the dashboard.
CREATE TABLE IF NOT EXISTS trading_snapshots (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    strategy_version INTEGER,
    equity_usd REAL NOT NULL,         -- cash + mark-to-market open positions
    cash_usd REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Real-money trading (see alpaca_client.py, tasks/live_trading_safety.py,
-- executor.py's _handle_live_trading_cycle). Deliberately separate
-- tables from paper_trades/trading_snapshots, not a shared table with
-- an is_live flag -- real and simulated activity must never be
-- reachable by the same unfiltered query, so a report or dashboard
-- panel that forgets a WHERE clause fails loudly (wrong table/empty
-- result) instead of silently blending real trades into a paper
-- report or vice versa.
--
-- Append-only real trade log, structurally parallel to paper_trades.
-- price_usd is always the REAL fill price Alpaca reports back, never
-- the pre-trade quote used to size the order -- real fills can differ
-- from the quoted price (slippage), and this table must reflect what
-- actually happened to real money, not what was requested.
CREATE TABLE IF NOT EXISTS live_trades (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    task_id TEXT REFERENCES tasks(id),
    alpaca_order_id TEXT NOT NULL,    -- traces every row back to the real broker order
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,               -- buy | sell
    quantity REAL NOT NULL,           -- the REAL filled quantity, not the requested one
    price_usd REAL NOT NULL,          -- the REAL fill price, not the pre-trade quote
    realized_pnl_usd REAL,            -- set for 'sell' only
    confidence_level TEXT,
    rationale TEXT,
    strategy_version INTEGER NOT NULL,
    live_cap_applied INTEGER DEFAULT 0,  -- true if live_trading_safety.py clamped this trade's size
    created_at TEXT DEFAULT (datetime('now'))
);

-- Real equity history, fetched fresh from Alpaca's own account endpoint
-- every live cycle -- never derived from locally-summed cash/positions,
-- so it can never silently drift from what the broker actually holds.
-- Powers the live equity curve and the drawdown circuit breaker, the
-- same role trading_snapshots plays for paper.
CREATE TABLE IF NOT EXISTS live_snapshots (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    strategy_version INTEGER,
    equity_usd REAL NOT NULL,         -- real equity, straight from Alpaca's /v2/account
    cash_usd REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Backtesting & bounded strategy search against REAL historical price
-- data (see tasks/backtest.py, tasks/strategy_backtest_search.py). One
-- row per triggered search: every candidate strategy tried, each with
-- its own TRAIN stats (what the model saw) and VALIDATION stats (a
-- held-out window it never saw) -- the defense against a strategy that
-- looks great on the data it was tuned against and nowhere else.
-- Recommend-only, like ops_maintenance_reports: nothing here ever
-- becomes the active strategy automatically. Deliberately separate
-- from paper_trades/live_trades/trading_snapshots -- a backtest never
-- writes to any of those, so simulated-on-history activity can never
-- be mistaken for a real paper cycle or a real trade.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    train_start_date TEXT NOT NULL,
    validation_split_date TEXT NOT NULL,  -- bars before this date are TRAIN, on/after are VALIDATION
    validation_end_date TEXT NOT NULL,
    max_candidates INTEGER NOT NULL,
    -- Full candidate list as JSON: [{parameters, rationale, confidence_level,
    -- train_stats, validation_stats, train_meets_bar, validation_meets_bar}, ...]
    -- -- same "structured JSON in a text column" pattern as
    -- ops_maintenance_reports.findings, kept here rather than normalized
    -- into a child table since a run's candidates are always read/shown
    -- together, never queried individually.
    candidates_json TEXT NOT NULL,
    best_candidate_index INTEGER,      -- index into candidates_json's array, by validation net P&L
    stopped_early INTEGER DEFAULT 0,   -- true if a candidate cleared the profitability bar
    created_at TEXT DEFAULT (datetime('now'))
);

-- App Development — feasibility/planning assessments produced by the
-- research_app_feasibility task type. Every field is the model's
-- ESTIMATE, never a guaranteed timeline/cost/outcome — see
-- tasks/research_app_feasibility.py's system prompt, which enforces
-- this framing and requires regulated-domain risks (payments, health
-- data, etc.) to be flagged for specialist review rather than resolved
-- here. confidence_level and complexity_tier are both mandatory,
-- validated enums.
CREATE TABLE IF NOT EXISTS app_feasibility_assessments (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    concept TEXT NOT NULL,
    platform_recommendation TEXT,
    suggested_tech_stack TEXT,
    complexity_tier TEXT,             -- simple|moderate|complex|very_complex
    estimated_timeline TEXT,
    estimated_cost_range TEXT,
    mvp_feature_scope TEXT,
    key_technical_risks TEXT,
    similar_existing_apps TEXT,
    confidence_level TEXT,            -- low|medium|high — see research_app_feasibility.py
    summary TEXT,
    reference_urls_used TEXT,         -- JSON array of URLs that actually fetched
    -- Set once, by POST .../app-feasibility/{id}/launch, when the owner
    -- turns this researched idea into a real business. NULL means "not
    -- launched yet". Same pattern as opportunities.launched_business_id.
    launched_business_id TEXT REFERENCES businesses(id),
    created_at TEXT DEFAULT (datetime('now'))
);

-- Ops/Maintenance — the sixth business vertical, and the only one with
-- no customer/storefront product: it watches this system's OWN
-- infrastructure (stuck tasks/approvals/orders, silent scheduled jobs,
-- database growth, recent errors, missing optional config) and
-- produces a recommend-only report for the owner. See
-- tasks/ops_maintenance_review.py. metrics_snapshot is the real,
-- code-computed data the model was actually given — kept alongside the
-- model's synthesis so a report can always be checked against the raw
-- numbers behind it, never just trusted blind.
CREATE TABLE IF NOT EXISTS ops_maintenance_reports (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    overall_severity TEXT,            -- ok|info|warning|critical
    findings TEXT,                    -- JSON array of {category, severity, description, recommendation}
    confidence_level TEXT,            -- low|medium|high
    summary TEXT,
    metrics_snapshot TEXT,            -- JSON: the real metrics collect_system_metrics() computed
    created_at TEXT DEFAULT (datetime('now'))
);

-- Real Estate — investment research assessments produced by the
-- research_real_estate task type. Research only: never an appraisal,
-- never a brokered transaction. Every field is the model's ESTIMATE —
-- see tasks/research_real_estate.py's system prompt, which requires
-- any jurisdiction-specific issue (zoning, disclosure law, rent
-- control, licensing) to be flagged for a licensed real estate agent/
-- appraiser/attorney, never resolved here. confidence_level is
-- mandatory.
CREATE TABLE IF NOT EXISTS real_estate_assessments (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    property_or_market TEXT NOT NULL,
    market_trend TEXT,
    comparable_properties TEXT,
    estimated_rental_yield TEXT,
    price_trend_assessment TEXT,
    risk_factors TEXT,
    confidence_level TEXT,            -- low|medium|high — see research_real_estate.py
    summary TEXT,
    reference_urls_used TEXT,         -- JSON array of URLs that actually fetched
    -- Set once, by POST .../real-estate/{id}/launch, when the owner
    -- turns this researched opportunity into a real business. NULL
    -- means "not launched yet". Same pattern as
    -- opportunities.launched_business_id.
    launched_business_id TEXT REFERENCES businesses(id),
    created_at TEXT DEFAULT (datetime('now'))
);
