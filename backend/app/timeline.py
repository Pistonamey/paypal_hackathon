"""An order's whole life as Gantt data: what has happened, what happens next, and the dates that matter.

Groups:
  Deal      mandate signed, deal agreed with a seller agent, owner approves (PayPal hold starts)
  Delivery  seller prepares and ships, delivery (with the owner's deadline), owner checks the goods
  Money     PayPal hold, hold renewal after PayPal's 3-day honor period, the 29-day hold limit,
            payment released to the seller, money returned to the owner
  Problem   only when something went wrong: reported, agents settle it

Every task carries status Done / Now / Planned. Planned dates are projections from the agreed terms
(the seller's promised delivery date, one day to check the goods). When something has happened,
the promise is kept as a baseline so the Gantt shows plan vs reality.
"""
import datetime as dt
import time

HONOR_DAYS = 3
MAX_HOLD_DAYS = 29
CHECK_DAYS = 1  # time the owner has to check the goods before payment is released

UTC = dt.timezone.utc


def zone(offset_minutes: int = 0) -> dt.timezone:
    """The viewer's timezone from JavaScript's getTimezoneOffset() (minutes behind UTC; India is -330)."""
    offset_minutes = max(-840, min(840, int(offset_minutes or 0)))
    return dt.timezone(dt.timedelta(minutes=-offset_minutes))


def _from_ts(ts: float | None, tz: dt.timezone = UTC) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(float(ts), tz) if ts else None


def _from_day(day: str | None, hour: int = 12, tz: dt.timezone = UTC) -> dt.datetime | None:
    """A calendar date (YYYY-MM-DD) as a moment on that day, in the viewer's timezone."""
    if not day:
        return None
    return dt.datetime.combine(dt.date.fromisoformat(day[:10]), dt.time(hour), tz)


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _money(v: float | None, cur: str) -> str:
    return f"{v:,.2f} {cur}" if v is not None else ""


class _Builder:
    def __init__(self) -> None:
        self.groups: dict[str, dict] = {}
        self.order: list[str] = []
        self.deps: list[dict] = []
        self.ids: set[str] = set()
        self.moments: list[dt.datetime] = []

    def group(self, gid: str, name: str, icon: str) -> None:
        self.groups[gid] = {"id": gid, "name": name, "expanded": True, "iconCls": icon, "kind": "group",
                            "children": []}
        self.order.append(gid)

    def task(self, gid: str, tid: str, name: str, start: dt.datetime, end: dt.datetime | None = None, *,
             status: str, kind: str, detail: str = "", amount: float | None = None,
             baseline: tuple[dt.datetime, dt.datetime] | None = None, deadline: dt.datetime | None = None) -> None:
        end = end if end and end > start else None
        t = {
            "id": tid, "name": name, "startDate": _iso(start), "manuallyScheduled": True,
            "status": status, "kind": kind, "detail": detail,
            "percentDone": 100 if status == "Done" else (50 if status == "Now" else 0),
            "cls": f"t-{kind} t-{status.lower()}",
        }
        if end:
            t["endDate"] = _iso(end)
        else:
            t["duration"] = 0  # a milestone
        if amount is not None:
            t["amount"] = round(float(amount), 2)
        if baseline:
            t["baselines"] = [{"startDate": _iso(baseline[0]), "endDate": _iso(baseline[1])}]
        if deadline:
            t["deadlineDate"] = _iso(deadline)
        self.groups[gid]["children"].append(t)
        self.ids.add(tid)
        self.moments += [start] + ([end] if end else [])

    def link(self, a: str, b: str) -> None:
        if a in self.ids and b in self.ids:
            self.deps.append({"id": f"{a}->{b}", "fromTask": a, "toTask": b, "type": 2})


def build(order: dict, now: float | None = None, tz_offset: int = 0) -> dict:
    tz = zone(tz_offset)
    now_t = _from_ts(now or time.time(), tz)
    m, w, p = order.get("mandate") or {}, order.get("winner") or {}, order.get("paypal") or {}
    times = order.get("times") or {}
    cur = m.get("currency", "USD")
    status = order.get("status", "negotiating")
    seller = w.get("seller_name") or "the seller"

    created = _from_ts(order.get("created_at"), tz) or now_t
    started = _from_ts(times.get("negotiating"), tz) or created
    agreed = _from_ts(times.get("awaiting_approval"), tz)
    approved = _from_ts(times.get("awaiting_paypal"), tz)
    held = _from_ts(times.get("held"), tz) or _from_ts(p.get("first_authorized_at") or p.get("authorized_at"), tz)
    delivered_ok = _from_ts(times.get("completed"), tz)
    problem_at = _from_ts(times.get("resolving"), tz)
    settled = _from_ts(times.get("settled"), tz)
    escalated = _from_ts(times.get("escalated"), tz)
    ended_no_deal = _from_ts(times.get("no_deal") or times.get("failed"), tz)

    deadline = _from_day(m.get("deadline"), 23, tz)
    promised = _from_day(w.get("delivery_date"), 12, tz)
    arrived = delivered_ok or problem_at  # the owner confirmed it, or reported a problem with it

    b = _Builder()

    # ---- Deal
    b.group("deal", "Deal", "fa fa-handshake")
    if order.get("ap2") or order.get("intent_mandate"):
        b.task("deal", "mandate", "Mandate signed", started, status="Done", kind="deal",
               detail=f"Budget {_money(m.get('budget_max'), cur)}, ask above {_money(m.get('approval_threshold'), cur)}, "
                      f"deliver by {m.get('deadline')}.")
    if ended_no_deal:
        b.task("deal", "nodeal", "No deal: no seller met the budget and deadline", ended_no_deal, status="Done",
               kind="problem", detail="No money moved.")
    elif agreed:
        saved = w.get("saved_vs_opening")
        b.task("deal", "agreed", f"Deal agreed with {seller}", agreed, status="Done", kind="deal", amount=w.get("total"),
               detail=f"{w.get('quantity', m.get('quantity'))} × {m.get('item')} at {_money(w.get('unit_price'), cur)} each"
                      + (f", {_money(saved, cur)} below their first quote." if saved else "."))
    else:
        b.task("deal", "agreed", "Agents negotiate the deal", max(now_t, started), status="Now", kind="deal",
               detail="Your buying agent is negotiating with the seller agents.")
    if not ended_no_deal:
        if held:
            b.task("deal", "approve", "You approved; PayPal is holding the money", held, status="Done", kind="deal",
                   amount=p.get("authorized_amount"), detail="Money held on your PayPal account, not paid yet.")
        else:
            when = max(now_t, agreed or now_t)
            b.task("deal", "approve", "You approve and PayPal holds the money", when,
                   status="Now" if status in ("awaiting_approval", "awaiting_paypal") else "Planned", kind="deal",
                   amount=w.get("total"), detail="Nothing is charged until you approve, and then it's only held.")

    # ---- Delivery
    b.group("delivery", "Delivery", "fa fa-truck")
    ship_from = held or max(now_t, agreed or now_t)
    planned_arrival = promised or deadline
    if not ended_no_deal and planned_arrival:
        if arrived:
            b.task("delivery", "ship", f"{seller} prepares and ships", ship_from, arrived, status="Done", kind="delivery",
                   baseline=(ship_from, planned_arrival) if promised else None,
                   detail=f"Promised for {w.get('delivery_date')}.")
            early = (planned_arrival.date() - arrived.date()).days if promised else 0
            when = (f"{early} day{'s' if early != 1 else ''} before the promised date" if early > 0 else
                    "on the promised date" if early == 0 else f"{-early} day(s) late")
            b.task("delivery", "arrived",
                   "Arrived: you confirmed it" if delivered_ok else "Arrived: you reported a problem",
                   arrived, status="Done", kind="delivery" if delivered_ok else "problem", deadline=deadline,
                   baseline=(planned_arrival, planned_arrival) if promised else None, detail=f"Arrived {when}.")
        else:
            b.task("delivery", "ship", f"{seller} prepares and ships", ship_from, planned_arrival,
                   status="Now" if held else "Planned", kind="delivery",
                   detail=("Promised by " + w["delivery_date"]) if promised else "Not agreed yet: your deadline is the limit.")
            first = w.get("first_delivery_date")
            moved = first and first != w.get("delivery_date")
            b.task("delivery", "arrived", f"Delivery promised by {seller}" if promised else "Delivery (your deadline)",
                   planned_arrival, status="Planned", kind="delivery", deadline=deadline,
                   # A moved delivery keeps its first promise as the baseline, so the move is visible.
                   baseline=(_from_day(first, 12, tz),) * 2 if moved else None,
                   detail=(f"Moved from {first} to {w.get('delivery_date')} ({seller} signed the change). " if moved else "")
                   + f"Your deadline is {m.get('deadline')}.")
            b.task("delivery", "check", "You check the goods", planned_arrival,
                   planned_arrival + dt.timedelta(days=CHECK_DAYS), status="Planned", kind="delivery",
                   detail="Confirm it's fine and the payment is released, or report a problem and it stays on hold.")

    # ---- Money
    amount = p.get("authorized_amount") or w.get("total")
    closed = p.get("capture_id") or p.get("voided")  # the hold ends with a capture, or a void if nothing is paid
    captured_at = delivered_ok if closed and delivered_ok else settled if closed else None
    if not ended_no_deal and amount:
        b.group("money", "Money (PayPal)", "fa fa-wallet")
        hold_start = held or ship_from
        projected_release = (planned_arrival + dt.timedelta(days=CHECK_DAYS)) if planned_arrival else None
        hold_end = captured_at or (escalated and now_t) or (problem_at and now_t) or projected_release
        if hold_end and hold_end <= hold_start:
            hold_end = hold_start + dt.timedelta(hours=6)
        b.task("money", "hold", f"PayPal holds {_money(amount, cur)}", hold_start, hold_end,
               status="Done" if captured_at else "Now" if held else "Planned", kind="money", amount=amount,
               detail="Held on your PayPal account. The seller can see it's guaranteed but can't take it.")
        open_until = hold_end or hold_start
        if p.get("reauthorized_at"):
            b.task("money", "renew", "Hold renewed", _from_ts(p["reauthorized_at"], tz), status="Done", kind="money",
                   detail="PayPal honors a hold for 3 days; your agent renewed it automatically.")
        elif not captured_at and open_until > hold_start + dt.timedelta(days=HONOR_DAYS):
            b.task("money", "renew", "Agent renews the hold", hold_start + dt.timedelta(days=HONOR_DAYS),
                   status="Planned", kind="money",
                   detail="PayPal honors a hold for 3 days, so your agent renews it before then.")
        if not captured_at:
            b.task("money", "limit", "Last day PayPal can keep the hold", hold_start + dt.timedelta(days=MAX_HOLD_DAYS),
                   status="Planned", kind="limit", detail="After 29 days a hold must be captured or it expires.")
        if captured_at:
            paid_name = (f"Hold voided: nothing paid to {seller}" if p.get("voided")
                         else f"Paid to {seller}: {_money(p.get('captured'), cur)}")
            b.task("money", "release", paid_name, captured_at, status="Done",
                   kind="money", amount=p.get("captured"),
                   baseline=(projected_release, projected_release) if projected_release else None,
                   detail="Released after you confirmed the goods." if delivered_ok else "Paid for the usable units only.")
            if p.get("released"):
                b.task("money", "returned", f"Returned to you: {_money(p['released'], cur)}", captured_at,
                       status="Done", kind="returned", amount=p["released"], detail="The rest of the hold, released back.")
        elif escalated:
            b.task("money", "release", "Dispute file ready; money stays on hold", escalated, status="Now",
                   kind="problem", detail="PayPal's resolution process decides what's paid.")
        elif projected_release:
            b.task("money", "release", f"Payment released to {seller}", projected_release,
                   status="Planned", kind="money", amount=amount,
                   detail="Only after you confirm the goods arrived in good shape.")

    # ---- Problem
    if problem_at:
        b.group("problem", "Problem", "fa fa-triangle-exclamation")
        a = order.get("assessment") or {}
        b.task("problem", "reported", f"You reported a problem ({a.get('damaged_units', '?')} damaged)", problem_at,
               status="Done", kind="problem", detail=order.get("problem_note") or "")
        end = settled or escalated
        b.task("problem", "settle", "Agents settle it" if settled else "Agents negotiate a fair settlement",
               problem_at, end or max(now_t, problem_at + dt.timedelta(hours=6)),
               status="Done" if end else "Now", kind="problem",
               detail=("Settled: paid for usable units only." if settled else
                       "No agreement: dispute file prepared." if escalated else "In progress."))

    for a_, b_ in [("mandate", "agreed"), ("agreed", "approve"), ("approve", "ship"), ("approve", "hold"),
                   ("ship", "arrived"), ("arrived", "check"), ("check", "release"), ("arrived", "release"),
                   ("arrived", "reported"), ("reported", "settle"), ("settle", "release"), ("renew", "release")]:
        if not (a_ == "arrived" and b_ == "release" and "check" in b.ids):
            b.link(a_, b_)

    tasks = [b.groups[g] for g in b.order if b.groups[g]["children"]]
    moments = b.moments + ([deadline] if deadline else []) + [now_t]
    start = min(moments).date() - dt.timedelta(days=1)
    end = max(moments).date() + dt.timedelta(days=3)
    leaves = [t for g in tasks for t in g["children"]]
    # "Now": what's under way (the hold bar is shown by the money meter, so it's only used as a last resort).
    current = sorted((t for t in leaves if t["status"] == "Now"), key=lambda t: (t["id"] == "hold", t["startDate"]))
    upcoming = sorted((t for t in leaves if t["status"] == "Planned" and t["kind"] != "limit"),
                      key=lambda t: t["startDate"])
    time_ranges = [{"id": "deadline", "name": "Your deadline", "startDate": _iso(deadline), "cls": "tr-deadline"}] \
        if deadline else []
    return {
        "orderId": order["id"],
        "currency": cur,
        "status": status,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "tasks": tasks,
        "dependencies": b.deps,
        "timeRanges": time_ranges,
        "now": {"name": current[0]["name"], "date": current[0]["startDate"]} if current else None,
        "next": {"name": upcoming[0]["name"], "date": upcoming[0]["startDate"]} if upcoming else None,
    }


CALENDAR_STEPS = {
    "arrived": ("Arrives", "teal"),
    "release": ("Payment", "green"),
    "renew": ("Hold renewal", "yellow"),
}


def calendar_events(orders: list[dict], now: float | None = None, tz_offset: int = 0) -> list[dict]:
    """Upcoming and recent deliveries, payments and hold renewals across all orders, for the calendar."""
    events = []
    for order in orders:
        if order.get("status") in ("no_deal", "failed", "choosing_product"):
            continue
        tl = build(order, now, tz_offset)
        m = order.get("mandate") or {}
        label = f"{m.get('quantity')} × {m.get('item')}"
        for g in tl["tasks"]:
            for t in g["children"]:
                if t["id"] not in CALENDAR_STEPS:
                    continue
                prefix, color = CALENDAR_STEPS[t["id"]]
                if t["kind"] == "problem":
                    color = "red"
                start = dt.date.fromisoformat(t["startDate"][:10])
                events.append({
                    "id": f"{order['id']}-{t['id']}",
                    "name": f"{prefix}: {label}",
                    "startDate": start.isoformat(),
                    "endDate": (start + dt.timedelta(days=1)).isoformat(),  # all-day events end the next day
                    "allDay": True,
                    "eventColor": color if t["status"] != "Done" else "gray",
                    "orderId": order["id"],
                    "status": t["status"],
                    "detail": t["name"],
                    # A delivery that hasn't arrived can be dragged to another day: the agents renegotiate it.
                    "draggable": t["id"] == "arrived" and order.get("status") in ("awaiting_approval", "held"),
                    "resizable": False,
                    "sellerName": (order.get("winner") or {}).get("seller_name"),
                })
    return events