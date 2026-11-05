-- 003_wallet_aggregates.sql — wallet-level aggregation of per-anchor metrics.
--
-- WHY A NEW TABLE AND NOT sub_scores
-- The recovered aggregation spec (docs/RECOVERED_FROM_TRANSCRIPT.md §7) assumed
-- sub_scores could carry these rows: (wallet, metric_name, rule_version, value,
-- status, anchor_count_computed, anchor_count_excluded, evidence). It cannot.
-- sub_scores as built in 001_init.sql is a *dimension* table: it hangs off
-- scores(id) via a NOT NULL score_id, its dimension column is CHECKed against the
-- eight scoring dimensions, and UNIQUE (score_id, dimension) is its whole key. A
-- wallet aggregate has no score run, no dimension and no score_id; storing one
-- there would require inventing a scores row that no consumer created, and the
-- dimension CHECK would have to be widened to a value that is not a dimension.
--
-- wallet_metrics is also wrong for this: it is one JSONB blob per (address, as_of,
-- window_start, window_end, pipeline_version), so per-metric status and per-metric
-- re-run idempotency are not expressible as constraints.
--
-- anchor_wallet_metrics is wrong in the other direction: it is per-(anchor, wallet),
-- NOT NULL anchor_id. An aggregate is across anchors by definition.
--
-- So: one row per (wallet, metric_name, rule_version). Re-running under the same
-- rule version rewrites the row; bumping the version adds one. That is the same
-- rule the anchor writers already follow, for the same reason — a re-run that
-- doubles the evidence silently weights one run twice.

CREATE TYPE wallet_aggregate_status AS ENUM (
  'computed',        -- a number exists
  'not_rankable',    -- something was computed, but not enough anchors to trust it
  'not_computable'   -- nothing was computed at all
);

CREATE TABLE wallet_aggregates (
  aggregate_id          BIGSERIAL PRIMARY KEY,
  wallet                TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  metric_name           anchor_metric_name NOT NULL,
  rule_version          TEXT NOT NULL,
  value                 NUMERIC(38,10),
  status                wallet_aggregate_status NOT NULL,
  anchor_count_computed INTEGER NOT NULL CHECK (anchor_count_computed >= 0),
  anchor_count_excluded INTEGER NOT NULL CHECK (anchor_count_excluded >= 0),
  -- Fraction of the wallet's anchor rows that produced a number. 0 when there
  -- were no rows at all, which reads correctly: no coverage, not full coverage.
  coverage              NUMERIC(6,4) NOT NULL CHECK (coverage >= 0 AND coverage <= 1),
  -- Per-anchor trace. Not prose: which anchor contributed, what it contributed,
  -- and why the excluded ones were excluded.
  evidence              JSONB NOT NULL DEFAULT '[]'::jsonb,
  computed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Re-run idempotency. The key deliberately includes rule_version: aggregates
  -- produced under different rules are not comparable, so a new version adds a
  -- row instead of rewriting history.
  CONSTRAINT wallet_aggregate_uniq UNIQUE (wallet, metric_name, rule_version),

  -- A computed aggregate has a value; a non-computed one does not. This is what
  -- keeps "not measured" distinguishable from "measured as 0.0" — the failure
  -- mode the per-anchor metrics already guard against.
  CONSTRAINT wallet_aggregate_value_present
    CHECK ((status = 'computed') = (value IS NOT NULL))
);

-- The consumer reads one wallet's latest aggregates per metric.
CREATE INDEX wallet_aggregates_wallet_idx
  ON wallet_aggregates (wallet, metric_name, computed_at DESC);
