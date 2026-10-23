"""Channel3: real product data and market prices across retailers.

Used twice:
  * seller agents price their catalog from the real market (list price = market + markup)
  * the buyer agent uses the same market data as leverage while negotiating
"""
import logging
import re
import statistics

import httpx

from . import config

log = logging.getLogger("mandate.channel3")

_cache: dict[str, dict | None] = {}

_PACK_RE = re.compile(
    r"(?:pack of|set of|case of|box of)\s*(\d{1,5})|(\d{1,5})\s*(?:-|\s)?(?:pack|count|ct|pcs|pieces|pc|units)\b",
    re.I,
)


def pack_size(title: str) -> int:
    """Listings are often multi-packs ("Pack of 50"). Normalize to a per-unit price."""
    m = _PACK_RE.search(title)
    if not m:
        return 1
    n = int(m.group(1) or m.group(2))
    return n if 1 < n <= 10000 else 1


async def market_benchmark(query: str, currency: str = "USD", limit: int = 20) -> dict | None:
    """Return {low, median, high, samples, examples[]} unit prices, or None if unavailable."""
    if not config.CHANNEL3_API_KEY:
        return None
    key = f"{query.lower()}|{currency}"
    if key in _cache:
        return _cache[key]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{config.CHANNEL3_BASE}/v1/search",
                headers={"x-api-key": config.CHANNEL3_API_KEY, "Content-Type": "application/json"},
                json={"query": query, "limit": limit, "filters": {"availability": ["InStock"]}},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Channel3 search failed: %s", exc)
        return None

    prices: list[float] = []
    examples: list[dict] = []
    for product in data.get("products", []):
        offers = [o for o in product.get("offers", []) if (o.get("price") or {}).get("currency") == currency]
        if not offers:
            continue
        best = min(offers, key=lambda o: o["price"]["price"])
        pack = pack_size(product.get("title") or "")
        price = float(best["price"]["price"]) / pack
        if price <= 0:
            continue
        prices.append(price)
        if len(examples) < 5:
            images = product.get("images") or []
            examples.append({
                "id": product.get("id"),
                "title": product.get("title"),
                "price": price,
                "domain": best.get("domain"),
                "url": best.get("url"),
                "image": (images[0].get("cleaned_url") or images[0].get("url")) if images else None,
            })
    if not prices:
        _cache[key] = None
        return None
    result = {
        "query": query,
        "currency": currency,
        "low": round(min(prices), 2),
        "median": round(statistics.median(prices), 2),
        "high": round(max(prices), 2),
        "samples": len(prices),
        "retailers": len({e["domain"] for e in examples if e.get("domain")}),
        "examples": examples,
        "source": "channel3",
    }
    _cache[key] = result
    return result
