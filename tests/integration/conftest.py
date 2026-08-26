"""Fixtures for tests that need a live Postgres database.

These are opt-in: every test in this package is marked ``integration`` and is
excluded by ``make test`` (``pytest -m "not integration"``). They run under
``make test-all``.

**They read ``TEST_DATABASE_URL``, never ``DATABASE_URL``, and there is no
fallback between them.**

That is not a stylistic preference. These fixtures used to read
``DatabaseConfig`` — which on any developer machine with a populated ``.env``
is the *deployed* database — and two things followed. The tests created and
deleted rows in a live database; and the deployed worker, polling that same
database, claimed the tests' freshly-ingested jobs within about a second and
processed them with its own handlers, so assertions failed for reasons entirely
unrelated to the code under test. Neither is fixable by remembering to override
a variable, or by pausing the deployment before every run. The fix has to be
that the production URL is not reachable from here at all.

Three independent guards, in the order they fire:

1. **A different variable.** No ``TEST_DATABASE_URL`` means the suite skips.
   Nothing routes back to ``DATABASE_URL``.
2. **Explicit intent.** ``ARIE_ALLOW_INTEGRATION_TEST_DB=1`` must also be set.
   A URL can be inherited from a shell or a CI secret; this flag is meaningless
   outside this suite, so setting it is a decision.
3. **A property of the database itself.** The target must carry the marker
   table that only ``scripts/test_db.py designate`` creates — and that command
   refuses to stamp anything matching ``DATABASE_URL`` or holding existing
   data. An environment variable cannot forge a table.

Guards 1 and 2 *skip* (a missing opt-in is not a failure). A configuration that
is present but dangerous or incomplete — the URL resolving to production, or an
undesignated database — **fails loudly**: silently skipping a misconfiguration
would let a run go green while testing nothing.

Local setup is in ``scripts/test_db.py``'s docstring. The short version: point
``TEST_DATABASE_URL`` at a *different database on the same local Postgres* than
the Compose stack uses (``arie_test`` vs. ``arie``), so the Compose workers
cannot see test jobs even while running.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from scripts.migrate import migrate
from scripts.test_db import IntegrationDatabaseGuardError, assert_not_production, marker_present

from arie.api.main import AppState, build_state, create_app
from arie.config import IntegrationDatabaseConfig
from arie.evidence.store import PostgresEvidenceStore
from arie.identity.resolver import IdentityResolver
from arie.jobs.queue import PostgresJobQueue
from arie.ledger.store import PostgresCostLedger

_SETUP_HINT = (
    "See scripts/test_db.py for setup. In short:\n"
    "  docker compose up -d db\n"
    "  export TEST_DATABASE_URL=postgresql://arie:arie_local_dev@localhost:5432/arie_test\n"
    "  export ARIE_ALLOW_INTEGRATION_TEST_DB=1\n"
    "  python scripts/test_db.py designate"
)


def _test_database_config() -> IntegrationDatabaseConfig:
    # Constructed fresh (not a module-level singleton) so a test run that
    # patches the environment before collection is still honoured.
    return IntegrationDatabaseConfig()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Apply migrations once per session; return the pooled connection string.

    Every guard is checked here, once, before any test runs — a per-test check
    would report the same misconfiguration dozens of times, and a check after
    the first write would be too late to matter.
    """
    db = _test_database_config()

    if not db.configured:
        pytest.skip(
            "TEST_DATABASE_URL is not set, so there is no database these tests are "
            "allowed to write to. They deliberately do not fall back to DATABASE_URL.\n"
            + _SETUP_HINT
        )
    if not db.allow:
        pytest.skip(
            "ARIE_ALLOW_INTEGRATION_TEST_DB=1 is not set. These tests create and "
            "delete rows; running them is an explicit choice, not a default.\n" + _SETUP_HINT
        )

    # Dangerous or incomplete from here down: fail, never skip.
    try:
        assert_not_production(db.url)
    except IntegrationDatabaseGuardError as exc:
        pytest.fail(str(exc), pytrace=False)

    if not marker_present(db.direct_url):
        pytest.fail(
            "the configured TEST_DATABASE_URL points at a database that has not been "
            "designated for integration testing, so this suite will not write to it.\n"
            + _SETUP_HINT,
            pytrace=False,
        )

    migrate(db.direct_url)
    return db.url


@pytest.fixture(scope="session")
def migrated_database_direct(migrated_database: str) -> str:
    """The *direct* (non-pooled) connection string for the same test database.

    Exists so a test that needs to re-run the migration runner asks a fixture
    instead of building its own config. Two tests in
    ``test_migrate_integration.py`` used to call ``DatabaseConfig()`` directly,
    which meant they ran migrations against **production** while every other
    fixture in the session used the test database — invisible for as long as
    both happened to be the same database, and a silent production write the
    moment they stopped being.

    Depends on ``migrated_database`` so every guard has already run.
    """
    return _test_database_config().direct_url


RUN_ID = f"it-{uuid.uuid4().hex[:8]}"
"""A short id unique to this pytest session, e.g. ``it-3f9a2c7e``.

Every row a test creates is tagged with it, which buys two things. Rows are
attributable — a stray row names the run that made it — and, more usefully,
concurrent or interrupted runs cannot collide: two sessions against the same
database generate different ids, so neither can be confused by the other's data.
Cleanup still deletes by primary key, never by this prefix; the id is for
attribution, not for a broad ``DELETE ... LIKE 'it-%'``.

A module constant rather than only a fixture because the ``source`` values are
built inside plain module-level helper functions, which a fixture cannot reach
without threading a parameter through every helper and caller in seven files.
conftest is imported once per session, so this is evaluated once per session —
the same guarantee, spelled in a way the helpers can actually use. The
:func:`integration_run_id` fixture returns this same value.
"""


def source_for(label: str) -> str:
    """``leads.source`` for this run: ``it-3f9a2c7e:pipeline``.

    Replaces the fixed literals (``pipeline-it``, ``shadow-it``, ``receipt-it``,
    ``webhook``) the suite used to hard-code. Those were shared by every run
    that ever executed, so a run that died before teardown left rows behind
    that the next run's foreign-key teardown then tripped over — which is
    exactly what happened, repeatedly.
    """
    return f"{RUN_ID}:{label}"


@pytest.fixture(scope="session")
def integration_run_id() -> str:
    """:data:`RUN_ID`, for tests that would rather ask for it than import it."""
    return RUN_ID


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
        # `NOT EXISTS` rather than a plain delete: identity rows are shared by
        # construction — `ON CONFLICT` returns an *existing* company/person
        # when two leads resolve to the same identity — so a row this test
        # touched may still be referenced by a lead another test (or another
        # run) owns. Deleting it would raise a foreign-key violation and abort
        # the whole teardown, stranding everything after it. Skipping it is
        # correct: whoever still references it will clean it up.
        if tracker.emails:
            cur.execute(
                "DELETE FROM persons p WHERE p.canonical_email = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.person_id = p.person_id)",
                (tracker.emails,),
            )
        if tracker.domains:
            cur.execute(
                "DELETE FROM companies c WHERE c.canonical_domain = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.company_id = c.company_id) "
                "AND NOT EXISTS (SELECT 1 FROM persons p WHERE p.company_id = c.company_id)",
                (tracker.domains,),
            )
        if tracker.company_names:
            cur.execute(
                "DELETE FROM companies c WHERE c.canonical_domain IS NULL "
                "AND c.normalized_name = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.company_id = c.company_id) "
                "AND NOT EXISTS (SELECT 1 FROM persons p WHERE p.company_id = c.company_id)",
                (tracker.company_names,),
            )
    db_conn.commit()


@pytest.fixture
def job_queue(migrated_database: str) -> Iterator[PostgresJobQueue]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=8, open=True)
    queue = PostgresJobQueue(pool)
    try:
        yield queue
    finally:
        queue.close()


@pytest.fixture
def worker_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    """A standalone pool for arie.jobs.worker's `pool` argument.

    Separate from `job_queue`'s internal pool on purpose — in real use the
    worker's transaction pool and the queue's claim/complete/fail pool are the
    same physical database but there's no requirement they share a Python
    pool object, and keeping them distinct here catches any accidental
    coupling between the two.
    """
    pool = ConnectionPool(migrated_database, min_size=1, max_size=8, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def cleanup_leads(db_conn: psycopg.Connection) -> Iterator[list[UUID]]:
    """Deletes leads (and, via ON DELETE CASCADE, their jobs and lead_events)."""
    lead_ids: list[UUID] = []
    yield lead_ids
    if lead_ids:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (lead_ids,))
        db_conn.commit()


@pytest.fixture
def cleanup_jobs(db_conn: psycopg.Connection) -> Iterator[list[UUID]]:
    """For jobs with no lead_id — lead-linked jobs are cleaned up via cleanup_leads' cascade."""
    job_ids: list[UUID] = []
    yield job_ids
    if job_ids:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE job_id = ANY(%s)", (job_ids,))
        db_conn.commit()


# ---------------------------------------------------------------- step 9 --
#
# `span_exporter` / `spans` are in tests/conftest.py, not here — see its
# docstring for why they have to be shared with the unit tests.


@pytest.fixture
def app_state(migrated_database: str) -> Iterator[AppState]:
    state = build_state(migrated_database, min_size=1, max_size=6)
    try:
        yield state
    finally:
        state.pool.close()


@pytest.fixture
def api_client(app_state: AppState) -> Iterator[TestClient]:
    """A client over an app wired to the test pool, with the lifespan bypassed.

    Passing `state` to ``create_app`` skips the real lifespan entirely, so the
    test owns the pool's lifetime rather than racing startup and shutdown
    against its own fixtures.

    ``raise_server_exceptions=False`` so a handler that raises produces a 500
    response to assert on, which is what the rollback tests need — the default
    re-raises into the test and the response is never formed.
    """
    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def cost_ledger(migrated_database: str) -> Iterator[PostgresCostLedger]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=4, open=True)
    ledger = PostgresCostLedger(pool)
    try:
        yield ledger
    finally:
        ledger.close()


@dataclass
class IngestCleanup:
    """Everything one ingestion/ledger test created, deleted in FK-safe order.

    A single fixture rather than composing ``cleanup_leads`` with
    ``cleanup_identity``: pytest tears fixtures down in reverse instantiation
    order, so which of those two ran first would depend on the order a test
    happened to request them — and getting it backwards fails on the FK from
    persons to companies only sometimes. One teardown, one explicit order.
    """

    lead_ids: list[UUID] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    company_names: list[str] = field(default_factory=list)
    provider_call_keys: list[str] = field(default_factory=list)
    model_call_keys: list[str] = field(default_factory=list)


@pytest.fixture
def cleanup_ingest(db_conn: psycopg.Connection) -> Iterator[IngestCleanup]:
    tracker = IngestCleanup()
    yield tracker
    with db_conn.cursor() as cur:
        # provider_calls/model_calls reference leads ON DELETE SET NULL, so
        # deleting the lead orphans rather than removes them — they have to go
        # first and by their own key, or a test database accumulates ledger rows
        # that every metrics-view assertion then has to work around.
        if tracker.provider_call_keys:
            cur.execute(
                "DELETE FROM provider_calls WHERE idempotency_key = ANY(%s)",
                (tracker.provider_call_keys,),
            )
        if tracker.model_call_keys:
            cur.execute(
                "DELETE FROM model_calls WHERE idempotency_key = ANY(%s)",
                (tracker.model_call_keys,),
            )
        if tracker.lead_ids:
            # Cascades to jobs, lead_events, scores, voi_decisions, human_reviews.
            cur.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (tracker.lead_ids,))
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


@pytest.fixture
def make_lead(
    db_conn: psycopg.Connection, cleanup_leads: list[UUID]
) -> Callable[[], tuple[UUID, int]]:
    """Factory for a minimal lead row. Returns (lead_id, version); registers for cleanup."""

    def _make(*, source: str = "test") -> tuple[UUID, int]:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (source) VALUES (%s) RETURNING lead_id, version", (source,)
            )
            row = cur.fetchone()
            assert row is not None
        db_conn.commit()
        lead_id, version = row
        cleanup_leads.append(lead_id)
        return lead_id, version

    return _make
