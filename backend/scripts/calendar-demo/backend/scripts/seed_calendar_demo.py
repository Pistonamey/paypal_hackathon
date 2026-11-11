"""Put a few demo orders in Firestore so the start-page calendar shows every colour.

    python scripts/seed_calendar_demo.py            # only SHOWS what it would add
    python scripts/seed_calendar_demo.py --apply    # adds them
    python scripts/seed_calendar_demo.py --remove   # deletes every demo order it added (and nothing else)

Run it from the backend folder with your normal .env (it needs your Firebase key and SIGNING_SEED).

The orders are made by Procuro's real flow, run here on your laptop with PayPal in mock mode, the
built-in seller agents and no AI, so every field, message and signed AP2 document is real. Then they
are moved in time and saved to Firestore:

  ord_demo_held_soon    money on hold since 2 days ago: yellow "Hold renewal" tomorrow, teal "Arrives",
                        green "Payment" the day after
  ord_demo_held_later   money on hold since today: yellow renewal in 3 days, teal and green next week
  ord_demo_dispute      delivery reported damaged, no agreement: red "Payment" (dispute) today
  ord_demo_dispute_2    same, went to dispute 2 days ago: red
  ord_demo_done         delivered and paid last week: gray "Arrives" and "Payment"

They're for looking at only. Their PayPal holds exist in mock mode, not in your sandbox, so don't click
"Received", "Report a problem" or drag them: PayPal would say the hold doesn't exist. Held demo orders
count towards the monthly cap like real ones (about $500 each), and the cleanup script leaves them alone.
Nothing is sent to PayPal or to the outside seller agents.
"""
import argparse
import asyncio
import copy
import datetime as dt
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Keep the real Firebase settings for later, then run the app itself fully offline.
from dotenv import dotenv_values  # noqa: E402

REAL = {**dotenv_values(ROOT / ".env"), **{k: v for k, v in os.environ.items() if k.startswith("FIREBASE")}}
os.environ.update({"PAYPAL_MODE": "mock", "ANTHROPIC_API_KEY": "", "CHANNEL3_API_KEY": "", "STORE_BACKEND": "memory",
                   "PUBLIC_BASE_URL": "http://testserver", "SELLER_AGENT_URLS": "", "SELLER_AGENTS_ONLY": "",
                   "FIREBASE_CREDENTIALS_JSON": "", "FIREBASE_CREDENTIALS_PATH": "",
                   "GOOGLE_APPLICATION_CREDENTIALS": ""})

import httpx  # noqa: E402

from app import a2a, config  # noqa: E402
from app.main import app  # noqa: E402
from app.store import FirestoreStore, firestore_safe, store as memory  # noqa: E402

DAY = 86400
transport = httpx.ASGITransport(app=app)
a2a.client_factory = lambda: httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60)
DATE_KEYS = {"deadline", "delivery_date", "first_delivery_date", "needed_by", "earliest", "start"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

# id, days to move back in time, what happens after the hold, (for disputes) how many days ago it went red
SCENARIOS = [
    ("ord_demo_held_soon", 2, "held", None),
    ("ord_demo_held_later", 0, "held", None),
    ("ord_demo_dispute", 4, "dispute", 0),
    ("ord_demo_dispute_2", 7, "dispute", 2),
    ("ord_demo_done", 8, "done", None),
]


def request_text() -> str:
    deadline = dt.date.today() + dt.timedelta(days=7)
    return (f"200 paper coffee cups with lids, 12 oz, delivered by {deadline.strftime('%b %d')}. "
            "Budget $600. Ask me before paying anything over $400.")


def shift(value, seconds: float, days: int, key: str = ""):
    """Move every timestamp back by `seconds` and every date back by `days`."""
    if isinstance(value, dict):
        return {k: shift(v, seconds, days, k) for k, v in value.items()}
    if isinstance(value, list):
        return [shift(v, seconds, days, key) for v in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 1.6e9 < value < 2.2e9:
        return value - seconds
    if isinstance(value, str) and key in DATE_KEYS and DATE.match(value):
        d = dt.date.fromisoformat(value[:10]) - dt.timedelta(days=days)
        return d.isoformat() + value[10:]
    return value


def rename(value, old: str, new: str):
    if isinstance(value, dict):
        return {k: rename(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [rename(v, old, new) for v in value]
    return value.replace(old, new) if isinstance(value, str) else value


async def make(c: httpx.AsyncClient, kind: str) -> str:
    r = await c.post("/api/orders", json={"text": request_text()})
    r.raise_for_status()
    oid = r.json()["id"]
    order = (await c.get(f"/api/orders/{oid}")).json()["order"]
    if order["status"] != "awaiting_approval":
        raise SystemExit(f"The offline run ended as {order['status']!r}, not a deal. Try again.")
    token = (await c.post(f"/api/orders/{oid}/approve")).json()["paypal"]["order_id"]
    (await c.post(f"/api/orders/{oid}/paypal-return", json={"token": token})).raise_for_status()
    if kind == "done":
        (await c.post(f"/api/orders/{oid}/delivered")).raise_for_status()
    return oid


def as_dispute(order: dict, msgs: list[dict], days_back: int, red_ago: int) -> None:
    """A delivery reported damaged where the agents couldn't agree: the money stays on hold.
    Times are set so that, once the whole order moves `days_back` days back, the dispute starts
    `red_ago` days before today and the delivery arrived the day before."""
    now = time.time() + (days_back - red_ago) * DAY
    order["status"] = "escalated"
    order.setdefault("times", {}).update({"resolving": now - DAY, "escalated": now - 3600})
    arrived = dt.date.fromtimestamp(now - DAY).isoformat()  # moved back with the rest of the order
    order["winner"]["delivery_date"] = order["winner"]["first_delivery_date"] = arrived
    # Its agent already renewed the hold once (PayPal honours a hold for 3 days).
    order.setdefault("paypal", {})["reauthorized_at"] = order["times"]["held"] + 3 * DAY
    order["problem_note"] = "A case of 200 cups arrived crushed and the lids don't fit the rest."
    order["assessment"] = {"damaged_units": 200, "usable_units": 0, "summary":
                           "All 200 cups are unusable: crushed case, lids don't fit.", "confidence": "high"}
    order["dispute_packet"] = {
        "summary": "Every cup in the delivery was unusable and the seller would only refund half.",
        "requested_outcome": "Full refund of the held amount; nothing captured.",
    }
    seller = order["winner"]["seller_id"]
    for text, frm, to, kind in [
        (f"You reported a problem: \"{order['problem_note']}\". Payment stays on hold.", "buyer", None, "system"),
        ("Assessment: all 200 cups unusable.", "buyer", None, "decision"),
        ("We'll refund half. The cups left our warehouse in good shape.", seller, "buyer", "message"),
        ("No agreement after 3 rounds. Dispute file prepared; your money stays on hold.", "buyer", None, "decision"),
    ]:
        msgs.append({"from": frm, "to": to, "kind": kind, "text": text, "data": {}, "ts": now - DAY + len(msgs)})


async def build() -> list[tuple[dict, list[dict], dict]]:
    made = []
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60) as c:
        for new_id, days_back, kind, red_ago in SCENARIOS:
            oid = await make(c, kind)
            order, msgs = memory.get_order(oid), memory.list_messages(oid)
            if kind == "dispute":
                as_dispute(order, msgs, days_back, red_ago)
            sid = order["winner"]["seller_id"]
            session = memory.get_doc("seller_sessions", f"{sid}_{oid}")
            order, msgs, session = (shift(x, days_back * DAY, days_back) for x in (order, msgs, session))
            order, msgs, session = (rename(x, oid, new_id) for x in (order, msgs, session))
            order.update({"id": new_id, "demo": True})
            made.append((order, msgs, {"id": f"{sid}_{new_id}", "data": session} if session else None))
    return made


def firestore() -> FirestoreStore:
    for k in ("FIREBASE_CREDENTIALS_JSON", "FIREBASE_CREDENTIALS_PATH", "FIREBASE_STORAGE_BUCKET",
              "GOOGLE_APPLICATION_CREDENTIALS"):
        setattr(config, k, REAL.get(k) or "")
    if not (config.FIREBASE_CREDENTIALS_JSON or config.FIREBASE_CREDENTIALS_PATH or config.GOOGLE_APPLICATION_CREDENTIALS):
        raise SystemExit("No Firebase key in backend/.env. Run this from the backend folder.")
    return FirestoreStore()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="save the demo orders to Firestore")
    ap.add_argument("--remove", action="store_true", help="delete the demo orders this script added")
    args = ap.parse_args()

    if args.remove:
        db = firestore().db
        gone = 0
        for snap in db.collection("orders").where("demo", "==", True).stream():
            ref = snap.reference
            for m in ref.collection("messages").stream():
                m.reference.delete()
            sid = (snap.to_dict().get("winner") or {}).get("seller_id")
            if sid:
                db.collection("seller_sessions").document(f"{sid}_{snap.id}").delete()
            ref.delete()
            gone += 1
        print(f"Removed {gone} demo order(s).")
        return 0

    made = asyncio.run(build())
    for order, _msgs, _s in made:
        w, p = order["winner"], order.get("paypal") or {}
        print(f"  {order['id']:<22} {order['status']:<10} {w['seller_name']:<11} ${w['total']:>8.2f}  "
              f"held {dt.date.fromtimestamp(order['times']['held'])}  delivery {w['delivery_date']}")
    from app import timeline
    print("\nOn the calendar:")
    for e in sorted(timeline.calendar_events([o for o, _m, _s in made]), key=lambda e: e["startDate"]):
        print(f"  {e['startDate']}  {e['eventColor']:<7} {e['name'][:60]}")
    if not args.apply:
        print("\nRun again with --apply to save these to Firestore.")
        return 0
    fs = firestore()
    for order, msgs, session in made:
        ref = fs.db.collection("orders").document(order["id"])
        for old in ref.collection("messages").stream():  # re-running replaces, never duplicates
            old.reference.delete()
        fs.save_order(order)
        for i, m in enumerate(msgs):
            ref.collection("messages").document(f"{i:06d}").set(firestore_safe({**m, "seq": i}))
        if session:
            fs.set_doc("seller_sessions", session["id"], session["data"])
    print(f"\nSaved {len(made)} demo orders. Reload Procuro: the calendar shows them now. "
          "Remove them with --remove.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
