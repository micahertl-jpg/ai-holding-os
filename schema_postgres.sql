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
    created_at TIMESTAMPTZ DEFAULT now()
);
