"""Whose signatures this seller accepts: the keys published in the trusted buying agent's A2A card.

The card at TRUSTED_BUYER_URL/.well-known/agent-card.json lists two public keys (JWK, ES256):
  signing.jwk                the buying agent, which signs release instructions (and closed mandates
                             when it pays on its own, through the key bound in the open mandate)
  signing.trustedSurfaceJwk  the owner's trusted surface, which signs the open mandates and her approvals
They are fetched over HTTP and cached for a few minutes.
"""
import time
from typing import Callable

import httpx

from . import config

client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=20)  # tests swap this

_cache: dict = {"at": 0.0, "keys": None}
TTL = 300


class TrustError(Exception):
    pass


def _jwk(card: dict, name: str) -> dict | None:
    signing = card.get("signing") or {}
    if signing.get(name):
        return signing[name]
    for ext in (card.get("capabilities") or {}).get("extensions") or []:
        found = ((ext.get("params") or {}).get("signing") or {}).get(name)
        if found:
            return found
    return None


async def buyer_keys() -> dict:
    """{"buyer": jwk, "surface": jwk} from the trusted buying agent's card."""
    if _cache["keys"] and time.time() - _cache["at"] < TTL:
        return _cache["keys"]
    url = f"{config.TRUSTED_BUYER_URL}/.well-known/agent-card.json"
    try:
        async with client_factory() as client:
            r = await client.get(url)
            r.raise_for_status()
            card = r.json()
    except Exception as exc:  # noqa: BLE001
        raise TrustError(f"couldn't read the buying agent's card at {url}: {exc}") from None
    keys = {"buyer": _jwk(card, "jwk"), "surface": _jwk(card, "trustedSurfaceJwk"), "name": card.get("name")}
    if not keys["buyer"] or not keys["surface"]:
        raise TrustError("the buying agent's card doesn't publish its signing keys")
    _cache.update(at=time.time(), keys=keys)
    return keys
