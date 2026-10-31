-- SolScope anchor registry (PostgreSQL 15+)
-- ---------------------------------------------------------------------------
-- Phase 1b's retrospective anchor pipeline needs somewhere for its three
-- artefacts to land, because a verdict or a metric that lives only in a Python
-- object cannot be audited, re-measured, or compared across rule versions.
--
-- Design rules, all inherited from 001_init.sql:
--   1. Every row records WHEN it was decided (evaluated_at / computed_at), never
--      updated in place.
--   2. Every verdict carries its rule identity (rule_id + rule_version) and the
--      numbers that tripped it (evidence). The 44/45 false-negative rate was
--      undiagnosable precisely because the measured values were not kept.
--   3. Status enums reproduce the Python enums verbatim (dots included) so the
--      mapping between solscope/quality_filter.py, solscope/metrics.py and these
--      tables is lossless rather than a translation that can drift.
--   4. "Not computable" is its own value, never NULL and never folded into
--      accepted/rejected — a wallet that could not be evaluated has not passed.
--
-- DEVIATION FROM THE WRITTEN SPEC, and why
-- anchor_wallet_metrics was specified as "PK on (anchor_id, wallet, metric_name,
-- window_days)". PostgreSQL requires PRIMARY KEY columns to be NOT NULL, and
-- window_days is deliberately NULL for the two metrics that have no window
-- (buyer_rank_at_entry, pct_entries_before_volume_spike). A literal PK would
-- force either a sentinel 0 or a NOT NULL window that lies about the metric.
-- The specified uniqueness is therefore enforced as
--     UNIQUE NULLS NOT DISTINCT (anchor_id, wallet, metric_name, window_days)
-- with a surrogate metric_id primary key — the same pattern 001_init.sql already
-- uses (see validation_result_uniq), and the same PG15+ requirement. NULL is
-- compared as a value, so a second write without ON CONFLICT does collide, which
-- is what idempotency needs.
--
-- WALLET FOREIGN KEYS AND STUB ROWS
-- Both child tables reference wallets(address) ON DELETE CASCADE. A verdict can
-- exist before profiling has ever touched the wallet, so solscope/anchor_writer.py
-- creates a minimal wallets row (chain_id 'solana', completeness 'unknown') before
-- writing. That is the honest representation of "we know this address exists
-- because it bought an anchor; we know nothing else about it yet".
--
-- The anchors.mint column is NOT a foreign key to tokens(mint) on purpose: an
-- anchor is a SEED for ingestion, so it necessarily precedes the tokens row that
-- ingestion will create. A FK here would invert the pipeline's direction.
-- ---------------------------------------------------------------------------

BEGIN;

-- ===========================================================================
-- Enum types
-- ===========================================================================

-- Where an anchor came from. `synthetic` exists so the calibration sets in
-- tests/test_phase1b_filter.py are representable without pretending to be real
-- market data — a synthetic anchor in the registry must be identifiable as such.
CREATE TYPE anchor_source_kind AS ENUM (
  'user_supplied',     -- named by the operator
  'dex_screener',      -- discovered by screening (docs/PHASE1B_SCOPING.md Q8)
  'synthetic'          -- calibration fixture, never a market claim
);

-- Mirrors solscope/quality_filter.py :: WalletVerdict, value for value.
CREATE TYPE anchor_wallet_verdict_kind AS ENUM (
  'wallet.accepted',
  'wallet.reject_genesis_burst',
  'wallet.not_computable_insufficient_anchors',
  'wallet.not_computable_incomplete_buyer_set',
  'wallet.not_computable_no_genesis'
);

-- Mirrors solscope/quality_filter.py :: RuleId. Only rules that exist are here;
-- sandwich/throughput/router_only are absent on purpose (NOT_PORTED_HEURISTICS).
CREATE TYPE quality_rule_id AS ENUM (
  'rule.genesis_burst'
);

-- The three early-entry metrics implemented in solscope/metrics.py.
CREATE TYPE anchor_metric_name AS ENUM (
  'max_subsequent_return',
  'buyer_rank_at_entry',
  'pct_entries_before_volume_spike'
);

-- Mirrors the status strings solscope/metrics.py emits. INSUFFICIENT_WINDOW and
-- NO_OBSERVATIONS stay distinct: one says time has not passed, the other says
-- time passed and nothing traded.
CREATE TYPE anchor_metric_status AS ENUM (
  'computed',
  'INSUFFICIENT_WINDOW',
  'NO_OBSERVATIONS',
  'NOT_COMPUTABLE'
);

-- ===========================================================================
-- 1. Anchors
-- ===========================================================================

CREATE TABLE anchors (
  anchor_id                BIGSERIAL PRIMARY KEY,
  mint                     TEXT NOT NULL UNIQUE,        -- one registry row per token
  genesis_slot             BIGINT,                      -- mint-creation slot (§2 route)
  genesis_time             TIMESTAMPTZ,                 -- mint-creation block time, NOT pool creation
  deployer                 TEXT,                        -- base58; NULL when unresolved
  first_buy_observed_at    TIMESTAMPTZ,                 -- earliest buy we have evidence for
  source                   anchor_source_kind NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by_rule_version  TEXT NOT NULL,               -- rule set under which this anchor was admitted

  -- genesis_time may legitimately be NULL: the Genesis-burst rule then reports
  -- wallet.not_computable_no_genesis rather than guessing a creation time from
  -- pool creation (swaps.token_age_seconds is POOL age and must not stand in).
  -- No CHECK forces it non-NULL, and no CHECK invents a default. A wrong genesis
  -- time silently manufactures rejections; a NULL one is auditable.
  CONSTRAINT anchor_genesis_slot_positive CHECK (genesis_slot IS NULL OR genesis_slot > 0)
);

COMMENT ON COLUMN anchors.genesis_time IS
  'Mint-creation block time (create_v2 / initialize_mint2), never pool creation.';
COMMENT ON COLUMN anchors.mint IS
  'No FK to tokens(mint): an anchor is a seed that precedes ingestion.';

-- ===========================================================================
-- 2. Per-(anchor, wallet) verdicts
-- ===========================================================================

CREATE TABLE anchor_wallet_verdicts (
  anchor_id     BIGINT NOT NULL REFERENCES anchors(anchor_id) ON DELETE CASCADE,
  wallet        TEXT   NOT NULL REFERENCES wallets(address)  ON DELETE CASCADE,
  verdict       anchor_wallet_verdict_kind NOT NULL,
  rule_id       quality_rule_id,                    -- NULL unless a rule decided it
  rule_version  TEXT   NOT NULL,                    -- e.g. 'genesis_burst/v1'
  evaluated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  evidence      JSONB  NOT NULL DEFAULT '{}'::jsonb, -- measured values, not prose

  -- Idempotency key. Re-running the same rule version over the same anchor
  -- rewrites the row instead of accumulating one per run; a NEW rule version
  -- adds a row, because verdicts from different thresholds are not comparable
  -- and must not overwrite each other.
  PRIMARY KEY (anchor_id, wallet, rule_version),

  -- A rejection is only meaningful if it names the rule that rejected. The
  -- converse also holds: naming a rule on an ACCEPTED or NOT_COMPUTABLE row
  -- would imply the rule fired when it did not.
  CONSTRAINT verdict_rule_consistency
    CHECK ((verdict = 'wallet.reject_genesis_burst') = (rule_id IS NOT NULL))
);

CREATE INDEX anchor_wallet_verdicts_wallet_idx
  ON anchor_wallet_verdicts (wallet);

-- ===========================================================================
-- 3. Per-(anchor, wallet, metric[, window]) values
-- ===========================================================================

CREATE TABLE anchor_wallet_metrics (
  metric_id     BIGSERIAL PRIMARY KEY,
  anchor_id     BIGINT NOT NULL REFERENCES anchors(anchor_id) ON DELETE CASCADE,
  wallet        TEXT   NOT NULL REFERENCES wallets(address)  ON DELETE CASCADE,
  metric_name   anchor_metric_name NOT NULL,
  window_days   INTEGER,                            -- NULL unless max_subsequent_return
  value         NUMERIC(38,10),                     -- NULL unless status='computed'
  status        anchor_metric_status NOT NULL,
  peak_day      INTEGER,                            -- integer day-of-peak from entry
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- The specified uniqueness, with NULL window_days compared as a value.
  CONSTRAINT anchor_metric_uniq
    UNIQUE NULLS NOT DISTINCT (anchor_id, wallet, metric_name, window_days),

  -- Only max_subsequent_return is windowed. A buyer rank carrying window_days
  -- would be a row whose key claims something the metric does not mean.
  CONSTRAINT anchor_metric_window_scope
    CHECK ((metric_name = 'max_subsequent_return') = (window_days IS NOT NULL)),

  CONSTRAINT anchor_metric_window_positive
    CHECK (window_days IS NULL OR window_days > 0),

  -- A computed metric has a value; a non-computed one does not. This is what
  -- makes INSUFFICIENT_WINDOW distinguishable from a measured 0.0.
  CONSTRAINT anchor_metric_value_present
    CHECK ((status = 'computed') = (value IS NOT NULL)),

  CONSTRAINT anchor_metric_peak_scope
    CHECK (peak_day IS NULL
           OR (metric_name = 'max_subsequent_return' AND peak_day >= 0))
);

CREATE INDEX anchor_wallet_metrics_wallet_idx
  ON anchor_wallet_metrics (wallet);

-- The Phase 1b scoring consumer reads every metric for one anchor at a time.
CREATE INDEX anchor_wallet_metrics_anchor_idx
  ON anchor_wallet_metrics (anchor_id, metric_name);

COMMIT;
