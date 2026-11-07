"""Minimal A2A (Agent2Agent) protocol support: agent cards + JSON-RPC `message/send`.

Each seller agent publishes   GET  {base}/.well-known/agent-card.json
and receives messages at      POST {base}        (JSON-RPC 2.0, method "message/send")

Commerce payloads travel as A2A DataParts: {"kind": "data", "data": {"action": ..., ...}}
with an optional TextPart carrying the human-readable message shown in the live feed.

AP2 documents (keys starting with "ap2.", e.g. "ap2.checkout", "ap2.mandates.PaymentMandateSdJwt",
"ap2.PaymentReceipt") come back from a merchant as an A2A Task artifact, the way Google's AP2 samples
return them; the agent's words go in the task's status message.
"""
import uuid
from typing import Any, Callable

import httpx

A2A_PROTOCOL_VERSION = "0.3.0"
AP2_EXTENSION_URI = "https://github.com/google-agentic-commerce/ap2/v1"  # AP2 v0.2, as in Google's samples

# Tests swap this for an in-process transport. In production it is plain HTTP,
# so any third-party seller agent at any URL can take part.
client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=60)


class A2AError(Exception):
    pass


def agent_card(*, name: str, description: str, url: str, skills: list[dict], signing: dict,
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
                "description": "Agent Payments Protocol v0.2: SD-JWT checkout and payment mandates, "
                               "merchant-signed checkout JWT, signed receipts",
                "required": role == "merchant",
                "params": {"roles": [role]},
            }],
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": skills,
        # Public keys (JWK, ES256) others use to check what this agent signs.
        "signing": signing,
    }


def make_message(data: dict, text: str | None = None, role: str = "user", context_id: str | None = None) -> dict:
    parts: list[dict] = [{"kind": "data", "data": data}]
    if text:
        parts.append({"kind": "text", "text": text})
    msg = {"kind": "message", "messageId": uuid.uuid4().hex, "role": role, "parts": parts}
    if context_id:
        msg["contextId"] = context_id
    return msg


def make_task(data: dict, text: str | None = None, context_id: str | None = None) -> dict:
    """A completed A2A Task: AP2 documents as an artifact, everything else in the status message."""
    docs = {k: v for k, v in data.items() if k.startswith("ap2.")}
    rest = {k: v for k, v in data.items() if not k.startswith("ap2.")}
    task = {
        "kind": "task", "id": uuid.uuid4().hex, "contextId": context_id or uuid.uuid4().hex,
        "status": {"state": "completed", "message": make_message(rest, text, "agent", context_id)},
        "artifacts": [{"artifactId": uuid.uuid4().hex, "name": "ap2", "parts": [{"kind": "data", "data": docs}]}],
    }
    return task


def _parts(parts: list[dict], data: dict, text: str) -> str:
    for part in parts or []:
        if part.get("kind") == "data":
            data.update(part.get("data") or {})
        elif part.get("kind") == "text":
            text += part.get("text", "")
    return text


def _whole(value: Any) -> Any:
    """Agents on the official A2A SDK send data parts as protobuf Values, where every number is a float.
    Turn whole numbers back into ints (200.0 -> 200)."""
    if isinstance(value, dict):
        return {k: _whole(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_whole(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def split_message(message: dict) -> tuple[dict, str]:
    """Data and text from an A2A Message, or from a Task (its status message plus its artifacts)."""
    data: dict[str, Any] = {}
    if message.get("kind") == "task":
        status = message.get("status") or {}
        text = _parts((status.get("message") or {}).get("parts"), data, "")
        if status.get("state") in ("failed", "rejected", "canceled"):
            raise A2AError(text or data.get("error") or f"the agent's task {status.get('state')}")
        for artifact in message.get("artifacts") or []:
            _parts(artifact.get("parts"), data, "")
    else:
        text = _parts(message.get("parts"), data, "")
    if data.get("action") == "error":
        # An agent that can't answer with a JSON-RPC error (e.g. one built on the A2A SDK) says so in data.
        raise A2AError(str(data.get("error") or text or "agent error"))
    return _whole(data), text


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