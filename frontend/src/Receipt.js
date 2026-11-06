// The order's receipt: every signed AP2 v0.2 document in order (your open mandates → the seller's
// signed checkout → the checkout and payment approvals → the seller's receipts), then Procuro's own
// signed extensions (a moved delivery, the release). Each shows who signed it, whether it checks out
// (verified on the server with Google's AP2 SDK), its fingerprint, what it points to, and the PayPal
// records it caused. Then PayPal's own record of the hold and capture, compared with the signed amounts.

import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { Bone, Spinner } from "./ui";
import "./receipt.css";

const short = (h, n = 12) => (h ? `${h.slice(0, n)}…` : "");
const when = (ts) =>
  ts ? new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";

function Mark({ ok }) {
  if (ok === null || ok === undefined) return <span className="rc-mark rc-wait" aria-label="Not yet">•</span>;
  return ok
    ? <span className="rc-mark rc-ok" aria-label="Passes">✓</span>
    : <span className="rc-mark rc-bad" aria-label="Fails">✗</span>;
}

function Copy({ text, label = "Copy" }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="rc-copy" onClick={() => {
      navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); });
    }}>{done ? "Copied" : label}</button>
  );
}

function PayPalRecord({ rec }) {
  return (
    <li className={`rc-pp rc-pp-${rec.kind}`}>
      <span className="rc-pp-label">{rec.label}</span>
      <code>{rec.id}</code>
      {rec.request_id && <span className="rc-pp-rid">PayPal-Request-Id <code>{rec.request_id}</code></span>}
      {rec.link && <a href={rec.link} target="_blank" rel="noreferrer">Open in PayPal sandbox</a>}
    </li>
  );
}

function Step({ step, index, raw, stepNo }) {
  const ext = step.standard === "Procuro extension";
  const shown = step.decoded ?? raw;
  return (
    <li className={`rc-step ${step.verified ? "" : "rc-step-bad"}`}>
      <span className="rc-num" aria-hidden="true">{index + 1}</span>
      <div className="rc-body">
        <div className="rc-step-head">
          <h3>{step.title}</h3>
          <span className="rc-type">
            {step.standard && <span className={`rc-std ${ext ? "rc-std-ext" : ""}`}>{step.standard}</span>}
            {step.type.replace(/_/g, " ")}
          </span>
        </div>
        <p className="rc-summary">{step.summary}</p>
        <p className="rc-meta">
          Signed by <b>{step.signed_by}</b>{step.issued_at ? <> · {when(step.issued_at)}</> : null}
          {step.format && <> · {step.format}</>}
        </p>
        <p className={`rc-sig ${step.verified ? "ok" : "bad"}`}>
          <Mark ok={step.verified} />
          {step.verified
            ? <>Checks out{step.alg ? ` (${step.alg})` : ""}, using the key from {step.key_from}.</>
            : <>Doesn't check out{step.verify_error ? `: ${step.verify_error}` : ". This step was changed after it was signed."}</>}
        </p>
        <dl className="rc-facts">
          <div><dt>Fingerprint</dt><dd><code title={step.hash}>{short(step.hash, 16)}</code> <Copy text={step.hash} /></dd></div>
          {step.id !== step.hash && (
            <div><dt>Id</dt><dd><code title={step.id}>{short(step.id, 24)}</code></dd></div>
          )}
          {step.refers_to.map((r) => (
            <div key={r.field}>
              <dt>Points to</dt>
              <dd className={r.ok ? "" : "rc-broken"}>
                <Mark ok={r.ok} /> <code title={r.id}>{r.id ? short(r.id, 18) : "nothing"}</code>
                {" "}<span className="rc-field">{r.field}</span>
                {r.ok && (r.label ? ` (${r.label})` : stepNo[r.id] ? ` (step ${stepNo[r.id]})`
                  : r.step === "paypal authorization" ? " (the PayPal hold)" : "")}
                {!r.ok && " (doesn't match)"}
              </dd>
            </div>
          ))}
        </dl>
        {step.paypal.length > 0 && <ul className="rc-pps">{step.paypal.map((p) => <PayPalRecord key={`${p.kind}-${p.id}`} rec={p} />)}</ul>}
        {shown && (
          <details className="rc-raw">
            <summary>Show exactly what was signed</summary>
            <pre>{JSON.stringify(shown, null, 2)}</pre>
            {step.token && (
              <>
                <p className="rc-raw-label">The signed token, as sent between the agents <Copy text={step.token} /></p>
                <pre className="rc-token">{step.token}</pre>
              </>
            )}
          </details>
        )}
      </div>
    </li>
  );
}

function LiveCheck({ orderId, version, hasHold }) {
  const [live, setLive] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = useCallback(() => {
    setBusy(true);
    api.receiptPayPal(orderId)
      .then(setLive)
      .catch((e) => setLive({ items: [], error: e.message }))
      .finally(() => setBusy(false));
  }, [orderId]);
  useEffect(() => {
    if (hasHold) run();
  }, [hasHold, run, version]);
  if (!hasHold) return null;
  return (
    <section className="rc-live" aria-live="polite">
      <div className="rc-live-head">
        <h3>PayPal's own records</h3>
        <button type="button" className="btn btn-small" onClick={run} disabled={busy}>Check again</button>
      </div>
      {busy && !live ? (
        <p className="rc-loading"><Spinner size={14} /> Asking PayPal for its record of the hold and the payment…</p>
      ) : (
        <>
          <ul className="rc-checks">
            {(live?.items || []).map((i) => (
              <li key={i.id + i.label}>
                <Mark ok={i.ok} />
                <div><b>{i.label}</b> <code>{i.id}</code> <span className="rc-status">{i.status}</span>
                  <p className="hint">{i.detail}</p></div>
              </li>
            ))}
          </ul>
          {live?.error && <p className="hint warn">Couldn't confirm with PayPal: {live.error}</p>}
          {live?.environment === "mock" && <p className="hint">Demo mode: these come from the stand-in for PayPal, not the sandbox.</p>}
        </>
      )}
    </section>
  );
}

function ReceiptSkeleton() {
  return (
    <div className="rc" aria-busy="true" aria-label="Loading the receipt">
      <Bone w="40%" h={22} />
      <Bone h={70} r={8} style={{ marginTop: 14 }} />
      {[0, 1, 2].map((i) => <Bone key={i} h={120} r={8} style={{ marginTop: 14 }} />)}
    </div>
  );
}

export default function Receipt({ orderId, version }) {
  const [r, setR] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    api.receipt(orderId).then((x) => live && setR(x)).catch((e) => live && setError(e.message));
    return () => { live = false; };
  }, [orderId, version]);

  if (error && !r) return <p className="hint warn">Couldn't load the receipt: {error}</p>;
  if (!r) return <ReceiptSkeleton />;
  if (!r.steps.length) {
    return <p className="hint">Nothing has been signed yet. Steps appear here as you and the agents sign them.</p>;
  }

  const raw = Object.fromEntries((r.mandates || []).map((m) => [m.id, m]));
  const stepNo = Object.fromEntries(r.steps.map((s, i) => [s.id, i + 1]));
  const failing = r.checks.filter((c) => c.ok === false).length;
  const download = () => {
    const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `receipt-${r.order_id}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };

  return (
    <div className="rc">
      <header className={`rc-head ${failing ? "rc-head-bad" : ""}`}>
        <div>
          <h2>{failing ? `${failing} check${failing > 1 ? "s" : ""} failed` : "Every step is signed and checks out"}</h2>
          <p className="hint">
            {r.steps.length} signed document{r.steps.length > 1 ? "s" : ""}, each pointing to the one before. Change
            any number and its signature and the fingerprint below stop matching.
          </p>
          {r.standard && (
            <p className="rc-standard">
              {r.standard === "AP2 v0.2" ? <>Agent Payments Protocol <b>v0.2</b>, checked with Google's AP2 SDK</> : r.standard}
              {r.mode === "human_not_present" && " · your agent paid on its own, within your limit"}
              {r.mode === "human_present" && " · you approved the payment"}
            </p>
          )}
        </div>
        <div className="rc-fp">
          <span>Receipt fingerprint (SHA-256)</span>
          <code title={r.fingerprint}>{short(r.fingerprint, 24)}</code>
          <div className="rc-fp-actions">
            <Copy text={r.fingerprint} label="Copy" />
            <button type="button" className="btn btn-small" onClick={download}>Download receipt</button>
          </div>
        </div>
      </header>

      <ul className="rc-checks">
        {r.checks.map((c) => (
          <li key={c.label}><Mark ok={c.ok} /><div><b>{c.label}</b><p className="hint">{c.detail}</p></div></li>
        ))}
      </ul>

      <LiveCheck orderId={orderId} version={version} hasHold={r.steps.some((s) => s.paypal.some((p) => p.kind === "authorization"))} />

      <ol className="rc-steps">
        {r.steps.map((s, i) => <Step key={s.id} step={s} index={i} raw={raw[s.id]} stepNo={stepNo} />)}
      </ol>
      {r.release_missing && (
        <p className="hint">This order was paid before receipts kept the signed release, so that last step isn't shown.</p>
      )}
      <p className="hint rc-foot">
        Check it without Procuro: download the receipt and, in the backend folder, run{" "}
        <code>python scripts/verify_receipt.py receipt-{r.order_id}.json</code>. It re-checks every signature,
        link and fingerprint on your own computer with Google's AP2 SDK.
      </p>
    </div>
  );
}