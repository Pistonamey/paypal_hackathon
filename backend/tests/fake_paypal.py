"""A small stand-in for the PayPal REST API, for tests only.

It answers the endpoints Procuro uses (OAuth, Orders v2, Payments v2, Vault v3, Invoicing v2) with the
same shapes PayPal returns, enforces PayPal-Request-Id idempotency the way PayPal does (same id, same
answer, nothing done twice), and refuses a zero capture like the real sandbox
(CANNOT_BE_ZERO_OR_NEGATIVE). The real SDK and the real Agent Toolkit talk to it over HTTP.
"""
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12].upper()}"


class FakePayPal:
    def __init__(self):
        self.app = FastAPI()
        self.orders: dict[str, dict] = {}
        self.auths: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.setup_tokens: dict[str, dict] = {}
        self.payment_tokens: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.seen: dict[str, tuple[int, dict]] = {}  # PayPal-Request-Id -> first answer
        self.calls: list[tuple[str, str, str | None]] = []  # (method, path, request id)
        self.base = ""
        self._routes()

    # ---------- helpers ----------
    def _once(self, request: Request, fn):
        """PayPal-Request-Id: a repeat returns the first answer and does nothing again."""
        rid = request.headers.get("paypal-request-id")
        self.calls.append((request.method, request.url.path, rid))
        if rid and rid in self.seen:
            status, body = self.seen[rid]
            return JSONResponse(body, status_code=status)
        status, body = fn()
        if rid:
            self.seen[rid] = (status, body)
        return JSONResponse(body, status_code=status)

    @staticmethod
    def _error(status: int, issue: str, field: str = "") -> tuple[int, dict]:
        return status, {"name": "UNPROCESSABLE_ENTITY", "message": "failed business validation",
                        "details": [{"field": field, "issue": issue}]}

    def _auth_body(self, aid: str) -> dict:
        a = self.auths[aid]
        return {"id": aid, "status": a["status"], "amount": a["amount"],
                "expiration_time": "2026-12-31T00:00:00Z"}

    def _order_body(self, oid: str) -> dict:
        o = self.orders[oid]
        unit = {"reference_id": o["reference_id"], "amount": o["amount"]}
        if o.get("auth"):
            unit["payments"] = {"authorizations": [self._auth_body(o["auth"])]}
        body = {"id": oid, "intent": "AUTHORIZE", "status": o["status"], "purchase_units": [unit],
                "links": [{"rel": "payer-action", "href": f"https://sandbox.paypal.test/checkoutnow?token={oid}"}]}
        if o.get("payer"):
            body["payer"] = o["payer"]
        return body

    def _routes(self):
        app = self.app

        @app.post("/v1/oauth2/token")
        async def token():
            return {"access_token": "A21-fake-token", "token_type": "Bearer", "expires_in": 32400,
                    "scope": "https://uri.paypal.com/services/invoicing"}

        # ---- Orders v2 ----
        @app.post("/v2/checkout/orders")
        async def create_order(request: Request):
            body = await request.json()

            def do():
                oid = _id("ORDER")
                pu = body["purchase_units"][0]
                vault_id = ((body.get("payment_source") or {}).get("paypal") or {}).get("vault_id")
                self.orders[oid] = {"reference_id": pu.get("reference_id"), "amount": pu["amount"],
                                    "status": "PAYER_ACTION_REQUIRED", "vault_id": vault_id}
                if vault_id:
                    if vault_id not in self.payment_tokens:
                        return self._error(422, "INVALID_PAYMENT_TOKEN", "/payment_source/paypal/vault_id")
                    # A saved PayPal account: the order is authorized in the same call.
                    aid = _id("AUTH")
                    self.auths[aid] = {"amount": pu["amount"], "status": "CREATED"}
                    self.orders[oid].update(status="COMPLETED", auth=aid,
                                            payer=self.payment_tokens[vault_id]["payer"])
                return 201, self._order_body(oid)
            return self._once(request, do)

        @app.post("/v2/checkout/orders/{oid}/authorize")
        async def authorize(oid: str, request: Request):
            def do():
                o = self.orders[oid]
                aid = _id("AUTH")
                self.auths[aid] = {"amount": o["amount"], "status": "CREATED"}
                o.update(status="COMPLETED", auth=aid, payer={"email_address": "maya@example.com"})
                return 201, self._order_body(oid)
            return self._once(request, do)

        # ---- Payments v2 ----
        @app.post("/v2/payments/authorizations/{aid}/capture")
        async def capture(aid: str, request: Request):
            body = await request.json()

            def do():
                value = float(body["amount"]["value"])
                if value <= 0:
                    return self._error(422, "CANNOT_BE_ZERO_OR_NEGATIVE", "/amount/value")
                if value > float(self.auths[aid]["amount"]["value"]) + 0.001:
                    return self._error(422, "AMOUNT_GREATER_THAN_AUTHORIZED", "/amount/value")
                self.auths[aid]["status"] = "CAPTURED"
                cap = {"id": _id("CAP"), "status": "COMPLETED", "amount": body["amount"],
                       "final_capture": body.get("final_capture", True),
                       "links": [{"rel": "up", "method": "GET",
                                  "href": f"{self.base}/v2/payments/authorizations/{aid}"}]}
                self.captures[cap["id"]] = cap
                return 201, cap
            return self._once(request, do)

        @app.get("/v2/payments/authorizations/{aid}")
        async def get_auth(aid: str):
            if aid not in self.auths:
                return JSONResponse(self._error(404, "INVALID_RESOURCE_ID")[1], status_code=404)
            return self._auth_body(aid)

        @app.get("/v2/payments/captures/{cid}")
        async def get_capture(cid: str):
            if cid not in self.captures:
                return JSONResponse(self._error(404, "INVALID_RESOURCE_ID")[1], status_code=404)
            return self.captures[cid]

        @app.post("/v2/payments/authorizations/{aid}/void")
        async def void(aid: str, request: Request):
            def do():
                self.auths[aid]["status"] = "VOIDED"
                return 200, self._auth_body(aid)
            return self._once(request, do)

        @app.post("/v2/payments/authorizations/{aid}/reauthorize")
        async def reauthorize(aid: str, request: Request):
            body = await request.json()

            def do():
                new = _id("AUTH")
                self.auths[new] = {"amount": body["amount"], "status": "CREATED"}
                return 201, self._auth_body(new)
            return self._once(request, do)

        # ---- Vault v3 ----
        @app.post("/v3/vault/setup-tokens")
        async def setup_token(request: Request):
            body = await request.json()

            def do():
                sid = _id("SETUP")
                ctx = body["payment_source"]["paypal"]["experience_context"]
                self.setup_tokens[sid] = {"status": "PAYER_ACTION_REQUIRED", "return_url": ctx["return_url"]}
                return 200, {"id": sid, "status": "PAYER_ACTION_REQUIRED",
                             "links": [{"rel": "approve", "href": f"https://sandbox.paypal.test/agreements/approve?approval_session_id={sid}"}]}
            return self._once(request, do)

        @app.post("/v3/vault/payment-tokens")
        async def payment_token(request: Request):
            body = await request.json()

            def do():
                setup = body["payment_source"]["token"]["id"]
                if setup not in self.setup_tokens or self.setup_tokens[setup]["status"] != "APPROVED":
                    return self._error(422, "TOKEN_NOT_APPROVED", "/payment_source/token/id")
                tid = _id("PTOKEN")
                payer = {"email_address": "maya@bakery.example", "name": {"given_name": "Maya", "surname": "Rao"},
                         "account_id": "PAYERMAYA01"}
                self.payment_tokens[tid] = {"payer": payer}
                return 201, {"id": tid, "customer": {"id": "CUST-MAYA"},
                             "payment_source": {"paypal": {**payer}}}
            return self._once(request, do)

        @app.delete("/v3/vault/payment-tokens/{tid}")
        async def delete_token(tid: str, request: Request):
            self.calls.append(("DELETE", request.url.path, request.headers.get("paypal-request-id")))
            self.payment_tokens.pop(tid, None)
            return Response(status_code=204)

        # ---- Invoicing v2 (what the Agent Toolkit calls) ----
        @app.post("/v2/invoicing/invoices")
        async def create_invoice(request: Request):
            body = await request.json()
            self.calls.append(("POST", request.url.path, request.headers.get("paypal-request-id")))
            iid = "INV2-" + "-".join(uuid.uuid4().hex[i:i + 4].upper() for i in range(0, 20, 4))
            self.invoices[iid] = {"status": "DRAFT", "body": body}
            return JSONResponse({"rel": "self", "href": f"{self.base}/v2/invoicing/invoices/{iid}", "method": "GET"},
                                status_code=201)

        @app.post("/v2/invoicing/invoices/{iid}/send")
        async def send_invoice(iid: str, request: Request):
            self.calls.append(("POST", request.url.path, request.headers.get("paypal-request-id")))
            self.invoices[iid]["status"] = "SENT"
            return JSONResponse({"rel": "payer-view",
                                 "href": f"https://www.sandbox.paypal.test/invoice/p/#{iid}", "method": "GET"})

        @app.post("/v2/invoicing/invoices/{iid}/generate-qr-code")
        async def qr(iid: str):
            # The real API answers with a base64 PNG in a multipart body; the toolkit passes it through.
            return Response(content="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
                            media_type="text/plain")

        @app.get("/v2/invoicing/invoices/{iid}")
        async def get_invoice(iid: str):
            inv = self.invoices[iid]
            items = inv["body"].get("items", [])
            total = sum(float(i["unit_amount"]["value"]) * float(i["quantity"]) for i in items)
            cur = items[0]["unit_amount"]["currency_code"] if items else "USD"
            return {"id": iid, "status": inv["status"],
                    "detail": {"metadata": {"recipient_view_url": f"https://www.sandbox.paypal.test/invoice/p/#{iid}"}},
                    "amount": {"currency_code": cur, "value": f"{total:.2f}"}}

    # ---------- test controls ----------
    def approve_setup_token(self, sid: str) -> None:
        """What the owner does on PayPal's page."""
        self.setup_tokens[sid]["status"] = "APPROVED"

    def pay_invoice(self, iid: str) -> None:
        self.invoices[iid]["status"] = "PAID"

    def start(self) -> str:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        while not self.server.started:
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self.base

    def stop(self) -> None:
        self.server.should_exit = True