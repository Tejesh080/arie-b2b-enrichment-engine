"""Redaction proven against real seeded rows read back through the real
read-only role — not just the unit-level regex tests in
``tests/mcp/unit/test_redaction.py``. Uses the same
``last_error`` field both ``inspect_job`` and ``get_recent_errors`` expose.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from arie_mcp import db
from arie_mcp.tools import errors, jobs
from tests.mcp.conftest import SeededJob, delete_job_and_lead, seed_job

pytestmark = pytest.mark.integration

_SECRET_BEARING_ERROR = (
    "lookup failed for jordan.ellis@example-corp.test using "
    "postgresql://svc:hunter2@db.internal:5432/prod and "
    "Authorization: Bearer sk-live-abc123DEF456"
)


@pytest.fixture
def seeded_secret_bearing_job(admin_conn: psycopg.Connection) -> Iterator[SeededJob]:
    seeded = seed_job(admin_conn, last_error=_SECRET_BEARING_ERROR)
    yield seeded
    delete_job_and_lead(admin_conn, seeded)


async def test_inspect_job_redacts_secrets_in_last_error(
    mcp_readonly_database_url: str, seeded_secret_bearing_job: SeededJob
) -> None:
    pool = db.build_pool(mcp_readonly_database_url)
    try:
        outcome = await jobs.inspect_job_impl(
            pool, seeded_secret_bearing_job.job_id, statement_timeout_ms=5000
        )
    finally:
        pool.close()

    last_error = outcome.data["job"]["last_error"]
    assert "jordan.ellis@example-corp.test" not in last_error
    assert "hunter2" not in last_error
    assert "db.internal" not in last_error
    assert "sk-live-abc123DEF456" not in last_error
    assert "[REDACTED]" in last_error


async def test_get_recent_errors_redacts_secrets_in_recent_job_failures(
    mcp_readonly_database_url: str, seeded_secret_bearing_job: SeededJob
) -> None:
    pool = db.build_pool(mcp_readonly_database_url)
    try:
        outcome = await errors.get_recent_errors_impl(
            pool, window_minutes=60, statement_timeout_ms=5000
        )
    finally:
        pool.close()

    matching = [
        row
        for row in outcome.data["recent_job_failures"]
        if row["job_id"] == str(seeded_secret_bearing_job.job_id)
    ]
    assert len(matching) == 1
    last_error = matching[0]["last_error"]
    assert "jordan.ellis@example-corp.test" not in last_error
    assert "hunter2" not in last_error
    assert "sk-live-abc123DEF456" not in last_error
    assert "[REDACTED]" in last_error
