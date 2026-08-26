"""Live V1 Foundation against a live database — the autonomy guard and spend caps.

The unit suites (``tests/unit/test_live_safety.py``,
``tests/unit/test_live_budget.py``) prove the routing and the arithmetic
exhaustively in the pure layer. This file proves the same invariants survive
the whole path: real database, real state machine, real ledger, real receipt.

Sibling to ``test_live_provider_integration.py``, which covers P5's original
live-mode wiring. Kept separate because these tests are about what the live
path is *forbidden* to do, and that is a different question from whether it
works at all.

The one real adapter's HTTP layer is mocked with ``httpx.MockTransport``
throughout — nothing here makes a real Abstract API call. The single
deliberately-real call lives in ``scripts/live_provider_smoke.py``.
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

from arie.config import LiveBudgetConfig, LiveProviderConfig
from arie.core.types import LeadStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import ClaimedJob
from arie.jobs.worker import JobContext, JobHandler
from arie.live.budget import DAILY_BUDGET_EXHAUSTED, PER_LEAD_BUDGET_EXHAUSTED, LiveSpendGuard
from arie.live.safety import FORBIDDEN_LIVE_STATUSES, LIVE_GUARD_REASON, PERMITTED_LIVE_STATUSES
from arie.providers.live_abstract import AbstractCompanyEnrichmentProvider

pytestmark = pytest.mark.integration

_TEST_WORKER_ID = "live-v1-foundation-it"
"""Marks a job this suite owns, so a stolen one is visibly stolen."""


def _strong_lead(request: httpx.Request) -> httpx.Response:
    """A response engineered to make the policy *want* to auto-route.

    120 employees sits in the scorer's top size tier (20.0 points) and
    "Computer Software" normalizes to its best industry (15.0). Any autonomy
    the guard fails to suppress shows up on this lead first.
    """
    return httpx.Response(200, json={"employee_count": 120, "industry": "Computer Software"})


def _mock_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AbstractCompanyEnrichmentProvider:
    return AbstractCompanyEnrichmentProvider(
        config=LiveProviderConfig(api_key="test-key", cost_usd_per_call=0.002),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def live_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    """One pool for the whole module, deliberately.

    A function-scoped pool opens and closes a fresh set of backend sessions per
    test. Against a remote *pooled* database (Supabase's transaction pooler)
    a dozen of those, layered on top of the per-test queue and API pools,
    exhausts the session budget — and the symptom is not a clean connection
    error but a job left `processing` with its lease held, which reads as a
    guard failure. Sharing one small pool removes the churn without weakening
    anything: the tests share a database anyway.
    """
    pool = ConnectionPool(migrated_database, min_size=1, max_size=4, open=True)
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
    prefix: str,
    mode: str = "normal",
) -> dict[str, Any]:
    domain = f"{prefix}-{uuid.uuid4().hex[:10]}.test"
    email = f"nobody@{domain}"
    cleanup.domains.append(domain)
    cleanup.emails.append(email)
    response = api_client.post(
        "/leads",
        json={
            "source": source_for("live-v1"),
            "email": email,
            "external_ref": f"live-v1-{uuid.uuid4().hex[:12]}",
            "company_domain": domain,
            "mode": mode,
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))
    return body


def _take_ownership(db_conn: psycopg.Connection, job_id: str) -> None:
    """Claim this test's own job before anything else can, or skip loudly.

    **Why this suite does not drive the real worker loop.** The configured
    database is shared with a *deployed* ARIE worker — visible in
    ``jobs.locked_by`` as a container hostname rather than a test worker id —
    which polls for ``compute_score`` and claims a freshly ingested lead within
    about a second. That worker runs its own handlers: a different provider
    mock, a different budget config, and (until this change ships) no autonomy
    guard at all. A test racing it does not fail because the guard is broken;
    it fails, intermittently, because someone else already processed the lead.

    So the queue mechanics are left to the suites whose subject they actually
    are (``test_pipeline_integration.py``, ``test_live_provider_integration.py``)
    and this file asserts on what the *live handler* decides: the state
    transition, the evidence written, the ledger, the receipt. Every one of
    those still goes through the real database.

    Taking the row out of ``pending`` is what makes that deterministic —
    ``PostgresJobQueue._CLAIM_SELECT`` only ever selects pending rows, so once
    this UPDATE lands the deployed worker cannot touch it. If it got there
    first, the test skips with a message naming the thief rather than
    reporting a guard failure that did not happen.
    """
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
        pytest.skip(
            f"another worker ({owner}) claimed job {job_id} before this test could — "
            "a deployed worker is polling the configured DATABASE_URL. Point the "
            "integration tests at a database no deployment is attached to."
        )


def _process_lead(
    live_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    body: dict[str, Any],
) -> None:
    """Run ``compute_score`` for one lead exactly as ``arie.jobs.worker`` would.

    Mirrors ``_process_one``'s shape deliberately: one connection, the lead's
    status/version read at the start of the same transaction, the handler, then
    a single commit. What it does not do is claim from the queue — see
    :func:`_take_ownership`.
    """
    job_id = uuid.UUID(body["job_id"])
    lead_id = uuid.UUID(body["lead_id"])

    with live_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
            row = cur.fetchone()
        assert row is not None
        job = ClaimedJob(
            job_id=job_id,
            lead_id=lead_id,
            job_type="compute_score",
            attempt_count=0,
            idempotency_key=None,
        )
        handlers["compute_score"](
            JobContext(conn=conn, job=job, lead_status=LeadStatus(row[0]), lead_version=row[1])
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


def _lead_status(db_conn: psycopg.Connection, lead_id: str) -> LeadStatus:
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
    assert row is not None
    return LeadStatus(row[0])


def _run(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    prefix: str,
    mode: str = "normal",
) -> dict[str, Any]:
    handlers = _handlers_for(live_pool, runtime, handler)
    body = _ingest(api_client, cleanup_ingest, prefix=prefix, mode=mode)
    _take_ownership(db_conn, body["job_id"])
    _process_lead(live_pool, handlers, db_conn, body)
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)
    return body


# ------------------------------------------------- Phase 1: the autonomy guard --


def test_a_live_lead_is_never_auto_routed_however_strong_it_scores(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
) -> None:
    """The Phase 1 invariant, end to end.

    This lead is the best one the live provider can produce. It still lands on
    AWAITING_HUMAN with a real pending review, because the confidence model
    gating that decision was calibrated on synthetic data and has never been
    validated against real provider evidence.
    """
    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        _strong_lead,
        prefix="guard",
    )

    status = _lead_status(db_conn, body["lead_id"])
    assert status is LeadStatus.AWAITING_HUMAN
    assert status in PERMITTED_LIVE_STATUSES
    assert status not in FORBIDDEN_LIVE_STATUSES


def test_the_escalation_opens_a_real_review_and_records_why(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
) -> None:
    """A guard that parks leads where nobody is looking is not a safety
    feature. The review row must exist and be actionable, and the event must
    say the guard did this rather than leaving an unexplained escalation."""
    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        _strong_lead,
        prefix="review",
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT review_id, original_decision, responded_at "
            "FROM human_reviews WHERE lead_id = %s",
            (body["lead_id"],),
        )
        review = cur.fetchone()
        cur.execute(
            "SELECT payload FROM lead_events "
            "WHERE lead_id = %s AND event_type = 'lead:escalated' "
            "ORDER BY event_id DESC LIMIT 1",
            (body["lead_id"],),
        )
        event = cur.fetchone()

    assert review is not None
    assert review[2] is None  # genuinely pending, not auto-resolved
    assert event is not None
    assert event[0]["reason"] == LIVE_GUARD_REASON
    # The recommendation reaches the reviewer intact rather than being
    # rewritten into "ARIE wanted a human".
    assert event[0]["original_decision"] == review[1]


def test_the_receipt_separates_recommendation_from_action(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
) -> None:
    """Recommendation, autonomous action, and final outcome stay three distinct
    facts — the property `arie.api.receipt`'s docstring already promised, now
    under a guard that makes them genuinely come apart."""
    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        _strong_lead,
        prefix="receipt",
    )

    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()

    assert receipt["decision"]["recommended_action"] in ("auto_route", "reject", "escalate_human")
    assert receipt["decision"]["autonomous"] is False
    assert receipt["decision"]["autonomy_guard"] == LIVE_GUARD_REASON
    assert receipt["decision"]["final_status"] == "AWAITING_HUMAN"
    assert receipt["human_review"]["required"] is True


def test_a_shadow_live_lead_still_terminates_as_shadow_evaluated(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
) -> None:
    """Shadow mode is orthogonal to the guard and is tested first in
    `_finalize_decision`: opening a real review row for a shadow lead would
    manufacture exactly the human action shadow mode exists to avoid."""
    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        _strong_lead,
        prefix="shadow",
        mode="shadow",
    )

    assert _lead_status(db_conn, body["lead_id"]) is LeadStatus.SHADOW_EVALUATED

    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM human_reviews WHERE lead_id = %s", (body["lead_id"],))
        assert cur.fetchone() is None


# ------------------------------- Phase 2/4: normalization through the real store --


def test_provider_vocabulary_is_normalized_before_it_reaches_the_evidence_store(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
) -> None:
    """`"Computer Software"` must land in `evidence.value` as the canonical
    `"software"`, not as the literal vendor string that scored zero."""
    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        _strong_lead,
        prefix="vocab",
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT field_name, value FROM evidence WHERE entity_id = %s",
            (uuid.UUID(body["company_id"]),),
        )
        stored: dict[str, Any] = dict(cur.fetchall())

    assert stored["industry"] == "software"
    assert stored["employee_count"] == 120


def test_an_unmappable_industry_leaves_the_field_unknown_rather_than_zero(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
) -> None:
    """The other half of Phase 4, through the store and the receipt: a category
    ARIE cannot map must read as genuinely unknown, not present-and-worthless."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"employee_count": 120, "industry": "Pet Grooming Franchises"}
        )

    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        handler,
        prefix="unmapped",
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT field_name FROM evidence WHERE entity_id = %s",
            (uuid.UUID(body["company_id"]),),
        )
        stored = {row[0] for row in cur.fetchall()}

    assert stored == {"employee_count"}

    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert "industry" in receipt["evidence"]["unknown_fields"]
    assert all(item["field"] != "industry" for item in receipt["evidence"]["items"])


# --------------------------------------- Phase 6: failure and budget exhaustion --


def test_a_provider_failure_is_its_own_stop_reason_and_does_not_lose_the_lead(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
) -> None:
    """A vendor outage is not a reason to dead-letter a real lead, and it must
    not be recorded as `all_providers_called` — which would claim the evidence
    genuinely does not exist rather than that ARIE failed to fetch it."""

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream unavailable"})

    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        failing,
        prefix="fail",
    )

    assert _lead_status(db_conn, body["lead_id"]) is LeadStatus.AWAITING_HUMAN

    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert receipt["stopping"]["reason_code"] == "provider_failed"
    assert "failed to respond usably" in receipt["stopping"]["explanation"]

    # The failed attempt is ledgered at zero cost: a vendor error is not
    # billed, and hiding the attempt would make the failure invisible.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, cost_usd FROM provider_calls WHERE lead_id = %s", (body["lead_id"],)
        )
        calls = cur.fetchall()
    assert [(row[0], float(row[1])) for row in calls] == [("error", 0.0)]


def test_a_provider_timeout_behaves_the_same_way(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
) -> None:
    def timing_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        timing_out,
        prefix="timeout",
    )

    assert _lead_status(db_conn, body["lead_id"]) is LeadStatus.AWAITING_HUMAN
    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert receipt["stopping"]["reason_code"] == "provider_failed"


def test_an_exhausted_per_lead_budget_stops_before_the_call_is_made(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 6's headline requirement: *no live provider call occurs* if it
    would exceed the cap. Proved by counting requests at the transport, not by
    inspecting a ledger row after the money is already gone."""
    monkeypatch.setattr(
        "arie.live.budget.LIVE_BUDGET", LiveBudgetConfig(daily_usd=1.0, per_lead_usd=0.0)
    )

    calls_made = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls_made
        calls_made += 1
        return _strong_lead(request)

    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        counting,
        prefix="budget",
    )

    assert calls_made == 0

    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert receipt["stopping"]["reason_code"] == PER_LEAD_BUDGET_EXHAUSTED
    assert receipt["providers"]["called"] == []
    # Budget exhaustion stops acquisition *and* requires a human — it never
    # silently decides the lead on whatever evidence it happened to have.
    assert _lead_status(db_conn, body["lead_id"]) is LeadStatus.AWAITING_HUMAN


def test_an_exhausted_daily_budget_stops_before_the_call_is_made(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-lead headroom, daily cap exhausted. The refusal must name the *daily*
    cap — an operator whose whole account is paused needs to know that, not
    that one lead happened to be expensive.

    Constructing this case takes a measurement rather than a constant, because
    `LiveBudgetConfig` refuses a per-lead cap above the daily one (a config
    where one lead can drain the day makes the daily cap decorative). So: read
    what the ledger says has been spent today, then set both caps to exactly
    that. A fresh lead has spent nothing, so the per-lead cap has room; the
    account has spent the whole day's allowance, so the daily cap does not.
    """
    baseline = LiveSpendGuard(live_pool, LiveBudgetConfig()).daily_spent_usd()
    spent_today = float(baseline)
    if spent_today < 0.002:
        # Nothing has been spent today yet on this database. Spend one call so
        # there is a real, non-zero daily total to cap against, rather than
        # asserting on a degenerate zero-budget configuration.
        _run(
            api_client,
            cleanup_ingest,
            cleanup_evidence,
            db_conn,
            live_pool,
            runtime,
            _strong_lead,
            prefix="daily-seed",
        )
        spent_today = float(LiveSpendGuard(live_pool, LiveBudgetConfig()).daily_spent_usd())

    assert spent_today >= 0.002
    monkeypatch.setattr(
        "arie.live.budget.LIVE_BUDGET",
        LiveBudgetConfig(daily_usd=spent_today, per_lead_usd=spent_today),
    )

    calls_made = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls_made
        calls_made += 1
        return _strong_lead(request)

    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        counting,
        prefix="daily",
    )

    assert calls_made == 0
    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert receipt["stopping"]["reason_code"] == DAILY_BUDGET_EXHAUSTED
    assert _lead_status(db_conn, body["lead_id"]) is LeadStatus.AWAITING_HUMAN


def test_a_generous_budget_does_not_block_an_ordinary_call(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    live_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. A cap that blocks everything would pass every test above
    while making live mode useless."""
    monkeypatch.setattr(
        "arie.live.budget.LIVE_BUDGET", LiveBudgetConfig(daily_usd=100.0, per_lead_usd=1.0)
    )

    calls_made = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls_made
        calls_made += 1
        return _strong_lead(request)

    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        runtime,
        counting,
        prefix="allowed",
    )

    assert calls_made == 1
    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert receipt["stopping"]["reason_code"] not in (
        PER_LEAD_BUDGET_EXHAUSTED,
        DAILY_BUDGET_EXHAUSTED,
    )


def test_the_spend_guard_reads_the_real_ledger(live_pool: ConnectionPool) -> None:
    """The SQL itself, against a real database — the unit suite drives the
    arithmetic through a fake pool and cannot catch a broken query."""
    guard = LiveSpendGuard(live_pool, LiveBudgetConfig(daily_usd=2.0, per_lead_usd=0.05))

    daily = guard.daily_spent_usd()
    assert daily >= 0

    # An unknown lead has spent nothing — COALESCE, not a None that would blow
    # up the comparison.
    allowance = guard.allowance(lead_id=uuid.uuid4(), estimated_cost_usd=0.002)
    assert allowance.lead_spent_usd == 0
    assert allowance.daily_spent_usd == daily
