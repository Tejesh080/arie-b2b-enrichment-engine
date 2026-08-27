"""``PROVIDER_MODE=live`` against a live database (post-M1 P5).

Mirrors `test_pipeline_integration.py`'s shape for the live handler: `POST
/leads` -> queue -> `build_handlers(provider_mode="live")`'s compute_score ->
durable evidence + ledger -> state graph -> receipt. The one real adapter's
HTTP layer is mocked with `httpx.MockTransport` throughout -- this suite never
makes a real Abstract API call (see `scripts/live_provider_smoke.py` for the
one deliberately-real path). Unlike the simulated suite, leads here have no
corpus membership at all -- that is the entire point of live mode.

**Scope, now that live mode runs two providers.** This suite injects Abstract
*alone* (`live_provider=`, the deliberate single-adapter form) and keeps doing
so on purpose: it exercises the company-enrichment path in isolation, where a
failure can only be that path's. The multi-provider behaviour -- ordering, when
Apollo is skipped, per-entity cache scoping, budget interaction across two
providers -- lives in `test_live_multi_provider_integration.py`. Apollo
therefore appears in these receipts under `providers.not_called`, which is the
truthful report: it is registered, and it was not called.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, source_for

from arie.config import LiveProviderConfig
from arie.evalgen.schema import EvalLead
from arie.identity.normalize import normalize_company_name
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobHandler, run_worker_cycle
from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME, AbstractCompanyEnrichmentProvider

pytestmark = pytest.mark.integration

_MAX_CYCLES = 6


def _success_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"employee_count": 4200, "industry": "Software"})


def _mock_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AbstractCompanyEnrichmentProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AbstractCompanyEnrichmentProvider(
        config=LiveProviderConfig(api_key="test-key", cost_usd_per_call=0.002), client=client
    )


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture
def live_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=6, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _handlers_for(
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
    handler: Callable[[httpx.Request], httpx.Response],
) -> dict[str, JobHandler]:
    return build_handlers(
        live_pool, runtime=runtime, provider_mode="live", live_provider=_mock_provider(handler)
    )


def _ingest(
    api_client: TestClient,
    cleanup: IngestCleanup,
    *,
    domain: str,
    email: str,
    mode: str = "normal",
) -> dict[str, Any]:
    cleanup.domains.append(domain)
    cleanup.emails.append(email)
    response = api_client.post(
        "/leads",
        json={
            "source": source_for("live"),
            "email": email,
            "external_ref": f"live-{uuid.uuid4().hex[:12]}",
            "company_domain": domain,
            "mode": mode,
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))
    return body


def _drive_to_completion(
    job_queue: PostgresJobQueue,
    live_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    job_id: str,
) -> str:
    for _ in range(_MAX_CYCLES):
        run_worker_cycle(
            job_queue,
            live_pool,
            handlers,
            worker_id=f"live-it-{uuid.uuid4().hex[:8]}",
            batch_size=3,
            job_types=["compute_score"],
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        assert row is not None
        if row[0] not in ("pending", "processing"):
            return str(row[0])
    return "pending"


def _register_cleanup(
    db_conn: psycopg.Connection,
    cleanup: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    body: dict[str, Any],
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT idempotency_key FROM provider_calls WHERE lead_id = %s", (body["lead_id"],)
        )
        cleanup.provider_call_keys.extend(row[0] for row in cur.fetchall())
    cleanup_evidence.append(uuid.UUID(body["company_id"]))
    cleanup_evidence.append(uuid.UUID(body["person_id"]))


def test_a_lead_with_no_corpus_membership_is_enriched_by_the_live_provider(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    job_queue: PostgresJobQueue,
    live_pool: ConnectionPool,
) -> None:
    """The headline P5 claim: a real (non-frozen-corpus) lead is enriched at
    all, unlike simulated mode's `UnknownCorpusIdentityError`."""
    domain = f"live-{uuid.uuid4().hex[:10]}.test"
    email = f"nobody@{domain}"
    handlers = _handlers_for(live_pool, runtime, _success_handler)

    body = _ingest(api_client, cleanup_ingest, domain=domain, email=email)
    status = _drive_to_completion(job_queue, live_pool, handlers, db_conn, body["job_id"])
    assert status == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT provider, status, cost_usd, cache_hit FROM provider_calls WHERE lead_id = %s",
            (body["lead_id"],),
        )
        calls = cur.fetchall()
        cur.execute(
            "SELECT field_name, value, source FROM evidence WHERE entity_id = %s",
            (uuid.UUID(body["company_id"]),),
        )
        evidence_rows = cur.fetchall()
        cur.execute(
            "SELECT policy_name, decision FROM decision_receipts WHERE lead_id = %s",
            (body["lead_id"],),
        )
        receipt_row = cur.fetchone()

    assert len(calls) == 1
    assert calls[0][0] == PROVIDER_NAME
    assert calls[0][1] == "success"
    assert float(calls[0][2]) == pytest.approx(0.002)
    assert calls[0][3] is False

    evidence_fields = {row[0]: row[1] for row in evidence_rows}
    assert evidence_fields["employee_count"] == 4200
    assert evidence_fields["industry"] == "software"
    assert all(row[2] == PROVIDER_NAME for row in evidence_rows)

    assert receipt_row is not None
    assert receipt_row[0] == "live_single_provider"

    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert receipt["providers"]["called"][0]["provider"] == PROVIDER_NAME
    # This suite injects Abstract alone, so Apollo is a registered live
    # provider that genuinely was not called -- and the receipt says so. This
    # asserted `[]` while Abstract was the only registered provider; the honest
    # value changed when a second one was wired, and a receipt that still
    # claimed "nothing else was available" would be the bug.
    assert receipt["providers"]["not_called"] == [HUNTER_PROVIDER_NAME, APOLLO_PROVIDER_NAME]
    assert receipt["shadow"] is False


def test_a_second_lead_at_the_same_domain_reuses_cached_evidence(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    job_queue: PostgresJobQueue,
    live_pool: ConnectionPool,
) -> None:
    """The interesting live-provider story per the P5 brief: ARIE decides it
    does NOT need to call the real paid API a second time because enough
    evidence already exists -- measured as a zero-cost cache_hit row, not a
    silent skip (handoff item #5)."""
    domain = f"live-{uuid.uuid4().hex[:10]}.test"
    calls_made = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls_made
        calls_made += 1
        return httpx.Response(200, json={"employee_count": 900, "industry": "fintech"})

    handlers = _handlers_for(live_pool, runtime, counting_handler)

    first = _ingest(api_client, cleanup_ingest, domain=domain, email=f"first@{domain}")
    assert _drive_to_completion(job_queue, live_pool, handlers, db_conn, first["job_id"]) == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, first)
    assert calls_made == 1

    second = _ingest(api_client, cleanup_ingest, domain=domain, email=f"second@{domain}")
    assert _drive_to_completion(job_queue, live_pool, handlers, db_conn, second["job_id"]) == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, second)

    # No second real call was made -- the mock transport's handler didn't run again.
    assert calls_made == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT cache_hit, cost_usd FROM provider_calls WHERE lead_id = %s",
            (second["lead_id"],),
        )
        second_calls = cur.fetchall()

    assert len(second_calls) == 1, "the cache hit must still be recorded, not silently skipped"
    assert second_calls[0][0] is True
    assert float(second_calls[0][1]) == 0.0

    receipt = api_client.get(f"/leads/{second['lead_id']}/receipt").json()
    assert receipt["evidence"]["cache_hits"] == 1
    assert receipt["providers"]["called"][0]["cache_hit"] is True
    assert receipt["providers"]["called"][0]["cost_usd"] == "0"


def test_a_lead_with_no_domain_never_calls_the_provider(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    runtime: SimulatedEnrichmentRuntime,
    job_queue: PostgresJobQueue,
    live_pool: ConnectionPool,
) -> None:
    """A free-mail lead with no resolvable company domain can never be
    enriched by a domain-keyed provider -- honest, not a fabricated call."""
    calls_made = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls_made
        calls_made += 1
        return httpx.Response(200, json={"employee_count": 1, "industry": "software"})

    handlers = _handlers_for(live_pool, runtime, counting_handler)
    email = f"solo-{uuid.uuid4().hex[:10]}@gmail.com"
    company_name = f"Solo Company {uuid.uuid4().hex[:8]}"
    cleanup_ingest.emails.append(email)
    # `domain_from_email` returns None for a free-mail domain like gmail.com
    # (arie.identity.normalize) -- resolution falls back to the name-only path,
    # which is what produces a company with `canonical_domain IS NULL`, the
    # case this test targets.
    cleanup_ingest.company_names.append(normalize_company_name(company_name))

    response = api_client.post(
        "/leads",
        json={
            "source": source_for("live"),
            "email": email,
            "external_ref": f"live-{uuid.uuid4().hex[:12]}",
            "company_name": company_name,
        },
    )
    assert response.status_code == 201
    body = response.json()
    cleanup_ingest.lead_ids.append(uuid.UUID(body["lead_id"]))

    status = _drive_to_completion(job_queue, live_pool, handlers, db_conn, body["job_id"])
    assert status == "done"
    assert calls_made == 0

    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert receipt["stopping"]["reason_code"] == "no_domain_available"
    assert receipt["providers"]["called"] == []
