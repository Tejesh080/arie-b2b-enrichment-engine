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

__all__ = ["SYSTEM_ACTOR_ID", "record_event"]

SYSTEM_ACTOR_ID: UUID = UUID("00000000-0000-0000-0000-000000000000")
"""The nil UUID, standing in for `actor_user_id` on an event with no human
behind it — Productization M6's Stripe webhook processing is the first
caller (`arie.billing.service`): a `checkout.session.completed` or
`customer.subscription.updated` delivery has no request-scoped user, only an
already-authorized Stripe account acting on a subscription a real owner
started. `actor_user_id` stays `NOT NULL` (`migrations/0024_organization
_invitations.sql`) rather than becoming nullable — a fixed, recognizable
sentinel is easier for an owner reading their own audit log to spot than a
`NULL` that could otherwise be read as "unknown," and it never collides with
a real Supabase `auth.users` id (all v4 UUIDs)."""

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
