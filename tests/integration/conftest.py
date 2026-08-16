"""Fixtures for tests that need a live Postgres database.

These are opt-in: every test in this package is marked ``integration`` and is
excluded by ``make test`` (``pytest -m "not integration"``). They run under
``make test-all``, which requires ``DATABASE_URL`` and ``DATABASE_DIRECT_URL``
to be set — otherwise the session fixture below skips the whole package with a
clear reason instead of failing on a missing connection.

These tests apply migrations and write and delete rows against whatever
database the configured URLs point at. Point them at a disposable database or a
Supabase branch, not a database you'd mind resetting.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from scripts.migrate import migrate

from arie.config import DatabaseConfig
from arie.evidence.store import PostgresEvidenceStore
from arie.identity.resolver import IdentityResolver


def _database_config() -> DatabaseConfig:
    # Constructed fresh (not the module-level singleton) so a test run that
    # patches the environment before collection is still honoured.
    return DatabaseConfig()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Apply migrations once per session; return the pooled connection string.

    Skips every test in this package if the database isn't configured, rather
    than letting each test fail individually on a connection error.
    """
    db = _database_config()
    if not db.direct_url or not db.url:
        pytest.skip("DATABASE_URL / DATABASE_DIRECT_URL not set — skipping DB integration tests")
    migrate(db.direct_url)
    return db.url


@pytest.fixture
def evidence_store(migrated_database: str) -> Iterator[PostgresEvidenceStore]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=4, open=True)
    store = PostgresEvidenceStore(pool)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def db_conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    """A plain connection for setup/teardown the store's interface doesn't expose."""
    with psycopg.connect(migrated_database) as conn:
        yield conn


@pytest.fixture
def cleanup_evidence(db_conn: psycopg.Connection) -> Iterator[list[UUID]]:
    """Delete rows for any entity_id a test registers here, after the test runs.

    Each test uses a fresh ``uuid4()`` entity_id, so tests never collide with
    each other or with real data — this just keeps a test database from
    accumulating fixture rows across runs.
    """
    entity_ids: list[UUID] = []
    yield entity_ids
    if entity_ids:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM evidence WHERE entity_id = ANY(%s)", (entity_ids,))
        db_conn.commit()


@pytest.fixture
def identity_resolver(migrated_database: str) -> Iterator[IdentityResolver]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=4, open=True)
    resolver = IdentityResolver(pool)
    try:
        yield resolver
    finally:
        resolver.close()


@dataclass
class IdentityCleanup:
    """Rows a test resolved and wants deleted afterward.

    Identity resolution doesn't generate fresh UUIDs the way evidence tests do
    (``ON CONFLICT`` may return an *existing* row from an earlier test run) —
    tests register normalized keys, and teardown deletes by those keys so a
    company/person genuinely created by this run is removed regardless of
    which call happened to return its row.
    """

    domains: list[str] = field(default_factory=list)
    company_names: list[str] = field(default_factory=list)  # normalized, for domain-less companies
    emails: list[str] = field(default_factory=list)


@pytest.fixture
def cleanup_identity(db_conn: psycopg.Connection) -> Iterator[IdentityCleanup]:
    tracker = IdentityCleanup()
    yield tracker
    with db_conn.cursor() as cur:
        # Persons first: company_id references companies(company_id) with no
        # ON DELETE clause (default RESTRICT), so a company row with a
        # surviving person would fail to delete.
        if tracker.emails:
            cur.execute("DELETE FROM persons WHERE canonical_email = ANY(%s)", (tracker.emails,))
        if tracker.domains:
            cur.execute(
                "DELETE FROM companies WHERE canonical_domain = ANY(%s)", (tracker.domains,)
            )
        if tracker.company_names:
            cur.execute(
                "DELETE FROM companies WHERE canonical_domain IS NULL AND normalized_name = ANY(%s)",
                (tracker.company_names,),
            )
    db_conn.commit()
