"""Owns every read/write of `organization_billing` and
`billing_webhook_events` (`migrations/0030_organization_billing.sql`). Pure
data access — no Stripe SDK calls (`arie.billing.stripe_gateway`), no
orchestration (`arie.billing.service`), matching this codebase's usual split
between a table-owning module and the endpoints/services that use it.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.billing.models import (
    BillingWebhookEventRecord,
    OrganizationBillingRecord,
    billing_row_to_record,
    webhook_event_row_to_record,
)

__all__ = [
    "get_billing",
    "hash_payload",
    "mark_webhook_event_processed",
    "record_webhook_event",
    "resolve_organization_id_by_customer",
    "update_billing_from_subscription",
    "update_billing_stripe_customer",
]

_BILLING_COLUMNS = """
    organization_id, stripe_customer_id, stripe_subscription_id, plan, status,
    current_period_start, current_period_end, cancel_at_period_end, canceled_at,
    last_event_created_at, created_at, updated_at
"""

_SELECT_BILLING = f"SELECT {_BILLING_COLUMNS} FROM organization_billing WHERE organization_id = %(organization_id)s"


def get_billing(conn: psycopg.Connection, *, organization_id: UUID) -> OrganizationBillingRecord:
    """Every organization has exactly one billing row by construction — see
    the migration's own backfill and `arie.provisioning
    .create_customer_organization`. Asserts rather than returning `None`,
    the same "an authenticated caller's own organization always exists"
    invariant `arie.limits.get_limits` relies on.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_BILLING, {"organization_id": organization_id})
        row = cur.fetchone()
    assert row is not None, f"organization {organization_id} has no billing row"
    return billing_row_to_record(row)


_SELECT_ORG_BY_CUSTOMER = "SELECT organization_id FROM organization_billing WHERE stripe_customer_id = %(stripe_customer_id)s"


def resolve_organization_id_by_customer(
    conn: psycopg.Connection, *, stripe_customer_id: str
) -> UUID | None:
    """The organization owning a given Stripe customer, or `None`. **The only
    way a webhook resolves `organization_id`** — never metadata alone (Part
    4's "not allow metadata alone to mutate arbitrary org" requirement); see
    `arie.billing.service.process_webhook_event`."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_ORG_BY_CUSTOMER, {"stripe_customer_id": stripe_customer_id})
        row = cur.fetchone()
    return row[0] if row is not None else None


_UPDATE_STRIPE_CUSTOMER = """
    UPDATE organization_billing SET stripe_customer_id = %(stripe_customer_id)s, updated_at = now()
    WHERE organization_id = %(organization_id)s
"""


def update_billing_stripe_customer(
    conn: psycopg.Connection, *, organization_id: UUID, stripe_customer_id: str
) -> None:
    """Persist a newly created/looked-up Stripe Customer id. Does not commit
    — always called as part of a larger transaction (`arie.billing.service
    .start_checkout`), matching every other write helper in this codebase.
    """
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_STRIPE_CUSTOMER,
            {"organization_id": organization_id, "stripe_customer_id": stripe_customer_id},
        )


_UPDATE_FROM_SUBSCRIPTION = """
    UPDATE organization_billing
    SET stripe_subscription_id = %(stripe_subscription_id)s,
        plan = %(plan)s,
        status = %(status)s,
        current_period_start = %(current_period_start)s,
        current_period_end = %(current_period_end)s,
        cancel_at_period_end = %(cancel_at_period_end)s,
        canceled_at = %(canceled_at)s,
        last_event_created_at = %(stripe_created_at)s,
        updated_at = now()
    WHERE organization_id = %(organization_id)s
      AND (last_event_created_at IS NULL OR last_event_created_at <= %(stripe_created_at)s)
    RETURNING organization_id
"""
"""The `last_event_created_at`-guarded `WHERE` is the out-of-order defense
(Part 26): an update whose own event is *older* than the last one already
applied matches zero rows rather than rolling subscription state backward —
`arie.billing.service` checks the returned row count and reports the event as
harmlessly stale rather than failed."""


def update_billing_from_subscription(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    stripe_subscription_id: str,
    plan: str,
    status: str,
    current_period_start: datetime | None,
    current_period_end: datetime | None,
    cancel_at_period_end: bool,
    canceled_at: datetime | None,
    stripe_created_at: datetime,
) -> bool:
    """Apply a subscription snapshot from a Stripe event. Returns `True` if
    the row was actually updated (this event was current or newer than the
    last one applied), `False` if it was discarded as stale. Does not commit.
    """
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_FROM_SUBSCRIPTION,
            {
                "organization_id": organization_id,
                "stripe_subscription_id": stripe_subscription_id,
                "plan": plan,
                "status": status,
                "current_period_start": current_period_start,
                "current_period_end": current_period_end,
                "cancel_at_period_end": cancel_at_period_end,
                "canceled_at": canceled_at,
                "stripe_created_at": stripe_created_at,
            },
        )
        row = cur.fetchone()
    return row is not None


def hash_payload(raw_body: bytes) -> str:
    """SHA-256 of the raw webhook body — retained for debugging/dedup
    correlation without persisting the full Stripe payload (which can carry
    more customer/card-adjacent metadata than this table needs to retain
    durably). Never used as a security check — signature verification
    (`arie.billing.stripe_gateway.construct_event`) is what proves a payload
    genuinely came from Stripe."""
    return hashlib.sha256(raw_body).hexdigest()


_INSERT_WEBHOOK_EVENT = """
    INSERT INTO billing_webhook_events (
        event_id, event_type, stripe_created_at, payload_hash, organization_id
    ) VALUES (
        %(event_id)s, %(event_type)s, %(stripe_created_at)s, %(payload_hash)s, %(organization_id)s
    )
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id, event_type, stripe_created_at, payload_hash, organization_id,
              received_at, processed_at, processing_status, sanitized_error
"""


def record_webhook_event(
    conn: psycopg.Connection,
    *,
    event_id: str,
    event_type: str,
    stripe_created_at: datetime,
    payload_hash: str,
    organization_id: UUID | None,
) -> BillingWebhookEventRecord | None:
    """Insert a durable, idempotent record of this Stripe event. Returns
    `None` if `event_id` was already recorded (a duplicate delivery) — Stripe
    explicitly documents redelivery as normal, and the primary key on
    `event_id` is what makes a second delivery a no-op INSERT rather than a
    second processing pass. Does not commit; the caller
    (`arie.billing.service.process_webhook_event`) commits once after this
    row and any entitlement-changing update land together.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _INSERT_WEBHOOK_EVENT,
            {
                "event_id": event_id,
                "event_type": event_type,
                "stripe_created_at": stripe_created_at,
                "payload_hash": payload_hash,
                "organization_id": organization_id,
            },
        )
        row = cur.fetchone()
    return webhook_event_row_to_record(row) if row is not None else None


_MARK_PROCESSED = """
    UPDATE billing_webhook_events
    SET processing_status = %(processing_status)s, processed_at = now(),
        sanitized_error = %(sanitized_error)s, organization_id = COALESCE(organization_id, %(organization_id)s)
    WHERE event_id = %(event_id)s
"""


def mark_webhook_event_processed(
    conn: psycopg.Connection,
    *,
    event_id: str,
    processing_status: str,
    organization_id: UUID | None = None,
    sanitized_error: str | None = None,
) -> None:
    """Stamp the terminal outcome of processing one event. `sanitized_error`
    must never carry a raw exception `str()` that could echo request
    internals — callers pass a short, pre-written classification (see
    `arie.billing.service`'s own error handling), the same discipline
    `arie.audit.record_event`'s payload contract already documents for a
    different table. Does not commit — the caller does, once, after this and
    any entitlement update land together.
    """
    with conn.cursor() as cur:
        cur.execute(
            _MARK_PROCESSED,
            {
                "event_id": event_id,
                "processing_status": processing_status,
                "organization_id": organization_id,
                "sanitized_error": sanitized_error,
            },
        )
