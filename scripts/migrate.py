"""Apply SQL migrations against a live Postgres/Supabase database.

Entry point for ``make db-migrate``. Uses the *direct* connection
(``DATABASE_DIRECT_URL``), not the pooled one (``DATABASE_URL``): a transaction
pooler in transaction mode (pgbouncer, which is what Supabase's pooled
connection is) does not guarantee the session-level semantics DDL wants, and
migrations are exactly the place that distinction matters — see
``docs/architecture.md``.

Every migration in this repo is already written to be idempotent
(``CREATE TABLE IF NOT EXISTS``, ``CREATE OR REPLACE VIEW``) — a deliberate
second line of defense. Applied-migration tracking on top of that is what
makes a re-run *report* "nothing to do" instead of silently re-executing
everything, and what catches a migration file being edited after it already
ran somewhere.

``--dry-run`` is a *true* read-only mode (fixed after Productization M3's
rollout found ``main()`` never parsed ``sys.argv`` at all, so the flag was
silently ignored and a "dry run" applied migrations for real). It performs
zero schema/data writes: no bootstrap ``CREATE TABLE``, no migration DDL, no
``schema_migrations`` insert. It still validates every already-applied
migration's on-disk checksum, so a corrupted/edited migration is reported the
same way a real run would refuse to proceed.
"""

from __future__ import annotations

import argparse
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

_RECORD_APPLIED_SQL = "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)"
_SELECT_ALL_APPLIED_SQL = "SELECT filename, checksum FROM schema_migrations"


def _already_applied(conn: psycopg.Connection) -> dict[str, str]:
    """``filename -> checksum`` for every migration already recorded as applied.

    Read-only — tolerates ``schema_migrations`` not existing yet (nothing
    applied), the same way ``arie.migrations.pending_migrations`` does, and
    critically never creates the table itself. That's what makes this safe to
    call from a dry run: a fresh database stays untouched rather than picking
    up a bootstrap ``CREATE TABLE`` as a side effect of merely checking.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_ALL_APPLIED_SQL)
            return dict(cur.fetchall())
    except psycopg.errors.UndefinedTable:
        # The failed statement leaves the transaction aborted; roll back so
        # the connection stays usable (and so this read never leaves
        # anything for the final `conn.rollback()` below to undo).
        conn.rollback()
        return {}


def migrate(
    conninfo: str, *, migrations_dir: Path = MIGRATIONS_DIR, dry_run: bool = False
) -> list[str]:
    """Apply pending migrations in order. Returns the filenames applied — or,
    with ``dry_run=True``, the filenames that *would* be applied, with zero
    writes actually performed (see the module docstring).

    Each migration runs in its own transaction: the DDL and the
    ``schema_migrations`` bookkeeping row commit together or not at all, so a
    failed migration never gets recorded as applied.

    Raises if a migration that already ran has since changed on disk — that
    file's applied checksum no longer matches, which ordering alone cannot
    make safe to silently skip or silently reapply. This check runs
    identically whether or not ``dry_run`` is set: verifying checksums is
    itself read-only, so a dry run catches a corrupted migration exactly like
    a real run would.
    """
    applied: list[str] = []

    with psycopg.connect(conninfo) as conn:
        already = _already_applied(conn)

        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(_BOOTSTRAP_SQL)
            conn.commit()

        for path in migration_files(migrations_dir):
            sql = path.read_text(encoding="utf-8")
            checksum = checksum_of(sql)

            existing_checksum = already.get(path.name)
            if existing_checksum is not None:
                if existing_checksum != checksum:
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

        if dry_run:
            # Paranoia, not load-bearing: nothing above executed a write in
            # dry-run mode, but an explicit rollback means this function's
            # "zero writes" contract holds even if a future edit adds one by
            # mistake — the connection's own exit-commit never gets the
            # chance to persist it.
            conn.rollback()

    return applied


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/migrate.py",
        description="Apply pending SQL migrations to the ARIE database.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report pending migrations and verify applied-migration checksums "
            "without writing anything — no schema changes, no schema_migrations "
            "rows, not even the bootstrap table on a fresh database."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not DATABASE.direct_url:
        print(
            "DATABASE_DIRECT_URL is not set. Copy .env.example to .env and fill in "
            "the direct (session) connection string before running migrations.",
            file=sys.stderr,
        )
        return 1

    try:
        applied = migrate(DATABASE.direct_url, dry_run=args.dry_run)
    except MigrationsDirectoryError as exc:
        print(
            f"{exc} — refusing to report success for schema this process cannot see.",
            file=sys.stderr,
        )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    verb = "Would apply" if args.dry_run else "Applied"
    if applied:
        print(f"{verb} {len(applied)} migration(s):")
        for name in applied:
            print(f"  {name}")
    else:
        print("Nothing to apply — schema is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
