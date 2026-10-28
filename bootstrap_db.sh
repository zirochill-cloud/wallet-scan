#!/usr/bin/env bash
#
# One-command database test harness — the supported way to run the two DB
# suites (tests/test_schema_smoke.py, tests/test_ledger_live.py).
#
#     bash bootstrap_db.sh
#
# Steps, all idempotent (re-running reuses whatever already exists):
#   1. stage portable PostgreSQL 16.6 at a SHORT path (copy .pgtools/pgsql,
#      or extract bin/lib/share from .pgtools/pg.zip if that copy is gone)
#   2. create the virtualenv at a SHORT path and install requirements-dev.txt
#   3. initdb a cluster once under the same short root
#   4. start the server on 127.0.0.1:5433
#   5. run `solscope migrate` against its own scratch database, assert >= 40 tables
#   6. run both DB suites, each of which must reach its final PASS marker
#   7. assert the admin database still has 0 public tables
# and stop the server on the way out, pass or fail.
#
# WHY EVERYTHING SITS UNDER A SHORT ROOT
# This project's path is 164 characters. Under it, psycopg cannot unpack its
# DLL, and PostgreSQL server processes die with 0xC0000142 (DLL initialisation
# failed) — four incidents of that class on 2026-10-26. Everything this script
# owns therefore lives under $SOLSCOPE_DBTEST_HOME (~60 characters):
#     C:\Users\<you>\AppData\Local\Temp\solscope-dbtest
#
# SKIP IS NOT A PASS: both suites exit 0 when psycopg is missing, so step 6
# greps for each suite's final PASS marker and fails if it never appeared.
#
# Needs: python on PATH, one network round-trip for pip on first run, and
# .pgtools/pgsql or .pgtools/pg.zip (gitignored, 516 MB — download URL in
# requirements-dev.txt).
#
# Exit status 0 means: migrations applied AND both suites passed AND the admin
# database untouched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SHORT="${SOLSCOPE_DBTEST_HOME:-/tmp/solscope-dbtest}"
PG_BIN="$SHORT/pgsql"
PGDATA="$SHORT/pgdata"
VENV="$SHORT/pvenv"
LOG="$SHORT/postgres.log"
PORT="${SOLSCOPE_DBTEST_PORT:-5433}"
TEST_DSN="host=127.0.0.1 port=$PORT user=postgres dbname=postgres"

step() { printf '\n=== %s ===\n' "$*"; }
die()  { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

pgctl() { timeout 90 "$PG_BIN/bin/pg_ctl.exe" -D "$PGDATA" "$@"; }

psqlq() {  # psqlq <database> <sql>  -> prints the scalar result
  timeout 60 "$PG_BIN/bin/psql.exe" -h 127.0.0.1 -p "$PORT" -U postgres -d "$1" -tAc "$2"
}

cleanup() {
  if [ -f "$PGDATA/PG_VERSION" ] && "$PG_BIN/bin/pg_ctl.exe" -D "$PGDATA" status >/dev/null 2>&1; then
    printf '\n=== stopping PostgreSQL ===\n'
    timeout 60 "$PG_BIN/bin/pg_ctl.exe" -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

mkdir -p "$SHORT"

# --- 1. binaries ----------------------------------------------------------
step "1/7 portable PostgreSQL at $PG_BIN"
if [ ! -x "$PG_BIN/bin/postgres.exe" ]; then
  rm -rf "$PG_BIN"
  if [ -x "$ROOT/.pgtools/pgsql/bin/postgres.exe" ]; then
    mkdir -p "$PG_BIN"
    cp -r "$ROOT/.pgtools/pgsql/bin" "$ROOT/.pgtools/pgsql/lib" "$ROOT/.pgtools/pgsql/share" "$PG_BIN/"
    echo "copied bin/ lib/ share/ from .pgtools/pgsql (pgAdmin's deep tree skipped)"
  elif [ -f "$ROOT/.pgtools/pg.zip" ]; then
    echo "extracting bin/ lib/ share/ from .pgtools/pg.zip"
    python - "$ROOT/.pgtools/pg.zip" "$PG_BIN" <<'PYEOF'
import pathlib, sys, zipfile

src, dst = sys.argv[1], pathlib.Path(sys.argv[2])
keep, n = ("pgsql/bin/", "pgsql/lib/", "pgsql/share/"), 0
with zipfile.ZipFile(src) as z:
    for info in z.infolist():
        if info.filename.startswith(keep) and not info.is_dir():
            target = dst / info.filename[len("pgsql/"):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
            n += 1
print(f"extracted {n} files")
PYEOF
  else
    die "no portable PostgreSQL found — need .pgtools/pgsql or .pgtools/pg.zip (see requirements-dev.txt for the download URL)"
  fi
fi
"$PG_BIN/bin/postgres.exe" --version || die "binaries do not run from $PG_BIN"

# --- 2. virtualenv --------------------------------------------------------
step "2/7 virtualenv at $VENV"
if [ ! -x "$VENV/Scripts/python.exe" ]; then
  python -m venv "$VENV" || die "venv creation failed"
fi
timeout 600 "$VENV/Scripts/python.exe" -m pip install --disable-pip-version-check \
  -r requirements-dev.txt 2>&1 | tail -15 || die "pip install failed"
"$VENV/Scripts/python.exe" -c "import psycopg, yaml; print('psycopg', psycopg.__version__, '+ pyyaml import cleanly')" \
  || die "installed packages do not import"

# --- 3. cluster -----------------------------------------------------------
step "3/7 cluster at $PGDATA"
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  rm -rf "$PGDATA"
  timeout 180 "$PG_BIN/bin/initdb.exe" -D "$PGDATA" -U postgres -A trust -E UTF8 --locale=C \
    > "$SHORT/initdb.log" 2>&1 || { tail -8 "$SHORT/initdb.log"; die "initdb failed"; }
  echo "cluster initialised (trust auth, superuser postgres)"
else
  echo "reusing existing cluster"
fi

# --- 4. start -------------------------------------------------------------
step "4/7 PostgreSQL on 127.0.0.1:$PORT"
if "$PG_BIN/bin/pg_ctl.exe" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "stopping a server left running by an earlier run"
  pgctl -m fast stop >/dev/null 2>&1 || true
fi
# -k needs a WINDOWS path: postgres.exe gets the whole -o string verbatim
# (MSYS only converts standalone path arguments), so a POSIX /tmp/... would
# resolve against the drive root and fail with "could not create lock file".
command -v cygpath >/dev/null 2>&1 || die "cygpath not found — run this under Git Bash"
SHORT_WIN=$(cygpath -w "$SHORT")
pgctl -o "-p $PORT -k $SHORT_WIN -h 127.0.0.1" -l "$LOG" -w start >/dev/null 2>&1 \
  || { tail -10 "$LOG" 2>/dev/null || true; die "pg_ctl start failed"; }
[ "$(psqlq postgres 'select 1')" = "1" ] || die "server is not answering queries"
echo "ready (log: $LOG)"

# --- 5. migrations --------------------------------------------------------
step "5/7 migrations via 'solscope migrate'"
psqlq postgres "DROP DATABASE IF EXISTS solscope_bootstrap" >/dev/null
psqlq postgres "CREATE DATABASE solscope_bootstrap" >/dev/null
SOLSCOPE_PGDSN="host=127.0.0.1 port=$PORT user=postgres dbname=solscope_bootstrap" \
  timeout 120 "$VENV/Scripts/python.exe" -m solscope migrate || die "'solscope migrate' failed"
n=$(psqlq solscope_bootstrap "select count(*) from information_schema.tables where table_schema='public'")
case "$n" in ''|*[!0-9]*) die "could not count tables in solscope_bootstrap (got: $n)";; esac
[ "$n" -ge 40 ] || die "migrate produced $n tables, expected >= 40"
echo "solscope_bootstrap: $n tables"

# --- 6. the two DB suites -------------------------------------------------
step "6/7 DB suites"
export SOLSCOPE_TEST_PGDSN="$TEST_DSN"

run_suite() {  # run_suite <file> <required final marker>
  local out
  out=$(timeout 300 "$VENV/Scripts/python.exe" "$1" 2>&1) || { printf '%s\n' "$out"; die "$1 exited non-zero"; }
  printf '%s\n' "$out"
  grep -q "$2" <<<"$out" || die "$1 never reached its PASS marker — SKIP is not a pass"
}

run_suite tests/test_schema_smoke.py "ALL SCHEMA CHECKS PASSED"
run_suite tests/test_ledger_live.py  "ALL LIVE LEDGER CHECKS PASSED"

# --- 7. invariant ---------------------------------------------------------
step "7/7 admin database untouched"
n=$(psqlq postgres "select count(*) from information_schema.tables where table_schema='public'")
[ "$n" = "0" ] || die "expected 0 public tables in 'postgres', got $n — a suite leaked into the admin database"
echo "postgres: 0 public tables — both suites used their own scratch databases"

printf '\nDB HARNESS GREEN: migrations + schema smoke + live ledger, all real.\n'
