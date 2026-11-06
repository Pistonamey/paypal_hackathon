// The negotiation, messenger style: one conversation per seller agent (plus one for your agent's own
// updates and PayPal), and above it a live chart of every offer against the owner's budget and
// approval limit. Built from the same order messages the backend already records; nothing new is
// fetched.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { BASE } from "./api";
import { money, shortDate } from "./format";
import { FeedSkeleton, Typing } from "./ui";
import "./negotiation.css";

// Categorical slots 1-3 of the validated palette (blue, orange, aqua), in the sellers' fixed order,
// so a seller keeps its colour on every screen.
const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#e87ba4"];
const UPDATES = "updates";

const SELLER_STATE = {
  requested: { label: "Quoting", tone: "busy" },
  negotiating: { label: "Negotiating", tone: "busy" },
  agreed: { label: "Agreed", tone: "ok" },
  walked_away: { label: "Walked away", tone: "off" },
  rejected: { label: "Said no", tone: "off" },
  declined: { label: "Can't supply", tone: "off" },
  dropped_deadline: { label: "Too late", tone: "off" },
  not_picked: { label: "Not picked", tone: "off" },
  closed: { label: "Closed", tone: "off" },
};

const ACTIVE = new Set(["negotiating", "resolving"]);

function initials(name) {
  return (name || "?").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
}

// Which conversation a message belongs to.
function threadOf(msg, sellerIds) {
  if (sellerIds.includes(msg.from)) return [msg.from];
  if (msg.from === "buyer" && sellerIds.includes(msg.to)) return [msg.to];
  if (msg.from === "buyer" && msg.to === "all") return sellerIds; // the quote request goes to everyone
  const about = msg.data?.seller_id;
  if (about && sellerIds.includes(about) && msg.kind === "decision" && !msg.data?.winner) return [about];
  return [UPDATES];
}

// One point per offer or counter-offer, in the order they happened.
function offerPoints(messages, sellerIds, quantity) {
  const points = [];
  messages.forEach((msg) => {
    const d = msg.data || {};
    if (sellerIds.includes(msg.from) && ["offer", "accept"].includes(d.action) && d.total != null) {
      points.push({ seq: msg.seq, seller: msg.from, side: "seller", total: d.total, unit: d.unit_price,
        accept: d.action === "accept", delivery: d.delivery_date });
    } else if (msg.from === "buyer" && d.action === "counter_offer" && sellerIds.includes(msg.to)) {
      points.push({ seq: msg.seq, seller: msg.to, side: "buyer", total: d.unit_price * quantity, unit: d.unit_price });
    }
  });
  return points.map((p, i) => ({ ...p, x: i }));
}

export default function Negotiation({ order, messages }) {
  const sellerIds = useMemo(() => Object.keys(order.sellers || {}), [order.sellers]);
  const colour = useMemo(() => Object.fromEntries(sellerIds.map((s, i) => [s, SERIES[i % SERIES.length]])), [sellerIds]);
  const winner = order.winner?.seller_id;

  const threads = useMemo(() => {
    const t = { [UPDATES]: [] };
    sellerIds.forEach((s) => (t[s] = []));
    messages.forEach((m) => threadOf(m, sellerIds).forEach((id) => t[id]?.push(m)));
    return t;
  }, [messages, sellerIds]);

  // Start on the winner, else the seller who spoke last; then stay wherever the owner clicks.
  const [picked, setPicked] = useState(null);
  const fallback = useMemo(() => {
    if (winner) return winner;
    const last = [...messages].reverse().find((m) => sellerIds.includes(m.from));
    return last?.from || sellerIds[0] || UPDATES;
  }, [winner, messages, sellerIds]);
  const current = picked && threads[picked] ? picked : fallback;

  // Unread counts: messages that arrived in a conversation the owner isn't looking at.
  const [seen, setSeen] = useState({});
  useEffect(() => {
    const last = threads[current]?.at(-1)?.seq ?? -1;
    if ((seen[current] ?? -1) < last) setSeen((s) => ({ ...s, [current]: last }));
  }, [current, threads, seen]);
  const unread = (id) => {
    if (id === current || seen[id] === undefined) return 0; // never opened: no badge flood on first load
    return threads[id].filter((m) => m.seq > seen[id]).length;
  };
  useEffect(() => {
    // Mark every conversation as read up to now on first render, so only new messages count.
    setSeen((s) => {
      const next = { ...s };
      Object.entries(threads).forEach(([id, list]) => {
        if (next[id] === undefined) next[id] = list.at(-1)?.seq ?? -1;
      });
      return next;
    });
  }, [threads]);

  const points = useMemo(() => offerPoints(messages, sellerIds, order.mandate.quantity), [messages, sellerIds, order.mandate.quantity]);

  if (!sellerIds.length) {
    return (
      <section className="nego">
        {order.status === "choosing_product"
          ? <p className="nego-empty">Pick a product above. Your agent then asks every seller agent for a quote, and the conversations show up here.</p>
          : <FeedSkeleton />}
      </section>
    );
  }

  const talking = ACTIVE.has(order.status);

  return (
    <section className="nego" aria-label="Negotiation">
      <OfferChart order={order} points={points} sellerIds={sellerIds} colour={colour} winner={winner}
        current={current} onPick={setPicked} />
      <div className="messenger">
        <nav className="convos" aria-label="Conversations">
          {sellerIds.map((sid) => {
            const s = order.sellers[sid];
            const state = SELLER_STATE[s.status] || { label: s.status, tone: "busy" };
            const last = threads[sid].at(-1);
            const latest = [...points].reverse().find((p) => p.seller === sid && p.side === "seller");
            const busy = talking && ["requested", "negotiating"].includes(s.status);
            const n = unread(sid);
            return (
              <button key={sid} className={`convo ${current === sid ? "is-on" : ""} tone-${state.tone}`}
                onClick={() => setPicked(sid)} aria-current={current === sid ? "true" : undefined}>
                <span className="avatar" style={{ background: colour[sid] }} aria-hidden="true">{initials(s.name)}</span>
                <span className="convo-main">
                  <span className="convo-top">
                    <b>{s.name}</b>
                    {latest && <span className="convo-price">{money(latest.total, order.mandate.currency)}</span>}
                  </span>
                  <span className="convo-sub">
                    {sid === winner ? <span className="chip chip-win">Your deal</span>
                      : <span className={`chip chip-${state.tone}`}>{state.label}</span>}
                    <span className="convo-preview">{busy ? "typing…" : last?.text}</span>
                  </span>
                </span>
                {n > 0 && <span className="badge" aria-label={`${n} new`}>{n}</span>}
              </button>
            );
          })}
          <button className={`convo ${current === UPDATES ? "is-on" : ""}`} onClick={() => setPicked(UPDATES)}
            aria-current={current === UPDATES ? "true" : undefined}>
            <span className="avatar avatar-you" aria-hidden="true">You</span>
            <span className="convo-main">
              <span className="convo-top"><b>Your agent and PayPal</b></span>
              <span className="convo-sub"><span className="convo-preview">{threads[UPDATES].at(-1)?.text}</span></span>
            </span>
            {unread(UPDATES) > 0 && <span className="badge">{unread(UPDATES)}</span>}
          </button>
        </nav>
        <Thread key={current} id={current} order={order} list={threads[current]} colour={colour}
          points={points} talking={talking} />
      </div>
    </section>
  );
}

function Thread({ id, order, list, colour, points, talking }) {
  const box = useRef(null);
  const stick = useRef(true);
  const cur = order.mandate.currency;
  const isSeller = id !== UPDATES;
  const s = isSeller ? order.sellers[id] : null;
  const state = s ? SELLER_STATE[s.status] || { label: s.status } : null;
  const latest = isSeller && [...points].reverse().find((p) => p.seller === id && p.side === "seller");
  const busy = isSeller ? talking && ["requested", "negotiating"].includes(s.status) : talking && order.status === "resolving";

  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [list.length, busy]);

  return (
    <div className="thread">
      <header className="thread-head">
        {isSeller ? (
          <>
            <span className="avatar" style={{ background: colour[id] }} aria-hidden="true">{initials(s.name)}</span>
            <div>
              <h3>{s.name}</h3>
              <p>Seller agent over A2A · {id === order.winner?.seller_id ? "your deal"
                : s.status === "not_picked" ? "not picked: another seller was cheaper" : state.label.toLowerCase()}</p>
            </div>
            {latest && (
              <div className="thread-offer">
                <b>{money(latest.total, cur)}</b>
                <span>{money(latest.unit, cur)} each{latest.delivery ? ` · by ${shortDate(latest.delivery)}` : ""}</span>
              </div>
            )}
          </>
        ) : (
          <>
            <span className="avatar avatar-you" aria-hidden="true">You</span>
            <div>
              <h3>Your agent and PayPal</h3>
              <p>What your agent decided, and every move of your money</p>
            </div>
          </>
        )}
      </header>
      <div className="thread-scroll" ref={box} tabIndex={0} aria-live="polite"
        onScroll={() => { const el = box.current; if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
        {list.length === 0 && <p className="nego-empty">Nothing here yet.</p>}
        <ol>
          {list.map((msg) => <Bubble key={msg.seq} msg={msg} order={order} thread={id} />)}
        </ol>
        {busy && <Typing text={isSeller ? `${s.name} and your agent are negotiating` : "Your agent is working"} />}
      </div>
    </div>
  );
}

function Bubble({ msg, order, thread }) {
  const cur = order.mandate.currency;
  const d = msg.data || {};
  const mine = msg.from === "buyer";
  const event = ["paypal", "system"].includes(msg.from) || msg.kind === "decision" || msg.kind === "system";
  const side = event ? "event" : mine ? "mine" : "theirs";
  const who = msg.from === "buyer" ? "Your agent" : msg.from === "paypal" ? "PayPal"
    : msg.from === "system" ? "Procuro" : order.sellers?.[msg.from]?.name || msg.from;
  return (
    <li className={`bub bub-${side} kind-${msg.kind}`}>
      {(side === "event" || thread === UPDATES) && <span className="bub-who">{who}</span>}
      {mine && msg.to === "all" && thread !== UPDATES && <span className="bub-who">Your agent, to all sellers</span>}
      <p>{msg.text}</p>
      {d.evidence?.length > 0 && (
        <div className="evidence">
          {d.evidence.map((ev, i) => (
            <a key={ev} href={`${BASE}/api/orders/${order.id}/evidence/${ev}`} target="_blank" rel="noreferrer">
              <img src={`${BASE}/api/orders/${order.id}/evidence/${ev}`} alt={`Damage photo ${i + 1}`} loading="lazy" />
            </a>
          ))}
        </div>
      )}
      {!mine && d.unit_price != null && d.total != null && (
        <span className="bub-offer">
          {d.action === "accept" ? (thread === order.winner?.seller_id ? "Agreed: " : "Price held if chosen: ") : "Offer: "}
          {money(d.unit_price, d.currency || cur)} each · {money(d.total, d.currency || cur)}
          {d.delivery_date ? ` · by ${shortDate(d.delivery_date)}` : ""}
        </span>
      )}
      {mine && d.action === "counter_offer" && (
        <span className="bub-offer">
          Counter: {money(d.unit_price, cur)} each · {money(d.unit_price * order.mandate.quantity, cur)}
        </span>
      )}
    </li>
  );
}

// ---------- the live offer chart ----------

// The chart draws at its real pixel width, so its text stays readable on a phone.
function useWidth(ref, fallback = 720) {
  const [w, setW] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el?.clientWidth) setW(Math.max(280, el.clientWidth));
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([entry]) => setW(Math.max(280, Math.round(entry.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1;
  const step0 = span / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => s >= step0) || step0;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

const OUT = ["dropped_deadline", "declined", "rejected", "walked_away", "not_picked", "closed"];

// The seller currently ahead: the deal once there is one, otherwise the cheapest latest offer from a
// seller still in the running.
function leaderOf(order, points, sellerIds) {
  if (order.winner) {
    return { sid: order.winner.seller_id, total: order.winner.total, unit: order.winner.unit_price,
      delivery: order.winner.delivery_date, final: true };
  }
  let best = null;
  sellerIds.forEach((sid) => {
    if (OUT.includes(order.sellers[sid].status)) return;
    const last = points.filter((p) => p.seller === sid && p.side === "seller").at(-1);
    if (last && last.total <= order.mandate.budget_max + 0.005 && (!best || last.total < best.total)) {
      best = { sid, total: last.total, unit: last.unit, delivery: last.delivery, final: false };
    }
  });
  return best;
}

// One small panel per seller, side by side on the same price scale: every seller is visible at once,
// and inside each panel the seller's offers come down while your agent's counters come up to meet them.
function OfferChart({ order, points, sellerIds, colour, winner, current, onPick }) {
  const m = order.mandate;
  const cur = m.currency;
  const [hover, setHover] = useState(null);
  const wrap = useRef(null);
  const grid = useRef(null);
  const total = useWidth(grid, 720);
  const stacked = total < 640;
  const gap = 12;
  // Each panel's inner width (panels have a 1.5px border on each side).
  const W = (stacked ? total : Math.floor((total - gap * (sellerIds.length - 1)) / sellerIds.length)) - 3;
  const H = stacked ? 118 : 150;
  const PAD = { l: 44, r: 14, t: 12, b: 10 };

  const values = [...points.map((p) => p.total), m.budget_max, m.approval_threshold].filter((v) => v != null);
  const lo0 = Math.min(...values);
  const hi0 = Math.max(...values);
  const pad = (hi0 - lo0) * 0.1 || hi0 * 0.1;
  const ticks = niceTicks(Math.max(0, lo0 - pad), hi0 + pad, 3);
  const lo = Math.min(ticks[0], lo0 - pad / 2);
  const hi = Math.max(ticks.at(-1), hi0 + pad / 2);
  const Y = (v) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);

  const series = sellerIds.map((sid) => {
    const mine = points.filter((p) => p.seller === sid).map((p, i) => ({ ...p, move: i }));
    return {
      sid,
      name: order.sellers[sid].name,
      status: order.sellers[sid].status,
      out: OUT.includes(order.sellers[sid].status),
      moves: mine,
      asks: mine.filter((p) => p.side === "seller"),
      counters: mine.filter((p) => p.side === "buyer"),
    };
  });
  // Same x scale in every panel, so a seller that dropped out early visibly stops early.
  const maxMoves = Math.max(4, ...series.map((s) => s.moves.length));
  const X = (i) => PAD.l + (i / (maxMoves - 1)) * (W - PAD.l - PAD.r);
  const path = (pts) => pts.map((p, i) => `${i ? "L" : "M"}${X(p.move).toFixed(1)},${Y(p.total).toFixed(1)}`).join("");

  const lead = leaderOf(order, points, sellerIds);
  const busy = ACTIVE.has(order.status);

  const show = (p, sid, e) => {
    const r = wrap.current?.getBoundingClientRect();
    const panel = e.currentTarget.ownerSVGElement?.getBoundingClientRect();
    if (!r || !panel) return;
    setHover({ p, sid, left: panel.left - r.left + X(p.move), top: panel.top - r.top + Y(p.total) });
  };

  return (
    <figure className="offer-chart" ref={wrap} onMouseLeave={() => setHover(null)}>
      <figcaption className="oc-head">
        <div className="oc-lead" aria-live="polite">
          {lead ? (
            <>
              <span className="oc-lead-label">{lead.final ? "Deal" : busy ? "Winning now" : "Best offer"}</span>
              <span className="oc-lead-main">
                <i style={{ background: colour[lead.sid] }} aria-hidden="true" />
                <b>{order.sellers[lead.sid].name}</b>
                <strong>{money(lead.total, cur)}</strong>
              </span>
              <span className="oc-lead-sub">
                {money(lead.unit, cur)} each{lead.delivery ? ` · by ${shortDate(lead.delivery)}` : ""} ·{" "}
                {money(m.budget_max - lead.total, cur)} under budget
              </span>
            </>
          ) : (
            <>
              <span className="oc-lead-label">{busy ? "Winning now" : "Best offer"}</span>
              <span className="oc-lead-main"><b>{busy ? "Waiting for quotes…" : "No offer inside your budget"}</b></span>
            </>
          )}
        </div>
        <ul className="oc-key">
          <li><i className="oc-k-ask" />Seller's offer</li>
          <li><i className="oc-k-counter" />Your agent's counter</li>
          <li><i className="oc-k-budget" />Budget {money(m.budget_max, cur)}</li>
          {m.approval_threshold < m.budget_max && <li><i className="oc-k-limit" />Ask me above {money(m.approval_threshold, cur)}</li>}
        </ul>
      </figcaption>

      <div ref={grid} className={`oc-panels ${stacked ? "is-stacked" : ""}`} style={{ "--n": sellerIds.length }}>
        {series.map((s, idx) => {
          const lastAsk = s.asks.at(-1);
          const lastCounter = s.counters.at(-1);
          const isLead = lead?.sid === s.sid;
          const agreed = s.status === "agreed";
          const note = s.out ? (SELLER_STATE[s.status]?.label || s.status)
            : agreed ? `Agreed at ${money(lastAsk?.total, cur)}`
            : lastAsk && lastCounter ? `${money(Math.abs(lastAsk.total - lastCounter.total), cur)} apart`
            : lastAsk ? "First quote in" : "Waiting for a quote";
          const ticksHere = idx === 0 || stacked;
          return (
            <div key={s.sid} role="button" tabIndex={0}
              className={`oc-panel ${s.out ? "is-out" : ""} ${isLead ? "is-lead" : ""} ${current === s.sid ? "is-on" : ""}`}
              style={{ "--c": colour[s.sid] }} onClick={() => onPick?.(s.sid)}
              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onPick?.(s.sid)}
              aria-label={`${s.name}: ${note}. Open the conversation.`}>
              <div className="oc-panel-head">
                <i aria-hidden="true" />
                <b>{s.name}</b>
                <strong>{lastAsk ? money(lastAsk.total, cur) : "–"}</strong>
              </div>
              <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} aria-hidden="true">
                {ticks.map((t) => (
                  <g key={t}>
                    <line x1={PAD.l} x2={W - PAD.r} y1={Y(t)} y2={Y(t)} className="oc-grid" />
                    {ticksHere && <text x={PAD.l - 6} y={Y(t) + 4} className="oc-tick" textAnchor="end">{money(t, cur).replace(/\.00$/, "")}</text>}
                  </g>
                ))}
                <line x1={PAD.l} x2={W - PAD.r} y1={Y(m.budget_max)} y2={Y(m.budget_max)} className="oc-budget" />
                {m.approval_threshold < m.budget_max && (
                  <line x1={PAD.l} x2={W - PAD.r} y1={Y(m.approval_threshold)} y2={Y(m.approval_threshold)} className="oc-limit" />
                )}
                {s.counters.length > 1 && <path d={path(s.counters)} className="oc-counter-line" />}
                {s.asks.length > 1 && <path d={path(s.asks)} className="oc-line" />}
                {s.counters.map((p) => (
                  <circle key={p.seq} cx={X(p.move)} cy={Y(p.total)} r={5} className="oc-pt oc-pt-counter"
                    onMouseEnter={(e) => show(p, s.sid, e)} />
                ))}
                {s.asks.map((p) => (
                  <circle key={p.seq} cx={X(p.move)} cy={Y(p.total)} r={5.5} className="oc-pt"
                    onMouseEnter={(e) => show(p, s.sid, e)} />
                ))}
                {isLead && lastAsk && <circle cx={X(lastAsk.move)} cy={Y(lastAsk.total)} r={11} className="oc-deal" />}
                {!s.moves.length && (
                  <text x={(PAD.l + W - PAD.r) / 2} y={H / 2} textAnchor="middle" className="oc-wait">{busy ? "Quoting…" : "No quote"}</text>
                )}
              </svg>
              <p className="oc-panel-note">
                {isLead && <span className={`chip ${lead.final ? "chip-win" : "chip-busy"}`}>{lead.final ? "Your deal" : "Winning"}</span>}
                {note}
              </p>
            </div>
          );
        })}
      </div>

      {hover && (
        <div className="oc-tip" style={{ left: hover.left, top: hover.top }}>
          <b>{hover.p.side === "buyer" ? `Your agent to ${order.sellers[hover.sid].name}` : order.sellers[hover.sid].name}</b>
          <span>{hover.p.side === "buyer" ? "Counter-offer" : hover.p.accept ? "Accepted" : "Offer"}: {money(hover.p.total, cur)}</span>
          <span>{money(hover.p.unit, cur)} each{hover.p.delivery ? ` · by ${shortDate(hover.p.delivery)}` : ""}</span>
        </div>
      )}
      <table className="sr-only">
        <caption>Offers in order</caption>
        <thead><tr><th>Move</th><th>From</th><th>To</th><th>Total</th><th>Each</th></tr></thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.seq}>
              <td>{p.x + 1}</td>
              <td>{p.side === "buyer" ? "Your agent" : order.sellers[p.seller].name}</td>
              <td>{p.side === "buyer" ? order.sellers[p.seller].name : "Your agent"}</td>
              <td>{money(p.total, cur)}</td>
              <td>{money(p.unit, cur)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}