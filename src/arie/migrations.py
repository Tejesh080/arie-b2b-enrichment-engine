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


class MigrationsDirectoryError(RuntimeError):
    """Raised when the migrations directory can't be trusted to be the real one.

    ``Path.glob()`` on a directory that doesn't exist returns ``[]``, not an
    error — indistinguishable, to a naive caller, from a real directory that
    genuinely has nothing pending. That silently made ``pending_migrations``
    report "fully migrated" for a ``MIGRATIONS_DIR`` that resolved to the
    wrong place (e.g. after switching the Dockerfile to a non-editable
    install, where the ``parents[2]`` arithmetic this module's own docstring
    depends on no longer lands in the repo root) — and made ``/healthz``
    report ``ok`` on a database with no schema at all. Failing loudly here
    means a caller must decide what "I can't tell" means for its own
    contract, rather than this module silently deciding it means "yes."
    """


def checksum_of(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migrations in application order.

    Lexical order on the numeric filename prefix (``0001_``, ``0002_``, ...) —
    the same ordering convention the migrations directory already uses.

    Raises ``MigrationsDirectoryError`` if `migrations_dir` doesn't exist or
    contains no ``*.sql`` files — this repo always ships at least
    ``0001_init.sql``, so zero files found means the path is wrong, not that
    there is genuinely nothing to migrate. See the exception's own docstring
    for why this matters more than it looks: this function backs both the
    applier (``scripts/migrate.py``) and the read-only readiness check
    (``pending_migrations`` below), so a silent empty result here was a
    fail-open bug in both.
    """
    if not migrations_dir.is_dir():
        raise MigrationsDirectoryError(f"migrations directory not found: {migrations_dir}")
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        raise MigrationsDirectoryError(f"no *.sql migration files found in {migrations_dir}")
    return files


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

    Propagates ``MigrationsDirectoryError`` from ``migration_files`` rather
    than swallowing it — a caller (``/healthz``) that can't tell what's
    pending must not report ready.
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
