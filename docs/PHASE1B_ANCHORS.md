# Phase 1b anchor verification (2026-11-07/08)

Five mint addresses were supplied as the surviving record of the lost Phase 1a
seeds (see `NOTES.md` § `DATA_LOSS_2026_10`). They are **claims**, not anchors,
until each one passes on-chain verification. This document records what the chain
says, not what the report says.

Probe: `spikes/verify_anchors.py` (scratch, not part of the package). Signature
pages are cached under `%TEMP%\solscope-anchor-verify\`.

## Account-level facts (verified)

Verified with `getAccountInfo` (`jsonParsed`). All five addresses are token
**mints**, not wallets.

| Mint | Program | Decimals | Symbol | Mint authority |
|---|---|---|---|---|
| `GB68eELgf864nrQHjNvrXHF6Xywpp1kYsb7fsGBupump` | Token-2022 | 6 | ChooChoo ("The Peptide Dog") | revoked |
| `zj1jpp7QMveWHLs61vL9KMZf254KvW7j4AAmBF8ry2k` | SPL Token (classic) | 6 | none on chain (reported `$BULLSHIT`) | revoked |
| `EVgwa5CHBVwk6sCTa4QJrVXYBKngZhYqh2a2ZGDApump` | Token-2022 | 6 | MACRODUCK | revoked |
| `7Y7V1a4m2nWK7BMgbka5B4vR1pDvCK7yva3Hnrqkraze` | Token-2022 | 6 | Nasduck (reported `$NASDUQ`) | revoked |
| `91ryaCo5yGpYZM3bs6GUPs97VWJQj7RozBmqPULgpump` | Token-2022 | 6 | TIGRINO ("Leopardus Tilcayo") | revoked |

Two of the reported symbols do not match the chain: the classic SPL mint carries
no symbol at all, and `7Y7V1a4…` is "Nasduck" rather than "NASDUQ". Neither is
disqualifying — pump.fun mints are commonly renamed after launch — but neither
can be confirmed from the report.

One reported address was one character short: `7YV1a4m2…hkraze` (43 chars) has no
account. The 44-character form in `NOTES.md` is the real mint and is what was
verified.

## Method

The first attempt used `getSignaturesForAddress`, which pages **backwards only**
at 1,000 signatures per call. These mints traded so heavily that four of the five
exhausted a 1,598–2,000 page budget without reaching genesis (thousands of
credits, longest run 07-30 11:54 from a 2026-11-08 head). Cached pages from those
runs are still what supplies the later part of each price series.

Genesis came instead from `getTransactionsForAddress` with `sortOrder: "asc"`:
the mint's **oldest** transaction in one call.

The launch slot of a bundled pump.fun mint is dominated by failed bundle legs —
2,498 of the first 2,549 signatures on ChooChoo failed. The signature listing
already carries `err`, so failed transactions are dropped before any payload is
fetched; only successful ones cost a `getTransaction` call.

Buyers are attributed from **balance deltas, not from the instruction's trader
slot**: the wallet is the transaction signer whose balance in the mint went up.
For a bundled launch, the fee payer and the buyer are frequently different
accounts, and a creation transaction can carry a dev buy whose trader slot is
empty. All 100 attributed buyers are signers with a positive mint balance, so
none needed the `keys[0]` fallback.

Price series: one observation per 6-hour bucket from the cached signature pages
(reaching back to roughly the mint's first day), plus every launch-slot trade
already decoded during the buyer scan. Observations whose executed size is below
100 base tokens or 0.001 SOL are dropped as dust: without that floor a one-token
wash trade becomes the "30-day maximum" and manufactures a four-digit multiple.

## Genesis results

All five are pump.fun bonding-curve creations (program
`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`, `Instruction: Create`), and each
genesis transaction is its own creation transaction, so the deployer is simply
the fee payer.

| Anchor | Genesis slot | Genesis (UTC) | In launch window | Deployer | First-20 buyers | Genesis→20th buyer | Deployer among them |
|---|---|---|---|---|---|---|---|
| ChooChoo | 436000073 | 2026-07-29T18:41:11Z | yes | `F3foKaoFLCC6CXfXFEmLFdDp2Vn9PKV4AXxbaK2zAjL7` | 20 | 25 s | no |
| BULLSHIT | 439887105 | 2026-08-17T16:48:48Z | yes | `GRmwzXEBvD11ovA95BWiXmYFHSVu5QrTFNoZSxNpk9Tg` | 20 | 176 s | yes |
| MACRODUCK | 443024778 | 2026-08-31T01:18:33Z | yes | `gVDXhoGbePACvSqN7CZBtQXFW9eyJwsgudPEwSydAkx` | 20 | 4 s | yes |
| Nasduck | 444931195 | 2026-09-07T00:39:35Z | yes | `DpCf41tm1kUa5gbxsoCiHXwRhuvKdYehPXfvMkSmD8cf` | 20 | 4 s | no |
| TIGRINO | 448038091 | 2026-09-18T09:06:35Z | yes | `AjygC8gewVhtDD36BLjbMaDUPC2VSMsa3gHrXdYYh36M` | 20 | 9 s | no |

Launch window under test: 2026-07-29 … 2026-09-19. Every genesis falls inside it,
so the supplied dates are not contradicted anywhere.

| Anchor | Genesis price | Max 30-day price | Max / genesis | Peak day | Drawdown from peak (trough) | Drawdown from peak (day 30) | Verdict |
|---|---|---|---|---|---|---|---|
| ChooChoo | 3.876e-08 | 6.755e-06 | 174.3× | 1.25 | 99.999% | 97.21% | `FAIL_DUMP` |
| BULLSHIT | 2.996e-08 | 5.833e-05 | 1947.0× | 11.00 | 99.899% | 99.90% | `FAIL_DUMP` |
| MACRODUCK | 4.577e-08 | 4.361e-06 | 95.3× | 0.50 | 99.995% | 95.91% | `FAIL_DUMP` |
| Nasduck | 4.480e-08 | 1.353e-05 | 302.1× | 4.75 | 99.994% | 99.95% | `FAIL_DUMP` |
| TIGRINO | 2.960e-08 | 3.630e-05 | 1226.3× | 2.25 | 99.990% | 99.15% | `FAIL_DUMP` |

Prices are SOL per whole token. Genesis price is the first decoded buy (buyer 1
in each case, within the creation slot). "Max" and both drawdowns are computed on
dust-filtered observations only.

Every anchor cleared the multiple gate (≥5×: the smallest was 95.3×) and then
failed the dump gate (drawdown ≥95%). The result does not depend on which
drawdown is meant: measured to the deepest post-peak observation or to the last
day-30 observation, all five are between 95.9% and 99.999% below their peak.

This is not a sampling artifact of the sparse grid in one direction only: the
peaks themselves were reached between 12 hours and 11 days after launch and are
sampled, and the collapse is still present at day 30 in the last observed trades.

## Independence check

`NOTES.md` § `ANCHOR_SELECTION_CRITERIA` requires deployer diversity, at least 24
hours between genesis timestamps, and at least one genesis→20th-buyer spread over
5 minutes.

| Criterion | Result |
|---|---|
| Distinct deployers | 5 of 5 — no two anchors share a creator |
| Genesis separation ≥24 h | yes — successive gaps 19 d, 14 d, 7 d, 11 d |
| At least one spread >5 min | yes — BULLSHIT 176 s (the other four are 4–25 s) |

Structural independence **passes**. It cannot rescue the set, because independence
is only applied to anchors that survive verification.

## Decision: STOP

**0 of 5 anchors survive.** The gate requires at least 3 survivors before any
anchor-discovery work.

Consequences, per the Phase 1b instruction:

- No S3 anchor-discovery module was written.
- No anchors were seeded, no positions reconstructed, no consumer or scores run.
- No criteria were loosened and the ≥3 gate was not lowered.

Five replacement mints are required. They must be addresses whose on-chain
history shows a peak of at least 5× genesis and a post-peak drawdown below 95%
over 30 days; verification of any replacement uses the same probe
(`python spikes/verify_anchors.py verify <mint>`) and the same thresholds.

## Decoder change made along the way

`solscope/decode.py` `_mints_from_accounts` now reads the pump.fun bonding-curve
mint at `accounts[2]` (quote = native SOL). Before that, every bonding-curve
trade resolved to no mint and was skipped as "mints unresolved", which made the
launch slot of every pump.fun mint undecodable — the exact transactions an anchor
campaign needs. Verified on ChooChoo's first buy: the instruction's `accounts[2]`
is the mint and `postTokenBalances` credits the signer at `accounts[6]` with it.

## Artifacts

Scratch JSON per mint (not committed): `%TEMP%\solscope-anchor-verify\verify_<mint>.json`
holds genesis, all 20 buyers with signature and executed price, the full price
series with per-observation size, and the metrics above.
