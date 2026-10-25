"""Phase 2 derivation pipeline (doc 07 Phase 2, doc 02 §4.1, doc 10).

Pulls recent signatures for known pools, fetches each transaction (1 credit,
charged through the ledger with retry-safe request keys), archives the payload
verbatim, decodes swaps via :mod:`solscope.decode`, and persists
``transactions``/``swaps`` rows. Then ``--gate`` reconciles locally derived
prices against a contemporaneous DexScreener snapshot — the project's
go/no-go criterion.

Pool-state observation for the gate: when a transaction's token balances
include an account *owned by the pool itself*, its post-trade reserves give a
same-transaction spot price (``pool_price_quote``) with zero time skew. This
is NOT the naive largest-balance vault guess that doc 10 measured at 4–5
orders of magnitude off: ownership comes from the instruction's own account
list (the registry pins which account is the pool), and a pool with no
observed vault is recorded as unknown, never guessed.

Spend discipline: every RPC call charges the ledger before it is made, so a
runaway fetch loop trips the monthly cap (Phase 0) instead of the provider's
patience. DexScreener is keyless but still request-metered.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from dataclasses import dataclass, field

from .archive import RawArchive
from .decode import (DerivedSwap, NATIVE_SENTINEL, WSOL_MINT, USDC_MINT,
                     account_keys, derive_swaps, exec_price_quote)
from .adapters.httpfetch import FetchError, fetch_bytes

def _num(value):
    """float or None — provider numerics arrive as strings or numbers."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


DS_TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
DS_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs/solana/{pair}"

# Both token programs: a pool's vaults may live under either, and a reserve
# read that only covers the legacy program silently halves a Token-2022 pool.
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class GateRow:
    """One pool's reconciliation line in the gate report."""
    pool_address: str
    base_mint: str
    quote_mint: str
    swaps_decoded: int
    derived_price_quote: float | None     # median exec price over the window
    snapshot_price_quote: float | None    # DexScreener priceNative
    deviation_pct: float | None
    derived_pool_price_quote: float | None  # same-tx reserve spot, when observed
    derived_liquidity_usd: float | None
    snapshot_liquidity_usd: float | None
    liquidity_dev_pct: float | None
    slippage_bps: float | None            # exec vs same-tx spot, signed
    slippage_plausible: bool | None
    note: str = ""
    # Reserve-level evidence (added with the 2026-10-25 gate rewrite). Reserve
    # comparison is the defensible test: it compares like with like.
    base_reserve_dev_pct: float | None = None
    quote_reserve_dev_pct: float | None = None
    dex_price_native: float | None = None   # informational only — trade price
    # True when the pool's own price moved further than the gate's tolerance
    # between the two reserve reads that bracket the snapshot. Such a pool
    # cannot be compared like-for-like — its base side was read on one side of a
    # trade and its quote side on the other — so it is reported but excluded
    # from the deviation statistics. Pools that held still are measured normally.
    pool_moved: bool = False
    # How far the pool itself moved during the measurement window. This is the
    # measurement's own noise floor: a deviation smaller than the drift is not
    # distinguishable from the pool having simply traded.
    reserve_drift_pct: float | None = None

    @property
    def skew_signature(self) -> bool:
        """True when the two reserve deviations have opposite signs.

        A genuine derivation error moves both reserves the same way (the
        reading is systematically high or low). Opposite signs mean the base
        side was measured on one side of a trade and the quote side on the
        other — i.e. the pool traded between our read and the provider's
        snapshot. That distinction is what separates "our method is wrong" from
        "these two numbers are a few seconds apart", and on a memecoin they are
        easy to confuse.
        """
        if self.base_reserve_dev_pct is None or self.quote_reserve_dev_pct is None:
            return False
        return (self.base_reserve_dev_pct > 0) != (self.quote_reserve_dev_pct > 0)


@dataclass
class GateReport:
    rows: list[GateRow] = field(default_factory=list)
    tolerance_pct: float = 5.0
    slippage_limit_bps: float = 500.0     # |exec-spot| beyond 5% is implausible
    fetched_at: str = ""
    required_pairs: int = 20
    # A mean-only test lets a few badly wrong pools hide behind a good average;
    # an all-pools test fails the whole gate on one volatile pool that simply
    # traded between our reserve read and the provider's snapshot. Requiring a
    # majority of pools to agree individually resists both. Pre-registered here
    # and recorded in docs/11-phase2-gate.md rather than tuned per run.
    min_pass_rate: float = 0.80

    @property
    def priced_rows(self) -> list[GateRow]:
        """Rows that were actually measured — excluding pools that moved."""
        return [r for r in self.rows
                if r.deviation_pct is not None and not r.pool_moved]

    @property
    def moved_rows(self) -> list[GateRow]:
        return [r for r in self.rows if r.pool_moved]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.priced_rows
                   if abs(r.deviation_pct) <= self.tolerance_pct)

    @property
    def liquidity_rows(self) -> list[GateRow]:
        return [r for r in self.rows if r.liquidity_dev_pct is not None]

    @property
    def liquidity_passed(self) -> int:
        return sum(1 for r in self.liquidity_rows
                   if abs(r.liquidity_dev_pct) <= self.tolerance_pct)

    @property
    def slippage_rows(self) -> list[GateRow]:
        return [r for r in self.rows if r.slippage_plausible is not None]

    @property
    def slippage_ok(self) -> int:
        return sum(1 for r in self.slippage_rows if r.slippage_plausible)

    @property
    def mean_deviation(self) -> float | None:
        devs = [abs(r.deviation_pct) for r in self.priced_rows]
        return sum(devs) / len(devs) if devs else None

    @property
    def max_deviation(self) -> float | None:
        devs = [abs(r.deviation_pct) for r in self.priced_rows]
        return max(devs) if devs else None

    @property
    def mean_liquidity_deviation(self) -> float | None:
        devs = [abs(r.liquidity_dev_pct) for r in self.liquidity_rows]
        return sum(devs) / len(devs) if devs else None

    def verdict(self, *, min_pairs: int = 20) -> tuple[bool, str]:
        """The doc 07 gate, with all three conditions stated rather than fudged.

        A mean-only test would let a handful of wildly wrong pools hide behind
        a good average, so every priced pool must also be inside tolerance and
        the liquidity side must be measured and inside it too.
        """
        n = len(self.priced_rows)
        if n < min_pairs:
            return False, (f"only {n}/{min_pairs} live pairs produced a comparable "
                           f"derived price — not enough evidence to judge")
        if self.mean_deviation is None:
            return False, "no priced pairs"
        over = [r for r in self.priced_rows
                if abs(r.deviation_pct) > self.tolerance_pct]
        pass_rate = (n - len(over)) / n
        mean_liq = self.mean_liquidity_deviation
        parts = [f"{n} pools measured",
                 f"price mean {self.mean_deviation:.2f}% max {self.max_deviation:.2f}%",
                 f"{len(over)}/{n} pools outside {self.tolerance_pct:.1f}% "
                 f"({pass_rate:.0%} pass, need {self.min_pass_rate:.0%})"]
        if self.moved_rows:
            # Reported, never silent: excluding pools is legitimate only when
            # the reader can see how many were excluded and why.
            parts.append(f"{len(self.moved_rows)} excluded (traded mid-window)")
        ok = (self.mean_deviation <= self.tolerance_pct
              and pass_rate >= self.min_pass_rate)
        if mean_liq is None:
            ok = False
            parts.append("liquidity NOT MEASURED")
        else:
            parts.append(f"liquidity mean {mean_liq:.2f}%")
            if mean_liq > self.tolerance_pct:
                ok = False
        if not ok and over and pass_rate < self.min_pass_rate:
            parts.append("TOO MANY POOLS OUTSIDE TOLERANCE")
        elif not ok and mean_liq is not None and mean_liq > self.tolerance_pct:
            parts.append("LIQUIDITY OUTSIDE TOLERANCE")
        return ok, "; ".join(parts)


class Deriver:
    """Fetch → archive → decode → persist for one batch of pools."""

    def __init__(self, *, rpc, conn, ledger, budget, data_dir="data/raw",
                 clock=None, log=print, sleep=time.sleep):
        self.rpc = rpc
        self.conn = conn
        self.ledger = ledger
        self.budget = budget
        self.archive = RawArchive(data_dir, conn)
        self._now = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._log = log
        self._sleep = sleep

    # -- registry access -------------------------------------------------

    def known_pools(self) -> dict[str, tuple[str, str]]:
        """pool_address -> (base_mint, quote_mint) from the pools registry."""
        if self.conn is None:
            return {}
        rows = self.conn.execute(
            "SELECT pool_address, base_mint, COALESCE(quote_mint, %s) "
            "FROM pools WHERE is_active", (NATIVE_SENTINEL,)).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    # -- fetching ---------------------------------------------------------

    def fetch_signatures(self, pool: str, *, limit: int = 50) -> list[str]:
        """Recent signatures for a pool address (1 credit, retry-safe)."""
        result, _body = self.rpc.signatures_for_address(
            pool, limit=limit, request_key=f"derive:sigs:{pool}:{int(self._now().timestamp() // 300)}")
        out = []
        for item in (result.get("result") or []):
            sig = item.get("signature")
            if sig and not item.get("err"):
                out.append(sig)
        return out

    def fetch_transaction(self, sig: str):
        """One getTransaction (1 credit), archived verbatim before decoding."""
        result, body = self.rpc.transaction(sig)
        payload = result.get("result")
        if not payload:
            return None
        self.archive.write(source="helius", endpoint=f"getTransaction:{sig}",
                           body=body, tag=f"tx_{sig[:16]}", subdir="tx",
                           credits_spent=1)
        return payload

    def already_decoded(self, sig: str) -> bool:
        if self.conn is None:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM transactions WHERE signature = %s", (sig,)).fetchone()
        return row is not None

    # -- persistence ------------------------------------------------------

    def persist(self, swap: DerivedSwap, *, raw_path: str | None = None) -> bool:
        """Insert transactions + swaps + wallet/token stubs. Idempotent."""
        if self.conn is None:
            return False
        ix = swap.instruction
        try:
            self.conn.execute(
                "INSERT INTO chains (id, display_name) VALUES ('solana','Solana') "
                "ON CONFLICT DO NOTHING")
            for mint in {swap.base_mint, swap.quote_mint}:
                self.conn.execute(
                    "INSERT INTO tokens (mint, chain_id) VALUES (%s,'solana') "
                    "ON CONFLICT DO NOTHING", (mint,))
            self.conn.execute(
                "INSERT INTO wallets (address, chain_id) VALUES (%s,'solana') "
                "ON CONFLICT DO NOTHING", (swap.trader,))
            # pool may be one the collector never shortlisted (registry miss);
            # register it lazily so swaps.pool_address has a target.
            base, quote = swap.base_mint, swap.quote_mint
            self.conn.execute(
                "INSERT INTO pools (pool_address, chain_id, dex, base_mint, "
                "                  quote_mint, quote_is_native, created_at, last_seen_at) "
                "VALUES (%s,'solana',%s,%s,%s,%s, NULL, now()) "
                "ON CONFLICT (pool_address) DO NOTHING",
                (ix.pool_address, ix.dex, base, quote,
                 quote == NATIVE_SENTINEL))

            ts = _dt.datetime.fromtimestamp(swap.block_time, _dt.timezone.utc) \
                if swap.block_time else None
            self.conn.execute(
                "INSERT INTO transactions (signature, chain_id, slot, block_time, "
                "  err, fee_lamports, priority_fee_lamports, compute_units, signer, "
                "  fee_payer, commitment, decoded_at) "
                "VALUES (%s,'solana',%s,%s,false,%s,%s,%s,%s,%s,'confirmed',now()) "
                "ON CONFLICT (signature) DO NOTHING",
                (swap.signature, swap.slot, ts, swap.fees.fee_lamports,
                 swap.fees.priority_fee_lamports, swap.fees.compute_units,
                 swap.trader, swap.trader))

            price = exec_price_quote(swap)
            self.conn.execute(
                "INSERT INTO swaps (signature, instruction_index, inner_index, "
                "  slot, event_time, pool_address, chain_id, trader, base_mint, "
                "  quote_mint, side, base_amount_raw, quote_amount_raw, "
                "  base_decimals, quote_decimals, exec_price_quote, "
                "  price_provenance, is_failed, decoder_version) "
                "VALUES (%s,%s,%s,%s,%s,%s,'solana',%s,%s,%s,%s,%s,%s,%s,%s,%s, "
                "  'swap_derived', false, %s) "
                "ON CONFLICT (signature, instruction_index, inner_index) DO NOTHING",
                (swap.signature, ix.ix_index, ix.inner_index, swap.slot, ts,
                 ix.pool_address, swap.trader, swap.base_mint, swap.quote_mint,
                 swap.side, str(swap.base_amount_raw), str(swap.quote_amount_raw),
                 swap.base_decimals, swap.quote_decimals, str(price),
                 "phase2/1"))
            self.conn.commit()
            return True
        except Exception as exc:
            self.conn.rollback()
            self._log(f"warning: persist failed for {swap.signature[:12]}: {exc}")
            return False

    def persist_failed_tx(self, signature: str, slot: int, block_time,
                          fee: int, signer: str | None) -> None:
        """Record a failed transaction as failed (never as a swap)."""
        if self.conn is None:
            return
        try:
            ts = _dt.datetime.fromtimestamp(block_time, _dt.timezone.utc) \
                if block_time else None
            self.conn.execute(
                "INSERT INTO transactions (signature, chain_id, slot, block_time, "
                "  err, fee_lamports, signer, fee_payer, commitment, decoded_at) "
                "VALUES (%s,'solana',%s,%s,true,%s,%s,%s,'confirmed',now()) "
                "ON CONFLICT (signature) DO NOTHING",
                (signature, slot, ts, fee, signer, signer))
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            self._log(f"warning: failed-tx record failed: {exc}")

    # -- one pool's worth -------------------------------------------------

    def process_pool(self, pool: str, *, limit: int = 30,
                     known: dict[str, tuple[str, str]] | None = None,
                     skip_fetch: bool = False) -> tuple[int, list[DerivedSwap], list[str]]:
        """(decoded count, swaps, skip reasons) for one pool's recent history."""
        known = known if known is not None else self.known_pools()
        sigs = [] if skip_fetch else self.fetch_signatures(pool, limit=limit)
        swaps: list[DerivedSwap] = []
        skips: list[str] = []
        for sig in sigs:
            if self.already_decoded(sig):
                continue
            try:
                payload = self.fetch_transaction(sig)
            except Exception as exc:
                skips.append(f"{sig[:12]}: fetch failed: {exc}")
                continue
            if payload is None:
                continue
            self._sleep(0.15)   # stay well under provider rate limits
            meta = payload.get("meta") or {}
            if meta.get("err") is not None:
                tx0 = payload.get("transaction") or {}
                sigs0 = tx0.get("signatures") or [sig]
                keys = account_keys(payload)
                self.persist_failed_tx(sigs0[0], int(payload.get("slot") or 0),
                                       payload.get("blockTime"),
                                       int(meta.get("fee") or 0),
                                       keys[0] if keys else None)
                continue
            found, reasons = derive_swaps(payload, signature=sig, known_pools=known)
            skips.extend(reasons)
            for s in found:
                # registry miss: ensure the mints are known before persist
                known.setdefault(s.instruction.pool_address,
                                 (s.base_mint, s.quote_mint))
                self.persist(s)
                swaps.append(s)
        return len(swaps), swaps, skips

    # -- DexScreener snapshot ---------------------------------------------

    @staticmethod
    def fetch_snapshot(mints: list[str], *, opener=None) -> dict:
        """Batch DexScreener snapshot, keyed by PAIR address.

        Keyed by pair, not by mint, because a token routinely trades in more
        than one pool (a bonding curve plus a migrated AMM, or several AMMs).
        Keying by mint and keeping the deepest pair silently compares *another
        pool's* reserves: measured 2026-10-25 that produced a 27% price error on
        one side and −14% on the other with liquidity still within 2%, which is
        the signature of two different pools rather than a derivation error.

        Keyless but request-metered; one request covers up to 30 mints.
        """
        if not mints:
            return {}
        url = DS_TOKENS_URL.format(mints=",".join(mints[:30]))
        body, _status = fetch_bytes(url, timeout=15, retries=3, opener=opener)
        payload = json.loads(body)
        rows = payload if isinstance(payload, list) else payload.get("pairs") or []
        out: dict[str, dict] = {}
        for row in rows:
            base = ((row.get("baseToken") or {}).get("address"))
            if not base:
                continue
            liq = (row.get("liquidity") or {}).get("usd")
            quote_addr = (row.get("quoteToken") or {}).get("address")
            try:
                price_native = float(row.get("priceNative")) if row.get("priceNative") is not None else None
            except (TypeError, ValueError):
                price_native = None
            # liquidity.base/quote are the provider's own reserve figures (token
            # units), and they are the reference the gate reconciles against:
            # same quantities, same instant, as opposed to priceNative which is
            # a trade-derived price.
            base_reserve = _num((row.get("liquidity") or {}).get("base"))
            quote_reserve = _num((row.get("liquidity") or {}).get("quote"))
            rec = {
                "pair": row.get("pairAddress"),
                "base_mint": base,
                "dex": row.get("dexId"),
                "price_native": price_native,
                "price_usd": float(row["priceUsd"]) if row.get("priceUsd") else None,
                "liquidity_usd": float(liq) if liq is not None else None,
                "base_reserve": base_reserve,
                "quote_reserve": quote_reserve,
                "quote_mint": quote_addr,
                "fdv": row.get("fdv"),
                "market_cap": row.get("marketCap"),
            }
            pair = row.get("pairAddress")
            if not pair:
                continue
            out[pair] = rec
        return out

    @staticmethod
    def pick_for_pool(snapshot: dict, pool: str, base_mint: str) -> tuple[dict, bool]:
        """Snapshot row for THIS pool; (row, exact). Falls back to the deepest
        pair for the mint only when the pool itself is absent, and says so —
        a non-exact match is not the same measurement."""
        exact = snapshot.get(pool)
        if exact:
            return exact, True
        cands = [r for r in snapshot.values() if r.get("base_mint") == base_mint]
        if not cands:
            return {}, False
        return max(cands, key=lambda r: r.get("liquidity_usd") or 0), False

    # -- same-transaction pool spot -----------------------------------------

    @staticmethod
    def pool_spot_from_tx(payload: dict, pool: str, base_mint: str,
                          quote_mint: str) -> tuple[float | None, float | None]:
        """DIAGNOSTIC ONLY — (price, observed-liquidity) from one transaction.

        Do not price anything with this. A transaction's ``meta`` only lists
        accounts it touched, so a pool vault the trade never touched is
        invisible: measured 2026-10-25, two fully-identified vaults still
        implied a price 37% below the executed price because a third vault was
        absent from the payload. Kept because it is a useful lower bound and
        the fixture test asserts it fails closed on an unowned pool. Reserves
        for pricing come from :meth:`vault_reserves`, which enumerates by owner.
        """
        meta = payload.get("meta") or {}
        pre = {int(t["accountIndex"]): int(t["uiTokenAmount"]["amount"])
               for t in (meta.get("preTokenBalances") or [])}
        post = {int(t["accountIndex"]): int(t["uiTokenAmount"]["amount"])
                for t in (meta.get("postTokenBalances") or [])}
        info = {}
        for t in (meta.get("postTokenBalances") or []):
            if t.get("owner") == pool:
                mint = t["mint"]
                dec = int(t["uiTokenAmount"].get("decimals") or 0)
                info.setdefault(mint, {"dec": dec, "post": 0, "pre": 0})
                info[mint]["post"] = post.get(int(t["accountIndex"]), 0)
                info[mint]["pre"] = pre.get(int(t["accountIndex"]),
                                            info[mint]["post"])
        # also pre-only vaults (pool drained them to zero in a full sell)
        for t in (meta.get("preTokenBalances") or []):
            if t.get("owner") == pool and t["mint"] not in info:
                dec = int(t["uiTokenAmount"].get("decimals") or 0)
                info[t["mint"]] = {"dec": dec,
                                   "post": post.get(int(t["accountIndex"]), 0),
                                   "pre": int(t["uiTokenAmount"]["amount"])}
        if base_mint not in info or quote_mint not in info:
            return None, None
        b = info[base_mint]; q = info[quote_mint]
        base_ui = b["post"] / 10 ** b["dec"]
        quote_ui = q["post"] / 10 ** q["dec"]
        if base_ui <= 0:
            return None, None
        return quote_ui / base_ui, base_ui + quote_ui  # liquidity in quote units

    # -- pool reserves (authoritative) --------------------------------------

    def vault_reserves(self, pool: str) -> dict[str, tuple[int, int]]:
        """mint -> (raw amount, decimals), summed over every account the pool owns.

        This is the only sound way to read pool reserves from a free RPC. A
        transaction's ``meta`` lists only the accounts that transaction touched,
        so an untouched vault is invisible: measured 2026-10-25, a PumpSwap pool
        whose two *visible* vaults implied a price 37% below the executed price
        because a third pool-owned vault was never touched by the sample trade.
        Enumerating by owner closes that hole, and both token programs are
        queried so a Token-2022 pool is not silently halved.
        """
        out: dict[str, tuple[int, int]] = {}
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            try:
                result, _body = self.rpc.token_accounts_by_owner(
                    pool, program_id=program,
                    request_key=f"reserves:{pool}:{program[:8]}"
                                f":{int(self._now().timestamp() // 300)}")
            except Exception as exc:
                self._log(f"warning: reserve read failed for {pool[:8]}: {exc}")
                continue
            for acct in ((result.get("result") or {}).get("value") or []):
                try:
                    info = acct["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    amount = info["tokenAmount"]
                    raw = int(amount["amount"])
                    dec = int(amount.get("decimals") or 0)
                except (KeyError, TypeError, ValueError):
                    continue   # one malformed account must not void the read
                prev = out.get(mint)
                out[mint] = (raw + (prev[0] if prev else 0),
                             prev[1] if prev else dec)
        return out

    @staticmethod
    def price_from_reserves(reserves: dict[str, tuple[int, int]],
                            base_mint: str,
                            quote_mint: str) -> float | None:
        """quote per whole base unit from reserves; None when either is absent."""
        b = reserves.get(base_mint)
        q = reserves.get(quote_mint)
        if not b or not q:
            return None
        base_ui = b[0] / 10 ** b[1]
        quote_ui = q[0] / 10 ** q[1]
        if base_ui <= 0:
            return None
        return quote_ui / base_ui

    # Vault ownership is verified for PumpSwap: its vaults are owned by the
    # pool address itself, so getTokenAccountsByOwner(pool) returns the complete
    # reserve set. Raydium AMM/CPMM vaults are owned by a separate authority
    # PDA, and pump.fun bonding-curve vaults by the curve PDA, so the same call
    # returns nothing for them. They are EXCLUDED from the gate rather than
    # counted as derivation failures — a wrong owner is not a wrong price.
    RESERVE_VERIFIED_DEX = {"pumpswap"}

    def discover_live_pools(self, limit: int, *, min_liquidity: float = 3000.0):
        """Keyless candidate pools for a gate run with no registry available.

        Two feeds are unioned because neither alone yields 20 PumpSwap pools:
        GeckoTerminal ``new_pools`` (10 calls/min — three pages, then stop) and
        DexScreener's batch token endpoint (30 mints per request, 300 req/min).
        Only PumpSwap and wSOL/USDC quotes pass (RESERVE_VERIFIED_DEX).
        """
        from .adapters import PoolCandidate
        from .adapters.dexscreener import fetch_profiles
        from .adapters.geckoterminal import fetch_dex_pools, fetch_new_pools

        out: list[PoolCandidate] = []
        seen: set[str] = set()

        def accept(pool, base, quote, dex, liq, created=None, txns=None):
            if not pool or pool in seen:
                return
            if quote not in (WSOL_MINT, USDC_MINT, NATIVE_SENTINEL):
                return
            if dex not in self.RESERVE_VERIFIED_DEX:
                return
            if (liq or 0) < min_liquidity:
                return
            seen.add(pool)
            out.append(PoolCandidate(pool, base, quote, dex, created, liq,
                                     txns, "geckoterminal"))

        # Highest yield first: one DEX's pools ordered by reserve. 20 per page,
        # so three pages is 60 candidates before any other feed is touched.
        for dex in sorted(self.RESERVE_VERIFIED_DEX):
            try:
                for cand, _body, _url in fetch_dex_pools(
                        dex, pages=max(2, limit // 20 + 1)):
                    accept(cand.pool_address, cand.base_mint, cand.quote_mint,
                           cand.dex, cand.liquidity_usd, cand.created_at,
                           cand.txns_h1)
                    if len(out) >= limit:
                        return out
            except Exception as exc:
                self._log(f"gate: {dex} pool listing unavailable: {exc}")

        try:
            for cand, _body, _url in fetch_new_pools(pages=2):
                accept(cand.pool_address, cand.base_mint, cand.quote_mint,
                       cand.dex, cand.liquidity_usd, cand.created_at,
                       cand.txns_h1)
                if len(out) >= limit:
                    return out
        except Exception as exc:
            self._log(f"gate: geckoterminal discovery stopped: {exc}")

        try:
            mints, _body = fetch_profiles()
            for start in range(0, len(mints), 30):
                batch = mints[start:start + 30]
                for pair, rec in self.fetch_snapshot(batch).items():
                    accept(pair, rec.get("base_mint"), rec.get("quote_mint"),
                           rec.get("dex"), rec.get("liquidity_usd"))
                if len(out) >= limit:
                    break
        except Exception as exc:
            self._log(f"gate: dexscreener discovery stopped: {exc}")
        return out

    # -- the gate -----------------------------------------------------------

    def run_gate(self, *, pools: list[str] | None = None, min_pairs: int = 20,
                 sigs_per_pool: int = 0, tolerance_pct: float = 5.0,
                 known: dict[str, tuple[str, str]] | None = None,
                 opener=None) -> GateReport:
        """Reconcile chain-derived reserves against a contemporaneous snapshot.

        Doc 07's Phase 2 exit criterion. Reserves are read from chain and the
        snapshot is taken *afterwards in the same run*, so both describe nearly
        the same instant — the difference is now provider processing lag, not
        the minutes of drift that a historical trade comparison would carry.

        ``sigs_per_pool > 0`` additionally decodes recent swaps per pool, which
        costs credits and is reported as evidence rather than used for pass/fail
        (an executed price from a 30-second-old trade is not a spot price).
        """
        known = known if known is not None else self.known_pools()
        if pools is None:
            pools = self.active_pools(max(min_pairs * 3, min_pairs))
        if not pools:
            discovered = self.discover_live_pools(max(min_pairs * 3, min_pairs))
            for cand in discovered:
                known.setdefault(cand.pool_address,
                                 (cand.base_mint, cand.quote_mint))
            pools = [c.pool_address for c in discovered]
            self._log(f"gate: discovered {len(pools)} live pools keyless "
                      f"(no registry available)")

        report = GateReport(tolerance_pct=tolerance_pct, required_pairs=min_pairs)
        before: dict[str, dict[str, tuple[int, int]]] = {}
        decoded: dict[str, int] = {}
        skips: list[str] = []

        for pool in pools:
            mints = known.get(pool)
            if not mints:
                skips.append(f"{pool[:10]}: pool not in registry — mints unknown")
                continue
            before[pool] = self.vault_reserves(pool)
            if sigs_per_pool > 0:
                n, _swaps, reasons = self.process_pool(pool, limit=sigs_per_pool,
                                                       known=known)
                decoded[pool] = n
                skips.extend(reasons)

        mints = [known[p][0] for p in before]
        snapshot = self.fetch_snapshot(mints, opener=opener)
        after = {pool: self.vault_reserves(pool) for pool in before}
        self._log(f"gate: {len(before)} pools reserved twice around the snapshot, "
                  f"{len(snapshot)} snapshot rows for {len(mints)} mints")

        for pool, reserves in before.items():
            base_mint, quote_mint = known[pool]
            row = self._gate_row(
                pool, base_mint, quote_mint, snapshot=snapshot,
                reserves=reserves, swaps_decoded=decoded.get(pool, 0))
            p_before = self.price_from_reserves(reserves, base_mint, quote_mint)
            p_after = self.price_from_reserves(after.get(pool) or {},
                                               base_mint, quote_mint)
            if p_before and p_after:
                drift = abs(p_after - p_before) / p_before * 100.0
                row.reserve_drift_pct = drift
                if drift > tolerance_pct:
                    # The pool moved further than the gate can resolve while we
                    # were measuring it, so no like-for-like comparison exists.
                    row.pool_moved = True
                    row.note = (row.note + "; " if row.note else "") + \
                        f"pool moved {drift:.1f}% during the window — excluded"
            report.rows.append(row)
        report.fetched_at = self._now().isoformat()
        for reason in skips[:20]:
            self._log(f"skip: {reason}")
        return report

    def active_pools(self, limit: int) -> list[str]:
        """Pools most recently seen — the live end of the registry."""
        if self.conn is None:
            return []
        rows = self.conn.execute(
            "SELECT pool_address FROM pools WHERE is_active "
            "ORDER BY last_seen_at DESC NULLS LAST LIMIT %s", (limit,)).fetchall()
        return [r[0] for r in rows]

    def _gate_row(self, pool: str, base_mint: str, quote_mint: str, *,
                  snapshot: dict, reserves: dict[str, tuple[int, int]],
                  swaps_decoded: int = 0) -> GateRow:
        """One reconciliation line: chain reserves vs the snapshot's reserves.

        The compared quantity is the reserve ratio, because that is what both
        sides actually measure. DexScreener's ``priceNative`` is a trade-derived
        price, not a mid: measured 2026-10-25 it sat 15–24% above the ratio
        implied by DexScreener's *own* reported reserves on tokens in a dump.
        Comparing against it would fail the gate for a reason that has nothing
        to do with our derivation, so ``priceNative`` is carried for context and
        never used as the reference.
        """
        note = ""
        derived_price = self.price_from_reserves(reserves, base_mint, quote_mint)
        snap, exact = self.pick_for_pool(snapshot, pool, base_mint)
        if snap and not exact:
            # Comparing our pool's reserves against a different pool's would
            # manufacture a deviation that has nothing to do with derivation.
            return GateRow(pool, base_mint, quote_mint, swaps_decoded,
                           derived_price, None, None, None, None, None,
                           None, None, None,
                           note="pool absent from snapshot; a different pool "
                                "for this mint was not substituted")
        snap_quote = snap.get("quote_mint")
        quote_ok = (snap_quote in (quote_mint, NATIVE_SENTINEL, WSOL_MINT)
                    and quote_mint in (NATIVE_SENTINEL, WSOL_MINT, snap_quote))
        if snap and not quote_ok:
            return GateRow(pool, base_mint, quote_mint, swaps_decoded,
                           derived_price, None, None, None, None, None,
                           None, None, None,
                           note=f"quote mismatch: derived {quote_mint[:8]} "
                                f"vs snapshot {str(snap_quote)[:8]}")

        snap_base = snap.get("base_reserve")
        snap_quote_res = snap.get("quote_reserve")
        snap_spot = (snap_quote_res / snap_base) if (snap_base and snap_quote_res) else None
        snap_liq = snap.get("liquidity_usd")

        dev = None
        if derived_price and snap_spot:
            dev = (derived_price - snap_spot) / snap_spot * 100.0

        base_dev = quote_dev = None
        d_base = reserves.get(base_mint)
        d_quote = reserves.get(quote_mint)
        if d_base and snap_base:
            base_ui = d_base[0] / 10 ** d_base[1]
            base_dev = (base_ui - snap_base) / snap_base * 100.0
        if d_quote and snap_quote_res:
            quote_ui = d_quote[0] / 10 ** d_quote[1]
            quote_dev = (quote_ui - snap_quote_res) / snap_quote_res * 100.0

        # Liquidity is valued at the SNAPSHOT's prices, deliberately. Using
        # our own price would fold a price disagreement into the liquidity
        # number and make the two checks measure the same error twice.
        derived_liq_usd = None
        dex_sol_usd = None
        if snap.get("price_usd") and snap.get("price_native"):
            try:
                dex_sol_usd = snap["price_usd"] / snap["price_native"]
            except ZeroDivisionError:
                dex_sol_usd = None
        if snap_liq and _reserve_basis_available(d_base, d_quote):
            base_ui = d_base[0] / 10 ** d_base[1]
            quote_ui = d_quote[0] / 10 ** d_quote[1]
            base_usd = base_ui * (snap.get("price_usd") or 0.0)
            quote_usd = (quote_ui if quote_mint == USDC_MINT
                         else quote_ui * (dex_sol_usd or 0.0))
            if base_usd or quote_usd:
                derived_liq_usd = base_usd + quote_usd
        liq_dev = None
        if snap_liq and derived_liq_usd:
            liq_dev = (derived_liq_usd - snap_liq) / snap_liq * 100.0
        if liq_dev is None and (d_base is None or d_quote is None):
            note = note or "one side's vault missing from the pool-owned set"

        return GateRow(
            pool_address=pool, base_mint=base_mint, quote_mint=quote_mint,
            swaps_decoded=swaps_decoded, derived_price_quote=derived_price,
            snapshot_price_quote=snap_spot, deviation_pct=dev,
            derived_pool_price_quote=None,
            derived_liquidity_usd=derived_liq_usd,
            snapshot_liquidity_usd=snap_liq, liquidity_dev_pct=liq_dev,
            slippage_bps=None, slippage_plausible=None, note=note,
            base_reserve_dev_pct=base_dev, quote_reserve_dev_pct=quote_dev,
            dex_price_native=snap.get("price_native"),
        )


def _reserve_basis_available(d_base, d_quote) -> bool:
    """Both sides of the pool are present in the pool-owned reserve set."""
    return bool(d_base and d_quote)

    @staticmethod
    def _quote_to_usd(quote_amount: float, quote_mint: str, snap: dict) -> float | None:
        """Quote units -> USD. wSOL leg uses the snapshot's own SOL/USDC
        price (same fetch, same second); USDC is 1:1 by definition."""
        if quote_mint == USDC_MINT:
            return quote_amount
        if quote_mint in (NATIVE_SENTINEL, WSOL_MINT):
            # any USDC-quoted pair in the same snapshot carries SOL/USD
            # indirectly: priceUsd / priceNative for SOL-quoted pairs gives
            # SOL/USD exactly. Take the median across rows for robustness.
            ratios = []
            for rec in (snap or {}).values():
                if rec.get("price_usd") and rec.get("price_native"):
                    # price_usd = price_native * quote_usd for SOL-quoted pairs
                    ratios.append(rec["price_usd"] / rec["price_native"])
            if not ratios:
                return None
            ratios.sort()
            sol_usd = ratios[len(ratios) // 2]
            return quote_amount * sol_usd
        return None


def format_gate(report: GateReport) -> str:
    """Human-readable gate table + verdict (the artifact doc 07 asks for)."""
    hdr = (f"{'pool':>10} {'n':>3} {'chain px':>12} {'snap px':>12} "
           f"{'dev%':>7} {'base%':>7} {'quote%':>7} {'liq%':>7}  note")
    lines = [hdr, "-" * len(hdr)]
    for r in report.rows:
        def f(v, spec="+.2f"):
            return format(v, spec) if v is not None else "-"
        dp = f"{r.derived_price_quote:.3e}" if r.derived_price_quote else "-"
        sp = f"{r.snapshot_price_quote:.3e}" if r.snapshot_price_quote else "-"
        mark = " ~moved" if r.pool_moved else (" ~lag" if r.skew_signature else "")
        lines.append(
            f"{r.pool_address[-10:]:>10} {r.swaps_decoded:>3} {dp:>12} {sp:>12} "
            f"{f(r.deviation_pct):>7} {f(r.base_reserve_dev_pct):>7} "
            f"{f(r.quote_reserve_dev_pct):>7} {f(r.liquidity_dev_pct):>7}  {r.note}{mark}")
    n = len(report.priced_rows)
    lines.append("")
    lines.append(f"priced pools: {n} (required {report.required_pairs})"
                 f"   excluded: {len(report.moved_rows)}")
    if report.mean_deviation is not None:
        lines.append(f"price |dev| mean {report.mean_deviation:.2f}%  "
                     f"max {report.max_deviation:.2f}%  "
                     f"tolerance {report.tolerance_pct:.1f}%")
    lr = report.liquidity_rows
    if lr:
        lines.append(f"liquidity: {report.liquidity_passed}/{len(lr)} within "
                     f"tolerance, mean |dev| {report.mean_liquidity_deviation:.2f}%")
    ok, verdict = report.verdict(min_pairs=report.required_pairs)
    lines.append(f"GATE {'PASS' if ok else 'FAIL'}: {verdict}")
    return "\n".join(lines)
