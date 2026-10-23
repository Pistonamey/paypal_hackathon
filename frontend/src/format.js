export function money(value, currency = "USD") {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(Number(value));
}

export function shortDate(iso) {
  if (!iso) return "—";
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export const AGENT_NAMES = {
  buyer: "Your agent",
  paypal: "PayPal",
  system: "System",
};

export function agentName(id, sellers) {
  return AGENT_NAMES[id] || sellers?.[id]?.name || id;
}
