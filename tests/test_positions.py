"""Phase 3 checks — FIFO position reconstruction (doc 07 Phase 3).

The Phase 3 exit criteria that can be proven without a block explorer are
proven here:

  * realized P&L and entry/exit quantities reconcile exactly for hand-computed
    histories (FIFO, partial exits, fees inside the cost basis);
  * **a wallet that received tokens by transfer or airdrop and dumped them is
    not shown as profitable** — the specific failure mode the schema exists to
    make impossible;
  * exit then re-entry produces two distinct episodes with separate entry facts;
  * anything that cannot be computed is None **with a reason**, never 0.

The explorer reconciliation in doc 07 needs three live wallets and is not this
test's job. The database section below needs PostgreSQL and skips cleanly
without it.

Run:  python tests/test_positions.py
"""
from __future__ import annotations

import datetime as _dt
import os
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.positions import (  # noqa: E402
    COMPLETE, PARTIAL, UNKNOWN, SwapLeg, TransferIn, format_book, reconstruct,
)

_failures: list[str] = []

T0 = _dt.datetime(2026, 10, 17, 4, 0, tzinfo=_dt.timezone.utc)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def leg(side: str, base: int, quote: int, *, slot: int = 1,
        minute: int = 0, swap_id: int = 1, fee: int = 0,
        quote_dec: int = 9, base_dec: int = 6,
        price_usd: float | None = None) -> SwapLeg:
    return SwapLeg(
        swap_id=swap_id, side=side, base_amount_raw=base, quote_amount_raw=quote,
        base_decimals=base_dec, quote_decimals=quote_dec, slot=slot,
        block_time=T0 + _dt.timedelta(minutes=minute), fee_lamports=fee,
        price_usd=price_usd,
    )


def main() -> int:
    print("\n[one buy, one sell]")
    book = reconstruct("W1", "M1", [
        leg("buy", 1_000_000, 100_000_000, slot=1, swap_id=1, fee=5_000),
        leg("sell", 1_000_000, 200_000_000, slot=2, minute=5, swap_id=2, fee=5_000),
    ])
    check("one episode", len(book.episodes) == 1, str(len(book.episodes)))
    ep = book.episodes[0]
    check("episode is closed", ep.is_closed)
    check("buy quantity recorded", ep.total_buy_qty_raw == 1_000_000)
    check("open quantity zero after full exit", ep.open_qty_raw == 0)
    # cost basis = quote paid + buy fee; sell fee is subtracted from P&L
    check("cost basis includes the buy fee",
          ep.cost_basis_lamports == 100_005_000, str(ep.cost_basis_lamports))
    check("realized P&L = proceeds - basis - sell fee",
          ep.realized_pnl_lamports == 200_000_000 - 100_005_000 - 5_000,
          str(ep.realized_pnl_lamports))
    check("realized P&L is not zero", ep.realized_pnl_lamports != 0)
    check("completeness is complete", ep.pnl_completeness == COMPLETE)
    check("provenance is swap_only", ep.inventory_origin == "swap_only")
    check("hold time measured", ep.hold_seconds == 300, str(ep.hold_seconds))
    check("one FIFO match", len(ep.lot_matches) == 1)
    # 99,990,000 realized on a 100,005,000 basis
    check("roi present", ep.roi is not None and abs(ep.roi - 0.99985) < 0.001, str(ep.roi))

    print("\n[FIFO ordering across lots]")
    book = reconstruct("W2", "M1", [
        leg("buy", 1_000, 1_000_000, slot=1, swap_id=11),
        leg("buy", 1_000, 3_000_000, slot=2, minute=1, swap_id=12),
        leg("sell", 1_500, 6_000_000, slot=3, minute=2, swap_id=13),
    ])
    ep = book.episodes[0]
    check("still open after partial exit", not ep.is_closed)
    check("remaining quantity 500", ep.open_qty_raw == 500, str(ep.open_qty_raw))
    check("two matches (oldest lot first)", len(ep.lot_matches) == 2, str(len(ep.lot_matches)))
    check("first match is the earlier buy",
          ep.lot_matches[0].buy_swap_id == 11, str(ep.lot_matches[0].buy_swap_id))
    check("first match takes the whole first lot",
          ep.lot_matches[0].matched_qty_raw == 1_000)
    check("second match takes 500 of the second lot",
          ep.lot_matches[1].matched_qty_raw == 500)
    check("cost allocated proportionally",
          ep.lot_matches[1].cost_lamports == 1_500_000,
          str(ep.lot_matches[1].cost_lamports))
    check("cost never exceeds what was paid",
          sum(m.cost_lamports for m in ep.lot_matches) <= ep.cost_basis_lamports)

    print("\n[exit and re-entry are separate episodes]")
    book = reconstruct("W3", "M1", [
        leg("buy", 1_000, 1_000_000, slot=1, swap_id=21, price_usd=0.01),
        leg("sell", 1_000, 2_000_000, slot=2, minute=1, swap_id=22),
        leg("buy", 2_000, 5_000_000, slot=3, minute=2, swap_id=23, price_usd=0.02),
    ])
    check("two episodes", len(book.episodes) == 2, str(len(book.episodes)))
    check("first is closed", book.episodes[0].is_closed)
    check("second is open", not book.episodes[1].is_closed)
    check("second episode has its own entry price",
          book.episodes[1].entry_price_usd == 0.02)
    check("open episode quantity is the re-entry size",
          book.episodes[1].open_qty_raw == 2_000)

    print("\n[transfer-in then dump is NOT profitable]")
    book = reconstruct("W4", "M1", [
        leg("sell", 1_000, 5_000_000, slot=5, swap_id=31),
    ], [TransferIn(amount_raw=1_000, kind="spl_transfer", slot=4, event_time=T0)])
    check("one episode recorded", len(book.episodes) == 1)
    ep = book.episodes[0]
    check("realized P&L is None, not a profit",
          ep.realized_pnl_lamports is None,
          str(ep.realized_pnl_lamports))
    check("no cost basis was invented", ep.cost_basis_lamports == 0)
    check("inventory origin says transfer",
          ep.inventory_origin == "includes_transfer_in", ep.inventory_origin)
    check("transfer quantity recorded", ep.transfer_in_qty_raw == 1_000)
    check("proceeds are recorded but excluded",
          ep.free_proceeds_lamports > 0 and ep.free_proceeds_lamports <= 5_000_000)
    check("completeness is partial", ep.pnl_completeness == PARTIAL)
    check("limitation explains why", "free-of-cost" in (ep.pnl_limitation or ""))
    check("no FIFO match exists for a transfer",
          all(m.buy_swap_id is not None for m in ep.lot_matches) and not ep.lot_matches)

    print("\n[airdrop inventory is treated the same way]")
    book = reconstruct("W5", "M1", [
        leg("sell", 1_000, 5_000_000, slot=5, swap_id=41),
    ], [TransferIn(amount_raw=1_000, kind="airdrop_claim", slot=4, event_time=T0)])
    ep = book.episodes[0]
    check("origin says airdrop", ep.inventory_origin == "includes_airdrop")
    check("realized P&L is None", ep.realized_pnl_lamports is None)

    print("\n[bought + transferred, then sold: only bought half earns P&L]")
    book = reconstruct("W6", "M1", [
        leg("buy", 500, 1_000_000, slot=1, swap_id=51),
        leg("sell", 1_000, 4_000_000, slot=3, minute=2, swap_id=52),
    ], [TransferIn(amount_raw=500, kind="spl_transfer", slot=2, event_time=T0)])
    ep = book.episodes[0]
    check("matched quantity is the bought half",
          sum(m.matched_qty_raw for m in ep.lot_matches) == 500)
    check("realized P&L excludes the free half",
          ep.realized_pnl_lamports == 2_000_000 - 1_000_000,
          str(ep.realized_pnl_lamports))
    check("free proceeds are reported separately",
          ep.free_proceeds_lamports == 2_000_000, str(ep.free_proceeds_lamports))
    check("inventory origin is flagged", ep.inventory_origin == "includes_transfer_in")
    check("completeness is partial", ep.pnl_completeness == PARTIAL)

    print("\n[selling inventory the window never saw bought]")
    book = reconstruct("W7", "M1", [
        leg("sell", 1_000, 5_000_000, slot=5, swap_id=61),
    ])
    ep = book.episodes[0]
    check("realized P&L is None with no evidence", ep.realized_pnl_lamports is None)
    check("completeness is unknown", ep.pnl_completeness == UNKNOWN)
    check("limitation names the observed window",
          "window" in (ep.pnl_limitation or ""), str(ep.pnl_limitation))
    check("the uncovered quantity is recorded", ep.uncovered_sell_qty_raw == 1_000)
    check("book total is None, not a number", book.realized_total_lamports is None)

    print("\n[input order does not matter]")
    legs = [
        leg("buy", 1_000, 1_000_000, slot=10, swap_id=71),
        leg("buy", 1_000, 3_000_000, slot=12, minute=2, swap_id=72),
        leg("sell", 2_000, 8_000_000, slot=11, minute=1, swap_id=73),
    ]
    a = reconstruct("W8", "M1", legs)
    b = reconstruct("W8", "M1", list(reversed(legs)))
    # The sell at slot 11 can only consume the lot bought at slot 10, so 1000 raw
    # is uncovered and the episode closes there; the buy at slot 12 opens a
    # second one. Order of the input must not change any of that.
    check("same episode count whichever way the legs arrive",
          len(a.episodes) == len(b.episodes) == 2,
          f"{len(a.episodes)} vs {len(b.episodes)}")
    check("same realized P&L regardless of input order",
          a.episodes[0].realized_pnl_lamports == b.episodes[0].realized_pnl_lamports)
    # The sell at slot 11 lands between the buys at 10 and 12: it can only be
    # covered by the first lot, so 1000 remains uncovered.
    check("slot ordering is respected, not input order",
          a.episodes[0].lot_matches and a.episodes[0].lot_matches[0].buy_swap_id == 71)

    print("\n[non-native quote cannot be a lamport basis]")
    book = reconstruct("W9", "M1", [
        leg("buy", 1_000, 1_000_000, slot=1, swap_id=81, quote_dec=6),
        leg("sell", 1_000, 2_000_000, slot=2, minute=1, swap_id=82, quote_dec=6),
    ])
    ep = book.episodes[0]
    check("realized P&L is None", ep.realized_pnl_lamports is None)
    check("completeness is unknown", ep.pnl_completeness == UNKNOWN)
    check("reason names the quote asset",
          "quote asset other than native SOL" in (ep.pnl_limitation or ""),
          str(ep.pnl_limitation))
    check("warning surfaced", any("not native SOL" in w for w in book.warnings))

    print("\n[open position: unrealized needs a mark and a rate]")
    book = reconstruct("W10", "M1", [
        leg("buy", 1_000_000, 100_000_000, slot=1, swap_id=91),
    ])
    ep = book.episodes[0]
    check("open episode", not ep.is_closed)
    check("unrealized is None without a mark", ep.unrealized_pnl_usd is None)
    check("reason mentions the mark", "no mark price" in (ep.pnl_limitation or ""),
          str(ep.pnl_limitation))
    marked = reconstruct("W10", "M1", [leg("buy", 1_000_000, 100_000_000, slot=1,
                                          swap_id=91)],
                         mark_price_usd=0.0002, mark_as_of=T0,
                         sol_usd=Decimal("200"))
    m = marked.episodes[0]
    # 1.0 token marked at 0.0002 USD = 0.0002 USD; basis 0.1 SOL = 20 USD.
    check("unrealized computed when marked",
          m.unrealized_pnl_usd is not None and abs(m.unrealized_pnl_usd + 19.9998) < 0.01,
          str(m.unrealized_pnl_usd))

    print("\n[allocation is exact: no lamport is lost or invented]")
    book = reconstruct("W11", "M1", [
        leg("buy", 333, 1_000_001, slot=1, swap_id=101),
        leg("buy", 333, 1_000_002, slot=2, minute=1, swap_id=102),
        leg("buy", 334, 1_000_003, slot=3, minute=2, swap_id=103),
        leg("sell", 1_000, 4_000_007, slot=4, minute=3, swap_id=104, fee=1_003),
    ])
    ep = book.episodes[0]
    check("all three lots matched", len(ep.lot_matches) == 3)
    check("matched quantity sums to the sell exactly",
          sum(m.matched_qty_raw for m in ep.lot_matches) == 1_000)
    check("proceeds split sums to the sell exactly",
          sum(m.proceeds_lamports for m in ep.lot_matches) == 4_000_007,
          str(sum(m.proceeds_lamports for m in ep.lot_matches)))
    check("fee split sums to the fee exactly",
          sum(m.fee_lamports for m in ep.lot_matches) == 1_003,
          str(sum(m.fee_lamports for m in ep.lot_matches)))
    check("cost allocation never exceeds the basis paid",
          sum(m.cost_lamports for m in ep.lot_matches) <= ep.cost_basis_lamports)
    check("match P&L sums to realized P&L",
          sum(m.pnl_lamports for m in ep.lot_matches) == ep.realized_pnl_lamports)

    print("\n[report renders]")
    text = format_book(reconstruct("W1", "M1", [
        leg("buy", 1_000_000, 100_000_000, slot=1),
        leg("sell", 1_000_000, 200_000_000, slot=2, minute=5),
    ]))
    check("report names wallet and token", "W1 / M1" in text)
    check("report shows a realized number", "+0." in text or "-0." in text)

    # ---------------------------------------------------------------- DB
    print("\n[database round-trip]")
    try:
        import psycopg
    except ImportError:
        print("  SKIP  psycopg not installed")
        return _finish()

    dsn = os.environ.get("SOLSCOPE_TEST_PGDSN")
    if not dsn:
        print("  SKIP  $SOLSCOPE_TEST_PGDSN not set (run through bootstrap_db.sh)")
        return _finish()

    from solscope.db import migrate
    from solscope.positions import load_legs, rebuild

    dbname = "solscope_positions_test"
    # The harness hands over a keyword DSN (host=… dbname=postgres), so split it
    # into parameters rather than string surgery on the path.
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    info = conninfo_to_dict(dsn)
    with psycopg.connect(make_conninfo(**info), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {dbname}")
        conn.execute(f"CREATE DATABASE {dbname}")
    target = make_conninfo(**{**info, "dbname": dbname})
    conn = psycopg.connect(target)
    try:
        migrate(conn)
        conn.execute("INSERT INTO chains (id, display_name) VALUES ('solana','Solana')")
        for mint in ("MintA",):
            conn.execute("INSERT INTO tokens (mint, chain_id) VALUES (%s,'solana')", (mint,))
        conn.execute("INSERT INTO wallets (address, chain_id) VALUES ('WalletA','solana')")
        for sig, slot in (("sig-buy", 10), ("sig-sell", 11)):
            conn.execute(
                "INSERT INTO transactions (signature, chain_id, slot, err) "
                "VALUES (%s,'solana',%s,false)", (sig, slot))
        rows = [
            ("sig-buy", 0, 10, 1_000_000, 100_000_000, "buy", 5_000),
            ("sig-sell", 0, 11, 1_000_000, 200_000_000, "sell", 5_000),
        ]
        for sig, ix, slot, base, quote, side, fee in rows:
            conn.execute(
                "INSERT INTO swaps (signature, instruction_index, slot, chain_id, "
                "  trader, base_mint, quote_mint, side, base_amount_raw, "
                "  quote_amount_raw, base_decimals, quote_decimals, exec_price_quote, "
                "  price_provenance, fee_lamports, decoder_version, event_time) "
                "VALUES (%s,%s,%s,'solana','WalletA','MintA',"
                "        'So11111111111111111111111111111111111111112',%s,%s,%s,6,9,%s,"
                "        'swap_derived',%s,'phase2/1',%s)",
                (sig, ix, slot, side, base, quote, quote / base, fee,
                 T0 + _dt.timedelta(minutes=slot)))
        conn.commit()

        check("legs load in chain order",
              [l.side for l in load_legs(conn, "WalletA", "MintA")] == ["buy", "sell"])
        rebuild(conn, wallet="WalletA")
        pos = conn.execute(
            "SELECT realized_pnl_lamports, is_closed, inventory_origin, "
            "       pnl_completeness FROM positions").fetchall()
        check("one position row written", len(pos) == 1, str(len(pos)))
        check("realized P&L persisted exactly",
              int(pos[0][0]) == 200_000_000 - 100_005_000 - 5_000, str(pos[0][0]))
        check("position persisted as closed", pos[0][1] is True)
        check("provenance persisted", pos[0][2] == "swap_only")
        check("completeness persisted", pos[0][3] == COMPLETE)
        check("lot_matches written", conn.execute(
            "SELECT count(*) FROM lot_matches").fetchone()[0] == 1)

        rebuild(conn, wallet="WalletA")
        check("rebuild is idempotent (still one position)", conn.execute(
            "SELECT count(*) FROM positions").fetchone()[0] == 1)
        check("rebuild is idempotent (still one match)", conn.execute(
            "SELECT count(*) FROM lot_matches").fetchone()[0] == 1)

        # A closed episode may coexist with an open one (doc 04 §2), but a
        # *second* open position for the same wallet+token may not — that is the
        # partial index which makes rebuild's delete-then-insert necessary.
        try:
            conn.execute(
                "INSERT INTO positions (address, chain_id, mint, opened_at, is_closed) "
                "VALUES ('WalletA','solana','MintA',now(),false)")
            check("a closed episode may coexist with an open one",
                  conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 2)
            try:
                conn.execute(
                    "INSERT INTO positions (address, chain_id, mint, opened_at, is_closed) "
                    "VALUES ('WalletA','solana','MintA',now(),false)")
                check("a second open position is rejected", False, "insert succeeded")
            except psycopg.errors.UniqueViolation:
                check("a second open position is rejected", True)
        finally:
            conn.rollback()
    finally:
        conn.close()

    return _finish()


def _finish() -> int:
    print()
    if _failures:
        print(f"{len(_failures)} CHECK(S) FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL POSITION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
