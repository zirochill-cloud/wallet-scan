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

## Appendix: the first 20 buyers of each anchor

Each row is the transaction signature that made the wallet a buyer; `t+s` is
seconds after the genesis transaction. Every wallet is a transaction signer
whose balance in the mint increased, and every row is reproducible from the
signature with `getTransaction`.

### ChooChoo (2026-07-29T18:41:11Z)

| # | Wallet | t+s | Executed price (SOL/token) | Signature |
|---|---|---|---|---|
| 1 | `5brv79eFZ2rGprXNvqgVJBkBptkkw8GJX1XydJyZLyAr` | 0 | 3.876e-08 | `hkeSSJrWFveeicGXnYEyLB6rejUNvCC4ucAN1fLQTwwe7u1FzsPqVHqCBKzgpWFyHqXFZGUsZ2fNsX7MTEqixwE` |
| 2 | `9U4M3hDNxGprhhetLBUFpwrjtZ3iK4xDbLGijq269WHr` | 1 | 7.970e-08 | `54fVqtdfZsFnC8ucp8EZXpL7Dpn9QTPy5GsVNRRy4yLrLogTGU2ykqDCbK39d9TtFqL58faYwBR81B9J3NyBQGVb` |
| 3 | `CFt926D6bJSFKFt34VJDTAqrPRMLQJVLA5vP1oeL3GTt` | 1 | 4.408e-08 | `3uYdBPYJ2xPzTQhLoRRVJPaqoCi8RKgwWqb7pKntgMai6bK4QJNp1DkTXhaLRECT7t5eBXoyqHd8eKVjZACvEK5d` |
| 4 | `AviBqnzP8mmdJFbb7USGQXcYT8GrZEbh7jiL5fRo869N` | 1 | 4.807e-08 | `3PUWEzy4RSvBNxYEtyNaBWf77qDGLafQ11vZarLz147A9LpH7H8EJy9NMPXakh29AcVQfA47MKDfnB35htqAEWt8` |
| 5 | `8ffjvojhshDexboDqQWU8DXt3C2gSAoZHZVDZH3Wvie2` | 3 | 4.313e-08 | `3Ts6XUfHzDdHizVuV7HSrJCk8eE3FuhF6RwbTbB8XDZuz8WJohGQeHwJZAePtBH7grdwuRuVTP5hpNjvGkojB2HQ` |
| 6 | `365ox2yoGYXQfzubohqCgzhg2MrG5PKxBRi4p9gpNQGi` | 3 | 4.604e-08 | `2bXNbFFmHWKqWQRnBgacpdStr5p4bQQ3uUfg8jodXoDKdMhRamV2Kfc6QFjDPcGJvcrDh6wLdfPa9kHYGYrCWj11` |
| 7 | `2mQj1wKkRxcgBJizAziAcVUsfC7cc4KmioDy4ssGLnUH` | 4 | 5.253e-08 | `3osLwHsME9b7nJ15JnVVGLPtngw4dBc81TGmr3Bw3RQ5KSiVvxKtgn2CiZrANdpodnTxieGczq8zk56j7vcu4cwB` |
| 8 | `6wMeHP7woFnUvZDJVkrd8S352THs5VZ8hLGhQjqR3Gba` | 5 | 5.224e-08 | `3P6ALKRxhwEBy5Q8aCYLTh3hyJrLL2feawDSCxYy8m8JVdJxHCTMbABnUJVZjKKVnXds54hiYbKLFjWDU2eziVUd` |
| 9 | `AR64rNW8eTsthyYgebVgtU7FwmBaV5dc8DtCfrv6owS4` | 9 | 4.320e-08 | `5ey2hYQd87NU5CWS4fEzPkyV4WPgNHyq3zN9zTRCGqLro7TuasvVYReYafpFHZXMGNzWYq3CBw6VbbP8ApZZ5W1F` |
| 10 | `8a6JShiMBwKrYN9b62o7aJtDcobDwXmTudai6PpRy18k` | 9 | 4.560e-08 | `4REuBJ84X1DiJGTNVmYcHAhq8AKhWcREuoJfgqW9FTTisKY7FST8sidNGaXF56wxfQ6cFQV8M28vJA9e6ALXc4Cf` |
| 11 | `DeNwH63Xgj3a4bXXHw79BiL5iBUNBQVaRYeLHQTQxL7z` | 9 | 4.644e-08 | `3LyuWcRPyNzTQpwmxCPFaEDQYFJ5F4E4mu3vbR7jEpWa4GUZUHAbzNf4bdmWc1CVMLA5dAGLzJAAz4LDL5z3Heqw` |
| 12 | `D2CU7ziXFZVhmfuV2uRHe1Un2YrmpowY1s58EqYACgHB` | 10 | 6.124e-08 | `4adLrUz26UbjZRx1CfiCnzdmGrAQTUxFrCvyLKnrvigcDv18QmRxuXtAvEusW7vyp6vyDA4NkzYS84GF3hebRUnF` |
| 13 | `9khkRvs76cmVAfRHswogDqBwikdHDKBH2e9zXLuaYrTn` | 10 | 4.849e-08 | `5PGe8dhpdYZcfzFaH1ZQBoC2t2e87Yw8qHPx9k4hKtEn2XaXvxBEQ52yGf5VoxmR76yHHnsGv5Beb2KVroiSjVV6` |
| 14 | `9pqbbqUw1Z3zVv4upx6jPKhmCaGbuXCZThhqWoTaR3cH` | 10 | 5.011e-08 | `44grp4PKfpsxp4Sfx87YxSeJavL1b2ZGc7o25ww52ENVEMNATDf2VNo1mXDEQxC1rcFtMHxcM4rJzWxon7QsnpuW` |
| 15 | `57WkTbDxxu3tC9Nhve8hQms5KBRa7MFEPSnnEeThivvX` | 10 | 5.170e-08 | `2VHyu89dvVbJGrBxuWCLYTAyWB6iLZZPSoAuQnW4wdMJYMQEthBr3m8KtYxM4NMg14LaBm1yvvnJNHxYssRU37js` |
| 16 | `348Xv4PvbMXLR4HFUbmowskWgwTnP5bVTzKVmD1fin97` | 11 | 5.564e-08 | `2FWfxrH9VrcLEUDkxK8z9svzL7dTSYC7h7cZhX3iThe2QrBVJASTD7GXgfSMqUZSDUypPW8jEbarpAYS6eQ2r1Yr` |
| 17 | `E1mgnLPgRZR4U7r6p3mmK4dZv9FbC2X64VdsUpgrqN9u` | 17 | 5.583e-08 | `3XXtwZskha7D1ra3eNJ1DDcxwDCcyrrBuKFnnhXfASPTf4yhfKnN1br4cvarCVz2uWDJNYm9HeJyifaF4twNY61r` |
| 18 | `5WdnohUqo1npALEwUT67nfDE16pyXHH5uKkvaCsjbUoP` | 24 | 3.845e-08 | `5HdTBiWasA48XZF6AzJVyfDh6hpEwZLb3Va4BNHvVCaMePJBj8Y5QxNdZmg17GHRBwGDd5PDV9ZuujhbL8BKbyhe` |
| 19 | `56eGmWrgsm7q7bxnY8fWjo39iDbuLLU4nJKw34BuYMeL` | 24 | 3.942e-08 | `3NToHCfB1eDkYrJEVjHZoYfF65fLxrchrVvQnJwTdsL8t1KGvov7cRik7ZD8wduJEYd7MAoAvRrWip4Y1w68Br3P` |
| 20 | `FkmwN69qR8AKtixDXwood4qgged3QC27kZeBJwrYm3S1` | 25 | 4.018e-08 | `2EHwSdrUsq1VS8eNQAGkvYFJms5NwPQdEYDDUEx7bRR9TALpY5T6mRwjXgFshGrKeTCDAi1u7RxoNvCQiiT9sazj` |

### BULLSHIT (2026-08-17T16:48:48Z)

| # | Wallet | t+s | Executed price (SOL/token) | Signature |
|---|---|---|---|---|
| 1 | `GRmwzXEBvD11ovA95BWiXmYFHSVu5QrTFNoZSxNpk9Tg` | 0 | 2.996e-08 | `9VASjouUJJyuLqySfSZvz3TW3ZP8EYkwqkx9KbhbwcmqdXAPUf1XoVNyGu7yRJ581zrPf9VimcreFJCjr3MURb3` |
| 2 | `52NUhyYcpwwycY2To1bLMtp6pjDjtUMFeUqQxZfBJgAt` | 2 | 3.328e-08 | `4vuePyvxvkWSCq8o1Tmmp4QQrSgu1mbfThgiVXb7vD7g4bXdbSsVuMFTasb9wiYHRe2HshenwSzSDJ8aDKDiGw7f` |
| 3 | `Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt` | 3 | 3.975e-08 | `5cud2NUgFGpYYnm7SmKbTeSXZMNhNBpngYaqVMqCcZFg4nsw8bzU3qHUfBtAfSDWh7p8GddbYLjbJ9uT7pF8UhYw` |
| 4 | `3TB4EwTwSrkWgjubYYbd6JNmVcTSJ44EgMMwKm8HZABQ` | 5 | 6.331e-08 | `4KBMxoZqe3yiddEdisDwasVDqMuLhgGqJ4KnivjCYrmPddsPPu1nLBUGDLtmhykajALmLMJ3uGbmAV3XkpaYzbRH` |
| 5 | `EM1ZyNA3g4rr9MBzSEA36QqWd7t7cp54zDidm3YskyeU` | 8 | 4.017e-08 | `4ZT8bcsvPTvWY7EbmikYwioBKXSnLeQWzFRgRqbwYjRtdCxeTN1Lqb7BgTBFsjMSLA9cvyuVX7jctjc3yTydw4G4` |
| 6 | `PMJA8UQDyWTFw2Smhyp9jGA6aTaP7jKHR7BPudrgyYN` | 8 | 4.310e-08 | `iKsaQa89YWEjB1jM8qHw1V5K4KQNNrDDY6igRJ17kLJaqKn3zR1n1ozdabGJaukFLtZESJHwM8J7ZeWXM6q3rtY` |
| 7 | `J2L495ZPcJ6Btuyd4YqXz5uw7hpKiQXGcLvALwumabED` | 8 | 4.639e-08 | `3EnXX7yoPKSRUmNBgifuJNLBxQsW2MRn3mZDybYqRn7LXBfieJtsRoXj2SDQe2TWZABgFDp2YJoFgby8nuPMsFL` |
| 8 | `F3Rmgd1wmFSF6V36cpLbZ9Q2XSdz1XKcD4L1UrKLQyyn` | 15 | 4.002e-08 | `3xQhAvm9sRWDA4rjJiB1gVbVmhWXihA67SHXYEFb4ezRtP3RCXeqqDkLEmLNYVqyLi3YaijKgjmejLo63cm6cqAw` |
| 9 | `hmpfGde3CsTyMEukAPqFaUXZLD69pGPJg8bqRNE8GFA` | 16 | 3.318e-08 | `3hQSSGdVwkcsq5SGvBfswW9y4Z2mqjVQQx2k5sVKwJxst8iXFRbbQeqj32cxNH64uE7B6YJErYmHNTXYuPZ2qM1m` |
| 10 | `3gHrfi6t4xG2KEH4gga9HxLRYqws9iTKdkLG55NwaTZJ` | 43 | 3.188e-08 | `3kiJi4isLn86NoJQ48MfTEGN9mya8KPiqY4zG7hAMpEUZ79c58NSR2v8STB8zGjWKP8AAvhjwnHA2rqFwzMKFBoi` |
| 11 | `7RCqvciFbU5nYL4feUPq49DmEJpdT3aYqkhgtv96Y1XY` | 139 | 3.607e-08 | `3XgBAJhsoPPoL95s1SmeVoGwQD9CaLj8mx3JCe6SARWKgceHJnonYwGPUPHyAZuRnFK3WteUCnGchRf1t95QMKTF` |
| 12 | `GPsdQQDYewZuYjG6u2fhvpTbPce9kKfoSE4usNLZUXPU` | 139 | 3.632e-08 | `2jSsxKf6Ez3b8y36ZAxwxeD7bqUztQFtyXLjytjVKUEKFNUWPbRT77XcepeYwCkKV6ZoQGFUMhTKERjpgqPt7cb1` |
| 13 | `42NxDyCL4uyLa4MtHPSaBL8zfwu4RUtXrMjJpsFqQ7wZ` | 145 | 4.103e-08 | `4ANW1PvTSWU3RnF2VX6HdRCGFR4AtqGinPMywBm742dApmUMwoUF8HgFGANLKaGVkiYU7rnbQdNBFUHtVERUQG72` |
| 14 | `8dtx2tr4TuJsYpri2suggFu1pg3DVjFLBBVmhtDy1MEF` | 148 | 4.556e-08 | `3xn5nfVEkTbQpsoHVDBMc94BqbJH2KqRWdy8uv9dnYHx8LmAPjMWeCv9R2ujtFC3UdyyoN4CSVo7vqbKPFbmiQuV` |
| 15 | `GZmUDsn8W1rtopeTgwVgab9rsRGMLVVMfsXwWKD1VS9p` | 148 | 4.733e-08 | `3562gTHMPwgmAtKitqk49w5HZt1oi5prbmbUPuf3A6TQzJ2JXaRZzVSw5FM7RxhduvZq1v4pRvTvNyGSqJxrk3QX` |
| 16 | `DTS6KBsfaveqP5WUuWpmk4Mac14nanwwnBaraUAWsrYJ` | 151 | 4.951e-08 | `25vNqCLAoKXcyQkv2dPSa5yDkwRZxzDGKGCuuup2dePDfCPZkGRLXarJ4GTJWFfcfveV3dY3Crx545xehugHKavx` |
| 17 | `DgN1YE7mo2nNgTJ2fL5645WXChrqUQuo7RaTuDZGp94Q` | 151 | 5.077e-08 | `2fmyVihZeZSsA8qnsAMrakvrNLmNXqFNHpLwmjhmoYCz9aiGeQ2iph6PY8KneE6y5uh97uaSBDq9ePGu2a9cq6NX` |
| 18 | `GAo55r3AsJFcEa8fAwq7t17AQCny8oMXdjpjmqqxRcsn` | 158 | 5.248e-08 | `3c1QhCnnzi2sRd72A2DHqYX7UYdysqjdD6pQ9zMhfNk5dcCsvM7ihJyoQPhRjGaUhTd1Mjn4z91MoSsKoaC8kRQh` |
| 19 | `2neGXs28bVKtKwGhKjz9CaU27saXpmNKenqmkADc6Rg7` | 173 | 7.394e-08 | `4TCcMPkpdpZKJyZzUE7b5GsPeh8QJVTUycfBoNBAjJ5twdhdn1JiL6oVjfs4rk9EqR9EsptTTaSQUeHVqV3hvPNh` |
| 20 | `Gfds2Ur4UVKwuwVSt8yvycYdH1vfwnf6i8TaWdf73Pn7` | 176 | 6.694e-08 | `4pszvFj9um6f18JUdz6zFyuaWFWk3u9sxFSexr8X5akDNGDsAVNj2h3w493EVEcKF2AF3YHfNCTykhaTHHuu9CxE` |

### MACRODUCK (2026-08-31T01:18:33Z)

| # | Wallet | t+s | Executed price (SOL/token) | Signature |
|---|---|---|---|---|
| 1 | `gVDXhoGbePACvSqN7CZBtQXFW9eyJwsgudPEwSydAkx` | 0 | 4.577e-08 | `5fqMfcpxAHncBXfwMbpQTCNHKRokg1uZYs9sJbSfpoELjsY6yctaWuZJ4424oqZaQG4Mitb81QLhfGprSkpv3bcJ` |
| 2 | `AWmeKTntPiVfnhBaQ13FWpUoqQ5SfoSVrYX2RYfaWavS` | 0 | 1.040e-07 | `2aVd8okSPgQhwgoNfT4m8dykGhuW6A9LRFSPqsJbzdQ8LMsAMJM6aVDdAQEtHEaPSrZQqzmeCnMakDSW4NCLDBkz` |
| 3 | `78Qkt6pnSvDAfDLPG97At5ksFLdPqgNUKRKtGh9g1e5D` | 1 | 1.553e-07 | `4kKuueB5SgBWwGjszP57VGLGJy9dweXJeoWvzbgnMZFq2896boEYh7n6ZVu19ufFXue22o9ypTxZwN6E9iGeEH8u` |
| 4 | `8NfcAge8C5jdGM4HcuZj9M8KajRaZ2h9Azp5UuTzh9k5` | 1 | 8.773e-08 | `3TQX78vY4kq3rtiNESbBJkWnvGXYL8LeWZpCgDt4Be2kQzPmChtVK7tJm1cq3Rtt7BXqWzqCdME1E7rDHcDBeDH9` |
| 5 | `3N1KKNyjKBxmRN66zF2jZwdtLaR8D3zXcAgKvuuFAdiS` | 2 | 8.499e-08 | `4iZmbR4K6AwQagYNa4XkGySyPwQsKnyUz7y1C97FuH2nbnqKk8FaQNbcKtY2E1Wa9phTxoSajSiHLous5bgZxvh7` |
| 6 | `HRCgBTh8fw3CpfNZruuUj9qDzwvTibQjgd3v7f4ak1zd` | 2 | 8.993e-08 | `2nEPGKbeu1eAirnrE4wwh9zVGcQ7z2zSGrcipWfbNFoF1CdVfxLpSM86NKmmSvmwQntGvttmZoA8nspjpWzZEPLw` |
| 7 | `6hVFtyYLovFvsigSquo3wqef54m6tFFFJPzxMv7ZZnGN` | 2 | 9.065e-08 | `k3uqaKpditNwB3GPkNSfhSKweyPbuZ7K3W7cTVwF62Ndu7frKmHaiQTmZzZmbmVbxdqGoS7vKeVpm46VqtHLAgN` |
| 8 | `G3ouJNKDPGXNUU1qtbZ3bwzqnnphMEB2qQjBiDfCbm4x` | 2 | 9.338e-08 | `355NyhhEQTPcFPn3Z2mTiDN9TnzRzjtnieTLbxrGxbuBhrKtuRxTsSYBMNuULdkx3N84x2qvJprGMv3Z1tejfXnh` |
| 9 | `9F1X92uBq9WSdUkj1KhfuQiNhMWtTd4NKBBk8oPnGfSJ` | 3 | 9.198e-08 | `4xkzz3oncm4k73KMaAzX7DRtXCRUB8njKrm2q424cP9YekBaxibqy2aH2mkjgRVZAHifsTX7WJxEFxCuUYxc4Rnq` |
| 10 | `Ain7MsHGhu6uqXB1z6XA7QUxqWxaRZJNk6kFVb3d2khm` | 3 | 9.229e-08 | `4zJFTF7aUxv3oz1AkaSpumJr1GdGuwfMe1LHeyhSRE9wS2AM8y2mQ3vikF5omudypDwzrstncqMHjf7ab3eLraE2` |
| 11 | `EpkTo4khMNuJV7fZhY5bGGiY7eCeKpnXf2fHoMwZCjMS` | 3 | 9.569e-08 | `2RrKHTQ9juXkChkevFEh1gbV71cgv787qfz9WBCjRbbgU8xkqCVbakDVZztC1FExwP5ZWszJUjMNoCafUnar9tmz` |
| 12 | `CseedJMVyZXMCQ2GBhiVqZnEn4BZ7nJtEwu9RUvimBAw` | 3 | 9.916e-08 | `23dnDXHqytmkivd1DmKdciReYXJLUwpAh2ML2ydH2G4S62nZoVKd4jP6bkReGGvnBgn5bjY6yHjq17uybaAjBWyy` |
| 13 | `35caaJFwzSNaFwFmP1f33wDP9cUFEXTLDsFnjna7kAjs` | 3 | 9.351e-08 | `KNuLmHar21gU2WWS93w569AMjmAG7KojM41TLu6p7eHebjaiZZTeCG1i5enWHtzuok61gJ5zVeFkastvjjWFYiZ` |
| 14 | `ESrdxDfxD1SS5qUXPTMQxydvLcT55G6wCexAnKvfPfRi` | 4 | 8.241e-08 | `2X5Dbbh95hp2cnLeyxENUmGcvs98nT8cYCvT8r7eK5hZHLdiKBxamSsb5s94k3zM3uuWoF9k2cExNh6rJin24eG6` |
| 15 | `Gku8cuthhvfoPoG18rg1EULt58xB7j1fgPoifJcyh411` | 4 | 8.569e-08 | `3Ay99U1YLfQSywhUgK3nEyGuNp1AjUqqhDDEBHqdJsKDS4qK4Rw9mJpqVnEJjMbvLQC1BmLDcY9FxoGhiuNv72Y3` |
| 16 | `DzRqREhHeiSVpGSurhnHvyqoFrweh99u9yUSdXL6CQft` | 4 | 8.055e-08 | `2tysUBYmm7H5RJz7KbxVJN7xcoZTh9afM4HUhRtVDwmmDbydwAR8FSojA2eCkWWLRu7SHLSmx7QXz8ZK7BWyaZAs` |
| 17 | `HoChUe1nYaFTCsgdD2NXHtESCk1teqSAmhmyiXPxVfd9` | 4 | 8.354e-08 | `5fUeeCQNoaDVaoVdMC2EgkfGrjDbTaqrsGpsFB9xUMeABkRbaHN2HGGnWptXdm3AzTf4n9aGWbUjZSYbcVBpP75c` |
| 18 | `Exwuds3iYWhUbSkFk7UDzyiV3zDHqidjrSusxNuXxJRe` | 4 | 1.003e-07 | `3pnEKubkWyo9B7Y9dWGw4iH654soEZ5a8KqQJJFcPcRgHkBrsRvAcRuhK6t4o9cimsEC8JiUKEZV7KoYoP1VnSn7` |
| 19 | `6vyWiNtkdre5pi55DF1ZkJWBou74PpL4G72vqWgoHBWy` | 4 | 9.824e-08 | `2K9xGT6yMqjGHhW1mbWBa9MFzgETHGAiGnH79myCMqBEGJgnDzYLJDs8wK4kNrVbqwZhDngrwaGtoMhrAXCRrGJB` |
| 20 | `GxHwGKafSrQ8tSLrinoDrezXF21BwfmLBD3d7efZ1okF` | 4 | 1.017e-07 | `4xg8t4GnS3ckFuo9EMcmqTxHcBNtkbmGt7F4KqLa3Qm7XE654h3uoptZ26eSzy19aF1yZ1f5JYiudcP54vCCpAEj` |

### NASDUQ/Nasduck (2026-09-07T00:39:35Z)

| # | Wallet | t+s | Executed price (SOL/token) | Signature |
|---|---|---|---|---|
| 1 | `AMDEmVocLtN5ji2gBY7BrmSHEyM5vt6TGszBbg4crMXg` | 0 | 4.480e-08 | `3qxnxZCZTbTSrYzib3kjfgyJ2BcjSRSsTXn2hJ2CA4LauPvig5FJVWzzqsNWjAAUVyPkvrG3sgNCz6Mr5ogA7q9V` |
| 2 | `55msLm128tQdXLXCXLxd756H9SaCo7wPpdetEioe7D8d` | 1 | 4.700e-08 | `QvA1Ddz6Ez25fY5Kr37mJCh8GZ3DV6q6RtaDbEvWYFqmaiYR66n6zh5ZekMq2rYkvnhFBh4GZYq589rquU417vP` |
| 3 | `ardinRsN1mNYVeoJWTBsWeYeXvuR9UUDGMsCDKpb6AT` | 2 | 5.155e-08 | `47rT8heWHfZen4Usf792BpDMSqFodVB3fF1qhnmj7AAs16uL1rFntL5gWJaYp2HkK2iAhPde8bqGBPCSgNHQLKQZ` |
| 4 | `FcFbJMHdwec2V7XzTY48GsRy5YAmJWimyUtafqB2qjG` | 2 | 7.206e-08 | `Dy6S1Y21NypvKwBdHMmP1Sqao6pNLwVRwbYiZYerA7MDYeuMdoGoHUnY6r2QJwMHZJamhNWDceDTSQZ65GHDHPE` |
| 5 | `7SHjoR1juE7ZK2RGT1XgDZLUs8MbgcDeVveBa1HqXATW` | 2 | 7.430e-08 | `4rY6BzdkX7JbAQrjpBCNN612YNenMWwPWWoB6yNMp2YdJLspBH4T59yP9QFmRDnnLDNBpot1vM2PsfnCpwQd7YAP` |
| 6 | `FsG8Fk86b92a2fX8nmxemNSt4iGDd78PWmUwQW4pBGm1` | 2 | 7.252e-08 | `3p6Ymuw2kmw6CWRMHfbGqUVTdkHiuke2a8W9X1YorJge6wbaEVD3MTBLWwfcV1MxKjrNQs52i59TnXBPnQ4z26B` |
| 7 | `7Eb8xdWMMGM5TBHGvsB7y3SL65r3gUzLXSJV39fgjcVy` | 2 | 7.282e-08 | `5SjRM6WpzTKQiAaJ1swWq7pNEWmufwT2SiZqSLUDXUCbnbG25eizNaN3p8WDP3AZacjQ745W7t43scicH7yhvV4d` |
| 8 | `FwA2rjEzvsyqLUKZmdMvGyFmrKnY4LLFzmQrt5tvPdta` | 2 | 5.748e-08 | `3CMAKkisRJzsv5YgLLZxgWCEQP1q43xAekga8ZVFwKY7BcUuqUv3A44sxw4J2svVXmy3dDp9Nq5vywYhctQjLA7i` |
| 9 | `kBWCyHmjagiqL3HSLatfmPWGUx3V7KeHVjQWemdX4dZ` | 2 | 5.995e-08 | `5qYbV2Qr3r5xWiE15t3G81FixU1GcBWnxJ8qfw5K17DUQrW4RrqbVE3MiKdoKBdpyhsfeHpSUGaU2PF9sVCY5hzA` |
| 10 | `2Nk5i4NGgL4kk4dsR7R5FcK3BPe8s21NNFnadXkVX2G8` | 2 | 6.018e-08 | `4U69sfcsE4QcWPHf39ZVkxKkJAokFYNHAxTEvMDEVe3eLTaJe6KJrJBF3SefHmwuJ57UMiVzojyExMTSiJCpommY` |
| 11 | `AUrkH8jWtG7our71fSYqCp524BAJuf5Tampe267wfyFA` | 2 | 7.202e-08 | `3LY1AffVooodDxxoyT5U3NGWSVarLuACRnBWmJV7DtKykYYMmfGepnZENuLUJRzpEqrriVhoLfjQqLCRzL2qm2mf` |
| 12 | `EH3kyN7n72ryqkLQLSSPLSTkarPmdWEoG8rWh6Wc23G1` | 3 | 7.516e-08 | `559JhUBNoAS1gBdKxei2TmaJRbCEJQa1KR5mKz3K54EPA83oSticuidjF7y8w4YR6cmRaUs7Yxqz6q9PFRhXLg8P` |
| 13 | `GMorFBqTjJQQ1Fsc5PL9n4JYNMYFzg6eYTCRq9f2SiGc` | 3 | 7.974e-08 | `qAsWkmFCVSfuGodGHtZ9GKdBkFPcxztYnZnA18b5TYCGiFt3LniZtMt52bLu8tSVzxFr4dvXerLvaoYajeUc6A6` |
| 14 | `AEYXnDo7zKJ9xe9qJMwZQpRBxWUcrppDnur1mozP5bvU` | 3 | 8.316e-08 | `2bt5YGqsbPsAfnXrNWv3HmJw9d1oqRjG15WhVWZwAEog5uP653mU5E4ZNjoyrsT482dLYmVVhxSi3QcQttybDwKZ` |
| 15 | `2hN82SXLUffhG3vxpezRmae1sGrCBMqHHBDxjmjpagqk` | 3 | 8.660e-08 | `27VusPY2QNV4txnBTj9BkBgFk3Hz9bEhP3dxH81mcuWb8YanaaNhVu2qGS7r71hEejaQiPnuTbQst23dYkAq1UiP` |
| 16 | `9gpdgZTNxvSrbiBcFM8aWdCUBvQrcprQR4TmsJLU2M9L` | 3 | 1.027e-07 | `QrcuwBfAXANLkFmN6Wv9n7FwwU9FQ3yGy4aqoyBS8XQkv4qxi8rEJisEHRFs9WaBvSpajkg4gnjfMvA72QaGKLe` |
| 17 | `2Cxfdys77rVfcHsedDgBtiCSp839bDufcBt9akDxLNXA` | 4 | 1.061e-07 | `5YtEWwH782wZroFrcyCdtE63EmdpJbJidRfNaVPokz27qHpSy7zYTVw1SSZtCpDM1Cjam68dJu4VtG1yapSyHLRJ` |
| 18 | `3HfK8CJrfJZQjSQkLwS7vrZka8juyWRnmm2Xis9ozF6o` | 4 | 1.063e-07 | `QvGHshmijHs1Z3n9Du2NCdoywT4MQMRLkzU9jGrFUv5sVpT22mHcMDu4CZDemoy1QtZ47dC6U266HskPVMcgnqC` |
| 19 | `3HsjWZJNgoAmhpbK8jGkbqX6zh1dxzctrxdXk46zb4Kt` | 4 | 1.089e-07 | `4n56EjBVadzeiuoXpmzY4xsXvNqgLzXyRnKs79Ku4QQ72fcEZazjogZ3wSDBJKQ3q3gjYd69ESkYyGwaqQdGqgys` |
| 20 | `JARE49JrmtmKeDnSxupb2784awi9HEAapNeRn3tfbRM` | 4 | 1.115e-07 | `EEXSpYYgykwaszM24ZZigJJ2KjjqkjADjCbcJrYHyUjcbC5ze7AeUBwb2hKnGcUTQJ7TkZkggmFuq7ShJV7NCTk` |

### TIGRINO (2026-09-18T09:06:35Z)

| # | Wallet | t+s | Executed price (SOL/token) | Signature |
|---|---|---|---|---|
| 1 | `BzDj6PHVMVSPsHW4KW7q8qupTCpQEKtPRLTnKZkvXkdo` | 2 | 2.960e-08 | `3azvLuijLUaKtp91NB45takNawfG5PiBJyyS63vVXgUUnV35DeiCn7psq19EWrANyiUpsnegCZxGrwYweEpUCvY9` |
| 2 | `CeUn52Jf1zehZmw7YbWW31F9LUypqFxj3qjy5KWvLiPa` | 2 | 3.057e-08 | `4hxm3x7B2haeGr6DvSQcweT8wC95shteMmpBu2gfdVgZR5b6HAeURUJu9toAnm9EqTKnbCMrpUHGP5vcdtnUonJK` |
| 3 | `8aXuJ2X76cDF2wfWWfyzGnscbomZt3w15b2vqaPGpCE` | 2 | 3.153e-08 | `5zMeCVhmjY4e3fb64WJjPfhWdTxzV4gAuJTLKhu2Ayy3u7j2nhH8GgtSvEYu4CyCZQbVFX1kdvwfiVSNF5jBvg77` |
| 4 | `F2jqSx5vi3zwbxcLXq7MWpGBNTBDd6Hffp6up8LTMbAY` | 2 | 3.245e-08 | `4269K9F8Gus45FHMti3ba6jdgiAvTthdKMZYTYnMmS8BxHN2YaodCg3UV1V9Zx9NhwJWBABVs71CU6fzGzCkpBTS` |
| 5 | `B9r6LwaixWL7VB9eSuJb48VWTHjYyhxcHy6a86LAWzzz` | 2 | 4.041e-08 | `5UqnhoQE42wpNRbFtB14VGRrFdUJ1fnQzWV89R6KD8iwrPYzcvZm75B7krCWtxk4M2REuvQEqbfL7NaCUHuigzs8` |
| 6 | `3FKStyPE8daDCcB4rWS8CuoJSbDBnoTR95uTw8hbDHSc` | 2 | 4.120e-08 | `3Zz1AJA8WZTbLhL8LU3UaaoFMBQ6bRVkoACZREAGMnAsGZD5yDhe1kvUcS75MY1BvdXgR9jxF9iEYgdTgXxfrWec` |
| 7 | `7WPMy3C639ETRNHuNedEe2HKEvwHYFgUP4wXwymBmXEg` | 2 | 4.204e-08 | `3xHwQW8YiXj26HfkBXBeG63MXQYE9UgLPbJTvEnSpGGGcZG2vGMKTTrkTncP5YVFtqxQLH1QoHfsK5yKjSFHhhUA` |
| 8 | `88887QrRZPZmsstEXsoXXB8E7nbmUDde4Gp5s7jG3ENu` | 2 | 4.282e-08 | `5zWhodhYDCKNoNt3Y1ka8wtmpNYFKYf8CNxC9cMtRBSC6gFZnArhMiqjyQN5efptDuhMdbhF26zV432eeUfEZHXt` |
| 9 | `9EHhuegvjnaEMGhXmgqhgyVa7fBnmqmYV93KQ76Bb59i` | 2 | 4.873e-08 | `42HVXbe5R5NbMtuGr79gzZMcDzyyRPxhQAUeJmEtd6nS9aGNdrDbwSt2KFrk1M5nfFEkABvjeT57Ku1EYfhaqcjG` |
| 10 | `BukaPo9VfTUUCTWcTNzxetNEDA9pxxzWSjt5anYp6zrZ` | 2 | 4.767e-08 | `2PAZY4yPJs3L3viwG97ZVK6MaJetpTAaurzBq3dMhecEsydW1YULLLtLdWGaUj7vd6nmAdxb1rM2zAhpNWGAdrXF` |
| 11 | `9999huSCf6QpepPHQVFLZ9smhsbwkV4XWPLboiy9qqRj` | 3 | 4.280e-08 | `2RvvQndgMFiTQ2Nm2wEitjyPDd4c6tZL92iuxxNBJnM3Nx721oyQbbkmYLnx4EZH88URztYucCwk6TLAze6gVb82` |
| 12 | `CCCCQCrL6zVjnDeucDzcxJgxAs5ahNmrhw1CDexPhqrd` | 4 | 5.520e-08 | `2ULUtq1E9bpbFrj5UXhkFDnm7qWMxRH2NHRVkkmKHQ1Fr5UkdpToakb6RxNFCiL3NAKLXFsDrJ2RdSedC1fhcACC` |
| 13 | `8pQbCA8NzQZpj1j9FvAAtEb3NAbJnEbyYHFwkm3WqEby` | 5 | 5.774e-08 | `oRAfzy2vSfFuj72Tg6NNXJ2EHMwwrkCsNgkcavHQZPMNLtcW87gEtBJDuiNA2T72TCbMj9K5UVqbeUdVtEaLrUU` |
| 14 | `8GSDbEhMy3LbuLnqyb1ehQr1wwX3yiMN5pL98NnEM32a` | 7 | 5.449e-08 | `4kzYAV3j29NMG5UKCgjqyVrf8GqLp15P9QiXmBV3q8nnZUpz89L4anL3R2N5i8Ko5L3mdcjGmxDzcc6wikeSPm2x` |
| 15 | `9KEaVWe91Yquwex26g2bFn85ij8PaEaUiVkjbLF2X7aX` | 8 | 5.580e-08 | `3tQywC2BE8t9cpVsk3oSxmbdnC8GUzcdAGJYcQhPteo1iLzfsVkGY4CMDTZVoDuwkBqxecXngGJsZpxGZHfmZNRX` |
| 16 | `3HtTsxBfKvPaCfsAVhU91bsQfG9WmHmpohUD1bGrTVVi` | 8 | 5.643e-08 | `5m9RbT5TfPkys7fjkrmSwx6uVqffZkFNkVMn9Yc5pLbaHSjfiTnLV8fMCFwE6xSLXv1XKgnFU4efh5pT7bcbbdve` |
| 17 | `ExqBy6d17yzFx5z44G2m3mbY8iTvMX8XmiYtuvarAud2` | 8 | 5.720e-08 | `4HvrJX1HaPbtWEF1vyvhHA23nN1DQ2VJTSzzdnt2GqJeNDoZGvBKd9TvKhWdXZQjaC4PYo8EGghT2R1kkEUUzbTi` |
| 18 | `sssssDdMNAWKingjpEojkTNdVuZrBe7FsJLaGtexe7d` | 8 | 5.781e-08 | `5q4saaCBDNmRB6XLpvaT9iXDgi57kebyJ39QqhPLahbEobQDgs6eWM1fAPzoV6SU9nSu5JfQwJTgftWGFyVKrrDd` |
| 19 | `3qFAgH2EYb5cPHFchAvv1ErunLqFwfZVtjXYPBeFDknc` | 9 | 7.352e-08 | `3haXADkvemVBYGdyDvcRgoc1CBXHm2Kbn97fyz1mVuUYuGY6uHXh16Ap7DS7UTzjkt3WTvVuNdHf9GTtGThZw7bG` |
| 20 | `HH1MsGCgtT6Bw3yAc3jawS4Y2ZaYFof1hnvUBgQpfukf` | 9 | 3.069e-05 | `5HVCQZhFprw2kADh7ePBcwwR4Q4jgYEeW3zg8JyZAUKATWDK1sb3VzoMatcMBK9WUeuZT3uVFTBSQ6yiA65zdY6S` |
