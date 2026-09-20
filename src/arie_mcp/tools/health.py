"""``get_system_health`` (spec § 9.1).

DB-grounded, not an HTTP proxy to ``/healthz``/``/healthz/worker`` — this is
the tool most likely to be reached for while the API process itself is
down, so it must not depend on the API being up. It never fails with
``DB_UNAVAILABLE``: an unreachable database is itself the answer this tool
exists to report, so it degrades to ``database_reachable: false`` inside a
successful result rather than erroring out.

Two policy constants are reused from ``arie.*`` rather than re-derived
(spec § 7.3): ``arie.migrations.migration_files`` (the on-disk migration
listing) and ``arie.config.WORKER_HEARTBEAT.stale_after_seconds`` (the
same staleness window ``GET /healthz/worker`` uses). The *reads themselves*
are re-implemented against ``mcp_diag`` views rather than calling
``arie.migrations.pending_migrations``/``arie.jobs.heartbeat.fleet_status``
directly — those functions query the bare ``schema_migrations``/
``worker_heartbeats`` tables, which ``arie_mcp_readonly`` has no grant on
by design (only the ``mcp_diag`` views are granted). See
``specs/mcp-engineering-interface.md`` and this slice's implementation
report for why that's a deliberate deviation from the spec's original
wording, not an oversight.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie.config import WORKER_HEARTBEAT
from arie.migrations import MigrationsDirectoryError, migration_files
from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import InternalToolError


class QueueDepth(BaseModel):
    pending: int
    processing: int
    failed: int
    dead_letter: int
    done: int


class WorkerFleetStatus(BaseModel):
    active_workers: int
    most_recent_heartbeat_at: datetime | None
    stale: bool


class SystemHealth(BaseModel):
    database_reachable: bool
    schema_up_to_date: bool
    pending_migrations: list[str]
    queue: QueueDepth
    worker_fleet: WorkerFleetStatus


_EMPTY_QUEUE = QueueDepth(pending=0, processing=0, failed=0, dead_letter=0, done=0)
_UNKNOWN_FLEET = WorkerFleetStatus(active_workers=0, most_recent_heartbeat_at=None, stale=True)


def _applied_migration_filenames(pool: ConnectionPool, *, statement_timeout_ms: int) -> set[str]:
    rows = db.run_query(
        pool,
        "SELECT filename FROM mcp_diag.v_schema_migrations",
        None,
        statement_timeout_ms=statement_timeout_ms,
    )
    return {row["filename"] for row in rows}


def _pending_migrations(pool: ConnectionPool, *, statement_timeout_ms: int) -> list[str]:
    try:
        on_disk = [path.name for path in migration_files()]
    except MigrationsDirectoryError as exc:
        raise InternalToolError("could not resolve the migrations directory") from exc
    applied = _applied_migration_filenames(pool, statement_timeout_ms=statement_timeout_ms)
    return [name for name in on_disk if name not in applied]


def _queue_depth(pool: ConnectionPool, *, statement_timeout_ms: int) -> QueueDepth:
    rows = db.run_query(
        pool,
        "SELECT status, job_count FROM mcp_diag.v_queue_depth",
        None,
        statement_timeout_ms=statement_timeout_ms,
    )
    by_status: dict[str, int] = {row["status"]: row["job_count"] for row in rows}
    return QueueDepth(
        pending=by_status.get("pending", 0),
        processing=by_status.get("processing", 0),
        failed=by_status.get("failed", 0),
        dead_letter=by_status.get("dead_letter", 0),
        done=by_status.get("done", 0),
    )


def _worker_fleet(pool: ConnectionPool, *, statement_timeout_ms: int) -> WorkerFleetStatus:
    rows = db.run_query(
        pool,
        "SELECT worker_instance_id, last_seen_at FROM mcp_diag.v_worker_heartbeats",
        None,
        statement_timeout_ms=statement_timeout_ms,
    )
    if not rows:
        return WorkerFleetStatus(active_workers=0, most_recent_heartbeat_at=None, stale=True)

    threshold = datetime.now(UTC) - timedelta(seconds=WORKER_HEARTBEAT.stale_after_seconds)
    seen_at = [row["last_seen_at"] for row in rows if row["last_seen_at"] is not None]
    active = [ts for ts in seen_at if ts >= threshold]
    most_recent = max(seen_at) if seen_at else None
    return WorkerFleetStatus(
        active_workers=len(active),
        most_recent_heartbeat_at=most_recent,
        stale=len(active) == 0,
    )


def _collect(pool: ConnectionPool, *, statement_timeout_ms: int) -> SystemHealth:
    """Runs synchronously inside a worker thread — see
    ``get_system_health_impl``'s ``asyncio.to_thread`` call. Kept as one
    function (rather than three separately-threaded calls) so the three
    reads share one clear failure boundary: if the database answers the
    reachability probe but then fails partway through, that surfaces as
    this tool's own DB_UNAVAILABLE, not a partially-populated result."""
    pending = _pending_migrations(pool, statement_timeout_ms=statement_timeout_ms)
    queue = _queue_depth(pool, statement_timeout_ms=statement_timeout_ms)
    fleet = _worker_fleet(pool, statement_timeout_ms=statement_timeout_ms)
    return SystemHealth(
        database_reachable=True,
        schema_up_to_date=not pending,
        pending_migrations=pending,
        queue=queue,
        worker_fleet=fleet,
    )


async def get_system_health_impl(
    pool: ConnectionPool | None, *, statement_timeout_ms: int
) -> ToolOutcome:
    """``pool is None`` (``MCP_READONLY_DATABASE_URL`` unset) is treated
    identically to an unreachable pool — both degrade to
    ``database_reachable: false`` inside a successful result, never a
    DB_UNAVAILABLE error. This is the one tool where "the database can't be
    reached" is itself a normal, useful answer rather than a failure."""
    degraded = ToolOutcome(
        data=_dump(
            SystemHealth(
                database_reachable=False,
                schema_up_to_date=False,
                pending_migrations=[],
                queue=_EMPTY_QUEUE,
                worker_fleet=_UNKNOWN_FLEET,
            )
        )
    )

    if pool is None:
        return degraded
    if not await asyncio.to_thread(db.check_reachable, pool):
        return degraded

    health = await asyncio.to_thread(_collect, pool, statement_timeout_ms=statement_timeout_ms)
    return ToolOutcome(data=_dump(health))


def _dump(health: SystemHealth) -> dict[str, Any]:
    return health.model_dump(mode="json")
