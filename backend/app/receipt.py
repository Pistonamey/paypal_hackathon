"""The receipt: every signed AP2 document of an order, in order, tied to what PayPal did.

For an order made with AP2 v0.2 (see mandates.py) the steps are:

  1. open checkout mandate     your rules: the exact item and quantity, only from these sellers
  2. open payment mandate      what your agent may pay on its own, to whom, with what
  3. the seller's checkout     its exact offer, as a signed Checkout JWT
  4. checkout mandate          approval of that exact checkout (by your agent under 1, or by you)
  5. payment mandate           approval to pay it (by your agent under 2, or by you), plus the PayPal hold
  6. checkout receipt          the seller's signed confirmation, pointing at 4
  7. payment receipt           the seller's signed confirmation for PayPal's hold, pointing at 5
  then Procuro's own signed extensions: a moved delivery date, and the release that captured or voided.

Each step is checked here with Google's AP2 SDK (signatures, SD-JWT delegation, nonce and audience,
the hashes that tie one document to the next) and gets a fingerprint: SHA-256 of the exact token.
The receipt's own fingerprint is SHA-256 over the step fingerprints in order.

The JSON download carries every token, the public keys and how to check each step, so
scripts/verify_receipt.py can re-check it with no access to Procuro. `paypal_check` then asks PayPal
for its own record of the hold and the capture and compares them with the signed amounts.

Orders from before AP2 v0.2 are built by receipt_legacy.py.
"""
import hashlib
import time

from . import a2a, config, mandates, paypal, receipt_legacy

SANDBOX_ACTIVITY = "https://www.sandbox.paypal.com/activity/payment/"
AP2 = "AP2 v0.2"
EXTENSION = "Procuro extension"


def receipt_fingerprint(hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()


def _money(v, cur="USD") -> str:
    try:
        return f"${float(v):,.2f}" if cur == "USD" else f"{float(v):,.2f} {cur}"
    except (TypeError, ValueError):
        return "?"


def _cents(v, cur="USD") -> str:
    return _money((v or 0) / 100, cur)


def _paypal_link(record_id: str | None) -> str | None:
    if not record_id or config.PAYPAL_MODE == "mock" or "sandbox" not in config.PAYPAL_BASE:
        return None
    return SANDBOX_ACTIVITY + record_id


def _when(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "?"


class _Steps:
    """Collects steps; each verifies itself and records what it points to."""

    def __init__(self):
        self.steps: list[dict] = []

    def add(self, *, id: str, type: str, standard: str, fmt: str, title: str, summary: str, signed_by: str,
            key_from: str, token: str, verify, verify_spec: dict, decoded=None, refs=(), paypal=()) -> dict:
        try:
            out = verify()
            ok, note = True, ""
            if decoded is None:
                decoded = out
        except Exception as exc:  # noqa: BLE001 - any failure is shown, not hidden
            ok, note = False, str(exc)[:300]
        step = {
            "id": id, "type": type, "standard": standard, "format": fmt, "title": title, "summary": summary,
            "signed_by": signed_by, "key_from": key_from, "verified": ok, "verify_error": note,
            "hash": mandates.sha256_b64url(token), "token": token, "decoded": _plain(decoded),
            "refers_to": [{"field": f, "id": rid, "ok": bool(good), "label": label} for f, rid, good, label in refs],
            "paypal": list(paypal), "verify": verify_spec,
        }
        self.steps.append(step)
        return step


def _plain(x):
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json", exclude_none=True)
    if isinstance(x, list):
        return [_plain(i) for i in x]
    return x


def _chain_payloads(chain: str, presentation: dict, nonce_key: str):
    """Verify a closed mandate: an SD-JWT chain on top of an open mandate (agent signed) or a single
    SD-JWT signed by the Trusted Surface (owner approved). Returns the verified payloads."""
    client = mandates.MandateClient()
    surface = mandates.key(mandates.SURFACE)
    if presentation.get("mode") == mandates.HUMAN_NOT_PRESENT:
        return client.verify(token=chain, key_or_provider=lambda _t: surface,
                             expected_aud=presentation.get("audience"), expected_nonce=presentation.get(nonce_key))
    return None


def build(order: dict) -> dict:
    ap = order.get("ap2")
    if not ap:
        out = receipt_legacy.build(order)
        out["standard"] = "earlier format (before AP2 v0.2)"
        return out

    m = order.get("mandate") or {}
    cur = m.get("currency", "USD")
    p = order.get("paypal") or {}
    w = order.get("winner") or {}
    sid = w.get("seller_id") or (ap.get("checkout") or {}).get("seller_id")
    seller = (order.get("sellers") or {}).get(sid or "", {})
    seller_name = seller.get("name") or sid or "The seller"
    seller_jwk = mandates.jwk_from(seller.get("jwk"))
    surface_label = "Procuro, your trusted surface, for you"
    surface_from = "the trusted-surface key in the buying agent's A2A card"
    S = _Steps()
    client = mandates.MandateClient()

    # 1 + 2: your rules
    oc_hash = ap["open_checkout_hash"]
    names = ", ".join(x["name"] for x in ap.get("merchants") or [])
    oc_payload = _safe(lambda: mandates.verify_open_checkout(ap["open_checkout"]))
    S.add(
        id=oc_hash, type="mandate.checkout.open.1", standard=AP2, fmt="SD-JWT, ES256", title="Your rules",
        summary=f"{m.get('quantity')} × {m.get('item')}, only from {names}. Valid until "
                f"{_when(oc_payload.exp if oc_payload else None)}.",
        signed_by=surface_label, key_from=surface_from, token=ap["open_checkout"],
        verify=lambda: mandates.verify_open_checkout(ap["open_checkout"]),
        verify_spec={"how": "sd-jwt", "key": "trusted_surface", "payload_type": "OpenCheckoutMandate"})
    op_payload = _safe(lambda: client.verify(token=ap["open_payment"], key_or_provider=mandates.key(mandates.SURFACE),
                                             payload_type=mandates.OpenPaymentMandate).mandate_payload)
    ref = next((c.conditional_transaction_id for c in (op_payload.constraints if op_payload else [])
                if getattr(c, "type", "") == "payment.reference"), None)
    S.add(
        id=mandates.sd_hash(ap["open_payment"]), type="mandate.payment.open.1", standard=AP2, fmt="SD-JWT, ES256",
        title="What your agent may pay on its own",
        summary=f"Up to {_money(m.get('approval_threshold'), cur)} without asking you, only to {names}, only with "
                "your saved PayPal, only for the checkout in step 1.",
        signed_by=surface_label, key_from=surface_from, token=ap["open_payment"],
        verify=lambda: client.verify(token=ap["open_payment"], key_or_provider=mandates.key(mandates.SURFACE),
                                     payload_type=mandates.OpenPaymentMandate).mandate_payload,
        verify_spec={"how": "sd-jwt", "key": "trusted_surface", "payload_type": "OpenPaymentMandate"},
        refs=[("payment.reference", ref, ref == oc_hash, "step 1")])

    checkout = ap.get("checkout")
    checkout_payload = None
    if checkout:
        checkout_payload = checkout.get("payload") or {}
        line = (checkout_payload.get("line_items") or [{}])[0]
        total = next((t["amount"] for t in checkout_payload.get("totals", []) if t["type"] == "total"), None)
        S.add(
            id=checkout["checkout_hash"], type="Checkout JWT", standard=AP2, fmt="JWT, ES256",
            title=f"{seller_name}'s signed checkout",
            summary=f"{line.get('quantity')} × {(line.get('item') or {}).get('title')} at "
                    f"{_cents((line.get('item') or {}).get('price'), cur)} = {_cents(total, cur)}, delivered "
                    f"{checkout_payload.get('delivery_date')}.",
            signed_by=f"{seller_name}'s agent", key_from=f"{seller_name}'s A2A card", token=checkout["checkout_jwt"],
            verify=lambda: mandates.verify_receipt(checkout["checkout_jwt"], seller_jwk),
            verify_spec={"how": "jwt", "key": f"seller:{sid}"})

    for r in ap.get("refusals") or []:
        body = _safe(lambda r=r: mandates.jwt_payload(r["receipt"])) or {}
        S.add(
            id=mandates.sha256_b64url(r["receipt"]), type="Error receipt", standard=AP2, fmt="JWT, ES256",
            title=f"{seller_name} refused a payment mandate",
            summary=f"{r.get('error_description') or body.get('error_description')} "
                    f"({'signed by your agent on its own' if r.get('mode') == mandates.HUMAN_NOT_PRESENT else 'approved by you'}).",
            signed_by=f"{seller_name}'s agent", key_from=f"{seller_name}'s A2A card", token=r["receipt"],
            verify=lambda r=r: mandates.verify_receipt(r["receipt"], seller_jwk),
            verify_spec={"how": "jwt", "key": f"seller:{sid}"},
            refs=[("reference", body.get("reference"),
                   body.get("reference") == mandates.sha256_b64url(mandates.leaf(r["payment_mandate"])),
                   "the refused payment mandate")])

    pres = ap.get("presentation") or {}
    agent = pres.get("mode") == mandates.HUMAN_NOT_PRESENT
    approver = ("Your buying agent, on its own under steps 1 and 2" if agent
                else "Procuro, your trusted surface, when you tapped Approve")
    approver_key = ("the agent key named in your open mandate (cnf)" if agent else surface_from)
    steps_ids = {s["type"]: s["id"] for s in S.steps}

    if ap.get("checkout_mandate") and checkout:
        chain = ap["checkout_mandate"]

        def v_checkout():
            closed = mandates.verify_checkout_mandate(chain, pres, expected_checkout_hash=checkout["checkout_hash"])
            return closed.model_dump(mode="json", exclude={"checkout_jwt"})
        closed_ok = _safe(v_checkout) is not None
        refs = [("checkout_hash", checkout["checkout_hash"], closed_ok, "step 3")]
        if agent:
            refs.append(("sd_hash (delegation)", oc_hash, closed_ok, "step 1"))
        S.add(
            id=mandates.sha256_b64url(mandates.leaf(chain)), type="mandate.checkout.1", standard=AP2,
            fmt="SD-JWT chain, ES256" if agent else "SD-JWT, ES256", title="Checkout approved",
            summary=f"Approves exactly {seller_name}'s checkout in step 3 (by its hash).",
            signed_by=approver, key_from=approver_key, token=chain, verify=v_checkout,
            verify_spec={"how": "sd-jwt-chain" if agent else "sd-jwt", "key": "trusted_surface",
                         "payload_type": "CheckoutMandate", "aud": pres.get("audience"),
                         "nonce": pres.get("checkout_nonce")},
            refs=refs)

    if ap.get("payment_mandate") and checkout:
        chain = ap["payment_mandate"]

        def v_payment():
            closed = mandates.verify_payment_mandate(
                chain, pres, expected_checkout_hash=checkout["checkout_hash"],
                open_checkout_chain=ap.get("checkout_mandate") if agent else None)
            return closed
        closed = _safe(v_payment)
        refs = [("transaction_id", checkout["checkout_hash"], closed is not None, "step 3")]
        if agent:
            refs.append(("sd_hash (delegation)", steps_ids.get("mandate.payment.open.1"), closed is not None,
                         "step 2"))
        pay_records = []
        if p.get("order_id"):
            pay_records.append({"kind": "order", "label": "PayPal order (intent AUTHORIZE)", "id": p["order_id"],
                                "request_id": f"create-{order['id']}{'-vault' if p.get('saved_paypal') else ''}"})
        for old in p.get("previous_authorization_ids") or []:
            pay_records.append({"kind": "authorization", "label": "First hold (renewed later)", "id": old,
                                "link": _paypal_link(old)})
        if p.get("authorization_id"):
            first = not p.get("previous_authorization_ids")
            pay_records.append({
                "kind": "authorization",
                "label": f"Hold of {_money(p.get('authorized_amount'), cur)}" + ("" if first else " (renewed)"),
                "id": p["authorization_id"], "amount": p.get("authorized_amount"),
                "request_id": (f"auth-{p.get('order_id')}" if first and not p.get("saved_paypal") and p.get("order_id")
                               else None if first else f"reauth-{(p.get('previous_authorization_ids') or [''])[-1]}"),
                "link": _paypal_link(p["authorization_id"])})
        amount = closed.payment_amount.amount if closed else None
        instrument = closed.payment_instrument.description if closed else "?"
        S.add(
            id=mandates.sha256_b64url(mandates.leaf(chain)), type="mandate.payment.1", standard=AP2,
            fmt="SD-JWT chain, ES256" if agent else "SD-JWT, ES256", title="Payment approved",
            summary=(f"Your agent paid {_cents(amount, cur)} on its own, within your "
                     f"{_money(m.get('approval_threshold'), cur)} limit. " if agent else
                     f"You approved paying {_cents(amount, cur)}. ") + f"With: {instrument}.",
            signed_by=approver, key_from=approver_key, token=chain,
            verify=lambda: v_payment(), decoded=closed,
            verify_spec={"how": "sd-jwt-chain" if agent else "sd-jwt", "key": "trusted_surface",
                         "payload_type": "PaymentMandate", "aud": pres.get("audience"),
                         "nonce": pres.get("payment_nonce")},
            refs=refs, paypal=pay_records)

    for name, title, chain_key in (("checkout_receipt", f"{seller_name} confirmed the order", "checkout_mandate"),
                                   ("payment_receipt", f"{seller_name} confirmed PayPal's hold", "payment_mandate")):
        token = ap.get(name)
        if not token or not ap.get(chain_key):
            continue
        body = _safe(lambda token=token: mandates.jwt_payload(token)) or {}
        target = mandates.sha256_b64url(mandates.leaf(ap[chain_key]))
        summary = (f"{body.get('status')}. PayPal order {body.get('order_id')}." if name == "checkout_receipt" else
                   f"{body.get('status')}. Payment {body.get('payment_id')} (PayPal authorization), PayPal order "
                   f"{body.get('psp_confirmation_id')}.")
        S.add(
            id=mandates.sha256_b64url(token), type=name.replace("_", " ").capitalize(), standard=AP2,
            fmt="JWT, ES256", title=title, summary=summary, signed_by=f"{seller_name}'s agent",
            key_from=f"{seller_name}'s A2A card", token=token,
            verify=lambda token=token: mandates.verify_receipt(token, seller_jwk),
            verify_spec={"how": "jwt", "key": f"seller:{sid}"},
            refs=[("reference", body.get("reference"), body.get("reference") == target,
                   "step " + str(next((i + 1 for i, s in enumerate(S.steps) if s["id"] == target), "?")))])

    for token in ap.get("amendments") or []:
        body = _safe(lambda token=token: mandates.jwt_payload(token)) or {}
        S.add(
            id=mandates.sha256_b64url(token), type="checkout amendment", standard=EXTENSION, fmt="JWT, ES256",
            title=f"{seller_name} moved the delivery",
            summary=f"{body.get('previous_delivery_date')} → {body.get('delivery_date')}. Same price: "
                    f"{_money(body.get('total'), cur)}.",
            signed_by=f"{seller_name}'s agent", key_from=f"{seller_name}'s A2A card", token=token,
            verify=lambda token=token: mandates.verify_extension(token, seller_jwk, mandates.AMENDMENT_TYP),
            verify_spec={"how": "jwt", "key": f"seller:{sid}", "typ": mandates.AMENDMENT_TYP},
            refs=[("checkout_hash", body.get("checkout_hash"),
                   bool(checkout) and body.get("checkout_hash") == checkout["checkout_hash"], "step 3")])

    if ap.get("release"):
        token = ap["release"]
        body = _safe(lambda: mandates.jwt_payload(token)) or {}
        amount = float(body.get("capture_amount") or 0)
        voided = p.get("voided") or amount < 0.01
        auth_id = body.get("authorization_id")
        records = []
        if voided:
            records.append({"kind": "void", "label": "Authorization voided: all money back", "id": auth_id,
                            "request_id": f"void-{auth_id}", "link": _paypal_link(auth_id)})
        elif p.get("capture_id"):
            records.append({"kind": "capture", "label": f"Captured {_money(p.get('captured'), cur)}",
                            "id": p["capture_id"], "amount": p.get("captured"),
                            "request_id": f"capture-{auth_id}-{amount:.2f}", "link": _paypal_link(p["capture_id"])})
        pay_id = mandates.sha256_b64url(mandates.leaf(ap["payment_mandate"])) if ap.get("payment_mandate") else None
        S.add(
            id=mandates.sha256_b64url(token), type="release", standard=EXTENSION, fmt="JWT, ES256",
            title="Nothing paid: hold voided" if voided else f"Release {_money(amount, cur)} to the seller",
            summary=body.get("note") or "", signed_by="Your buying agent",
            key_from="the buying agent's A2A card", token=token,
            verify=lambda: mandates.verify_extension(token, mandates.key(mandates.BUYER), mandates.RELEASE_TYP),
            verify_spec={"how": "jwt", "key": "buyer_agent", "typ": mandates.RELEASE_TYP},
            refs=[("payment_mandate_ref", body.get("payment_mandate_ref"),
                   bool(pay_id) and body.get("payment_mandate_ref") == pay_id, "the payment approval"),
                  ("authorization_id", auth_id,
                   auth_id in ([p.get("authorization_id")] + (p.get("previous_authorization_ids") or [])),
                   "the PayPal hold")],
            paypal=records)

    steps = S.steps
    hashes = [s["hash"] for s in steps]
    return {
        "order_id": order["id"], "status": order.get("status"), "currency": cur, "standard": AP2,
        "mode": pres.get("mode"), "generated_at": int(time.time()),
        "paypal_environment": "mock" if config.PAYPAL_MODE == "mock" else
                              ("sandbox" if "sandbox" in config.PAYPAL_BASE else "live"),
        "fingerprint": receipt_fingerprint(hashes) if hashes else None,
        "steps": steps,
        "checks": _checks(order, steps, checkout_payload),
        # Everything needed to re-check this offline (scripts/verify_receipt.py).
        "public_keys": {"trusted_surface": mandates.public_jwk(mandates.SURFACE),
                        "buyer_agent": mandates.public_jwk(mandates.BUYER),
                        **({f"seller:{sid}": seller.get("jwk")} if sid and seller.get("jwk") else {})},
        "release_missing": False,
    }


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _check(label: str, ok: bool | None, detail: str) -> dict:
    return {"label": label, "ok": ok, "detail": detail}


def _checks(order: dict, steps: list[dict], checkout_payload: dict | None) -> list[dict]:
    m = order.get("mandate") or {}
    cur = m.get("currency", "USD")
    ap = order["ap2"]
    p = order.get("paypal") or {}
    out = []
    bad = [s["title"] for s in steps if not s["verified"]]
    out.append(_check("Every signature checks out", not bad,
                      "Checked with Google's AP2 SDK against each agent's published key." if not bad
                      else f"Fails: {', '.join(bad)}"))
    broken = [s["title"] for s in steps for r in s["refers_to"] if not r["ok"]]
    out.append(_check("Each document points to the one before it", not broken,
                      "Unbroken chain from your rules to the money." if not broken
                      else f"Broken link at: {', '.join(broken)}"))
    if checkout_payload:
        open_checkout = _safe(lambda: mandates.verify_open_checkout(ap["open_checkout"]))
        problems = (mandates.checkout_violations(open_checkout, checkout_payload) if open_checkout
                    else ["your rules don't verify"])
        line = (checkout_payload.get("line_items") or [{}])[0]
        if line.get("quantity") != m.get("quantity"):
            problems.append(f"quantity {line.get('quantity')} instead of {m.get('quantity')}")
        out.append(_check("The seller's checkout fits your rules", not problems,
                          "Right item, right quantity, an allowed seller (AP2 constraint check)." if not problems
                          else "; ".join(problems)))
        total = next((t["amount"] for t in checkout_payload.get("totals", []) if t["type"] == "total"), 0) / 100
        out.append(_check("The price stayed inside your budget", total <= float(m.get("budget_max") or 0) + 0.005,
                          f"{_money(total, cur)} against your {_money(m.get('budget_max'), cur)} budget."))
    pay = next((s for s in steps if s["type"] == "mandate.payment.1"), None)
    if pay:
        decoded = pay.get("decoded") or {}
        amount = ((decoded.get("payment_amount") or {}).get("amount") or 0) / 100
        out.append(_check("You paid for exactly the checkout the seller signed", pay["verified"],
                          f"The approvals carry the checkout's hash, for {_money(amount, cur)}." if pay["verified"]
                          else pay.get("verify_error") or "doesn't match"))
        if ap.get("mode") == mandates.HUMAN_NOT_PRESENT:
            out.append(_check("Your approval rule was followed", pay["verified"],
                              f"Your agent paid {_money(amount, cur)} without asking, under your "
                              f"{_money(m.get('approval_threshold'), cur)} limit; the seller checked it against your "
                              "open payment mandate."))
        else:
            out.append(_check("Your approval rule was followed", True, "You approved this payment yourself."))
        receipts = [s for s in steps if s["type"] in ("Checkout receipt", "Payment receipt")]
        if receipts:
            ok = all(s["verified"] and all(r["ok"] for r in s["refers_to"]) for s in receipts)
            out.append(_check("The seller's receipts point at your approvals", ok,
                              "Each receipt names the hash of the exact mandate it answers."))
        if p.get("authorized_amount") is not None:
            held = float(p["authorized_amount"])
            out.append(_check("PayPal held exactly the signed amount", abs(held - amount) < 0.005,
                              f"Held {_money(held, cur)}, signed {_money(amount, cur)}."))
    if ap.get("amendments") and checkout_payload:
        total = next((t["amount"] for t in checkout_payload.get("totals", []) if t["type"] == "total"), 0) / 100
        same = all(abs(float((_safe(lambda t=t: mandates.jwt_payload(t)) or {}).get("total") or 0) - total) < 0.005
                   for t in ap["amendments"])
        n = len(ap["amendments"])
        out.append(_check("Date changes didn't change the price", same,
                          f"{n} signed change{'s' if n > 1 else ''}, same total."))
    if ap.get("release"):
        body = _safe(lambda: mandates.jwt_payload(ap["release"])) or {}
        want = float(body.get("capture_amount") or 0)
        held = float(p.get("authorized_amount") or 0)
        if p.get("voided") or want < 0.01:
            out.append(_check("The seller got only what you released", bool(p.get("voided")),
                              f"Nothing captured; all {_money(held, cur)} released back to you."))
        else:
            got = float(p.get("captured") or 0)
            out.append(_check("The seller got only what you released",
                              abs(got - want) < 0.005 and got <= held + 0.005,
                              f"Released {_money(want, cur)}, captured {_money(got, cur)} of {_money(held, cur)} held."))
    elif pay and order.get("status") in ("held", "resolving", "escalated"):
        out.append(_check("The seller got only what you released", None,
                          "Not paid yet: the money is held until you confirm delivery."))
    return out


def _signed_amounts(order: dict) -> tuple[float, float | None]:
    """(the amount the payment approval signed, the amount the release signed)."""
    ap = order.get("ap2")
    if ap:
        pres = ap.get("presentation") or {}
        closed = _safe(lambda: mandates.verify_payment_mandate(
            ap["payment_mandate"], pres, expected_checkout_hash=ap["checkout"]["checkout_hash"],
            open_checkout_chain=ap.get("checkout_mandate") if pres.get("mode") == mandates.HUMAN_NOT_PRESENT else None))
        signed = closed.payment_amount.amount / 100 if closed else 0.0
        release = _safe(lambda: mandates.jwt_payload(ap["release"])) if ap.get("release") else None
        return signed, float(release["capture_amount"]) if release else None
    payment = (order.get("payment_mandate") or {}).get("payload") or {}
    release = (order.get("release_mandate") or {}).get("payload")
    return float(payment.get("total") or 0), float(release.get("capture_amount") or 0) if release else None


class _OutsideSeller:
    """An outside seller is paid into its own PayPal account, which Procuro can't read. Ask the seller agent
    over A2A for PayPal's records of this order (the `payment_status` action)."""

    def __init__(self, order_id: str, seller: dict):
        self.order_id, self.url, self._answer = order_id, seller["url"], None

    async def _status(self) -> dict:
        if self._answer is None:
            try:
                self._answer, _ = await a2a.send(self.url, {"action": "payment_status", "order_id": self.order_id},
                                                 "What does PayPal show for this order?", self.order_id)
            except a2a.A2AError as exc:
                raise paypal.PayPalError(f"the seller agent couldn't report PayPal's records: {exc}") from None
        return self._answer

    async def get_authorization(self, _authorization_id: str) -> dict:
        auth = (await self._status()).get("authorization")
        if not auth:
            raise paypal.PayPalError("the seller agent reports no PayPal hold for this order")
        return auth

    async def get_capture(self, _capture_id: str) -> dict:
        cap = (await self._status()).get("capture")
        if not cap:
            raise paypal.PayPalError("the seller agent reports no PayPal capture for this order")
        return cap


async def paypal_check(order: dict) -> dict:
    """Ask PayPal for its own record of the hold and the capture and compare with the signed steps."""
    p = order.get("paypal") or {}
    w = order.get("winner") or {}
    cur = (order.get("mandate") or {}).get("currency", "USD")
    if not p.get("authorization_id") or not w.get("seller_id"):
        return {"items": [], "note": "Nothing at PayPal yet."}
    seller = (order.get("sellers") or {}).get(w["seller_id"], {})
    client = _OutsideSeller(order["id"], seller) if seller.get("outside") else paypal.client_for(w["seller_id"])
    signed, released = _signed_amounts(order)
    items = []
    try:
        auth = await client.get_authorization(p["authorization_id"])
        items.append({
            "label": "PayPal's record of the hold", "id": auth["id"], "status": auth["status"],
            "amount": auth["amount"], "ok": auth["amount"] is not None and abs(auth["amount"] - signed) < 0.005,
            "detail": f"PayPal says {auth['status']}, {_money(auth['amount'], cur)}; the payment approval signed "
                      f"{_money(signed, cur)}."})
        if p.get("capture_id"):
            cap = await client.get_capture(p["capture_id"])
            want = released if released is not None else float(p.get("captured") or 0)
            linked = cap.get("authorization_id") in (None, p["authorization_id"])
            items.append({
                "label": "PayPal's record of the payment", "id": cap["id"], "status": cap["status"],
                "amount": cap["amount"],
                "ok": cap["amount"] is not None and abs(cap["amount"] - want) < 0.005 and linked,
                "detail": f"PayPal captured {_money(cap['amount'], cur)} from this hold; the release signed "
                          f"{_money(want, cur)}." + ("" if linked else " It came from a different hold.")})
        elif p.get("voided"):
            items.append({"label": "Hold voided at PayPal", "id": auth["id"], "status": auth["status"],
                          "amount": 0, "ok": auth["status"] == "VOIDED",
                          "detail": f"PayPal shows the hold as {auth['status']}."})
    except paypal.PayPalError as exc:
        return {"items": items, "error": str(exc)}
    return {"items": items, "checked_at": int(time.time()),
            "environment": "mock" if config.PAYPAL_MODE == "mock" else "sandbox",
            "source": "the seller agent's own PayPal account, reported over A2A" if seller.get("outside") else "PayPal"}