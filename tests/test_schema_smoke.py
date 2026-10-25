"""Schema smoke test: applies schema/001_init.sql to a real PostgreSQL and asserts the
constraints behave. Reproduces the check that caught two real bugs in the first draft.

Run:  python tests/test_schema_smoke.py
Requires: psycopg[binary] and pgserver (see requirements-dev.txt), OR a reachable
Postgres via $SOLSCOPE_TEST_PGDSN. Skips cleanly if neither is available.
"""
from __future__ import annotations

import os
import sys
import pathlib

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema" / "001_init.sql"

try:
    import psycopg
    import psycopg.errors as E
except ImportError:  # pragma: no cover
    print("SKIP: psycopg not installed (pip install 'psycopg[binary]')")
    sys.exit(0)

SWAP_SQL = """
INSERT INTO swaps(signature,instruction_index,inner_index,slot,pool_address,chain_id,trader,
  base_mint,quote_mint,side,base_amount_raw,quote_amount_raw,base_decimals,quote_decimals,
  exec_price_quote,pool_price_quote,price_provenance,decoder_version)
VALUES (%s,%s,%s,100,'POOL','solana',%s,'MEME',
  'So11111111111111111111111111111111111111112','buy',1,1,6,9,1,1,'swap_derived','d')
"""

TRANSFER_SQL = """
INSERT INTO token_transfers(signature,instruction_index,inner_index,slot,mint,to_address,
  amount_raw,decimals,transfer_kind)
VALUES (%s,%s,%s,100,'MEME','W1',10,6,'spl_transfer')
"""

POSITION_SQL = """
INSERT INTO positions(address,chain_id,mint,opened_at,is_closed)
VALUES ('W1','solana','MEME',%s,%s)
"""

_failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        _failures.append(name)
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:140]}")


def expect(exc_type, fn) -> None:
    try:
        fn()
    except exc_type:
        return
    except Exception as other:  # noqa: BLE001
        raise AssertionError(f"expected {exc_type.__name__}, got {type(other).__name__}: {other}") from other
    raise AssertionError(f"expected {exc_type.__name__}, but the statement succeeded")


def connect():
    """Return (admin_dsn, dbname, cleanup) for a scratch database."""
    dsn = os.environ.get("SOLSCOPE_TEST_PGDSN")
    if dsn:
        return {"dsn": dsn}, "solscope_smoke", lambda: None

    try:
        import pgserver
    except ImportError:
        print("SKIP: neither $SOLSCOPE_TEST_PGDSN nor pgserver available")
        sys.exit(0)

    server = pgserver.get_server("/tmp/pgdata-solscope")
    return {"host": "/tmp/pgdata-solscope", "user": "postgres"}, "solscope_smoke", lambda: None


def main() -> int:
    conninfo, dbname, _ = connect()
    schema = SCHEMA_PATH.read_text()

    admin = dict(conninfo)
    if "dsn" in admin:
        admin = {"conninfo": admin["dsn"]}
    with psycopg.connect(**admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {dbname}")
        cur.execute(f"CREATE DATABASE {dbname}")

    target = dict(conninfo)
    if "dsn" in target:
        target = {"conninfo": target["dsn"]}
    else:
        target["dbname"] = dbname   # connect into the database we just created
    with psycopg.connect(**target, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(schema)
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )
        n_tables = cur.fetchone()[0]
        print(f"schema applied; {n_tables} tables, {len(schema)} bytes of DDL\n")
        assert n_tables >= 40, f"expected >= 40 tables, got {n_tables}"

        cur.execute("INSERT INTO chains(id,display_name) VALUES ('solana','Solana')")
        cur.execute(
            "INSERT INTO tokens(mint,chain_id,symbol,decimals,launchpad,created_at) "
            "VALUES ('MEME','solana','MEME',6,'pump_fun',now()-interval '3 hours')"
        )
        cur.execute(
            "INSERT INTO pools(pool_address,chain_id,dex,base_mint,quote_mint,created_at) "
            "VALUES ('POOL','solana','pump_fun','MEME',"
            "'So11111111111111111111111111111111111111112',now()-interval '3 hours')"
        )
        for addr in ("W1", "W2"):
            cur.execute(
                "INSERT INTO wallets(address,chain_id,history_completeness) "
                "VALUES (%s,'solana','complete')",
                (addr,),
            )
        for sig in ("SB", "SX"):
            cur.execute(
                "INSERT INTO transactions(signature,chain_id,slot,commitment) "
                "VALUES (%s,'solana',100,'finalized')",
                (sig,),
            )

        # --- regression tests for bugs found by this harness -----------------
        cur.execute(SWAP_SQL, ("SB", 0, None, "W1"))
        check(
            "REGRESSION duplicate swap with NULL inner_index rejected",
            lambda: expect(E.UniqueViolation, lambda: cur.execute(SWAP_SQL, ("SB", 0, None, "W2"))),
        )
        check(
            "distinct inner_index still inserts",
            lambda: cur.execute(SWAP_SQL, ("SB", 0, 3, "W2")),
        )

        cur.execute(TRANSFER_SQL, ("SX", 0, None))
        check(
            "REGRESSION duplicate transfer with NULL inner_index rejected",
            lambda: expect(E.UniqueViolation, lambda: cur.execute(TRANSFER_SQL, ("SX", 0, None))),
        )

        cur.execute(POSITION_SQL, ("2026-01-01", False))
        check(
            "REGRESSION second OPEN position for same wallet+token rejected",
            lambda: expect(E.UniqueViolation, lambda: cur.execute(POSITION_SQL, ("2026-01-02", False))),
        )
        check(
            "a CLOSED position may coexist with an open one",
            lambda: cur.execute(POSITION_SQL, ("2025-12-01", True)),
        )
        check(
            "a new position may open after the previous one closed",
            lambda: (
                cur.execute(
                    "UPDATE positions SET is_closed=true, closed_at=now() "
                    "WHERE address='W1' AND mint='MEME' AND opened_at='2026-01-01'"
                ),
                cur.execute(POSITION_SQL, ("2026-02-01", False)),
            ),
        )

        # --- domain integrity ------------------------------------------------
        check(
            "swap side must be buy|sell",
            lambda: expect(
                E.CheckViolation,
                lambda: cur.execute(SWAP_SQL.replace("'buy'", "'hodl'"), ("SB", 9, None, "W2")),
            ),
        )
        check(
            "unknown inventory_origin rejected",
            lambda: expect(
                E.CheckViolation,
                lambda: cur.execute(
                    "INSERT INTO positions(address,chain_id,mint,opened_at,inventory_origin) "
                    "VALUES ('W2','solana','MEME',now(),'free_money')"
                ),
            ),
        )
        check(
            "unknown owning_subscore rejected",
            lambda: expect(
                E.CheckViolation,
                lambda: cur.execute(
                    "INSERT INTO metric_definitions(metric_key,display_name,description,"
                    "formula,required_inputs,owning_subscore) "
                    "VALUES ('x','x','x','x','{}','vibes')"
                ),
            ),
        )
        check(
            "FK rejects swap for unknown wallet",
            lambda: expect(
                E.ForeignKeyViolation, lambda: cur.execute(SWAP_SQL, ("SB", 11, None, "GHOST"))
            ),
        )

        # --- the core early-entry query --------------------------------------
        cur.execute(
            """
            SELECT s.trader, s.token_age_seconds, s.mcap_usd
            FROM swaps s
            WHERE s.base_mint='MEME' AND s.side='buy' AND NOT s.is_failed
              AND s.event_time <= now()
              AND s.event_time = (
                SELECT min(s2.event_time) FROM swaps s2
                WHERE s2.trader=s.trader AND s2.base_mint=s.base_mint AND s2.side='buy')
            """
        )
        rows = cur.fetchall()
        print(f"\n  early-entry query returns {len(rows)} row(s) "
              f"(0 expected here: seeded swaps have NULL event_time, which the "
              f"point-in-time filter intentionally excludes)")

        # --- enum integrity ---------------------------------------------------
        check(
            "invalid subscription_kind rejected",
            lambda: expect(
                E.InvalidTextRepresentation,
                lambda: cur.execute(
                    "INSERT INTO stream_subscriptions(provider,kind,target,chain_id) "
                    "VALUES ('helius','telepathy','X','solana')"
                ),
            ),
        )

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("ALL SCHEMA CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
