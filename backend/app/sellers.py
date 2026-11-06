"""Seller agents: independent AI merchants that speak A2A and get paid through PayPal.

Each seller has PRIVATE information the buyer never sees: its cost basis, its
lowest acceptable price (floor), and its negotiating style. The model decides
how to respond; code enforces the hard limits (never sell below the floor,
never capture more than was authorized, never act on an unsigned instruction).

Checkout and payment follow AP2 v0.2 (see mandates.py): the seller signs a Checkout JWT that must fit
the owner's open checkout mandate, checks the closed checkout and payment mandates before creating the
PayPal order, and answers with signed checkout and payment receipts (or a signed refusal).
"""
import datetime as dt
import logging

from . import a2a, config, mandates, paypal, schedule
from .llm import ask_json
from .store import store

log = logging.getLogger("procuro.sellers")

FALLBACK_UNIT_COST = 2.00  # used only when Channel3 has no market data for the item

SELLERS: dict[str, dict] = {
    "quickprint": {
        "name": "QuickPrint",
        "tagline": "Wholesale supplier with fast dispatch, flexible on price for volume.",
        "markup": 1.35, "floor": 1.08, "lead_days": 3, "max_qty": 5000,
        "style": "friendly and flexible; will concede for a firm order but always protects margin",
    },
    "boxcraft": {
        "name": "BoxCraft",
        "tagline": "Premium supplier with the fastest delivery.",
        "markup": 1.45, "floor": 1.22, "lead_days": 2, "max_qty": 2000,
        "style": "confident premium brand; concedes slowly and justifies price with speed and quality",
    },
    "packright": {
        "name": "PackRight",
        "tagline": "Lowest prices, standard shipping.",
        "markup": 1.20, "floor": 1.03, "lead_days": 10, "max_qty": 10000,
        "style": "budget seller; quick to discount but cannot ship any faster",
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
        signing={"alg": "ES256", "jwk": mandates.public_jwk(f"seller:{seller_id}")},
        role="merchant",
        skills=[
            {"id": "quote", "name": "Quote", "description": "Price a quantity of a product with a delivery date",
             "tags": ["commerce", "quote"]},
            {"id": "negotiate", "name": "Negotiate", "description": "Respond to counter-offers",
             "tags": ["commerce", "negotiation"]},
            {"id": "checkout", "name": "AP2 checkout with PayPal", "description":
                "Sign an AP2 checkout, verify checkout and payment mandates, hold with PayPal (intent "
                "AUTHORIZE) and return signed receipts; capture or void on a signed release",
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


WHOLESALE_SHARE = 0.70  # a reseller's cost is assumed to be ~70% of the lowest retail price


def _unit_cost(product: dict | None) -> float:
    """Cost basis for the exact Channel3 product being quoted (as sold: a 3-pack is one unit)."""
    if product and product.get("low"):
        return _r(float(product["low"]) * WHOLESALE_SHARE)
    return FALLBACK_UNIT_COST


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
        "close": _close,
        "reschedule": _reschedule,
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
        return {"action": "decline", "reason": "capacity"}, f"Sorry, we can't supply {qty} units right now."
    product = data.get("product")
    cost = _unit_cost(product)
    list_price = _r(cost * s["markup"])
    floor = _r(cost * s["floor"])
    # Our own production schedule decides the earliest delivery we can promise.
    start = schedule.next_start(seller_id, s["lead_days"], exclude=order_id)
    delivery = (start + dt.timedelta(days=s["lead_days"])).isoformat()
    busy = schedule.busy_reason(seller_id, start)
    session = {"cost": cost, "floor": floor, "list_price": list_price, "current": list_price, "rounds": 0,
               "item": item, "quantity": qty, "currency": currency, "delivery_date": delivery,
               "start": start.isoformat(),
               "retail_low": product.get("low") if product else None,
               "product_id": product.get("id") if product else None,
               "image": product.get("image") if product else None}
    _save(seller_id, order_id, session)

    reply = await ask_json(
        system=f"You are {s['name']}, an AI sales agent. Style: {s['style']}. Write like a real merchant.",
        prompt=(f"A buyer agent wants {qty} x '{item}' by {data.get('needed_by')}"
                + (f" (lowest retail price {product['low']:.2f} {currency} each)" if product else "") + ". "
                f"Requirements: {data.get('requirements') or 'none'}. Your quote: {list_price:.2f} {currency}/unit, "
                f"delivery {delivery}."
                + (f" Delivery is later than usual because {busy}; say so briefly." if busy else "")
                + " Write a one- or two-sentence quote message. JSON: {\"message\": str}"),
        fallback=lambda: {"message": f"We can do {qty} at {list_price:.2f} {currency} each, delivered by {delivery}."
                          + (f" (Later than usual: {busy}.)" if busy else "")},
        max_tokens=200,
    )
    offer = {"action": "offer", "seller_id": seller_id, "unit_price": list_price, "quantity": qty,
             "total": _r(list_price * qty), "currency": currency, "delivery_date": delivery}
    if busy:
        offer["busy_until"] = start.isoformat()
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
            return {"decision": "accept", "unit_price": current,
                    "message": "We can hold that price for you if you choose us."}
        if proposed >= floor:
            return {"decision": "accept", "unit_price": proposed,
                    "message": "That works for us. We'll hold that price if you choose us."}
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
                "The buyer is comparing several sellers and sends a signed cart only to the one it picks, so "
                "accepting means you hold this price if chosen. Don't say the order is confirmed or that you "
                "will confirm it.\n"
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


async def _close(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    """The buyer went with someone else (or with no one). Drop the quote and sign off politely."""
    s = SELLERS[seller_id]
    ses = _load(seller_id, order_id)
    if ses:
        ses["closed"] = True
        _save(seller_id, order_id, ses)
    reply = await ask_json(
        system=f"You are {s['name']}, an AI sales agent. Style: {s['style']}. Write like a real merchant.",
        prompt=(f"The buyer's agent says: \"{data.get('message', '')}\" The negotiation is over and you were not "
                "chosen. Reply in one short, gracious sentence. JSON: {\"message\": str}"),
        fallback=lambda: {"message": "Understood. Thanks for considering us, and we're here next time."},
        max_tokens=120,
    )
    return {"action": "closed", "seller_id": seller_id}, reply.get("message", "")


async def _reschedule(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    """The buyer asks for a different delivery date. Our production schedule decides, not the model:
    an earlier date needs a free slot for our whole lead time. If it fits we sign a change to the cart
    (new date, same price); otherwise we say the nearest date we can do."""
    s = SELLERS[seller_id]
    ses = _load(seller_id, order_id)
    if not ses or not (ses.get("checkout_hash") or ses.get("cart_mandate_id")):
        raise a2a.A2AError("no agreed checkout for this order")
    if ses.get("capture_id") or ses.get("voided"):
        raise a2a.A2AError("this order is already closed")
    if ses.get("checkout_hash") and data.get("checkout_hash") != ses["checkout_hash"]:
        raise a2a.A2AError("that isn't the checkout we signed")
    try:
        wanted = dt.date.fromisoformat(str(data["delivery_date"])[:10])
    except (KeyError, ValueError):
        raise a2a.A2AError("delivery_date must be YYYY-MM-DD") from None
    old = ses["delivery_date"]
    if wanted.isoformat() == old:
        return {"action": "rescheduled", "delivery_date": old, "unchanged": True}, "That's already the date."

    lead = s["lead_days"]
    ok, date = schedule.slot_for(seller_id, lead, wanted, order_id)
    item = f"{ses['quantity']} x {ses['item']}"
    if not ok:
        earlier = wanted.isoformat() < old
        why = (f"we need {lead} days to make and ship it" if date <= dt.date.today() + dt.timedelta(days=lead)
               else schedule.busy_reason(seller_id, date - dt.timedelta(days=lead)) or "our production slots are full")
        reply = await ask_json(
            system=f"You are {s['name']}, an AI sales agent. Style: {s['style']}. Write like a real merchant.",
            prompt=(f"The buyer asked to move delivery of {item} from {old} to {wanted}. You can't: {why}. "
                    f"The nearest date you can do is {date}. Decline in one or two short, helpful sentences and offer "
                    "that date. JSON: {\"message\": str}"),
            fallback=lambda: {"message": f"Sorry, we can't deliver by {wanted}: {why}. The {'earliest' if earlier else 'nearest'} "
                                         f"we can do is {date}."},
            max_tokens=150,
        )
        return {"action": "reschedule_declined", "earliest": date.isoformat(), "reason": why}, reply.get("message", "")

    start = max(dt.date.today(), wanted - dt.timedelta(days=lead))
    amendment = mandates.amendment(seller_id, {
        "iss": agent_url(seller_id),
        "order_id": order_id,
        "checkout_hash": ses.get("checkout_hash"),
        "merchant": s["name"],
        "change": "delivery_date",
        "previous_delivery_date": old,
        "delivery_date": wanted.isoformat(),
        "total": ses.get("total"),
        "currency": ses["currency"],
        "price_unchanged": True,
    })
    ses["delivery_date"] = wanted.isoformat()
    ses.setdefault("amendments", []).append(mandates.sha256_b64url(amendment))
    _save(seller_id, order_id, ses)
    schedule.move(seller_id, order_id, start=start.isoformat(), delivery_date=wanted.isoformat())
    reply = await ask_json(
        system=f"You are {s['name']}, an AI sales agent. Style: {s['style']}. Write like a real merchant.",
        prompt=(f"The buyer asked to move delivery of {item} from {old} to {wanted}, and your schedule has room. "
                "Confirm in one short sentence: same order, same price. JSON: {\"message\": str}"),
        fallback=lambda: {"message": f"Done: we'll deliver on {wanted} instead of {old}. Same order, same price."},
        max_tokens=120,
    )
    return {"action": "rescheduled", "delivery_date": wanted.isoformat(), "previous": old,
            "amendment": amendment}, reply.get("message", "")


# ---------- AP2 checkout ----------

def _me(seller_id: str):
    return mandates.merchant(seller_id, SELLERS[seller_id]["name"], agent_url(seller_id))


async def _finalize(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    """Sign the agreed deal as an AP2 Checkout JWT, after checking it fits the owner's open checkout mandate."""
    ses = _load(seller_id, order_id)
    if not ses:
        raise a2a.A2AError("no active quote for this order")
    price = float(data["unit_price"])
    if price < ses["current"] - 0.005:
        raise a2a.A2AError("price does not match our last offer")
    try:
        open_checkout = mandates.verify_open_checkout(data[mandates.CHECKOUT_MANDATE_KEY])
    except Exception as exc:  # noqa: BLE001
        raise a2a.A2AError(f"missing or invalid open checkout mandate: {exc}") from None
    total = _r(price * ses["quantity"])
    token, checkout_hash, payload = mandates.checkout_jwt(
        seller_id=seller_id, seller=_me(seller_id), order_id=order_id, item=ses["item"],
        product_id=ses.get("product_id"), quantity=ses["quantity"], unit_price=price, currency=ses["currency"],
        extra={"order_id": order_id, "delivery_date": ses["delivery_date"], "image": ses.get("image"),
               "payment_terms": "PayPal authorization at approval; capture on confirmed delivery"})
    problems = mandates.checkout_violations(open_checkout, payload)
    if problems:
        raise a2a.A2AError("this checkout doesn't fit the buyer's mandate: " + "; ".join(problems))
    ses.update({"agreed_price": price, "total": total, "checkout_hash": checkout_hash, "checkout_jwt": token})
    _save(seller_id, order_id, ses)
    lead = SELLERS[seller_id]["lead_days"]
    schedule.reserve(seller_id, order_id, item=ses["item"], quantity=ses["quantity"], total=total,
                     currency=ses["currency"], delivery_date=ses["delivery_date"],
                     start=ses.get("start") or (dt.date.fromisoformat(ses["delivery_date"])
                                                 - dt.timedelta(days=lead)).isoformat())
    return {"action": "checkout", mandates.CHECKOUT_KEY: {
        "checkout_jwt": token, "checkout_hash": checkout_hash, "total": total, "currency": ses["currency"],
        "delivery_date": ses["delivery_date"]}}, "Here's our signed checkout. Price and delivery are locked in."


def _refuse(seller_id: str, chain: str | None, err: "mandates.MandateRejected", payment: bool) -> dict:
    receipt = mandates.error_receipt(seller_id, agent_url(seller_id), chain, err.code, err.description, payment)
    key = mandates.PAYMENT_RECEIPT_KEY if payment else mandates.CHECKOUT_RECEIPT_KEY
    return {"action": "rejected", "error": err.code, "error_description": err.description, key: receipt}


def _receipts(seller_id: str, ses: dict, auth: dict) -> dict:
    """Signed AP2 receipts once PayPal holds the money: they point at the exact closed mandates."""
    url = agent_url(seller_id)
    if not ses.get("checkout_chain"):
        return {}  # an order from before AP2 receipts
    return {
        mandates.CHECKOUT_RECEIPT_KEY: mandates.checkout_receipt(seller_id, url, ses["checkout_chain"],
                                                                 ses["paypal_order_id"]),
        mandates.PAYMENT_RECEIPT_KEY: mandates.payment_receipt(
            seller_id, url, ses["payment_chain"], payment_id=auth["authorization_id"],
            psp_id=ses["paypal_order_id"], network_id=auth["authorization_id"]),
    }


async def _create_payment(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    """AP2 checks, in code: the closed checkout mandate approves OUR checkout, the payment mandate pays it,
    the amount and payee are right, and (when the agent signed on its own) every limit in the owner's open
    mandates holds. Only then is a PayPal order created."""
    ses = _load(seller_id, order_id)
    if not ses.get("checkout_hash"):
        raise a2a.A2AError("no signed checkout for this order")
    checkout_chain = data.get(mandates.CHECKOUT_MANDATE_KEY)
    payment_chain = data.get(mandates.PAYMENT_MANDATE_KEY)
    presentation = data.get(mandates.PRESENTATION_KEY) or {}
    try:
        mandates.verify_checkout_mandate(checkout_chain or "", presentation,
                                         expected_checkout_hash=ses["checkout_hash"])
    except mandates.MandateRejected as err:
        return _refuse(seller_id, checkout_chain, err, payment=False), f"We can't accept that checkout mandate: {err}"
    try:
        closed = mandates.verify_payment_mandate(
            payment_chain or "", presentation, expected_checkout_hash=ses["checkout_hash"],
            open_checkout_chain=checkout_chain if presentation.get("mode") == mandates.HUMAN_NOT_PRESENT else None)
        if closed.payee.id != seller_id:
            raise mandates.MandateRejected("payment_mismatch", "the payment mandate pays a different merchant")
        if closed.payment_amount.amount != mandates.cents(ses["total"]) or closed.payment_amount.currency != ses["currency"]:
            raise mandates.MandateRejected("payment_mismatch", "the payment amount isn't the checkout total")
        saved = closed.payment_instrument.id == mandates.PAYPAL_SAVED.id
        vault_id = data.get("vault_id")
        if saved and not (vault_id and paypal.can_use_saved_paypal(seller_id)):
            # A saved PayPal only works for the PayPal app that saved it.
            raise mandates.MandateRejected("payment_instrument_unavailable",
                                           "we can't charge a saved PayPal from our own PayPal account")
    except mandates.MandateRejected as err:
        return _refuse(seller_id, payment_chain, err, payment=True), f"We can't accept that payment: {err.description}"

    ses.update({"checkout_chain": checkout_chain, "payment_chain": payment_chain, "presentation": presentation})
    line = mandates.jwt_payload(ses["checkout_jwt"])["line_items"][0]
    order = await paypal.client_for(seller_id).create_order(
        reference_id=order_id,
        description=f"{line['quantity']} x {line['item']['title']}",
        items=[{"name": line["item"]["title"], "quantity": line["quantity"],
                "unit_price": line["item"]["price"] / 100, "product_id": ses.get("product_id")}],
        total=ses["total"],
        currency=ses["currency"],
        return_url=data.get("return_url", ""),
        cancel_url=data.get("cancel_url", ""),
        brand_name=SELLERS[seller_id]["name"],
        vault_id=vault_id if saved else None,
    )
    ses["paypal_order_id"] = order["id"]
    if order.get("authorization"):
        # Paid with the saved PayPal: PayPal authorized it in the same call, so the money is held now.
        auth = order["authorization"]
        ses.update({"authorization_id": auth["authorization_id"], "authorized_amount": auth["amount"],
                    "authorized_at": dt.datetime.now(dt.timezone.utc).isoformat(), "saved_paypal": True})
        _save(seller_id, order_id, ses)
        schedule.mark(seller_id, order_id, "confirmed")
        return {"action": "authorized", "paypal_order_id": order["id"], "saved_paypal": True, **auth,
                **_receipts(seller_id, ses, auth)}, \
            f"Funds of {auth['amount']:.2f} {ses['currency']} are held with your saved PayPal. Starting production now."
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
    schedule.mark(seller_id, order_id, "confirmed")
    return {"action": "authorized", **auth, **_receipts(seller_id, ses, auth)}, \
        f"Funds of {auth['amount']:.2f} {ses['currency']} are held. Starting production now."


def _release_instruction(ses: dict, data: dict) -> dict:
    """Never move money on an unsigned instruction: the buying agent's signed release (a Procuro extension
    to AP2) must check out with its published key and name this exact hold."""
    try:
        release = mandates.verify_extension(data.get("release") or "", mandates.jwk_from(mandates.public_jwk(
            mandates.BUYER)), mandates.RELEASE_TYP)
    except Exception:  # noqa: BLE001
        raise a2a.A2AError("missing or invalid signed release instruction") from None
    if release.get("authorization_id") != ses.get("authorization_id"):
        raise a2a.A2AError("the release names a different PayPal authorization")
    return release


async def _release(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(seller_id, order_id)
    release = _release_instruction(ses, data)
    amount = float(release["capture_amount"])
    if amount > float(ses.get("authorized_amount", 0)) + 0.005:
        raise a2a.A2AError("capture exceeds the authorized amount")
    cap = await paypal.client_for(seller_id).capture(
        ses["authorization_id"], amount, ses["currency"], final=True, note=release.get("note", ""))
    ses.update({"captured": amount, "capture_id": cap["capture_id"]})
    _save(seller_id, order_id, ses)
    schedule.mark(seller_id, order_id, "delivered", captured=amount,
                  settled=amount < float(ses["authorized_amount"]) - 0.005)
    released = _r(float(ses["authorized_amount"]) - amount)
    return {"action": "captured", "captured": amount, "released": released, **cap}, \
        f"Payment of {amount:.2f} {ses['currency']} received. Thank you!"


async def _void(seller_id: str, order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(seller_id, order_id)
    release = _release_instruction(ses, data)
    await paypal.client_for(seller_id).void(ses["authorization_id"])
    ses["voided"] = True
    _save(seller_id, order_id, ses)
    if release.get("capture_amount") is not None:
        # Delivered, but the agreed settlement was nothing: the booking ends delivered and unpaid.
        schedule.mark(seller_id, order_id, "delivered", captured=0.0, settled=True)
    else:
        schedule.mark(seller_id, order_id, "cancelled")
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