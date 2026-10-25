"""Provider adapters (doc 02 §7): one narrow module per data source.

Every adapter here is keyless or env-keyed, returns verbatim bytes plus parsed
JSON, and never routes through ``getTransfersByAddress`` (blocked on the
Helius free plan — doc 01 §1.1).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolCandidate:
    """One pool observation from a discovery feed, normalised for filtering.

    ``txns_h1`` is None when the feed carries no recent-transaction window;
    the shortlist filter fails closed on unknown activity (a rug has no
    reliable activity fields either).
    """

    pool_address: str
    base_mint: str
    quote_mint: str
    dex: str
    created_at: _dt.datetime | None
    liquidity_usd: float | None
    txns_h1: int | None
    source: str          # 'geckoterminal' | 'dexscreener'

    def age_hours(self, now: _dt.datetime) -> float | None:
        if self.created_at is None:
            return None
        return (now - self.created_at).total_seconds() / 3600.0
