/*
 * dashboard.js — DOM wiring only. Fetches JSON from the same-origin API
 * (served by this same FastAPI process, so no CORS setup is needed) and
 * hands the data to the pure, unit-tested functions in
 * dashboard-render.js. This file itself is NOT unit-tested — it can
 * only be exercised in a real browser against a running server — so it
 * is kept deliberately thin: fetch, then call a render function,
 * nothing clever here.
 */
(function () {
  const R = window.DashboardRender;
  let currentBusinessId = null;
  let refreshInFlight = false;
  const AUTO_REFRESH_INTERVAL_MS = 5000;

  // Revenue trend chart state -- purely a display concern over orders
  // already fetched by loadDashboard(), so switching the range or
  // selecting a bar never needs a network round-trip.
  let lastOrders = [];
  let ordersChartRangeDays = 7;
  function renderOrdersChartPanel() {
    setHtmlIfChanged("orders-chart", R.renderOrdersChart(R.computeOrdersRevenueByDay(lastOrders, ordersChartRangeDays)));
  }

  // Shared tactile click feedback for every button on the page (see the
  // single delegated listener wired near the bottom of this file).
  const reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function addRipple(el, evt) {
    if (reduceMotion) return;
    const rect = el.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) * 1.5;
    const span = document.createElement("span");
    span.className = "ripple";
    span.style.width = span.style.height = size + "px";
    const cx = evt && typeof evt.clientX === "number" ? evt.clientX : rect.left + rect.width / 2;
    const cy = evt && typeof evt.clientY === "number" ? evt.clientY : rect.top + rect.height / 2;
    span.style.left = (cx - rect.left - size / 2) + "px";
    span.style.top = (cy - rect.top - size / 2) + "px";
    el.appendChild(span);
    span.addEventListener("animationend", () => span.remove());
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" },
    }, opts));
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${body}`);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function showError(msg) {
    const el = document.getElementById("error-banner");
    el.textContent = msg;
    el.classList.remove("hidden");
    setTimeout(() => el.classList.add("hidden"), 6000);
  }

  // The dashboard polls every AUTO_REFRESH_INTERVAL_MS regardless of
  // whether anything actually changed. Blindly reassigning innerHTML
  // on every poll destroys and recreates every child node even when
  // the markup is byte-identical, which replays every CSS entrance
  // animation in there (the orbital web's node-in/line animations, the
  // opportunity/approval card fade-ins, etc.) -- the exact
  // "disappears and reappears on every refresh" bug the owner
  // reported. Skipping the DOM write entirely when the rendered HTML
  // hasn't changed leaves the existing nodes (and their animation
  // state) alone.
  const lastHtmlById = {};
  function setHtmlIfChanged(id, html) {
    if (lastHtmlById[id] === html) return;
    lastHtmlById[id] = html;
    document.getElementById(id).innerHTML = html;
  }

  async function loadBusinessList() {
    const businesses = await api("/businesses");
    document.getElementById("business-select").innerHTML =
      R.renderBusinessOptions(businesses);
    if (businesses.length > 0 && !currentBusinessId) {
      currentBusinessId = businesses[0].id;
      document.getElementById("business-select").value = currentBusinessId;
      await loadDashboard();
    }
  }

  async function loadOverview() {
    // Global, business-independent -- runs regardless of which business
    // (if any) is currently selected below. This is what makes pending
    // approvals on a business the owner isn't currently viewing actually
    // visible, instead of silently sitting unnoticed.
    const data = await api("/overview");
    setHtmlIfChanged("global-approvals-list", R.renderApprovalsList(data.pending_approvals));
    setHtmlIfChanged("global-stats", R.renderGlobalStats(data));
    setHtmlIfChanged("businesses-overview-table", R.renderBusinessesOverviewTable(data.businesses));
    setHtmlIfChanged("system-core-center", R.renderSystemCoreCenter(data));
    setHtmlIfChanged("core-orbital-overlay", R.renderOrbitalRing(data));
    setHtmlIfChanged("ops-report", R.renderOpsReport(data.latest_ops_report));
  }

  async function loadDashboard() {
    if (!currentBusinessId) return;
    const data = await api(`/businesses/${currentBusinessId}/dashboard`);
    setHtmlIfChanged("business-header", R.renderBusinessHeader(data.business));
    setHtmlIfChanged("agents-table", R.renderAgentsTable(data.agents));
    setHtmlIfChanged("tasks-table", R.renderTasksTable(data.tasks));
    setHtmlIfChanged("arc-summary", R.renderArcSummary(data.arc_summary));
    setHtmlIfChanged("jobs-table", R.renderJobsTable(data.scheduled_jobs));
    setHtmlIfChanged("orders-table", R.renderOrdersTable(data.orders));
    lastOrders = data.orders || [];
    renderOrdersChartPanel();
    setHtmlIfChanged("opportunities-list", R.renderOpportunitiesTable(data.opportunities));
    setHtmlIfChanged("roblox-trends-list", R.renderRobloxTrendsTable(data.roblox_trends));
    setHtmlIfChanged("app-feasibility-list", R.renderAppFeasibilityTable(data.app_feasibility_assessments));
    setHtmlIfChanged("real-estate-list", R.renderRealEstateTable(data.real_estate_assessments));
    setHtmlIfChanged("allocate-arc-agent-select", R.renderAgentOptions(data.agents));
    setHtmlIfChanged("trading-portfolio", R.renderTradingPortfolio(data.trading_portfolio));
    setHtmlIfChanged("trading-positions",
      R.renderTradingPositions(data.trading_portfolio ? data.trading_portfolio.positions : []));
    setHtmlIfChanged("trading-trades", R.renderTradingTrades(data.trading_trades));
    setHtmlIfChanged("trading-strategy-versions",
      R.renderTradingStrategyVersions(data.trading_strategy_versions));
  }

  async function refresh() {
    // Guard against overlapping calls: if a slow request from the auto-
    // refresh interval is still in flight when the next tick (or a manual
    // action) fires, skip it rather than piling up concurrent fetches.
    if (refreshInFlight) return;
    refreshInFlight = true;
    try {
      // loadOverview() runs unconditionally (global, not tied to
      // currentBusinessId) -- loadDashboard() is a no-op until a
      // business is selected.
      await Promise.all([loadOverview(), loadDashboard()]);
    } catch (e) {
      showError("Failed to refresh: " + e.message);
    } finally {
      refreshInFlight = false;
    }
  }

  // --- event wiring ---

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      await loadOverview();
    } catch (e) {
      showError("Failed to load overview: " + e.message);
    }

    try {
      await loadBusinessList();
    } catch (e) {
      showError("Failed to load businesses: " + e.message);
    }

    // Poll for changes made server-side without any click here — this is
    // the whole point of the scheduler: tasks it creates should show up
    // on their own, not only when some unrelated button happens to call
    // refresh(). Found missing during real click-through testing (a
    // scheduled job's tasks only appeared after clicking Disable, which
    // incidentally triggered a refresh).
    setInterval(refresh, AUTO_REFRESH_INTERVAL_MS);

    document.getElementById("business-select").addEventListener("change", async (ev) => {
      currentBusinessId = ev.target.value || null;
      await refresh();
    });

    document.getElementById("refresh-btn").addEventListener("click", refresh);

    document.getElementById("create-business-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const f = ev.target;
      try {
        const result = await api("/businesses", {
          method: "POST",
          body: JSON.stringify({
            name: f.name.value,
            type: f.type.value || null,
            objective: f.objective.value || null,
            budget_usd: parseFloat(f.budget_usd.value) || 0,
          }),
        });
        f.reset();
        currentBusinessId = result.id;
        await loadBusinessList();
        document.getElementById("business-select").value = currentBusinessId;
        await refresh();
      } catch (e) {
        showError("Failed to create business: " + e.message);
      }
    });

    document.getElementById("create-agent-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      try {
        await api(`/businesses/${currentBusinessId}/agents`, {
          method: "POST",
          body: JSON.stringify({
            name: f.name.value,
            role: f.role.value || null,
            department: f.department.value || null,
            permission_level: parseInt(f.permission_level.value, 10) || 1,
          }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to create agent: " + e.message);
      }
    });

    document.getElementById("create-task-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      try {
        await api(`/businesses/${currentBusinessId}/tasks`, {
          method: "POST",
          body: JSON.stringify({
            objective: f.objective.value,
            department: f.department.value || null,
            permission_level_required: parseInt(f.permission_level_required.value, 10) || 1,
          }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to create task: " + e.message);
      }
    });

    document.getElementById("create-job-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      try {
        await api(`/businesses/${currentBusinessId}/scheduled-jobs`, {
          method: "POST",
          body: JSON.stringify({
            name: f.name.value,
            objective: f.objective.value,
            interval_seconds: parseInt(f.interval_seconds.value, 10),
            department: f.department.value || null,
            permission_level_required: parseInt(f.permission_level_required.value, 10) || 1,
          }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to create scheduled job: " + e.message);
      }
    });

    document.getElementById("research-opportunity-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      const urlsRaw = f.reference_urls.value.trim();
      const reference_urls = urlsRaw
        ? urlsRaw.split(",").map((u) => u.trim()).filter(Boolean)
        : [];
      if (reference_urls.length > 3) {
        showError("Reference URLs are capped at 3 (comma-separated).");
        return;
      }
      try {
        await api(`/businesses/${currentBusinessId}/opportunities/research`, {
          method: "POST",
          body: JSON.stringify({ topic: f.topic.value, reference_urls }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to request opportunity research: " + e.message);
      }
    });

    document.getElementById("research-roblox-trend-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      const urlsRaw = f.reference_urls.value.trim();
      const reference_urls = urlsRaw
        ? urlsRaw.split(",").map((u) => u.trim()).filter(Boolean)
        : [];
      if (reference_urls.length > 3) {
        showError("Reference URLs are capped at 3 (comma-separated).");
        return;
      }
      try {
        await api(`/businesses/${currentBusinessId}/roblox-trends/research`, {
          method: "POST",
          body: JSON.stringify({ concept: f.concept.value, reference_urls }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to request Roblox trend research: " + e.message);
      }
    });

    document.getElementById("research-app-feasibility-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      const urlsRaw = f.reference_urls.value.trim();
      const reference_urls = urlsRaw
        ? urlsRaw.split(",").map((u) => u.trim()).filter(Boolean)
        : [];
      if (reference_urls.length > 3) {
        showError("Reference URLs are capped at 3 (comma-separated).");
        return;
      }
      try {
        await api(`/businesses/${currentBusinessId}/app-feasibility/research`, {
          method: "POST",
          body: JSON.stringify({ concept: f.concept.value, reference_urls }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to request app feasibility research: " + e.message);
      }
    });

    document.getElementById("research-real-estate-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      const urlsRaw = f.reference_urls.value.trim();
      const reference_urls = urlsRaw
        ? urlsRaw.split(",").map((u) => u.trim()).filter(Boolean)
        : [];
      if (reference_urls.length > 3) {
        showError("Reference URLs are capped at 3 (comma-separated).");
        return;
      }
      try {
        await api(`/businesses/${currentBusinessId}/real-estate/research`, {
          method: "POST",
          body: JSON.stringify({ property_or_market: f.property_or_market.value, reference_urls }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to request real estate research: " + e.message);
      }
    });

    document.getElementById("create-trading-portfolio-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      const watchlistRaw = f.watchlist.value.trim();
      const watchlist = watchlistRaw
        ? watchlistRaw.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean)
        : [];
      try {
        await api(`/businesses/${currentBusinessId}/trading/portfolio`, {
          method: "POST",
          body: JSON.stringify({
            starting_cash_usd: parseFloat(f.starting_cash_usd.value) || 10000,
            watchlist,
          }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to create paper trading portfolio: " + e.message);
      }
    });

    document.getElementById("enable-auto-trading-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      try {
        await api(`/businesses/${currentBusinessId}/trading/enable-auto-trading`, {
          method: "POST",
          body: JSON.stringify({
            cycle_interval_seconds: parseInt(f.cycle_interval_seconds.value, 10) || 14400,
            review_interval_seconds: parseInt(f.review_interval_seconds.value, 10) || 86400,
          }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to enable auto-trading: " + e.message);
      }
    });

    document.getElementById("trigger-cycle-btn").addEventListener("click", async () => {
      if (!currentBusinessId) { showError("Select a business first."); return; }
      try {
        await api(`/businesses/${currentBusinessId}/trading/cycle`, { method: "POST", body: "{}" });
        await refresh();
      } catch (e) {
        showError("Failed to trigger trading cycle: " + e.message);
      }
    });

    document.getElementById("trigger-review-btn").addEventListener("click", async () => {
      if (!currentBusinessId) { showError("Select a business first."); return; }
      try {
        await api(`/businesses/${currentBusinessId}/trading/strategy-review`, { method: "POST", body: "{}" });
        await refresh();
      } catch (e) {
        showError("Failed to trigger strategy review: " + e.message);
      }
    });

    document.getElementById("trigger-ops-review-btn").addEventListener("click", async () => {
      // Business-independent -- no currentBusinessId guard needed, unlike
      // the trading triggers above.
      try {
        await api("/ops/review", { method: "POST", body: "{}" });
        await refresh();
      } catch (e) {
        showError("Failed to trigger ops review: " + e.message);
      }
    });

    document.getElementById("allocate-arc-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!currentBusinessId) { showError("Select a business first."); return; }
      const f = ev.target;
      if (!f.agent_id.value) { showError("No agent selected to allocate ARC to."); return; }
      try {
        await api(`/businesses/${currentBusinessId}/banker/allocate`, {
          method: "POST",
          body: JSON.stringify({
            agent_id: f.agent_id.value,
            amount: parseFloat(f.amount.value),
            reason: f.reason.value || "budget allocation",
          }),
        });
        f.reset();
        await refresh();
      } catch (e) {
        showError("Failed to allocate ARC: " + e.message);
      }
    });

    // Event delegation for buttons rendered dynamically inside tables/lists.
    // Dispatches on the explicit data-action attribute, NOT on CSS classes —
    // classes are for styling only. A real bug happened here once already:
    // the job-toggle button reused .btn-pause/.btn-approve for styling, and
    // a class-based handler misrouted clicks on it to the agent/approval
    // endpoints with no valid id, producing a 404. data-action makes that
    // class of bug structurally impossible, however styling is reused later.
    document.body.addEventListener("click", async (ev) => {
      const t = ev.target;
      const action = t.dataset.action;
      if (!action) return;
      try {
        if (action === "pause-agent") {
          await api(`/agents/${t.dataset.agentId}/pause`, { method: "POST", body: "{}" });
          await refresh();
        } else if (action === "retire-agent") {
          await api(`/agents/${t.dataset.agentId}/retire`, { method: "POST", body: "{}" });
          await refresh();
        } else if (action === "approve") {
          await api(`/approvals/${t.dataset.approvalId}/approve`, { method: "POST", body: "{}" });
          await refresh();
        } else if (action === "reject") {
          await api(`/approvals/${t.dataset.approvalId}/reject`, { method: "POST", body: "{}" });
          await refresh();
        } else if (action === "set-job-enabled") {
          const enabled = t.dataset.setEnabled === "true";
          await api(`/scheduled-jobs/${t.dataset.jobId}/set-enabled`, {
            method: "POST",
            body: JSON.stringify({ enabled }),
          });
          await refresh();
        } else if (action === "delete-opportunity") {
          if (!currentBusinessId) return;
          await api(`/businesses/${currentBusinessId}/opportunities/${t.dataset.id}`, { method: "DELETE" });
          await refresh();
        } else if (action === "delete-roblox-trend") {
          if (!currentBusinessId) return;
          await api(`/businesses/${currentBusinessId}/roblox-trends/${t.dataset.id}`, { method: "DELETE" });
          await refresh();
        } else if (action === "delete-app-feasibility") {
          if (!currentBusinessId) return;
          await api(`/businesses/${currentBusinessId}/app-feasibility/${t.dataset.id}`, { method: "DELETE" });
          await refresh();
        } else if (action === "delete-real-estate") {
          if (!currentBusinessId) return;
          await api(`/businesses/${currentBusinessId}/real-estate/${t.dataset.id}`, { method: "DELETE" });
          await refresh();
        } else if (action === "set-orders-range") {
          // Pure display state over data already in hand -- no network
          // call, so this never needs to go through refresh().
          ordersChartRangeDays = parseInt(t.dataset.range, 10) || 7;
          document.querySelectorAll('[data-action="set-orders-range"]').forEach((b) => {
            b.setAttribute("aria-pressed", b === t ? "true" : "false");
          });
          document.getElementById("orders-chart-selection").textContent = "";
          renderOrdersChartPanel();
        } else if (action === "select-order-bar") {
          document.querySelectorAll(".orders-chart-bar.selected").forEach((b) => b.classList.remove("selected"));
          t.classList.add("selected");
          document.getElementById("orders-chart-selection").textContent =
            t.dataset.label + ": " + R.fmtUsd(parseFloat(t.dataset.value));
        }
      } catch (e) {
        showError("Action failed: " + e.message);
      }
    });

    // Keyboard activation for the chart bars: role="button" on an SVG
    // <rect> doesn't get free Enter/Space-triggers-click behavior the
    // way a real <button> does, so this dispatches one explicitly and
    // lets the delegated click handler above do the actual work.
    document.body.addEventListener("keydown", (ev) => {
      if ((ev.key === "Enter" || ev.key === " ") && ev.target.dataset.action === "select-order-bar") {
        ev.preventDefault();
        ev.target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      }
    });

    // Hover/focus tooltip for the revenue chart bars. mouseover/mouseout
    // (not mouseenter/mouseleave) so this can be delegated on body like
    // everything else here; focus/blur need the capture phase since
    // neither of those bubbles.
    const chartTooltip = document.getElementById("chart-tooltip");
    document.body.addEventListener("mouseover", (ev) => {
      const bar = ev.target.closest && ev.target.closest(".orders-chart-bar");
      if (!bar) return;
      chartTooltip.textContent = bar.dataset.label + ": " + R.fmtUsd(parseFloat(bar.dataset.value));
      chartTooltip.classList.remove("hidden");
    });
    document.body.addEventListener("mousemove", (ev) => {
      if (chartTooltip.classList.contains("hidden")) return;
      chartTooltip.style.left = ev.clientX + "px";
      chartTooltip.style.top = ev.clientY + "px";
    });
    document.body.addEventListener("mouseout", (ev) => {
      const bar = ev.target.closest && ev.target.closest(".orders-chart-bar");
      if (!bar || (ev.relatedTarget && bar.contains(ev.relatedTarget))) return;
      chartTooltip.classList.add("hidden");
    });
    document.body.addEventListener("focus", (ev) => {
      if (!ev.target.classList || !ev.target.classList.contains("orders-chart-bar")) return;
      const rect = ev.target.getBoundingClientRect();
      chartTooltip.textContent = ev.target.dataset.label + ": " + R.fmtUsd(parseFloat(ev.target.dataset.value));
      chartTooltip.style.left = (rect.left + rect.width / 2) + "px";
      chartTooltip.style.top = rect.top + "px";
      chartTooltip.classList.remove("hidden");
    }, true);
    document.body.addEventListener("blur", (ev) => {
      if (ev.target.classList && ev.target.classList.contains("orders-chart-bar")) {
        chartTooltip.classList.add("hidden");
      }
    }, true);

    // Click-ripple on every button on the page, uniformly -- one
    // delegated listener rather than wiring it into each of the many
    // individual button handlers above and elsewhere in this file.
    document.body.addEventListener("click", (ev) => {
      const btn = ev.target.closest && ev.target.closest("button");
      if (btn) addRipple(btn, ev);
    });
  });
})();
