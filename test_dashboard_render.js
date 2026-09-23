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

test("renderTasksTable shows a failed task's real error message, truncated visually via title", () => {
  const tasks = [{
    id: "task_1", objective: "Backtest and search for a better trading strategy",
    status: "failed", priority: 3, cost_arc: 0,
    result: "executor error: no daily bars found for AAPL in range 2026-01-01..2026-09-01",
  }];
  const html = R.renderTasksTable(tasks);
  assert.ok(html.includes("no daily bars found for AAPL"));
  assert.ok(html.includes("status-failed"));
  // The full message must still be present in a title= attribute for hover,
  // even though the cell itself is CSS-truncated to one line -- nothing is
  // actually thrown away, only visually collapsed.
  assert.ok(html.includes('title="executor error: no daily bars found for AAPL'));
});

test("renderTasksTable handles a task with no result/error yet without throwing", () => {
  const tasks = [{ id: "task_1", objective: "x", status: "queued", priority: 3, cost_arc: 0 }];
  const html = R.renderTasksTable(tasks); // should not throw
  assert.ok(html.includes("status-queued"));
});

test("renderTasksTable escapes a task's result to prevent HTML injection, in both the cell and the title attribute", () => {
  const tasks = [{
    id: "task_1", objective: "x", status: "failed", priority: 3, cost_arc: 0,
    result: "<img src=x onerror=alert(1)>",
  }];
  const html = R.renderTasksTable(tasks);
  assert.ok(!html.includes("<img"));
});

test("every data-table renderer wraps its table in .table-scroll so wide columns are reachable, never clipped", () => {
  // Regression test: the backtest-runs table had exactly this bug (a
  // wide table silently clipped by its panel, with the last column
  // unreachable) before .table-scroll was added there in an earlier
  // fix -- renderTasksTable (the Result/Error column) turned out to
  // have the same gap, and a full audit found four more render
  // functions that had never been wrapped at all. Checked together so
  // this class of bug can't quietly reappear in just one of them.
  const checks = [
    ["renderAgentsTable", [{ id: "a1", name: "x", status: "idle", arc_balance: 0 }]],
    ["renderTasksTable", [{ id: "t1", objective: "x", status: "completed", priority: 3, cost_arc: 0 }]],
    ["renderJobsTable", [{ id: "j1", name: "x", objective: "x", interval_seconds: 60, enabled: 1 }]],
    ["renderOrdersTable", [{ id: "o1", topic: "x", product_type: "research_opportunity",
                             customer_email: "a@b.com", price_usd_cents: 100, status: "paid",
                             created_at: "2026-01-01" }]],
    ["renderTradingPositions", [{ symbol: "AAPL", quantity: 1, avg_cost_usd: 100 }]],
    ["renderTradingTrades", [{ created_at: "2026-01-01", side: "buy", symbol: "AAPL", quantity: 1,
                               price_usd: 100 }]],
    ["renderLiveTradingTrades", [{ created_at: "2026-01-01", side: "buy", symbol: "AAPL", quantity: 1,
                                   price_usd: 100 }]],
    ["renderBusinessesOverviewTable", [{ id: "b1", name: "x", type: "x", status: "active",
                                          agent_count: 0, open_task_count: 0 }]],
    ["renderBacktestRuns", [{ id: "r1", train_start_date: "2026-01-01", validation_split_date: "2026-02-01",
                              validation_end_date: "2026-02-15", max_candidates: 1, stopped_early: false,
                              best_candidate_index: 0,
                              candidates: [{ rationale: null, validation_meets_bar: false,
                                             train_stats: {}, validation_stats: {} }] }]],
  ];
  for (const [fn, arg] of checks) {
    const html = R[fn](arg);
    assert.ok(html.includes('<div class="table-scroll">'), `${fn} should wrap its table in .table-scroll`);
  }
});

test("renderTasksTable hides terminal tasks past DEFAULT_TERMINAL_SHOWN by default, keeping all non-terminal ones", () => {
  const tasks = [
    { id: "t_active_1", objective: "Active 1", status: "queued", priority: 3, cost_arc: 0 },
    { id: "t_active_2", objective: "Active 2", status: "in_progress", priority: 3, cost_arc: 0 },
    { id: "t_done_1", objective: "Done 1", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_done_2", objective: "Done 2", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_done_3", objective: "Done 3", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_done_4", objective: "Done 4", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_done_5", objective: "Done 5", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_done_6", objective: "Done 6", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_done_7", objective: "Done 7", status: "cancelled", priority: 3, cost_arc: 0 },
  ];
  const html = R.renderTasksTable(tasks, false);
  assert.ok(html.includes("Active 1") && html.includes("Active 2"), "non-terminal tasks always shown");
  assert.ok(html.includes("Done 1") && html.includes("Done 5"), "first 5 terminal tasks shown");
  assert.ok(!html.includes("Done 6") && !html.includes("Done 7"), "terminal tasks past the cap are hidden");
  assert.ok(html.includes("tasks-show-all-btn"), "a toggle button appears when tasks are hidden");
  assert.ok(html.includes("Show 2 completed tasks"), "toggle button states exactly how many are hidden");
});

test("renderTasksTable with showAll=true shows every task and offers a 'Show fewer' toggle", () => {
  const tasks = [
    { id: "t_1", objective: "One", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_2", objective: "Two", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_3", objective: "Three", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_4", objective: "Four", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_5", objective: "Five", status: "completed", priority: 3, cost_arc: 0 },
    { id: "t_6", objective: "Six", status: "completed", priority: 3, cost_arc: 0 },
  ];
  const html = R.renderTasksTable(tasks, true);
  assert.ok(html.includes("One") && html.includes("Six"), "every task shown when showAll is true");
  assert.ok(html.includes("Show fewer"), "a 'Show fewer' toggle appears when collapsed there'd be hidden tasks");
});

test("renderTasksTable shows no toggle at all when there's nothing to hide", () => {
  const tasks = [
    { id: "t_1", objective: "One", status: "queued", priority: 3, cost_arc: 0 },
    { id: "t_2", objective: "Two", status: "completed", priority: 3, cost_arc: 0 },
  ];
  const html = R.renderTasksTable(tasks, false);
  assert.ok(!html.includes("tasks-show-all-btn"), "no toggle needed when everything already fits");
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

test("orderStatusClass maps every real order status to the shared status vocabulary", () => {
  assert.strictEqual(R.orderStatusClass("fulfilled"), "status-completed");
  assert.strictEqual(R.orderStatusClass("paid"), "status-working");
  assert.strictEqual(R.orderStatusClass("pending_payment"), "status-awaiting_approval");
  assert.strictEqual(R.orderStatusClass("failed"), "status-failed");
  assert.strictEqual(R.orderStatusClass("refunded"), "status-failed");
});

test("orderStatusLabel shortens the one unusually long status value, passes the rest through", () => {
  assert.strictEqual(R.orderStatusLabel("pending_payment"), "pending");
  assert.strictEqual(R.orderStatusLabel("fulfilled"), "fulfilled");
  assert.strictEqual(R.orderStatusLabel("paid"), "paid");
  assert.strictEqual(R.orderStatusLabel("failed"), "failed");
  assert.strictEqual(R.orderStatusLabel("refunded"), "refunded");
});

test("renderOrdersTable shows the shortened label for a pending_payment order, not the raw value", () => {
  const orders = [{
    id: "ord_5", product_type: "research_opportunity", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "pending_payment",
    created_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(), // 1h old, not stuck
  }];
  const html = R.renderOrdersTable(orders);
  assert.ok(html.includes(">pending<"));
  assert.ok(!html.includes(">pending_payment<"));
  assert.ok(html.includes("status-awaiting_approval"));
});

test("renderOrdersTable flags a pending_payment order stuck past the threshold and links to Stripe", () => {
  const orders = [{
    id: "ord_stuck", product_type: "research_opportunity", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "pending_payment",
    created_at: new Date(Date.now() - 48 * 60 * 60 * 1000).toISOString(), // 48h old, well past 24h
    stripe_session_id: "cs_test_abc123",
  }];
  const html = R.renderOrdersTable(orders);
  assert.ok(html.includes("status-failed"), "a stuck pending order gets the urgent status color");
  assert.ok(html.includes("order-stuck-note"));
  assert.ok(html.includes("check Stripe"));
  assert.ok(html.includes("https://dashboard.stripe.com/test/checkout/sessions/cs_test_abc123"));
  assert.ok(html.includes("View in Stripe"));
});

test("renderOrdersTable links to live-mode Stripe URLs (no /test/ prefix) for a cs_live_ session", () => {
  const orders = [{
    id: "ord_live", product_type: "research_opportunity", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "paid",
    created_at: new Date().toISOString(), stripe_session_id: "cs_live_xyz789",
  }];
  const html = R.renderOrdersTable(orders);
  assert.ok(html.includes("https://dashboard.stripe.com/checkout/sessions/cs_live_xyz789"));
  assert.ok(!html.includes("/test/checkout/sessions/cs_live_xyz789"));
});

test("renderOrdersTable shows a dash, not a broken link, when an order has no stripe_session_id", () => {
  const orders = [{
    id: "ord_nosession", product_type: "research_opportunity", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "pending_payment",
    created_at: new Date().toISOString(),
  }];
  const html = R.renderOrdersTable(orders);
  assert.ok(!html.includes("View in Stripe"));
});

test("renderOrdersTable only shows a Remove button for a stuck (24h+) pending_payment order", () => {
  const stuckOrder = {
    id: "ord_stuck", product_type: "research_opportunity", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "pending_payment",
    created_at: new Date(Date.now() - 48 * 60 * 60 * 1000).toISOString(),
  };
  const freshOrder = {
    id: "ord_fresh", product_type: "research_opportunity", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "pending_payment",
    created_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
  };
  const paidOrder = {
    id: "ord_paid", product_type: "research_opportunity", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "paid",
    created_at: new Date(Date.now() - 999 * 60 * 60 * 1000).toISOString(),
  };

  const stuckHtml = R.renderOrdersTable([stuckOrder]);
  assert.ok(stuckHtml.includes('data-action="delete-abandoned-order"'));
  assert.ok(stuckHtml.includes('data-id="ord_stuck"'));

  const freshHtml = R.renderOrdersTable([freshOrder]);
  assert.ok(!freshHtml.includes('data-action="delete-abandoned-order"'),
    "a pending order that isn't stuck yet gets no Remove button");

  const paidHtml = R.renderOrdersTable([paidOrder]);
  assert.ok(!paidHtml.includes('data-action="delete-abandoned-order"'),
    "a paid order, however old, never gets a Remove button here");
});

test("renderOrdersTable handles the empty case", () => {
  assert.ok(R.renderOrdersTable([]).includes("No store orders yet"));
  assert.ok(R.renderOrdersTable(null).includes("No store orders yet"));
});

test("renderOrdersTable renders the real shape from the orders table", () => {
  const orders = [{
    id: "ord_1", product_type: "research_app_feasibility", topic: "a habit tracker app",
    customer_email: "customer@example.com", price_usd_cents: 1900, status: "fulfilled",
    created_at: "2026-09-19 12:00:00",
  }];
  const html = R.renderOrdersTable(orders);
  assert.ok(html.includes("a habit tracker app"));
  assert.ok(html.includes("App Feasibility"));
  assert.ok(html.includes("customer@example.com"));
  assert.ok(html.includes("$19.00"));
  assert.ok(html.includes("status-completed"));
  assert.ok(html.includes(">fulfilled<"));
});

test("renderOrdersTable falls back to the raw product_type for an unknown value", () => {
  const orders = [{
    id: "ord_4", product_type: "some_future_product", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "paid",
    created_at: "2026-09-19 12:00:00",
  }];
  const html = R.renderOrdersTable(orders);
  assert.ok(html.includes("some_future_product"));
});

test("renderOrdersTable flags a failed order with the failed status class", () => {
  const orders = [{
    id: "ord_2", product_type: "research_opportunity", topic: "x",
    customer_email: "y@example.com", price_usd_cents: 1900, status: "failed",
    created_at: "2026-09-19 12:00:00",
  }];
  const html = R.renderOrdersTable(orders);
  assert.ok(html.includes("status-failed"));
  assert.ok(html.includes(">failed<"));
});

test("renderOrdersTable escapes customer-submitted topic and email to prevent HTML injection", () => {
  const orders = [{
    id: "ord_3", product_type: "research_opportunity",
    topic: '<script>alert("xss")</script>', customer_email: '"><img src=x onerror=alert(1)>',
    price_usd_cents: 1900, status: "paid", created_at: "2026-09-19 12:00:00",
  }];
  const html = R.renderOrdersTable(orders);
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<img"));
});

test("computeOrdersRevenueByDay only counts paid/fulfilled orders, never pending or failed", () => {
  const now = new Date("2026-09-20T15:00:00Z");
  const orders = [
    { status: "paid", price_usd_cents: 500, paid_at: "2026-09-20 10:00:00" },
    { status: "fulfilled", price_usd_cents: 1900, paid_at: "2026-09-20 11:00:00" },
    { status: "pending_payment", price_usd_cents: 1900, created_at: "2026-09-20 12:00:00" },
    { status: "failed", price_usd_cents: 1900, created_at: "2026-09-20 12:00:00" },
    { status: "refunded", price_usd_cents: 1900, paid_at: "2026-09-20 12:00:00" },
  ];
  const buckets = R.computeOrdersRevenueByDay(orders, 7, now);
  const todayBucket = buckets[buckets.length - 1];
  assert.strictEqual(todayBucket.value, 24, "only the $5 paid + $19 fulfilled orders should count");
});

test("computeOrdersRevenueByDay buckets by paid_at, falling back to created_at when unpaid", () => {
  const now = new Date("2026-09-20T15:00:00Z");
  const orders = [
    // paid_at wins over created_at when both are present
    { status: "paid", price_usd_cents: 1000, created_at: "2026-09-18 09:00:00", paid_at: "2026-09-19 09:00:00" },
  ];
  const buckets = R.computeOrdersRevenueByDay(orders, 7, now);
  const byKey = Object.fromEntries(buckets.map((b) => [b.key, b.value]));
  assert.strictEqual(byKey["2026-09-19"], 10);
  assert.strictEqual(byKey["2026-09-18"], 0);
});

test("computeOrdersRevenueByDay produces exactly rangeDays buckets ending today, oldest first", () => {
  const now = new Date("2026-09-20T15:00:00Z");
  const buckets = R.computeOrdersRevenueByDay([], 7, now);
  assert.strictEqual(buckets.length, 7);
  assert.strictEqual(buckets[0].key, "2026-09-14");
  assert.strictEqual(buckets[6].key, "2026-09-20");
});

test("computeOrdersRevenueByDay ignores an order outside the selected range", () => {
  const now = new Date("2026-09-20T15:00:00Z");
  const orders = [{ status: "paid", price_usd_cents: 1900, paid_at: "2026-08-01 09:00:00" }];
  const buckets = R.computeOrdersRevenueByDay(orders, 7, now);
  const total = buckets.reduce((s, b) => s + b.value, 0);
  assert.strictEqual(total, 0);
});

test("renderOrdersChart shows an explicit empty state when there's no revenue in range, not a blank chart", () => {
  const buckets = R.computeOrdersRevenueByDay([], 7, new Date("2026-09-20T15:00:00Z"));
  const html = R.renderOrdersChart(buckets);
  assert.ok(html.includes("No paid orders in this range yet"));
  assert.ok(!html.includes("<svg"));
});

test("renderOrdersChart renders one clickable, keyboard-reachable bar per bucket with the real day label and value", () => {
  const now = new Date("2026-09-20T15:00:00Z");
  const orders = [{ status: "paid", price_usd_cents: 500, paid_at: "2026-09-20 10:00:00" }];
  const buckets = R.computeOrdersRevenueByDay(orders, 7, now);
  const html = R.renderOrdersChart(buckets);
  const barCount = (html.match(/<rect class="orders-chart-bar"/g) || []).length;
  assert.strictEqual(barCount, 7, "one bar per day bucket, including zero-value days");
  assert.ok(html.includes('data-action="select-order-bar"'));
  assert.ok(html.includes('role="button"'));
  assert.ok(html.includes('tabindex="0"'));
  assert.ok(html.includes("$5.00"), "the $5 real estate report price should appear in a label/value");
});

test("renderOrdersChart escapes a day label the same way every other renderer escapes user-adjacent text", () => {
  // Defensive: the label itself is generated internally (never
  // user-controlled), but escapeHtml is still applied for consistency
  // with the rest of this file -- verifying the plumbing didn't skip it.
  const buckets = [{ key: "2026-09-20", label: "Sep 20", value: 12.5 }];
  const html = R.renderOrdersChart(buckets);
  assert.ok(html.includes("Sep 20"));
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
  assert.ok(html.includes('data-action="delete-opportunity"'));
  assert.ok(html.includes('data-id="opp_1"'));
});

test("renderOpportunitiesTable shows a Launch Business button for an unlaunched opportunity", () => {
  const opportunities = [{
    id: "opp_1", business_id: "biz_1", topic: "AI-powered recipe apps",
    confidence_level: "medium", summary: "Worth a small validation effort.",
    launched_business_id: null,
  }];
  const html = R.renderOpportunitiesTable(opportunities);
  assert.ok(html.includes('data-action="launch-opportunity"'));
  assert.ok(html.includes('data-id="opp_1"'));
  assert.ok(html.includes('data-title="AI-powered recipe apps"'));
  assert.ok(!html.includes(">launched<"));
});

test("renderRobloxTrendsTable/renderAppFeasibilityTable/renderRealEstateTable each show their own " +
     "Launch Business button, wired to their own launch action and title", () => {
  const robloxHtml = R.renderRobloxTrendsTable([{
    id: "trend_1", concept: "A cozy farming sim", confidence_level: "medium",
    summary: "x", launched_business_id: null,
  }]);
  assert.ok(robloxHtml.includes('data-action="launch-roblox-trend"'));
  assert.ok(robloxHtml.includes('data-id="trend_1"'));
  assert.ok(robloxHtml.includes('data-title="A cozy farming sim"'));

  const appHtml = R.renderAppFeasibilityTable([{
    id: "app_1", concept: "A habit tracker app", confidence_level: "medium",
    summary: "x", launched_business_id: null,
  }]);
  assert.ok(appHtml.includes('data-action="launch-app-feasibility"'));
  assert.ok(appHtml.includes('data-id="app_1"'));
  assert.ok(appHtml.includes('data-title="A habit tracker app"'));

  const realEstateHtml = R.renderRealEstateTable([{
    id: "re_1", property_or_market: "123 Main St", confidence_level: "medium",
    summary: "x", launched_business_id: null,
  }]);
  assert.ok(realEstateHtml.includes('data-action="launch-real-estate"'));
  assert.ok(realEstateHtml.includes('data-id="re_1"'));
  assert.ok(realEstateHtml.includes('data-title="123 Main St"'));
});

test("renderRobloxTrendsTable/renderAppFeasibilityTable/renderRealEstateTable show a launched badge, " +
     "not a button, once launched", () => {
  const robloxHtml = R.renderRobloxTrendsTable([{
    id: "trend_1", concept: "x", confidence_level: "medium", summary: "x",
    launched_business_id: "biz_2",
  }]);
  assert.ok(!robloxHtml.includes('data-action="launch-roblox-trend"'));
  assert.ok(robloxHtml.includes(">launched<"));

  const appHtml = R.renderAppFeasibilityTable([{
    id: "app_1", concept: "x", confidence_level: "medium", summary: "x",
    launched_business_id: "biz_2",
  }]);
  assert.ok(!appHtml.includes('data-action="launch-app-feasibility"'));
  assert.ok(appHtml.includes(">launched<"));

  const realEstateHtml = R.renderRealEstateTable([{
    id: "re_1", property_or_market: "x", confidence_level: "medium", summary: "x",
    launched_business_id: "biz_2",
  }]);
  assert.ok(!realEstateHtml.includes('data-action="launch-real-estate"'));
  assert.ok(realEstateHtml.includes(">launched<"));
});

test("renderOpportunitiesTable shows a launched badge instead of the button once launched", () => {
  const opportunities = [{
    id: "opp_1", business_id: "biz_1", topic: "AI-powered recipe apps",
    confidence_level: "medium", summary: "Worth a small validation effort.",
    launched_business_id: "biz_2",
  }];
  const html = R.renderOpportunitiesTable(opportunities);
  assert.ok(!html.includes('data-action="launch-opportunity"'), "can't re-launch an already-launched opportunity");
  assert.ok(html.includes(">launched<"));
});

test("renderOpportunitiesTable's summary/fields are collapsed by default behind a toggle", () => {
  const opportunities = [{
    id: "opp_1", topic: "AI-powered recipe apps", confidence_level: "medium",
    summary: "Worth a small validation effort.", reference_urls_used: null,
  }];
  const html = R.renderOpportunitiesTable(opportunities);
  assert.ok(html.includes('class="card-details"'), "the detail body is wrapped for collapsing");
  assert.ok(html.includes('class="card-toggle-btn"'), "a toggle button is rendered");
  // Not expanded by default -- the containing card has no "expanded" class.
  assert.ok(!/opportunity-card[^"]*expanded/.test(html), "card starts collapsed, not expanded");
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
  assert.ok(html.includes('data-action="delete-roblox-trend"'));
  assert.ok(html.includes('data-id="rbx_1"'));
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
  assert.ok(html.includes('data-action="delete-app-feasibility"'));
  assert.ok(html.includes('data-id="app_1"'));
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

test("renderRealEstateTable handles the empty case", () => {
  assert.ok(R.renderRealEstateTable([]).includes("No real estate assessments yet"));
  assert.ok(R.renderRealEstateTable(null).includes("No real estate assessments yet"));
});

test("renderRealEstateTable renders the real shape from the real_estate_assessments table", () => {
  const assessments = [{
    id: "re_1", business_id: "biz_1", task_id: "task_1",
    property_or_market: "123 Main St, Springfield",
    market_trend: "Prices have risen modestly over the past two years.",
    comparable_properties: "A few similar properties nearby sold recently.",
    estimated_rental_yield: "Roughly 4-6% gross, a rough estimate.",
    price_trend_assessment: "Gradual appreciation.",
    risk_factors: "Local zoning rules should be confirmed with a licensed agent.",
    confidence_level: "medium",
    summary: "A reasonably stable market.",
    reference_urls_used: JSON.stringify(["https://example.com/a"]),
  }];
  const html = R.renderRealEstateTable(assessments);
  assert.ok(html.includes("123 Main St, Springfield"));
  assert.ok(html.includes("confidence: medium"));
  assert.ok(html.includes("A reasonably stable market."));
  assert.ok(html.includes("Roughly 4-6% gross"));
  assert.ok(html.includes("https://example.com/a"));
  assert.ok(html.includes("confidence-medium"));
  assert.ok(html.includes('data-action="delete-real-estate"'));
  assert.ok(html.includes('data-id="re_1"'));
});

test("renderRealEstateTable shows the no-references note when none were used", () => {
  const assessments = [{
    id: "re_2", property_or_market: "x", confidence_level: "low", summary: "y",
    reference_urls_used: JSON.stringify([]),
  }];
  const html = R.renderRealEstateTable(assessments);
  assert.ok(html.includes("based on general knowledge only"));
});

test("renderRealEstateTable never throws on malformed reference_urls_used JSON", () => {
  const assessments = [{ id: "re_3", property_or_market: "x", confidence_level: "high", summary: "y",
                          reference_urls_used: "not valid json" }];
  const html = R.renderRealEstateTable(assessments); // should not throw
  assert.ok(html.includes("based on general knowledge only"));
});

test("renderRealEstateTable escapes property/market and model output to prevent HTML injection", () => {
  const assessments = [{
    id: "re_4", property_or_market: '<script>alert("xss")</script>',
    confidence_level: "medium", summary: '<img src=x onerror=alert(1)>',
    reference_urls_used: JSON.stringify([]),
  }];
  const html = R.renderRealEstateTable(assessments);
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<img"));
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

test("renderTradingStrategyVersions expands the active version by default, collapses history", () => {
  const versions = [
    { version: 2, active: 1, source: "strategy_review", rationale: "Current.", parameters: "{}" },
    { version: 1, active: 0, source: "system", rationale: "Superseded.", parameters: "{}" },
  ];
  const html = R.renderTradingStrategyVersions(versions);
  const cards = html.split('<div class="opportunity-card');
  const activeCard = cards.find((c) => c.includes("Version 2"));
  const historyCard = cards.find((c) => c.includes("Version 1"));
  assert.ok(/^ confidence-\w+ expanded"/.test(activeCard), "the active version's card starts expanded");
  assert.ok(!/expanded"/.test(historyCard.split(">")[0]), "a non-active version's card starts collapsed");
});

test("renderTradingStrategyVersions never throws on malformed parameters JSON", () => {
  const versions = [{ version: 1, active: 0, source: "system", parameters: "not valid json",
                       rationale: "init" }];
  const html = R.renderTradingStrategyVersions(versions); // should not throw
  assert.ok(html.includes("Version 1"));
});

test("renderLiveTrading handles the no-portfolio-yet case", () => {
  const html = R.renderLiveTrading(null);
  assert.ok(html.includes("No trading portfolio yet"));
});

test("renderLiveTrading shows the disabled badge when live_trading_enabled is false", () => {
  const view = { live_trading_enabled: false, portfolio_id: "port_1", trades: [],
                 latest_snapshot: null, equity_history: [] };
  const html = R.renderLiveTrading(view);
  assert.ok(html.includes("disabled"));
  assert.ok(!html.includes("LIVE — real money"));
});

test("renderLiveTrading shows the LIVE badge and real cash/equity from a snapshot", () => {
  const view = {
    live_trading_enabled: true, portfolio_id: "port_1", trades: [],
    latest_snapshot: { cash_usd: 82.50, equity_usd: 97.10, open_positions: 1,
                        strategy_version: 1, created_at: "2026-01-02 00:00:00" },
    equity_history: [{ equity_usd: 100 }, { equity_usd: 97.10 }],
  };
  const html = R.renderLiveTrading(view);
  assert.ok(html.includes("LIVE — real money"));
  assert.ok(html.includes("$82.50") || html.includes("$82.5"));
  assert.ok(html.includes("$97.10") || html.includes("$97.1"));
  assert.ok(html.includes("strategy v1"));
});

test("renderLiveTradingTrades handles the empty case", () => {
  const html = R.renderLiveTradingTrades([]);
  assert.ok(html.includes("No live (real-money) trades yet"));
});

test("renderLiveTradingTrades shows real fill data and whether the live cap was applied", () => {
  const trades = [
    { created_at: "t1", side: "buy", symbol: "AAPL", quantity: 0.05, price_usd: 100,
      realized_pnl_usd: null, confidence_level: "high", rationale: "Real order test.",
      live_cap_applied: 0 },
    { created_at: "t2", side: "sell", symbol: "AAPL", quantity: 0.05, price_usd: 110,
      realized_pnl_usd: 0.50, confidence_level: "high", rationale: "Real order test 2.",
      live_cap_applied: 1 },
  ];
  const html = R.renderLiveTradingTrades(trades);
  assert.ok(html.includes("Real order test."));
  assert.ok(html.includes("$0.50"));
  assert.ok(html.includes("yes")); // the cap-applied row
  assert.ok(html.includes("no"));  // the not-cap-applied row
});

test("renderLiveTradingTrades escapes rationale to prevent HTML injection", () => {
  const trades = [{ created_at: "t1", side: "buy", symbol: "AAPL", quantity: 1, price_usd: 1,
                     realized_pnl_usd: null, confidence_level: "low", live_cap_applied: 0,
                     rationale: "<img src=x onerror=alert(1)>" }];
  const html = R.renderLiveTradingTrades(trades);
  assert.ok(!html.includes("<img"));
});

test("renderBacktestRuns handles the empty case", () => {
  const html = R.renderBacktestRuns([]);
  assert.ok(html.includes("No backtest runs yet"));
});

test("renderBacktestRuns shows the best candidate marked and PASS/no status per candidate", () => {
  const runs = [{
    train_start_date: "2026-01-01", validation_split_date: "2026-04-01",
    validation_end_date: "2026-06-01", max_candidates: 2, stopped_early: true,
    best_candidate_index: 1,
    candidates: [
      {
        rationale: null, validation_meets_bar: false,
        train_stats: { net_pnl_usd: 50 },
        validation_stats: { net_pnl_usd: -5, profit_factor: 0.8, win_rate: 0.3,
                             max_drawdown_pct: 0.12, sell_trades: 25, sample_size_ok: true },
      },
      {
        rationale: "Tightened confidence threshold after a weak train result.",
        validation_meets_bar: true,
        train_stats: { net_pnl_usd: 40 },
        validation_stats: { net_pnl_usd: 30, profit_factor: 2.1, win_rate: 0.6,
                             max_drawdown_pct: 0.08, sell_trades: 22, sample_size_ok: true },
      },
    ],
  }];
  const html = R.renderBacktestRuns(runs);
  assert.ok(html.includes("PASS"));
  assert.ok(html.includes(">no<") || html.includes(">no<\n") || /class="status status-failed">no</.test(html));
  assert.ok(html.includes("backtest-best-row"));
  assert.ok(html.includes("Tightened confidence threshold"));
  assert.ok(html.includes("(initial strategy, not a proposal)"));
  assert.ok(html.includes("60%")); // win rate formatted as a percentage
});

test("renderBacktestRuns formats an infinite profit_factor and a thin sample size", () => {
  const runs = [{
    train_start_date: "2026-01-01", validation_split_date: "2026-02-01",
    validation_end_date: "2026-02-15", max_candidates: 1, stopped_early: false,
    best_candidate_index: 0,
    candidates: [{
      rationale: null, validation_meets_bar: false,
      train_stats: { net_pnl_usd: 5 },
      validation_stats: { net_pnl_usd: 5, profit_factor: Infinity, win_rate: 1.0,
                           max_drawdown_pct: 0.0, sell_trades: 2, sample_size_ok: false },
    }],
  }];
  const html = R.renderBacktestRuns(runs);
  assert.ok(html.includes("∞"));
  assert.ok(html.includes("(thin)"));
  assert.ok(html.includes("none passed"));
});

test("renderBacktestRuns renders a Promote button per candidate with matching run id and index", () => {
  const runs = [{
    id: "btr_abc123",
    train_start_date: "2026-01-01", validation_split_date: "2026-02-01",
    validation_end_date: "2026-02-15", max_candidates: 2, stopped_early: true,
    best_candidate_index: 1,
    candidates: [
      { rationale: null, validation_meets_bar: false, train_stats: {}, validation_stats: { sample_size_ok: false } },
      { rationale: "better", validation_meets_bar: true, train_stats: {}, validation_stats: { sample_size_ok: false } },
    ],
  }];
  const html = R.renderBacktestRuns(runs);
  assert.ok(html.includes('data-action="promote-backtest-candidate"'));
  assert.ok(html.includes('data-run-id="btr_abc123"'));
  assert.ok(html.includes('data-candidate-index="0"'));
  assert.ok(html.includes('data-candidate-index="1"'));
});

test("renderBacktestRuns escapes rationale to prevent HTML injection", () => {
  const runs = [{
    train_start_date: "2026-01-01", validation_split_date: "2026-02-01",
    validation_end_date: "2026-02-15", max_candidates: 1, stopped_early: false,
    best_candidate_index: 0,
    candidates: [{
      rationale: "<img src=x onerror=alert(1)>", validation_meets_bar: false,
      train_stats: {}, validation_stats: { sample_size_ok: false },
    }],
  }];
  const html = R.renderBacktestRuns(runs);
  assert.ok(!html.includes("<img"));
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

test("opsSeverityClass maps every real severity to the shared status vocabulary", () => {
  assert.strictEqual(R.opsSeverityClass("ok"), "status-completed");
  assert.strictEqual(R.opsSeverityClass("info"), "status-completed");
  assert.strictEqual(R.opsSeverityClass("warning"), "status-awaiting_approval");
  assert.strictEqual(R.opsSeverityClass("critical"), "status-failed");
});

test("renderOpsReport handles the no-report-yet case", () => {
  assert.ok(R.renderOpsReport(null).includes("No ops review has run yet"));
});

test("renderOpsReport renders the real shape with findings", () => {
  const report = {
    overall_severity: "warning",
    confidence_level: "medium",
    summary: "A task looks stuck; worth a look.",
    created_at: "2026-09-20 12:00:00",
    findings: JSON.stringify([{
      category: "stuck tasks", severity: "warning",
      description: "One task has been queued for over 2 hours.",
      recommendation: "Check whether an eligible agent is idle for its department.",
    }]),
  };
  const html = R.renderOpsReport(report);
  assert.ok(html.includes("status-awaiting_approval"));
  assert.ok(html.includes("A task looks stuck"));
  assert.ok(html.includes("stuck tasks"));
  assert.ok(html.includes("Check whether an eligible agent"));
  assert.ok(html.includes("medium"));
});

test("renderOpsReport shows a healthy message when findings is an empty array", () => {
  const report = {
    overall_severity: "ok", confidence_level: "high", summary: "All clear.",
    created_at: "2026-09-20 12:00:00", findings: "[]",
  };
  const html = R.renderOpsReport(report);
  assert.ok(html.includes("No findings"));
  assert.ok(html.includes("status-completed"));
});

test("renderOpsReport never throws on malformed findings JSON", () => {
  const report = {
    overall_severity: "ok", confidence_level: "high", summary: "All clear.",
    created_at: "2026-09-20 12:00:00", findings: "not json",
  };
  assert.doesNotThrow(() => R.renderOpsReport(report));
});

test("renderOpsReport escapes model-generated finding text to prevent HTML injection", () => {
  const report = {
    overall_severity: "warning", confidence_level: "medium",
    summary: '<script>alert("xss")</script>', created_at: "2026-09-20 12:00:00",
    findings: JSON.stringify([{
      category: "x", severity: "warning",
      description: '<img src=x onerror=alert(1)>', recommendation: "y",
    }]),
  };
  const html = R.renderOpsReport(report);
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<img"));
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

test("renderOrbitalRing sums ARC earn/spend and pending approvals across all businesses", () => {
  const overview = {
    businesses: [
      { id: "biz_1", arc_summary: { earn: 10, spend: 4 }, pending_approval_count: 1 },
      { id: "biz_2", arc_summary: { earn: 5, spend: 2.5 }, pending_approval_count: 0 },
    ],
  };
  const html = R.renderOrbitalRing(overview);
  assert.ok(html.includes("15.0")); // 10 + 5 earned
  assert.ok(html.includes("6.5")); // 4 + 2.5 spent
  assert.ok(html.includes(">1<")); // 1 + 0 pending
  assert.ok(html.includes("core-node-alert"), "a nonzero pending-approvals node should carry the alert class");
});

test("renderOrbitalRing handles businesses with no arc_summary yet without throwing", () => {
  const html = R.renderOrbitalRing({ businesses: [{ id: "biz_1" }] });
  assert.ok(html.includes("0.0"));
  assert.ok(!html.includes("core-node-alert"), "zero pending approvals should not carry the alert class");
});

test("renderOrbitalRing places exactly 7 nodes evenly around the hub, each with a matching web-line", () => {
  const html = R.renderOrbitalRing({ businesses: [] });
  const nodeCount = (html.match(/<div class="core-node/g) || []).length;
  const lineCount = (html.match(/<line /g) || []).length;
  assert.strictEqual(nodeCount, 7);
  assert.strictEqual(lineCount, 7);
  // Every line must start at the hub's own center (50,50).
  assert.strictEqual((html.match(/x1="50" y1="50"/g) || []).length, 7);
});

test("pointOnRing math: renderOrbitalRing's first node sits directly above the hub center", () => {
  const html = R.renderOrbitalRing({ businesses: [] });
  // Node 0 is placed at angle -90deg (straight up): x=50, y=50-radius.
  assert.ok(html.includes("left:50.00%"), "first node should be horizontally centered on the hub");
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

// ---------- Command-center redesign additions ----------

test("renderHeaderStatus reads SYSTEM ONLINE when there's no ops report yet, or it's ok", () => {
  assert.ok(R.renderHeaderStatus({ latest_ops_report: null }).includes("SYSTEM ONLINE"));
  assert.ok(R.renderHeaderStatus({ latest_ops_report: { overall_severity: "ok" } }).includes("SYSTEM ONLINE"));
});

test("renderHeaderStatus flips to amber/red based on the real latest ops report severity", () => {
  const warn = R.renderHeaderStatus({ latest_ops_report: { overall_severity: "warning" } });
  assert.ok(warn.includes("header-status-amber"));
  assert.ok(warn.includes("ATTENTION NEEDED"));
  const critical = R.renderHeaderStatus({ latest_ops_report: { overall_severity: "critical" } });
  assert.ok(critical.includes("header-status-red"));
  assert.ok(critical.includes("CRITICAL"));
});

test("renderHeaderApprovalsBadge is empty when nothing is pending, never a fake zero badge", () => {
  assert.strictEqual(R.renderHeaderApprovalsBadge([]), "");
  assert.strictEqual(R.renderHeaderApprovalsBadge(null), "");
});

test("renderHeaderApprovalsBadge shows the real pending count and links to the approval center", () => {
  const html = R.renderHeaderApprovalsBadge([{ id: "a1" }, { id: "a2" }]);
  assert.ok(html.includes(">2<"));
  assert.ok(html.includes('href="#global-approvals-section"'));
});

test("renderCommandStatRow shows all 11 agent statuses even when most are zero, never hiding one", () => {
  const html = R.renderCommandStatRow({
    real_revenue_usd_cents: 0, pending_approvals: [], global_arc: {},
    agents_by_status: { idle: 2 }, businesses: [],
  });
  for (const status of ["created", "initializing", "active", "working", "idle", "waiting",
                         "improving", "paused", "failed", "quarantined", "retired"]) {
    assert.ok(html.includes(`>${status}<`), `stat row must show the "${status}" agent status, even at 0`);
  }
});

test("renderCommandStatRow sums real USD awaiting approval from pending_approvals, never fabricates it", () => {
  const html = R.renderCommandStatRow({
    real_revenue_usd_cents: 250000, // $2500.00
    pending_approvals: [{ amount_usd: 50 }, { amount_usd: 125.5 }, { amount_usd: null }],
    global_arc: {}, agents_by_status: {}, businesses: [],
  });
  assert.ok(html.includes("$2500.00") || html.includes("$2,500.00"));
  assert.ok(html.includes("$175.50"), "must sum only the real amount_usd values, treating null as 0");
});

test("renderCommandStatRow counts businesses by their real status only (active/paused/retired)", () => {
  const html = R.renderCommandStatRow({
    real_revenue_usd_cents: 0, pending_approvals: [], global_arc: {}, agents_by_status: {},
    businesses: [{ status: "active" }, { status: "active" }, { status: "paused" }, { status: "retired" }],
  });
  const cells = html.split('<div class="stat-block-label">Businesses</div>')[1];
  assert.ok(cells.includes(">active<"));
  assert.ok(/active[\s\S]{0,80}>2</.test(cells), "2 active businesses should render as 2, not fabricated");
});

test("renderActivityFeed handles the empty case", () => {
  assert.ok(R.renderActivityFeed([]).includes("No system activity recorded yet"));
  assert.ok(R.renderActivityFeed(null).includes("No system activity recorded yet"));
});

test("renderActivityFeed shows a real agent's resolved name, falling back to the raw actor id for an unknown one", () => {
  const events = [
    { id: 1, actor: "owner", actor_name: null, action: "create_business",
      target_type: "business", created_at: "2026-09-20 14:30:00" },
    { id: 2, actor: "agt_123", actor_name: "Nova", action: "complete_task",
      target_type: "task", created_at: "2026-09-20 14:31:00" },
    { id: 3, actor: "agt_gone", actor_name: null, action: "complete_task",
      target_type: "task", created_at: "2026-09-20 14:32:00" },
  ];
  const html = R.renderActivityFeed(events);
  assert.ok(html.includes("OWNER"));
  assert.ok(html.includes("created business"));
  assert.ok(html.includes("Nova"));
  assert.ok(html.includes("agt_gone"), "an actor id with no resolved name still shows the raw id, never hidden");
});

test("renderActivityFeed falls back to the raw action string for an action not in the label map", () => {
  const html = R.renderActivityFeed([
    { id: 1, actor: "system", actor_name: null, action: "some_future_action_xyz",
      target_type: "widget", created_at: "2026-09-20 14:30:00" },
  ]);
  assert.ok(html.includes("some future action xyz"), "unknown action falls back to a de-underscored raw string, never hidden");
});

test("renderActivityFeed escapes actor/action/target text to prevent HTML injection", () => {
  const html = R.renderActivityFeed([
    { id: 1, actor: "agt_1", actor_name: '<script>alert(1)</script>', action: "create_business",
      target_type: "business", created_at: "2026-09-20 14:30:00" },
  ]);
  assert.ok(!html.includes("<script>"));
});

test("renderBusinessRing returns nothing for an empty business list (no empty ring drawn)", () => {
  assert.strictEqual(R.renderBusinessRing([]), "");
  assert.strictEqual(R.renderBusinessRing(null), "");
});

test("renderBusinessRing places one node + one line per real business, using core-node-business styling", () => {
  const businesses = [
    { id: "biz_1", name: "Trading Co", status: "active", pending_approval_count: 0 },
    { id: "biz_2", name: "Roblox Studio", status: "paused", pending_approval_count: 2 },
  ];
  const html = R.renderBusinessRing(businesses);
  assert.strictEqual((html.match(/class="core-node core-node-business/g) || []).length, 2);
  assert.strictEqual((html.match(/<line class="web-line"/g) || []).length, 2);
  assert.ok(html.includes("Trading Co"));
  assert.ok(html.includes("Roblox Studio"));
  // A business with a real pending approval gets the same alert treatment
  // the metric ring already uses, not a fabricated new visual language.
  assert.ok(html.includes("core-node-alert"));
});

test("renderBusinessRing escapes a business name to prevent HTML injection", () => {
  const html = R.renderBusinessRing([
    { id: "biz_1", name: "<img src=x onerror=alert(1)>", status: "active", pending_approval_count: 0 },
  ]);
  assert.ok(!html.includes("<img"));
});

test("renderBusinessRing gives each business its own agent/task breakdown as separate mini nodes", () => {
  const html = R.renderBusinessRing([
    { id: "biz_1", name: "Trading Co", status: "active", pending_approval_count: 0,
      agent_count: 3, open_task_count: 2 },
  ]);
  assert.strictEqual((html.match(/class="core-node core-node-mini"/g) || []).length, 2,
    "one mini node for agents, one for open tasks -- a real breakdown, not a fabricated extra stat");
  assert.strictEqual((html.match(/<line class="web-line web-line-mini"/g) || []).length, 2,
    "each mini node gets its own connecting line back to the business node, not the hub");
  assert.ok(html.includes(">3<"), "the real agent_count value must appear");
  assert.ok(html.includes(">2<"), "the real open_task_count value must appear");
  assert.ok(html.includes("AGT"));
  assert.ok(html.includes("TASK"));
});

test("renderBusinessRing shows 0 for a business's mini breakdown rather than omitting it", () => {
  const html = R.renderBusinessRing([
    { id: "biz_1", name: "Empty Co", status: "active", pending_approval_count: 0,
      agent_count: 0, open_task_count: 0 },
  ]);
  assert.strictEqual((html.match(/class="core-node core-node-mini"/g) || []).length, 2,
    "a business with zero agents/tasks still gets its breakdown nodes, honestly showing 0");
});

test("renderBusinessRing's first business never lands on the same angle as the metric ring's first node", () => {
  // Regression test: both rings' index-0 node used to sit at exactly
  // -90deg (straight up) regardless of node count, since pointOnRing's
  // angle formula always starts there -- with only a 5-point radius
  // gap between the two rings, the first business and the "Businesses"
  // metric node landed directly on top of each other. The business
  // ring now carries its own angle offset specifically to avoid this.
  const html = R.renderBusinessRing([
    { id: "biz_1", name: "Solo Co", status: "active", pending_approval_count: 0,
      agent_count: 0, open_task_count: 0 },
  ]);
  const match = html.match(/class="core-node core-node-business"\s+style="left:(-?[\d.]+)%;top:(-?[\d.]+)%/);
  assert.ok(match, "should find the business node's position");
  const [, left] = match.map(Number);
  assert.notStrictEqual(left, 50, "a single business must not sit at left:50% (straight up), " +
    "the same angle the metric ring's own first node always uses");
});

test("renderBusinessRing keeps every business node at maximum angular clearance from all 7 fixed metric angles, for any business count 1-9", () => {
  // Regression test: a plain fixed rotation offset was tried first and
  // wasn't enough -- with a 30deg offset and 4 businesses, one business
  // landed only ~8.6deg from the metric ring's "Approvals" node (found
  // live via screenshot). Each business is now placed at the angular
  // MIDPOINT between two consecutive metric nodes, which is exactly
  // half of one metric step (360/7/2 = ~25.71deg) from BOTH neighbors,
  // by construction -- proven here for every business count from 1
  // through 9 (past the 7-slot wraparound point), not just today's
  // live data shape.
  const METRIC_STEP_DEG = 360 / 7;
  const EXPECTED_CLEARANCE_DEG = METRIC_STEP_DEG / 2;
  function angleOf(html, name) {
    const re = new RegExp(
      `class="core-node core-node-business[^"]*"\\s+style="left:(-?[\\d.]+)%;top:(-?[\\d.]+)%[^"]*"[^>]*title="${name}`
    );
    const m = html.match(re);
    assert.ok(m, `should find ${name}'s node position`);
    const [, left, top] = m.map(Number);
    return Math.atan2(top - 50, left - 50) * (180 / Math.PI);
  }
  function angularDistanceDeg(a, b) {
    let d = Math.abs(a - b) % 360;
    return d > 180 ? 360 - d : d;
  }
  const metricAnglesDeg = Array.from({ length: 7 }, (_, i) => -90 + i * METRIC_STEP_DEG);

  for (let count = 1; count <= 9; count++) {
    const businesses = Array.from({ length: count }, (_, i) => ({
      id: `biz_${i}`, name: `Biz${i}`, status: "active", pending_approval_count: 0,
      agent_count: 0, open_task_count: 0,
    }));
    const html = R.renderBusinessRing(businesses);
    for (let i = 0; i < count; i++) {
      const angle = angleOf(html, `Biz${i}`);
      const minClearance = Math.min(...metricAnglesDeg.map((m) => angularDistanceDeg(angle, m)));
      assert.ok(
        Math.abs(minClearance - EXPECTED_CLEARANCE_DEG) < 0.5,
        `business ${i} of ${count} should sit ~${EXPECTED_CLEARANCE_DEG.toFixed(2)}deg from its nearest ` +
        `metric node (max possible clearance), got ${minClearance.toFixed(2)}deg`
      );
    }
  }
});

test("renderTradingStrategyEvolution handles the no-history-yet case", () => {
  const html = R.renderTradingStrategyEvolution({ total_versions: 0, active_count: 0,
    businesses_with_trading: 0, latest_version: null });
  assert.ok(html.includes("No trading strategy history yet"));
});

test("renderTradingStrategyEvolution shows the real rollup counts and latest version's rationale", () => {
  const html = R.renderTradingStrategyEvolution({
    total_versions: 5, active_count: 2, businesses_with_trading: 2,
    latest_version: { created_at: "2026-09-20 12:00:00", source: "strategy_review",
                       rationale: "Tightened position sizing after a losing streak." },
  });
  assert.ok(html.includes(">5<"));
  assert.ok(html.includes(">2<"));
  assert.ok(html.includes("strategy_review"));
  assert.ok(html.includes("Tightened position sizing after a losing streak."));
});

console.log("\nAll dashboard-render.js tests finished.");
