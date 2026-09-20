from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg_pool import ConnectionPool

from arie_mcp import db
from arie_mcp.errors import DbUnavailableError
from arie_mcp.tools import errors

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _fake_pool() -> ConnectionPool:
    return cast(ConnectionPool, object())


async def test_no_pool_raises_db_unavailable() -> None:
    with pytest.raises(DbUnavailableError):
        await errors.get_recent_errors_impl(None, window_minutes=60, statement_timeout_ms=5000)


async def test_aggregates_job_and_provider_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = uuid4()
    call_id = uuid4()

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_failed_jobs" in sql and "GROUP BY" in sql:
            return [{"job_type": "compute_score", "n": 3}]
        if "v_failed_jobs" in sql:
            return [
                {
                    "job_id": job_id,
                    "job_type": "compute_score",
                    "status": "dead_letter",
                    "last_error": "boom",
                    "created_at": _NOW,
                }
            ]
        if "v_provider_call_errors" in sql and "GROUP BY" in sql:
            return [{"kind": "quota_exhausted", "n": 5}]
        if "v_provider_call_errors" in sql:
            return [
                {
                    "call_id": call_id,
                    "provider": "hunter_combined_enrichment",
                    "error_kind": "quota_exhausted",
                    "suppressed_reason": None,
                    "requested_at": _NOW,
                }
            ]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await errors.get_recent_errors_impl(
        _fake_pool(), window_minutes=60, statement_timeout_ms=5000
    )

    assert outcome.data["job_failure_count"] == 3
    assert outcome.data["provider_error_count"] == 5
    assert outcome.data["job_failures_by_type"] == [{"key": "compute_score", "count": 3}]
    assert outcome.data["provider_errors_by_kind"] == [{"key": "quota_exhausted", "count": 5}]
    assert len(outcome.data["recent_job_failures"]) == 1
    assert len(outcome.data["recent_provider_errors"]) == 1
    assert outcome.row_count == 8


async def test_redacts_last_error_in_recent_job_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_failed_jobs" in sql and "GROUP BY" in sql:
            return []
        if "v_failed_jobs" in sql:
            return [
                {
                    "job_id": uuid4(),
                    "job_type": "compute_score",
                    "status": "dead_letter",
                    "last_error": "failed for jordan.ellis@example.test",
                    "created_at": _NOW,
                }
            ]
        return []

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await errors.get_recent_errors_impl(
        _fake_pool(), window_minutes=60, statement_timeout_ms=5000
    )

    last_error = outcome.data["recent_job_failures"][0]["last_error"]
    assert "jordan.ellis@example.test" not in last_error
    assert "[REDACTED]" in last_error


async def test_truncated_flag_when_recent_rows_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "GROUP BY" in sql and "v_failed_jobs" in sql:
            return [{"job_type": "compute_score", "n": 500}]
        if "v_failed_jobs" in sql:
            return [
                {
                    "job_id": uuid4(),
                    "job_type": "compute_score",
                    "status": "failed",
                    "last_error": None,
                    "created_at": _NOW,
                }
                for _ in range(20)
            ]
        return []

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await errors.get_recent_errors_impl(
        _fake_pool(), window_minutes=60, statement_timeout_ms=5000
    )

    assert outcome.truncated is True
