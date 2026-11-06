"""Server side of the AG Studio <-> Claude adapter.

The browser adapter (frontend/src/dashboard/claudeAdapter.js) translates AG Studio's
turn into an Anthropic Messages request and posts it here. This route adds the API
key, pins the model, caps output, rate-limits by client, and returns Claude's reply.
The key never reaches the browser.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException

from . import config
from .llm import _get_client

MAX_OUTPUT_TOKENS = 4096
MAX_TOOLS = 64
RATE_LIMIT = 120  # requests per client per hour; Studio makes several per question
_hits: dict[str, deque] = defaultdict(deque)


def _rate_limit(client_id: str) -> None:
    now = time.time()
    q = _hits[client_id]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Too many AI requests from this browser. Try again later.")
    q.append(now)


def _clean_tool_choice(choice: dict | None, has_tools: bool) -> dict | None:
    if not has_tools or not choice:
        return None
    if choice.get("type") == "none":
        return {"type": "none"}
    # Forcing a specific tool ("any"/"tool") isn't supported by every model; ask, don't force.
    return {"type": "auto"}


async def run_turn(body: dict, client_id: str) -> dict:
    if not config.AI_ENABLED:
        raise HTTPException(503, "The AI assistant needs ANTHROPIC_API_KEY on the server.")
    _rate_limit(client_id)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages are required")
    tools = body.get("tools") or []
    if len(tools) > MAX_TOOLS:
        raise HTTPException(400, "too many tools")
    request = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": min(int(body.get("max_tokens") or MAX_OUTPUT_TOKENS), MAX_OUTPUT_TOKENS),
        "messages": messages,
    }
    if body.get("system"):
        request["system"] = str(body["system"])
    if tools:
        request["tools"] = tools
        choice = _clean_tool_choice(body.get("tool_choice"), True)
        if choice:
            request["tool_choice"] = choice
    try:
        resp = await _get_client().messages.create(**request)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", 502)
        raise HTTPException(status if isinstance(status, int) else 502, f"Claude request failed: {exc}") from exc
    return resp.model_dump(mode="json")
