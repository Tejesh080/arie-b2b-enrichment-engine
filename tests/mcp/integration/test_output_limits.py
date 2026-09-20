"""Proves the row cap (spec § 11) against real seeded data: more than 100
matching jobs exist, and ``list_failed_jobs`` still returns at most 100,
flagged ``truncated``."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from arie_mcp import db
from arie_mcp.limits import MAX_ROWS
from arie_mcp.tools import jobs
from tests.mcp.conftest import SeededJob, delete_job_and_lead, seed_job

pytestmark = pytest.mark.integration

_ROW_COUNT = MAX_ROWS + 25


@pytest.fixture
def many_failed_jobs(admin_conn: psycopg.Connection) -> Iterator[list[SeededJob]]:
    seeded = [seed_job(admin_conn, job_type="tests-mcp-output-limits") for _ in range(_ROW_COUNT)]
    yield seeded
    for job in seeded:
        delete_job_and_lead(admin_conn, job)


async def test_list_failed_jobs_caps_at_max_rows_and_flags_truncated(
    mcp_readonly_database_url: str, many_failed_jobs: list[SeededJob]
) -> None:
    pool = db.build_pool(mcp_readonly_database_url)
    try:
        outcome = await jobs.list_failed_jobs_impl(
            pool,
            jobs.ListFailedJobsInput(job_type="tests-mcp-output-limits", limit=MAX_ROWS),
            statement_timeout_ms=5000,
        )
    finally:
        pool.close()

    assert outcome.row_count == MAX_ROWS
    assert len(outcome.data["jobs"]) == MAX_ROWS
    assert outcome.truncated is True


async def test_list_failed_jobs_default_page_size_is_smaller_than_max(
    mcp_readonly_database_url: str, many_failed_jobs: list[SeededJob]
) -> None:
    pool = db.build_pool(mcp_readonly_database_url)
    try:
        outcome = await jobs.list_failed_jobs_impl(
            pool,
            jobs.ListFailedJobsInput(job_type="tests-mcp-output-limits"),
            statement_timeout_ms=5000,
        )
    finally:
        pool.close()

    assert outcome.row_count == 20
    assert outcome.truncated is True
