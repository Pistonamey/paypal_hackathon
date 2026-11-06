// Bryntum Calendar on the start screen: the owner clicks the day the goods must arrive by, and that
// exact date goes to the buying agent as the mandate's deadline. The calendar also shows what's
// already coming (deliveries, payment releases, hold renewals from other orders, via
// GET /api/calendar), so the owner can see the week before committing to a date.
//
// A delivery that hasn't arrived yet can be dragged to another day. The drop waits while the buying
// agent asks that seller agent over A2A; the seller decides from its own production schedule. If it
// agrees (and signs a change to the cart), the delivery lands on the new day; if not, it snaps back
// and the note says the nearest date the seller can do.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BryntumCalendar } from "@bryntum/calendar-react-thin";
import { CalendarDrag } from "@bryntum/calendar-thin";
import { api } from "../api";
import { day, isoDay, useBryntumStyles } from "./base";
import { CalendarSkeleton, Spinner } from "../ui";

// With thin packages each feature registers itself when imported.
export const CALENDAR_FEATURES = [CalendarDrag];

const startOfToday = () => {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
};

export default function DeliveryCalendar({ value, onChange, onMoved }) {
  const ref = useRef(null);
  const styled = useBryntumStyles();
  const picked = useRef(value);
  const [events, setEvents] = useState(null); // null while loading
  const [note, setNote] = useState("");
  const [painted, setPainted] = useState(false);
  const [moving, setMoving] = useState(null); // { seller, date } while the agents talk

  const loadEvents = useCallback(() => api.calendar().then(setEvents).catch(() => setEvents([])), []);
  useEffect(() => {
    loadEvents();
  }, [loadEvents]);

  // A dragged delivery: ask the seller agent, and only let the drop land if it agrees.
  const moveDelivery = useCallback(async ({ eventRecord, newStartDate }) => {
    if (!eventRecord.draggable) return false;
    const date = new Date(newStartDate);
    date.setHours(0, 0, 0, 0);
    if (date <= startOfToday()) {
      setNote("Deliveries can only move to a day after today.");
      return false;
    }
    const iso = isoDay(date);
    const seller = eventRecord.sellerName || "the seller";
    setMoving({ seller, date: iso });
    setNote("");
    try {
      const r = await api.reschedule(eventRecord.orderId, iso);
      if (r.accepted) {
        setNote(`${r.message} Same price; your PayPal hold didn't change.`);
        loadEvents(); // payment release and renewals move with it
        onMoved?.(eventRecord.orderId);
        return true;
      }
      setNote(r.message + (r.earliest ? " Drag it there, or pick another day." : ""));
      return false;
    } catch (e) {
      setNote(`Couldn't move it: ${e.message}`);
      return false;
    } finally {
      setMoving(null);
    }
  }, [loadEvents, onMoved]);

  // Keep the skeleton up until Bryntum has drawn the month (with a safety net if paint never fires).
  useEffect(() => {
    if (!styled) return undefined;
    const t = setTimeout(() => setPainted(true), 2500);
    return () => clearTimeout(t);
  }, [styled]);

  // Re-draw the cells so the highlighted day follows the selection.
  useEffect(() => {
    picked.current = value;
    ref.current?.instance?.activeView?.refresh?.();
  }, [value]);

  const pick = useCallback(
    (date) => {
      if (!date) return false;
      const d = new Date(date);
      d.setHours(0, 0, 0, 0);
      if (d <= startOfToday()) {
        setNote("Pick a day after today.");
        return false;
      }
      setNote("");
      onChange(isoDay(d));
      return false; // don't open a day view or create an event
    },
    [onChange]
  );

  const config = useMemo(
    () => ({
      mode: "month",
      date: startOfToday(),
      readOnly: false, // only so deliveries can be dragged; nothing can be created, edited or deleted
      autoCreate: false,
      sidebar: false,
      modes: {
        day: null,
        week: null,
        year: null,
        agenda: null,
        month: {
          sixWeeks: false,
          showWeekColumn: false,
          autoCreate: false,
          dayCellRenderer: (cellData, domConfig) => {
            const key = isoDay(cellData.date);
            domConfig.className = domConfig.className || {};
            domConfig.className["is-picked"] = key === picked.current;
            domConfig.className["is-past"] = cellData.date <= startOfToday();
            return domConfig;
          },
        },
      },
      eventEditFeature: false,
      dragFeature: { creatable: false, resizable: false, draggable: true },
      eventMenuFeature: false,
      scheduleMenuFeature: false,
      eventTooltipFeature: {
        // Read-only: the tooltip just explains the step, no edit/delete tools.
        tools: { edit: false, delete: false },
      },
    }),
    []
  );

  return (
    <section className="cal" aria-label="Choose the delivery date">
      <div className="cal-head">
        <h2>When do you need it by?</h2>
        <p className="cal-picked">
          {value ? (
            <>
              Deliver by <b>{day(`${value}T12:00:00`)}</b>
            </>
          ) : (
            "Click a day on the calendar."
          )}
        </p>
      </div>
      <div className="cal-body boot" aria-busy={!painted}>
        {styled && <BryntumCalendar
          ref={ref}
          {...config}
          events={events || []}
          onPaint={() => requestAnimationFrame(() => setPainted(true))}
          onScheduleClick={({ date }) => pick(date)}
          onEmptyCellClick={({ date }) => pick(date)}
          onDayNumberClick={({ date }) => pick(date)}
          onEventClick={({ date }) => pick(date)}
          onBeforeDragMoveEnd={moveDelivery}
          onBeforeDragMove={({ eventRecord }) => !!eventRecord.draggable}
        />}
        <div className={`boot-cover ${painted ? "is-gone" : ""}`} aria-hidden={painted}>
          <CalendarSkeleton bare />
        </div>
      </div>
      <p className="cal-note" role="status">
        {moving ? (
          <span className="cal-loading"><Spinner size={14} /> Asking {moving.seller} to deliver on {day(`${moving.date}T12:00:00`)}…</span>
        ) : note || (events === null ? (
          <span className="cal-loading"><Spinner size={14} /> Checking your other orders' deliveries and payments</span>
        ) : events.length
          ? (events.some((e) => e.draggable)
            ? "Already on your calendar: your deliveries and payments. Drag a delivery to another day and your agent asks the seller to move it."
            : "Already on your calendar: deliveries, payments and hold renewals from your other orders.")
          : "Click any day after today. Deliveries and payments from your orders will show here too.")}
      </p>
    </section>
  );
}