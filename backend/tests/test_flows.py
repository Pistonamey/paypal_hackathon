"""End-to-end tests of both user stories with PayPal in mock mode and the rule-based fallback agents.

Real agents talk over real A2A HTTP calls (routed in-process here).
"""
import datetime as dt
import os

os.environ.update({"PAYPAL_MODE": "mock", "ANTHROPIC_API_KEY": "", "CHANNEL3_API_KEY": "",
                   "PUBLIC_BASE_URL": "http://testserver", "FIREBASE_CREDENTIALS_JSON": "",
                   "GOOGLE_APPLICATION_CREDENTIALS": "", "SELLER_AGENT_URLS": ""})

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import a2a, mandates  # noqa: E402
from app.main import app  # noqa: E402

transport = httpx.ASGITransport(app=app)
a2a.client_factory = lambda: httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60)


def maya_request() -> str:
    deadline = dt.date.today() + dt.timedelta(days=7)
    return (f"200 cake boxes with my logo, delivered by {deadline.strftime('%b %d')}. "
            "Budget $600. Ask me before paying anything over $400.")


async def negotiate_and_hold(c: httpx.AsyncClient) -> dict:
    r = await c.post("/api/orders", json={"text": maya_request()})
    assert r.status_code == 200, r.text
    order_id = r.json()["id"]
    state = (await c.get(f"/api/orders/{order_id}")).json()
    order = state["order"]
    assert order["status"] == "awaiting_approval", state["messages"]
    assert order["sellers"]["packright"]["status"] == "dropped_deadline"
    assert order["winner"]["total"] <= 600
    assert order["winner"]["needs_explicit_approval"] is True
    # The seller's AP2 checkout is signed with the key in its agent card.
    sid = order["winner"]["seller_id"]
    checkout = order["ap2"]["checkout"]
    assert mandates.verify_receipt(checkout["checkout_jwt"], mandates.jwk_from(order["sellers"][sid]["jwk"]))
    assert checkout["payload"]["merchant"]["id"] == sid

    r = await c.post(f"/api/orders/{order_id}/approve")
    assert r.status_code == 200, r.text
    token = r.json()["paypal"]["order_id"]
    r = await c.post(f"/api/orders/{order_id}/paypal-return", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "held"
    return r.json()


@pytest.mark.anyio
async def test_agent_cards_are_published():
    async with client() as c:
        card = (await c.get("/agents/quickprint/.well-known/agent-card.json")).json()
        assert card["name"] == "QuickPrint"
        assert card["capabilities"]["extensions"][0]["uri"] == a2a.AP2_EXTENSION_URI


@pytest.mark.anyio
async def test_happy_customer():
    async with client() as c:
        order = await negotiate_and_hold(c)
        held = order["paypal"]["authorized_amount"]
        r = await c.post(f"/api/orders/{order['id']}/delivered")
        assert r.status_code == 200, r.text
        done = r.json()
        assert done["status"] == "completed"
        assert done["paypal"]["captured"] == held
        assert done["paypal"]["released"] == 0
        # Seller negotiated down from its opening offer.
        assert done["winner"]["saved_vs_opening"] > 0


@pytest.mark.anyio
async def test_dissatisfied_customer_partial_capture():
    async with client() as c:
        order = await negotiate_and_hold(c)
        held = order["paypal"]["authorized_amount"]
        r = await c.post(f"/api/orders/{order['id']}/report",
                         data={"note": "60 crushed, logo is crooked on most of them."})
        assert r.status_code == 200, r.text
        state = (await c.get(f"/api/orders/{order['id']}")).json()
        o = state["order"]
        assert o["assessment"]["damaged_units"] == 60
        assert o["status"] == "settled", [m["text"] for m in state["messages"]]
        unit = o["winner"]["unit_price"]
        assert o["paypal"]["captured"] <= round(140 * unit, 2)
        assert o["paypal"]["released"] > 0
        assert round(o["paypal"]["captured"] + o["paypal"]["released"], 2) == round(held, 2)
        sellers_ = (await c.get("/api/sellers")).json()
        winner = next(s for s in sellers_ if s["id"] == o["winner"]["seller_id"])
        assert winner["score"] < 100


@pytest.mark.anyio
async def test_seller_rejects_unsigned_release():
    async with client() as c:
        order = await negotiate_and_hold(c)
        sid = order["winner"]["seller_id"]
        auth = order["paypal"]["authorization_id"]
        # Signed by the seller's own key instead of the buying agent's: refused.
        forged = mandates._sign(f"seller:{sid}", {"authorization_id": auth, "capture_amount": 1},
                                mandates.RELEASE_TYP)
        for bad in (None, "not-a-jwt", forged):
            with pytest.raises(a2a.A2AError):
                await a2a.send(f"http://testserver/agents/{sid}",
                               {"action": "release", "order_id": order["id"], "release": bad})
        # Correctly signed, but for a different hold: refused too.
        other = mandates.release_instruction({"authorization_id": "AUTH-OTHER", "capture_amount": 1})
        with pytest.raises(a2a.A2AError):
            await a2a.send(f"http://testserver/agents/{sid}",
                           {"action": "release", "order_id": order["id"], "release": other})


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_settling_at_zero_voids_the_hold_instead_of_capturing_nothing():
    """PayPal rejects a 0.00 capture (CANNOT_BE_ZERO_OR_NEGATIVE), so paying nothing must void the hold."""
    from app import analytics, buyer, schedule, timeline
    from app.store import store
    async with client() as c:
        held = await negotiate_and_hold(c)
        order = store.get_order(held["id"])
        reply = await buyer._release(order, 0.0, "Nothing usable arrived.")
        assert reply == {"action": "voided", "captured": 0.0, "released": held["paypal"]["authorized_amount"]}
        assert order["paypal"]["voided"] and order["paypal"]["captured"] == 0.0
        order["status"] = "settled"
        buyer._save(order)
        state = (await c.get(f"/api/orders/{held['id']}")).json()
        assert any("voided" in m["text"] for m in state["messages"])
        # Nothing reads as still held, and the timeline says the hold was voided.
        row, _, money = analytics._rows_for_order(store.get_order(held["id"]))
        assert row["held"] == 0
        assert not any(e["type"] == "Paid to seller" for e in money)
        tl = timeline.build(store.get_order(held["id"]))
        names = [t["name"] for g in tl["tasks"] for t in g["children"]]
        assert any(n.startswith("Hold voided") for n in names)
        sid = held["winner"]["seller_id"]
        assert schedule.bookings(sid)[held["id"]]["status"] == "delivered"


@pytest.mark.anyio
async def test_sellers_that_lose_are_told_and_no_conversation_is_left_open():
    async with client() as c:
        held = await negotiate_and_hold(c)
        state = (await c.get(f"/api/orders/{held['id']}")).json()
        winner = held["winner"]["seller_id"]
        for sid, entry in state["order"]["sellers"].items():
            if sid == winner:
                continue
            assert entry["status"] not in ("requested", "negotiating", "agreed"), (sid, entry["status"])
        losers = [s for s, e in state["order"]["sellers"].items() if e["status"] == "not_picked"]
        assert losers, "at least one seller agreed and lost in this scenario"
        for sid in losers:
            to_it = [m for m in state["messages"] if m["from"] == "buyer" and m.get("to") == sid]
            assert "gone with another seller" in to_it[-1]["text"]
            assert any(m["from"] == sid and m["data"].get("action") == "closed" for m in state["messages"])
        # Seller replies before the pick never claim the order is confirmed.
        assert not any("Deal." == m["text"].strip() for m in state["messages"])