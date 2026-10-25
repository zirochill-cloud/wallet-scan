"""Phase 1 forward-collector checks (doc 07 Phase 1 exit criteria).

Everything here runs offline against fakes: the filter, the token bucket, the
append-only archive's dedupe, the checkpoint overlap math, WSS metering, the
frame loop, and a full collector tick with injected feeds — including the
restart-is-idempotent property the kill-recovery criterion rests on.

Run: .venv/bin/python tests/test_phase1.py
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.adapters import PoolCandidate
from solscope.adapters.dexscreener import parse_pairs, parse_profiles
from solscope.adapters.geckoterminal import parse_pool
from solscope.adapters.helius import LaserStream
from solscope.archive import RawArchive
from solscope.checkpoint import Checkpoint, CheckpointStore
from solscope.collector import Collector, Meter, passes_filter
from solscope.config import load_budget, load_collector
from solscope.ledger import BudgetExceeded, LedgerRow
from solscope.ratelimit import TokenBucket

NOW = _dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=_dt.timezone.utc)
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def expect_raises(exc_type, fn):
    try:
        fn()
    except exc_type as exc:
        return exc
    except Exception as other:  # noqa: BLE001 - test helper surfaces mismatches
        raise AssertionError(f"expected {exc_type.__name__}, got {type(other).__name__}: {other}")
    raise AssertionError(f"expected {exc_type.__name__}, but call succeeded")


# -- fakes ----------------------------------------------------------------

class FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakePoolDB:
    """In-memory stand-in for chains/tokens/pools — just enough SQL surface
    for the collector's upserts and restart reload."""

    def __init__(self):
        self.pools: dict[str, tuple] = {}
        self.mints: set[str] = set()
        self.checkpoints: dict[tuple, tuple] = {}
        self.subscriptions: dict[str, dict] = {}
        self.disconnects: list[str] = []

    def execute(self, sql: str, params: tuple = ()):
        sql = " ".join(sql.split())
        if sql.startswith("INSERT INTO job_checkpoints"):
            job, part, slot, ev_time, ev_id, overlap, _state = params
            prev = self.checkpoints.get((job, part))
            merged = list(prev) if prev else [job, part, None, None, None, None, overlap]
            # COALESCE semantics from the real UPSERT: Nulls keep the old value.
            if slot is not None:
                merged[2] = slot
            if ev_time is not None:
                merged[3] = ev_time
            if ev_id is not None:
                merged[4] = ev_id
            self.checkpoints[(job, part)] = tuple(merged)
            return FakeCursor([])
        if sql.startswith("INSERT INTO stream_subscriptions"):
            target = params[-1]
            self.subscriptions.setdefault(
                target, {"target": target, "disconnect_count": 0,
                         "last_error": None, "is_active": True})
            return FakeCursor([])
        if sql.startswith("SELECT target FROM stream_subscriptions"):
            rows = [(s["target"],) for s in self.subscriptions.values()
                    if s["is_active"]]
            return FakeCursor(rows)
        if sql.startswith("UPDATE stream_subscriptions SET disconnect_count"):
            error, target = params
            if target in self.subscriptions:
                self.subscriptions[target]["disconnect_count"] += 1
                self.subscriptions[target]["last_error"] = error
                self.subscriptions[target]["is_active"] = False
                self.disconnects.append(target)
            return FakeCursor([])
        if sql.startswith("UPDATE stream_subscriptions SET last_message_at"):
            return FakeCursor([])
        if sql.startswith("SELECT job, partition_key"):
            return FakeCursor([self.checkpoints.get((params[0], params[1]))]
                              if (params[0], params[1]) in self.checkpoints else [])
        if sql.startswith("INSERT INTO tokens"):
            self.mints.add(params[0])
            return FakeCursor([params[0]])
        if sql.startswith("INSERT INTO pools"):
            if params[0] in self.pools:
                return FakeCursor([])          # ON CONFLICT DO NOTHING
            self.pools[params[0]] = params
            return FakeCursor([params[0]])
        if sql.startswith("INSERT INTO chains"):
            return FakeCursor([])
        if sql.startswith("SELECT pool_address FROM pools"):
            return FakeCursor([(p,) for p in self.pools])
        if sql.startswith("SELECT mint FROM tokens"):
            return FakeCursor([(m,) for m in self.mints])
        return FakeCursor([])

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class FakeLedger:
    """Records charges; can be armed to refuse like a cap would."""

    def __init__(self, refuse_after: int | None = None):
        self.charges: list[dict] = []
        self.refuse_after = refuse_after

    def charge(self, **kwargs):
        if self.refuse_after is not None and len(self.charges) >= self.refuse_after:
            from solscope.ledger import BudgetExceeded
            raise BudgetExceeded("cap reached (fake)")
        self.charges.append(kwargs)
        credits = kwargs.get("credits")
        if credits is None:
            credits = kwargs.get("units", 0)
        return LedgerRow(len(self.charges), kwargs["provider"], kwargs["job"],
                         kwargs["operation"], kwargs.get("units", 0), credits,
                         _dt.date(2026, 10, 1))


class FakeWS:
    """Scripted websocket: outbound frames recorded, inbound queue scripted.

    Deliberately mirrors ``websocket-client`` 1.9.2's real API — ``recv(self)``
    takes NO timeout kwarg, and the idle budget lives at socket level via
    ``settimeout``. An earlier double that accepted ``timeout=None`` hid a
    signature mismatch which zeroed the live stream (Phase 1 run 2026-10-17):
    every frame loop iteration died with TypeError before reading a byte.
    """

    def __init__(self, inbound: list[str] | None = None, hang: bool = False):
        self.sent: list[str] = []
        self._inbound = list(inbound or [])
        self.hang = hang
        self.closed = False
        self.socket_timeouts: list[float | None] = []

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def settimeout(self, value) -> None:
        self.socket_timeouts.append(value)

    def recv(self):
        if self._inbound:
            return self._inbound.pop(0)
        if self.hang:
            import socket
            raise socket.timeout("scripted idle")
        raise ConnectionError("scripted close")

    def close(self) -> None:
        self.closed = True


class TimeoutRecvWS(FakeWS):
    """The other real shape: a socket whose ``recv`` accepts the kwarg."""

    def recv(self, timeout=None):  # noqa: ARG002 — mirrors that library's sig
        if self._inbound:
            return self._inbound.pop(0)
        if self.hang:
            import socket
            raise socket.timeout("scripted idle")
        raise ConnectionError("scripted close")


# -- fixtures (shapes verified live 2026-10-16) ---------------------------

GT_POOL = {
    "id": "solana_3w5wDZ1dLqZJFbUozxwNpry7eWhoKNs9XFBMopEz5vVi",
    "type": "pool",
    "attributes": {
        "address": "3w5wDZ1dLqZJFbUozxwNpry7eWhoKNs9XFBMopEz5vVi",
        "name": "BARRON / SOL",
        "pool_created_at": "2026-10-16T09:00:00Z",      # 3h before NOW
        "reserve_in_usd": "2140.8647",
        "transactions": {"h1": {"buys": 65, "sells": 57}},
    },
    "relationships": {
        "base_token": {"data": {"id": "solana_8y5oXWLSzG6rneTmF4mU615UKpjDqkeGE6SZ5SukBTgE", "type": "token"}},
        "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112", "type": "token"}},
        "dex": {"data": {"id": "pumpswap", "type": "dex"}},
    },
}

DS_PAIRS = {
    "pairs": [{
        "chainId": "solana",
        "dexId": "pumpswap",
        "pairAddress": "PAIRaddr111111111111111111111111111111111",
        "baseToken": {"address": "MINT111111111111111111111111111111111111111"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
        "pairCreatedAt": 1760619600000,                 # ms epoch
        "liquidity": {"usd": 5231.44},
        "txns": {"h1": {"buys": 30, "sells": 20}},
    }]
}


def cand(**overrides) -> PoolCandidate:
    base = dict(pool_address="POOL111", base_mint="MINT1", quote_mint="So11",
                dex="pumpswap", created_at=NOW - _dt.timedelta(hours=3),
                liquidity_usd=5000.0, txns_h1=42, source="test")
    base.update(overrides)
    return PoolCandidate(**base)


# -- tests ----------------------------------------------------------------

def test_filter() -> None:
    ok, reason = passes_filter(cand(), min_liquidity_usd=2000, min_age_hours=1.0,
                               min_txns_h1=5, now=NOW)
    check("filter accepts a liquid 3h-old active pool", ok and reason == "pass")

    ok, reason = passes_filter(cand(liquidity_usd=500.0), min_liquidity_usd=2000,
                               min_age_hours=1.0, min_txns_h1=5, now=NOW)
    check("filter rejects low liquidity", not ok and reason == "low_liquidity")

    ok, reason = passes_filter(cand(liquidity_usd=None), min_liquidity_usd=2000,
                               min_age_hours=1.0, min_txns_h1=5, now=NOW)
    check("filter fails closed on unknown liquidity", not ok and reason == "unknown_liquidity")

    ok, reason = passes_filter(cand(created_at=NOW - _dt.timedelta(minutes=10)),
                               min_liquidity_usd=2000, min_age_hours=1.0,
                               min_txns_h1=5, now=NOW)
    check("filter rejects pools younger than one hour", not ok and reason == "too_new")

    ok, reason = passes_filter(cand(created_at=None), min_liquidity_usd=2000,
                               min_age_hours=1.0, min_txns_h1=5, now=NOW)
    check("filter fails closed on unknown age", not ok and reason == "unknown_age")

    ok, reason = passes_filter(cand(txns_h1=None), min_liquidity_usd=2000,
                               min_age_hours=1.0, min_txns_h1=5, now=NOW)
    check("filter fails closed on unknown activity", not ok and reason == "unknown_activity")

    ok, reason = passes_filter(cand(txns_h1=None), min_liquidity_usd=2000,
                               min_age_hours=1.0, min_txns_h1=None, now=NOW)
    check("filter: null min_txns_h1 disables the activity gate", ok and reason == "pass")


def test_ratelimit() -> None:
    t = [0.0]
    slept: list[float] = []
    bucket = TokenBucket(2, 60, clock=lambda: t[0], sleeper=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    check("bucket starts with capacity tokens", bucket.try_acquire(2) and not bucket.try_acquire(1))
    t[0] += 2.0  # 2 seconds at 60/min = 2 tokens refilled
    check("bucket refills with time", bucket.try_acquire(2))
    before = len(slept)
    bucket.acquire(1)
    check("acquire sleeps when empty", len(slept) > before)
    expect_raises(ValueError, lambda: TokenBucket(0, 60))


def test_archive_dedupe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        archive = RawArchive(pathlib.Path(tmp))
        body = json.dumps({"n": 1}).encode()
        r1 = archive.write(source="s", endpoint="e", body=body, tag="x", subdir="discovery")
        r2 = archive.write(source="s", endpoint="e", body=body, tag="x", subdir="discovery")
        check("identical body+endpoint is not re-archived",
              r1.path == r2.path and r1.wrote_file and not r2.wrote_file)
        r3 = archive.write(source="s", endpoint="other", body=body, tag="x", subdir="discovery")
        check("same body from a different endpoint is a new observation",
              r3.indexed_new and r3.path != r1.path)
        files = [p for p in (pathlib.Path(tmp)).rglob("*.json")]
        check("on-disk files never duplicated", len(files) == 2, f"{len(files)} files")


def test_checkpoint_overlap() -> None:
    store = CheckpointStore(conn=None, default_overlap=64)
    store.get = lambda *a, **k: None  # cold start
    check("cold start returns an empty window",
          store.recovery_window(job="collect", now_slot=1000) == (1000, 1000))

    store.get = lambda *a, **k: Checkpoint("collect", "", 1000, None, None, None, 64)
    lo, hi = store.recovery_window(job="collect", now_slot=1100)
    check("recovery window starts overlap_slots before the checkpoint",
          lo == 1000 - 64 and hi == 1100)


def test_meter() -> None:
    metering = {"wss_bytes_per_unit": 100000, "wss_credits_per_unit": 2,
                "wss_connection_credits": 1, "flush_every_bytes": 100000}
    ledger = FakeLedger()
    meter = Meter(ledger=ledger, metering=metering)
    meter.add_connection()
    check("connection open charges 1 credit",
          ledger.charges[-1]["operation"] == "websocket_connection" and ledger.charges[-1]["units"] == 1)
    meter.add_bytes(99_999)
    check("sub-unit bytes do not flush early", len(ledger.charges) == 1)
    meter.add_bytes(1)          # crosses 100 KB exactly -> auto flush
    check("100KB flushes 1 unit", len(ledger.charges) == 2 and ledger.charges[-1]["units"] == 1)
    meter.add_bytes(150_000)
    check("1.5 units rounds up to 2", len(ledger.charges) == 3 and ledger.charges[-1]["units"] == 2)
    meter.flush()
    check("flush after empty accumulation is a no-op", len(ledger.charges) == 3)
    keys = [c.get("request_key") for c in ledger.charges[1:]]
    check("flush request keys are sequenced (crash-safe idempotency)",
          keys == [f"wss-data:collect:{meter.run_token}:1",
                   f"wss-data:collect:{meter.run_token}:2"])

    # Regression: a second process (a restarted collector) numbers its flushes
    # from 1 again. If keys collide across runs the ledger treats them as
    # retries and drops real spend — measured against a live archive once.
    m3_ledger = FakeLedger()
    m3 = Meter(ledger=m3_ledger, metering=metering)
    m3.add_bytes(100_000)
    check("flush keys never collide across runs",
          m3_ledger.charges[0]["request_key"] != keys[0])

    broke = FakeLedger(refuse_after=1)
    m2 = Meter(ledger=broke, metering=metering)
    m2.add_bytes(100_000)      # charge 1: allowed
    expect_raises(BudgetExceeded, lambda: m2.add_bytes(100_000))  # charge 2: refused
    check("a ledger refusal propagates from the flush path (fail-closed)", True)


def test_parsers() -> None:
    gt = parse_pool(GT_POOL)
    check("GT pool parses to a candidate",
          gt is not None and gt.pool_address == GT_POOL["id"].split("_")[1]
          and gt.base_mint.startswith("8y5o") and gt.dex == "pumpswap"
          and abs(gt.liquidity_usd - 2140.8647) < 1e-6 and gt.txns_h1 == 122
          and gt.created_at is not None)
    check("GT malformed record degrades to None", parse_pool({"id": "x"}) is None)

    mints = parse_profiles(json.dumps([
        {"chainId": "solana", "tokenAddress": "MINT_A"},
        {"chainId": "ethereum", "tokenAddress": "0xdead"},
        {"chainId": "solana"},
    ]).encode())
    check("profiles feed extracts Solana mints only", mints == ["MINT_A"])
    check("profiles feed degrades on malformed JSON", parse_profiles(b"not json") == [])

    pairs = parse_pairs(json.dumps(DS_PAIRS).encode(), mint="MINT1")
    check("DS pair lookup parses liquidity/created_at/txns",
          len(pairs) == 1 and pairs[0].pool_address.startswith("PAIRaddr")
          and pairs[0].liquidity_usd == 5231.44 and pairs[0].txns_h1 == 50
          and pairs[0].created_at is not None)


def test_laserstream() -> None:
    notif = json.dumps({"jsonrpc": "2.0", "method": "logsNotification",
                        "params": {"subscription": 7, "result": {"value": {"signature": "sig1"}}}})
    confirm = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})
    ws = FakeWS(inbound=[confirm, notif])
    stream = LaserStream(url="wss://test", ws_factory=lambda url: ws)
    stream.connect()
    stream.subscribe("POOL_A")
    req = json.loads(ws.sent[0])
    check("subscribe sends one logsSubscribe with exactly one mention",
          req["method"] == "logsSubscribe" and req["params"][0]["mentions"] == ["POOL_A"])
    stream.note_confirmation(1, 7)
    frames = []
    try:
        for f in stream.frames(max_seconds=5):
            frames.append(f)
    except ConnectionError:
        pass
    check("confirmation + notification frames arrive verbatim",
          [f.kind for f in frames] == ["confirmation", "notification"])
    check("notification maps server sub id back to the pool",
          frames[1].subscription_id == "POOL_A")
    check("byte accounting counts raw frame bytes", stream.bytes_received > 0)

    ws2 = FakeWS(inbound=[notif])       # closes (with an error) after frames
    s2 = LaserStream(url="wss://test", ws_factory=lambda url: ws2)
    s2.connect()
    got = []
    raised = None
    try:
        for f in s2.frames(max_seconds=5):
            got.append(f)
    except ConnectionError as exc:
        raised = exc
    check("socket death surfaces as an error after delivering frames",
          raised is not None and len(got) == 1 and got[0].kind == "notification")


def test_frame_recv_signatures() -> None:
    """The frame loop must survive both real ``recv()`` shapes.

    Live failure 2026-10-17: ``websocket-client``'s ``recv`` is ``(self)``,
    but the frame loop (and the test double) passed ``timeout=…`` — every
    iteration raised TypeError before reading a byte, so a 10-minute run
    archived **0** frames while the collector believed it was streaming.
    The socket's signature is probed per connection, not assumed.
    """
    notif = json.dumps({"jsonrpc": "2.0", "method": "logsNotification",
                        "params": {"subscription": 7,
                                   "result": {"value": {"signature": "sig9"}}}})

    # Shape A — websocket-client: recv(self), idle budget set on the socket.
    ws = FakeWS(inbound=[notif])
    strict = LaserStream(url="wss://test", ws_factory=lambda url: ws)
    strict.connect()
    check("websocket-client recv() detected as kwarg-less",
          strict._recv_takes_timeout is False)
    got, raised = [], None
    try:
        for f in strict.frames(timeout=0.2, max_seconds=5):
            got.append(f)
    except ConnectionError as exc:
        raised = exc
    check("kwarg-less socket still delivers frames (no TypeError)",
          raised is not None and [f.kind for f in got] == ["notification"])
    check("idle budget applied at socket level via settimeout",
          bool(ws.socket_timeouts) and all(t <= 0.2 for t in ws.socket_timeouts),
          f"settimeout calls: {ws.socket_timeouts}")

    # Shape B — a socket whose recv() does take the kwarg.
    ws2 = TimeoutRecvWS(inbound=[notif])
    flex = LaserStream(url="wss://test", ws_factory=lambda url: ws2)
    flex.connect()
    check("kwarg-taking recv detected", flex._recv_takes_timeout is True)
    got2, raised2 = [], None
    try:
        for f in flex.frames(timeout=0.2, max_seconds=5):
            got2.append(f)
    except ConnectionError as exc:
        raised2 = exc
    check("kwarg socket delivers the same frames",
          raised2 is not None and [f.kind for f in got2] == ["notification"])

    # An idle (no data) strict socket must be indistinguishable from a quiet
    # stream: timeout → continue, not a crash, and not a silent early end.
    idle = FakeWS(inbound=[], hang=True)
    quiet = LaserStream(url="wss://test", ws_factory=lambda url: idle)
    quiet.connect()
    delivered = list(quiet.frames(timeout=0.05, max_seconds=0.2))
    check("idle strict socket yields nothing but does not raise",
          delivered == [])


def test_kill_recovery_resume_and_checkpoint() -> None:
    """Restart wiring: settle checkpoints from frames, then resume (doc 07).

    Run 1 streams a confirmation + a notification (with ``context.slot``),
    records the subscription and settles ``wss:<pool>`` — disk first, mark
    second. A brand-new Collector over the same database must then (a) queue
    the pool for re-subscription even though this process never discovered it,
    (b) re-fetch the outage window exactly once through the injected RPC, and
    (c) register zero new pools — the reload-is-idempotent property.
    """
    cfg = load_collector()
    budget = load_budget()
    pool = parse_pool(GT_POOL).pool_address   # from the fixture, verdant GT shape

    confirmation = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 3})
    notification = json.dumps({
        "jsonrpc": "2.0", "method": "logsNotification",
        "params": {"subscription": 3,
                   "result": {"context": {"slot": 900},
                              "value": {"signature": "sig7"}}}})

    def gt_fetcher(pages=1):
        body = json.dumps({"data": [GT_POOL]}).encode()
        return [(parse_pool(GT_POOL), body, "https://fake/gt?page=1")]

    rpc_calls: list[dict] = []

    def rpc_factory():
        class FakeRpc:
            def signatures_for_address(self, address, *, limit=1000,
                                       until=None, request_key=None):
                rpc_calls.append({"address": address, "limit": limit,
                                  "request_key": request_key})
                return ({"result": [{"signature": "sig7"}]},
                        b'{"jsonrpc":"2.0","result":[{"signature":"sig7"}]}')
        return FakeRpc()

    def fresh_ws(url):                       # a new socket per (re)connect
        return FakeWS(inbound=[confirmation, notification], hang=True)

    with tempfile.TemporaryDirectory() as tmp:
        discovery = dict(cfg.discovery, dexscreener_enabled=False)
        cfg2 = type(cfg)(job=cfg.job, data_dir=pathlib.Path(tmp), discovery=discovery,
                         filter=cfg.filter, subscription=cfg.subscription,
                         metering=cfg.metering, checkpoint=cfg.checkpoint)
        db = FakePoolDB()

        c1 = Collector(cfg=cfg2, budget=budget, conn=db, api_key="test-key",
                       ws_factory=fresh_ws, gt_fetcher=gt_fetcher,
                       rpc_factory=rpc_factory,
                       clock=lambda: NOW, sleep=lambda s: None)
        s1 = c1.run(once=True)

        check("run 1 registers the candidate pool", pool in db.pools)
        check("run 1 records the subscription row", pool in db.subscriptions)
        check("run 1 archives every frame",
              s1.frames_archived >= 2, f"got {s1.frames_archived}")
        wss_dir = pathlib.Path(tmp) / "wss"
        check("wss frames land on disk verbatim", wss_dir.exists()
              and any(wss_dir.rglob("*")))
        cp = db.checkpoints.get((cfg.job, f"wss:{pool}"))
        check("run 1 settles wss:<pool> with the frame's slot",
              cp is not None and cp[2] == 900, f"checkpoint={cp}")
        check("run 1 performs no gap recovery (nothing missed)", rpc_calls == [])

        # Brand-new process, same DB: discovery re-polls an already-known pool.
        c2 = Collector(cfg=cfg2, budget=budget, conn=db, api_key="test-key",
                       ws_factory=fresh_ws, gt_fetcher=gt_fetcher,
                       rpc_factory=rpc_factory,
                       clock=lambda: NOW, sleep=lambda s: None)
        s2 = c2.run(once=True)

        check("restart re-fetches the outage window exactly once",
              len(rpc_calls) == 1 and rpc_calls[0]["address"] == pool,
              f"calls={rpc_calls}")
        check("restart recovery carries an idempotent request_key",
              bool(rpc_calls)
              and str(rpc_calls[0]["request_key"]).startswith(f"gap:{pool}:"),
              f"calls={rpc_calls}")
        check("recovery body archived verbatim under gaps/",
              (pathlib.Path(tmp) / "gaps").exists())
        check("restart re-subscribes the previously watched pool",
              pool in c2._subscribed, f"subscribed={sorted(c2._subscribed)}")
        check("restart registers zero new pools (idempotent)",
              s2.pools_new == 0, f"pools_new={s2.pools_new}")
        look = db.subscriptions.get(pool)
        check("restart does not mark the pool as disconnected", look is not None
              and look["disconnect_count"] == 0, f"row={look}")


def test_collector_tick_idempotent() -> None:
    """Two full ticks over identical feed data: the second must add nothing.

    This is the offline backbone of the kill-recovery criterion: a restart
    re-polls the overlap and archive-level dedupe plus the pools table make
    the replay a no-op — no duplicate events, no duplicate registry rows.
    """
    cfg = load_collector()
    budget = load_budget()

    def gt_fetcher(pages=1):
        body = json.dumps({"data": [GT_POOL]}).encode()
        cand = parse_pool(GT_POOL)
        return [(cand, body, "https://fake/gt?page=1")]

    with tempfile.TemporaryDirectory() as tmp:
        discovery_off_ds = dict(cfg.discovery, dexscreener_enabled=False,
                                 geckoterminal_pages=1)
        cfg2 = type(cfg)(job=cfg.job, data_dir=pathlib.Path(tmp), discovery=discovery_off_ds,
                         filter=cfg.filter, subscription=cfg.subscription,
                         metering=cfg.metering, checkpoint=cfg.checkpoint)
        db = FakePoolDB()
        c = Collector(cfg=cfg2, budget=budget, conn=db, gt_fetcher=gt_fetcher,
                      clock=lambda: NOW, sleep=lambda s: None)
        s1 = c.run(once=True, discovery_only=True)
        check("first tick archives and registers the pool",
              s1.pools_new == 1 and s1.geckoterminal_requests == 1
              and s1.dexscreener_requests == 0)

        # Simulated restart: brand-new Collector, same database.
        c2 = Collector(cfg=cfg2, budget=budget, conn=db, gt_fetcher=gt_fetcher,
                       clock=lambda: NOW, sleep=lambda s: None)
        s2 = c2.run(once=True, discovery_only=True)
        check("restart re-poll registers zero new pools (idempotent)",
              s2.pools_new == 0, f"pools_new={s2.pools_new}")
        check("pools table holds exactly one row", len(db.pools) == 1)

        archive_root = pathlib.Path(tmp)
        gt_files = list(archive_root.rglob("gt_new_pools*.json"))
        check("duplicate feed response archived once on disk", len(gt_files) == 1,
              f"{len(gt_files)} files: {[f.name for f in gt_files]}")

        pool_files = [p for p in archive_root.rglob("*.json")]
        check("no duplicate payload files across restart", len(pool_files) == 1)


def test_budget_enforcement_in_collector() -> None:
    """A ledger that refuses must stop the run — not degrade it."""
    cfg = load_collector()
    budget = load_budget()

    def gt_fetcher(pages=1):
        body = json.dumps({"data": [GT_POOL]}).encode()
        return [(parse_pool(GT_POOL), body, "https://fake/gt?page=1")]

    with tempfile.TemporaryDirectory() as tmp:
        cfg2 = type(cfg)(job=cfg.job, data_dir=pathlib.Path(tmp),
                         discovery=dict(cfg.discovery, dexscreener_enabled=False),
                         filter=cfg.filter, subscription=cfg.subscription,
                         metering=cfg.metering, checkpoint=cfg.checkpoint)
        ledger = FakeLedger(refuse_after=0)
        c = Collector(cfg=cfg2, budget=budget, ledger=ledger, gt_fetcher=gt_fetcher,
                      clock=lambda: NOW, sleep=lambda s: None)
        expect_raises(BudgetExceeded, lambda: c.run(once=True, discovery_only=True))


def test_config_loads() -> None:
    cfg = load_collector()
    check("collector.yaml loads", cfg.job == "collect" and cfg.filter["min_age_hours"] == 1.0)
    budget = load_budget()
    helius = budget.provider("helius")
    check("budget meters WSS bytes (research correction)",
          helius.operation_costs.get("websocket_data") == 2
          and helius.operation_costs.get("websocket_connection") == 1)
    check("no zero-credit websocket line remains",
          "websocket_message" not in helius.operation_costs)
    check("keyless discovery providers are request-metered",
          budget.provider("geckoterminal").cost_of("new_pools") == 1
          and budget.provider("dexscreener").cost_of("token_profiles") == 1)


def main() -> int:
    for fn in (test_config_loads, test_filter, test_ratelimit, test_archive_dedupe,
               test_checkpoint_overlap, test_meter, test_parsers, test_laserstream,
               test_frame_recv_signatures, test_kill_recovery_resume_and_checkpoint,
               test_collector_tick_idempotent, test_budget_enforcement_in_collector):
        print(f"{fn.__name__}:")
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURES: {', '.join(_failures)}")
        return 1
    print("ALL PHASE 1 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
