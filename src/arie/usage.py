"""Tenant-scoped usage/cost visibility — Productization M3, Part 7.

**Not billing.** This reports the same "modelled cost" arithmetic
`arie.ledger.pricing`/`decision_receipts` already compute per lead, summed
over an organization and a date range — it is not a new pricing model and
it is not a claim about actual vendor invoices. See that module's own
docstring for the modelled-vs-billed distinction this deliberately does not
blur; the "modelled cost" caption is UI copy the frontend already owns
(`providerMode.ts`), not restated here.

**Deliberately does not read `v_pipeline_metrics`/`v_provider_health`/
`v_escalation_rate`.** Those views predate multi-tenancy (migrations 0002/
0005/0007/0009) and were never updated to filter or `GROUP BY
organization_id` — reading them for a single organization's usage report
would silently aggregate every other organization's activity into the
answer. Every query below is written fresh, directly against `leads`/
`provider_calls`/`model_calls`, filtered by `organization_id` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.statemachine.transitions import AWAITING_REVIEW, FAILURE, QUALIFIED, REJECTED

__all__ = ["UsageSummary", "get_usage_summary"]


@dataclass(frozen=True)
class UsageSummary:
    from_at: datetime
    to_at: datetime
    leads_processed: int
    """`leads.created_at` falling in `[from_at, to_at)` for this organization —
    "processed" meaning "entered the pipeline," not "reached a decision";
    a lead ingested near the end of the range may still show as `pending`."""
    qualified_count: int
    rejected_count: int
    review_count: int
    pending_count: int
    failed_count: int
    provider_calls: int
    """Billable calls only (`cache_hit = false`) — matches `LeadCost.provider_calls`."""
    cache_hits: int
    provider_cost_usd: float
    model_cost_usd: float

    @property
    def total_cost_usd(self) -> float:
        return self.provider_cost_usd + self.model_cost_usd


_SELECT_LEAD_STATUS_COUNTS = """
    SELECT status, count(*) AS n
    FROM leads
    WHERE organization_id = %(organization_id)s AND created_at >= %(from_at)s AND created_at < %(to_at)s
    GROUP BY status
"""

# Provider/model call costs are counted by their *own* timestamp
# (`requested_at`/`created_at`), not the lead's — a lead ingested just before
# the range boundary could still be enriched just after it, and "cost
# incurred during this window" is the more honest reading of a usage report
# than "cost for leads created during this window."
_SELECT_PROVIDER_CALL_TOTALS = """
    SELECT
        COALESCE(SUM(cost_usd) FILTER (WHERE cache_hit = false), 0) AS provider_cost_usd,
        count(*) FILTER (WHERE cache_hit = false) AS provider_calls,
        count(*) FILTER (WHERE cache_hit = true) AS cache_hits
    FROM provider_calls
    WHERE organization_id = %(organization_id)s
      AND requested_at >= %(from_at)s AND requested_at < %(to_at)s
"""

_SELECT_MODEL_CALL_TOTALS = """
    SELECT COALESCE(SUM(cost_usd), 0) AS model_cost_usd
    FROM model_calls
    WHERE organization_id = %(organization_id)s
      AND created_at >= %(from_at)s AND created_at < %(to_at)s
"""


def get_usage_summary(
    conn: psycopg.Connection, *, organization_id: UUID, from_at: datetime, to_at: datetime
) -> UsageSummary:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _SELECT_LEAD_STATUS_COUNTS,
            {"organization_id": organization_id, "from_at": from_at, "to_at": to_at},
        )
        status_counts = {row["status"]: row["n"] for row in cur.fetchall()}

        cur.execute(
            _SELECT_PROVIDER_CALL_TOTALS,
            {"organization_id": organization_id, "from_at": from_at, "to_at": to_at},
        )
        provider_row = cur.fetchone()

        cur.execute(
            _SELECT_MODEL_CALL_TOTALS,
            {"organization_id": organization_id, "from_at": from_at, "to_at": to_at},
        )
        model_row = cur.fetchone()
    assert provider_row is not None
    assert model_row is not None

    total_leads = sum(status_counts.values())
    qualified = sum(n for status, n in status_counts.items() if status in QUALIFIED)
    rejected = sum(n for status, n in status_counts.items() if status in REJECTED)
    review = sum(n for status, n in status_counts.items() if status in AWAITING_REVIEW)
    failed = sum(n for status, n in status_counts.items() if status in FAILURE)
    pending = max(0, total_leads - (qualified + rejected + review + failed))

    return UsageSummary(
        from_at=from_at,
        to_at=to_at,
        leads_processed=total_leads,
        qualified_count=qualified,
        rejected_count=rejected,
        review_count=review,
        pending_count=pending,
        failed_count=failed,
        provider_calls=provider_row["provider_calls"],
        cache_hits=provider_row["cache_hits"],
        provider_cost_usd=float(provider_row["provider_cost_usd"]),
        model_cost_usd=float(model_row["model_cost_usd"]),
    )
