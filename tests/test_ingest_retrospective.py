"""Mocked tests for the retrospective mint-scoped ingest.

No network, no database: the RPC, the decoder and ``rebuild`` are all injected
or monkeypatched, so these cases pin the walk's control flow (paging, filtering,
failure isolation, credit accounting, dry-run discipline) rather than Helius.
"""

import datetime as _dt
from types import SimpleNamespace

import solscope.ingest_retrospective as ir

GENESIS = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
MINT = "Mint1111111111111111111111111111111111111111"


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, *, conflict=False, existing_id=77):
        self.sql = []
        self.params = []
        self.conflict = conflict
        self.existing_id = existing_id
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.params.append(params)
        if "RETURNING id" in sql:
            return _Cursor(None if self.conflict else (self.existing_id,))
        if sql.startswith("SELECT id FROM swaps"):
            return _Cursor((self.existing_id,))
        return _Cursor(None)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def find(self, needle):
        return [i for i, s in enumerate(self.sql) if needle in s]


class FakeRpc:
    def __init__(self, pages, tx_by_sig):
        self.pages = pages
        self.tx_by_sig = tx_by_sig
        self.signature_calls = []
        self.tx_calls = []

    def signatures_for_address(self, address, *, limit=1000, before=None, until=None):
        self.signature_calls.append(before)
        index = 0 if before is None else \
            next(i + 1 for i, p in enumerate(self.pages)
                 if p and p[-1]["signature"] == before)
        return {"result": self.pages[index] if index < len(self.pages) else []}, b""

    def transaction(self, signature):
        self.tx_calls.append(signature)
        return self.tx_by_sig[signature], b""


def swap(sig, wallet, mint=None, side="buy"):
    return SimpleNamespace(
        signature=sig, slot=10, block_time=int(GENESIS.timestamp()) + 60,
        trader=wallet, base_mint=mint or MINT, quote_mint="So11111111111111111111111111111111111111112",
        side=side, base_amount_raw=1_000, quote_amount_raw=10_000, base_decimals=6,
        quote_decimals=9,
        quote_settlement="token",
        instruction=SimpleNamespace(ix_index=0, inner_index=None, pool_address=None,
                                    quote_is_native=False),
        fees=SimpleNamespace(fee_lamports=5_000, priority_fee_lamports=0, compute_units=900),
    )


def page(*sigs, offset=0):
    return [{"signature": s, "slot": 10 + offset,
             "blockTime": int(GENESIS.timestamp()) + 60 + offset} for s in sigs]


def run(rpc, conn, *, dry_run, buyers=frozenset(GENESIS.__class__ and {"W1"})):
    return ir.fetch_anchor_history(
        conn, anchor_mint=MINT, genesis_time=GENESIS, window_days=30,
        buyer_wallets=set(buyers), config={"rpc": rpc}, dry_run=dry_run,
    )


def main() -> int:
    checks = []

    def check(name, ok, detail=""):
        checks.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if not ok else ""))

    original_derive, original_rebuild = ir.derive_swaps, ir.rebuild
    rebuilt = []

    def fake_rebuild(conn, *, wallet=None, mint=None, **_kw):
        rebuilt.append((wallet, mint))

    try:
        ir.rebuild = fake_rebuild

        # 1 + 5: three pages, cursor walked backwards, credits counted per call.
        pages = [page("a1", "a2"), page("b1", "b2"), page("c1", "c2")]
        txs = {f"p{i}": {"mint": MINT} for i in range(6)}
        sigs = ["a1", "a2", "b1", "b2", "c1", "c2"]
        txs = {s: {"mint": MINT} for s in sigs}
        rpc = FakeRpc(pages, txs)
        ir.derive_swaps = lambda tx, signature=None: [swap(signature, "W1")]
        conn = FakeConn()
        report = run(rpc, conn, dry_run=True)
        check("pagination: all six signatures fetched", report.signatures_fetched == 6,
              report.signatures_fetched)
        check("pagination: second page carries page 1's oldest signature as before",
              rpc.signature_calls[1] == "a2", rpc.signature_calls)
        # Three full pages, then one empty page that ends the walk: 4 signature
        # calls + 6 transaction calls. The empty page is a real, billable call.
        check("credit accounting: 3 pages + terminating page + 6 transactions",
              report.credits_used == 10, report.credits_used)

        # 2: all decoded swaps written; only buyer wallets get rebuilt.
        rebuilt.clear()
        conn = FakeConn()
        report = run(FakeRpc(pages, txs), conn, dry_run=False, buyers={"W1"})
        check("buyer filter: swaps written for the anchor's whole history",
              report.swaps_written == 6, report.swaps_written)
        check("buyer filter: rebuild only for requested wallets",
              rebuilt == [("W1", MINT)], rebuilt)

        # 3: a transaction with no swap in the anchor mint is skipped, not decoded.
        foreign = dict(txs)
        foreign["a1"] = {"mint": "OtherMint"}
        ir.derive_swaps = lambda tx, signature=None: (
            [swap(signature, "W1", mint="OtherMint11111111111111111111111111111111")]
            if tx["mint"] != MINT else [swap(signature, "W1")])
        report = run(FakeRpc(pages, foreign), FakeConn(), dry_run=True)
        check("foreign mint: counted as skipped",
              report.transactions_skipped == 1, report.transactions_skipped)

        # 4: one bad transaction is recorded and the walk continues.
        def flaky(tx, signature=None):
            if signature == "b1":
                raise ValueError("bad decode")
            return [swap(signature, "W1")]

        ir.derive_swaps = flaky
        report = run(FakeRpc(pages, txs), FakeConn(), dry_run=True)
        check("bad decode: failure recorded, run continues",
              len(report.failures) == 1 and report.failures[0]["signature"] == "b1"
              and report.transactions_decoded == 5, report.failures)

        # 6: dry run writes nothing at all.
        rebuilt.clear()
        ir.derive_swaps = lambda tx, signature=None: [swap(signature, "W1")]
        conn = FakeConn()
        report = run(FakeRpc(pages, txs), conn, dry_run=True)
        check("dry_run: entry_legs for every decoded leg",
              len(report.entry_legs) == 6 and report.entry_legs[0]["wallet"] == "W1",
              len(report.entry_legs))
        check("dry_run: no swaps and no positions written",
              not conn.find("INSERT INTO swaps") and not rebuilt
              and report.swaps_written == 0, conn.sql[-1:])

        # 7: conflict on insert falls back to SELECT, and the id survives.
        conn = FakeConn(conflict=True, existing_id=4242)
        report = run(FakeRpc(pages, txs), conn, dry_run=False)
        selects = conn.find("SELECT id FROM swaps")
        check("RETURNING fallback: SELECT issued on conflict", bool(selects), conn.sql)
        check("RETURNING fallback: existing id resolved",
              any("IS NOT DISTINCT FROM" in conn.sql[i] for i in selects)
              and report.swaps_written == 6, report.swaps_written)

        # 8: the leg carries both keys the two consumers need.
        leg = ir._leg(swap("sig-1", "W1", side="sell"), 9)
        check("SwapLeg: swap_id (lot_matches) and signature (entry_swap_signature) both set",
              leg.swap_id == 9 and leg.signature == "sig-1", (leg.swap_id, leg.signature))
    finally:
        ir.derive_swaps, ir.rebuild = original_derive, original_rebuild

    failed = checks.count(False)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
