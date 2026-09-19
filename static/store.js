// store.js — the public storefront. Fetches the real product catalog
// (prices come from the server, never hardcoded here) and, on submit,
// asks the server to start a real Stripe Checkout session and redirects
// the customer to Stripe's own hosted payment page. This page never
// touches a card number itself.

(function () {
  "use strict";

  function fmtPrice(cents) {
    return "$" + (cents / 100).toFixed(2);
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function renderProducts(products, storeConfigured) {
    const container = document.getElementById("products");
    if (!storeConfigured) {
      container.innerHTML =
        '<p style="text-align:center;color:#b91c1c;">The store is not accepting orders ' +
        "yet — please check back soon.</p>";
      return;
    }
    if (!products || products.length === 0) {
      container.innerHTML = '<p style="text-align:center;color:#6b6b6b;">No products available.</p>';
      return;
    }
    container.innerHTML = products
      .map(
        (p) => `
      <div class="product-card">
        <h2>${escapeHtml(p.name)}</h2>
        <div class="product-price">${fmtPrice(p.price_usd_cents)}</div>
        <p class="product-description">${escapeHtml(p.description)}</p>
        <form class="order-form" data-product-type="${escapeHtml(p.product_type)}">
          <input name="topic" placeholder="What should we research? (e.g. a topic, niche, or game concept)" required>
          <input name="customer_email" type="email" placeholder="Your email (report is sent here)" required>
          <button type="submit">Pay ${fmtPrice(p.price_usd_cents)} &amp; Order Report</button>
          <p class="form-error"></p>
        </form>
      </div>`
      )
      .join("");

    container.querySelectorAll(".order-form").forEach((form) => {
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const errorEl = form.querySelector(".form-error");
        errorEl.classList.remove("visible");
        const button = form.querySelector("button");
        button.disabled = true;
        button.textContent = "Redirecting to secure checkout…";
        try {
          const res = await fetch("/store/checkout", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              product_type: form.dataset.productType,
              topic: form.topic.value,
              customer_email: form.customer_email.value,
            }),
          });
          if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.detail || `${res.status} ${res.statusText}`);
          }
          const data = await res.json();
          window.location.href = data.checkout_url;
        } catch (e) {
          errorEl.textContent = "Could not start checkout: " + e.message;
          errorEl.classList.add("visible");
          button.disabled = false;
          button.textContent = button.dataset.originalText || "Try again";
        }
      });
    });
  }

  async function init() {
    try {
      const res = await fetch("/store/products");
      const data = await res.json();
      renderProducts(data.products, data.store_configured);
    } catch (e) {
      document.getElementById("products").innerHTML =
        '<p style="text-align:center;color:#b91c1c;">Could not load products: ' +
        escapeHtml(e.message) + "</p>";
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
