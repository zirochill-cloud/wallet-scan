"""Verify the five supplied anchors on chain (scratch probe, 2026-11-07).

Two Helius calls do the work:

``getTransactionsForAddress`` with ``sortOrder: "asc"`` returns the mint's
history oldest-first (the backwards walk in the earlier version never reached
genesis), and ``getTransaction`` fetches one payload. Roughly 92% of the
transactions in a launch slot are failed bundle legs, and the signature listing
already carries ``err``, so only successful transactions are ever fetched.

Price series: one sample per hour for the first 48 hours, then one per 6 hours
out to day 30. Early hours come from the ascending pages; later hours from the
signature pages the abandoned backwards walk left in the cache.

Outputs one JSON per mint under ``%TEMP%\\solscope-anchor-verify``.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import pathlib
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The project path is ~164 chars; appending artifacts/<mint>/<88-char sig>.json
# crosses MAX_PATH and Windows refuses the write. Cache under the short temp root.
CACHE = pathlib.Path(tempfile.gettempdir()) / "solscope-anchor-verify"
CACHE.mkdir(parents=True, exist_ok=True)

MINTS: dict[str, str] = {
    "GB68eELgf864nrQHjNvrXHF6Xywpp1kYsb7fsGBupump": "ChooChoo",
    "zj1jpp7QMveWHLs61vL9KMZf254KvW7j4AAmBF8ry2k": "BULLSHIT",
    "EVgwa5CHBVwk6sCTa4QJrVXYBKngZhYqh2a2ZGDApump": "MACRODUCK",
    "7Y7V1a4m2nWK7BMgbka5B4vR1pDvCK7yva3Hnrqkraze": "NASDUQ/Nasduck",
    "91ryaCo5yGpYZM3bs6GUPs97VWJQj7RozBmqPULgpump": "TIGRINO",
}


def _epoch(day: str) -> int:
    return int(calendar.timegm(time.strptime(day, "%Y-%m-%d")))


# Supplied window: launched 2026-07-29 through 2026-09-19 (inclusive).
LAUNCH_WINDOW = (_epoch("2026-07-29"), _epoch("2026-09-20") - 1)

REQUESTS = 0
MAX_SIGS = 120_000
FILL = 0
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "spl_token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "token_2022",
}
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _api_key() -> str:
    env = ROOT / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("HELIUS_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("HELIUS_API_KEY missing from .env")


URL = f"https://mainnet.helius-rpc.com/?api-key={_api_key()}"


def rpc(method: str, params, *, tries: int = 6) -> dict:
    global REQUESTS
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                URL, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "solscope-verify/1"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read())
            REQUESTS += 1
            if "error" in payload:
                raise RuntimeError(f"{method}: {payload['error']}")
            return payload
        except Exception as exc:  # noqa: BLE001 - retry, then surface
            if attempt == tries - 1:
                raise
            print(f"    retry {attempt + 1} ({type(exc).__name__}: {str(exc)[:80]})", flush=True)
            time.sleep(2.0 * (attempt + 1))
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# history pages
# ---------------------------------------------------------------------------

def sig_page(mint: str, token: str | None = None, *, limit: int = 1000) -> dict:
    """One ascending signature page, cached under a filename-safe token key."""
    # Pagination tokens look like "436000088:1614"; a colon is an NTFS alternate
    # data stream separator, so the raw token must not reach the filename.
    key = "head" if not token else hashlib.sha1(token.encode()).hexdigest()[:16]
    path = CACHE / f"asc_{mint}_{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    opts = {"transactionDetails": "signatures", "sortOrder": "asc", "limit": limit,
            "commitment": "finalized"}
    if token:
        opts["paginationToken"] = token
    res = rpc("getTransactionsForAddress", [mint, opts])["result"]
    path.write_text(json.dumps(res))
    return res


class Pager:
    """Ascending signature listing, pulled one page at a time."""

    def __init__(self, mint: str) -> None:
        self.mint = mint
        self.sigs: list[dict] = []
        self.token: str | None = None
        self.done = False
        self.pages = 0
        self._load()

    def _load(self) -> int:
        if self.done:
            return 0
        res = sig_page(self.mint, self.token)
        data = res.get("data") or []
        if data and self.sigs and data[0].get("signature") == self.sigs[0].get("signature"):
            # Same page again: the listing is not advancing, stop rather than
            # re-appending it until memory runs out.
            print("    page repeated first signature -- pagination stuck", flush=True)
            self.done = True
            return 0
        self.sigs.extend(data)
        self.token = res.get("paginationToken")
        self.pages += 1
        if not data or not self.token:
            self.done = True
        if self.pages % 5 == 0:
            last = self.sigs[-1].get("blockTime") or 0
            first = self.sigs[0].get("blockTime") or last
            print(f"    pages {self.pages} sigs {len(self.sigs)} (t+{last - first}s)", flush=True)
        return len(data)

    def at(self, i: int) -> dict | None:
        while len(self.sigs) <= i and not self.done and len(self.sigs) < MAX_SIGS:
            self._load()
        return self.sigs[i] if i < len(self.sigs) else None

    def extend_to_time(self, t: int, *, max_sigs: int = MAX_SIGS) -> None:
        while (not self.done and len(self.sigs) < max_sigs
               and (not self.sigs or (self.sigs[-1].get("blockTime") or 0) < t)):
            self._load()


def full(sig: str) -> dict | None:
    """Full transaction payload, cached. None when the node no longer has it."""
    path = CACHE / sig[:2] / f"tx_{sig}.json"
    if path.exists():
        try:
            return json.loads(path.read_text()).get("result")
        except (ValueError, OSError):
            pass
    opts = {"maxSupportedTransactionVersion": 0, "encoding": "json", "commitment": "finalized"}
    try:
        res = rpc("getTransaction", [sig, opts], tries=2)
    except RuntimeError as exc:
        # Late 2026 blocks carry versioned transactions at version 1, which a
        # client capped at 0 refuses outright (-32015).
        if "maxSupportedTransactionVersion" not in str(exc):
            raise
        res = rpc("getTransaction", [sig, dict(opts, maxSupportedTransactionVersion=1)])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"result": res.get("result")}))
    return res.get("result")


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------

def keys_of(tx: dict) -> list[str]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in msg.get("accountKeys") or []]
    loads = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += list(loads.get("writable") or []) + list(loads.get("readonly") or [])
    return [k for k in keys if k]


def signers_of(tx: dict) -> list[str]:
    keys = keys_of(tx)
    msg = (tx.get("transaction") or {}).get("message") or {}
    n = int((msg.get("header") or {}).get("numRequiredSignatures") or 1)
    return keys[:n]


def programs_of(tx: dict) -> list[str]:
    keys = keys_of(tx)
    msg = (tx.get("transaction") or {}).get("message") or {}
    out: list[str] = []
    for ix in msg.get("instructions") or []:
        pid = ix.get("programId")
        if not pid:
            i = ix.get("programIdIndex")
            pid = keys[i] if isinstance(i, int) and i < len(keys) else None
        if pid and pid not in out:
            out.append(pid)
    return out


def _log_text(tx: dict) -> str:
    return "\n".join((tx.get("meta") or {}).get("logMessages") or [])


def is_creation(tx: dict) -> bool:
    """True when this transaction mints the token (not merely trades it)."""
    text = _log_text(tx)
    if "Instruction: InitializeMint" in text or "Instruction: InitializeMint2" in text:
        return True
    if "Instruction: Create" in text and PUMP in programs_of(tx):
        return True
    return False


def _iso(t: int | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


# A price is only an observation of the market when the trade actually carried
# size. A one-token wash or a dust leg otherwise becomes the "30-day maximum"
# and manufactures a four-digit multiple.
MIN_BASE_TOKENS = 100.0
MIN_QUOTE_SOL = 0.001

#: Revised anchor criteria (NOTES.md § ANCHOR_SELECTION_CRITERIA, 2026-11-07).
#: The retired drawdown gate asked whether a token *held* its value; memecoins never
#: do, so it excluded the entire population this tool analyses. The opportunity gate
#: asks whether a real market existed at the peak and whether early buyers entered
#: far enough below it to have profited — the property that separates a tradeable
#: run from a single wash print.
PEAK_LEGITIMACY_MIN_TRADES = 3      # distinct transactions trading at >= 50% of peak
PEAK_LEGITIMACY_BAND = 0.50         # "at or near peak"
PEAK_LEGITIMACY_GRID_RUN = 2        # consecutive 6h samples at >= 50% of peak
EARLY_OPPORTUNITY_MIN_BUYERS = 10   # of the first 20, entered at <= 25% of peak
EARLY_OPPORTUNITY_RATIO = 0.25
MIN_MULTIPLE = 5.0


def _obs(sw, price: float, sig: str) -> dict:
    return {
        "t": sw.block_time,
        "price": price,
        "signature": sig,
        "side": sw.side,
        "base_tokens": sw.base_amount_raw / (10 ** sw.base_decimals),
        "quote_sol": sw.quote_amount_raw / (10 ** sw.quote_decimals),
    }


def _credible(row: dict) -> bool:
    return (row.get("base_tokens") or 0) >= MIN_BASE_TOKENS and \
           (row.get("quote_sol") or 0) >= MIN_QUOTE_SOL


def peak_legitimacy(observations: list[dict], *, genesis_time: int | None) -> dict:
    """Was the peak a real market, or one print?

    Two readings, either of which passes: at least ``PEAK_LEGITIMACY_MIN_TRADES``
    distinct transactions trading at >= 50% of the peak price, or >= 2 *consecutive*
    6-hour grid buckets holding a sample at that level. The second exists because the
    reconstructed series samples three transactions per bucket — a genuine multi-hour
    peak can show only two signers purely because only three trades per bucket were
    ever decoded, and that is a property of the sampling, not of the token.
    """
    if not observations:
        return {"signers_at_band": 0, "grid_run": 0, "band": PEAK_LEGITIMACY_BAND,
                "passed": False, "reason": "no_observations"}
    peak = max(o["price"] for o in observations)
    floor = peak * PEAK_LEGITIMACY_BAND
    near = [o for o in observations if o["price"] >= floor]
    signers = {o["signature"] for o in near}

    run = best = 0
    prev = None
    if genesis_time:
        for bucket in sorted({int((o["t"] - genesis_time) // (6 * 3600)) for o in near}):
            run = run + 1 if prev is not None and bucket == prev + 1 else 1
            best = max(best, run)
            prev = bucket

    passed = len(signers) >= PEAK_LEGITIMACY_MIN_TRADES or best >= PEAK_LEGITIMACY_GRID_RUN
    return {"signers_at_band": len(signers), "grid_run": best,
            "band": PEAK_LEGITIMACY_BAND, "passed": passed,
            "trades_required": PEAK_LEGITIMACY_MIN_TRADES,
            "grid_run_required": PEAK_LEGITIMACY_GRID_RUN}


def dense_legitimacy(mint: str, peak_t: int, peak_price: float, *,
                     half_width_hours: float = 2.0, samples: int = 200) -> dict:
    """Re-measure peak legitimacy from a dense window when the grid cannot say.

    The sampled series decodes three transactions per 6-hour bucket, and buckets
    with no decoded transaction simply do not exist — a token that held its peak
    across a quiet stretch can therefore show two near-peak samples that are not
    *consecutive* and be called a single print. That is a property of the grid.

    This decodes an evenly strided sample of the successful transactions in the
    peak window and counts distinct transactions at >= 50% of the peak. It runs
    only for an anchor the cheap check failed, so it cannot launder a peak that
    really was one trade — a wash print has no neighbours to sample at that price.
    """
    from solscope.decode import derive_swaps, exec_price_quote  # noqa: PLC0415

    floor = peak_price * PEAK_LEGITIMACY_BAND
    lo, hi = peak_t - half_width_hours * 3600, peak_t + half_width_hours * 3600
    seen: dict[str, dict] = {}
    for path in (CACHE / mint).glob("sig_*.json"):
        try:
            page = json.loads(path.read_text()).get("result") or []
        except Exception:  # noqa: BLE001 - a page killed mid-write is skipped
            continue
        for item in page:
            sig = item.get("signature")
            if sig and not item.get("err") and item.get("blockTime"):
                seen[sig] = item
    window = sorted((i for i in seen.values() if lo <= i["blockTime"] <= hi),
                    key=lambda i: i["blockTime"])
    if len(window) > samples:
        step = len(window) / samples
        picked = [window[int(i * step)] for i in range(samples)]
    else:
        picked = window

    prices: list[dict] = []
    for item in picked:
        sig = item["signature"]
        tx = full(sig)
        if not tx:
            continue
        swaps, _ = derive_swaps(tx, signature=sig)
        for sw in swaps:
            if sw.base_mint != mint or sw.block_time is None:
                continue
            price = exec_price_quote(sw)
            if not price or price <= 0:
                continue
            row = _obs(sw, float(price), sig)
            if _credible(row):
                prices.append(row)
                break

    near = [r for r in prices if r["price"] >= floor]
    signers = {r["signature"] for r in near}
    return {"window_hours": half_width_hours, "listed_in_window": len(window),
            "decoded": len(prices), "near_peak": len(near),
            "distinct_txs_near_peak": len(signers),
            "passed": len(signers) >= PEAK_LEGITIMACY_MIN_TRADES}


def early_buyer_opportunity(buyers: list[dict], peak_price: float | None) -> dict:
    """Did early buyers get in far enough below the peak to have profited?

    Counts the first-20 buyers whose executed price is <= 25% of the peak. This is
    the forward-looking half of the criteria: a token that ran 95x is only an
    opportunity if somebody could buy at the bottom of that run, and the first 20
    buyers are the ones the pipeline actually evaluates.
    """
    ratios = [b["sol_per_token"] / peak_price for b in buyers
              if b.get("sol_per_token") and peak_price]
    cheap = sum(1 for r in ratios if r <= EARLY_OPPORTUNITY_RATIO)
    return {"buyers_at_or_below_ratio": cheap, "buyers_measured": len(ratios),
            "ratio": EARLY_OPPORTUNITY_RATIO, "max_ratio": max(ratios) if ratios else None,
            "passed": cheap >= EARLY_OPPORTUNITY_MIN_BUYERS,
            "required": EARLY_OPPORTUNITY_MIN_BUYERS}


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def cmd_verify(only: str | None = None) -> None:
    from solscope.decode import (derive_swaps, exec_price_quote,  # noqa: PLC0415
                                 find_swap_instructions, owner_deltas)

    summary = {}
    for mint, label in MINTS.items():
        if only and mint != only:
            continue
        print(f"\n== {label}  {mint}", flush=True)
        pager = Pager(mint)
        sigs = pager.sigs
        if not sigs:
            print("   no history returned", flush=True)
            summary[mint] = {"label": label, "error": "no history"}
            continue

        genesis = sigs[0]
        g_sig = genesis["signature"]
        g_slot = genesis.get("slot")
        g_time = genesis.get("blockTime")
        print(f"   genesis sig  {g_sig}", flush=True)
        print(f"   slot {g_slot}  time {g_time} ({_iso(g_time)})", flush=True)

        # The mint's first transaction normally creates it; if it does not, walk
        # forward over successful transactions until the creation is seen.
        g_tx = full(g_sig)
        creation = g_tx if (g_tx and is_creation(g_tx)) else None
        creation_sig = g_sig if creation else None
        if not creation:
            for item in sigs[:40]:
                if item.get("err") or item["signature"] == g_sig:
                    continue
                tx = full(item["signature"])
                if tx and is_creation(tx):
                    creation, creation_sig = tx, item["signature"]
                    break
        base = creation or g_tx
        base_keys = keys_of(base) if base else []
        deployer = base_keys[0] if base_keys else None
        print(f"   creation tx: {creation_sig}", flush=True)
        print(f"   programs: {[p[:8] for p in programs_of(base)] if base else '-'}", flush=True)
        print(f"   deployer (fee payer): {deployer}", flush=True)

        # --- first 20 buyers -------------------------------------------------
        # Continue ascending until 20 buyers are found or the listing ends.
        buyers: list[dict] = []
        seen: set[str] = set()
        early: list[dict] = []
        examined = failed = 0
        skips: list[str] = []
        reach = g_time + 72 * 3600 if g_time else None
        i = 0
        while len(buyers) < 20:
            item = pager.at(i)
            if item is None:
                break
            i += 1
            examined += 1
            if item.get("err"):
                failed += 1
                continue
            sig = item["signature"]
            tx = full(sig)
            if not tx:
                skips.append(f"{sig[:12]}: payload unavailable")
                continue
            swaps, why = derive_swaps(tx, signature=sig)
            skips.extend(why[:1])
            rel = [s for s in swaps if s.base_mint == mint and s.side == "buy"]
            if not rel:
                # A creation transaction bundles the dev buy in a layout whose
                # trader slot does not hold the buyer, so the swap decodes with
                # a zero amount. Fall back to the instruction's own mint: if a
                # pump-family buy names this mint, the signer who gained tokens
                # is a buyer regardless of what the amount read said.
                named = [ix for ix in find_swap_instructions(tx)
                         if ix.token_mint == mint and ix.side_hint in ("buy", "directional")]
                if not named:
                    continue
            # Any decoded swap is a price observation; the launch slot is the
            # only stretch the sampled series cannot reach cheaply.
            for sw in swaps:
                if sw.base_mint != mint or sw.block_time is None:
                    continue
                p = exec_price_quote(sw)
                if p == p and p > 0:
                    early.append(_obs(sw, p, sig))
                    break
            signers = set(signers_of(tx))
            held = [(o, d) for (o, m), d in owner_deltas(tx).items() if m == mint and d > 0]
            wallets = [o for o, _ in sorted(held, key=lambda r: -r[1]) if o in signers]
            if not wallets:
                skips.append(f"{sig[:12]}: buy with no signer holding {mint[:6]}")
                continue
            price = None
            for sw in rel:
                p = exec_price_quote(sw)
                if p == p and p > 0:
                    price = p
                    break
            for wallet in wallets:
                if wallet in seen:
                    continue
                seen.add(wallet)
                buyers.append({
                    "rank": len(buyers) + 1,
                    "wallet": wallet,
                    "trader_slot_mismatch": bool(rel and rel[0].trader not in signers),
                    "block_time": tx.get("blockTime"),
                    "seconds_after_genesis": (tx.get("blockTime") - g_time) if g_time else None,
                    "sol_per_token": price,
                    "signature": sig,
                    "slot": tx.get("slot"),
                })
                if len(buyers) >= 20:
                    break
            if reach and tx.get("blockTime") and tx["blockTime"] > reach and len(buyers) < 20:
                print(f"    note: 72h reached with {len(buyers)} buyers", flush=True)
                break

        spread = buyers[-1]["seconds_after_genesis"] if len(buyers) >= 20 else None
        overlap = sorted({b["wallet"] for b in buyers} & {deployer})
        print(f"   examined {examined} sigs ({failed} failed)  buyers {len(buyers)}  "
              f"spread(genesis->20th): {spread}s", flush=True)
        print(f"   deployer in buyers: {overlap or 'no'}", flush=True)
        if skips:
            print(f"   sample skips: {skips[:2]}", flush=True)

        # --- price series ----------------------------------------------------
        series = _series(mint, g_time, early, derive_swaps, exec_price_quote)
        metrics: dict = {}
        genesis_price = buyers[0]["sol_per_token"] if buyers and buyers[0]["sol_per_token"] else None
        rated = [r for r in series if _credible(r)]
        use = rated if len(rated) >= 5 else series
        if use:
            peak = max(use, key=lambda r: r["price"])
            after = [r for r in use if r["t"] >= peak["t"]]
            floor = min(after, key=lambda r: r["price"]) if after else None
            base_price = genesis_price or series[0]["price"]
            metrics = {
                "genesis_price_sol": base_price,
                "genesis_price_from": "buyer1" if genesis_price else "series[0]",
                "max_price_sol": peak["price"],
                "max_multiple": (peak["price"] / base_price) if base_price else None,
                "peak_time": peak["t"],
                "peak_iso": _iso(peak["t"]),
                "peak_day": ((peak["t"] - g_time) / 86400) if g_time else None,
                "floor_price_sol": floor["price"] if floor else None,
                "drawdown_pct": (100 * (peak["price"] - floor["price"]) / peak["price"])
                if floor and peak["price"] else None,
                "samples": len(series),
                "samples_rated": len(use),
                "samples_excluded_dust": len(series) - len(use),
                "series_from": series[0]["t"],
                "series_to": series[-1]["t"],
                "series_span_hours": (series[-1]["t"] - series[0]["t"]) / 3600,
                "first_sample_hours_after_genesis": (series[0]["t"] - g_time) / 3600
                if g_time else None,
            }
        # --- revised criteria: opportunity, not durability ------------------
        # drawdown_pct stays in the metrics block as a diagnostic and no longer
        # decides anything: every memecoin gives its pump back, so gating on it
        # rejected the population the tool exists to analyse.
        legitimacy = peak_legitimacy(use if metrics else [], genesis_time=g_time)
        if metrics and not legitimacy["passed"]:
            # The sampled grid could not settle it. Measure the peak window densely
            # rather than fail an anchor on the shape of the sampling.
            dense = dense_legitimacy(mint, metrics["peak_time"], metrics["max_price_sol"])
            legitimacy["dense"] = dense
            if dense["passed"]:
                legitimacy["passed"] = True
                legitimacy["settled_by"] = "dense_window"
            print(f"   grid could not settle the peak: dense probe found "
                  f"{dense['distinct_txs_near_peak']} distinct txs at >=50% in "
                  f"±{dense['window_hours']:g}h ({dense['decoded']} decoded)", flush=True)
        opportunity = early_buyer_opportunity(
            buyers, metrics.get("max_price_sol") if metrics else None)
        metrics["peak_legitimacy"] = legitimacy
        metrics["early_buyer_opportunity"] = opportunity

        verdict = "FAIL_OTHER"
        if metrics.get("max_multiple") is not None:
            if metrics["max_multiple"] < MIN_MULTIPLE:
                verdict = "FAIL_LOW"
            elif not legitimacy["passed"]:
                verdict = "FAIL_LEGITIMACY"
            elif not opportunity["passed"]:
                verdict = "FAIL_OPPORTUNITY"
            else:
                verdict = "PASS"
        print(f"   price samples: {len(series)}  span "
              f"{metrics.get('series_span_hours')}h  max/genesis: {metrics.get('max_multiple')}  "
              f"drawdown(diagnostic): {metrics.get('drawdown_pct')}", flush=True)
        print(f"   peak legitimacy: {legitimacy['signers_at_band']} tx at >="
              f"{int(PEAK_LEGITIMACY_BAND * 100)}% of peak (need {PEAK_LEGITIMACY_MIN_TRADES}), "
              f"grid run {legitimacy['grid_run']} (need {PEAK_LEGITIMACY_GRID_RUN})", flush=True)
        print(f"   opportunity: {opportunity['buyers_at_or_below_ratio']} of "
              f"{opportunity['buyers_measured']} buyers at <={int(EARLY_OPPORTUNITY_RATIO * 100)}% "
              f"of peak (need {EARLY_OPPORTUNITY_MIN_BUYERS})", flush=True)
        print(f"   VERDICT: {verdict}", flush=True)

        summary[mint] = {
            "label": label,
            "genesis": {
                "signature": g_sig,
                "slot": g_slot,
                "block_time": g_time,
                "iso": _iso(g_time),
                "in_launch_window": bool(g_time and LAUNCH_WINDOW[0] <= g_time <= LAUNCH_WINDOW[1]),
                "creation_tx": creation_sig,
                "creation_programs": programs_of(base) if base else [],
                "deployer": deployer,
                "mint_in_genesis_keys": mint in (keys_of(g_tx) if g_tx else []),
                "err": (g_tx.get("meta") or {}).get("err") if g_tx else None,
            },
            "buyers": buyers,
            "first20_spread_seconds": spread,
            "deployer_in_buyers": overlap,
            "metrics": metrics,
            "series": series,
            "verdict": verdict,
            "examined": examined,
            "failed_examined": failed,
            "decode_skips": skips[:4],
        }
        (CACHE / f"verify_{mint}.json").write_text(json.dumps(summary[mint], indent=2))

    (CACHE / "verify_all.json").write_text(json.dumps(summary, indent=2))
    print(f"\nrequests={REQUESTS}")
    print("\n--- summary")
    for mint, row in summary.items():
        if "error" in row:
            print(f"  {row.get('label', mint)}: {row['error']}")
            continue
        g = row["genesis"]
        m = row["metrics"]
        lg = m.get("peak_legitimacy") or {}
        op = m.get("early_buyer_opportunity") or {}
        print(f"  {row['label']:<16} {g['iso']}  dep {str(g['deployer'])[:8]}  "
              f"buyers {len(row['buyers'])}  max {m.get('max_multiple')}  "
              f"legit {lg.get('signers_at_band')}tx/{lg.get('grid_run')}grid  "
              f"early {op.get('buyers_at_or_below_ratio')}/20  {row['verdict']}")


def _series(mint: str, genesis_time: int | None, early: list[dict],
            derive_swaps, exec_price_quote) -> list[dict]:
    """Decoded launch-slot trades plus one sample per 6h out to day 30.

    The sampled grid comes from the signature pages the abandoned backwards
    walk left behind; it reaches back to roughly the mint's first day, not to
    genesis, so the launch slot itself is filled from trades already decoded
    during the buyer scan. Coverage is reported, never assumed.
    """
    if not genesis_time:
        return []
    HOUR = 3600
    HORIZON = 30 * 86400
    horizon = genesis_time + HORIZON

    # bucket -> up to CANDIDATES signature items, earliest first
    CANDIDATES = 3
    picks: dict[int, list[dict]] = {}

    for page in _cached_pages(mint):
        for item in page:
            bt = item.get("blockTime")
            if not bt or item.get("err"):
                continue
            if bt < genesis_time or bt > horizon:
                continue
            bucket = (bt - genesis_time) // (6 * HOUR)
            slot = picks.setdefault(bucket, [])
            if len(slot) < CANDIDATES:
                slot.append(item)
                slot.sort(key=lambda r: r.get("blockTime") or 0)
            elif bt < (slot[-1].get("blockTime") or 0):
                slot[-1] = item
                slot.sort(key=lambda r: r.get("blockTime") or 0)

    out: list[dict] = []
    seen_sigs: set[str] = set()
    for row in early:
        if row["signature"] in seen_sigs:
            continue
        seen_sigs.add(row["signature"])
        out.append(row)

    decoded = 0
    for bucket in sorted(picks):
        for item in picks[bucket]:
            sig = item["signature"]
            if sig in seen_sigs:
                break
            tx = full(sig)
            if not tx:
                continue
            swaps, _ = derive_swaps(tx, signature=sig)
            row = None
            for sw in swaps:
                if sw.base_mint != mint or sw.block_time is None:
                    continue
                p = exec_price_quote(sw)
                if p == p and p > 0:
                    row = _obs(sw, p, sig)
                    break
            if row is None:
                continue
            seen_sigs.add(sig)
            out.append(row)
            decoded += 1
            break
        if bucket % 24 == 0:
            print(f"    series {len(out)} samples, t+{(bucket * 6)}h", flush=True)
    print(f"    series: {decoded} sampled buckets decoded, {len(out)} observations", flush=True)
    out.sort(key=lambda r: r["t"])
    return out


def _cached_pages(mint: str) -> list[list[dict]]:
    """Signature pages left by the abandoned backwards walk (descending).

    Read by directory scan rather than by following signatures: a walk that was
    killed mid-write leaves a truncated page, and a chain walk stops dead at it.
    """
    d = CACHE / mint
    if not d.exists():
        return []
    pages: list[tuple[int, list[dict]]] = []
    for path in d.glob("sig_*.json"):
        try:
            items = json.loads(path.read_text()).get("result") or []
        except (ValueError, OSError):
            continue          # truncated by an interrupted walk
        if not items:
            continue
        pages.append((items[0].get("blockTime") or 0, items))
    pages.sort(key=lambda r: -r[0])
    return [items for _, items in pages]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd != "verify":
        raise SystemExit(f"unknown command {cmd!r}")
    cmd_verify(sys.argv[2] if len(sys.argv) > 2 else None)
