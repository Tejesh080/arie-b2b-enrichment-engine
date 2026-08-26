"""arie.statemachine.apply and the full worker cycle, against a real database.

Requires TEST_DATABASE_URL; skipped otherwise (see
conftest.py). Covers: atomic commit of job completion + lead transition
together, optimistic-concurrency conflicts, the lead_events audit trail, and
the worker loop's exception handling (rollback + retry, and the case where no
handler is registered for a job_type).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from arie.core.types import LeadStatus
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobContext, run_worker_cycle
from arie.statemachine.apply import OptimisticConcurrencyError, apply_transition

pytestmark = pytest.mark.integration


# --- apply_transition: atomicity and optimistic concurrency ---------------------


def test_apply_transition_updates_status_version_and_event_together(
    db_conn: psycopg.Connection,
    migrated_database: str,
    make_lead: Callable[[], tuple[UUID, int]],
) -> None:
    lead_id, version = make_lead()

    with psycopg.connect(migrated_database) as conn:
        result = apply_transition(
            conn,
            lead_id=lead_id,
            expected_version=version,
            new_status=LeadStatus.SCORING,
            event_type="test:advance",
            payload={"note": "hello"},
        )
        conn.commit()

    assert result.new_version == version + 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
        assert row is not None
        assert row == ("SCORING", version + 1)

        cur.execute("SELECT event_type, payload FROM lead_events WHERE lead_id = %s", (lead_id,))
        events = cur.fetchall()
        assert len(events) == 1
        assert events[0][0] == "test:advance"
        assert events[0][1] == {"note": "hello"}


def test_apply_transition_without_commit_persists_nothing(
    db_conn: psycopg.Connection,
    migrated_database: str,
    make_lead: Callable[[], tuple[UUID, int]],
) -> None:
    """apply_transition never commits itself -- the caller decides when. A
    caller that rolls back instead sees no partial effect."""
    lead_id, version = make_lead()

    with psycopg.connect(migrated_database) as conn:
        apply_transition(
            conn,
            lead_id=lead_id,
            expected_version=version,
            new_status=LeadStatus.SCORING,
            event_type="test:advance",
        )
        conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
        assert row is not None
        assert row == ("NEW", version)
        cur.execute("SELECT COUNT(*) FROM lead_events WHERE lead_id = %s", (lead_id,))
        count_row = cur.fetchone()
        assert count_row is not None
        assert count_row[0] == 0


def test_optimistic_concurrency_conflict_raises_and_leaves_no_trace(
    db_conn: psycopg.Connection,
    migrated_database: str,
    make_lead: Callable[[], tuple[UUID, int]],
) -> None:
    lead_id, version = make_lead()

    # A concurrent writer advances the lead first and commits.
    with psycopg.connect(migrated_database) as winner_conn:
        apply_transition(
            winner_conn,
            lead_id=lead_id,
            expected_version=version,
            new_status=LeadStatus.SCORING,
            event_type="winner",
        )
        winner_conn.commit()

    # A second writer, still holding the stale version it read before the
    # winner committed, tries to transition the same lead.
    with psycopg.connect(migrated_database) as loser_conn:
        with pytest.raises(OptimisticConcurrencyError) as exc_info:
            apply_transition(
                loser_conn,
                lead_id=lead_id,
                expected_version=version,  # stale
                new_status=LeadStatus.FETCHING_EVIDENCE,
                event_type="loser",
            )
        loser_conn.rollback()

    assert exc_info.value.lead_id == lead_id
    assert exc_info.value.expected_version == version

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
        assert row is not None
        assert row == ("SCORING", version + 1)  # only the winner's transition applied

        cur.execute(
            "SELECT event_type FROM lead_events WHERE lead_id = %s ORDER BY event_id", (lead_id,)
        )
        events = [r[0] for r in cur.fetchall()]
        assert events == ["winner"]  # the loser's event was never written


# --- run_worker_cycle: the full atomic path --------------------------------------


def test_worker_cycle_commits_job_completion_and_transition_together(
    job_queue: PostgresJobQueue,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    make_lead: Callable[[], tuple[UUID, int]],
    cleanup_jobs: list[UUID],
) -> None:
    lead_id, _version = make_lead()
    enqueued = job_queue.enqueue(lead_id=lead_id, job_type="advance_probe")
    cleanup_jobs.append(enqueued.job_id)

    def handler(ctx: JobContext) -> LeadStatus:
        assert ctx.lead_status == LeadStatus.NEW
        return LeadStatus.SCORING

    results = run_worker_cycle(
        job_queue, worker_pool, {"advance_probe": handler}, worker_id="w1", batch_size=1
    )

    assert len(results) == 1
    assert results[0].outcome == "done"

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        job_row = cur.fetchone()
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()

    assert job_row is not None and job_row[0] == "done"
    assert lead_row is not None and lead_row == ("SCORING", 2)


def test_worker_cycle_rolls_back_and_retries_on_handler_exception(
    job_queue: PostgresJobQueue,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    make_lead: Callable[[], tuple[UUID, int]],
    cleanup_jobs: list[UUID],
) -> None:
    lead_id, _version = make_lead()
    enqueued = job_queue.enqueue(lead_id=lead_id, job_type="explodes")
    cleanup_jobs.append(enqueued.job_id)

    def handler(ctx: JobContext) -> LeadStatus:
        raise RuntimeError("simulated handler failure")

    results = run_worker_cycle(
        job_queue, worker_pool, {"explodes": handler}, worker_id="w1", batch_size=1
    )

    assert len(results) == 1
    assert results[0].outcome == "retry"
    assert "simulated handler failure" in (results[0].detail or "")

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        job_row = cur.fetchone()
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()

    assert job_row is not None
    assert job_row == ("pending", 1)
    assert lead_row is not None
    assert lead_row == ("NEW", 1)  # untouched -- the handler's transaction rolled back


def test_worker_cycle_routes_optimistic_conflict_through_the_retry_path(
    job_queue: PostgresJobQueue,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    migrated_database: str,
    make_lead: Callable[[], tuple[UUID, int]],
    cleanup_jobs: list[UUID],
) -> None:
    lead_id, version = make_lead()
    enqueued = job_queue.enqueue(lead_id=lead_id, job_type="contended")
    cleanup_jobs.append(enqueued.job_id)

    def handler(ctx: JobContext) -> LeadStatus:
        # Simulates a concurrent writer advancing the lead between when this
        # job read its version (in JobContext) and when it tries to apply its
        # own transition -- the apply_transition call inside the worker loop
        # will see a stale expected_version and raise.
        with psycopg.connect(migrated_database) as other_conn:
            apply_transition(
                other_conn,
                lead_id=lead_id,
                expected_version=version,
                new_status=LeadStatus.SCORING,
                event_type="concurrent_writer",
            )
            other_conn.commit()
        return LeadStatus.FETCHING_EVIDENCE

    results = run_worker_cycle(
        job_queue, worker_pool, {"contended": handler}, worker_id="w1", batch_size=1
    )

    assert len(results) == 1
    assert results[0].outcome == "retry"
    assert "lead" in (results[0].detail or "").lower()

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        job_row = cur.fetchone()
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()

    assert job_row is not None and job_row[0] == "pending"
    # Only the concurrent writer's transition survived; the job's own
    # (conflicting) transition never applied.
    assert lead_row is not None and lead_row == ("SCORING", version + 1)


def test_worker_cycle_fails_a_job_with_no_registered_handler(
    job_queue: PostgresJobQueue,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    cleanup_jobs: list[UUID],
) -> None:
    enqueued = job_queue.enqueue(lead_id=None, job_type="unregistered_type")
    cleanup_jobs.append(enqueued.job_id)

    results = run_worker_cycle(job_queue, worker_pool, {}, worker_id="w1", batch_size=1)

    assert len(results) == 1
    assert results[0].outcome == "retry"
    assert "no handler registered" in (results[0].detail or "")

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        row = cur.fetchone()
        assert row is not None
        assert row == ("pending", 1)


def test_worker_cycle_reports_lost_lease_when_ownership_moves_mid_handler(
    job_queue: PostgresJobQueue,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    cleanup_jobs: list[UUID],
) -> None:
    """The worker-loop-level guarantee behind the ownership-fencing fix
    (arie.jobs.queue.JobOwnershipError): if this worker's lease moves to
    another worker while its handler is still running -- the same shape as a
    real lease expiring mid-handler and being reclaimed -- `complete()` must
    refuse to write, and the cycle must report `lost_lease` rather than
    either crashing the whole loop or silently marking the job done out from
    under whoever now owns it."""
    enqueued = job_queue.enqueue(lead_id=None, job_type="lease_stolen_mid_handler")
    cleanup_jobs.append(enqueued.job_id)

    def handler(ctx: JobContext) -> None:
        # Simulates a concurrent claim() reclaiming this exact job's expired
        # lease while this handler is still running, on a separate
        # connection so it's visible to complete()'s own connection after.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET locked_by = 'thief', locked_at = now() WHERE job_id = %s",
                (ctx.job.job_id,),
            )
        db_conn.commit()
        return None

    results = run_worker_cycle(
        job_queue,
        worker_pool,
        {"lease_stolen_mid_handler": handler},
        worker_id="victim",
        batch_size=1,
    )

    assert len(results) == 1
    assert results[0].outcome == "lost_lease"
    assert "no longer holds" in (results[0].detail or "")

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, locked_by FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        row = cur.fetchone()
    assert row == ("processing", "thief"), (
        "the thief's ownership must survive victim's stale report"
    )


def test_reprocessing_after_a_crash_applies_the_transition_exactly_once(
    job_queue: PostgresJobQueue,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    make_lead: Callable[[], tuple[UUID, int]],
    cleanup_jobs: list[UUID],
) -> None:
    """The end-to-end idempotency property: a worker that dies mid-job and a
    worker that later successfully reprocesses the same job must leave the
    lead in a state consistent with exactly one transition -- not two, and
    not a merge of both attempts."""
    lead_id, _version = make_lead()
    enqueued = job_queue.enqueue(lead_id=lead_id, job_type="crash_then_succeed")
    cleanup_jobs.append(enqueued.job_id)

    # First attempt: claim directly (bypassing run_worker_cycle) and simulate
    # a hard crash -- claimed, but never completed, failed, or committed.
    [claimed] = job_queue.claim(worker_id="doomed", job_types=["crash_then_succeed"], limit=1)
    assert claimed.attempt_count == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET locked_at = %s WHERE job_id = %s",
            (datetime.now(UTC) - timedelta(seconds=10), claimed.job_id),
        )
    db_conn.commit()

    calls = 0

    def handler(ctx: JobContext) -> LeadStatus:
        nonlocal calls
        calls += 1
        return LeadStatus.SCORING

    # Second cycle: claim() reclaims the expired lease (attempt_count -> 1)
    # and this time the handler actually runs and succeeds.
    results = run_worker_cycle(
        job_queue,
        worker_pool,
        {"crash_then_succeed": handler},
        worker_id="rescuer",
        batch_size=1,
        lease_seconds=5,
    )

    assert len(results) == 1
    assert results[0].outcome == "done"
    assert calls == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (enqueued.job_id,))
        job_row = cur.fetchone()
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM lead_events WHERE lead_id = %s", (lead_id,))
        event_count_row = cur.fetchone()

    assert job_row is not None
    assert job_row[0] == "done"
    assert job_row[1] == 1  # the reclaim counted as one attempt; the crash never got a second
    assert lead_row is not None
    assert lead_row == ("SCORING", 2)  # exactly one transition applied, not two
    assert event_count_row is not None
    assert event_count_row[0] == 1
