"""PostgresJobQueue against a real Postgres database.

Requires DATABASE_URL / DATABASE_DIRECT_URL; skipped otherwise (see
conftest.py). Covers: concurrent claiming (SELECT ... FOR UPDATE SKIP
LOCKED), duplicate prevention (idempotency_key dedup), retry/backoff/DLQ
progression (including lease-expiry reclaim), and rollback safety.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest

from arie.jobs.queue import JobStatus, PostgresJobQueue

pytestmark = pytest.mark.integration


# --- duplicate prevention / idempotency -----------------------------------------


def test_enqueue_dedupes_on_idempotency_key(
    job_queue: PostgresJobQueue, db_conn: psycopg.Connection, cleanup_jobs: list[UUID]
) -> None:
    key = f"dedupe-{UUID(int=1)}"
    first = job_queue.enqueue(lead_id=None, job_type="noop", idempotency_key=key)
    second = job_queue.enqueue(lead_id=None, job_type="noop", idempotency_key=key)
    cleanup_jobs.append(first.job_id)

    assert first.created is True
    assert second.created is False
    assert first.job_id == second.job_id

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM jobs WHERE idempotency_key = %s", (key,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_enqueue_without_idempotency_key_never_dedupes(
    job_queue: PostgresJobQueue, cleanup_jobs: list[UUID]
) -> None:
    first = job_queue.enqueue(lead_id=None, job_type="noop")
    second = job_queue.enqueue(lead_id=None, job_type="noop")
    cleanup_jobs.extend([first.job_id, second.job_id])

    assert first.job_id != second.job_id
    assert first.created is True
    assert second.created is True


# --- concurrent claiming ---------------------------------------------------------


def test_concurrent_claims_never_overlap(
    job_queue: PostgresJobQueue, cleanup_jobs: list[UUID]
) -> None:
    enqueued = [job_queue.enqueue(lead_id=None, job_type="concurrent_probe") for _ in range(10)]
    cleanup_jobs.extend(j.job_id for j in enqueued)

    def _claim(worker_id: str) -> list[UUID]:
        return [
            j.job_id
            for j in job_queue.claim(worker_id=worker_id, job_types=["concurrent_probe"], limit=5)
        ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(_claim, "worker-a")
        future_b = pool.submit(_claim, "worker-b")
        claimed_a = future_a.result()
        claimed_b = future_b.result()

    assert set(claimed_a).isdisjoint(claimed_b)
    assert len(claimed_a) + len(claimed_b) == 10
    assert set(claimed_a) | set(claimed_b) == {j.job_id for j in enqueued}


def test_claim_skips_jobs_not_yet_due(
    db_conn: psycopg.Connection, job_queue: PostgresJobQueue, cleanup_jobs: list[UUID]
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (job_type, next_retry_at) VALUES (%s, %s) RETURNING job_id",
            ("future_probe", datetime.now(UTC) + timedelta(hours=1)),
        )
        row = cur.fetchone()
        assert row is not None
    db_conn.commit()
    cleanup_jobs.append(row[0])

    claimed = job_queue.claim(worker_id="w1", job_types=["future_probe"], limit=10)

    assert claimed == []


# --- retry / backoff / dead-letter ------------------------------------------------


def test_fail_reschedules_then_dead_letters(
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    migrated_database: str,
    cleanup_jobs: list[UUID],
) -> None:
    enqueued = job_queue.enqueue(lead_id=None, job_type="always_fails")
    cleanup_jobs.append(enqueued.job_id)
    max_attempts = 3

    for expected_attempt in (1, 2):
        [claimed] = job_queue.claim(worker_id="w1", job_types=["always_fails"], limit=1)
        assert claimed.job_id == enqueued.job_id
        before = datetime.now(UTC)
        with psycopg.connect(migrated_database) as work_conn:
            outcome = job_queue.fail(
                work_conn,
                claimed.job_id,
                error=f"boom {expected_attempt}",
                max_attempts=max_attempts,
            )
            work_conn.commit()
        assert outcome.status == JobStatus.PENDING
        assert outcome.attempt_count == expected_attempt
        # Full jitter can legitimately pick a near-zero delay, so compare
        # against a timestamp captured *before* the call rather than "now"
        # (a real-time comparison after the call would race a small jitter
        # draw and could flake).
        assert outcome.next_retry_at is not None and outcome.next_retry_at >= before

        # Force it due immediately so the test doesn't wait out real backoff.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET next_retry_at = now() WHERE job_id = %s", (claimed.job_id,)
            )
        db_conn.commit()

    [claimed] = job_queue.claim(worker_id="w1", job_types=["always_fails"], limit=1)
    with psycopg.connect(migrated_database) as work_conn:
        outcome = job_queue.fail(
            work_conn, claimed.job_id, error="boom 3", max_attempts=max_attempts
        )
        work_conn.commit()

    assert outcome.status == JobStatus.DEAD_LETTER
    assert outcome.attempt_count == max_attempts

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        row = cur.fetchone()
        assert row is not None
        assert row == ("dead_letter", max_attempts)


def test_dead_lettered_jobs_are_never_reclaimed(
    job_queue: PostgresJobQueue, db_conn: psycopg.Connection, cleanup_jobs: list[UUID]
) -> None:
    enqueued = job_queue.enqueue(lead_id=None, job_type="already_dead")
    cleanup_jobs.append(enqueued.job_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'dead_letter', attempt_count = 9 WHERE job_id = %s",
            (enqueued.job_id,),
        )
    db_conn.commit()

    claimed = job_queue.claim(worker_id="w1", job_types=["already_dead"], limit=10)

    assert claimed == []


# --- lease expiry -----------------------------------------------------------------


def test_expired_lease_is_reclaimed_and_counts_as_an_attempt(
    job_queue: PostgresJobQueue, db_conn: psycopg.Connection, cleanup_jobs: list[UUID]
) -> None:
    enqueued = job_queue.enqueue(lead_id=None, job_type="crashes_worker")
    cleanup_jobs.append(enqueued.job_id)

    [claimed] = job_queue.claim(worker_id="doomed-worker", job_types=["crashes_worker"], limit=1)
    assert claimed.attempt_count == 0

    # Simulate the worker crashing mid-job: locked, never completed or failed.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET locked_at = %s WHERE job_id = %s",
            (datetime.now(UTC) - timedelta(seconds=10), claimed.job_id),
        )
    db_conn.commit()

    [reclaimed] = job_queue.claim(
        worker_id="rescuer", job_types=["crashes_worker"], limit=1, lease_seconds=5, max_attempts=5
    )

    assert reclaimed.job_id == claimed.job_id
    assert reclaimed.attempt_count == 1


def test_lease_reclaim_eventually_dead_letters_a_permanently_crashing_job(
    job_queue: PostgresJobQueue, db_conn: psycopg.Connection, cleanup_jobs: list[UUID]
) -> None:
    enqueued = job_queue.enqueue(lead_id=None, job_type="always_crashes")
    cleanup_jobs.append(enqueued.job_id)
    max_attempts = 2

    for _ in range(max_attempts):
        claimed = job_queue.claim(
            worker_id="doomed",
            job_types=["always_crashes"],
            limit=1,
            lease_seconds=5,
            max_attempts=max_attempts,
        )
        assert len(claimed) == 1
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET locked_at = %s WHERE job_id = %s",
                (datetime.now(UTC) - timedelta(seconds=10), claimed[0].job_id),
            )
        db_conn.commit()

    # One more claim attempt triggers the reclaim that pushes attempt_count to
    # max_attempts and dead-letters it, rather than serving it again.
    final_claim = job_queue.claim(
        worker_id="doomed",
        job_types=["always_crashes"],
        limit=1,
        lease_seconds=5,
        max_attempts=max_attempts,
    )
    assert final_claim == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        row = cur.fetchone()
        assert row is not None
        assert row == ("dead_letter", max_attempts)


# --- rollback safety --------------------------------------------------------------


def test_rollback_leaves_no_trace(
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    migrated_database: str,
    make_lead: Callable[[], tuple[UUID, int]],
) -> None:
    """Mid-transaction work that's rolled back instead of committed must be
    completely invisible afterward -- the guarantee arie.jobs.worker relies on
    when a handler raises."""
    lead_id, _version = make_lead()
    enqueued = job_queue.enqueue(lead_id=lead_id, job_type="rollback_probe")

    with psycopg.connect(migrated_database) as work_conn:
        job_queue.complete(work_conn, enqueued.job_id)
        with work_conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET status = 'SCORING', version = version + 1 WHERE lead_id = %s",
                (lead_id,),
            )
        work_conn.rollback()  # simulates a crash / raised exception before commit

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        job_row = cur.fetchone()
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()

    assert job_row is not None
    assert job_row[0] == "pending"  # complete() never committed
    assert lead_row is not None
    assert lead_row == ("NEW", 1)  # untouched
