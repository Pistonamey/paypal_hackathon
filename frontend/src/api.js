const BASE = (process.env.REACT_APP_API_URL || "http://localhost:8000").replace(/\/$/, "");

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
  createOrder: (text) => request("/api/orders", json("POST", { text })),
  listOrders: () => request("/api/orders"),
  getOrder: (id) => request(`/api/orders/${id}`),
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
};
