"""The worker loop — claims jobs and runs them to completion, atomically.

No real handlers are registered here yet. Wiring `compute_score` /
`fetch_evidence` / `integrate_evidence` / `finalize_decision` to real logic
(the scoring engine, real provider adapters, `CalibratedBoundsPolicy`) is
deliberately deferred — see `arie.statemachine.transitions`'s module
docstring for why. What this module guarantees today: whatever a handler
does, its effect on the lead and the job's own bookkeeping commit together or
not at all, and a handler that raises is retried with backoff and eventually
dead-lettered, uniformly, regardless of what job_type it was.

Since M1 Step 9 it also continues the trace that created the job. `jobs.trace_context`
carries the W3C context captured when the ingestion request enqueued the work,
and `_process_one` starts its span as a child of it — so a lead's HTTP request
and every processing attempt it caused appear in one trace, across two
processes and however much wall-clock separates them. See
`arie.observability.tracing` for why that link has to travel through the queue
row and nowhere else.
"""

from __future__ import annotations

import socket
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from arie.config import DATABASE, RUNTIME
from arie.core.types import LeadStatus
from arie.jobs.queue import ClaimedJob, PostgresJobQueue
from arie.observability.tracing import (
    configure_tracing,
    extract_trace_context,
    get_tracer,
    record_error,
    set_attributes,
    shutdown_tracing,
    traced,
)
from arie.statemachine.apply import apply_transition

_SELECT_LEAD_STATE = "SELECT status, version FROM leads WHERE lead_id = %(lead_id)s"

_TRACER = get_tracer("arie.jobs.worker")


@dataclass(frozen=True)
class JobContext:
    """Everything a handler gets: a live connection inside the work
    transaction, the claimed job, and the lead's current state, read fresh
    (a plain read, not ``FOR UPDATE``) at the start of that same transaction.

    Deliberately not locked: this is *optimistic* concurrency, per the task
    that built it — a handler may take a while (a slow provider call, once
    real handlers exist), and holding a row lock for that whole span would
    turn "optimistic" into "pessimistic" and serialize work that doesn't need
    to be. Instead, ``apply_transition`` checks the version at write time and
    raises ``OptimisticConcurrencyError`` if it moved — see
    ``arie.statemachine.apply``.
    """

    conn: psycopg.Connection
    job: ClaimedJob
    lead_status: LeadStatus | None
    lead_version: int | None


JobHandler = Callable[[JobContext], LeadStatus | None]
"""Returns the lead's new status, or None if this job type doesn't drive a
lead transition (e.g. a maintenance job with no lead_id). Raising fails the
job — the worker loop routes every exception through the same retry/backoff/
dead-letter path, regardless of job_type."""


@dataclass(frozen=True)
class CycleResult:
    job_id: uuid.UUID
    job_type: str
    outcome: str
    """"done", "retry", or "dead_letter"."""
    detail: str | None = None


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def run_worker_cycle(
    queue: PostgresJobQueue,
    pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    *,
    worker_id: str | None = None,
    batch_size: int = 1,
    job_types: Sequence[str] | None = None,
    lease_seconds: int | None = None,
    max_attempts: int | None = None,
) -> list[CycleResult]:
    """Claim and process one batch of due jobs. Returns one result per job claimed.

    Claiming is its own, already-committed transaction (``queue.claim``).
    Each claimed job then gets a fresh transaction for the actual work: run
    the handler, mark the job done, apply the lead's state transition — all
    committed together, or all rolled back together if anything raises.

    `job_types` restricts what this worker will claim, for running a pool
    dedicated to one kind of work — and for tests against a shared database,
    where an unfiltered claim would happily pick up another test's job.

    It deliberately does **not** default to ``handlers.keys()``, tempting as
    that is. A job whose type has no handler registered anywhere must still be
    claimed, failed, and eventually dead-lettered; silently declining to claim
    it would leave it pending forever, which looks identical to a backlog and
    is far harder to notice than a dead letter.
    """
    resolved_worker_id = worker_id or _default_worker_id()
    resolved_lease = lease_seconds if lease_seconds is not None else RUNTIME.worker_lease_seconds
    resolved_max_attempts = (
        max_attempts if max_attempts is not None else RUNTIME.worker_max_attempts
    )

    # The claim span stands alone rather than parenting the processing spans:
    # one claim can return a batch of jobs from unrelated traces, so making it
    # their parent would splice unrelated leads' work into whichever trace
    # happened to be claimed alongside them. Each job reattaches to its own
    # originating trace inside `_process_one`.
    with traced(
        _TRACER, "job.claim", attributes={"arie.worker_id": resolved_worker_id}
    ) as claim_span:
        claimed = queue.claim(
            worker_id=resolved_worker_id,
            job_types=job_types,
            limit=batch_size,
            lease_seconds=resolved_lease,
            max_attempts=resolved_max_attempts,
        )
        claim_span.set_attribute("arie.jobs_claimed", len(claimed))

    return [
        _process_one(queue, pool, handlers, job, max_attempts=resolved_max_attempts)
        for job in claimed
    ]


def _process_one(
    queue: PostgresJobQueue,
    pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    job: ClaimedJob,
    *,
    max_attempts: int,
) -> CycleResult:
    # Reattach to the trace that enqueued this job, if there was one. A missing
    # or unparseable context yields None, which starts a fresh trace rather
    # than failing the job — a broken trace header is an observability problem,
    # never a reason to drop work.
    parent = extract_trace_context(job.trace_context)

    with _TRACER.start_as_current_span("job.process", context=parent) as span:
        set_attributes(
            span,
            {
                "arie.job_id": job.job_id,
                "arie.job_type": job.job_type,
                "arie.lead_id": job.lead_id,
                "arie.job.attempt": job.attempt_count,
            },
        )

        handler = handlers.get(job.job_type)
        if handler is None:
            error = f"no handler registered for job_type {job.job_type!r}"
            record_error(span, error)
            result = _fail(queue, pool, job, max_attempts=max_attempts, error=error)
            span.set_attribute("arie.job.outcome", result.outcome)
            return result

        try:
            with pool.connection() as conn:
                lead_status: LeadStatus | None = None
                lead_version: int | None = None
                if job.lead_id is not None:
                    with conn.cursor() as cur:
                        cur.execute(_SELECT_LEAD_STATE, {"lead_id": job.lead_id})
                        row = cur.fetchone()
                        if row is not None:
                            lead_status, lead_version = LeadStatus(row[0]), row[1]

                context = JobContext(
                    conn=conn, job=job, lead_status=lead_status, lead_version=lead_version
                )
                new_status = handler(context)

                queue.complete(conn, job.job_id)

                if new_status is not None:
                    if job.lead_id is None or lead_version is None:
                        raise ValueError(
                            f"handler for {job.job_type!r} returned a new status "
                            "but job has no lead"
                        )
                    apply_transition(
                        conn,
                        lead_id=job.lead_id,
                        expected_version=lead_version,
                        new_status=new_status,
                        event_type=f"job:{job.job_type}",
                        payload={"job_id": str(job.job_id)},
                    )
                    span.set_attribute("arie.lead.new_status", str(new_status))

                conn.commit()

            span.set_attribute("arie.job.outcome", "done")
            return CycleResult(job_id=job.job_id, job_type=job.job_type, outcome="done")

        except Exception as exc:
            # The span is marked ERROR even though the exception stops here: a
            # job that is retried is still a job that failed, and swallowing it
            # for tracing purposes would hide every transient failure until it
            # became a dead letter.
            record_error(span, exc)
            result = _fail(queue, pool, job, max_attempts=max_attempts, error=str(exc))
            span.set_attribute("arie.job.outcome", result.outcome)
            return result


def _fail(
    queue: PostgresJobQueue,
    pool: ConnectionPool,
    job: ClaimedJob,
    *,
    max_attempts: int,
    error: str,
) -> CycleResult:
    with pool.connection() as conn:
        outcome = queue.fail(conn, job.job_id, error=error, max_attempts=max_attempts)
        conn.commit()
    result_kind = "dead_letter" if outcome.status == "dead_letter" else "retry"
    return CycleResult(job_id=job.job_id, job_type=job.job_type, outcome=result_kind, detail=error)


def main() -> int:
    if not DATABASE.url:
        print("DATABASE_URL is not set — see .env.example")
        return 1

    configure_tracing()
    pool = ConnectionPool(DATABASE.url, min_size=1, max_size=5, open=True)
    queue = PostgresJobQueue(pool)
    handlers: dict[str, JobHandler] = {}  # real handlers not wired yet — see module docstring

    print(
        f"Worker starting (poll interval {RUNTIME.worker_poll_interval_sec}s, "
        f"{len(handlers)} handler(s) registered). Ctrl-C to stop."
    )
    try:
        while True:
            for result in run_worker_cycle(queue, pool, handlers):
                suffix = f" ({result.detail})" if result.detail else ""
                print(f"{result.job_type} {result.job_id}: {result.outcome}{suffix}")
            time.sleep(RUNTIME.worker_poll_interval_sec)
    except KeyboardInterrupt:
        print("Worker stopping.")
        return 0
    finally:
        queue.close()
        pool.close()
        shutdown_tracing()


if __name__ == "__main__":
    raise SystemExit(main())
