// Shared loading and progress pieces. Every screen shows the shape of what's coming (skeletons) or
// a spinner tied to the thing that's busy, never a bare "Loading…" line.

import "./ui.css";

export function Spinner({ size = 20, label, invert = false, className = "" }) {
  return (
    <span className={`spinner ${invert ? "spinner-invert" : ""} ${className}`} role={label ? "status" : undefined}
      style={{ width: size, height: size }}>
      {label && <span className="sr-only">{label}</span>}
    </span>
  );
}

// Button content: a spinner and a short "what's happening" label while busy, otherwise the normal label.
export function Busy({ busy, text, invert = false, children }) {
  if (!busy) return children;
  return (
    <span className="busy">
      <Spinner size={18} invert={invert} />
      <span>{text}</span>
    </span>
  );
}

export function Bone({ w = "100%", h = 14, r = 6, className = "", style }) {
  return <span className={`bone ${className}`} style={{ width: w, height: h, borderRadius: r, ...style }} aria-hidden="true" />;
}

// Wraps a skeleton so screen readers hear one "Loading x" instead of a pile of empty shapes.
function Loading({ label, className = "", children }) {
  return (
    <div className={`skeleton ${className}`} role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}

// Three dots in a bubble: the agents are talking right now.
export function Typing({ text }) {
  return (
    <div className="typing" role="status">
      <span className="typing-dots" aria-hidden="true"><i /><i /><i /></span>
      <span>{text}</span>
    </div>
  );
}

export function FeedSkeleton() {
  const rows = [["start", "62%"], ["end", "54%"], ["start", "70%"], ["end", "46%"], ["start", "58%"]];
  return (
    <Loading label="Loading the conversation" className="feed-skel">
      {rows.map(([side, w], i) => (
        <div key={i} className={`bubble-skel bubble-${side}`} style={{ width: w }}>
          <Bone w="34%" h={10} />
          <Bone h={12} />
          <Bone w="80%" h={12} />
        </div>
      ))}
    </Loading>
  );
}

export function OrderSkeleton() {
  return (
    <Loading label="Loading your order" className="order-skel">
      <div className="steps-skel">
        {[0, 1, 2, 3, 4].map((i) => (
          <span key={i} className="step-skel"><Bone w={26} h={26} r={99} /><Bone w="60%" h={12} /></span>
        ))}
      </div>
      <div className="order">
        <aside className="order-side">
          <div className="card-skel">
            <Bone w="70%" h={22} />
            <div className="row-skel"><Bone h={34} /><Bone h={34} /><Bone h={34} /></div>
          </div>
          <div className="card-skel"><Bone w="80%" h={20} /><Bone h={12} /><Bone w="60%" h={12} /><Bone h={46} r={6} /></div>
          <div className="card-skel bare"><Bone w="65%" h={30} /><Bone h={22} r={4} /></div>
        </aside>
        <section className="order-main">
          <div className="row-skel tabs-skel"><Bone w={150} h={18} /><Bone w={80} h={18} /></div>
          <div className="feed"><FeedSkeleton /></div>
        </section>
      </div>
    </Loading>
  );
}

export function CalendarSkeleton({ bare = false }) {
  const grid = (
    <div className="cal-skel">
      <div className="row-skel"><Bone w={64} h={26} /><Bone w={50} h={26} /><Bone w={140} h={22} /></div>
      <div className="cal-grid-skel">
        {Array.from({ length: 35 }, (_, i) => <Bone key={i} h="100%" r={4} />)}
      </div>
    </div>
  );
  if (bare) return grid;
  return (
    <Loading label="Loading the calendar" className="cal">
      <div className="cal-head"><h2>When do you need it by?</h2></div>
      {grid}
    </Loading>
  );
}

export function GanttSkeleton({ bare = false }) {
  const bars = [[0, 8], [4, 10], [10, 14], [14, 30], [18, 22], [22, 40], [30, 56], [40, 60], [52, 70], [62, 74]];
  const body = (
    <div className="gantt-skel">
      {bars.map(([from, to], i) => (
        <div key={i} className="gantt-row-skel">
          <Bone w={i % 3 === 0 ? "55%" : "75%"} h={12} />
          <div className="gantt-track-skel">
            <Bone h={14} style={{ marginLeft: `${from}%`, width: `${Math.max(3, to - from)}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
  if (bare) return body;
  return <Loading label="Loading the timeline" className="timeline">{body}</Loading>;
}

export function BoardSkeleton() {
  return (
    <Loading label="Loading the sellers' schedules" className="board">
      <div className="board-head"><div><Bone w={120} h={28} /><Bone w={520} h={12} style={{ marginTop: 10 }} /><Bone w={420} h={12} style={{ marginTop: 6 }} /></div></div>
      <div className="board-skel">
        {[[10, 34, 64], [18, 40], [10, 70, 30]].map((bars, i) => (
          <div key={i} className="board-row-skel">
            <div><Bone w="70%" h={16} /><Bone w="50%" h={11} style={{ marginTop: 6 }} /></div>
            <div><Bone w="60%" h={14} /><Bone w="45%" h={11} style={{ marginTop: 6 }} /></div>
            <div className="board-track-skel">
              {bars.map((w, j) => <Bone key={j} h={26} w={`${w}%`} style={{ marginLeft: `${12 + j * 3}%` }} />)}
            </div>
          </div>
        ))}
      </div>
    </Loading>
  );
}

export function DashboardTilesSkeleton() {
  return (
    <div className="dash-body-skel">
      <div className="dash-tiles">
        {[0, 1, 2, 3].map((i) => <div key={i} className="card-skel"><Bone w="60%" h={12} /><Bone w="45%" h={28} /></div>)}
      </div>
      <div className="dash-panels">
        <div className="card-skel tall"><Bone w="40%" h={14} /><Bone h={16} /><Bone w="80%" h={16} /><Bone w="65%" h={16} /></div>
        <div className="card-skel tall"><Bone w="50%" h={14} /><Bone h={14} /><Bone h={14} /><Bone w="70%" h={14} /></div>
      </div>
      <div className="dash-panels">
        <div className="card-skel tall"><Bone w="35%" h={14} /><Bone h={120} /></div>
        <div className="card-skel tall"><Bone w="45%" h={14} /><Bone w={140} h={140} r={99} style={{ justifySelf: "center" }} /></div>
      </div>
    </div>
  );
}

export function DashboardSkeleton() {
  return (
    <Loading label="Loading your spending" className="spend dash-skel">
      <Bone w={240} h={34} />
      <Bone w={360} h={12} style={{ marginTop: 10 }} />
      <div className="row-skel tabs-skel" style={{ marginTop: 22 }}><Bone w={130} h={18} /><Bone w={170} h={18} /></div>
      <DashboardTilesSkeleton />
    </Loading>
  );
}

export function HistorySkeleton() {
  return (
    <Loading label="Loading your earlier orders" className="history-skel">
      {[0, 1, 2].map((i) => (
        <div key={i} className="history-row"><Bone w={`${40 - i * 6}%`} h={14} /><Bone w={110} h={22} r={99} /></div>
      ))}
    </Loading>
  );
}

// ---------- where an order is, at a glance ----------

const SUB = {
  you: "Your turn",
  agent: "Agents working",
  wait: "Waiting",
  problem: "Needs attention",
};

// Each step: done, current (with who's acting), or still to come.
export function orderSteps(order) {
  const s = order.status;
  const hasProduct = !!(order.candidates?.length || order.product);
  const seller = order.winner?.seller_name || "Seller";
  const steps = [
    ...(hasProduct ? [{ key: "product", label: "Pick the product" }] : []),
    { key: "negotiate", label: "Agents negotiate" },
    { key: "approve", label: "You approve" },
    { key: "deliver", label: "Delivery", note: "Money on hold" },
    { key: "paid", label: `${seller} paid` },
  ];
  const at = (key, who, note) => ({ key, who, note });
  const current = {
    choosing_product: at("product", "you"),
    negotiating: at("negotiate", "agent"),
    awaiting_approval: at("approve", "you"),
    awaiting_paypal: at("approve", "you", "Finish in PayPal"),
    held: at("deliver", "you", "Confirm when it arrives"),
    resolving: at("deliver", "agent", "Sorting out a problem"),
    escalated: at("deliver", "problem", "Dispute file ready"),
    no_deal: at("negotiate", "problem", "No deal"),
    failed: at("negotiate", "problem", "Stopped"),
  }[s];
  const finished = s === "completed" || s === "settled";
  const idx = finished ? steps.length : steps.findIndex((x) => x.key === current?.key);
  return steps.map((step, i) => {
    if (i < idx) return { ...step, state: "done" };
    if (i === idx) return { ...step, state: "now", who: current.who, note: current.note || SUB[current.who] };
    if (finished) return { ...step, state: "done" };
    return { ...step, state: "todo" };
  }).map((step) =>
    step.key === "paid" && s === "settled" ? { ...step, label: `${seller} partly paid` } : step
  );
}

export function Stepper({ order }) {
  const steps = orderSteps(order);
  return (
    <ol className="stepper" aria-label="Order progress">
      {steps.map((st, i) => (
        <li key={st.key} className={`st-step is-${st.state} ${st.who ? `by-${st.who}` : ""}`}
          aria-current={st.state === "now" ? "step" : undefined}>
          <span className="st-dot" aria-hidden="true">
            {st.state === "done" ? "✓" : st.state === "now" && st.who === "agent" ? <Spinner size={16} /> :
              st.state === "now" && st.who === "problem" ? "!" : i + 1}
          </span>
          <span className="st-text">
            <span className="st-label">{st.label}</span>
            {st.state === "now" && <span className="st-note">{st.note}</span>}
            {st.state === "done" && <span className="sr-only">done</span>}
          </span>
        </li>
      ))}
    </ol>
  );
}