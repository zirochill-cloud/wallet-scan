"""DexScreener keyless adapter — secondary discovery source.

``/token-profiles/latest/v1`` (60 req/min) surfaces recently *profiled* mints,
not pairs: it carries no pair address, liquidity or creation time (verified
against the reference docs 2026-10-16). Each new Solana mint therefore goes
through the token-pair lookup (300 req/min family) to get real pairs before
the shortlist filter sees it.

Doc 10 finding #6: DexScreener 403s the default ``Python-urllib`` UA, so all
requests go through httpfetch's browser-like UA.
"""
from __future__ import annotations

import datetime as _dt
import json

from . import PoolCandidate
from .httpfetch import fetch_bytes

BASE = "https://api.dexscreener.com"
PROFILES_URL = BASE + "/token-profiles/latest/v1"
PAIRS_URL = BASE + "/latest/dex/tokens/{mint}"


def parse_profiles(body: bytes) -> list[str]:
    """Solana mints named by the profiles feed. Malformed entries are skipped."""
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []
    mints: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("chainId") != "solana":
            continue
        addr = item.get("tokenAddress")
        if isinstance(addr, str) and addr:
            mints.append(addr)
    return mints


def parse_pairs(body: bytes, *, mint: str) -> list[PoolCandidate]:
    """Normalise all Solana pairs DexScreener knows for ``mint``."""
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    pairs = payload.get("pairs") or [] if isinstance(payload, dict) else []
    out: list[PoolCandidate] = []
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        pool_address = pair.get("pairAddress")
        base = (pair.get("baseToken") or {}).get("address")
        quote = (pair.get("quoteToken") or {}).get("address")
        if not pool_address or not base:
            continue

        created_ms = pair.get("pairCreatedAt")
        created_at = None
        if isinstance(created_ms, (int, float)):
            created_at = _dt.datetime.fromtimestamp(created_ms / 1000.0, tz=_dt.timezone.utc)

        liq = (pair.get("liquidity") or {}).get("usd")
        try:
            liquidity = float(liq) if liq is not None else None
        except (TypeError, ValueError):
            liquidity = None

        h1 = (pair.get("txns") or {}).get("h1") or {}
        try:
            txns_h1 = int(h1.get("buys", 0)) + int(h1.get("sells", 0))
        except (TypeError, ValueError):
            txns_h1 = None

        out.append(PoolCandidate(
            pool_address=pool_address,
            base_mint=base,
            quote_mint=quote or "",
            dex=pair.get("dexId", "unknown"),
            created_at=created_at,
            liquidity_usd=liquidity,
            txns_h1=txns_h1,
            source="dexscreener",
        ))
        # DexScreener returns the deepest pool first; keeping every pair is
        # harmless because the filter + subscription cap bound the shortlist.
    return out


def fetch_profiles(*, timeout: float = 15.0, retries: int = 3,
                   opener=None) -> tuple[list[str], bytes]:
    body, _ = fetch_bytes(PROFILES_URL, timeout=timeout, retries=retries, opener=opener)
    return parse_profiles(body), body


def fetch_pairs(mint: str, *, timeout: float = 15.0, retries: int = 3,
                opener=None) -> tuple[list[PoolCandidate], bytes]:
    url = PAIRS_URL.format(mint=mint)
    body, _ = fetch_bytes(url, timeout=timeout, retries=retries, opener=opener)
    return parse_pairs(body, mint=mint), body
