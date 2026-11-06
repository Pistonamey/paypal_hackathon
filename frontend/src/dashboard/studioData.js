// AG Studio data sources for the Spend dashboard: the tables from GET /api/analytics,
// how they relate, a calendar, and a few calculated measures.

const CALENDAR_ID = "calendar";

// Money shows as $1,234.50 everywhere (widgets, grids, tooltips).
const USD = { format: new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }) };

const fields = (specs) =>
  specs.map(([id, name, format, hide]) => ({
    id,
    name,
    format,
    ...(format === "currencyFormat" && { formatOptions: USD }),
    ...(hide && { hide }),
  }));

const manyToOne = (id, source, target) => {
  const [sourceTable, sourceField] = source.split(".");
  const [targetTable, targetField] = target.split(".");
  return {
    id,
    source: { tableId: sourceTable, fieldId: sourceField },
    target: { tableId: targetTable, fieldId: targetField },
    type: "many-to-one",
  };
};

const sumOf = (id) => ({ id, aggregation: "sum" });

const ratio = (id, name, description, numerator, denominator) => ({
  id,
  name,
  description,
  format: "percentageFormat",
  isMeasure: true,
  expression: { operator: "divide", inputs: [sumOf(numerator), sumOf(denominator)] },
});

export function buildStudioData(t) {
  const dates = [...t.orders.map((o) => o.orderDate), ...t.money.map((m) => m.date)].filter(Boolean).sort();
  const today = t.asOf.slice(0, 10);

  return {
    description:
      "Purchases made by Procuro, an AI buying agent, for a small business. Each order was negotiated with " +
      "seller agents and paid through PayPal: money is held (authorized) at approval and only paid to the " +
      "seller when delivery is confirmed. After a problem, part of the hold can be returned to the buyer. " +
      "Amounts are USD. Every row is a real order.",
    sources: [
      {
        id: "orders",
        description:
          "One row per purchase. Opening quote is the winning seller's first price; final price is the " +
          "negotiated deal. Held is money still on hold in PayPal; paid to seller and returned to you are " +
          "the outcome after delivery.",
        data: t.orders,
        fields: fields([
          ["id", "Order ID", "textFormat"],
          ["orderDate", "Order Date", "dateFormat"],
          ["item", "Item", "textFormat"],
          ["quantity", "Quantity", "integerFormat"],
          ["status", "Status Code", "textFormat", true],
          ["statusGroup", "Status", "textFormat"],
          ["sellerId", "Seller ID", "textFormat", true],
          ["seller", "Seller", "textFormat"],
          ["currency", "Currency", "textFormat", true],
          ["budget", "Budget", "currencyFormat"],
          ["approvalLimit", "Approval Limit", "currencyFormat"],
          ["openingQuote", "Opening Quote", "currencyFormat"],
          ["finalPrice", "Final Price", "currencyFormat"],
          ["unitPrice", "Unit Price", "currencyFormat"],
          ["savedByNegotiation", "Saved by Negotiation", "currencyFormat"],
          ["underBudget", "Under Budget", "currencyFormat"],
          ["held", "On Hold", "currencyFormat"],
          ["paidToSeller", "Paid to Seller", "currencyFormat"],
          ["returnedToYou", "Returned to You", "currencyFormat"],
          ["deadline", "Deadline", "dateFormat"],
          ["deliveryDate", "Delivery Date", "dateFormat"],
          ["daysBeforeDeadline", "Days Before Deadline", "integerFormat"],
          ["neededApproval", "Needed Approval", "booleanFormat"],
          ["hadProblem", "Had Problem", "booleanFormat"],
          ["problemOrders", "Problem Orders", "integerFormat"],
          ["orderCount", "Orders", "integerFormat"],
          ["damagedUnits", "Damaged Units", "integerFormat"],
          ["offersReceived", "Offers Received", "integerFormat"],
          ["marketUnitPrice", "Market Unit Price", "currencyFormat"],
        ]),
      },
      {
        id: "offers",
        description:
          "Every price a seller agent put forward during negotiation. Round 0 is the first quote; later " +
          "rounds are replies to the buyer agent's counter-offers.",
        data: t.offers,
        fields: fields([
          ["id", "Offer ID", "textFormat", true],
          ["orderId", "Order ID", "textFormat", true],
          ["sellerId", "Offer Seller ID", "textFormat", true],
          ["seller", "Offer Seller", "textFormat"],
          ["round", "Round", "integerFormat"],
          ["stage", "Stage", "textFormat"],
          ["unitPrice", "Offered Unit Price", "currencyFormat"],
          ["total", "Offered Total", "currencyFormat"],
          ["deliveryDate", "Offered Delivery", "dateFormat"],
          ["sellerOutcome", "Seller Outcome", "textFormat"],
          ["won", "Won the Order", "booleanFormat"],
        ]),
      },
      {
        id: "money",
        name: "Money Events",
        description: "PayPal money movements per order: held at approval, paid to seller, returned to you, hold renewed.",
        data: t.money,
        fields: fields([
          ["id", "Event ID", "textFormat", true],
          ["orderId", "Event Order ID", "textFormat", true],
          ["date", "Event Date", "dateFormat"],
          ["type", "Money Event", "textFormat"],
          ["amount", "Event Amount", "currencyFormat"],
          ["seller", "Event Seller", "textFormat"],
          ["sellerId", "Event Seller ID", "textFormat", true],
        ]),
      },
      {
        id: "sellers",
        description: "Seller agents and the buyer agent's reliability score for each (100 = no problems).",
        data: t.sellers,
        fields: fields([
          ["id", "Seller Key", "textFormat", true],
          ["name", "Seller Name", "textFormat"],
          ["ordersWon", "Orders Won", "integerFormat"],
          ["problemOrders", "Seller Problem Orders", "integerFormat"],
          ["reliability", "Reliability Score", "integerFormat"],
          ["quotesGiven", "Quotes Given", "integerFormat"],
          ["leadDays", "Lead Time (days)", "integerFormat"],
        ]),
      },
    ],
    relationships: [
      manyToOne("offer-order", "offers.orderId", "orders.id"),
      manyToOne("money-order", "money.orderId", "orders.id"),
      manyToOne("order-seller", "orders.sellerId", "sellers.id"),
      { id: "orders-calendar", source: { tableId: "orders", fieldId: "orderDate" }, target: { calendarId: CALENDAR_ID } },
      { id: "money-calendar", source: { tableId: "money", fieldId: "date" }, target: { calendarId: CALENDAR_ID } },
    ],
    calendars: [
      {
        id: CALENDAR_ID,
        label: "Calendar",
        range: {
          from: { type: "date", value: dates[0] ?? today },
          to: { type: "date", value: dates.at(-1) ?? today },
        },
        fragments: ["year", "quarter", "month", "week", "day", "dayOfWeek"],
      },
    ],
    expressions: [
      ratio("savingsRate", "Savings Rate", "Saved by negotiation / opening quote.", "orders.savedByNegotiation", "orders.openingQuote"),
      ratio("problemRate", "Problem Rate", "Share of orders that arrived with a problem.", "orders.problemOrders", "orders.orderCount"),
      ratio("returnedShare", "Returned Share", "Money returned to you / money paid to sellers.", "orders.returnedToYou", "orders.paidToSeller"),
    ],
  };
}