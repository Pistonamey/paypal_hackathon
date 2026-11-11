// PayPal pieces of the app:
//  - WalletCard: connect the owner's PayPal once (Vault v3), auto-pay on/off, the monthly cap, disconnect.
//  - ShareOrder: split a bulk order with a partner business, billed with a PayPal invoice (Agent Toolkit).

import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { api } from "./api";
import { money } from "./format";
import { Busy, Spinner } from "./ui";
import "./paypal.css";

export function useWallet() {
  const [wallet, setState] = useState(null);
  const latest = useRef(0); // only the newest answer wins, so a slow first check can't undo a fresh connect
  const setWallet = useCallback((w) => {
    latest.current += 1;
    setState(w);
  }, []);
  const reload = useCallback(() => {
    const mine = ++latest.current;
    return api.wallet()
      .then((w) => mine === latest.current && setState(w))
      .catch((e) => mine === latest.current && setState({ status: "unavailable", error: e.message }));
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);
  return [wallet, reload, setWallet];
}

export function WalletCard({ wallet, onChange, onError, onRetry }) {
  const [cap, setCap] = useState("");
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirmOff, setConfirmOff] = useState(false);
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    if (wallet?.monthly_limit) setCap(String(wallet.monthly_limit));
    if (wallet?.auto_pay !== undefined) setAuto(wallet.auto_pay);
  }, [wallet?.monthly_limit, wallet?.auto_pay]);

  if (!wallet) {
    return (
      <section className="wallet wallet-loading" aria-busy="true">
        <Spinner size={16} /> Checking your PayPal connection
      </section>
    );
  }
  if (wallet.status === "unavailable") {
    // Never vanish silently: say the check failed and offer to try again.
    return (
      <section className="wallet wallet-warn" role="alert">
        <h2>Couldn't check your PayPal connection</h2>
        <p className="hint">The app couldn't reach the server{wallet.error ? ` (${wallet.error})` : ""}. Is the backend running?</p>
        {onRetry && <button type="button" className="btn btn-small" onClick={onRetry}>Try again</button>}
      </section>
    );
  }

  const run = async (fn) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError?.(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (wallet.status !== "active") {
    return (
      <section className="wallet" aria-label="Connect PayPal">
        <h2>Let your agent pay within your limits</h2>
        {wallet.status === "pending" && (
          <p className="wallet-pending">
            You started connecting PayPal but it wasn't finished, so it isn't connected yet. Connect again and
            approve on PayPal's page; you'll come straight back here.
          </p>
        )}
        <p>
          Connect PayPal once. Deals under an order's <b>ask me above</b> limit are then held automatically, and
          approving bigger ones is one tap. Money is only held until you confirm delivery.
        </p>
        <div className="wallet-fields">
          <label className="wallet-cap">
            <span>Monthly cap</span>
            <span className="money-input">
              <i>$</i>
              <input type="number" min="1" step="any" inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} />
            </span>
          </label>
          <label className="check">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            Auto-pay deals under my limit
          </label>
        </div>
        <button type="button" className="btn btn-paypal" disabled={busy || !(Number(cap) > 0)}
          onClick={() => run(async () => {
            const { approve_url: url } = await api.walletConnect(Number(cap), auto);
            window.location.href = url;
          })}>
          <Busy busy={busy} invert text="Opening PayPal">Connect PayPal</Busy>
        </button>
        <p className="hint">You approve once on PayPal. You can disconnect at any time.</p>
      </section>
    );
  }

  const used = wallet.spent_this_month || 0;
  const limit = wallet.monthly_limit || 0;
  const pct = limit ? Math.min(100, (used / limit) * 100) : 0;

  return (
    <section className="wallet wallet-on" aria-label="Your PayPal">
      <div className="wallet-head">
        <span className="wallet-dot" aria-hidden="true" />
        <div>
          <h2>PayPal connected</h2>
          <p className="hint">{[wallet.payer_name, wallet.payer_email].filter(Boolean).join(" · ")}</p>
        </div>
      </div>
      <label className="check">
        <input type="checkbox" checked={!!wallet.auto_pay} disabled={busy}
          onChange={(e) => run(async () => onChange(await api.walletUpdate({ auto_pay: e.target.checked })))} />
        Auto-pay deals under each order's <b>&nbsp;ask me above&nbsp;</b> limit
      </label>
      <div className="wallet-usage">
        <div className="wallet-usage-line">
          <span>This month</span>
          <b>{money(used)} of {money(limit)}</b>
        </div>
        <div className="wallet-bar" role="img" aria-label={`${money(used)} of a ${money(limit)} monthly cap used`}>
          <span style={{ width: `${pct}%` }} />
        </div>
      </div>
      <div className="wallet-capform">
        <label className="wallet-cap">
          <span>Monthly cap</span>
          <span className="money-input"><i>$</i>
            <input type="number" min="1" step="any" inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} />
          </span>
        </label>
        <button type="button" className="btn btn-small" disabled={busy || Number(cap) === limit || !(Number(cap) > 0)}
          onClick={() => run(async () => onChange(await api.walletUpdate({ monthly_limit: Number(cap) })))}>Save</button>
      </div>
      {confirmOff ? (
        <div className="wallet-confirm" role="alert">
          <p>Log out of PayPal? Procuro forgets this PayPal account, and every payment needs approving on PayPal again
            until you connect one.</p>
          <div className="btn-row">
            <button type="button" className="btn btn-small" disabled={busy}
              onClick={() => run(async () => { onChange(await api.walletDisconnect()); setConfirmOff(false); })}>
              <Busy busy={busy} text="Logging out">Yes, log out</Busy>
            </button>
            <button type="button" className="btn btn-small" onClick={() => setConfirmOff(false)}>Stay connected</button>
          </div>
        </div>
      ) : (
        <div className="wallet-account">
          <button type="button" className="btn btn-small" disabled={busy}
            onClick={() => {
              setSwitching(true);
              run(async () => {
                // The current account stays connected until the new one is approved on PayPal.
                const { approve_url: url } = await api.walletConnect(limit || 1000, !!wallet.auto_pay);
                window.location.href = url;
              }).finally(() => setSwitching(false));
            }}>
            <Busy busy={busy && switching} text="Opening PayPal">Sign in with another PayPal account</Busy>
          </button>
          <button type="button" className="link wallet-off" onClick={() => setConfirmOff(true)}>Log out of PayPal</button>
          <p className="hint">
            Switching keeps this account until the new one is approved. On PayPal's page, click <b>Not you?</b> (or log
            out there) if it signs you straight back in as {wallet.payer_email || "this account"}.
          </p>
        </div>
      )}
    </section>
  );
}

function QrCode({ value, png }) {
  const [src, setSrc] = useState(png ? `data:image/png;base64,${png}` : null);
  useEffect(() => {
    if (png || !value) return;
    QRCode.toDataURL(value, { margin: 1, width: 180, color: { dark: "#16301f", light: "#ffffff" } })
      .then(setSrc)
      .catch(() => setSrc(null));
  }, [value, png]);
  if (!src) return null;
  return <img className="qr" src={src} alt="QR code to pay this invoice with PayPal" />;
}

const INVOICE_STATUS = { PAID: "Paid", SENT: "Sent", DRAFT: "Draft", UNPAID: "Sent", CANCELLED: "Cancelled",
  PARTIALLY_PAID: "Partly paid", MARKED_AS_PAID: "Paid" };

export function ShareOrder({ order, reload, onError }) {
  const m = order.mandate;
  const w = order.winner;
  const shares = order.shares || [];
  const shared = shares.reduce((n, s) => n + (s.status === "CANCELLED" ? 0 : s.quantity), 0);
  const available = Math.max(0, m.quantity - shared - 1);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [qty, setQty] = useState("");
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const unpaid = shares.some((s) => !["PAID", "MARKED_AS_PAID", "CANCELLED"].includes(s.status) && !s.mock);
  const amount = (Number(qty) || 0) * (w?.unit_price || 0);

  useEffect(() => {
    // Check once for payments when the order opens.
    if (unpaid) api.refreshShares(order.id).then(reload).catch(() => {});
  }, [order.id]);

  if (!w) return null;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.share(order.id, { partner_name: name, partner_email: email, quantity: Number(qty) });
      setOpen(false);
      setName("");
      setEmail("");
      setQty("");
      await reload();
    } catch (ex) {
      onError(ex.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="share" aria-label="Share this order">
      <h2>Share this order</h2>
      <p className="hint">
        Splitting a bulk order with a neighbouring business? Your agent bills them for their part at the
        {" "}{money(w.unit_price, m.currency)} each it negotiated, with a PayPal invoice they can pay by link or QR code.
      </p>
      {shares.length > 0 && (
        <ul className="shares">
          {shares.map((s) => (
            <li key={s.invoice_id} className="share-row">
              <div className="share-main">
                <b>{s.partner_name}</b>
                <span>{s.quantity} × {money(s.unit_price, s.currency)} = <b>{money(s.amount, s.currency)}</b></span>
                <span className={`chip ${s.status === "PAID" || s.status === "MARKED_AS_PAID" ? "chip-paid" : "chip-sent"}`}>
                  {INVOICE_STATUS[s.status] || s.status}
                </span>
                {s.pay_url ? (
                  <a href={s.pay_url} target="_blank" rel="noreferrer">Open the PayPal invoice</a>
                ) : (
                  <span className="hint">{s.mock ? "Demo mode: no real invoice" : s.invoice_id}</span>
                )}
              </div>
              {s.pay_url && s.status !== "PAID" && <QrCode value={s.pay_url} png={s.qr_png} />}
            </li>
          ))}
        </ul>
      )}
      {unpaid && (
        <button type="button" className="btn btn-small" disabled={checking}
          onClick={async () => {
            setChecking(true);
            try { await api.refreshShares(order.id); await reload(); } catch (e) { onError(e.message); }
            finally { setChecking(false); }
          }}>
          <Busy busy={checking} text="Asking PayPal">Check for payments</Busy>
        </button>
      )}
      {available > 0 && !open && (
        <button type="button" className="btn btn-small" onClick={() => setOpen(true)}>Share part of this order</button>
      )}
      {open && (
        <form className="share-form" onSubmit={submit}>
          <label><span>Their business</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Corner Café" required />
          </label>
          <label><span>Their email</span>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="orders@cornercafe.com" required />
          </label>
          <label><span>How many {m.item}?</span>
            <input type="number" min="1" max={available} value={qty} onChange={(e) => setQty(e.target.value)} required />
          </label>
          <p className="hint">
            {Number(qty) > 0 ? <>They pay <b>{money(amount, m.currency)}</b>. </> : null}
            You keep {Math.max(0, m.quantity - shared - (Number(qty) || 0))} of {m.quantity}.
          </p>
          <div className="btn-row">
            <button className="btn primary" disabled={busy || !(Number(qty) >= 1 && Number(qty) <= available)}>
              <Busy busy={busy} invert text="Sending the invoice">Send PayPal invoice</Busy>
            </button>
            <button type="button" className="btn" onClick={() => setOpen(false)} disabled={busy}>Cancel</button>
          </div>
        </form>
      )}
    </section>
  );
}
