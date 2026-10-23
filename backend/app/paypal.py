"""PayPal Orders v2 + Payments v2 (sandbox).

Money flow used by Mandate:
  1. seller agent creates an order with intent=AUTHORIZE (its own PayPal business account)
  2. the buyer approves it in PayPal's own approval page
  3. seller agent authorizes -> funds are HELD, not taken
  4a. delivery confirmed -> capture the full amount
  4b. problem -> capture only the agreed amount with final_capture=true (rest released)
  4c. order never happens -> void the authorization
  Holds are honored for 3 days and can be reauthorized up to day 29.
"""
import time
import uuid

import httpx

from . import config


class PayPalError(Exception):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


def _money(value: float, currency: str) -> dict:
    return {"currency_code": currency, "value": f"{value:.2f}"}


class PayPalClient:
    def __init__(self, client_id: str, client_secret: str, base: str = config.PAYPAL_BASE):
        if not client_id or not client_secret:
            raise PayPalError("PayPal sandbox credentials are not configured")
        self.client_id, self.client_secret, self.base = client_id, client_secret, base
        self._token: str | None = None
        self._token_exp = 0.0

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = await client.post(
            f"{self.base}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        if r.status_code >= 400:
            raise PayPalError("PayPal authentication failed", r.json() if r.content else {})
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3000))
        return self._token

    async def _call(self, method: str, path: str, json: dict | None = None, request_id: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._access_token(client)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
                "PayPal-Request-Id": request_id or uuid.uuid4().hex,
            }
            r = await client.request(method, f"{self.base}{path}", json=json, headers=headers)
            data = r.json() if r.content else {}
            if r.status_code >= 400:
                raise PayPalError(f"PayPal {method} {path} failed ({r.status_code})", data)
            return data

    async def create_order(self, *, reference_id: str, description: str, items: list[dict], total: float,
                           currency: str, return_url: str, cancel_url: str, brand_name: str) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": reference_id,
                "custom_id": reference_id,
                "description": description[:127],
                "amount": {**_money(total, currency), "breakdown": {"item_total": _money(total, currency)}},
                "items": [{
                    "name": i["name"][:127],
                    "quantity": str(i["quantity"]),
                    "unit_amount": _money(i["unit_price"], currency),
                    "category": "PHYSICAL_GOODS",
                } for i in items],
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": brand_name[:127],
                "user_action": "CONTINUE",
                "shipping_preference": "NO_SHIPPING",
                "return_url": return_url,
                "cancel_url": cancel_url,
            }}},
        }
        data = await self._call("POST", "/v2/checkout/orders", body, request_id=f"create-{reference_id}")
        approve = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
        return {"id": data["id"], "status": data.get("status"), "approve_url": approve}

    async def authorize_order(self, order_id: str) -> dict:
        data = await self._call("POST", f"/v2/checkout/orders/{order_id}/authorize", {}, request_id=f"auth-{order_id}")
        auth = data["purchase_units"][0]["payments"]["authorizations"][0]
        payer = data.get("payer") or {}
        return {
            "authorization_id": auth["id"],
            "status": auth["status"],
            "amount": float(auth["amount"]["value"]),
            "expiration_time": auth.get("expiration_time"),
            "payer_email": payer.get("email_address"),
        }

    async def capture(self, authorization_id: str, amount: float, currency: str, final: bool = True,
                      note: str = "") -> dict:
        body = {"amount": _money(amount, currency), "final_capture": final}
        if note:
            body["note_to_payer"] = note[:255]
        data = await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/capture", body)
        return {"capture_id": data["id"], "status": data["status"], "amount": float(data["amount"]["value"])}

    async def void(self, authorization_id: str) -> dict:
        await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/void", None)
        return {"status": "VOIDED"}

    async def reauthorize(self, authorization_id: str, amount: float, currency: str) -> dict:
        data = await self._call("POST", f"/v2/payments/authorizations/{authorization_id}/reauthorize",
                                {"amount": _money(amount, currency)})
        return {"authorization_id": data["id"], "status": data["status"],
                "expiration_time": data.get("expiration_time")}

    async def verify_webhook(self, headers: dict, event: dict, webhook_id: str) -> bool:
        body = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        data = await self._call("POST", "/v1/notifications/verify-webhook-signature", body)
        return data.get("verification_status") == "SUCCESS"


class MockPayPalClient:
    """In-memory stand-in for local development and tests only (PAYPAL_MODE=mock)."""

    orders: dict[str, dict] = {}
    auths: dict[str, dict] = {}

    def __init__(self, *_: str, **__: str):
        pass

    async def create_order(self, *, reference_id, description, items, total, currency, return_url, cancel_url,
                           brand_name) -> dict:
        oid = f"MOCK-{uuid.uuid4().hex[:10].upper()}"
        self.orders[oid] = {"total": total, "currency": currency, "status": "CREATED"}
        return {"id": oid, "status": "PAYER_ACTION_REQUIRED", "approve_url": f"{return_url}&token={oid}"}

    async def authorize_order(self, order_id: str) -> dict:
        o = self.orders[order_id]
        aid = f"AUTH-{uuid.uuid4().hex[:10].upper()}"
        self.auths[aid] = {"amount": o["total"], "status": "CREATED"}
        return {"authorization_id": aid, "status": "CREATED", "amount": o["total"],
                "expiration_time": None, "payer_email": "maya@example.com"}

    async def capture(self, authorization_id, amount, currency, final=True, note="") -> dict:
        a = self.auths[authorization_id]
        if amount > a["amount"] + 0.001:
            raise PayPalError("capture exceeds authorization")
        a["status"] = "CAPTURED" if final else "PARTIALLY_CAPTURED"
        return {"capture_id": f"CAP-{uuid.uuid4().hex[:10].upper()}", "status": "COMPLETED", "amount": amount}

    async def void(self, authorization_id) -> dict:
        self.auths[authorization_id]["status"] = "VOIDED"
        return {"status": "VOIDED"}

    async def reauthorize(self, authorization_id, amount, currency) -> dict:
        aid = f"AUTH-{uuid.uuid4().hex[:10].upper()}"
        self.auths[aid] = {"amount": amount, "status": "CREATED"}
        return {"authorization_id": aid, "status": "CREATED", "expiration_time": None}

    async def verify_webhook(self, headers, event, webhook_id) -> bool:
        return True


_clients: dict[str, object] = {}


def client_for(seller_id: str):
    if config.PAYPAL_MODE == "mock":
        return MockPayPalClient()
    if seller_id not in _clients:
        cid, secret = config.seller_paypal_credentials(seller_id)
        _clients[seller_id] = PayPalClient(cid, secret)
    return _clients[seller_id]
