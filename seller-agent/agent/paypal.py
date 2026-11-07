"""Copied from Procuro's backend (app/paypal.py) so this agent stands alone; only `client()` differs.

PayPal (sandbox) through PayPal's official Server SDK (`paypal-server-sdk`).

Money flow used by Procuro:
  1. seller agent creates an order with intent=AUTHORIZE
       a. normally the owner approves it on PayPal's page, then the seller authorizes it
       b. with a saved PayPal account (Vault v3 payment token) the order is authorized in that same call:
          no redirect, so the buying agent can pay on its own inside the owner's limits
  2. funds are HELD, not taken
  3a. delivery confirmed -> capture the full amount
  3b. problem -> capture only the agreed amount with final_capture=true (rest released)
  3c. nothing is owed -> void the authorization
  Holds are honored for 3 days and can be reauthorized up to day 29.

Every call that moves money carries a PayPal-Request-Id built from what it does (the order, the
authorization, the amount), not a random one. If an answer is lost and the call is retried, PayPal
returns its first answer instead of creating a second order or capturing twice. That makes POST safe
to retry, so the SDK retries it too.

The SDK is synchronous; calls run in a worker thread so the API stays responsive.
"""
import asyncio
import json
import time
import uuid

import httpx
from paypalserversdk.configuration import Configuration, Environment
from paypalserversdk.exceptions.api_exception import ApiException
from paypalserversdk.http.auth.o_auth_2 import ClientCredentialsAuthCredentials
from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient

from . import config


class PayPalError(Exception):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


def _money(value: float, currency: str) -> dict:
    return {"currency_code": currency, "value": f"{value:.2f}"}


def _auth_fields(auth: dict, payer: dict | None = None) -> dict:
    return {
        "authorization_id": auth["id"],
        "status": auth["status"],
        "amount": float(auth["amount"]["value"]),
        "expiration_time": auth.get("expiration_time"),
        "payer_email": (payer or {}).get("email_address"),
    }


def _lookup_fields(data: dict) -> dict:
    """What PayPal's own record of an authorization or capture says."""
    up = next((l.get("href", "") for l in data.get("links", []) if l.get("rel") == "up"), "")
    amount = data.get("amount") or {}
    return {
        "id": data.get("id"),
        "status": data.get("status"),
        "amount": float(amount["value"]) if amount.get("value") is not None else None,
        "currency": amount.get("currency_code"),
        "final_capture": data.get("final_capture"),
        # A capture links "up" to the authorization it was taken from.
        "authorization_id": up.rstrip("/").rsplit("/", 1)[-1] if "/authorizations/" in up else None,
        "create_time": data.get("create_time"),
        "update_time": data.get("update_time"),
        "expiration_time": data.get("expiration_time"),
    }


class PayPalClient:
    def __init__(self, client_id: str, client_secret: str, base: str | None = None):
        base = base or config.PAYPAL_BASE
        if not client_id or not client_secret:
            raise PayPalError(
                "PayPal sandbox credentials are missing. Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in "
                "backend/.env (or Heroku config vars), then restart the server.")
        self.client_id, self.client_secret, self.base = client_id, client_secret, base.rstrip("/")
        live = "sandbox" not in self.base
        env = Environment.PRODUCTION if live else Environment.SANDBOX
        cfg = Configuration(
            environment=env,
            timeout=30,
            max_retries=2,
            backoff_factor=2,
            retry_methods=["GET", "PUT", "POST"],  # safe: every write carries a PayPal-Request-Id
            client_credentials_auth_credentials=ClientCredentialsAuthCredentials(
                o_auth_client_id=client_id, o_auth_client_secret=client_secret),
        )
        if self.base not in ("https://api-m.paypal.com", "https://api-m.sandbox.paypal.com"):
            # A custom base (tests point this at a local stand-in for PayPal).
            cfg.environments = {env: {server: self.base for server in Configuration.environments[env]}}
        self.sdk = PaypalServersdkClient(config=cfg)
        self._token: str | None = None
        self._token_exp = 0.0

    async def _sdk(self, what: str, fn, options: dict) -> dict:
        def run():
            try:
                resp = fn(options)
            except ApiException as exc:
                body = {}
                try:
                    body = json.loads(exc.response.text) if exc.response is not None and exc.response.text else {}
                except ValueError:
                    pass
                issue = (body.get("details") or [{}])[0].get("issue") or body.get("name") or "ERROR"
                raise PayPalError(f"PayPal {what} failed ({exc.response_code}) {issue}", body) from None
            return json.loads(resp.text) if resp.text else {}
        return await asyncio.to_thread(run)

    # ---------- orders ----------

    async def create_order(self, *, reference_id: str, description: str, items: list[dict], total: float,
                           currency: str, return_url: str, cancel_url: str, brand_name: str,
                           vault_id: str | None = None) -> dict:
        unit = {
            "reference_id": reference_id,
            "custom_id": reference_id,
            "description": description[:127],
            "amount": {**_money(total, currency), "breakdown": {"item_total": _money(total, currency)}},
            "items": [{
                "name": i["name"][:127],
                "quantity": str(i["quantity"]),
                "unit_amount": _money(i["unit_price"], currency),
                "category": "PHYSICAL_GOODS",
                **({"sku": str(i["product_id"])[:127]} if i.get("product_id") else {}),
            } for i in items],
        }
        if vault_id:
            source = {"paypal": {"vault_id": vault_id}}
        else:
            source = {"paypal": {"experience_context": {
                "brand_name": brand_name[:127], "user_action": "CONTINUE", "shipping_preference": "NO_SHIPPING",
                "return_url": return_url, "cancel_url": cancel_url,
            }}}
        body = {"intent": "AUTHORIZE", "purchase_units": [unit], "payment_source": source}
        data = await self._sdk("create order", self.sdk.orders.create_order, {
            "body": body, "prefer": "return=representation",
            "paypal_request_id": f"create-{reference_id}{'-vault' if vault_id else ''}"})
        out = {"id": data["id"], "status": data.get("status"),
               "approve_url": next((l["href"] for l in data.get("links", [])
                                    if l.get("rel") in ("payer-action", "approve")), None)}
        auths = (data.get("purchase_units") or [{}])[0].get("payments", {}).get("authorizations") or []
        if auths:  # paid with a saved PayPal account: already authorized
            out["authorization"] = _auth_fields(auths[0], data.get("payer"))
        elif vault_id:
            raise PayPalError(f"PayPal returned no authorization for the saved account (status {data.get('status')})",
                              data)
        return out

    async def authorize_order(self, order_id: str) -> dict:
        data = await self._sdk("authorize", self.sdk.orders.authorize_order, {
            "id": order_id, "prefer": "return=representation", "paypal_request_id": f"auth-{order_id}"})
        return _auth_fields(data["purchase_units"][0]["payments"]["authorizations"][0], data.get("payer"))

    # ---------- payments ----------

    async def capture(self, authorization_id: str, amount: float, currency: str, final: bool = True,
                      note: str = "") -> dict:
        body = {"amount": _money(amount, currency), "final_capture": final}
        if note:
            body["note_to_payer"] = note[:255]
        data = await self._sdk("capture", self.sdk.payments.capture_authorized_payment, {
            "authorization_id": authorization_id, "body": body, "prefer": "return=representation",
            "paypal_request_id": f"capture-{authorization_id}-{amount:.2f}"})
        return {"capture_id": data["id"], "status": data["status"], "amount": float(data["amount"]["value"])}

    async def void(self, authorization_id: str) -> dict:
        await self._sdk("void", self.sdk.payments.void_payment, {
            "authorization_id": authorization_id, "paypal_request_id": f"void-{authorization_id}"})
        return {"status": "VOIDED"}

    async def reauthorize(self, authorization_id: str, amount: float, currency: str) -> dict:
        data = await self._sdk("reauthorize", self.sdk.payments.reauthorize_payment, {
            "authorization_id": authorization_id, "body": {"amount": _money(amount, currency)},
            "prefer": "return=representation", "paypal_request_id": f"reauth-{authorization_id}"})
        return {"authorization_id": data["id"], "status": data["status"],
                "expiration_time": data.get("expiration_time")}

    # ---------- read back (for the receipt) ----------

    async def get_authorization(self, authorization_id: str) -> dict:
        data = await self._sdk("look up authorization", self.sdk.payments.get_authorized_payment,
                               {"authorization_id": authorization_id})
        return _lookup_fields(data)

    async def get_capture(self, capture_id: str) -> dict:
        data = await self._sdk("look up capture", self.sdk.payments.get_captured_payment, {"capture_id": capture_id})
        return _lookup_fields(data)

    # ---------- Vault v3: save the owner's PayPal once ----------

    async def create_setup_token(self, *, return_url: str, cancel_url: str, brand_name: str,
                                 description: str) -> dict:
        body = {"payment_source": {"paypal": {
            "description": description[:127],
            "usage_pattern": "IMMEDIATE",
            "usage_type": "MERCHANT",
            "customer_type": "CONSUMER",
            "permit_multiple_payment_tokens": True,
            "experience_context": {"brand_name": brand_name[:127], "return_url": return_url,
                                   "cancel_url": cancel_url, "shipping_preference": "NO_SHIPPING"},
        }}}
        data = await self._sdk("save PayPal (setup token)", self.sdk.vault.create_setup_token, {
            "body": body, "paypal_request_id": f"setup-{uuid.uuid4().hex}"})
        approve = next((l["href"] for l in data.get("links", []) if l.get("rel") == "approve"), None)
        return {"id": data["id"], "status": data.get("status"), "approve_url": approve}

    async def create_payment_token(self, setup_token_id: str) -> dict:
        body = {"payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}}
        data = await self._sdk("save PayPal (payment token)", self.sdk.vault.create_payment_token, {
            "body": body, "paypal_request_id": f"ptoken-{setup_token_id}"})
        pp = (data.get("payment_source") or {}).get("paypal") or {}
        name = pp.get("name") or {}
        return {"payment_token_id": data["id"], "customer_id": (data.get("customer") or {}).get("id"),
                "payer_email": pp.get("email_address"),
                "payer_name": " ".join(p for p in (name.get("given_name"), name.get("surname")) if p)
                or name.get("full_name") or "",
                "payer_id": pp.get("account_id")}

    async def delete_payment_token(self, payment_token_id: str) -> None:
        # This SDK method takes the id itself, not an options dict.
        await self._sdk("forget saved PayPal", lambda _: self.sdk.vault.delete_payment_token(payment_token_id), {})

    # ---------- webhooks (the SDK has no notifications API, so this one call is plain REST) ----------

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = await client.post(f"{self.base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                              auth=(self.client_id, self.client_secret))
        if r.status_code >= 400:
            raise PayPalError("PayPal authentication failed", r.json() if r.content else {})
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3000))
        return self._token

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
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._access_token(client)
            r = await client.post(f"{self.base}/v1/notifications/verify-webhook-signature", json=body,
                                  headers={"Authorization": f"Bearer {token}"})
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise PayPalError(f"PayPal webhook verification failed ({r.status_code})", data)
        return data.get("verification_status") == "SUCCESS"


class MockPayPalClient:
    """In-memory stand-in for local development and tests only (PAYPAL_MODE=mock)."""

    orders: dict[str, dict] = {}
    auths: dict[str, dict] = {}
    captures: dict[str, dict] = {}
    setup_tokens: dict[str, dict] = {}
    payment_tokens: dict[str, dict] = {}
    client_id = "mock"

    def __init__(self, *_: str, **__: str):
        pass

    def _new_auth(self, amount: float) -> dict:
        aid = f"AUTH-{uuid.uuid4().hex[:10].upper()}"
        self.auths[aid] = {"amount": amount, "status": "CREATED"}
        return {"authorization_id": aid, "status": "CREATED", "amount": amount,
                "expiration_time": None, "payer_email": "maya@example.com"}

    async def create_order(self, *, reference_id, description, items, total, currency, return_url, cancel_url,
                           brand_name, vault_id=None) -> dict:
        oid = f"MOCK-{uuid.uuid4().hex[:10].upper()}"
        self.orders[oid] = {"total": total, "currency": currency, "status": "CREATED"}
        if vault_id:
            if vault_id not in self.payment_tokens:
                raise PayPalError("PayPal create order failed (422) INVALID_PAYMENT_TOKEN")
            return {"id": oid, "status": "COMPLETED", "approve_url": None, "authorization": self._new_auth(total)}
        return {"id": oid, "status": "PAYER_ACTION_REQUIRED", "approve_url": f"{return_url}&token={oid}"}

    async def authorize_order(self, order_id: str) -> dict:
        return self._new_auth(self.orders[order_id]["total"])

    async def capture(self, authorization_id, amount, currency, final=True, note="") -> dict:
        a = self.auths[authorization_id]
        if amount <= 0:
            raise PayPalError("PayPal capture failed (422) CANNOT_BE_ZERO_OR_NEGATIVE")
        if amount > a["amount"] + 0.001:
            raise PayPalError("capture exceeds authorization")
        a["status"] = "CAPTURED" if final else "PARTIALLY_CAPTURED"
        cid = f"CAP-{uuid.uuid4().hex[:10].upper()}"
        self.captures[cid] = {"amount": amount, "currency": currency, "authorization_id": authorization_id,
                              "final": final}
        return {"capture_id": cid, "status": "COMPLETED", "amount": amount}

    async def void(self, authorization_id) -> dict:
        self.auths[authorization_id]["status"] = "VOIDED"
        return {"status": "VOIDED"}

    async def get_authorization(self, authorization_id: str) -> dict:
        if authorization_id not in self.auths:
            raise PayPalError("PayPal look up authorization failed (404) INVALID_RESOURCE_ID")
        a = self.auths[authorization_id]
        return {"id": authorization_id, "status": a["status"], "amount": a["amount"], "currency": "USD",
                "final_capture": None, "authorization_id": None, "create_time": None, "update_time": None,
                "expiration_time": None}

    async def get_capture(self, capture_id: str) -> dict:
        if capture_id not in self.captures:
            raise PayPalError("PayPal look up capture failed (404) INVALID_RESOURCE_ID")
        c = self.captures[capture_id]
        return {"id": capture_id, "status": "COMPLETED", "amount": c["amount"], "currency": c["currency"],
                "final_capture": c["final"], "authorization_id": c["authorization_id"], "create_time": None,
                "update_time": None, "expiration_time": None}

    async def reauthorize(self, authorization_id, amount, currency) -> dict:
        aid = f"AUTH-{uuid.uuid4().hex[:10].upper()}"
        self.auths[aid] = {"amount": amount, "status": "CREATED"}
        return {"authorization_id": aid, "status": "CREATED", "expiration_time": None}

    async def create_setup_token(self, *, return_url, cancel_url, brand_name, description) -> dict:
        sid = f"SETUP-{uuid.uuid4().hex[:10].upper()}"
        self.setup_tokens[sid] = {"status": "PAYER_ACTION_REQUIRED"}
        sep = "&" if "?" in return_url else "?"
        return {"id": sid, "status": "PAYER_ACTION_REQUIRED", "approve_url": f"{return_url}{sep}approval_token_id={sid}"}

    async def create_payment_token(self, setup_token_id: str) -> dict:
        if setup_token_id not in self.setup_tokens:
            raise PayPalError("PayPal save PayPal (payment token) failed (422) TOKEN_NOT_APPROVED")
        tid = f"PTOKEN-{uuid.uuid4().hex[:10].upper()}"
        self.payment_tokens[tid] = {"setup": setup_token_id}
        return {"payment_token_id": tid, "customer_id": "CUST-MOCK", "payer_email": "maya@example.com",
                "payer_name": "Maya", "payer_id": "PAYERMOCK"}

    async def delete_payment_token(self, payment_token_id: str) -> None:
        self.payment_tokens.pop(payment_token_id, None)

    async def verify_webhook(self, headers, event, webhook_id) -> bool:
        return True


_clients: dict[str, object] = {}


_instance = None


def client():
    """This agent's own PayPal account (or the in-memory stand-in when PAYPAL_MODE=mock)."""
    global _instance
    if _instance is None:
        _instance = (MockPayPalClient() if config.PAYPAL_MODE == "mock"
                     else PayPalClient(config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET, base=config.PAYPAL_BASE))
    return _instance
