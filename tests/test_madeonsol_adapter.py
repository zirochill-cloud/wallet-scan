"""MadeOnSol adapter — mocked-HTTP tests (no live calls, no key needed).

The adapter's job is to be *honest* about a third-party API whose free tier
drops fields (wallet addresses on window feeds), refuses routes (403/PRO) and
caps quota. So these tests inject a router as ``opener`` and assert on the
adapter's behaviour at exactly those boundaries:

    60/min burst        the 61st request sleeps, the first 60 do not
    200/day cap         the 201st request is refused *before* dispatch
    grouping            a token with 3 KOL buyers is a candidate; 2 is not
    mcap filter         above the cap is dropped, unknown MC fails closed
    tier/auth/quota     HTTP 401/403/429 map to distinct, key-free errors

None of this proves anything about live response *content*: the shapes come
from the public OpenAPI document (spec version 1.20.0, read 2026-11-09), and a
live call with a real key is still required to confirm field presence. Nothing
here writes a usage file outside a temp dir, and nothing here seeds anchors.

Run: python tests/test_madeonsol_adapter.py
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import urllib.error
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.adapters.madeonsol import (  # noqa: E402
    AuthError,
    MadeOnSolClient,
    MadeOnSolError,
    QuotaExhausted,
    TierRequired,
    parse_entry_order,
    parse_leaderboard,
    parse_trades,
)

KEY = "msk_test_key_not_real"
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        _failures.append(label)


# --------------------------------------------------------------------------- #
# fake transport
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class Router:
    """URL-substring router standing in for urllib. Records every request."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append((url, {k.lower(): v for k, v in request.headers.items()}))
        for needle, responder in self.routes.items():
            if needle in url:
                if callable(responder):
                    return responder(url)
                return FakeResponse(responder)
        raise AssertionError(f"unmocked URL: {url}")

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def http_error(url: str, code: int, payload: dict | None = None) -> urllib.error.HTTPError:
    body = json.dumps(payload or {}).encode("utf-8")
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(body))


def leaderboard_payload() -> dict:
    return {
        "leaderboard": [{
            "name": "alpha", "wallet": "W1", "pnl": 12.5, "buy_count": 30,
            "sell_count": 28, "volume": 400.0, "win_rate": 61.0,
            "avg_entry_mc_usd": 85_000, "entry_mc_samples": 27,
            "percentile_early_entry_30d": 88.0, "median_hold_minutes_30d": 42.0,
        }],
        "pagination": {"limit": 50, "offset": 0, "returned": 1, "has_more": False},
        "period": "7d",
    }


def first_touches_payload(events: list[dict], *, has_more: bool = False,
                          cursor: str | None = None,
                          scan_truncated: bool = False) -> dict:
    return {
        "events": events,
        "count": len(events),
        "next_cursor": cursor,
        "has_more": has_more,
        "scan": {"post_filtered": True, "scanned": 50,
                 "scan_truncated": scan_truncated, "scan_budget": 500},
    }


def event(mint: str, mcap: float | None, symbol: str = "TKN") -> dict:
    return {
        "token_mint": mint,
        "token_symbol": symbol,
        "first_buy_at": "2026-11-01T10:00:00Z",
        "market_cap_usd_at_first_buy": mcap,
        "first_kol": {"name": "alpha", "winrate_7d": 61.0},
        "token_age_minutes": 3,
    }


def entry_order_payload(mint: str, wallets: list[str]) -> dict:
    return {
        "token_mint": mint,
        "token_symbol": "TKN",
        "total_kol_buyers": len(wallets),
        "returned": len(wallets),
        "has_more": False,
        "complete": True,
        "first_buy_at": "2026-11-01T10:00:00Z",
        "last_buy_at": "2026-11-01T10:20:00Z",
        "span_sec": 1200,
        "entries": [
            {
                "rank": i + 1, "kol_name": f"k{i}", "wallet": w,
                "first_buy_at": "2026-11-01T10:00:00Z",
                "seconds_after_first": i * 90, "sol_amount": 1.5,
                "token_amount": 1000.0, "tx_signature": f"sig{i}",
            }
            for i, w in enumerate(wallets)
        ],
    }


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def test_parsing() -> None:
    print("\n[parsing] documented field names")

    rows = parse_leaderboard(leaderboard_payload())
    check("leaderboard row keeps wallet/pnl/avg_entry_mc_usd", len(rows) == 1
          and rows[0].wallet == "W1" and rows[0].pnl_sol == 12.5
          and rows[0].avg_entry_mc_usd == 85_000.0)
    check("entry_mc_samples carried separately from the average",
          rows[0].entry_mc_samples == 27)

    trades = parse_trades({"trades": [{
        "wallet": "W1", "kol_name": "alpha", "token_mint": "M1",
        "action": "buy", "sol_amount": 2.0, "token_amount": 500.0,
        "price_usd_at_trade": 0.00012, "market_cap_usd_at_trade": 120_000,
        "tx_signature": "s1", "traded_at": "2026-11-01T10:05:00Z",
    }]})
    check("trade exposes mcap+price at trade and traded_at", len(trades) == 1
          and trades[0].market_cap_usd_at_trade == 120_000.0
          and trades[0].price_usd_at_trade == 0.00012
          and trades[0].traded_at is not None
          and trades[0].traded_at.year == 2026)

    order = parse_entry_order(entry_order_payload("M1", ["W1", "W2"]))
    check("entry order ranks and counts", order.total_kol_buyers == 2
          and [e.rank for e in order.entries] == [1, 2]
          and order.entries[0].wallet == "W1")

    # Out-of-order rows must be sorted, not trusted as delivered.
    shuffled = entry_order_payload("M1", ["W1", "W2", "W3"])
    shuffled["entries"].reverse()
    check("entry order sorts by rank", [e.rank for e in parse_entry_order(shuffled).entries] == [1, 2, 3])

    # Live regression (2026-11-10): the per-token route carries token_mint in the
    # envelope only, so a row without its own mint must inherit it — otherwise
    # every row is dropped and the token looks KOL-free.
    per_token = parse_trades({
        "token_mint": "M_ENV", "count": 1, "has_more": True,
        "trades": [{"id": "u1", "wallet": "W9", "kol_name": "k9", "action": "buy",
                    "sol_amount": 1.0, "market_cap_usd_at_trade": 5_822.62,
                    "price_usd_at_trade": 0.00001, "tx_signature": "s9",
                    "traded_at": "2026-11-10T19:12:15+00:00"}],
    })
    check("per-token trades inherit the envelope mint (not dropped)",
          len(per_token) == 1 and per_token[0].token_mint == "M_ENV"
          and per_token[0].market_cap_usd_at_trade == 5_822.62,
          f"{per_token}")

    null_mcap = parse_trades({"trades": [{
        "wallet": "W1", "token_mint": "M1", "action": "sell",
        "market_cap_usd_at_trade": None, "traded_at": "2026-11-01T10:05:00Z",
    }]})
    check("null mcap stays None (never coerced to 0)",
          null_mcap[0].market_cap_usd_at_trade is None)


# --------------------------------------------------------------------------- #
# rate limiting + quota
# --------------------------------------------------------------------------- #

def test_burst_limit() -> None:
    print("\n[rate limit] 60/min burst")
    clock = FakeClock()
    router = Router({"/kol/leaderboard": leaderboard_payload()})
    client = MadeOnSolClient(KEY, burst_per_min=60, usage_path=None,
                             opener=router, clock=clock, sleeper=clock.sleep)

    for _ in range(60):
        client.get_leaderboard(limit=50, period="7d")
    check("the first 60 calls do not sleep", clock.sleeps == [], f"{clock.sleeps}")

    client.get_leaderboard(limit=50, period="7d")
    check("the 61st call is throttled (~1s at 60/min)",
          len(clock.sleeps) == 1 and abs(clock.sleeps[0] - 1.0) < 1e-6,
          f"{clock.sleeps}")
    check("all 61 requests were dispatched", len(router.calls) == 61)


def test_daily_cap() -> None:
    print("\n[daily cap] refuses at 200")
    clock = FakeClock()
    router = Router({"/kol/leaderboard": leaderboard_payload()})
    client = MadeOnSolClient(KEY, daily_cap=200, burst_per_min=1000,
                             usage_path=None, opener=router,
                             clock=clock, sleeper=clock.sleep)

    for _ in range(200):
        client.get_leaderboard(limit=50, period="7d")
    check("200 calls fit the free-tier day", client.quota().used_today == 200)

    before = len(router.calls)
    try:
        client.get_leaderboard(limit=50, period="7d")
        check("the 201st call raises QuotaExhausted", False, "no exception")
    except QuotaExhausted as exc:
        check("the 201st call raises QuotaExhausted", True)
        check("...and never reaches the network", len(router.calls) == before)
        check("...with the quota wording", "cap" in str(exc))
    check("quota().exhausted is True", client.quota().exhausted)
    check("budget_for(1) is False", not client.budget_for(1))


def test_usage_persistence() -> None:
    print("\n[usage] persisted counter + UTC day rollover")
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "data" / "madeonsol_usage.json"
        router = Router({"/kol/leaderboard": leaderboard_payload()})
        client = MadeOnSolClient(KEY, usage_path=path, opener=router,
                                 today="2026-11-09")
        client.get_leaderboard(limit=10)
        client.get_leaderboard(limit=10)
        saved = json.loads(path.read_text(encoding="utf-8"))
        check("counter written to the gitignored data/ path", saved == {"date": "2026-11-09", "used": 2})
        check("parent dir is created", path.parent.is_dir())

        again = MadeOnSolClient(KEY, usage_path=path, opener=router, today="2026-11-09")
        check("a new client resumes the same day's count", again.quota().used_today == 2)

        tomorrow = MadeOnSolClient(KEY, usage_path=path, opener=router, today="2026-11-10")
        check("a new UTC day resets the counter", tomorrow.quota().used_today == 0)


# --------------------------------------------------------------------------- #
# grouping / filter logic
# --------------------------------------------------------------------------- #

def test_grouping() -> None:
    print("\n[grouping] >= 3 KOLs is a candidate")
    router = Router({
        "/kol/first-touches": first_touches_payload([event("M_THREE", 180_000),
                                                     event("M_TWO", 180_000)]),
        "/kol/tokens/M_THREE/entry-order": entry_order_payload("M_THREE", ["W1", "W2", "W3"]),
        "/kol/tokens/M_TWO/entry-order": entry_order_payload("M_TWO", ["W1", "W2"]),
    })
    client = MadeOnSolClient(KEY, usage_path=None, opener=router)
    found = client.find_kol_anchors(min_kols=3, max_entry_mcap_usd=None, limit_tokens=20)

    check("only the 3-KOL token survives", [c.token_mint for c in found] == ["M_THREE"],
          f"{[c.token_mint for c in found]}")
    candidate = found[0]
    check("kol_count comes from entry order (not the touch filter)",
          candidate.kol_count == 3)
    check("all three KOL wallets are kept", candidate.kol_wallets == ("W1", "W2", "W3"))
    check("ranks 1..3 recorded", candidate.entry_ranks == (1, 2, 3))
    check("3 KOLs is MODERATE (5+ is STRONG)", candidate.verdict == "MODERATE")
    check("candidate is flagged unverified until Helius confirms first-20",
          candidate.unverified is True)
    check("entry MC from the first-touch event", candidate.entry_mcap_usd == 180_000.0)

    calls = client.calls_made
    check("cost = 1 candidate page + 1 entry-order per candidate", calls == 3, f"{calls} calls")


def test_strong_verdict() -> None:
    print("\n[verdict] 5+ KOLs at low mcap is STRONG")
    router = Router({
        "/kol/first-touches": first_touches_payload([event("M_FIVE", 90_000)]),
        "/kol/tokens/M_FIVE/entry-order": entry_order_payload(
            "M_FIVE", ["W1", "W2", "W3", "W4", "W5"]),
    })
    client = MadeOnSolClient(KEY, usage_path=None, opener=router)
    found = client.find_kol_anchors(min_kols=3, limit_tokens=5)
    check("5 KOLs -> STRONG", len(found) == 1 and found[0].verdict == "STRONG",
          f"{[c.verdict for c in found]}")
    check("kol_count is 5", found[0].kol_count == 5)


def test_weak_band() -> None:
    print("\n[verdict] bands are absolute, not relative to min_kols")
    router = Router({
        "/kol/first-touches": first_touches_payload([event("M_TWO", 90_000)]),
        "/kol/tokens/M_TWO/entry-order": entry_order_payload("M_TWO", ["W1", "W2"]),
    })
    client = MadeOnSolClient(KEY, usage_path=None, opener=router)
    found = client.find_kol_anchors(min_kols=2, limit_tokens=5)
    check("a 2-KOL token is included at min_kols=2 but scored WEAK",
          len(found) == 1 and found[0].verdict == "WEAK",
          f"{[c.verdict for c in found]}")

    floor = MadeOnSolClient(KEY, usage_path=None, opener=router)
    check("the same token is excluded at the default min_kols=3",
          floor.find_kol_anchors(limit_tokens=5) == [])


def test_mcap_filter() -> None:
    print("\n[mcap filter] band applies, unknown fails closed")
    router = Router({
        "/kol/first-touches": first_touches_payload([
            event("M_CHEAP", 250_000),
            event("M_PRICEY", 900_000),
            event("M_UNKNOWN", None),
        ]),
        "/kol/tokens/M_CHEAP/entry-order": entry_order_payload("M_CHEAP", ["W1", "W2", "W3"]),
        "/kol/tokens/M_PRICEY/entry-order": entry_order_payload("M_PRICEY", ["W1", "W2", "W3"]),
        "/kol/tokens/M_UNKNOWN/entry-order": entry_order_payload("M_UNKNOWN", ["W1", "W2", "W3"]),
    })
    client = MadeOnSolClient(KEY, usage_path=None, opener=router)
    found = client.find_kol_anchors(min_kols=3, max_entry_mcap_usd=500_000, limit_tokens=20)

    check("only the in-band token survives", [c.token_mint for c in found] == ["M_CHEAP"],
          f"{[c.token_mint for c in found]}")
    capped = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
              for u in router.urls() if "/kol/first-touches" in u]
    check("the cap is sent to the API, not only applied locally",
          bool(capped) and float(capped[0]["max_mc_usd"][0]) == 500_000.0
          and float(capped[0]["min_n_touches"][0]) == 3.0,
          f"{capped}")
    # A candidate with unknown MC is dropped rather than assumed cheap: the
    # server's own contract says unknown-MC tokens fall out of a capped query.
    check("unknown entry MC is excluded when a cap is set", "M_UNKNOWN" not in
          [c.token_mint for c in found])

    free_band = MadeOnSolClient(KEY, usage_path=None, opener=router)
    unbanded = free_band.find_kol_anchors(min_kols=3, max_entry_mcap_usd=None, limit_tokens=20)
    check("with no cap, unknown MC is allowed through and noted",
          "M_UNKNOWN" in [c.token_mint for c in unbanded]
          and any("unknown" in n for c in unbanded if c.token_mint == "M_UNKNOWN"
                  for n in c.notes))


def test_confirmation_disagrees() -> None:
    print("\n[confirmation] trust entry order over the touch filter")
    router = Router({
        "/kol/first-touches": first_touches_payload([event("M_LIAR", 100_000)]),
        # Server-side min_n_touches said >=3, entry order says 2 distinct buyers.
        "/kol/tokens/M_LIAR/entry-order": entry_order_payload("M_LIAR", ["W1", "W2"]),
    })
    client = MadeOnSolClient(KEY, usage_path=None, opener=router)
    check("a token the filter over-reported is dropped",
          client.find_kol_anchors(min_kols=3) == [])


# --------------------------------------------------------------------------- #
# error mapping
# --------------------------------------------------------------------------- #

def test_error_mapping() -> None:
    print("\n[errors] 401 / 403 / 429 are distinct and key-free")

    def raise_401(url): raise http_error(url, 401, {"error": "invalid_api_key"})
    def raise_403(url): raise http_error(url, 403, {"error": "tier_required",
                                                   "upgrade": {"required_tier": "PRO"}})
    def raise_429(url): raise http_error(url, 429, {"error": "rate_limit_exceeded"})

    for label, fn, exc_type in [("401 -> AuthError", raise_401, AuthError),
                                ("403 -> TierRequired", raise_403, TierRequired),
                                ("429 -> MadeOnSolError", raise_429, MadeOnSolError)]:
        client = MadeOnSolClient(KEY, usage_path=None,
                                 opener=Router({"/kol/coordination": fn}), retries=0)
        try:
            client._get("/kol/coordination")
            check(label, False, "no exception")
        except exc_type as exc:
            check(label, True)
            check(f"{label}: key never appears in the message", KEY not in str(exc))
            if exc_type is TierRequired:
                check("403 exposes the required tier", exc.required_tier == "PRO",
                      f"{exc.required_tier}")
        except MadeOnSolError as exc:
            check(label, False, f"wrong type {type(exc).__name__}")

    client = MadeOnSolClient(KEY, usage_path=None,
                             opener=Router({"/kol/coordination": raise_403}), retries=0)
    try:
        client._get("/kol/coordination")
    except TierRequired:
        pass
    check("a refused tier does not consume the day's quota", client.quota().used_today == 0)


def test_request_shape() -> None:
    print("\n[request] auth header + documented query params")
    router = Router({"/kol/feed": {"trades": []}})
    client = MadeOnSolClient(KEY, usage_path=None, opener=router)
    client.get_kol_trades("WALLET123", limit=50, since=None)

    url, headers = router.calls[0]
    check("Bearer auth header sent", headers.get("authorization") == f"Bearer {KEY}")
    check("per-KOL feed uses /kol/feed?kol=", "/kol/feed?kol=WALLET123" in url, url)
    check("None params are omitted, not sent as 'None'", "since=None" not in url, url)

    token_router = Router({"/kol/tokens/M9/trades": {
        "token_mint": "M9", "count": 1, "has_more": False,
        "trades": [{"wallet": "W1", "kol_name": "k", "action": "buy",
                    "market_cap_usd_at_trade": 4_000.0, "traded_at": "2026-11-10T19:00:00Z"}],
    }})
    made = MadeOnSolClient(KEY, usage_path=None, opener=token_router)
    rows = made.get_token_kol_trades("M9", limit=20)
    check("get_token_kol_trades returns rows with the requested mint",
          len(rows) == 1 and rows[0].token_mint == "M9", f"{rows}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    print("MadeOnSol adapter — mocked HTTP (no live calls)")
    test_parsing()
    test_burst_limit()
    test_daily_cap()
    test_usage_persistence()
    test_grouping()
    test_strong_verdict()
    test_weak_band()
    test_mcap_filter()
    test_confirmation_disagrees()
    test_error_mapping()
    test_request_shape()

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("ALL MADEONSOL ADAPTER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
