-- AI Holding Company OS — Core Schema (Postgres)
-- Mirrors schema.sql exactly in structure; only the dialect differs
-- (SERIAL/IDENTITY instead of AUTOINCREMENT, TIMESTAMPTZ instead of
-- TEXT+datetime('now')). Keep the two files in sync by hand — this is
-- an MVP, not a migration framework.

CREATE TABLE IF NOT EXISTS businesses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    objective TEXT,
    budget_usd DOUBLE PRECISION DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    name TEXT NOT NULL,
    role TEXT,
    department TEXT,
    manager_id TEXT REFERENCES agents(id),
    status TEXT DEFAULT 'created',
    version TEXT DEFAULT 'v1.0',
    model TEXT,
    permission_level INTEGER DEFAULT 1,
    budget_arc DOUBLE PRECISION DEFAULT 0,
    arc_balance DOUBLE PRECISION DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    agent_id TEXT REFERENCES agents(id),
    parent_task_id TEXT REFERENCES tasks(id),
    objective TEXT NOT NULL,
    department TEXT,
    task_type TEXT DEFAULT 'manual',
    task_input TEXT,
    priority INTEGER DEFAULT 3,
    status TEXT DEFAULT 'queued',
    budget_arc DOUBLE PRECISION DEFAULT 0,
    cost_arc DOUBLE PRECISION DEFAULT 0,
    permission_level_required INTEGER DEFAULT 1,
    result TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS arc_ledger (
    id SERIAL PRIMARY KEY,
    agent_id TEXT,
    business_id TEXT,
    amount DOUBLE PRECISION NOT NULL,
    entry_type TEXT NOT NULL,
    reason TEXT,
    task_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    description TEXT,
    business_id TEXT,
    agent_id TEXT,
    task_id TEXT REFERENCES tasks(id),
    amount_usd DOUBLE PRECISION,
    risk_level TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT now(),
    decided_at TIMESTAMPTZ,
    decision_notes TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    actor TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Mirrors schema.sql's scheduled_jobs exactly, including using TEXT
-- (not TIMESTAMPTZ) for next_run_at/last_run_at: this codebase has
-- never run against real Postgres (no PyPI access in the sandbox that
-- built it), so this deliberately avoids introducing an untested
-- text-to-timestamptz implicit-cast dependency for a feature that
-- writes externally-computed timestamps as query parameters.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    task_type TEXT DEFAULT 'manual',
    task_input TEXT,
    department TEXT,
    permission_level_required INTEGER DEFAULT 1,
    budget_arc DOUBLE PRECISION DEFAULT 0,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

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
    confidence_level TEXT,
    summary TEXT,
    reference_urls_used TEXT,
    -- Set once, by POST .../opportunities/{id}/launch, when the owner
    -- turns this researched idea into a real business -- see api.py's
    -- launch_opportunity(). NULL means "not launched yet".
    launched_business_id TEXT REFERENCES businesses(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- See schema.sql for the full explanation of orders/real_transactions:
-- deliberately separate from arc_ledger since ARC never represents
-- real money, and real_transactions is only ever written after a
-- verified Stripe webhook (stripe_client.verify_webhook_signature()).

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    product_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    price_usd_cents INTEGER NOT NULL,
    currency TEXT DEFAULT 'usd',
    stripe_session_id TEXT,
    stripe_payment_intent_id TEXT,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    status TEXT DEFAULT 'pending_payment',
    created_at TIMESTAMPTZ DEFAULT now(),
    paid_at TIMESTAMPTZ,
    fulfilled_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS real_transactions (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES orders(id),
    direction TEXT NOT NULL,
    source TEXT NOT NULL,
    destination TEXT NOT NULL,
    amount_usd_cents INTEGER NOT NULL,
    currency TEXT DEFAULT 'usd',
    business_id TEXT REFERENCES businesses(id),
    purpose TEXT,
    stripe_event_id TEXT UNIQUE,
    compliance_status TEXT DEFAULT 'unreviewed',
    occurred_at TIMESTAMPTZ DEFAULT now()
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
    confidence_level TEXT,
    summary TEXT,
    reference_urls_used TEXT,
    launched_business_id TEXT REFERENCES businesses(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Automated Stock Trading — PAPER TRADING ONLY. See schema.sql for the
-- full explanation; this mirrors it exactly.
CREATE TABLE IF NOT EXISTS paper_portfolios (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    agent_id TEXT REFERENCES agents(id),
    starting_cash_usd DOUBLE PRECISION NOT NULL,
    cash_usd DOUBLE PRECISION NOT NULL,
    -- Owner-controlled real-money switch -- see schema.sql for the
    -- full explanation; this mirrors it exactly.
    live_trading_enabled INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    symbol TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(portfolio_id, symbol)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    task_id TEXT REFERENCES tasks(id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    price_usd DOUBLE PRECISION NOT NULL,
    realized_pnl_usd DOUBLE PRECISION,
    confidence_level TEXT,
    rationale TEXT,
    strategy_version INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trading_strategy_versions (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    version INTEGER NOT NULL,
    parameters TEXT NOT NULL,
    rationale TEXT,
    confidence_level TEXT,
    source TEXT NOT NULL DEFAULT 'system',
    active INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trading_snapshots (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    strategy_version INTEGER,
    equity_usd DOUBLE PRECISION NOT NULL,
    cash_usd DOUBLE PRECISION NOT NULL,
    open_positions INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Real-money trading -- see schema.sql for the full explanation of why
-- these are deliberately separate tables from paper_trades/
-- trading_snapshots; this mirrors it exactly.
CREATE TABLE IF NOT EXISTS live_trades (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    task_id TEXT REFERENCES tasks(id),
    alpaca_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    price_usd DOUBLE PRECISION NOT NULL,
    realized_pnl_usd DOUBLE PRECISION,
    confidence_level TEXT,
    rationale TEXT,
    strategy_version INTEGER NOT NULL,
    live_cap_applied INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS live_snapshots (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT REFERENCES paper_portfolios(id),
    strategy_version INTEGER,
    equity_usd DOUBLE PRECISION NOT NULL,
    cash_usd DOUBLE PRECISION NOT NULL,
    open_positions INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Backtesting & strategy search — see schema.sql for the full
-- explanation; this mirrors it exactly.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    train_start_date TEXT NOT NULL,
    validation_split_date TEXT NOT NULL,
    validation_end_date TEXT NOT NULL,
    max_candidates INTEGER NOT NULL,
    candidates_json TEXT NOT NULL,
    best_candidate_index INTEGER,
    stopped_early INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- market_data_usage — see schema.sql for the full explanation; this
-- mirrors it exactly.
CREATE TABLE IF NOT EXISTS market_data_usage (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- App Development — see schema.sql for the full explanation; this
-- mirrors it exactly.
CREATE TABLE IF NOT EXISTS app_feasibility_assessments (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    concept TEXT NOT NULL,
    platform_recommendation TEXT,
    suggested_tech_stack TEXT,
    complexity_tier TEXT,
    estimated_timeline TEXT,
    estimated_cost_range TEXT,
    mvp_feature_scope TEXT,
    key_technical_risks TEXT,
    similar_existing_apps TEXT,
    confidence_level TEXT,
    summary TEXT,
    reference_urls_used TEXT,
    launched_business_id TEXT REFERENCES businesses(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Ops/Maintenance — see schema.sql for the full explanation; this
-- mirrors it exactly.
CREATE TABLE IF NOT EXISTS ops_maintenance_reports (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    task_id TEXT REFERENCES tasks(id),
    overall_severity TEXT,
    findings TEXT,
    confidence_level TEXT,
    summary TEXT,
    metrics_snapshot TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Real Estate — see schema.sql for the full explanation; this mirrors
-- it exactly.
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
    confidence_level TEXT,
    summary TEXT,
    reference_urls_used TEXT,
    launched_business_id TEXT REFERENCES businesses(id),
    created_at TIMESTAMPTZ DEFAULT now()
);
