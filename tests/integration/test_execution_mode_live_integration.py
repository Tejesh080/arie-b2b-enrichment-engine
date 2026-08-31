"""Productization M5 — end-to-end: an organization's own execution_mode and
BYOK credential, exercised through the actual production code path
(`arie.jobs.handlers.build_handlers(..., provider_mode="live")` with
*nothing* injected — the default `compute_score` takes for any organization
that isn't a test or `scripts/live_provider_smoke.py`).

Everything below mocks the vendor HTTP layer by substituting
`arie.live.provider_availability._ADAPTER_BUILDERS`, the one seam that
constructs a real `httpx.Client` from an organization's resolved credential
— never a real network call, matching every other live-mode integration
test in this suite (`test_live_multi_provider_integration.py` mocks the same
way, one level up, by injecting whole adapters instead of a resolver).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, source_for
from tests.integration.test_provider_configs_integration import (
    _client_as,
    _insert_org,
)
from tests.integration.test_provider_configs_integration import (
    app_state_with_vault as app_state_with_vault,
)
from tests.integration.test_provider_configs_integration import (
    vault_stub as vault_stub,
)

from arie.api.main import AppState
from arie.config import LIVE_PROVIDER
from arie.core.types import LeadStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import ClaimedJob
from arie.jobs.worker import JobContext, JobHandler
from arie.live import provider_availability
from arie.organizations import set_execution_mode
from arie.provider_configs import set_provider_credential
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.providers.live_abstract import AbstractCompanyEnrichmentProvider

pytestmark = pytest.mark.integration

_TEST_WORKER_ID = "execution-mode-live-it"


def _mock_abstract(employee_count: int, industry: str) -> AbstractCompanyEnrichmentProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"employee_count": employee_count, "industry": industry})

    return AbstractCompanyEnrichmentProvider.build(
        config=dataclasses.replace(LIVE_PROVIDER, api_key="unused-mock-path"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def mocked_abstract_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitutes the ONE seam `resolve_organization_providers` uses to turn
    a resolved credential into a real adapter — the credential itself still
    flows through `arie.credential_resolver`/Vault exactly as in production;
    only the resulting HTTP client is fake."""
    monkeypatch.setitem(
        provider_availability._ADAPTER_BUILDERS,
        ABSTRACT_PROVIDER_NAME,
        lambda raw_credential: _mock_abstract(employee_count=5, industry="Construction"),
    )


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def live_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=4, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def live_handlers(
    live_pool: ConnectionPool, runtime: SimulatedEnrichmentRuntime
) -> dict[str, JobHandler]:
    """The real production construction path — no `live_provider`/
    `live_providers` injected, so `compute_score` resolves every job's
    providers from its own organization's execution_mode/credentials."""
    return build_handlers(live_pool, runtime=runtime, provider_mode="live")


def _org_client(
    app_state_with_vault: AppState, db_conn: psycopg.Connection, *, execution_mode: str
) -> tuple[TestClient, uuid.UUID]:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    actor = uuid.uuid4()
    set_execution_mode(
        db_conn, organization_id=org_id, execution_mode=execution_mode, actor_user_id=actor
    )
    client = _client_as(app_state_with_vault, organization_id=org_id, user_id=actor, role="owner")
    return client, org_id


def _ingest(client: TestClient, cleanup: IngestCleanup) -> dict[str, object]:
    domain = f"exec-mode-{uuid.uuid4().hex[:10]}.test"
    email = f"nobody-{uuid.uuid4().hex[:8]}@{domain}"
    cleanup.domains.append(domain)
    cleanup.emails.append(email)
    response = client.post(
        "/leads",
        json={
            "source": source_for("execution-mode"),
            "email": email,
            "external_ref": f"execution-mode-{uuid.uuid4().hex[:12]}",
            "company_domain": domain,
        },
    )
    assert response.status_code == 201
    body: dict[str, object] = response.json()
    cleanup.lead_ids.append(uuid.UUID(str(body["lead_id"])))
    return body


def _take_ownership(db_conn: psycopg.Connection, job_id: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'processing', locked_by = %(worker)s, locked_at = now() "
            "WHERE job_id = %(job_id)s AND status = 'pending'",
            {"worker": _TEST_WORKER_ID, "job_id": job_id},
        )
        taken = cur.rowcount
        cur.execute("SELECT locked_by FROM jobs WHERE job_id = %(job_id)s", {"job_id": job_id})
        row = cur.fetchone()
    db_conn.commit()
    if not taken:
        owner = row[0] if row else "unknown"
        pytest.skip(f"another worker ({owner}) claimed job {job_id} before this test could")


def _process(
    live_pool: ConnectionPool, handlers: dict[str, JobHandler], body: dict[str, object]
) -> None:
    job_id = uuid.UUID(str(body["job_id"]))
    lead_id = uuid.UUID(str(body["lead_id"]))
    with live_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
            row = cur.fetchone()
        assert row is not None
        handlers["compute_score"](
            JobContext(
                conn=conn,
                job=ClaimedJob(
                    job_id=job_id,
                    lead_id=lead_id,
                    job_type="compute_score",
                    attempt_count=0,
                    idempotency_key=None,
                ),
                lead_status=LeadStatus(row[0]),
                lead_version=row[1],
            )
        )
        conn.commit()


def _lead_status(db_conn: psycopg.Connection, lead_id: str) -> LeadStatus:
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
    assert row is not None
    return LeadStatus(row[0])


def _provider_call_rows(db_conn: psycopg.Connection, lead_id: str) -> list[tuple[str, str, bool]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT provider, credential_source, cache_hit FROM provider_calls "
            "WHERE lead_id = %s ORDER BY requested_at",
            (lead_id,),
        )
        return cur.fetchall()


def _receipt_snapshot(db_conn: psycopg.Connection, lead_id: str) -> dict[str, object]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT evidence_snapshot FROM decision_receipts WHERE lead_id = %s", (lead_id,)
        )
        row = cur.fetchone()
    assert row is not None
    return dict(row[0] or {})


# --------------------------------------------------------------------- simulated --


def test_simulated_organization_makes_zero_real_calls_even_in_a_live_worker(
    app_state_with_vault: AppState,
    live_pool: ConnectionPool,
    live_handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    mocked_abstract_adapter: None,
) -> None:
    """Success criterion #1's live-path counterpart: an organization that
    hasn't opted into live processing gets zero real provider calls even
    while the worker process itself runs PROVIDER_MODE=live — the mocked
    adapter above is never even constructed, let alone called."""
    client, _org_id = _org_client(app_state_with_vault, db_conn, execution_mode="simulated")
    body = _ingest(client, cleanup_ingest)
    _take_ownership(db_conn, str(body["job_id"]))

    _process(live_pool, live_handlers, body)

    rows = _provider_call_rows(db_conn, str(body["lead_id"]))
    assert rows == []  # no ledger row at all - not even a suppressed/cache-hit one

    snapshot = _receipt_snapshot(db_conn, str(body["lead_id"]))
    assert snapshot["execution_mode"] == "simulated"
    assert (
        snapshot["provider_unavailability"][ABSTRACT_PROVIDER_NAME]
        == "provider_mode_disallows_live"
    )

    status = _lead_status(db_conn, str(body["lead_id"]))
    assert status in (LeadStatus.AWAITING_HUMAN, LeadStatus.SHADOW_EVALUATED)


# -------------------------------------------------------------------- live_shadow --


def test_live_shadow_organization_acquires_real_evidence_but_never_routes(
    app_state_with_vault: AppState,
    live_pool: ConnectionPool,
    live_handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    mocked_abstract_adapter: None,
) -> None:
    client, org_id = _org_client(app_state_with_vault, db_conn, execution_mode="live_shadow")
    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="org-own-secret-key",
        actor_user_id=uuid.uuid4(),
    )
    body = _ingest(client, cleanup_ingest)
    lead_id = str(body["lead_id"])
    _take_ownership(db_conn, str(body["job_id"]))

    _process(live_pool, live_handlers, body)

    rows = _provider_call_rows(db_conn, lead_id)
    assert len(rows) == 1
    provider, credential_source, cache_hit = rows[0]
    assert provider == ABSTRACT_PROVIDER_NAME
    assert credential_source == "organization"
    assert cache_hit is False

    snapshot = _receipt_snapshot(db_conn, lead_id)
    assert snapshot["execution_mode"] == "live_shadow"

    # Forced non-operative regardless of the lead's own (default, non-shadow)
    # ingestion mode - the org-level setting overrides it, per Part 3/14.
    status = _lead_status(db_conn, lead_id)
    assert status == LeadStatus.SHADOW_EVALUATED


# ---------------------------------------------------------------- live_human_only --


def test_live_human_only_organization_routes_to_awaiting_human(
    app_state_with_vault: AppState,
    live_pool: ConnectionPool,
    live_handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    mocked_abstract_adapter: None,
) -> None:
    client, org_id = _org_client(app_state_with_vault, db_conn, execution_mode="live_human_only")
    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="org-own-secret-key-2",
        actor_user_id=uuid.uuid4(),
    )
    body = _ingest(client, cleanup_ingest)
    lead_id = str(body["lead_id"])
    _take_ownership(db_conn, str(body["job_id"]))

    _process(live_pool, live_handlers, body)

    rows = _provider_call_rows(db_conn, lead_id)
    assert len(rows) == 1
    assert rows[0][1] == "organization"  # credential_source

    snapshot = _receipt_snapshot(db_conn, lead_id)
    assert snapshot["execution_mode"] == "live_human_only"

    status = _lead_status(db_conn, lead_id)
    assert status == LeadStatus.AWAITING_HUMAN


# ----------------------------------------------------------------------- security --


def test_the_raw_credential_never_appears_in_the_ledger_or_receipt(
    app_state_with_vault: AppState,
    live_pool: ConnectionPool,
    live_handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    mocked_abstract_adapter: None,
) -> None:
    secret = "sk-must-never-leak-anywhere-12345"
    client, org_id = _org_client(app_state_with_vault, db_conn, execution_mode="live_shadow")
    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential=secret,
        actor_user_id=uuid.uuid4(),
    )
    body = _ingest(client, cleanup_ingest)
    lead_id = str(body["lead_id"])
    _take_ownership(db_conn, str(body["job_id"]))

    _process(live_pool, live_handlers, body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT * FROM provider_calls WHERE lead_id = %s", (lead_id,))
        provider_call_rows = cur.fetchall()
        cur.execute("SELECT * FROM decision_receipts WHERE lead_id = %s", (lead_id,))
        receipt_rows = cur.fetchall()

    assert secret not in str(provider_call_rows)
    assert secret not in str(receipt_rows)

    receipt_response = client.get(f"/leads/{lead_id}/receipt")
    assert receipt_response.status_code == 200
    assert secret not in receipt_response.text
