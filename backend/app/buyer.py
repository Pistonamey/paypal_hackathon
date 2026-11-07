"""The buying agent: works for the user inside a signed mandate.

It turns plain language into a mandate, discovers seller agents over A2A,
checks real market prices on Channel3, negotiates, and pays through PayPal
only with the user's approval. Money is held until delivery is confirmed.

The owner's rules become AP2 v0.2 open checkout and payment mandates; each purchase is approved with
closed AP2 mandates for the seller's exact signed checkout (see mandates.py).
"""
import asyncio
import datetime as dt
import logging
import re
import time
import uuid

from . import a2a, channel3, config, invoices, mandates, sellers, wallet
from .llm import ask_json
from .store import store

log = logging.getLogger("procuro.buyer")

BUYER_ID = "buyer:mandate"
MAX_ROUNDS = 3
HONOR_PERIOD_DAYS = 3
MAX_REAUTH_DAYS = 29


def _r(x: float) -> float:
    return round(x + 1e-9, 2)


def _now() -> float:
    return time.time()


def card() -> dict:
    return a2a.agent_card(
        name="Procuro Buyer Agent",
        description="Buys on a user's behalf inside a signed mandate: budget, approval limit, deadline.",
        url=f"{config.PUBLIC_BASE_URL}/agents/buyer",
        signing={"alg": "ES256", "jwk": mandates.public_jwk(mandates.BUYER),
                 # The owner's Trusted Surface signs her open mandates (and her approvals).
                 "trustedSurfaceJwk": mandates.public_jwk(mandates.SURFACE)},
        role="shopper",
        skills=[{"id": "procure", "name": "Procure", "description": "Find, negotiate and pay for goods",
                 "tags": ["commerce", "negotiation", "paypal"]}],
    )


def say(order_id: str, sender: str, text: str, kind: str = "chat", to: str | None = None, data: dict | None = None):
    store.add_message(order_id, {"from": sender, "to": to, "kind": kind, "text": text, "data": data or {}})


def _save(order: dict) -> None:
    now = _now()
    order["updated_at"] = now
    # When each step first happened, for the timeline (Gantt) and the dashboard.
    order.setdefault("times", {}).setdefault(order["status"], now)
    store.save_order(order)


# ---------------------------------------------------------------- mandate

def _fallback_parse(text: str) -> dict:
    money = [float(m.replace(",", "")) for m in re.findall(r"[$₹€£]\s?([\d,]+(?:\.\d+)?)", text)]
    qty = re.search(r"(?<![\d,.$₹€£])\b(\d{1,6})\s+([a-zA-Z][\w\s-]{2,60}?)(?=,|\.|\bwith\b|\bfor\b|\bby\b|\bdelivered\b|$)", text)
    deadline = re.search(r"\bby\s+([A-Z][a-z]{2,8}\.?\s+\d{1,2})", text)
    deadline_iso = None
    if deadline:
        today = dt.date.today()
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                d = dt.datetime.strptime(f"{deadline.group(1).replace('.', '')} {today.year}", fmt).date()
                if d < today:
                    d = d.replace(year=today.year + 1)
                deadline_iso = d.isoformat()
                break
            except ValueError:
                continue
    amount = r"[$₹€£]\s?([\d,]+(?:\.\d+)?)"
    b = re.search(amount + r"\s*budget", text, re.I) or re.search(r"budget\s*(?:of|is|:)?\s*" + amount, text, re.I)
    t = re.search(r"(?:over|above|more than)\s*" + amount, text, re.I)
    budget = float(b.group(1).replace(",", "")) if b else (max(money) if money else 0)
    threshold = float(t.group(1).replace(",", "")) if t else budget
    return {
        "item": qty.group(2).strip() if qty else text[:60],
        "quantity": int(qty.group(1)) if qty else 1,
        "budget_max": budget,
        "approval_threshold": threshold,
        "currency": "USD",
        "deadline": deadline_iso or (dt.date.today() + dt.timedelta(days=14)).isoformat(),
        "requirements": "",
    }


def valid_deadline(value: str | None) -> str | None:
    """A delivery date picked on the calendar: YYYY-MM-DD, from tomorrow to a year ahead."""
    if not value:
        return None
    try:
        d = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("The delivery date must look like 2026-11-09.") from exc
    today = dt.date.today()
    if d <= today:
        raise ValueError("Pick a delivery date after today.")
    if d > today + dt.timedelta(days=366):
        raise ValueError("Pick a delivery date within the next year.")
    return d.isoformat()


async def parse_mandate(text: str, deadline: str | None = None) -> dict:
    today = dt.date.today().isoformat()
    if deadline:
        text = f"{text}\n(The owner picked the delivery deadline on the calendar: {deadline}.)"
    m = await ask_json(
        system="You turn a shopper's request into a strict purchasing mandate for an AI buying agent.",
        prompt=(f"Today is {today}. Request: \"{text}\"\n"
                "Extract: item (short product search phrase), quantity (int), budget_max (total, number), "
                "approval_threshold (total above which the user must approve; if not stated use budget_max), "
                "currency (ISO, default USD), deadline (YYYY-MM-DD delivery deadline; if missing, 14 days from "
                "today), requirements (e.g. custom logo, color; empty string if none).\n"
                'JSON: {"item": str, "quantity": int, "budget_max": number, "approval_threshold": number, '
                '"currency": str, "deadline": str, "requirements": str}'),
        fallback=lambda: _fallback_parse(text),
        max_tokens=300,
    )
    m["quantity"] = max(1, int(m.get("quantity") or 1))
    m["budget_max"] = float(m.get("budget_max") or 0)
    m["approval_threshold"] = float(m.get("approval_threshold") or m["budget_max"])
    if m["budget_max"] > 0:
        # Asking before paying "over X" only makes sense when X is within the budget.
        m["approval_threshold"] = min(m["approval_threshold"], m["budget_max"])
    m["currency"] = (m.get("currency") or config.DEFAULT_CURRENCY).upper()
    if deadline:
        m["deadline"] = deadline  # the date picked on the calendar always wins over anything in the text
    m["deadline_source"] = "calendar" if deadline else "text"
    return m


def _seller_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _merchants() -> list:
    """Every seller agent the buying agent may deal with: the owner's mandate names them."""
    out = []
    for url in _seller_urls():
        sid = _seller_id(url)
        out.append(mandates.merchant(sid, sellers.SELLERS.get(sid, {}).get("name") or sid, url))
    return out


def _sign_intent(order: dict, product: dict | None = None) -> dict:
    """The owner confirmed her rules: Procuro's screen (AP2's Trusted Surface) signs the open mandates."""
    m = order["mandate"]
    deadline = dt.datetime.combine(dt.date.fromisoformat(m["deadline"]), dt.time(23, 59), dt.timezone.utc)
    merchants = _merchants()
    signed = mandates.open_mandates(
        item=m["item"], product_id=(product or {}).get("id"), quantity=m["quantity"], merchants=merchants,
        approval_limit=m["approval_threshold"], currency=m["currency"], expires_at=int(deadline.timestamp()))
    return {**signed, "merchants": [x.model_dump() for x in merchants], "signed_at": int(_now())}


async def create_order(text: str, deadline: str | None = None) -> dict:
    mandate = await parse_mandate(text, valid_deadline(deadline))
    if mandate["budget_max"] <= 0:
        raise ValueError("Please include a budget, for example: 'Budget $600'.")
    order_id = f"ord_{uuid.uuid4().hex[:10]}"
    order = {
        "id": order_id, "created_at": _now(), "status": "negotiating", "request_text": text,
        "mandate": mandate, "ap2": None, "benchmark": None, "product": None, "candidates": [],
        "sellers": {}, "winner": None, "paypal": {}, "events": [],
    }
    candidates = await channel3.find_products(text, mandate["item"], mandate["quantity"], mandate["budget_max"],
                                              mandate["currency"])
    if candidates:
        # The owner picks the exact product; the mandate is signed once that's settled.
        order.update({"status": "choosing_product", "candidates": candidates})
        _save(order)
        rec = next(p for p in candidates if p.get("recommended"))
        say(order_id, "buyer", f"Found {len(candidates)} real products on Channel3 for \"{mandate['item']}\". "
            f"I'd go with {rec['title']} (from {rec['low']:.2f} {mandate['currency']}). Pick the one you want and "
            "how many, and I'll negotiate for exactly that.", kind="system",
            data={"candidates": [p["id"] for p in candidates]})
        return order
    order["ap2"] = _sign_intent(order)
    _save(order)
    _say_mandate(order)
    return order


def _say_mandate(order: dict) -> None:
    m = order["mandate"]
    say(order["id"], "buyer", f"Mandate signed: {m['quantity']} x {m['item']}, budget "
        f"{m['budget_max']:.2f} {m['currency']}, ask before paying over "
        f"{m['approval_threshold']:.2f}, delivered by {m['deadline']}. Signed as AP2 open checkout and "
        "payment mandates.", kind="system",
        data={"open_checkout_hash": order["ap2"]["open_checkout_hash"]})


def choose_product(order_id: str, product_id: str, quantity: int) -> dict:
    order = store.get_order(order_id)
    if not order or order["status"] != "choosing_product":
        raise ValueError("this order isn't waiting for a product choice")
    product = next((p for p in order.get("candidates") or [] if p["id"] == product_id), None)
    if not product:
        raise ValueError("that product isn't one of the options for this order")
    if not 1 <= int(quantity) <= 100000:
        raise ValueError("quantity must be between 1 and 100,000")
    m = order["mandate"]
    m.update({"item": product["title"], "quantity": int(quantity)})
    order.update({"product": product, "benchmark": channel3.benchmark_for(product), "status": "negotiating"})
    order["ap2"] = _sign_intent(order, product)
    _save(order)
    retail_total = product["low"] * m["quantity"]
    note = (f" At the lowest retail price that's {retail_total:.2f} {m['currency']}, above your budget, so I'll "
            "need to negotiate hard." if retail_total > m["budget_max"] else "")
    say(order_id, "buyer", f"You picked {m['quantity']} x {product['title']}. {product['retailers']} retailer(s) "
        f"on Channel3 sell this exact item for {product['low']:.2f}–{product['high']:.2f} {m['currency']}.{note}",
        kind="system", data={"product_id": product["id"], "benchmark": order["benchmark"]})
    _say_mandate(order)
    return order


# ---------------------------------------------------------------- negotiation

def _seller_urls() -> list[str]:
    if config.SELLER_AGENTS_ONLY and config.SELLER_AGENT_URLS:
        return list(config.SELLER_AGENT_URLS)
    built_in = [sellers.agent_url(s) for s in sellers.SELLERS]
    return built_in + [u for u in config.SELLER_AGENT_URLS if u not in built_in]


def card_url(card: dict, fallback: str) -> str:
    """Where to send A2A messages: `url` (A2A v0.3 cards) or the JSON-RPC interface (A2A v1.0 cards)."""
    if card.get("url"):
        return card["url"]
    for iface in card.get("supportedInterfaces") or []:
        if iface.get("protocolBinding", "JSONRPC").upper() == "JSONRPC" and iface.get("url"):
            return iface["url"]
    return fallback


def card_jwk(card: dict) -> dict | None:
    """The agent's public signing key: our cards put it in `signing`; A2A v1.0 cards (which can't carry
    custom fields) put it in the AP2 extension's params."""
    if (card.get("signing") or {}).get("jwk"):
        return card["signing"]["jwk"]
    for ext in (card.get("capabilities") or {}).get("extensions") or []:
        if ext.get("uri") == mandates.EXTENSION_URI and ((ext.get("params") or {}).get("signing") or {}).get("jwk"):
            return ext["params"]["signing"]["jwk"]
    return None


def is_outside(seller: dict) -> bool:
    """A seller agent that isn't one of the three hosted by this app."""
    return not str(seller.get("url", "")).startswith(f"{config.PUBLIC_BASE_URL}/agents/")


async def _buyer_move(order: dict, sid: str, offer: dict, competing: dict | None, round_no: int) -> dict:
    m, bench = order["mandate"], order["benchmark"]
    qty, cur = m["quantity"], m["currency"]
    budget_unit = m["budget_max"] / qty

    def fallback() -> dict:
        target = offer["unit_price"] * 0.88
        if bench:
            target = min(target, bench["median"])
        if competing:
            target = min(target, competing["unit_price"] * 0.97)
        target = _r(max(target, offer["unit_price"] * 0.75))
        if offer["unit_price"] <= target + 0.01 or round_no >= MAX_ROUNDS:
            if offer["total"] <= m["budget_max"]:
                return {"decision": "accept", "unit_price": offer["unit_price"], "message": "Agreed."}
            return {"decision": "walk", "unit_price": offer["unit_price"],
                    "message": "That's more than I'm able to spend on this order, so I'll pass. Thanks for "
                               "your time."}
        parts = [f"We're looking for a firm order of {qty}."]
        if bench:
            parts.append(f"Retailers sell this exact item for {bench['low']:.2f}–{bench['high']:.2f} {cur}.")
        if competing:
            parts.append(f"Another seller is at {competing['unit_price']:.2f} delivered {competing['delivery_date']}.")
        parts.append(f"Can you do {target:.2f} per unit?")
        return {"decision": "counter", "unit_price": target, "message": " ".join(parts)}

    market = (f"Channel3: {bench['retailers']} retailer(s) sell this exact product for {bench['low']}-{bench['high']} "
              f"{cur} each (median {bench['median']})." if bench else "No market data available.")
    comp = (f"Best competing offer: {competing['unit_price']:.2f}/unit from another seller, delivery "
            f"{competing['delivery_date']}." if competing else "No competing offers.")
    move = await ask_json(
        system=("You are a sharp, honest AI buying agent negotiating for a small business owner. Get the best "
                "price without lying. You may cite real market data and real competing offers, never invented "
                "ones. Be brief and polite."),
        prompt=(f"PRIVATE (never reveal): total budget {m['budget_max']:.2f} {cur} "
                f"({budget_unit:.2f}/unit max). Deadline {m['deadline']}.\n"
                f"Buying {qty} x {m['item']}. Requirements: {m.get('requirements') or 'none'}.\n"
                f"{market}\n{comp}\n"
                f"Seller {order['sellers'][sid]['name']} currently offers {offer['unit_price']:.2f}/unit "
                f"(total {offer['total']:.2f}), delivery {offer['delivery_date']}. Round {round_no} of {MAX_ROUNDS}.\n"
                "HOW THE WINNER IS CHOSEN: the lowest total price among offers that arrive by the deadline. "
                "Arriving earlier than the deadline is worth nothing extra, so never offer to pay more for it. "
                "A counter-offer only helps if it is below the best competing on-time offer.\n"
                "Decide: accept, counter (propose a lower per-unit price with a short persuasive message), or walk.\n"
                'JSON: {"decision": "accept"|"counter"|"walk", "unit_price": number, "message": str}'),
        fallback=fallback,
        max_tokens=300,
    )
    move["unit_price"] = float(move.get("unit_price") or offer["unit_price"])
    if move.get("decision") == "accept" and offer["total"] > m["budget_max"] + 0.005:
        move = {"decision": "counter", "unit_price": _r(budget_unit * 0.97),
                "message": move.get("message") or "That's above what I can approve. Can you meet me here?"}
    if move.get("decision") == "counter" and competing and move["unit_price"] >= competing["unit_price"] - 0.005:
        # Guard: a counter at or above a rival's price can't win, so ask this seller to beat the rival.
        target = _r(competing["unit_price"] * 0.97)
        move["unit_price"] = target
        move["message"] = (f"I have an offer at {competing['unit_price']:.2f} per unit that arrives in time. "
                           f"To win the order you'd need to beat it. Can you do {target:.2f}?")
    if move.get("decision") == "counter" and move["unit_price"] >= offer["unit_price"]:
        move["decision"] = "accept"
    return move


def _best(active: dict[str, dict], exclude: str | None = None) -> dict | None:
    pool = [o for s, o in active.items() if s != exclude]
    return min(pool, key=lambda o: o["unit_price"]) if pool else None


async def negotiate(order_id: str) -> None:
    order = store.get_order(order_id)
    m = order["mandate"]
    try:
        # Discover seller agents through their published A2A agent cards.
        cards: dict[str, dict] = {}
        for url in _seller_urls():
            try:
                c = await a2a.fetch_card(url)
                sid = _seller_id(url)
                cards[sid] = c
                order["sellers"][sid] = {"name": c["name"], "url": card_url(c, url), "status": "contacted",
                                         "offers": [], "jwk": card_jwk(c)}
                if is_outside(order["sellers"][sid]):
                    order["sellers"][sid]["outside"] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("agent card fetch failed for %s: %s", url, exc)
        _save(order)
        say(order_id, "buyer", f"Found {len(cards)} seller agents: {', '.join(c['name'] for c in cards.values())}. "
            "Requesting quotes.", kind="system")

        product = order.get("product")
        request = {"action": "request_quote", "order_id": order_id, "item": m["item"], "quantity": m["quantity"],
                   "currency": m["currency"], "needed_by": m["deadline"], "requirements": m.get("requirements", ""),
                   # The exact Channel3 product every seller quotes on, with its real retail price.
                   "product": {k: product[k] for k in ("id", "title", "brand", "image", "low", "median", "retailers")}
                   if product else None}

        async def quote(sid: str):
            return sid, await a2a.send(order["sellers"][sid]["url"], request,
                                       f"Requesting a quote for {m['quantity']} x {m['item']}.", order_id)

        say(order_id, "buyer", f"Quote request: {m['quantity']} x {m['item']}, needed by {m['deadline']}.",
            to="all", data=request)
        results = await asyncio.gather(*(quote(s) for s in cards), return_exceptions=True)

        active: dict[str, dict] = {}
        for res in results:
            if isinstance(res, Exception):
                log.warning("quote failed: %s", res)
                continue
            sid, (offer, text) = res
            say(order_id, sid, text or "Quote attached.", to="buyer", data=offer)
            entry = order["sellers"][sid]
            if offer.get("action") != "offer":
                entry["status"] = "declined"
                continue
            entry["offers"].append(offer)
            entry["opening_total"] = offer["total"]
            if offer["delivery_date"] > m["deadline"]:
                entry["status"] = "dropped_deadline"
                why = (f" Their production is booked until {offer['busy_until']}."
                       if offer.get("busy_until") else "")
                say(order_id, "buyer", f"{entry['name']} delivers {offer['delivery_date']}, after the "
                    f"{m['deadline']} deadline.{why} Dropping this offer.", kind="decision",
                    data={"seller_id": sid})
                continue
            entry["status"] = "negotiating"
            active[sid] = offer
        _save(order)

        agreed: dict[str, dict] = {}
        for round_no in range(1, MAX_ROUNDS + 1):
            open_sellers = [s for s in active if s not in agreed]
            if not open_sellers:
                break
            for sid in sorted(open_sellers, key=lambda s: -active[s]["unit_price"]):
                offer = active[sid]
                move = await _buyer_move(order, sid, offer, _best(active, exclude=sid), round_no)
                if move["decision"] == "walk":
                    order["sellers"][sid]["status"] = "walked_away"
                    say(order_id, "buyer", move.get("message") or "We'll pass, thanks.", to=sid)
                    active.pop(sid)
                    continue
                if move["decision"] == "accept":
                    agreed[sid] = offer
                    order["sellers"][sid]["status"] = "agreed"
                    say(order_id, "buyer", move.get("message") or "Agreed, pending final selection.", to=sid)
                    continue
                say(order_id, "buyer", move["message"], to=sid,
                    data={"action": "counter_offer", "unit_price": move["unit_price"]})
                reply, text = await a2a.send(order["sellers"][sid]["url"], {
                    "action": "counter_offer", "order_id": order_id, "unit_price": move["unit_price"],
                    "message": move["message"]}, move["message"], order_id)
                say(order_id, sid, text or "", to="buyer", data=reply)
                order["sellers"][sid]["offers"].append(reply)
                if reply["action"] == "reject":
                    order["sellers"][sid]["status"] = "rejected"
                    active.pop(sid)
                    continue
                active[sid] = reply
                if reply["action"] == "accept" or reply.get("final"):
                    agreed[sid] = reply
                    order["sellers"][sid]["status"] = "agreed"
            _save(order)
        # Sellers still open after the last round stand at their latest offer.
        for sid, offer in active.items():
            agreed.setdefault(sid, offer)

        await _select(order, agreed)
    except Exception as exc:  # noqa: BLE001
        log.exception("negotiation failed")
        order = store.get_order(order_id) or order
        order["status"] = "failed"
        order["error"] = str(exc)
        _save(order)
        say(order_id, "system", f"Negotiation stopped: {exc}", kind="error")


def _reliability(sid: str) -> int:
    stats = store.get_doc("sellers", sid)
    return int(stats.get("score", 100))


def _no_deal_message(order: dict) -> str:
    """Explain what would have worked, with the real numbers."""
    m, cur = order["mandate"], order["mandate"]["currency"]
    on_time = [e["offers"][-1] for e in order["sellers"].values()
               if e.get("offers") and e["offers"][-1].get("total") and e["status"] != "dropped_deadline"]
    late = [e["offers"][-1] for e in order["sellers"].values()
            if e.get("offers") and e["status"] == "dropped_deadline"]
    parts = ["No deal, and no money moved."]
    if on_time:
        best = min(on_time, key=lambda o: o["total"])
        parts.append(f"The best offer that arrives in time was {best['total']:.2f} {cur} "
                     f"({best['unit_price']:.2f} each), above your {m['budget_max']:.2f} budget.")
    if late:
        cheap = min(late, key=lambda o: o["total"])
        if cheap["total"] <= m["budget_max"]:
            parts.append(f"A seller can do it for {cheap['total']:.2f} if you can wait until "
                         f"{cheap['delivery_date']}.")
    bench = order.get("benchmark")
    if bench:
        parts.append(f"Retailers sell this item for about {bench['median']:.2f} {cur} each, so a budget near "
                     f"{bench['median'] * m['quantity']:.0f} {cur} is realistic.")
    elif on_time:
        parts.append("Raise the budget or move the deadline, and I'll try again.")
    return " ".join(parts)


async def _close_others(order: dict, keep: str | None, message: str) -> None:
    """Tell every seller still at the table that it wasn't picked, so no conversation is left hanging."""
    order_id = order["id"]
    open_ids = [s for s, e in order["sellers"].items()
                if s != keep and e.get("status") in ("requested", "negotiating", "agreed")]

    async def close(sid: str) -> None:
        say(order_id, "buyer", message, to=sid, data={"action": "close"})
        try:
            _, text = await a2a.send(order["sellers"][sid]["url"], {
                "action": "close", "order_id": order_id, "message": message}, message, order_id)
            if text:
                say(order_id, sid, text, to="buyer", data={"action": "closed"})
        except Exception as exc:  # noqa: BLE001 - a seller that can't be reached doesn't block the deal
            log.warning("could not close negotiation with %s: %s", sid, exc)
        order["sellers"][sid]["status"] = "not_picked" if keep else "closed"

    await asyncio.gather(*(close(s) for s in open_ids))
    _save(order)


async def _select(order: dict, agreed: dict[str, dict]) -> None:
    m, order_id = order["mandate"], order["id"]
    valid = {s: o for s, o in agreed.items()
             if o["total"] <= m["budget_max"] + 0.005 and o["delivery_date"] <= m["deadline"]}
    if not valid:
        order["status"] = "no_deal"
        _save(order)
        say(order_id, "buyer", _no_deal_message(order), kind="decision")
        await _close_others(order, keep=None,
                            message="Thanks for your time. We couldn't make this one work within our limits.")
        return

    ranked = sorted(valid.items(), key=lambda kv: (kv[1]["total"], kv[1]["delivery_date"], -_reliability(kv[0])))
    sid, deal = ranked[0]
    opening = order["sellers"][sid].get("opening_total", deal["total"])
    highest_open = max(e.get("opening_total", 0) for e in order["sellers"].values()) or opening
    others = [(order["sellers"][s]["name"], o["total"], o["delivery_date"]) for s, o in ranked[1:]]
    dropped = [e["name"] for e in order["sellers"].values() if e["status"] == "dropped_deadline"]
    bench = order["benchmark"]

    explanation = await ask_json(
        system="You are a buying agent explaining your choice to a busy small business owner in plain language.",
        prompt=(f"Chosen: {order['sellers'][sid]['name']} at {deal['total']:.2f} {m['currency']} "
                f"({deal['unit_price']:.2f}/unit), delivery {deal['delivery_date']} (deadline {m['deadline']}). "
                f"Their opening offer was {opening:.2f}. Other final offers: {others}. "
                f"Dropped for missing the deadline: {dropped}. Market median per unit: "
                f"{bench['median'] if bench else 'unknown'}. In 2-3 short sentences explain why. "
                'JSON: {"explanation": str}'),
        fallback=lambda: {"explanation": (
            f"{order['sellers'][sid]['name']} gave the lowest total ({deal['total']:.2f} {m['currency']}) that "
            f"arrives by {deal['delivery_date']}, before your {m['deadline']} deadline. That's "
            f"{opening - deal['total']:.2f} below their opening price.")},
        max_tokens=250,
    )

    reply, _ = await a2a.send(order["sellers"][sid]["url"], {
        "action": "finalize", "order_id": order_id, "unit_price": deal["unit_price"],
        mandates.CHECKOUT_MANDATE_KEY: order["ap2"]["open_checkout"]}, "Finalizing the agreed terms.", order_id)
    checkout = _check_checkout(order, sid, deal, reply.get(mandates.CHECKOUT_KEY) or {})

    needs_approval = deal["total"] > m["approval_threshold"]
    order.update({
        "status": "awaiting_approval",
        "winner": {**deal, "seller_id": sid, "seller_name": order["sellers"][sid]["name"],
                   "explanation": explanation.get("explanation", ""),
                   "saved_vs_opening": _r(opening - deal["total"]),
                   "saved_vs_highest_quote": _r(highest_open - deal["total"]),
                   "needs_explicit_approval": needs_approval},
    })
    order["ap2"]["checkout"] = checkout
    _save(order)
    say(order_id, sid, "Signed AP2 checkout attached. Terms are locked.", to="buyer", kind="mandate",
        data={"checkout_hash": checkout["checkout_hash"]})
    auto_ok, why = wallet.auto_pay_check(deal["total"]) if not needs_approval else (False, "")
    if needs_approval:
        limit_note = f"It's above your {m['approval_threshold']:.2f} limit, so I need your approval."
    elif auto_ok:
        limit_note = "It's within your auto-approve limit, so I'll hold it with your saved PayPal."
    elif wallet.payment_token():
        limit_note = f"It's within your auto-approve limit, but {why}, so please approve it."
    else:
        limit_note = "It's within your auto-approve limit. Confirm to pay with PayPal."
    say(order_id, "buyer", f"Best deal: {order['sellers'][sid]['name']}, {deal['total']:.2f} {m['currency']}, "
        f"delivery {deal['delivery_date']}. {explanation.get('explanation', '')} {limit_note}", kind="decision",
        data={"winner": sid})
    await _close_others(order, keep=sid,
                        message="Thanks for working with us. We've gone with another seller this time, so "
                                "there's no order from us on this one.")
    if auto_ok:
        try:
            await _pay(order, user_present=False, why=why)
        except Exception as exc:  # noqa: BLE001 - the deal stands; the owner can still approve by hand
            log.warning("auto-pay failed for %s: %s", order_id, exc)
            say(order_id, "buyer", f"I couldn't hold it automatically ({exc}). Approve it when you're ready.",
                kind="decision")


def _check_checkout(order: dict, sid: str, deal: dict, doc: dict) -> dict:
    """The seller's signed checkout must be signed by that seller and be exactly the deal agreed."""
    m = order["mandate"]
    seller_jwk = mandates.jwk_from(order["sellers"][sid].get("jwk"))
    try:
        payload = mandates.verify_receipt(doc["checkout_jwt"], seller_jwk)  # a plain ES256 JWT check
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"the seller's checkout signature doesn't verify ({exc})") from None
    if mandates.sha256_b64url(doc["checkout_jwt"]) != doc.get("checkout_hash"):
        raise RuntimeError("the checkout hash doesn't match the signed checkout")
    total = next((t["amount"] for t in payload.get("totals", []) if t["type"] == "total"), None)
    line = (payload.get("line_items") or [{}])[0]
    problems = [p for p, bad in (
        ("a different merchant", (payload.get("merchant") or {}).get("id") != sid),
        ("a different total", total != mandates.cents(deal["total"])),
        ("a different quantity", line.get("quantity") != m["quantity"]),
        ("a different currency", payload.get("currency") != m["currency"]),
    ) if bad]
    if problems:
        raise RuntimeError("the seller's signed checkout has " + ", ".join(problems))
    return {"checkout_jwt": doc["checkout_jwt"], "checkout_hash": doc["checkout_hash"], "seller_id": sid,
            "payload": payload}


# ---------------------------------------------------------------- payment

async def approve(order_id: str) -> dict:
    """The owner tapped Approve. With a saved PayPal the hold happens right here; otherwise PayPal's page."""
    order = store.get_order(order_id)
    if not order or order["status"] != "awaiting_approval":
        raise ValueError("order is not waiting for approval")
    if not (order.get("ap2") or {}).get("checkout"):
        raise ValueError("This order was made before Procuro moved to AP2 v0.2. Please start a new order.")
    return await _pay(order, user_present=True)


async def _pay(order: dict, *, user_present: bool, why: str = "", allow_saved: bool = True) -> dict:
    """Approve the seller's exact checkout with AP2 mandates and ask the seller for a PayPal hold.

    user_present=False is auto-pay (AP2 "human not present"): the agent signs the closed checkout and
    payment mandates itself, on top of the owner's open mandates, so the seller can check every limit.
    user_present=True: the owner tapped Approve, so Procuro's Trusted Surface signs for her.
    """
    order_id, m = order["id"], order["mandate"]
    ap = order["ap2"]
    checkout = ap["checkout"]
    sid = order["winner"]["seller_id"]
    seller = order["sellers"][sid]
    vault_id = wallet.payment_token() if allow_saved else None
    total, cur = order["winner"]["total"], m["currency"]
    closed = mandates.close_mandates(
        mode=mandates.HUMAN_PRESENT if user_present else mandates.HUMAN_NOT_PRESENT,
        open_checkout=ap["open_checkout"], open_payment=ap["open_payment"],
        checkout_token=checkout["checkout_jwt"], checkout_hash=checkout["checkout_hash"],
        payee=mandates.merchant(sid, seller["name"], seller["url"]), amount=total, currency=cur,
        saved_paypal=bool(vault_id), audience=seller["url"])
    reply, text = await a2a.send(seller["url"], {
        "action": "create_payment", "order_id": order_id,
        mandates.CHECKOUT_MANDATE_KEY: closed["checkout_mandate"],
        mandates.PAYMENT_MANDATE_KEY: closed["payment_mandate"],
        mandates.PRESENTATION_KEY: closed["presentation"],
        **({"vault_id": vault_id} if vault_id else {}),
        "return_url": f"{config.FRONTEND_URL}/?order={order_id}&paypal=return",
        "cancel_url": f"{config.FRONTEND_URL}/?order={order_id}&paypal=cancel",
    }, "AP2 checkout and payment mandates for the agreed checkout.", order_id)
    order = store.get_order(order_id) or order
    ap = order["ap2"]

    if reply.get("action") == "rejected":
        refusal = {"at": _now(), "mode": closed["mode"], "error": reply.get("error"),
                   "error_description": reply.get("error_description"),
                   "receipt": reply.get(mandates.PAYMENT_RECEIPT_KEY) or reply.get(mandates.CHECKOUT_RECEIPT_KEY),
                   "checkout_mandate": closed["checkout_mandate"], "payment_mandate": closed["payment_mandate"],
                   "presentation": closed["presentation"]}
        ap.setdefault("refusals", []).append(refusal)
        _save(order)
        say(order_id, sid, text or reply.get("error_description", ""), to="buyer", data={"action": "rejected"})
        if reply.get("error") == "payment_instrument_unavailable" and vault_id:
            if user_present:
                # Same approval, paid on PayPal's own page instead of the saved account.
                return await _pay(order, user_present=True, allow_saved=False)
            say(order_id, "buyer", f"{order['winner']['seller_name']} can't take your saved PayPal, so this one "
                "needs your approval on PayPal's page.", kind="decision")
            return order
        raise RuntimeError(f"{order['winner']['seller_name']} refused the payment: {reply.get('error_description')}")

    ap.update({"mode": closed["mode"], "checkout_mandate": closed["checkout_mandate"],
               "payment_mandate": closed["payment_mandate"], "presentation": closed["presentation"],
               "user_present": user_present})
    if reply.get("action") == "authorized":
        _store_receipts(order, reply)
        order["paypal"].update({"order_id": reply["paypal_order_id"], "authorization_id": reply["authorization_id"],
                                "authorized_amount": reply["amount"], "authorized_at": _now(),
                                "expiration_time": reply.get("expiration_time"), "auth_status": reply["status"],
                                "saved_paypal": True, "auto_paid": not user_present})
        order["status"] = "held"
        _save(order)
        if user_present:
            line = (f"You approved the deal. AP2 checkout and payment mandates signed, and {total:.2f} {cur} is "
                    "held with your saved PayPal, no trip to PayPal needed. Nothing is paid until you confirm delivery.")
        else:
            line = (f"{total:.2f} {cur} is within your {m['approval_threshold']:.2f} ask-me-above limit "
                    f"({why}), so I signed the AP2 mandates myself under your open mandates and held it with your "
                    "saved PayPal. Nothing is paid until you confirm delivery, and you can still report a problem.")
        say(order_id, "buyer", line, kind="payment",
            data={"checkout_hash": order["ap2"]["checkout"]["checkout_hash"], "paypal_order_id": reply["paypal_order_id"],
                  "auto": not user_present})
        say(order_id, "paypal", f"Authorization {reply['authorization_id']}: {reply['amount']:.2f} {cur} held on "
            "your PayPal account.", kind="payment",
            data={k: v for k, v in reply.items() if not k.startswith("ap2.")})
        say(order_id, sid, text, to="buyer")
        return order

    # No saved PayPal: approve on PayPal's own page.
    order["paypal"].update({"order_id": reply["paypal_order_id"], "approve_url": reply["approve_url"]})
    order["status"] = "awaiting_paypal"
    _save(order)
    say(order_id, "buyer", "You approved the deal. AP2 checkout and payment mandates signed. Opening PayPal so you "
        "can authorize the payment. Money will be held, not taken.", kind="payment",
        data={"checkout_hash": order["ap2"]["checkout"]["checkout_hash"], "paypal_order_id": reply["paypal_order_id"]})
    return order


def _store_receipts(order: dict, reply: dict) -> None:
    """Keep the seller's signed AP2 receipts, after checking its signature and that they point at our mandates."""
    sid = order["winner"]["seller_id"]
    seller_jwk = mandates.jwk_from(order["sellers"][sid].get("jwk"))
    ap = order["ap2"]
    for key, chain, name in ((mandates.CHECKOUT_RECEIPT_KEY, ap["checkout_mandate"], "checkout_receipt"),
                             (mandates.PAYMENT_RECEIPT_KEY, ap["payment_mandate"], "payment_receipt")):
        token = reply.get(key)
        if not token:
            continue
        body = mandates.verify_receipt(token, seller_jwk)
        if body.get("reference") != mandates.sha256_b64url(mandates.leaf(chain)):
            raise RuntimeError(f"the seller's {name.replace('_', ' ')} points at a different mandate")
        ap[name] = token


async def paypal_returned(order_id: str, token: str) -> dict:
    order = store.get_order(order_id)
    if not order or order["paypal"].get("order_id") != token:
        raise ValueError("PayPal order does not match")
    if order["status"] == "held":
        return order
    sid = order["winner"]["seller_id"]
    reply, text = await a2a.send(order["sellers"][sid]["url"], {
        "action": "authorize", "order_id": order_id, "paypal_order_id": token}, "Authorize the approved order.",
        order_id)
    order = store.get_order(order_id) or order
    if order.get("ap2"):
        _store_receipts(order, reply)
    order["paypal"].update({"authorization_id": reply["authorization_id"], "authorized_amount": reply["amount"],
                            "authorized_at": _now(), "expiration_time": reply.get("expiration_time"),
                            "auth_status": reply["status"]})
    order["status"] = "held"
    _save(order)
    say(order_id, "paypal", f"Authorization {reply['authorization_id']}: {reply['amount']:.2f} "
        f"{order['mandate']['currency']} held on your PayPal account.", kind="payment",
        data={k: v for k, v in reply.items() if not k.startswith("ap2.")})
    say(order_id, sid, text, to="buyer")
    return order


async def _release(order: dict, amount: float, note: str) -> dict:
    sid = order["winner"]["seller_id"]
    ap = order.get("ap2") or {}
    release = mandates.release_instruction({
        "iss": f"{config.PUBLIC_BASE_URL}/agents/buyer", "order_id": order["id"],
        "authorization_id": order["paypal"]["authorization_id"], "capture_amount": _r(amount), "final": True,
        "note": note, "currency": order["mandate"]["currency"],
        # Which approved payment this settles (sha256 of the closed AP2 payment mandate).
        "payment_mandate_ref": mandates.sha256_b64url(mandates.leaf(ap["payment_mandate"]))
        if ap.get("payment_mandate") else None})
    if order.get("ap2") is not None:
        order["ap2"]["release"] = release  # kept for the receipt
    if _r(amount) < 0.01:
        # PayPal can't capture zero. Paying nothing means voiding the hold: every cent goes back.
        reply, text = await a2a.send(order["sellers"][sid]["url"], {
            "action": "void", "order_id": order["id"], "release": release}, note, order["id"])
        held = float(order["paypal"].get("authorized_amount") or 0)
        order["paypal"].update({"captured": 0.0, "released": held, "voided": True, "capture_id": None,
                                "capture_status": "VOIDED"})
        say(order["id"], "paypal", f"Authorization voided: nothing paid to {order['winner']['seller_name']}, "
            f"all {held:.2f} {order['mandate']['currency']} released back to you.", kind="payment",
            data={"action": "voided", "captured": 0.0, "released": held})
        say(order["id"], sid, text, to="buyer")
        return {"action": "voided", "captured": 0.0, "released": held}
    reply, text = await a2a.send(order["sellers"][sid]["url"], {
        "action": "release", "order_id": order["id"], "release": release}, note, order["id"])
    order["paypal"].update({"captured": reply["captured"], "released": reply["released"],
                            "capture_id": reply["capture_id"], "capture_status": reply["status"]})
    say(order["id"], "paypal", f"Captured {reply['captured']:.2f} {order['mandate']['currency']} to "
        f"{order['winner']['seller_name']}." + (f" Released {reply['released']:.2f} back to you."
                                                 if reply["released"] > 0 else ""), kind="payment", data=reply)
    say(order["id"], sid, text, to="buyer")
    return reply


def _record_outcome(sid: str, problem: bool) -> None:
    stats = store.get_doc("sellers", sid) or {"orders": 0, "problems": 0}
    stats["orders"] = stats.get("orders", 0) + 1
    stats["problems"] = stats.get("problems", 0) + (1 if problem else 0)
    # Smoothed so one bad order lowers trust without wiping it out (starts from 4 "good" orders).
    good = stats["orders"] - stats["problems"]
    stats["score"] = round(100 * (good + 4) / (stats["orders"] + 4))
    store.set_doc("sellers", sid, stats)


async def confirm_delivery(order_id: str) -> dict:
    order = store.get_order(order_id)
    if not order or order["status"] != "held":
        raise ValueError("order has no active hold")
    say(order_id, "buyer", "Delivery confirmed by you. Releasing payment to the seller.", kind="decision")
    await _release(order, order["paypal"]["authorized_amount"], "Delivery confirmed. Thank you!")
    order["status"] = "completed"
    _save(order)
    _record_outcome(order["winner"]["seller_id"], problem=False)
    return order


# ---------------------------------------------------------------- problems

async def assess_damage(order: dict, note: str, photos: list[tuple[str, str]]) -> dict:
    qty = order["mandate"]["quantity"]

    def fallback() -> dict:
        nums = [int(n) for n in re.findall(r"\b(\d{1,6})\b", note) if int(n) <= qty]
        damaged = nums[0] if nums else 0
        cosmetic = bool(re.search(r"crooked|off-?cent|misprint|faded|scratch|smudge", note, re.I))
        return {"damaged_units": damaged, "usable_units": qty - damaged,
                "defect": "cosmetic print defect on usable units" if cosmetic else "none",
                "defect_severity": "minor" if cosmetic else "none",
                "recommended_discount_pct": 15 if cosmetic else 0,
                "summary": f"{damaged} of {qty} units reported damaged." + (" Print defect is cosmetic; boxes are "
                                                                              "still usable." if cosmetic else "")}

    result = await ask_json(
        system=("You are a careful, fair claims assessor working for the buyer. Judge only what the evidence "
                "supports. Don't exaggerate damage. Distinguish unusable units from cosmetic defects."),
        prompt=(f"Order: {qty} x {order['mandate']['item']} at {order['winner']['unit_price']:.2f}/unit. "
                f"Buyer's note: \"{note}\". {len(photos)} photo(s) attached.\n"
                "Estimate damaged_units (unusable), usable_units, any defect on usable units and its severity "
                "(none/minor/major), and a fair recommended_discount_pct on usable units (0-50).\n"
                'JSON: {"damaged_units": int, "usable_units": int, "defect": str, "defect_severity": str, '
                '"recommended_discount_pct": number, "summary": str}'),
        fallback=fallback,
        images=photos,
        max_tokens=400,
    )
    damaged = max(0, min(qty, int(result.get("damaged_units", 0))))
    result["damaged_units"] = damaged
    result["usable_units"] = qty - damaged
    result["recommended_discount_pct"] = max(0.0, min(50.0, float(result.get("recommended_discount_pct", 0))))
    result["photos"] = len(photos)
    return result


async def report_problem(order_id: str, note: str, photos: list[tuple[str, str]],
                         evidence: list[dict] | None = None) -> dict:
    order = store.get_order(order_id)
    if not order or order["status"] != "held":
        raise ValueError("only orders with an active hold can be disputed this way")
    order["status"] = "resolving"
    order["problem_note"] = note
    order["evidence"] = [{k: e[k] for k in ("id", "sha256", "media_type", "original_bytes", "stored") if k in e}
                         for e in (evidence or [])]
    _save(order)
    photo_note = f" with {len(photos)} photo{'s' if len(photos) != 1 else ''}" if photos else ""
    say(order_id, "buyer", f"You reported a problem{photo_note}: \"{note}\". Assessing the evidence. "
        "Payment stays on hold.", kind="system",
        data={"evidence": [e["id"] for e in order["evidence"]]} if order["evidence"] else None)
    assessment = await assess_damage(order, note, photos)
    order["assessment"] = assessment
    _save(order)
    say(order_id, "buyer", f"Assessment: {assessment['summary']}", kind="decision", data=assessment)
    return order


async def resolve(order_id: str) -> None:
    order = store.get_order(order_id)
    try:
        await _resolve(order)
    except Exception as exc:  # noqa: BLE001
        log.exception("resolution failed")
        order = store.get_order(order_id)
        order["status"] = "escalated"
        order["error"] = str(exc)
        _save(order)
        say(order_id, "system", f"Resolution stopped: {exc}. Your payment is still on hold.", kind="error")


async def _resolve(order: dict) -> None:
    order_id, m, w, a = order["id"], order["mandate"], order["winner"], order["assessment"]
    sid, cur = w["seller_id"], m["currency"]
    held = float(order["paypal"]["authorized_amount"])
    fair_full = _r(a["usable_units"] * w["unit_price"])  # never pay more than this
    proposal = _r(fair_full * (1 - a["recommended_discount_pct"] / 100))
    evidence = " (photos attached)" if a.get("photos") else ""
    reason = (f"{a['damaged_units']} of {m['quantity']} units are unusable{evidence}."
              + (f" Usable units have a {a['defect_severity']} defect: {a['defect']}; asking "
                 f"{a['recommended_discount_pct']:.0f}% off those." if a["recommended_discount_pct"] else ""))
    history: list[dict] = []
    for round_no in range(1, 4):
        if round_no == 1:
            msg = (f"Payment for damaged units won't be released. {reason} I propose capturing {proposal:.2f} "
                   f"{cur} of the {held:.2f} held and releasing the rest.")
        else:
            msg = f"I can move to {proposal:.2f} {cur} for the usable units. That's my offer to settle today."
        say(order_id, "buyer", msg, to=sid, data={"action": "propose_remedy", "capture_amount": proposal})
        reply, text = await a2a.send(order["sellers"][sid]["url"], {
            "action": "propose_remedy", "order_id": order_id, "assessment": a, "round": round_no,
            "proposal": {"capture_amount": proposal, "reason": reason}}, msg, order_id)
        say(order_id, sid, text, to="buyer", data=reply)
        history.append(reply)
        if reply["action"] == "remedy_accept":
            break
        if reply["action"] == "remedy_reject":
            proposal = None
            break
        # Seller countered: evaluate it against the mandate.
        counter_amt = float(reply["capture_amount"])
        reprint = reply.get("reprint_by")
        move = await ask_json(
            system="You are the buyer's agent resolving a damaged order fairly and firmly.",
            prompt=(f"Deadline was {m['deadline']}; reprint offered by {reprint}. Seller wants to capture "
                    f"{counter_amt:.2f}. Our proposal: {proposal:.2f}. Full price for usable units only: "
                    f"{fair_full:.2f}. Round {round_no} of 3.\n"
                    "A reprint after the deadline is worthless to the buyer. Never agree above the usable-units "
                    "price. Choose: accept their amount, or a new proposal between ours and theirs.\n"
                    'JSON: {"decision": "accept"|"counter", "capture_amount": number, "message": str}'),
            fallback=lambda: ({"decision": "accept", "capture_amount": counter_amt, "message": "Agreed."}
                              if counter_amt <= proposal + (fair_full - proposal) / 2 + 0.01 else
                              {"decision": "counter",
                               "capture_amount": _r((proposal + min(counter_amt, fair_full)) / 2),
                               "message": "A reprint after the event doesn't help. Let's settle on the usable units."}),
            max_tokens=250,
        )
        if reprint and reprint > m["deadline"]:
            say(order_id, "buyer", f"A reprint on {reprint} is after the {m['deadline']} deadline, so it doesn't "
                "help. Settling on payment instead.", kind="decision")
        if move.get("decision") == "accept" and counter_amt <= fair_full + 0.005:
            proposal = counter_amt
            # Seller already agreed to its own number.
            history.append({"action": "remedy_accept", "capture_amount": counter_amt})
            break
        proposal = _r(min(float(move.get("capture_amount") or proposal), fair_full))
    else:
        proposal = None

    agreed = history and history[-1]["action"] == "remedy_accept" and proposal is not None
    if agreed:
        released = _r(held - proposal)
        order["remedy"] = {"capture_amount": proposal, "released": released, "rounds": len(history)}
        say(order_id, "buyer", f"Settled: pay {proposal:.2f} {cur} for the usable units and release "
            f"{released:.2f} back to you.", kind="decision")
        await _release(order, proposal, f"Partial capture for {a['usable_units']} usable units, as agreed.")
        order["status"] = "settled"
        _save(order)
        _record_outcome(sid, problem=True)
        say(order_id, "buyer", f"{w['seller_name']}'s reliability score is now {_reliability(sid)}. I'll factor "
            "that in next time.", kind="system")
        return

    order["dispute_packet"] = {
        "summary": f"Order {order_id}: {a['damaged_units']} of {m['quantity']} units arrived unusable.",
        "open_checkout_mandate_hash": (order.get("ap2") or {}).get("open_checkout_hash"),
        "checkout_hash": ((order.get("ap2") or {}).get("checkout") or {}).get("checkout_hash"),
        "agreed_terms": ((order.get("ap2") or {}).get("checkout") or {}).get("payload"),
        "assessment": a,
        "customer_note": order.get("problem_note"),
        # Each photo's SHA-256 is taken from the original upload, so the evidence can be verified later.
        "evidence": order.get("evidence", []),
        "negotiation": history,
        "paypal_authorization_id": order["paypal"]["authorization_id"],
        "requested_outcome": f"Pay no more than {fair_full:.2f} {cur} for usable units",
    }
    order["status"] = "escalated"
    _save(order)
    _record_outcome(sid, problem=True)
    say(order_id, "buyer", "The seller wouldn't agree to a fair settlement. Your money is still on hold and I've "
        "prepared a complete dispute file (mandates, agreed terms, evidence, negotiation log) for PayPal's "
        "resolution process.", kind="decision")


# ---------------------------------------------------------------- questions + maintenance

async def ask(order_id: str, question: str) -> str:
    order = store.get_order(order_id)
    msgs = store.list_messages(order_id)
    log_text = "\n".join(f"[{x['from']}] {x['text']}" for x in msgs[-60:])
    res = await ask_json(
        system="You are the user's buying agent answering questions about an order you handled. Be specific, "
               "use the actual numbers, and be honest about trade-offs.",
        prompt=(f"Order record: {({k: order.get(k) for k in ('mandate', 'benchmark', 'winner', 'paypal', 'status', 'assessment', 'remedy')})}\n"
                f"Sellers: {({s: {'name': e['name'], 'status': e['status'], 'offers': e['offers']} for s, e in order['sellers'].items()})}\n"
                f"Feed:\n{log_text}\n\nQuestion: {question}\nJSON: {{\"answer\": str}}"),
        fallback=lambda: {"answer": (order.get("winner") or {}).get("explanation", "No decision yet.")},
        max_tokens=400,
    )
    return res.get("answer", "")


async def renew_holds() -> list[str]:
    """PayPal honors an authorization for 3 days; reauthorize once inside the 29-day window."""
    renewed = []
    for order in store.list_orders_by_status("held"):
        p = order.get("paypal") or {}
        if p.get("reauthorized"):
            continue
        age_days = (_now() - float(p.get("authorized_at", _now()))) / 86400
        if HONOR_PERIOD_DAYS <= age_days < MAX_REAUTH_DAYS:
            sid = order["winner"]["seller_id"]
            reply, _ = await a2a.send(order["sellers"][sid]["url"], {
                "action": "reauthorize", "order_id": order["id"]}, "Renew the hold.", order["id"])
            p.setdefault("previous_authorization_ids", []).append(p.get("authorization_id"))
            p.update({"authorization_id": reply["authorization_id"], "reauthorized": True,
                      "first_authorized_at": p.get("authorized_at"), "authorized_at": _now(),
                      "reauthorized_at": _now()})
            order["paypal"] = p
            _save(order)
            say(order["id"], "paypal", f"Hold renewed before expiry (new authorization {reply['authorization_id']}).",
                kind="payment")
            renewed.append(order["id"])
    return renewed


# ---------------------------------------------------------------- sharing a bulk order

async def share_order(order_id: str, *, partner_name: str, partner_email: str, quantity: int,
                      note: str = "") -> dict:
    """Bill a partner business for part of this order with a PayPal invoice (Agent Toolkit)."""
    order = store.get_order(order_id)
    if not order:
        raise ValueError("order not found")
    s = await invoices.share(order, partner_name=partner_name, partner_email=partner_email, quantity=quantity,
                             note=note)
    _save(order)
    cur = s["currency"]
    say(order_id, "buyer", f"Sent {s['partner_name']} a PayPal invoice for their share: {s['quantity']} x "
        f"{order['mandate']['item']} at the negotiated {s['unit_price']:.2f} {cur} each, {s['amount']:.2f} {cur} "
        f"in total. PayPal emailed it to {s['partner_email']}" + (" (demo mode: not really sent)." if s.get("mock")
                                                                   else ", with a pay link and a QR code."),
        kind="payment", data={"invoice_id": s["invoice_id"], "amount": s["amount"]})
    return order


async def refresh_shares(order_id: str) -> dict:
    order = store.get_order(order_id)
    if not order:
        raise ValueError("order not found")
    before = {s["invoice_id"]: s.get("status") for s in order.get("shares", [])}
    await invoices.refresh(order)
    _save(order)
    for s in order.get("shares", []):
        if s.get("status") == "PAID" and before.get(s["invoice_id"]) != "PAID":
            say(order_id, "paypal", f"{s['partner_name']} paid their invoice: {s['amount']:.2f} {s['currency']} "
                f"for {s['quantity']} x {order['mandate']['item']}.", kind="payment",
                data={"invoice_id": s["invoice_id"]})
    return order


# ---------------------------------------------------------------- moving a delivery date

MOVABLE = ("awaiting_approval", "held")
MAX_HOLD_DAYS = 29  # PayPal honors an authorization for up to 29 days, renewals included


async def reschedule(order_id: str, new_date: str) -> dict:
    """The owner dragged the delivery to another day. Ask the seller over A2A; it decides from its own
    production schedule. If it agrees it signs a change to the cart (same price); the PayPal hold stays
    as it is, so nothing is paid twice."""
    order = store.get_order(order_id)
    if not order:
        raise ValueError("order not found")
    if order["status"] not in MOVABLE:
        raise ValueError("Only deliveries that haven't arrived yet can be moved.")
    try:
        wanted = dt.date.fromisoformat(str(new_date)[:10])
    except ValueError:
        raise ValueError("The new date must be YYYY-MM-DD.") from None
    if wanted <= dt.date.today():
        raise ValueError("Pick a day after today.")
    w, m, p = order["winner"], order["mandate"], order.get("paypal") or {}
    old = w["delivery_date"]
    if wanted.isoformat() == old:
        return {"accepted": True, "order": order, "message": "That's already the delivery date."}

    # PayPal can hold the money for 29 days at most (from the first authorization), and the owner needs a
    # day after delivery to check the goods before it's released.
    if order["status"] == "held":
        held_since = float(p.get("first_authorized_at") or p.get("authorized_at") or _now())
        hold_ends = dt.date.fromtimestamp(held_since) + dt.timedelta(days=MAX_HOLD_DAYS)
        latest = hold_ends - dt.timedelta(days=1)
        if wanted > latest:
            msg = (f"PayPal can only hold your payment until {hold_ends}, so the latest delivery that still leaves "
                   f"you a day to check it is {latest}.")
            say(order_id, "buyer", f"I can't move delivery to {wanted}: {msg}", kind="decision")
            return {"accepted": False, "order": order, "message": msg, "earliest": None, "latest": latest.isoformat()}

    sid = w["seller_id"]
    seller_name = w["seller_name"]
    say(order_id, "buyer", f"Can you deliver on {wanted.isoformat()} instead of {old}? Same order, same price.",
        to=sid, data={"action": "reschedule", "delivery_date": wanted.isoformat()})
    reply, text = await a2a.send(order["sellers"][sid]["url"], {
        "action": "reschedule", "order_id": order_id, "delivery_date": wanted.isoformat(),
        "checkout_hash": ((order.get("ap2") or {}).get("checkout") or {}).get("checkout_hash")},
        f"Please move delivery to {wanted}.", order_id)
    say(order_id, sid, text or "", to="buyer", data=reply)

    if reply.get("action") != "rescheduled":
        earliest = reply.get("earliest")
        msg = f"{seller_name} can't deliver on {wanted}" + (f". The nearest date they can do is {earliest}." if earliest else ".")
        say(order_id, "buyer", f"{msg} Delivery stays on {old}.", kind="decision",
            data={"seller_id": sid, "earliest": earliest})
        return {"accepted": False, "order": store.get_order(order_id), "message": msg, "earliest": earliest}

    amendment = reply.get("amendment")
    if amendment:
        try:
            mandates.verify_extension(amendment, mandates.jwk_from(order["sellers"][sid].get("jwk")),
                                      mandates.AMENDMENT_TYP)
        except Exception:  # noqa: BLE001
            raise RuntimeError("the seller's signed change to the checkout doesn't verify") from None
    order = store.get_order(order_id)
    w, m = order["winner"], order["mandate"]  # fresh copies: the conversation was saved meanwhile
    w.setdefault("first_delivery_date", old)
    w["delivery_date"] = wanted.isoformat()
    if order.get("ap2") is not None and amendment:
        order["ap2"].setdefault("amendments", []).append(amendment)
    order.setdefault("reschedules", []).append({"from": old, "to": wanted.isoformat(), "at": _now(),
                                                "amendment_id": mandates.sha256_b64url(amendment) if amendment else None})
    note = ""
    if wanted.isoformat() > m["deadline"]:
        note = f" That's after the {m['deadline']} deadline you set, so I've moved your deadline to match."
        m["deadline_before_move"] = m.get("deadline_before_move") or m["deadline"]
        m["deadline"] = wanted.isoformat()
    _save(order)
    held = " Your PayPal hold is unchanged, so nothing is paid twice." if order["status"] == "held" else ""
    say(order_id, "buyer", f"Delivery moved from {old} to {wanted}. {seller_name} signed the change to the checkout: "
        f"same price.{held}{note}", kind="decision",
        data={"amendment_id": mandates.sha256_b64url(amendment) if amendment else None})
    return {"accepted": True, "order": order, "message": f"{seller_name} will deliver on {wanted}."}


# ---------------------------------------------------------------- the Sellers board, for outside sellers

BOARD_STATUS = {"awaiting_approval": "reserved", "awaiting_paypal": "reserved", "held": "confirmed",
                "resolving": "confirmed", "escalated": "disputed", "completed": "delivered", "settled": "delivered"}


async def outside_board(today: dt.date) -> dict:
    """Rows for the outside seller agents on the Sellers board.

    Procuro can't see an outside seller's production schedule (it runs its own business on its own
    server), so its row shows only the orders placed with it from here: from the day the order was made
    to its delivery date, coloured by where the money is.
    """
    rows: dict[str, dict] = {}
    events: list[dict] = []
    for order in store.list_orders():
        w = order.get("winner") or {}
        sid = w.get("seller_id")
        seller = (order.get("sellers") or {}).get(sid or "", {})
        status = BOARD_STATUS.get(order.get("status"))
        if not (sid and seller.get("outside") and status and w.get("delivery_date")):
            continue
        row = rows.setdefault(sid, {"id": sid, "name": seller.get("name") or sid, "url": seller.get("url"),
                                    "outside": True, "inProgress": 0})
        if status in ("reserved", "confirmed"):
            row["inProgress"] += 1
        p = order.get("paypal") or {}
        created = dt.datetime.fromtimestamp(order.get("created_at") or _now(), dt.timezone.utc).date()
        delivery = dt.date.fromisoformat(w["delivery_date"][:10])
        m = order.get("mandate") or {}
        events.append({
            "id": f"{sid}-{order['id']}", "resourceId": sid, "orderId": order["id"],
            "name": f"{m.get('quantity')} x {m.get('item')}",
            "startDate": min(created, delivery).isoformat(),
            "endDate": (delivery + dt.timedelta(days=1)).isoformat(),
            "deliveryDate": w["delivery_date"][:10], "plannedDelivery": w.get("first_delivery_date"),
            "status": status, "total": w.get("total"),
            "captured": (0.0 if p.get("voided") else p.get("captured")) if status == "delivered" else None,
            "currency": m.get("currency", "USD"), "outside": True,
        })
    # Outside sellers you haven't ordered from yet still get a row (named from their agent card).
    for url in config.SELLER_AGENT_URLS:
        sid = _seller_id(url)
        if sid in rows:
            continue
        try:
            card = await asyncio.wait_for(a2a.fetch_card(url), timeout=4)
            name = card.get("name") or sid
        except Exception:  # noqa: BLE001 - unreachable right now: still list it
            name = sid
        rows[sid] = {"id": sid, "name": name, "url": url, "outside": True, "inProgress": 0}
    return {"resources": list(rows.values()), "events": events}