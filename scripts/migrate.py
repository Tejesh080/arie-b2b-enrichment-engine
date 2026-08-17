"""Apply SQL migrations against a live Postgres/Supabase database.

Entry point for ``make db-migrate``. Uses the *direct* connection
(``DATABASE_DIRECT_URL``), not the pooled one (``DATABASE_URL``): a transaction
pooler in transaction mode (pgbouncer, which is what Supabase's pooled
connection is) does not guarantee the session-level semantics DDL wants, and
migrations are exactly the place that distinction matters — see
``docs/06-m1-handoff.md``.

Every migration in this repo is already written to be idempotent
(``CREATE TABLE IF NOT EXISTS``, ``CREATE OR REPLACE VIEW``) — a deliberate
second line of defense. Applied-migration tracking on top of that is what
makes a re-run *report* "nothing to do" instead of silently re-executing
everything, and what catches a migration file being edited after it already
ran somewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from arie.config import DATABASE
from arie.migrations import MIGRATIONS_DIR, MigrationsDirectoryError, checksum_of, migration_files

__all__ = [
    "MIGRATIONS_DIR",
    "MigrationsDirectoryError",
    "checksum_of",
    "main",
    "migrate",
    "migration_files",
]

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_ALREADY_APPLIED_SQL = "SELECT checksum FROM schema_migrations WHERE filename = %s"
_RECORD_APPLIED_SQL = "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)"


def migrate(
    conninfo: str, *, migrations_dir: Path = MIGRATIONS_DIR, dry_run: bool = False
) -> list[str]:
    """Apply pending migrations in order. Returns the filenames applied.

    Each migration runs in its own transaction: the DDL and the
    ``schema_migrations`` bookkeeping row commit together or not at all, so a
    failed migration never gets recorded as applied.

    Raises if a migration that already ran has since changed on disk — that
    file's applied checksum no longer matches, which ordering alone cannot
    make safe to silently skip or silently reapply.
    """
    applied: list[str] = []

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(_BOOTSTRAP_SQL)
        conn.commit()

        for path in migration_files(migrations_dir):
            sql = path.read_text(encoding="utf-8")
            checksum = checksum_of(sql)

            with conn.cursor() as cur:
                cur.execute(_ALREADY_APPLIED_SQL, (path.name,))
                row = cur.fetchone()

            if row is not None:
                if row[0] != checksum:
                    raise RuntimeError(
                        f"{path.name} was already applied with a different checksum. "
                        "A migration must never be edited after it has run anywhere — "
                        "add a new migration instead."
                    )
                continue

            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(_RECORD_APPLIED_SQL, (path.name, checksum))
                conn.commit()

            applied.append(path.name)

    return applied


def main() -> int:
    if not DATABASE.direct_url:
        print(
            "DATABASE_DIRECT_URL is not set. Copy .env.example to .env and fill in "
            "the direct (session) connection string before running migrations.",
            file=sys.stderr,
        )
        return 1

    try:
        applied = migrate(DATABASE.direct_url)
    except MigrationsDirectoryError as exc:
        print(
            f"{exc} — refusing to report success for schema this process cannot see.",
            file=sys.stderr,
        )
        return 1

    if applied:
        print(f"Applied {len(applied)} migration(s):")
        for name in applied:
            print(f"  {name}")
    else:
        print("Nothing to apply — schema is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
