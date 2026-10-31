"""Writers for the Phase 1b anchor registry (``schema/002_anchor_registry.sql``).

Three artefacts, three writers, one rule each: **a re-run must not create a second
row.** The retrospective pipeline is designed to be re-runnable over the same anchor
set (a rule version bump re-evaluates everything), so every write is an upsert on
the table's own key. Without that, a re-run doubles the evidence and any aggregate
over it silently weights one run twice.

Deliberate choices, all of which are auditable from the rows themselves:

* **Writes are one transaction each.** A caller that needs several writes atomic
  should pass a connection it manages; these helpers commit what they wrote rather
  than leaving half an ingest pending on a later exception.
* **A wallet row is created if missing.** Verdicts and metrics reference
  ``wallets(address)``, but a wallet can be evaluated before it is ever profiled.
  The stub records only that the address exists (chain 'solana', completeness
  'unknown'). It does not invent a first-seen time — that is profiling's job, and a
  guessed value there would be indistinguishable from a measured one later.
* **Anchor re-writes refresh, they do not re-admit.** Resolution improves (a deployer
  gets found, a genesis time arrives), so nullable fields are COALESCEd forward and
  never blanked by a later write that knows less. ``source`` and
  ``created_by_rule_version`` keep their first value: they describe *admission*, and
  an anchor that arrived from one source did not retroactively arrive from another.
* **Metric shape is checked here, not only by the database.** The schema CHECKs are
  the last line of defence; a Python-side error names the offending field instead of
  surfacing a constraint name nobody can read at 3am.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .quality_filter import RuleId, WalletVerdict

__all__ = [
    "ANCHOR_SOURCES",
    "METRIC_NAMES",
    "METRIC_STATUSES",
    "AnchorInput",
    "AnchorWriterError",
    "write_anchor",
    "write_metric",
    "write_verdict",
]

#: Values of ``anchor_source_kind``. Synthetic is representable so calibration
#: fixtures never have to masquerade as market data.
ANCHOR_SOURCES: frozenset[str] = frozenset({"user_supplied", "dex_screener", "synthetic"})

#: Values of ``anchor_metric_name`` — the three implemented early-entry metrics.
METRIC_NAMES: frozenset[str] = frozenset(
    {"max_subsequent_return", "buyer_rank_at_entry", "pct_entries_before_volume_spike"}
)

#: Values of ``anchor_metric_status``. ``computed`` is the only status allowed to
#: carry a value; the rest record *why* there is no value, which is the whole point.
METRIC_STATUSES: frozenset[str] = frozenset(
    {"computed", "INSUFFICIENT_WINDOW", "NO_OBSERVATIONS", "NOT_COMPUTABLE"}
)


class AnchorWriterError(ValueError):
    """A write was rejected before reaching the database.

    Raised for input the schema would also refuse, but with a message naming the
    field — and for one case the schema cannot express as cleanly: a metric whose
    status and value contradict each other.
    """


class _Connection(Protocol):
    """Minimal DB surface these writers need. Keeps them unit-testable."""

    def execute(self, sql: str, params: tuple = ...): ...  # pragma: no cover
    def commit(self) -> None: ...  # pragma: no cover


@dataclass(frozen=True)
class AnchorInput:
    """One anchor as it is admitted to the registry.

    ``genesis_time`` is the **mint-creation** block time (``create_v2`` /
    ``initialize_mint2``), never pool creation: ``swaps.token_age_seconds`` is pool
    age and substituting it here would make every anchor look minutes old and
    manufacture rejections (docs/PHASE1A_RECOVERY.md §2).

    Every optional field is optional because resolution is incremental: an anchor
    may be admitted by mint alone and have its deployer, genesis slot, genesis time
    and earliest observed buy filled in by later passes.
    """

    mint: str
    source: str
    created_by_rule_version: str
    genesis_slot: int | None = None
    genesis_time: _dt.datetime | None = None
    deployer: str | None = None
    first_buy_observed_at: _dt.datetime | None = None

    def __post_init__(self) -> None:
        if not self.mint:
            raise AnchorWriterError("AnchorInput.mint is required")
        if self.source not in ANCHOR_SOURCES:
            raise AnchorWriterError(
                f"AnchorInput.source {self.source!r} is not one of {sorted(ANCHOR_SOURCES)}"
            )
        if not self.created_by_rule_version:
            raise AnchorWriterError(
                "AnchorInput.created_by_rule_version is required — an anchor admitted "
                "under no named rule set cannot be re-evaluated later"
            )
        if self.genesis_slot is not None and self.genesis_slot <= 0:
            raise AnchorWriterError("AnchorInput.genesis_slot must be positive when known")


def _ensure_wallet(conn, wallet: str) -> None:
    """Create the minimal wallets row a verdict or metric needs to reference.

    ``ON CONFLICT DO NOTHING`` so a real, profiled wallet is never overwritten by a
    stub from the anchor pipeline — the stub is a floor, not a claim.

    The ``chains`` row is ensured first because ``wallets.chain_id`` references it:
    the registry is the first thing to run on a fresh database, before any ingest has
    seeded reference data, and a FK failure there would read as a bug in the anchor
    pipeline. Only id and display name are written; ``native_mint``/``native_decimals``
    stay NULL for the chain registry owner to fill, rather than duplicating Solana
    constants in a writer that has no business owning them.
    """
    if not wallet:
        raise AnchorWriterError("wallet is required")
    conn.execute(
        """
        INSERT INTO chains (id, display_name) VALUES ('solana', 'Solana')
        ON CONFLICT (id) DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO wallets (address, chain_id, history_completeness)
        VALUES (%s, 'solana', 'unknown')
        ON CONFLICT (address) DO NOTHING
        """,
        (wallet,),
    )


def write_anchor(conn, anchor: AnchorInput) -> int:
    """Insert an anchor, or refresh an existing one. Returns its ``anchor_id``.

    Idempotent on ``mint``: the second write produces no second row and returns the
    same id. Nullable fields are COALESCEd forward so a resolution pass that knows
    less than a previous one cannot erase what was already established. ``source``
    and ``created_by_rule_version`` keep their first value — they record how the
    anchor was admitted, and re-admission is not what a refresh is.
    """
    try:
        row = conn.execute(
            """
            INSERT INTO anchors (
                mint, genesis_slot, genesis_time, deployer, first_buy_observed_at,
                source, created_by_rule_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (mint) DO UPDATE SET
                genesis_slot          = COALESCE(EXCLUDED.genesis_slot, anchors.genesis_slot),
                genesis_time          = COALESCE(EXCLUDED.genesis_time, anchors.genesis_time),
                deployer              = COALESCE(EXCLUDED.deployer, anchors.deployer),
                first_buy_observed_at = COALESCE(EXCLUDED.first_buy_observed_at,
                                                 anchors.first_buy_observed_at)
            RETURNING anchor_id
            """,
            (
                anchor.mint,
                anchor.genesis_slot,
                anchor.genesis_time,
                anchor.deployer,
                anchor.first_buy_observed_at,
                anchor.source,
                anchor.created_by_rule_version,
            ),
        ).fetchone()
    except Exception:
        _rollback(conn)
        raise
    conn.commit()
    return int(row[0])


def write_verdict(
    conn,
    *,
    anchor_id: int,
    wallet: str,
    verdict: WalletVerdict | str,
    rule_version: str,
    rule_id: RuleId | str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    """Record one (anchor, wallet, rule_version) verdict, upserting on re-run.

    The key includes ``rule_version`` on purpose: verdicts produced under different
    thresholds are not comparable, so a new version adds a row instead of rewriting
    history. Re-evaluating under the *same* version rewrites the row, because the
    inputs changed or the earlier run was wrong — either way there is one answer per
    version, not two.

    ``evidence`` carries the measured values (per-anchor deltas, burst hits,
    thresholds), not prose: the 44/45 false-negative rate was undiagnosable because
    the numbers were thrown away.

    ``rule_id`` must be present exactly when the verdict is a rule-driven rejection;
    the schema CHECK enforces the same invariant, this raises a readable error first.
    """
    try:
        verdict_value = WalletVerdict(verdict).value
    except ValueError as exc:
        raise AnchorWriterError(f"unknown verdict {verdict!r}") from exc

    rule_value: str | None
    if rule_id is None:
        rule_value = None
    else:
        try:
            rule_value = RuleId(rule_id).value
        except ValueError as exc:
            raise AnchorWriterError(f"unknown rule_id {rule_id!r}") from exc

    if not rule_version:
        raise AnchorWriterError("rule_version is required — an unversioned verdict is unreadable")

    is_rejection = verdict_value == WalletVerdict.REJECT_GENESIS_BURST.value
    if is_rejection != (rule_value is not None):
        raise AnchorWriterError(
            f"verdict {verdict_value!r} with rule_id={rule_value!r}: a rejection must name "
            "its rule, and a non-rejection must not"
        )

    try:
        _ensure_wallet(conn, wallet)
        conn.execute(
            """
            INSERT INTO anchor_wallet_verdicts (
                anchor_id, wallet, verdict, rule_id, rule_version, evidence
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (anchor_id, wallet, rule_version) DO UPDATE SET
                verdict      = EXCLUDED.verdict,
                rule_id      = EXCLUDED.rule_id,
                evidence     = EXCLUDED.evidence,
                evaluated_at = now()
            """,
            (
                anchor_id,
                wallet,
                verdict_value,
                rule_value,
                rule_version,
                _json(evidence),
            ),
        )
    except Exception:
        _rollback(conn)
        raise
    conn.commit()


def write_metric(
    conn,
    *,
    anchor_id: int,
    wallet: str,
    metric_name: str,
    status: str,
    value: Any | None = None,
    window_days: int | None = None,
    peak_day: int | None = None,
) -> None:
    """Record one metric value, upserting on (anchor, wallet, metric, window).

    ``status`` is load-bearing. ``computed`` is the only status that carries a value;
    ``INSUFFICIENT_WINDOW`` (the entry is younger than the window) and
    ``NO_OBSERVATIONS`` (the window is mature and held no swap-derived price) and
    ``NOT_COMPUTABLE`` all record *why* there is no number. Storing 0.0 or NULL with
    no status would make "not measured" indistinguishable from "measured as zero",
    which is the mistake that turns a missing-data problem into a false behavioural
    signal.

    ``window_days`` is set for ``max_subsequent_return`` and must be NULL for the two
    unwindowed metrics. NULL is compared as a value by the unique constraint
    (``UNIQUE NULLS NOT DISTINCT``), so the second write for ``buyer_rank_at_entry``
    collides as intended rather than accumulating duplicates.
    """
    if metric_name not in METRIC_NAMES:
        raise AnchorWriterError(f"unknown metric {metric_name!r}")
    if status not in METRIC_STATUSES:
        raise AnchorWriterError(f"unknown metric status {status!r}")

    is_windowed = metric_name == "max_subsequent_return"
    if is_windowed != (window_days is not None):
        raise AnchorWriterError(
            f"{metric_name}: window_days must be set exactly for max_subsequent_return "
            f"(got {window_days!r})"
        )
    if window_days is not None and window_days <= 0:
        raise AnchorWriterError("window_days must be positive")

    if (status == "computed") != (value is not None):
        raise AnchorWriterError(
            f"{metric_name}: status {status!r} with value {value!r} — computed requires a "
            "value, every other status forbids one"
        )

    if peak_day is not None:
        if not is_windowed or peak_day < 0:
            raise AnchorWriterError(
                "peak_day applies only to max_subsequent_return and must be >= 0"
            )

    try:
        _ensure_wallet(conn, wallet)
        conn.execute(
            """
            INSERT INTO anchor_wallet_metrics (
                anchor_id, wallet, metric_name, window_days, value, status, peak_day
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (anchor_id, wallet, metric_name, window_days) DO UPDATE SET
                value       = EXCLUDED.value,
                status      = EXCLUDED.status,
                peak_day    = EXCLUDED.peak_day,
                computed_at = now()
            """,
            (anchor_id, wallet, metric_name, window_days, value, status, peak_day),
        )
    except Exception:
        _rollback(conn)
        raise
    conn.commit()


def _rollback(conn) -> None:
    """Leave the connection usable after a failed write.

    A constraint violation aborts the enclosing transaction, so without this the
    caller's *next* write fails with ``InFailedSqlTransaction`` and the original
    cause is buried. Best-effort: a connection too broken to roll back is already
    lost, and masking the real exception would hide why.
    """
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 - never mask the original failure
        pass


def _json(evidence: Mapping[str, Any] | None) -> str:
    """Serialise evidence for the JSONB column, defaulting to an empty object."""
    import json

    return json.dumps(dict(evidence or {}), default=str, sort_keys=True)
