"""Receipts for orders made before Procuro moved to AP2 v0.2 (Ed25519 "AP2-style" mandates).

New orders use receipt.py. Each step here is a mandate one agent signed (Ed25519). The receipt shows, for each one:
  - who signed it, and whether the signature checks out against the key in that agent's A2A card
  - its SHA-256 fingerprint (of the whole signed mandate, signature included)
  - which earlier step it points to (the cart names the intent, the payment names the cart, ...)
  - the PayPal records it caused (order, authorization, capture or void) and their PayPal-Request-Ids

Then a short list of checks a judge can read: the price stayed inside the budget, PayPal held exactly
the signed amount, the capture never went over the hold, and so on.

The whole receipt has one fingerprint: SHA-256 over the step fingerprints in order. Change any step
and both change. The JSON download carries the full mandates and public keys, so
scripts/verify_receipt.py can check it with no access to Procuro at all.

`paypal_check` goes one step further and asks PayPal for its own record of the authorization and
capture, so the amounts in the signed mandates are compared with PayPal's books, not ours.
"""
import hashlib
import time

from . import config, signing

BUYER_ID = "buyer:mandate"
SANDBOX_ACTIVITY = "https://www.sandbox.paypal.com/activity/payment/"


def fingerprint(mandate: dict) -> str:
    return hashlib.sha256(signing.canonical(mandate)).hexdigest()


def receipt_fingerprint(hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()


def _money(v, cur="USD") -> str:
    try:
        return f"${float(v):,.2f}" if cur == "USD" else f"{float(v):,.2f} {cur}"
    except (TypeError, ValueError):
        return "?"


def _seller_id(signer: str) -> str | None:
    return signer.split(":", 1)[1] if signer.startswith("seller:") else None


def _signer(order: dict, signer: str) -> tuple[str, str | None, str]:
    """(label, published public key, where the key came from)."""
    if signer == BUYER_ID:
        return "Your buying agent", signing.public_key_b64(BUYER_ID), "the buying agent's A2A card"
    sid = _seller_id(signer)
    s = (order.get("sellers") or {}).get(sid or "", {})
    name = s.get("name") or sid or signer
    if s.get("public_key"):
        return f"{name}'s agent", s["public_key"], f"{name}'s A2A card"
    return f"{name}'s agent", None, "derived (no published key on record)"


def _paypal_link(record_id: str | None) -> str | None:
    if not record_id or config.PAYPAL_MODE == "mock" or "sandbox" not in config.PAYPAL_BASE:
        return None
    return SANDBOX_ACTIVITY + record_id


def _step(order: dict, mandate: dict, title: str, summary: str, refs: dict[str, str | None],
          seen: dict[str, str]) -> dict:
    label, key, key_from = _signer(order, mandate.get("signer", ""))
    ok = signing.verify(mandate, key)
    h = fingerprint(mandate)
    links = [{"field": field, "id": rid, "ok": bool(rid) and rid in seen, "step": seen.get(rid or "")}
             for field, rid in refs.items()]
    seen[mandate["id"]] = mandate["type"]
    return {
        "type": mandate["type"], "id": mandate["id"], "title": title, "summary": summary,
        "signer": mandate.get("signer"), "signed_by": label, "key_from": key_from, "public_key": key,
        "issued_at": mandate.get("issued_at"), "alg": mandate.get("alg", "Ed25519"),
        "signature": mandate.get("signature"), "hash": h, "verified": ok, "refers_to": links, "paypal": [],
    }


def build(order: dict) -> dict:
    m = order.get("mandate") or {}
    cur = m.get("currency", "USD")
    p = order.get("paypal") or {}
    seen: dict[str, str] = {}
    steps: list[dict] = []
    mandates: list[dict] = []

    intent = order.get("intent_mandate")
    if intent:
        ip = intent["payload"]
        steps.append(_step(order, intent, "Your rules",
                           f"{ip.get('quantity')} × {ip.get('item')}, budget up to {_money(ip.get('budget_max'), cur)}, "
                           f"ask you above {_money(ip.get('approval_threshold'), cur)}, deliver by {ip.get('deadline')}.",
                           {}, seen))
        mandates.append(intent)

    cart = order.get("cart_mandate")
    if cart:
        cp = cart["payload"]
        item = (cp.get("items") or [{}])[0]
        steps.append(_step(order, cart, f"{cp.get('merchant', 'The seller')}'s signed offer",
                           f"{item.get('quantity')} × {item.get('name')} at {_money(item.get('unit_price'), cur)} = "
                           f"{_money(cp.get('total'), cur)}, delivered {cp.get('delivery_date')}.",
                           {"intent_mandate_id": cp.get("intent_mandate_id")}, seen))
        mandates.append(cart)

    payment = order.get("payment_mandate")
    pay_step = None
    if payment:
        pp = payment["payload"]
        who = ("You approved it" if pp.get("user_present")
               else f"Your agent paid on its own: under your {_money(pp.get('approval_threshold'), cur)} limit")
        method = "your saved PayPal" if pp.get("payment_method") == "paypal_saved" else "PayPal checkout"
        pay_step = _step(order, payment, "Payment approved",
                         f"{who}. Hold {_money(pp.get('total'), cur)} with {method}.",
                         {"cart_mandate_id": pp.get("cart_mandate_id"),
                          "intent_mandate_id": pp.get("intent_mandate_id")}, seen)
        if p.get("order_id"):
            pay_step["paypal"].append({
                "kind": "order", "label": "PayPal order (intent AUTHORIZE)", "id": p["order_id"],
                "request_id": f"create-{order['id']}{'-vault' if p.get('saved_paypal') else ''}"})
        for old in p.get("previous_authorization_ids") or []:
            pay_step["paypal"].append({"kind": "authorization", "label": "First hold (renewed later)", "id": old,
                                       "link": _paypal_link(old)})
        if p.get("authorization_id"):
            first = not p.get("previous_authorization_ids")
            pay_step["paypal"].append({
                "kind": "authorization",
                "label": f"Hold of {_money(p.get('authorized_amount'), cur)}" + ("" if first else " (renewed)"),
                "id": p["authorization_id"], "amount": p.get("authorized_amount"),
                "request_id": (f"auth-{p.get('order_id')}" if first and not p.get("saved_paypal") and p.get("order_id")
                               else None if first else f"reauth-{(p.get('previous_authorization_ids') or [''])[-1]}"),
                "link": _paypal_link(p["authorization_id"])})
    middle = [(payment, pay_step)] if pay_step else []

    for amendment in order.get("cart_amendments") or []:
        if not amendment:
            continue
        ap = amendment["payload"]
        middle.append((amendment, _step(order, amendment, f"{ap.get('merchant', 'The seller')} moved the delivery",
                                        f"{ap.get('previous_delivery_date')} → {ap.get('delivery_date')}. "
                                        f"Same price: {_money(ap.get('total'), cur)}.",
                                        {"cart_mandate_id": ap.get("cart_mandate_id")}, seen)))
    # A delivery can move before or after the payment: keep the steps in the order they were signed.
    for mandate, step in sorted(middle, key=lambda x: x[0].get("issued_at") or 0):
        steps.append(step)
        mandates.append(mandate)

    release = order.get("release_mandate")
    if release:
        rp = release["payload"]
        amount = float(rp.get("capture_amount") or 0)
        voided = p.get("voided") or amount < 0.01
        auth_id = rp.get("authorization_id")
        # The release names a PayPal authorization, not a mandate: it counts as linked when that's the
        # hold the payment step created.
        step = _step(order, release, "Nothing paid: hold voided" if voided else
                     f"Release {_money(amount, cur)} to the seller",
                     rp.get("note") or "", {}, seen)
        step["refers_to"] = [{"field": "authorization_id", "id": auth_id,
                              "ok": auth_id in ([p.get("authorization_id")] + (p.get("previous_authorization_ids") or [])),
                              "step": "paypal authorization"}]
        if voided:
            step["paypal"].append({"kind": "void", "label": "Authorization voided: all money back", "id": auth_id,
                                   "request_id": f"void-{auth_id}", "link": _paypal_link(auth_id)})
        elif p.get("capture_id"):
            step["paypal"].append({"kind": "capture", "label": f"Captured {_money(p.get('captured'), cur)}",
                                   "id": p["capture_id"], "amount": p.get("captured"),
                                   "request_id": f"capture-{auth_id}-{amount:.2f}", "link": _paypal_link(p["capture_id"])})
        steps.append(step)
        mandates.append(release)

    hashes = [s["hash"] for s in steps]
    return {
        "order_id": order["id"],
        "status": order.get("status"),
        "currency": cur,
        "generated_at": int(time.time()),
        "paypal_environment": "mock" if config.PAYPAL_MODE == "mock" else
                              ("sandbox" if "sandbox" in config.PAYPAL_BASE else "live"),
        "fingerprint": receipt_fingerprint(hashes) if hashes else None,
        "steps": steps,
        "checks": _checks(order, steps),
        # Everything needed to check the receipt offline (scripts/verify_receipt.py).
        "mandates": mandates,
        "public_keys": {s["signer"]: s["public_key"] for s in steps if s.get("public_key")},
        "release_missing": bool(p.get("capture_id") or p.get("voided")) and not release,
    }


def _check(label: str, ok: bool | None, detail: str) -> dict:
    return {"label": label, "ok": ok, "detail": detail}


def _checks(order: dict, steps: list[dict]) -> list[dict]:
    m = order.get("mandate") or {}
    cur = m.get("currency", "USD")
    p = order.get("paypal") or {}
    intent, cart = order.get("intent_mandate"), order.get("cart_mandate")
    payment, release = order.get("payment_mandate"), order.get("release_mandate")
    out = []
    if steps:
        bad = [s["title"] for s in steps if not s["verified"]]
        out.append(_check("Every signature checks out", not bad,
                          "Checked against each agent's published key." if not bad else f"Fails: {', '.join(bad)}"))
        broken = [s["title"] for s in steps for r in s["refers_to"] if not r["ok"]]
        out.append(_check("Each step points to the one before it", not broken,
                          "Unbroken chain from your rules to the money." if not broken
                          else f"Broken link at: {', '.join(broken)}"))
    if intent and cart:
        total, budget = float(cart["payload"]["total"]), float(intent["payload"]["budget_max"])
        out.append(_check("The price stayed inside your budget", total <= budget + 0.005,
                          f"{_money(total, cur)} signed against a {_money(budget, cur)} budget."))
    if cart and payment:
        same = (payment["payload"].get("cart_mandate_id") == cart["id"]
                and abs(float(payment["payload"]["total"]) - float(cart["payload"]["total"])) < 0.005)
        out.append(_check("You paid for exactly the cart the seller signed", same,
                          f"Payment names {cart['id']} for {_money(payment['payload']['total'], cur)}."))
        pp = payment["payload"]
        if pp.get("user_present"):
            out.append(_check("Your approval rule was followed", True, "You approved this payment yourself."))
        else:
            limit = float(pp.get("approval_threshold") or (intent or {}).get("payload", {}).get("approval_threshold") or 0)
            out.append(_check("Your approval rule was followed", float(pp["total"]) <= limit + 0.005,
                              f"Paid without asking: {_money(pp['total'], cur)} is under your {_money(limit, cur)} limit."))
    if payment and p.get("authorized_amount") is not None:
        held, signed = float(p["authorized_amount"]), float(payment["payload"]["total"])
        out.append(_check("PayPal held exactly the signed amount", abs(held - signed) < 0.005,
                          f"Held {_money(held, cur)}, signed {_money(signed, cur)}."))
    amendments = [a for a in order.get("cart_amendments") or [] if a]
    if amendments and cart:
        same = all(abs(float(a["payload"].get("total") or 0) - float(cart["payload"]["total"])) < 0.005
                   for a in amendments)
        out.append(_check("Date changes didn't change the price", same,
                          f"{len(amendments)} signed change{'s' if len(amendments) > 1 else ''}, same total."))
    if release:
        want = float(release["payload"].get("capture_amount") or 0)
        held = float(p.get("authorized_amount") or 0)
        if p.get("voided") or want < 0.01:
            out.append(_check("The seller got only what you released", bool(p.get("voided")),
                              f"Nothing captured; all {_money(held, cur)} released back to you."))
        else:
            got = float(p.get("captured") or 0)
            out.append(_check("The seller got only what you released",
                              abs(got - want) < 0.005 and got <= held + 0.005,
                              f"Released {_money(want, cur)}, captured {_money(got, cur)} of {_money(held, cur)} held."))
    elif payment and order.get("status") in ("held", "resolving", "escalated"):
        out.append(_check("The seller got only what you released", None,
                          "Not paid yet: the money is held until you confirm delivery."))
    return out