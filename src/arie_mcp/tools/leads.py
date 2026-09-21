"""``list_recent_leads`` — the read-only lead-discovery tool.

Closes the gap that blocked decision-quality inspection: ``inspect_routing_decision``
takes a ``lead_id`` a caller must already have. Reads exclusively through
``mcp_diag.v_lead_recent`` (migrations/0041) — the same PII-free identity
columns ``v_lead_summary`` (migrations/0040) already exposes, plus a
decision receipt's scalar score/bounds/confidence/decision/stop_reason
columns where one exists, plus the lead's most recent job id/status for
chaining into ``inspect_job``. No email/name/company/raw-evidence column is
reachable through this view under any join — ``decision_receipts.evidence_snapshot``
is deliberately not selected.

``evidence_sufficiency`` itself is not included: deriving it requires an
organization's ICP qualify/reject thresholds (``arie.recommendations``), which
this view does not resolve — re-deriving that logic a second time in SQL
would be exactly the duplicated-ledger risk this schema's existing tools
avoid elsewhere (see ``configuration.py``'s own docstring). A caller can read
``receipt_score_lower``/``receipt_score_upper`` directly as a bounds-width
proxy instead.
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

_LIST_RECENT_LEADS_MAX_ROWS = 50
_LIST_RECENT_LEADS_DEFAULT_ROWS = 20


class RecentLead(BaseModel):
    lead_id: UUID
    organization_id: UUID | None
    status: str
    is_shadow: bool
    created_at: datetime
    updated_at: datetime
    receipt_decision: str | None
    receipt_autonomous: bool | None
    receipt_confidence: float | None
    receipt_score_value: float | None
    receipt_score_lower: float | None
    receipt_score_upper: float | None
    receipt_stop_reason: str | None
    latest_job_id: UUID | None
    latest_job_status: str | None


def _list_recent_leads(
    pool: ConnectionPool,
    *,
    organization_id: UUID | None,
    limit: int,
    statement_timeout_ms: int,
) -> tuple[list[RecentLead], bool]:
    capped = cap_limit(
        limit, default=_LIST_RECENT_LEADS_DEFAULT_ROWS, maximum=_LIST_RECENT_LEADS_MAX_ROWS
    )

    clauses = ["1 = 1"]
    params: dict[str, object] = {"fetch_limit": capped + 1}
    if organization_id is not None:
        clauses.append("organization_id = %(organization_id)s")
        params["organization_id"] = str(organization_id)

    sql = (
        "SELECT lead_id, organization_id, status, is_shadow, created_at, updated_at, "
        "receipt_decision, receipt_autonomous, receipt_confidence, receipt_score_value, "
        "receipt_score_lower, receipt_score_upper, receipt_stop_reason, "
        "latest_job_id, latest_job_status "
        "FROM mcp_diag.v_lead_recent WHERE " + " AND ".join(clauses) + " "
        "ORDER BY created_at DESC LIMIT %(fetch_limit)s"
    )
    rows = db.run_query(pool, sql, params, statement_timeout_ms=statement_timeout_ms)

    truncated = len(rows) > capped
    leads = [RecentLead.model_validate(row) for row in rows[:capped]]
    return leads, truncated


async def list_recent_leads_impl(
    pool: ConnectionPool | None,
    *,
    organization_id: UUID | None,
    limit: int,
    statement_timeout_ms: int,
) -> ToolOutcome:
    if pool is None:
        raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")

    leads, truncated = await asyncio.to_thread(
        _list_recent_leads,
        pool,
        organization_id=organization_id,
        limit=limit,
        statement_timeout_ms=statement_timeout_ms,
    )
    return ToolOutcome(
        data={"leads": [lead.model_dump(mode="json") for lead in leads]},
        row_count=len(leads),
        truncated=truncated,
    )
