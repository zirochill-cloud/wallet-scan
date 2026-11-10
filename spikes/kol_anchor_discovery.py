"""KOL-anchor discovery spike — read-only, FREE-tier MadeOnSol only.

    python spikes/kol_anchor_discovery.py [--min-kols 3] [--max-mcap 500000]

Answers one question: **which tokens did several tracked KOLs enter early, and
how early?** It prints candidates and their KOL entry ranks/mcaps, then stops.

What it deliberately does NOT do:

* **Seed nothing.** No database writes, no ``anchor_writer`` call, no
  ``data/seed_anchors.json``. Choosing an anchor stays a human decision after
  reading this output.
* **Spend Helius credits.** Discovery runs entirely on MadeOnSol's free tier.
  The anchors that come out are marked ``unverified`` on purpose: MadeOnSol
  ranks *tracked-KOL* first buys only, so "KOL #1" is not the same claim as
  "the token's first buyer". Confirming the real first-20 buyer set is a
  Helius job for whatever candidates get approved.

Cost: 1 candidate page + 1 entry-order + 1 trades page per candidate
(~3 calls per token), against a 200/day free quota. The script prints the
projected cost before it spends anything.

Caveat to carry into any decision: free-tier feeds are 5 minutes delayed and
trades below 0.05 SOL / $3.50 are never stored, so an absent KOL entry may be
"too small to persist" rather than "did not happen".
"""
from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from solscope.adapters.madeonsol import (  # noqa: E402
    AuthError,
    KOLAnchorCandidate,
    MadeOnSolClient,
    MadeOnSolError,
    QuotaExhausted,
    TierRequired,
)

KEY_ENV = "MADEONSOL_API_KEY"
VERDICT_ORDER = {"STRONG": 0, "MODERATE": 1, "WEAK": 2}


def _api_key() -> str | None:
    """Env var first (``set -a; source .env``), then the project ``.env`` file —
    the same convention as ``spikes/verify_anchors.py``. Never printed."""
    import os

    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    env_file = ROOT / ".env"
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{KEY_ENV}="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kol_anchor_discovery.py",
        description="Find tokens several KOLs entered early (read-only; seeds nothing).",
    )
    parser.add_argument("--min-kols", type=int, default=3,
                        help="minimum distinct KOL buyers for a candidate (default: 3)")
    parser.add_argument("--max-mcap", type=float, default=None,
                        help="max market cap at the first KOL buy, in USD "
                             "(e.g. 500000). Candidates with unknown MC are dropped "
                             "when this is set.")
    parser.add_argument("--limit-tokens", type=int, default=20,
                        help="max candidates to confirm and print (default: 20)")
    parser.add_argument("--pages", type=int, default=3,
                        help="candidate pages to scan before confirming (default: 3)")
    parser.add_argument("--page-size", type=int, default=50,
                        help="events per candidate page (default: 50)")
    return parser.parse_args(argv)


def _usd(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


def _ts(value: _dt.datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def entry_mcaps(client: MadeOnSolClient, mint: str) -> tuple[dict[str, tuple[float | None, _dt.datetime | None]], str]:
    """``wallet -> (mcap at that wallet's first buy, traded_at)``.

    Entry order carries ranks and times but **no mcap**, so the price dimension
    has to come from the trade records. Free tier clamps the page to 20 rows, so
    a KOL entering late may be missing here — reported, never guessed.
    """
    trades = client.get_token_kol_trades(mint, limit=20)
    best: dict[str, tuple[float | None, _dt.datetime | None]] = {}
    for trade in trades:
        if trade.action != "buy":
            continue
        current = best.get(trade.wallet)
        if current is None or (trade.traded_at and current[1] and trade.traded_at < current[1]) \
                or (current[1] is None and trade.traded_at is not None):
            best[trade.wallet] = (trade.market_cap_usd_at_trade, trade.traded_at)
    note = ""
    if len(trades) >= 20:
        note = "mcaps read from the first 20 stored trades (BASIC page clamp)"
    return best, note


def render(candidate: KOLAnchorCandidate, mcaps: dict[str, tuple[float | None, _dt.datetime | None]],
           index: int) -> list[str]:
    lines = [
        f"[{index}] {candidate.verdict:<8} {candidate.token_mint}  {candidate.token_symbol or '?'}",
        f"    KOL buyers: {candidate.kol_count}   ranks: "
        f"{','.join(str(r) for r in candidate.entry_ranks) or '-'}   "
        f"first KOL buy: {_ts(candidate.first_kol_buy_at)}   "
        f"entry MC: {_usd(candidate.entry_mcap_usd)}",
    ]
    for rank, wallet in zip(candidate.entry_ranks, candidate.kol_wallets):
        mcap, traded_at = mcaps.get(wallet, (None, None))
        flag = "  (mcap unavailable — not in the stored trade page)" if mcap is None else ""
        lines.append(f"      #{rank:<3} {wallet}  {_usd(mcap):>9}  {_ts(traded_at)}{flag}")
    for note in candidate.notes:
        lines.append(f"    note: {note}")
    if candidate.unverified:
        lines.append("    note: KOL ranks only — the token's true first-20 buyers are NOT "
                     "Helius-verified")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    key = _api_key()
    if not key:
        print(f"{KEY_ENV} is not set.", file=sys.stderr)
        print(f"  Add it to .env (untracked) as {KEY_ENV}=msk_... and re-run.\n"
              f"  The client itself is built and tested against mocks; no live call was made.",
              file=sys.stderr)
        return 2

    client = MadeOnSolClient(key, usage_path=ROOT / "data" / "madeonsol_usage.json")
    quota = client.quota()
    projected = 1 + args.limit_tokens * 2
    print("MadeOnSol KOL-anchor discovery — FREE tier, read-only, seeds nothing")
    print(f"  budget: {quota.remaining}/{quota.daily_cap} calls left today "
          f"(projected cost for this run: ~{projected})")
    print(f"  filters: min_kols={args.min_kols}  "
          f"max_entry_mcap={_usd(args.max_mcap) if args.max_mcap else 'none'}  "
          f"limit_tokens={args.limit_tokens}")
    if not client.budget_for(projected):
        print(f"\n  Refusing to start: {quota.remaining} calls left, ~{projected} needed.",
              file=sys.stderr)
        return 3

    try:
        candidates = client.find_kol_anchors(
            min_kols=args.min_kols,
            max_entry_mcap_usd=args.max_mcap,
            limit_tokens=args.limit_tokens,
            pages=args.pages,
            page_size=args.page_size,
        )
    except (AuthError, TierRequired, QuotaExhausted, MadeOnSolError) as exc:
        print(f"\nAPI failure: {exc}", file=sys.stderr)
        return 1

    if not candidates:
        print("\nNo token met the filters on the pages scanned.")
        print("  Worth knowing: /kol/first-touches paging depth is undocumented, so this "
              "may mean 'none in reach' rather than 'none exist'.")
        print(f"\ncalls used: {client.calls_made}")
        return 0

    candidates.sort(key=lambda c: (VERDICT_ORDER.get(c.verdict, 9), -c.kol_count,
                                   c.entry_mcap_usd if c.entry_mcap_usd is not None else 1e18))
    print(f"\n{len(candidates)} candidate(s), strongest first:\n")
    for index, candidate in enumerate(candidates, start=1):
        try:
            mcaps, note = entry_mcaps(client, candidate.token_mint)
        except MadeOnSolError as exc:
            mcaps, note = {}, f"mcaps unavailable: {exc}"
        for line in render(candidate, mcaps, index):
            print(line)
        if note:
            print(f"    note: {note}")
        print()

    used = client.quota()
    print(f"calls used: {client.calls_made}  (today {used.used_today}/{used.daily_cap}, "
          f"{used.remaining} left)")
    print("Nothing was seeded. These candidates are KOL-labelled, not Helius-verified; "
          "the true first-20 buyer check belongs to the seeding decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
