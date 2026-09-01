"""`arie.billing.stripe_gateway`'s pure parsing helper —
`subscription_period_bounds` reads Stripe's post-Basil (2025-03-31) shape,
where `current_period_start`/`current_period_end` live on each subscription
*item*, not the subscription's own top level. Everything else in that module
makes a real Stripe API call and is exercised only against Stripe's test
mode / mocked at the service layer — see
tests/integration/test_billing_integration.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from arie.billing.stripe_gateway import subscription_period_bounds


def test_subscription_period_bounds_reads_the_first_item() -> None:
    subscription = {
        "id": "sub_123",
        "items": {
            "data": [
                {"current_period_start": 1_735_689_600, "current_period_end": 1_738_368_000},
                {"current_period_start": 999, "current_period_end": 999},
            ]
        },
    }
    start, end = subscription_period_bounds(subscription)
    assert start == datetime(2025, 1, 1, tzinfo=UTC)
    assert end == datetime(2025, 2, 1, tzinfo=UTC)


def test_subscription_period_bounds_handles_no_items_gracefully() -> None:
    assert subscription_period_bounds({"id": "sub_123", "items": {"data": []}}) == (None, None)


def test_subscription_period_bounds_handles_a_missing_items_key() -> None:
    assert subscription_period_bounds({"id": "sub_123"}) == (None, None)
