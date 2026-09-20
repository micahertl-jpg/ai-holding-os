// store-success.js — polls the order's real status after a customer
// returns from Stripe Checkout. Never claims completion until the
// server actually reports status='fulfilled' (which only happens
// after fulfillment.py has confirmed a real email was sent).

(function () {
  "use strict";

  const POLL_INTERVAL_MS = 3000;
  const MAX_POLLS = 60; // ~3 minutes, generous for a real LLM research call

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function getOrderId() {
    return new URLSearchParams(window.location.search).get("order_id");
  }

  function render(order, timedOut) {
    const el = document.getElementById("status-page");
    if (!order) {
      el.innerHTML = `
        <div class="status-icon status-failed">&#9888;</div>
        <h1>Order not found</h1>
        <p>We couldn't find that order. If you were just charged, contact support with your
        payment confirmation.</p>`;
      return;
    }
    if (order.status === "fulfilled") {
      el.innerHTML = `
        <div class="status-icon status-fulfilled">&#10003;</div>
        <h1>Your report is on its way!</h1>
        <p>We've emailed your <strong>${escapeHtml(order.product_type)}</strong> report on
        "<strong>${escapeHtml(order.topic)}</strong>" — check your inbox (and spam folder,
        just in case).</p>
        <p class="cross-sell"><a href="/static/store.html">Order another report &rarr;</a></p>`;
      return;
    }
    if (order.status === "failed") {
      el.innerHTML = `
        <div class="status-icon status-failed">&#9888;</div>
        <h1>Something went wrong</h1>
        <p>Your order on "<strong>${escapeHtml(order.topic)}</strong>" hit a problem while we
        were preparing it. Your payment was received — please contact support for a refund
        or a retry.</p>`;
      return;
    }
    if (timedOut) {
      el.innerHTML = `
        <div class="status-icon status-pending">&#8987;</div>
        <h1>Still working on it</h1>
        <p>Your order on "<strong>${escapeHtml(order.topic)}</strong>" is taking longer than
        usual. It will still be emailed to you once ready — no need to stay on this page.</p>`;
      return;
    }
    el.innerHTML = `
      <div class="status-icon status-pending">&#8987;</div>
      <h1>Preparing your report…</h1>
      <p>This usually takes under a minute. This page will update automatically —
      you can also close it and wait for the email.</p>`;
  }

  async function poll(orderId, attempt) {
    let order = null;
    try {
      const res = await fetch(`/store/orders/${orderId}`);
      if (res.ok) order = await res.json();
    } catch (e) {
      // network blip — just try again next tick
    }

    if (order && (order.status === "fulfilled" || order.status === "failed")) {
      render(order, false);
      return;
    }
    if (attempt >= MAX_POLLS) {
      render(order, true);
      return;
    }
    render(order, false);
    setTimeout(() => poll(orderId, attempt + 1), POLL_INTERVAL_MS);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const orderId = getOrderId();
    if (!orderId) {
      render(null, false);
      return;
    }
    poll(orderId, 0);
  });
})();
