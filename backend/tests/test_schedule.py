"""Moving a delivery date: the seller agent decides from its schedule and signs a change to the cart."""
import datetime as dt

import pytest

import test_flows as flows
from app import schedule, sellers, signing
from app.store import store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def day(n: int) -> str:
    return (dt.date.today() + dt.timedelta(days=n)).isoformat()


@pytest.mark.anyio
async def test_a_later_date_is_agreed_signed_and_moves_everything():
    async with flows.client() as c:
        held = await flows.negotiate_and_hold(c)
        sid, old = held["winner"]["seller_id"], held["winner"]["delivery_date"]
        auth = held["paypal"]["authorization_id"]
        r = await c.post(f"/api/orders/{held['id']}/reschedule", json={"delivery_date": day(9)})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["accepted"], out
        o = out["order"]
        assert o["winner"]["delivery_date"] == day(9) and o["winner"]["first_delivery_date"] == old
        # The seller signed the change; price and the PayPal hold are untouched.
        amendment = o["cart_amendments"][-1]
        assert amendment["type"] == "cart_amendment" and amendment["payload"]["price_unchanged"]
        assert signing.verify(amendment, o["sellers"][sid]["public_key"])
        assert o["paypal"]["authorization_id"] == auth and o["status"] == "held"
        # The seller's production slot moved, and the calendar shows the new day.
        assert schedule.bookings(sid)[held["id"]]["delivery_date"] == day(9)
        events = (await c.get("/api/calendar")).json()
        arrive = next(e for e in events if e["id"] == f"{held['id']}-arrived")
        assert arrive["startDate"] == day(9) and arrive["draggable"] is True
        msgs = (await c.get(f"/api/orders/{held['id']}")).json()["messages"]
        assert any("nothing is paid twice" in m["text"] for m in msgs)
        # Moving past the deadline moves the deadline with it, and says so.
        assert o["mandate"]["deadline"] == day(9)
        assert any("moved your deadline to match" in m["text"] for m in msgs)


@pytest.mark.anyio
async def test_too_soon_for_the_seller_is_declined_with_the_nearest_date():
    async with flows.client() as c:
        held = await flows.negotiate_and_hold(c)
        sid = held["winner"]["seller_id"]
        lead = sellers.SELLERS[sid]["lead_days"]
        r = await c.post(f"/api/orders/{held['id']}/reschedule", json={"delivery_date": day(1)})
        out = r.json()
        if lead > 1:
            assert out["accepted"] is False and out["earliest"] >= day(lead)
            assert store.get_order(held["id"])["winner"]["delivery_date"] == held["winner"]["delivery_date"]


@pytest.mark.anyio
async def test_a_full_schedule_blocks_an_earlier_date():
    async with flows.client() as c:
        held = await flows.negotiate_and_hold(c)
        sid = held["winner"]["seller_id"]
        lead = sellers.SELLERS[sid]["lead_days"]
        # Fill every slot from today for 30 days with other orders.
        for i in range(schedule.slots(sid)):
            schedule.reserve(sid, f"busy-{i}", item="x", quantity=1, total=1, currency="USD",
                             start=day(0), delivery_date=day(30))
            schedule.mark(sid, f"busy-{i}", "confirmed")
        r = await c.post(f"/api/orders/{held['id']}/reschedule", json={"delivery_date": day(lead + 1)})
        out = r.json()
        assert out["accepted"] is False and out["earliest"] == day(30 + lead)


@pytest.mark.anyio
async def test_paypal_29_day_hold_limit():
    async with flows.client() as c:
        held = await flows.negotiate_and_hold(c)
        r = await c.post(f"/api/orders/{held['id']}/reschedule", json={"delivery_date": day(40)})
        out = r.json()
        assert out["accepted"] is False and "hold" in out["message"]
        assert out["latest"] == day(28)


@pytest.mark.anyio
async def test_only_open_orders_can_move():
    async with flows.client() as c:
        held = await flows.negotiate_and_hold(c)
        await c.post(f"/api/orders/{held['id']}/delivered")
        r = await c.post(f"/api/orders/{held['id']}/reschedule", json={"delivery_date": day(9)})
        assert r.status_code == 409
        events = (await c.get("/api/calendar")).json()
        assert not any(e["draggable"] for e in events if e["orderId"] == held["id"])