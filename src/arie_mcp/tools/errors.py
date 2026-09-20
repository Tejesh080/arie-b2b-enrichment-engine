"""``get_recent_errors`` (spec § 9.6) — a bounded, aggregated summary, not a
raw log dump. Categorizes by the two failure sources this schema actually
has structured data for: job-queue failures (``jobs.status IN ('failed',
'dead_letter')``) and provider-call errors/suppressions
(``provider_calls.error_kind``/``suppressed_reason``). There is no generic
application-error log table in this schema to draw a third category from —
this tool reports what the data actually supports, not an invented one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import DbUnavailableError
from arie_mcp.limits import MAX_STRING_LEN, truncate_str
from arie_mcp.redaction import redact

_RECENT_ROW_CAP = 20


class ErrorCount(BaseModel):
    key: str
    count: int


class RecentJobFailure(BaseModel):
    job_id: str
    job_type: str
    status: str
    last_error: str | None
    created_at: datetime


class RecentProviderError(BaseModel):
    call_id: str
    provider: str
    error_kind: str | None
    suppressed_reason: str | None
    requested_at: datetime


class RecentErrorsReport(BaseModel):
    window_minutes: int
    job_failure_count: int
    provider_error_count: int
    job_failures_by_type: list[ErrorCount]
    provider_errors_by_kind: list[ErrorCount]
    recent_job_failures: list[RecentJobFailure]
    recent_provider_errors: list[RecentProviderError]


_JOB_FAILURE_COUNTS_SQL = """
    SELECT job_type, count(*) AS n
    FROM mcp_diag.v_failed_jobs
    WHERE created_at >= now() - make_interval(mins => %(window_minutes)s)
    GROUP BY job_type
    ORDER BY n DESC
"""

_JOB_FAILURE_RECENT_SQL = """
    SELECT job_id, job_type, status, last_error, created_at
    FROM mcp_diag.v_failed_jobs
    WHERE created_at >= now() - make_interval(mins => %(window_minutes)s)
    ORDER BY created_at DESC
    LIMIT %(row_cap)s
"""

_PROVIDER_ERROR_COUNTS_SQL = """
    SELECT coalesce(error_kind, suppressed_reason, 'unknown') AS kind, count(*) AS n
    FROM mcp_diag.v_provider_call_errors
    WHERE requested_at >= now() - make_interval(mins => %(window_minutes)s)
    GROUP BY 1
    ORDER BY n DESC
"""

_PROVIDER_ERROR_RECENT_SQL = """
    SELECT call_id, provider, error_kind, suppressed_reason, requested_at
    FROM mcp_diag.v_provider_call_errors
    WHERE requested_at >= now() - make_interval(mins => %(window_minutes)s)
    ORDER BY requested_at DESC
    LIMIT %(row_cap)s
"""


def _collect(
    pool: ConnectionPool, *, window_minutes: int, statement_timeout_ms: int
) -> RecentErrorsReport:
    params: dict[str, Any] = {"window_minutes": window_minutes, "row_cap": _RECENT_ROW_CAP}

    job_counts = db.run_query(
        pool, _JOB_FAILURE_COUNTS_SQL, params, statement_timeout_ms=statement_timeout_ms
    )
    job_recent = db.run_query(
        pool, _JOB_FAILURE_RECENT_SQL, params, statement_timeout_ms=statement_timeout_ms
    )
    provider_counts = db.run_query(
        pool, _PROVIDER_ERROR_COUNTS_SQL, params, statement_timeout_ms=statement_timeout_ms
    )
    provider_recent = db.run_query(
        pool, _PROVIDER_ERROR_RECENT_SQL, params, statement_timeout_ms=statement_timeout_ms
    )

    recent_job_failures = [
        RecentJobFailure(
            job_id=str(row["job_id"]),
            job_type=row["job_type"],
            status=row["status"],
            last_error=truncate_str(redact(row["last_error"]), max_len=MAX_STRING_LEN),
            created_at=row["created_at"],
        )
        for row in job_recent
    ]
    recent_provider_errors = [
        RecentProviderError(
            call_id=str(row["call_id"]),
            provider=row["provider"],
            error_kind=row["error_kind"],
            suppressed_reason=row["suppressed_reason"],
            requested_at=row["requested_at"],
        )
        for row in provider_recent
    ]

    return RecentErrorsReport(
        window_minutes=window_minutes,
        job_failure_count=sum(row["n"] for row in job_counts),
        provider_error_count=sum(row["n"] for row in provider_counts),
        job_failures_by_type=[
            ErrorCount(key=row["job_type"], count=row["n"]) for row in job_counts
        ],
        provider_errors_by_kind=[
            ErrorCount(key=row["kind"], count=row["n"]) for row in provider_counts
        ],
        recent_job_failures=recent_job_failures,
        recent_provider_errors=recent_provider_errors,
    )


async def get_recent_errors_impl(
    pool: ConnectionPool | None, *, window_minutes: int, statement_timeout_ms: int
) -> ToolOutcome:
    if pool is None:
        raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")

    report = await asyncio.to_thread(
        _collect, pool, window_minutes=window_minutes, statement_timeout_ms=statement_timeout_ms
    )
    total = report.job_failure_count + report.provider_error_count
    shown = len(report.recent_job_failures) + len(report.recent_provider_errors)
    return ToolOutcome(
        data=report.model_dump(mode="json"),
        row_count=total,
        truncated=shown < total,
    )
