"""Shared HTTP fetch for keyless adapters.

Doc 10 finding #6, measured live: **DexScreener 403s Python's default
``Python-urllib/x.y`` User-Agent** on an otherwise successful request, so a
browser-like UA is not decoration — it is required for the call to succeed at
all.

Retries cover 429/5xx with exponential backoff, honouring ``Retry-After``.
Other 4xx are terminal: retrying a 404 or 400 just burns rate limit.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 solscope/0.1"
)


class FetchError(RuntimeError):
    """A provider call failed after retries (or terminally)."""

    def __init__(self, url: str, status: int | None, message: str) -> None:
        super().__init__(f"{url}: {message}" + (f" (HTTP {status})" if status else ""))
        self.url = url
        self.status = status


def fetch_bytes(url: str, *, timeout: float = 15.0, retries: int = 3,
                headers: dict[str, str] | None = None,
                sleeper=time.sleep, opener=None) -> tuple[bytes, int]:
    """GET ``url``, returning (body, http_status). Raises FetchError on failure."""
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    last_status: int | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=req_headers)
        opener = opener or urllib.request.urlopen
        try:
            with opener(request, timeout=timeout) as resp:
                return resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            if exc.code == 429 or exc.code >= 500:
                if attempt < retries:
                    sleeper(_backoff_seconds(attempt, exc.headers.get("Retry-After")))
                    continue
            raise FetchError(url, exc.code, f"{exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                sleeper(_backoff_seconds(attempt, None))
                continue
            raise FetchError(url, None, f"network error: {exc}") from exc

    raise FetchError(url, last_status, "retries exhausted")  # pragma: no cover


def fetch_json(url: str, **kwargs) -> tuple[object, int]:
    """fetch_bytes + json.loads. Malformed JSON is a hard error, not silence."""
    body, status = fetch_bytes(url, **kwargs)
    try:
        return json.loads(body), status
    except json.JSONDecodeError as exc:
        raise FetchError(url, status, f"malformed JSON: {exc}") from exc


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(60.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    base = min(30.0, 2.0 ** attempt)
    return base + random.uniform(0, base / 2)  # jitter: several loops at once
