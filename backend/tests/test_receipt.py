"""The receipt: signed steps in order, fingerprints, links to PayPal, and checks that catch tampering."""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

import test_flows as flows
import test_paypal_wallet as pw
from app import receipt
from app.store import store

VERIFY = Path(__file__).resolve().parents[1] / "scripts" / "verify_receipt.py"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def types(r):
    return [s["type"] for s in r["steps"]]


@pytest.mark.anyio
async def test_a_paid_order_has_a_complete_receipt_that_checks_out(tmp_path):
    async with flows.client() as c:
        order = await flows.negotiate_and_hold(c)
        oid = order["id"]
        r = (await c.get(f"/api/orders/{oid}/receipt")).json()
        assert types(r) == ["intent_mandate", "cart_mandate", "payment_mandate"]
        not_yet = [x for x in r["checks"] if x["ok"] is None]
        assert not_yet and "held" in not_yet[0]["detail"]

        assert (await c.post(f"/api/orders/{oid}/delivered")).json()["status"] == "completed"
        r = (await c.get(f"/api/orders/{oid}/receipt")).json()
        assert types(r) == ["intent_mandate", "cart_mandate", "payment_mandate", "release_mandate"]
        assert all(s["verified"] for s in r["steps"])
        assert all(x["ok"] for x in r["checks"]), r["checks"]
        cart, pay, rel = r["steps"][1], r["steps"][2], r["steps"][3]
        assert "A2A card" in cart["key_from"] and cart["signer"].startswith("seller:")
        assert pay["refers_to"][0] == {"field": "cart_mandate_id", "id": cart["id"], "ok": True,
                                       "step": "cart_mandate"}
        kinds = [x["kind"] for x in pay["paypal"]]
        assert kinds == ["order", "authorization"]
        assert rel["paypal"][0]["kind"] == "capture" and rel["paypal"][0]["request_id"].startswith("capture-")
        assert r["fingerprint"] == receipt.receipt_fingerprint([s["hash"] for s in r["steps"]])

        # PayPal's own records agree with the signed steps.
        live = (await c.get(f"/api/orders/{oid}/receipt/paypal")).json()
        assert [i["ok"] for i in live["items"]] == [True, True]

        # Anyone can check the download without Procuro.
        path = tmp_path / "receipt.json"
        path.write_text(json.dumps(r))
        out = subprocess.run([sys.executable, str(VERIFY), str(path)], capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "matches" in out.stdout and "FAILS" not in out.stdout

        # Change one number in a signed step: its signature fails, and so does the offline check.
        o = store.get_order(oid)
        o["cart_mandate"]["payload"]["total"] = round(o["cart_mandate"]["payload"]["total"] - 50, 2)
        store.save_order(o)
        bad = (await c.get(f"/api/orders/{oid}/receipt")).json()
        assert bad["fingerprint"] != r["fingerprint"]
        assert not bad["steps"][1]["verified"]
        sig = next(x for x in bad["checks"] if x["label"] == "Every signature checks out")
        assert sig["ok"] is False and "signed offer" in sig["detail"]
        paid = next(x for x in bad["checks"] if x["label"].startswith("You paid for exactly"))
        assert paid["ok"] is False
        path.write_text(json.dumps(bad))
        assert subprocess.run([sys.executable, str(VERIFY), str(path)], capture_output=True).returncode == 1


@pytest.mark.anyio
async def test_partial_capture_and_reschedule_show_up_on_the_receipt():
    async with flows.client() as c:
        order = await flows.negotiate_and_hold(c)
        oid = order["id"]
        new_day = (dt.date.fromisoformat(order["winner"]["delivery_date"]) + dt.timedelta(days=3)).isoformat()
        assert (await c.post(f"/api/orders/{oid}/reschedule", json={"delivery_date": new_day})).json()["accepted"]
        await c.post(f"/api/orders/{oid}/report", data={"note": "60 crushed, logo is crooked on most of them."})
        r = (await c.get(f"/api/orders/{oid}/receipt")).json()
        assert types(r) == ["intent_mandate", "cart_mandate", "payment_mandate", "cart_amendment", "release_mandate"]
        assert all(x["ok"] for x in r["checks"]), r["checks"]
        assert any(x["label"] == "Date changes didn't change the price" for x in r["checks"])
        o = store.get_order(oid)
        rel = r["steps"][-1]
        assert rel["paypal"][0]["amount"] == pytest.approx(o["paypal"]["captured"])
        assert o["paypal"]["captured"] < o["paypal"]["authorized_amount"]


@pytest.mark.anyio
async def test_auto_pay_receipt_names_the_limit_it_paid_under():
    async with flows.client() as c:
        await pw.connect(c)
        o = (await pw.place(c, pw.request(limit=500)))["order"]
        r = (await c.get(f"/api/orders/{o['id']}/receipt")).json()
        pay = r["steps"][2]
        assert "on its own" in pay["summary"]
        rule = next(x for x in r["checks"] if x["label"] == "Your approval rule was followed")
        assert rule["ok"] and "under your" in rule["detail"]
        assert pay["paypal"][0]["request_id"] == f"create-{o['id']}-vault"


@pytest.mark.anyio
async def test_receipt_checks_paypal_through_the_real_sdk(fake_paypal):
    async with flows.client() as c:
        order = await flows.negotiate_and_hold(c)
        await c.post(f"/api/orders/{order['id']}/delivered")
        live = (await c.get(f"/api/orders/{order['id']}/receipt/paypal")).json()
        assert live.get("environment") == "sandbox" and [i["ok"] for i in live["items"]] == [True, True], live
        # A capture PayPal doesn't know about is reported, not hidden.
        o = store.get_order(order["id"])
        o["paypal"]["capture_id"] = "CAP-NOT-REAL"
        store.save_order(o)
        live = (await c.get(f"/api/orders/{order['id']}/receipt/paypal")).json()
        assert "INVALID_RESOURCE_ID" in live["error"]


fake_paypal = pw.fake_paypal