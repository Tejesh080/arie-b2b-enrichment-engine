"""Designate a database as safe for the integration suite to destroy.

**Why a designation step exists at all.** The integration suite writes and
deletes rows, and for a while it did that against the deployed Supabase
database, because its fixtures read ``DATABASE_URL``. Pointing them at
``TEST_DATABASE_URL`` instead fixes the common case, but a variable is still
just a string: nothing stops a copy-paste, an inherited shell export, or a CI
secret wired to the wrong value from aiming that variable at production too.

So the suite does not trust the URL alone. It requires the database at the far
end to carry a **marker table that only this script creates** — and this script
refuses to create it anywhere that looks like production. The check is therefore
a property of the database, established once, deliberately, by a human running a
command whose name says what it does. An environment variable cannot forge it.

    python scripts/test_db.py designate     # stamp the marker (guarded)
    python scripts/test_db.py status        # what is configured, what is marked

**Local setup, start to finish** (the compose stack's own Postgres, a
*different database* on it so the compose workers can never see test jobs):

    docker compose up -d db
    export TEST_DATABASE_URL=postgresql://arie:arie_local_dev@localhost:5432/arie_test
    export ARIE_ALLOW_INTEGRATION_TEST_DB=1
    python scripts/test_db.py designate
    make test-all

``designate`` creates the database if it does not exist, so there is no separate
``createdb`` step to forget.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import sys
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from arie.config import DatabaseConfig, IntegrationDatabaseConfig

MARKER_TABLE = "arie_integration_test_marker"
"""Deliberately not created by any migration.

Putting it in ``migrations/`` would stamp every database the migration runner
touches — including production — which is precisely the opposite of what the
marker is for. It has to be the one schema object the normal deploy path never
creates.
"""

_CREATE_MARKER = sql.SQL(
    """
    CREATE TABLE IF NOT EXISTS {table} (
        designated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        designated_by  TEXT NOT NULL,
        host           TEXT NOT NULL,
        note           TEXT NOT NULL
    )
    """
).format(table=sql.Identifier(MARKER_TABLE))

_MARKER_NOTE = (
    "Integration tests may create and delete rows in this database at will. "
    "Never point DATABASE_URL at it."
)


class IntegrationDatabaseGuardError(RuntimeError):
    """Refusal to designate or use a database as a test target."""


def _identity(conninfo: str) -> tuple[str, str, str]:
    """(host, port, dbname) — what makes two URLs the *same database*.

    Compared instead of the raw strings because the same database is reachable
    through several spellings: different credentials, a pooled vs. direct port,
    a trailing query string. Only these three decide whether two connections
    land on the same rows.
    """
    parts = urlsplit(conninfo)
    return (
        (parts.hostname or "").lower(),
        str(parts.port or 5432),
        parts.path.lstrip("/").lower(),
    )


def assert_not_production(test_url: str, production_url: str | None = None) -> None:
    """Refuse a test URL that resolves to the configured production database.

    Only catches the case where both are visible in one environment — which is
    the common developer machine, where ``.env`` supplies ``DATABASE_URL``. It
    is a cheap check that catches an obvious mistake, not the guarantee; the
    marker is the guarantee.
    """
    resolved = DatabaseConfig().url if production_url is None else production_url
    if not resolved:
        return
    if _identity(test_url) == _identity(resolved):
        host, port, name = _identity(test_url)
        raise IntegrationDatabaseGuardError(
            f"TEST_DATABASE_URL resolves to the same database as DATABASE_URL "
            f"({host}:{port}/{name}). The integration suite deletes rows; it must "
            "never share a database with a deployment. Create a separate database "
            "(see this module's docstring) and point TEST_DATABASE_URL at that."
        )


def marker_present(conninfo: str) -> bool:
    """Whether this database has been designated. Never creates anything."""
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (MARKER_TABLE,))
        row = cur.fetchone()
        if row is None or not row[0]:
            return False
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(MARKER_TABLE)))
        count_row = cur.fetchone()
    return bool(count_row and count_row[0])


def _looks_populated(conninfo: str) -> str | None:
    """A one-line description of production-shaped data, or None if clean.

    Checked before designating so the marker cannot be stamped onto a database
    that is already somebody's real one. ``leads`` is the table to look at: it
    is the only one that accumulates rows a person would miss, and its
    ``source`` column records who created each row.
    """
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('leads') IS NOT NULL")
        row = cur.fetchone()
        if row is None or not row[0]:
            return None  # no schema yet — an empty database is the ideal target
        cur.execute("SELECT count(*) FROM leads")
        total_row = cur.fetchone()
        total = int(total_row[0]) if total_row else 0
        if not total:
            return None
        cur.execute("SELECT source, count(*) FROM leads GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
        by_source = ", ".join(f"{name}={count}" for name, count in cur.fetchall())
    return f"{total} existing lead(s) — {by_source}"


def _ensure_database(conninfo: str) -> bool:
    """Create the target database if absent. Returns True if it was created.

    Connects to the server's ``postgres`` maintenance database to do it, which
    is the only way to issue ``CREATE DATABASE``. A missing target is the normal
    first-run state, not an error worth making someone resolve by hand.
    """
    _host, _port, name = _identity(conninfo)
    try:
        with psycopg.connect(conninfo, connect_timeout=10):
            return False
    except psycopg.OperationalError as exc:
        if "does not exist" not in str(exc):
            raise

    parts = urlsplit(conninfo)
    maintenance = conninfo.replace(f"/{parts.path.lstrip('/')}", "/postgres", 1)
    with (
        psycopg.connect(maintenance, autocommit=True, connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return True


def designate(conninfo: str, *, production_url: str | None = None) -> None:
    """Mark a database as a disposable integration-test target. Guarded."""
    assert_not_production(conninfo, production_url)

    if _ensure_database(conninfo):
        print(f"created database {_identity(conninfo)[2]!r}")

    populated = _looks_populated(conninfo)
    if populated is not None and not marker_present(conninfo):
        raise IntegrationDatabaseGuardError(
            f"refusing to designate a database that already holds data: {populated}. "
            "The integration suite deletes rows. Point TEST_DATABASE_URL at an empty "
            "database, or drop this one first if it is genuinely disposable."
        )

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_MARKER)
            cur.execute(
                sql.SQL("INSERT INTO {} (designated_by, host, note) VALUES (%s, %s, %s)").format(
                    sql.Identifier(MARKER_TABLE)
                ),
                (getpass.getuser(), socket.gethostname(), _MARKER_NOTE),
            )
        conn.commit()

    host, port, name = _identity(conninfo)
    print(f"designated {host}:{port}/{name} as an integration-test database")


def _status(test_config: IntegrationDatabaseConfig) -> int:
    production = DatabaseConfig().url
    print(f"DATABASE_URL configured:              {'yes' if production else 'no'}")
    print(f"TEST_DATABASE_URL configured:         {'yes' if test_config.url else 'no'}")
    print(f"ARIE_ALLOW_INTEGRATION_TEST_DB=1:     {'yes' if test_config.allow else 'no'}")

    if not test_config.url:
        print("\nintegration tests will SKIP — no test database configured")
        return 0

    host, port, name = _identity(test_config.url)
    print(f"test database:                        {host}:{port}/{name}")
    try:
        assert_not_production(test_config.url)
    except IntegrationDatabaseGuardError as exc:
        print(f"\nREFUSED: {exc}")
        return 1

    try:
        marked = marker_present(test_config.direct_url)
    except psycopg.OperationalError as exc:
        print(f"\nunreachable: {type(exc).__name__}")
        return 1

    print(f"designated (marker present):          {'yes' if marked else 'no'}")
    if not marked:
        print("\nrun `python scripts/test_db.py designate` before `make test-all`")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["designate", "status"])
    args = parser.parse_args(argv)

    config = IntegrationDatabaseConfig()

    if args.command == "status":
        return _status(config)

    if not config.url:
        print(
            "TEST_DATABASE_URL is not set. Integration tests never fall back to "
            "DATABASE_URL — see this script's docstring for local setup.",
            file=sys.stderr,
        )
        return 1

    try:
        designate(config.direct_url)
    except IntegrationDatabaseGuardError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
