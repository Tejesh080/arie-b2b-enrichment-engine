"""Row shapes for `organization_billing` / `billing_webhook_events`
(`migrations/0030_organization_billing.sql`). Plain data — no DB access here,
see `arie.billing.repository` for that."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

__all__ = [
    "PLANS",
    "SUBSCRIBED_STATUSES",
    "BillingWebhookEventRecord",
    "OrganizationBillingRecord",
]

PLANS: tuple[str, ...] = ("internal", "starter", "growth", "pro")
"""Mirrors `organization_billing.plan`'s CHECK constraint. `internal` is
never purchasable — see `arie.billing.plans.PLAN_DEFINITIONS`."""

SUBSCRIBED_STATUSES: tuple[str, ...] = ("active", "trialing")
"""The only `organization_billing.status` values under which a *non-internal*
plan's own entitlements apply — every other status (including `none`, the
default for a never-checked-out organization) falls back to the safe
`unsubscribed` floor. See `arie.billing.plans.resolve_organization_entitlements`."""


@dataclass(frozen=True)
class OrganizationBillingRecord:
    organization_id: UUID
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    plan: str
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None
    last_event_created_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_subscribed(self) -> bool:
        return self.plan == "internal" or self.status in SUBSCRIBED_STATUSES


@dataclass(frozen=True)
class BillingWebhookEventRecord:
    event_id: str
    event_type: str
    stripe_created_at: datetime
    payload_hash: str
    organization_id: UUID | None
    received_at: datetime
    processed_at: datetime | None
    processing_status: str
    sanitized_error: str | None


def billing_row_to_record(row: Mapping[str, Any]) -> OrganizationBillingRecord:
    return OrganizationBillingRecord(
        organization_id=row["organization_id"],
        stripe_customer_id=row["stripe_customer_id"],
        stripe_subscription_id=row["stripe_subscription_id"],
        plan=row["plan"],
        status=row["status"],
        current_period_start=row["current_period_start"],
        current_period_end=row["current_period_end"],
        cancel_at_period_end=row["cancel_at_period_end"],
        canceled_at=row["canceled_at"],
        last_event_created_at=row["last_event_created_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def webhook_event_row_to_record(row: Mapping[str, Any]) -> BillingWebhookEventRecord:
    return BillingWebhookEventRecord(
        event_id=row["event_id"],
        event_type=row["event_type"],
        stripe_created_at=row["stripe_created_at"],
        payload_hash=row["payload_hash"],
        organization_id=row["organization_id"],
        received_at=row["received_at"],
        processed_at=row["processed_at"],
        processing_status=row["processing_status"],
        sanitized_error=row["sanitized_error"],
    )
