"""Seed the five verified anchors into a live database (Path B, 2026-11-07).

The manual path, not S3 discovery: the anchors are supplied, verified, and fixed, so
this writes exactly the rows the retrospective pipeline consumes —

    anchors -> anchor_wallet_verdicts / anchor_wallet_metrics -> wallet_aggregates

and then hands the result to ``solscope.consumer``. What it does **not** write is a
position: ``positions.reconstruct`` needs a wallet's full swap history in the token,
and no ingest has produced one. Writing an episode from the single launch buy would
record "this position is open" when the wallet may have exited an hour later, which
is an invented fact rather than an incomplete one.

Every metric is written with the status that describes what was actually measurable:

* ``max_subsequent_return`` — computed. Entry price is the wallet's own executed
  price, observations are the dust-filtered swap-implied series, ``as_of`` is the
  30-day horizon, so the window is mature rather than truncated.
* ``buyer_rank_at_entry`` — NOT_COMPUTABLE. The buyer set is the first 20 of the
  mint, not all of them; ranking a prefix of history is exactly the artefact
  ``docs/PHASE1B_SCOPING.md`` Q2 warns about.
* ``pct_entries_before_volume_spike`` — NOT_COMPUTABLE. A volume baseline needs every
  swap's size in an hour; the sampled price series has three trades per six hours and
  cannot supply one.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
import tempfile
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE = pathlib.Path(tempfile.gettempdir()) / "solscope-anchor-verify"

RULE_VERSION = "phase1b-anchors-2026-11-07"
METRIC_RETURN = "max_subsequent_return"
METRIC_RANK = "buyer_rank_at_entry"
METRIC_SPIKE = "pct_entries_before_volume_spike"
WINDOW_DAYS = 30


def _utc(epoch: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)


def _load() -> list[dict]:
    rows = []
    for path in sorted(CACHE.glob("verify_*.json")):
        if path.name == "verify_all.json":
            continue
        data = json.loads(path.read_text())
        if data.get("verdict") != "PASS":
            continue
        # The mint is the filename: verify_<mint>.json.
        data["mint"] = path.stem[len("verify_"):]
        rows.append(data)
    return sorted(rows, key=lambda r: r["genesis"]["block_time"])


def seed(conn) -> dict:
    from solscope.aggregation import (
        AnchorMetricRow,
        aggregate_buyer_rank_at_entry,
        aggregate_max_subsequent_return,
        aggregate_pct_entries_before_volume_spike,
    )
    from solscope.anchor_writer import (
        AnchorInput,
        write_aggregate,
        write_anchor,
        write_metric,
        write_verdict,
    )
    from solscope.metrics import PriceObservation, max_subsequent_return
    from solscope.quality_filter import WalletVerdict

    summary = {"anchors": 0, "wallets": set(), "computed": 0, "not_computable": 0,
               "verdicts": 0, "aggregates": 0}

    for data in _load():
        mint = data["mint"]
        genesis_time = data["genesis"]["block_time"]
        buyers = data["buyers"]
        anchor_id = write_anchor(conn, AnchorInput(
            mint=mint,
            source="user_supplied",
            created_by_rule_version=RULE_VERSION,
            genesis_slot=data["genesis"]["slot"],
            genesis_time=_utc(genesis_time),
            deployer=data["genesis"]["deployer"],
            first_buy_observed_at=_utc(buyers[0]["block_time"]) if buyers else None,
        ))
        summary["anchors"] += 1

        # Dust-filtered, swap-implied prices: the same series the verdict was measured
        # on, so a wallet's multiple and the anchor's multiple cannot disagree.
        observations = [
            PriceObservation(at=_utc(row["t"]), price_quote=Decimal(str(row["price"])))
            for row in data["series"]
            if (row.get("base_tokens") or 0) >= 100 and (row.get("quote_sol") or 0) >= 0.001
        ]
        # The horizon the series was sampled to is genesis + 30d, so a buyer who
        # entered seconds after genesis has a window a few seconds short of mature
        # at exactly that instant. The margin makes every window mature without
        # extending it: no observation exists past genesis + 30d, so nothing is
        # imputed — the window simply ends where the data does.
        as_of = _utc(genesis_time + WINDOW_DAYS * 86400 + 3600)

        for buyer in buyers:
            wallet = buyer["wallet"]
            summary["wallets"].add(wallet)
            if not buyer.get("sol_per_token"):
                continue
            entry_time = _utc(buyer["block_time"])
            result = max_subsequent_return(
                entry_price_quote=Decimal(str(buyer["sol_per_token"])),
                entry_time=entry_time,
                observations=observations,
                as_of=as_of,
                windows_days=(WINDOW_DAYS,),
                headline_days=WINDOW_DAYS,
            )
            window = result.windows[0]
            if window.status == "OK":
                write_metric(conn, anchor_id=anchor_id, wallet=wallet,
                             metric_name=METRIC_RETURN, status="computed",
                             value=window.multiple, window_days=WINDOW_DAYS,
                             peak_day=window.peak_day)
            else:
                write_metric(conn, anchor_id=anchor_id, wallet=wallet,
                             metric_name=METRIC_RETURN, status=window.status,
                             window_days=WINDOW_DAYS)

            write_metric(conn, anchor_id=anchor_id, wallet=wallet,
                         metric_name=METRIC_RANK, status="NOT_COMPUTABLE")
            write_metric(conn, anchor_id=anchor_id, wallet=wallet,
                         metric_name=METRIC_SPIKE, status="NOT_COMPUTABLE")
            summary["not_computable"] += 2

            # One anchor per wallet by construction, so the burst rule (which needs
            # GENESIS_BURST_MIN_ANCHORS anchors to fire or clear) cannot be evaluated.
            # ACCEPTED would claim a rule was cleared that was never applied.
            write_verdict(conn, anchor_id=anchor_id, wallet=wallet,
                          verdict=WalletVerdict.NOT_COMPUTABLE_INSUFFICIENT_ANCHORS,
                          rule_version=RULE_VERSION,
                          evidence={"anchors_observed": 1,
                                    "anchor_mint": mint,
                                    "entry_price_sol_per_token": buyer["sol_per_token"],
                                    "entry_seconds_after_genesis": buyer["seconds_after_genesis"],
                                    "entry_signature": buyer["signature"],
                                    "buyer_set": "first_20_of_mint"})
            summary["verdicts"] += 1

            computed = (Decimal(str(window.multiple))
                        if window.status == "OK" and window.multiple is not None else None)
            rows = [
                AnchorMetricRow(anchor_id=anchor_id, metric_name=METRIC_RETURN,
                                status="computed" if computed is not None else window.status,
                                value=computed, wallet=wallet),
                AnchorMetricRow(anchor_id=anchor_id, metric_name=METRIC_RANK,
                                status="NOT_COMPUTABLE", value=None, wallet=wallet),
                AnchorMetricRow(anchor_id=anchor_id, metric_name=METRIC_SPIKE,
                                status="NOT_COMPUTABLE", value=None, wallet=wallet),
            ]
            write_aggregate(conn, wallet,
                            aggregate_max_subsequent_return([rows[0]]), RULE_VERSION)
            write_aggregate(conn, wallet,
                            aggregate_buyer_rank_at_entry([rows[1]]), RULE_VERSION)
            write_aggregate(conn, wallet,
                            aggregate_pct_entries_before_volume_spike([rows[2]]),
                            RULE_VERSION)
            summary["aggregates"] += 3
            if computed is not None:
                summary["computed"] += 1

    return summary


def main() -> int:
    import psycopg

    from solscope.db import connect, migrate

    with connect() as conn:
        applied = [m.path.name for m in migrate(conn)]
        print(f"migrations: {len(applied)} applied/verified")
        summary = seed(conn)
        print(f"anchors seeded:        {summary['anchors']}")
        print(f"distinct wallets:      {len(summary['wallets'])}")
        print(f"return metrics:        {summary['computed']} computed, "
              f"{summary['not_computable']} marked NOT_COMPUTABLE")
        print(f"verdicts:              {summary['verdicts']}")
        print(f"wallet_aggregates:     {summary['aggregates']}")
        for table in ("anchors", "anchor_wallet_metrics", "anchor_wallet_verdicts",
                      "wallet_aggregates"):
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"  {table:<24}{count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
