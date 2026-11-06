"""Each seller agent's production schedule.

A seller can only work on a few orders at once (its production slots). Every deal it
signs books a slot from the day production starts until delivery. When all slots are
taken, the seller's next quote moves to the first day a slot frees up, and it tells the
buyer agent why. So the schedule isn't just shown on the Sellers board: the agents use it.

A booking goes reserved (cart signed) -> confirmed (PayPal holds the money) -> delivered
(paid) or cancelled (hold voided). Reservations nobody approves expire after a day.
"""
import datetime as dt
import time

from .store import store

COLLECTION = "seller_schedules"
RESERVATION_HOURS = 24
ACTIVE = ("reserved", "confirmed")

# How many orders each seller can prepare at the same time.
SLOTS = {"quickprint": 2, "boxcraft": 1, "packright": 3}


def slots(seller_id: str) -> int:
    return SLOTS.get(seller_id, 2)


def _today() -> dt.date:
    return dt.date.today()


def _day(value: str) -> dt.date:
    return dt.date.fromisoformat(value[:10])


def bookings(seller_id: str) -> dict[str, dict]:
    return store.get_doc(COLLECTION, seller_id).get("bookings", {})


def _save(seller_id: str, items: dict[str, dict]) -> None:
    store.set_doc(COLLECTION, seller_id, {"bookings": items})


def _expired(b: dict, now: float) -> bool:
    return b["status"] == "reserved" and now - b.get("reserved_at", now) > RESERVATION_HOURS * 3600


def _occupying(items: dict[str, dict], now: float | None = None, exclude: str | None = None) -> list[dict]:
    """Bookings that hold a slot: confirmed ones, plus reservations that haven't expired."""
    now = now or time.time()
    return [b for oid, b in items.items()
            if oid != exclude and b["status"] in ACTIVE and not _expired(b, now)]


def load_by_day(seller_id: str, start: dt.date, days: int, exclude: str | None = None) -> list[int]:
    """How many slots are taken on each day from `start`. A booking holds its slot from
    production start up to (not including) the delivery day."""
    counts = [0] * days
    for b in _occupying(bookings(seller_id), exclude=exclude):
        b_start, b_end = _day(b["start"]), _day(b["delivery_date"])
        for i in range(days):
            d = start + dt.timedelta(days=i)
            if b_start <= d < b_end:
                counts[i] += 1
    return counts


def next_start(seller_id: str, lead_days: int, today: dt.date | None = None,
               exclude: str | None = None, horizon: int = 120) -> dt.date:
    """First day a new order can start production and keep a slot free for `lead_days` days."""
    today = today or _today()
    counts = load_by_day(seller_id, today, horizon + lead_days, exclude=exclude)
    cap = slots(seller_id)
    for i in range(horizon):
        if all(c < cap for c in counts[i:i + lead_days]):
            return today + dt.timedelta(days=i)
    return today + dt.timedelta(days=horizon)


def reserve(seller_id: str, order_id: str, *, item: str, quantity: int, total: float, currency: str,
            start: str, delivery_date: str) -> dict:
    items = bookings(seller_id)
    booking = {"order_id": order_id, "item": item, "quantity": quantity, "total": total, "currency": currency,
               "start": start, "delivery_date": delivery_date, "status": "reserved", "reserved_at": time.time()}
    items[order_id] = booking
    _save(seller_id, items)
    return booking


def mark(seller_id: str, order_id: str, status: str, **extra) -> dict | None:
    items = bookings(seller_id)
    b = items.get(order_id)
    if not b:
        return None
    b["status"] = status
    b[f"{status}_at"] = time.time()
    b.update(extra)
    if status == "delivered":
        # Delivered (and paid) today: the slot frees up from today, whatever the plan was.
        today = _today().isoformat()
        b["planned_delivery"] = b["delivery_date"]
        b["delivery_date"] = today
        b["start"] = min(b["start"], today)
    _save(seller_id, items)
    return b


def move(seller_id: str, order_id: str, *, start: str, delivery_date: str) -> dict | None:
    """The buyer and seller agreed a new delivery date: the booking's slot moves with it."""
    items = bookings(seller_id)
    b = items.get(order_id)
    if not b:
        return None
    b.setdefault("first_delivery_date", b["delivery_date"])
    b.update({"start": start, "delivery_date": delivery_date, "moved_at": time.time()})
    _save(seller_id, items)
    return b


def slot_for(seller_id: str, lead_days: int, wanted: dt.date, order_id: str,
             today: dt.date | None = None) -> tuple[bool, dt.date]:
    """Can this seller deliver on `wanted`? Production needs `lead_days` days in a free slot before it.

    Returns (yes, date): the wanted date if it fits, otherwise the nearest delivery date that does.
    The order's own booking doesn't count against itself.
    """
    today = today or _today()
    earliest = next_start(seller_id, lead_days, today, exclude=order_id) + dt.timedelta(days=lead_days)
    if wanted < earliest:
        return False, earliest
    start = wanted - dt.timedelta(days=lead_days)
    fits = next_start(seller_id, lead_days, start, exclude=order_id)
    if fits == start:
        return True, wanted
    return False, fits + dt.timedelta(days=lead_days)


def busy_reason(seller_id: str, start: dt.date, today: dt.date | None = None) -> str | None:
    today = today or _today()
    if start <= today:
        return None
    n = slots(seller_id)
    return (f"all {n} of our production slots are booked" if n > 1 else "our production line is booked") + \
        f" until {start.isoformat()}"


# ---------- the Sellers board ----------

def _iso_day(d: dt.date) -> str:
    return d.isoformat()


def board(sellers: dict[str, dict], today: dt.date | None = None) -> dict:
    """Rows (one per seller agent), bars (one per booking), and the days each seller is full."""
    today = today or _today()
    now = time.time()
    resources, events, full = [], [], []
    first, last = today - dt.timedelta(days=3), today + dt.timedelta(days=18)
    for sid, s in sellers.items():
        items = bookings(sid)
        cap = slots(sid)
        nxt = next_start(sid, s["lead_days"], today)
        occupying = _occupying(items, now)
        resources.append({
            "id": sid, "name": s["name"], "tagline": s["tagline"], "slots": cap, "leadDays": s["lead_days"],
            "inProgress": len(occupying),
            "nextStart": _iso_day(nxt), "nextDelivery": _iso_day(nxt + dt.timedelta(days=s["lead_days"])),
        })
        for oid, b in items.items():
            status = b["status"]
            if status == "cancelled" or _expired(b, now):
                continue
            if status == "confirmed":
                order = store.get_order(oid) or {}
                if order.get("status") == "escalated":
                    status = "disputed"
            start, delivery = _day(b["start"]), _day(b["delivery_date"])
            first, last = min(first, start), max(last, delivery + dt.timedelta(days=3))
            events.append({
                "id": f"{sid}-{oid}", "resourceId": sid, "orderId": oid,
                "name": f"{b['quantity']} x {b['item']}",
                "startDate": _iso_day(start),
                "endDate": _iso_day(delivery + dt.timedelta(days=1)),  # the bar covers the delivery day
                "deliveryDate": b["delivery_date"], "plannedDelivery": b.get("planned_delivery"),
                "status": status, "total": b.get("total"), "captured": b.get("captured"),
                "currency": b.get("currency", "USD"),
            })
        # Shade the days this seller has no free slot.
        horizon = (last - today).days + 1
        counts = load_by_day(sid, today, horizon)
        run = None
        for i, c in enumerate(counts + [0]):
            if c >= cap and run is None:
                run = i
            elif c < cap and run is not None:
                full.append({"id": f"full-{sid}-{run}", "resourceId": sid, "name": "Fully booked",
                             "startDate": _iso_day(today + dt.timedelta(days=run)),
                             "endDate": _iso_day(today + dt.timedelta(days=i))})
                run = None
    first = max(first, today - dt.timedelta(days=14))  # older history scrolls off the board
    return {"today": _iso_day(today), "startDate": _iso_day(first), "endDate": _iso_day(last),
            "resources": resources, "events": events, "fullyBooked": full}