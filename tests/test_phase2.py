"""Phase 2 checks — decoder + derivation against REAL mainnet fixtures.

Every fixture under tests/fixtures/ was fetched live from Helius on
2026-10-17 (slots ~457.9M–458.0M) and stores the full getTransaction result.
These tests assert the doc 07 Phase 2 derivation contract:

  * swap instructions are located by program id + discriminator, not by
    log-message scraping;
  * executed amounts come from balance deltas, not instruction bounds;
  * pool spot comes only from pool-OWNED vaults, never largest-balance
    guessing (doc 10 §3);
  * fee components are split, not lumped;
  * failed transactions produce no swaps;
  * persisted rows are idempotent under the schema's NULLS NOT DISTINCT key.

Run: python tests/test_phase2.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.decode import (  # noqa: E402
    PROGRAMS, UNVERIFIED_PROGRAMS, WSOL_MINT, account_keys, anchor_disc,
    derive_swaps, exec_price_quote, fee_components, find_swap_instructions,
    owner_deltas,
)
from solscope.derive import Deriver, GateReport, GateRow, format_gate  # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
    else:
        _failures.append(label)
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def main() -> int:
    print("\n[fixtures present]")
    directs = sorted(FIX.glob("tx_direct_*.json"))
    checks_all = sorted(FIX.glob("tx_*.json"))
    check("direct swap fixtures fetched live", len(directs) >= 6, f"got {len(directs)}")
    check("aggregator/legacy fixtures present", len(checks_all) >= 9, f"got {len(checks_all)}")

    print("\n[discriminators — anchor scheme]")
    check("sha256('global:buy')[:8] == 66063d1201daebea",
          anchor_disc("buy").hex() == "66063d1201daebea")
    check("sha256('global:sell')[:8] == 33e685a4017f83ad",
          anchor_disc("sell").hex() == "33e685a4017f83ad")
    check("launchlab buy_exact_in matches verified bytes",
          anchor_disc("buy_exact_in").hex() == "faea0d7bd59c13ec")
    check("cpmm swap_base_input matches verified bytes",
          anchor_disc("swap_base_input").hex() == "8fbe5adac41e33de")
    # e445a52e51cb9a1d is observed live on the PumpSwap program but is NOT one
    # of its buy/sell names (no plausible Anchor name hashes to it — checked
    # 2026-10-25), so it must stay unregistered. The design rule is that an
    # unknown discriminator is skipped rather than guessed: a decoder that
    # assumes "the only other instruction is probably a buy" invents trades.
    check("unresolvable live discriminator stays unregistered (fail closed)",
          not any(bytes.fromhex("e445a52e51cb9a1d") in spec.trades
                  for spec in PROGRAMS.values()))

    print("\n[registry sanity]")
    check("pumpswap user index is 1", PROGRAMS["pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"].trader_index == 1)
    check("meteora explicitly unverified", any("meteora" in v for v in UNVERIFIED_PROGRAMS.values()))

    print("\n[direct pumpswap fixtures — decode]")
    buy = load("tx_direct_buy_3zS41H.json")
    swaps, skips = derive_swaps(buy)
    check("buy fixture decodes exactly 1 swap", len(swaps) == 1, f"{len(swaps)}: {skips[:1]}")
    s = swaps[0]
    check("side is buy", s.side == "buy")
    check("dex is pumpswap", s.instruction.dex == "pumpswap")
    check("pool attributed from accounts[0] (base58, 32-byte key)",
          len(s.instruction.pool_address) == 44
          and s.instruction.pool_address != s.trader)
    check("trader is the fee payer", s.trader == account_keys(buy)[0])
    check("base amount from deltas > 0", s.base_amount_raw > 0)
    check("quote amount from deltas > 0", s.quote_amount_raw > 0)
    p = exec_price_quote(s)
    check("exec price positive and sane (1e-10..1e-2 SOL/token)", 1e-10 < p < 1e-2, f"{p:.3e}")
    # instruction bound must NOT be the executed amount: buy bound was
    # max_sol_cost=239798007 while actual paid differs (slippage/fees).
    check("executed != instruction bound (bounds are not prices)",
          s.quote_amount_raw != 239798007, f"quote={s.quote_amount_raw}")

    sell = load("tx_direct_sell_4TwoyZ.json")
    swaps2, skips2 = derive_swaps(sell)
    check("sell fixture decodes exactly 1 swap", len(swaps2) == 1, f"{len(swaps2)}: {skips2[:1]}")
    if swaps2:
        s2 = swaps2[0]
        check("side is sell", s2.side == "sell")
        check("different pool than the buy fixture",
              s2.instruction.pool_address != s.instruction.pool_address)
        p2 = exec_price_quote(s2)
        check("sell exec price positive and sane", 1e-10 < p2 < 1e-2, f"{p2:.3e}")

    print("\n[failed transaction]")
    failed = {
        "slot": 1, "blockTime": 1792200000,
        "transaction": {"signatures": ["deadbeef"],
                        "message": {"accountKeys": ["A"], "instructions": []}},
        "meta": {"err": {"InstructionError": [0, "Custom"]}, "fee": 5000,
                 "preTokenBalances": [], "postTokenBalances": [],
                 "preBalances": [], "postBalances": []},
    }
    fs, fskips = derive_swaps(failed)
    check("failed tx yields no swaps", fs == [])
    check("failed tx yields an explanatory skip", any("failed" in k for k in fskips))

    print("\n[owner-delta attribution]")
    deltas = owner_deltas(buy)
    # trader's base delta must exist and be nonzero; netting across ALL
    # accounts for the mint must NOT be how we measure (doc 10 §4.3).
    trader = account_keys(buy)[0]
    base_mint = s.base_mint
    check("trader base delta present", (trader, base_mint) in deltas)
    net = sum(v for (o, m), v in deltas.items() if m == base_mint)
    mine = deltas[(trader, base_mint)]
    check("trader leg != whole-tx net (netting cancels the trade)",
          mine != net or net == 0, f"leg={mine} net={net}")

    print("\n[native quote settlement — transient wSOL nets to zero]")
    # Mirrors the live 2026-10-25 PumpSwap pattern: the buyer's wSOL account is
    # created and closed in the same transaction, so it appears only in
    # postTokenBalances with amount 0 and never in preTokenBalances. A
    # token-delta-only decoder reports "no quote delta" for a swap that
    # plainly happened; the executed quote is the lamport delta minus fee.
    def b58encode(raw: bytes) -> str:
        n = int.from_bytes(raw, "big")
        alpha = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        out = ""
        while n:
            n, r = divmod(n, 58)
            out = alpha[r] + out
        pad = len(raw) - len(raw.lstrip(b"\x00"))
        return "1" * pad + (out or "1")

    trader = "TRADERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    pool = "POOLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    base_live = "BASE" + "x" * 40
    keys = [trader, pool, base_live, WSOL_MINT, "VAULT" + "x" * 39]
    # PumpSwap layout is accounts[0]=pool, [1]=user, [3]=base mint, [4]=quote
    # mint; program id lives in the key list like any other account.
    prog_ix = len(keys)
    keys = keys + ["pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"]
    synthetic = {
        "slot": 1, "blockTime": 1792965802,
        "transaction": {"signatures": ["nat1"], "message": {
            "accountKeys": keys,
            "instructions": [{
                "programIdIndex": prog_ix,
                "accounts": [1, 0, 4, 2, 3],          # pool, user, fee, base, quote
                "data": b58encode(anchor_disc("buy_exact_quote_in") + b"\x00" * 16),
            }]}},
        "meta": {
            "err": None, "fee": 5000,
            # trader's base account: 0 -> 100,000 (the bought tokens)
            "preTokenBalances": [
                {"accountIndex": 3, "mint": base_live, "owner": trader,
                 "uiTokenAmount": {"amount": "0", "decimals": 6}},
                # pool base vault drains by exactly the trader's gain
                {"accountIndex": 4, "mint": base_live, "owner": pool,
                 "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
            ],
            "postTokenBalances": [
                {"accountIndex": 3, "mint": base_live, "owner": trader,
                 "uiTokenAmount": {"amount": "100000", "decimals": 6}},
                {"accountIndex": 4, "mint": base_live, "owner": pool,
                 "uiTokenAmount": {"amount": "900000", "decimals": 6}},
                # the transient wSOL account: closed, so post = 0 and no pre
                {"accountIndex": 5, "mint": WSOL_MINT, "owner": trader,
                 "uiTokenAmount": {"amount": "0", "decimals": 9}},
            ],
            "preBalances": [10_000_000, 0, 0, 0, 0, 0],
            "postBalances": [8_500_000, 0, 0, 0, 0, 0],
            "innerInstructions": [],
        },
    }
    syn_swaps, syn_skips = derive_swaps(
        synthetic, signature="nat1",
        known_pools={pool: (base_live, WSOL_MINT)})
    check("native-settled swap decodes at all", len(syn_swaps) == 1, str(syn_skips[:1]))
    if syn_swaps:
        ns = syn_swaps[0]
        check("quote settlement marked native", ns.quote_settlement == "native",
              ns.quote_settlement)
        check("quote = lamport delta minus fee (1,500,000 - 5,000)",
              ns.quote_amount_raw == 1_495_000, str(ns.quote_amount_raw))
        check("base amount from pool-side delta", ns.base_amount_raw == 100_000)

    print("\n[fees]")
    f = fee_components(buy)
    check("fee_lamports from meta", f.fee_lamports == buy["meta"]["fee"])
    check("priority fee extracted from ComputeBudget",
          f.priority_fee_lamports is not None and f.priority_fee_lamports >= 0,
          str(f.priority_fee_lamports))
    check("compute units extracted", f.compute_units is not None and f.compute_units > 0,
          str(f.compute_units))
    check("priority fee <= total fee", (f.priority_fee_lamports or 0) <= f.fee_lamports)

    print("\n[pool spot — ownership, not largest-balance]")
    pool = s.instruction.pool_address
    spot, liq = Deriver.pool_spot_from_tx(buy, pool, s.base_mint, s.quote_mint)
    if spot is not None:
        check("same-tx spot price positive", spot > 0)
        spot2, _ = Deriver.pool_spot_from_tx(buy, pool + "x", s.base_mint, s.quote_mint)
        check("wrong pool address yields None (never guessed)", spot2 is None)
        # Measured 2026-10-25: this observed-only ratio sits ~27% BELOW the
        # executed price on a buy, which a true CPMM cannot do. The cause is
        # that meta lists only accounts the transaction touched, so a vault it
        # never touched is invisible. Asserting the defect rather than a
        # comfortable bound — this is exactly why pricing uses vault_reserves
        # (enumerate by owner) and why _gate_row never calls this.
        check("observed-only ratio is NOT a usable price (documents the defect)",
              abs(p - spot) / spot > 0.20, f"exec={p:.3e} spot={spot:.3e}")
        check("gate row ignores the observed-only ratio",
              Deriver(rpc=None, conn=None, ledger=None, budget=None,
                      log=lambda *a: None)._gate_row(
                  pool, s.base_mint, s.quote_mint,
                  snapshot={s.base_mint: {"quote_mint": s.quote_mint,
                                          "base_reserve": 1.0,
                                          "quote_reserve": 1e-7,
                                          "liquidity_usd": 1000.0,
                                          "price_usd": 1e-7,
                                          "price_native": 1e-7}},
                  reserves={s.base_mint: (1_000_000, 6),
                            s.quote_mint: (1_000_000_000, 9)}).derived_pool_price_quote
              is None)
    else:
        # acceptable only if the pool owns no vaults in this payload
        owners = {t.get("owner") for t in buy["meta"]["postTokenBalances"]}
        check("spot None only when pool owns no vault", pool not in owners)

    print("\n[aggregator-routed fixture — abstain rather than guess]")
    agg = load("tx_buy_0.json")
    agg_swaps, agg_skips = derive_swaps(agg)
    # Jupiter-routed: either decoded with attribution or explicitly skipped —
    # never decoded with a wrong trader.
    wrong = [sw for sw in agg_swaps if sw.trader not in {account_keys(agg)[0]}]
    check("routed swap either skipped or trader==fee payer",
          (not agg_swaps) or not wrong, f"{len(agg_swaps)} decoded, {len(wrong)} wrong")

    print("\n[gate report arithmetic]")
    rows = [
        GateRow("pA", "m", "q", 3, 1.00e-7, 1.00e-7, 0.5, 0.99e-7, 12000.0, 12000.0, 0.9, -30.0, True),
        GateRow("pB", "m", "q", 5, 2.00e-7, 2.02e-7, -1.0, 1.98e-7, 8000.0, 8100.0, -1.2, 40.0, True),
        GateRow("pC", "m", "q", 2, 3.00e-7, 3.20e-7, -6.25, 3.1e-7, 5000.0, 5200.0, -3.8, 900.0, False),
        GateRow("pD", "m", "q", 1, None, None, None, None, None, None, None, None, None, note="no snap"),
    ]
    rep = GateReport(rows=rows, tolerance_pct=5.0)
    check("priced rows counted", len(rep.priced_rows) == 3)
    check("mean deviation", abs(rep.mean_deviation - (0.5 + 1.0 + 6.25) / 3) < 1e-9,
          f"{rep.mean_deviation}")
    check("max deviation 6.25", abs(rep.max_deviation - 6.25) < 1e-9)
    check("within-tolerance count 2/3", rep.passed == 2)
    check("liquidity pass count 3/3", rep.liquidity_passed == 3)
    check("mean liquidity deviation",
          abs(rep.mean_liquidity_deviation - (0.9 + 1.2 + 3.8) / 3) < 1e-9,
          f"{rep.mean_liquidity_deviation}")
    # A mean inside tolerance must NOT pass while one pool is far outside it —
    # otherwise a few badly wrong pools hide behind a good average.
    ok, why = rep.verdict(min_pairs=3)
    check("one outlier in three fails the pass-rate rule", not ok, why)
    check("verdict names the outside-tolerance count", "1/3" in why, why)
    # 26/27 must pass: a single volatile pool that traded between our reserve
    # read and the provider snapshot is lag, not a derivation error.
    mostly = GateReport(rows=[rows[0]] * 26 + [rows[2]], tolerance_pct=5.0)
    ok_m, why_m = mostly.verdict(min_pairs=27)
    check("one outlier in 27 passes (96% > 80% rule)", ok_m, why_m)
    thin = GateReport(rows=[rows[0]] * 5 + [rows[2]] * 5, tolerance_pct=5.0)
    ok_t, _ = thin.verdict(min_pairs=10)
    check("50% outside tolerance fails", not ok_t)
    # A pool that traded between the two reserve reads cannot be compared
    # like-for-like; it must be excluded AND surfaced, never silently dropped.
    moved = GateReport(rows=[rows[0]] * 4 +
                       [GateRow("pM", "m", "q", 1, 1e-7, 1.2e-7, -16.0,
                                None, None, None, None, None, None,
                                pool_moved=True)],
                       tolerance_pct=5.0)
    ok_mv, why_mv = moved.verdict(min_pairs=4)
    check("moved pool is excluded from stats", ok_mv and len(moved.priced_rows) == 4, why_mv)
    check("moved pool is still reported", len(moved.moved_rows) == 1)
    check("exclusion is stated in the verdict", "excluded (traded mid-window)" in why_mv, why_mv)
    # Excluding pools must not be a way to reach the pair minimum.
    lonely = GateReport(rows=[rows[0]] * 3 +
                        [GateRow("pM", "m", "q", 1, 1e-7, 1.2e-7, -16.0,
                                 None, None, None, None, None, None,
                                 pool_moved=True)],
                        tolerance_pct=5.0)
    ok_l, why_l = lonely.verdict(min_pairs=4)
    check("excluded pools do not count toward the minimum", not ok_l, why_l)
    rep2 = GateReport(rows=rows[:2], tolerance_pct=5.0)
    ok2, _ = rep2.verdict(min_pairs=2)
    check("gate passes on clean rows", ok2)
    rep3 = GateReport(rows=rows[:2], tolerance_pct=5.0)
    ok3, why3 = rep3.verdict(min_pairs=20)
    check("insufficient pairs never pass", not ok3 and "only 2/20" in why3, why3)
    # Liquidity is half the doc 07 criterion: unmeasured liquidity must not pass.
    noliq = GateReport(rows=[GateRow("pE", "m", "q", 1, 1e-7, 1e-7, 0.1,
                                     None, None, None, None, None, None)],
                       tolerance_pct=5.0)
    ok4, why4 = noliq.verdict(min_pairs=1)
    check("unmeasured liquidity fails the gate", not ok4 and "NOT MEASURED" in why4, why4)
    text = format_gate(rep)
    check("gate table renders verdict", "GATE" in text and ("PASS" in text or "FAIL" in text))

    print("\n[snapshot parsing]")
    sample = json.dumps([
        {"pairAddress": "P1", "baseToken": {"address": "MINT_A"},
         "quoteToken": {"address": WSOL_MINT}, "priceNative": "0.0000002",
         "priceUsd": "0.000034",
         "liquidity": {"usd": 15000.0, "base": 1.0e8, "quote": 200.0},
         "fdv": 340000, "marketCap": 340000},
        {"pairAddress": "P2", "baseToken": {"address": "MINT_A"},
         "quoteToken": {"address": WSOL_MINT}, "priceNative": "0.00000021",
         "priceUsd": "0.000036", "liquidity": {"usd": 9000.0}},
    ]).encode()

    class _Resp:
        """urlopen-compatible fake: fetch_bytes uses a context manager."""
        status = 200
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *exc): return False
    def opener(req, timeout=0): return _Resp(sample)
    snap = Deriver.fetch_snapshot(["MINT_A"], opener=opener)
    check("snapshot keyed by pair address", set(snap) == {"P1", "P2"})
    check("price_native parsed as float", abs(snap["P1"]["price_native"] - 2e-7) < 1e-12)
    check("provider reserves parsed (base/quote)",
          snap["P1"]["base_reserve"] == 1.0e8
          and snap["P1"]["quote_reserve"] == 200.0)
    row_exact, is_exact = Deriver.pick_for_pool(snap, "P1", "MINT_A")
    check("exact pool match is preferred", is_exact and row_exact["liquidity_usd"] == 15000.0)
    row_fb, is_exact2 = Deriver.pick_for_pool(snap, "OTHER", "MINT_A")
    check("other pool falls back to deepest but is flagged not-exact",
          (not is_exact2) and row_fb["liquidity_usd"] == 15000.0)
    check("unknown mint yields no row", Deriver.pick_for_pool(snap, "X", "NOPE") == ({}, False))

    print("\n[reserve-implied price is the gate reference, not priceNative]")
    # Reserves 200 SOL / 1e8 tokens => 2.0e-6, while priceNative says 2e-7.
    # The gate must use the reserve ratio: both sides then measure the same
    # quantity (reserves at one instant) instead of comparing a mid price
    # against a trade-derived price.
    d = Deriver(rpc=None, conn=None, ledger=None, budget=None, log=lambda *a: None)
    chain = Deriver.price_from_reserves(
        {WSOL_MINT: (200 * 10**9, 9), "MINT_A": (100_000_000 * 10**6, 6)},
        "MINT_A", WSOL_MINT)
    check("price_from_reserves = quote/base", abs(chain - 2e-6) < 1e-12, str(chain))
    row = d._gate_row("P1", "MINT_A", WSOL_MINT, snapshot=snap,
                      reserves={WSOL_MINT: (200 * 10**9, 9),
                                "MINT_A": (100_000_000 * 10**6, 6)})
    check("gate row uses reserve ratio as both sides",
          row.deviation_pct is not None and abs(row.deviation_pct) < 1e-9,
          str(row.deviation_pct))
    check("priceNative kept for context only",
          abs(row.dex_price_native - 2e-7) < 1e-12)
    check("reserve deviations computed",
          abs(row.base_reserve_dev_pct) < 1e-9 and abs(row.quote_reserve_dev_pct) < 1e-9,
          f"{row.base_reserve_dev_pct}/{row.quote_reserve_dev_pct}")
    # 10% off on the base side must show up on both the price and reserve lines
    row2 = d._gate_row("P1", "MINT_A", WSOL_MINT, snapshot=snap,
                       reserves={WSOL_MINT: (200 * 10**9, 9),
                                 "MINT_A": (110_000_000 * 10**6, 6)})
    check("a snapshot row for a different pool is refused, not averaged in",
          d._gate_row("NOPE", "MINT_A", WSOL_MINT, snapshot=snap,
                      reserves={WSOL_MINT: (200 * 10**9, 9),
                                "MINT_A": (100_000_000 * 10**6, 6)}).deviation_pct
          is None)
    check("a 10% base-reserve error is visible",
          row2.base_reserve_dev_pct is not None
          and abs(row2.base_reserve_dev_pct - 10.0) < 1e-6
          and row2.deviation_pct < -5.0,
          f"base={row2.base_reserve_dev_pct} price={row2.deviation_pct}")

    print("\n[persist writes a decoded swap]", )
    class _FakeConn:
        """Records SQL instead of connecting, so persist() is exercised without a DB."""
        def __init__(self): self.calls = []
        def execute(self, sql, params=None): self.calls.append((sql, params))
        def commit(self): self.calls.append(("COMMIT", None))
        def rollback(self): self.calls.append(("ROLLBACK", None))
    conn = _FakeConn()
    deriver = Deriver(rpc=None, conn=conn, ledger=None, budget=None,
                      log=lambda *a: None)
    ok = deriver.persist(s)
    swap_calls = [(q, p) for q, p in conn.calls if "INSERT INTO swaps" in q]
    check("persist succeeds on a decoded swap", ok)
    check("a swaps row is written", len(swap_calls) == 1, str(len(swap_calls)))
    if swap_calls:
        params = swap_calls[0][1]
        check("exec price is computed, not passed through",
              params[14] not in (None, "")
              and abs(float(params[14]) - exec_price_quote(s)) < 1e-18,
              str(params[14]))
        check("price provenance recorded", "swap_derived" in swap_calls[0][0])
    check("committed", conn.calls[-1][0] == "COMMIT")

    print("\n[derive CLI wiring]")
    from solscope.cli import build_parser
    args = build_parser().parse_args(["derive", "--gate", "--pairs", "20",
                                      "--tolerance", "10"])
    check("derive parses with gate flags", args.command == "derive" and args.gate
          and args.pairs == 20 and args.tolerance == 10.0)
    args2 = build_parser().parse_args(["score"])
    check("score is still a stub", args2.command == "score")

    print()
    if _failures:
        print(f"{len(_failures)} CHECK(S) FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL PHASE 2 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
