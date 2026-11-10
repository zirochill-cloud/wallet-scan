"""Helius adapter: LaserStream WSS (standard methods) + HTTP gap recovery.

Verified against Helius docs (2026-10-16):

* Endpoint: ``wss://mainnet.helius-rpc.com/?api-key=<KEY>`` — the key rides in
  the query string; no auth-header method is documented.
* **Standard Solana pubsub is included on Free**, including ``logsSubscribe``.
  Helius extensions are not.
* ``logsSubscribe`` mentions take **exactly one** address (Solana spec: more
  returns Invalid params), so one subscription per shortlisted pool.
* Metering: **2 credits per 0.1 MB uncompressed streamed data, 1 credit to
  open a connection.** The collector converts byte counts to credits; nothing
  here assumes WSS is free.
* Free plan: up to 5 simultaneous WSS connections.

The socket is injected (``ws_factory``) so every code path here is unit
testable without a network, and so the same frame loop runs against
``websocket-client`` in production.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .httpfetch import USER_AGENT, FetchError, fetch_bytes

DEFAULT_WSS_URL = "wss://mainnet.helius-rpc.com/?api-key={key}"
RPC_HTTP = "https://mainnet.helius-rpc.com/?api-key={key}"
ADMIN_USAGE = "https://admin-api.helius.xyz/v0/admin/projects/{project}/usage"


def api_key_from_env(env: dict | None = None) -> str | None:
    env = env if env is not None else os.environ
    key = env.get("HELIUS_API_KEY", "").strip()
    return key or None


@dataclass
class Frame:
    """One raw WSS frame, verbatim, with parse attempted but not required."""
    raw: str
    received_at: _dt.datetime
    parsed: dict | None
    kind: str            # 'notification' | 'confirmation' | 'error' | 'unparsed'
    subscription_id: str | None


class LaserStream:
    """logsSubscribe stream over one socket. Frame iteration is pull-based so
    the collector interleaves discovery polls without threads."""

    def __init__(self, *, url: str, ws_factory: Callable, log=print) -> None:
        self._url = url
        self._ws_factory = ws_factory
        self._log = log
        self._ws = None
        self._next_id = 1
        self._subs: dict[int, str] = {}      # request id -> pool address
        self.subscription_ids: dict[str, int] = {}   # pool -> server sub id
        self.bytes_received = 0
        # websocket-client 1.x ``recv()`` takes NO timeout kwarg (its timeout is
        # socket-level), while some other sockets accept one. Probe the real
        # socket's signature instead of assuming — assuming silently killed the
        # stream leg with TypeError(…unexpected keyword argument 'timeout').
        self._recv_takes_timeout: bool = False

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def pools(self) -> list[str]:
        return list(self._subs.values())

    def connect(self) -> None:
        if self._ws is not None:
            return
        self._ws = self._ws_factory(self._url)
        self._recv_takes_timeout = _recv_takes_timeout(self._ws)

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None
                self.subscription_ids.clear()

    def subscribe(self, pool: str) -> int:
        """Send one logsSubscribe for ``pool`` (one mention per subscription)."""
        if self._ws is None:
            raise RuntimeError("subscribe() before connect()")
        req_id = self._next_id
        self._next_id += 1
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "logsSubscribe",
            "params": [{"mentions": [pool]}, {"commitment": "confirmed"}],
        }
        self._ws.send(json.dumps(msg))
        self._subs[req_id] = pool
        return req_id

    def frames(self, *, timeout: float = 1.0,
               max_seconds: float | None = None) -> Iterator[Frame]:
        """Yield frames until the deadline. A socket error propagates — that
        is how the collector learns the stream died and decides to reconnect
        (and to run gap recovery); a quiet end would hide the disconnect."""
        clock = _dt.datetime.now
        deadline = None
        if max_seconds is not None:
            import time as _time
            deadline = _time.monotonic() + max_seconds
        while True:
            if deadline is not None:
                import time as _time
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    return
                timeout = min(timeout, remaining)
            try:
                raw = self._recv(timeout)
            except Exception as exc:  # socket timeout is normal; closure ends it
                if _is_timeout(exc):
                    continue
                raise
            if raw is None:
                return
            self.bytes_received += len(raw.encode("utf-8", "replace"))
            yield self._to_frame(raw)

    def _recv(self, timeout: float):
        """One receive with an idle budget, whichever way the socket spells it.

        Supported signatures handled:

        * ``recv(timeout=…)`` — pass it through (kwarg round-trips to the fake
          and to libraries that document it).
        * ``recv()`` (websocket-client) — set the same budget on the socket via
          ``settimeout``, so an idle stream still surfaces as a timeout exception
          (``WebSocketTimeoutException`` → ``_is_timeout`` → loop continues).
        * neither → plain ``recv()``; the connection-level timeout still bounds it.
        """
        if self._recv_takes_timeout:
            return self._ws.recv(timeout=timeout)
        setter = getattr(self._ws, "settimeout", None)
        if setter is not None:
            setter(timeout)
        return self._ws.recv()

    def _to_frame(self, raw: str) -> Frame:
        now = _dt.datetime.now(_dt.timezone.utc)
        try:
            parsed = json.loads(raw)
        except ValueError:
            return Frame(raw, now, None, "unparsed", None)

        if not isinstance(parsed, dict):
            return Frame(raw, None or now, parsed, "unparsed", None)
        if "method" in parsed:                    # notification
            sid = parsed.get("params", {}).get("subscription")
            pool = None
            for p, s in self.subscription_ids.items():
                if s == sid:
                    pool = p
                    break
            return Frame(raw, now, parsed, "notification",
                         pool or (str(sid) if sid is not None else None))
        if "error" in parsed:                     # subscription error etc.
            return Frame(raw, now, parsed, "error", None)
        return Frame(raw, now, parsed, "confirmation", None)

    def note_confirmation(self, req_id, result) -> str | None:
        """Map a subscription confirmation's server id back to its pool.

        Returns the pool address (None if the id is unknown) so callers can
        attribute the frame to a pool rather than to a bare request id.
        """
        pool = self._subs.get(req_id)
        if pool is not None and isinstance(result, int):
            self.subscription_ids[pool] = result
        return pool


def _is_timeout(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or "timedout" in name


def _recv_takes_timeout(ws) -> bool:
    """Does this socket's ``recv`` accept a ``timeout`` kwarg?

    Probed per-connection: websocket-client's is ``(self)``, the unit-test
    double historically accepted ``timeout=None``, and other libraries differ.
    Match reality rather than either of those.
    """
    import inspect
    try:
        return "timeout" in inspect.signature(ws.recv).parameters
    except (TypeError, ValueError):          # builtins/C-implemented recv
        return False


class HeliusRpc:
    """Minimal HTTP RPC for gap recovery: signatures + transactions.

    Every call takes the ledger so spend is charged as it is made, with a
    ``request_key`` so a retried recovery after a crash cannot double-bill.
    """

    def __init__(self, *, api_key: str, ledger=None, job: str = "collect",
                 opener=None) -> None:
        self._url = RPC_HTTP.format(key=api_key)
        self._ledger = ledger
        self._job = job
        self._opener = opener or urllib.request.urlopen
        self._req_id = 1

    def _rpc(self, method: str, params: list, *, operation: str,
             request_key: str | None) -> tuple[dict, bytes]:
        if self._ledger is not None:
            self._ledger.charge(
                provider="helius", job=self._job, operation=operation, units=1,
                request_key=request_key,
            )
        payload = json.dumps({
            "jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params,
        }).encode()
        self._req_id += 1
        req = urllib.request.Request(
            self._url, data=payload,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        )
        body = self._post(req)
        try:
            return json.loads(body), body
        except ValueError as exc:
            raise FetchError(self._url, 200, f"malformed JSON: {exc}") from exc

    def _post(self, req) -> bytes:
        with self._opener(req, timeout=20) as resp:
            return resp.read()

    def signatures_for_address(self, address: str, *, limit: int = 1000,
                               before: str | None = None,
                               until: str | None = None,
                               request_key: str | None = None) -> tuple[dict, bytes]:
        """Signatures for ``address``, newest first.

        ``before`` is the backward cursor: pass the oldest signature already
        seen and the next page continues older. ``until`` is different — it
        stops *at* a signature. Without ``before`` a busy mint truncates at
        ``limit`` (measured: a 2,549-signature anchor returns 1,000 and the
        walk silently ends), which is why retrospective paging requires it.
        """
        params: list = [address, {"limit": limit}]
        if before:
            params[1]["before"] = before
        if until:
            params[1]["until"] = until
        return self._rpc("getSignaturesForAddress", params,
                         operation="get_signatures_for_address",
                         request_key=request_key)

    def token_accounts_by_owner(self, owner: str, *, program_id: str,
                                request_key: str | None = None) -> tuple[dict, bytes]:
        """Every token account ``owner`` holds, parsed (Phase 2 reserves).

        This is how pool reserves are read correctly. A single transaction's
        ``meta`` only lists accounts that transaction touched, so a pool vault
        untouched by the trade is invisible — measured 2026-10-25: a pool's
        two *visible* vaults implied a price 37% off the executed price while
        the pool-owned set was complete and reconciled to 0.12% against an
        independent snapshot. Enumerating by owner is the only reliable way
        to see the whole reserve set.
        """
        return self._rpc("getTokenAccountsByOwner",
                         [owner, {"programId": program_id},
                          {"encoding": "jsonParsed"}],
                         operation="get_token_accounts_by_owner",
                         request_key=request_key)

    def transaction(self, signature: str) -> tuple[dict, bytes]:
        # maxSupportedTransactionVersion MUST be >= 1: pump.fun/Raydium swaps
        # are versioned transactions, and asking for 0 returns a -32015 error
        # instead of data from exactly the pools that matter (doc 10 §4.1,
        # measured live 2026-10-15). If mainnet ever ships a higher version,
        # raise this ceiling — under-asking fails closed to "no data".
        return self._rpc("getTransaction",
                         [signature, {"encoding": "json",
                                      "maxSupportedTransactionVersion": 1,
                                      "commitment": "confirmed"}],
                         operation="get_transaction",
                         request_key=f"tx:{signature}")


def fetch_usage(*, api_key: str, project_id: str,
                opener=None) -> dict:
    """Provider-reported usage for the current credit cycle (Admin API).

    This is the independent side of the Phase 1 exit criterion 'recorded spend
    matches provider-reported usage'. Requires the project id from the Helius
    dashboard; without it the caller must report the criterion as unverified,
    not as passing.
    """
    url = ADMIN_USAGE.format(project=project_id)
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key, "User-Agent": USER_AGENT})
    opener = opener or urllib.request.urlopen
    with opener(req, timeout=20) as resp:
        return json.loads(resp.read())
