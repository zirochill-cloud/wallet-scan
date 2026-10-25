"""Database connection and schema migration.

Migrations are plain ``schema/NNN_name.sql`` files applied in filename order
inside a single transaction each, tracked in ``schema_migrations`` so a
re-run is a no-op. Applying the schema is deliberately a separate, explicit
step from running a job: a job that silently migrates on start is a job that
can surprise a production database.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

SCHEMA_DIR = pathlib.Path(__file__).resolve().parent.parent / "schema"

_MIGRATION_RE = re.compile(r"^(?P<order>\d+)[_-](?P<name>.+)\.sql$")


@dataclass(frozen=True)
class Migration:
    order: int
    name: str
    path: pathlib.Path


class MigrationError(RuntimeError):
    """A migration file is malformed or could not be applied."""


def discover_migrations(schema_dir: pathlib.Path | None = None) -> list[Migration]:
    """Return migrations sorted by numeric order, rejecting duplicates."""
    schema_dir = schema_dir or SCHEMA_DIR
    if not schema_dir.exists():
        raise MigrationError(f"schema directory not found: {schema_dir}")

    found: dict[int, Migration] = {}
    for path in sorted(schema_dir.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if not m:
            raise MigrationError(
                f"migration filename {path.name!r} must match NNN_name.sql"
            )
        order = int(m.group("order"))
        if order in found:
            raise MigrationError(
                f"duplicate migration order {order}: "
                f"{found[order].path.name} and {path.name}"
            )
        found[order] = Migration(order=order, name=m.group("name"), path=path)

    return [found[k] for k in sorted(found)]


def _ensure_tracking_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            order_key  INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()


def applied_orders(conn) -> set[int]:
    _ensure_tracking_table(conn)
    rows = conn.execute("SELECT order_key FROM schema_migrations").fetchall()
    return {int(r[0]) for r in rows}


def migrate(conn, schema_dir: pathlib.Path | None = None) -> list[Migration]:
    """Apply pending migrations. Returns the ones actually applied."""
    migrations = discover_migrations(schema_dir)
    already = applied_orders(conn)
    applied: list[Migration] = []

    for mig in migrations:
        if mig.order in already:
            continue
        sql = mig.path.read_text()
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (order_key, name) VALUES (%s, %s)",
                (mig.order, mig.name),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise MigrationError(
                f"migration {mig.path.name} failed: {exc}"
            ) from exc
        applied.append(mig)

    return applied


def connect(dsn: str | None = None):
    """Open a psycopg connection.

    ``dsn`` defaults to the ``SOLSCOPE_PGDSN`` environment variable. Raising a
    clear error here beats a confusing failure deep inside a job.
    """
    import os

    dsn = dsn or os.environ.get("SOLSCOPE_PGDSN")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        raise MigrationError(
            "psycopg is required for database access. "
            "Install with: pip install 'psycopg[binary]'"
        ) from exc

    try:
        return psycopg.connect(dsn) if dsn else psycopg.connect()
    except Exception as exc:
        raise MigrationError(
            f"could not connect to PostgreSQL (set SOLSCOPE_PGDSN): {exc}"
        ) from exc
