"""Delete old orders from Firestore, with a backup first.

    python scripts/delete_orders.py --through 2026-11-06            # only SHOWS what it would delete
    python scripts/delete_orders.py --through 2026-11-06 --apply    # backs up, then deletes

Run it from the backend folder (not backend/tests) with your normal .env.

"--through 2026-11-06" means every order created on or before Nov 6, in your time zone (--tz, default
Asia/Kolkata). For each order it removes:
  - the order and its messages
  - its damage photos
  - its counter
  - the built-in seller agents' notes on it (seller_sessions)
  - its bars on the Sellers board (seller_schedules)

Before deleting, everything is written to backups/orders-through-<date>-<time>.json so nothing is lost
for good. (That folder is git-ignored.) If a run is cut off (say the internet drops), just run it again:
it carries on with whatever is left, and tidies the Sellers board for everything already deleted. PayPal is never called: deleting an order doesn't capture,
void or refund anything at PayPal. Outside sellers like NorthBox keep their own records.
"""
import argparse
import base64
import datetime as dt
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import schedule, sellers  # noqa: E402
from app.store import store  # noqa: E402

BACKUPS = Path(__file__).resolve().parents[1] / "backups"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


def zone(name: str) -> dt.tzinfo:
    """Windows has no time zone database unless `tzdata` is installed; India time needs none."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name in ("Asia/Kolkata", "Asia/Calcutta", "IST"):
            return IST
        raise SystemExit(f"Unknown time zone {name!r} here. Run: python -m pip install tzdata") from None


def _plain(value):
    """Make Firestore values JSON-safe for the backup."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(bytes(value)).decode()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--through", required=True, metavar="YYYY-MM-DD", help="delete orders created on or before this day")
    ap.add_argument("--tz", default="Asia/Kolkata", help="time zone the day is in (default Asia/Kolkata)")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: only show)")
    args = ap.parse_args()

    if type(store).__name__ != "FirestoreStore":
        print("Not connected to Firestore. Run this from the backend folder (cd backend), e.g.:\n"
              "    python scripts/delete_orders.py --through 2026-11-06")
        return 1
    day = dt.date.fromisoformat(args.through)
    tz = zone(args.tz)
    cutoff = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tz).timestamp()

    orders = store.list_orders()
    old = [o for o in orders if (o.get("created_at") or 0) < cutoff]
    print(f"{len(orders)} orders in Firestore; {len(old)} created on or before {day} ({args.tz}).")
    for o in sorted(old, key=lambda x: x.get("created_at") or 0):
        m, w = o.get("mandate") or {}, o.get("winner") or {}
        when = dt.datetime.fromtimestamp(o.get("created_at") or 0, tz).strftime("%b %d %H:%M")
        print(f"  {o['id']}  {when}  {str(o.get('status')):<18} {m.get('quantity')} x {str(m.get('item'))[:38]:<38} "
              f"{w.get('seller_name') or '-'}")
    if not old:
        return 0
    if not args.apply:
        print(f"\nRun again with --apply to back these up and delete them.")
        return 0

    db = store.db
    BACKUPS.mkdir(exist_ok=True)
    backup = {"made_at": time.time(), "through": str(day), "tz": args.tz, "orders": []}
    for o in old:
        oid = o["id"]
        msgs = [_plain(d.to_dict()) for d in db.collection("orders").document(oid).collection("messages").stream()]
        evidence = []
        for e in o.get("evidence") or []:
            snap = db.collection("evidence").document(e["id"]).get()
            if snap.exists:
                evidence.append(_plain(snap.to_dict()))
        sessions = {}
        for sid in sellers.SELLERS:
            snap = db.collection("seller_sessions").document(f"{sid}_{oid}").get()
            if snap.exists:
                sessions[sid] = _plain(snap.to_dict())
        backup["orders"].append({"order": _plain(o), "messages": msgs, "evidence": evidence,
                                 "seller_sessions": sessions})
    path = BACKUPS / f"orders-through-{day}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(backup, indent=1), encoding="utf-8")
    print(f"\nBacked up {len(old)} orders to {path}")

    ids = {o["id"] for o in old}
    for o in old:
        oid = o["id"]
        ref = db.collection("orders").document(oid)
        for d in ref.collection("messages").stream():
            d.reference.delete()
        ref.delete()
        db.collection("counters").document(oid).delete()
        for e in o.get("evidence") or []:
            db.collection("evidence").document(e["id"]).delete()
        for sid in sellers.SELLERS:
            db.collection("seller_sessions").document(f"{sid}_{oid}").delete()
    remaining = {o["id"] for o in store.list_orders()}
    for sid in sellers.SELLERS:  # drop bars for deleted orders (also from an earlier, interrupted run)
        items = schedule.bookings(sid)
        kept = {k: v for k, v in items.items() if k not in ids and k in remaining}
        if len(kept) != len(items):
            schedule._save(sid, kept)
    print(f"Deleted {len(old)} orders. {len(orders) - len(old)} remain. Nothing was changed at PayPal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())