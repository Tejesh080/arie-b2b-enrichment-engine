"""Unit tests for ``inspect_job``/``list_failed_jobs`` with a mocked DB
layer — no real Postgres."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg_pool import ConnectionPool

from arie_mcp import db
from arie_mcp.errors import NotFoundError
from arie_mcp.tools import jobs

_JOB_ID = uuid4()
_LEAD_ID = uuid4()
_ORG_ID = uuid4()
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _fake_pool() -> ConnectionPool:
    """A stand-in pool: every code path under test reaches the database
    only through the monkeypatched ``db.run_query``, so the pool object
    itself is never dereferenced — it just needs to satisfy the type
    signature."""
    return cast(ConnectionPool, object())


def _job_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "job_id": _JOB_ID,
        "lead_id": _LEAD_ID,
        "job_type": "compute_score",
        "status": "dead_letter",
        "attempt_count": 3,
        "next_retry_at": None,
        "locked_by": None,
        "locked_at": None,
        "last_error": "boom",
        "created_at": _NOW,
    }
    row.update(overrides)
    return row


def _job_detail_row(**overrides: Any) -> dict[str, Any]:
    row = _job_row(
        lead_status="FAILED",
        lead_is_shadow=False,
        lead_organization_id=_ORG_ID,
    )
    row.update(overrides)
    return row


def _patch_run_query(
    monkeypatch: pytest.MonkeyPatch, fn: Callable[..., list[dict[str, Any]]]
) -> None:
    monkeypatch.setattr(db, "run_query", fn)


async def test_inspect_job_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_query(monkeypatch, lambda *a, **k: [_job_detail_row()])

    outcome = await jobs.inspect_job_impl(_fake_pool(), _JOB_ID, statement_timeout_ms=5000)

    assert outcome.row_count == 1
    assert outcome.data["job"]["job_id"] == str(_JOB_ID)
    assert outcome.data["job"]["lead_status"] == "FAILED"
    assert outcome.data["job"]["lead_organization_id"] == str(_ORG_ID)


async def test_inspect_job_not_found_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_query(monkeypatch, lambda *a, **k: [])

    with pytest.raises(NotFoundError):
        await jobs.inspect_job_impl(_fake_pool(), uuid4(), statement_timeout_ms=5000)


async def test_inspect_job_truncates_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    long_error = "x" * 900
    _patch_run_query(monkeypatch, lambda *a, **k: [_job_detail_row(last_error=long_error)])

    outcome = await jobs.inspect_job_impl(_fake_pool(), _JOB_ID, statement_timeout_ms=5000)

    assert len(outcome.data["job"]["last_error"]) == 500


async def test_list_failed_jobs_default_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_job_row(job_id=uuid4()) for _ in range(5)]
    _patch_run_query(monkeypatch, lambda *a, **k: rows)

    outcome = await jobs.list_failed_jobs_impl(
        _fake_pool(), jobs.ListFailedJobsInput(), statement_timeout_ms=5000
    )

    assert outcome.row_count == 5
    assert outcome.truncated is False


async def test_list_failed_jobs_caps_and_flags_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tool asks the DB for `limit + 1` rows to distinguish "exactly the
    # cap" from "more exist" — the fake here returns exactly limit+1.
    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        fetch_limit = params["fetch_limit"]
        return [_job_row(job_id=uuid4()) for _ in range(fetch_limit)]

    _patch_run_query(monkeypatch, fake_run_query)

    outcome = await jobs.list_failed_jobs_impl(
        _fake_pool(), jobs.ListFailedJobsInput(limit=10), statement_timeout_ms=5000
    )

    assert outcome.row_count == 10
    assert outcome.truncated is True


async def test_list_failed_jobs_requested_limit_is_capped_at_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        captured["fetch_limit"] = params["fetch_limit"]
        return []

    _patch_run_query(monkeypatch, fake_run_query)

    await jobs.list_failed_jobs_impl(
        _fake_pool(), jobs.ListFailedJobsInput(limit=100), statement_timeout_ms=5000
    )

    # limit=100 (already the max) + 1 lookahead row
    assert captured["fetch_limit"] == 101


def test_list_failed_jobs_input_rejects_limit_above_100() -> None:
    with pytest.raises(ValueError):
        jobs.ListFailedJobsInput(limit=101)


def test_list_failed_jobs_input_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        jobs.ListFailedJobsInput(status="bogus")  # type: ignore[arg-type]
