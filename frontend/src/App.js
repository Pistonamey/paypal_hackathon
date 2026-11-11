import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import Negotiation from "./Negotiation";
import Receipt from "./Receipt";
import { ShareOrder, useWallet, WalletCard } from "./PayPal";
import { money, shortDate } from "./format";
import { BoardSkeleton, Busy, CalendarSkeleton, DashboardSkeleton, GanttSkeleton, HistorySkeleton,
  OrderSkeleton, Spinner, Stepper } from "./ui";

// AG Studio is large, so the dashboard only loads when it is opened.
const Dashboard = lazy(() => import("./dashboard/Dashboard"));
// Bryntum is large too: the calendar and timeline load on first use.
const DeliveryCalendar = lazy(() => import("./bryntum/DeliveryCalendar"));
const Timeline = lazy(() => import("./bryntum/Timeline"));
const SellerBoard = lazy(() => import("./bryntum/SellerBoard"));

const ACTIVE = new Set(["negotiating", "awaiting_paypal", "resolving"]);

function exampleRequest() {
  return "500 paper coffee cups with lids, 12 oz. Budget $300. Ask me before paying anything over $200.";
}

function inDays(n) {
  const d = new Date();
  d.setDate(d.getDate() + n);
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
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
  const [view, setView] = useState(["spend", "sellers"].includes(query.view) ? query.view : "orders");
  const [orderId, setOrderId] = useState(query.order || null);
  const [state, setState] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [wallet, reloadWallet, setWallet] = useWallet();
  const returnHandled = useRef(false);

  const load = useCallback(async () => {
    if (!orderId) return;
    try {
      setState(await api.getOrder(orderId));
    } catch (e) {
      setError(e.message);
    }
  }, [orderId]);

  // Coming back from PayPal after connecting the owner's PayPal (Vault setup token approved).
  useEffect(() => {
    if (returnHandled.current || !query.paypal_connect) return;
    returnHandled.current = true;
    window.history.replaceState(null, "", "/");
    if (query.paypal_connect === "return") {
      api.walletFinish(query.approval_token_id)
        .then((w) => {
          setWallet(w);
          setNotice(`PayPal connected${w.payer_email ? ` as ${w.payer_email}` : ""}. Deals under your limit will now be ` +
            "held without a trip to PayPal.");
        })
        .catch((e) => setError(e.message));
    } else {
      setError("You closed PayPal before connecting. Nothing changed: any PayPal account that was connected still is.");
    }
  }, [query, setWallet]);

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

  const start = async (text, deadline) => {
    setError("");
    const order = await api.createOrder(text, deadline);
    setOrderId(order.id);
    setOrderInUrl(order.id);
    setState({ order, messages: [] });
  };

  const showView = (next) => {
    setView(next);
    window.history.replaceState(null, "", next !== "orders" ? `/?view=${next}` : orderId ? `/?order=${orderId}` : "/");
  };

  const reset = () => {
    setView("orders");
    setOrderId(null);
    setState(null);
    setError("");
    setOrderInUrl(null);
  };

  return (
    <div className={`app ${view !== "orders" ? "app-wide" : ""}`}>
      <header className="topbar">
        <button className="wordmark" onClick={reset}>Procuro</button>
        <p className="topbar-note">Agent-to-agent buying over A2A · signed with AP2 · paid through PayPal</p>
        <nav className="views" aria-label="Sections">
          <button className={`tab ${view === "orders" ? "tab-on" : ""}`} onClick={() => showView("orders")}
            aria-current={view === "orders" ? "page" : undefined}>Orders</button>
          <button className={`tab ${view === "spend" ? "tab-on" : ""}`} onClick={() => showView("spend")}
            aria-current={view === "spend" ? "page" : undefined}>Spend</button>
          <button className={`tab ${view === "sellers" ? "tab-on" : ""}`} onClick={() => showView("sellers")}
            aria-current={view === "sellers" ? "page" : undefined}>Sellers</button>
          {(orderId || view !== "orders") && (
            <button className="btn btn-small" onClick={reset}>New order</button>
          )}
        </nav>
      </header>

      {notice && (
        <div className="notice" role="status">
          {notice} <button className="link" onClick={() => setNotice("")}>Dismiss</button>
        </div>
      )}
      {error && (
        <div className="banner" role="alert">
          {error} <button className="link" onClick={() => setError("")}>Dismiss</button>
        </div>
      )}

      {view === "spend" ? (
        <Suspense fallback={<DashboardSkeleton />}>
          <Dashboard />
        </Suspense>
      ) : view === "sellers" ? (
        <Suspense fallback={<BoardSkeleton />}>
          <SellerBoard onOpenOrder={(id) => { setView("orders"); setState(null); setOrderId(id); setOrderInUrl(id); }} />
        </Suspense>
      ) : !orderId ? (
        <Start onStart={start} onOpen={(id) => { setOrderId(id); setOrderInUrl(id); }}
          wallet={wallet} onWallet={setWallet} onError={setError} onWalletRetry={reloadWallet} />
      ) : !state ? (
        <OrderSkeleton />
      ) : (
        <OrderView state={state} reload={async () => { await load(); reloadWallet(); }} onError={setError}
          onNewOrder={reset} wallet={wallet} />
      )}
    </div>
  );
}

const RECENT = 6; // orders shown on the start page before "Show all"

function Start({ onStart, onOpen, wallet, onWallet, onError, onWalletRetry }) {
  const [text, setText] = useState(exampleRequest());
  const [deadline, setDeadline] = useState(inDays(7));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [orders, setOrders] = useState(null); // null while loading

  useEffect(() => {
    api.listOrders().then(setOrders).catch(() => setOrders([]));
  }, []);

  const [showAll, setShowAll] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await onStart(text, deadline);
    } catch (ex) {
      setErr(ex.message);
      setBusy(false);
    }
  };

  return (
    <main className="start">
      <h1 className="start-title">Send your agent to negotiate with theirs.</h1>
      <p className="start-sub">
        Seller agents run on their own servers. They meet yours over A2A, every deal is signed with AP2, and PayPal
        holds the money until the goods arrive.
      </p>
      <div className="start-grid">
        <div className="start-left">
        <form onSubmit={submit} className="mandate-form">
          <label htmlFor="req" className="field-label">What should your agent buy?</label>
          <textarea id="req" value={text} onChange={(e) => setText(e.target.value)} rows={5} />
          <p className="hint">Include a budget and the amount above which it should ask you first. Pick the delivery date on the calendar.</p>
          {err && <p className="form-error">{err}</p>}
          <button className="btn primary" disabled={busy || !text.trim() || !deadline}>
            <Busy busy={busy} invert text="Your agent is reading your request">Start buying</Busy>
          </button>
        </form>
        <WalletCard wallet={wallet} onChange={onWallet} onError={onError} onRetry={onWalletRetry} />
        </div>
        <Suspense fallback={<CalendarSkeleton />}>
          <DeliveryCalendar value={deadline} onChange={setDeadline} />
        </Suspense>
      </div>
      {orders === null ? (
        <section className="history">
          <h2>Earlier orders</h2>
          <HistorySkeleton />
        </section>
      ) : orders.length > 0 && (
        <section className="history">
          <h2>Earlier orders</h2>
          <ul>
            {(showAll ? orders : orders.slice(0, RECENT)).map((o) => (
              <li key={o.id}>
                <button className="history-row" onClick={() => onOpen(o.id)}>
                  <span className="history-what">
                    <b>{o.mandate?.quantity} × {o.mandate?.item}</b>
                    <span>
                      {o.winner ? `${o.winner.seller_name}, ${money(o.winner.total, o.mandate?.currency)}` : "No seller yet"}
                      {o.created_at ? ` · ${shortDate(new Date(o.created_at * 1000).toISOString().slice(0, 10))}` : ""}
                    </span>
                  </span>
                  <StatusPill status={o.status} />
                </button>
              </li>
            ))}
          </ul>
          {orders.length > RECENT && (
            <button type="button" className="btn btn-small history-more" onClick={() => setShowAll((v) => !v)}>
              {showAll ? "Show only the latest" : `Show all ${orders.length} orders`}
            </button>
          )}
        </section>
      )}
    </main>
  );
}

const STATUS_TEXT = {
  choosing_product: "Pick a product",
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
  cancelled: "Cancelled",
};

function StatusPill({ status }) {
  return <span className={`pill pill-${status}`}>{STATUS_TEXT[status] || status}</span>;
}

function OrderView({ state, reload, onError, onNewOrder, wallet }) {
  const { order, messages } = state;
  const [pane, setPane] = useState("conversation");
  const m = order.mandate;
  return (
    <>
    <Stepper order={order} />
    <main className="order">
      <aside className="order-side">
        <section className="mandate-card">
          <div className="mandate-head">
            {order.product?.image && <img className="mandate-img" src={order.product.image} alt="" />}
            <h1>
              {order.status === "choosing_product" ? m.item : `${m.quantity} × ${m.item}`}
              {order.product?.brand && <span className="brand">{order.product.brand}</span>}
            </h1>
            <StatusPill status={order.status} />
          </div>
          <dl className="rules">
            <div><dt>Budget</dt><dd>{money(m.budget_max, m.currency)}</dd></div>
            <div><dt>Ask me above</dt><dd>{money(m.approval_threshold, m.currency)}</dd></div>
            <div><dt>Deliver by</dt><dd>{shortDate(m.deadline)}</dd></div>
          </dl>
          {m.requirements && <p className="req">{m.requirements}</p>}
          {(order.ap2 || order.intent_mandate) && (
            <p className="signed">
              {order.ap2 ? "Signed as AP2 v0.2 mandates" : `Signed mandate ${order.intent_mandate.id}`}
              <button type="button" className="link" onClick={() => setPane("receipt")}>See the receipt</button>
            </p>
          )}
        </section>

        <Actions order={order} reload={reload} onError={onError} onNewOrder={onNewOrder} wallet={wallet} />
        <MoneyMeter order={order} />
        {["held", "completed", "settled"].includes(order.status) && (
          <ShareOrder order={order} reload={reload} onError={onError} />
        )}
        {order.benchmark && <Market bench={order.benchmark} />}
      </aside>

      <section className="order-main">
        {order.status === "choosing_product" && <ProductPicker order={order} reload={reload} onError={onError} />}
        <div className="panes" role="tablist" aria-label="Order views">
          <button role="tab" aria-selected={pane === "conversation"} className={`tab ${pane === "conversation" ? "tab-on" : ""}`}
            onClick={() => setPane("conversation")}>Negotiation</button>
          <button role="tab" aria-selected={pane === "timeline"} className={`tab ${pane === "timeline" ? "tab-on" : ""}`}
            onClick={() => setPane("timeline")}>Timeline</button>
          {(order.ap2 || order.intent_mandate) && (
            <button role="tab" aria-selected={pane === "receipt"} className={`tab ${pane === "receipt" ? "tab-on" : ""}`}
              onClick={() => setPane("receipt")}>Receipt</button>
          )}
        </div>
        {pane === "receipt" ? (
          <Receipt orderId={order.id} version={`${order.status}-${order.updated_at}`} />
        ) : pane === "timeline" ? (
          <Suspense fallback={<GanttSkeleton />}>
            <Timeline orderId={order.id} version={`${order.status}-${order.updated_at}`} />
          </Suspense>
        ) : (
          <Negotiation order={order} messages={messages} />
        )}
        {order.winner && <AskBox orderId={order.id} />}
      </section>
    </main>
    </>
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
  const closed = p.capture_id || p.voided; // the hold ended: captured, or voided when nothing is paid
  const held = p.authorization_id && !closed ? total : 0;
  const pct = (v) => `${Math.max(0, Math.min(100, (v / total) * 100))}%`;

  let line = `${money(total, cur)} agreed. Nothing charged yet.`;
  if (held) line = `${money(held, cur)} held, not paid`;
  if (closed) line = released > 0 && captured <= 0
    ? `Nothing paid, ${money(released, cur)} returned`
    : released > 0
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
      {p.saved_paypal && (
        <p className="saved-note">Held with your saved PayPal{p.auto_paid ? ", automatically, inside your limit" : ""}.</p>
      )}
      {p.authorization_id && (
        <p className="meter-sub">PayPal authorization {p.authorization_id}
          {p.reauthorized ? " (renewed)" : ""}</p>
      )}
    </section>
  );
}

function Actions({ order, reload, onError, onNewOrder, wallet }) {
  const saved = wallet?.status === "active";
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

  if (order.status === "choosing_product") {
    return (
      <div className="working-card is-you">
        <p><b>Pick the exact product</b>Choose one of the real products on the right. Sellers will quote on exactly that item.</p>
      </div>
    );
  }

  if (order.status === "negotiating") {
    const n = Object.keys(order.sellers || {}).length;
    return (
      <div className="working-card" role="status">
        <Spinner size={20} />
        <p><b>Negotiating with {n || "the"} seller agent{n === 1 ? "" : "s"}</b>
          This takes about a minute. You'll be asked before any money moves.</p>
      </div>
    );
  }

  if (order.status === "resolving") {
    return (
      <div className="working-card" role="status">
        <Spinner size={20} />
        <p><b>Sorting out the problem with {w?.seller_name || "the seller"}</b>
          Your agent is reviewing the photos and negotiating. Your money stays on hold meanwhile.</p>
      </div>
    );
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
        <MoveDelivery order={order} reload={reload} onError={onError} />
        <button className="btn primary" disabled={busy} onClick={() => run(async () => {
          const o = await api.approve(order.id);
          // With a saved PayPal the hold is already done; otherwise approve on PayPal's page.
          if (o.status !== "awaiting_paypal" || !o.paypal?.approve_url) return;
          window.location.href = o.paypal.approve_url;
        })}>
          <Busy busy={busy} invert text={saved ? "Holding with your PayPal" : "Opening PayPal"}>
            {saved ? `Approve and hold ${money(w.total, cur)} with your PayPal` : `Approve and authorize ${money(w.total, cur)} with PayPal`}
          </Busy>
        </button>
        <p className="hint">
          {saved
            ? <>One tap: your saved PayPal holds the money, no trip to PayPal. It's only paid to {w.seller_name} after you confirm delivery.</>
            : <>PayPal will hold the money. It's only paid to {w.seller_name} after you confirm delivery.
              {" "}Connect PayPal on the start page and this becomes one tap, and deals under your limit hold themselves.</>}
        </p>
      </section>
    );
  }

  if (order.status === "awaiting_paypal") {
    return (
      <section className="deal">
        <h2>Finish in PayPal</h2>
        <p>Waiting for you to authorize the payment in PayPal. Nothing is charged until you confirm delivery.</p>
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
        <h2>Has it arrived?</h2>
        <p>{w?.seller_name} promised delivery by {shortDate(w?.delivery_date)}. When it arrives, tell your agent how it went.</p>
        <div className="btn-row">
          <button className="btn primary" disabled={busy} onClick={() => run(() => api.delivered(order.id))}>
            <Busy busy={busy} invert text={`Paying ${w?.seller_name || "the seller"}`}>Received, looks good</Busy>
          </button>
          <button className="btn" disabled={busy} onClick={() => setReporting(true)}>Report a problem</button>
        </div>
        <MoveDelivery order={order} reload={reload} onError={onError} />
        <p className="hint">"Received" pays {w?.seller_name} {money(order.paypal?.authorized_amount ?? w?.total, cur)} from the hold.
          "Report a problem" keeps the money on hold while your agent sorts it out.</p>
      </section>
    );
  }

  const again = (
    <button className="btn primary" onClick={onNewOrder}>Start a new order</button>
  );

  if (order.status === "escalated" && order.dispute_packet) {
    return (
      <section className="deal deal-alert">
        <h2>Dispute file ready</h2>
        <p>{order.dispute_packet.summary} Your money is still on hold.</p>
        <p className="hint">{order.dispute_packet.requested_outcome}</p>
        {again}
      </section>
    );
  }

  if (order.status === "completed" || order.status === "settled") {
    const p = order.paypal || {};
    return (
      <section className="deal">
        <h2>{order.status === "completed" ? "Order complete" : "Settled"}</h2>
        <p>
          {money(p.captured, cur)} paid to {w?.seller_name}
          {p.released > 0 ? `, ${money(p.released, cur)} returned to you` : ""}.
        </p>
        {again}
      </section>
    );
  }

  if (order.status === "no_deal" || order.status === "failed") {
    return (
      <section className="deal">
        <p>{order.error || "No deal was made, and no money moved."}</p>
        {again}
      </section>
    );
  }
  return null;
}

// Ask the seller agent for another delivery day (the same request as dragging it on the calendar).
function MoveDelivery({ order, reload, onError }) {
  const w = order.winner;
  const [open, setOpen] = useState(false);
  const [date, setDate] = useState(w?.delivery_date || "");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  if (!w) return null;
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const r = await api.reschedule(order.id, date);
      setResult(r);
      if (r.accepted) setOpen(false);
      await reload();
    } catch (ex) {
      onError(ex.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="move">
      {!open ? (
        <button type="button" className="link move-open" onClick={() => { setOpen(true); setResult(null); }}>
          Need it on a different day?
        </button>
      ) : (
        <form className="move-form" onSubmit={submit}>
          <label className="field-label" htmlFor="move-date">Deliver on</label>
          <div className="move-row">
            <input id="move-date" type="date" value={date} min={inDays(1)} onChange={(e) => setDate(e.target.value)} />
            <button className="btn btn-small" disabled={busy || !date || date === w.delivery_date}>
              <Busy busy={busy} text={`Asking ${w.seller_name}`}>Ask {w.seller_name}</Busy>
            </button>
            <button type="button" className="link" onClick={() => setOpen(false)} disabled={busy}>Cancel</button>
          </div>
          <p className="hint">Your agent asks {w.seller_name} over A2A. The price and your PayPal hold don't change.</p>
        </form>
      )}
      {result && <p className={`move-result ${result.accepted ? "is-ok" : "is-no"}`} role="status">{result.message}</p>}
    </div>
  );
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
        <Busy busy={busy} invert text="Sending to your agent">Send to my agent</Busy>
      </button>
    </form>
  );
}

function Market({ bench }) {
  return (
    <section className="market">
      <h2>Market check</h2>
      <p>
        {bench.retailers} retailer{bench.retailers === 1 ? "" : "s"} on Channel3 sell this exact item for{" "}
        {bench.low === bench.high
          ? money(bench.low, bench.currency)
          : `${money(bench.low, bench.currency)} to ${money(bench.high, bench.currency)}`}{" "}
        each.
      </p>
      <ul className="retailers">
        {(bench.offers || []).slice(0, 4).map((o, i) => (
          <li key={`${o.domain}-${i}`}>
            <span>{o.domain}</span>
            <b>{money(o.price, bench.currency)}</b>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ProductPicker({ order, reload, onError }) {
  const m = order.mandate;
  const products = order.candidates || [];
  const recommended = products.find((p) => p.recommended) || products[0];
  const [picked, setPicked] = useState(recommended?.id);
  const product = products.find((p) => p.id === picked) || recommended;
  const [qty, setQty] = useState(recommended?.suggested_quantity || m.quantity);
  const [busy, setBusy] = useState(false);

  const pick = (p) => {
    setPicked(p.id);
    setQty(p.suggested_quantity || m.quantity);
  };
  const retail = product ? product.low * (Number(qty) || 0) : 0;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.chooseProduct(order.id, product.id, Number(qty));
      await reload();
    } catch (ex) {
      onError(ex.message);
      setBusy(false);
    }
  };

  return (
    <form className="picker" onSubmit={submit}>
      <h2>Which product should your agent buy?</h2>
      <p className="hint">Real products from Channel3. Sellers will quote on exactly the one you pick.</p>
      <div className="products" role="radiogroup" aria-label="Products">
        {products.map((p) => (
          <button type="button" key={p.id} role="radio" aria-checked={p.id === product?.id}
            className={`product ${p.id === product?.id ? "product-on" : ""}`} onClick={() => pick(p)}>
            <div className="product-img">{p.image ? <img src={p.image} alt="" loading="lazy" /> : <span>No photo</span>}</div>
            {p.recommended && <span className="pick-badge">Agent's pick</span>}
            <span className="product-title">{p.title}</span>
            {p.brand && <span className="product-brand">{p.brand}</span>}
            <span className="product-price">
              {money(p.low, m.currency)}
              {p.high > p.low && <> to {money(p.high, m.currency)}</>}
            </span>
            <span className="product-meta">
              {p.retailers} retailer{p.retailers === 1 ? "" : "s"}
              {p.units_per_item ? `, ${p.units_per_item} per pack` : ""}
            </span>
          </button>
        ))}
      </div>
      {product && (
        <div className="picker-foot">
          <label className="field-label" htmlFor="qty">How many of this item?</label>
          <div className="qty-row">
            <input id="qty" type="number" min="1" max="100000" value={qty} onChange={(e) => setQty(e.target.value)} />
            <span className="hint">
              {product.units_per_item
                ? `${(Number(qty) || 0) * product.units_per_item} units in total. `
                : "Pack size isn't stated, so each counts as one. "}
              At the lowest retail price that's {money(retail, m.currency)}
              {retail > m.budget_max ? `, over your ${money(m.budget_max, m.currency)} budget.` : "."}
            </span>
          </div>
          <button className="btn primary" disabled={busy || !(Number(qty) >= 1)}>
            <Busy busy={busy} invert text="Starting the negotiation">{`Negotiate for ${Number(qty) || 0} × this item`}</Busy>
          </button>
        </div>
      )}
    </form>
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
        <button className="btn" disabled={busy || !q.trim()}><Busy busy={busy} text="Thinking">Ask</Busy></button>
      </div>
      {answer && <p className="answer">{answer}</p>}
    </form>
  );
}
