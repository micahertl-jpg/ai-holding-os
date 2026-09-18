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

console.log("\nAll dashboard-render.js tests finished.");
