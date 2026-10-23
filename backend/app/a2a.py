"""Minimal A2A (Agent2Agent) protocol support: agent cards + JSON-RPC `message/send`.

Each seller agent publishes   GET  {base}/.well-known/agent-card.json
and receives messages at      POST {base}        (JSON-RPC 2.0, method "message/send")

Commerce payloads travel as A2A DataParts: {"kind": "data", "data": {"action": ..., ...}}
with an optional TextPart carrying the human-readable message shown in the live feed.
"""
import uuid
from typing import Any, Callable

import httpx

A2A_PROTOCOL_VERSION = "0.3.0"
AP2_EXTENSION_URI = "https://github.com/google-agentic-commerce/ap2/tree/v0.1"

# Tests swap this for an in-process transport. In production it is plain HTTP,
# so any third-party seller agent at any URL can take part.
client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=60)


class A2AError(Exception):
    pass


def agent_card(*, name: str, description: str, url: str, skills: list[dict], public_key: str,
               role: str) -> dict:
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": url,
        "preferredTransport": "JSONRPC",
        "version": "0.1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [{
                "uri": AP2_EXTENSION_URI,
                "description": "Agent Payments Protocol: signed intent, cart and payment mandates",
                "required": True,
                "params": {"roles": [role]},
            }],
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": skills,
        "signing": {"alg": "Ed25519", "publicKey": public_key},
    }


def make_message(data: dict, text: str | None = None, role: str = "user", context_id: str | None = None) -> dict:
    parts: list[dict] = [{"kind": "data", "data": data}]
    if text:
        parts.append({"kind": "text", "text": text})
    msg = {"kind": "message", "messageId": uuid.uuid4().hex, "role": role, "parts": parts}
    if context_id:
        msg["contextId"] = context_id
    return msg


def split_message(message: dict) -> tuple[dict, str]:
    data: dict[str, Any] = {}
    text = ""
    for part in message.get("parts", []):
        if part.get("kind") == "data":
            data.update(part.get("data") or {})
        elif part.get("kind") == "text":
            text += part.get("text", "")
    return data, text


def rpc_result(req_id: Any, message: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": message}


def rpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def fetch_card(base_url: str) -> dict:
    async with client_factory() as client:
        r = await client.get(f"{base_url}/.well-known/agent-card.json")
        r.raise_for_status()
        return r.json()


async def send(agent_url: str, data: dict, text: str | None = None, context_id: str | None = None) -> tuple[dict, str]:
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {"message": make_message(data, text, "user", context_id)},
    }
    async with client_factory() as client:
        r = await client.post(agent_url, json=payload)
        r.raise_for_status()
        body = r.json()
    if "error" in body:
        raise A2AError(body["error"].get("message", "agent error"))
    return split_message(body["result"])
