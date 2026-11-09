"""Real-data calibration of the quality filter on the 100 seeded anchor buyers.

Evidence only. This script imports the filter and calls it; it changes no rule, no
threshold and no database row. It reads the five anchors and their 100 first-20
buyers from the database, pulls each wallet's on-chain history over Helius RPC, and
answers three questions the synthetic calibration could not:

  1. what does ``screen_wallet`` do to real wallets, and which rule does the work?
  2. which thresholds are load-bearing on this sample, and which are inert?
  3. do the verdicts survive a look at the wallets' actual activity profiles?

Usage
-----
    python spikes/calibrate_filter.py fetch      # pull + derive, cache per wallet
    python spikes/calibrate_filter.py report     # tables, sweeps, JSON artifact
    python spikes/calibrate_filter.py all        # fetch anything missing, then report

Fetches are cached under ``%TEMP%/solscope-filter-calibration`` as *derived*
observations (swaps, positions, profile), not raw payloads — 100 wallets of full
transactions is hundreds of megabytes, and the sweep re-uses the derivation.

Sampling, stated plainly
------------------------
A wallet's history is sampled **newest-first** and truncated at ``MAX_TXS``
transactions, so the dropped end is the distant past rather than the present. The
first pass sampled oldest-first; that labelled a wallet ``insufficient_swaps`` from a
quiet early slice whose ten most recent transactions were all swaps — a sampling
artifact, not a wallet property. Order is recorded on every observation
(``sample_order``) so a rate computed over a 2-day slice is not read as a lifetime
average.

Router detection is a program-id allowlist. A swap routed by an aggregator this list
does not know counts as *no* aggregator, which can only make ``router_only`` fire
less often — the classifier gap is reported as ``unrouted_swaps`` rather than hidden.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import statistics
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from solscope.quality_filter import (  # noqa: E402
    DEFAULT_SCREEN_CONFIG,
    GENESIS_BURST_MIN_ANCHORS,
    AnchorObservation,
    FilterVerdict,
    RoundTrip,
    SwapObservation,
    WalletAnchorEntry,
    WalletVerdict,
    genesis_burst,
    router_only,
    sandwich,
    screen_wallet,
    throughput,
)

#: Newest-first sample. The first pass used ascending order and truncated at
#: MAX_TXS, which for a wallet whose early history is quiet captured a swap-free
#: slice and reported it as ``insufficient_swaps`` while its ten most recent
#: transactions were all swaps. Recent behaviour is what a screening rule is about,
#: so the sample runs backwards from now and the oldest end is what gets dropped.
CACHE = pathlib.Path(tempfile.gettempdir()) / "solscope-filter-calibration" / "newest"
CACHE.mkdir(parents=True, exist_ok=True)

PAGE = 500
MAX_TXS = 1500

# Jupiter v6/v4 and the OKX router are the three this project can name with
# confidence. Everything else is deliberately absent: a wrong entry invents an
# aggregator share the chain does not show.
AGGREGATORS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter_v6",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "jupiter_v4",
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5dZxr5vF": "okx_router",
}

POOLS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm_v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1zpdTHKxQB5qKP1C": "raydium_cpmm",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca_whirlpool",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump_fun",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora_dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora_damm",
}


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

def _api_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("HELIUS_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("HELIUS_API_KEY missing from .env")


URL = f"https://mainnet.helius-rpc.com/?api-key={_api_key()}"


def rpc(method: str, params, *, tries: int = 5) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                URL, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "solscope-cal/1"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = json.loads(resp.read())
            if "error" in payload:
                raise RuntimeError(f"{method}: {payload['error']}")
            return payload["result"]
        except Exception:  # noqa: BLE001 - retry, then surface
            if attempt == tries - 1:
                raise
            time.sleep(2.0 * (attempt + 1))
    raise AssertionError("unreachable")


def pages(wallet: str):
    """Newest-first pages of full transactions, stopping at MAX_TXS."""
    token = None
    seen = 0
    while seen < MAX_TXS:
        opts = {
            "transactionDetails": "full",
            "sortOrder": "desc",
            "limit": min(PAGE, MAX_TXS - seen),
            "commitment": "finalized",
            "maxSupportedTransactionVersion": 1,
        }
        if token:
            opts["paginationToken"] = token
        res = rpc("getTransactionsForAddress", [wallet, opts])
        data = res.get("data") or []
        yield data
        seen += len(data)
        token = res.get("paginationToken")
        if not data or not token:
            return


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

def _keys(tx: dict) -> list[str]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in msg.get("accountKeys") or []]
    loads = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += list(loads.get("writable") or []) + list(loads.get("readonly") or [])
    return [k for k in keys if k]


def _signers(tx: dict) -> list[str]:
    keys = _keys(tx)
    msg = (tx.get("transaction") or {}).get("message") or {}
    n = int((msg.get("header") or {}).get("numRequiredSignatures") or 1)
    return keys[:n]


def _token_deltas(tx: dict, wallet: str) -> dict[str, int]:
    """Per-mint token delta for ``wallet``, from pre/post balances."""
    keys = _keys(tx)
    meta = tx.get("meta") or {}
    pre = {b.get("accountIndex"): b for b in meta.get("preTokenBalances") or []}
    post = {b.get("accountIndex"): b for b in meta.get("postTokenBalances") or []}
    out: dict[str, int] = {}
    for idx in set(pre) | set(post):
        before, after = pre.get(idx) or {}, post.get(idx) or {}
        owner = after.get("owner") or before.get("owner")
        if not owner and isinstance(idx, int) and idx < len(keys):
            owner = keys[idx]  # token account owned by the wallet itself
        if owner != wallet:
            continue
        mint = after.get("mint") or before.get("mint")
        if not mint:
            continue
        a = int(((after.get("uiTokenAmount") or {}).get("amount")) or 0)
        b = int(((before.get("uiTokenAmount") or {}).get("amount")) or 0)
        out[mint] = out.get(mint, 0) + (a - b)
    return out


def _sol_delta(tx: dict, wallet: str) -> int:
    keys = _keys(tx)
    meta = tx.get("meta") or {}
    if wallet not in keys:
        return 0
    i = keys.index(wallet)
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if i < len(pre) and i < len(post):
        return int(post[i]) - int(pre[i])
    return 0


def derive(wallet: str) -> dict:
    """Fetch, then reduce to swaps / positions / profile. Never stores raw txs."""
    swaps: list[SwapObservation] = []
    events: dict[str, list[tuple[dt.datetime, int]]] = {}
    kinds = {"swap": 0, "transfer": 0, "other": 0}
    sol_amounts: list[int] = []
    times: list[dt.datetime] = []
    mints: set[str] = set()
    first_buy: dict[str, dt.datetime] = {}
    n_tx = 0
    truncated = False

    # Pages arrive newest-first; the derivation wants chronology (first buy, first
    # sell, hold duration), so sort the whole sample by block time once.
    collected: list[dict] = []
    for page in pages(wallet):
        collected.extend(page)
    collected.sort(key=lambda t: t.get("blockTime") or 0)

    for tx in collected:
        n_tx += 1
        when = tx.get("blockTime")
        if not when:
            continue
        at = dt.datetime.fromtimestamp(when, dt.timezone.utc)
        times.append(at)
        deltas = _token_deltas(tx, wallet)
        keys = set(_keys(tx))
        agg = next((AGGREGATORS[k] for k in keys if k in AGGREGATORS), None)
        pool = next((POOLS[k] for k in keys if k in POOLS), None)
        owner_signed = wallet in _signers(tx)

        if deltas and (agg or pool):
            kinds["swap"] += 1
            swaps.append(SwapObservation(at=at, aggregator=agg, pool=None if agg else pool))
            sol_amounts.append(abs(_sol_delta(tx, wallet)))
            for mint, delta in deltas.items():
                mints.add(mint)
                events.setdefault(mint, []).append((at, delta))
                if delta > 0:
                    first_buy.setdefault(mint, at)
        elif deltas:
            kinds["transfer"] += 1
        elif owner_signed:
            kinds["other"] += 1
    truncated = n_tx >= MAX_TXS

    trips: list[RoundTrip] = []
    for mint, evs in events.items():
        evs.sort(key=lambda e: e[0])
        opened = None
        for at, delta in evs:
            if delta > 0 and opened is None:
                opened = at
            elif delta < 0 and opened is not None:
                trips.append(RoundTrip(mint=mint, opened_at=opened, closed_at=at))
                opened = None

    gaps = []
    ordered = sorted(times)
    for a, b in zip(ordered, ordered[1:]):
        gaps.append((b - a).total_seconds())

    span = (ordered[-1] - ordered[0]).total_seconds() if len(ordered) > 1 else 0.0
    hours = {t.hour for t in ordered}
    profile = {
        "txs": n_tx,
        "sample_order": "newest_first",
        "truncated": truncated,
        "truncated_at": MAX_TXS if truncated else None,
        "span_days": round(span / 86400, 3),
        "kinds": kinds,
        "swaps": len(swaps),
        "distinct_mints": len(mints),
        "aggregator_swaps": sum(1 for s in swaps if s.aggregator),
        "direct_pool_swaps": sum(1 for s in swaps if s.hit_pool_directly),
        # Token movements with no allowlisted venue. Some are plain transfers and some
        # are swaps through a router this script does not name; they are counted
        # together because the script cannot tell them apart, and they are NOT added
        # to the swap series -- which can only make router_only fire less often.
        "token_moves_no_venue": kinds["transfer"],
        "swaps_per_day": round(len(swaps) / max(span, 60.0) * 86400, 2) if swaps else 0.0,
        "median_gap_seconds": round(statistics.median(gaps), 1) if gaps else None,
        "distinct_hours": len(hours),
        "sol_amounts_median": int(statistics.median(sol_amounts)) if sol_amounts else None,
        "sol_amounts_cv": (
            round(statistics.pstdev(sol_amounts) / statistics.fmean(sol_amounts), 3)
            if len(sol_amounts) > 1 and statistics.fmean(sol_amounts) else None
        ),
        "first_buy_at": {m: t.isoformat() for m, t in list(first_buy.items())[:20]},
    }
    return {
        "wallet": wallet,
        "swaps": [
            {"at": s.at.isoformat(), "aggregator": s.aggregator, "pool": s.pool} for s in swaps
        ],
        "positions": [
            {
                "mint": t.mint,
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in trips
        ],
        "first_buy_at": {m: t.isoformat() for m, t in first_buy.items()},
        "profile": profile,
    }


def _load_obs(wallet: str, *, refresh: bool = False) -> dict:
    path = CACHE / f"obs_{wallet}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    obs = derive(wallet)
    path.write_text(json.dumps(obs))
    return obs


# ---------------------------------------------------------------------------
# Inputs from the database
# ---------------------------------------------------------------------------

def load_sample() -> tuple[dict[str, AnchorObservation], dict[str, str], dict[str, int]]:
    """Anchors, wallet -> anchor mint, and anchor_id -> anchor_id (for reporting)."""
    from solscope import db

    conn = db.connect()
    rows = conn.execute(
        "SELECT anchor_id, mint, genesis_time, deployer FROM anchors ORDER BY anchor_id"
    ).fetchall()
    anchors: dict[str, AnchorObservation] = {}
    for _aid, mint, genesis, deployer in rows:
        anchors[mint] = AnchorObservation(mint, genesis, deployer=deployer)
    pairs = conn.execute(
        "SELECT v.wallet, a.mint FROM anchor_wallet_verdicts v "
        "JOIN anchors a ON a.anchor_id = v.anchor_id ORDER BY v.wallet"
    ).fetchall()
    conn.close()
    return anchors, {w: m for w, m in pairs}, {m: i for i, m in enumerate(sorted(anchors), 1)}


# ---------------------------------------------------------------------------
# Step 1: screen every wallet
# ---------------------------------------------------------------------------

def anchor_entries(o: dict, mint: str) -> list[WalletAnchorEntry]:
    """The wallet's entry into its anchor, or none when no buy was observed.

    ``WalletAnchorEntry.first_buy_at`` is non-optional by contract, so an unobserved
    buy is an EMPTY entry list — the wallet has no anchor evidence — rather than an
    entry with a null timestamp. An invented timestamp would manufacture a burst
    verdict; an empty list correctly yields not_computable.
    """
    if mint not in o["first_buy_at"]:
        return []
    return [WalletAnchorEntry(
        anchor_mint=mint,
        first_buy_at=dt.datetime.fromisoformat(o["first_buy_at"][mint]),
    )]


def rebuild_inputs(o: dict) -> tuple[list[SwapObservation], list[RoundTrip]]:
    swaps = [SwapObservation(at=dt.datetime.fromisoformat(s["at"]),
                             aggregator=s["aggregator"], pool=s["pool"])
             for s in o["swaps"]]
    trips = [RoundTrip(mint=p["mint"],
                       opened_at=dt.datetime.fromisoformat(p["opened_at"]) if p["opened_at"] else None,
                       closed_at=dt.datetime.fromisoformat(p["closed_at"]) if p["closed_at"] else None)
             for p in o["positions"]]
    return swaps, trips


def screen_all(anchors, wallet_anchor, obs, config=None):
    out: dict[str, FilterVerdict] = {}
    for wallet, mint in wallet_anchor.items():
        swaps, trips = rebuild_inputs(obs[wallet])
        out[wallet] = screen_wallet(wallet, anchors, trips, swaps, config,
                                    entries=anchor_entries(obs[wallet], mint))
    return out


def verdict_bucket(verdict: WalletVerdict) -> str:
    if verdict is WalletVerdict.ACCEPTED:
        return "accepted"
    if verdict.value.startswith("wallet.reject"):
        return "rejected"
    return "not_computable"


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "all"
    anchors, wallet_anchor, _ = load_sample()
    wallets = sorted(wallet_anchor)
    print(f"sample: {len(wallets)} wallets over {len(anchors)} anchors", flush=True)

    if mode in ("fetch", "all"):
        t0 = time.time()
        for i, w in enumerate(wallets, 1):
            if (CACHE / f"obs_{w}.json").exists():
                continue
            obs = _load_obs(w)
            p = obs["profile"]
            print(f"  [{i}/{len(wallets)}] {w[:8]}… txs {p['txs']} swaps {p['swaps']} "
                  f"span {p['span_days']}d ({time.time() - t0:.0f}s)", flush=True)
        print(f"fetch done in {time.time() - t0:.0f}s", flush=True)

    obs = {w: _load_obs(w) for w in wallets}

    # --- step 1 ---------------------------------------------------------
    verdicts = screen_all(anchors, wallet_anchor, obs)
    counts = {"accepted": 0, "rejected": 0, "not_computable": 0}
    by_verdict: dict[str, int] = {}
    for v in verdicts.values():
        counts[verdict_bucket(v.verdict)] += 1
        by_verdict[v.verdict.value] = by_verdict.get(v.verdict.value, 0) + 1

    print("\n## Step 1 — screen_wallet on 100 real wallets\n")
    print("| Outcome | Wallets |")
    print("|---|---|")
    for verdict, n in sorted(by_verdict.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"| `{verdict}` | {n} |")
    print(f"| **total** | {sum(by_verdict.values())} |")
    print(f"\nrejection rate: {counts['rejected']}/{len(wallets)} = "
          f"{counts['rejected'] / len(wallets):.0%}")

    # --- per-rule firing, in isolation ----------------------------------
    isolated: dict[str, set[str]] = {"genesis_burst": set(), "sandwich": set(),
                                     "throughput": set(), "router_only": set()}
    burst_computable = 0
    for wallet, mint in wallet_anchor.items():
        o = obs[wallet]
        swaps, trips = rebuild_inputs(o)
        entries = anchor_entries(o, mint)
        if entries and anchors[mint].genesis_at:
            burst_computable += 1
        if genesis_burst(wallet, anchors, entries=entries):
            isolated["genesis_burst"].add(wallet)
        if sandwich(wallet, trips):
            isolated["sandwich"].add(wallet)
        if throughput(wallet, swaps):
            isolated["throughput"].add(wallet)
        if router_only(wallet, swaps):
            isolated["router_only"].add(wallet)

    print("\n### Per-rule firing (each rule evaluated alone, default thresholds)\n")
    print("| Rule | Wallets it fires on | Share |")
    print("|---|---|---|")
    for rule, ws in isolated.items():
        print(f"| `{rule}` | {len(ws)} | {len(ws) / len(wallets):.0%} |")
    print(f"\nwallets whose genesis window was computable at all: {burst_computable} "
          f"(each wallet is listed under ONE anchor, so N=2 cannot be reached)")

    # --- overlap --------------------------------------------------------
    print("\n### Rule overlap (wallets where >=2 heuristics fire)\n")
    names = ["sandwich", "throughput", "router_only"]
    print("| pair | co-firing wallets |")
    print("|---|---|")
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            print(f"| {a} + {b} | {len(isolated[a] & isolated[b])} |")
    multi = [w for w in wallets if sum(w in isolated[n] for n in names) >= 2]
    print(f"\nwallets with >=2 heuristics firing: {len(multi)} / {len(wallets)}")

    # --- not computable -------------------------------------------------
    thin = [w for w, o in obs.items() if o["profile"]["swaps"] < 3]
    print(f"\n### Wallets with <3 swaps (not_computable): {len(thin)}")

    # --- step 2: sweeps -------------------------------------------------
    print("\n## Step 2 — threshold sensitivity (each variant in isolation)\n")
    sweeps = {
        "throughput_max_swaps_per_day": [50, 100, 200, 500, 1000],
        "router_only_min_aggregator_fraction": [0.80, 0.90, 0.95, 0.99],
        "sandwich_max_seconds": [30, 60, 90, 180],
    }
    sweep_rows: list[dict] = []

    def wallet_inputs(wallet: str):
        return rebuild_inputs(obs[wallet])

    for key, values in sweeps.items():
        print(f"**{key}**\n")
        print("| value | wallets the rule fires on |")
        print("|---|---|")
        hits: list[int] = []
        for value in values:
            n = 0
            for wallet in wallets:
                swaps, trips = wallet_inputs(wallet)
                if key == "throughput_max_swaps_per_day":
                    fired = throughput(wallet, swaps, max_swaps_per_day=value)
                elif key == "router_only_min_aggregator_fraction":
                    fired = router_only(wallet, swaps, min_fraction=value)
                else:
                    fired = sandwich(wallet, trips, max_seconds=value)
                n += bool(fired)
            hits.append(n)
            print(f"| {value} | {n} |")
            sweep_rows.append({"rule": key, "value": value, "fired": n})
        spread = max(hits) - min(hits)
        print(f"\n-> spread {spread} wallets: "
              f"{'LOAD-BEARING' if spread >= 10 else 'inert on this sample'}\n")

    print("**genesis_burst (N, Y)**\n")
    print("| N | Y | wallets it fires on |")
    print("|---|---|---|")
    gb_hits: list[int] = []
    for n_anchors in (2, 3):
        for y in (30, 60, 120):
            n = 0
            for wallet, mint in wallet_anchor.items():
                entries = anchor_entries(obs[wallet], mint)
                n += bool(genesis_burst(wallet, anchors, entries=entries,
                                        min_anchors=n_anchors, max_seconds=y))
            gb_hits.append(n)
            print(f"| {n_anchors} | {y}s | {n} |")
            sweep_rows.append({"rule": "genesis_burst", "value": f"N={n_anchors},Y={y}",
                               "fired": n})
    print("\n-> structurally inert here: no wallet is in two anchors' first-20 sets, so "
          "N=2 is unreachable and every cell is 0. This is a property of the sample, "
          "not evidence about the rule.\n")

    # --- artifact -------------------------------------------------------
    artifact = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": dict(DEFAULT_SCREEN_CONFIG),
        "sample": {"wallets": len(wallets), "anchors": len(anchors)},
        "outcomes": by_verdict,
        "rejection_rate": counts["rejected"] / len(wallets),
        "per_rule_isolated": {k: sorted(v) for k, v in isolated.items()},
        "genesis_computable": burst_computable,
        "multi_rule_wallets": multi,
        "thin_swaps": thin,
        "sweeps": sweep_rows,
        # Only the measured values a reader needs to re-check the verdict. The full
        # ``measured`` dicts carry per-wallet mint LISTS (hundreds of addresses for a
        # sandwich rejection), which is megabytes of evidence for a question the
        # counts already answer.
        "wallets": {
            w: {
                "anchor_mint": wallet_anchor[w],
                "verdict": verdicts[w].verdict.value,
                "rule_id": verdicts[w].rule_id.value if verdicts[w].rule_id else None,
                "measured": {
                    k: verdicts[w].measured.get(k)
                    for k in ("anchors_observed", "swaps_observed", "positions_observed",
                              "quick_round_trips", "swaps_per_day", "aggregator_fraction",
                              "direct_pool_swaps", "min_swaps_to_screen")
                    if k in verdicts[w].measured
                },
                "profile": obs[w]["profile"],
            }
            for w in wallets
        },
    }
    out_path = ROOT / "spikes" / "filter_calibration.json"
    out_path.write_text(json.dumps(artifact, indent=2))
    print(f"artifact: {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
