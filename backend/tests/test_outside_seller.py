"""Procuro's buying agent dealing with NorthBox, the outside seller agent in ../seller-agent.

NorthBox runs as its own app here (on the official A2A SDK, with its own signing key, its own PayPal
stand-in and its own storage) at http://northbox.test. Every call between the two goes over HTTP,
routed in-process: discovery from its A2A v1.0 card, negotiation, the AP2 checkout and mandates,
PayPal, the release, and PayPal's records for the receipt.
"""
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("a2a.server.routes", reason="needs the A2A SDK: pip install -r requirements-dev.txt")

import test_flows as flows  # noqa: E402  (sets up the backend's test environment)
import test_paypal_wallet as pw  # noqa: E402
from app import a2a, buyer, config, mandates  # noqa: E402
from app.main import app as procuro  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "seller-agent"))
import agent.config as nb_config  # noqa: E402

nb_config.PUBLIC_BASE_URL = "http://northbox.test"
nb_config.TRUSTED_BUYER_URL = "http://testserver/agents/buyer"
nb_config.PAYPAL_MODE = "mock"
nb_config.SIGNING_SEED = "northbox-test-seed"
from agent import main as northbox, trust  # noqa: E402

NORTHBOX = "http://northbox.test/agents/northbox"


class Router(httpx.AsyncBaseTransport):
    """Sends each request to the app that owns its host, like the internet would."""

    def __init__(self):
        self.apps = {"testserver": httpx.ASGITransport(app=procuro),
                     "northbox.test": httpx.ASGITransport(app=northbox.app)}

    async def handle_async_request(self, request):
        return await self.apps[request.url.host].handle_async_request(request)


router = Router()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def wire(monkeypatch):
    monkeypatch.setattr(a2a, "client_factory", lambda: httpx.AsyncClient(transport=router, timeout=60))
    monkeypatch.setattr(trust, "client_factory", lambda: httpx.AsyncClient(transport=router, timeout=60))
    monkeypatch.setattr(config, "SELLER_AGENT_URLS", [NORTHBOX])
    trust._cache.update(at=0.0, keys=None)
    yield


def only_northbox(monkeypatch):
    monkeypatch.setattr(config, "SELLER_AGENTS_ONLY", True)


@pytest.mark.anyio
async def test_northbox_publishes_an_a2a_v1_card_with_ap2_and_its_own_key():
    async with httpx.AsyncClient(transport=router) as c:
        card = (await c.get(f"{NORTHBOX}/.well-known/agent-card.json")).json()
    assert card["name"] == "NorthBox Packaging"
    assert {i["protocolVersion"] for i in card["supportedInterfaces"]} == {"1.0", "0.3"}
    ext = card["capabilities"]["extensions"][0]
    assert ext["uri"] == mandates.EXTENSION_URI and ext["params"]["roles"] == ["merchant"]
    jwk = buyer.card_jwk(card)
    assert jwk and jwk != mandates.public_jwk("seller:northbox")  # its own seed, not Procuro's
    assert buyer.card_url(card, "x") == NORTHBOX


@pytest.mark.anyio
async def test_it_answers_a2a_v1_send_message_too():
    body = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": {
        "messageId": "m1", "role": "ROLE_USER", "contextId": "ord_v1",
        "parts": [{"data": {"action": "request_quote", "order_id": "ord_v1", "item": "cake boxes",
                            "quantity": 100, "currency": "USD"}}]}}}
    async with httpx.AsyncClient(transport=router) as c:
        r = (await c.post(NORTHBOX, json=body, headers={"A2A-Version": "1.0"})).json()
    msg = r["result"]["message"]
    data = next(p["data"] for p in msg["parts"] if "data" in p)
    assert data["action"] == "offer" and data["quantity"] == 100


@pytest.mark.anyio
async def test_buying_from_the_outside_seller_end_to_end(monkeypatch, tmp_path):
    only_northbox(monkeypatch)
    async with flows.client() as c:
        r = await c.post("/api/orders", json={"text": flows.maya_request()})
        oid = r.json()["id"]
        order = (await c.get(f"/api/orders/{oid}")).json()["order"]
        seller = order["sellers"]["northbox"]
        assert seller["outside"] and seller["name"] == "NorthBox Packaging" and seller["url"] == NORTHBOX
        assert order["status"] == "awaiting_approval", order["status"]
        assert order["winner"]["seller_id"] == "northbox"
        # Its checkout is signed with ITS key, which the buyer took from its card.
        assert mandates.verify_receipt(order["ap2"]["checkout"]["checkout_jwt"], mandates.jwk_from(seller["jwk"]))

        approved = (await c.post(f"/api/orders/{oid}/approve")).json()
        assert approved["status"] == "awaiting_paypal"
        held = (await c.post(f"/api/orders/{oid}/paypal-return",
                             json={"token": approved["paypal"]["order_id"]})).json()
        assert held["status"] == "held"

        receipt = (await c.get(f"/api/orders/{oid}/receipt")).json()
        assert all(s["verified"] for s in receipt["steps"]), [(s["title"], s["verify_error"]) for s in receipt["steps"]]
        assert all(x["ok"] is not False for x in receipt["checks"]), receipt["checks"]

        done = (await c.post(f"/api/orders/{oid}/delivered")).json()
        assert done["status"] == "completed" and done["paypal"]["capture_id"]
        receipt = (await c.get(f"/api/orders/{oid}/receipt")).json()
        assert all(x["ok"] for x in receipt["checks"]), receipt["checks"]

        live = (await c.get(f"/api/orders/{oid}/receipt/paypal")).json()
        assert [i["ok"] for i in live["items"]] == [True, True], live
        assert "seller agent" in live["source"]

    # NorthBox kept its own record of the conversation.
    actions = [e["in"] for e in northbox.store.recent(100) if e["order_id"] == oid]
    for a in ("request_quote", "finalize", "create_payment", "authorize", "release", "payment_status"):
        assert a in actions


@pytest.mark.anyio
async def test_auto_pay_falls_back_to_approval_because_the_seller_has_its_own_paypal(monkeypatch):
    only_northbox(monkeypatch)
    async with flows.client() as c:
        await pw.connect(c)
        o = (await pw.place(c, pw.request(limit=500)))["order"]
        assert o["winner"]["seller_id"] == "northbox"
        # The agent signed the payment on its own, but NorthBox can't charge a PayPal saved with
        # Procuro's PayPal app: it refuses with a signed AP2 error receipt, so the owner approves instead.
        assert o["status"] == "awaiting_approval"
        refusal = o["ap2"]["refusals"][0]
        assert refusal["error"] == "payment_instrument_unavailable"
        body = mandates.verify_receipt(refusal["receipt"], mandates.jwk_from(o["sellers"]["northbox"]["jwk"]))
        assert body["status"] == "Error"
        r = (await c.get(f"/api/orders/{o['id']}/receipt")).json()
        assert "Error receipt" in [s["type"] for s in r["steps"]]


@pytest.mark.anyio
async def test_it_refuses_mandates_not_signed_by_the_trusted_buyer():
    """An open checkout mandate signed by some other key is refused before any checkout is signed."""
    import time
    from jwcrypto.jwk import JWK
    real = mandates._keys.get(mandates.SURFACE)
    mandates._keys[mandates.SURFACE] = JWK.generate(kty="EC", crv="P-256", kid="impostor")
    try:
        forged = mandates.open_mandates(item="cake boxes", product_id=None, quantity=100,
                                        merchants=[mandates.merchant("northbox", "NorthBox", NORTHBOX)],
                                        approval_limit=1000, currency="USD", expires_at=int(time.time()) + 3600)
    finally:
        if real is not None:
            mandates._keys[mandates.SURFACE] = real
        else:
            mandates._keys.pop(mandates.SURFACE, None)
    await a2a.send(NORTHBOX, {"action": "request_quote", "order_id": "ord_forged", "item": "cake boxes",
                              "quantity": 100, "currency": "USD"})
    with pytest.raises(a2a.A2AError, match="open checkout mandate"):
        await a2a.send(NORTHBOX, {"action": "finalize", "order_id": "ord_forged", "unit_price": 99,
                                  mandates.CHECKOUT_MANDATE_KEY: forged["open_checkout"]})