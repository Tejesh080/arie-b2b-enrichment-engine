"""Trace propagation across the process boundary (M1 Step 9).

The claim under test: an HTTP request, the job it enqueues, and the worker that
later processes that job all land in **one trace**, even though the API and the
worker share no memory and are separated by an arbitrary amount of wall clock.
The only thing connecting them is `jobs.trace_context`, so these tests check
that column really is carrying the link — not that spans merely exist.

The failure modes they rule out are the quiet ones: a worker that starts a fresh
trace per job (so a slow lead's request and its processing can never be seen
together), and a broken trace header that fails the job instead of being ignored.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, source_for

from arie.api.main import AppState, create_app
from arie.config import ObservabilityConfig
from arie.core.types import LeadStatus
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobContext, JobHandler, run_worker_cycle
from arie.statemachine.transitions import job_type_for, next_status

pytestmark = pytest.mark.integration


def _compute_score_job_type() -> str:
    """The job type ingestion enqueues, taken from the state graph rather than
    hardcoded — if the graph's first step is renamed, these tests follow it."""
    job_type = job_type_for(LeadStatus.NEW)
    assert job_type is not None, "NEW must have outgoing work"
    return job_type


_COMPUTE_SCORE: str = _compute_score_job_type()


def _identity(cleanup: IngestCleanup) -> tuple[str, str]:
    domain = f"trace-{uuid.uuid4().hex[:12]}.test"
    email = f"ada@{domain}"
    cleanup.domains.append(domain)
    cleanup.emails.append(email)
    return domain, email


def _payload(email: str, domain: str) -> dict[str, Any]:
    return {
        "source": source_for("trace"),
        "email": email,
        "company_domain": domain,
        "external_ref": f"crm-{uuid.uuid4().hex[:10]}",
    }


def _advance(context: JobContext) -> LeadStatus | None:
    """A handler that does the smallest real thing: move the lead one step.

    Real handlers (scoring, evidence fetching, the policy) are Step 10 and
    beyond; what these tests need is a handler that genuinely completes, so the
    span covers a committed transaction rather than a no-op.
    """
    assert context.lead_status is not None
    return next_status(context.lead_status)


def _by_name(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    matches = [span for span in spans.get_finished_spans() if span.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} span, got {len(matches)}"
    return matches[0]


def _trace_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return int(span.context.trace_id)


def _run_worker_for(
    job_id: uuid.UUID,
    queue: PostgresJobQueue,
    pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    job_types: list[str],
) -> Any:
    """Drive the worker until it processes `job_id`, and return that result.

    Selecting by job id rather than trusting "the only result": the database is
    shared, and a claim is allowed to return somebody else's job of the same
    type without that being a failure of anything under test here.
    """
    for _ in range(5):
        results = run_worker_cycle(queue, pool, handlers, batch_size=5, job_types=job_types)
        for result in results:
            if result.job_id == job_id:
                return result
        if not results:
            break
    raise AssertionError(f"worker never claimed job {job_id}")


def test_ingestion_stores_the_requests_trace_context_on_the_job(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    spans: InMemorySpanExporter,
) -> None:
    domain, email = _identity(cleanup_ingest)

    response = api_client.post("/leads", json=_payload(email, domain))
    lead_id = uuid.UUID(response.json()["lead_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    with db_conn.cursor() as cur:
        cur.execute("SELECT trace_context FROM jobs WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()

    assert row is not None
    carrier = row[0]
    assert carrier is not None, "the job must carry the trace that created it"
    assert "traceparent" in carrier

    # The stored context is the enqueue span's, and it belongs to the request's trace.
    enqueue_span = _by_name(spans, "job.enqueue")
    stored_trace_id = int(carrier["traceparent"].split("-")[1], 16)
    assert stored_trace_id == _trace_id(enqueue_span)
    assert _trace_id(enqueue_span) == _trace_id(_by_name(spans, "lead.ingest"))


def test_worker_processing_continues_the_trace_that_enqueued_the_job(
    api_client: TestClient,
    app_state: AppState,
    worker_pool: ConnectionPool,
    cleanup_ingest: IngestCleanup,
    spans: InMemorySpanExporter,
) -> None:
    """The headline guarantee of Step 9's observability work.

    Two processes, one trace: `job.process` is a child of `job.enqueue`, so a
    lead's whole lifecycle is one thing to look at rather than two unrelated
    traces nobody can join up after the fact.
    """
    domain, email = _identity(cleanup_ingest)

    response = api_client.post("/leads", json=_payload(email, domain))
    lead_id = uuid.UUID(response.json()["lead_id"])
    job_id = uuid.UUID(response.json()["job_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    ingest_trace_id = _trace_id(_by_name(spans, "lead.ingest"))
    enqueue_span = _by_name(spans, "job.enqueue")

    result = _run_worker_for(
        job_id,
        app_state.queue,
        worker_pool,
        {_COMPUTE_SCORE: _advance},
        [_COMPUTE_SCORE],
    )
    assert result.outcome == "done"

    process_span = _by_name(spans, "job.process")
    assert _trace_id(process_span) == ingest_trace_id, (
        "the worker must continue the request's trace, not start its own"
    )
    assert process_span.parent is not None
    assert enqueue_span.context is not None
    assert process_span.parent.span_id == enqueue_span.context.span_id

    attributes = dict(process_span.attributes or {})
    assert attributes["arie.job_type"] == _COMPUTE_SCORE
    assert attributes["arie.lead_id"] == str(lead_id)
    assert attributes["arie.job.outcome"] == "done"
    assert attributes["arie.lead.new_status"] == LeadStatus.SCORING


def test_http_server_span_and_worker_span_share_one_trace(
    app_state: AppState,
    worker_pool: ConnectionPool,
    cleanup_ingest: IngestCleanup,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full chain, with FastAPI's own instrumentation attached.

    The other tests start from `lead.ingest`; this one starts from the actual
    HTTP server span, which is what an operator would click on. Instrumentation
    is normally skipped when no OTLP endpoint is configured, so the config is
    patched to switch it on.
    """
    monkeypatch.setattr(
        "arie.api.main.OBSERVABILITY",
        ObservabilityConfig(service_name="arie-tests", otlp_endpoint="http://localhost:4318"),
    )
    domain, email = _identity(cleanup_ingest)
    app = create_app(state=app_state)

    try:
        with TestClient(app) as client:
            response = client.post("/leads", json=_payload(email, domain))

        lead_id = uuid.UUID(response.json()["lead_id"])
        job_id = uuid.UUID(response.json()["job_id"])
        cleanup_ingest.lead_ids.append(lead_id)

        result = _run_worker_for(
            job_id,
            app_state.queue,
            worker_pool,
            {_COMPUTE_SCORE: _advance},
            [_COMPUTE_SCORE],
        )
        assert result.outcome == "done"

        server_spans = [span for span in spans.get_finished_spans() if span.kind is SpanKind.SERVER]
        assert server_spans, "FastAPI instrumentation should have produced a server span"

        process_span = _by_name(spans, "job.process")
        assert _trace_id(server_spans[0]) == _trace_id(process_span)
    finally:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.uninstrument_app(app)


def test_a_job_enqueued_outside_a_trace_still_processes(
    app_state: AppState,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    spans: InMemorySpanExporter,
) -> None:
    """A maintenance job, a backfill, anything enqueued before tracing was on.

    NULL `trace_context` means "start a new trace", never "fail".
    """
    job_type = f"untraced_{uuid.uuid4().hex[:8]}"
    enqueued = app_state.queue.enqueue(lead_id=None, job_type=job_type)

    with db_conn.cursor() as cur:
        cur.execute("SELECT trace_context FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        row = cur.fetchone()
    assert row is not None and row[0] is None

    try:
        result = _run_worker_for(
            enqueued.job_id,
            app_state.queue,
            worker_pool,
            {job_type: lambda _context: None},
            [job_type],
        )
        assert result.outcome == "done"

        process_span = _by_name(spans, "job.process")
        assert process_span.parent is None, "no parent to attach to, so it roots its own trace"
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        db_conn.commit()


def test_a_corrupt_trace_context_does_not_fail_the_job(
    app_state: AppState,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    spans: InMemorySpanExporter,
) -> None:
    """Observability must never be able to drop work.

    A malformed `traceparent` — a truncated header, a future spec version, a
    hand-edited row — is ignored and the job runs normally.
    """
    job_type = f"corrupt_{uuid.uuid4().hex[:8]}"
    enqueued = app_state.queue.enqueue(lead_id=None, job_type=job_type)

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET trace_context = %s::jsonb WHERE job_id = %s",
            ('{"traceparent": "utter-nonsense"}', enqueued.job_id),
        )
    db_conn.commit()

    try:
        result = _run_worker_for(
            enqueued.job_id,
            app_state.queue,
            worker_pool,
            {job_type: lambda _context: None},
            [job_type],
        )
        assert result.outcome == "done"
        assert _by_name(spans, "job.process").parent is None
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        db_conn.commit()


def test_a_failing_job_produces_an_error_span_in_the_requests_trace(
    api_client: TestClient,
    app_state: AppState,
    worker_pool: ConnectionPool,
    cleanup_ingest: IngestCleanup,
    spans: InMemorySpanExporter,
) -> None:
    """A retried job is still a job that failed.

    The worker swallows the handler's exception by design — that is what
    retry/backoff *is* — so nothing would mark the span ERROR unless it is done
    deliberately, and every transient failure would be invisible until it
    became a dead letter.
    """
    domain, email = _identity(cleanup_ingest)
    response = api_client.post("/leads", json=_payload(email, domain))
    lead_id = uuid.UUID(response.json()["lead_id"])
    job_id = uuid.UUID(response.json()["job_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    ingest_trace_id = _trace_id(_by_name(spans, "lead.ingest"))

    def _explode(_context: JobContext) -> LeadStatus | None:
        raise RuntimeError("handler exploded")

    result = _run_worker_for(
        job_id, app_state.queue, worker_pool, {_COMPUTE_SCORE: _explode}, [_COMPUTE_SCORE]
    )
    assert result.outcome == "retry"

    process_span = _by_name(spans, "job.process")
    assert process_span.status.status_code is StatusCode.ERROR
    assert _trace_id(process_span) == ingest_trace_id
    assert dict(process_span.attributes or {})["arie.job.outcome"] == "retry"


def test_the_lead_is_untouched_when_its_job_fails(
    api_client: TestClient,
    app_state: AppState,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
) -> None:
    """The Step 8 transaction guarantee, still holding through the Step 9 path.

    A handler that raises after the lead was read must leave the lead exactly
    as it was — same status, same version — because the state transition and
    the job's completion commit together or not at all.
    """
    domain, email = _identity(cleanup_ingest)
    response = api_client.post("/leads", json=_payload(email, domain))
    lead_id = uuid.UUID(response.json()["lead_id"])
    job_id = uuid.UUID(response.json()["job_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    def _explode(_context: JobContext) -> LeadStatus | None:
        raise RuntimeError("handler exploded after reading the lead")

    _run_worker_for(
        job_id, app_state.queue, worker_pool, {_COMPUTE_SCORE: _explode}, [_COMPUTE_SCORE]
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead = cur.fetchone()
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (job_id,))
        job = cur.fetchone()

    assert lead == (LeadStatus.NEW, 1), "no partial transition survives a failed handler"
    assert job is not None
    assert job[0] == "pending", "and the job is queued to try again"
    assert job[1] == 1
