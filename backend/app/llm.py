"""Thin wrapper around Claude that always returns parsed JSON."""
import json
import logging
import re
from typing import Any, Callable

from . import config

log = logging.getLogger("procuro.llm")

_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        workspace = getattr(config, "ANTHROPIC_WORKSPACE_ID", "")
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, default_headers=headers)
    return _client


def _extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


async def ask_json(
    system: str,
    prompt: str,
    fallback: Callable[[], dict],
    images: list[tuple[str, str]] | None = None,
    max_tokens: int = 1024,
) -> dict[str, Any]:
    """Ask Claude for a JSON object. `images` is a list of (media_type, base64_data).

    `fallback` produces a rule-based answer when no API key is configured or the
    call fails, so a single bad response never breaks a live negotiation.
    """
    if not config.AI_ENABLED:
        return fallback()
    content: list[dict] = []
    for media_type, data in images or []:
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    content.append({"type": "text", "text": prompt + "\n\nRespond with a single JSON object only."})
    try:
        resp = await _get_client().messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=max(max_tokens, 1024),
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _extract_json(text)
    except Exception as exc:  # noqa: BLE001 - keep the negotiation alive
        log.warning("Claude call failed, using fallback: %s", exc)
        return fallback()