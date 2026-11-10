"""Throwaway probe: one anchor's retrospective history, and what it would buy.

Prints the FetchReport, the per-wallet rebuild failures, and — when a database
with scores is attached — the confidence distribution. Without a scoring run it
prints the report only: a confidence number that was not computed is worse than
no number, because it looks like evidence.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import tempfile


def _wallets_from_cache(anchor: str) -> list[str]:
    path = pathlib.Path(tempfile.gettempdir()) / "solscope-anchor-verify" / f"verify_{anchor}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [b["wallet"] for b in data.get("buyers", []) if b.get("wallet")]


def _genesis_from_cache(anchor: str) -> _dt.datetime | None:
    path = pathlib.Path(tempfile.gettempdir()) / "solscope-anchor-verify" / f"verify_{anchor}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    genesis = data.get("genesis")
    if isinstance(genesis, (int, float)):
        return _dt.datetime.fromtimestamp(int(genesis), _dt.timezone.utc)
    if isinstance(genesis, str) and genesis:
        return _dt.datetime.fromisoformat(genesis.replace("Z", "+00:00"))
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor", required=True, help="anchor mint")
    parser.add_argument("--wallets-file", default=None,
                        help="newline-delimited buyer wallets (default: verify cache)")
    parser.add_argument("--dry-run", action="store_true",
                        help="decode and report, write nothing")
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--genesis", default=None, help="ISO genesis time override")
    args = parser.parse_args(argv)

    from solscope.ingest_retrospective import fetch_anchor_history

    if args.wallets_file:
        wallets = [w.strip() for w in pathlib.Path(args.wallets_file).read_text().split() if w.strip()]
    else:
        wallets = _wallets_from_cache(args.anchor)
    if not wallets:
        print("no buyer wallets: pass --wallets-file (cache miss for this anchor)")
        return 2

    genesis = None
    if args.genesis:
        genesis = _dt.datetime.fromisoformat(args.genesis.replace("Z", "+00:00"))
    genesis = genesis or _genesis_from_cache(args.anchor)
    if genesis is None:
        print("no genesis time: pass --genesis (cache has no genesis_time)")
        return 2

    dsn = os.environ.get("SOLSCOPE_PGDSN")
    if not dsn:
        print("SOLSCOPE_PGDSN is not set")
        return 2

    import psycopg

    config = {"api_key": os.environ.get("HELIUS_API_KEY")}
    with psycopg.connect(dsn) as conn:
        report = fetch_anchor_history(
            conn, anchor_mint=args.anchor, genesis_time=genesis,
            window_days=args.window_days, buyer_wallets=set(wallets),
            config=config, dry_run=args.dry_run,
        )

    print(f"anchor      {report.anchor_mint}")
    print(f"signatures  {report.signatures_fetched}")
    print(f"decoded     {report.transactions_decoded}")
    print(f"skipped     {report.transactions_skipped}")
    print(f"swaps       {report.swaps_written}{' (dry run: not written)' if args.dry_run else ''}")
    print(f"credits     {report.credits_used}")
    print(f"failures    {len(report.failures)}")
    for failure in report.failures:
        print(f"  position_fetch_failed: {failure['signature']}: {failure['reason']}")
    print(f"entry legs  {len(report.entry_legs)}")

    if args.dry_run:
        print("\ndry run: no rows written, no confidence computed")
        return 0
    print("\nswaps and positions written; run `consumer` then `scores` for confidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
