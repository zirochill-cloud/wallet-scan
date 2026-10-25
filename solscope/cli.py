"""solscope CLI — the single entry point for every phase.

Phase 0 wires up the skeleton: argument parsing, configuration loading, the
migration runner, and a credit-ledger self-check. Each analytics subcommand
currently resolves to "not implemented yet" for its body, because doc 03 scopes
those to later phases. What is real now:

  * ``solscope migrate``  — applies schema/*.sql in order, tracked, idempotent
  * ``solscope doctor``   — verifies config parses and the budget is sane
  * ``solscope budget``   — prints current spend vs the monthly cap

The rest (``discover ingest derive score report watch validate``) exist so the
surface is stable and later phases drop into a command that already parses.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable

from . import __version__
from .config import BudgetConfig, ConfigError, load_budget, load_scoring
from .db import MigrationError, connect, migrate

# Subcommands scheduled in doc 07. Bodies arrive with their phase; the
# parser accepts them now so scripts and docs can be written against a
# stable interface.
STUB_COMMANDS = {
    "discover": "Phase 4 — candidate wallet discovery",
    "score": "Phase 6 — scoring engine",
    "report": "Phase 7 — templated reports",
    "watch": "Phase 10 — monitoring and alerts",
    "validate": "Phase 9 — out-of-sample validation",
}


@dataclass
class Ctx:
    """Everything a subcommand needs, resolved once in main()."""

    budget: BudgetConfig
    dsn: str | None
    verbose: bool


def _cmd_migrate(ctx: Ctx, args: argparse.Namespace) -> int:
    conn = connect(ctx.dsn)
    try:
        applied = migrate(conn)
        if applied:
            for mig in applied:
                print(f"applied {mig.path.name}")
        else:
            print("schema already up to date")
    finally:
        conn.close()
    return 0


def _cmd_doctor(ctx: Ctx, args: argparse.Namespace) -> int:
    """Verify configuration parses and the budget is internally consistent."""
    failures: list[str] = []

    print(f"solscope {__version__}")
    for name, provider in sorted(ctx.budget.providers.items()):
        print(
            f"  provider {name}: monthly_cap={provider.monthly_cap} "
            f"warn_ratio={provider.warn_ratio} "
            f"operations={len(provider.operation_costs)}"
        )

    # An operation cost of zero on a paid provider is worth flagging: it means
    # that call would never be reflected in the budget.
    for name, provider in ctx.budget.providers.items():
        if name == "dexscreener":
            continue  # keyless by design
        free = [op for op, cost in provider.operation_costs.items() if cost == 0]
        if free:
            print(f"  note: {name} has zero-cost operations: {', '.join(sorted(free))}")

    try:
        scoring = load_scoring()
        print(f"  scoring.yaml: {len(scoring)} top-level keys")
    except ConfigError as exc:
        failures.append(f"scoring.yaml: {exc}")

    if failures:
        for f in failures:
            print(f"FAIL {f}", file=sys.stderr)
        return 1
    print("ok")
    return 0


def _cmd_budget(ctx: Ctx, args: argparse.Namespace) -> int:
    """Show recorded spend against the monthly cap for each provider."""
    conn = connect(ctx.dsn)
    try:
        from .ledger import CreditLedger, period_month_for
        import datetime as _dt

        ledger = CreditLedger(conn, ctx.budget)
        period = period_month_for(_dt.datetime.now(_dt.timezone.utc))
        print(f"billing period: {period.isoformat()}")
        for name, provider in sorted(ctx.budget.providers.items()):
            spent = ledger.spent(name, period)
            pct = (spent / provider.monthly_cap * 100) if provider.monthly_cap else 0.0
            print(
                f"  {name}: {spent}/{provider.monthly_cap} credits "
                f"({pct:.1f}% used, {provider.monthly_cap - spent} remaining)"
            )
    finally:
        conn.close()
    return 0


def _log_live(*msg, **kwargs) -> None:
    """Line-as-it-happens logging for the collector.

    Build a buffered pipe when a run is redirected to a file (``solscope
    ingest > run.log``) holds output back until the process exits — so live
    monitoring, warnings and hung-run diagnosis all miss it. Flush each line.
    """
    kwargs.setdefault("flush", True)
    print(*msg, **kwargs)


def _cmd_ingest(ctx: Ctx, args: argparse.Namespace) -> int:
    """Phase 1 forward collector: discover, filter, subscribe, archive."""
    import os

    from .collector import Collector, disk_stats
    from .config import load_collector

    cfg = load_collector()
    data_dir = cfg.data_dir

    if args.stats:
        stats = disk_stats(data_dir)
        print(f"archive root: {data_dir}")
        print(f"files: {stats['total_files']}  bytes: {stats['total_bytes']}")
        for sub, bucket in sorted(stats["per_subdir"].items()):
            print(f"  {sub}: {bucket['files']} files, {bucket['bytes']} bytes")
        if stats["bytes_per_hour"] is not None:
            print(
                f"observed over {stats['span_hours']:.2f}h: "
                f"{stats['bytes_per_hour']:.0f} bytes/hour "
                f"(projected {stats['projected_bytes_per_day']/1e6:.1f} MB/day)"
            )
        else:
            print("not enough history to project growth (need >= 2 files)")
        return 0

    from .adapters.helius import api_key_from_env
    from .ledger import BudgetExceeded, CreditLedger

    conn = None
    ledger = None
    if args.no_db:
        print("note: DB-free run — no spend metering, no checkpoints", file=sys.stderr)
    elif ctx.dsn is not None or os.environ.get("SOLSCOPE_PGDSN"):
        conn = connect(ctx.dsn)
        ledger = CreditLedger(conn, ctx.budget)
    else:
        print(
            "error: no database configured (set SOLSCOPE_PGDSN or pass --no-db "
            "for a DB-free run that skips spend metering and checkpoints)",
            file=sys.stderr,
        )
        return 1

    collector = Collector(
        cfg=cfg, budget=ctx.budget, conn=conn, ledger=ledger,
        api_key=api_key_from_env(), log=_log_live,
    )
    try:
        summary = collector.run(
            duration=args.duration, once=args.once, discovery_only=args.discovery_only)
    except BudgetExceeded as exc:
        print(f"BUDGET EXCEEDED — collector stopped: {exc}", file=sys.stderr)
        return 3
    finally:
        if conn is not None:
            conn.close()

    print(
        f"pools seen {summary.pools_seen}  new {summary.pools_new}  "
        f"subscriptions {summary.subscriptions}"
    )
    print(
        f"frames archived {summary.frames_archived}  "
        f"bytes {summary.bytes_archived + collector.archive_bytes}"
    )
    print(
        f"requests: geckoterminal {summary.geckoterminal_requests}  "
        f"dexscreener {summary.dexscreener_requests}  "
        f"helius credits {summary.helius_credits}"
    )
    for err in summary.errors:
        print(f"warning: {err}", file=sys.stderr)
    return 0


def _cmd_derive(ctx: Ctx, args: argparse.Namespace) -> int:
    """Phase 2: fetch → decode → persist; with --gate, reconcile vs DexScreener."""
    import os

    from .adapters.helius import HeliusRpc, api_key_from_env
    from .derive import Deriver, format_gate
    from .ledger import BudgetExceeded, CreditLedger

    if ctx.dsn is None and not os.environ.get("SOLSCOPE_PGDSN") and not args.no_db:
        print("error: derive needs a database (set SOLSCOPE_PGDSN or --no-db "
              "for decode-only output)", file=sys.stderr)
        return 1

    conn = None
    ledger = None
    if not args.no_db:
        conn = connect(ctx.dsn)
        ledger = CreditLedger(conn, ctx.budget)

    key = api_key_from_env()
    if not key:
        print("error: HELIUS_API_KEY not set (source .env first)", file=sys.stderr)
        return 1
    rpc = HeliusRpc(api_key=key, ledger=ledger, job="derive")
    deriver = Deriver(rpc=rpc, conn=conn, ledger=ledger, budget=ctx.budget,
                      log=_log_live)

    try:
        if args.gate:
            report = deriver.run_gate(min_pairs=args.pairs,
                                      sigs_per_pool=args.sigs,
                                      tolerance_pct=args.tolerance)
            print(format_gate(report))
            if args.json:
                import dataclasses
                import json as _json
                ok_, why_ = report.verdict(min_pairs=args.pairs)
                payload = {
                    "verdict": "PASS" if ok_ else "FAIL",
                    "reason": why_,
                    "tolerance_pct": report.tolerance_pct,
                    "required_pairs": report.required_pairs,
                    "min_pass_rate": report.min_pass_rate,
                    "fetched_at": report.fetched_at,
                    "mean_deviation_pct": report.mean_deviation,
                    "max_deviation_pct": report.max_deviation,
                    "mean_liquidity_deviation_pct": report.mean_liquidity_deviation,
                    "rows": [dataclasses.asdict(r) for r in report.rows],
                }
                with open(args.json, "w", encoding="utf-8") as fh:
                    _json.dump(payload, fh, indent=2)
                print(f"wrote {args.json}")
            ok, _ = report.verdict(min_pairs=args.pairs)
            return 0 if ok else 4   # 4 = gate ran but did not pass

        # plain decode pass over the most recently active pools
        pools = deriver.active_pools(args.pairs)
        known = deriver.known_pools()
        total = 0
        for pool in pools:
            n, _swaps, reasons = deriver.process_pool(pool, limit=args.sigs,
                                                      known=known)
            total += n
            for r in reasons[:3]:
                if args.verbose:
                    print(f"skip: {r}")
        print(f"decoded {total} swap(s) across {len(pools)} pool(s)")
        return 0
    except BudgetExceeded as exc:
        print(f"BUDGET EXCEEDED — derive stopped: {exc}", file=sys.stderr)
        return 3
    finally:
        if conn is not None:
            conn.close()


def _cmd_stub(name: str) -> Callable[[Ctx, argparse.Namespace], int]:
    def run(ctx: Ctx, args: argparse.Namespace) -> int:
        print(
            f"solscope {name} is not implemented yet — {STUB_COMMANDS[name]}.\n"
            f"See docs/07-roadmap.md for this phase's exit criteria.",
            file=sys.stderr,
        )
        return 2  # distinct from a crash: recognized but not built
    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solscope",
        description="Smart-money wallet intelligence for Solana (design-phase skeleton).",
    )
    parser.add_argument("--version", action="version", version=f"solscope {__version__}")
    parser.add_argument("--dsn", default=None,
                        help="PostgreSQL DSN (default: $SOLSCOPE_PGDSN)")
    parser.add_argument("--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply schema/*.sql in order (idempotent)")
    sub.add_parser("doctor", help="verify configuration parses and is sane")
    sub.add_parser("budget", help="show spend vs the monthly credit cap")

    ingest = sub.add_parser("ingest", help="Phase 1 — forward collector")
    ingest.add_argument("--duration", type=float, default=None,
                        help="seconds to run (default: until killed)")
    ingest.add_argument("--once", action="store_true",
                        help="one tick of each feed, then exit (smoke test)")
    ingest.add_argument("--discovery-only", action="store_true",
                        help="skip the WSS leg; discover + archive only")
    ingest.add_argument("--no-db", action="store_true",
                        help="DB-free run: no ledger, no checkpoints (explicit opt-out)")
    ingest.add_argument("--stats", action="store_true",
                        help="print archive size + growth projection, then exit")

    derive = sub.add_parser("derive", help="Phase 2 — swap decode + price derivation")
    derive.add_argument("--gate", action="store_true",
                        help="run the doc 07 go/no-go reconciliation vs DexScreener")
    derive.add_argument("--pairs", type=int, default=20,
                        help="how many live pairs the gate requires (default 20)")
    derive.add_argument("--sigs", type=int, default=0,
                        help="signatures to decode per pool as extra evidence "
                             "(default 0 — the gate prices from reserves and "
                             "needs no per-transaction credits)")
    derive.add_argument("--tolerance", type=float, default=5.0,
                        help="gate tolerance in percent (default 5.0)")
    derive.add_argument("--no-db", action="store_true",
                        help="decode without persisting (stdout only)")
    derive.add_argument("--json", default=None, metavar="PATH",
                        help="write the gate report as JSON evidence")

    for name in STUB_COMMANDS:
        sub.add_parser(name, help=STUB_COMMANDS[name])

    return parser


_HANDLERS: dict[str, Callable[[Ctx, argparse.Namespace], int]] = {
    "migrate": _cmd_migrate,
    "doctor": _cmd_doctor,
    "budget": _cmd_budget,
    "ingest": _cmd_ingest,
    "derive": _cmd_derive,
    **{name: _cmd_stub(name) for name in STUB_COMMANDS},
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        budget = load_budget()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    ctx = Ctx(budget=budget, dsn=args.dsn, verbose=args.verbose)

    try:
        return _HANDLERS[args.command](ctx, args)
    except (ConfigError, MigrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # surface a clean message, not a traceback, by default
        if ctx.verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
