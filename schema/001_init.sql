-- SolScope initial schema (PostgreSQL 15+)
-- ---------------------------------------------------------------------------
-- Design rules encoded here:
--   1. Never store a token amount as a float. Raw integer units + decimals, always.
--   2. Every event carries BOTH event_time (block/slot time) and ingest_time (when we
--      learned it). Point-in-time analysis filters on both.
--   3. Every derived value carries a provenance enum and a nullable reason.
--      "unknown" is NULL + reason, never 0.
--   4. Scores are immutable snapshots keyed by (wallet, as_of, score_run_id).
--      Scores are never updated in place.
--   5. Raw provider payloads are retained and referenced, never overwritten.
--
-- REQUIRES PostgreSQL 15+ (for UNIQUE NULLS NOT DISTINCT).
-- Verified to apply cleanly on PostgreSQL 16 and to reject the invalid inputs it should;
-- see docs/04-data-model.md for the verified test results.
-- ---------------------------------------------------------------------------

BEGIN;

-- ===========================================================================
-- 0. Reference / enum types
-- ===========================================================================

CREATE TYPE provenance_kind AS ENUM (
  'swap_derived',      -- computed from the swap's own pool reserves (highest trust)
  'pool_ohlcv',        -- interpolated from a candle series
  'snapshot_api',      -- current-state API snapshot, only valid at fetch time
  'provider_parsed',   -- provider's own decode
  'manual'             -- human entry
);

CREATE TYPE completeness_kind AS ENUM (
  'complete',          -- full history within the analysis window
  'partial',           -- known gaps
  'truncated',         -- credit budget or provider limit cut it short
  'unknown'
);

CREATE TYPE recommendation_kind AS ENUM (
  'track', 'watch', 'investigate', 'deprioritize', 'exclude'
);

CREATE TYPE alert_kind AS ENUM (
  'new_token_buy', 'cluster_co_buy', 'position_exit',
  'performance_change', 'behavior_change', 'token_deterioration', 'data_quality'
);

CREATE TYPE subscription_kind AS ENUM (
  'poll', 'websocket', 'webhook', 'backfill'
);

-- ===========================================================================
-- 1. Chains and tokens
-- ===========================================================================

CREATE TABLE chains (
  id                TEXT PRIMARY KEY,           -- 'solana'
  display_name      TEXT NOT NULL,
  native_mint       TEXT,
  native_decimals   SMALLINT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Provider-agnostic token registry. Metadata is cached, never authoritative.
CREATE TABLE tokens (
  mint              TEXT PRIMARY KEY,           -- base58 mint address
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  symbol            TEXT,
  name              TEXT,
  decimals          SMALLINT,                   -- NULL until resolved
  mint_authority    TEXT,                       -- non-null => supply is mutable
  freeze_authority  TEXT,
  is_mintable       BOOLEAN,
  launchpad         TEXT,                       -- 'pump_fun', 'raydium_launchlab', ...
  created_slot      BIGINT,                     -- slot of the pool/first trade
  created_at        TIMESTAMPTZ,                -- block time of the launch event
  metadata_uri      TEXT,
  metadata_json     JSONB,
  metadata_source   provenance_kind,
  metadata_fetched_at TIMESTAMPTZ,
  metadata_completeness completeness_kind NOT NULL DEFAULT 'unknown',
  first_seen_ingest_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT tokens_chain_mint_uniq UNIQUE (chain_id, mint)
);

CREATE INDEX tokens_created_at_idx  ON tokens (created_at DESC);
CREATE INDEX tokens_launchpad_idx   ON tokens (launchpad, created_at DESC);

-- Supply changes as events, because mcap = price * supply and supply can move.
CREATE TABLE token_supply_events (
  id                BIGSERIAL PRIMARY KEY,
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  mint              TEXT NOT NULL REFERENCES tokens(mint) ON DELETE CASCADE,
  supply_raw        NUMERIC(38,0) NOT NULL,     -- raw integer units
  decimals          SMALLINT NOT NULL,
  event_slot        BIGINT,
  event_time        TIMESTAMPTZ NOT NULL,
  ingest_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
  provenance        provenance_kind NOT NULL,
  source            TEXT NOT NULL,
  raw_payload_id    BIGINT,
  CONSTRAINT token_supply_event_uniq UNIQUE (mint, event_time, supply_raw)
);

CREATE INDEX token_supply_events_lookup_idx
  ON token_supply_events (mint, event_time DESC);

-- ===========================================================================
-- 2. Pools  (a token may trade in several pools concurrently)
-- ===========================================================================

CREATE TABLE pools (
  pool_address      TEXT PRIMARY KEY,
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  dex               TEXT NOT NULL,               -- 'pump_fun', 'raydium_amm_v4', 'pumpswap'
  pool_type         TEXT,                        -- 'bonding_curve', 'constant_product', ...
  base_mint         TEXT NOT NULL REFERENCES tokens(mint),
  quote_mint        TEXT NOT NULL,
  quote_is_native   BOOLEAN NOT NULL DEFAULT false,
  created_slot      BIGINT,
  created_at        TIMESTAMPTZ,
  migrated_at       TIMESTAMPTZ,                 -- bonding curve -> AMM
  migrated_to_pool  TEXT REFERENCES pools(pool_address),
  is_active         BOOLEAN NOT NULL DEFAULT true,
  last_seen_at      TIMESTAMPTZ,
  first_seen_ingest_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX pools_base_mint_idx ON pools (base_mint, created_at DESC);
CREATE INDEX pools_created_at_idx ON pools (created_at DESC);

-- Reserve state is needed for point-in-time price. One row per observed state change.
CREATE TABLE pool_reserve_snapshots (
  id                BIGSERIAL PRIMARY KEY,
  pool_address      TEXT NOT NULL REFERENCES pools(pool_address) ON DELETE CASCADE,
  slot              BIGINT NOT NULL,
  event_time        TIMESTAMPTZ NOT NULL,
  ingest_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
  base_reserve_raw  NUMERIC(38,0) NOT NULL,
  quote_reserve_raw NUMERIC(38,0) NOT NULL,
  base_decimals     SMALLINT NOT NULL,
  quote_decimals    SMALLINT NOT NULL,
  lp_supply_raw     NUMERIC(38,0),
  provenance        provenance_kind NOT NULL,
  source            TEXT NOT NULL,
  raw_payload_id    BIGINT,
  CONSTRAINT pool_reserve_snapshot_uniq UNIQUE (pool_address, slot)
);

CREATE INDEX pool_reserve_snapshots_lookup_idx
  ON pool_reserve_snapshots (pool_address, event_time DESC);

-- Current-state snapshot from keyless APIs. Only meaningful at fetched_at.
CREATE TABLE pool_snapshots (
  id                BIGSERIAL PRIMARY KEY,
  pool_address      TEXT NOT NULL REFERENCES pools(pool_address) ON DELETE CASCADE,
  fetched_at        TIMESTAMPTZ NOT NULL,
  price_usd         NUMERIC(38,18),
  liquidity_usd     NUMERIC(38,2),
  fdv_usd           NUMERIC(38,2),
  mcap_usd          NUMERIC(38,2),
  volume_24h_usd    NUMERIC(38,2),
  buys_24h          INTEGER,
  sells_24h         INTEGER,
  source            TEXT NOT NULL,
  raw_payload_id    BIGINT,
  CONSTRAINT pool_snapshot_uniq UNIQUE (pool_address, fetched_at, source)
);

-- ===========================================================================
-- 3. Wallets and clusters
-- ===========================================================================

CREATE TABLE wallets (
  address           TEXT PRIMARY KEY,            -- base58 pubkey. NOT an identity.
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  first_seen_at     TIMESTAMPTZ,                 -- earliest observed activity
  first_seen_ingest_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_activity_at  TIMESTAMPTZ,
  tx_count_observed BIGINT NOT NULL DEFAULT 0,
  history_completeness completeness_kind NOT NULL DEFAULT 'unknown',
  history_window_start TIMESTAMPTZ,
  history_window_end   TIMESTAMPTZ,
  -- Detected infrastructure fingerprints (bots, market makers, routers)
  is_likely_bot     BOOLEAN NOT NULL DEFAULT false,
  is_likely_infra   BOOLEAN NOT NULL DEFAULT false,
  exclusion_reason  TEXT,
  notes             TEXT
);

CREATE INDEX wallets_last_activity_idx ON wallets (last_activity_at DESC NULLS LAST);

-- Third-party labels are WEAK evidence. Modelled separately from our own conclusions.
CREATE TABLE wallet_labels (
  id                BIGSERIAL PRIMARY KEY,
  address           TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  label             TEXT NOT NULL,
  label_source      TEXT NOT NULL,
  confidence        SMALLINT CHECK (confidence BETWEEN 0 AND 100),
  observed_at       TIMESTAMPTZ NOT NULL,
  raw_payload_id    BIGINT,
  CONSTRAINT wallet_label_uniq UNIQUE (address, label, label_source, observed_at)
);

-- Wallets that may share an owner. CLUSTERING IS EVIDENCE OF COORDINATION, NOT PROOF
-- OF OWNERSHIP. Members of a cluster must never be counted as independent signals.
CREATE TABLE wallet_clusters (
  cluster_id        BIGSERIAL PRIMARY KEY,
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  method            TEXT NOT NULL,               -- 'common_funder', 'co_purchase', 'same_slot', 'jito_bundle'
  confidence        SMALLINT CHECK (confidence BETWEEN 0 AND 100),
  evidence          JSONB NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  superseded_by     BIGINT REFERENCES wallet_clusters(cluster_id)
);

CREATE TABLE wallet_cluster_members (
  cluster_id        BIGINT NOT NULL REFERENCES wallet_clusters(cluster_id) ON DELETE CASCADE,
  address           TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  role              TEXT,                        -- 'funder', 'operator', 'member'
  added_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (cluster_id, address)
);

-- Entity-level funding links, the raw material for clustering.
CREATE TABLE wallet_funding_links (
  id                BIGSERIAL PRIMARY KEY,
  child_address     TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  funder_address    TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  first_funding_sig TEXT,
  first_funding_at  TIMESTAMPTZ,
  amount_raw        NUMERIC(38,0),
  decimals          SMALLINT,
  funder_is_exchange BOOLEAN NOT NULL DEFAULT false,  -- MUST be excluded from clustering
  evidence          JSONB,
  CONSTRAINT wallet_funding_link_uniq UNIQUE NULLS NOT DISTINCT (child_address, funder_address, first_funding_sig)
);

CREATE INDEX wallet_funding_links_funder_idx ON wallet_funding_links (funder_address);

-- ===========================================================================
-- 4. Raw payload archive (append-only; everything derived is recomputable from this)
-- ===========================================================================

CREATE TABLE raw_payloads (
  id                BIGSERIAL PRIMARY KEY,
  source            TEXT NOT NULL,               -- 'helius', 'dexscreener', ...
  endpoint          TEXT NOT NULL,
  request_key       TEXT,                        -- normalised request fingerprint
  fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  http_status       INTEGER,
  credits_spent     INTEGER NOT NULL DEFAULT 0,
  body_sha256       BYTEA NOT NULL,              -- dedupe / integrity
  storage_path      TEXT NOT NULL,               -- path under data/raw/
  byte_size         INTEGER,
  CONSTRAINT raw_payload_dedupe UNIQUE (source, endpoint, body_sha256)
);

CREATE INDEX raw_payloads_fetched_idx ON raw_payloads (fetched_at DESC);

-- ===========================================================================
-- 5. Transactions and decoded events
-- ===========================================================================

CREATE TABLE transactions (
  signature         TEXT PRIMARY KEY,
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  slot              BIGINT NOT NULL,
  block_time        TIMESTAMPTZ,                 -- NULL if provider omits it
  ingest_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
  err               BOOLEAN NOT NULL DEFAULT false,   -- failed transactions
  fee_lamports      BIGINT,
  priority_fee_lamports BIGINT,
  jito_tip_lamports BIGINT,
  compute_units     BIGINT,
  signer            TEXT,
  fee_payer         TEXT,
  is_vote           BOOLEAN NOT NULL DEFAULT false,
  commitment        TEXT,                        -- 'finalized', 'confirmed', 'processed'
  bundle_id         TEXT,                        -- Jito bundle, when observable
  raw_payload_id    BIGINT REFERENCES raw_payloads(id),
  decoded_at        TIMESTAMPTZ
);

CREATE INDEX transactions_slot_idx      ON transactions (slot);
CREATE INDEX transactions_block_time_idx ON transactions (block_time DESC);
CREATE INDEX transactions_signer_idx    ON transactions (signer, block_time DESC);
CREATE INDEX transactions_bundle_idx    ON transactions (bundle_id) WHERE bundle_id IS NOT NULL;

-- Decoded swaps. Every price/mcap in the system originates from a row here.
CREATE TABLE swaps (
  id                BIGSERIAL PRIMARY KEY,
  signature         TEXT NOT NULL REFERENCES transactions(signature) ON DELETE CASCADE,
  instruction_index SMALLINT NOT NULL,
  inner_index       SMALLINT,                    -- NULL for top-level
  slot              BIGINT NOT NULL,
  event_time        TIMESTAMPTZ,
  ingest_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
  pool_address      TEXT REFERENCES pools(pool_address),
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  trader            TEXT NOT NULL REFERENCES wallets(address),
  base_mint         TEXT NOT NULL REFERENCES tokens(mint),
  quote_mint        TEXT NOT NULL,
  side              TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  base_amount_raw   NUMERIC(38,0) NOT NULL,
  quote_amount_raw  NUMERIC(38,0) NOT NULL,
  base_decimals     SMALLINT NOT NULL,
  quote_decimals    SMALLINT NOT NULL,
  -- Prices. exec_* is what the trader actually got; pool_* is the reserve ratio.
  exec_price_quote  NUMERIC(38,18) NOT NULL,     -- quote per base, slippage included
  exec_price_usd    NUMERIC(38,18),
  pool_price_quote  NUMERIC(38,18),
  pool_price_usd    NUMERIC(38,18),
  price_provenance  provenance_kind NOT NULL,
  price_usd_source  TEXT,                        -- how quote->USD was obtained
  slippage_bps      NUMERIC(12,4),
  liquidity_usd     NUMERIC(38,2),
  mcap_usd          NUMERIC(38,2),               -- price * circulating supply, as of this slot
  mcap_provenance   provenance_kind,
  token_age_seconds BIGINT,                      -- event_time - pool.created_at
  fee_lamports      BIGINT,
  is_failed         BOOLEAN NOT NULL DEFAULT false,
  decoder_version   TEXT NOT NULL,
  raw_payload_id    BIGINT REFERENCES raw_payloads(id),
  -- NULLS NOT DISTINCT is REQUIRED: inner_index is NULL for top-level swaps, and plain
  -- UNIQUE treats NULLs as distinct, so the constraint would silently admit duplicates
  -- for the common case. Verified by test; see docs/04-data-model.md.
  CONSTRAINT swaps_event_uniq UNIQUE NULLS NOT DISTINCT (signature, instruction_index, inner_index)
);

CREATE INDEX swaps_trader_time_idx ON swaps (trader, event_time DESC);
CREATE INDEX swaps_pool_time_idx   ON swaps (pool_address, event_time DESC);
CREATE INDEX swaps_token_time_idx  ON swaps (base_mint, event_time DESC);
CREATE INDEX swaps_time_idx        ON swaps (event_time DESC);
-- The query every early-entry calculation makes: first buy per wallet per token.
CREATE INDEX swaps_wallet_token_idx ON swaps (trader, base_mint, event_time);

-- Non-swap movements. CRITICAL: these must never be counted as purchases at zero cost.
CREATE TABLE token_transfers (
  id                BIGSERIAL PRIMARY KEY,
  signature         TEXT NOT NULL,
  instruction_index SMALLINT NOT NULL,
  inner_index       SMALLINT,
  slot              BIGINT NOT NULL,
  event_time        TIMESTAMPTZ,
  ingest_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
  mint              TEXT NOT NULL,
  from_address      TEXT,
  to_address        TEXT,
  amount_raw        NUMERIC(38,0) NOT NULL,
  decimals          SMALLINT NOT NULL,
  transfer_kind     TEXT NOT NULL CHECK (transfer_kind IN
                       ('spl_transfer','sol_transfer','airdrop_claim','mint','burn')),
  raw_payload_id    BIGINT REFERENCES raw_payloads(id),
  CONSTRAINT token_transfer_uniq UNIQUE NULLS NOT DISTINCT (signature, instruction_index, inner_index)
);

CREATE INDEX token_transfers_to_idx   ON token_transfers (to_address, event_time DESC);
CREATE INDEX token_transfers_from_idx ON token_transfers (from_address, event_time DESC);
CREATE INDEX token_transfers_mint_idx ON token_transfers (mint, event_time DESC);

-- ===========================================================================
-- 6. Positions and P&L
-- ===========================================================================

-- A position is one wallet's holding in one token. FIFO consumption across swaps.
CREATE TABLE positions (
  position_id       BIGSERIAL PRIMARY KEY,
  address           TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  mint              TEXT NOT NULL REFERENCES tokens(mint),
  pool_address      TEXT REFERENCES pools(pool_address),
  opened_at         TIMESTAMPTZ NOT NULL,
  opened_slot       BIGINT,
  closed_at         TIMESTAMPTZ,                 -- NULL while open
  is_closed         BOOLEAN NOT NULL DEFAULT false,
  -- Cost basis of everything acquired via a swap (transfers excluded by design).
  total_buy_qty_raw NUMERIC(38,0) NOT NULL DEFAULT 0,
  total_sell_qty_raw NUMERIC(38,0) NOT NULL DEFAULT 0,
  open_qty_raw      NUMERIC(38,0) NOT NULL DEFAULT 0,
  cost_basis_lamports NUMERIC(38,0) NOT NULL DEFAULT 0,
  proceeds_lamports NUMERIC(38,0) NOT NULL DEFAULT 0,
  fees_lamports     NUMERIC(38,0) NOT NULL DEFAULT 0,
  realized_pnl_lamports NUMERIC(38,0),           -- NULL if basis incomplete
  realized_pnl_usd  NUMERIC(38,2),
  unrealized_pnl_usd NUMERIC(38,2),
  unrealized_price_usd NUMERIC(38,18),
  unrealized_as_of  TIMESTAMPTZ,
  roi                 NUMERIC(20,10),            -- realized / cost basis
  hold_seconds        BIGINT,
  -- Early-entry facts, captured at first buy
  entry_price_usd     NUMERIC(38,18),
  entry_mcap_usd      NUMERIC(38,2),
  entry_liquidity_usd NUMERIC(38,2),
  entry_token_age_seconds BIGINT,
  entry_slot          BIGINT,
  -- Forward outcome, fixed windows and point-in-time safe
  max_price_1h_usd    NUMERIC(38,18),
  max_price_6h_usd    NUMERIC(38,18),
  max_price_24h_usd   NUMERIC(38,18),
  max_price_7d_usd    NUMERIC(38,18),
  max_price_30d_usd   NUMERIC(38,18),
  max_multiple_30d    NUMERIC(20,10),
  max_drawdown_pct    NUMERIC(20,10),
  -- Realistic exit accounting
  exit_impact_bps     NUMERIC(12,4),             -- slippage the wallet itself would have hit
  realized_capture    NUMERIC(20,10),            -- realized return / max favorable return
  -- Provenance
  inventory_origin    TEXT NOT NULL DEFAULT 'swap_only'
                        CHECK (inventory_origin IN ('swap_only','includes_transfer_in','includes_airdrop')),
  transfer_in_qty_raw NUMERIC(38,0) NOT NULL DEFAULT 0,
  pnl_completeness    completeness_kind NOT NULL DEFAULT 'unknown',
  pnl_limitation      TEXT,                      -- human-readable reason, when not complete
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A wallet may accumulate many closed position episodes in a token over time, but only
-- ONE may be open at once. Without this, a second concurrent position would double-count
-- inventory and corrupt P&L. Partial index is the correct primitive here; a table-level
-- UNIQUE on (address, mint, opened_at) would have permitted concurrent open positions.
CREATE UNIQUE INDEX positions_one_open_per_wallet_token
  ON positions (address, mint) WHERE NOT is_closed;

CREATE INDEX positions_wallet_idx     ON positions (address, opened_at DESC);
CREATE INDEX positions_mint_idx       ON positions (mint, opened_at);
CREATE INDEX positions_closed_idx     ON positions (is_closed, closed_at DESC);
CREATE INDEX positions_early_entry_idx ON positions (entry_token_age_seconds)
  WHERE entry_token_age_seconds IS NOT NULL;

-- FIFO consumption records. Each row links one sell against one buy lot.
CREATE TABLE lot_matches (
  id                BIGSERIAL PRIMARY KEY,
  position_id       BIGINT NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE,
  buy_swap_id       BIGINT NOT NULL REFERENCES swaps(id),
  sell_swap_id      BIGINT NOT NULL REFERENCES swaps(id),
  matched_qty_raw   NUMERIC(38,0) NOT NULL,
  cost_lamports     NUMERIC(38,0) NOT NULL,
  proceeds_lamports NUMERIC(38,0) NOT NULL,
  fee_lamports      NUMERIC(38,0) NOT NULL DEFAULT 0,
  pnl_lamports      NUMERIC(38,0) NOT NULL,
  hold_seconds      BIGINT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT lot_match_uniq UNIQUE (buy_swap_id, sell_swap_id, matched_qty_raw)
);

CREATE INDEX lot_matches_position_idx ON lot_matches (position_id);

-- Realized equity curve, ordered by exit time. Source for drawdown.
CREATE TABLE equity_snapshots (
  id                BIGSERIAL PRIMARY KEY,
  address           TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  as_of             TIMESTAMPTZ NOT NULL,
  realized_pnl_usd  NUMERIC(38,2) NOT NULL,
  unrealized_pnl_usd NUMERIC(38,2),
  marked_value_usd  NUMERIC(38,2),               -- MTM value of open positions
  open_positions    INTEGER NOT NULL DEFAULT 0,
  -- NOTE: mark-to-market, NOT realized-only. A wallet that never sells losses must not
  -- appear to have zero drawdown.
  peak_value_usd    NUMERIC(38,2),
  drawdown_pct      NUMERIC(20,10),
  CONSTRAINT equity_snapshot_uniq UNIQUE (address, as_of)
);

CREATE INDEX equity_snapshots_addr_idx ON equity_snapshots (address, as_of DESC);

-- ===========================================================================
-- 7. Derived metrics
-- ===========================================================================

-- Metric catalogue: definitions live in the database so reports can cite them.
CREATE TABLE metric_definitions (
  metric_key        TEXT PRIMARY KEY,
  display_name      TEXT NOT NULL,
  description       TEXT NOT NULL,
  formula           TEXT NOT NULL,
  unit              TEXT,
  required_inputs   TEXT[] NOT NULL,
  -- Enforces the no-double-counting rule: each metric belongs to exactly one sub-score.
  owning_subscore   TEXT NOT NULL CHECK (owning_subscore IN
                      ('early_entry','profitability','repeatability','risk',
                       'token_selection','exit_effectiveness','integrity','confidence')),
  min_sample        INTEGER,
  known_limitations TEXT,
  version           INTEGER NOT NULL DEFAULT 1
);

-- Immutable metric snapshots. Keyed by as_of so history is preserved.
CREATE TABLE wallet_metrics (
  id                BIGSERIAL PRIMARY KEY,
  address           TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  as_of             TIMESTAMPTZ NOT NULL,
  window_start      TIMESTAMPTZ NOT NULL,
  window_end        TIMESTAMPTZ NOT NULL,
  metrics           JSONB NOT NULL,               -- metric_key -> value
  metric_quality    JSONB NOT NULL,               -- metric_key -> {completeness, reason, provenance}
  sample_sizes      JSONB NOT NULL,               -- metric_key -> n
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  pipeline_version  TEXT NOT NULL,
  CONSTRAINT wallet_metrics_uniq UNIQUE (address, as_of, window_start, window_end, pipeline_version)
);

CREATE INDEX wallet_metrics_asof_idx ON wallet_metrics (as_of DESC);

-- Cohort benchmarks: what a wallet is compared against. Essential for cross-period
-- comparability and for removing "everything pumped that week" effects.
CREATE TABLE cohort_benchmarks (
  id                BIGSERIAL PRIMARY KEY,
  cohort_key        TEXT NOT NULL,               -- e.g. 'launch_week:2026-W14'
  cohort_kind       TEXT NOT NULL CHECK (cohort_kind IN
                      ('launch_week','liquidity_band','mcap_band','all_tokens','wallet_median')),
  metric_key        TEXT NOT NULL,
  window_days       INTEGER NOT NULL,
  n_samples         INTEGER NOT NULL,
  value_mean        NUMERIC(38,10),
  value_median      NUMERIC(38,10),
  value_p10         NUMERIC(38,10),
  value_p25         NUMERIC(38,10),
  value_p75         NUMERIC(38,10),
  value_p90         NUMERIC(38,10),
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Point-in-time safety: a benchmark is only usable for scores with as_of >= available_at.
  available_at      TIMESTAMPTZ NOT NULL,
  CONSTRAINT cohort_benchmark_uniq UNIQUE (cohort_key, metric_key, window_days, available_at)
);

CREATE INDEX cohort_benchmarks_lookup_idx ON cohort_benchmarks (metric_key, cohort_key);

-- ===========================================================================
-- 8. Scoring
-- ===========================================================================

-- Frozen, versioned scoring configuration. Weights are never edited in place.
CREATE TABLE score_config_versions (
  config_version    TEXT PRIMARY KEY,           -- e.g. 'v1.0.0'
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  frozen_at         TIMESTAMPTZ,                -- set before any validation run
  weights           JSONB NOT NULL,
  parameters        JSONB NOT NULL,             -- shrinkage k, thresholds, caps, ...
  config_sha256     BYTEA NOT NULL,
  notes             TEXT,
  is_frozen         BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE score_runs (
  score_run_id      BIGSERIAL PRIMARY KEY,
  config_version    TEXT NOT NULL REFERENCES score_config_versions(config_version),
  as_of             TIMESTAMPTZ NOT NULL,        -- point-in-time cutoff
  window_start      TIMESTAMPTZ NOT NULL,
  window_end        TIMESTAMPTZ NOT NULL,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ,
  wallets_scored    INTEGER,
  pipeline_version  TEXT NOT NULL,
  is_experiment     BOOLEAN NOT NULL DEFAULT false,
  experiment_label  TEXT,                        -- e.g. 'discovery', 'validation-holdout'
  CONSTRAINT score_run_uniq UNIQUE (config_version, as_of, window_start, window_end, experiment_label)
);

CREATE TABLE scores (
  id                BIGSERIAL PRIMARY KEY,
  score_run_id      BIGINT NOT NULL REFERENCES score_runs(score_run_id) ON DELETE CASCADE,
  address           TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  as_of             TIMESTAMPTZ NOT NULL,
  overall_score     NUMERIC(6,2) NOT NULL CHECK (overall_score BETWEEN 0 AND 100),
  confidence_score  NUMERIC(6,2) NOT NULL CHECK (confidence_score BETWEEN 0 AND 100),
  -- Ranking value. Pulls low-evidence wallets toward 50 rather than rewarding or
  -- punishing them: adjusted = 50 + (score - 50) * confidence/100
  confidence_adjusted_score NUMERIC(6,2) NOT NULL,
  band              TEXT NOT NULL CHECK (band IN
                      ('track','watch','investigate','deprioritize','exclude')),
  rank_overall      INTEGER,
  rank_at_percentile NUMERIC(6,3),
  recommendation    recommendation_kind NOT NULL,
  -- Cluster propagation: a wallet that is one of N members of a cluster is not an
  -- independent signal, and this says by how much it was discounted.
  cluster_id        BIGINT REFERENCES wallet_clusters(cluster_id),
  independence_factor NUMERIC(6,4) NOT NULL DEFAULT 1.0,
  gated_by          TEXT,                        -- e.g. 'integrity<30' if a gate fired
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scores_uniq UNIQUE (score_run_id, address)
);

CREATE INDEX scores_run_rank_idx     ON scores (score_run_id, confidence_adjusted_score DESC);
CREATE INDEX scores_address_time_idx ON scores (address, as_of DESC);

CREATE TABLE sub_scores (
  id                BIGSERIAL PRIMARY KEY,
  score_id          BIGINT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
  dimension         TEXT NOT NULL CHECK (dimension IN
                      ('early_entry','profitability','repeatability','risk',
                       'token_selection','exit_effectiveness','integrity','confidence')),
  raw_value         NUMERIC(38,10),
  normalized_value  NUMERIC(6,2) CHECK (normalized_value BETWEEN 0 AND 100),
  shrunk_value      NUMERIC(6,2) CHECK (shrunk_value BETWEEN 0 AND 100),
  weight            NUMERIC(6,4) NOT NULL,
  contribution      NUMERIC(6,2),                -- weight * value
  sample_size       INTEGER,
  shrinkage_applied NUMERIC(6,4),
  confidence        NUMERIC(6,2),
  explanation       TEXT NOT NULL,               -- deterministic, always populated
  CONSTRAINT sub_score_uniq UNIQUE (score_id, dimension)
);

-- The evidence trail. Every sub-score must be reconstructible from its evidence rows.
CREATE TABLE score_evidence (
  id                BIGSERIAL PRIMARY KEY,
  score_id          BIGINT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
  dimension         TEXT NOT NULL,
  evidence_kind     TEXT NOT NULL CHECK (evidence_kind IN
                      ('metric','position','swap','cluster','benchmark','quality_flag','counterfactual')),
  metric_key        TEXT REFERENCES metric_definitions(metric_key),
  subject_id        TEXT,                        -- position_id, swap id, cluster id, ...
  value             NUMERIC(38,10),
  benchmark_value   NUMERIC(38,10),
  delta             NUMERIC(38,10),
  provenance        provenance_kind,
  note              TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX score_evidence_score_idx ON score_evidence (score_id, dimension);

-- What would move the needle. Computed deterministically by re-running the score with
-- one metric raised/lowered to its next band boundary.
CREATE TABLE score_sensitivity (
  id                BIGSERIAL PRIMARY KEY,
  score_id          BIGINT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
  metric_key        TEXT NOT NULL REFERENCES metric_definitions(metric_key),
  current_value     NUMERIC(38,10),
  threshold_value   NUMERIC(38,10),              -- value needed to reach next band
  required_evidence TEXT NOT NULL,               -- e.g. '6 more closed positions'
  score_delta       NUMERIC(6,2) NOT NULL,
  resulting_band    TEXT,
  -- Direction of the counterfactual: what would move the score up, and what down.
  direction         TEXT NOT NULL CHECK (direction IN ('increase','decrease')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX score_sensitivity_score_idx ON score_sensitivity (score_id);

-- ===========================================================================
-- 9. Data quality
-- ===========================================================================

CREATE TABLE data_quality_flags (
  id                BIGSERIAL PRIMARY KEY,
  scope             TEXT NOT NULL CHECK (scope IN ('wallet','token','pool','position','score')),
  subject_id        TEXT NOT NULL,
  flag_code         TEXT NOT NULL,               -- 'missing_entry_price','gap_in_history', ...
  severity          TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
  detail            TEXT NOT NULL,
  affected_metrics  TEXT[],
  detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at       TIMESTAMPTZ,
  CONSTRAINT data_quality_flag_uniq UNIQUE (scope, subject_id, flag_code, detail)
);

CREATE INDEX data_quality_flags_subject_idx ON data_quality_flags (scope, subject_id);

-- ===========================================================================
-- 10. Monitoring
-- ===========================================================================

CREATE TABLE watchlists (
  watchlist_id      BIGSERIAL PRIMARY KEY,
  name              TEXT NOT NULL UNIQUE,
  description       TEXT,
  min_score         NUMERIC(6,2),
  min_confidence    NUMERIC(6,2),
  is_active         BOOLEAN NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE watchlist_members (
  watchlist_id      BIGINT NOT NULL REFERENCES watchlists(watchlist_id) ON DELETE CASCADE,
  address           TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
  added_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  added_by          TEXT NOT NULL DEFAULT 'auto',
  score_at_add      NUMERIC(6,2),
  confidence_at_add NUMERIC(6,2),
  rationale         TEXT,
  removed_at        TIMESTAMPTZ,
  PRIMARY KEY (watchlist_id, address)
);

CREATE TABLE alert_rules (
  rule_id           BIGSERIAL PRIMARY KEY,
  name              TEXT NOT NULL UNIQUE,
  kind              alert_kind NOT NULL,
  is_active         BOOLEAN NOT NULL DEFAULT false,
  min_wallet_score  NUMERIC(6,2),
  min_confidence    NUMERIC(6,2),
  min_liquidity_usd NUMERIC(38,2),
  max_mcap_usd      NUMERIC(38,2),
  min_tx_size_usd   NUMERIC(38,2),
  -- Independent wallets required. Cluster members count as ONE.
  min_independent_wallets INTEGER,
  cooldown_seconds  INTEGER NOT NULL DEFAULT 3600,
  params            JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE alerts (
  alert_id          BIGSERIAL PRIMARY KEY,
  rule_id           BIGINT NOT NULL REFERENCES alert_rules(rule_id),
  kind              alert_kind NOT NULL,
  fired_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_time        TIMESTAMPTZ NOT NULL,
  subject_token     TEXT REFERENCES tokens(mint),
  subject_pool      TEXT REFERENCES pools(pool_address),
  severity          TEXT NOT NULL CHECK (severity IN ('info','notable','high')),
  title             TEXT NOT NULL,
  -- Grounded explanation assembled from fact rows, then optionally narrated.
  explanation       TEXT NOT NULL,
  facts             JSONB NOT NULL,
  evidence_ids      BIGINT[],
  independent_wallets INTEGER,
  suppression_key   TEXT NOT NULL,
  CONSTRAINT alert_dedupe UNIQUE (rule_id, suppression_key)
);

CREATE INDEX alerts_fired_idx ON alerts (fired_at DESC);

CREATE TABLE alert_deliveries (
  id                BIGSERIAL PRIMARY KEY,
  alert_id          BIGINT NOT NULL REFERENCES alerts(alert_id) ON DELETE CASCADE,
  channel           TEXT NOT NULL,               -- 'telegram','ntfy','discord','stdout'
  attempted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at      TIMESTAMPTZ,
  status            TEXT NOT NULL CHECK (status IN ('sent','failed','skipped')),
  error             TEXT,
  CONSTRAINT alert_delivery_uniq UNIQUE (alert_id, channel, attempted_at)
);

-- ===========================================================================
-- 11. Operational tables
-- ===========================================================================

-- Hard monthly credit cap enforced in code. A runaway loop must fail, not burn the budget.
CREATE TABLE credit_ledger (
  id                BIGSERIAL PRIMARY KEY,
  provider          TEXT NOT NULL,
  job               TEXT NOT NULL,
  operation         TEXT NOT NULL,
  units             INTEGER NOT NULL,            -- requests made
  credits           INTEGER NOT NULL,
  period_month      DATE NOT NULL,               -- first day of the billing month
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  request_key       TEXT,
  notes             TEXT
);

CREATE INDEX credit_ledger_period_idx ON credit_ledger (provider, period_month);

CREATE TABLE job_runs (
  run_id            BIGSERIAL PRIMARY KEY,
  job               TEXT NOT NULL,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ,
  status            TEXT NOT NULL CHECK (status IN ('running','success','failed','partial')),
  rows_written      BIGINT,
  credits_spent     INTEGER,
  error             TEXT,
  params            JSONB
);

CREATE INDEX job_runs_job_idx ON job_runs (job, started_at DESC);

-- Active stream subscriptions, so the collector can reconcile what it believes it is
-- listening to against what the provider says is live.
CREATE TABLE stream_subscriptions (
  id                BIGSERIAL PRIMARY KEY,
  provider          TEXT NOT NULL,               -- 'helius'
  kind              subscription_kind NOT NULL,
  target            TEXT NOT NULL,               -- pool / account / program being watched
  chain_id          TEXT NOT NULL REFERENCES chains(id),
  is_active         BOOLEAN NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_message_at   TIMESTAMPTZ,
  disconnect_count  INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  CONSTRAINT stream_subscription_uniq UNIQUE (provider, kind, target)
);

-- Restart-safe high-water marks. Advance ONLY after successful ingestion.
CREATE TABLE job_checkpoints (
  job               TEXT NOT NULL,
  partition_key     TEXT NOT NULL DEFAULT '',
  last_slot         BIGINT,
  last_event_time   TIMESTAMPTZ,
  last_event_id     TEXT,
  last_success_at   TIMESTAMPTZ,
  overlap_slots     BIGINT NOT NULL DEFAULT 64,  -- deliberate re-fetch window
  state             JSONB,
  PRIMARY KEY (job, partition_key)
);

-- Ingestion bookkeeping so credits are never spent twice on the same event.
CREATE TABLE ingestion_queue (
  id                BIGSERIAL PRIMARY KEY,
  job               TEXT NOT NULL,
  subject           TEXT NOT NULL,               -- signature, pool, or wallet
  priority          SMALLINT NOT NULL DEFAULT 100,
  status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','in_progress','done','failed','skipped')),
  attempts          SMALLINT NOT NULL DEFAULT 0,
  credits_estimate  INTEGER,
  enqueued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at      TIMESTAMPTZ,
  error             TEXT,
  CONSTRAINT ingestion_queue_uniq UNIQUE (job, subject)
);

CREATE INDEX ingestion_queue_pending_idx ON ingestion_queue (job, status, priority)
  WHERE status IN ('pending','failed');

-- ===========================================================================
-- 12. Validation harness (doc 09)
-- ===========================================================================

CREATE TABLE validation_experiments (
  experiment_id     BIGSERIAL PRIMARY KEY,
  label             TEXT NOT NULL UNIQUE,
  config_version    TEXT NOT NULL REFERENCES score_config_versions(config_version),
  discovery_start   TIMESTAMPTZ NOT NULL,
  discovery_end     TIMESTAMPTZ NOT NULL,
  validation_start  TIMESTAMPTZ NOT NULL,
  validation_end    TIMESTAMPTZ NOT NULL,
  baseline_kind     TEXT NOT NULL CHECK (baseline_kind IN
                      ('random_wallets','all_wallet_median','buy_every_new_token','market')),
  n_wallets         INTEGER,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  notes             TEXT
);

-- The result of a validation run. The deliverable of the whole project.
CREATE TABLE validation_results (
  id                BIGSERIAL PRIMARY KEY,
  experiment_id     BIGINT NOT NULL REFERENCES validation_experiments(experiment_id) ON DELETE CASCADE,
  metric_key        TEXT NOT NULL,               -- 'rank_ic','precision_at_10','shadow_return', ...
  top_decile_value  NUMERIC(38,10),
  baseline_value    NUMERIC(38,10),
  lift              NUMERIC(38,10),
  ci_low            NUMERIC(38,10),
  ci_high           NUMERIC(38,10),
  n_samples         INTEGER,
  is_significant    BOOLEAN,
  notes             TEXT,
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT validation_result_uniq UNIQUE (experiment_id, metric_key)
);

-- Individual alert outcomes, tracked forward so false-positive rates are measurable.
CREATE TABLE alert_outcomes (
  id                BIGSERIAL PRIMARY KEY,
  alert_id          BIGINT NOT NULL REFERENCES alerts(alert_id) ON DELETE CASCADE,
  evaluated_at      TIMESTAMPTZ NOT NULL,
  horizon_hours     INTEGER NOT NULL,
  token_max_multiple NUMERIC(20,10),
  token_return_pct  NUMERIC(20,10),
  was_false_positive BOOLEAN,
  notes             TEXT,
  CONSTRAINT alert_outcome_uniq UNIQUE (alert_id, horizon_hours)
);

COMMIT;
