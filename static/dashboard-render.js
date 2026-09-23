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
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Name</th><th>Role</th><th>Dept</th><th>Status</th>
            <th>Perm</th><th>ARC</th><th>Actions</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // Statuses a task never leaves once reached (see orchestrator.py) --
  // used below to tell a routine finished cycle log apart from
  // something that still needs attention.
  var TERMINAL_TASK_STATUSES = { completed: true, failed: true, cancelled: true };

  // With auto-trading running a cycle every few hours, this table's
  // already-newest-first, already-capped-at-50 list still fills up
  // almost entirely with routine completed cycle logs within a day or
  // two, burying anything that actually needs a look (queued/assigned/
  // in-progress, or a recent failure). Default view keeps every
  // non-terminal task plus the DEFAULT_TERMINAL_SHOWN most recent
  // terminal ones, in their original (newest-first) order; `showAll`
  // reveals the rest of what was fetched, with no extra API call --
  // see dashboard.js's tasksShowAll/lastTasks.
  var DEFAULT_TERMINAL_SHOWN = 5;

  function renderTasksTable(tasks, showAll) {
    if (!tasks || tasks.length === 0) {
      return '<p class="empty">No tasks yet.</p>';
    }
    let visible = tasks;
    let hiddenCount = 0;
    if (!showAll) {
      let terminalSeen = 0;
      visible = tasks.filter((t) => {
        if (!TERMINAL_TASK_STATUSES[t.status]) return true;
        terminalSeen += 1;
        return terminalSeen <= DEFAULT_TERMINAL_SHOWN;
      });
      hiddenCount = tasks.length - visible.length;
    }
    const rows = visible
      .map(
        (t) => `
        <tr>
          <td>${escapeHtml(t.objective)}</td>
          <td><span class="status status-${escapeHtml(t.status)}">${escapeHtml(t.status)}</span></td>
          <td>${escapeHtml(t.priority)}</td>
          <td>${fmtArc(t.cost_arc)}</td>
          <td class="task-result-cell" title="${t.result ? escapeHtml(t.result) : ""}">${
            t.result ? escapeHtml(t.result) : ""
          }</td>
        </tr>`
      )
      .join("");
    let toggle = "";
    if (hiddenCount > 0) {
      toggle = `<p class="tasks-toggle"><button type="button" class="tasks-show-all-btn">` +
        `Show ${hiddenCount} completed task${hiddenCount === 1 ? "" : "s"}</button></p>`;
    } else if (showAll && tasks.some((t) => TERMINAL_TASK_STATUSES[t.status])) {
      toggle = '<p class="tasks-toggle"><button type="button" class="tasks-show-all-btn">Show fewer</button></p>';
    }
    return `
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Objective</th><th>Status</th><th>Priority</th><th>Cost (ARC)</th>
            <th>Result / Error</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      ${toggle}`;
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
            <button class="btn-small btn-job-edit-interval"
                    data-action="set-job-interval"
                    data-job-id="${escapeHtml(j.id)}"
                    data-current-interval="${escapeHtml(j.interval_seconds)}">
              Edit Interval
            </button>
          </td>
        </tr>`;
      })
      .join("");
    return `
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Name</th><th>Objective</th><th>Interval</th>
            <th>Next Run</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // Maps a storefront order's status to the same four-color status
  // vocabulary used everywhere else in the dashboard (status-completed
  // = green, status-working = cyan/in-progress, status-awaiting_approval
  // = amber/needs-a-look, status-failed = red), so an owner recognizes
  // "this needs attention" at a glance without learning a second color
  // scheme just for orders.
  // Mirrors OPS_STUCK_ORDER_THRESHOLD_HOURS' default in
  // tasks/ops_maintenance_review.py -- keeps this table's own visual
  // flag in sync with the same threshold the ops report uses, so a
  // stuck order looks urgent here too, not just after running a
  // review.
  const STUCK_ORDER_THRESHOLD_HOURS = 24;

  function orderStatusClass(status, ageHours) {
    if (status === "fulfilled") return "status-completed";
    if (status === "paid") return "status-working";
    if (status === "pending_payment") {
      return ageHours != null && ageHours >= STUCK_ORDER_THRESHOLD_HOURS
        ? "status-failed" : "status-awaiting_approval";
    }
    return "status-failed"; // failed | refunded | anything unexpected
  }

  function _hoursSince(isoString) {
    const then = Date.parse(isoString);
    if (Number.isNaN(then)) return null;
    return (Date.now() - then) / 3600000;
  }

  // Stripe's own checkout-session dashboard page -- the one place that
  // can actually answer "did this customer pay" for an order stuck in
  // pending_payment, since this system deliberately never marks an
  // order 'paid' except in direct response to a verified webhook (see
  // api.py's stripe_webhook()). Session ids are prefixed cs_test_/
  // cs_live_, which is also how Stripe's own dashboard URLs pick
  // test vs. live mode.
  function _stripeSessionUrl(sessionId) {
    if (!sessionId) return null;
    const testPrefix = sessionId.startsWith("cs_test_") ? "test/" : "";
    return `https://dashboard.stripe.com/${testPrefix}checkout/sessions/${encodeURIComponent(sessionId)}`;
  }

  // "pending_payment" is an unbreakable 16-character token that alone
  // forced the Status column wider than the whole orders table could
  // spare -- every other real status value is already short enough to
  // display as-is.
  function orderStatusLabel(status) {
    return status === "pending_payment" ? "pending" : status;
  }

  // Short labels for the table -- the raw product_type values are long,
  // unbreakable snake_case tokens (e.g. "research_app_feasibility") that
  // don't wrap, so they were forcing the table wider than its panel.
  const ORDER_PRODUCT_LABELS = {
    research_opportunity: "Opportunity Research",
    research_roblox_trend: "Roblox Trend Research",
    research_app_feasibility: "App Feasibility",
    research_real_estate: "Real Estate Research",
  };

  function orderProductLabel(productType) {
    return ORDER_PRODUCT_LABELS[productType] || productType;
  }

  function renderOrdersTable(orders) {
    if (!orders || orders.length === 0) {
      return '<p class="empty">No store orders yet.</p>';
    }
    const rows = orders
      .map((o) => {
        const ageHours = o.status === "pending_payment" ? _hoursSince(o.created_at) : null;
        const stuck = ageHours != null && ageHours >= STUCK_ORDER_THRESHOLD_HOURS;
        const stripeUrl = _stripeSessionUrl(o.stripe_session_id);
        return `
        <tr>
          <td>${escapeHtml(o.topic)}</td>
          <td>${escapeHtml(orderProductLabel(o.product_type))}</td>
          <td>${escapeHtml(o.customer_email)}</td>
          <td>${fmtUsd((o.price_usd_cents || 0) / 100)}</td>
          <td><span class="status ${orderStatusClass(o.status, ageHours)}">${escapeHtml(orderStatusLabel(o.status))}</span>${
            stuck ? ` <span class="order-stuck-note">(${ageHours.toFixed(0)}h, check Stripe)</span>` : ""
          }</td>
          <td>${escapeHtml(o.created_at)}</td>
          <td>${stripeUrl
            ? `<a href="${escapeHtml(stripeUrl)}" target="_blank" rel="noopener">View in Stripe</a>`
            : "—"}</td>
          <td>${stuck
            ? `<button class="btn-remove-card" data-action="delete-abandoned-order"
                data-id="${escapeHtml(o.id)}"
                title="Only enabled once verified in Stripe to have never been paid">Remove</button>`
            : ""}</td>
        </tr>`;
      })
      .join("");
    return `
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Topic</th><th>Product</th><th>Customer</th>
            <th>Price</th><th>Status</th><th>Created</th><th>Stripe</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // Aggregates real orders into one bar per calendar day for the last
  // `rangeDays` days (today inclusive), counting only orders whose
  // payment actually succeeded ("paid" or "fulfilled") -- a pending or
  // failed checkout never collected money, and a refund would overstate
  // it, so both are excluded. Dates are compared as plain "YYYY-MM-DD"
  // prefixes of the stored timestamp (paid_at, falling back to
  // created_at) rather than parsed through Date/toISOString: SQLite and
  // Postgres serialize timestamps slightly differently, and slicing the
  // first 10 characters works identically for both without risking a
  // timezone-driven off-by-one-day shift. `now` is an injectable clock
  // (defaults to the real current time) purely so this is deterministic
  // to unit test.
  const ORDER_REVENUE_STATUSES = { paid: true, fulfilled: true };
  function computeOrdersRevenueByDay(orders, rangeDays, now) {
    const days = rangeDays || 7;
    const today = now || new Date();
    const buckets = [];
    const indexByKey = {};
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - i));
      const key = d.toISOString().slice(0, 10);
      const label = d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
      indexByKey[key] = buckets.length;
      buckets.push({ key, label, value: 0 });
    }
    for (const o of orders || []) {
      if (!ORDER_REVENUE_STATUSES[o.status]) continue;
      const raw = o.paid_at || o.created_at;
      if (!raw || !/^\d{4}-\d{2}-\d{2}/.test(String(raw))) continue;
      const idx = indexByKey[String(raw).slice(0, 10)];
      if (idx === undefined) continue; // outside the selected range
      buckets[idx].value += (o.price_usd_cents || 0) / 100;
    }
    return buckets;
  }

  // Pure SVG bar-chart renderer over already-bucketed data (see
  // computeOrdersRevenueByDay) -- kept separate from the aggregation
  // above so each half can be unit tested with hand-built fixtures,
  // matching computeOverviewCounts/renderGlobalStats's existing split.
  // Bars carry data-action="select-order-bar" so dashboard.js's single
  // delegated click handler can pick them up like every other button
  // here, plus role/tabindex so they're keyboard-reachable.
  function renderOrdersChart(buckets) {
    if (!buckets || buckets.length === 0) {
      return '<p class="empty">No data for this range.</p>';
    }
    const total = buckets.reduce((s, b) => s + b.value, 0);
    if (total <= 0) {
      return '<p class="empty">No paid orders in this range yet.</p>';
    }
    const width = 640, height = 200, padL = 46, padR = 8, padT = 10, padB = 24;
    const innerW = width - padL - padR, innerH = height - padT - padB;
    const max = Math.max(...buckets.map((b) => b.value)) * 1.15 || 1;
    const gap = innerW / buckets.length;
    const barW = Math.max(2, gap * 0.62);
    const labelStep = buckets.length <= 7 ? 1 : buckets.length <= 30 ? 5 : 15;

    const gridLines = [0.33, 0.66, 1]
      .map((f) => {
        const y = padT + innerH * (1 - f);
        return `<line class="orders-chart-grid" x1="${padL}" x2="${width - padR}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" />` +
          `<text class="orders-chart-axis-label" x="${padL - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end">${fmtUsd(max * f)}</text>`;
      })
      .join("");
    const baseline = `<line class="orders-chart-grid" x1="${padL}" x2="${width - padR}" y1="${(padT + innerH).toFixed(1)}" y2="${(padT + innerH).toFixed(1)}" />`;

    const bars = buckets
      .map((b, i) => {
        const x = padL + i * gap + (gap - barW) / 2;
        const barH = (b.value / max) * innerH;
        const y = padT + innerH - barH;
        const showLabel = i % labelStep === 0 || i === buckets.length - 1;
        const labelEl = showLabel
          ? `<text class="orders-chart-axis-label" x="${(x + barW / 2).toFixed(1)}" y="${height - 8}" text-anchor="middle">${escapeHtml(b.label)}</text>`
          : "";
        return `<rect class="orders-chart-bar" data-action="select-order-bar" data-label="${escapeHtml(b.label)}" ` +
          `data-value="${b.value.toFixed(2)}" tabindex="0" role="button" ` +
          `aria-label="${escapeHtml(b.label)}: ${fmtUsd(b.value)}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" ` +
          `width="${barW.toFixed(1)}" height="${Math.max(1, barH).toFixed(1)}" rx="2"></rect>${labelEl}`;
      })
      .join("");

    return `
      <div class="orders-chart">
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily revenue for the selected range">
          ${gridLines}${baseline}${bars}
        </svg>
      </div>`;
  }

  // Shared by every research-report card renderer below (opportunities,
  // roblox trends, app feasibility, real estate) -- they were four
  // near-identical copies of the same card shape (head + summary + a
  // field list + references), which meant the collapsible-details
  // behavior added here would otherwise need to be kept in sync by hand
  // in four places. Collapsed by default (see .card-details in
  // dashboard.css) -- these reports can run long, and a business with
  // several researched concepts turns into a lot of scrolling if every
  // card is fully expanded at once; the head (title + confidence) is
  // enough to scan the list, full detail is one click away.
  function _renderResearchCard(title, confidence, summary, fields, urls, removeAction, removeId,
                                removeTitle, extraHeadHtml) {
    const confEsc = escapeHtml(confidence || "unknown");
    const statusClass = confEsc === "high" ? "idle" : confEsc === "medium" ? "awaiting_approval" : "failed";
    const fieldsHtml = fields
      .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value || "")}</dd>`)
      .join("");
    const refsHtml = urls.length > 0
      ? `<p class="opportunity-refs">References: ${urls.map((u) => escapeHtml(u)).join(", ")}</p>`
      : '<p class="opportunity-refs">No reference URLs fetched — based on general knowledge only.</p>';
    return `
        <div class="opportunity-card confidence-${confEsc}">
          <div class="opportunity-head">
            <strong>${escapeHtml(title)}</strong>
            <span class="opportunity-head-right">
              <span class="status status-${statusClass}">confidence: ${confEsc}</span>
              ${extraHeadHtml || ""}
              <button type="button" class="card-toggle-btn">Details</button>
              <button class="btn-remove-card" data-action="${removeAction}" data-id="${escapeHtml(removeId)}"
                title="${escapeHtml(removeTitle)}">Remove</button>
            </span>
          </div>
          <div class="card-details">
            <p class="opportunity-summary">${escapeHtml(summary || "")}</p>
            <dl class="opportunity-fields">${fieldsHtml}</dl>
            ${refsHtml}
          </div>
        </div>`;
  }

  // Shared by every research vertical's card renderer -- once launched
  // (see POST .../{vertical}/{id}/launch), the button is replaced by a
  // plain badge so the same record can never be launched twice from
  // here. `launchAction` is the data-action the dashboard.js dispatcher
  // routes on (e.g. "launch-opportunity"); `launchedBusinessId` is the
  // record's own launched_business_id field.
  function _launchButtonHtml(launchAction, id, title, launchedBusinessId) {
    return launchedBusinessId
      ? '<span class="status status-idle" title="A business already exists for this">launched</span>'
      : `<button type="button" class="btn-small" data-action="${escapeHtml(launchAction)}"
          data-id="${escapeHtml(id)}" data-title="${escapeHtml(title)}">Launch Business</button>`;
  }

  function _referenceUrls(raw) {
    try {
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function renderOpportunitiesTable(opportunities) {
    if (!opportunities || opportunities.length === 0) {
      return '<p class="empty">No opportunities researched yet.</p>';
    }
    const cards = opportunities
      .map((o) => {
        const launchHtml = _launchButtonHtml("launch-opportunity", o.id, o.topic, o.launched_business_id);
        return _renderResearchCard(
          o.topic, o.confidence_level, o.summary,
          [
            ["Market size", o.market_size], ["Competition", o.competition],
            ["Startup cost", o.startup_cost], ["Revenue potential", o.revenue_potential],
            ["Time to market", o.time_to_market], ["Operational complexity", o.operational_complexity],
            ["Legal/regulatory risk", o.legal_regulatory_risk], ["Capital requirements", o.capital_requirements],
            ["Downside risk", o.downside_risk],
          ],
          _referenceUrls(o.reference_urls_used), "delete-opportunity", o.id, "Remove this researched opportunity",
          launchHtml,
        );
      })
      .join("");
    return `<div class="opportunities-list">${cards}</div>`;
  }

  function renderRobloxTrendsTable(trends) {
    if (!trends || trends.length === 0) {
      return '<p class="empty">No Roblox concepts researched yet.</p>';
    }
    const cards = trends
      .map((t) => _renderResearchCard(
        t.concept, t.confidence_level, t.summary,
        [
          ["Player demand signals", t.player_demand_signals], ["Competition level", t.competition_level],
          ["Build complexity", t.build_complexity], ["Target audience", t.target_audience],
          ["Monetization fit", t.monetization_fit], ["Estimated dev time", t.estimated_dev_time],
          ["Similar successful games", t.similar_successful_games], ["Risk factors", t.risk_factors],
        ],
        _referenceUrls(t.reference_urls_used), "delete-roblox-trend", t.id, "Remove this researched concept",
        _launchButtonHtml("launch-roblox-trend", t.id, t.concept, t.launched_business_id),
      ))
      .join("");
    return `<div class="opportunities-list">${cards}</div>`;
  }

  function renderAppFeasibilityTable(assessments) {
    if (!assessments || assessments.length === 0) {
      return '<p class="empty">No app feasibility assessments yet.</p>';
    }
    const cards = assessments
      .map((a) => _renderResearchCard(
        a.concept, a.confidence_level, a.summary,
        [
          ["Platform recommendation", a.platform_recommendation], ["Suggested tech stack", a.suggested_tech_stack],
          ["Complexity tier", a.complexity_tier], ["Estimated timeline", a.estimated_timeline],
          ["Estimated cost range", a.estimated_cost_range], ["MVP feature scope", a.mvp_feature_scope],
          ["Key technical risks", a.key_technical_risks], ["Similar existing apps", a.similar_existing_apps],
        ],
        _referenceUrls(a.reference_urls_used), "delete-app-feasibility", a.id, "Remove this feasibility assessment",
        _launchButtonHtml("launch-app-feasibility", a.id, a.concept, a.launched_business_id),
      ))
      .join("");
    return `<div class="opportunities-list">${cards}</div>`;
  }

  function renderRealEstateTable(assessments) {
    if (!assessments || assessments.length === 0) {
      return '<p class="empty">No real estate assessments yet.</p>';
    }
    const cards = assessments
      .map((a) => _renderResearchCard(
        a.property_or_market, a.confidence_level, a.summary,
        [
          ["Market trend", a.market_trend], ["Comparable properties", a.comparable_properties],
          ["Estimated rental yield", a.estimated_rental_yield],
          ["Price trend assessment", a.price_trend_assessment], ["Risk factors", a.risk_factors],
        ],
        _referenceUrls(a.reference_urls_used), "delete-real-estate", a.id, "Remove this assessment",
        _launchButtonHtml("launch-real-estate", a.id, a.property_or_market, a.launched_business_id),
      ))
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
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Symbol</th><th>Quantity</th><th>Avg Cost</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
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
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>When</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th>
            <th>Realized P&amp;L</th><th>Confidence</th><th>Rationale</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function renderLiveTrading(view) {
    if (!view) {
      return '<p class="empty">No trading portfolio yet — create a paper trading portfolio ' +
        'above first, then enable live trading below.</p>';
    }
    const snap = view.latest_snapshot;
    const history = view.equity_history || [];
    const sparkline = renderSparkline(history.map((h) => ({ value: h.equity_usd })), { color: "var(--red)" });
    const statusBadge = view.live_trading_enabled
      ? '<span class="status status-failed">LIVE — real money</span>'
      : '<span class="status status-idle">disabled</span>';
    return `
      <div class="arc-summary">
        <div class="arc-stat"><span class="label">Status</span><span class="value">${statusBadge}</span></div>
        <div class="arc-stat"><span class="label">Real Cash</span><span class="value">${
          snap ? fmtUsd(snap.cash_usd) : "n/a"
        }</span></div>
        <div class="arc-stat"><span class="label">Real Equity</span><span class="value">${
          snap ? fmtUsd(snap.equity_usd) : "n/a"
        }</span></div>
        <div class="arc-stat"><span class="label">Open positions</span><span class="value">${
          snap ? escapeHtml(snap.open_positions) : "0"
        }</span></div>
      </div>
      <div class="sparkline-block">
        <div class="stat-bars-title">Real equity trend (last ${history.length} snapshots)</div>
        ${sparkline}
      </div>
      <p class="panel-note">${
        snap
          ? `Last real snapshot: ${escapeHtml(snap.created_at)} (strategy v${escapeHtml(snap.strategy_version)}).`
          : "No live trading cycle has run yet."
      }</p>`;
  }

  function renderLiveTradingTrades(trades) {
    if (!trades || trades.length === 0) {
      return '<p class="empty">No live (real-money) trades yet.</p>';
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
          <td>${t.live_cap_applied ? "yes" : "no"}</td>
          <td>${escapeHtml(t.confidence_level || "")}</td>
          <td>${escapeHtml(t.rationale || "")}</td>
        </tr>`;
      })
      .join("");
    return `
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>When</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th>
            <th>Realized P&amp;L</th><th>Cap Applied</th><th>Confidence</th><th>Rationale</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function _fmtBacktestStat(v) {
    if (v === null || v === undefined) return "n/a";
    if (v === Infinity) return "∞";
    if (typeof v === "number") return v.toFixed(2);
    return String(v);
  }

  function renderBacktestRuns(runs) {
    if (!runs || runs.length === 0) {
      return '<p class="empty">No backtest runs yet — use the form above to test the current ' +
        'strategy against real historical data.</p>';
    }
    const cards = runs
      .map((run) => {
        const candidates = run.candidates || [];
        const rows = candidates
          .map((c, i) => {
            const isBest = i === run.best_candidate_index;
            const vs = c.validation_stats || {};
            const ts = c.train_stats || {};
            const winPct = vs.win_rate != null ? `${(vs.win_rate * 100).toFixed(0)}%` : "n/a";
            const ddPct = vs.max_drawdown_pct != null ? `${(vs.max_drawdown_pct * 100).toFixed(1)}%` : "n/a";
            const trades = vs.sell_trades != null ? vs.sell_trades : 0;
            return `
            <tr class="${isBest ? "backtest-best-row" : ""}">
              <td>${i}${isBest ? " ★" : ""}</td>
              <td>${c.validation_meets_bar
                ? '<span class="status status-idle">PASS</span>'
                : '<span class="status status-failed">no</span>'}</td>
              <td>${_fmtBacktestStat(vs.net_pnl_usd)}</td>
              <td>${_fmtBacktestStat(vs.profit_factor)}</td>
              <td>${winPct}</td>
              <td>${ddPct}</td>
              <td>${trades}${vs.sample_size_ok ? "" : " (thin)"}</td>
              <td>${_fmtBacktestStat(ts.net_pnl_usd)}</td>
              <td>${escapeHtml(c.rationale || "(initial strategy, not a proposal)")}</td>
              <td><button type="button" class="btn-small" data-action="promote-backtest-candidate"
                data-run-id="${escapeHtml(run.id)}" data-candidate-index="${i}">Promote</button></td>
            </tr>`;
          })
          .join("");
        return `
        <div class="opportunity-card confidence-${run.stopped_early ? "high" : "low"}">
          <div class="opportunity-head">
            <strong>${escapeHtml(run.train_start_date)} → ${escapeHtml(run.validation_end_date)}</strong>
            <span class="status ${run.stopped_early ? "status-idle" : "status-awaiting_approval"}">${
              run.stopped_early ? "strategy found" : "none passed"
            }</span>
          </div>
          <p class="opportunity-summary">Validation window starts ${escapeHtml(run.validation_split_date)}. ${
            candidates.length
          } candidate(s) tried (max ${escapeHtml(run.max_candidates)}). "Promote" sets a candidate as the
            business's active strategy (used by future paper/live trading cycles) -- this never happens
            automatically, a candidate passing PASS is only a recommendation.</p>
          <div class="table-scroll">
            <table class="data-table">
              <thead><tr><th>#</th><th>Bar</th><th>Val P&amp;L</th><th>Val PF</th><th>Val Win%</th>
                <th>Val MaxDD</th><th>Val Trades</th><th>Train P&amp;L</th><th>Rationale</th><th></th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
        </div>`;
      })
      .join("");
    return cards;
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
        // The active version is the one you actually came here to check
        // -- shown expanded by default; every prior version is history,
        // collapsed until you specifically want to compare against it.
        return `
        <div class="opportunity-card confidence-${confidence}${v.active ? " expanded" : ""}">
          <div class="opportunity-head">
            <strong>Version ${escapeHtml(v.version)}${v.active ? " (active)" : ""}</strong>
            <span class="opportunity-head-right">
              <span class="status status-${v.source === "owner_override" ? "awaiting_approval" : "idle"}">${escapeHtml(v.source)}</span>
              <button type="button" class="card-toggle-btn">Details</button>
            </span>
          </div>
          <div class="card-details">
            <p class="opportunity-summary">${escapeHtml(v.rationale || "")}</p>
            <dl class="opportunity-fields">
              <dt>Watchlist</dt><dd>${watchlist}</dd>
              <dt>Max position %</dt><dd>${params.max_position_pct != null ? (params.max_position_pct * 100).toFixed(0) + "%" : ""}</dd>
              <dt>Max trade % of cash</dt><dd>${params.max_trade_pct_of_cash != null ? (params.max_trade_pct_of_cash * 100).toFixed(0) + "%" : ""}</dd>
              <dt>Max open positions</dt><dd>${escapeHtml(params.max_open_positions)}</dd>
              <dt>Drawdown halt</dt><dd>${params.drawdown_halt_pct != null ? (params.drawdown_halt_pct * 100).toFixed(0) + "%" : ""}</dd>
              <dt>Min confidence to trade</dt><dd>${escapeHtml(params.min_confidence_to_trade)}</dd>
            </dl>
          </div>
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
          <span class="core-node-dot"></span>
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

  // Maps an ops report's severity to the same shared status vocabulary
  // used everywhere else (completed=green, awaiting_approval=amber,
  // failed=red) -- "ok"/"info" both read as healthy at a glance, since
  // neither needs the owner's attention.
  function opsSeverityClass(severity) {
    if (severity === "warning") return "status-awaiting_approval";
    if (severity === "critical") return "status-failed";
    return "status-completed"; // ok | info
  }

  function renderOpsReport(report) {
    if (!report) {
      return '<p class="empty">No ops review has run yet.</p>';
    }
    const findings = (() => {
      try {
        return JSON.parse(report.findings || "[]");
      } catch (e) {
        return [];
      }
    })();
    const findingsHtml = findings.length
      ? findings
          .map(
            (f) => `
            <div class="approval-card">
              <div class="approval-desc">
                <strong><span class="status ${opsSeverityClass(f.severity)}">${escapeHtml(f.severity)}</span>
                ${escapeHtml(f.category)}</strong>
                <div>${escapeHtml(f.description)}</div>
                <div class="panel-note">Recommendation: ${escapeHtml(f.recommendation)}</div>
              </div>
            </div>`
          )
          .join("")
      : '<p class="empty">No findings — everything looked healthy.</p>';
    return `
      <div class="approval-card">
        <div class="approval-desc">
          <strong><span class="status ${opsSeverityClass(report.overall_severity)}">${escapeHtml(report.overall_severity)}</span>
          Confidence: ${escapeHtml(report.confidence_level)}</strong>
          <div>${escapeHtml(report.summary)}</div>
          <div class="panel-note">Reviewed ${escapeHtml(report.created_at)}</div>
        </div>
      </div>
      ${findingsHtml}`;
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
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Agents</th>
            <th>Open Tasks</th><th>Approvals</th><th>ARC Earned / Spent</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // ---------- Header: system status + approvals badge ----------

  function renderHeaderStatus(overview) {
    const report = overview.latest_ops_report;
    const severity = report ? report.overall_severity : "ok";
    const cls = severity === "critical" ? " header-status-red"
      : severity === "warning" ? " header-status-amber" : "";
    const label = severity === "critical" ? "CRITICAL"
      : severity === "warning" ? "ATTENTION NEEDED" : "SYSTEM ONLINE";
    return `
      <div class="header-status${cls}">
        <span class="header-status-dot"></span>
        <span>${escapeHtml(label)}</span>
      </div>`;
  }

  function renderHeaderApprovalsBadge(pendingApprovals) {
    const count = (pendingApprovals || []).length;
    if (count === 0) return "";
    return `
      <a href="#global-approvals-section" class="header-approvals-badge">
        <span>${escapeHtml(count)}</span> awaiting approval
      </a>`;
  }

  // ---------- Command stat row: Real USD / ARC / Agents / Businesses ----------

  // Mirrors AGENT_STATUSES in registry.py exactly -- shown in full so
  // the row stays correct even for a status no running code assigns
  // yet, rather than only ever showing whichever few happen to have a
  // nonzero count today.
  const ALL_AGENT_STATUSES = [
    "created", "initializing", "active", "working", "idle",
    "waiting", "improving", "paused", "failed", "quarantined", "retired",
  ];
  const ALL_BUSINESS_STATUSES = ["active", "paused", "retired"];
  const ALERT_AGENT_STATUSES = { failed: true, quarantined: true };

  function _statCell(label, value, tone) {
    return `
      <div>
        <div class="stat-cell-label">${escapeHtml(label)}</div>
        <div class="stat-cell-value${tone ? " tone-" + tone : ""}">${escapeHtml(value)}</div>
      </div>`;
  }

  function _statBlock(label, cellsHtml) {
    return `
      <div class="stat-block">
        <div class="stat-block-label">${escapeHtml(label)}</div>
        <div class="stat-block-cells">${cellsHtml}</div>
      </div>`;
  }

  function renderCommandStatRow(overview) {
    const revenue = (overview.real_revenue_usd_cents || 0) / 100;
    const pendingApprovals = overview.pending_approvals || [];
    const awaitingUsd = pendingApprovals.reduce(
      (sum, p) => sum + Number(p.amount_usd || 0), 0
    );

    const arc = overview.global_arc || {};
    const earned = Number(arc.earn || 0) + Number(arc.allocation || 0);
    const spent = Math.abs(Number(arc.spend || 0));

    const agentsByStatus = overview.agents_by_status || {};
    const bizCounts = { active: 0, paused: 0, retired: 0 };
    (overview.businesses || []).forEach((b) => {
      if (Object.prototype.hasOwnProperty.call(bizCounts, b.status)) bizCounts[b.status]++;
    });

    const usdCells =
      _statCell("Revenue Collected", fmtUsd(revenue), "green") +
      _statCell("Awaiting Approval", fmtUsd(awaitingUsd), awaitingUsd > 0 ? "amber" : undefined);

    const arcCells =
      _statCell("Earned", fmtArc(earned)) +
      _statCell("Spent", fmtArc(spent));

    const agentCells = ALL_AGENT_STATUSES.map((s) => {
      const count = agentsByStatus[s] || 0;
      const tone = ALERT_AGENT_STATUSES[s] && count > 0 ? "red" : undefined;
      return _statCell(s.replace(/_/g, " "), String(count), tone);
    }).join("");

    const bizCells = ALL_BUSINESS_STATUSES.map((s) =>
      _statCell(s, String(bizCounts[s]), s === "active" ? "green" : undefined)
    ).join("");

    return (
      _statBlock("Real USD", usdCells) +
      _statBlock("ARC (internal)", arcCells) +
      _statBlock("Agents", agentCells) +
      _statBlock("Businesses", bizCells)
    );
  }

  // ---------- Live Activity feed (backed by GET /audit) ----------

  // Known audit_log `action` values mapped to a short human label --
  // gathered directly from every db.audit(...) call site in this
  // codebase. Anything not in this map falls back to the raw action
  // string with underscores turned to spaces -- an unrecognized action
  // is never hidden, just unprettified.
  const AUDIT_ACTION_LABELS = {
    create_business: "created business",
    create_agent: "created agent",
    pause_agent: "paused agent",
    retire_agent: "retired agent",
    approve_action: "approved request",
    reject_action: "rejected request",
    allocate_arc: "allocated ARC",
    reward_arc: "rewarded ARC",
    charge_arc: "charged ARC",
    penalize_arc: "penalized ARC",
    create_task: "created task",
    assign_task: "assigned task",
    complete_task: "completed task",
    fail_task: "task failed",
    task_awaiting_approval: "task awaiting approval",
    task_approved_promoted: "approved task promoted",
    task_rejected_cancelled: "rejected task cancelled",
    task_unassigned_no_agent: "task unassigned — no agent available",
    create_trading_portfolio: "created trading portfolio",
    enable_auto_trading: "enabled auto-trading",
    enable_live_trading: "enabled LIVE trading",
    disable_live_trading: "disabled live trading",
    trading_strategy_updated: "trading strategy updated",
    trading_strategy_overridden: "trading strategy overridden by owner",
    trading_drawdown_halt: "trading paused — drawdown limit hit",
    live_trading_halt: "LIVE trading halted",
    live_order_failed: "live order failed",
    live_order_unconfirmed: "live order unconfirmed",
    strategy_backtest_search_completed: "backtest search completed",
    opportunity_assessed: "researched an opportunity",
    roblox_trend_assessed: "researched a Roblox concept",
    app_feasibility_assessed: "assessed app feasibility",
    real_estate_assessed: "researched real estate",
    order_paid: "order paid",
    order_fulfilled: "order fulfilled",
    order_fulfillment_failed: "order fulfillment failed",
    ops_maintenance_reviewed: "ran ops review",
    owner_digest_sent: "sent owner digest",
    scheduled_job_fired: "scheduled job fired",
    scheduled_job_provisioned: "scheduled job created",
    set_scheduled_job_enabled: "toggled scheduled job",
    set_scheduled_job_interval: "changed job interval",
    reconcile_stuck_agent: "reconciled a stuck agent",
    ops_business_provisioned: "provisioned business",
    set_business_status: "changed business status",
    delete_abandoned_order: "removed abandoned order",
    delete_opportunity: "removed opportunity",
    delete_roblox_trend: "removed Roblox concept",
    delete_app_feasibility_assessment: "removed feasibility assessment",
    delete_real_estate_assessment: "removed real estate assessment",
  };

  // Actions that represent something going wrong or needing attention
  // vs. routine/positive activity -- same severity intent as the rest
  // of the dashboard's status vocabulary, just applied to a raw action
  // string instead of a status enum.
  const AUDIT_ACTION_ALERT = {
    fail_task: "red", task_unassigned_no_agent: "amber", trading_drawdown_halt: "red",
    live_trading_halt: "red", live_order_failed: "red", live_order_unconfirmed: "amber",
    task_awaiting_approval: "amber", reject_action: "amber",
    order_fulfillment_failed: "red", scheduler_tick_error: "red",
    executor_pass_error: "red", fulfillment_pass_error: "red",
    owner_ops_report_alert_failed: "red", order_email_failed: "amber",
    owner_failed_order_alert_failed: "red",
  };

  function _auditActionLabel(action) {
    return AUDIT_ACTION_LABELS[action] || String(action || "").replace(/_/g, " ");
  }

  function _auditActorLabel(entry) {
    if (entry.actor === "owner") return "OWNER";
    if (entry.actor === "system") return "SYSTEM";
    return entry.actor_name || entry.actor || "unknown";
  }

  function renderActivityFeed(events) {
    if (!events || events.length === 0) {
      return '<p class="empty">No system activity recorded yet.</p>';
    }
    const rows = events
      .map((e) => {
        const dotColor = AUDIT_ACTION_ALERT[e.action] === "red" ? "var(--red)"
          : AUDIT_ACTION_ALERT[e.action] === "amber" ? "var(--amber)"
          : "var(--accent)";
        const ts = String(e.created_at || "").slice(5, 16).replace("T", " "); // MM-DD HH:MM
        return `
        <div class="activity-row">
          <span class="activity-ts">${escapeHtml(ts)}</span>
          <span class="activity-dot" style="background:${dotColor};color:${dotColor}"></span>
          <span>
            <span class="activity-actor">${escapeHtml(_auditActorLabel(e))}</span>
            <span class="activity-text">${escapeHtml(_auditActionLabel(e.action))}${
              e.target_type ? ` &middot; ${escapeHtml(e.target_type)}` : ""
            }</span>
          </span>
        </div>`;
      })
      .join("");
    return `<div class="activity-feed">${rows}</div>`;
  }

  // ---------- System Core: outer ring of real business nodes ----------

  // Second ring around the same hub as renderOrbitalRing's metric
  // nodes -- real businesses (name, status, pending-approval count),
  // at a larger radius so the two rings never collide. Returns its own
  // <line>s + node <div>s in the same shape as renderOrbitalRing, meant
  // to be concatenated into the same #core-orbital-overlay container.
  const BUSINESS_RING_RADIUS_PCT = 47;

  function renderBusinessRing(businesses) {
    if (!businesses || businesses.length === 0) return "";
    const positioned = businesses.map((b, i) =>
      Object.assign({}, b, pointOnRing(i, businesses.length, BUSINESS_RING_RADIUS_PCT))
    );
    const lines = positioned
      .map(
        (p, i) => `
        <line class="web-line" x1="50" y1="50" x2="${p.x.toFixed(2)}" y2="${p.y.toFixed(2)}"
          style="animation-delay:${(i * 0.15 + 0.3).toFixed(2)}s;opacity:0.25" />`
      )
      .join("");
    const nodeDivs = positioned
      .map((p, i) => {
        const alert = Number(p.pending_approval_count || 0) > 0;
        const dotColor = alert ? "var(--amber)" : p.status === "active" ? "var(--green)" : "var(--muted)";
        return `
        <div class="core-node core-node-business${alert ? " core-node-alert" : ""}"
          style="left:${p.x.toFixed(2)}%;top:${p.y.toFixed(2)}%;animation-delay:${(i * 0.08 + 0.2).toFixed(2)}s"
          title="${escapeHtml(p.name)} — ${escapeHtml(p.status)}">
          <span class="core-node-biz-dot" style="background:${dotColor};color:${dotColor}"></span>
          <span class="core-node-label">${escapeHtml(p.name)}</span>
        </div>`;
      })
      .join("");
    return `
      <svg class="core-web-lines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">${lines}</svg>
      ${nodeDivs}`;
  }

  // ---------- Trading Strategy Evolution (global summary) ----------

  function renderTradingStrategyEvolution(evolution) {
    if (!evolution || !evolution.total_versions) {
      return '<p class="empty">No trading strategy history yet — versions appear once a ' +
        'business starts paper or live trading.</p>';
    }
    const latest = evolution.latest_version;
    return `
      <div class="evolution-summary">
        <div class="evolution-row"><span class="k">Strategy versions (all businesses)</span><span class="v">${escapeHtml(evolution.total_versions)}</span></div>
        <div class="evolution-row"><span class="k">Currently active</span><span class="v">${escapeHtml(evolution.active_count)}</span></div>
        <div class="evolution-row"><span class="k">Businesses with a trading strategy</span><span class="v">${escapeHtml(evolution.businesses_with_trading)}</span></div>
        ${latest ? `<div class="evolution-row"><span class="k">Most recent change</span><span class="v">${escapeHtml(latest.created_at)} (${escapeHtml(latest.source)})</span></div>` : ""}
        ${latest && latest.rationale ? `<div class="evolution-row"><span class="k">Rationale</span><span class="v">${escapeHtml(latest.rationale)}</span></div>` : ""}
      </div>`;
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
    renderBusinessRing,
    renderHeaderStatus,
    renderHeaderApprovalsBadge,
    renderCommandStatRow,
    renderActivityFeed,
    renderTradingStrategyEvolution,
    renderBusinessOptions,
    renderAgentOptions,
    renderBusinessHeader,
    renderAgentsTable,
    renderTasksTable,
    renderApprovalsList,
    renderArcSummary,
    renderJobsTable,
    orderStatusClass,
    orderStatusLabel,
    renderOrdersTable,
    computeOrdersRevenueByDay,
    renderOrdersChart,
    renderOpportunitiesTable,
    renderRobloxTrendsTable,
    renderAppFeasibilityTable,
    renderRealEstateTable,
    opsSeverityClass,
    renderOpsReport,
    renderGlobalStats,
    renderBusinessesOverviewTable,
    renderTradingPortfolio,
    renderTradingPositions,
    renderTradingTrades,
    renderTradingStrategyVersions,
    renderLiveTrading,
    renderLiveTradingTrades,
    renderBacktestRuns,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.DashboardRender = api;
  }
})(typeof window !== "undefined" ? window : this);
