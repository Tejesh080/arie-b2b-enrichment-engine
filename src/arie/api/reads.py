"""Read queries for the runtime API.

Kept apart from ``arie.api.ingest`` because they answer to a different rule:
ingestion owns a transaction and is careful about what commits together, while
these are single-statement reads with no such obligation. Mixing them in one
module invites a read being quietly added inside the ingestion transaction and
widening it for no reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.core.types import LeadStatus

_SELECT_LEAD = """
    SELECT lead_id, status, version, source, external_ref,
           company_id, person_id, budget_usd_cap, created_at, updated_at
    FROM leads
    WHERE lead_id = %(lead_id)s
"""


@dataclass(frozen=True)
class LeadRecord:
    lead_id: UUID
    status: LeadStatus
    version: int
    source: str
    external_ref: str | None
    company_id: UUID | None
    person_id: UUID | None
    budget_usd_cap: Decimal
    created_at: datetime
    updated_at: datetime


def fetch_lead(conn: psycopg.Connection, lead_id: UUID) -> LeadRecord | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_LEAD, {"lead_id": lead_id})
        row = cur.fetchone()
    if row is None:
        return None
    return LeadRecord(
        lead_id=row["lead_id"],
        status=LeadStatus(row["status"]),
        version=row["version"],
        source=row["source"],
        external_ref=row["external_ref"],
        company_id=row["company_id"],
        person_id=row["person_id"],
        budget_usd_cap=row["budget_usd_cap"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
