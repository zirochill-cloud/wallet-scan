"""GeckoTerminal keyless adapter — the primary new-pool discovery feed.

``GET /api/v2/networks/solana/new_pools`` returns pools created in the last
~48h, 20 per page, at the keyless tier's ~10 calls/min (doc 01 §2.2 — the
conservative published figure; the older FAQ said 30/min). Unlike DexScreener's
profile feeds it carries actual *pools*: address, creation time, USD reserves,
and windowed transaction counts — everything the shortlist filter needs without
a second lookup.

Response shape verified live 2026-10-16: resource ids are ``solana_<mint>``;
``attributes.reserve_in_usd`` and ``transactions.h1.{buys,sells}`` are strings
and ints respectively.
"""
from __future__ import annotations

import datetime as _dt

from . import PoolCandidate
from .httpfetch import fetch_bytes

BASE = "https://api.geckoterminal.com/api/v2"
NEW_POOLS = BASE + "/networks/solana/new_pools?page={page}"
# Same resource shape as new_pools, but ordered by reserve for one DEX. Used by
# the Phase 2 gate, which needs ~20 live pools of a single verified family;
# new_pools alone does not yield that many in three pages.
DEX_POOLS = BASE + "/networks/solana/dexes/{dex}/pools?page={page}"


def _parse_iso(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mint_from_id(res_id: str) -> str:
    # GT prefixes ids with the network: 'solana_<mint>'.
    return res_id.split("_", 1)[1] if "_" in res_id else res_id


def parse_pool(item: dict) -> PoolCandidate | None:
    """Normalise one GT pool resource. Returns None on a malformed record —
    a provider schema change must degrade the feed, not crash the collector."""
    try:
        attrs = item["attributes"]
        rels = item["relationships"]
        pool_address = attrs["address"]
        base_mint = _mint_from_id(rels["base_token"]["data"]["id"])
        quote_mint = _mint_from_id(rels["quote_token"]["data"]["id"])
        dex = rels.get("dex", {}).get("data", {}).get("id", "unknown")
    except (KeyError, TypeError, AttributeError):
        return None

    reserve = attrs.get("reserve_in_usd")
    try:
        liquidity = float(reserve) if reserve is not None else None
    except (TypeError, ValueError):
        liquidity = None

    h1 = (attrs.get("transactions") or {}).get("h1") or {}
    try:
        txns_h1 = int(h1.get("buys", 0)) + int(h1.get("sells", 0))
    except (TypeError, ValueError):
        txns_h1 = None

    return PoolCandidate(
        pool_address=pool_address,
        base_mint=base_mint,
        quote_mint=quote_mint,
        dex=dex,
        created_at=_parse_iso(attrs.get("pool_created_at")),
        liquidity_usd=liquidity,
        txns_h1=txns_h1,
        source="geckoterminal",
    )


def fetch_dex_pools(dex: str = "pumpswap", *, pages: int = 1,
                    timeout: float = 15.0, retries: int = 3,
                    opener=None, sleep=None) -> list[tuple[PoolCandidate, bytes, str]]:
    """Pools for one DEX, ordered by reserve.

    Same parsing as ``fetch_new_pools`` (identical resource shape). ``sleep`` is
    called between pages because the keyless tier allows ~10 calls/min and
    hammering it returns HTTP 429.
    """
    import json
    import time as _time

    pause = sleep or _time.sleep
    out: list[tuple[PoolCandidate, bytes, str]] = []
    for page in range(1, pages + 1):
        if page > 1:
            pause(6.5)
        url = DEX_POOLS.format(dex=dex, page=page)
        body, _status = fetch_bytes(url, timeout=timeout, retries=retries,
                                    opener=opener)
        try:
            payload = json.loads(body)
        except ValueError:
            break
        for item in payload.get("data", []):
            cand = parse_pool(item)
            if cand is not None:
                out.append((cand, body, url))
    return out


def fetch_new_pools(*, pages: int = 1, timeout: float = 15.0, retries: int = 3,
                    opener=None) -> list[tuple[PoolCandidate, bytes, str]]:
    """Fetch up to ``pages`` pages of new pools.

    Returns (candidate, verbatim_body, url) per pool so the caller can archive
    each payload verbatim without re-serialising (raw/ must hold what the
    provider actually sent).
    """
    out: list[tuple[PoolCandidate, bytes, str]] = []
    for page in range(1, pages + 1):
        url = NEW_POOLS.format(page=page)
        body, _status = fetch_bytes(url, timeout=timeout, retries=retries, opener=opener)
        try:
            import json
            payload = json.loads(body)
        except ValueError:
            break  # malformed page: keep what we have, fail visibly upstream
        for item in payload.get("data", []):
            cand = parse_pool(item)
            if cand is not None:
                out.append((cand, body, url))
    return out
