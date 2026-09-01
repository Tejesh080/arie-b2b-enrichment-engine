"""Apply SQL migrations against a live Postgres/Supabase database.

**Every invocation must name its database explicitly.** There is no default
target, no default mode, and no environment variable that can supply either.
``python scripts/migrate.py`` on its own is an error, not a production write::

    python scripts/migrate.py --target test --dry-run
    python scripts/migrate.py --target test --apply
    python scripts/migrate.py --target production --dry-run
    python scripts/migrate.py --target production --apply --confirm-production-write

This shape exists because of a real incident. The runner used to read
``DATABASE_DIRECT_URL`` unconditionally, so a session that had exported
``TEST_DATABASE_URL``/``TEST_DATABASE_DIRECT_URL`` — believing that aimed the
tooling at a scratch database, because that is precisely what it does for the
integration suite (see :class:`arie.config.IntegrationDatabaseConfig`) — ran
``python scripts/migrate.py`` and applied four unreleased migrations to
*production*. Nothing was mistyped and nothing was ignored; the command simply
had no way to express which database was meant, and silently picked the
dangerous one. The fix is not a warning. It is removing the ability to leave
the target unstated.

Two environment families, strictly separated, neither able to reach the other:

``--target test``
    ``TEST_DATABASE_DIRECT_URL``, else ``TEST_DATABASE_URL``. Never
    ``DATABASE_*``, so a populated ``.env`` on a developer machine cannot
    redirect a test run at the deployment. Additionally refuses to run if the
    resolved test database turns out to be the *same server and database* as
    ``DATABASE_URL``/``DATABASE_DIRECT_URL`` — the one way the variable split
    alone could still be pointed at production.

``--target production``
    ``DATABASE_DIRECT_URL``, else ``DATABASE_URL``. Never ``TEST_*``, so an
    exported test URL cannot silently become the production target — the exact
    inversion of the incident above.

The *direct* connection matters for production specifically: Supabase's pooled
connection is pgbouncer in transaction mode, which does not guarantee the
session-level semantics DDL wants, and migrations are exactly where that
distinction bites (see ``docs/architecture.md``). So a production ``--apply``
that could only resolve the pooled ``DATABASE_URL`` is refused; a production
``--dry-run`` over the pooler is fine, because it only reads. For an ordinary
Postgres — which every supported test target is — the two URLs are the same
server, so ``--target test`` draws no such distinction.

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
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from arie.config import DatabaseConfig, IntegrationDatabaseConfig
from arie.migrations import MIGRATIONS_DIR, MigrationsDirectoryError, checksum_of, migration_files

__all__ = [
    "MIGRATIONS_DIR",
    "TARGETS",
    "MigrationsDirectoryError",
    "ResolvedTarget",
    "TargetResolutionError",
    "checksum_of",
    "describe_target",
    "main",
    "migrate",
    "migration_files",
    "resolve_target",
]

TARGETS = ("test", "production")

_TARGET_VARIABLES: dict[str, tuple[str, str]] = {
    "test": ("TEST_DATABASE_DIRECT_URL", "TEST_DATABASE_URL"),
    "production": ("DATABASE_DIRECT_URL", "DATABASE_URL"),
}

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_RECORD_APPLIED_SQL = "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)"
_SELECT_ALL_APPLIED_SQL = "SELECT filename, checksum FROM schema_migrations"


class TargetResolutionError(RuntimeError):
    """The named target could not be resolved to a database this run may use.

    Distinct from the ``RuntimeError`` :func:`migrate` raises for a checksum
    mismatch: this one is decided *before* any connection is opened, and is
    the class of failure that must never degrade into "fall back to whatever
    URL is configured".
    """


@dataclass(frozen=True)
class ResolvedTarget:
    """Which database a run resolved to, and how it got there."""

    target: str
    conninfo: str
    variable: str
    """The environment variable the connection string actually came from —
    printed back to the operator so "which database is this" is answered by
    the run itself rather than by trusting the flag."""
    pooled_fallback: bool
    """True when only the pooled URL was set and the direct one was not."""


def _identity(conninfo: str) -> tuple[str, str, str] | None:
    """``(host, port, database)`` for `conninfo`, or ``None`` if unparseable.

    Deliberately allowlist-based rather than "parse and strip the password":
    only these three components are ever extracted, so no parsing quirk or
    unusual connection-string form can route a credential into output. libpq
    key/value strings (``host=... password=... dbname=...``) are handled too,
    because ``urlsplit`` on one of those yields the whole string as ``path``
    and would otherwise print the password verbatim.
    """
    if "://" in conninfo:
        parts = urlsplit(conninfo)
        host = (parts.hostname or "").lower()
        try:
            port = str(parts.port or 5432)
        except ValueError:  # malformed port — treat the whole string as opaque
            return None
        database = parts.path.lstrip("/").lower()
        if not host and not database:
            return None
        return (host, port, database)

    if "=" not in conninfo:
        return None
    fields: dict[str, str] = {}
    for token in conninfo.split():
        key, _, value = token.partition("=")
        if key in {"host", "port", "dbname"}:
            fields[key] = value
    if not fields:
        return None
    return (fields.get("host", "").lower(), fields.get("port", "5432"), fields.get("dbname", ""))


def _production_identities() -> set[tuple[str, str, str]]:
    config = DatabaseConfig()
    identities = set()
    for url in (config.direct_url, config.url):
        if url and (identity := _identity(url)) is not None:
            identities.add(identity)
    return identities


def resolve_target(target: str) -> ResolvedTarget:
    """Resolve `target` to a connection string from *its own* variable family.

    Reads the environment through freshly constructed config objects rather
    than the module-level singletons, so the resolution reflects the process's
    environment at call time — and so a test can prove the families cannot
    cross-contaminate by setting one and clearing the other.
    """
    if target not in _TARGET_VARIABLES:
        raise TargetResolutionError(f"unknown target: {target!r} (expected one of {TARGETS})")

    direct_var, pooled_var = _TARGET_VARIABLES[target]
    # os.getenv, not the config field, decides *which variable* was used:
    # IntegrationDatabaseConfig.direct_url already folds the pooled fallback
    # in, so the field alone cannot distinguish the two cases.
    direct_url = os.getenv(direct_var, "")
    pooled_url = IntegrationDatabaseConfig().url if target == "test" else DatabaseConfig().url

    if direct_url:
        resolved = ResolvedTarget(target, direct_url, direct_var, pooled_fallback=False)
    elif pooled_url:
        resolved = ResolvedTarget(target, pooled_url, pooled_var, pooled_fallback=True)
    else:
        raise TargetResolutionError(
            f"target {target!r} is not configured: set {direct_var} "
            f"(or {pooled_var}). This runner never falls back to the other "
            f"target's variables, so no other environment value can stand in."
        )

    if target == "test":
        identity = _identity(resolved.conninfo)
        if identity is not None and identity in _production_identities():
            host, port, database = identity
            raise TargetResolutionError(
                f"{resolved.variable} resolves to {host}:{port}/{database}, which is the "
                "same database as DATABASE_URL/DATABASE_DIRECT_URL. Refusing to run a "
                "'test' migration against production. Point the TEST_* variables at a "
                "separate database (see scripts/test_db.py)."
            )

    return resolved


def describe_target(resolved: ResolvedTarget) -> str:
    """A one-line, password-free description of where a run is pointed."""
    identity = _identity(resolved.conninfo)
    if identity is None:
        where = "(connection string not in a recognized form — identity not shown)"
    else:
        host, port, database = identity
        where = f"{host or '(unknown host)'}:{port}/{database or '(default database)'}"
    return f"Target: {resolved.target} -> {where}  [from {resolved.variable}]"


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
        description=(
            "Apply pending SQL migrations to an explicitly named ARIE database. "
            "There is no default target and no default mode: a bare invocation "
            "is an error, never a production write."
        ),
        epilog=(
            "examples:\n"
            "  scripts/migrate.py --target test --apply\n"
            "  scripts/migrate.py --target production --dry-run\n"
            "  scripts/migrate.py --target production --apply --confirm-production-write\n"
            "\n"
            "targets:\n"
            "  test        TEST_DATABASE_DIRECT_URL, else TEST_DATABASE_URL. Never DATABASE_*.\n"
            "  production  DATABASE_DIRECT_URL, else DATABASE_URL. Never TEST_*.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        choices=TARGETS,
        required=True,
        help=(
            "Which database to act on. Required — the connection string is "
            "resolved from this target's own environment variables only, and "
            "the other target's variables can never stand in for them."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report pending migrations and verify applied-migration checksums "
            "without writing anything — no schema changes, no schema_migrations "
            "rows, not even the bootstrap table on a fresh database."
        ),
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply pending migrations. Required to write; never implied.",
    )
    parser.add_argument(
        "--confirm-production-write",
        action="store_true",
        help=(
            "Acknowledge that '--target production --apply' writes to the live "
            "customer database. Required for that combination and ignored for "
            "every other one."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.target == "production" and args.apply and not args.confirm_production_write:
        print(
            "Refusing to apply migrations to production without an explicit "
            "acknowledgement.\n"
            "Re-run as:\n"
            "  python scripts/migrate.py --target production --apply "
            "--confirm-production-write\n"
            "To see what would run first (zero writes):\n"
            "  python scripts/migrate.py --target production --dry-run",
            file=sys.stderr,
        )
        return 1

    try:
        resolved = resolve_target(args.target)
    except TargetResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.apply and resolved.pooled_fallback and args.target == "production":
        # Reads over the transaction pooler are fine; DDL is not (see the
        # module docstring). Refuse rather than apply schema changes through
        # a connection whose session semantics are not guaranteed.
        print(
            f"{describe_target(resolved)}\n"
            "DATABASE_DIRECT_URL is not set, so this would apply schema changes over the "
            "pooled connection. Supabase's pooled URL is pgbouncer in transaction mode and "
            "does not guarantee the session semantics DDL needs. Set DATABASE_DIRECT_URL "
            "(the direct/session connection string) and re-run.",
            file=sys.stderr,
        )
        return 1

    # Printed before anything connects, for both modes: the operator sees which
    # database is about to be touched while it is still possible to stop.
    print(describe_target(resolved))
    print("Mode:   dry run (no writes)" if args.dry_run else "Mode:   apply")

    try:
        applied = migrate(resolved.conninfo, dry_run=args.dry_run)
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
