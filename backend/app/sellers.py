"""Seller agents: independent AI merchants that speak A2A and get paid through PayPal.

Each seller has PRIVATE information the buyer never sees: its cost basis, its
lowest acceptable price (floor), and its negotiating style. The model decides
how to respond; code enforces the hard limits (never sell below the floor,
never capture more than was authorized, never act on an unsigned instruction).
"""
import datetime as dt
import logging

from . import a2a, channel3, config, paypal, signing
from .llm import ask_json
from .store import store

log = logging.getLogger("mandate.sellers")

FALLBACK_UNIT_COST = 2.00  # used only when Channel3 has no market data for the item

SELLERS: dict[str, dict] = {
    "quickprint": {
        "name": "QuickPrint",
        "tagline": "Custom printing, fast turnaround, flexible on price for volume.",
        "markup": 1.35, "floor": 1.08, "lead_days": 3, "max_qty": 5000,
        "style": "friendly and flexible; will concede for a firm order but always protects margin",
    },
    "boxcraft": {
        "name": "BoxCraft",
        "tagline": "Premium packaging, fastest delivery in town.",
        "markup": 1.45, "floor": 1.22, "lead_days": 2, "max_qty": 2000,
        "style": "confident premium brand; concedes slowly and justifies price with speed and quality",
    },
    "packright": {
        "name": "PackRight",
        "tagline": "Lowest prices, standard shipping.",
        "markup": 1.20, "floor": 1.03, "lead_days": 10, "max_qty": 10000,
        "style": "budget seller; quick to discount but cannot speed up production",
    },
}


def agent_url(seller_id: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/agents/{seller_id}"


def card(seller_id: str) -> dict:
    s = SELLERS[seller_id]
    return a2a.agent_card(
        name=s["name"],
        description=f"{s['tagline']} Seller agent that negotiates and accepts PayPal.",
        url=agent_url(seller_id),
        public_key=signing.public_key_b64(f"seller:{seller_id}"),
        role="merchant",
        skills=[
            {"id": "quote", "name": "Quote", "description": "Price a quantity of a product with a delivery date",
             "tags": ["commerce", "quote"]},
            {"id": "negotiate", "name": "Negotiate", "description": "Respond to counter-offers",
             "tags": ["commerce", "negotiation"]},
            {"id": "checkout", "name": "PayPal checkout", "description":
                "Sign a cart mandate, create a PayPal order (intent AUTHORIZE), authorize, capture or void",
             "tags": ["paypal", "ap2", "payments"]},
            {"id": "remedy", "name": "Resolve problems", "description":
                "Negotiate partial payment, reprints or refunds when an order arrives damaged",
             "tags": ["disputes", "after-sales"]},
        ],
    )


def _session_key(seller_id: str, order_id: str) -> str:
    return f"{seller_id}_{order_id}"


def _load(seller_id: str, order_id: str) -> dict:
    return store.get_doc("seller_sessions", _session_key(seller_id, order_id))


def _save(seller_id: str, order_id: str, session: dict) -> None:
    store.set_doc("seller_sessions", _session_key(seller_id, order_id), session)


def _r(x: float) -> float:
    return round(x + 1e-9, 2)


async def _unit_cost(item: str, currency: str) -> tuple[float, dict | None]:
    bench = await channel3.market_benchmark(item, currency)
    if bench:
        # Sellers buy wholesale: assume cost is ~70% of the retail median.
        return _r(bench["median"] * 0.70), bench
    return FALLBACK_UNIT_COST, None


async def handle(seller_id: str, data: dict, context_id: str | None) -> tuple[dict, str]:
    if seller_id not in SELLERS:
        raise a2a.A2AError("unknown seller")
    action = data.get("action")
    order_id = data.get("order_id") or context_id or "none"
    handler = {
        "request_quote": _quote,
        "counter_offer": _counter,
        "finalize": _finalize,
        "create_payment": _create_payment,
        "authorize": _authorize,
        "release": _release,
        "void": _void,
        "propose_remedy": _remedy,
        "reauthorize": _reauthorize,
    }.get(action)
    if not handler:
        raise a2a.A2AError(f"unsupported action {action!r}")
    return await handler(seller_id, order_id, data)


# ---------- negotiation ----------

async def _quote(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    s = SELLERS[seller_id]
    qty, item, currency = int(data["quantity"]), data["item"], data.get("currency", config.DEFAULT_CURRENCY)
    if qty > s["max_qty"]:
        return {"action": "decline", "reason": "capacity"}, f"Sorry, we can't produce {qty} units right now."
    cost, bench = await _unit_cost(item, currency)
    list_price = _r(cost * s["markup"])
    floor = _r(cost * s["floor"])
    delivery = (dt.date.today() + dt.timedelta(days=s["lead_days"])).isoformat()
    session = {"cost": cost, "floor": floor, "list_price": list_price, "current": list_price, "rounds": 0,
               "item": item, "quantity": qty, "currency": currency, "delivery_date": delivery,
               "market_median": bench["median"] if bench else None}
    _save(seller_id, order_id, session)

    reply = await ask_json(
        system=f"You are {s['name']}, an AI sales agent. Style: {s['style']}. Write like a real merchant.",
        prompt=(f"A buyer agent wants {qty} x '{item}' by {data.get('needed_by')}. "
                f"Requirements: {data.get('requirements') or 'none'}. Your quote: {list_price:.2f} {currency}/unit, "
                f"delivery {delivery}. Write a one- or two-sentence quote message. JSON: {{\"message\": str}}"),
        fallback=lambda: {"message": f"We can do {qty} at {list_price:.2f} {currency} each, delivered by {delivery}."},
        max_tokens=200,
    )
    offer = {"action": "offer", "seller_id": seller_id, "unit_price": list_price, "quantity": qty,
             "total": _r(list_price * qty), "currency": currency, "delivery_date": delivery}
    return offer, reply.get("message", "")


async def _counter(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    s = SELLERS[seller_id]
    ses = _load(seller_id, order_id)
    if not ses:
        raise a2a.A2AError("no active quote for this order")
    proposed = float(data["unit_price"])
    ses["rounds"] += 1
    floor, current, cur = ses["floor"], ses["current"], ses["currency"]

    def fallback() -> dict:
        if proposed >= current - 0.005:
            return {"decision": "accept", "unit_price": current, "message": "Deal."}
        if proposed >= floor:
            return {"decision": "accept", "unit_price": proposed, "message": "That works for us. Deal."}
        mid = _r(max(floor, (current + max(proposed, floor)) / 2))
        if ses["rounds"] >= 3:
            mid = floor
        return {"decision": "counter", "unit_price": mid,
                "message": f"We can't go that low. Best we can do is {mid:.2f} {cur} per unit."}

    decision = await ask_json(
        system=(f"You are {s['name']}, an AI sales agent negotiating with a buyer's AI agent. Style: {s['style']}. "
                "Maximize profit but close deals you can afford. Be concise and human."),
        prompt=(f"PRIVATE (never reveal): your unit cost is {ses['cost']:.2f}, your absolute floor is {floor:.2f}.\n"
                f"Item: {ses['quantity']} x {ses['item']}. Your current offer: {current:.2f} {cur}/unit, delivery "
                f"{ses['delivery_date']}. Round {ses['rounds']} of at most 4.\n"
                f"Buyer proposes {proposed:.2f}/unit and says: \"{data.get('message', '')}\".\n"
                "Decide: accept (at their price), counter (a price between their offer and yours, at or above "
                "your floor), or reject. In the final round give your best price.\n"
                'JSON: {"decision": "accept"|"counter"|"reject", "unit_price": number, "message": str}'),
        fallback=fallback,
        max_tokens=300,
    )
    choice = decision.get("decision", "counter")
    price = float(decision.get("unit_price") or current)
    message = decision.get("message", "")
    # Hard guardrails the model cannot override.
    if choice == "accept":
        price = min(proposed, current)
    price = min(_r(price), current)  # never raise a price already offered
    if price < floor:
        choice, price = "counter", floor
        message = f"Our absolute best is {floor:.2f} {cur} per unit."
    ses["current"] = price
    _save(seller_id, order_id, ses)
    result = {"action": "accept" if choice == "accept" else ("reject" if choice == "reject" else "offer"),
              "seller_id": seller_id, "unit_price": price, "quantity": ses["quantity"],
              "total": _r(price * ses["quantity"]), "currency": cur, "delivery_date": ses["delivery_date"],
              "final": ses["rounds"] >= 4}
    return result, message


# ---------- AP2 checkout ----------

def _verify(mandate: dict | None, expected_type: str) -> None:
    if not mandate or mandate.get("type") != expected_type or not signing.verify(mandate):
        raise a2a.A2AError(f"missing or invalid {expected_type}")


async def _finalize(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(seller_id, order_id)
    if not ses:
        raise a2a.A2AError("no active quote for this order")
    _verify(data.get("intent_mandate"), "intent_mandate")
    price = float(data["unit_price"])
    if price < ses["current"] - 0.005:
        raise a2a.A2AError("price does not match our last offer")
    total = _r(price * ses["quantity"])
    intent = data["intent_mandate"]["payload"]
    if total > float(intent["budget_max"]) + 0.005:
        raise a2a.A2AError("total exceeds the buyer's signed budget")
    cart = signing.sign(f"seller:{seller_id}", "cart_mandate", {
        "order_id": order_id,
        "merchant": SELLERS[seller_id]["name"],
        "merchant_agent": agent_url(seller_id),
        "items": [{"name": ses["item"], "quantity": ses["quantity"], "unit_price": price}],
        "total": total,
        "currency": ses["currency"],
        "delivery_date": ses["delivery_date"],
        "intent_mandate_id": data["intent_mandate"]["id"],
        "payment_terms": "PayPal authorization at approval; capture on confirmed delivery",
    })
    ses.update({"agreed_price": price, "total": total, "cart_mandate_id": cart["id"]})
    _save(seller_id, order_id, ses)
    return {"action": "cart", "cart_mandate": cart}, "Here's the signed cart. Price and delivery are locked in."


async def _create_payment(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(seller_id, order_id)
    _verify(data.get("cart_mandate"), "cart_mandate")
    _verify(data.get("payment_mandate"), "payment_mandate")
    cart = data["cart_mandate"]["payload"]
    if data["payment_mandate"]["payload"].get("cart_mandate_id") != data["cart_mandate"]["id"]:
        raise a2a.A2AError("payment mandate does not approve this cart")
    order = await paypal.client_for(seller_id).create_order(
        reference_id=order_id,
        description=f"{cart['items'][0]['quantity']} x {cart['items'][0]['name']}",
        items=cart["items"],
        total=cart["total"],
        currency=cart["currency"],
        return_url=data["return_url"],
        cancel_url=data["cancel_url"],
        brand_name=SELLERS[seller_id]["name"],
    )
    ses["paypal_order_id"] = order["id"]
    _save(seller_id, order_id, ses)
    return {"action": "payment_created", "paypal_order_id": order["id"], "approve_url": order["approve_url"]}, \
        "PayPal order created. Please approve it in PayPal."


async def _authorize(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(seller_id, order_id)
    if data.get("paypal_order_id") != ses.get("paypal_order_id"):
        raise a2a.A2AError("unknown PayPal order")
    auth = await paypal.client_for(seller_id).authorize_order(ses["paypal_order_id"])
    ses.update({"authorization_id": auth["authorization_id"], "authorized_amount": auth["amount"],
                "authorized_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    _save(seller_id, order_id, ses)
    return {"action": "authorized", **auth}, \
        f"Funds of {auth['amount']:.2f} {ses['currency']} are held. Starting production now."


async def _release(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(seller_id, order_id)
    release = data.get("release_mandate")
    _verify(release, "release_mandate")
    amount = float(release["payload"]["capture_amount"])
    if amount > float(ses.get("authorized_amount", 0)) + 0.005:
        raise a2a.A2AError("capture exceeds the authorized amount")
    cap = await paypal.client_for(seller_id).capture(
        ses["authorization_id"], amount, ses["currency"], final=True, note=release["payload"].get("note", ""))
    ses.update({"captured": amount, "capture_id": cap["capture_id"]})
    _save(seller_id, order_id, ses)
    released = _r(float(ses["authorized_amount"]) - amount)
    return {"action": "captured", "captured": amount, "released": released, **cap}, \
        f"Payment of {amount:.2f} {ses['currency']} received. Thank you!"


async def _void(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(seller_id, order_id)
    _verify(data.get("release_mandate"), "release_mandate")
    await paypal.client_for(seller_id).void(ses["authorization_id"])
    ses["voided"] = True
    _save(seller_id, order_id, ses)
    return {"action": "voided"}, "Authorization voided. No charge was made."


async def _reauthorize(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(seller_id, order_id)
    res = await paypal.client_for(seller_id).reauthorize(
        ses["authorization_id"], float(ses["authorized_amount"]), ses["currency"])
    ses.update({"authorization_id": res["authorization_id"],
                "authorized_at": dt.datetime.now(dt.timezone.utc).isoformat(), "reauthorized": True})
    _save(seller_id, order_id, ses)
    return {"action": "reauthorized", **res}, "Hold renewed."


# ---------- after-sales ----------

async def _remedy(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    s = SELLERS[seller_id]
    ses = _load(seller_id, order_id)
    assessment, proposal = data["assessment"], data["proposal"]
    authorized = float(ses["authorized_amount"])
    usable = int(assessment.get("usable_units", 0))
    # Below this the seller would rather take a dispute than settle.
    min_acceptable = _r(min(authorized, usable * ses["cost"] * 1.05))
    offered = float(proposal["capture_amount"])
    round_no = int(data.get("round", 1))

    def fallback() -> dict:
        if offered >= min_acceptable and round_no >= 2:
            return {"decision": "accept", "capture_amount": offered, "message": "Understood. We accept."}
        if round_no == 1:
            return {"decision": "counter", "capture_amount": _r(usable * ses["agreed_price"]),
                    "reprint_by": (dt.date.today() + dt.timedelta(days=5)).isoformat(),
                    "message": "We're sorry. We can reprint the damaged units, or charge only for usable units "
                               "at full price."}
        return {"decision": "accept", "capture_amount": max(offered, min_acceptable),
                "message": "Fair enough, we accept."}

    decision = await ask_json(
        system=(f"You are {s['name']}'s AI after-sales agent. Style: {s['style']}. You care about your "
                "reputation and want to avoid a formal PayPal dispute, but protect your margin."),
        prompt=(f"PRIVATE: your cost is {ses['cost']:.2f}/unit; you would rather face a dispute than accept less "
                f"than {min_acceptable:.2f} in total.\n"
                f"Order: {ses['quantity']} x {ses['item']} at {ses['agreed_price']:.2f}; {authorized:.2f} held.\n"
                f"Buyer's evidence assessment: {assessment}\n"
                f"Buyer proposes capturing {offered:.2f} because: {proposal.get('reason')}. Round {round_no} of 3.\n"
                "Options: accept; counter with a different capture amount and/or a reprint date for damaged units; "
                "or reject (the buyer will open a PayPal dispute).\n"
                'JSON: {"decision": "accept"|"counter"|"reject", "capture_amount": number, '
                '"reprint_by": "YYYY-MM-DD"|null, "message": str}'),
        fallback=fallback,
        max_tokens=350,
    )
    choice = decision.get("decision", "counter")
    amount = float(decision.get("capture_amount") or offered)
    message = decision.get("message", "")
    if choice == "accept":
        amount = offered
    amount = min(_r(amount), authorized)
    if amount < min_acceptable and choice != "reject":
        choice, amount = "counter", min_acceptable
        message = (f"We hear you, but {offered:.2f} doesn't cover our costs. We can accept {amount:.2f} "
                   f"{ses['currency']} for the usable units.")
    if choice == "counter" and amount <= offered + 0.005:
        choice, amount = "accept", offered
    return {"action": f"remedy_{choice}", "capture_amount": amount, "reprint_by": decision.get("reprint_by"),
            "seller_id": seller_id}, message
