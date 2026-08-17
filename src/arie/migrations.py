"""Migration discovery — read-only, shared by the applier and the API.

``scripts/migrate.py`` (ADR 0005's "only path that touches production") owns
*applying* migrations and stays the sole writer of ``schema_migrations``. This
module owns naming and listing them, and reading (never writing) what's
already applied — logic ``arie.api.main``'s readiness check needs too, and
which must not fork from what the applier considers a migration or this
project ends up with two competing definitions of "the schema is up to date."

Living under ``src/arie`` rather than ``scripts/`` is deliberate: ``arie`` is
an installed package, importable regardless of working directory or how the
process was launched, which a runtime readiness check has to be able to rely
on. ``scripts/`` only resolves on ``sys.path`` when the process happened to be
started from the repo root — true for ``make``/pytest/the Docker image's
``WORKDIR``, but not a contract worth depending a health check's correctness
on.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def checksum_of(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migrations in application order.

    Lexical order on the numeric filename prefix (``0001_``, ``0002_``, ...) —
    the same ordering convention the migrations directory already uses.
    """
    return sorted(migrations_dir.glob("*.sql"))


def pending_migrations(
    conn: psycopg.Connection, *, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Migration filenames on disk with no matching row in ``schema_migrations``.

    Purely a read against an existing connection — never applies anything,
    unlike ``scripts.migrate.migrate()``. An empty result means the schema
    this process's ``migrations/`` directory describes is fully applied; a
    non-empty one means either a migration hasn't run yet (the exact
    clean-start race the Compose ``migrate`` service's
    ``service_completed_successfully`` gate exists to close) or
    ``schema_migrations`` itself doesn't exist yet (bootstrapped, but nothing
    applied).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
    except psycopg.errors.UndefinedTable:
        # The failed statement leaves the transaction aborted; roll back so
        # this connection is safe to hand back to a pool for reuse.
        conn.rollback()
        return [path.name for path in migration_files(migrations_dir)]
    return [path.name for path in migration_files(migrations_dir) if path.name not in applied]
