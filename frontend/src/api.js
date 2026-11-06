export const BASE = (process.env.REACT_APP_API_URL || "http://localhost:8000").replace(/\/$/, "");

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail;
    const message = typeof detail === "string" ? detail : detail?.error || `Request failed (${res.status})`;
    throw new Error(message);
  }
  return body;
}

const json = (method, data) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: data ? JSON.stringify(data) : undefined,
});

export const api = {
  createOrder: (text, deadline) => request("/api/orders", json("POST", { text, deadline })),
  listOrders: () => request("/api/orders"),
  getOrder: (id) => request(`/api/orders/${id}`),
  chooseProduct: (id, productId, quantity) =>
    request(`/api/orders/${id}/product`, json("POST", { product_id: productId, quantity })),
  approve: (id) => request(`/api/orders/${id}/approve`, json("POST")),
  paypalReturn: (id, token) => request(`/api/orders/${id}/paypal-return`, json("POST", { token })),
  delivered: (id) => request(`/api/orders/${id}/delivered`, json("POST")),
  report: (id, note, files) => {
    const form = new FormData();
    form.append("note", note);
    [...files].forEach((f) => form.append("photos", f));
    return request(`/api/orders/${id}/report`, { method: "POST", body: form });
  },
  ask: (id, question) => request(`/api/orders/${id}/ask`, json("POST", { question })),
  sellers: () => request("/api/sellers"),
  sellerSchedule: () => request("/api/sellers/schedule"),
  analytics: () => request("/api/analytics"),
  health: () => request("/health"),
  // tz: so dates are placed on the viewer's own calendar days
  timeline: (id) => request(`/api/orders/${id}/timeline?tz=${new Date().getTimezoneOffset()}`),
  calendar: () => request(`/api/calendar?tz=${new Date().getTimezoneOffset()}`),
  // The owner's PayPal, saved once (Vault v3)
  wallet: () => request("/api/paypal-wallet"),
  walletConnect: (monthlyLimit, autoPay) =>
    request("/api/paypal-wallet/connect", json("POST", { monthly_limit: monthlyLimit, auto_pay: autoPay })),
  walletFinish: (setupTokenId) =>
    request("/api/paypal-wallet/finish", json("POST", { setup_token_id: setupTokenId || null })),
  walletUpdate: (settings) => request("/api/paypal-wallet", json("PATCH", settings)),
  walletDisconnect: () => request("/api/paypal-wallet", json("DELETE")),
  // Ask the seller agent to deliver on another day
  reschedule: (id, date) => request(`/api/orders/${id}/reschedule`, json("POST", { delivery_date: date })),
  // Every signed step with its fingerprint, and PayPal's own record of the money
  receipt: (id) => request(`/api/orders/${id}/receipt`),
  receiptPayPal: (id) => request(`/api/orders/${id}/receipt/paypal`),
  // Sharing a bulk order with a partner business (PayPal invoices)
  share: (id, partner) => request(`/api/orders/${id}/shares`, json("POST", partner)),
  refreshShares: (id) => request(`/api/orders/${id}/shares/refresh`, json("POST")),
};