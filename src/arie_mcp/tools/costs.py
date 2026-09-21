"""``get_enrichment_costs`` (spec § 9.9) — provider-level cost rollups only,
never a per-lead breakdown (that would grow unbounded with lead volume).
Bounded to a 90-day maximum lookback regardless of what a caller requests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import DbUnavailableError

MAX_LOOKBACK_DAYS = 90
DEFAULT_LOOKBACK_DAYS = 7

_GROUP_COLUMNS = {"provider": "provider", "day": "day"}


class CostRollupRow(BaseModel):
    group_key: str
    cost_usd: float
    credits_used: float | None
    call_count: int
    """Cost-bearing calls only — cache hits are excluded (a cache hit does
    not invoke the vendor and is reported separately in cache_hit_count).
    Compare inspect_provider_health's own call_count, which counts every
    consultation including cache hits — the two tools answer different
    questions ("did this cost money" vs "was this provider consulted at
    all"), not disagreeing measurements of the same thing."""
    cache_hit_count: int


class EnrichmentCostsReport(BaseModel):
    effective_since: datetime
    group_by: Literal["provider", "day"]
    organization_id: UUID | None
    provider: str | None
    rows: list[CostRollupRow]


def _effective_since(since: datetime | None) -> datetime:
    now = datetime.now(UTC)
    floor = now - timedelta(days=MAX_LOOKBACK_DAYS)
    if since is None:
        return now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    since_aware = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    return max(since_aware, floor)


def _collect(
    pool: ConnectionPool,
    *,
    organization_id: UUID | None,
    provider: str | None,
    since: datetime | None,
    group_by: Literal["provider", "day"],
    statement_timeout_ms: int,
) -> EnrichmentCostsReport:
    effective_since = _effective_since(since)
    col = _GROUP_COLUMNS[group_by]

    clauses = ["day >= %(since)s"]
    params: dict[str, Any] = {"since": effective_since}
    if provider is not None:
        clauses.append("provider = %(provider)s")
        params["provider"] = provider
    if organization_id is not None:
        clauses.append("organization_id = %(organization_id)s")
        params["organization_id"] = str(organization_id)

    sql = (
        f"SELECT {col} AS group_key, sum(cost_usd) AS cost_usd, sum(credits_used) AS credits_used, "
        f"sum(call_count) AS call_count, sum(cache_hit_count) AS cache_hit_count "
        f"FROM mcp_diag.v_cost_rollup WHERE " + " AND ".join(clauses) + f" GROUP BY {col} "
        "ORDER BY cost_usd DESC NULLS LAST"
    )
    rows = db.run_query(pool, sql, params, statement_timeout_ms=statement_timeout_ms)

    return EnrichmentCostsReport(
        effective_since=effective_since,
        group_by=group_by,
        organization_id=organization_id,
        provider=provider,
        rows=[
            CostRollupRow(
                group_key=str(row["group_key"]),
                cost_usd=float(row["cost_usd"] or 0),
                credits_used=float(row["credits_used"])
                if row["credits_used"] is not None
                else None,
                call_count=row["call_count"] or 0,
                cache_hit_count=row["cache_hit_count"] or 0,
            )
            for row in rows
        ],
    )


async def get_enrichment_costs_impl(
    pool: ConnectionPool | None,
    *,
    organization_id: UUID | None,
    provider: str | None,
    since: datetime | None,
    group_by: Literal["provider", "day"],
    statement_timeout_ms: int,
) -> ToolOutcome:
    if pool is None:
        raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")

    report = await asyncio.to_thread(
        _collect,
        pool,
        organization_id=organization_id,
        provider=provider,
        since=since,
        group_by=group_by,
        statement_timeout_ms=statement_timeout_ms,
    )
    return ToolOutcome(data=report.model_dump(mode="json"), row_count=len(report.rows))
