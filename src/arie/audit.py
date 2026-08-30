"""Append-only organization audit log (Productization M4 Part 2). Owns every
write to `organization_audit_events` (`migrations/0024_organization_invitations
.sql`) — a small, deliberately generic module: one function, no per-event-type
machinery, because the free-text `event_type` column is designed to grow new
values without a migration or a code change here.

**The payload contract, enforced by convention, not by this module's code:**
`payload` must never carry a secret, credential, raw invitation token, or any
value that would make this table something other than safe to show an
owner/admin verbatim. Every call site in this milestone passes only IDs,
role names, provider names, and status strings — see each call site's own
comment for why its particular payload is safe. `record_event` does not
(and cannot) validate this itself; the discipline belongs to the caller,
the same way `arie.apikeys.create_api_key`'s docstring places "never log the
raw key" on its own callers rather than trying to detect a secret shape.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

__all__ = ["record_event"]

_INSERT_EVENT = """
    INSERT INTO organization_audit_events (organization_id, actor_user_id, event_type, payload)
    VALUES (%(organization_id)s, %(actor_user_id)s, %(event_type)s, %(payload)s)
"""


def record_event(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one audit row. Does not commit — always called from inside a
    caller's own transaction (the action being audited and the audit row
    itself land together, or neither does), matching every other write
    helper in this codebase.
    """
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_EVENT,
            {
                "organization_id": organization_id,
                "actor_user_id": actor_user_id,
                "event_type": event_type,
                "payload": Jsonb(payload or {}),
            },
        )
