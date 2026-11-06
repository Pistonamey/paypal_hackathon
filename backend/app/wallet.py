"""The owner's PayPal, saved once (PayPal Vault v3).

The owner approves once on PayPal's page. PayPal returns a payment token that Procuro keeps; with it a
seller can authorize (hold) a payment without sending the owner back to PayPal. Procuro uses it in two
ways, both inside the owner's signed mandate:

  - auto-pay: a deal at or under the order's "ask me above" limit is held straight away, as long as
    this month's total stays under the owner's monthly cap. Nobody has to be there.
  - one-tap approve: a deal above the limit still needs the owner, but approving is one tap in the
    app instead of a trip to PayPal.

A hold is still only a hold: nothing is taken until delivery is confirmed. The owner can turn auto-pay
off, change the cap, or disconnect (which deletes the token at PayPal) at any time.
"""
import datetime as dt
import time

from . import config, paypal
from .store import store

DOC = ("settings", "paypal_wallet")
DEFAULT_MONTHLY_LIMIT = 1000.0


def _load() -> dict:
    return store.get_doc(*DOC) or {"status": "none"}


def _save(w: dict) -> None:
    store.set_doc(*DOC, w)


def _month_start(now: float | None = None) -> float:
    d = dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc)
    return dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp()


def committed(order: dict) -> float:
    """Money this order has taken or is holding: what was captured once it closed, the hold before."""
    p = order.get("paypal") or {}
    if p.get("voided"):
        return 0.0
    if p.get("capture_id") or order.get("status") in ("completed", "settled"):
        return float(p.get("captured") or 0)
    return float(p.get("authorized_amount") or 0) if p.get("authorization_id") else 0.0


def spent_this_month(now: float | None = None) -> float:
    start = _month_start(now)
    return round(sum(committed(o) for o in store.list_orders()
                     if (o.get("paypal") or {}).get("authorized_at", 0) >= start), 2)


def view() -> dict:
    """What the app shows. The payment token itself never leaves the server."""
    w = _load()
    out = {"status": w.get("status", "none"), "auto_pay": w.get("auto_pay", True),
           "monthly_limit": w.get("monthly_limit", DEFAULT_MONTHLY_LIMIT)}
    if out["status"] == "active":
        out.update({"payer_email": w.get("payer_email"), "payer_name": w.get("payer_name"),
                    "connected_at": w.get("connected_at"), "spent_this_month": spent_this_month(),
                    "currency": config.DEFAULT_CURRENCY})
    return out


def payment_token() -> str | None:
    w = _load()
    return w.get("payment_token_id") if w.get("status") == "active" else None


def auto_pay_check(total: float) -> tuple[bool, str]:
    """Can the agent hold this much on its own? Returns (yes, reason in plain words)."""
    w = _load()
    if w.get("status") != "active":
        return False, "your PayPal isn't connected"
    if not w.get("auto_pay", True):
        return False, "auto-pay is off"
    limit = float(w.get("monthly_limit", DEFAULT_MONTHLY_LIMIT))
    spent = spent_this_month()
    if spent + total > limit + 0.005:
        return False, f"it would take this month to {spent + total:.2f}, over your {limit:.2f} monthly cap"
    return True, f"{spent + total:.2f} of your {limit:.2f} monthly cap"


async def start(*, monthly_limit: float | None = None, auto_pay: bool = True) -> dict:
    client = paypal.platform_client()
    setup = await client.create_setup_token(
        return_url=f"{config.FRONTEND_URL}/?paypal_connect=return",
        cancel_url=f"{config.FRONTEND_URL}/?paypal_connect=cancel",
        brand_name="Procuro",
        description="Procuro buying agent: holds payments for deals you allow, never more")
    w = _load()
    if w.get("status") != "active":
        w["status"] = "pending"
    w.update({"setup_token_id": setup["id"], "auto_pay": auto_pay,
              "monthly_limit": float(monthly_limit or w.get("monthly_limit") or DEFAULT_MONTHLY_LIMIT)})
    _save(w)
    return {"approve_url": setup["approve_url"]}


async def finish(setup_token_id: str | None = None) -> dict:
    w = _load()
    setup = setup_token_id or w.get("setup_token_id")
    if not setup or setup != w.get("setup_token_id"):
        raise ValueError("This PayPal approval doesn't match the one Procuro started. Try connecting again.")
    token = await paypal.platform_client().create_payment_token(setup)
    old = w.get("payment_token_id")
    w.update({"status": "active", "payment_token_id": token["payment_token_id"],
              "customer_id": token.get("customer_id"), "payer_email": token.get("payer_email"),
              "payer_name": token.get("payer_name"), "payer_id": token.get("payer_id"),
              "connected_at": time.time()})
    w.pop("setup_token_id", None)
    _save(w)
    if old and old != token["payment_token_id"]:
        try:
            await paypal.platform_client().delete_payment_token(old)
        except paypal.PayPalError:
            pass  # the old token simply stays unused
    return view()


def update(*, auto_pay: bool | None = None, monthly_limit: float | None = None) -> dict:
    w = _load()
    if auto_pay is not None:
        w["auto_pay"] = bool(auto_pay)
    if monthly_limit is not None:
        if monthly_limit <= 0:
            raise ValueError("The monthly cap must be more than zero.")
        w["monthly_limit"] = round(float(monthly_limit), 2)
    _save(w)
    return view()


async def disconnect() -> dict:
    w = _load()
    token = w.get("payment_token_id")
    if token:
        await paypal.platform_client().delete_payment_token(token)
    _save({"status": "none", "auto_pay": w.get("auto_pay", True),
           "monthly_limit": w.get("monthly_limit", DEFAULT_MONTHLY_LIMIT)})
    return view()