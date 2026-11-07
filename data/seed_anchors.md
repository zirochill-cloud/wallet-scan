# Anchor input — what to supply, and what happens to it

This file is a **placeholder for input, not code**. Nothing reads it yet. Fill it in (or replace
it with a JSON file of the same shape) and the next session can seed the anchor tables from it
without asking you anything else. Path A and Path B in `docs/PHASE1B_STATE.md` § 3 both start
from exactly this information; they differ only in who fills in the buyer lists.

## What one anchor needs

| Field | Required | Notes |
|---|---|---|
| `mint` | yes | The token mint address. This is what "anchor" means: one token, one launch. |
| `genesis_slot` | no | The slot the mint first appears in. Needed to bound "bought first" by time; can be filled in later. |
| `genesis_time` | no | UTC instant of genesis. Same reason; also what the 30-day pump window is measured from. |
| `deployer` | no | The wallet that created the mint. Used to detect two anchors that are really one launch. |
| `first_20_buyers` | for Path B | Ordered buy addresses, earliest first. Rank 0 = first distinct buyer of the token. This ordering is what `buyer_rank_at_entry` measures. |

Selection rules the pipeline assumes (they are not enforced in code — they are why the anchor
set is worth anything):

- 3–5 mints, from **different weeks and different deployers**, launched **30–90 days ago**;
- each token **pumped ≥5×** within 30 days of launch;
- a wallet needs **≥3 distinct anchors** before it can be ranked at all.

## Example shape

Three anchors, two buyers each shown for brevity — a real set has ~15–20 buyers per anchor.

```
ANCHOR 1
  mint            7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU
  genesis_slot    289412773
  genesis_time    2026-09-14T11:03:27Z
  deployer        5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1
  first_20_buyers
    0  9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM
    1  3RfM4bYc8tKzYmNQvJpXs1oHdLgUbCwVeA2nTkPqS7Ra
    ...

ANCHOR 2
  mint            4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R
  genesis_slot    291088411
  genesis_time    2026-09-28T04:41:09Z
  deployer        HXsK1n1zqBzZfSk5cQ6bA7nWmJt8vYpR3dLgUeTc2VbM
  first_20_buyers
    0  9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM
    1  Bv8dQr1sZt4oHkNbYpJ6xWfLmCgAe2nTkV9zRqP7UbXs
    ...

ANCHOR 3
  mint            EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
  genesis_slot    300775201
  genesis_time    2026-10-11T19:22:48Z
  deployer        7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj
  first_20_buyers
    0  3RfM4bYc8tKzYmNQvJpXs1oHdLgUbCwVeA2nTkPqS7Ra
    1  9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM
    ...
```

(The mints above are real Solana token addresses used as plausibility placeholders — replace
them with the tokens you actually want, and make sure they meet the selection rules above.)

## What the seeder does with it

For each `(anchor, wallet)` pair, an eventual seeding script calls the functions that already
exist in `solscope/anchor_writer.py`:

1. `write_anchor(conn, AnchorInput(mint, source, created_by_rule_version, genesis_slot,
   genesis_time, deployer))` — the anchor rows;
2. `write_verdict(conn, anchor_id, wallet, verdict, rule_version)` — did this wallet buy the
   anchor, and in what role;
3. `write_metric(conn, anchor_id, wallet, metric_name, status, value, window_days)` for the three
   Phase 1b metrics — `max_subsequent_return`, `buyer_rank_at_entry`,
   `pct_entries_before_volume_spike`.

After that, `solscope positions` → `solscope consumer` → `solscope scores` run unchanged. The
consumer does not care where the rows came from; it only cares that they are there.
