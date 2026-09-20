"""``inspect_job`` and ``list_failed_jobs`` (spec § 9.4, § 9.5).

Both read exclusively through ``mcp_diag.v_job_detail`` /
``mcp_diag.v_failed_jobs`` (migrations/0039) — job/lead identifiers and
statuses only, never ``persons``/``companies`` columns, so there is no path
by which a name, email, or domain reaches either tool's output.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import NotFoundError
from arie_mcp.limits import cap_limit, truncate_str
from arie_mcp.redaction import redact


class JobDetail(BaseModel):
    job_id: UUID
    lead_id: UUID | None
    job_type: str
    status: str
    attempt_count: int
    next_retry_at: datetime | None
    locked_by: str | None
    locked_at: datetime | None
    last_error: str | None
    created_at: datetime
    lead_status: str | None
    lead_is_shadow: bool | None
    lead_organization_id: UUID | None


class FailedJob(BaseModel):
    job_id: UUID
    lead_id: UUID | None
    job_type: str
    status: str
    attempt_count: int
    next_retry_at: datetime | None
    locked_by: str | None
    locked_at: datetime | None
    last_error: str | None
    created_at: datetime


class ListFailedJobsInput(BaseModel):
    status: Literal["failed", "dead_letter"] | None = None
    job_type: str | None = None
    since: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)


def _row_to_job_detail(row: dict[str, Any]) -> JobDetail:
    row = dict(row)
    # Redact before truncating — truncating first could cut a secret in half,
    # leaving an unredacted fragment past the point a pattern still matches.
    row["last_error"] = truncate_str(redact(row.get("last_error")))
    return JobDetail.model_validate(row)


def _row_to_failed_job(row: dict[str, Any]) -> FailedJob:
    row = dict(row)
    row["last_error"] = truncate_str(redact(row.get("last_error")))
    return FailedJob.model_validate(row)


def _inspect_job(pool: ConnectionPool, job_id: UUID, *, statement_timeout_ms: int) -> JobDetail:
    rows = db.run_query(
        pool,
        "SELECT * FROM mcp_diag.v_job_detail WHERE job_id = %(job_id)s",
        {"job_id": str(job_id)},
        statement_timeout_ms=statement_timeout_ms,
    )
    if not rows:
        raise NotFoundError(f"no job found with job_id={job_id}")
    return _row_to_job_detail(rows[0])


async def inspect_job_impl(
    pool: ConnectionPool, job_id: UUID, *, statement_timeout_ms: int
) -> ToolOutcome:
    detail = await asyncio.to_thread(
        _inspect_job, pool, job_id, statement_timeout_ms=statement_timeout_ms
    )
    return ToolOutcome(data={"job": detail.model_dump(mode="json")}, row_count=1)


def _list_failed_jobs(
    pool: ConnectionPool, params: ListFailedJobsInput, *, statement_timeout_ms: int
) -> tuple[list[FailedJob], bool]:
    limit = cap_limit(params.limit)

    clauses = ["1 = 1"]
    query_params: dict[str, Any] = {"fetch_limit": limit + 1}
    if params.status is not None:
        clauses.append("status = %(status)s")
        query_params["status"] = params.status
    if params.job_type is not None:
        clauses.append("job_type = %(job_type)s")
        query_params["job_type"] = params.job_type
    if params.since is not None:
        clauses.append("created_at >= %(since)s")
        query_params["since"] = params.since

    sql = (
        "SELECT * FROM mcp_diag.v_failed_jobs WHERE "
        + " AND ".join(clauses)
        + " ORDER BY created_at DESC LIMIT %(fetch_limit)s"
    )
    rows = db.run_query(pool, sql, query_params, statement_timeout_ms=statement_timeout_ms)

    truncated = len(rows) > limit
    jobs = [_row_to_failed_job(row) for row in rows[:limit]]
    return jobs, truncated


async def list_failed_jobs_impl(
    pool: ConnectionPool, params: ListFailedJobsInput, *, statement_timeout_ms: int
) -> ToolOutcome:
    jobs, truncated = await asyncio.to_thread(
        _list_failed_jobs, pool, params, statement_timeout_ms=statement_timeout_ms
    )
    return ToolOutcome(
        data={"jobs": [job.model_dump(mode="json") for job in jobs]},
        row_count=len(jobs),
        truncated=truncated,
    )
