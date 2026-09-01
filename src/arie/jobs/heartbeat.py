"""A DB-backed "is a worker process actually alive" signal
(`migrations/0032_worker_heartbeats.sql`, Productization M6 Part 28) —
independent of `/healthz`, which only proves the API can reach the database
and the schema is current, never whether anything is consuming the job
queue. Two halves: :func:`beat` (called periodically by `arie.jobs.worker
.main`) and :func:`fleet_status` (read by `GET /healthz/worker`,
`arie.api.main`).
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.config import WORKER_HEARTBEAT

__all__ = ["WorkerFleetStatus", "beat", "fleet_status", "new_worker_instance_id"]

_LOGGER = logging.getLogger("arie.jobs.heartbeat")


def new_worker_instance_id() -> str:
    """`hostname:pid` — unique per worker process, stable for that
    process's whole lifetime (unlike `arie.jobs.worker._default_worker_id`,
    which is deliberately re-randomized every poll cycle for job-claim
    purposes and is not a process identity)."""
    return f"{socket.gethostname()}:{os.getpid()}"


_UPSERT_HEARTBEAT = """
    INSERT INTO worker_heartbeats (worker_instance_id, hostname, pid, started_at, last_seen_at)
    VALUES (%(worker_instance_id)s, %(hostname)s, %(pid)s, %(started_at)s, now())
    ON CONFLICT (worker_instance_id) DO UPDATE SET last_seen_at = now()
"""


def beat(pool: ConnectionPool, *, worker_instance_id: str, started_at: datetime) -> None:
    """Upsert this worker's liveness row. Best-effort — a failed write must
    never stop job processing; caught and logged, never raised, matching
    `arie.review_notifications`'s own "notification/observability failure
    is not a processing failure" discipline.
    """
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _UPSERT_HEARTBEAT,
                    {
                        "worker_instance_id": worker_instance_id,
                        "hostname": socket.gethostname(),
                        "pid": os.getpid(),
                        "started_at": started_at,
                    },
                )
            conn.commit()
    except Exception:
        _LOGGER.exception("failed to write worker heartbeat")


@dataclass(frozen=True)
class WorkerFleetStatus:
    healthy: bool
    """`True` iff at least one worker has heartbeat within
    `WORKER_HEARTBEAT.stale_after_seconds`."""
    active_workers: int
    most_recent_heartbeat_at: datetime | None


_SELECT_RECENT = """
    SELECT count(*) AS active_workers, max(last_seen_at) AS most_recent
    FROM worker_heartbeats
    WHERE last_seen_at >= now() - (%(stale_after_seconds)s || ' seconds')::interval
"""


def fleet_status(conn: psycopg.Connection) -> WorkerFleetStatus:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_RECENT, {"stale_after_seconds": WORKER_HEARTBEAT.stale_after_seconds})
        row = cur.fetchone()
    assert row is not None
    active = row["active_workers"] or 0
    return WorkerFleetStatus(
        healthy=active > 0, active_workers=active, most_recent_heartbeat_at=row["most_recent"]
    )
