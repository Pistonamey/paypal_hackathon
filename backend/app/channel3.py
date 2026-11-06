"""Channel3: the product catalog the agents trade in.

1. The buyer agent searches Channel3 for the user's request and shows real products
   (photo, title, retail prices across retailers).
2. The user picks one. That exact product, sold as listed (a 3-pack is one unit), is what
   every seller agent quotes on, priced from its real retail offers.
3. The market check is that same product's offers across retailers: a like-for-like price.
"""
import asyncio
import logging
import math
import re
import statistics

import httpx

from . import config
from .llm import ask_json

log = logging.getLogger("procuro.channel3")

MAX_CANDIDATES = 6
_cache: dict[str, list[dict]] = {}
_lock = asyncio.Lock()

_PACK_RE = re.compile(
    r"(?:pack of|set of|case of|box of|lot of)\s*(\d{1,5})|(\d{1,5})\s*(?:-|\s)?(?:pack|count|ct|pcs|pieces|pc|bags)\b",
    re.I,
)


def pack_size(text: str) -> int | None:
    """Units per listing when the text states it ("Pack of 6", "12 count"); None when it doesn't."""
    m = _PACK_RE.search(text or "")
    if not m:
        return None
    n = int(m.group(1) or m.group(2))
    return n if 1 < n <= 10000 else None


def _r(x: float) -> float:
    return round(x + 1e-9, 2)


class _SearchFailed(Exception):
    pass


async def _search(query: str, limit: int) -> dict:
    """POST /v1/search with one retry. Logs the real reason on failure."""
    bodies = [{"query": query, "limit": limit, "filters": {"availability": ["InStock"]}},
              {"query": query, "limit": limit}]
    last = ""
    for attempt, body in enumerate(bodies, start=1):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10)) as client:
                r = await client.post(
                    f"{config.CHANNEL3_BASE}/v1/search",
                    headers={"x-api-key": config.CHANNEL3_API_KEY, "Content-Type": "application/json"},
                    json=body,
                )
            if r.status_code < 400:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (401, 403):
                break  # bad key: retrying won't help
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        log.warning("Channel3 search attempt %d failed (%s)", attempt, last)
    raise _SearchFailed(last)


def _to_product(p: dict, currency: str) -> dict | None:
    offers = []
    for o in p.get("offers") or []:
        price = (o.get("price") or {})
        if price.get("currency") != currency or not price.get("price") or float(price["price"]) <= 0:
            continue
        if o.get("availability") and o["availability"] != "InStock":
            continue
        offers.append({"domain": o.get("domain"), "price": _r(float(price["price"])), "url": o.get("url")})
    if not offers:
        return None
    offers.sort(key=lambda o: o["price"])
    prices = [o["price"] for o in offers]
    images = p.get("images") or []
    main = next((i for i in images if i.get("is_main_image")), images[0] if images else None)
    brands = [b.get("name") for b in (p.get("brands") or []) if b.get("name")]
    text = " ".join([p.get("title") or "", p.get("description") or "", " ".join(p.get("key_features") or [])])
    return {
        "id": p.get("id"),
        "title": p.get("title") or "Untitled product",
        "brand": brands[0] if brands else None,
        "image": (main.get("cleaned_url") or main.get("url")) if main else None,
        "description": (p.get("description") or "")[:400],
        "offers": offers[:8],
        "low": prices[0],
        "median": _r(statistics.median(prices)),
        "high": prices[-1],
        "retailers": len({o["domain"] for o in offers}),
        "currency": currency,
        "units_per_item": pack_size(text),
    }


async def find_products(request: str, item: str, quantity: int, budget: float, currency: str = "USD") -> list[dict]:
    """Real products for this request, most relevant first, with the agent's pick marked `recommended`."""
    if not config.CHANNEL3_API_KEY:
        return []
    key = f"{item.lower()}|{currency}"
    async with _lock:
        if key not in _cache:
            try:
                data = await _search(item, 20)
            except _SearchFailed as exc:
                log.warning("Channel3 search failed: %s", exc)
                return []  # not cached, so the next order tries again
            _cache[key] = [x for x in (_to_product(p, currency) for p in data.get("products", [])) if x]
        products = [dict(p) for p in _cache[key]]
    if not products:
        return []
    return await _rank(request, item, quantity, budget, products)


async def _rank(request: str, item: str, quantity: int, budget: float, products: list[dict]) -> list[dict]:
    listing = "\n".join(
        f"{i}. {p['title']} | brand {p['brand'] or '?'} | {p['low']:.2f}-{p['high']:.2f} at {p['retailers']} "
        f"retailer(s) | {p['description'][:160]}" for i, p in enumerate(products))

    def fallback() -> dict:
        # Without Claude: keep products whose title mentions every main word of the request's item.
        words = [w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]+", item.lower())
                 if w not in {"with", "my", "the", "and", "for", "of", "a", "custom", "logo"}]
        keep = [i for i, p in enumerate(products)
                if all(w in p["title"].lower() for w in words)] or list(range(len(products)))
        return {"products": [{"i": i, "units_per_item": products[i]["units_per_item"]} for i in keep],
                "recommended": keep[0]}

    res = await ask_json(
        system="You help a small business owner choose exactly which real product their buying agent should buy.",
        prompt=(f"The owner asked for: \"{request}\" (about {quantity} units, total budget {budget:.2f}).\n"
                f"Products found:\n{listing}\n\n"
                "List the products that genuinely match the request, best match first (leave out accessories "
                "and different products). For each, give units_per_item: how many individual units one listing "
                "contains, read from the title or description (e.g. a 3-pack = 3), or null if not stated. "
                "Pick `recommended`: the best match that fits the budget for the quantity needed.\n"
                'Fields: {"products": [{"i": int, "units_per_item": int|null}], "recommended": int}'),
        fallback=fallback,
        max_tokens=800,
    )
    ranked, seen = [], set()
    for row in res.get("products") or []:
        try:
            i = int(row["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < len(products) and i not in seen:
            seen.add(i)
            units = row.get("units_per_item")
            p = products[i]
            if isinstance(units, (int, float)) and 1 <= units <= 10000:
                p["units_per_item"] = int(units)
            ranked.append(p)
    if not ranked:
        ranked = products
    rec = res.get("recommended")
    rec_id = products[rec]["id"] if isinstance(rec, int) and 0 <= rec < len(products) else ranked[0]["id"]
    ranked = ranked[:MAX_CANDIDATES]
    for p in ranked:
        p["recommended"] = p["id"] == rec_id
        units = p.get("units_per_item") or 1
        p["suggested_quantity"] = max(1, math.ceil(quantity / units))
    if not any(p["recommended"] for p in ranked):
        ranked[0]["recommended"] = True
    return ranked


def benchmark_for(product: dict) -> dict:
    """The market check for one exact product: its own retail offers."""
    return {
        "product_id": product["id"],
        "title": product["title"],
        "currency": product["currency"],
        "low": product["low"],
        "median": product["median"],
        "high": product["high"],
        "retailers": product["retailers"],
        "offers": product["offers"],
        "source": "channel3",
    }