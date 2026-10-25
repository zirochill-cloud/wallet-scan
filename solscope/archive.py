"""Append-only verbatim archive under ``data/raw/`` (doc 02 §3).

Invariant: **raw/ is append-only and never rewritten.** Every derived value
must be recomputable from it — that is the mitigation for provider churn
(doc 01 §1.5): when a free tier disappears, what was already fetched survives.

Two guarantees this module enforces itself, so no caller has to remember them:

1. **Nothing is written twice.** Files are created with ``O_EXCL``; if a file
   with the same name already exists and holds the same bytes, the write is a
   no-op (already archived). Identical content under a different name never
   happens for the same logical stream because names embed the source tag and
   a timestamp, and duplicate *content* across restarts is caught by the
   ``raw_payloads`` unique index.
2. **The DB index is best-effort, the file is the source of truth.** With no
   connection (or a DB outage) files still land on disk; the caller decides
   fail-closed behaviour for *spend*, not for archival.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import pathlib
from dataclasses import dataclass


@dataclass(frozen=True)
class ArchiveResult:
    path: pathlib.Path
    sha256: bytes
    byte_size: int
    indexed_new: bool        # False when raw_payloads already held this body
    wrote_file: bool         # False when an identical file already existed


class RawArchive:
    """Verbatim archiver rooted at a data dir, optionally indexing raw_payloads."""

    def __init__(self, root: pathlib.Path, conn=None) -> None:
        self.root = pathlib.Path(root)
        self._conn = conn

    def write(self, *, source: str, endpoint: str, body: bytes,
              request_key: str | None = None, http_status: int | None = None,
              credits_spent: int = 0, subdir: str = "", tag: str = "evt",
              suffix: str = ".json", when: _dt.datetime | None = None) -> ArchiveResult:
        """Archive one verbatim payload and index it.

        ``endpoint`` participates in the dedupe key so the same body arriving
        from two different endpoints is archived twice (they are different
        observations), while the same body from the same endpoint is one
        observation no matter how often a poll loop repeats it.
        """
        digest = hashlib.sha256(body).digest()
        when = when or _dt.datetime.now(_dt.timezone.utc)

        day = when.strftime("%Y-%m-%d")
        stamp = when.strftime("%H%M%S%f")
        base = self.root / subdir / day if subdir else self.root / day
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:80]

        # Content-addressed over (endpoint, body): the same observation has
        # the same name forever, so a restart replaying the overlap writes no
        # second file — the dedupe survives even without the DB index.
        key = hashlib.sha256(endpoint.encode("utf-8") + b"\0" + body).hexdigest()[:16]
        path = base / f"{safe_tag}_{key}{suffix}"
        wrote = self._write_exclusive(path, body)
        if wrote is None:  # pragma: no cover - sha-prefix collision
            path = base / f"{stamp}_{safe_tag}_{key}{suffix}"
            wrote = self._write_exclusive(path, body)
            if wrote is None:
                raise OSError(f"could not archive payload under {base}")

        indexed_new = self._index(source=source, endpoint=endpoint, digest=digest,
                                  path=path, size=len(body), request_key=request_key,
                                  http_status=http_status, credits_spent=credits_spent)
        return ArchiveResult(path=path, sha256=digest, byte_size=len(body),
                             indexed_new=indexed_new, wrote_file=wrote)

    def _write_exclusive(self, path: pathlib.Path, body: bytes) -> bool | None:
        """Create ``path`` atomically. True=wrote, False=identical file exists,
        None=name exists with different content."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = None
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, body)
            return True
        except FileExistsError:
            try:
                return False if path.read_bytes() == body else None
            except OSError:
                return None
        finally:
            if fd is not None:
                os.close(fd)

    def _index(self, *, source, endpoint, digest, path, size,
               request_key, http_status, credits_spent) -> bool:
        if self._conn is None:
            return True
        try:
            cur = self._conn.execute(
                "INSERT INTO raw_payloads "
                "(source, endpoint, request_key, http_status, credits_spent, "
                " body_sha256, storage_path, byte_size) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (source, endpoint, body_sha256) DO NOTHING "
                "RETURNING id",
                (source, endpoint, request_key, http_status, credits_spent,
                 digest, str(path), size),
            )
            row = cur.fetchone() if cur is not None else None
            self._conn.commit()
            return row is not None
        except Exception:
            self._conn.rollback()
            return False
