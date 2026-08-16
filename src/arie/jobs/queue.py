"""The job queue — Postgres, `SELECT ... FOR UPDATE SKIP LOCKED`, nothing else.

See ADR 0002: one `jobs` table, no Redis, no Temporal. The queue interface is
deliberately small — `enqueue`, `claim`, `complete`, `fail` — so the backend
could be swapped without touching business logic, and so it's obvious which
operations manage their own transaction and which don't:

- `enqueue` and `claim` are standalone: each opens its own connection, does
  its work, and commits.
- `complete` and `fail` take a caller-provided connection and never commit.
  `complete` must share a transaction with the lead-state transition it
  accompanies (arie.statemachine.apply) — that shared commit *is* "job
  completion and lead-state transition commit atomically". `fail` shares a
  transaction too, but a different one: the one **recovering** from the work
  transaction that just rolled back, not the one that failed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.jobs.backoff import compute_backoff

_RECLAIM_EXPIRED_LEASES = """
    UPDATE jobs
    SET status = CASE WHEN attempt_count + 1 >= %(max_attempts)s THEN 'dead_letter' ELSE 'pending' END,
        attempt_count = attempt_count + 1,
        next_retry_at = now(),
        locked_by = NULL,
        locked_at = NULL,
        last_error = 'lease expired: worker did not report completion within the lease window'
    WHERE status = 'processing' AND locked_at < %(cutoff)s
"""

_CLAIM_SELECT = """
    SELECT job_id, lead_id, job_type, attempt_count, idempotency_key
    FROM jobs
    WHERE status = 'pending' AND next_retry_at <= now() {job_type_filter}
    ORDER BY next_retry_at
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
"""

_CLAIM_UPDATE = """
    UPDATE jobs SET status = 'processing', locked_by = %(worker_id)s, locked_at = now()
    WHERE job_id = ANY(%(job_ids)s)
"""

_ENQUEUE = """
    INSERT INTO jobs (lead_id, job_type, idempotency_key)
    VALUES (%(lead_id)s, %(job_type)s, %(idempotency_key)s)
    ON CONFLICT (idempotency_key) DO UPDATE SET job_type = jobs.job_type
    RETURNING job_id, (xmax = 0) AS created
"""

_COMPLETE = "UPDATE jobs SET status = 'done' WHERE job_id = %(job_id)s"

_SELECT_ATTEMPT_COUNT = "SELECT attempt_count FROM jobs WHERE job_id = %(job_id)s FOR UPDATE"

_RESCHEDULE = """
    UPDATE jobs
    SET status = 'pending', attempt_count = %(attempt_count)s,
        next_retry_at = %(next_retry_at)s, last_error = %(error)s,
        locked_by = NULL, locked_at = NULL
    WHERE job_id = %(job_id)s
"""

_DEAD_LETTER = """
    UPDATE jobs
    SET status = 'dead_letter', attempt_count = %(attempt_count)s, last_error = %(error)s
    WHERE job_id = %(job_id)s
"""


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class EnqueuedJob:
    job_id: UUID
    created: bool
    """False if an existing job with the same idempotency_key was found instead."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: UUID
    lead_id: UUID | None
    job_type: str
    attempt_count: int
    idempotency_key: str | None


@dataclass(frozen=True)
class FailOutcome:
    status: JobStatus
    """JobStatus.PENDING if rescheduled, JobStatus.DEAD_LETTER if the attempt budget is spent."""
    next_retry_at: datetime | None
    attempt_count: int


class PostgresJobQueue:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, conninfo: str, *, min_size: int = 1, max_size: int = 10) -> PostgresJobQueue:
        pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size, open=True)
        return cls(pool)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> PostgresJobQueue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def enqueue(
        self,
        *,
        lead_id: UUID | None,
        job_type: str,
        idempotency_key: str | None = None,
    ) -> EnqueuedJob:
        """Create a job, or return the existing one if `idempotency_key` already exists.

        A null `idempotency_key` never dedupes — Postgres treats every NULL as
        distinct under a UNIQUE constraint, so callers with no natural dedup
        key (e.g. a one-off maintenance job) simply always get a new row.
        """
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _ENQUEUE,
                {"lead_id": lead_id, "job_type": job_type, "idempotency_key": idempotency_key},
            )
            row = cur.fetchone()
            assert row is not None
            conn.commit()
        return EnqueuedJob(job_id=row["job_id"], created=row["created"])

    def claim(
        self,
        *,
        worker_id: str,
        job_types: Sequence[str] | None = None,
        limit: int = 1,
        lease_seconds: int = 300,
        max_attempts: int = 4,
    ) -> list[ClaimedJob]:
        """Claim up to `limit` due jobs, reclaiming any expired leases first.

        One transaction start to finish: the row locks `FOR UPDATE SKIP
        LOCKED` takes are what let a second, concurrent `claim()` skip these
        exact rows instead of blocking on them — that guarantee only holds
        while this transaction is open, which is why the claiming UPDATE
        happens here rather than being left to the caller.

        `max_attempts` applies to lease reclaiming too, not just `fail()`: a
        worker that crashes hard (killed, OOM, network partition) never calls
        `fail()` at all, so without counting reclaims against the same budget
        a wedged job would cycle claim -> crash -> reclaim forever and never
        reach the dead letter queue.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=lease_seconds)
        job_type_filter = "AND job_type = ANY(%(job_types)s)" if job_types is not None else ""
        select_sql = _CLAIM_SELECT.format(job_type_filter=job_type_filter)

        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_RECLAIM_EXPIRED_LEASES, {"cutoff": cutoff, "max_attempts": max_attempts})

            params: dict[str, Any] = {"limit": limit}
            if job_types is not None:
                params["job_types"] = list(job_types)
            cur.execute(select_sql, params)
            rows = cur.fetchall()

            if rows:
                job_ids = [row["job_id"] for row in rows]
                cur.execute(_CLAIM_UPDATE, {"worker_id": worker_id, "job_ids": job_ids})

            conn.commit()

        return [
            ClaimedJob(
                job_id=row["job_id"],
                lead_id=row["lead_id"],
                job_type=row["job_type"],
                attempt_count=row["attempt_count"],
                idempotency_key=row["idempotency_key"],
            )
            for row in rows
        ]

    def complete(self, conn: psycopg.Connection, job_id: UUID) -> None:
        """Mark a job done. Does not commit — see the module docstring."""
        with conn.cursor() as cur:
            cur.execute(_COMPLETE, {"job_id": job_id})

    def fail(
        self,
        conn: psycopg.Connection,
        job_id: UUID,
        *,
        error: str,
        max_attempts: int,
    ) -> FailOutcome:
        """Record a failed attempt: reschedule with backoff, or dead-letter if
        the attempt budget is spent. Does not commit — see the module docstring.
        """
        with conn.cursor() as cur:
            cur.execute(_SELECT_ATTEMPT_COUNT, {"job_id": job_id})
            row = cur.fetchone()
            assert row is not None
            attempt_count = row[0] + 1

            if attempt_count >= max_attempts:
                cur.execute(
                    _DEAD_LETTER, {"job_id": job_id, "attempt_count": attempt_count, "error": error}
                )
                return FailOutcome(
                    status=JobStatus.DEAD_LETTER, next_retry_at=None, attempt_count=attempt_count
                )

            delay = compute_backoff(attempt_count)
            next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
            cur.execute(
                _RESCHEDULE,
                {
                    "job_id": job_id,
                    "attempt_count": attempt_count,
                    "next_retry_at": next_retry_at,
                    "error": error,
                },
            )
            return FailOutcome(
                status=JobStatus.PENDING, next_retry_at=next_retry_at, attempt_count=attempt_count
            )
