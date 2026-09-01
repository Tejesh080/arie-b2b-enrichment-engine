"""Person-identity validation and provider-outcome suppression (Live V1).

The two production fixes the 2026-08-29 abstract-hunter-live-1 experiment's
findings required:

* A person-provider match at the right *company* is not automatically a match
  on the right *person* (the Stripe/Patrick Bosmans case) — ``MISMATCH``
  evidence must never reach the scorer.
* A provider's own recent *settled* outcome — success (even partial), or a
  miss — must stop an identical follow-up request from re-buying nothing new
  (the Jason Fried cache test). Section 13's scenarios A-G, against a real
  database and a real ledger, with the vendor's HTTP layer mocked.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any, Literal

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, source_for

from arie.config import HunterConfig, LiveOutcomeCacheConfig
from arie.core.types import LeadStatus, ProviderStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import ClaimedJob
from arie.jobs.worker import JobContext, JobHandler
from arie.live.outcome_cache import ProviderOutcomeGuard
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME as HUNTER
from arie.providers.live_hunter import HunterEnrichmentProvider
from arie.tenancy import LEGACY_ORGANIZATION_ID as ORG

pytestmark = pytest.mark.integration

_TEST_WORKER_ID = "provider-outcome-identity-it"
_HUNTER_COST = 0.0049


# ------------------------------------------------------------------ vendor mocks --


def _hunter_person(
    *, full_name: str, title: str, role: str, domain: str, employer: str = "Acme"
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        first, _, last = full_name.partition(" ")
        return httpx.Response(
            200,
            json={
                "data": {
                    "person": {
                        "name": {"fullName": full_name, "givenName": first, "familyName": last},
                        "employment": {
                            "name": employer,
                            "domain": domain,
                            "title": title,
                            "role": role,
                        },
                    },
                    "company": {},
                }
            },
        )

    return handler


def _hunter_miss(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"errors": [{"id": "not_found", "code": 404}]})


def _hunter_failing(status_code: int) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"errors": [{"id": "server_error"}]})

    return handler


def _counting(
    inner: Callable[[httpx.Request], httpx.Response], counter: list[int]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        counter[0] += 1
        return inner(request)

    return handler


def _hunter_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HunterEnrichmentProvider:
    return HunterEnrichmentProvider(
        config=HunterConfig(api_key="test-key", cost_usd_per_success=_HUNTER_COST),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# ------------------------------------------------------------------ scaffolding --


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


def _handlers_for(
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
    hunter: HunterEnrichmentProvider,
    *,
    strategy: Literal["optimized", "evaluation_parallel"] = "optimized",
) -> dict[str, JobHandler]:
    return build_handlers(
        live_pool,
        runtime=runtime,
        provider_mode="live",
        live_providers=[hunter],
        live_strategy=strategy,
    )


def _ingest(
    api_client: TestClient,
    cleanup: IngestCleanup,
    *,
    prefix: str,
    domain: str | None = None,
    email: str | None = None,
    full_name: str | None = None,
    mode: str = "normal",
) -> dict[str, Any]:
    resolved_domain = domain or f"{prefix}-{uuid.uuid4().hex[:10]}.test"
    resolved_email = email or f"nobody-{uuid.uuid4().hex[:8]}@{resolved_domain}"
    cleanup.domains.append(resolved_domain)
    cleanup.emails.append(resolved_email)
    payload: dict[str, Any] = {
        "source": source_for("outcome-identity"),
        "email": resolved_email,
        "external_ref": f"outcome-identity-{uuid.uuid4().hex[:12]}",
        "company_domain": resolved_domain,
        "mode": mode,
    }
    if full_name is not None:
        payload["full_name"] = full_name
    response = api_client.post("/leads", json=payload)
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))
    return body


def _take_ownership(db_conn: psycopg.Connection, job_id: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'processing', locked_by = %(worker)s, locked_at = now() "
            "WHERE job_id = %(job_id)s AND status = 'pending'",
            {"worker": _TEST_WORKER_ID, "job_id": job_id},
        )
        taken = cur.rowcount
    db_conn.commit()
    if not taken:
        pytest.skip(f"another worker claimed job {job_id} before this test could")


def _process_lead(
    live_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    body: dict[str, Any],
) -> None:
    job_id = uuid.UUID(body["job_id"])
    lead_id = uuid.UUID(body["lead_id"])
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
    with db_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'done' WHERE job_id = %s", (job_id,))
    db_conn.commit()


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


def _run(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    *,
    domain: str | None = None,
    email: str | None = None,
    full_name: str | None = None,
    mode: str = "normal",
) -> dict[str, Any]:
    body = _ingest(
        api_client,
        cleanup_ingest,
        prefix="outcome-identity",
        domain=domain,
        email=email,
        full_name=full_name,
        mode=mode,
    )
    _take_ownership(db_conn, body["job_id"])
    _process_lead(live_pool, handlers, db_conn, body)
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)
    return body


def _receipt(api_client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    return payload


def _snapshot(db_conn: psycopg.Connection, lead_id: str) -> dict[str, Any]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT evidence_snapshot FROM decision_receipts WHERE lead_id = %s", (lead_id,)
        )
        row = cur.fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def _evidence_sources(db_conn: psycopg.Connection, entity_id: str) -> dict[str, str]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT field_name, source FROM evidence WHERE entity_id = %s", (uuid.UUID(entity_id),)
        )
        return {row[0]: row[1] for row in cur.fetchall()}


# ========================================================= identity validation --


def test_a_same_company_wrong_person_match_is_not_scored(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """The Stripe case, reproduced with a mock: same domain, different real
    person. Hunter's title_seniority/title_function must not reach evidence
    or scoring, and the receipt must record the mismatch."""
    domain = f"stripe-{uuid.uuid4().hex[:8]}.test"
    hunter = _hunter_provider(
        _hunter_person(
            full_name="Patrick Bosmans", title="IT Administrator", role="it", domain=domain
        )
    )
    handlers = _handlers_for(live_pool, runtime, hunter)

    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=f"patrick@{domain}",
        full_name="Patrick Collison",
    )

    sources = _evidence_sources(db_conn, body["person_id"])
    assert HUNTER not in sources.values(), "mismatched person evidence must not be persisted"

    snapshot = _snapshot(db_conn, body["lead_id"])
    findings = snapshot.get("identity_findings", [])
    assert any(f["provider"] == HUNTER and f["verdict"] == "MISMATCH" for f in findings)

    receipt = _receipt(api_client, body)
    assert receipt["decision"]["final_status"] in ("AWAITING_HUMAN", "SHADOW_EVALUATED")


def test_a_matching_name_and_domain_is_scored_normally(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    domain = f"acme-{uuid.uuid4().hex[:8]}.test"
    hunter = _hunter_provider(
        _hunter_person(full_name="Jane Doe", title="VP of Sales", role="sales", domain=domain)
    )
    handlers = _handlers_for(live_pool, runtime, hunter)

    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=f"jane@{domain}",
        full_name="Jane Doe",
    )

    sources = _evidence_sources(db_conn, body["person_id"])
    assert sources.get("title_seniority") == HUNTER

    snapshot = _snapshot(db_conn, body["lead_id"])
    findings = snapshot.get("identity_findings", [])
    assert any(f["provider"] == HUNTER and f["verdict"] == "VERIFIED" for f in findings)


# ==================================================== outcome-cache scenarios --


def test_scenario_a_a_full_success_is_not_re_bought(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Section 13.A. Both declared fields map cleanly — the pre-existing
    evidence-cache path — a second identical request must still make zero
    HTTP calls."""
    domain = f"co-{uuid.uuid4().hex[:8]}.test"
    email = f"vp@{domain}"
    counter = [0]
    hunter = _hunter_provider(
        _counting(
            _hunter_person(
                full_name="Vera Sales", title="VP of Sales", role="sales", domain=domain
            ),
            counter,
        )
    )
    handlers = _handlers_for(live_pool, runtime, hunter)

    first = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1
    sources = _evidence_sources(db_conn, first["person_id"])
    assert sources.get("title_seniority") == HUNTER
    assert sources.get("title_function") == HUNTER

    _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1, "a full prior success must suppress the identical re-call"


def test_scenario_b_a_partial_success_is_not_re_bought(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Section 13.B — and the exact Jason Fried defect: Hunter returns only
    title_seniority (title_function stays UNKNOWN); a second identical
    request for the same person must make zero HTTP calls."""
    domain = f"co-{uuid.uuid4().hex[:8]}.test"
    email = f"founder@{domain}"
    counter = [0]
    hunter = _hunter_provider(
        _counting(
            _hunter_person(
                full_name="Sam Founder", title="Founder", role="chief_vibes", domain=domain
            ),
            counter,
        )
    )
    handlers = _handlers_for(live_pool, runtime, hunter)

    first = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1
    sources = _evidence_sources(db_conn, first["person_id"])
    assert "title_seniority" in sources
    assert "title_function" not in sources  # "chief_vibes" is unmappable — correctly UNKNOWN

    second = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1, "a partial prior success must suppress the identical re-call"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT suppressed_reason FROM provider_calls WHERE lead_id = %s AND provider = %s",
            (second["lead_id"], HUNTER),
        )
        rows = [row[0] for row in cur.fetchall()]
    assert "recent_partial" in rows


def test_scenario_c_a_miss_is_not_re_bought_inside_its_ttl(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Section 13.C. A negative cache: no evidence row is ever produced by a
    miss, so without the outcome guard this would re-call forever."""
    domain = f"co-{uuid.uuid4().hex[:8]}.test"
    email = f"nobody@{domain}"
    counter = [0]
    hunter = _hunter_provider(_counting(_hunter_miss, counter))
    handlers = _handlers_for(live_pool, runtime, hunter)

    _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1

    second = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1, "a recent miss must suppress the identical re-call"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT suppressed_reason FROM provider_calls WHERE lead_id = %s AND provider = %s",
            (second["lead_id"], HUNTER),
        )
        rows = [row[0] for row in cur.fetchall()]
    assert "recent_miss" in rows


def test_scenario_e_a_timeout_is_not_treated_as_a_settled_miss(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Section 13.E. A 5xx/timeout must never suppress the next request the
    way a MISS does — the cooldown/backoff machinery, not the outcome cache,
    is what governs a provider that is down."""
    domain = f"co-{uuid.uuid4().hex[:8]}.test"
    email = f"nobody@{domain}"
    counter = [0]
    hunter = _hunter_provider(_counting(_hunter_failing(503), counter))
    handlers = _handlers_for(live_pool, runtime, hunter)

    _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1

    second = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 2, "a server error must not be suppressed like a settled miss"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT suppressed_reason FROM provider_calls WHERE lead_id = %s AND provider = %s",
            (second["lead_id"], HUNTER),
        )
        rows = [row[0] for row in cur.fetchall()]
    assert all(r is None for r in rows)


def test_scenario_f_different_person_same_company_does_not_cross_contaminate(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Section 13.F. The outcome guard is keyed by (provider, entity_type,
    entity_id) — a person's own id, never the company's — so a miss for one
    colleague must never suppress a lookup for a different one."""
    domain = f"co-{uuid.uuid4().hex[:8]}.test"
    counter = [0]
    hunter = _hunter_provider(_counting(_hunter_miss, counter))
    handlers = _handlers_for(live_pool, runtime, hunter)

    _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=f"alice@{domain}",
    )
    assert counter[0] == 1

    _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=f"bob@{domain}",
    )
    assert counter[0] == 2, "a different person at the same company must still be asked"


def test_outcome_guard_ttl_expiry_makes_the_provider_askable_again(
    live_pool: ConnectionPool, cleanup_evidence: list[uuid.UUID]
) -> None:
    """Section 13.D, at the guard level (no wall-clock sleep in an
    integration test): a miss older than the configured TTL must not
    suppress the next request."""
    from arie.ledger.store import PostgresCostLedger

    ledger = PostgresCostLedger(live_pool)
    entity_id = uuid.uuid4()
    cleanup_evidence.append(entity_id)
    write = ledger.record_provider_call(
        idempotency_key=f"outcome-ttl-{uuid.uuid4().hex}",
        provider=HUNTER,
        entity_type="person",
        entity_id=entity_id,
        status=ProviderStatus.MISS,
        cost_usd=0.0,
        latency_ms=10.0,
        organization_id=ORG,
    )
    assert write.recorded

    guard_within_ttl = ProviderOutcomeGuard(
        live_pool, LiveOutcomeCacheConfig(miss_ttl_seconds=3600.0)
    )
    assert (
        guard_within_ttl.recent_miss(HUNTER, "person", entity_id, organization_id=ORG) is not None
    )

    guard_expired = ProviderOutcomeGuard(live_pool, LiveOutcomeCacheConfig(miss_ttl_seconds=0.0))
    assert guard_expired.recent_miss(HUNTER, "person", entity_id, organization_id=ORG) is None


# ============================================ Productization M5 Issue 3 --
# Paid-call unknown-outcome / retry safety: a genuine timeout or transport-
# level failure — where ARIE cannot tell whether the vendor received (and
# possibly began billing for) the request — must not be blindly re-issued
# by a worker retry, unlike a settled miss (which is free to re-check
# quickly) or a definite rejection like a 503 (scenario E above, which
# stays retryable — the cooldown/backoff machinery governs that, not this
# guard).


def _hunter_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.TimeoutException("simulated vendor timeout", request=request)


def test_a_genuine_timeout_suppresses_an_identical_retry_and_never_settles_as_a_miss(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """The crash/retry scenario Issue 3 exists for: the first attempt times
    out (uncertain — the vendor may have received the request), the job is
    retried (a second, independent job processing run against the exact
    same lead/company/person, exactly what `arie.jobs.worker`'s own retry
    path does), and the retry must NOT blindly re-issue the same paid call.
    """
    domain = f"co-{uuid.uuid4().hex[:8]}.test"
    email = f"nobody@{domain}"
    counter = [0]
    hunter = _hunter_provider(_counting(_hunter_timeout, counter))
    handlers = _handlers_for(live_pool, runtime, hunter)

    first = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_kind, cost_usd FROM provider_calls "
            "WHERE lead_id = %s AND provider = %s",
            (first["lead_id"], HUNTER),
        )
        first_row = cur.fetchone()
    assert first_row is not None
    assert first_row[0] == str(ProviderStatus.TIMEOUT)
    assert first_row[1] == "timeout"
    assert first_row[2] == 0  # never fabricate a cost for an unresolved attempt

    # Simulates a worker-level job retry for the same lead: a second,
    # independent `compute_score` run against the same company/person
    # entities — not the same job row, which is the point: even a
    # *different* job re-processing this identity must not repurchase.
    second = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1, "an uncertain (timeout) outcome must suppress the identical retry"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT suppressed_reason, status FROM provider_calls "
            "WHERE lead_id = %s AND provider = %s",
            (second["lead_id"], HUNTER),
        )
        second_rows = cur.fetchall()
    assert any(row[0] == "uncertain_outcome" for row in second_rows)
    # Never recorded as a settled MISS — that would be a fabricated fact
    # about a vendor that was never actually confirmed to have answered.
    assert all(row[1] != str(ProviderStatus.MISS) for row in second_rows)


def test_a_transport_level_error_is_also_treated_as_uncertain(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Not only `httpx.TimeoutException` — any connection-level
    `httpx.HTTPError` (`error_kind` prefixed `transport_error:`, every live
    adapter's shared vocabulary) means "no response was ever received" and
    is exactly as uncertain as a timeout."""

    def _connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    domain = f"co-{uuid.uuid4().hex[:8]}.test"
    email = f"nobody@{domain}"
    counter = [0]
    hunter = _hunter_provider(_counting(_connect_error, counter))
    handlers = _handlers_for(live_pool, runtime, hunter)

    _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1

    second = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        domain=domain,
        email=email,
    )
    assert counter[0] == 1, "a connection-level failure must also suppress the identical retry"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT suppressed_reason FROM provider_calls WHERE lead_id = %s AND provider = %s",
            (second["lead_id"], HUNTER),
        )
        rows = [row[0] for row in cur.fetchall()]
    assert "uncertain_outcome" in rows


def test_a_503_server_error_is_not_treated_as_uncertain_and_stays_retryable(
    live_pool: ConnectionPool, cleanup_evidence: list[uuid.UUID]
) -> None:
    """The other half of the classification (Issue 4-adjacent precision):
    a *definite* HTTP response — even a failing one — means the vendor was
    reached, so it is never suppressed by this guard. Scenario E above
    already proves this behaviourally through the full acquisition loop;
    this pins the guard's own classification directly."""
    from arie.ledger.store import PostgresCostLedger

    ledger = PostgresCostLedger(live_pool)
    entity_id = uuid.uuid4()
    cleanup_evidence.append(entity_id)
    ledger.record_provider_call(
        idempotency_key=f"outcome-uncertain-{uuid.uuid4().hex}",
        provider=HUNTER,
        entity_type="person",
        entity_id=entity_id,
        status=ProviderStatus.ERROR,
        cost_usd=0.0,
        latency_ms=10.0,
        organization_id=ORG,
        error_kind="server_error",
    )

    guard = ProviderOutcomeGuard(
        live_pool, LiveOutcomeCacheConfig(uncertain_outcome_ttl_seconds=3600.0)
    )
    assert guard.recent_uncertain_outcome(HUNTER, "person", entity_id, organization_id=ORG) is None


def test_uncertain_outcome_guard_ttl_expiry_makes_the_provider_askable_again(
    live_pool: ConnectionPool, cleanup_evidence: list[uuid.UUID]
) -> None:
    from arie.ledger.store import PostgresCostLedger

    ledger = PostgresCostLedger(live_pool)
    entity_id = uuid.uuid4()
    cleanup_evidence.append(entity_id)
    ledger.record_provider_call(
        idempotency_key=f"outcome-uncertain-ttl-{uuid.uuid4().hex}",
        provider=HUNTER,
        entity_type="person",
        entity_id=entity_id,
        status=ProviderStatus.TIMEOUT,
        cost_usd=0.0,
        latency_ms=10.0,
        organization_id=ORG,
        error_kind="timeout",
    )

    guard_within_ttl = ProviderOutcomeGuard(
        live_pool, LiveOutcomeCacheConfig(uncertain_outcome_ttl_seconds=3600.0)
    )
    assert (
        guard_within_ttl.recent_uncertain_outcome(HUNTER, "person", entity_id, organization_id=ORG)
        is not None
    )

    guard_expired = ProviderOutcomeGuard(
        live_pool, LiveOutcomeCacheConfig(uncertain_outcome_ttl_seconds=0.0)
    )
    assert (
        guard_expired.recent_uncertain_outcome(HUNTER, "person", entity_id, organization_id=ORG)
        is None
    )


def test_uncertain_outcome_suppression_is_organization_scoped(
    live_pool: ConnectionPool, cleanup_evidence: list[uuid.UUID], db_conn: psycopg.Connection
) -> None:
    from tests.integration.test_provider_configs_integration import _insert_org

    from arie.ledger.store import PostgresCostLedger

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    _insert_org(db_conn, org_a)
    _insert_org(db_conn, org_b)

    ledger = PostgresCostLedger(live_pool)
    entity_id = uuid.uuid4()
    cleanup_evidence.append(entity_id)
    ledger.record_provider_call(
        idempotency_key=f"outcome-uncertain-tenancy-{uuid.uuid4().hex}",
        provider=HUNTER,
        entity_type="person",
        entity_id=entity_id,
        status=ProviderStatus.TIMEOUT,
        cost_usd=0.0,
        latency_ms=10.0,
        organization_id=org_a,
        error_kind="timeout",
    )

    guard = ProviderOutcomeGuard(
        live_pool, LiveOutcomeCacheConfig(uncertain_outcome_ttl_seconds=3600.0)
    )
    assert (
        guard.recent_uncertain_outcome(HUNTER, "person", entity_id, organization_id=org_a)
        is not None
    )
    assert (
        guard.recent_uncertain_outcome(HUNTER, "person", entity_id, organization_id=org_b) is None
    )
