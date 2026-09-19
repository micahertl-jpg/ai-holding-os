/*
 * test_dashboard_render.js — real unit tests for the pure rendering
 * functions in static/dashboard-render.js, run with plain Node (no
 * jsdom, no browser — these functions build HTML strings, they don't
 * touch the DOM). Sample data mirrors the exact JSON shapes confirmed
 * live against api.py's real endpoints, not guessed shapes.
 *
 * Run: node test_dashboard_render.js
 */
const assert = require("assert");
const R = require("./static/dashboard-render.js");

function test(name, fn) {
  try {
    fn();
    console.log(`PASS: ${name}`);
  } catch (e) {
    console.error(`FAIL: ${name}\n  ${e.message}`);
    process.exitCode = 1;
  }
}

test("escapeHtml prevents raw HTML/script injection from agent or task names", () => {
  const evil = '<script>alert("x")</script>';
  const out = R.escapeHtml(evil);
  assert.ok(!out.includes("<script>"), "should not contain a raw <script> tag");
  assert.strictEqual(out, "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;");
});

test("renderBusinessOptions handles the empty case", () => {
  const html = R.renderBusinessOptions([]);
  assert.ok(html.includes("No businesses yet"));
});

test("renderBusinessOptions renders real business shape correctly", () => {
  const businesses = [
    { id: "biz_13e527bb3cf2", name: "Test Co", status: "active" },
  ];
  const html = R.renderBusinessOptions(businesses);
  assert.ok(html.includes('value="biz_13e527bb3cf2"'));
  assert.ok(html.includes("Test Co"));
  assert.ok(html.includes("active"));
});

test("renderAgentOptions handles the empty case", () => {
  const html = R.renderAgentOptions([]);
  assert.ok(html.includes("No agents yet"));
});

test("renderAgentOptions renders real agent shape with its ARC balance", () => {
  const agents = [{ id: "agt_abc123", name: "Researcher", arc_balance: 87.5 }];
  const html = R.renderAgentOptions(agents);
  assert.ok(html.includes('value="agt_abc123"'));
  assert.ok(html.includes("Researcher"));
  assert.ok(html.includes("87.5") || html.includes("87.50"));
});

test("renderBusinessHeader shows budget with the not-moved disclaimer", () => {
  const biz = {
    name: "Test Co", status: "active", objective: "first live API test",
    budget_usd: 100,
  };
  const html = R.renderBusinessHeader(biz);
  assert.ok(html.includes("Test Co"));
  assert.ok(html.includes("$100.00"));
  assert.ok(html.includes("not moved"));
});

test("renderAgentsTable handles the real agent shape returned by the API", () => {
  const agents = [{
    id: "agt_74f73a30047b", business_id: "biz_13e527bb3cf2", name: "Rex",
    role: "Research Analyst", department: "research", manager_id: null,
    status: "idle", version: "v1.0", model: "unassigned",
    permission_level: 2, budget_arc: 0, arc_balance: 0,
  }];
  const html = R.renderAgentsTable(agents);
  assert.ok(html.includes("Rex"));
  assert.ok(html.includes("Research Analyst"));
  assert.ok(html.includes('data-agent-id="agt_74f73a30047b"'));
  assert.ok(html.includes("0.0")); // arc_balance formatted
  assert.ok(html.includes('data-action="pause-agent"'));
  assert.ok(html.includes('data-action="retire-agent"'));
});

test("renderAgentsTable handles the empty case without throwing", () => {
  assert.ok(R.renderAgentsTable([]).includes("No agents yet"));
  assert.ok(R.renderAgentsTable(null).includes("No agents yet"));
});

test("renderTasksTable renders status and cost correctly", () => {
  const tasks = [{
    id: "task_1", objective: "Scan 5 candidate niches", status: "completed",
    priority: 3, cost_arc: 4,
  }];
  const html = R.renderTasksTable(tasks);
  assert.ok(html.includes("Scan 5 candidate niches"));
  assert.ok(html.includes("status-completed"));
  assert.ok(html.includes("4.0"));
});

test("renderApprovalsList shows risk level, description, amount, and action buttons", () => {
  const approvals = [{
    id: "appr_1", action_type: "execute_task",
    description: "Task task_x requires human approval before execution",
    amount_usd: 50, risk_level: "high",
  }];
  const html = R.renderApprovalsList(approvals);
  assert.ok(html.includes("[high]"));
  assert.ok(html.includes("$50.00"));
  assert.ok(html.includes('data-approval-id="appr_1"'));
  assert.ok(html.includes("btn-approve"));
  assert.ok(html.includes("btn-reject"));
  assert.ok(html.includes('data-action="approve"'));
  assert.ok(html.includes('data-action="reject"'));
});

test("renderApprovalsList handles the empty case", () => {
  assert.ok(R.renderApprovalsList([]).includes("Nothing awaiting"));
});

test("renderApprovalsList omits the amount span when amount_usd is null", () => {
  const approvals = [{
    id: "appr_2", description: "Some non-monetary action", amount_usd: null,
    risk_level: "medium",
  }];
  const html = R.renderApprovalsList(approvals);
  assert.ok(!html.includes("$0.00"), "should not fabricate a $0.00 amount");
});

test("renderArcSummary handles the real (sparse) shape from banker.business_summary", () => {
  // Real observed shape only includes keys that actually occurred, e.g. {}
  // for a brand-new business, or {allocation: 20} with no earn/spend yet.
  assert.ok(R.renderArcSummary({}).includes("0.0"));
  const html = R.renderArcSummary({ allocation: 20 });
  assert.ok(html.includes("20.0"));
});

test("renderJobsTable handles the empty case", () => {
  assert.ok(R.renderJobsTable([]).includes("No scheduled jobs yet"));
  assert.ok(R.renderJobsTable(null).includes("No scheduled jobs yet"));
});

test("renderJobsTable renders an enabled job with a Disable button", () => {
  const jobs = [{
    id: "job_1", name: "Daily scan", objective: "Scan for new opportunities",
    interval_seconds: 3600, next_run_at: "2026-09-18 12:00:00", enabled: 1,
  }];
  const html = R.renderJobsTable(jobs);
  assert.ok(html.includes("Daily scan"));
  assert.ok(html.includes("3600s"));
  assert.ok(html.includes("2026-09-18 12:00:00"));
  assert.ok(html.includes(">enabled<"));
  assert.ok(html.includes("Disable"));
  assert.ok(html.includes('data-set-enabled="false"'));
  assert.ok(html.includes('data-action="set-job-enabled"'));
  // Regression check: the job-toggle button must NOT carry the agent/approval
  // action classes (btn-pause, btn-retire, btn-approve, btn-reject), because
  // dashboard.js's click handler checks those classes first — if a job
  // button matches one, it gets hijacked as an agent/approval action with
  // no agent_id, producing exactly a 404 "agent not found". This is not
  // hypothetical: it happened during real click-through testing.
  for (const forbidden of ["btn-pause", "btn-retire", "btn-approve", "btn-reject"]) {
    assert.ok(!html.includes(forbidden),
      `job button HTML must not contain the "${forbidden}" class — it would be ` +
      `misrouted by dashboard.js's class-based click handler`);
  }
});

test("renderJobsTable renders a disabled job with an Enable button", () => {
  const jobs = [{
    id: "job_2", name: "Paused job", objective: "x", interval_seconds: 60,
    next_run_at: "2026-09-18 12:00:00", enabled: 0,
  }];
  const html = R.renderJobsTable(jobs);
  assert.ok(html.includes(">disabled<"));
  assert.ok(html.includes("Enable"));
  assert.ok(html.includes('data-set-enabled="true"'));
  for (const forbidden of ["btn-pause", "btn-retire", "btn-approve", "btn-reject"]) {
    assert.ok(!html.includes(forbidden),
      `job button HTML must not contain the "${forbidden}" class`);
  }
});

test("renderOpportunitiesTable handles the empty case", () => {
  assert.ok(R.renderOpportunitiesTable([]).includes("No opportunities researched yet"));
  assert.ok(R.renderOpportunitiesTable(null).includes("No opportunities researched yet"));
});

test("renderOpportunitiesTable renders the real shape from the opportunities table", () => {
  const opportunities = [{
    id: "opp_1", business_id: "biz_1", task_id: "task_1",
    topic: "AI-powered recipe apps",
    market_size: "Moderate, per reference material.",
    competition: "Fragmented.",
    startup_cost: "Low.",
    revenue_potential: "Modest.",
    time_to_market: "A few weeks.",
    operational_complexity: "Low.",
    legal_regulatory_risk: "Minimal.",
    capital_requirements: "Under $5k.",
    downside_risk: "Low.",
    confidence_level: "medium",
    summary: "Worth a small validation effort.",
    reference_urls_used: JSON.stringify(["https://example.com/a"]),
  }];
  const html = R.renderOpportunitiesTable(opportunities);
  assert.ok(html.includes("AI-powered recipe apps"));
  assert.ok(html.includes("confidence: medium"));
  assert.ok(html.includes("Worth a small validation effort."));
  assert.ok(html.includes("Moderate, per reference material."));
  assert.ok(html.includes("https://example.com/a"));
  assert.ok(html.includes("confidence-medium"));
});

test("renderOpportunitiesTable shows the no-references note when none were used", () => {
  const opportunities = [{
    id: "opp_2", topic: "x", confidence_level: "low", summary: "y",
    reference_urls_used: JSON.stringify([]),
  }];
  const html = R.renderOpportunitiesTable(opportunities);
  assert.ok(html.includes("based on general knowledge only"));
});

test("renderOpportunitiesTable never throws on malformed reference_urls_used JSON", () => {
  const opportunities = [{ id: "opp_3", topic: "x", confidence_level: "high", summary: "y",
                            reference_urls_used: "not valid json" }];
  const html = R.renderOpportunitiesTable(opportunities); // should not throw
  assert.ok(html.includes("based on general knowledge only"));
});

test("renderRobloxTrendsTable handles the empty case", () => {
  assert.ok(R.renderRobloxTrendsTable([]).includes("No Roblox concepts researched yet"));
  assert.ok(R.renderRobloxTrendsTable(null).includes("No Roblox concepts researched yet"));
});

test("renderRobloxTrendsTable renders the real shape from the roblox_trends table", () => {
  const trends = [{
    id: "rbx_1", business_id: "biz_1", task_id: "task_1",
    concept: "obby with a twist mechanic",
    player_demand_signals: "Moderate, per reference material.",
    competition_level: "Fragmented.",
    build_complexity: "Moderate.",
    target_audience: "Kids/teens.",
    monetization_fit: "Game passes plausible.",
    estimated_dev_time: "A few weeks.",
    similar_successful_games: "A couple of comparable experiences.",
    risk_factors: "Genre is moderately saturated.",
    confidence_level: "medium",
    summary: "Worth a small prototype effort.",
    reference_urls_used: JSON.stringify(["https://example.com/a"]),
  }];
  const html = R.renderRobloxTrendsTable(trends);
  assert.ok(html.includes("obby with a twist mechanic"));
  assert.ok(html.includes("confidence: medium"));
  assert.ok(html.includes("Worth a small prototype effort."));
  assert.ok(html.includes("Moderate, per reference material."));
  assert.ok(html.includes("https://example.com/a"));
  assert.ok(html.includes("confidence-medium"));
});

test("renderRobloxTrendsTable shows the no-references note when none were used", () => {
  const trends = [{
    id: "rbx_2", concept: "x", confidence_level: "low", summary: "y",
    reference_urls_used: JSON.stringify([]),
  }];
  const html = R.renderRobloxTrendsTable(trends);
  assert.ok(html.includes("based on general knowledge only"));
});

test("renderRobloxTrendsTable never throws on malformed reference_urls_used JSON", () => {
  const trends = [{ id: "rbx_3", concept: "x", confidence_level: "high", summary: "y",
                     reference_urls_used: "not valid json" }];
  const html = R.renderRobloxTrendsTable(trends); // should not throw
  assert.ok(html.includes("based on general knowledge only"));
});

test("renderAppFeasibilityTable handles the empty case", () => {
  assert.ok(R.renderAppFeasibilityTable([]).includes("No app feasibility assessments yet"));
  assert.ok(R.renderAppFeasibilityTable(null).includes("No app feasibility assessments yet"));
});

test("renderAppFeasibilityTable renders the real shape from the app_feasibility_assessments table", () => {
  const assessments = [{
    id: "app_1", business_id: "biz_1", task_id: "task_1",
    concept: "a habit tracker with social accountability",
    platform_recommendation: "Cross-platform mobile via React Native.",
    suggested_tech_stack: "React Native, FastAPI, Postgres.",
    complexity_tier: "moderate",
    estimated_timeline: "8-12 weeks for an MVP",
    estimated_cost_range: "Roughly $15k-$40k, a rough estimate.",
    mvp_feature_scope: "Account creation and one core interaction loop.",
    key_technical_risks: "Push notification reliability.",
    similar_existing_apps: "A few comparable apps exist.",
    confidence_level: "medium",
    summary: "Worth a small MVP validation effort.",
    reference_urls_used: JSON.stringify(["https://example.com/a"]),
  }];
  const html = R.renderAppFeasibilityTable(assessments);
  assert.ok(html.includes("a habit tracker with social accountability"));
  assert.ok(html.includes("confidence: medium"));
  assert.ok(html.includes("Worth a small MVP validation effort."));
  assert.ok(html.includes("React Native, FastAPI, Postgres."));
  assert.ok(html.includes("https://example.com/a"));
  assert.ok(html.includes("confidence-medium"));
});

test("renderAppFeasibilityTable shows the no-references note when none were used", () => {
  const assessments = [{
    id: "app_2", concept: "x", confidence_level: "low", summary: "y",
    reference_urls_used: JSON.stringify([]),
  }];
  const html = R.renderAppFeasibilityTable(assessments);
  assert.ok(html.includes("based on general knowledge only"));
});

test("renderAppFeasibilityTable never throws on malformed reference_urls_used JSON", () => {
  const assessments = [{ id: "app_3", concept: "x", confidence_level: "high", summary: "y",
                          reference_urls_used: "not valid json" }];
  const html = R.renderAppFeasibilityTable(assessments); // should not throw
  assert.ok(html.includes("based on general knowledge only"));
});

test("renderRadialGauge computes the correct dasharray ratio for a partial value", () => {
  // size=88, stroke=7 (defaults) -> r=40.5, circumference = 2*PI*40.5 ~= 254.47
  // value=25, max=100 -> 25% -> dash ~= 63.6
  const html = R.renderRadialGauge(25, 100, "test label");
  assert.ok(html.includes("25%"), "should show a rounded percentage as the default display value");
  assert.ok(html.includes("test label"));
  const dashMatch = html.match(/stroke-dasharray="([\d.]+) ([\d.]+)"/);
  assert.ok(dashMatch, "should render a stroke-dasharray attribute");
  const [, dash, circumference] = dashMatch.map(Number);
  assert.ok(Math.abs(dash / circumference - 0.25) < 0.01, "dash/circumference should be ~0.25");
});

test("renderRadialGauge clamps ratio to [0,1] and never divides by zero on a zero max", () => {
  const zeroMax = R.renderRadialGauge(5, 0, "x"); // should not throw, falls back to max=1
  assert.ok(zeroMax.includes("radial-gauge"));
  const overMax = R.renderRadialGauge(150, 100, "x");
  assert.ok(overMax.includes("100%"), "a value over max should clamp display to 100%, not overflow");
});

test("renderRadialGauge accepts a custom displayValue and color", () => {
  const html = R.renderRadialGauge(3, 6, "agents idle", { displayValue: "3/6", color: "var(--green)" });
  assert.ok(html.includes("3/6"));
  assert.ok(html.includes("var(--green)"));
});

test("statusColorVar maps known statuses to the right CSS var and falls back for unknown", () => {
  assert.strictEqual(R.statusColorVar("idle"), "var(--green)");
  assert.strictEqual(R.statusColorVar("working"), "var(--accent)");
  assert.strictEqual(R.statusColorVar("awaiting_approval"), "var(--amber)");
  assert.strictEqual(R.statusColorVar("failed"), "var(--red)");
  assert.strictEqual(R.statusColorVar("some_unknown_status"), "var(--muted)");
});

test("renderStatusBars handles the empty case", () => {
  assert.ok(R.renderStatusBars({}).includes("No data yet"));
  assert.ok(R.renderStatusBars(null).includes("No data yet"));
});

test("renderStatusBars renders a bar per nonzero status, sized relative to the max", () => {
  const html = R.renderStatusBars({ queued: 2, completed: 10, failed: 0 });
  assert.ok(html.includes("queued"));
  assert.ok(html.includes("completed"));
  assert.ok(!html.includes("failed"), "a zero-count status should be omitted, not shown as an empty bar");
  assert.ok(html.includes("width:100%"), "the max-count status (completed:10) should fill the full track");
  assert.ok(html.includes("width:20%"), "queued:2 relative to max 10 should be a 20% bar");
});

test("renderArcSummary includes a utilization gauge alongside the existing stat blocks", () => {
  const html = R.renderArcSummary({ allocation: 100, spend: 25 });
  assert.ok(html.includes("radial-gauge"));
  assert.ok(html.includes("25%"), "spend 25 / allocation 100 should render a 25% gauge");
  assert.ok(html.includes("100.0")); // existing Allocated stat still present
});

test("renderGlobalStats includes idle/completion gauges and a tasks-by-status bar chart", () => {
  const overview = {
    businesses: [{ id: "biz_1" }],
    agents_by_status: { idle: 2, working: 2 },
    tasks_by_status: { queued: 1, completed: 3 },
    real_revenue_usd_cents: 0,
  };
  const html = R.renderGlobalStats(overview);
  assert.ok(html.includes("radial-gauge"));
  assert.ok(html.includes("Tasks by status"));
  assert.ok(html.includes("queued"));
  assert.ok(html.includes("completed"));
});

test("renderSparkline shows a not-enough-history message for fewer than 2 points", () => {
  assert.ok(R.renderSparkline([]).includes("Not enough history"));
  assert.ok(R.renderSparkline([{ value: 100 }]).includes("Not enough history"));
  assert.ok(R.renderSparkline(null).includes("Not enough history"));
});

test("renderSparkline renders a polyline scaled to the value range, colored by trend", () => {
  const up = R.renderSparkline([{ value: 100 }, { value: 105 }, { value: 120 }]);
  assert.ok(up.includes("sparkline-line"));
  assert.ok(up.includes("var(--green)"), "an upward trend should default to green");
  const down = R.renderSparkline([{ value: 120 }, { value: 100 }]);
  assert.ok(down.includes("var(--red)"), "a downward trend should default to red");
});

test("renderSparkline never throws on a flat (zero-range) series", () => {
  const html = R.renderSparkline([{ value: 50 }, { value: 50 }, { value: 50 }]);
  assert.ok(html.includes("sparkline-line"));
});

test("renderTradingPortfolio includes an equity sparkline when history is present", () => {
  const view = {
    portfolio: { starting_cash_usd: 10000, cash_usd: 8000, created_at: "2026-01-01 00:00:00" },
    positions: [],
    latest_snapshot: { equity_usd: 10500, open_positions: 0, strategy_version: 1, created_at: "2026-01-02 00:00:00" },
    equity_history: [{ equity_usd: 10000 }, { equity_usd: 10200 }, { equity_usd: 10500 }],
  };
  const html = R.renderTradingPortfolio(view);
  assert.ok(html.includes("sparkline-line"));
  assert.ok(html.includes("Equity trend (last 3 snapshots)"));
});

test("renderTradingPortfolio degrades gracefully when equity_history is missing (older API shape)", () => {
  const view = {
    portfolio: { starting_cash_usd: 5000, cash_usd: 5000, created_at: "2026-01-01 00:00:00" },
    positions: [],
    latest_snapshot: null,
  };
  const html = R.renderTradingPortfolio(view); // should not throw
  assert.ok(html.includes("Not enough history"));
});

test("renderTradingPortfolio handles the no-portfolio-yet case", () => {
  const html = R.renderTradingPortfolio(null);
  assert.ok(html.includes("No paper trading portfolio yet"));
  assert.ok(html.includes("Enable Auto-Trading"));
});

test("renderTradingPortfolio shows cash/equity/P&L with a real snapshot", () => {
  const view = {
    portfolio: { starting_cash_usd: 10000, cash_usd: 8000, created_at: "2026-01-01 00:00:00" },
    positions: [{ symbol: "AAPL", quantity: 10, avg_cost_usd: 200 }],
    latest_snapshot: {
      equity_usd: 10500, open_positions: 1, strategy_version: 2,
      created_at: "2026-01-02 00:00:00",
    },
  };
  const html = R.renderTradingPortfolio(view);
  assert.ok(html.includes("$8000.00") || html.includes("$8,000.00") || html.includes("$8000"));
  assert.ok(html.includes("$10500.00") || html.includes("$10500"));
  assert.ok(html.includes("$500.00")); // total P&L = 10500 - 10000
  assert.ok(html.includes("strategy v2"));
});

test("renderTradingPortfolio falls back cleanly with no snapshot yet", () => {
  const view = {
    portfolio: { starting_cash_usd: 5000, cash_usd: 5000, created_at: "2026-01-01 00:00:00" },
    positions: [],
    latest_snapshot: null,
  };
  const html = R.renderTradingPortfolio(view);
  assert.ok(html.includes("No trading cycle has run yet"));
  assert.ok(html.includes("n/a"));
});

test("renderTradingPositions handles the empty case", () => {
  const html = R.renderTradingPositions([]);
  assert.ok(html.includes("No open positions"));
});

test("renderTradingPositions renders symbol/quantity/avg cost", () => {
  const html = R.renderTradingPositions([{ symbol: "NVDA", quantity: 3.5, avg_cost_usd: 120.25 }]);
  assert.ok(html.includes("NVDA"));
  assert.ok(html.includes("3.5000"));
  assert.ok(html.includes("$120.25"));
});

test("renderTradingTrades handles the empty case", () => {
  const html = R.renderTradingTrades([]);
  assert.ok(html.includes("No paper trades yet"));
});

test("renderTradingTrades shows realized P&L only for sell rows, and shows rationale/confidence", () => {
  const trades = [
    { created_at: "t1", side: "buy", symbol: "AAPL", quantity: 5, price_usd: 190,
      realized_pnl_usd: null, confidence_level: "medium", rationale: "Momentum looked solid." },
    { created_at: "t2", side: "sell", symbol: "AAPL", quantity: 5, price_usd: 200,
      realized_pnl_usd: 50, confidence_level: "high", rationale: "Hit target." },
  ];
  const html = R.renderTradingTrades(trades);
  assert.ok(html.includes("Momentum looked solid."));
  assert.ok(html.includes("Hit target."));
  assert.ok(html.includes("$50.00"));
  assert.ok(html.includes("—")); // the buy row's empty realized P&L placeholder
});

test("renderTradingTrades escapes rationale to prevent HTML injection", () => {
  const trades = [{ created_at: "t1", side: "buy", symbol: "AAPL", quantity: 1, price_usd: 1,
                     realized_pnl_usd: null, confidence_level: "low",
                     rationale: "<img src=x onerror=alert(1)>" }];
  const html = R.renderTradingTrades(trades);
  assert.ok(!html.includes("<img"));
});

test("renderTradingStrategyVersions handles the empty case", () => {
  const html = R.renderTradingStrategyVersions([]);
  assert.ok(html.includes("No strategy versions yet"));
});

test("renderTradingStrategyVersions shows parameters, active badge, and source", () => {
  const versions = [{
    version: 2, active: 1, source: "strategy_review", confidence_level: "medium",
    rationale: "Tightened position sizing after a losing streak.",
    parameters: JSON.stringify({
      watchlist: ["AAPL", "MSFT"], max_position_pct: 0.15, max_trade_pct_of_cash: 0.08,
      max_open_positions: 4, drawdown_halt_pct: 0.12, min_confidence_to_trade: "high",
    }),
  }];
  const html = R.renderTradingStrategyVersions(versions);
  assert.ok(html.includes("Version 2 (active)"));
  assert.ok(html.includes("strategy_review"));
  assert.ok(html.includes("Tightened position sizing"));
  assert.ok(html.includes("AAPL, MSFT"));
  assert.ok(html.includes("15%"));
  assert.ok(html.includes("12%"));
});

test("renderTradingStrategyVersions never throws on malformed parameters JSON", () => {
  const versions = [{ version: 1, active: 0, source: "system", parameters: "not valid json",
                       rationale: "init" }];
  const html = R.renderTradingStrategyVersions(versions); // should not throw
  assert.ok(html.includes("Version 1"));
});

test("renderGlobalStats sums agents/tasks across all businesses and formats real revenue", () => {
  const overview = {
    businesses: [{ id: "biz_1" }, { id: "biz_2" }],
    agents_by_status: { idle: 3, working: 2, paused: 1 },
    tasks_by_status: { queued: 2, assigned: 1, completed: 10, failed: 1 },
    real_revenue_usd_cents: 3800,
  };
  const html = R.renderGlobalStats(overview);
  assert.ok(html.includes(">2<")); // 2 businesses
  assert.ok(html.includes(">6<")); // 3+2+1 = 6 agents
  assert.ok(html.includes(">3<")); // 2 queued + 1 assigned = 3 open tasks (excludes completed/failed)
  assert.ok(html.includes("$38.00")); // 3800 cents
});

test("renderGlobalStats handles an empty system without throwing", () => {
  const html = R.renderGlobalStats({ businesses: [], agents_by_status: {}, tasks_by_status: {} });
  assert.ok(html.includes(">0<"));
  assert.ok(html.includes("$0.00"));
});

test("renderBusinessesOverviewTable handles the empty case", () => {
  const html = R.renderBusinessesOverviewTable([]);
  assert.ok(html.includes("No businesses yet"));
});

test("renderBusinessesOverviewTable shows a pending-approvals badge only when count > 0", () => {
  const businesses = [
    { id: "biz_1", name: "Has Approvals", type: "trading", status: "active",
      agent_count: 2, open_task_count: 1, pending_approval_count: 3,
      arc_summary: { earn: 50, spend: 10 } },
    { id: "biz_2", name: "No Approvals", type: "opportunity_discovery", status: "active",
      agent_count: 1, open_task_count: 0, pending_approval_count: 0,
      arc_summary: { earn: 5, spend: 0 } },
  ];
  const html = R.renderBusinessesOverviewTable(businesses);
  assert.ok(html.includes("Has Approvals"));
  assert.ok(html.includes("3 pending"));
  assert.ok(html.includes("No Approvals"));
  assert.ok(html.includes("—")); // the no-approvals placeholder
  assert.ok(html.includes("50.0 / 10.0"));
});

test("renderBusinessesOverviewTable escapes business name/type to prevent HTML injection", () => {
  const businesses = [{
    id: "biz_1", name: '<img src=x onerror=alert(1)>', type: "<script>evil()</script>",
    status: "active", agent_count: 0, open_task_count: 0, pending_approval_count: 0,
    arc_summary: {},
  }];
  const html = R.renderBusinessesOverviewTable(businesses);
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<script>evil"));
});

test("renderSystemCoreCenter shows OPERATIONAL with no pending approvals, an amber alert with some", () => {
  const quiet = R.renderSystemCoreCenter({
    businesses: [{ id: "biz_1", pending_approval_count: 0 }],
    agents_by_status: { idle: 1 }, tasks_by_status: {},
  });
  assert.ok(quiet.includes("OPERATIONAL"));
  assert.ok(!quiet.includes("core-hud-status-amber"));

  const alert = R.renderSystemCoreCenter({
    businesses: [{ id: "biz_1", pending_approval_count: 2 }],
    agents_by_status: {}, tasks_by_status: {},
  });
  assert.ok(alert.includes("AWAITING APPROVAL"));
  assert.ok(alert.includes("core-hud-status-amber"));
});

test("renderSystemCoreCenter includes the same business/agent/open-task counts as renderGlobalStats", () => {
  const overview = {
    businesses: [{ id: "biz_1" }, { id: "biz_2" }],
    agents_by_status: { idle: 3, working: 2 },
    tasks_by_status: { queued: 2, completed: 5 },
  };
  const center = R.renderSystemCoreCenter(overview);
  assert.ok(center.includes("2 BIZ"));
  assert.ok(center.includes("5 AGENTS"));
  assert.ok(center.includes("2 OPEN TASKS"));
});

test("renderSystemCoreSideStats sums ARC earn/spend and pending approvals across all businesses", () => {
  const overview = {
    businesses: [
      { id: "biz_1", arc_summary: { earn: 10, spend: 4 }, pending_approval_count: 1 },
      { id: "biz_2", arc_summary: { earn: 5, spend: 2.5 }, pending_approval_count: 0 },
    ],
  };
  const html = R.renderSystemCoreSideStats(overview);
  assert.ok(html.includes("15.0")); // 10 + 5 earned
  assert.ok(html.includes("6.5")); // 4 + 2.5 spent
  assert.ok(html.includes(">1<")); // 1 + 0 pending
});

test("renderSystemCoreSideStats handles businesses with no arc_summary yet without throwing", () => {
  const html = R.renderSystemCoreSideStats({ businesses: [{ id: "biz_1" }] });
  assert.ok(html.includes("0.0"));
});

test("computeOverviewCounts matches the shape renderGlobalStats relies on", () => {
  const counts = R.computeOverviewCounts({
    businesses: [{ id: "biz_1" }],
    agents_by_status: { idle: 2, working: 1 },
    tasks_by_status: { queued: 1, completed: 4 },
  });
  assert.strictEqual(counts.totalBusinesses, 1);
  assert.strictEqual(counts.totalAgents, 3);
  assert.strictEqual(counts.idleAgents, 2);
  assert.strictEqual(counts.openTasks, 1);
  assert.strictEqual(counts.totalTasks, 5);
});

console.log("\nAll dashboard-render.js tests finished.");
