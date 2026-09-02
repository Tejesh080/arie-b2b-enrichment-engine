""" "When I log in, what should I do?" — one bounded, read-only aggregate.

M7 Slice 7, Parts H/Q. Composes existing, already-tested pieces; there is
almost no new logic in this module. Priority counts and the top-5 "work
today" list reuse `arie.copilot_service`'s own bounded lead pool and
`arie.copilot.rank_work_today` verbatim — Part H2's explicit "do not
duplicate ranking logic" — and the rest (latest batch, open proposals,
feedback summary) are one call each into modules M7 already shipped.

**No LLM call. No write. No side effect.** Every function below only reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import psycopg

from arie.batches import BatchRecord, list_batches
from arie.copilot import CopilotLeadReference, rank_work_today, to_reference
from arie.copilot_service import _fetch_lead_pool
from arie.feedback import FeedbackAggregate, aggregate_feedback
from arie.intelligence.proposals import ProposalRecord, list_proposals
from arie.recommendations import CustomerPriority

__all__ = [
    "TOP_LEADS_LIMIT",
    "DashboardSummary",
    "load_dashboard",
]

TOP_LEADS_LIMIT = 5


@dataclass(frozen=True)
class DashboardSummary:
    priority_counts: dict[str, int]
    """All four `CustomerPriority` values, zero-filled — over the same
    bounded recent-activity pool `arie.copilot_service` uses, not a full
    table scan (see that module's own `POOL_LIMIT` docstring)."""
    top_leads: tuple[CopilotLeadReference, ...]
    """Up to `TOP_LEADS_LIMIT`, via `arie.copilot.rank_work_today` — the
    exact ranking `POST /copilot/query`'s `work_today` intent already uses."""
    latest_batch: BatchRecord | None
    open_proposals: tuple[ProposalRecord, ...]
    feedback: FeedbackAggregate


def load_dashboard(
    conn: psycopg.Connection, *, organization_id: UUID, user_id: UUID
) -> DashboardSummary:
    pool = _fetch_lead_pool(conn, organization_id=organization_id, user_id=user_id)
    summaries = [row.summary for row in pool]

    priority_counts = {str(p): 0 for p in CustomerPriority}
    for summary in summaries:
        priority_counts[str(summary.priority)] += 1

    top_leads = tuple(to_reference(s) for s in rank_work_today(summaries)[:TOP_LEADS_LIMIT])

    batches = list_batches(conn, organization_id=organization_id, limit=1)
    latest_batch = batches[0] if batches else None

    proposals = list_proposals(conn, organization_id=organization_id, limit=20)
    open_proposals = tuple(p for p in proposals if p.is_open)[:5]

    feedback = aggregate_feedback(conn, organization_id=organization_id)

    return DashboardSummary(
        priority_counts=priority_counts,
        top_leads=top_leads,
        latest_batch=latest_batch,
        open_proposals=open_proposals,
        feedback=feedback,
    )
