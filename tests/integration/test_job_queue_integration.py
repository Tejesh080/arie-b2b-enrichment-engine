"""PostgresJobQueue against a real Postgres database.

Requires TEST_DATABASE_URL; skipped otherwise (see
conftest.py). Covers: concurrent claiming (SELECT ... FOR UPDATE SKIP
LOCKED), duplicate prevention (idempotency_key dedup), retry/backoff/DLQ
progression (including lease-expiry reclaim), and rollback safety.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from arie.jobs.queue import JobOwnershipError, JobStatus, PostgresJobQueue

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
                worker_id="w1",
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
            work_conn, claimed.job_id, worker_id="w1", error="boom 3", max_attempts=max_attempts
        )
        work_conn.commit()

    assert outcome.status == JobStatus.DEAD_LETTER
    assert outcome.attempt_count == max_attempts

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        row = cur.fetchone()
        assert row is not None
        assert row == ("dead_letter", max_attempts)


def test_re_enqueueing_a_dead_lettered_job_requeues_it_with_a_fresh_budget(
    job_queue: PostgresJobQueue, db_conn: psycopg.Connection, cleanup_jobs: list[UUID]
) -> None:
    """The audit-fixed bug: `idempotency_key` is a plain UNIQUE, not scoped by
    status, so a redelivery matching a `dead_letter` job used to match it
    forever with `created=False` and leave it dead — the lead it belonged to
    would never be claimed by a worker again, with no recovery short of a
    manual `DELETE`. A redelivery is the natural (and often only) signal that
    a permanently failed job should be retried, so it must requeue instead —
    with a fresh attempt budget, since the run that already exhausted its old
    one is over, not resumed."""
    key = f"dlq-recover-{uuid4()}"
    first = job_queue.enqueue(lead_id=None, job_type="dlq_recover_probe", idempotency_key=key)
    cleanup_jobs.append(first.job_id)

    # Burn it down to dead_letter, as if a worker had exhausted its attempts
    # and abandoned a stale lease along the way.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'dead_letter', attempt_count = 4, "
            "last_error = 'boom', locked_by = 'ghost-worker', locked_at = now() "
            "WHERE job_id = %s",
            (first.job_id,),
        )
    db_conn.commit()

    redelivered = job_queue.enqueue(lead_id=None, job_type="dlq_recover_probe", idempotency_key=key)

    assert redelivered.job_id == first.job_id
    assert redelivered.created is False, "same row, not a second job"
    assert redelivered.requeued is True

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, locked_by, locked_at, last_error FROM jobs "
            "WHERE job_id = %s",
            (first.job_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == 0, "fresh attempt budget, not a continuation of the exhausted one"
    assert row[2] is None and row[3] is None, "stale lease cleared"
    assert row[4] == "boom", "last_error kept as history of the previous failure"

    # And it's genuinely claimable again — the actual point of requeuing, not
    # just a status label.
    [claimed] = job_queue.claim(worker_id="w1", job_types=["dlq_recover_probe"], limit=1)
    assert claimed.job_id == first.job_id
    assert claimed.attempt_count == 0


@pytest.mark.parametrize("live_status", ["pending", "processing", "done"])
def test_re_enqueueing_a_live_or_completed_job_is_untouched(
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    cleanup_jobs: list[UUID],
    live_status: str,
) -> None:
    """The fix above must not widen: a redelivery matching anything other
    than a dead_letter job keeps today's plain-idempotent behaviour exactly —
    nothing about the existing row changes, `created=False`, `requeued=False`."""
    key = f"live-dedupe-{live_status}-{uuid4()}"
    first = job_queue.enqueue(lead_id=None, job_type="live_dedupe_probe", idempotency_key=key)
    cleanup_jobs.append(first.job_id)

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = %s, attempt_count = 2, "
            "locked_by = 'w1', locked_at = now() WHERE job_id = %s",
            (live_status, first.job_id),
        )
    db_conn.commit()

    redelivered = job_queue.enqueue(lead_id=None, job_type="live_dedupe_probe", idempotency_key=key)

    assert redelivered.job_id == first.job_id
    assert redelivered.created is False
    assert redelivered.requeued is False

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, locked_by FROM jobs WHERE job_id = %s", (first.job_id,)
        )
        row = cur.fetchone()
    assert row == (live_status, 2, "w1")


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


# --- ownership fencing --------------------------------------------------------------
#
# The audit-fixed bug: complete()/fail() used to match by job_id alone, with
# no check that the caller still held the processing lease. A *stale* worker
# — one whose lease was reclaimed after expiring, or that otherwise lost a
# claim it thinks it still holds — could resurrect a job another worker
# already finished, or double-count a single real-world failure. Every test
# below reproduces one of the two concrete corruption modes from the audit
# and asserts the fencing predicate (`status='processing' AND
# locked_by=worker_id`) now refuses the write instead.


def test_complete_by_the_owning_worker_succeeds(
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    migrated_database: str,
    cleanup_jobs: list[UUID],
) -> None:
    """The happy path the fencing predicate must not break."""
    enqueued = job_queue.enqueue(lead_id=None, job_type="fence_happy_path")
    cleanup_jobs.append(enqueued.job_id)
    [claimed] = job_queue.claim(worker_id="w1", job_types=["fence_happy_path"], limit=1)

    with psycopg.connect(migrated_database) as work_conn:
        job_queue.complete(work_conn, claimed.job_id, worker_id="w1")
        work_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        row = cur.fetchone()
    assert row is not None and row[0] == "done"


def test_a_stale_worker_cannot_resurrect_a_job_another_worker_completed(
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    migrated_database: str,
    cleanup_jobs: list[UUID],
) -> None:
    """Reproduces the audit's exact scenario: worker A finishes a job for
    real; worker B (which lost the version race, or whose lease was reclaimed
    and reassigned to A in the first place) reports failure for the same job
    afterward. Before this fix, B's `fail()` matched by job_id alone and set
    a `done` row back to `pending` -- a successfully-completed job resurrected
    into the queue, eventually dead-lettering a lead that actually succeeded."""
    enqueued = job_queue.enqueue(lead_id=None, job_type="fence_resurrection_probe")
    cleanup_jobs.append(enqueued.job_id)
    [claimed] = job_queue.claim(
        worker_id="workerA", job_types=["fence_resurrection_probe"], limit=1
    )

    with psycopg.connect(migrated_database) as work_conn:
        job_queue.complete(work_conn, claimed.job_id, worker_id="workerA")
        work_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        assert cur.fetchone() == ("done", 0)

    # Worker B, unaware A already finished it, reports its own (stale) failure.
    with psycopg.connect(migrated_database) as work_conn:
        with pytest.raises(JobOwnershipError):
            job_queue.fail(
                work_conn,
                claimed.job_id,
                worker_id="workerB",
                error="B lost the race",
                max_attempts=4,
            )
        work_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, last_error FROM jobs WHERE job_id = %s",
            (enqueued.job_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row == ("done", 0, None), "B's failure report must leave the completed job untouched"


def test_a_stale_worker_cannot_complete_a_job_reclaimed_by_another_worker(
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    migrated_database: str,
    cleanup_jobs: list[UUID],
) -> None:
    """The mirror case: worker A is still (as far as it knows) processing a
    job, but its lease already expired and worker B reclaimed it. A finishes
    its now-meaningless work and calls complete() -- before this fix, that
    would mark the job 'done' out from under B, which is still legitimately
    working on it."""
    enqueued = job_queue.enqueue(lead_id=None, job_type="fence_reclaim_probe")
    cleanup_jobs.append(enqueued.job_id)
    [claimed_by_a] = job_queue.claim(
        worker_id="workerA", job_types=["fence_reclaim_probe"], limit=1, lease_seconds=300
    )

    # Simulate A's lease having actually expired (a slow handler, a GC pause,
    # a network partition) and B reclaiming the now-abandoned job.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE job_id = %s",
            (claimed_by_a.job_id,),
        )
    db_conn.commit()
    [claimed_by_b] = job_queue.claim(
        worker_id="workerB",
        job_types=["fence_reclaim_probe"],
        limit=1,
        lease_seconds=5,
        max_attempts=4,
    )
    assert claimed_by_b.job_id == claimed_by_a.job_id
    assert claimed_by_b.attempt_count == 1, "the reclaim itself counts as one attempt"

    # A, unaware it lost the lease, tries to report completion.
    with psycopg.connect(migrated_database) as work_conn:
        with pytest.raises(JobOwnershipError):
            job_queue.complete(work_conn, claimed_by_a.job_id, worker_id="workerA")
        work_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, locked_by FROM jobs WHERE job_id = %s",
            (claimed_by_a.job_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row == ("processing", 1, "workerB"), (
        "B's ownership must survive A's stale completion report"
    )


def test_a_stale_worker_cannot_double_count_an_attempt_after_reclaim(
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    migrated_database: str,
    cleanup_jobs: list[UUID],
) -> None:
    """The attempt-count-correctness half of the same reclaim scenario:
    before this fix, A's late fail() call would increment attempt_count a
    *second* time for the one real failure the reclaim already counted --
    dead-lettering jobs at roughly half their configured budget under any
    lease churn."""
    enqueued = job_queue.enqueue(lead_id=None, job_type="fence_double_count_probe")
    cleanup_jobs.append(enqueued.job_id)
    max_attempts = 4
    [claimed_by_a] = job_queue.claim(
        worker_id="workerA", job_types=["fence_double_count_probe"], limit=1, lease_seconds=300
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE job_id = %s",
            (claimed_by_a.job_id,),
        )
    db_conn.commit()
    [claimed_by_b] = job_queue.claim(
        worker_id="workerB",
        job_types=["fence_double_count_probe"],
        limit=1,
        lease_seconds=5,
        max_attempts=max_attempts,
    )
    assert claimed_by_b.attempt_count == 1

    with psycopg.connect(migrated_database) as work_conn:
        with pytest.raises(JobOwnershipError):
            job_queue.fail(
                work_conn,
                claimed_by_a.job_id,
                worker_id="workerA",
                error="A failed late",
                max_attempts=max_attempts,
            )
        work_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute("SELECT attempt_count FROM jobs WHERE job_id = %s", (claimed_by_a.job_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 1, "one real failure, one attempt consumed -- not two"


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
    # complete() is ownership-fenced (requires status='processing' AND
    # locked_by=<worker_id>) -- claim() first, as any real caller must, so
    # the row is actually in the state complete() is willing to touch.
    [claimed] = job_queue.claim(worker_id="w1", job_types=["rollback_probe"], limit=1)
    assert claimed.job_id == enqueued.job_id

    with psycopg.connect(migrated_database) as work_conn:
        job_queue.complete(work_conn, enqueued.job_id, worker_id="w1")
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
    # claim()'s own transaction committed 'processing' before work_conn ever
    # opened; complete()'s 'done' write is what rolled back, so the job is
    # back to the last state that *did* commit, not to 'pending'.
    assert job_row[0] == "processing"
    assert lead_row is not None
    assert lead_row == ("NEW", 1)  # untouched
