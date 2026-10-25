#!/usr/bin/env python3
"""Phase 2 de-risk spike: derive a token's execution price from raw chain data.

Tests the project's load-bearing assumption (docs 02, 05): that a decoded swap
carries enough information to compute an exact point-in-time price with no
look-ahead, instead of depending on candle/snapshot APIs. Uses only stdlib +
public Solana RPC + DexScreener's keyless API as the independent reference.

Method (the one that actually reconciles — see docs/10 for why the obvious
alternatives fail):
  1. Resolve the token's DEEPEST pool via DexScreener. Sampling a shared
     mega-pool (e.g. SOL/USDC) is wrong: its feed is dominated by
     aggregator-routed transactions that merely reference it while swapping
     elsewhere, so derived prices reflect unrelated trades.
  2. Pull recent transactions on that pool. Request
     maxSupportedTransactionVersion=1 — these swaps are versioned txs, and
     asking for 0 returns -32015 instead of data.
  3. For each swap, take the DOMINANT-|delta| leg per mint. Netting each mint
     across all accounts is wrong: the user's account (-X) and the pool vault
     (+X) cancel to ~0 and the trade vanishes.
  4. execution_price = |quote_leg| / |base_leg|, exact for that trade.
  5. Compare to DexScreener's priceNative (SOL-denominated, so no separate
     SOL/USD conversion is introduced as a confound).

Interpretation of the ~2% residual: it is NOT derivation error. It is the
expected gap between a trade's execution price (which includes that trade's own
amortised price impact) and the pool's spot price at snapshot time. Larger
trades deviate more, which is the signature of a real AMM curve.

Usage:
  python3 spikes/phase2_spike.py --token <mint>            # auto-resolve deepest pool
  python3 spikes/phase2_spike.py --pool <pairAddress> -n 25 --tolerance-pct 5.0
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

RPC_URL = "https://api.mainnet-beta.solana.com"
DS_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
DS_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs/solana/{pool}"

# DexScreener 403s urllib's default "Python-urllib/x.y" agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) phase2-spike"}

WSOL_MINT = "So11111111111111111111111111111111111111112"

# uncdog / SOL on PumpSwap — a representative ~$20k-liquidity memecoin pool,
# deliberately not a best case.
DEFAULT_TOKEN = "EN3rLpbQa6ZUdH1SrS9oHkkWQUkQBj6VjnqZQhzrpump"


def http_json(url: str, data: bytes | None = None, retries: int = 5):
    last: Exception | None = None
    # The RPC endpoint requires an explicit JSON content type on POST; without
    # it, it answers 415 Unsupported Media Type rather than parsing the body.
    headers = dict(HEADERS)
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last = exc
            # 429 on public RPC is routine; back off and retry.
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


def rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    payload = http_json(RPC_URL, data=body)
    if "error" in payload:
        raise RuntimeError(f"rpc error: {payload['error']}")
    return payload["result"]


def deepest_pool(token: str) -> dict:
    """Resolve the token's deepest pool (highest reported liquidity)."""
    data = http_json(DS_TOKEN_URL.format(mint=token), retries=3)
    pairs = [p for p in (data.get("pairs") or []) if (p.get("liquidity") or {}).get("usd")]
    if not pairs:
        raise RuntimeError(f"no liquidity-bearing pair for token {token}")
    p = max(pairs, key=lambda x: x["liquidity"]["usd"])
    if not p.get("priceNative"):
        raise RuntimeError("deepest pair has no priceNative")
    return {
        "pair": p["pairAddress"],
        "dex": p["dexId"],
        "base": p["baseToken"]["symbol"],
        "quote": p["quoteToken"]["symbol"],
        "price_native": float(p["priceNative"]),
        "price_usd": float(p["priceUsd"]) if p.get("priceUsd") else None,
        "liquidity_usd": p["liquidity"]["usd"],
    }


def pair_snapshot(pair: str) -> float:
    """Current priceNative for one pair (SOL-denominated for SOL-quoted pools)."""
    data = http_json(DS_PAIR_URL.format(pool=pair), retries=3)
    pairs = data.get("pairs") or []
    if not pairs or not pairs[0].get("priceNative"):
        raise RuntimeError("pair snapshot missing priceNative")
    return float(pairs[0]["priceNative"])


def account_deltas(tx: dict) -> dict[tuple[int, str], float]:
    """Per-(accountIndex, mint) uiAmount change across the transaction."""
    meta = tx["meta"]
    out: dict[tuple[int, str], float] = {}
    for side, sign in ((meta.get("preTokenBalances") or [], -1),
                       (meta.get("postTokenBalances") or [], +1)):
        for entry in side:
            amt = entry["uiTokenAmount"].get("uiAmount") or 0.0
            key = (entry["accountIndex"], entry["mint"])
            out[key] = out.get(key, 0.0) + sign * amt
    return out


def dominant_legs(deltas: dict[tuple[int, str], float]) -> dict[str, float]:
    """Trade size per mint, taken from the single dominant account leg.

    Netting each mint across all accounts is WRONG for measuring trade size: a
    swap puts the user's token account (-X) and the pool vault (+X) in the same
    transaction, so the net is ~0 and the trade vanishes.
    """
    per_mint: dict[str, float] = {}
    for (_idx, mint), d in deltas.items():
        if abs(d) > abs(per_mint.get(mint, 0.0)):
            per_mint[mint] = d
    return {m: d for m, d in per_mint.items() if abs(d) > 1e-9}


def derive_swap(tx: dict, base_mint: str, quote_mint: str) -> dict | None:
    """If this tx is a clean two-sided swap, return its execution-price parts."""
    deltas = account_deltas(tx)
    legs = dominant_legs(deltas)
    base = legs.get(base_mint)
    quote = legs.get(quote_mint)
    if base is None or quote is None:
        return None
    if base * quote >= 0:
        return None  # must be opposite signs (pay one, receive the other)
    return {
        "base_amount": abs(base),
        "quote_amount": abs(quote),
        "execution_price": abs(quote) / abs(base),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=DEFAULT_TOKEN, help="token mint to resolve a pool for")
    ap.add_argument("--pool", default=None, help="pair address (overrides --token resolution)")
    ap.add_argument("-n", "--num", type=int, default=25, help="signatures to scan")
    ap.add_argument("--tolerance-pct", type=float, default=5.0,
                    help="max |execution - spot| / spot, allowing for price impact + time skew")
    ap.add_argument("--out", default="spikes/phase2_reconciliation.json")
    args = ap.parse_args()

    if args.pool:
        snap = None
        try:
            snap = deepest_pool(args.token)
        except RuntimeError:
            pass
        base_mint, quote_mint = WSOL_MINT, WSOL_MINT
        pair = args.pool
        base_sym = quote_sym = "?"
        # Without token resolution we cannot know the base mint; require --token.
        resolved = deepest_pool(args.token)
        base_mint, quote_mint = WSOL_MINT, args.token
        base_sym, quote_sym = resolved["base"], resolved["quote"]
        pair = args.pool
    else:
        resolved = deepest_pool(args.token)
        pair = resolved["pair"]
        base_sym, quote_sym = resolved["base"], resolved["quote"]
        # For SOL-quoted pools: base = token, quote = wSOL. priceNative is then
        # SOL per token, which is what execution_price will also be expressed in.
        base_mint, quote_mint = args.token, WSOL_MINT

    print(f"[pool] {pair}  {base_sym}/{quote_sym}  dex={resolved.get('dex')}  "
          f"liq=${resolved.get('liquidity_usd'):,.0f}")

    before = pair_snapshot(pair)
    sigs = [s["signature"] for s in rpc("getSignaturesForAddress", [pair, {"limit": args.num}])]
    print(f"[rpc] scanned {len(sigs)} signatures")

    swaps: list[dict] = []
    for i, sig in enumerate(sigs):
        try:
            tx = rpc("getTransaction",
                     [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                            "commitment": "confirmed"}])
        except RuntimeError as exc:
            print(f"  [{i:>2}] {sig[:12]}… rpc error: {exc}")
            continue
        if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
            continue
        found = derive_swap(tx, base_mint, quote_mint)
        if not found:
            continue
        found.update({"signature": sig, "blockTime": tx.get("blockTime")})
        swaps.append(found)

    if not swaps:
        print("FAIL: no clean two-sided swaps derived")
        return 2

    after = pair_snapshot(pair)
    ref = (before + after) / 2.0
    drift = abs(after - before) / ref * 100 if ref else 0.0
    print(f"[derive] {len(swaps)} clean swaps; reference={ref:.10f} "
          f"(before={before:.10f} after={after:.10f} drift={drift:.3f}%)")

    checked = failures = 0
    max_dev = 0.0
    for s in sorted(swaps, key=lambda x: x["blockTime"] or 0):
        dev = abs(s["execution_price"] - ref) / ref * 100
        s["deviation_pct"] = dev
        s["within_tolerance"] = dev <= args.tolerance_pct
        max_dev = max(max_dev, dev)
        checked += 1
        if not s["within_tolerance"]:
            failures += 1
        print(f"  {s['signature'][:12]}… {s['base_amount']:>14.2f} {base_sym} <- "
              f"{s['quote_amount']:.6f}  price={s['execution_price']:.10f}  dev={dev:.2f}%")

    result = {
        "pair": pair,
        "token": args.token,
        "base_symbol": base_sym,
        "quote_symbol": quote_sym,
        "reference": ref,
        "before": before,
        "after": after,
        "drift_pct": drift,
        "tolerance_pct": args.tolerance_pct,
        "swaps_derived": len(swaps),
        "compared": checked,
        "within_tolerance": checked - failures,
        "failed": failures,
        "max_deviation_pct": max_dev,
        "derived_rows": swaps,
    }
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)

    print()
    print(f"[result] {checked - failures}/{checked} within {args.tolerance_pct}% "
          f"(mean dev {sum(s['deviation_pct'] for s in swaps)/len(swaps):.2f}%, "
          f"max {max_dev:.2f}%)")
    if failures:
        print("FAIL: some swaps off tolerance")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
