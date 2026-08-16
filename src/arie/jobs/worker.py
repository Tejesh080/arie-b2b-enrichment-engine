"""The worker loop — claims jobs and runs them to completion, atomically.

No real handlers are registered here yet. Wiring `compute_score` /
`fetch_evidence` / `integrate_evidence` / `finalize_decision` to real logic
(the scoring engine, real provider adapters, `CalibratedBoundsPolicy`) is
deliberately deferred — see `arie.statemachine.transitions`'s module
docstring for why. What this module guarantees today: whatever a handler
does, its effect on the lead and the job's own bookkeeping commit together or
not at all, and a handler that raises is retried with backoff and eventually
dead-lettered, uniformly, regardless of what job_type it was.
"""

from __future__ import annotations

import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from arie.config import DATABASE, RUNTIME
from arie.core.types import LeadStatus
from arie.jobs.queue import ClaimedJob, PostgresJobQueue
from arie.statemachine.apply import apply_transition

_SELECT_LEAD_STATE = "SELECT status, version FROM leads WHERE lead_id = %(lead_id)s"


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
    lease_seconds: int | None = None,
    max_attempts: int | None = None,
) -> list[CycleResult]:
    """Claim and process one batch of due jobs. Returns one result per job claimed.

    Claiming is its own, already-committed transaction (``queue.claim``).
    Each claimed job then gets a fresh transaction for the actual work: run
    the handler, mark the job done, apply the lead's state transition — all
    committed together, or all rolled back together if anything raises.
    """
    resolved_worker_id = worker_id or _default_worker_id()
    resolved_lease = lease_seconds if lease_seconds is not None else RUNTIME.worker_lease_seconds
    resolved_max_attempts = (
        max_attempts if max_attempts is not None else RUNTIME.worker_max_attempts
    )

    claimed = queue.claim(
        worker_id=resolved_worker_id,
        limit=batch_size,
        lease_seconds=resolved_lease,
        max_attempts=resolved_max_attempts,
    )

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
    handler = handlers.get(job.job_type)
    if handler is None:
        return _fail(
            queue,
            pool,
            job,
            max_attempts=max_attempts,
            error=f"no handler registered for job_type {job.job_type!r}",
        )

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
                        f"handler for {job.job_type!r} returned a new status but job has no lead"
                    )
                apply_transition(
                    conn,
                    lead_id=job.lead_id,
                    expected_version=lead_version,
                    new_status=new_status,
                    event_type=f"job:{job.job_type}",
                    payload={"job_id": str(job.job_id)},
                )

            conn.commit()
        return CycleResult(job_id=job.job_id, job_type=job.job_type, outcome="done")

    except Exception as exc:
        return _fail(queue, pool, job, max_attempts=max_attempts, error=str(exc))


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


if __name__ == "__main__":
    raise SystemExit(main())
