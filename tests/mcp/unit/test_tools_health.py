"""Unit tests for ``get_system_health`` with a mocked DB layer — no real
Postgres. ``arie_mcp.db.run_query``/``check_reachable`` are monkeypatched
via the module object (``arie_mcp.tools.health`` calls them as
``db.run_query(...)``), so patching ``arie_mcp.db.run_query`` affects the
tool exactly as a real DB failure would.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from psycopg_pool import ConnectionPool

from arie_mcp import db
from arie_mcp.tools import health


def _fake_pool() -> ConnectionPool:
    return cast(ConnectionPool, object())


class _FakeRows:
    """Maps a SQL string (matched by a distinguishing substring) to the
    rows ``run_query`` should return — good enough for these tests without
    a real query planner."""

    def __init__(self, by_marker: dict[str, list[dict[str, Any]]]) -> None:
        self._by_marker = by_marker

    def __call__(
        self, pool: object, sql: str, params: object, *, statement_timeout_ms: int
    ) -> list[dict[str, Any]]:
        for marker, rows in self._by_marker.items():
            if marker in sql:
                return rows
        raise AssertionError(f"no fake rows registered for query: {sql}")


@pytest.fixture(autouse=True)
def _reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "check_reachable", lambda pool: True)


async def test_reports_unreachable_database_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "check_reachable", lambda pool: False)

    outcome = await health.get_system_health_impl(_fake_pool(), statement_timeout_ms=5000)

    assert outcome.data["database_reachable"] is False
    assert outcome.data["schema_up_to_date"] is False


async def test_no_pool_configured_is_treated_like_unreachable() -> None:
    outcome = await health.get_system_health_impl(None, statement_timeout_ms=5000)

    assert outcome.data["database_reachable"] is False


async def test_reports_pending_migrations_and_schema_not_up_to_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health, "migration_files", lambda: [_FakePath("0001_init.sql"), _FakePath("0002_new.sql")]
    )
    monkeypatch.setattr(
        db,
        "run_query",
        _FakeRows(
            {
                "v_schema_migrations": [{"filename": "0001_init.sql"}],
                "v_queue_depth": [],
                "v_worker_heartbeats": [],
            }
        ),
    )

    outcome = await health.get_system_health_impl(_fake_pool(), statement_timeout_ms=5000)

    assert outcome.data["schema_up_to_date"] is False
    assert outcome.data["pending_migrations"] == ["0002_new.sql"]


async def test_schema_up_to_date_when_nothing_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "migration_files", lambda: [_FakePath("0001_init.sql")])
    monkeypatch.setattr(
        db,
        "run_query",
        _FakeRows(
            {
                "v_schema_migrations": [{"filename": "0001_init.sql"}],
                "v_queue_depth": [],
                "v_worker_heartbeats": [],
            }
        ),
    )

    outcome = await health.get_system_health_impl(_fake_pool(), statement_timeout_ms=5000)

    assert outcome.data["schema_up_to_date"] is True
    assert outcome.data["pending_migrations"] == []


async def test_queue_depth_defaults_missing_statuses_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health, "migration_files", lambda: [_FakePath("0001_init.sql")])
    monkeypatch.setattr(
        db,
        "run_query",
        _FakeRows(
            {
                "v_schema_migrations": [{"filename": "0001_init.sql"}],
                "v_queue_depth": [{"status": "pending", "job_count": 3}],
                "v_worker_heartbeats": [],
            }
        ),
    )

    outcome = await health.get_system_health_impl(_fake_pool(), statement_timeout_ms=5000)

    queue = outcome.data["queue"]
    assert queue == {"pending": 3, "processing": 0, "failed": 0, "dead_letter": 0, "done": 0}


async def test_worker_fleet_stale_when_last_heartbeat_outside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health, "migration_files", lambda: [_FakePath("0001_init.sql")])
    stale_heartbeat = datetime.now(UTC) - timedelta(hours=6)
    monkeypatch.setattr(
        db,
        "run_query",
        _FakeRows(
            {
                "v_schema_migrations": [{"filename": "0001_init.sql"}],
                "v_queue_depth": [],
                "v_worker_heartbeats": [
                    {"worker_instance_id": "host:1", "last_seen_at": stale_heartbeat}
                ],
            }
        ),
    )

    outcome = await health.get_system_health_impl(_fake_pool(), statement_timeout_ms=5000)

    fleet = outcome.data["worker_fleet"]
    assert fleet["active_workers"] == 0
    assert fleet["stale"] is True


async def test_worker_fleet_healthy_when_recent_heartbeat_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health, "migration_files", lambda: [_FakePath("0001_init.sql")])
    recent_heartbeat = datetime.now(UTC) - timedelta(seconds=5)
    monkeypatch.setattr(
        db,
        "run_query",
        _FakeRows(
            {
                "v_schema_migrations": [{"filename": "0001_init.sql"}],
                "v_queue_depth": [],
                "v_worker_heartbeats": [
                    {"worker_instance_id": "host:1", "last_seen_at": recent_heartbeat}
                ],
            }
        ),
    )

    outcome = await health.get_system_health_impl(_fake_pool(), statement_timeout_ms=5000)

    fleet = outcome.data["worker_fleet"]
    assert fleet["active_workers"] == 1
    assert fleet["stale"] is False


class _FakePath:
    def __init__(self, name: str) -> None:
        self.name = name
