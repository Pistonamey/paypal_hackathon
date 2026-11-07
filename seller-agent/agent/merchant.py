"""NorthBox's business: how it quotes, haggles, signs AP2 checkouts and gets paid through its own PayPal.

Private to this agent (the buyer never sees it): its cost basis, its floor price and its rules.
Code, not a model, decides every number and every check:
  - never sell below the floor, never raise a price already offered
  - only sign a checkout that fits the owner's AP2 open checkout mandate
  - only take payment on AP2 checkout and payment mandates that verify against the keys the buying
    agent publishes, for exactly our checkout, our merchant id and our total
  - never capture more than PayPal holds, and only on the buying agent's signed release

The actions and their data are the same ones Procuro's own seller agents speak, so the buying agent
can't tell this one apart except by its card and its URL.
"""
import datetime as dt

from . import config, mandates, paypal, trust
from .store import store

SELLER = {  # set per seller through environment variables (see config.py)
    "markup": config.MARKUP, "floor": config.FLOOR, "lead_days": config.LEAD_DAYS, "max_qty": config.MAX_QTY,
    "tagline": config.TAGLINE,
}
FALLBACK_UNIT_COST = 2.00
WHOLESALE_SHARE = 0.70


class Refused(Exception):
    """A request this agent won't act on. The buyer gets {"action": "error", "error": ...}."""


def _r(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def agent_url() -> str:
    return f"{config.PUBLIC_BASE_URL}/agents/{config.AGENT_ID}"


def me():
    return mandates.merchant(config.AGENT_ID, config.AGENT_NAME, agent_url())


def _load(order_id: str) -> dict:
    return store.get(order_id)


def _save(order_id: str, ses: dict) -> None:
    store.put(order_id, ses)


async def handle(data: dict, context_id: str | None) -> tuple[dict, str]:
    action = data.get("action")
    order_id = str(data.get("order_id") or context_id or "none")
    handler = {
        "request_quote": _quote, "counter_offer": _counter, "close": _close, "finalize": _finalize,
        "create_payment": _create_payment, "authorize": _authorize, "release": _release, "void": _void,
        "reauthorize": _reauthorize, "propose_remedy": _remedy, "reschedule": _reschedule,
        "payment_status": _payment_status,
    }.get(action)
    if not handler:
        raise Refused(f"unsupported action {action!r}")
    return await handler(order_id, data)


# ---------------------------------------------------------------- negotiation

async def _quote(order_id: str, data: dict) -> tuple[dict, str]:
    qty, item = int(data["quantity"]), str(data["item"])
    currency = data.get("currency") or config.DEFAULT_CURRENCY
    if qty > SELLER["max_qty"]:
        return {"action": "decline", "reason": "capacity"}, f"Sorry, {qty} is more than our line can make right now."
    product = data.get("product") or None
    cost = _r(float(product["low"]) * WHOLESALE_SHARE) if product and product.get("low") else FALLBACK_UNIT_COST
    list_price, floor = _r(cost * SELLER["markup"]), _r(cost * SELLER["floor"])
    delivery = (dt.date.today() + dt.timedelta(days=SELLER["lead_days"])).isoformat()
    _save(order_id, {"cost": cost, "floor": floor, "list_price": list_price, "current": list_price, "rounds": 0,
                     "item": item, "quantity": qty, "currency": currency, "delivery_date": delivery,
                     "product_id": (product or {}).get("id"), "image": (product or {}).get("image")})
    offer = {"action": "offer", "seller_id": config.AGENT_ID, "unit_price": list_price, "quantity": qty,
             "total": _r(list_price * qty), "currency": currency, "delivery_date": delivery}
    return offer, (f"{config.GREETING} {qty} at {list_price:.2f} "
                   f"{currency} each, delivered by {delivery}.")


async def _counter(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    if not ses:
        raise Refused("no active quote for this order")
    proposed = float(data["unit_price"])
    ses["rounds"] += 1
    floor, current, cur = ses["floor"], ses["current"], ses["currency"]
    if proposed >= current - 0.005:
        choice, price, msg = "accept", current, "We can hold that price for you if you choose us."
    elif proposed >= floor:
        choice, price, msg = "accept", _r(proposed), "That works for us. We'll hold it if you choose us."
    else:
        price = floor if ses["rounds"] >= 3 else _r(max(floor, (current + max(proposed, floor)) / 2))
        choice = "counter"
        msg = (f"That's below our cost. Our best is {price:.2f} {cur} per unit." if price == floor
               else f"We can't go that low. We can meet you at {price:.2f} {cur} per unit.")
    price = min(price, current)
    ses["current"] = price
    _save(order_id, ses)
    return {"action": "accept" if choice == "accept" else "offer", "seller_id": config.AGENT_ID, "unit_price": price,
            "quantity": ses["quantity"], "total": _r(price * ses["quantity"]), "currency": cur,
            "delivery_date": ses["delivery_date"], "final": ses["rounds"] >= 4}, msg


async def _close(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    if ses:
        ses["closed"] = True
        _save(order_id, ses)
    return {"action": "closed", "seller_id": config.AGENT_ID}, "No problem. Thanks for asking us, and good luck with the order."


# ---------------------------------------------------------------- AP2 checkout

async def _finalize(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    if not ses:
        raise Refused("no active quote for this order")
    price = float(data["unit_price"])
    if price < ses["current"] - 0.005:
        raise Refused("price does not match our last offer")
    keys = await trust.buyer_keys()
    try:
        open_checkout = mandates.verify_open_checkout(data[mandates.CHECKOUT_MANDATE_KEY],
                                                      mandates.jwk_from(keys["surface"]))
    except Exception as exc:  # noqa: BLE001
        raise Refused(f"missing or invalid open checkout mandate: {exc}") from None
    total = _r(price * ses["quantity"])
    token, checkout_hash, payload = mandates.checkout_jwt(
        seller_id=config.AGENT_ID, seller=me(), order_id=order_id, item=ses["item"], product_id=ses.get("product_id"),
        quantity=ses["quantity"], unit_price=price, currency=ses["currency"],
        extra={"order_id": order_id, "delivery_date": ses["delivery_date"], "image": ses.get("image"),
               "payment_terms": "PayPal authorization at approval; capture on confirmed delivery"})
    problems = mandates.checkout_violations(open_checkout, payload)
    if problems:
        raise Refused("this checkout doesn't fit the buyer's mandate: " + "; ".join(problems))
    ses.update({"agreed_price": price, "total": total, "checkout_hash": checkout_hash, "checkout_jwt": token})
    _save(order_id, ses)
    return {"action": "checkout", mandates.CHECKOUT_KEY: {
        "checkout_jwt": token, "checkout_hash": checkout_hash, "total": total, "currency": ses["currency"],
        "delivery_date": ses["delivery_date"]}}, "Here's our signed checkout. Price and date are locked in."


def _refuse(chain: str | None, err: "mandates.MandateRejected", payment: bool) -> dict:
    receipt = mandates.error_receipt(config.AGENT_ID, agent_url(), chain, err.code, err.description, payment)
    key = mandates.PAYMENT_RECEIPT_KEY if payment else mandates.CHECKOUT_RECEIPT_KEY
    return {"action": "rejected", "error": err.code, "error_description": err.description, key: receipt}


def _receipts(ses: dict, auth: dict) -> dict:
    return {
        mandates.CHECKOUT_RECEIPT_KEY: mandates.checkout_receipt(config.AGENT_ID, agent_url(), ses["checkout_chain"],
                                                                 ses["paypal_order_id"]),
        mandates.PAYMENT_RECEIPT_KEY: mandates.payment_receipt(
            config.AGENT_ID, agent_url(), ses["payment_chain"], payment_id=auth["authorization_id"],
            psp_id=ses["paypal_order_id"], network_id=auth["authorization_id"]),
    }


async def _create_payment(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    if not ses.get("checkout_hash"):
        raise Refused("no signed checkout for this order")
    surface = mandates.jwk_from((await trust.buyer_keys())["surface"])
    checkout_chain = data.get(mandates.CHECKOUT_MANDATE_KEY)
    payment_chain = data.get(mandates.PAYMENT_MANDATE_KEY)
    presentation = data.get(mandates.PRESENTATION_KEY) or {}
    if presentation.get("audience") and presentation["audience"] != agent_url():
        err = mandates.MandateRejected("wrong_audience", "these mandates were presented to a different merchant")
        return _refuse(payment_chain, err, payment=True), err.description
    try:
        mandates.verify_checkout_mandate(checkout_chain or "", presentation,
                                         expected_checkout_hash=ses["checkout_hash"], surface_jwk=surface)
    except mandates.MandateRejected as err:
        return _refuse(checkout_chain, err, payment=False), f"We can't accept that checkout mandate: {err}"
    try:
        closed = mandates.verify_payment_mandate(
            payment_chain or "", presentation, expected_checkout_hash=ses["checkout_hash"],
            open_checkout_chain=checkout_chain if presentation.get("mode") == mandates.HUMAN_NOT_PRESENT else None,
            surface_jwk=surface)
        if closed.payee.id != config.AGENT_ID:
            raise mandates.MandateRejected("payment_mismatch", "the payment mandate pays a different merchant")
        if closed.payment_amount.amount != mandates.cents(ses["total"]) or closed.payment_amount.currency != ses["currency"]:
            raise mandates.MandateRejected("payment_mismatch", "the payment amount isn't the checkout total")
        saved = closed.payment_instrument.id == mandates.PAYPAL_SAVED.id
        if saved and not (data.get("vault_id") and config.ACCEPT_SAVED_PAYPAL):
            raise mandates.MandateRejected(
                "payment_instrument_unavailable",
                "we're paid into our own PayPal account, so we can't charge a PayPal saved with another app")
    except mandates.MandateRejected as err:
        return _refuse(payment_chain, err, payment=True), f"We can't accept that payment: {err.description}"

    ses.update({"checkout_chain": checkout_chain, "payment_chain": payment_chain, "presentation": presentation})
    line = mandates.jwt_payload(ses["checkout_jwt"])["line_items"][0]
    order = await paypal.client().create_order(
        reference_id=order_id, description=f"{line['quantity']} x {line['item']['title']}",
        items=[{"name": line["item"]["title"], "quantity": line["quantity"], "unit_price": line["item"]["price"] / 100,
                "product_id": ses.get("product_id")}],
        total=ses["total"], currency=ses["currency"], return_url=data.get("return_url", ""),
        cancel_url=data.get("cancel_url", ""), brand_name=config.AGENT_NAME,
        vault_id=data.get("vault_id") if saved else None)
    ses["paypal_order_id"] = order["id"]
    if order.get("authorization"):
        auth = order["authorization"]
        ses.update({"authorization_id": auth["authorization_id"], "authorized_amount": auth["amount"]})
        _save(order_id, ses)
        return {"action": "authorized", "paypal_order_id": order["id"], "saved_paypal": True, **auth,
                **_receipts(ses, auth)}, f"{auth['amount']:.2f} {ses['currency']} is held. Starting production."
    _save(order_id, ses)
    return {"action": "payment_created", "paypal_order_id": order["id"], "approve_url": order["approve_url"]}, \
        "PayPal order created on our account. Please approve it in PayPal."


async def _authorize(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    if data.get("paypal_order_id") != ses.get("paypal_order_id"):
        raise Refused("unknown PayPal order")
    auth = await paypal.client().authorize_order(ses["paypal_order_id"])
    ses.update({"authorization_id": auth["authorization_id"], "authorized_amount": auth["amount"]})
    _save(order_id, ses)
    return {"action": "authorized", **auth, **_receipts(ses, auth)}, \
        f"{auth['amount']:.2f} {ses['currency']} is held. Starting production now."


async def _release_instruction(ses: dict, data: dict) -> dict:
    buyer = mandates.jwk_from((await trust.buyer_keys())["buyer"])
    try:
        release = mandates.verify_extension(data.get("release") or "", buyer, mandates.RELEASE_TYP)
    except Exception:  # noqa: BLE001
        raise Refused("missing or invalid signed release instruction") from None
    if release.get("authorization_id") != ses.get("authorization_id"):
        raise Refused("the release names a different PayPal authorization")
    return release


async def _release(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    release = await _release_instruction(ses, data)
    amount = float(release["capture_amount"])
    if amount > float(ses.get("authorized_amount", 0)) + 0.005:
        raise Refused("capture exceeds the authorized amount")
    cap = await paypal.client().capture(ses["authorization_id"], amount, ses["currency"], final=True,
                                        note=release.get("note", ""))
    ses.update({"captured": amount, "capture_id": cap["capture_id"]})
    _save(order_id, ses)
    return {"action": "captured", "captured": amount, "released": _r(float(ses["authorized_amount"]) - amount), **cap}, \
        f"Payment of {amount:.2f} {ses['currency']} received in our PayPal. Thank you!"


async def _void(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    await _release_instruction(ses, data)
    await paypal.client().void(ses["authorization_id"])
    ses["voided"] = True
    _save(order_id, ses)
    return {"action": "voided"}, "Authorization voided. No charge was made."


async def _reauthorize(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    res = await paypal.client().reauthorize(ses["authorization_id"], float(ses["authorized_amount"]), ses["currency"])
    ses.update({"authorization_id": res["authorization_id"], "reauthorized": True})
    _save(order_id, ses)
    return {"action": "reauthorized", **res}, "Hold renewed."


async def _payment_status(order_id: str, data: dict) -> tuple[dict, str]:
    """PayPal's own record of the hold and capture on our account, for the buyer's receipt."""
    ses = _load(order_id)
    if not ses.get("authorization_id"):
        return {"action": "payment_status", "authorization": None, "capture": None}, "Nothing held yet."
    client = paypal.client()
    auth = await client.get_authorization(ses["authorization_id"])
    cap = await client.get_capture(ses["capture_id"]) if ses.get("capture_id") else None
    return {"action": "payment_status", "authorization": auth, "capture": cap}, "Here's what PayPal shows."


# ---------------------------------------------------------------- after delivery

async def _remedy(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    assessment, proposal = data["assessment"], data["proposal"]
    authorized = float(ses["authorized_amount"])
    usable = int(assessment.get("usable_units", 0))
    min_acceptable = _r(min(authorized, usable * ses["cost"] * 1.05))
    offered = float(proposal["capture_amount"])
    round_no = int(data.get("round", 1))
    if round_no == 1 and offered < _r(usable * ses["agreed_price"]) - 0.005:
        amount = min(_r(usable * ses["agreed_price"]), authorized)
        return {"action": "remedy_counter", "capture_amount": amount,
                "reprint_by": (dt.date.today() + dt.timedelta(days=5)).isoformat(), "seller_id": config.AGENT_ID}, \
            "Sorry about that. We'll charge only the usable boxes at the agreed price, or reprint the damaged ones."
    amount = max(offered, min_acceptable)
    if amount <= offered + 0.005:
        return {"action": "remedy_accept", "capture_amount": offered, "seller_id": config.AGENT_ID}, \
            "Fair enough, we accept."
    return {"action": "remedy_counter", "capture_amount": amount, "seller_id": config.AGENT_ID}, \
        f"We can accept {amount:.2f} {ses['currency']} for the usable boxes, but not less."


async def _reschedule(order_id: str, data: dict) -> tuple[dict, str]:
    ses = _load(order_id)
    if not ses.get("checkout_hash"):
        raise Refused("no agreed checkout for this order")
    if ses.get("capture_id") or ses.get("voided"):
        raise Refused("this order is already closed")
    if data.get("checkout_hash") != ses["checkout_hash"]:
        raise Refused("that isn't the checkout we signed")
    try:
        wanted = dt.date.fromisoformat(str(data["delivery_date"])[:10])
    except (KeyError, ValueError):
        raise Refused("delivery_date must be YYYY-MM-DD") from None
    old = ses["delivery_date"]
    earliest = dt.date.today() + dt.timedelta(days=SELLER["lead_days"])
    if wanted < earliest:
        return {"action": "reschedule_declined", "earliest": earliest.isoformat(),
                "reason": f"we need {SELLER['lead_days']} days to make and ship it"}, \
            f"Sorry, we need {SELLER['lead_days']} days to make these. The earliest we can do is {earliest}."
    amendment = mandates.amendment(config.AGENT_ID, {
        "iss": agent_url(), "order_id": order_id, "checkout_hash": ses["checkout_hash"], "merchant": config.AGENT_NAME,
        "change": "delivery_date", "previous_delivery_date": old, "delivery_date": wanted.isoformat(),
        "total": ses.get("total"), "currency": ses["currency"], "price_unchanged": True})
    ses["delivery_date"] = wanted.isoformat()
    _save(order_id, ses)
    return {"action": "rescheduled", "delivery_date": wanted.isoformat(), "previous": old, "amendment": amendment}, \
        f"Done: we'll deliver on {wanted} instead of {old}. Same order, same price."