"""Per-endpoint token-bucket rate limiter (doc 08 trap 6).

DexScreener allows 300 req/min on pair endpoints but only 60 req/min on
discovery endpoints, and GeckoTerminal's keyless tier is roughly 10 calls/min
per IP. A careless polling loop exhausts the low one and can get the client
blocked, so every outbound feed goes through a bucket here.

A bucket is refill-per-second based and blocking on acquire: the caller's loop
is throttled rather than the provider. Sleeping in the caller thread is
acceptable for a collector — it *should* slow down, not buffer requests.
"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """Blocking token bucket: ``capacity`` requests, refilled at ``per_minute``/min."""

    def __init__(self, capacity: int, per_minute: float, *, clock=None, sleeper=None) -> None:
        if capacity <= 0 or per_minute <= 0:
            raise ValueError(
                f"capacity and per_minute must be > 0 (got {capacity}, {per_minute})"
            )
        self.capacity = float(capacity)
        self.per_second = float(per_minute) / 60.0
        self._tokens = float(capacity)
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._lock = threading.Lock()
        self._last = self._clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.per_second)
        self._last = now

    def try_acquire(self, n: float = 1.0) -> bool:
        """Take ``n`` tokens if available; never blocks. False means throttled."""
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def acquire(self, n: float = 1.0) -> float:
        """Take ``n`` tokens, sleeping as needed. Returns seconds slept."""
        slept = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return slept
                deficit = n - self._tokens
                wait = deficit / self.per_second
            self._sleep(wait)
            slept += wait

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens
