"""``inspect_routing_decision`` (spec § 9.10) — the acquisition-order audit
trail for one lead: why ARIE continued buying evidence, or stopped, at each
step. Reads ``voi_decisions`` (which already stores rejected candidates,
not just chosen ones) and the lead's own status/is_shadow context — no
``persons``/``companies`` column is reachable through either.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import DbUnavailableError, NotFoundError


class RoutingStep(BaseModel):
    step_number: int
    candidate_provider: str
    p_flips_decision: float
    business_value: float
    expected_cost: float
    latency_penalty: float
    net_evoi: float
    chosen: bool
    confidence_before: float | None
    confidence_after: float | None
    created_at: datetime


class LeadRoutingReport(BaseModel):
    lead_id: UUID
    lead_status: str
    lead_is_shadow: bool
    steps: list[RoutingStep]


_STEPS_SQL = """
    SELECT step_number, candidate_provider, p_flips_decision, business_value,
           expected_cost, latency_penalty, net_evoi, chosen,
           confidence_before, confidence_after, created_at
    FROM mcp_diag.v_voi_decisions
    WHERE lead_id = %(lead_id)s
    ORDER BY step_number
"""

_LEAD_SQL = """
    SELECT status, is_shadow FROM mcp_diag.v_lead_summary WHERE lead_id = %(lead_id)s
"""


def _collect(
    pool: ConnectionPool, lead_id: UUID, *, statement_timeout_ms: int
) -> LeadRoutingReport:
    params = {"lead_id": str(lead_id)}
    step_rows = db.run_query(pool, _STEPS_SQL, params, statement_timeout_ms=statement_timeout_ms)
    if not step_rows:
        raise NotFoundError(f"no routing decisions recorded for lead_id={lead_id}")

    # voi_decisions.lead_id has a FK to leads(lead_id) ON DELETE CASCADE, so
    # having at least one step row guarantees the lead itself still exists.
    lead_rows = db.run_query(pool, _LEAD_SQL, params, statement_timeout_ms=statement_timeout_ms)
    lead_row = lead_rows[0]

    return LeadRoutingReport(
        lead_id=lead_id,
        lead_status=lead_row["status"],
        lead_is_shadow=lead_row["is_shadow"],
        steps=[RoutingStep.model_validate(row) for row in step_rows],
    )


async def inspect_routing_decision_impl(
    pool: ConnectionPool | None, lead_id: UUID, *, statement_timeout_ms: int
) -> ToolOutcome:
    if pool is None:
        raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")

    report = await asyncio.to_thread(
        _collect, pool, lead_id, statement_timeout_ms=statement_timeout_ms
    )
    return ToolOutcome(data=report.model_dump(mode="json"), row_count=len(report.steps))
