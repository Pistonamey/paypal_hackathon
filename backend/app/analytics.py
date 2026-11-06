"""Tables for the AG Studio "Spend" dashboard.

Built only from real orders in the store: every row is an order the agents actually
negotiated and paid (or held) through PayPal.
"""
import datetime as dt

from . import sellers as seller_agents
from .store import store

STATUS_GROUP = {
    "negotiating": "In progress",
    "awaiting_approval": "In progress",
    "awaiting_paypal": "In progress",
    "held": "On hold",
    "completed": "Paid",
    "resolving": "Problem",
    "settled": "Settled after problem",
    "escalated": "Dispute",
    "no_deal": "No deal",
    "failed": "No deal",
}


def _r(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def _date(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()


def _rows_for_order(order: dict) -> tuple[dict, list[dict], list[dict]]:
    m = order.get("mandate") or {}
    w = order.get("winner") or {}
    p = order.get("paypal") or {}
    sid = w.get("seller_id")
    seller_entry = (order.get("sellers") or {}).get(sid or "", {})
    opening = seller_entry.get("opening_total", w.get("total"))
    held = p.get("authorized_amount") if p.get("authorization_id") and not p.get("capture_id") and not p.get("voided") else 0
    captured = p.get("captured") or 0
    released = p.get("released") or 0
    created = order.get("created_at", 0)
    rounds = sum(1 for e in (order.get("sellers") or {}).values() for o in e.get("offers", [])) or 0
    deadline, delivery = m.get("deadline"), w.get("delivery_date")
    days_early = None
    if deadline and delivery:
        days_early = (dt.date.fromisoformat(deadline) - dt.date.fromisoformat(delivery)).days
    assessment = order.get("assessment") or {}

    row = {
        "id": order["id"],
        "orderDate": _date(created) if created else None,
        "item": m.get("item"),
        "quantity": m.get("quantity"),
        "status": order.get("status"),
        "statusGroup": STATUS_GROUP.get(order.get("status"), order.get("status")),
        "sellerId": sid,
        "seller": w.get("seller_name") or "No seller",
        "currency": m.get("currency", "USD"),
        "budget": m.get("budget_max"),
        "approvalLimit": m.get("approval_threshold"),
        "openingQuote": opening,
        "finalPrice": w.get("total"),
        "unitPrice": w.get("unit_price"),
        "savedByNegotiation": _r(opening - w["total"]) if opening and w.get("total") else 0,
        "underBudget": _r(m["budget_max"] - w["total"]) if w.get("total") and m.get("budget_max") else None,
        "held": _r(held or 0),
        "paidToSeller": _r(captured),
        "returnedToYou": _r(released),
        "deadline": deadline,
        "deliveryDate": delivery,
        "daysBeforeDeadline": days_early,
        "neededApproval": bool(w.get("needs_explicit_approval")),
        "hadProblem": bool(assessment),
        "problemOrders": 1 if assessment else 0,
        "orderCount": 1,
        "damagedUnits": assessment.get("damaged_units", 0),
        "offersReceived": rounds,
        "marketUnitPrice": (order.get("benchmark") or {}).get("median"),
    }

    offers = []
    for s_id, entry in (order.get("sellers") or {}).items():
        for i, o in enumerate(entry.get("offers", [])):
            if not o.get("unit_price"):
                continue
            offers.append({
                "id": f"{order['id']}-{s_id}-{i}",
                "orderId": order["id"],
                "sellerId": s_id,
                "seller": entry.get("name", s_id),
                "round": i,
                "stage": "First quote" if i == 0 else f"Round {i}",
                "unitPrice": o["unit_price"],
                "total": o.get("total"),
                "deliveryDate": o.get("delivery_date"),
                "sellerOutcome": entry.get("status"),
                "won": s_id == sid,
            })

    events = []
    day = row["orderDate"]
    if p.get("authorization_id"):
        events.append({"type": "Held", "amount": p.get("authorized_amount", 0)})
    if captured:
        events.append({"type": "Paid to seller", "amount": captured})
    if released:
        events.append({"type": "Returned to you", "amount": released})
    if p.get("reauthorized"):
        events.append({"type": "Hold renewed", "amount": p.get("authorized_amount", 0)})
    money = [{"id": f"{order['id']}-m{i}", "orderId": order["id"], "date": day, "seller": w.get("seller_name"),
              "sellerId": sid, **e} for i, e in enumerate(events)]
    return row, offers, money


# ------------------------------------------------------------------ public

def tables() -> dict:
    orders, offers, money = [], [], []
    live = store.list_orders()
    for o in live:
        r, of, mo = _rows_for_order(o)
        orders.append(r)
        offers += of
        money += mo

    seller_rows = []
    for sid, s in seller_agents.SELLERS.items():
        stats = store.get_doc("sellers", sid)
        won = [r for r in orders if r["sellerId"] == sid]
        problems = sum(1 for r in won if r["hadProblem"])
        quoted = [x for x in offers if x["sellerId"] == sid and x["round"] == 0]
        seller_rows.append({
            "id": sid,
            "name": s["name"],
            "ordersWon": len(won),
            "problemOrders": problems,
            "reliability": stats.get("score", round(100 * (len(won) - problems + 4) / (len(won) + 4))),
            "quotesGiven": len(quoted),
            "leadDays": s["lead_days"],
        })
    return {
        "asOf": dt.datetime.now(dt.timezone.utc).isoformat(),
        "liveOrders": len(live),
        "orders": orders,
        "offers": offers,
        "money": money,
        "sellers": seller_rows,
    }