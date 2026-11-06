// The dashboard's AI team, all running on Claude through ./claudeAdapter.
//
//   Procuro Analyst (primary, ours)  knows how Procuro works: mandates, negotiation, PayPal holds.
//                                    Answers spending questions, reads an order's real agent
//                                    conversation, and delegates building to Studio's own agents.
//   Studio's built-in agents         lead, data, page, planning, widget: build and edit widgets/pages.

import { createAiHarness, directLlmRunner } from "ag-studio";
import { api as backend } from "../api";
import { claudeAdapter } from "./claudeAdapter";

const PROMPT_STARTERS = [
  { label: "What did negotiation save?", prompt: "How much has negotiation saved, and which seller gave up the most?" },
  { label: "Money on hold", prompt: "Which orders still have money on hold in PayPal, and what happens next?" },
  { label: "Problem orders", prompt: "Add a widget showing problem orders by seller and explain what happened to the money." },
  { label: "Why this seller?", prompt: "For the most recent live order, explain why the agent picked that seller." },
];

const ANALYST_INSTRUCTIONS = `You are the Procuro Analyst, the spending analyst inside Procuro.

How Procuro works (use this to explain numbers, never contradict it):
- The owner writes a mandate: what to buy, a budget, an approval limit and a delivery deadline.
- Their buying agent negotiates with independent seller agents (QuickPrint, BoxCraft, PackRight).
  Sellers that can't deliver by the deadline are dropped. The lowest total that arrives in time wins.
- Payment uses PayPal authorization: money is HELD at approval and only PAID to the seller when the
  owner confirms delivery. If an order arrives damaged, the agents negotiate and PayPal captures
  only part of the hold; the rest is RETURNED to the owner the same day. If no deal is reached a
  dispute file is prepared and the money stays on hold.
- "Saved by negotiation" = winning seller's opening quote minus the final price.

How to work:
- Answer with real numbers from the data (use the query tool). Keep answers short and plain.
- To explain a specific live order's negotiation, read it with get_order_story instead of guessing.
- To add, change or arrange widgets or pages, delegate to the built-in agents. Prefer Procuro's own
  widgets when they fit: "money-status" (held/paid/returned per group) and "negotiation-receipt"
  (first quote vs negotiated price per group).`;

function summariseStory(order, messages) {
  const lines = messages.map((m) => `[${m.from}${m.to ? ` → ${m.to}` : ""}] ${m.text}`);
  const w = order.winner || {};
  return [
    `Order ${order.id}: ${order.mandate?.quantity} × ${order.mandate?.item}, status ${order.status}.`,
    w.seller_name ? `Winner: ${w.seller_name} at ${w.total} (${w.unit_price}/unit), delivery ${w.delivery_date}.` : "No winner.",
    `Agent conversation (${lines.length} messages):`,
    ...lines.slice(-60),
  ].join("\n");
}

export function buildAi(endpoint) {
  const adapter = claudeAdapter({ endpoint });

  return ({ api }) => {
    const orderStory = api.defineAiTool({
      name: "get_order_story",
      description:
        "Read the full agent-to-agent conversation and payment record for one LIVE order by its order id " +
        "(orders.id, e.g. ord_1a2b3c).",
      params: (s) => s.object({ orderId: s.string({ description: "The order id, e.g. ord_1a2b3c4d5e." }) }),
      execute: async (args, ctx) => {
        try {
          const { order, messages } = await backend.getOrder(args.orderId);
          return ctx.success(summariseStory(order, messages), { orderId: order.id, status: order.status });
        } catch (e) {
          return ctx.error(`Could not load order ${args.orderId}: ${e.message}`);
        }
      },
    });

    return createAiHarness(api, ({ builtIn, tools }) => {
      const studio = tools.studio;
      const analyst = directLlmRunner({
        id: "mandate",
        name: "Procuro Analyst",
        description: "Explains spending, savings, holds and problems in Procuro's own terms.",
        adapter,
        instructions: () => ANALYST_INSTRUCTIONS,
        tools: () => [
          studio.viewSchema(),
          studio.executeQuery(),
          studio.viewPage(),
          orderStory,
          tools.delegateTo(Object.keys(builtIn)),
        ],
        maxTurns: 30,
      });
      return {
        agents: [analyst, ...Object.values(builtIn).map((definition) => directLlmRunner({ ...definition, adapter }))],
        primary: "mandate",
        promptStarters: PROMPT_STARTERS,
      };
    });
  };
}