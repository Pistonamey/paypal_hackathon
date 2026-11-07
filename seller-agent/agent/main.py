"""NorthBox's A2A server, built on the official A2A Python SDK (a2a-sdk).

    GET  /agents/northbox/.well-known/agent-card.json   its A2A agent card (A2A v1.0, with v0.3 fields)
    POST /agents/northbox                               A2A JSON-RPC: SendMessage (v1.0) and message/send (v0.3)
    GET  /                                              a small page showing what this agent is doing
    GET  /health

Every request carries one DataPart, e.g. {"action": "request_quote", ...}. The reply is an A2A Message,
or a completed Task whose artifact holds the AP2 documents (checkout, receipts), as in Google's AP2 samples.
"""
import html
import json
import logging
import time

from a2a.helpers import proto_helpers as ph
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import a2a_pb2 as pb
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from google.protobuf import json_format, struct_pb2

from . import config, mandates, merchant, paypal
from .store import store

log = logging.getLogger("northbox")
logging.basicConfig(level=logging.INFO)


def _ints(value):
    """A2A data parts are protobuf Values, so every number arrives as a float. Put whole numbers back."""
    if isinstance(value, dict):
        return {k: _ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ints(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def build_card() -> pb.AgentCard:
    url = merchant.agent_url()
    ap2 = struct_pb2.Struct()
    json_format.ParseDict({
        "roles": ["merchant"],
        # Public key (JWK, ES256) for everything this agent signs: checkouts, receipts, amendments.
        "signing": {"alg": "ES256", "jwk": mandates.public_jwk(f"seller:{config.AGENT_ID}")},
    }, ap2)
    return pb.AgentCard(
        name=config.AGENT_NAME,
        description=f"{merchant.SELLER['tagline']} Seller agent that negotiates and accepts PayPal.",
        version="1.0.0",
        provider=pb.AgentProvider(organization=f"{config.AGENT_NAME} (demo seller)", url=config.PUBLIC_BASE_URL),
        supported_interfaces=[
            pb.AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0"),
            pb.AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="0.3"),
        ],
        capabilities=pb.AgentCapabilities(streaming=False, extensions=[pb.AgentExtension(
            uri=mandates.EXTENSION_URI, required=True, params=ap2,
            description="Agent Payments Protocol v0.2: SD-JWT checkout and payment mandates, merchant-signed "
                        "checkout JWT, signed receipts")]),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            pb.AgentSkill(id="quote", name="Quote", description="Price a quantity of a product with a delivery date",
                          tags=["commerce", "quote"]),
            pb.AgentSkill(id="negotiate", name="Negotiate", description="Respond to counter-offers",
                          tags=["commerce", "negotiation"]),
            pb.AgentSkill(id="checkout", name="AP2 checkout with PayPal",
                          description="Sign an AP2 checkout, verify checkout and payment mandates, hold with PayPal "
                                      "and return signed receipts; capture or void on a signed release",
                          tags=["paypal", "ap2", "payments"]),
        ],
    )


class MerchantExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        data: dict = {}
        for part in ph.get_data_parts(context.message.parts):
            if isinstance(part, dict):
                data.update(part)
        data = _ints(data)
        said = ph.get_message_text(context.message)
        try:
            result, text = await merchant.handle(data, context.context_id)
        except (merchant.Refused, mandates.MandateRejected) as exc:
            result, text = {"action": "error", "error": str(exc)}, str(exc)
        except Exception as exc:  # noqa: BLE001 - tell the buyer, don't leave it hanging
            log.exception("action %s failed", data.get("action"))
            result, text = {"action": "error", "error": f"{type(exc).__name__}: {exc}"}, "Something went wrong on our side."
        store.log({"order_id": data.get("order_id") or context.context_id, "in": data.get("action"),
                   "said": said[:300], "out": result.get("action"), "reply": text[:300],
                   "price": data.get("unit_price"), "result_price": result.get("unit_price")})

        docs = {k: v for k, v in result.items() if k.startswith("ap2.")}
        rest = {k: v for k, v in result.items() if not k.startswith("ap2.")}
        if docs:
            task = ph.new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await updater.add_artifact([ph.new_data_part(docs)], name="ap2")
            await updater.complete(ph.new_message([ph.new_data_part(rest), ph.new_text_part(text)],
                                                  context_id=task.context_id, task_id=task.id))
        else:
            await event_queue.enqueue_event(ph.new_message([ph.new_data_part(rest), ph.new_text_part(text)],
                                                           context_id=context.context_id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:  # nothing long-running
        return None


app = FastAPI(title=f"{config.AGENT_NAME} seller agent")
_card = build_card()
_handler = DefaultRequestHandlerV2(agent_executor=MerchantExecutor(), task_store=InMemoryTaskStore(), agent_card=_card)
for route in (create_agent_card_routes(_card, card_url=f"/agents/{config.AGENT_ID}/.well-known/agent-card.json")
              + create_agent_card_routes(_card)  # also at /.well-known/agent-card.json
              + create_jsonrpc_routes(_handler, f"/agents/{config.AGENT_ID}", enable_v0_3_compat=True)):
    app.router.routes.append(route)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "agent": config.AGENT_ID, "paypal_mode": config.PAYPAL_MODE,
            "paypal_credentials": bool(config.PAYPAL_CLIENT_ID and config.PAYPAL_CLIENT_SECRET),
            "store": store.kind, "trusted_buyer": config.TRUSTED_BUYER_URL}


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    rows = []
    for e in store.recent(60):
        when = time.strftime("%b %d %H:%M:%S", time.gmtime(e.get("at", 0)))
        rows.append(f"<tr><td>{when}</td><td><code>{html.escape(str(e.get('order_id') or ''))}</code></td>"
                    f"<td><b>{html.escape(str(e.get('in') or ''))}</b><br><span>{html.escape(e.get('said') or '')}</span></td>"
                    f"<td><b>{html.escape(str(e.get('out') or ''))}</b><br><span>{html.escape(e.get('reply') or '')}</span></td></tr>")
    card_url = f"/agents/{config.AGENT_ID}/.well-known/agent-card.json"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(config.AGENT_NAME)}</title>
<style>
:root{{--ink:#15253a;--soft:#5b6b7f;--line:#d9e0e8;--bg:#f6f8fb;--sheet:#fff;--accent:#2f6f4f}}
body{{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}}
main{{max-width:980px;margin:0 auto;padding:28px 16px}}
h1{{margin:0 0 4px;font-size:26px}} p{{margin:4px 0 14px;color:var(--soft)}}
.facts{{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 22px}}
.facts span{{background:var(--sheet);border:1px solid var(--line);border-radius:99px;padding:3px 12px;font-size:13px}}
table{{width:100%;border-collapse:collapse;background:var(--sheet);border:1px solid var(--line);border-radius:8px;overflow:hidden}}
th,td{{text-align:left;vertical-align:top;padding:9px 12px;border-bottom:1px solid var(--line);font-size:14px}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--soft)}}
td span{{color:var(--soft)}} code{{font-size:12.5px}} a{{color:#1d4d9e;font-weight:600}}
@media (max-width:640px){{td:nth-child(1),th:nth-child(1){{display:none}}}}
</style></head><body><main>
<h1>{html.escape(config.AGENT_NAME)}</h1>
<p>An independent seller agent on its own server. It speaks A2A (official A2A SDK) and AP2 v0.2, signs with its
own key and is paid into its own PayPal account. Procuro's buying agent finds it from its
<a href="{card_url}">agent card</a>.</p>
<div class="facts"><span>Agent id: {html.escape(config.AGENT_ID)}</span><span>PayPal: {html.escape(config.PAYPAL_MODE)}</span>
<span>Trusts buyer: {html.escape(config.TRUSTED_BUYER_URL)}</span><span>Storage: {store.kind}</span></div>
<table><thead><tr><th>When (UTC)</th><th>Order</th><th>Buyer agent asked</th><th>We answered</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="4">No conversations yet.</td></tr>'}</tbody></table>
</main></body></html>"""


@app.get("/api/log")
def activity() -> list[dict]:
    return store.recent(60)


__all__ = ["app", "paypal", "json"]
