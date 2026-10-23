import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { agentName, money, shortDate } from "./format";

const ACTIVE = new Set(["negotiating", "awaiting_paypal", "resolving"]);

function exampleRequest() {
  const d = new Date();
  d.setDate(d.getDate() + 7);
  const when = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return `200 cake boxes with my logo, delivered by ${when}. Budget $600. Ask me before paying anything over $400.`;
}

function useQuery() {
  const params = new URLSearchParams(window.location.search);
  return Object.fromEntries(params.entries());
}

function setOrderInUrl(id) {
  const url = id ? `/?order=${id}` : "/";
  window.history.replaceState(null, "", url);
}

export default function App() {
  const query = useQuery();
  const [orderId, setOrderId] = useState(query.order || null);
  const [state, setState] = useState(null);
  const [error, setError] = useState("");
  const returnHandled = useRef(false);

  const load = useCallback(async () => {
    if (!orderId) return;
    try {
      setState(await api.getOrder(orderId));
    } catch (e) {
      setError(e.message);
    }
  }, [orderId]);

  // Coming back from PayPal's approval page.
  useEffect(() => {
    if (returnHandled.current || !query.order || !query.paypal) return;
    returnHandled.current = true;
    const id = query.order;
    setOrderInUrl(id);
    if (query.paypal === "return" && query.token) {
      api.paypalReturn(id, query.token).then(load).catch((e) => setError(e.message));
    } else if (query.paypal === "cancel") {
      setError("You closed PayPal before authorizing. Nothing was charged. You can approve again.");
    }
  }, [query, load]);

  useEffect(() => {
    if (!orderId) return undefined;
    load();
    const status = state?.order?.status;
    const every = !status || ACTIVE.has(status) ? 1200 : 5000;
    const t = setInterval(load, every);
    return () => clearInterval(t);
  }, [orderId, load, state?.order?.status]);

  const start = async (text) => {
    setError("");
    const order = await api.createOrder(text);
    setOrderId(order.id);
    setOrderInUrl(order.id);
    setState({ order, messages: [] });
  };

  const reset = () => {
    setOrderId(null);
    setState(null);
    setError("");
    setOrderInUrl(null);
  };

  return (
    <div className="app">
      <header className="topbar">
        <button className="wordmark" onClick={reset}>Mandate</button>
        <p className="topbar-note">Your agent buys within your rules. PayPal holds the money until you're happy.</p>
      </header>

      {error && (
        <div className="banner" role="alert">
          {error} <button className="link" onClick={() => setError("")}>Dismiss</button>
        </div>
      )}

      {!orderId ? (
        <Start onStart={start} onOpen={(id) => { setOrderId(id); setOrderInUrl(id); }} />
      ) : !state ? (
        <p className="loading">Loading order…</p>
      ) : (
        <OrderView state={state} reload={load} onError={setError} />
      )}
    </div>
  );
}

function Start({ onStart, onOpen }) {
  const [text, setText] = useState(exampleRequest());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [orders, setOrders] = useState([]);

  useEffect(() => {
    api.listOrders().then(setOrders).catch(() => {});
  }, []);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await onStart(text);
    } catch (ex) {
      setErr(ex.message);
      setBusy(false);
    }
  };

  return (
    <main className="start">
      <h1 className="start-title">Tell your agent what you need, and what it's allowed to spend.</h1>
      <form onSubmit={submit} className="mandate-form">
        <label htmlFor="req" className="field-label">What should your agent buy?</label>
        <textarea id="req" value={text} onChange={(e) => setText(e.target.value)} rows={4} />
        <p className="hint">Include a budget, when you need it, and the amount above which it should ask you first.</p>
        {err && <p className="form-error">{err}</p>}
        <button className="btn primary" disabled={busy || !text.trim()}>
          {busy ? "Signing your mandate…" : "Start buying"}
        </button>
      </form>
      {orders.length > 0 && (
        <section className="history">
          <h2>Earlier orders</h2>
          <ul>
            {orders.slice(0, 6).map((o) => (
              <li key={o.id}>
                <button className="history-row" onClick={() => onOpen(o.id)}>
                  <span>{o.mandate?.quantity} × {o.mandate?.item}</span>
                  <StatusPill status={o.status} />
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}

const STATUS_TEXT = {
  negotiating: "Negotiating",
  awaiting_approval: "Needs your approval",
  awaiting_paypal: "Waiting for PayPal",
  held: "Money on hold",
  completed: "Paid",
  resolving: "Resolving a problem",
  settled: "Settled",
  escalated: "Dispute ready",
  no_deal: "No deal",
  failed: "Stopped",
};

function StatusPill({ status }) {
  return <span className={`pill pill-${status}`}>{STATUS_TEXT[status] || status}</span>;
}

function OrderView({ state, reload, onError }) {
  const { order, messages } = state;
  const m = order.mandate;
  return (
    <main className="order">
      <aside className="order-side">
        <section className="mandate-card">
          <div className="mandate-head">
            <h1>{m.quantity} × {m.item}</h1>
            <StatusPill status={order.status} />
          </div>
          <dl className="rules">
            <div><dt>Budget</dt><dd>{money(m.budget_max, m.currency)}</dd></div>
            <div><dt>Ask me above</dt><dd>{money(m.approval_threshold, m.currency)}</dd></div>
            <div><dt>Deliver by</dt><dd>{shortDate(m.deadline)}</dd></div>
          </dl>
          {m.requirements && <p className="req">{m.requirements}</p>}
          <p className="signed">Signed mandate {order.intent_mandate?.id}</p>
        </section>

        <MoneyMeter order={order} />
        <Actions order={order} reload={reload} onError={onError} />
        {order.benchmark && <Market bench={order.benchmark} />}
      </aside>

      <section className="order-main">
        <Feed messages={messages} sellers={order.sellers} />
        {order.winner && <AskBox orderId={order.id} />}
      </section>
    </main>
  );
}

function MoneyMeter({ order }) {
  const cur = order.mandate.currency;
  const p = order.paypal || {};
  const total = p.authorized_amount ?? order.winner?.total;
  if (!total) {
    return (
      <section className="meter meter-empty">
        <p className="meter-line">No money has moved.</p>
        <p className="meter-sub">Your agent can't pay anyone until you approve a deal.</p>
      </section>
    );
  }
  const captured = p.captured || 0;
  const released = p.released || 0;
  const held = p.authorization_id && !p.capture_id ? total : 0;
  const pct = (v) => `${Math.max(0, Math.min(100, (v / total) * 100))}%`;

  let line = `${money(total, cur)} agreed. Nothing charged yet.`;
  if (held) line = `${money(held, cur)} held, not paid`;
  if (p.capture_id) line = released > 0
    ? `${money(captured, cur)} paid, ${money(released, cur)} returned`
    : `${money(captured, cur)} paid`;

  return (
    <section className="meter" aria-label="Where your money is">
      <p className="meter-line">{line}</p>
      <div className="bar" role="img" aria-label={line}>
        {captured > 0 && <span className="seg seg-paid" style={{ width: pct(captured) }} />}
        {released > 0 && <span className="seg seg-back" style={{ width: pct(released) }} />}
        {held > 0 && <span className="seg seg-held" style={{ width: "100%" }} />}
      </div>
      <ul className="legend">
        <li><i className="key key-held" />On hold in your PayPal</li>
        <li><i className="key key-paid" />Paid to seller</li>
        <li><i className="key key-back" />Returned to you</li>
      </ul>
      {p.authorization_id && (
        <p className="meter-sub">PayPal authorization {p.authorization_id}
          {p.reauthorized ? " (renewed)" : ""}</p>
      )}
    </section>
  );
}

function Actions({ order, reload, onError }) {
  const [busy, setBusy] = useState(false);
  const [reporting, setReporting] = useState(false);
  const w = order.winner;
  const cur = order.mandate.currency;

  const run = async (fn) => {
    setBusy(true);
    try {
      await fn();
      await reload();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (order.status === "negotiating") {
    return <p className="working">Your agent is negotiating with seller agents. Watch the conversation on the right.</p>;
  }

  if (order.status === "awaiting_approval" && w) {
    return (
      <section className="deal">
        <h2>{w.seller_name} for {money(w.total, cur)}</h2>
        <p className="deal-facts">
          {money(w.unit_price, cur)} each, delivered {shortDate(w.delivery_date)}.{" "}
          {w.saved_vs_opening > 0 && <>Negotiated {money(w.saved_vs_opening, cur)} off their first quote.</>}
        </p>
        <p className="deal-why">{w.explanation}</p>
        <button className="btn primary" disabled={busy} onClick={() => run(async () => {
          const o = await api.approve(order.id);
          window.location.href = o.paypal.approve_url;
        })}>
          {busy ? "Opening PayPal…" : `Approve and authorize ${money(w.total, cur)} with PayPal`}
        </button>
        <p className="hint">PayPal will hold the money. It's only paid to {w.seller_name} after you confirm delivery.</p>
      </section>
    );
  }

  if (order.status === "awaiting_paypal") {
    return (
      <section className="deal">
        <p>Waiting for you to authorize in PayPal.</p>
        {order.paypal?.approve_url && (
          <a className="btn primary" href={order.paypal.approve_url}>Open PayPal</a>
        )}
      </section>
    );
  }

  if (order.status === "held") {
    return reporting ? (
      <ProblemForm order={order} onDone={() => { setReporting(false); reload(); }} onError={onError} />
    ) : (
      <section className="deal">
        <p>When your order arrives, tell your agent how it went.</p>
        <div className="btn-row">
          <button className="btn primary" disabled={busy} onClick={() => run(() => api.delivered(order.id))}>
            Received, looks good
          </button>
          <button className="btn" disabled={busy} onClick={() => setReporting(true)}>Report a problem</button>
        </div>
      </section>
    );
  }

  if (order.status === "escalated" && order.dispute_packet) {
    return (
      <section className="deal deal-alert">
        <h2>Dispute file ready</h2>
        <p>{order.dispute_packet.summary} Your money is still on hold.</p>
        <p className="hint">{order.dispute_packet.requested_outcome}</p>
      </section>
    );
  }

  if (order.status === "no_deal" || order.status === "failed") {
    return <p className="working">{order.error || "No deal was made, and no money moved."}</p>;
  }
  return null;
}

function ProblemForm({ order, onDone, onError }) {
  const [note, setNote] = useState("");
  const [files, setFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.report(order.id, note, files);
      onDone();
    } catch (ex) {
      onError(ex.message);
      setBusy(false);
    }
  };
  return (
    <form className="deal problem" onSubmit={submit}>
      <label className="field-label" htmlFor="note">What's wrong with the order?</label>
      <textarea id="note" rows={3} value={note} onChange={(e) => setNote(e.target.value)}
        placeholder="60 boxes arrived crushed, and the logo is off-center on the rest." />
      <label className="field-label" htmlFor="photos">Photos (up to 4)</label>
      <input id="photos" type="file" accept="image/*" multiple onChange={(e) => setFiles(e.target.files)} />
      <p className="hint">Your payment stays on hold while your agent sorts this out with the seller.</p>
      <button className="btn primary" disabled={busy || !note.trim()}>
        {busy ? "Sending to your agent…" : "Send to my agent"}
      </button>
    </form>
  );
}

function Market({ bench }) {
  return (
    <section className="market">
      <h2>Market check</h2>
      <p>
        {bench.samples} comparable listings on Channel3: {money(bench.low, bench.currency)} to{" "}
        {money(bench.high, bench.currency)} each, typically {money(bench.median, bench.currency)}.
      </p>
      <ul className="examples">
        {bench.examples.slice(0, 3).map((ex) => (
          <li key={ex.id}>
            {ex.image && <img src={ex.image} alt="" />}
            <span>{ex.title}</span>
            <b>{money(ex.price, bench.currency)}</b>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Feed({ messages, sellers }) {
  const end = useRef(null);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);
  return (
    <section className="feed" aria-live="polite">
      <h2 className="feed-title">Agent conversation</h2>
      {messages.length === 0 && <p className="hint">Your agent is getting started…</p>}
      <ol>
        {messages.map((msg) => {
          const side = msg.from === "buyer" ? "mine" : ["paypal", "system"].includes(msg.from) ? "event" : "theirs";
          return (
            <li key={msg.seq} className={`msg msg-${side} kind-${msg.kind}`}>
              <span className="who">
                {agentName(msg.from, sellers)}
                {msg.to && msg.to !== "buyer" && msg.from === "buyer" && (
                  <> to {msg.to === "all" ? "all sellers" : agentName(msg.to, sellers)}</>
                )}
              </span>
              <p>{msg.text}</p>
              {msg.data?.unit_price && msg.from !== "buyer" && (
                <span className="offer">
                  {money(msg.data.unit_price, msg.data.currency)} each, {money(msg.data.total, msg.data.currency)} total
                  {msg.data.delivery_date ? `, delivered ${shortDate(msg.data.delivery_date)}` : ""}
                </span>
              )}
            </li>
          );
        })}
      </ol>
      <div ref={end} />
    </section>
  );
}

function AskBox({ orderId }) {
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await api.ask(orderId, q);
      setAnswer(res.answer);
    } catch (ex) {
      setAnswer(ex.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="ask" onSubmit={submit}>
      <label className="field-label" htmlFor="ask">Ask your agent about this order</label>
      <div className="ask-row">
        <input id="ask" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Why didn't you pick BoxCraft?" />
        <button className="btn" disabled={busy || !q.trim()}>{busy ? "Thinking…" : "Ask"}</button>
      </div>
      {answer && <p className="answer">{answer}</p>}
    </form>
  );
}
