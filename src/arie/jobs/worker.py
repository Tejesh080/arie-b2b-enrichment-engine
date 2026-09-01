"""The worker loop — claims jobs and runs them to completion, atomically.

Production handlers live in `arie.jobs.handlers` and are wired in by
`main()`: `compute_score` runs `CalibratedBoundsPolicy` over the simulated
provider registry and drives the lead through the state graph itself — see
that module's docstring for why it is one handler rather than four. This
module stays agnostic about what handlers do; what it guarantees is that
whatever a handler does, its effect on the lead and the job's own bookkeeping
commit together or not at all, and a handler that raises is retried with
backoff and eventually dead-lettered, uniformly, regardless of job_type.

Since M1 Step 9 it also continues the trace that created the job. `jobs.trace_context`
carries the W3C context captured when the ingestion request enqueued the work,
and `_process_one` starts its span as a child of it — so a lead's HTTP request
and every processing attempt it caused appear in one trace, across two
processes and however much wall-clock separates them. See
`arie.observability.tracing` for why that link has to travel through the queue
row and nowhere else.
"""

from __future__ import annotations

import contextlib
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType

import psycopg
from psycopg_pool import ConnectionPool

from arie.config import DATABASE, RUNTIME, WORKER_HEARTBEAT
from arie.core.types import LeadStatus
from arie.jobs.heartbeat import beat, new_worker_instance_id
from arie.jobs.queue import ClaimedJob, JobOwnershipError, PostgresJobQueue
from arie.observability.tracing import (
    configure_tracing,
    extract_trace_context,
    get_tracer,
    record_error,
    set_attributes,
    shutdown_tracing,
    traced,
)
from arie.review_notifications import notify_review_required
from arie.statemachine.apply import OptimisticConcurrencyError, apply_transition
from arie.statemachine.transitions import job_type_for

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
    """"done", "retry", "dead_letter", or "lost_lease" (the ownership-fencing
    guard fired — see `JobOwnershipError` — and this attempt touched nothing)."""
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
        _process_one(
            queue,
            pool,
            handlers,
            job,
            worker_id=resolved_worker_id,
            max_attempts=resolved_max_attempts,
        )
        for job in claimed
    ]


def _process_one(
    queue: PostgresJobQueue,
    pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    job: ClaimedJob,
    *,
    worker_id: str,
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
            result = _fail(
                queue, pool, job, worker_id=worker_id, max_attempts=max_attempts, error=error
            )
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

                # Ownership-fenced: raises JobOwnershipError, caught below,
                # if this worker's lease was reclaimed while the handler ran.
                # That must stop everything after it too — including the
                # lead transition next — which is why it runs before that,
                # not after.
                queue.complete(conn, job.job_id, worker_id=worker_id)

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

            if new_status is LeadStatus.AWAITING_HUMAN and job.lead_id is not None:
                # Best-effort, after the transaction that opened the review
                # has actually committed — see arie.review_notifications'
                # own docstring for why this can never run inside it.
                notify_review_required(pool, lead_id=job.lead_id)

            span.set_attribute("arie.job.outcome", "done")
            return CycleResult(job_id=job.job_id, job_type=job.job_type, outcome="done")

        except JobOwnershipError as exc:
            # Not a handler failure to retry -- retrying here would mean
            # writing to a job row another worker now owns. The transaction
            # above never committed, so nothing this attempt did (including
            # anything the handler itself wrote via `ctx.conn`) survives;
            # whoever holds the lease now is solely responsible for this job.
            record_error(span, exc)
            result = CycleResult(
                job_id=job.job_id, job_type=job.job_type, outcome="lost_lease", detail=str(exc)
            )
            span.set_attribute("arie.job.outcome", result.outcome)
            return result

        except Exception as exc:
            # The span is marked ERROR even though the exception stops here: a
            # job that is retried is still a job that failed, and swallowing it
            # for tracing purposes would hide every transient failure until it
            # became a dead letter.
            record_error(span, exc)
            result = _fail(
                queue, pool, job, worker_id=worker_id, max_attempts=max_attempts, error=str(exc)
            )
            span.set_attribute("arie.job.outcome", result.outcome)
            return result


def _fail(
    queue: PostgresJobQueue,
    pool: ConnectionPool,
    job: ClaimedJob,
    *,
    worker_id: str,
    max_attempts: int,
    error: str,
) -> CycleResult:
    """The single chokepoint that calls `queue.fail()` — both `_process_one`
    call sites (no handler registered; the handler raised) go through here,
    so `JobOwnershipError` from a lease lost between claiming and now only
    needs handling in one place.
    """
    try:
        with pool.connection() as conn:
            outcome = queue.fail(
                conn, job.job_id, worker_id=worker_id, error=error, max_attempts=max_attempts
            )
            if outcome.status == "dead_letter" and job.lead_id is not None:
                # The attempt budget is spent — nothing will ever advance this
                # lead again, so tell its pollers that explicitly instead of
                # leaving them in a "not finalized yet" loop forever (the exact
                # contract FAILURE's docstring in arie.statemachine.transitions
                # states). Same transaction as the dead-letter itself: the job
                # and the lead can't disagree about whether processing is over.
                _mark_lead_dead_lettered(conn, lead_id=job.lead_id, job=job, error=error)
            conn.commit()
    except JobOwnershipError as exc:
        return CycleResult(
            job_id=job.job_id, job_type=job.job_type, outcome="lost_lease", detail=str(exc)
        )
    result_kind = "dead_letter" if outcome.status == "dead_letter" else "retry"
    return CycleResult(job_id=job.job_id, job_type=job.job_type, outcome=result_kind, detail=error)


def _mark_lead_dead_lettered(
    conn: psycopg.Connection, *, lead_id: uuid.UUID, job: ClaimedJob, error: str
) -> None:
    """Transition a dead-lettered job's lead to DEAD_LETTER, if the queue
    still owns its progress.

    Guarded by ``job_type_for``: only statuses the queue auto-advances (the
    NEW→DECISION scaffold) can be stranded by a dead letter. A lead already
    terminal, awaiting a human, or otherwise outside the scaffold is not this
    job's to move. Best-effort by design — an ``OptimisticConcurrencyError``
    (someone else advanced the lead between the read and the write) means the
    lead is demonstrably not stranded, so losing the race is success.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_LEAD_STATE, {"lead_id": lead_id})
        row = cur.fetchone()
    if row is None:
        return
    status, version = LeadStatus(row[0]), row[1]
    if job_type_for(status) is None:
        return
    with contextlib.suppress(OptimisticConcurrencyError):
        apply_transition(
            conn,
            lead_id=lead_id,
            expected_version=version,
            new_status=LeadStatus.DEAD_LETTER,
            event_type="job:dead_letter",
            payload={"job_id": str(job.job_id), "job_type": job.job_type, "error": error[:500]},
        )


def install_graceful_shutdown(stop: threading.Event) -> None:
    """Route SIGTERM the same way SIGINT already is: set `stop` and let the
    current cycle finish, rather than each signal's own default disposition.

    SIGTERM's default disposition is immediate termination — no exception, no
    unwind, no `finally`. Left unhandled, `docker stop`/`compose down` (which
    send SIGTERM, not Ctrl-C) would kill the process before ``main``'s
    ``finally`` block below ever ran, leaking the pool's open connections
    however the container runtime cleans them up rather than closing them
    deliberately. Registering the same handler for SIGINT too means Ctrl-C
    and SIGTERM now behave identically, and neither raises ``KeyboardInterrupt``
    mid-cycle the way Python's default SIGINT handler could — the loop below
    always finishes processing whatever ``run_worker_cycle`` already claimed
    before checking whether to stop.
    """

    def _handler(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> int:
    if not DATABASE.url:
        print("DATABASE_URL is not set — see .env.example")
        return 1

    configure_tracing()
    pool = ConnectionPool(DATABASE.url, min_size=1, max_size=5, open=True)
    queue = PostgresJobQueue(pool)

    # Imported here, not at module top: build_handlers pulls in the dataset
    # generator and the sklearn fitting stack, which nothing else touching
    # this module (tests, the API process) should pay for.
    from arie.jobs.handlers import build_handlers

    print("Building enrichment runtime (seeded dataset + confidence model refit)…")
    handlers = build_handlers(pool)

    print(
        f"Worker starting (poll interval {RUNTIME.worker_poll_interval_sec}s, "
        f"{len(handlers)} handler(s) registered: {', '.join(sorted(handlers))}). "
        "Ctrl-C or SIGTERM to stop."
    )

    stop = threading.Event()
    install_graceful_shutdown(stop)

    worker_instance_id = new_worker_instance_id()
    started_at = datetime.now(UTC)
    beat(pool, worker_instance_id=worker_instance_id, started_at=started_at)
    last_heartbeat = time.monotonic()

    try:
        while not stop.is_set():
            for result in run_worker_cycle(queue, pool, handlers):
                suffix = f" ({result.detail})" if result.detail else ""
                print(f"{result.job_type} {result.job_id}: {result.outcome}{suffix}")

            # Throttled independently of the poll interval (which can be as
            # tight as 2s) so a busy worker doesn't hammer the heartbeat
            # table every cycle — see arie.config.WorkerHeartbeatConfig.
            now = time.monotonic()
            if now - last_heartbeat >= WORKER_HEARTBEAT.interval_seconds:
                beat(pool, worker_instance_id=worker_instance_id, started_at=started_at)
                last_heartbeat = now

            # Interruptible: returns as soon as a signal sets `stop`, instead
            # of finishing out the full poll interval before noticing.
            stop.wait(RUNTIME.worker_poll_interval_sec)
    finally:
        print("Worker stopping.")
        queue.close()
        pool.close()
        shutdown_tracing()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
