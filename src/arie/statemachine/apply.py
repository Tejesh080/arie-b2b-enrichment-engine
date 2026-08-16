"""Transactional lead-state transitions with optimistic concurrency.

``leads.version`` (0001_init.sql: "optimistic concurrency guard") exists for
exactly this: two workers should never be able to silently clobber each
other's view of a lead. This module is the only place that ever writes
``leads.status``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from arie.core.types import LeadStatus

_UPDATE_LEAD_STATUS = """
    UPDATE leads
    SET status = %(status)s, version = version + 1, updated_at = now()
    WHERE lead_id = %(lead_id)s AND version = %(expected_version)s
    RETURNING version
"""

_INSERT_LEAD_EVENT = """
    INSERT INTO lead_events (lead_id, event_type, payload)
    VALUES (%(lead_id)s, %(event_type)s, %(payload)s)
"""


class OptimisticConcurrencyError(RuntimeError):
    """Raised when a lead's version no longer matches what the caller expected.

    Means another writer transitioned this lead between when the caller read
    its version and when this transition tried to apply — the caller's view
    was stale. Never partial: the UPDATE's WHERE clause means either the
    transition fully applies or nothing does.
    """

    def __init__(self, lead_id: UUID, expected_version: int) -> None:
        self.lead_id = lead_id
        self.expected_version = expected_version
        super().__init__(
            f"lead {lead_id} is not at version {expected_version} — "
            "it was changed by another writer since this job read it"
        )


@dataclass(frozen=True)
class TransitionResult:
    new_version: int


def apply_transition(
    conn: psycopg.Connection,
    *,
    lead_id: UUID,
    expected_version: int,
    new_status: LeadStatus,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> TransitionResult:
    """Move a lead to `new_status` and record the transition, atomically.

    Does **not** commit — the caller (``arie.jobs.worker``) commits this
    together with the job's own completion, in the same transaction. That
    single commit is what "job completion and lead-state transition commit
    atomically" means in practice: if either write fails, both roll back.

    Raises ``OptimisticConcurrencyError`` if `expected_version` is stale.
    Safe to retry after re-reading the lead's current version — this function
    makes no assumption about *why* the version didn't match, only that it
    didn't.
    """
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_LEAD_STATUS,
            {
                "status": str(new_status),
                "lead_id": lead_id,
                "expected_version": expected_version,
            },
        )
        row = cur.fetchone()
        if row is None:
            raise OptimisticConcurrencyError(lead_id, expected_version)
        new_version: int = row[0]

        cur.execute(
            _INSERT_LEAD_EVENT,
            {
                "lead_id": lead_id,
                "event_type": event_type,
                "payload": Jsonb(payload or {}),
            },
        )

    return TransitionResult(new_version=new_version)
