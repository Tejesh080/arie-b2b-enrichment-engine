"""``list_organizations`` — the read-only tenant-discovery tool.

Exists to close one specific, confirmed gap: every other tool that accepts
``organization_id`` (``inspect_provider_health``, ``get_enrichment_costs``,
``inspect_configuration``) requires a caller to already have one from outside
MCP. Reads exclusively through ``mcp_diag.v_organization_activity``
(migrations/0041) — ``slug`` (not ``organizations.name``, the customer's own
business name) as the one display label, plus ``execution_mode`` and a
lightweight lead-count/last-activity rollup. No member, billing-secret, or
provider-secret column is reachable through this view under any join.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import DbUnavailableError
from arie_mcp.limits import cap_limit

_LIST_ORGANIZATIONS_MAX_ROWS = 50
_LIST_ORGANIZATIONS_DEFAULT_ROWS = 20


class OrganizationSummary(BaseModel):
    organization_id: UUID
    slug: str
    status: str
    execution_mode: str
    created_at: datetime
    billing_plan: str | None
    lead_count: int
    last_lead_created_at: datetime | None


_LIST_SQL = """
    SELECT organization_id, slug, status, execution_mode, created_at,
           billing_plan, lead_count, last_lead_created_at
    FROM mcp_diag.v_organization_activity
    ORDER BY created_at DESC
    LIMIT %(fetch_limit)s
"""


def _list_organizations(
    pool: ConnectionPool, *, limit: int, statement_timeout_ms: int
) -> tuple[list[OrganizationSummary], bool]:
    capped = cap_limit(
        limit, default=_LIST_ORGANIZATIONS_DEFAULT_ROWS, maximum=_LIST_ORGANIZATIONS_MAX_ROWS
    )
    rows = db.run_query(
        pool,
        _LIST_SQL,
        {"fetch_limit": capped + 1},
        statement_timeout_ms=statement_timeout_ms,
    )
    truncated = len(rows) > capped
    orgs = [OrganizationSummary.model_validate(row) for row in rows[:capped]]
    return orgs, truncated


async def list_organizations_impl(
    pool: ConnectionPool | None, *, limit: int, statement_timeout_ms: int
) -> ToolOutcome:
    if pool is None:
        raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")

    orgs, truncated = await asyncio.to_thread(
        _list_organizations, pool, limit=limit, statement_timeout_ms=statement_timeout_ms
    )
    return ToolOutcome(
        data={"organizations": [org.model_dump(mode="json") for org in orgs]},
        row_count=len(orgs),
        truncated=truncated,
    )
