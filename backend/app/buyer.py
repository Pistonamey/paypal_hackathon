"""The buying agent: works for the user inside a signed mandate.

It turns plain language into a mandate, discovers seller agents over A2A,
checks real market prices on Channel3, negotiates, and pays through PayPal
only with the user's approval. Money is held until delivery is confirmed.
"""
import asyncio
import datetime as dt
import logging
import re
import time
import uuid

from . import a2a, channel3, config, sellers, signing
from .llm import ask_json
from .store import store

log = logging.getLogger("mandate.buyer")

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
        name="Mandate Buyer Agent",
        description="Buys on a user's behalf inside a signed mandate: budget, approval limit, deadline.",
        url=f"{config.PUBLIC_BASE_URL}/agents/buyer",
        public_key=signing.public_key_b64(BUYER_ID),
        role="shopper",
        skills=[{"id": "procure", "name": "Procure", "description": "Find, negotiate and pay for goods",
                 "tags": ["commerce", "negotiation", "paypal"]}],
    )


def say(order_id: str, sender: str, text: str, kind: str = "chat", to: str | None = None, data: dict | None = None):
    store.add_message(order_id, {"from": sender, "to": to, "kind": kind, "text": text, "data": data or {}})


def _save(order: dict) -> None:
    order["updated_at"] = _now()
    store.save_order(order)


# ---------------------------------------------------------------- mandate

def _fallback_parse(text: str) -> dict:
    money = [float(m.replace(",", "")) for m in re.findall(r"[$₹€£]\s?([\d,]+(?:\.\d+)?)", text)]
    qty = re.search(r"\b(\d{1,6})\s+([a-zA-Z][\w\s-]{2,60}?)(?=,|\.|\bwith\b|\bfor\b|\bby\b|\bdelivered\b|$)", text)
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
    budget = max(money) if money else 0
    threshold = min(money) if len(money) > 1 else budget
    return {
        "item": qty.group(2).strip() if qty else text[:60],
        "quantity": int(qty.group(1)) if qty else 1,
        "budget_max": budget,
        "approval_threshold": threshold,
        "currency": "USD",
        "deadline": deadline_iso or (dt.date.today() + dt.timedelta(days=14)).isoformat(),
        "requirements": "",
    }


async def parse_mandate(text: str) -> dict:
    today = dt.date.today().isoformat()
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
    m["currency"] = (m.get("currency") or config.DEFAULT_CURRENCY).upper()
    return m


async def create_order(text: str) -> dict:
    mandate = await parse_mandate(text)
    if mandate["budget_max"] <= 0:
        raise ValueError("Please include a budget, for example: 'Budget $600'.")
    order_id = f"ord_{uuid.uuid4().hex[:10]}"
    intent = signing.sign(BUYER_ID, "intent_mandate", {
        "order_id": order_id,
        "user_prompt": text,
        "item": mandate["item"],
        "quantity": mandate["quantity"],
        "budget_max": mandate["budget_max"],
        "approval_threshold": mandate["approval_threshold"],
        "currency": mandate["currency"],
        "deadline": mandate["deadline"],
        "requirements": mandate.get("requirements", ""),
        "user_present_approval_required_above": mandate["approval_threshold"],
    })
    order = {
        "id": order_id, "created_at": _now(), "status": "negotiating", "request_text": text,
        "mandate": mandate, "intent_mandate": intent, "benchmark": None, "sellers": {},
        "winner": None, "paypal": {}, "events": [],
    }
    _save(order)
    say(order_id, "buyer", f"Mandate signed: {mandate['quantity']} x {mandate['item']}, budget "
        f"{mandate['budget_max']:.2f} {mandate['currency']}, ask before paying over "
        f"{mandate['approval_threshold']:.2f}, delivered by {mandate['deadline']}.", kind="system",
        data={"intent_mandate_id": intent["id"]})
    return order


# ---------------------------------------------------------------- negotiation

def _seller_urls() -> list[str]:
    return config.SELLER_AGENT_URLS or [sellers.agent_url(s) for s in sellers.SELLERS]


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
            return {"decision": "accept" if offer["total"] <= m["budget_max"] else "walk",
                    "unit_price": offer["unit_price"], "message": "Agreed."}
        parts = [f"We're looking for a firm order of {qty}."]
        if bench:
            parts.append(f"Comparable items sell for about {bench['median']:.2f} {cur} across "
                         f"{bench['samples']} listings.")
        if competing:
            parts.append(f"Another seller is at {competing['unit_price']:.2f} delivered {competing['delivery_date']}.")
        parts.append(f"Can you do {target:.2f} per unit?")
        return {"decision": "counter", "unit_price": target, "message": " ".join(parts)}

    market = (f"Channel3 market data: {bench['samples']} listings, per-unit low {bench['low']}, median "
              f"{bench['median']}, high {bench['high']} {cur}." if bench else "No market data available.")
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
                f"Seller {sid} currently offers {offer['unit_price']:.2f}/unit "
                f"(total {offer['total']:.2f}), delivery {offer['delivery_date']}. Round {round_no} of {MAX_ROUNDS}.\n"
                "Decide: accept, counter (propose a lower per-unit price with a short persuasive message), or walk.\n"
                'JSON: {"decision": "accept"|"counter"|"walk", "unit_price": number, "message": str}'),
        fallback=fallback,
        max_tokens=300,
    )
    move["unit_price"] = float(move.get("unit_price") or offer["unit_price"])
    if move.get("decision") == "accept" and offer["total"] > m["budget_max"] + 0.005:
        move = {"decision": "counter", "unit_price": _r(budget_unit * 0.97),
                "message": move.get("message") or "That's above what I can approve. Can you meet me here?"}
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
        bench = await channel3.market_benchmark(m["item"], m["currency"])
        order["benchmark"] = bench
        _save(order)
        if bench:
            say(order_id, "buyer", f"Checked the market on Channel3: {bench['samples']} listings, typically "
                f"{bench['low']:.2f}–{bench['median']:.2f} {m['currency']} per unit.", kind="system",
                data={"benchmark": bench})

        # Discover seller agents through their published A2A agent cards.
        cards: dict[str, dict] = {}
        for url in _seller_urls():
            try:
                c = await a2a.fetch_card(url)
                sid = url.rstrip("/").split("/")[-1]
                cards[sid] = c
                order["sellers"][sid] = {"name": c["name"], "url": c["url"], "status": "contacted", "offers": [],
                                         "public_key": c.get("signing", {}).get("publicKey")}
            except Exception as exc:  # noqa: BLE001
                log.warning("agent card fetch failed for %s: %s", url, exc)
        _save(order)
        say(order_id, "buyer", f"Found {len(cards)} seller agents: {', '.join(c['name'] for c in cards.values())}. "
            "Requesting quotes.", kind="system")

        request = {"action": "request_quote", "order_id": order_id, "item": m["item"], "quantity": m["quantity"],
                   "currency": m["currency"], "needed_by": m["deadline"], "requirements": m.get("requirements", "")}

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
                say(order_id, "buyer", f"{entry['name']} delivers {offer['delivery_date']}, after the "
                    f"{m['deadline']} deadline. Dropping this offer.", kind="decision", data={"seller_id": sid})
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


async def _select(order: dict, agreed: dict[str, dict]) -> None:
    m, order_id = order["mandate"], order["id"]
    valid = {s: o for s, o in agreed.items()
             if o["total"] <= m["budget_max"] + 0.005 and o["delivery_date"] <= m["deadline"]}
    if not valid:
        order["status"] = "no_deal"
        _save(order)
        say(order_id, "buyer", "No seller could meet your budget and deadline. No money was moved. "
            "Try a higher budget or a later deadline.", kind="decision")
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
        "intent_mandate": order["intent_mandate"]}, "Finalizing the agreed terms.", order_id)
    cart = reply["cart_mandate"]
    seller_key = order["sellers"][sid].get("public_key")
    if not signing.verify(cart, seller_key):
        raise RuntimeError("seller's cart mandate signature is invalid")

    needs_approval = deal["total"] > m["approval_threshold"]
    order.update({
        "status": "awaiting_approval",
        "winner": {**deal, "seller_id": sid, "seller_name": order["sellers"][sid]["name"],
                   "explanation": explanation.get("explanation", ""),
                   "saved_vs_opening": _r(opening - deal["total"]),
                   "saved_vs_highest_quote": _r(highest_open - deal["total"]),
                   "needs_explicit_approval": needs_approval},
        "cart_mandate": cart,
    })
    _save(order)
    say(order_id, sid, "Signed cart mandate attached. Terms are locked.", to="buyer", kind="mandate",
        data={"cart_mandate_id": cart["id"]})
    limit_note = (f"It's above your {m['approval_threshold']:.2f} limit, so I need your approval."
                  if needs_approval else "It's within your auto-approve limit. Confirm to pay with PayPal.")
    say(order_id, "buyer", f"Best deal: {order['sellers'][sid]['name']}, {deal['total']:.2f} {m['currency']}, "
        f"delivery {deal['delivery_date']}. {explanation.get('explanation', '')} {limit_note}", kind="decision",
        data={"winner": sid})


# ---------------------------------------------------------------- payment

async def approve(order_id: str) -> dict:
    order = store.get_order(order_id)
    if not order or order["status"] != "awaiting_approval":
        raise ValueError("order is not waiting for approval")
    cart = order["cart_mandate"]
    payment_mandate = signing.sign(BUYER_ID, "payment_mandate", {
        "order_id": order_id,
        "cart_mandate_id": cart["id"],
        "intent_mandate_id": order["intent_mandate"]["id"],
        "total": cart["payload"]["total"],
        "currency": cart["payload"]["currency"],
        "user_present": True,
        "approved_at": int(_now()),
        "payment_method": "paypal",
    })
    sid = order["winner"]["seller_id"]
    reply, _ = await a2a.send(order["sellers"][sid]["url"], {
        "action": "create_payment", "order_id": order_id, "cart_mandate": cart,
        "payment_mandate": payment_mandate,
        "return_url": f"{config.FRONTEND_URL}/?order={order_id}&paypal=return",
        "cancel_url": f"{config.FRONTEND_URL}/?order={order_id}&paypal=cancel",
    }, "Creating the PayPal order for the approved cart.", order_id)
    order["payment_mandate"] = payment_mandate
    order["paypal"].update({"order_id": reply["paypal_order_id"], "approve_url": reply["approve_url"]})
    order["status"] = "awaiting_paypal"
    _save(order)
    say(order_id, "buyer", "You approved the deal. Payment mandate signed. Opening PayPal so you can authorize "
        "the payment. Money will be held, not taken.", kind="payment",
        data={"payment_mandate_id": payment_mandate["id"], "paypal_order_id": reply["paypal_order_id"]})
    return order


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
    order["paypal"].update({"authorization_id": reply["authorization_id"], "authorized_amount": reply["amount"],
                            "authorized_at": _now(), "expiration_time": reply.get("expiration_time"),
                            "auth_status": reply["status"]})
    order["status"] = "held"
    _save(order)
    say(order_id, "paypal", f"Authorization {reply['authorization_id']}: {reply['amount']:.2f} "
        f"{order['mandate']['currency']} held on your PayPal account.", kind="payment", data=reply)
    say(order_id, sid, text, to="buyer")
    return order


async def _release(order: dict, amount: float, note: str) -> dict:
    sid = order["winner"]["seller_id"]
    release = signing.sign(BUYER_ID, "release_mandate", {
        "order_id": order["id"], "authorization_id": order["paypal"]["authorization_id"],
        "capture_amount": _r(amount), "final": True, "note": note})
    reply, text = await a2a.send(order["sellers"][sid]["url"], {
        "action": "release", "order_id": order["id"], "release_mandate": release}, note, order["id"])
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


async def report_problem(order_id: str, note: str, photos: list[tuple[str, str]]) -> dict:
    order = store.get_order(order_id)
    if not order or order["status"] != "held":
        raise ValueError("only orders with an active hold can be disputed this way")
    order["status"] = "resolving"
    _save(order)
    photo_note = f" with {len(photos)} photo{'s' if len(photos) != 1 else ''}" if photos else ""
    say(order_id, "buyer", f"You reported a problem{photo_note}: \"{note}\". Assessing the evidence. "
        "Payment stays on hold.", kind="system")
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
        "intent_mandate": order["intent_mandate"]["id"],
        "cart_mandate": order["cart_mandate"]["id"],
        "agreed_terms": order["cart_mandate"]["payload"],
        "assessment": a,
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
    for order in store.list_orders():
        p = order.get("paypal") or {}
        if order.get("status") != "held" or p.get("reauthorized"):
            continue
        age_days = (_now() - float(p.get("authorized_at", _now()))) / 86400
        if HONOR_PERIOD_DAYS <= age_days < MAX_REAUTH_DAYS:
            sid = order["winner"]["seller_id"]
            reply, _ = await a2a.send(order["sellers"][sid]["url"], {
                "action": "reauthorize", "order_id": order["id"]}, "Renew the hold.", order["id"])
            p.update({"authorization_id": reply["authorization_id"], "reauthorized": True,
                      "authorized_at": _now()})
            order["paypal"] = p
            _save(order)
            say(order["id"], "paypal", f"Hold renewed before expiry (new authorization {reply['authorization_id']}).",
                kind="payment")
            renewed.append(order["id"])
    return renewed
