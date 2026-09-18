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
    created_at TEXT DEFAULT (datetime('now'))
);
