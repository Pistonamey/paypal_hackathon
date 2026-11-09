"""Share a bulk order with a partner business, billed through PayPal's official Agent Toolkit.

Sam buys 1,000 coffee cups at a negotiated bulk price and the bakery next door takes 300 of them. Procuro
bills the bakery for its share with a real PayPal invoice: itemized (what, how many, the negotiated unit
price, which order and seller it came from), sent by PayPal with a pay link, plus a QR code to pay on
the spot. When the bakery pays, the order shows it.

The invoices go through PayPal's Agent Toolkit (`paypal_agent_toolkit`, vendored unchanged in
backend/vendor, see VENDORED.md): `create_invoice`, `send_invoice`, `generate_invoice_qr_code` and
`get_invoice`, called through `PayPalAPI.run(method, params)` exactly as the toolkit's own agent
framework adapters call them. The toolkit is synchronous, so calls run in a worker thread.

In this sandbox the invoices come from Procuro's PayPal app account. In production they'd be issued
from Sam's own PayPal Business account.
"""
import asyncio
import json
import os
import sys
import time
import uuid

from . import config

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from paypal_agent_toolkit.shared.api import PayPalAPI  # noqa: E402
from paypal_agent_toolkit.shared.configuration import Context  # noqa: E402

SHAREABLE = ("held", "completed", "settled")
_api: PayPalAPI | None = None
_base_override: str | None = None  # tests point the toolkit at a local stand-in for PayPal


class InvoiceError(Exception):
    pass


def _toolkit() -> PayPalAPI:
    global _api
    if _api is None:
        if not (config.PAYPAL_CLIENT_ID and config.PAYPAL_CLIENT_SECRET):
            raise InvoiceError("PayPal sandbox credentials are missing, so invoices can't be sent.")
        _api = PayPalAPI(client_id=config.PAYPAL_CLIENT_ID, secret=config.PAYPAL_CLIENT_SECRET,
                         context=Context(sandbox="sandbox" in config.PAYPAL_BASE, source="procuro"))
        if _base_override:
            _api._paypal_client.base_url = _base_override
    return _api


async def _run(method: str, params: dict):
    def call():
        try:
            out = _toolkit().run(method, params)
        except InvoiceError:
            raise
        except Exception as exc:  # requests errors, pydantic validation, PayPal 4xx
            raise InvoiceError(f"PayPal {method} failed: {exc}") from exc
        if isinstance(out, str):
            try:
                return json.loads(out) if out else {}
            except ValueError:
                return {"raw": out}
        return out or {}
    return await asyncio.to_thread(call)


def _invoice_id(created: dict) -> str:
    if created.get("id"):
        return created["id"]
    href = created.get("href") or ""
    if "/invoices/" in href:
        return href.rstrip("/").rsplit("/", 1)[-1]
    raise InvoiceError(f"PayPal created an invoice but returned no id: {created}")


def shared_quantity(order: dict) -> int:
    return sum(s["quantity"] for s in order.get("shares", []) if s.get("status") != "CANCELLED")


async def share(order: dict, *, partner_name: str, partner_email: str, quantity: int, note: str = "") -> dict:
    """Bill a partner for `quantity` units of this order at the negotiated unit price."""
    if order.get("status") not in SHAREABLE:
        raise ValueError("You can share an order once it's paid for or held.")
    w, m = order["winner"], order["mandate"]
    partner_name, partner_email = partner_name.strip(), partner_email.strip()
    if not partner_name or "@" not in partner_email:
        raise ValueError("Add the partner's business name and email.")
    available = m["quantity"] - shared_quantity(order) - 1  # Sam keeps at least one
    if available < 1:
        raise ValueError("Nothing left to share on this order.")
    if not 1 <= quantity <= available:
        raise ValueError(f"Share between 1 and {available} units; the rest stays with you.")

    unit, cur = float(w["unit_price"]), m["currency"]
    amount = round(unit * quantity, 2)
    reference = f"{order['id']}-{len(order.get('shares', [])) + 1}"
    if config.PAYPAL_MODE == "mock":
        iid = f"INV2-MOCK-{uuid.uuid4().hex[:8].upper()}"
        record = {"invoice_id": iid, "pay_url": None, "qr_png": None, "status": "SENT", "mock": True}
    else:
        created = await _run("create_invoice", {
            "currency_code": cur,
            "reference": reference,
            "invoice_date": time.strftime("%Y-%m-%d"),
            "note": (note or f"Your share of a bulk order of {m['quantity']} {m['item']} from {w['seller_name']}, "
                     "negotiated and held through PayPal by Procuro.")[:4000],
            "invoicer_business_name": "Sam's Coffee (via Procuro)",
            "primary_recipients": [{"billing_info": {"business_name": partner_name, "email_address": partner_email}}],
            "items": [{
                "name": f"{m['item']} (share of order {order['id']})"[:200],
                "description": f"{quantity} of {m['quantity']} from {w['seller_name']} at the negotiated "
                               f"{unit:.2f} {cur} each, delivered by {w.get('delivery_date')}"[:1000],
                "quantity": str(quantity),
                "unit_amount": {"currency_code": cur, "value": f"{unit:.2f}"},
                "unit_of_measure": "QUANTITY",
            }],
        })
        iid = _invoice_id(created)
        sent = await _run("send_invoice", {"invoice_id": iid, "send_to_recipient": True,
                                            "note": "Pay your share by PayPal. Thank you!"})
        pay_url = sent.get("href") if isinstance(sent, dict) else None
        qr = None
        try:
            got = await _run("generate_invoice_qr_code", {"invoice_id": iid, "width": 300, "height": 300})
            qr = got.get("raw") if isinstance(got, dict) else None  # base64 PNG when PayPal returns one
        except InvoiceError:
            pass  # the app draws its own QR code from the pay link
        if not pay_url:
            info = await _run("get_invoice", {"invoice_id": iid})
            pay_url = ((info.get("detail") or {}).get("metadata") or {}).get("recipient_view_url")
        record = {"invoice_id": iid, "pay_url": pay_url, "qr_png": qr, "status": "SENT"}

    record.update({"partner_name": partner_name, "partner_email": partner_email, "quantity": quantity,
                   "unit_price": unit, "amount": amount, "currency": cur, "created_at": time.time()})
    order.setdefault("shares", []).append(record)
    return record


async def refresh(order: dict) -> list[dict]:
    """Ask PayPal which shares have been paid."""
    for s in order.get("shares", []):
        if s.get("mock") or s.get("status") in ("PAID", "CANCELLED", "REFUNDED"):
            continue
        info = await _run("get_invoice", {"invoice_id": s["invoice_id"]})
        if info.get("status"):
            if info["status"] == "PAID" and s.get("status") != "PAID":
                s["paid_at"] = time.time()
            s["status"] = info["status"]
        view = ((info.get("detail") or {}).get("metadata") or {}).get("recipient_view_url")
        if view and not s.get("pay_url"):
            s["pay_url"] = view
    return order.get("shares", [])
