// The built-in "Spend control" report: two pages of AG Studio report state.
// Field ids ('orders.paidToSeller') refer to the data sources in ./studioData.js.

const at = (xTrack, yTrack, xSpan, ySpan) => ({ xTrack, yTrack, xSpan, ySpan });
const title = (text) => ({ title: { enabled: true, text } });
const sum = (id) => ({ id, aggregation: "sum" });
const avg = (id) => ({ id, aggregation: "avg" });
const count = (id) => ({ id, aggregation: "countd" });

const FONT = '"Bricolage Grotesque", "Helvetica Neue", Arial, sans-serif';
const typo = (fontSize, fontWeight = "normal") => ({ fontFamily: FONT, fontSize, fontWeight, fontStyle: "normal" });

const kpi = (text, value, sparklineX) => ({
  type: "value",
  dataMapping: { value: [value], ...(sparklineX && { sparklineX: [{ id: sparklineX }] }) },
  format: {
    title: { enabled: true, text, typography: typo(14, "bold") },
    caption: { enabled: true, text: "", typography: typo(0) },
    style: { typography: typo(26, "bold") },
  },
});

const overview = {
  id: "overview",
  widgets: {
    "kpi-paid": kpi("Paid to sellers", sum("orders.paidToSeller"), "calendar::month"),
    "kpi-saved": kpi("Saved by negotiation", sum("orders.savedByNegotiation"), "calendar::month"),
    "kpi-held": kpi("On hold in PayPal", sum("orders.held")),
    "kpi-returned": kpi("Returned after problems", sum("orders.returnedToYou")),
    "money-by-seller": {
      type: "money-status",
      dataMapping: {
        category: [{ id: "orders.seller" }],
        held: [sum("orders.held")],
        paid: [sum("orders.paidToSeller")],
        returned: [sum("orders.returnedToYou")],
      },
      format: title("Where the money is, by seller"),
    },
    "receipt-by-seller": {
      type: "negotiation-receipt",
      dataMapping: {
        category: [{ id: "orders.seller" }],
        opening: [sum("orders.openingQuote")],
        final: [sum("orders.finalPrice")],
      },
      format: title("Negotiation receipt"),
    },
    "spend-by-month": {
      type: "column-chart-stacked",
      dataMapping: {
        categoryKey: [{ id: "calendar::month" }],
        valueKey: [sum("orders.paidToSeller"), sum("orders.savedByNegotiation")],
      },
      format: {
        ...title("Paid and saved, by month"),
        style: { theme: { common: { legend: { enabled: true } } } },
      },
    },
    "status-donut": {
      type: "donut-chart",
      dataMapping: {
        categoryKey: [{ id: "orders.statusGroup" }],
        valueKey: [sum("orders.orderCount")],
      },
      format: title("Orders by outcome"),
    },
    orders: {
      type: "grid",
      dataMapping: {
        // Plain fields (no aggregation): one row per order.
        cols: [
          { id: "orders.orderDate" },
          { id: "orders.item" },
          { id: "orders.seller" },
          { id: "orders.statusGroup" },
          { id: "orders.budget" },
          { id: "orders.openingQuote" },
          { id: "orders.finalPrice" },
          { id: "orders.savedByNegotiation" },
          { id: "orders.held" },
          { id: "orders.paidToSeller" },
          { id: "orders.returnedToYou" },
        ],
      },
      sort: [{ field: { id: "orders.orderDate" }, direction: "desc" }],
      format: title("Every order"),
    },
  },
  widgetLayout: {
    "kpi-paid": at(0, 0, 6, 8),
    "kpi-saved": at(6, 0, 6, 8),
    "kpi-held": at(12, 0, 6, 8),
    "kpi-returned": at(18, 0, 6, 8),
    "money-by-seller": at(0, 8, 14, 15),
    "receipt-by-seller": at(14, 8, 10, 15),
    "spend-by-month": at(0, 23, 16, 17),
    "status-donut": at(16, 23, 8, 17),
    orders: at(0, 40, 24, 22),
  },
};

const negotiation = {
  id: "negotiation",
  widgets: {
    "kpi-savings-rate": kpi("Savings rate", { id: "savingsRate" }),
    "kpi-problem-rate": kpi("Problem rate", { id: "problemRate" }),
    "kpi-offers": kpi("Offers per order", avg("orders.offersReceived")),
    "kpi-early": kpi("Days early, on average", avg("orders.daysBeforeDeadline")),
    "concession-curve": {
      type: "line-chart",
      dataMapping: {
        categoryKey: [{ id: "offers.round" }],
        valueKey: [avg("offers.unitPrice")],
        legendKey: [{ id: "offers.seller" }],
      },
      format: {
        ...title("How far each seller moves, round by round (avg unit price)"),
        style: { theme: { common: { legend: { enabled: true } } } },
      },
    },
    reliability: {
      type: "bar-chart-grouped",
      dataMapping: {
        categoryKey: [{ id: "sellers.name" }],
        valueKey: [sum("sellers.reliability")],
      },
      format: title("Seller reliability score"),
    },
    "wins-by-seller": {
      type: "pie-chart",
      dataMapping: {
        categoryKey: [{ id: "orders.seller" }],
        valueKey: [sum("orders.orderCount")],
      },
      format: title("Orders won"),
    },
    problems: {
      type: "grid",
      dataMapping: {
        cols: [
          { id: "orders.orderDate" },
          { id: "orders.item" },
          { id: "orders.seller" },
          { id: "orders.statusGroup" },
          { id: "orders.damagedUnits" },
          { id: "orders.paidToSeller" },
          { id: "orders.returnedToYou" },
        ],
      },
      sort: [{ field: { id: "orders.damagedUnits" }, direction: "desc" }],
      format: title("Orders that arrived with a problem"),
    },
  },
  // Widget-level filters: the problems grid only lists orders that had a problem, and the
  // win share leaves out orders that ended with no deal (no seller).
  filter: {
    widget: {
      problems: [{ field: { id: "orders.hadProblem" }, model: { operator: "isTrue" } }],
      "wins-by-seller": [{ field: { id: "orders.seller" }, model: { operator: "isNotBlank" } }],
    },
  },
  widgetLayout: {
    "kpi-savings-rate": at(0, 0, 6, 7),
    "kpi-problem-rate": at(6, 0, 6, 7),
    "kpi-offers": at(12, 0, 6, 7),
    "kpi-early": at(18, 0, 6, 7),
    "concession-curve": at(0, 7, 16, 18),
    "wins-by-seller": at(16, 7, 8, 18),
    reliability: at(0, 25, 10, 16),
    problems: at(10, 25, 14, 16),
  },
};

export const spendReport = {
  selectedPageId: overview.id,
  pages: [overview, negotiation],
  // Each page carries its own filter widget, so the side filters panel starts closed.
  panels: { filters: { collapsed: true } },
};

export const PAGES = [
  { id: overview.id, title: "Spend overview" },
  { id: negotiation.id, title: "Sellers and negotiation" },
];