"""Mandate API: the buyer app's REST API plus the A2A endpoints of every hosted agent."""
import base64
import logging

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import a2a, buyer, config, paypal, sellers
from .store import store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mandate")

app = FastAPI(title="Mandate", description="Agent-to-agent commerce with signed mandates and PayPal holds")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MandateIn(BaseModel):
    text: str


class PayPalReturnIn(BaseModel):
    token: str


class QuestionIn(BaseModel):
    question: str


def _order_or_404(order_id: str) -> dict:
    order = store.get_order(order_id)
    if not order:
        raise HTTPException(404, "order not found")
    return order


@app.get("/health")
def health() -> dict:
    return {"ok": True, "ai": config.AI_ENABLED, "paypal_mode": config.PAYPAL_MODE,
            "channel3": bool(config.CHANNEL3_API_KEY), "store": type(store).__name__}


# ------------------------------------------------------------------ buyer app API

@app.post("/api/orders")
async def create_order(body: MandateIn, background: BackgroundTasks) -> dict:
    try:
        order = await buyer.create_order(body.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    background.add_task(buyer.negotiate, order["id"])
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
    except (a2a.A2AError, paypal.PayPalError) as exc:
        details = getattr(exc, "details", None)
        raise HTTPException(502, {"error": str(exc), "details": details}) from exc


@app.post("/api/orders/{order_id}/approve")
async def approve(order_id: str) -> dict:
    return await _guard(buyer.approve(order_id))


@app.post("/api/orders/{order_id}/paypal-return")
async def paypal_return(order_id: str, body: PayPalReturnIn) -> dict:
    return await _guard(buyer.paypal_returned(order_id, body.token))


@app.post("/api/orders/{order_id}/delivered")
async def delivered(order_id: str) -> dict:
    return await _guard(buyer.confirm_delivery(order_id))


@app.post("/api/orders/{order_id}/report")
async def report(order_id: str, background: BackgroundTasks, note: str = Form(...),
                 photos: list[UploadFile] = File(default=[])) -> dict:
    images = []
    for f in photos[:4]:
        data = await f.read()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(413, "each photo must be under 5 MB")
        media = f.content_type if f.content_type in ("image/jpeg", "image/png", "image/webp", "image/gif") \
            else "image/jpeg"
        images.append((media, base64.b64encode(data).decode()))
    order = await _guard(buyer.report_problem(order_id, note, images))
    background.add_task(buyer.resolve, order_id)
    return order


@app.post("/api/orders/{order_id}/ask")
async def ask(order_id: str, body: QuestionIn) -> dict:
    _order_or_404(order_id)
    return {"answer": await buyer.ask(order_id, body.question)}


@app.get("/api/sellers")
def seller_stats() -> list[dict]:
    return [{"id": sid, "name": s["name"], "tagline": s["tagline"], **store.get_doc("sellers", sid)}
            for sid, s in sellers.SELLERS.items()]


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
        return a2a.rpc_error(req_id, -32001, f"{exc}")
    return a2a.rpc_result(req_id, a2a.make_message(result, text, "agent", message.get("contextId")))
