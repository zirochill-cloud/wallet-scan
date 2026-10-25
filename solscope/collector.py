"""Phase 1 forward collector (doc 07 Phase 1, doc 02 §4).

Pipeline per tick:

1. **Discover** — GeckoTerminal ``new_pools`` (primary, carries pools) and
   DexScreener token-profiles → pair lookup (secondary). Every response is
   archived verbatim before parsing.
2. **Filter** — liquidity, age, recent trades. Everything failing here is
   never subscribed and never fetched again: the filter *is* the credit
   funnel.
3. **Register** — surviving pools upserted into ``pools``/``tokens``; only
   pools that were new to the DB get subscribed (restarts re-subscribe but do
   not re-process).
4. **Stream** — one ``logsSubscribe`` per pool on LaserStream WSS. Every
   frame archived verbatim under ``data/raw/wss/``; bytes metered into
   Helius credits as they stream (2 credits / 0.1 MB, 1 per connection).
5. **Checkpoint** — high-water marks advance only after the corresponding
   data is on disk; recovery replays ``overlap_slots`` and lets archive
   dedupe collapse the overlap.

Everything network-shaped is injectable so the whole pipeline is unit
testable; the live run uses the same code paths.
"""
from __future__ import annotations

import datetime as _dt
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import PoolCandidate
from .archive import RawArchive
from .checkpoint import CheckpointStore
from .config import BudgetConfig, CollectorConfig
from .ratelimit import TokenBucket

JOB = "collect"


@dataclass
class RunSummary:
    pools_seen: int = 0
    pools_new: int = 0
    frames_archived: int = 0
    bytes_archived: int = 0
    payloads_archived: int = 0
    helius_credits: int = 0
    dexscreener_requests: int = 0
    geckoterminal_requests: int = 0
    subscriptions: int = 0
    errors: list[str] = field(default_factory=list)


def passes_filter(cand: PoolCandidate, *, min_liquidity_usd: float,
                  min_age_hours: float, min_txns_h1: int | None,
                  now: _dt.datetime) -> tuple[bool, str]:
    """The shortlist gate. Returns (pass, reason); reason is for logs/tests.

    Fails closed on unknowns: a pool with no known liquidity or no known
    activity window is indistinguishable from a rug, so it does not pass.
    """
    if cand.liquidity_usd is None:
        return False, "unknown_liquidity"
    if cand.liquidity_usd < min_liquidity_usd:
        return False, "low_liquidity"
    age = cand.age_hours(now)
    if age is None:
        return False, "unknown_age"
    if age < min_age_hours:
        return False, "too_new"
    if min_txns_h1 is not None:
        if cand.txns_h1 is None:
            return False, "unknown_activity"
        if cand.txns_h1 < min_txns_h1:
            return False, "low_activity"
    return True, "pass"


class Meter:
    """Settles streamed WSS bytes into the credit ledger incrementally.

    A crash can lose at most the unflushed remainder (bounded by
    ``flush_every_bytes``), which is why flushing is frequent rather than
    deferred to a clean shutdown — unmetered stream time is exactly the
    runaway-loop exposure the ledger exists to prevent.
    """

    def __init__(self, *, ledger, metering: dict, job: str = JOB, log=print) -> None:
        self._ledger = ledger
        self._per_unit = int(metering["wss_bytes_per_unit"])
        self._credits_per_unit = int(metering["wss_credits_per_unit"])
        self._conn_credits = int(metering["wss_connection_credits"])
        self._flush_every = int(metering.get("flush_every_bytes", self._per_unit * 10))
        self._job = job
        self._log = log
        # Per-process token: ledger idempotency is scoped to retries *within* a
        # run. Without it, run N+1's flush #k collides with run N's flush #k
        # and the later charge is silently swallowed as a "retry" — the stream
        # is spent but the ledger stops recording it.
        self.run_token = uuid.uuid4().hex[:12]
        self.unflushed_bytes = 0
        self.credits_charged = 0
        self._flush_seq = 0
        self._conn_seq = 0

    def add_bytes(self, n: int) -> None:
        self.unflushed_bytes += n
        if self.unflushed_bytes >= self._flush_every:
            self.flush()

    def add_connection(self) -> None:
        if self._ledger is None:
            self.credits_charged += self._conn_credits
            return
        self._conn_seq += 1
        row = self._ledger.charge(
            provider="helius", job=self._job, operation="websocket_connection",
            units=1,
            request_key=f"wss-conn:{self._job}:{self.run_token}:{self._conn_seq}",
        )
        self.credits_charged += row.credits

    def flush(self) -> int:
        """Charge accumulated bytes; returns credits charged this flush."""
        units = math.ceil(self.unflushed_bytes / self._per_unit) if self.unflushed_bytes else 0
        if units == 0:
            return 0
        credits = units * self._credits_per_unit
        self._flush_seq += 1
        if self._ledger is None:
            self.credits_charged += credits
        else:
            row = self._ledger.charge(
                provider="helius", job=self._job, operation="websocket_data",
                units=units,
                request_key=f"wss-data:{self._job}:{self.run_token}:{self._flush_seq}",
                notes=f"{self.unflushed_bytes} bytes streamed",
            )
            self.credits_charged += row.credits
        self.unflushed_bytes = 0
        return credits


def _default_ws_factory(url: str):
    import websocket  # lazy: discovery-only runs need no ws library
    return websocket.create_connection(url, timeout=10)


class Collector:
    def __init__(self, *, cfg: CollectorConfig, budget: BudgetConfig,
                 conn=None, ledger=None, archive: RawArchive | None = None,
                 api_key: str | None = None, ws_factory=None,
                 gt_fetcher=None, ds_profiles_fetcher=None, ds_pairs_fetcher=None,
                 rpc_factory=None,
                 clock=None, sleep=time.sleep, log=print) -> None:
        self.cfg = cfg
        self.budget = budget
        self.conn = conn
        self.ledger = ledger
        self.archive = archive or RawArchive(cfg.data_dir, conn)
        self.checkpoints = CheckpointStore(conn, cfg.checkpoint["overlap_slots"]) if conn else None
        self.api_key = api_key
        self._ws_factory = ws_factory or _default_ws_factory
        self._gt_fetcher = gt_fetcher
        self._ds_profiles = ds_profiles_fetcher
        self._ds_pairs = ds_pairs_fetcher
        self._rpc_factory = rpc_factory      # HeliusRpc stand-in for tests
        self._now = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._sleep = sleep
        self._log = log
        self.meter = Meter(ledger=ledger, metering=cfg.metering, job=cfg.job, log=log)

        # Conservative buckets: GT keyless ~10/min, DS profiles 60/min,
        # DS pair lookup 300/min (doc 01 §2.1, doc 08 trap 6).
        self._gt_bucket = TokenBucket(10, 10)
        self._ds_profile_bucket = TokenBucket(60, 60)
        self._ds_pair_bucket = TokenBucket(300, 300)

        self._streams: list = []            # LaserStream instances
        self._subscribed: set[str] = set()  # pool addresses on a stream
        self._to_subscribe: list[str] = []  # pending: resume + re-drop pools
        self._next_heartbeat = 0.0
        self._known_pools: set[str] = set()  # pools already in the DB
        self._known_mints: set[str] = set()  # mints already in the DB
        self._bytes_archived = 0

    # -- discovery ------------------------------------------------------

    def poll_geckoterminal(self, summary: RunSummary) -> list[PoolCandidate]:
        if self._gt_fetcher is None:
            return []
        pages = int(self.cfg.discovery.get("geckoterminal_pages", 1))
        self._gt_bucket.acquire(pages)
        rows = self._gt_fetcher(pages=pages)
        archived_pages: set[str] = set()
        cands: list[PoolCandidate] = []
        for cand, body, url in rows:
            if url not in archived_pages:
                self._archive(source="geckoterminal", endpoint=url, body=body,
                              tag="gt_new_pools")
                archived_pages.add(url)
            cands.append(cand)
        self._charge_requests("geckoterminal", "new_pools", pages)
        summary.geckoterminal_requests += pages
        return cands

    def poll_dexscreener(self, summary: RunSummary) -> list[PoolCandidate]:
        if self._ds_profiles is None:
            return []
        self._ds_profile_bucket.acquire()
        mints, body = self._ds_profiles()
        self._archive(source="dexscreener", endpoint="token-profiles/latest/v1",
                      body=body, tag="ds_profiles")
        self._charge_requests("dexscreener", "token_profiles", 1)
        summary.dexscreener_requests += 1

        cands: list[PoolCandidate] = []
        max_lookups = int(self.cfg.discovery.get("dexscreener_max_lookups", 20))
        lookups = 0
        for mint in mints:
            if mint in self._known_mints:
                continue
            if lookups >= max_lookups:
                break
            if self._ds_pairs is None:
                break
            self._ds_pair_bucket.acquire()
            pairs, pbody = self._ds_pairs(mint)
            self._archive(source="dexscreener", endpoint=f"tokens/{mint}",
                          body=pbody, tag=f"ds_pairs_{mint[:8]}")
            self._charge_requests("dexscreener", "token_pairs", 1)
            summary.dexscreener_requests += 1
            lookups += 1
            cands.extend(pairs)
        return cands

    def _refresh_known(self) -> None:
        """Load already-registered pools/mints so restarts don't reprocess."""
        self._known_pools = set()
        self._known_mints = set()
        if self.conn is None:
            return
        try:
            rows = self.conn.execute("SELECT pool_address FROM pools").fetchall()
            self._known_pools = {r[0] for r in rows}
            rows = self.conn.execute("SELECT mint FROM tokens").fetchall()
            self._known_mints = {r[0] for r in rows}
        except Exception as exc:
            self._log(f"warning: could not load known pools: {exc}")

    # -- registration ---------------------------------------------------

    def register(self, cands: list[PoolCandidate], summary: RunSummary) -> list[PoolCandidate]:
        """Upsert chains/tokens/pools; return candidates that were NEW."""
        new: list[PoolCandidate] = []
        for cand in cands:
            if cand.pool_address in self._known_pools:
                continue
            if self.conn is not None:
                self._insert_pool(cand)
            self._known_pools.add(cand.pool_address)
            self._known_mints.add(cand.base_mint)
            new.append(cand)
        summary.pools_new += len(new)
        return new

    def _insert_pool(self, cand: PoolCandidate) -> None:
        try:
            self.conn.execute("INSERT INTO chains (id, display_name) VALUES ('solana', 'Solana') ON CONFLICT DO NOTHING")
            self.conn.execute(
                "INSERT INTO tokens (mint, chain_id) VALUES (%s, 'solana') ON CONFLICT DO NOTHING",
                (cand.base_mint,))
            if cand.quote_mint:
                self.conn.execute(
                    "INSERT INTO tokens (mint, chain_id) VALUES (%s, 'solana') ON CONFLICT DO NOTHING",
                    (cand.quote_mint,))
            cur = self.conn.execute(
                "INSERT INTO pools (pool_address, chain_id, dex, base_mint, quote_mint, "
                "                  quote_is_native, created_at, last_seen_at) "
                "VALUES (%s, 'solana', %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (pool_address) DO NOTHING RETURNING pool_address",
                (cand.pool_address, cand.dex, cand.base_mint, cand.quote_mint or None,
                 bool(cand.quote_mint) and cand.quote_mint in ("So11111111111111111111111111111111111111112",),
                 cand.created_at))
            is_new = cur.fetchone() is not None
            self.conn.commit()
            if not is_new:
                self._known_pools.add(cand.pool_address)
        except Exception as exc:
            self.conn.rollback()
            self._log(f"warning: pool insert failed for {cand.pool_address}: {exc}")

    # -- helpers --------------------------------------------------------

    def _archive(self, *, source: str, endpoint: str, body: bytes, tag: str,
                 subdir: str = "discovery", credits_spent: int = 0) -> None:
        res = self.archive.write(source=source, endpoint=endpoint, body=body,
                                 tag=tag, subdir=subdir, credits_spent=credits_spent)
        self._bytes_archived += res.byte_size

    @property
    def archive_bytes(self) -> int:
        """Discovery/HTTP payloads archived (WSS bytes go through Meter)."""
        return self._bytes_archived

    def _charge_requests(self, provider: str, operation: str, units: int) -> None:
        if self.ledger is None:
            return
        self.ledger.charge(provider=provider, job=self.cfg.job,
                           operation=operation, units=units)

    # -- WSS streaming --------------------------------------------------

    def _wss_enabled(self, discovery_only: bool) -> bool:
        return bool(self.api_key) and not discovery_only

    def _ensure_streams(self, pools: list[str], summary: RunSummary) -> None:
        """Subscribe the given pools, opening connections as capacity requires.

        ``pools`` are addresses: freshly discovered candidates *and* pools
        queued for resume (restart) or re-subscription (a dropped socket).
        """
        if not pools:
            return
        per_conn = int(self.cfg.subscription["subscriptions_per_connection"])
        max_conn = int(self.cfg.subscription["max_connections"])
        max_pools = int(self.cfg.subscription["max_pools"])

        for address in pools:
            if address in self._subscribed:
                continue
            if len(self._subscribed) >= max_pools:
                self._log(f"shortlist cap reached ({max_pools}); {len(pools)} pools deferred")
                break
            stream = next((s for s in self._streams if len(s.pools) < per_conn), None)
            if stream is None:
                if len(self._streams) >= max_conn:
                    self._log(f"connection cap reached ({max_conn}); deferring remaining pools")
                    break
                from .adapters.helius import DEFAULT_WSS_URL, LaserStream
                stream = LaserStream(
                    url=DEFAULT_WSS_URL.format(key=self.api_key),
                    ws_factory=self._ws_factory, log=self._log)
                stream.connect()
                self._streams.append(stream)
                self.meter.add_connection()
            stream.subscribe(address)
            self._subscribed.add(address)
            summary.subscriptions += 1
            self._record_subscription(address)

    def _record_subscription(self, pool: str) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute("INSERT INTO chains (id, display_name) VALUES ('solana', 'Solana') ON CONFLICT DO NOTHING")
            self.conn.execute(
                "INSERT INTO stream_subscriptions (provider, kind, target, chain_id) "
                "VALUES ('helius', 'websocket', %s, 'solana') "
                "ON CONFLICT (provider, kind, target) DO UPDATE SET is_active = true",
                (pool,))
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            self._log(f"warning: subscription row failed for {pool}: {exc}")

    def drain_frames(self, seconds: float, summary: RunSummary) -> None:
        """Pull and archive every frame from every stream for a short slice.

        Settling the per-pool high-water mark happens here, AFTER the archive
        write: disk first, checkpoint second, so a crash between the two
        replays an overlap instead of skipping it.
        """
        high: dict[str, tuple[int | None, _dt.datetime]] = {}
        for stream in self._streams:
            try:
                for frame in stream.frames(max_seconds=seconds):
                    target = frame.subscription_id or "unknown"
                    if frame.kind == "confirmation" and isinstance(frame.parsed, dict):
                        # Map the server's sub id -> pool BEFORE targeting, so
                        # archives, liveness bytes and checkpoints name a pool
                        # instead of an opaque id. Without this call every
                        # notification is filed under 'unknown'/sid and the
                        # wss:<pool> checkpoint the recovery reads never
                        # matches (live observation 2026-10-17).
                        mapped = stream.note_confirmation(frame.parsed.get("id"),
                                                         frame.parsed.get("result"))
                        if mapped:
                            target = mapped
                    elif frame.kind == "error" and isinstance(frame.parsed, dict):
                        summary.errors.append(
                            f"subscription error: {frame.parsed.get('error')}")
                        self._log(f"warning: subscription error: {frame.parsed.get('error')}")
                    body = frame.raw.encode("utf-8", "replace")
                    self.archive.write(source="helius_wss",
                                       endpoint=f"logsSubscribe:{target}",
                                       body=body, subdir="wss", tag=target)
                    self.meter.add_bytes(len(body))
                    summary.frames_archived += 1
                    summary.bytes_archived += len(body)
                    self._touch_subscription(target)
                    self._track_frame(high, target, frame)
            except Exception as exc:
                summary.errors.append(f"stream error: {exc}")
                self._log(f"warning: stream dropped: {exc}")
                self._note_disconnect(stream, str(exc), summary)
        self._settle_checkpoints(high)

    # -- checkpoints ----------------------------------------------------

    @staticmethod
    def _track_frame(high: dict, target: str, frame) -> None:
        """Remember the best (slot, time) seen per pool in this slice."""
        slot = None
        if isinstance(frame.parsed, dict):
            # pubsub notifications nest the context under params.result, e.g.
            # {"method":"logsNotification","params":{"result":{"context":…}}}.
            nest = frame.parsed.get("params")
            payload = nest.get("result") if isinstance(nest, dict) else None
            ctx = None
            if isinstance(nest, dict) and isinstance(nest.get("context"), dict):
                ctx = nest["context"]
            elif isinstance(payload, dict) and isinstance(payload.get("context"), dict):
                ctx = payload["context"]
            elif isinstance(frame.parsed.get("context"), dict):
                ctx = frame.parsed["context"]
            if ctx is not None and isinstance(ctx.get("slot"), int):
                slot = ctx["slot"]
        ts = frame.received_at
        prev = high.get(target)
        if prev is None:
            high[target] = (slot, ts)
            return
        best_slot = slot if (slot is not None and (prev[0] is None or slot > prev[0])) else prev[0]
        best_ts = max(prev[1], ts) if prev[1] is not None else ts
        high[target] = (best_slot, best_ts)

    def _settle_checkpoints(self, high: dict) -> None:
        """Advance ``wss:<pool>`` partitions — only after frames are archived."""
        if self.checkpoints is None or not high:
            return
        for target, (slot, ts) in high.items():
            try:
                self.checkpoints.advance(job=self.cfg.job,
                                         partition_key=f"wss:{target}",
                                         last_slot=slot, last_event_time=ts)
            except Exception as exc:
                if self.conn is not None:
                    self.conn.rollback()
                self._log(f"warning: checkpoint settle failed for {target}: {exc}")

    def heartbeat(self) -> None:
        """Advance the checkpoint of every live pool between frames.

        On ``logsSubscribe`` silence MEANS "no transactions for this pool", so a
        healthy stream with no frames proves coverage up to now. Without this a
        quiet pool's window stops at its last frame, and a restart would
        re-fetch hours of signatures instead of the actual downtime.
        """
        if self.checkpoints is None or not self._streams:
            return
        now_mono = time.monotonic()
        interval = float(self.cfg.checkpoint.get("heartbeat_seconds", 30))
        if now_mono < self._next_heartbeat:
            return
        self._next_heartbeat = now_mono + interval
        live = {p for s in self._streams for p in s.pools}
        now = self._now()
        for pool in sorted(live - {""}):
            try:
                self.checkpoints.advance(job=self.cfg.job,
                                         partition_key=f"wss:{pool}",
                                         last_event_time=now)
            except Exception as exc:
                if self.conn is not None:
                    self.conn.rollback()
                self._log(f"warning: checkpoint heartbeat failed for {pool}: {exc}")

    def _touch_subscription(self, pool: str) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(
                "UPDATE stream_subscriptions SET last_message_at = now() "
                "WHERE provider = 'helius' AND kind = 'websocket' AND target = %s",
                (pool,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()

    def _note_disconnect(self, stream, error: str, summary: RunSummary) -> None:
        """Record the disconnect, queue re-subscription, recover the outage.

        Three things a dropped socket must not lose:

        1. the row (operators need to see it happened),
        2. the subscription — the pools go back on ``_to_subscribe`` so the
           next tick re-subscribes them instead of silently watching nothing,
        3. the frames missed between the last checkpoint and the reconnect,
           which have no replay in pubsub and are re-fetched explicitly.
        """
        pools = list(stream.pools)
        stream.close()
        if stream in self._streams:
            self._streams.remove(stream)
        self._subscribed -= set(pools)
        self._to_subscribe.extend(p for p in pools if p not in self._to_subscribe)
        if self.conn is not None:
            for pool in pools:
                try:
                    self.conn.execute(
                        "UPDATE stream_subscriptions SET disconnect_count = disconnect_count + 1, "
                        "  last_error = %s, is_active = false "
                        "WHERE provider = 'helius' AND kind = 'websocket' AND target = %s",
                        (error[:500], pool))
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
        self.recover_gaps(summary, pools=pools)


    # -- gap recovery ---------------------------------------------------

    def recover_gaps(self, summary: RunSummary, pools: list[str] | None = None) -> int:
        """Re-fetch recent signatures for pools whose stream is not covering them.

        WSS pubsub has no replay: messages missed while disconnected are gone
        from the stream. The checkpoint's overlap window turns "gone" into
        "re-fetchable" via getSignaturesForAddress, and archive dedupe makes
        the deliberate overlap cost nothing twice.

        ``pools`` defaults to every pool previously recorded as subscribed —
        which is what a restart sees, since a new process starts with an empty
        in-memory subscription set. Pools with no checkpoint window are
        skipped: without a known "covered up to" time there is nothing to
        replay, and fetching blind would burn a credit per pool for nothing.
        """
        if self.api_key is None or self.checkpoints is None:
            return 0
        if pools is None:
            pools = self._previously_subscribed()
        rpc = self._make_rpc()
        recovered = 0
        now = self._now()
        # Overlap is expressed in slots in the schema; getSignaturesForAddress
        # filters by time, so convert: 0.4 s/slot is Solana's nominal rate.
        for pool in list(pools):
            cp = self.checkpoints.get(self.cfg.job, f"wss:{pool}")
            if cp is None or cp.last_event_time is None:
                continue
            overlap = _dt.timedelta(seconds=cp.overlap_slots * 0.4)
            since = cp.last_event_time - overlap
            if since > now:
                since = now
            try:
                result, body = rpc.signatures_for_address(
                    pool, limit=200,
                    request_key=f"gap:{pool}:{self.meter.run_token}:{int(since.timestamp())}")
            except Exception as exc:
                summary.errors.append(f"gap recovery {pool}: {exc}")
                self._log(f"warning: gap recovery failed for {pool}: {exc}")
                continue
            self.archive.write(source="helius", endpoint=f"gap:{pool}", body=body,
                               subdir="gaps", tag=pool, credits_spent=1)
            recovered += 1
            sigs = result.get("result") or []
            if sigs:
                latest = sigs[0].get("signature")
                self.checkpoints.advance(job=self.cfg.job, partition_key=f"wss:{pool}",
                                         last_event_id=latest, last_event_time=now)
        return recovered

    def _queue_subscribe(self, resume: list[str]) -> None:
        """Queue pools for (re-)subscription without duplicating the queue."""
        for p in resume:
            if p not in self._subscribed and p not in self._to_subscribe:
                self._to_subscribe.append(p)

    def _previously_subscribed(self) -> list[str]:
        """Pool addresses recorded as subscribed in an earlier process."""
        if self.conn is None:
            return []
        try:
            rows = self.conn.execute(
                "SELECT target FROM stream_subscriptions "
                "WHERE provider = 'helius' AND kind = 'websocket' "
                "ORDER BY last_message_at DESC NULLS LAST",
            ).fetchall()
            return [r[0] for r in rows]
        except Exception as exc:
            self.conn.rollback()
            self._log(f"warning: could not load previous subscriptions: {exc}")
            return []

    def _make_rpc(self):
        if self._rpc_factory is not None:
            return self._rpc_factory()
        from .adapters.helius import HeliusRpc
        return HeliusRpc(api_key=self.api_key, ledger=self.ledger, job=self.cfg.job)

    # -- the run loop ---------------------------------------------------

    def run(self, *, duration: float | None = None, once: bool = False,
            discovery_only: bool = False) -> RunSummary:
        """Run the collector. ``once`` does exactly one tick of each feed."""
        from .adapters.geckoterminal import fetch_new_pools as _gt_default
        from .adapters.dexscreener import fetch_profiles as _ds_profiles_default
        from .adapters.dexscreener import fetch_pairs as _ds_pairs_default

        if self._gt_fetcher is None and self.cfg.discovery.get("geckoterminal_enabled", True):
            self._gt_fetcher = _gt_default
        if self._ds_profiles is None and self.cfg.discovery.get("dexscreener_enabled", True):
            self._ds_profiles = _ds_profiles_default
            self._ds_pairs = self._ds_pairs or _ds_pairs_default

        summary = RunSummary()
        run_id = self._start_run()
        self._refresh_known()
        deadline = time.monotonic() + duration if duration else None
        timeout = float(self.cfg.discovery.get("http_timeout_seconds", 15))
        retries = int(self.cfg.discovery.get("http_retries", 3))
        if self._gt_fetcher is not None and getattr(self._gt_fetcher, "__name__", "") == "fetch_new_pools":
            self._gt_fetcher = lambda pages: _gt_default(pages=pages, timeout=timeout, retries=retries)

        gt_every = float(self.cfg.discovery["geckoterminal_poll_seconds"])
        ds_every = float(self.cfg.discovery["dexscreener_poll_seconds"])
        next_gt = next_ds = 0.0

        if self.api_key is None and not discovery_only:
            self._log("note: HELIUS_API_KEY not set — discovery + archiving only, no WSS stream")

        try:
            if self._wss_enabled(discovery_only):
                resume = self._previously_subscribed()
                if resume:
                    self._log(f"resuming {len(resume)} pool(s) subscribed by an earlier run")
                    recovered = self.recover_gaps(summary, pools=resume)
                    if recovered:
                        self._log(f"gap recovery re-fetched signatures for {recovered} pool(s)")
                    self._queue_subscribe(resume)

            while True:
                now_mono = time.monotonic()
                if deadline is not None and now_mono >= deadline:
                    break
                tick_new: list[PoolCandidate] = []

                if self._gt_fetcher is not None and now_mono >= next_gt:
                    cands = self.poll_geckoterminal(summary)
                    tick_new += self._process_candidates(cands, summary)
                    next_gt = time.monotonic() + gt_every

                if self._ds_profiles is not None and now_mono >= next_ds:
                    cands = self.poll_dexscreener(summary)
                    tick_new += self._process_candidates(cands, summary)
                    next_ds = time.monotonic() + ds_every

                if self._wss_enabled(discovery_only):
                    wanted = [*(c.pool_address for c in tick_new), *self._to_subscribe]
                    self._ensure_streams(list(dict.fromkeys(wanted)), summary)
                    self._to_subscribe = [p for p in self._to_subscribe
                                          if p not in self._subscribed]
                    self.drain_frames(0.5, summary)
                    self.heartbeat()

                if once:
                    break
                self._sleep(1.0)

            # Bookkeeping lives INSIDE the try: a crash while settling spend
            # or advancing the checkpoint must mark the run failed, not leave
            # it 'running' forever (observed live on 2026-10-16).
            self.meter.flush()
            if self.checkpoints is not None:
                self.checkpoints.advance(job=self.cfg.job, partition_key="feeds",
                                         last_event_time=self._now())
            self._finish_run(run_id, summary, status="success")
        except BaseException as exc:
            summary.errors.append(f"{type(exc).__name__}: {exc}")
            self._finish_run(run_id, summary, status="failed", error=str(exc))
            raise
        finally:
            for stream in self._streams:
                stream.close()
            try:
                self.meter.flush()   # best-effort for crash paths; refusal above already surfaced
            except Exception as exc:
                summary.errors.append(f"meter flush: {exc}")
                self._log(f"WARNING: final meter flush failed: {exc}")
        summary.helius_credits = self.meter.credits_charged
        summary.pools_seen = len(self._known_pools)
        return summary

    def _process_candidates(self, cands: list[PoolCandidate],
                            summary: RunSummary) -> list[PoolCandidate]:
        now = self._now()
        f = self.cfg.filter
        accepted: list[PoolCandidate] = []
        for cand in cands:
            summary.pools_seen += 1
            ok, reason = passes_filter(
                cand, min_liquidity_usd=float(f["min_liquidity_usd"]),
                min_age_hours=float(f["min_age_hours"]),
                min_txns_h1=f.get("min_txns_h1"), now=now)
            if not ok:
                continue
            accepted.append(cand)
        return self.register(accepted, summary)

    # -- bookkeeping ----------------------------------------------------

    def _start_run(self):
        if self.conn is None:
            return None
        try:
            # A SIGKILL cannot run bookkeeping, so a killed predecessor's row
            # stays 'running' forever and the history shows two live
            # collectors. The next start is the earliest honest witness:
            # close those rows out as failed-with-interruption instead of
            # leaving them open (accruing while we verify exit criteria).
            self.conn.execute(
                "UPDATE job_runs SET status = 'failed', finished_at = now(), "
                "  error = COALESCE(error, 'interrupted before shutdown bookkeeping "
                "(process died or was killed)') "
                "WHERE job = %s AND status = 'running'",
                (self.cfg.job,))
            cur = self.conn.execute(
                "INSERT INTO job_runs (job, status, params) VALUES (%s, 'running', %s) "
                "RETURNING run_id",
                (self.cfg.job, '{"phase": 1}'))
            row = cur.fetchone()
            self.conn.commit()
            return row[0] if row else None
        except Exception as exc:
            self.conn.rollback()
            self._log(f"warning: job_runs start failed: {exc}")
            return None

    def _finish_run(self, run_id, summary: RunSummary, *, status: str,
                    error: str | None = None) -> None:
        if self.conn is None or run_id is None:
            return
        try:
            self.conn.execute(
                "UPDATE job_runs SET finished_at = now(), status = %s, "
                "  rows_written = %s, credits_spent = %s, error = %s "
                "WHERE run_id = %s",
                (status, summary.frames_archived + summary.pools_new,
                 summary.helius_credits, error, run_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()


def disk_stats(data_dir: Path, *, now: _dt.datetime | None = None) -> dict:
    """Measure archived growth so storage can be projected, not guessed (doc 07)."""
    data_dir = Path(data_dir)
    per_subdir: dict[str, dict[str, int]] = {}
    total_bytes = total_files = 0
    mtimes: list[float] = []
    if data_dir.exists():
        for path in sorted(data_dir.rglob("*")):
            if not path.is_file():
                continue
            size = path.stat().st_size
            rel = path.relative_to(data_dir)
            sub = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            bucket = per_subdir.setdefault(sub, {"files": 0, "bytes": 0})
            bucket["files"] += 1
            bucket["bytes"] += size
            total_bytes += size
            total_files += 1
            mtimes.append(path.stat().st_mtime)
    span_hours = 0.0
    if len(mtimes) >= 2:
        span_hours = (max(mtimes) - min(mtimes)) / 3600.0
    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "per_subdir": per_subdir,
        "span_hours": span_hours,
        "bytes_per_hour": (total_bytes / span_hours) if span_hours > 0 else None,
        "projected_bytes_per_day": (total_bytes / span_hours * 24) if span_hours > 0 else None,
    }
