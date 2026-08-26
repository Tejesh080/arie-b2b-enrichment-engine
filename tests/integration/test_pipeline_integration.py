"""The full production pipeline against a live database (M1 Step 12 fix).

`POST /leads` → job queue → `build_handlers`' compute_score → policy over the
simulated registry → durable evidence + ledger → state graph walk → terminal
status (or a pending human review). This is the path a fresh
`docker compose up` runs; these tests exercise the same composition against
the shared Supabase database.

Expected outcomes are not hardcoded: each test runs the same policy over the
same corpus lead *in memory* first and asserts the database ended up wherever
that run says it should — the dataset is a pure function of its seed, so this
is deterministic, and it keeps the test honest if the corpus ever changes.
Assertions stick to decision/status/confidence and avoid exact costs or call
counts, which legitimately differ when a previous run's still-fresh durable
evidence turns purchases into cache hits.

Worker cycles here claim with a `compute_score` filter against a shared
database — the same caveat tests/integration/conftest.py already documents.
Tests in this suite run sequentially and clean up their own jobs via the lead
cascade, so a claimed job is ours in practice; each loop checks *our* job's
row rather than trusting claim results.
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

from arie.core.types import LeadStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import (
    SimulatedEnrichmentRuntime,
    build_handlers,
    build_runtime,
    decision_route,
)
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobHandler, run_worker_cycle
from arie.policy.base import EvidenceCache, PolicyOutcome, RunContext
from arie.providers.simulated import CallLedger

pytestmark = pytest.mark.integration

_MAX_CYCLES = 6


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def pipeline_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=6, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def handlers(
    runtime: SimulatedEnrichmentRuntime, pipeline_pool: ConnectionPool
) -> dict[str, JobHandler]:
    return build_handlers(pipeline_pool, runtime=runtime, provider_mode="simulated")


def _expected_outcome(runtime: SimulatedEnrichmentRuntime, lead: EvalLead) -> PolicyOutcome:
    """The same policy, run in memory with a fresh cache — the ground truth."""
    ctx = RunContext(registry=runtime.registry, ledger=CallLedger(), cache=EvidenceCache())
    return runtime.policy.run(lead, ctx)


def _corpus_lead_with_route(
    runtime: SimulatedEnrichmentRuntime, leads: list[EvalLead], wanted_route: str
) -> tuple[EvalLead, PolicyOutcome]:
    """First test-split corpus lead whose in-memory outcome takes `wanted_route`."""
    for lead in leads:
        if lead.split != "test":
            continue
        outcome = _expected_outcome(runtime, lead)
        if decision_route(outcome.decision, outcome.autonomous) == wanted_route:
            return lead, outcome
    raise AssertionError(f"no test-split corpus lead routes to {wanted_route!r}")


def _ingest_corpus_lead(
    api_client: TestClient, cleanup: IngestCleanup, lead: EvalLead
) -> dict[str, Any]:
    cleanup.domains.append(lead.company.canonical_domain)
    cleanup.emails.append(lead.person.email)
    response = api_client.post(
        "/leads",
        json={
            "source": source_for("pipeline"),
            "email": lead.person.email,
            "external_ref": f"pipe-{uuid.uuid4().hex[:12]}",
            "company_domain": lead.company.canonical_domain,
            "company_name": lead.company.legal_name,
            "full_name": lead.person.full_name,
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))
    return body


def _drive_job_to_completion(
    job_queue: PostgresJobQueue,
    pipeline_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    job_id: str,
) -> str:
    """Run worker cycles until our job leaves 'pending'/'processing'; return its status."""
    for _ in range(_MAX_CYCLES):
        run_worker_cycle(
            job_queue,
            pipeline_pool,
            handlers,
            worker_id=f"pipeline-it-{uuid.uuid4().hex[:8]}",
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


def _register_ledger_and_evidence_cleanup(
    db_conn: psycopg.Connection,
    cleanup: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    lead_body: dict[str, Any],
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT idempotency_key FROM provider_calls WHERE lead_id = %s",
            (lead_body["lead_id"],),
        )
        cleanup.provider_call_keys.extend(row[0] for row in cur.fetchall())
    cleanup_evidence.append(uuid.UUID(lead_body["company_id"]))
    cleanup_evidence.append(uuid.UUID(lead_body["person_id"]))


def test_an_autonomous_lead_is_processed_to_its_terminal_status(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    pipeline_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    """The headline Step 12 fix: a lead POSTed to the API is actually processed
    through the queue by the wired handlers, end to end."""
    corpus_lead, expected = _corpus_lead_with_route(runtime, leads, "auto_route")
    body = _ingest_corpus_lead(api_client, cleanup_ingest, corpus_lead)

    job_status = _drive_job_to_completion(
        job_queue, pipeline_pool, handlers, db_conn, body["job_id"]
    )

    assert job_status == "done"
    _register_ledger_and_evidence_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (body["lead_id"],))
        lead_row = cur.fetchone()
        cur.execute(
            "SELECT event_type FROM lead_events WHERE lead_id = %s ORDER BY event_id",
            (body["lead_id"],),
        )
        events = [row[0] for row in cur.fetchall()]
        cur.execute(
            "SELECT total_score, decision_confidence FROM scores WHERE lead_id = %s",
            (body["lead_id"],),
        )
        score_row = cur.fetchone()
        cur.execute("SELECT count(*) FROM provider_calls WHERE lead_id = %s", (body["lead_id"],))
        calls_row = cur.fetchone()

    assert lead_row is not None
    assert lead_row[0] == LeadStatus.AUTO_ROUTED, (
        "the in-memory policy run says this corpus lead auto-routes"
    )
    # ingest + one hop per scaffold status + the decision branch itself
    assert events == [
        "lead:ingested",
        "policy:scoring",
        "policy:fetching_evidence",
        "policy:integrating",
        "policy:decision",
        "policy:decided",
    ]
    assert lead_row[1] == 1 + 5  # one version bump per post-ingest transition

    assert score_row is not None
    assert float(score_row[0]) == pytest.approx(expected.scoring.breakdown.total_score)
    assert float(score_row[1]) == pytest.approx(expected.confidence, abs=1e-6)

    assert calls_row is not None
    assert calls_row[0] >= 1, "the policy bought at least one provider call, ledgered durably"

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM evidence WHERE entity_id = %s", (body["company_id"],))
        evidence_row = cur.fetchone()
    assert evidence_row is not None and evidence_row[0] >= 1, (
        "purchased evidence must land durably in the evidence table"
    )


def test_a_low_confidence_lead_escalates_and_opens_a_pending_review(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    pipeline_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    """The escalation branch composes Step 11's request_review: the lead lands
    in AWAITING_HUMAN with exactly one pending review carrying the model's own
    decision as original_decision — the audit v_escalation_rate reads."""
    corpus_lead, expected = _corpus_lead_with_route(runtime, leads, "escalate_human")
    body = _ingest_corpus_lead(api_client, cleanup_ingest, corpus_lead)

    job_status = _drive_job_to_completion(
        job_queue, pipeline_pool, handlers, db_conn, body["job_id"]
    )

    assert job_status == "done"
    _register_ledger_and_evidence_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (body["lead_id"],))
        lead_row = cur.fetchone()
        cur.execute(
            "SELECT original_decision, final_decision, responded_at FROM human_reviews "
            "WHERE lead_id = %s",
            (body["lead_id"],),
        )
        reviews = cur.fetchall()
        cur.execute(
            "SELECT event_type FROM lead_events WHERE lead_id = %s ORDER BY event_id",
            (body["lead_id"],),
        )
        events = [row[0] for row in cur.fetchall()]

    assert lead_row is not None and lead_row[0] == LeadStatus.AWAITING_HUMAN
    assert len(reviews) == 1, "exactly one pending review per escalated lead (0006's index)"
    assert reviews[0][0] == str(expected.decision)
    assert reviews[0][1] is None and reviews[0][2] is None, "pending, not answered"
    assert events[-1] == "lead:escalated"


def test_the_m1_smoke_path_ingestion_through_human_review_decision(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    pipeline_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    """The M1 smoke test: ingestion -> queue -> worker -> decision -> human
    review, driven end to end through the real HTTP surface. Everything up to
    the pending review is exactly `test_a_low_confidence_lead_escalates_and_
    opens_a_pending_review` above; this test is the one that keeps going —
    `GET /reviews/{review_id}` then `POST /reviews/{review_id}/decision` —
    because nothing else in the suite exercises that leg of the path Step 11
    built (`test_human_review_integration.py` opens its own bare lead+review
    rather than arriving via ingestion and the worker, and the escalation test
    above stops at the pending review)."""
    corpus_lead, expected = _corpus_lead_with_route(runtime, leads, "escalate_human")
    body = _ingest_corpus_lead(api_client, cleanup_ingest, corpus_lead)

    job_status = _drive_job_to_completion(
        job_queue, pipeline_pool, handlers, db_conn, body["job_id"]
    )
    assert job_status == "done"
    _register_ledger_and_evidence_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT review_id FROM human_reviews WHERE lead_id = %s", (body["lead_id"],))
        review_row = cur.fetchone()
    assert review_row is not None, "the escalation branch must have opened exactly one review"
    review_id = review_row[0]

    get_response = api_client.get(f"/reviews/{review_id}")
    assert get_response.status_code == 200
    review_body = get_response.json()
    assert review_body["is_pending"] is True
    assert review_body["lead_status"] == LeadStatus.AWAITING_HUMAN
    assert review_body["original_decision"] == str(expected.decision)

    decision_response = api_client.post(
        f"/reviews/{review_id}/decision",
        json={
            "action": "approve",
            "reviewer": "smoke-test-reviewer",
            "expected_lead_version": review_body["lead_version"],
        },
    )
    assert decision_response.status_code == 200
    decision_body = decision_response.json()
    assert decision_body["already_applied"] is False
    assert decision_body["final_decision"] == "auto_route"
    assert decision_body["lead_status"] == LeadStatus.AUTO_ROUTED

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (body["lead_id"],))
        final_lead = cur.fetchone()
        cur.execute(
            "SELECT final_decision, reviewer, responded_at FROM human_reviews WHERE review_id = %s",
            (review_id,),
        )
        final_review = cur.fetchone()
        cur.execute(
            "SELECT event_type FROM lead_events WHERE lead_id = %s ORDER BY event_id",
            (body["lead_id"],),
        )
        events = [row[0] for row in cur.fetchall()]

    assert final_lead is not None
    assert final_lead[0] == LeadStatus.AUTO_ROUTED, (
        "a human 'approve' resolves to the same terminal status the automatic "
        "auto_route branch would have used"
    )
    assert final_lead[1] == decision_body["lead_version"]
    assert final_review is not None
    assert final_review[0] == "auto_route"
    assert final_review[1] == "smoke-test-reviewer"
    assert final_review[2] is not None, "responded_at must be set once decided"
    assert events[-1] == "human_review:decided"


def test_an_out_of_corpus_lead_is_synthesized_and_processed_to_a_terminal_status(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    pipeline_pool: ConnectionPool,
) -> None:
    """An identity the frozen corpus has never heard of — the public demo's
    'submit your own lead' path. Before arie.providers.synthetic these jobs
    dead-lettered by design and the lead sat in NEW forever; now the same
    pipeline synthesizes deterministic simulated evidence and lands a real
    terminal status and receipt. The in-memory synthetic run is the ground
    truth, exactly as the corpus tests above treat theirs."""
    from arie.providers.simulated import build_from_leads
    from arie.providers.synthetic import synthesize_corpus_lead

    marker = uuid.uuid4().hex[:10]
    domain = f"maple-harbor-{marker}.com"
    email = f"rowan.ellis@{domain}"

    synthetic = synthesize_corpus_lead(
        canonical_email=email,
        canonical_domain=domain,
        full_name="Rowan Ellis",
        company_name="Maple Harbor Group",
    )
    _, registry = build_from_leads([synthetic])
    expected = runtime.policy.run(
        synthetic, RunContext(registry=registry, ledger=CallLedger(), cache=EvidenceCache())
    )
    expected_status = {
        "auto_route": LeadStatus.AUTO_ROUTED,
        "reject": LeadStatus.SYNCED,
        "escalate_human": LeadStatus.AWAITING_HUMAN,
    }[decision_route(expected.decision, expected.autonomous)]

    cleanup_ingest.domains.append(domain)
    cleanup_ingest.emails.append(email)
    response = api_client.post(
        "/leads",
        json={
            "source": source_for("pipeline"),
            "email": email,
            "external_ref": f"synth-{marker}",
            "company_domain": domain,
            "company_name": "Maple Harbor Group",
            "full_name": "Rowan Ellis",
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    cleanup_ingest.lead_ids.append(uuid.UUID(body["lead_id"]))

    job_status = _drive_job_to_completion(
        job_queue, pipeline_pool, handlers, db_conn, body["job_id"]
    )
    assert job_status == "done", "out-of-corpus leads must process, not dead-letter"
    _register_ledger_and_evidence_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (body["lead_id"],))
        lead_row = cur.fetchone()
        cur.execute(
            "SELECT decision, confidence FROM decision_receipts WHERE lead_id = %s",
            (body["lead_id"],),
        )
        receipt_row = cur.fetchone()

    assert lead_row is not None and lead_row[0] == expected_status
    assert receipt_row is not None, "a synthesized lead still gets a full decision receipt"
    assert receipt_row[0] == str(expected.decision)
    assert float(receipt_row[1]) == pytest.approx(expected.confidence, abs=1e-6)


def test_a_dead_lettered_job_marks_its_lead_dead_letter(
    db_conn: psycopg.Connection,
    job_queue: PostgresJobQueue,
    pipeline_pool: ConnectionPool,
    make_lead: Any,
    cleanup_jobs: list[uuid.UUID],
) -> None:
    """When the attempt budget is spent, the lead must be told — FAILURE's
    contract in arie.statemachine.transitions — rather than sitting in NEW
    while its pollers wait forever. Driven with an empty handler registry so
    the job dead-letters deterministically on its first attempt."""
    lead_id, _version = make_lead(source=source_for("deadletter"))
    enqueued = job_queue.enqueue(lead_id=lead_id, job_type="compute_score")
    cleanup_jobs.append(enqueued.job_id)

    results = run_worker_cycle(
        job_queue,
        pipeline_pool,
        handlers={},
        worker_id=f"deadletter-it-{uuid.uuid4().hex[:8]}",
        batch_size=1,
        job_types=["compute_score"],
        max_attempts=1,
    )
    assert [r.outcome for r in results if r.job_id == enqueued.job_id] == ["dead_letter"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        job_row = cur.fetchone()
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
        cur.execute(
            "SELECT event_type FROM lead_events WHERE lead_id = %s ORDER BY event_id",
            (lead_id,),
        )
        events = [row[0] for row in cur.fetchall()]

    assert job_row is not None and job_row[0] == "dead_letter"
    assert lead_row is not None and lead_row[0] == LeadStatus.DEAD_LETTER
    assert lead_row[1] == 2, "exactly one audited transition, NEW -> DEAD_LETTER"
    assert "job:dead_letter" in events
