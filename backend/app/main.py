"""Procuro API: the buyer app's REST API plus the A2A endpoints of every hosted agent."""
import base64
import datetime as dt
import logging

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import a2a, analytics, buyer, config, evidence, invoices, paypal, receipt, schedule, sellers, studio_ai, timeline, wallet
from . import store as store_module
from .store import store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("procuro")

app = FastAPI(title="Procuro", description="Agent-to-agent commerce with signed mandates and PayPal holds")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MandateIn(BaseModel):
    text: str
    deadline: str | None = None  # YYYY-MM-DD picked on the calendar


class PayPalReturnIn(BaseModel):
    token: str


class WalletConnectIn(BaseModel):
    monthly_limit: float | None = None
    auto_pay: bool = True


class WalletFinishIn(BaseModel):
    setup_token_id: str | None = None


class WalletSettingsIn(BaseModel):
    auto_pay: bool | None = None
    monthly_limit: float | None = None


class RescheduleIn(BaseModel):
    delivery_date: str


class InvoiceShareIn(BaseModel):
    partner_name: str
    partner_email: str
    quantity: int
    note: str = ""


class QuestionIn(BaseModel):
    question: str


class ProductChoiceIn(BaseModel):
    product_id: str
    quantity: int


def _order_or_404(order_id: str) -> dict:
    order = store.get_order(order_id)
    if not order:
        raise HTTPException(404, "order not found")
    return order


@app.get("/health")
def health() -> dict:
    return {"ok": True, "ai": config.AI_ENABLED, "paypal_mode": config.PAYPAL_MODE,
            "paypal_credentials": {sid: all(config.seller_paypal_credentials(sid)) for sid in sellers.SELLERS},
            "channel3": bool(config.CHANNEL3_API_KEY), "store": store.kind,
            "store_error": store_module.STORE_ERROR, "photo_storage": store.photo_storage}


@app.on_event("startup")
def _startup_check() -> None:
    if config.PAYPAL_MODE != "mock" and not all(config.seller_paypal_credentials("quickprint")):
        log.warning("PayPal credentials not found. Payments will fail until PAYPAL_CLIENT_ID and "
                    "PAYPAL_CLIENT_SECRET are set in backend/.env and the server is restarted.")


# ------------------------------------------------------------------ buyer app API

@app.post("/api/orders")
async def create_order(body: MandateIn, background: BackgroundTasks) -> dict:
    try:
        order = await buyer.create_order(body.text, body.deadline)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if order["status"] == "negotiating":  # no product to choose (Channel3 not configured or no results)
        background.add_task(buyer.negotiate, order["id"])
    return order


@app.post("/api/orders/{order_id}/product")
def choose_product(order_id: str, body: ProductChoiceIn, background: BackgroundTasks) -> dict:
    """The owner picks the exact Channel3 product and quantity; negotiation starts for that product."""
    try:
        order = buyer.choose_product(order_id, body.product_id, body.quantity)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    background.add_task(buyer.negotiate, order_id)
    return order


@app.get("/api/orders")
def list_orders() -> list[dict]:
    return store.list_orders()


@app.get("/api/orders/{order_id}")
def get_order(order_id: str, after: int = -1) -> dict:
    order = _order_or_404(order_id)
    return {"order": order, "messages": store.list_messages(order_id, after)}


async def _guard(coro):
    try:
        return await coro
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (a2a.A2AError, paypal.PayPalError, RuntimeError) as exc:
        details = getattr(exc, "details", None)
        raise HTTPException(502, {"error": str(exc), "details": details}) from exc


@app.post("/api/orders/{order_id}/approve")
async def approve(order_id: str) -> dict:
    return await _guard(buyer.approve(order_id))


@app.post("/api/orders/{order_id}/reschedule")
async def reschedule(order_id: str, body: RescheduleIn) -> dict:
    """Move a delivery to another day: the buying agent asks the seller agent over A2A."""
    return await _guard(buyer.reschedule(order_id, body.delivery_date))


# ---------- sharing a bulk order (PayPal invoices through the Agent Toolkit) ----------

@app.post("/api/orders/{order_id}/shares")
async def share_order(order_id: str, body: InvoiceShareIn) -> dict:
    try:
        return await _guard(buyer.share_order(order_id, partner_name=body.partner_name,
                                              partner_email=body.partner_email, quantity=body.quantity,
                                              note=body.note))
    except invoices.InvoiceError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/orders/{order_id}/shares/refresh")
async def refresh_shares(order_id: str) -> dict:
    try:
        return await _guard(buyer.refresh_shares(order_id))
    except invoices.InvoiceError as exc:
        raise HTTPException(502, str(exc)) from exc


# ---------- the owner's PayPal, saved once (Vault v3) ----------

@app.get("/api/paypal-wallet")
async def wallet_view() -> dict:
    return wallet.view()


@app.post("/api/paypal-wallet/connect")
async def wallet_connect(body: WalletConnectIn) -> dict:
    """Start saving the owner's PayPal: returns PayPal's approval page."""
    return await _guard(wallet.start(monthly_limit=body.monthly_limit, auto_pay=body.auto_pay))


@app.post("/api/paypal-wallet/finish")
async def wallet_finish(body: WalletFinishIn) -> dict:
    """Back from PayPal: turn the approved setup token into the saved payment token."""
    return await _guard(wallet.finish(body.setup_token_id))


@app.patch("/api/paypal-wallet")
async def wallet_settings(body: WalletSettingsIn) -> dict:
    try:
        return wallet.update(auto_pay=body.auto_pay, monthly_limit=body.monthly_limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/paypal-wallet")
async def wallet_disconnect() -> dict:
    return await _guard(wallet.disconnect())


@app.post("/api/orders/{order_id}/paypal-return")
async def paypal_return(order_id: str, body: PayPalReturnIn) -> dict:
    return await _guard(buyer.paypal_returned(order_id, body.token))


@app.post("/api/orders/{order_id}/delivered")
async def delivered(order_id: str) -> dict:
    return await _guard(buyer.confirm_delivery(order_id))


@app.post("/api/orders/{order_id}/report")
async def report(order_id: str, background: BackgroundTasks, note: str = Form(...),
                 photos: list[UploadFile] = File(default=[])) -> dict:
    order = _order_or_404(order_id)
    if order["status"] != "held":
        raise HTTPException(409, "only orders with an active hold can be disputed this way")
    raw = []
    for f in photos[:4]:
        data = await f.read()
        if not data:
            continue
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(413, "each photo must be under 15 MB")
        raw.append((f.content_type or "image/jpeg", data))
    try:
        records, for_claude = evidence.save_photos(order_id, raw)
    except evidence.EvidenceError as exc:
        raise HTTPException(400, str(exc)) from exc
    images = [(media, base64.b64encode(data).decode()) for media, data in for_claude]
    order = await _guard(buyer.report_problem(order_id, note, images, records))
    background.add_task(buyer.resolve, order_id)
    return order


@app.get("/api/calendar")
def calendar(tz: int = 0) -> list[dict]:
    """Deliveries, payment releases and hold renewals across orders, for the delivery-date calendar.
    `tz` is the browser's getTimezoneOffset(), so dates land on the viewer's calendar days."""
    return timeline.calendar_events(store.list_orders(limit=200), tz_offset=tz)


@app.get("/api/orders/{order_id}/timeline")
def order_timeline(order_id: str, tz: int = 0) -> dict:
    """The order's whole life as Gantt data: done, happening now, and planned (in the viewer's timezone)."""
    return timeline.build(_order_or_404(order_id), tz_offset=tz)


@app.get("/api/orders/{order_id}/receipt")
def order_receipt(order_id: str) -> dict:
    """Every signed step of the order with its fingerprint, tied to the PayPal records it caused."""
    return receipt.build(_order_or_404(order_id))


@app.get("/api/orders/{order_id}/receipt/paypal")
async def order_receipt_paypal(order_id: str) -> dict:
    """PayPal's own record of the hold and capture, compared with the signed steps."""
    return await receipt.paypal_check(_order_or_404(order_id))


@app.get("/api/orders/{order_id}/evidence/{evidence_id}")
def evidence_photo(order_id: str, evidence_id: str) -> Response:
    """A damage photo attached to an order (the original if stored in Cloud Storage)."""
    found = store.load_evidence(evidence_id)
    if not found or found[0].get("order_id") != order_id:
        raise HTTPException(404, "photo not found")
    record, data = found
    return Response(content=data, media_type=record.get("media_type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=86400",
                             "X-Content-SHA256": record.get("sha256", "")})


@app.post("/api/orders/{order_id}/ask")
async def ask(order_id: str, body: QuestionIn) -> dict:
    _order_or_404(order_id)
    return {"answer": await buyer.ask(order_id, body.question)}


@app.get("/api/sellers/schedule")
async def seller_schedule():
    """The Sellers board: each seller agent's production slots, bookings and fully booked days."""
    board = schedule.board(sellers.SELLERS)
    # Outside seller agents: only the orders placed with them from here (their own schedule is private).
    outside = await buyer.outside_board(dt.date.fromisoformat(board["today"]))
    if outside["resources"]:
        board["resources"] += outside["resources"]
        board["events"] += outside["events"]
        today = dt.date.fromisoformat(board["today"])
        days = [dt.date.fromisoformat(e["startDate"]) for e in outside["events"]] + \
               [dt.date.fromisoformat(e["endDate"]) + dt.timedelta(days=2) for e in outside["events"]]
        if days:
            board["startDate"] = max(min([dt.date.fromisoformat(board["startDate"])] + days),
                                     today - dt.timedelta(days=14)).isoformat()
            board["endDate"] = max([dt.date.fromisoformat(board["endDate"])] + days).isoformat()
    return board


@app.get("/api/sellers")
def seller_stats() -> list[dict]:
    return [{"id": sid, "name": s["name"], "tagline": s["tagline"], **store.get_doc("sellers", sid)}
            for sid, s in sellers.SELLERS.items()]


@app.get("/api/analytics")
def analytics_tables() -> dict:
    """Tables for the AG Studio Spend dashboard, built from real orders only."""
    return analytics.tables()


@app.post("/api/studio-ai/messages")
async def studio_ai_turn(request: Request) -> dict:
    """One AG Studio AI turn, answered by Claude. Called by the browser adapter."""
    body = await request.json()
    client_id = request.headers.get("x-forwarded-for", request.client.host if request.client else "anon")
    return await studio_ai.run_turn(body, client_id.split(",")[0].strip())


@app.post("/api/internal/renew-holds")
async def renew_holds(x_cron_secret: str = Header(default="")) -> dict:
    if x_cron_secret != config.CRON_SECRET:
        raise HTTPException(401, "bad cron secret")
    return {"renewed": await buyer.renew_holds()}


@app.post("/api/paypal/webhook")
async def paypal_webhook(request: Request) -> dict:
    event = await request.json()
    if config.PAYPAL_WEBHOOK_ID and config.PAYPAL_MODE != "mock":
        ok = await paypal.client_for("default").verify_webhook(
            {k.lower(): v for k, v in request.headers.items()}, event, config.PAYPAL_WEBHOOK_ID)
        if not ok:
            raise HTTPException(400, "webhook signature verification failed")
    resource = event.get("resource") or {}
    order_id = resource.get("custom_id")
    if order_id and store.get_order(order_id):
        buyer.say(order_id, "paypal", f"Webhook: {event.get('event_type')} ({resource.get('status', '')})",
                  kind="webhook", data={"event_type": event.get("event_type"), "resource_id": resource.get("id")})
    return {"received": True}


# ------------------------------------------------------------------ A2A agents

@app.get("/agents/buyer/.well-known/agent-card.json")
def buyer_card() -> dict:
    return buyer.card()


@app.get("/agents/{seller_id}/.well-known/agent-card.json")
def seller_card(seller_id: str) -> dict:
    if seller_id not in sellers.SELLERS:
        raise HTTPException(404, "unknown agent")
    return sellers.card(seller_id)


@app.post("/agents/{seller_id}")
async def seller_rpc(seller_id: str, request: Request) -> dict:
    body = await request.json()
    req_id = body.get("id")
    if body.get("jsonrpc") != "2.0" or body.get("method") != "message/send":
        return a2a.rpc_error(req_id, -32601, "method not found")
    message = (body.get("params") or {}).get("message") or {}
    data, _ = a2a.split_message(message)
    try:
        result, text = await sellers.handle(seller_id, data, message.get("contextId"))
    except a2a.A2AError as exc:
        return a2a.rpc_error(req_id, -32000, str(exc))
    except paypal.PayPalError as exc:
        log.warning("PayPal error in %s: %s %s", seller_id, exc, exc.details)
        detail = exc.details.get("message") or exc.details.get("error_description") or ""
        return a2a.rpc_error(req_id, -32001, f"{exc} {detail}".strip())
    if any(k.startswith("ap2.") for k in result):
        return a2a.rpc_result(req_id, a2a.make_task(result, text, message.get("contextId")))
    return a2a.rpc_result(req_id, a2a.make_message(result, text, "agent", message.get("contextId")))