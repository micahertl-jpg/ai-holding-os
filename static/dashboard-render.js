/*
 * dashboard-render.js — pure functions that turn API JSON into HTML
 * strings. Deliberately has ZERO dependency on `document`/`window`/
 * `fetch`, so it can be required and unit-tested directly under plain
 * Node (see test_dashboard_render.js), not just eyeballed in a browser.
 * dashboard.js is the thin layer that actually calls these and assigns
 * the result to innerHTML.
 */
(function (root) {
  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtArc(n) {
    if (n === null || n === undefined) return "0.0";
    return Number(n).toFixed(1);
  }

  function fmtUsd(n) {
    if (n === null || n === undefined) return "$0.00";
    return "$" + Number(n).toFixed(2);
  }

  function renderAgentOptions(agents) {
    if (!agents || agents.length === 0) {
      return '<option value="">No agents yet — add one above</option>';
    }
    return agents
      .map(
        (a) =>
          `<option value="${escapeHtml(a.id)}">${escapeHtml(a.name)} (balance: ${fmtArc(
            a.arc_balance
          )})</option>`
      )
      .join("");
  }

  function renderBusinessOptions(businesses) {
    if (!businesses || businesses.length === 0) {
      return '<option value="">No businesses yet — create one below</option>';
    }
    return businesses
      .map(
        (b) =>
          `<option value="${escapeHtml(b.id)}">${escapeHtml(b.name)} (${escapeHtml(
            b.status
          )})</option>`
      )
      .join("");
  }

  function renderBusinessHeader(biz) {
    if (!biz) return "";
    return `
      <div class="biz-header">
        <h2>${escapeHtml(biz.name)} <span class="badge badge-${escapeHtml(biz.status)}">${escapeHtml(
      biz.status
    )}</span></h2>
        <p class="objective">${escapeHtml(biz.objective || "")}</p>
        <p class="budget">Authorized real-USD budget: ${fmtUsd(biz.budget_usd)}
          <span class="note">(not moved — no payment integration exists)</span></p>
      </div>`;
  }

  function renderAgentsTable(agents) {
    if (!agents || agents.length === 0) {
      return '<p class="empty">No agents yet.</p>';
    }
    const rows = agents
      .map(
        (a) => `
        <tr>
          <td>${escapeHtml(a.name)}</td>
          <td>${escapeHtml(a.role || "")}</td>
          <td>${escapeHtml(a.department || "")}</td>
          <td><span class="status status-${escapeHtml(a.status)}">${escapeHtml(a.status)}</span></td>
          <td>${escapeHtml(a.permission_level)}</td>
          <td>${fmtArc(a.arc_balance)}</td>
          <td>
            <button class="btn-small btn-pause" data-action="pause-agent" data-agent-id="${escapeHtml(a.id)}">Pause</button>
            <button class="btn-small btn-retire" data-action="retire-agent" data-agent-id="${escapeHtml(a.id)}">Retire</button>
          </td>
        </tr>`
      )
      .join("");
    return `
      <table class="data-table">
        <thead><tr><th>Name</th><th>Role</th><th>Dept</th><th>Status</th>
          <th>Perm</th><th>ARC</th><th>Actions</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderTasksTable(tasks) {
    if (!tasks || tasks.length === 0) {
      return '<p class="empty">No tasks yet.</p>';
    }
    const rows = tasks
      .map(
        (t) => `
        <tr>
          <td>${escapeHtml(t.objective)}</td>
          <td><span class="status status-${escapeHtml(t.status)}">${escapeHtml(t.status)}</span></td>
          <td>${escapeHtml(t.priority)}</td>
          <td>${fmtArc(t.cost_arc)}</td>
        </tr>`
      )
      .join("");
    return `
      <table class="data-table">
        <thead><tr><th>Objective</th><th>Status</th><th>Priority</th><th>Cost (ARC)</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderApprovalsList(approvals) {
    if (!approvals || approvals.length === 0) {
      return '<p class="empty">Nothing awaiting your approval.</p>';
    }
    return approvals
      .map(
        (p) => `
        <div class="approval-card risk-${escapeHtml(p.risk_level)}">
          <div class="approval-desc">
            <strong>[${escapeHtml(p.risk_level)}]</strong> ${escapeHtml(p.description)}
            ${p.amount_usd ? `<span class="amount">${fmtUsd(p.amount_usd)}</span>` : ""}
          </div>
          <div class="approval-actions">
            <button class="btn-approve" data-action="approve" data-approval-id="${escapeHtml(p.id)}">Approve</button>
            <button class="btn-reject" data-action="reject" data-approval-id="${escapeHtml(p.id)}">Reject</button>
          </div>
        </div>`
      )
      .join("");
  }

  function renderArcSummary(summary) {
    const allocation = fmtArc(summary && summary.allocation);
    const earn = fmtArc(summary && summary.earn);
    const spend = fmtArc(summary && summary.spend);
    const penalty = fmtArc(summary && summary.penalty);
    return `
      <div class="arc-summary">
        <div class="arc-stat"><span class="label">Allocated</span><span class="value">${allocation}</span></div>
        <div class="arc-stat"><span class="label">Earned</span><span class="value">${earn}</span></div>
        <div class="arc-stat"><span class="label">Spent</span><span class="value">${spend}</span></div>
        <div class="arc-stat"><span class="label">Penalties</span><span class="value">${penalty}</span></div>
      </div>`;
  }

  function renderJobsTable(jobs) {
    if (!jobs || jobs.length === 0) {
      return '<p class="empty">No scheduled jobs yet.</p>';
    }
    const rows = jobs
      .map((j) => {
        const enabled = j.enabled === 1 || j.enabled === true;
        return `
        <tr>
          <td>${escapeHtml(j.name)}</td>
          <td>${escapeHtml(j.objective)}</td>
          <td>${escapeHtml(j.interval_seconds)}s</td>
          <td>${escapeHtml(j.next_run_at || "")}</td>
          <td><span class="status ${enabled ? "status-idle" : "status-paused"}">${
          enabled ? "enabled" : "disabled"
        }</span></td>
          <td>
            <button class="btn-small btn-job-toggle ${enabled ? "btn-job-disable" : "btn-job-enable"}"
                    data-action="set-job-enabled"
                    data-job-id="${escapeHtml(j.id)}"
                    data-set-enabled="${enabled ? "false" : "true"}">
              ${enabled ? "Disable" : "Enable"}
            </button>
          </td>
        </tr>`;
      })
      .join("");
    return `
      <table class="data-table">
        <thead><tr><th>Name</th><th>Objective</th><th>Interval</th>
          <th>Next Run</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderOpportunitiesTable(opportunities) {
    if (!opportunities || opportunities.length === 0) {
      return '<p class="empty">No opportunities researched yet.</p>';
    }
    const cards = opportunities
      .map((o) => {
        const confidence = escapeHtml(o.confidence_level || "unknown");
        let urls = [];
        try {
          urls = o.reference_urls_used ? JSON.parse(o.reference_urls_used) : [];
        } catch (e) {
          urls = [];
        }
        return `
        <div class="opportunity-card confidence-${confidence}">
          <div class="opportunity-head">
            <strong>${escapeHtml(o.topic)}</strong>
            <span class="status status-${confidence === "high" ? "idle" : confidence === "medium" ? "awaiting_approval" : "failed"}">
              confidence: ${confidence}
            </span>
          </div>
          <p class="opportunity-summary">${escapeHtml(o.summary || "")}</p>
          <dl class="opportunity-fields">
            <dt>Market size</dt><dd>${escapeHtml(o.market_size || "")}</dd>
            <dt>Competition</dt><dd>${escapeHtml(o.competition || "")}</dd>
            <dt>Startup cost</dt><dd>${escapeHtml(o.startup_cost || "")}</dd>
            <dt>Revenue potential</dt><dd>${escapeHtml(o.revenue_potential || "")}</dd>
            <dt>Time to market</dt><dd>${escapeHtml(o.time_to_market || "")}</dd>
            <dt>Operational complexity</dt><dd>${escapeHtml(o.operational_complexity || "")}</dd>
            <dt>Legal/regulatory risk</dt><dd>${escapeHtml(o.legal_regulatory_risk || "")}</dd>
            <dt>Capital requirements</dt><dd>${escapeHtml(o.capital_requirements || "")}</dd>
            <dt>Downside risk</dt><dd>${escapeHtml(o.downside_risk || "")}</dd>
          </dl>
          ${urls.length > 0
            ? `<p class="opportunity-refs">References: ${urls.map((u) => escapeHtml(u)).join(", ")}</p>`
            : '<p class="opportunity-refs">No reference URLs fetched — based on general knowledge only.</p>'}
        </div>`;
      })
      .join("");
    return `<div class="opportunities-list">${cards}</div>`;
  }

  function renderRobloxTrendsTable(trends) {
    if (!trends || trends.length === 0) {
      return '<p class="empty">No Roblox concepts researched yet.</p>';
    }
    const cards = trends
      .map((t) => {
        const confidence = escapeHtml(t.confidence_level || "unknown");
        let urls = [];
        try {
          urls = t.reference_urls_used ? JSON.parse(t.reference_urls_used) : [];
        } catch (e) {
          urls = [];
        }
        return `
        <div class="opportunity-card confidence-${confidence}">
          <div class="opportunity-head">
            <strong>${escapeHtml(t.concept)}</strong>
            <span class="status status-${confidence === "high" ? "idle" : confidence === "medium" ? "awaiting_approval" : "failed"}">
              confidence: ${confidence}
            </span>
          </div>
          <p class="opportunity-summary">${escapeHtml(t.summary || "")}</p>
          <dl class="opportunity-fields">
            <dt>Player demand signals</dt><dd>${escapeHtml(t.player_demand_signals || "")}</dd>
            <dt>Competition level</dt><dd>${escapeHtml(t.competition_level || "")}</dd>
            <dt>Build complexity</dt><dd>${escapeHtml(t.build_complexity || "")}</dd>
            <dt>Target audience</dt><dd>${escapeHtml(t.target_audience || "")}</dd>
            <dt>Monetization fit</dt><dd>${escapeHtml(t.monetization_fit || "")}</dd>
            <dt>Estimated dev time</dt><dd>${escapeHtml(t.estimated_dev_time || "")}</dd>
            <dt>Similar successful games</dt><dd>${escapeHtml(t.similar_successful_games || "")}</dd>
            <dt>Risk factors</dt><dd>${escapeHtml(t.risk_factors || "")}</dd>
          </dl>
          ${urls.length > 0
            ? `<p class="opportunity-refs">References: ${urls.map((u) => escapeHtml(u)).join(", ")}</p>`
            : '<p class="opportunity-refs">No reference URLs fetched — based on general knowledge only.</p>'}
        </div>`;
      })
      .join("");
    return `<div class="opportunities-list">${cards}</div>`;
  }

  const api = {
    escapeHtml,
    fmtArc,
    fmtUsd,
    renderBusinessOptions,
    renderAgentOptions,
    renderBusinessHeader,
    renderAgentsTable,
    renderTasksTable,
    renderApprovalsList,
    renderArcSummary,
    renderJobsTable,
    renderOpportunitiesTable,
    renderRobloxTrendsTable,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.DashboardRender = api;
  }
})(typeof window !== "undefined" ? window : this);
