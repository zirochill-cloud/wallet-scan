"""Triangulate DexScreener's priceNative against two independent sources.

Question (doc 11 §2.2): priceNative sits above the reserve ratio implied by
DexScreener's own reported reserves on tokens in a dump. Two readings 30s apart
tell us which quantity is stale:

  chain_reserve_ratio   we read the pool's vaults ourselves at instant T
  provider_reserve      DexScreener's liquidity.base/quote at instant T
  priceNative           DexScreener's price at instant T
  exec_price            what a recent trade actually paid (chain, at its own time)

If priceNative stays above BOTH reserve ratios at both instants while exec
prices track one of them, priceNative is a trade-derived price and the gate's
reserve-against-reserves comparison is the correct one.

Run:  set -a; source .env; set +a; python spikes/triangulate.py
Stdlib + solscope only. Costs ~2 RPC calls per pool per round plus one
DexScreener request per round.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.adapters.helius import HeliusRpc, api_key_from_env  # noqa: E402
from solscope.derive import Deriver, TOKEN_2022_PROGRAM, TOKEN_PROGRAM  # noqa: E402
from solscope.decode import derive_swaps, exec_price_quote  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
GAP_SECONDS = 30
POOLS = [
    ("DXp5zehrAhtNz7yMV16D5FFojagJBTBTzLu4ydKjsNto",
     "FcqKM8X2Fme8GLkPsyRpsbXZhLkRSbuaorrnheu5pump"),
    ("H2hyLzX9VnjhamfXUkd1c8Q9B8GMSxsdoEuuMSjTcVjf",
     "CsovxRtEM6g4jcVGtcTB3xwycoAuPjEafpUTR6zrpump"),
    ("9uLRSjzqhDsWgCn7tAnMVYEZ4t8uFVtGp75Me9HzriM8",
     "9rjHzQPpVL4XuYZ3S2bRk4CPYTubpAYpRvpY5pwzpump"),
]


def reserves(rpc: HeliusRpc, pool: str) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        result, _ = rpc.token_accounts_by_owner(pool, program_id=program)
        for acct in (result.get("result") or {}).get("value") or []:
            try:
                info = acct["account"]["data"]["parsed"]["info"]
                amount = info["tokenAmount"]
                raw, dec = int(amount["amount"]), int(amount.get("decimals") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            prev = out.get(info["mint"])
            out[info["mint"]] = (raw + (prev[0] if prev else 0), prev[1] if prev else dec)
    return out


def dex_pair(rpc_pool: str) -> dict:
    from solscope.adapters.httpfetch import fetch_bytes
    body, _ = fetch_bytes(f"https://api.dexscreener.com/latest/dex/pairs/solana/{rpc_pool}",
                          timeout=15, retries=3)
    payload = json.loads(body)
    rows = payload.get("pairs") or ([payload.get("pair")] if payload.get("pair") else [])
    return rows[0] if rows else {}


def executed_prices(rpc: HeliusRpc, pool: str, base: str, quote: str, limit: int = 10) -> list[float]:
    """Recent decoded swap prices for this pool (chain, at each trade's own time)."""
    result, _ = rpc.signatures_for_address(pool, limit=limit,
                                           request_key=f"tri:{pool}:{int(time.time()//600)}")
    out = []
    for item in (result.get("result") or []):
        if item.get("err"):
            continue
        tx, _ = rpc.transaction(item["signature"])
        payload = tx.get("result")
        if not payload:
            continue
        swaps, _ = derive_swaps(payload, signature=item["signature"],
                                 known_pools={pool: (base, quote)})
        for s in swaps:
            if s.instruction.pool_address == pool:
                p = exec_price_quote(s)
                if p == p and p > 0:
                    out.append(p)
        if out:
            break
    return out


def main() -> int:
    key = api_key_from_env()
    if not key:
        print("HELIUS_API_KEY not set"); return 1
    rpc = HeliusRpc(api_key=key, job="triangulate")
    deriver = Deriver(rpc=rpc, conn=None, ledger=None, budget=None, log=print)

    rounds = []
    for rnd in (1, 2):
        snap = deriver.fetch_snapshot([b for _p, b in POOLS])
        sample = {}
        for pool, base in POOLS:
            res = reserves(rpc, pool)
            chain = deriver.price_from_reserves(res, base, WSOL)
            row = snap.get(pool) or {}
            prov = None
            if row.get("quote_reserve") and row.get("base_reserve"):
                prov = row["quote_reserve"] / row["base_reserve"]
            sample[pool] = {
                "chain": chain,
                "provider_reserve": prov,
                "price_native": row.get("price_native"),
                "price_usd": row.get("price_usd"),
                "provider_liq_usd": row.get("liquidity_usd"),
            }
        rounds.append(sample)
        print(f"round {rnd} captured at t={time.strftime('%H:%M:%S')}")
        if rnd == 1:
            print(f"  sleeping {GAP_SECONDS}s ...")
            time.sleep(GAP_SECONDS)

    # third number: what a trade actually paid, chain-sourced
    execs = {}
    for pool, base in POOLS[:3]:
        try:
            e = executed_prices(rpc, pool, base, WSOL)
            execs[pool] = e
        except Exception as exc:
            execs[pool] = []
            print(f"  exec fetch failed for {pool[:10]}: {exc}")

    report = {"rounds": rounds, "exec": execs,
              "gap_seconds": GAP_SECONDS, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    pathlib.Path("artifacts").mkdir(exist_ok=True)
    pathlib.Path("artifacts/triangulation.json").write_text(json.dumps(report, indent=2))

    print("\npool           t  chain_ratio   provider_res  priceNative   exec(last)")
    print("-" * 88)
    for pool, base in POOLS:
        for i, r in enumerate(rounds, 1):
            s = r[pool]
            ex = execs.get(pool) or []
            exs = f"{ex[-1]:.3e}" if ex else "-"
            def f(v):
                return f"{v:.3e}" if v else "     -      "
            print(f"{pool[:12]}  t{i}  {f(s['chain'])}  {f(s['provider_reserve'])}  "
                  f"{f(s['price_native'])}  {exs}")

    print("\ncomparisons (ratio priceNative / X > 1 means priceNative is ABOVE X):")
    for pool, _base in POOLS:
        c1, p1, n1 = (rounds[0][pool][k] for k in ("chain", "provider_reserve", "price_native"))
        c2, p2, n2 = (rounds[1][pool][k] for k in ("chain", "provider_reserve", "price_native"))
        if not all((c1, p1, n1, c2, p2, n2)):
            print(f"  {pool[:12]}  incomplete"); continue
        ex = execs.get(pool) or []
        exr = f"{ex[-1]/n1:.3f}" if ex else None
        print(f"  {pool[:12]}  t1: native/chain={n1/c1:.3f} native/provider={n1/p1:.3f}"
              f"  t2: native/chain={n2/c2:.3f} native/provider={n2/p2:.3f}"
              f"  chain drift={abs(c2-c1)/c1*100:.2f}%  exec/native={exr}")
    print("\nwrote artifacts/triangulation.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
