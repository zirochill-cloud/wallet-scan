-- 005_scores_band_basis.sql — record what the confidence pull was applied to.
--
-- WHY A COLUMN AND NOT A CONSTANT
-- `confidence_adjusted_score` is one number with two possible meanings depending
-- on how much of the model exists. While five of the eight dimensions are
-- unbuilt, `overall_score` is the neutral placeholder 50.00 and adjusting it
-- produces 50.00 for every wallet — a column of identical values that would read
-- as a ranking. D21 therefore applies the pull to the one dimension this phase
-- actually measures:
--
--     confidence_adjusted_score = shrunk_early_entry x confidence_score / 100
--
-- and bands derived from it are *early-entry* bands: "is this wallet's early-entry
-- ability worth tracking?", not "is this wallet good?". When the remaining
-- dimensions exist, the basis flips to `overall_score` and the same column starts
-- meaning full-wallet ranking. Both semantics are legitimate; only one is
-- available at each phase, and a reader of a row must be able to tell which one
-- they are looking at without knowing the release history. Hence the column.
--
-- The DEFAULT backfills existing rows correctly: every row written before this
-- migration used the early-entry pull (or the pre-D21 constant, which no longer
-- has a reader). The CHECK keeps a future phase from inventing a third basis.

BEGIN;

ALTER TABLE scores ADD COLUMN band_basis TEXT NOT NULL
  DEFAULT 'shrunk_early_entry'
  CHECK (band_basis IN ('shrunk_early_entry', 'overall_score'));

COMMENT ON COLUMN scores.band_basis IS
  'Which score the confidence pull was applied to: shrunk_early_entry (5 of 8 dimensions unbuilt) or overall_score (full model).';

COMMIT;
