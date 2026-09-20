"""The stdio MCP server entrypoint.

Launch: ``python -m arie_mcp.server``. Transport is stdio only in V0.1
(spec § 5) — there is no network listener anywhere in this module.

Pool, settings, and the audit logger are built once in :func:`build_server`
and captured by each tool's closure — a single-process, single-instance
stdio server has no need for the lifespan/dependency-injection machinery a
multi-connection server would; that would be complexity this slice doesn't
need, not a shortcut around one it does.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from psycopg_pool import ConnectionPool
from pydantic import Field

from arie_mcp import __version__, db
from arie_mcp.audit import AuditLogger
from arie_mcp.envelope import ToolOutcome, ToolResult, run_tool
from arie_mcp.errors import DbUnavailableError
from arie_mcp.settings import Settings, get_settings
from arie_mcp.tools import health, jobs

_LOGGER = logging.getLogger("arie_mcp.server")

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

_INSTRUCTIONS = (
    "Read-only runtime diagnostics for the ARIE backend (this repository). "
    "Every tool is safe to call at any time: none of them write application "
    "state. get_system_health works even if the API process is down. "
    "inspect_job/list_failed_jobs need MCP_READONLY_DATABASE_URL configured; "
    "if it is not, they report DB_UNAVAILABLE rather than falling back to any "
    "other database credential."
)


def build_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or get_settings()
    audit_logger = AuditLogger(settings.audit_log_dir)
    pool: ConnectionPool | None = (
        db.build_pool(settings.readonly_database_url) if settings.database_configured else None
    )

    mcp = MCPServer(name="arie-mcp", version=__version__, instructions=_INSTRUCTIONS)

    @mcp.tool(
        description=(
            "Consolidated system health: database reachability, schema "
            "migration status, job queue depth, and worker fleet liveness. "
            "Works even when the API process is down."
        ),
        annotations=_READ_ONLY,
    )
    async def get_system_health() -> ToolResult:
        async def _body() -> ToolOutcome:
            return await health.get_system_health_impl(
                pool, statement_timeout_ms=settings.db_statement_timeout_ms
            )

        return await run_tool(
            "get_system_health",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={},
        )

    @mcp.tool(
        description=(
            "Look up a single job by id: its queue status, attempt count, "
            "last error (truncated), and its parent lead's status/is_shadow/"
            "organization_id. Returns NOT_FOUND if the job_id doesn't exist."
        ),
        annotations=_READ_ONLY,
    )
    async def inspect_job(job_id: UUID) -> ToolResult:
        async def _body() -> ToolOutcome:
            if pool is None:
                raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")
            return await jobs.inspect_job_impl(
                pool, job_id, statement_timeout_ms=settings.db_statement_timeout_ms
            )

        return await run_tool(
            "inspect_job",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={"job_id": str(job_id)},
        )

    @mcp.tool(
        description=(
            "List jobs in 'failed' or 'dead_letter' status, optionally "
            "filtered by status/job_type/since, newest first. Capped at 100 "
            "rows regardless of the requested limit; `truncated=true` means "
            "more rows exist beyond the cap."
        ),
        annotations=_READ_ONLY,
    )
    async def list_failed_jobs(
        status: Literal["failed", "dead_letter"] | None = None,
        job_type: str | None = None,
        since: datetime | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ToolResult:
        # Flat keyword arguments, not one wrapping pydantic-model parameter
        # (unlike inspect_job's job_id) — a caller invoking this tool with no
        # arguments at all must work, and a single required model-typed
        # parameter has no way to be "absent" at the MCP protocol layer.
        # `ListFailedJobsInput` still owns the actual field validation
        # (ge=1/le=100 on `limit`, the status enum); this is just how those
        # same fields reach the tool signature.
        params = jobs.ListFailedJobsInput(
            status=status, job_type=job_type, since=since, limit=limit
        )

        async def _body() -> ToolOutcome:
            if pool is None:
                raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")
            return await jobs.list_failed_jobs_impl(
                pool, params, statement_timeout_ms=settings.db_statement_timeout_ms
            )

        return await run_tool(
            "list_failed_jobs",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload=params.model_dump(mode="json"),
        )

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = build_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
