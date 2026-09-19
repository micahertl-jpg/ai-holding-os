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

  // Same four-way color mapping the .status-* CSS classes use, exposed
  // as a JS helper so instrument widgets (gauges, bar charts) can pick
  // a matching fill color for a given status/entry_type string.
  function statusColorVar(status) {
    const s = String(status || "").toLowerCase();
    if (["idle", "completed", "active", "earn"].includes(s)) return "var(--green)";
    if (["working", "assigned", "queued"].includes(s)) return "var(--accent)";
    if (["awaiting_approval", "penalty"].includes(s)) return "var(--amber)";
    if (["failed", "paused", "retired", "cancelled", "spend"].includes(s)) return "var(--red)";
    return "var(--muted)";
  }

  // A single instrument-panel radial gauge (SVG ring), matching the
  // dial-style readouts on a HUD control panel. Pure function of
  // value/max — no DOM/animation state, so it's trivial to unit test
  // and re-renders cleanly every dashboard refresh.
  function renderRadialGauge(value, max, label, opts) {
    opts = opts || {};
    const size = opts.size || 88;
    const stroke = opts.stroke || 7;
    const r = (size - stroke) / 2;
    const circumference = 2 * Math.PI * r;
    const safeMax = max && max > 0 ? max : 1;
    const ratio = Math.max(0, Math.min(1, (Number(value) || 0) / safeMax));
    const dash = (circumference * ratio).toFixed(1);
    const color = opts.color || "var(--accent)";
    const displayValue =
      opts.displayValue !== undefined ? opts.displayValue : Math.round(ratio * 100) + "%";
    return `
      <div class="radial-gauge" style="width:${size}px;height:${size}px;" title="${escapeHtml(
        label || ""
      )}">
        <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">
          <circle class="radial-gauge-track" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" />
          <circle class="radial-gauge-fill" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none"
            stroke="${color}" stroke-dasharray="${dash} ${circumference.toFixed(1)}"
            transform="rotate(-90 ${size / 2} ${size / 2})" />
        </svg>
        <div class="radial-gauge-label">
          <span class="radial-gauge-value">${escapeHtml(displayValue)}</span>
          ${label ? `<span class="radial-gauge-sub">${escapeHtml(label)}</span>` : ""}
        </div>
      </div>`;
  }

  // A small horizontal bar-chart instrument, e.g. "tasks by status" or
  // "agents by status" — takes a {status: count} map (the exact shape
  // GET /overview already returns) and needs no extra aggregation.
  function renderStatusBars(counts) {
    const entries = Object.entries(counts || {}).filter(([, c]) => c > 0);
    if (entries.length === 0) {
      return '<p class="empty">No data yet.</p>';
    }
    const max = Math.max(...entries.map(([, c]) => c));
    const rows = entries
      .map(([status, count]) => {
        const pct = max > 0 ? Math.round((count / max) * 100) : 0;
        return `
        <div class="stat-bar-row">
          <span class="stat-bar-label">${escapeHtml(status)}</span>
          <div class="stat-bar-track">
            <div class="stat-bar-fill" style="width:${pct}%; background:${statusColorVar(
              status
            )};"></div>
          </div>
          <span class="stat-bar-count">${escapeHtml(count)}</span>
        </div>`;
      })
      .join("");
    return `<div class="stat-bars">${rows}</div>`;
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
    const allocationNum = Number((summary && summary.allocation) || 0);
    const spendNum = Number((summary && summary.spend) || 0);
    const allocation = fmtArc(allocationNum);
    const earn = fmtArc(summary && summary.earn);
    const spend = fmtArc(spendNum);
    const penalty = fmtArc(summary && summary.penalty);
    const gauge = renderRadialGauge(spendNum, allocationNum || spendNum || 1, "ARC utilized", {
      color: "var(--accent)",
    });
    return `
      <div class="instrument-row">
        ${gauge}
        <div class="arc-summary">
          <div class="arc-stat"><span class="label">Allocated</span><span class="value">${allocation}</span></div>
          <div class="arc-stat"><span class="label">Earned</span><span class="value">${earn}</span></div>
          <div class="arc-stat"><span class="label">Spent</span><span class="value">${spend}</span></div>
          <div class="arc-stat"><span class="label">Penalties</span><span class="value">${penalty}</span></div>
        </div>
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

  function renderAppFeasibilityTable(assessments) {
    if (!assessments || assessments.length === 0) {
      return '<p class="empty">No app feasibility assessments yet.</p>';
    }
    const cards = assessments
      .map((a) => {
        const confidence = escapeHtml(a.confidence_level || "unknown");
        let urls = [];
        try {
          urls = a.reference_urls_used ? JSON.parse(a.reference_urls_used) : [];
        } catch (e) {
          urls = [];
        }
        return `
        <div class="opportunity-card confidence-${confidence}">
          <div class="opportunity-head">
            <strong>${escapeHtml(a.concept)}</strong>
            <span class="status status-${confidence === "high" ? "idle" : confidence === "medium" ? "awaiting_approval" : "failed"}">
              confidence: ${confidence}
            </span>
          </div>
          <p class="opportunity-summary">${escapeHtml(a.summary || "")}</p>
          <dl class="opportunity-fields">
            <dt>Platform recommendation</dt><dd>${escapeHtml(a.platform_recommendation || "")}</dd>
            <dt>Suggested tech stack</dt><dd>${escapeHtml(a.suggested_tech_stack || "")}</dd>
            <dt>Complexity tier</dt><dd>${escapeHtml(a.complexity_tier || "")}</dd>
            <dt>Estimated timeline</dt><dd>${escapeHtml(a.estimated_timeline || "")}</dd>
            <dt>Estimated cost range</dt><dd>${escapeHtml(a.estimated_cost_range || "")}</dd>
            <dt>MVP feature scope</dt><dd>${escapeHtml(a.mvp_feature_scope || "")}</dd>
            <dt>Key technical risks</dt><dd>${escapeHtml(a.key_technical_risks || "")}</dd>
            <dt>Similar existing apps</dt><dd>${escapeHtml(a.similar_existing_apps || "")}</dd>
          </dl>
          ${urls.length > 0
            ? `<p class="opportunity-refs">References: ${urls.map((u) => escapeHtml(u)).join(", ")}</p>`
            : '<p class="opportunity-refs">No reference URLs fetched — based on general knowledge only.</p>'}
        </div>`;
      })
      .join("");
    return `<div class="opportunities-list">${cards}</div>`;
  }

  // A minimal line+area sparkline instrument, e.g. for an equity curve.
  // `points` is [{value}, ...] in chronological (oldest-first) order —
  // exactly the shape GET .../trading/portfolio's equity_history
  // returns. Real recorded history only; never interpolated/fabricated
  // points, and it says plainly when there isn't enough history yet
  // rather than drawing a flat or misleading line.
  function renderSparkline(points, opts) {
    opts = opts || {};
    const width = opts.width || 220;
    const height = opts.height || 46;
    if (!points || points.length < 2) {
      return '<p class="empty sparkline-empty">Not enough history yet for a trend line.</p>';
    }
    const values = points.map((p) => Number(p.value));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const stepX = width / (values.length - 1);
    const coords = values.map((v, i) => [
      (i * stepX).toFixed(1),
      (height - ((v - min) / range) * (height - 6) - 3).toFixed(1),
    ]);
    const linePoints = coords.map(([x, y]) => `${x},${y}`).join(" ");
    const areaPoints = `0,${height} ${linePoints} ${width},${height}`;
    const trendUp = values[values.length - 1] >= values[0];
    const color = opts.color || (trendUp ? "var(--green)" : "var(--red)");
    return `
      <div class="sparkline">
        <svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" preserveAspectRatio="none">
          <polygon class="sparkline-area" points="${areaPoints}" style="fill:${color}" />
          <polyline class="sparkline-line" points="${linePoints}" style="stroke:${color}" fill="none" />
        </svg>
      </div>`;
  }

  function renderTradingPortfolio(view) {
    if (!view) {
      return '<p class="empty">No paper trading portfolio yet — create one below, or use ' +
        '"Enable Auto-Trading" to set everything up (portfolio + agent + schedule) in one step.</p>';
    }
    const p = view.portfolio;
    const snap = view.latest_snapshot;
    const pnl = snap ? snap.equity_usd - p.starting_cash_usd : null;
    const history = view.equity_history || [];
    const sparkline = renderSparkline(history.map((h) => ({ value: h.equity_usd })));
    return `
      <div class="arc-summary">
        <div class="arc-stat"><span class="label">Cash</span><span class="value">${fmtUsd(p.cash_usd)}</span></div>
        <div class="arc-stat"><span class="label">Equity (last cycle)</span><span class="value">${
          snap ? fmtUsd(snap.equity_usd) : "n/a"
        }</span></div>
        <div class="arc-stat"><span class="label">Total P&amp;L</span><span class="value">${
          pnl !== null ? fmtUsd(pnl) : "n/a"
        }</span></div>
        <div class="arc-stat"><span class="label">Open positions</span><span class="value">${
          snap ? escapeHtml(snap.open_positions) : view.positions.length
        }</span></div>
      </div>
      <div class="sparkline-block">
        <div class="stat-bars-title">Equity trend (last ${history.length} snapshots)</div>
        ${sparkline}
      </div>
      <p class="panel-note">Started with ${fmtUsd(p.starting_cash_usd)} in simulated cash on
        ${escapeHtml(p.created_at)}.${
          snap ? ` Last snapshot: ${escapeHtml(snap.created_at)} (strategy v${escapeHtml(snap.strategy_version)}).`
               : " No trading cycle has run yet."
        }</p>`;
  }

  function renderTradingPositions(positions) {
    if (!positions || positions.length === 0) {
      return '<p class="empty">No open positions.</p>';
    }
    const rows = positions
      .map(
        (p) => `
        <tr>
          <td>${escapeHtml(p.symbol)}</td>
          <td>${Number(p.quantity).toFixed(4)}</td>
          <td>${fmtUsd(p.avg_cost_usd)}</td>
        </tr>`
      )
      .join("");
    return `
      <table class="data-table">
        <thead><tr><th>Symbol</th><th>Quantity</th><th>Avg Cost</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderTradingTrades(trades) {
    if (!trades || trades.length === 0) {
      return '<p class="empty">No paper trades yet.</p>';
    }
    const rows = trades
      .map((t) => {
        const hasPnl = t.realized_pnl_usd !== null && t.realized_pnl_usd !== undefined;
        return `
        <tr>
          <td>${escapeHtml(t.created_at)}</td>
          <td><span class="status ${t.side === "buy" ? "status-idle" : "status-working"}">${escapeHtml(t.side)}</span></td>
          <td>${escapeHtml(t.symbol)}</td>
          <td>${Number(t.quantity).toFixed(4)}</td>
          <td>${fmtUsd(t.price_usd)}</td>
          <td>${hasPnl ? fmtUsd(t.realized_pnl_usd) : "—"}</td>
          <td>${escapeHtml(t.confidence_level || "")}</td>
          <td>${escapeHtml(t.rationale || "")}</td>
        </tr>`;
      })
      .join("");
    return `
      <table class="data-table">
        <thead><tr><th>When</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th>
          <th>Realized P&amp;L</th><th>Confidence</th><th>Rationale</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderTradingStrategyVersions(versions) {
    if (!versions || versions.length === 0) {
      return '<p class="empty">No strategy versions yet.</p>';
    }
    const cards = versions
      .map((v) => {
        let params = {};
        try {
          params = v.parameters ? JSON.parse(v.parameters) : {};
        } catch (e) {
          params = {};
        }
        const confidence = escapeHtml(v.confidence_level || "unknown");
        const watchlist = (params.watchlist || []).map((s) => escapeHtml(s)).join(", ");
        return `
        <div class="opportunity-card confidence-${confidence}">
          <div class="opportunity-head">
            <strong>Version ${escapeHtml(v.version)}${v.active ? " (active)" : ""}</strong>
            <span class="status status-${v.source === "owner_override" ? "awaiting_approval" : "idle"}">${escapeHtml(v.source)}</span>
          </div>
          <p class="opportunity-summary">${escapeHtml(v.rationale || "")}</p>
          <dl class="opportunity-fields">
            <dt>Watchlist</dt><dd>${watchlist}</dd>
            <dt>Max position %</dt><dd>${params.max_position_pct != null ? (params.max_position_pct * 100).toFixed(0) + "%" : ""}</dd>
            <dt>Max trade % of cash</dt><dd>${params.max_trade_pct_of_cash != null ? (params.max_trade_pct_of_cash * 100).toFixed(0) + "%" : ""}</dd>
            <dt>Max open positions</dt><dd>${escapeHtml(params.max_open_positions)}</dd>
            <dt>Drawdown halt</dt><dd>${params.drawdown_halt_pct != null ? (params.drawdown_halt_pct * 100).toFixed(0) + "%" : ""}</dd>
            <dt>Min confidence to trade</dt><dd>${escapeHtml(params.min_confidence_to_trade)}</dd>
          </dl>
        </div>`;
      })
      .join("");
    return `<div class="opportunities-list">${cards}</div>`;
  }

  // Shared by renderGlobalStats and the System Core hero readouts so the
  // two panels can never silently disagree on what "open tasks" or
  // "total agents" means.
  function computeOverviewCounts(overview) {
    const totalBusinesses = (overview.businesses || []).length;
    const agentsByStatus = overview.agents_by_status || {};
    const totalAgents = Object.values(agentsByStatus).reduce((a, b) => a + b, 0);
    const idleAgents = agentsByStatus.idle || 0;
    const tasksByStatus = overview.tasks_by_status || {};
    const terminal = new Set(["completed", "failed", "cancelled"]);
    const openTasks = Object.entries(tasksByStatus)
      .filter(([status]) => !terminal.has(status))
      .reduce((sum, [, c]) => sum + c, 0);
    const totalTasks = Object.values(tasksByStatus).reduce((a, b) => a + b, 0);
    return { totalBusinesses, totalAgents, idleAgents, tasksByStatus, openTasks, totalTasks };
  }

  // The big central "System Core" HUD's live-text overlay: a plain
  // status line (flips to an amber alert the moment anything is
  // actually waiting on the owner) plus a compact business/agent/task
  // readout, both real, both already computed by computeOverviewCounts.
  function renderSystemCoreCenter(overview) {
    const counts = computeOverviewCounts(overview);
    const pendingApprovals = (overview.businesses || []).reduce(
      (sum, b) => sum + Number(b.pending_approval_count || 0), 0
    );
    const alert = pendingApprovals > 0;
    return `
      <div class="core-hud-status${alert ? " core-hud-status-amber" : ""}">${
        alert ? "AWAITING APPROVAL" : "OPERATIONAL"
      }</div>
      <div class="core-hud-readout">${escapeHtml(counts.totalBusinesses)} BIZ &middot; ${escapeHtml(
        counts.totalAgents
      )} AGENTS<br>${escapeHtml(counts.openTasks)} OPEN TASKS</div>`;
  }

  // The System Core hero panel's side readouts: system-wide ARC totals
  // summed across every business's own arc_summary (GET /overview
  // already includes one arc_summary per business) -- a genuinely new
  // aggregate, not shown anywhere else in the dashboard.
  // Places `n` items evenly around a circle centered at (50,50) in a
  // 0-100 percentage coordinate space, starting at the top (12 o'clock)
  // and going clockwise -- shared math for both the orbital node badges
  // and the web-lines connecting them back to the hub, so the two can
  // never drift out of sync with each other.
  function pointOnRing(index, count, radiusPct) {
    const angle = (index / count) * 2 * Math.PI - Math.PI / 2;
    return {
      x: 50 + Math.cos(angle) * radiusPct,
      y: 50 + Math.sin(angle) * radiusPct,
    };
  }

  const ORBITAL_RING_RADIUS_PCT = 42;

  // The System Core hub's orbiting stat nodes + the animated web-lines
  // connecting them to the center -- real system-wide figures (same
  // computeOverviewCounts + per-business arc_summary aggregation the
  // old flat side-stats list used), just laid out radially instead of
  // as a list, to match the "web-like, centered on the globe" hub
  // design. Returns one HTML fragment containing both the <line>s and
  // the node <div>s, since they're positioned from the exact same data
  // and always redrawn together.
  function renderOrbitalRing(overview) {
    const counts = computeOverviewCounts(overview);
    const businesses = overview.businesses || [];
    const totalEarn = businesses.reduce(
      (sum, b) => sum + Number((b.arc_summary && b.arc_summary.earn) || 0), 0
    );
    const totalSpend = businesses.reduce(
      (sum, b) => sum + Number((b.arc_summary && b.arc_summary.spend) || 0), 0
    );
    const pendingApprovals = businesses.reduce(
      (sum, b) => sum + Number(b.pending_approval_count || 0), 0
    );
    const revenue = (overview.real_revenue_usd_cents || 0) / 100;

    const nodes = [
      { label: "Businesses", value: String(counts.totalBusinesses) },
      { label: "Agents", value: String(counts.totalAgents) },
      { label: "Open Tasks", value: String(counts.openTasks) },
      { label: "Revenue", value: fmtUsd(revenue) },
      { label: "ARC Earned", value: fmtArc(totalEarn) },
      { label: "ARC Spent", value: fmtArc(totalSpend) },
      { label: "Approvals", value: String(pendingApprovals), alert: pendingApprovals > 0 },
    ];

    const positioned = nodes.map((node, i) => Object.assign({}, node, pointOnRing(i, nodes.length, ORBITAL_RING_RADIUS_PCT)));

    const lines = positioned
      .map(
        (p, i) => `
        <line class="web-line" x1="50" y1="50" x2="${p.x.toFixed(2)}" y2="${p.y.toFixed(2)}"
          style="animation-delay:${(i * 0.15).toFixed(2)}s" />`
      )
      .join("");

    const nodeDivs = positioned
      .map(
        (p, i) => `
        <div class="core-node${p.alert ? " core-node-alert" : ""}"
          style="left:${p.x.toFixed(2)}%;top:${p.y.toFixed(2)}%;animation-delay:${(i * 0.08).toFixed(2)}s">
          <span class="core-node-value">${escapeHtml(p.value)}</span>
          <span class="core-node-label">${escapeHtml(p.label)}</span>
        </div>`
      )
      .join("");

    return `
      <svg class="core-web-lines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">${lines}</svg>
      ${nodeDivs}`;
  }

  function renderGlobalStats(overview) {
    const counts = computeOverviewCounts(overview);
    const totalBusinesses = counts.totalBusinesses;
    const totalAgents = counts.totalAgents;
    const idleAgents = counts.idleAgents;
    const openTasks = counts.openTasks;
    const totalTasks = counts.totalTasks;
    const tasksByStatus = counts.tasksByStatus;
    const revenue = (overview.real_revenue_usd_cents || 0) / 100;
    const idleGauge = renderRadialGauge(idleAgents, totalAgents || 1, "agents idle", {
      color: "var(--green)",
    });
    const completionGauge = renderRadialGauge(totalTasks - openTasks, totalTasks || 1, "tasks done", {
      color: "var(--accent)",
    });
    return `
      <div class="arc-summary">
        <div class="arc-stat"><span class="label">Businesses</span><span class="value">${totalBusinesses}</span></div>
        <div class="arc-stat"><span class="label">Agents</span><span class="value">${totalAgents}</span></div>
        <div class="arc-stat"><span class="label">Open Tasks</span><span class="value">${openTasks}</span></div>
        <div class="arc-stat"><span class="label">Real Revenue Collected</span><span class="value">${fmtUsd(revenue)}</span></div>
      </div>
      <div class="instrument-row instrument-row-tight">
        ${idleGauge}
        ${completionGauge}
        <div class="stat-bars-block">
          <div class="stat-bars-title">Tasks by status</div>
          ${renderStatusBars(tasksByStatus)}
        </div>
      </div>`;
  }

  function renderBusinessesOverviewTable(businesses) {
    if (!businesses || businesses.length === 0) {
      return '<p class="empty">No businesses yet — create one below.</p>';
    }
    const rows = businesses
      .map((b) => {
        const arc = b.arc_summary || {};
        const approvalsCell =
          b.pending_approval_count > 0
            ? `<span class="status status-awaiting_approval">${escapeHtml(b.pending_approval_count)} pending</span>`
            : "—";
        return `
        <tr>
          <td>${escapeHtml(b.name)}</td>
          <td>${escapeHtml(b.type || "")}</td>
          <td><span class="badge badge-${escapeHtml(b.status)}">${escapeHtml(b.status)}</span></td>
          <td>${escapeHtml(b.agent_count)}</td>
          <td>${escapeHtml(b.open_task_count)}</td>
          <td>${approvalsCell}</td>
          <td>${fmtArc(arc.earn)} / ${fmtArc(arc.spend)}</td>
        </tr>`;
      })
      .join("");
    return `
      <table class="data-table">
        <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Agents</th>
          <th>Open Tasks</th><th>Approvals</th><th>ARC Earned / Spent</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  const api = {
    escapeHtml,
    fmtArc,
    fmtUsd,
    statusColorVar,
    renderRadialGauge,
    renderStatusBars,
    renderSparkline,
    computeOverviewCounts,
    renderSystemCoreCenter,
    renderOrbitalRing,
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
    renderAppFeasibilityTable,
    renderGlobalStats,
    renderBusinessesOverviewTable,
    renderTradingPortfolio,
    renderTradingPositions,
    renderTradingTrades,
    renderTradingStrategyVersions,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.DashboardRender = api;
  }
})(typeof window !== "undefined" ? window : this);
