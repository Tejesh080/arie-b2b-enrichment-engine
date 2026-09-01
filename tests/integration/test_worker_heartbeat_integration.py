"""Worker liveness against a real database (Productization M6 Part 28).

`/healthz` answers "can the API reach the database and is the schema
current". It cannot answer "is anything actually consuming the job queue" —
an API with a healthy database and a dead worker fleet looks perfect and
processes nothing. That is the failure this table exists to make visible, so
these tests are mostly about the *distinction* between the two checks rather
than about the SQL.

Requires TEST_DATABASE_URL; skipped otherwise (see conftest.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from arie.api.main import AppState, create_app
from arie.config import WorkerHeartbeatConfig
from arie.jobs import heartbeat as heartbeat_module
from arie.jobs.heartbeat import beat, fleet_status, new_worker_instance_id

pytestmark = pytest.mark.integration


@pytest.fixture
def clean_heartbeats(db_conn: psycopg.Connection) -> Iterator[None]:
    """`worker_heartbeats` is a tiny, global (non-tenant) table, and
    `fleet_status` aggregates *every* row in it. A stray row left by the local
    Compose worker — or by a previous test in this file — would make "the
    fleet is down" unobservable, so each test starts and ends from empty.

    Truncating a shared table is acceptable here precisely because nothing
    else depends on its contents: it is pure liveness telemetry, rebuilt by
    the next heartbeat of any running worker.
    """
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM worker_heartbeats")
    db_conn.commit()
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM worker_heartbeats")
    db_conn.commit()


def _client(app_state: AppState) -> TestClient:
    return TestClient(create_app(state=app_state), raise_server_exceptions=False)


def test_no_workers_reports_unhealthy(clean_heartbeats: None, db_conn: psycopg.Connection) -> None:
    status = fleet_status(db_conn)
    assert status.healthy is False
    assert status.active_workers == 0
    assert status.most_recent_heartbeat_at is None


def test_a_beat_makes_the_fleet_healthy(
    clean_heartbeats: None, worker_pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    beat(worker_pool, worker_instance_id="it-worker-1", started_at=datetime.now(UTC))

    status = fleet_status(db_conn)
    assert status.healthy is True
    assert status.active_workers == 1
    assert status.most_recent_heartbeat_at is not None


def test_repeated_beats_upsert_rather_than_accumulate(
    clean_heartbeats: None, worker_pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """One row per worker *process*, not per beat — otherwise the table grows
    without bound and `active_workers` counts heartbeats instead of workers."""
    started = datetime.now(UTC)
    for _ in range(3):
        beat(worker_pool, worker_instance_id="it-worker-1", started_at=started)

    assert fleet_status(db_conn).active_workers == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT started_at FROM worker_heartbeats WHERE worker_instance_id = %s",
            ("it-worker-1",),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == started, "started_at must record process start, not last beat"


def test_two_workers_are_counted_separately(
    clean_heartbeats: None, worker_pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    now = datetime.now(UTC)
    beat(worker_pool, worker_instance_id="it-worker-1", started_at=now)
    beat(worker_pool, worker_instance_id="it-worker-2", started_at=now)

    assert fleet_status(db_conn).active_workers == 2


def test_a_stale_heartbeat_is_not_counted(
    clean_heartbeats: None,
    worker_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the whole table: a worker that *used* to be alive must
    stop counting once it goes quiet, or the check reports health forever
    after a single beat."""
    monkeypatch.setattr(
        heartbeat_module, "WORKER_HEARTBEAT", WorkerHeartbeatConfig(stale_after_seconds=60.0)
    )
    beat(worker_pool, worker_instance_id="it-worker-1", started_at=datetime.now(UTC))

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE worker_heartbeats SET last_seen_at = %s WHERE worker_instance_id = %s",
            (datetime.now(UTC) - timedelta(seconds=120), "it-worker-1"),
        )
    db_conn.commit()

    status = fleet_status(db_conn)
    assert status.healthy is False
    assert status.active_workers == 0
    assert status.most_recent_heartbeat_at is None, (
        "most_recent_heartbeat_at reports the freshest *live* beat, not the freshest row"
    )


def test_a_returning_worker_becomes_healthy_again(
    clean_heartbeats: None, worker_pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Recovery has to be automatic — nothing clears a stale row, so a worker
    that comes back must be able to make its own existing row fresh again."""
    beat(worker_pool, worker_instance_id="it-worker-1", started_at=datetime.now(UTC))
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE worker_heartbeats SET last_seen_at = %s",
            (datetime.now(UTC) - timedelta(hours=1),),
        )
    db_conn.commit()
    assert fleet_status(db_conn).healthy is False

    beat(worker_pool, worker_instance_id="it-worker-1", started_at=datetime.now(UTC))

    assert fleet_status(db_conn).healthy is True


def test_a_failing_beat_never_raises(
    clean_heartbeats: None, migrated_database: str, db_conn: psycopg.Connection
) -> None:
    """Liveness telemetry must not be able to stop job processing. A closed
    pool is the cheapest real failure to induce; the contract is that `beat`
    swallows it, so the worker loop that calls it keeps running."""
    pool = ConnectionPool(migrated_database, min_size=1, max_size=2, open=True)
    pool.close()

    beat(pool, worker_instance_id="it-worker-doomed", started_at=datetime.now(UTC))

    assert fleet_status(db_conn).active_workers == 0


def test_worker_instance_id_is_stable_within_a_process() -> None:
    """Unlike `arie.jobs.worker._default_worker_id`, which is deliberately
    re-randomized per poll cycle for job-claim purposes, this one identifies
    the *process* — a changing value would insert a new row per beat."""
    assert new_worker_instance_id() == new_worker_instance_id()


# ------------------------------------------------------ GET /healthz/worker --


def test_healthz_worker_reports_a_down_fleet(clean_heartbeats: None, app_state: AppState) -> None:
    response = _client(app_state).get("/healthz/worker")

    assert response.status_code == 200, (
        "a down worker fleet is reported in the body, never as a non-200 — "
        "the endpoint itself is up and answering correctly"
    )
    body = response.json()
    assert body["healthy"] is False
    assert body["active_workers"] == 0
    assert body["most_recent_heartbeat_at"] is None


def test_healthz_worker_reports_a_live_fleet(
    clean_heartbeats: None, app_state: AppState, worker_pool: ConnectionPool
) -> None:
    beat(worker_pool, worker_instance_id="it-worker-1", started_at=datetime.now(UTC))

    body = _client(app_state).get("/healthz/worker").json()

    assert body["healthy"] is True
    assert body["active_workers"] == 1
    assert body["most_recent_heartbeat_at"] is not None


def test_healthz_stays_healthy_while_the_worker_fleet_is_down(
    clean_heartbeats: None, app_state: AppState
) -> None:
    """The separation that makes the split worth having. A dead fleet is a
    real operational problem, but `/healthz` is what gates traffic promotion
    and container restarts — and neither of those fixes a worker. Folding
    worker liveness into it would flap the API's own monitor over something
    the API cannot repair.
    """
    client = _client(app_state)

    assert client.get("/healthz/worker").json()["healthy"] is False

    api_health = client.get("/healthz")
    assert api_health.status_code == 200
    assert api_health.json()["status"] == "ok"


def test_healthz_worker_needs_no_authentication(
    clean_heartbeats: None, app_state: AppState
) -> None:
    """An infra liveness probe has no caller identity — same as `/healthz`.
    Asserted explicitly because every *other* route added in M6 is
    authenticated, so "this one isn't" must be a decision, not an oversight.
    The response carries no tenant data: a count and a timestamp.
    """
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)

    response = client.get("/healthz/worker")

    assert response.status_code == 200
    assert set(response.json()) == {"healthy", "active_workers", "most_recent_heartbeat_at"}
