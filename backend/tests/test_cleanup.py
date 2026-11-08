"""scripts/cleanup_sandbox_orders.py: cancels only orders that can never finish, and moves no money."""
import importlib.util
from pathlib import Path

import pytest

import test_flows as flows
from app import analytics, wallet
from app.store import store

spec = importlib.util.spec_from_file_location(
    "cleanup", Path(__file__).resolve().parents[1] / "scripts" / "cleanup_sandbox_orders.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_cleans_only_orders_that_cannot_finish():
    async with flows.client() as c:
        good = await flows.negotiate_and_hold(c)                 # a live hold PayPal knows: keep
        lost = await flows.negotiate_and_hold(c)                 # a hold PayPal no longer has: clean
        o = store.get_order(lost["id"])
        o["paypal"]["authorization_id"] = "AUTH-FROM-ANOTHER-APP"
        store.save_order(o)
        old = await flows.negotiate_and_hold(c)                  # waiting, from before AP2: clean
        o = store.get_order(old["id"])
        o.update({"status": "awaiting_approval", "ap2": None})
        store.save_order(o)
        done = await flows.negotiate_and_hold(c)
        await c.post(f"/api/orders/{done['id']}/delivered")      # paid: keep

        reasons = {oid: await cleanup.reason_to_clean(store.get_order(oid), 2, False)
                   for oid in (good["id"], lost["id"], old["id"], done["id"])}
        assert reasons[good["id"]] is None and reasons[done["id"]] is None
        assert "no longer has" in reasons[lost["id"]] and "before Procuro moved to AP2" in reasons[old["id"]]

        cleanup.clean(store.get_order(lost["id"]), reasons[lost["id"]])
        o = store.get_order(lost["id"])
        assert o["status"] == "cancelled" and o["paypal"]["abandoned"] and o["cleanup"]["reason"]
        assert wallet.committed(o) == 0
        assert "cancelled during sandbox cleanup" in store.list_messages(lost["id"], -1)[-1]["text"]
        row = next(r for r in analytics.tables()["orders"] if r.get("orderId", r.get("id")) == lost["id"])
        assert row["held"] == 0 and row["statusGroup"] == "Cancelled"