"""Read-only reporting over stored scores — the body of `solscope scores` (S12c).

The gap this closes: Option 3's turn ended with bands and gate outcomes that could
only be inspected by writing SQL by hand. A number nobody can look at is a number
nobody can argue with, so the read path is part of the deliverable rather than an
afterthought.

What it reads: the latest ``score_run`` only. Historical runs stay in the table for
audit, but a report that silently mixed runs would add up two different configs.
``--as-of`` narrows to rows computed at or before an instant on top of that.

``gate_passed`` is derived from ``gated_by`` (``confidence_gate:`` is only written
when the gate fails) rather than stored twice; the alternative is a second column
that can disagree with the first.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from decimal import Decimal
from typing import Sequence

BANDS = ("track", "watch", "investigate", "promising_thin", "exclude")

FETCH_SQL = """
    SELECT s.address,
           s.band,
           s.confidence_adjusted_score,
           e.raw_value,
           c.confidence,
           s.gated_by,
           s.band_basis
    FROM scores s
    LEFT JOIN sub_scores e
           ON e.score_id = s.id AND e.dimension = 'early_entry'
    LEFT JOIN sub_scores c
           ON c.score_id = s.id AND c.dimension = 'confidence'
    WHERE s.score_run_id = (SELECT max(score_run_id) FROM scores)
      AND (%s::text IS NULL OR s.address = %s)
      AND (%s::text IS NULL OR s.band = %s)
      AND (%s::timestamptz IS NULL OR s.as_of <= %s)
    ORDER BY s.confidence_adjusted_score DESC, s.address
    LIMIT %s
"""


@dataclasses.dataclass(frozen=True)
class ScoreRow:
    """One wallet as a reader needs it: verdict, numbers, and why."""

    wallet: str
    band: str
    adjusted: Decimal | None
    raw_early_entry: Decimal | None
    confidence: Decimal | None
    gate_passed: bool
    gated_by: str
    band_basis: str


def fetch_scores(conn, *, wallet: str | None = None, band: str | None = None,
                 limit: int = 50, as_of: _dt.datetime | None = None) -> list[ScoreRow]:
    """Rows from the latest run, best adjusted score first.

    ``band`` filters on the stored band exactly; ``as_of`` filters on when the row
    was computed, not on the run's window, so it can only narrow, never broaden.
    """
    if band is not None and band not in BANDS:
        raise ValueError(f"unknown band {band!r}; expected one of {', '.join(BANDS)}")
    limit = max(1, int(limit))
    rows = conn.execute(
        FETCH_SQL,
        (wallet, wallet, band, band, as_of, as_of, limit),
    ).fetchall()
    out: list[ScoreRow] = []
    for address, row_band, adjusted, raw, confidence, gated_by, basis in rows:
        gate_failed = "confidence_gate:" in (gated_by or "")
        out.append(ScoreRow(
            wallet=address,
            band=row_band,
            adjusted=Decimal(adjusted) if adjusted is not None else None,
            raw_early_entry=Decimal(raw) if raw is not None else None,
            confidence=Decimal(confidence) if confidence is not None else None,
            gate_passed=not gate_failed,
            gated_by=gated_by or "",
            band_basis=basis or "",
        ))
    return out


def format_scores(rows: Sequence[ScoreRow]) -> str:
    """A fixed-width table, plus the basis line that says what the bands mean."""
    if not rows:
        return "no scores stored (run the consumer first, or widen the filters)"
    header = (f"{'wallet':<44} {'band':<15} {'adjusted':>8} {'raw':>7} "
              f"{'conf':>6} {'gate':>6}  why")
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.wallet:<44} {row.band:<15} "
            f"{_num(row.adjusted):>8} {_num(row.raw_early_entry):>7} "
            f"{_num(row.confidence):>6} {'pass' if row.gate_passed else 'fail':>6}  "
            f"{row.gated_by or '-'}")
    bases = {row.band_basis for row in rows if row.band_basis}
    if len(bases) == 1:
        basis = bases.pop()
        lines.append("")
        lines.append(
            f"basis: {basis}" + (
                " - early-entry bands: 'is this wallet's early-entry ability worth "
                "tracking?', not a full-wallet verdict (D21)"
                if basis == "shrunk_early_entry" else ""))
    return "\n".join(lines)


def _num(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.2f}"
