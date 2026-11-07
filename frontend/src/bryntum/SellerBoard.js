// The Sellers board: a Bryntum Scheduler with one row per seller agent and a bar for every order
// it's preparing, from the day production starts to delivery. Bars are coloured by where the
// money is. Shaded days are when a seller has no free production slot; that's what pushes its
// next quote later, so the agents read this schedule, not just Maya.

import { useEffect, useMemo, useRef, useState } from "react";
import { BryntumScheduler } from "@bryntum/scheduler-react-thin";
import { StringHelper } from "@bryntum/core-thin";
import { EventTooltip, ResourceTimeRanges, TimeRanges } from "@bryntum/scheduler-thin";
import { api } from "../api";
import { day, money, useBryntumStyles } from "./base";
import { BoardSkeleton, GanttSkeleton } from "../ui";

// With thin packages each feature registers itself when imported, so import every one we turn on.
export const BOARD_FEATURES = [EventTooltip, ResourceTimeRanges, TimeRanges];

const STATUS = {
  reserved: { label: "Waiting for approval", color: "gray" },
  confirmed: { label: "Money on hold", color: "yellow" },
  delivered: { label: "Delivered and paid", color: "green" },
  settled: { label: "Delivered, partly paid", color: "orange" },
  disputed: { label: "In dispute", color: "red" },
};

// Bryntum records only expose the fields they know as properties; extra ones live in `data`.
const isOutside = (r) => Boolean(r?.outside ?? r?.data?.outside ?? r?.originalData?.outside);

function statusOf(e) {
  return e.status === "delivered" && e.captured != null && e.total != null && e.captured < e.total - 0.005
    ? "settled"
    : e.status;
}

const enc = (v) => StringHelper.encodeHtml(String(v ?? ""));

export default function SellerBoard({ onOpenOrder }) {
  const ref = useRef(null);
  const styled = useBryntumStyles();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .sellerSchedule()
        .then((d) => alive && setData(d))
        .catch((e) => alive && setError(e.message));
    load();
    const t = setInterval(load, 10000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const resources = useMemo(() => data?.resources || [], [data]);
  const events = useMemo(
    () =>
      (data?.events || []).map((e) => {
        const s = statusOf(e);
        return { ...e, status: s, eventColor: STATUS[s]?.color || "gray", cls: `bk-${s}` };
      }),
    [data]
  );

  useEffect(() => {
    const sch = ref.current?.instance;
    if (!sch || !data) return;
    sch.setTimeSpan(new Date(data.startDate), new Date(data.endDate));
    // Open on the last couple of days, so today and what's coming fill the view.
    const from = new Date(data.today);
    from.setDate(from.getDate() - 2);
    sch.scrollToDate(from, { block: "start", animate: false });
    requestAnimationFrame(() => setReady(true));
  }, [data, styled]);

  const narrow = typeof window !== "undefined" && window.matchMedia("(max-width: 700px)").matches;

  const config = useMemo(
    () => ({
      readOnly: true,
      emptyText: "",
      viewPreset: "weekAndDayLetter",
      tickSize: 64, // wide days, so a bar has room for its name
      autoHeight: true, // every seller row stays visible however many orders it stacks
      rowHeight: 64,
      barMargin: 5,
      eventStyle: "colored",
      eventLayout: "stack",
      columns: [
        {
          text: "Seller agent",
          field: "name",
          width: narrow ? 150 : 190,
          htmlEncode: false,
          renderer: ({ record }) =>
            isOutside(record)
              ? `<div class="sb-seller"><b>${enc(record.name)}</b>` +
                `<span><i class="sb-outside">Outside</i> your orders only</span></div>`
              : `<div class="sb-seller"><b>${enc(record.name)}</b>` +
                `<span>${enc(record.slots)} slot${record.slots > 1 ? "s" : ""} · ${enc(record.leadDays)}-day lead</span>` +
                (narrow ? `<span>Next free: ${enc(day(record.nextDelivery))}</span>` : "") +
                `</div>`,
        },
        {
          text: "Next free delivery",
          field: "nextDelivery",
          width: 180,
          hidden: narrow, // on a phone the timeline needs the room; the tooltip still tells the story
          htmlEncode: false,
          renderer: ({ record }) => {
            if (isOutside(record)) {
              // Its schedule is private to it: your agent learns its dates when it asks for a quote.
              return `<div class="sb-next"><b>Asks when quoting</b>` +
                `<span>${enc(record.inProgress ?? record.data?.inProgress ?? 0)} of your orders in progress</span></div>`;
            }
            const busy = record.nextStart > (data?.today || "");
            return `<div class="sb-next ${busy ? "is-busy" : ""}"><b>${enc(day(record.nextDelivery))}</b>` +
              `<span>${busy ? `Booked until ${enc(day(record.nextStart))}` : `${enc(record.inProgress)} of ${enc(record.slots)} in use`}</span></div>`;
          },
        },
      ],
      eventRenderer: ({ eventRecord }) => ({
        tag: "div",
        className: "sb-bar",
        children: [
          { tag: "b", text: eventRecord.name },
          { tag: "span", text: `${STATUS[eventRecord.status]?.label || eventRecord.status} · ${money(eventRecord.total, eventRecord.currency)}` },
        ],
      }),
      eventTooltipFeature: {
        template: ({ eventRecord: e }) =>
          `<div class="tip"><b>${enc(e.name)}</b>` +
          (isOutside(e)
            ? `<div>Ordered ${enc(day(e.startDate))}, delivery ${enc(day(e.deliveryDate))}</div>`
            : `<div>Production starts ${enc(day(e.startDate))}, delivery ${enc(day(e.deliveryDate))}</div>`) +
          (e.plannedDelivery && e.plannedDelivery !== e.deliveryDate
            ? `<div>Promised for ${enc(day(e.plannedDelivery))}</div>` : "") +
          `<div>${enc(STATUS[e.status]?.label || e.status)}: ${enc(money(e.total, e.currency))}` +
          (e.captured != null ? `, paid ${enc(money(e.captured, e.currency))}` : "") +
          `</div><div class="tip-detail">Click to open the order.</div></div>`,
      },
      resourceTimeRangesFeature: true,
      timeRangesFeature: { showCurrentTimeLine: { name: "Today" }, showHeaderElements: true },
      eventMenuFeature: false,
      scheduleMenuFeature: false,
      cellMenuFeature: false,
      eventEditFeature: false,
      eventDragFeature: false,
      eventResizeFeature: false,
      eventDragCreateFeature: false,
      cellEditFeature: false,
    }),
    [data?.today]
  );

  const onEventClick = ({ eventRecord }) => eventRecord?.orderId && onOpenOrder?.(eventRecord.orderId);

  if (error) return <p className="hint warn">Couldn't load the sellers' schedules: {error}</p>;
  if (!data) return <BoardSkeleton />;

  const active = events.filter((e) => e.status === "confirmed" || e.status === "reserved").length;

  return (
    <section className="board" aria-label="Seller schedules">
      <div className="board-head">
        <div>
          <h1>Sellers</h1>
          <p className="board-sub">
            Each seller agent can prepare only a few orders at once. When its slots are full (shaded), its next quote
            moves to the first free day, and your agent weighs that against your deadline. Outside sellers run on
            their own servers and keep their schedules private, so their rows show only your orders with them.
          </p>
        </div>
        <p className="board-count">
          <b>{active}</b> order{active === 1 ? "" : "s"} in production
        </p>
      </div>
      <ul className="timeline-key board-key">
        <li><i style={{ background: "#9aa7b6" }} />Waiting for approval</li>
        <li><i className="k-hold" />Money on hold</li>
        <li><i style={{ background: "#43a047" }} />Delivered and paid</li>
        <li><i style={{ background: "#fb8c00" }} />Partly paid</li>
        <li><i style={{ background: "#e53935" }} />In dispute</li>
        <li><i className="k-full" />Fully booked</li>
      </ul>
      <div className="board-scheduler boot" aria-busy={!ready}>
        {styled && (
          <BryntumScheduler
            ref={ref}
            {...config}
            resources={resources}
            events={events}
            resourceTimeRanges={data.fullyBooked}
            startDate={new Date(data.startDate)}
            endDate={new Date(data.endDate)}
            onEventClick={onEventClick}
          />
        )}
        <div className={`boot-cover ${ready ? "is-gone" : ""}`} aria-hidden={ready}>
          <GanttSkeleton bare />
        </div>
      </div>
      {!data.events.length && (
        <p className="hint">No orders yet. When a seller signs a deal, its booking shows up here.</p>
      )}
    </section>
  );
}