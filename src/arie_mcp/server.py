"""The stdio MCP server entrypoint, and the shared tool registry both
transports serve.

Launch: ``python -m arie_mcp.server``. This module's own ``main()`` is
stdio-only (spec § 5) — no network listener, no auth, unchanged since V0.1.
:func:`build_server` is what's shared: it registers all 11 read-only tools
once, identically regardless of caller, and optionally accepts OAuth wiring
that only the remote entrypoint (``arie_mcp.http_server``, a separate
process/deployment) ever supplies — see that module for the Streamable HTTP
+ OAuth transport. Tool implementations are never duplicated between the two;
only how a caller reaches them differs.

Pool, settings, and the audit logger are built once in :func:`build_server`
and captured by each tool's closure — a single-process, single-instance
stdio server has no need for the lifespan/dependency-injection machinery a
multi-connection server would; that would be complexity this slice doesn't
need, not a shortcut around one it does.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from psycopg_pool import ConnectionPool
from pydantic import Field

from arie_mcp import __version__, db
from arie_mcp.audit import AuditLogger
from arie_mcp.envelope import ToolOutcome, ToolResult, run_tool
from arie_mcp.errors import DbUnavailableError
from arie_mcp.settings import Settings, get_settings
from arie_mcp.tools import (
    configuration,
    contract,
    costs,
    errors,
    health,
    jobs,
    migrations,
    providers,
    routing,
)

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


def build_server(
    settings: Settings | None = None,
    *,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> MCPServer:
    """Build the one tool registry both transports serve.

    The 11 tools below are registered identically regardless of caller —
    stdio (``main``, below) always calls this with every ``auth*`` argument
    left at its default ``None``, exactly as before this parameter existed.
    The remote Streamable HTTP entrypoint (``arie_mcp.http_server``) is the
    only caller that ever passes them; nothing about *which* tools exist or
    what they do changes with transport, only how — or whether — a caller
    must authenticate to reach them.
    """
    settings = settings or get_settings()
    audit_logger = AuditLogger(settings.audit_log_dir)
    pool: ConnectionPool | None = (
        db.build_pool(settings.readonly_database_url) if settings.database_configured else None
    )

    mcp = MCPServer(
        name="arie-mcp",
        version=__version__,
        instructions=_INSTRUCTIONS,
        auth_server_provider=auth_server_provider,
        token_verifier=token_verifier,
        auth=auth,
    )

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

    @mcp.tool(
        description=(
            "Read the running API process's live OpenAPI schema (GET "
            "/openapi.json against ARIE_MCP_API_BASE_URL, default "
            "http://localhost:8000). The only tool that talks to the API "
            "process rather than the database. Optional path_prefix filters "
            "routes; include_schemas=true also returns component schemas "
            "(bounded — truncated=true if they'd exceed the response cap)."
        ),
        annotations=_READ_ONLY,
    )
    async def inspect_api_contract(
        path_prefix: str | None = None, include_schemas: bool = False
    ) -> ToolResult:
        async def _body() -> ToolOutcome:
            return await contract.inspect_api_contract_impl(
                settings.api_base_url,
                path_prefix=path_prefix,
                include_schemas=include_schemas,
                timeout_seconds=settings.tool_timeout_seconds,
            )

        return await run_tool(
            "inspect_api_contract",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={"path_prefix": path_prefix, "include_schemas": include_schemas},
        )

    @mcp.tool(
        description=(
            "Applied vs. pending migrations, with checksum-integrity status "
            "for each applied one. Read-only — no apply capability exists "
            "here or anywhere in this server."
        ),
        annotations=_READ_ONLY,
    )
    async def inspect_migrations() -> ToolResult:
        async def _body() -> ToolOutcome:
            return await migrations.inspect_migrations_impl(
                pool, statement_timeout_ms=settings.db_statement_timeout_ms
            )

        return await run_tool(
            "inspect_migrations",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={},
        )

    @mcp.tool(
        description=(
            "Bounded, aggregated recent-failure summary: job-queue failures "
            "and provider-call errors/suppressions within window_minutes "
            "(default 60, max 1440). Counts by category plus up to 20 most "
            "recent rows of each — never an unbounded raw log dump."
        ),
        annotations=_READ_ONLY,
    )
    async def get_recent_errors(
        window_minutes: Annotated[int, Field(ge=1, le=1440)] = 60,
    ) -> ToolResult:
        async def _body() -> ToolOutcome:
            return await errors.get_recent_errors_impl(
                pool,
                window_minutes=window_minutes,
                statement_timeout_ms=settings.db_statement_timeout_ms,
            )

        return await run_tool(
            "get_recent_errors",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={"window_minutes": window_minutes},
        )

    @mcp.tool(
        description=(
            "The real ARIE live-provider registry (arie.live.providers) — "
            "which providers are wired, their default cheapest-first "
            "acquisition order, and whether an order override is "
            "configured. No DB call; process-static."
        ),
        annotations=_READ_ONLY,
    )
    async def list_providers() -> ToolResult:
        async def _body() -> ToolOutcome:
            return await providers.list_providers_impl()

        return await run_tool(
            "list_providers", _body, settings=settings, audit_logger=audit_logger, input_payload={}
        )

    @mcp.tool(
        description=(
            "One provider's availability: whether it is currently inside a "
            "quota cooldown (and until when), recent call/error/cache-hit "
            "counts, and last success/call times. organization_id scopes to "
            "one tenant; omit it for a global aggregate across all "
            "organizations. Never returns credentials or raw payloads."
        ),
        annotations=_READ_ONLY,
    )
    async def inspect_provider_health(
        provider: str, organization_id: UUID | None = None
    ) -> ToolResult:
        async def _body() -> ToolOutcome:
            return await providers.inspect_provider_health_impl(
                pool,
                provider,
                organization_id,
                statement_timeout_ms=settings.db_statement_timeout_ms,
            )

        return await run_tool(
            "inspect_provider_health",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={
                "provider": provider,
                "organization_id": str(organization_id) if organization_id else None,
            },
        )

    @mcp.tool(
        description=(
            "Provider-level enrichment cost rollup (cost_usd/credits_used/"
            "call_count/cache_hit_count), grouped by provider or by day. "
            "Bounded to a 90-day maximum lookback regardless of `since`; "
            "defaults to the last 7 days. Optional provider/organization_id "
            "filters. No per-lead breakdown."
        ),
        annotations=_READ_ONLY,
    )
    async def get_enrichment_costs(
        organization_id: UUID | None = None,
        provider: str | None = None,
        since: datetime | None = None,
        group_by: Literal["provider", "day"] = "provider",
    ) -> ToolResult:
        async def _body() -> ToolOutcome:
            return await costs.get_enrichment_costs_impl(
                pool,
                organization_id=organization_id,
                provider=provider,
                since=since,
                group_by=group_by,
                statement_timeout_ms=settings.db_statement_timeout_ms,
            )

        return await run_tool(
            "get_enrichment_costs",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={
                "organization_id": str(organization_id) if organization_id else None,
                "provider": provider,
                "since": since.isoformat() if since else None,
                "group_by": group_by,
            },
        )

    @mcp.tool(
        description=(
            "Why ARIE continued acquiring evidence, or stopped, for one "
            "lead: the full voi_decisions step-by-step trail (candidate "
            "provider, expected value/cost, net EVoI, whether chosen), plus "
            "the lead's own status/is_shadow. NOT_FOUND if the lead has no "
            "routing decisions recorded."
        ),
        annotations=_READ_ONLY,
    )
    async def inspect_routing_decision(lead_id: UUID) -> ToolResult:
        async def _body() -> ToolOutcome:
            return await routing.inspect_routing_decision_impl(
                pool, lead_id, statement_timeout_ms=settings.db_statement_timeout_ms
            )

        return await run_tool(
            "inspect_routing_decision",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={"lead_id": str(lead_id)},
        )

    @mcp.tool(
        description=(
            "Configuration presence, never values: process-level env-var "
            "category booleans (is Stripe/Supabase/a provider/etc "
            "configured — never the key itself), plus — only when "
            "organization_id is given — that organization's execution_mode "
            "and resolved plan entitlements (tier name and numeric limits, "
            "never a Stripe customer/subscription id)."
        ),
        annotations=_READ_ONLY,
    )
    async def inspect_configuration(organization_id: UUID | None = None) -> ToolResult:
        async def _body() -> ToolOutcome:
            return await configuration.inspect_configuration_impl(
                pool, organization_id, statement_timeout_ms=settings.db_statement_timeout_ms
            )

        return await run_tool(
            "inspect_configuration",
            _body,
            settings=settings,
            audit_logger=audit_logger,
            input_payload={"organization_id": str(organization_id) if organization_id else None},
        )

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = build_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
