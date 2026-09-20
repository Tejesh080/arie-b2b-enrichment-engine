"""Fixtures for tests that need a live Postgres database, scoped to
``tests/mcp``.

Self-contained rather than importing from ``tests/integration/conftest.py``
— each test package in this repo owns its own guard chain rather than
depending on another package's fixtures, and the guard chain itself is
short enough that duplicating it is cheaper than coupling two test
packages together.

Same three guards as ``tests/integration/conftest.py``, for the same
reason: **only** ``TEST_DATABASE_URL`` (never ``DATABASE_URL``), **only**
with ``ARIE_ALLOW_INTEGRATION_TEST_DB=1``, and **only** against a database
carrying ``scripts/test_db.py``'s designation marker. See that file's own
docstring for the incident this exists to prevent.

Every test in this package is marked ``integration`` (``pytestmark`` in each
module) and is therefore excluded by ``make test``/``pytest -m "not
integration"``, included by ``make test-all``/``pytest -m integration`` —
the same bucket the rest of this repo's DB-backed tests already live in.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from scripts.migrate import migrate
from scripts.test_db import IntegrationDatabaseGuardError, assert_not_production, marker_present

from arie.config import IntegrationDatabaseConfig
from arie.tenancy import LEGACY_ORGANIZATION_ID

_SETUP_HINT = (
    "See scripts/test_db.py for setup. In short:\n"
    "  docker compose up -d db\n"
    "  export TEST_DATABASE_URL=postgresql://arie:arie_local_dev@localhost:5432/arie_test\n"
    "  export ARIE_ALLOW_INTEGRATION_TEST_DB=1\n"
    "  python scripts/test_db.py designate"
)

_READONLY_ROLE = "arie_mcp_readonly"


def _test_database_config() -> IntegrationDatabaseConfig:
    return IntegrationDatabaseConfig()


@pytest.fixture(scope="session")
def mcp_migrated_database() -> str:
    """Same guard sequence as ``tests/integration/conftest.py``'s
    ``migrated_database`` — duplicated deliberately, see module docstring.
    Applies every migration (including 0039, which is what makes this
    package's tests meaningful) and returns the pooled admin connection
    string."""
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
def mcp_readonly_password(mcp_migrated_database: str) -> str:
    """Sets a random, test-only password for ``arie_mcp_readonly`` on the
    disposable test database. Never written to any file — the migration
    itself (migrations/0039) deliberately never sets one, so this is the
    only place this session's password exists."""
    password = secrets.token_urlsafe(24)
    with psycopg.connect(mcp_migrated_database, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(_READONLY_ROLE), sql.Literal(password)
            )
        )
    return password


def _with_credentials(url: str, *, user: str, password: str) -> str:
    parts = urlsplit(url)
    netloc = f"{user}:{password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture(scope="session")
def mcp_readonly_database_url(mcp_migrated_database: str, mcp_readonly_password: str) -> str:
    """The connection string this package's tools/tests use as
    ``arie_mcp_readonly`` — same host/port/dbname as the admin connection,
    different role entirely."""
    return _with_credentials(
        mcp_migrated_database, user=_READONLY_ROLE, password=mcp_readonly_password
    )


@pytest.fixture
def admin_conn(mcp_migrated_database: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection with the same privileges migrations run with — for
    seeding/cleaning up test rows, never for anything a tool under test
    should be doing itself."""
    with psycopg.connect(mcp_migrated_database, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@dataclass(frozen=True)
class SeededJob:
    job_id: uuid.UUID
    lead_id: uuid.UUID


def seed_job(
    conn: psycopg.Connection[Any],
    *,
    status: str = "dead_letter",
    job_type: str = "compute_score",
    attempt_count: int = 3,
    last_error: str | None = "RuntimeError: synthetic failure for tests/mcp",
    lead_status: str = "FAILED",
    lead_source: str = "tests-mcp",
) -> SeededJob:
    """Inserts one lead and one job referencing it, both cleaned up by the
    caller (``delete_job_and_lead``) — this package's tests never rely on
    the database being empty, only on their own seeded rows being findable.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO leads (source, external_ref, status, organization_id)
            VALUES (%(source)s, %(external_ref)s, %(status)s, %(organization_id)s)
            RETURNING lead_id
            """,
            {
                "source": lead_source,
                "external_ref": f"tests-mcp-{uuid.uuid4().hex[:12]}",
                "status": lead_status,
                "organization_id": str(LEGACY_ORGANIZATION_ID),
            },
        )
        lead_row = cur.fetchone()
        assert lead_row is not None
        lead_id = lead_row["lead_id"]

        cur.execute(
            """
            INSERT INTO jobs (lead_id, job_type, status, attempt_count, last_error)
            VALUES (%(lead_id)s, %(job_type)s, %(status)s, %(attempt_count)s, %(last_error)s)
            RETURNING job_id
            """,
            {
                "lead_id": lead_id,
                "job_type": job_type,
                "status": status,
                "attempt_count": attempt_count,
                "last_error": last_error,
            },
        )
        job_row = cur.fetchone()
        assert job_row is not None
    return SeededJob(job_id=job_row["job_id"], lead_id=lead_id)


def delete_job_and_lead(conn: psycopg.Connection[Any], seeded: SeededJob) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE job_id = %s", (str(seeded.job_id),))
        cur.execute("DELETE FROM leads WHERE lead_id = %s", (str(seeded.lead_id),))


@dataclass(frozen=True)
class SeededScenario:
    """A full investigation scenario: one lead with a dead-lettered job, a
    quota-exhausted provider_calls error, and a voi_decisions step — enough
    for the extended debugging workflow (get_system_health ->
    get_recent_errors -> list_failed_jobs -> inspect_job ->
    inspect_provider_health -> get_enrichment_costs ->
    inspect_routing_decision) to have something real to find at every step.
    """

    job: SeededJob
    provider: str
    call_id: uuid.UUID
    decision_id: uuid.UUID


def seed_full_scenario(
    conn: psycopg.Connection[Any], *, provider: str = "abstract_company_enrichment"
) -> SeededScenario:
    job = seed_job(conn)
    marker = uuid.uuid4().hex[:8]

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO provider_calls (
                lead_id, provider, entity_type, entity_id, idempotency_key,
                completed_at, cost_usd, status, cache_hit, error_kind, organization_id
            )
            VALUES (
                %(lead_id)s, %(provider)s, 'company', %(entity_id)s, %(idempotency_key)s,
                now(), 0.0, 'error', false, 'quota_exhausted', %(organization_id)s
            )
            RETURNING call_id
            """,
            {
                "lead_id": job.lead_id,
                "provider": provider,
                "entity_id": uuid.uuid4(),
                "idempotency_key": f"tests-mcp-scenario-{marker}",
                "organization_id": str(LEGACY_ORGANIZATION_ID),
            },
        )
        call_row = cur.fetchone()
        assert call_row is not None

        cur.execute(
            """
            INSERT INTO voi_decisions (
                lead_id, step_number, candidate_provider, p_flips_decision,
                business_value, expected_cost, latency_penalty, net_evoi, chosen,
                confidence_before, confidence_after, organization_id
            )
            VALUES (
                %(lead_id)s, 1, %(provider)s, 0.35, 10.0, 0.00165, 0.001, 3.4, true,
                0.5, 0.7, %(organization_id)s
            )
            RETURNING decision_id
            """,
            {
                "lead_id": job.lead_id,
                "provider": provider,
                "organization_id": str(LEGACY_ORGANIZATION_ID),
            },
        )
        decision_row = cur.fetchone()
        assert decision_row is not None

    return SeededScenario(
        job=job,
        provider=provider,
        call_id=call_row["call_id"],
        decision_id=decision_row["decision_id"],
    )


def delete_full_scenario(conn: psycopg.Connection[Any], scenario: SeededScenario) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM voi_decisions WHERE decision_id = %s", (str(scenario.decision_id),)
        )
        cur.execute("DELETE FROM provider_calls WHERE call_id = %s", (str(scenario.call_id),))
    delete_job_and_lead(conn, scenario.job)


@pytest.fixture
def seeded_scenario(admin_conn: psycopg.Connection[Any]) -> Iterator[SeededScenario]:
    scenario = seed_full_scenario(admin_conn)
    yield scenario
    delete_full_scenario(admin_conn, scenario)


@pytest.fixture
def seeded_dead_letter_job(admin_conn: psycopg.Connection[Any]) -> Iterator[SeededJob]:
    seeded = seed_job(admin_conn)
    yield seeded
    delete_job_and_lead(admin_conn, seeded)


@pytest.fixture
def seeded_pii_job(admin_conn: psycopg.Connection[Any]) -> Iterator[dict[str, str]]:
    """A job/lead pair whose *lead* points at a person/company carrying a
    real-shaped name, email, and domain — the fixture the PII test (spec
    § 16) needs to prove those strings never reach a tool's serialized
    output, not merely that the view definition omits their columns.

    ``last_error`` is deliberately a generic message, not one containing the
    name/email: spec § 8 explicitly does *not* promise ``last_error`` is
    PII-free (only length-capped) — this fixture proves the guarantee the
    spec actually makes (no ``persons``/``companies`` column is reachable
    through any join), not one it doesn't.
    """
    marker = uuid.uuid4().hex[:8]
    full_name = f"Priscilla Okonkwo-{marker}"
    email = f"priscilla.okonkwo.{marker}@northwind-example.test"
    domain = f"northwind-{marker}-example.test"

    with admin_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO companies (name, canonical_domain) VALUES (%s, %s) RETURNING company_id",
            (f"Northwind Example {marker}", domain),
        )
        company_row = cur.fetchone()
        assert company_row is not None
        company_id = company_row["company_id"]

        cur.execute(
            """
            INSERT INTO persons (company_id, canonical_email, full_name, organization_id)
            VALUES (%(company_id)s, %(email)s, %(full_name)s, %(organization_id)s)
            RETURNING person_id
            """,
            {
                "company_id": company_id,
                "email": email,
                "full_name": full_name,
                "organization_id": str(LEGACY_ORGANIZATION_ID),
            },
        )
        person_row = cur.fetchone()
        assert person_row is not None
        person_id = person_row["person_id"]

        cur.execute(
            """
            INSERT INTO leads (person_id, company_id, source, external_ref, status, organization_id)
            VALUES (%(person_id)s, %(company_id)s, 'tests-mcp-pii', %(external_ref)s, 'FAILED', %(organization_id)s)
            RETURNING lead_id
            """,
            {
                "person_id": person_id,
                "company_id": company_id,
                "external_ref": f"tests-mcp-pii-{marker}",
                "organization_id": str(LEGACY_ORGANIZATION_ID),
            },
        )
        lead_row = cur.fetchone()
        assert lead_row is not None
        lead_id = lead_row["lead_id"]

        cur.execute(
            """
            INSERT INTO jobs (lead_id, job_type, status, attempt_count, last_error)
            VALUES (%(lead_id)s, 'compute_score', 'dead_letter', 3, %(last_error)s)
            RETURNING job_id
            """,
            {
                "lead_id": lead_id,
                "last_error": "RuntimeError: synthetic failure for tests/mcp PII fixture",
            },
        )
        job_row = cur.fetchone()
        assert job_row is not None
        job_id = job_row["job_id"]

    yield {
        "job_id": str(job_id),
        "lead_id": str(lead_id),
        "full_name": full_name,
        "email": email,
        "domain": domain,
    }

    with admin_conn.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE job_id = %s", (str(job_id),))
        cur.execute("DELETE FROM leads WHERE lead_id = %s", (str(lead_id),))
        cur.execute("DELETE FROM persons WHERE person_id = %s", (str(person_id),))
        cur.execute("DELETE FROM companies WHERE company_id = %s", (str(company_id),))


@pytest.fixture
def audit_log_dir(tmp_path: Any) -> str:
    return str(tmp_path / "mcp-audit")
