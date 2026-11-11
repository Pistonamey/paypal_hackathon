"""Tidy up test orders that can never finish, so the demo looks like a real business.

    python scripts/cleanup_sandbox_orders.py            # only SHOWS what it would clean
    python scripts/cleanup_sandbox_orders.py --apply    # does it
    python scripts/cleanup_sandbox_orders.py --order ord_abc123 --order ord_def456 --apply
                                                        # cancel these orders, whatever the checks say

Run it from the backend folder (not backend/tests) with your normal .env (it uses the same Firestore and PayPal keys).

An order is cleaned when it's stuck and can't be finished from here:
  - money held, but PayPal says the hold belongs to another PayPal app (for example, made before you
    switched to the US sandbox app), or the hold no longer exists
  - waiting for approval or for PayPal, but made before Procuro moved to AP2 v0.2 (it can't be paid now)
  - waiting for approval or for PayPal for more than --stale-days days (default 2)
  - its seller is an outside agent at a localhost address (made while NorthBox ran on your laptop),
    unless you pass --keep-local

Cleaning marks the order "cancelled" with a note saying why, stops it counting as money on hold or as
spending, and frees the built-in seller's production slot. It never calls PayPal to capture or void, and
never deletes anything. Orders that are paid, settled, in dispute, no-deal or stopped are left alone.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paypal, schedule, sellers  # noqa: E402
from app.store import store  # noqa: E402

WAITING = ("awaiting_approval", "awaiting_paypal")
HELD = ("held", "resolving")


async def reason_to_clean(order: dict, stale_days: int, keep_local: bool) -> str | None:
    status = order.get("status")
    if order.get("demo") or status not in WAITING + HELD:
        return None  # demo orders from seed_calendar_demo.py are left alone
    w = order.get("winner") or {}
    sid = w.get("seller_id")
    seller = (order.get("sellers") or {}).get(sid or "", {})
    if seller.get("outside") and "localhost" in str(seller.get("url")) and not keep_local:
        return f"its seller ({seller.get('name')}) was an outside agent running on localhost"
    if status in WAITING:
        if not order.get("ap2"):
            return "made before Procuro moved to AP2 v0.2, so it can't be paid now"
        age_days = (time.time() - (order.get("created_at") or time.time())) / 86400
        if age_days > stale_days:
            return f"waiting for {age_days:.0f} days"
        return None
    auth = (order.get("paypal") or {}).get("authorization_id")
    if not auth:
        return "marked as held but has no PayPal authorization"
    if seller.get("outside"):
        return None  # its hold is on the outside seller's own PayPal account; we can't check it from here
    try:
        await paypal.client_for(sid).get_authorization(auth)
    except paypal.PayPalError as exc:
        text = str(exc)
        if any(k in text for k in ("NOT_AUTHORIZED", "PERMISSION_DENIED", "401", "403")):
            return "PayPal says this hold belongs to a different PayPal app"
        if any(k in text for k in ("INVALID_RESOURCE_ID", "RESOURCE_NOT_FOUND", "404")):
            return "PayPal no longer has this hold"
        return None  # some other PayPal problem: leave it rather than guess
    return None


def clean(order: dict, why: str) -> None:
    order["status"] = "cancelled"
    order["cleanup"] = {"at": time.time(), "reason": why, "by": "scripts/cleanup_sandbox_orders.py"}
    p = order.setdefault("paypal", {})
    if p.get("authorization_id") and not p.get("capture_id"):
        p["abandoned"] = True  # no longer counts as money on hold or spending
    order["updated_at"] = time.time()
    order.setdefault("times", {}).setdefault("cancelled", order["updated_at"])
    store.save_order(order)
    store.add_message(order["id"], {"from": "buyer", "to": None, "kind": "decision", "data": {},
                                    "text": f"Order cancelled during sandbox cleanup: {why}. No money was moved."})
    sid = (order.get("winner") or {}).get("seller_id")
    if sid in sellers.SELLERS:
        schedule.mark(sid, order["id"], "cancelled")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually clean (default: only show)")
    ap.add_argument("--stale-days", type=int, default=2, help="waiting longer than this counts as stuck")
    ap.add_argument("--keep-local", action="store_true", help="don't clean orders with localhost outside sellers")
    ap.add_argument("--order", action="append", default=[], metavar="ORDER_ID",
                    help="cancel this order even if the checks pass (e.g. PayPal refuses to capture it); repeatable")
    args = ap.parse_args()

    if type(store).__name__ != "FirestoreStore":
        print("Not connected to Firestore, so there are no saved orders to look at. Run this from the backend\n"
              "folder (cd backend), where your .env and Firebase key path work, e.g.:\n"
              "    python scripts/cleanup_sandbox_orders.py")
        return 1
    orders = store.list_orders()
    print(f"Looking at {len(orders)} orders in Firestore...")
    found = []
    named = set(args.order)
    for order in orders:
        if order["id"] in named:
            if order.get("status") in ("completed", "settled", "cancelled"):
                print(f"{order['id']} is already {order.get('status')}; leaving it.")
            else:
                found.append((order, "cancelled by you (it couldn't be finished)"))
            named.discard(order["id"])
            continue
        why = await reason_to_clean(order, args.stale_days, args.keep_local)
        if why:
            found.append((order, why))
    for missing in named:
        print(f"No order {missing} in Firestore.")
    if not found:
        print("Nothing to clean.")
        return 0
    for order, why in found:
        m = order.get("mandate") or {}
        w = order.get("winner") or {}
        print(f"{order['id']}  {order.get('status'):<18} {m.get('quantity')} x {str(m.get('item'))[:40]:<40} "
              f"{w.get('seller_name') or '-':<20} {why}")
    if not args.apply:
        print(f"\n{len(found)} order(s) would be cancelled. Run again with --apply to do it.")
        return 0
    for order, why in found:
        clean(order, why)
    print(f"\nCancelled {len(found)} order(s). Nothing was charged, voided or deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
