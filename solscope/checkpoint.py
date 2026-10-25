"""Restart-safe job checkpoints backed by ``job_checkpoints`` (schema 001).

The contract the schema states: high-water marks **advance only after
successful ingestion**. Advancing before the work is durable turns a crash
into a silent gap; advancing after means a crash replays a small, known window
— which is exactly what the overlap is for.

The overlap window is the mechanism behind the Phase 1 recovery exit criterion
(doc 07): on restart, re-fetch from ``last_slot - overlap_slots`` forward and
let archive-level dedupe (``raw_payloads`` unique index + O_EXCL files)
collapse the replayed overlap into zero duplicate events. No gaps because the
window starts before the last confirmed position; no duplicates because
dedupe, not because the fetch is assumed exact.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Checkpoint:
    job: str
    partition_key: str
    last_slot: int | None
    last_event_time: _dt.datetime | None
    last_event_id: str | None
    last_success_at: _dt.datetime | None
    overlap_slots: int


class CheckpointStore:
    """Thin, unit-testable facade over job_checkpoints."""

    def __init__(self, conn, default_overlap: int = 64) -> None:
        self._conn = conn
        self._overlap = default_overlap

    def get(self, job: str, partition_key: str = "") -> Checkpoint | None:
        row = self._conn.execute(
            "SELECT job, partition_key, last_slot, last_event_time, last_event_id, "
            "       last_success_at, overlap_slots "
            "FROM job_checkpoints WHERE job = %s AND partition_key = %s",
            (job, partition_key),
        ).fetchone()
        if not row:
            return None
        return Checkpoint(*row)

    def advance(self, *, job: str, partition_key: str = "",
                last_slot: int | None = None,
                last_event_time: _dt.datetime | None = None,
                last_event_id: str | None = None,
                state: dict | None = None) -> Checkpoint:
        """Advance the high-water mark. Call ONLY after the data is durable."""
        self._conn.execute(
            "INSERT INTO job_checkpoints "
            "  (job, partition_key, last_slot, last_event_time, last_event_id, "
            "   last_success_at, overlap_slots, state) "
            "VALUES (%s, %s, %s, %s, %s, now(), %s, %s) "
            "ON CONFLICT (job, partition_key) DO UPDATE SET "
            "  last_slot        = COALESCE(EXCLUDED.last_slot, job_checkpoints.last_slot), "
            "  last_event_time  = COALESCE(EXCLUDED.last_event_time, job_checkpoints.last_event_time), "
            "  last_event_id    = COALESCE(EXCLUDED.last_event_id, job_checkpoints.last_event_id), "
            "  last_success_at  = now(), "
            "  state            = COALESCE(EXCLUDED.state, job_checkpoints.state)",
            (job, partition_key, last_slot, last_event_time, last_event_id,
             self._overlap, state),
        )
        self._conn.commit()
        got = self.get(job, partition_key)
        assert got is not None
        return got

    def recovery_window(self, *, job: str, partition_key: str = "",
                        now_slot: int) -> tuple[int, int]:
        """(from_slot, to_slot) to re-fetch on restart: last confirmed position
        minus the overlap, up to now. A cold start returns (now_slot, now_slot)
        — nothing to replay, but the caller still gets a valid window.
        """
        cp = self.get(job, partition_key)
        if cp is None or cp.last_slot is None:
            return (now_slot, now_slot)
        return (max(0, cp.last_slot - cp.overlap_slots), now_slot)

    def time_window(self, *, job: str, partition_key: str = "",
                    now: _dt.datetime) -> tuple[_dt.datetime | None, _dt.datetime]:
        """Time-based recovery window for feeds without slot granularity."""
        cp = self.get(job, partition_key)
        if cp is None or cp.last_event_time is None:
            return (None, now)
        return (cp.last_event_time, now)
