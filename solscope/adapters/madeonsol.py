"""MadeOnSol adapter — KOL labels, entry order, and KOL-anchored token discovery.

Verified against the live public reference (``/api-reference.md``) and the
OpenAPI document (``/api/v1/openapi.json``, version 1.20.0) on **2026-11-09**,
read without a key. What the spec actually says, and what this module relies on:

* Base URL ``https://madeonsol.com/api/v1``; auth ``Authorization: Bearer msk_…``.
  ``api.madeonsol.com`` does not exist — do not derive a host from the product
  name.
* Every endpoint used here is **FREE** (``x-madeonsol-tier: FREE``):
  ``/kol/leaderboard``, ``/kol/wallets``, ``/kol/feed``,
  ``/kol/first-touches``, ``/kol/tokens/{mint}/trades``,
  ``/kol/tokens/{mint}/entry-order``. ``/kol/coordination`` and
  ``/kol/coordination/history`` are **PRO** and return 403 on a free key, so the
  one-call historical "tokens ≥N KOLs" finder is NOT available to us.
* Live-feed endpoints (``/kol/feed``, ``/kol/first-touches``,
  ``/kol/tokens/{mint}/trades``) are **5 minutes delayed on the free tier** and
  carry ``delay`` / ``as_of``. Leaderboards and entry order are real-time on
  every tier. Scored/aggregated fields are not affected by the feed delay.
* **No endpoint exposes an "is early" boolean.** Earliness has to be derived.
  The two usable inputs are ``rank`` from ``/entry-order`` (MadeOnSol's own SQL
  first-buy order per wallet, deterministic on ``(traded_at, tx_signature,
  wallet)``) and ``market_cap_usd_at_trade`` from the trade records. Trades
  below the persistence floor (0.05 SOL / $3.50) are **not stored**, so an
  "early" verdict here means "early among trades large enough to persist",
  which is a real and reportable blind spot rather than a rounding detail.
* Pagination is heterogeneous: ``limit``/``offset`` on leaderboard and the
  window feeds (which walk a **fixed top-N universe**, not every token), and
  opaque keyset ``cursor`` on the feeds and per-token trades.
* Quota: 200 calls/day and a burst ceiling. Response headers carry
  ``X-RateLimit-Limit`` / ``-Remaining`` / ``-Used`` / ``-Reset`` (epoch
  seconds); ``/me`` reports daily *and* burst quota state. We enforce the daily
  cap locally as well, because a 429 mid-discovery wastes the calls already
  spent.

Why a local counter when the API reports quota: the headers tell you what is
left *after* a call, and ``/me`` costs a call. A persisted counter lets the
discovery script refuse to start when there is not enough budget for the run,
instead of dying halfway through and leaving a half-confirmed candidate list.
The counter is advisory — server headers win when they disagree (see
``QuotaState``), and the file lives under the gitignored ``data/`` tree.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ..ratelimit import TokenBucket
from .httpfetch import FetchError, fetch_bytes

BASE_URL = "https://madeonsol.com/api/v1"

DEFAULT_DAILY_CAP = 200
DEFAULT_BURST_PER_MIN = 60
DEFAULT_USAGE_PATH = pathlib.Path("data") / "madeonsol_usage.json"

# Rank 20 is the first-20-buyer window the quality filter already models; the
# cap-table and early-buyer panels are ranked to 20 by construction, so a KOL
# entering later than 20 is not an "early buyer" for any purpose this tool has.
EARLY_RANK_MAX = 20

# A token needs this many tracked-KOL buyers before it is interesting as an
# anchor. Matches the observed coordination threshold and `min_kols` default on
# the PRO coordination route.
DEFAULT_MIN_KOLS = 3


class MadeOnSolError(RuntimeError):
    """A MadeOnSol call failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, path: str, status: int | None, message: str) -> None:
        super().__init__(f"{path}: {message}" + (f" (HTTP {status})" if status else ""))
        self.path = path
        self.status = status


class AuthError(MadeOnSolError):
    """401 — missing/invalid key, or an expired free-tier key (they last 30 days)."""


class TierRequired(MadeOnSolError):
    """403 — the key's tier is below the route's gate (e.g. PRO on a free key)."""

    def __init__(self, path: str, status: int | None, message: str, *,
                 required_tier: str | None = None) -> None:
        super().__init__(path, status, message)
        self.required_tier = required_tier


class QuotaExhausted(MadeOnSolError):
    """The local daily counter is at the cap; no request was sent."""


@dataclass(frozen=True)
class QuotaState:
    """Local view of the daily budget. ``server_remaining`` is the last value a
    response header reported (None before the first call) — when the server says
    fewer calls remain than the local counter does, the server is right."""

    used_today: int
    daily_cap: int
    day: str
    server_remaining: int | None = None

    @property
    def remaining(self) -> int:
        local = max(0, self.daily_cap - self.used_today)
        if self.server_remaining is None:
            return local
        return min(local, max(0, self.server_remaining))

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


@dataclass(frozen=True)
class KOLRecord:
    """One ranked leaderboard row. Notably has NO recent-token list."""

    name: str
    wallet: str
    pnl_sol: float | None
    win_rate: float | None
    buy_count: int | None
    sell_count: int | None
    volume: float | None
    avg_entry_mc_usd: float | None
    entry_mc_samples: int | None
    percentile_early_entry_30d: float | None
    median_hold_minutes_30d: float | None
    period: str


@dataclass(frozen=True)
class KOLTrade:
    """One stored KOL trade. This is the only place price/MC-at-trade appear."""

    wallet: str
    kol_name: str | None
    token_mint: str
    token_symbol: str | None
    action: str                       # 'buy' | 'sell'
    sol_amount: float | None
    token_amount: float | None
    price_usd_at_trade: float | None
    market_cap_usd_at_trade: float | None
    tx_signature: str | None
    traded_at: _dt.datetime | None


@dataclass(frozen=True)
class EntryOrderEntry:
    """One KOL's first buy on a token, with the server's entry rank."""

    rank: int
    wallet: str
    kol_name: str | None
    first_buy_at: _dt.datetime | None
    seconds_after_first: int | None
    sol_amount: float | None
    token_amount: float | None
    tx_signature: str | None


@dataclass(frozen=True)
class EntryOrder:
    """KOL entry order for one mint. ``total_kol_buyers`` counts ALL KOL first
    buys, not just the returned page, so it is the count to trust."""

    token_mint: str
    token_symbol: str | None
    total_kol_buyers: int
    returned: int
    has_more: bool
    first_buy_at: _dt.datetime | None
    span_sec: int | None
    entries: tuple[EntryOrderEntry, ...]
    delayed: bool = False


@dataclass(frozen=True)
class FirstTouch:
    """A KOL first-touch event: the free tier's only *historical* multi-KOL feed.

    ``market_cap_usd_at_first_buy`` is what makes this usable as a candidate
    source without spending a call per token on its trades.
    """

    token_mint: str
    token_symbol: str | None
    first_buy_at: _dt.datetime | None
    market_cap_usd_at_first_buy: float | None
    first_kol_name: str | None
    token_age_minutes: int | None


@dataclass(frozen=True)
class KOLAnchorCandidate:
    """A token proposed as an anchor because several KOLs entered it early.

    ``verdict`` is deliberately coarse and derived only from counts and MC:
    STRONG (>= 5 KOLs at low MC), MODERATE (min_kols..4), WEAK (seen but not
    confirmed). Nothing here has been Helius-verified — see ``unverified``.
    """

    token_mint: str
    token_symbol: str | None
    kol_count: int
    kol_wallets: tuple[str, ...]
    entry_ranks: tuple[int, ...]
    first_kol_buy_at: _dt.datetime | None
    entry_mcap_usd: float | None
    verdict: str
    unverified: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #

def _dt_from_iso(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    """Numbers arrive as float/int/null. Never coerce a null to 0 — a missing
    MC is unknown, and treating it as a computed value would smuggle a fake
    data point into a "STRONG" verdict."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_leaderboard(payload: dict, *, period: str = "7d") -> list[KOLRecord]:
    rows = payload.get("leaderboard")
    if not isinstance(rows, list):
        return []
    out: list[KOLRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        wallet = row.get("wallet")
        if not isinstance(wallet, str) or not wallet:
            continue
        out.append(KOLRecord(
            name=row.get("name") or "",
            wallet=wallet,
            pnl_sol=_num(row.get("pnl")),
            win_rate=_num(row.get("win_rate")),
            buy_count=_int(row.get("buy_count")),
            sell_count=_int(row.get("sell_count")),
            volume=_num(row.get("volume")),
            avg_entry_mc_usd=_num(row.get("avg_entry_mc_usd")),
            entry_mc_samples=_int(row.get("entry_mc_samples")),
            percentile_early_entry_30d=_num(row.get("percentile_early_entry_30d")),
            median_hold_minutes_30d=_num(row.get("median_hold_minutes_30d")),
            period=payload.get("period") or period,
        ))
    return out


def parse_trades(payload: dict) -> list[KOLTrade]:
    rows = payload.get("trades")
    if not isinstance(rows, list):
        return []
    out: list[KOLTrade] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mint = row.get("token_mint") or row.get("mint")
        wallet = row.get("wallet") or row.get("wallet_address")
        if not isinstance(mint, str) or not isinstance(wallet, str):
            continue
        out.append(KOLTrade(
            wallet=wallet,
            kol_name=row.get("kol_name"),
            token_mint=mint,
            token_symbol=row.get("token_symbol"),
            action=(row.get("action") or "").lower() or "unknown",
            sol_amount=_num(row.get("sol_amount")),
            token_amount=_num(row.get("token_amount")),
            price_usd_at_trade=_num(row.get("price_usd_at_trade")),
            market_cap_usd_at_trade=_num(row.get("market_cap_usd_at_trade")),
            tx_signature=row.get("tx_signature"),
            traded_at=_dt_from_iso(row.get("traded_at")),
        ))
    return out


def parse_entry_order(payload: dict, *, fallback_mint: str = "") -> EntryOrder:
    entries_raw = payload.get("entries")
    entries: list[EntryOrderEntry] = []
    for row in entries_raw if isinstance(entries_raw, list) else []:
        if not isinstance(row, dict):
            continue
        rank = _int(row.get("rank"))
        wallet = row.get("wallet")
        if rank is None or not isinstance(wallet, str):
            continue
        entries.append(EntryOrderEntry(
            rank=rank,
            wallet=wallet,
            kol_name=row.get("kol_name"),
            first_buy_at=_dt_from_iso(row.get("first_buy_at")),
            seconds_after_first=_int(row.get("seconds_after_first")),
            sol_amount=_num(row.get("sol_amount")),
            token_amount=_num(row.get("token_amount")),
            tx_signature=row.get("tx_signature"),
        ))
    entries.sort(key=lambda e: e.rank)
    return EntryOrder(
        token_mint=payload.get("token_mint") or fallback_mint,
        token_symbol=payload.get("token_symbol"),
        total_kol_buyers=_int(payload.get("total_kol_buyers")) or len(entries),
        returned=_int(payload.get("returned")) or len(entries),
        has_more=bool(payload.get("has_more")),
        first_buy_at=_dt_from_iso(payload.get("first_buy_at")),
        span_sec=_int(payload.get("span_sec")),
        entries=tuple(entries),
        delayed=bool(payload.get("delay")),
    )


def parse_first_touches(payload: dict) -> list[FirstTouch]:
    events = payload.get("events")
    out: list[FirstTouch] = []
    for row in events if isinstance(events, list) else []:
        if not isinstance(row, dict):
            continue
        mint = row.get("token_mint")
        if not isinstance(mint, str) or not mint:
            continue
        first_kol = row.get("first_kol") if isinstance(row.get("first_kol"), dict) else {}
        out.append(FirstTouch(
            token_mint=mint,
            token_symbol=row.get("token_symbol"),
            first_buy_at=_dt_from_iso(row.get("first_buy_at")),
            market_cap_usd_at_first_buy=_num(row.get("market_cap_usd_at_first_buy")),
            first_kol_name=first_kol.get("name"),
            token_age_minutes=_int(row.get("token_age_minutes")),
        ))
    return out


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

class MadeOnSolClient:
    """Thin, honest client for the FREE-tier KOL routes.

    Every call goes through one token bucket (burst ceiling) and one persisted
    daily counter. The key is held in memory only and never logged, written or
    included in an error message.
    """

    BASE_URL = BASE_URL

    def __init__(
        self,
        api_key: str,
        *,
        daily_cap: int = DEFAULT_DAILY_CAP,
        burst_per_min: int = DEFAULT_BURST_PER_MIN,
        usage_path: str | os.PathLike[str] | None = DEFAULT_USAGE_PATH,
        opener=None,
        timeout: float = 20.0,
        retries: int = 2,
        clock=time.monotonic,
        sleeper=time.sleep,
        today: str | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ValueError("api_key is required (free tier: https://madeonsol.com/developer)")
        self._key = key
        self._opener = opener
        self._timeout = timeout
        self._retries = retries
        self._sleeper = sleeper
        self._today = today
        self._bucket = TokenBucket(capacity=burst_per_min, per_minute=burst_per_min,
                                   clock=clock, sleeper=sleeper)
        self._usage_path = pathlib.Path(usage_path) if usage_path else None
        self._daily_cap = daily_cap
        self.calls_made = 0
        self.server_remaining: int | None = None
        self._used, self._day = self._load_usage()

    # -- quota bookkeeping -------------------------------------------------- #

    def _day_key(self) -> str:
        if self._today:
            return self._today
        return _dt.datetime.now(_dt.timezone.utc).date().isoformat()

    def _load_usage(self) -> tuple[int, str]:
        day = self._day_key()
        if self._usage_path is None:
            return 0, day
        try:
            raw = json.loads(self._usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0, day
        if not isinstance(raw, dict) or raw.get("date") != day:
            return 0, day          # a new UTC day resets the free-tier quota
        used = _int(raw.get("used")) or 0
        return max(0, used), day

    def _save_usage(self) -> None:
        if self._usage_path is None:
            return
        try:
            self._usage_path.parent.mkdir(parents=True, exist_ok=True)
            self._usage_path.write_text(
                json.dumps({"date": self._day, "used": self._used}, indent=1) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass                   # never fail a successful API call on a local write

    def quota(self) -> QuotaState:
        return QuotaState(used_today=self._used, daily_cap=self._daily_cap,
                          day=self._day, server_remaining=self.server_remaining)

    def budget_for(self, calls: int) -> bool:
        """True when ``calls`` more requests fit in today's remaining budget."""
        return self.quota().remaining >= calls

    # -- transport ---------------------------------------------------------- #

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        if self.quota().exhausted:
            raise QuotaExhausted(path, None,
                                 f"daily cap {self._daily_cap} reached for {self._day}")

        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.BASE_URL}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        self._bucket.acquire()
        try:
            body, status = fetch_bytes(
                url,
                timeout=self._timeout,
                retries=self._retries,
                headers={"Authorization": f"Bearer {self._key}"},
                sleeper=self._sleeper,
                opener=self._opener,
            )
        except FetchError as exc:
            self._raise_for(path, exc)

        # Only a dispatched request is charged — a refused or failed call is not
        # counted, so a 403 does not eat the day's budget.
        self.calls_made += 1
        self._used += 1
        self._save_usage()

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise MadeOnSolError(path, status, f"malformed JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise MadeOnSolError(path, status, "unexpected payload (not an object)")
        return payload

    def _raise_for(self, path: str, exc: FetchError) -> None:
        """Map transport errors onto the API's documented failure modes."""
        status = exc.status
        payload: dict | None = None
        cause = exc.__cause__
        if isinstance(cause, urllib.error.HTTPError):
            try:
                payload = json.loads(cause.read().decode("utf-8", "replace"))
            except Exception:      # body already consumed / not JSON — detail is optional
                payload = None

        detail = ""
        required_tier = None
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("message") or "")
            upgrade = payload.get("upgrade")
            if isinstance(upgrade, dict):
                required_tier = upgrade.get("required_tier") or upgrade.get("tier")
            required_tier = required_tier or payload.get("required_tier")

        if status == 401:
            raise AuthError(path, status,
                            detail or "invalid or expired key (free keys last 30 days)")
        if status == 403:
            raise TierRequired(path, status,
                               detail or "tier below this route's gate",
                               required_tier=required_tier)
        if status == 429:
            raise MadeOnSolError(path, status, detail or "rate limited — honour Retry-After")
        raise MadeOnSolError(path, status, detail or str(exc))

    # -- endpoints ---------------------------------------------------------- #

    def get_leaderboard(self, limit: int = 100, *, period: str = "7d",
                        offset: int = 0) -> list[KOLRecord]:
        """Ranked KOLs by realized PnL (FREE). Rows carry no recent-token list,
        so this is a roster ranking, not a discovery source."""
        payload = self._get("/kol/leaderboard",
                            {"period": period, "limit": limit, "offset": offset})
        return parse_leaderboard(payload, period=period)

    def get_roster(self, *, limit: int = 200, offset: int = 0,
                   active: str = "true") -> dict[str, str]:
        """``{name: wallet}`` for tracked KOLs (FREE). Needed because the window
        feeds expose KOL *names* only below ULTRA — the wallets are stripped, so
        names must be resolved through this roster."""
        payload = self._get("/kol/wallets",
                            {"limit": limit, "offset": offset, "active": active})
        out: dict[str, str] = {}
        for row in payload.get("wallets") or []:
            if not isinstance(row, dict):
                continue
            name, wallet = row.get("name"), row.get("wallet_address")
            if isinstance(name, str) and isinstance(wallet, str) and name not in out:
                out[name] = wallet
        return out

    def get_kol_trades(self, wallet: str, limit: int = 50,
                       since: _dt.datetime | None = None, *,
                       cursor: str | None = None,
                       action: str | None = None) -> list[KOLTrade]:
        """One KOL's stored trades (FREE, 5-min delayed). There is no
        ``/kol/{wallet}/trades`` route — ``/kol/feed?kol=`` is the per-KOL list."""
        params = {
            "kol": wallet,
            "limit": limit,
            "since": since.isoformat() if since else None,
            "cursor": cursor,
            "action": action,
        }
        return parse_trades(self._get("/kol/feed", params))

    def get_token_kol_trades(self, mint: str, *, limit: int = 20,
                             cursor: str | None = None,
                             action: str | None = None) -> list[KOLTrade]:
        """Every stored KOL trade on one mint, newest first (FREE; BASIC is
        clamped to 20 rows/page). This is where MC-at-trade comes from."""
        params = {"limit": limit, "cursor": cursor, "action": action}
        return parse_trades(self._get(f"/kol/tokens/{urllib.parse.quote(mint)}/trades", params))

    def get_entry_order(self, mint: str, *, limit: int = EARLY_RANK_MAX) -> EntryOrder:
        """KOL first-buy order on one mint (FREE, real-time). Gives ``rank`` and
        ``total_kol_buyers`` — but **no mcap**: join to the trade records for that."""
        payload = self._get(f"/kol/tokens/{urllib.parse.quote(mint)}/entry-order",
                            {"limit": limit})
        return parse_entry_order(payload, fallback_mint=mint)

    def get_first_touches(self, *, min_kols: int = DEFAULT_MIN_KOLS,
                          max_mc_usd: float | None = None,
                          min_mc_usd: float | None = None,
                          limit: int = 50,
                          since: _dt.datetime | None = None,
                          cursor: str | None = None,
                          preset: str | None = None) -> tuple[list[FirstTouch], dict]:
        """Historical "token first touched by a KOL" feed (FREE), filtered to
        tokens with >= ``min_kols`` touches and an optional entry-MC band.

        Returns the parsed events plus the raw envelope, because ``has_more`` /
        ``next_cursor`` / ``scan.scan_truncated`` are needed to page honestly:
        the server may apply the MC filter after fetching candidates, and
        ``scan_truncated`` means more matches may exist past the cursor.
        """
        params = {
            "min_n_touches": min_kols,
            "max_mc_usd": max_mc_usd,
            "min_mc_usd": min_mc_usd,
            "limit": limit,
            "since": since.isoformat() if since else None,
            "cursor": cursor,
            "preset": preset,
        }
        payload = self._get("/kol/first-touches", params)
        return parse_first_touches(payload), payload

    def get_trending(self, *, period: str = "12h", min_kols: int = DEFAULT_MIN_KOLS,
                     limit: int = 20, offset: int = 0) -> list[dict]:
        """Tokens with KOL convergence *right now* (FREE, window-bound: 5m–12h,
        page size capped at 20 below ULTRA, and wallet addresses are ULTRA-only)."""
        payload = self._get("/kol/tokens/trending",
                            {"period": period, "min_kols": min_kols,
                             "limit": limit, "offset": offset})
        rows = payload.get("trending")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    # -- composite ---------------------------------------------------------- #

    def find_kol_anchors(
        self,
        min_kols: int = DEFAULT_MIN_KOLS,
        max_entry_mcap_usd: float | None = None,
        limit_tokens: int = 20,
        *,
        max_rank: int = EARLY_RANK_MAX,
        pages: int = 3,
        page_size: int = 50,
    ) -> list[KOLAnchorCandidate]:
        """Tokens that >= ``min_kols`` KOLs entered, confirmed by entry order.

        Two-stage on purpose: ``/kol/first-touches`` supplies candidates cheaply
        (one call per 50 tokens, with MC at the first KOL buy), then ONE
        ``/kol/tokens/{mint}/entry-order`` call per candidate confirms the KOL
        count and gives real ranks. The confirmation is what keeps a
        server-side ``min_n_touches`` filter from being taken on faith.

        ``max_entry_mcap_usd`` fails closed: a candidate whose entry MC is
        unknown is dropped when a cap is set, matching the API's own semantics
        ("tokens with unknown entry MC drop out when set") rather than silently
        passing a token we cannot price.

        Cost: ``1 + ceil(distinct_tokens / page_size) + limit_tokens`` calls.
        """
        if min_kols < 1:
            raise ValueError("min_kols must be >= 1")

        seen: dict[str, FirstTouch] = {}
        cursor: str | None = None
        for page in range(pages):
            events, envelope = self.get_first_touches(
                min_kols=min_kols, max_mc_usd=max_entry_mcap_usd,
                limit=page_size, cursor=cursor,
            )
            for event in events:
                seen.setdefault(event.token_mint, event)
            if len(seen) >= limit_tokens:
                break
            cursor = envelope.get("next_cursor")
            scan = envelope.get("scan") if isinstance(envelope.get("scan"), dict) else {}
            if not envelope.get("has_more") and not scan.get("scan_truncated"):
                break
            if not cursor and page + 1 < pages:
                break

        candidates: list[KOLAnchorCandidate] = []
        for mint, event in list(seen.items())[:limit_tokens]:
            order = self.get_entry_order(mint, limit=max(max_rank, min_kols))
            notes: list[str] = []
            if order.total_kol_buyers < min_kols:
                notes.append(
                    f"first-touches reported a touch but entry order sees only "
                    f"{order.total_kol_buyers} KOL buyer(s)"
                )
                continue

            entry_mcap = event.market_cap_usd_at_first_buy
            if entry_mcap is None:
                notes.append("entry MC unknown at the first KOL buy")
                if max_entry_mcap_usd is not None:
                    continue          # fail closed on an unpriced candidate
            if max_entry_mcap_usd is not None and entry_mcap is not None \
                    and entry_mcap > max_entry_mcap_usd:
                continue

            ranks = tuple(e.rank for e in order.entries[:max_rank])
            if not ranks:
                notes.append("entry order returned no rows within the rank window")
            elif max(ranks) > max_rank:
                notes.append(f"a KOL entered at rank {max(ranks)}, beyond {max_rank}")

            # Absolute bands, not relative to min_kols: the caller may lower the
            # inclusion floor (e.g. min_kols=2 for exploration) and still needs
            # the verdict to mean the same thing every run.
            if order.total_kol_buyers >= 5:
                verdict = "STRONG"
            elif order.total_kol_buyers >= 3:
                verdict = "MODERATE"
            else:
                verdict = "WEAK"

            candidates.append(KOLAnchorCandidate(
                token_mint=mint,
                token_symbol=event.token_symbol,
                kol_count=order.total_kol_buyers,
                kol_wallets=tuple(e.wallet for e in order.entries[:max_rank]),
                entry_ranks=ranks,
                first_kol_buy_at=order.first_buy_at or event.first_buy_at,
                entry_mcap_usd=entry_mcap,
                verdict=verdict,
                unverified=True,
                notes=tuple(notes),
            ))
        return candidates
