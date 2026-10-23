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

from app import a2a, signing  # noqa: E402
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
    assert signing.verify(order["cart_mandate"], order["sellers"][order["winner"]["seller_id"]]["public_key"])

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
        forged = {"type": "release_mandate", "id": "x", "issued_at": 0, "signer": "buyer:mandate",
                  "payload": {"capture_amount": 1}, "signature": "AAAA", "alg": "Ed25519"}
        with pytest.raises(a2a.A2AError):
            await a2a.send(f"http://testserver/agents/{sid}",
                           {"action": "release", "order_id": order["id"], "release_mandate": forged})


@pytest.fixture
def anyio_backend():
    return "asyncio"
