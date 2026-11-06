// Two custom AG Studio widgets that speak Mandate's own language:
//   money-status        where the money is for each group: held in PayPal, paid to the seller, returned to you
//   negotiation-receipt what each seller first asked for, struck through, next to what was negotiated
// Both are ordinary Studio widgets: their fields are chosen in the data panel, page filters and
// cross-filters apply, and the AI can place them like any built-in widget.

import { useEffect, useState } from "react";
import { createWidgets } from "ag-studio-react";

const usd = (v) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? "—"
    : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(Number(v));

// Studio returns one row per group, keyed by field. Look a value up by the field's id, and fall
// back to the column position so a widget never breaks on a key naming change.
function cell(row, field, index) {
  if (!field || !row) return undefined;
  if (field.id in row) return row[field.id];
  const key = Object.keys(row).find((k) => k.endsWith(field.id) || k.includes(field.id));
  if (key) return row[key];
  return Object.values(row)[index];
}

function useWidgetRows(params, slots) {
  const { widgetApi, dataMapping } = params;
  const [rows, setRows] = useState([]);
  const mapped = slots.map((s) => dataMapping?.[s]?.[0]);
  const signature = JSON.stringify(mapped.map((f) => f && [f.id, f.aggregation]));

  useEffect(() => {
    let cancelled = false;
    const fields = mapped.filter(Boolean);
    if (!mapped[0] || fields.length < 2) {
      widgetApi.setDisplayState("incompleteDataMapping");
      return undefined;
    }
    widgetApi.setDisplayState("loading", { prominent: rows.length === 0 });
    widgetApi
      .getData({ fields, limit: { count: 50 } })
      .then((res) => {
        if (cancelled) return;
        const out = (res.results?.rows || []).map((row) => {
          const values = {};
          let i = 0;
          slots.forEach((slot, idx) => {
            if (mapped[idx]) values[slot] = cell(row, mapped[idx], i++);
          });
          return values;
        });
        setRows(out);
        widgetApi.setDisplayState(out.length ? "displayed" : "noData");
      })
      .catch(() => !cancelled && widgetApi.setDisplayState("noData"));
    return () => {
      cancelled = true;
    };
    // Re-query whenever Studio re-renders the widget (new fields, filters or data).
  }, [signature, params]);

  return rows;
}

// ---------------------------------------------------------------- money status

function MoneyStatus(params) {
  const rows = useWidgetRows(params, ["category", "held", "paid", "returned"]).filter(
    (r) => r.category !== null && r.category !== undefined && r.category !== "" &&
      (+r.held || 0) + (+r.paid || 0) + (+r.returned || 0) > 0
  );
  const max = Math.max(1, ...rows.map((r) => (+r.held || 0) + (+r.paid || 0) + (+r.returned || 0)));
  return (
    <div className="w-money">
      <ul>
        {rows.map((r, i) => {
          const held = +r.held || 0;
          const paid = +r.paid || 0;
          const back = +r.returned || 0;
          const total = held + paid + back;
          return (
            <li key={`${r.category}-${i}`}>
              <div className="w-money-head">
                <span className="w-money-name">{r.category ?? "(none)"}</span>
                <span className="w-money-sum">
                  {held > 0 && <b className="t-held">{usd(held)} held</b>}
                  {paid > 0 && <span>{usd(paid)} paid</span>}
                  {back > 0 && <span className="t-back">{usd(back)} back</span>}
                </span>
              </div>
              <div className="w-bar" style={{ width: `${(total / max) * 100}%` }}
                role="img" aria-label={`${r.category}: ${usd(held)} held, ${usd(paid)} paid, ${usd(back)} returned`}>
                {paid > 0 && <span className="w-seg w-paid" style={{ flex: paid }} />}
                {back > 0 && <span className="w-seg w-back" style={{ flex: back }} />}
                {held > 0 && <span className="w-seg w-held" style={{ flex: held }} />}
              </div>
            </li>
          );
        })}
      </ul>
      <p className="w-legend">
        <i className="k k-held" />On hold in PayPal <i className="k k-paid" />Paid to seller <i className="k k-back" />Returned to you
      </p>
    </div>
  );
}

// ---------------------------------------------------------------- negotiation receipt

function NegotiationReceipt(params) {
  const rows = useWidgetRows(params, ["category", "opening", "final"]).filter(
    (r) => r.category !== null && r.category !== undefined && r.category !== "" && (+r.opening || 0) > 0
  );
  const opening = rows.reduce((a, r) => a + (+r.opening || 0), 0);
  const final = rows.reduce((a, r) => a + (+r.final || 0), 0);
  const saved = opening - final;
  return (
    <div className="w-receipt">
      <ul>
        {rows.map((r, i) => {
          const o = +r.opening || 0;
          const f = +r.final || 0;
          const pct = o > 0 ? Math.round(((o - f) / o) * 100) : 0;
          return (
            <li key={`${r.category}-${i}`}>
              <span className="w-r-name">{r.category ?? "(none)"}</span>
              <s className="w-r-was">{usd(o)}</s>
              <span className="w-r-now">{usd(f)}</span>
              <span className="w-r-pct">{pct > 0 ? `−${pct}%` : "—"}</span>
            </li>
          );
        })}
      </ul>
      <p className="w-r-total">
        Your agent negotiated <b>{usd(saved)}</b> off first quotes
        {opening > 0 && <> ({Math.round((saved / opening) * 100)}%)</>}.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------- definitions

const numeric = (aiDescription) => ({
  type: "field",
  supportedRoles: ["numeric"],
  requires: { per: "dataMapping.category", cardinality: "one" },
  aiDescription,
});
const category = {
  type: "field",
  supportedRoles: ["category"],
  requires: { cardinality: "many" },
  required: true,
  aiDescription: "What to group by, for example orders.seller or orders.item.",
};

export const moneyStatusWidget = {
  id: "money-status",
  label: "Money status",
  icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="5" width="13" height="6" rx="1"/><path d="M6 5v6M10 5v6"/></svg>',
  dataMapping: {
    category,
    held: numeric("Money still on hold in PayPal, e.g. sum of orders.held."),
    paid: numeric("Money paid to the seller, e.g. sum of orders.paidToSeller."),
    returned: numeric("Money returned to the buyer, e.g. sum of orders.returnedToYou."),
  },
  form: ({ createDefaults }) =>
    createDefaults({
      dataMappingItems: [
        { key: "category", label: "Group by" },
        { key: "held", label: "On hold" },
        { key: "paid", label: "Paid to seller" },
        { key: "returned", label: "Returned to you" },
      ],
    }),
  comp: MoneyStatus,
  defaultSize: { width: 12, height: 16 },
  minSize: { width: 6, height: 8 },
  ai: {
    label: "Money status",
    description: "Stacked bar per group showing how much money is held in PayPal, paid to the seller and returned to the buyer.",
    usage: "Use to show where money stands, e.g. by seller or by item. Map held, paid and returned to the matching sums.",
  },
};

export const negotiationReceiptWidget = {
  id: "negotiation-receipt",
  label: "Negotiation receipt",
  icon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 1.5h10v13l-2-1.2-1.6 1.2-1.4-1.2-1.4 1.2L5 13.3l-2 1.2z"/><path d="M5.5 5.5h5M5.5 8h5"/></svg>',
  dataMapping: {
    category,
    opening: numeric("The first price quoted, e.g. sum of orders.openingQuote."),
    final: numeric("The negotiated price, e.g. sum of orders.finalPrice."),
  },
  form: ({ createDefaults }) =>
    createDefaults({
      dataMappingItems: [
        { key: "category", label: "Group by" },
        { key: "opening", label: "First quote" },
        { key: "final", label: "Negotiated price" },
      ],
    }),
  comp: NegotiationReceipt,
  defaultSize: { width: 10, height: 14 },
  minSize: { width: 6, height: 8 },
  ai: {
    label: "Negotiation receipt",
    description: "Receipt-style list with each group's first quote struck through beside the negotiated price and the percentage saved.",
    usage: "Use to show what negotiation saved, e.g. by seller. Map opening to openingQuote and final to finalPrice.",
  },
};

export const buildWidgets = (widgetConfig) =>
  createWidgets({
    additionalTypes: [moneyStatusWidget, negotiationReceiptWidget],
    menu: [...widgetConfig.menu, { label: "Mandate", widgetIds: ["money-status", "negotiation-receipt"] }],
  });
