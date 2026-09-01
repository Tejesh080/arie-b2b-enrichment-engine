"""The worker → review-notification seam, against a real database.

**The bug this file exists for.** `run_worker_cycle` used to notify only when
the handler *returned* `AWAITING_HUMAN`. The simulated `compute_score`
handler — the only one the deployment runs — walks the state machine hop by
hop and returns `None` by design, so the condition silently excluded the
entire production path. Leads reached `AWAITING_HUMAN` with a pending
`human_reviews` row and no notification ever fired, with nothing in the logs
to say so. It was found by the M6 production canary, not by a test, which is
why the coverage lives here now.

The tests drive the real handler through the real worker loop rather than
asserting on the condition directly: the defect was precisely that the
condition looked right in isolation and did not match the handler it guarded.

Requires TEST_DATABASE_URL; skipped otherwise (see conftest.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, source_for

import arie.jobs.worker as worker_module
from arie.core.types import LeadStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import (
    SimulatedEnrichmentRuntime,
    build_handlers,
    build_runtime,
    decision_route,
)
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobContext, JobHandler, run_worker_cycle
from arie.policy.base import EvidenceCache, PolicyOutcome, RunContext
from arie.providers.simulated import CallLedger

pytestmark = pytest.mark.integration

_MAX_CYCLES = 6


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def seam_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=6, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def handlers(
    runtime: SimulatedEnrichmentRuntime, seam_pool: ConnectionPool
) -> dict[str, JobHandler]:
    return build_handlers(seam_pool, runtime=runtime, provider_mode="simulated")


@pytest.fixture
def notified(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Record every lead id the worker asks about, without sending anything.

    Patched in `arie.jobs.worker`'s namespace, so the real
    `notify_review_required` — and its dedup insert — stays untouched for the
    tests below that need the genuine article.
    """
    seen: list[uuid.UUID] = []
    monkeypatch.setattr(
        worker_module, "notify_review_required", lambda pool, *, lead_id: seen.append(lead_id)
    )
    return seen


def _outcome(runtime: SimulatedEnrichmentRuntime, lead: EvalLead) -> PolicyOutcome:
    ctx = RunContext(registry=runtime.registry, ledger=CallLedger(), cache=EvidenceCache())
    return runtime.policy.run(lead, ctx)


def _corpus_lead_routing_to(
    runtime: SimulatedEnrichmentRuntime, leads: list[EvalLead], wanted: str
) -> EvalLead:
    for lead in leads:
        if lead.split != "test":
            continue
        outcome = _outcome(runtime, lead)
        if decision_route(outcome.decision, outcome.autonomous) == wanted:
            return lead
    raise AssertionError(f"no test-split corpus lead routes to {wanted!r}")


def _ingest(api_client: TestClient, cleanup: IngestCleanup, lead: EvalLead) -> dict[str, Any]:
    cleanup.domains.append(lead.company.canonical_domain)
    cleanup.emails.append(lead.person.email)
    response = api_client.post(
        "/leads",
        json={
            "source": source_for("review-seam"),
            "email": lead.person.email,
            "external_ref": f"seam-{uuid.uuid4().hex[:12]}",
            "company_domain": lead.company.canonical_domain,
            "company_name": lead.company.legal_name,
            "full_name": lead.person.full_name,
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))
    return body


def _drive(
    job_queue: PostgresJobQueue,
    seam_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    job_id: str,
) -> str:
    for _ in range(_MAX_CYCLES):
        run_worker_cycle(
            job_queue,
            seam_pool,
            handlers,
            worker_id=f"seam-it-{uuid.uuid4().hex[:8]}",
            batch_size=3,
            job_types=["compute_score"],
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        assert row is not None
        if row[0] not in ("pending", "processing"):
            return str(row[0])
    raise AssertionError("job never left pending/processing")


def _run_until_claimed(
    job_queue: PostgresJobQueue,
    seam_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    job_id: Any,
) -> bool:
    """Cycle until *our* job comes back done. The queue is shared, so a single
    cycle legitimately returns somebody else's job of the same type — the same
    caveat tests/integration/conftest.py already documents."""
    for _ in range(_MAX_CYCLES):
        for result in run_worker_cycle(
            job_queue,
            seam_pool,
            handlers,
            worker_id=f"seam-it-{uuid.uuid4().hex[:8]}",
            batch_size=5,
            job_types=["compute_score"],
        ):
            if result.job_id == job_id:
                return result.outcome == "done"
    return False


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


# ------------------------------------------- the path the deployment runs --


def test_simulated_mode_reaching_awaiting_human_notifies(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    job_queue: PostgresJobQueue,
    seam_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    runtime: SimulatedEnrichmentRuntime,
    leads: list[EvalLead],
    notified: list[uuid.UUID],
) -> None:
    """The regression. The simulated handler returns `None` after applying its
    own transitions, so nothing about its return value says "awaiting human" —
    the worker has to consult the database, and this proves it does."""
    lead = _corpus_lead_routing_to(runtime, leads, "escalate_human")
    body = _ingest(api_client, cleanup_ingest, lead)
    lead_id = uuid.UUID(body["lead_id"])

    assert _drive(job_queue, seam_pool, handlers, db_conn, body["job_id"]) == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (lead_id,))
        status = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM human_reviews WHERE lead_id = %s AND responded_at IS NULL",
            (lead_id,),
        )
        pending = cur.fetchone()
    assert pending is not None
    pending_reviews = pending[0]

    assert status is not None and status[0] == LeadStatus.AWAITING_HUMAN
    assert pending_reviews == 1, "precondition: the walk opened exactly one pending review"
    assert lead_id in notified, (
        "a lead that ended AWAITING_HUMAN through the simulated handler's own "
        "internal walk must still reach notify_review_required"
    )


def test_a_handler_that_returns_none_is_asked_even_when_no_review_opened(
    seam_pool: ConnectionPool,
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    make_lead: Any,
    notified: list[uuid.UUID],
) -> None:
    """`None` means "I applied my own transitions", not "nothing happened", so
    the worker cannot rule out a review from the return value alone. Asking
    costs one indexed lookup; `notify_review_required` no-ops on its own."""
    lead_id, _version = make_lead(source=source_for("review-seam"))
    job_id = job_queue.enqueue(job_type="compute_score", lead_id=lead_id).job_id

    def applies_nothing(ctx: JobContext) -> LeadStatus | None:
        return None

    assert _run_until_claimed(job_queue, seam_pool, {"compute_score": applies_nothing}, job_id)
    assert lead_id in notified


# ----------------------------------------- transitions that open no review --


@pytest.mark.parametrize("route", ["auto_route", "reject"])
def test_a_non_review_outcome_does_not_notify(
    route: str,
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    job_queue: PostgresJobQueue,
    seam_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    runtime: SimulatedEnrichmentRuntime,
    leads: list[EvalLead],
) -> None:
    """The widened condition must not turn every completed job into a
    notification. This uses the *real* `notify_review_required`, so it asserts
    the end state — no dedup row — rather than that the worker asked."""
    lead = _corpus_lead_routing_to(runtime, leads, route)
    body = _ingest(api_client, cleanup_ingest, lead)
    lead_id = uuid.UUID(body["lead_id"])

    assert _drive(job_queue, seam_pool, handlers, db_conn, body["job_id"]) == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (lead_id,))
        status = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM human_review_notifications n "
            "JOIN human_reviews r USING (review_id) WHERE r.lead_id = %s",
            (lead_id,),
        )
        marker_row = cur.fetchone()
    assert marker_row is not None
    markers = marker_row[0]

    assert status is not None and status[0] != LeadStatus.AWAITING_HUMAN
    assert markers == 0, "a lead that never awaited a human must leave no notification marker"


def test_a_handler_returning_an_explicit_non_review_status_does_not_notify(
    seam_pool: ConnectionPool,
    job_queue: PostgresJobQueue,
    make_lead: Any,
    notified: list[uuid.UUID],
) -> None:
    """The fast path is preserved: a handler that names its transition tells
    the worker exactly where the lead landed (here `SCORING`, the real first
    hop out of `NEW`), so no lookup happens at all."""
    lead_id, _version = make_lead(source=source_for("review-seam"))
    job_id = job_queue.enqueue(job_type="compute_score", lead_id=lead_id).job_id

    def routes(ctx: JobContext) -> LeadStatus | None:
        return LeadStatus.SCORING

    assert _run_until_claimed(job_queue, seam_pool, {"compute_score": routes}, job_id)
    assert lead_id not in notified


# ------------------------------------------------------ dedup on re-asking --


def test_asking_twice_notifies_once(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    job_queue: PostgresJobQueue,
    seam_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    runtime: SimulatedEnrichmentRuntime,
    leads: list[EvalLead],
) -> None:
    """Widening the condition makes the worker ask more often — a retry, a
    reclaimed lease, a re-enqueued job. The dedup insert in
    `notify_review_required` is what keeps that from becoming a second email,
    so this exercises the real function twice and counts markers, not calls.
    """
    lead = _corpus_lead_routing_to(runtime, leads, "escalate_human")
    body = _ingest(api_client, cleanup_ingest, lead)
    lead_id = uuid.UUID(body["lead_id"])

    assert _drive(job_queue, seam_pool, handlers, db_conn, body["job_id"]) == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    from arie.review_notifications import notify_review_required

    for _ in range(3):
        notify_review_required(seam_pool, lead_id=lead_id)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_review_notifications n "
            "JOIN human_reviews r USING (review_id) WHERE r.lead_id = %s",
            (lead_id,),
        )
        marker_row = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM human_reviews WHERE lead_id = %s AND responded_at IS NULL",
            (lead_id,),
        )
        pending_row = cur.fetchone()
    assert marker_row is not None and pending_row is not None
    markers, pending = marker_row[0], pending_row[0]

    assert pending == 1
    assert markers == 1, "repeated asks must leave exactly one notification marker"


def test_a_lead_with_no_review_leaves_no_marker_when_asked(
    seam_pool: ConnectionPool, db_conn: psycopg.Connection, make_lead: Any
) -> None:
    """The no-op path, asserted directly: asking about a lead that never
    escalated must not invent a review or a marker."""
    from arie.review_notifications import notify_review_required

    lead_id, _version = make_lead(source=source_for("review-seam"))

    notify_review_required(seam_pool, lead_id=lead_id)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_review_notifications n "
            "JOIN human_reviews r USING (review_id) WHERE r.lead_id = %s",
            (lead_id,),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == 0
