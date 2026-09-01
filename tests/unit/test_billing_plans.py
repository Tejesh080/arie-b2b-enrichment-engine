"""Entitlement resolution (Productization M6 Part 5/6) — the pure mapping
logic in `arie.billing.plans`, exercised with a monkeypatched `get_billing`
so no live database is needed. DB-touching behavior (member-count
enforcement, `sync_organization_limits` writing `organizations`) is covered
by tests/integration/test_billing_integration.py instead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

import psycopg
import pytest

from arie.billing import plans as billing_plans
from arie.billing.models import OrganizationBillingRecord

_UNUSED_CONN = cast(psycopg.Connection, None)
_ORG_ID = uuid.uuid4()


def _billing(*, plan: str, status: str) -> OrganizationBillingRecord:
    now = datetime.now(UTC)
    return OrganizationBillingRecord(
        organization_id=_ORG_ID,
        stripe_customer_id=None,
        stripe_subscription_id=None,
        plan=plan,
        status=status,
        current_period_start=None,
        current_period_end=None,
        cancel_at_period_end=False,
        canceled_at=None,
        last_event_created_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    "plan,status,expected_plan",
    [
        ("internal", "none", "internal"),  # internal ignores status entirely
        ("internal", "canceled", "internal"),
        ("starter", "active", "starter"),
        ("starter", "trialing", "starter"),
        ("growth", "active", "growth"),
        ("pro", "trialing", "pro"),
        ("starter", "none", "unsubscribed"),
        ("starter", "past_due", "unsubscribed"),
        ("starter", "canceled", "unsubscribed"),
        ("starter", "unpaid", "unsubscribed"),
        ("starter", "incomplete", "unsubscribed"),
        ("starter", "incomplete_expired", "unsubscribed"),
        ("starter", "paused", "unsubscribed"),
    ],
)
def test_resolve_organization_entitlements_matrix(
    monkeypatch: pytest.MonkeyPatch, plan: str, status: str, expected_plan: str
) -> None:
    monkeypatch.setattr(
        billing_plans,
        "get_billing",
        lambda conn, *, organization_id: _billing(plan=plan, status=status),
    )
    entitlements = billing_plans.resolve_organization_entitlements(
        _UNUSED_CONN, organization_id=_ORG_ID
    )
    assert entitlements.plan == expected_plan


def test_unsubscribed_is_the_safe_floor_below_every_purchasable_plan() -> None:
    floor = billing_plans.UNSUBSCRIBED
    for name in ("starter", "growth", "pro", "internal"):
        plan = billing_plans.PLAN_DEFINITIONS[name]
        assert floor.max_leads_per_month <= plan.max_leads_per_month
        assert floor.max_csv_rows_per_upload <= plan.max_csv_rows_per_upload
        assert floor.max_modeled_spend_usd_per_month <= plan.max_modeled_spend_usd_per_month
        assert floor.max_members <= plan.max_members
    assert floor.live_provider_feature_allowed is False


def test_every_purchasable_plan_allows_live_provider_features() -> None:
    for name in ("starter", "growth", "pro", "internal"):
        assert billing_plans.PLAN_DEFINITIONS[name].live_provider_feature_allowed is True


def test_internal_plan_matches_legacy_organization_defaults() -> None:
    # Mirrors migrations/0026_organization_limits.sql's original defaults —
    # the Legacy Organization's effective behavior must be unchanged by M6.
    internal = billing_plans.PLAN_DEFINITIONS["internal"]
    assert internal.max_leads_per_month == 5000
    assert internal.max_csv_rows_per_upload == 200
    assert internal.max_modeled_spend_usd_per_month == 50.0


def test_is_live_provider_feature_allowed_reflects_resolved_entitlements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        billing_plans,
        "get_billing",
        lambda conn, *, organization_id: _billing(plan="starter", status="none"),
    )
    assert (
        billing_plans.is_live_provider_feature_allowed(_UNUSED_CONN, organization_id=_ORG_ID)
        is False
    )


def test_billing_record_is_subscribed_property() -> None:
    assert _billing(plan="internal", status="none").is_subscribed is True
    assert _billing(plan="starter", status="active").is_subscribed is True
    assert _billing(plan="starter", status="trialing").is_subscribed is True
    assert _billing(plan="starter", status="past_due").is_subscribed is False
    assert _billing(plan="starter", status="none").is_subscribed is False
