"""`arie.billing.service`'s pure helpers — price/plan mapping and Stripe
subscription-object parsing, tested without a live database or Stripe
account. Checkout/Portal/webhook orchestration is covered by
tests/integration/test_billing_integration.py instead.
"""

from __future__ import annotations

import pytest

from arie.billing import service as billing_service
from arie.billing.models import PLANS
from arie.config import StripeConfig


def test_purchasable_plans_excludes_internal() -> None:
    assert "internal" not in billing_service.PURCHASABLE_PLANS
    assert set(billing_service.PURCHASABLE_PLANS) < set(PLANS)


def test_plan_for_price_id_resolves_a_configured_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        billing_service,
        "STRIPE",
        StripeConfig(
            secret_key="sk_test",
            webhook_secret="whsec_test",
            price_starter="price_starter_123",
            price_growth="price_growth_123",
            price_pro="price_pro_123",
        ),
    )
    assert billing_service._plan_for_price_id("price_growth_123") == "growth"
    assert billing_service._plan_for_price_id("price_unknown") is None
    assert billing_service._plan_for_price_id(None) is None


def test_first_item_price_id_reads_the_nested_shape() -> None:
    subscription = {"items": {"data": [{"price": {"id": "price_abc"}}]}}
    assert billing_service._first_item_price_id(subscription) == "price_abc"


def test_first_item_price_id_handles_missing_items() -> None:
    assert billing_service._first_item_price_id({}) is None
    assert billing_service._first_item_price_id({"items": {"data": []}}) is None


def test_start_checkout_rejects_an_unpurchasable_plan_before_any_stripe_or_db_call() -> None:
    with pytest.raises(billing_service.PurchasableUnknownPlanError):
        billing_service.start_checkout(
            None,  # type: ignore[arg-type]
            organization_id=None,  # type: ignore[arg-type]
            actor_user_id=None,  # type: ignore[arg-type]
            actor_email="owner@example.com",
            plan="internal",
            success_url="https://app.example/success",
            cancel_url="https://app.example/cancel",
        )


def test_scheduled_to_cancel_reads_the_legacy_boolean() -> None:
    assert billing_service._scheduled_to_cancel({"cancel_at_period_end": True}) is True
    assert billing_service._scheduled_to_cancel({"cancel_at_period_end": False}) is False
    assert billing_service._scheduled_to_cancel({}) is False


def test_scheduled_to_cancel_reads_cancel_at_when_the_boolean_stays_false() -> None:
    """The shape a Customer Portal cancellation actually produces on Stripe
    API versions from `2026-08-26` on: `cancel_at_period_end` stays `False`
    and `cancel_at` carries the end of service. Reading only the boolean
    recorded such a subscription as renewing normally, so the console's
    "cancels on <date>" notice never rendered for a cancellation the
    customer had just requested."""
    portal_cancellation = {
        "cancel_at_period_end": False,
        "cancel_at": 1_790_885_913,
        "canceled_at": 1_788_294_091,
        "status": "active",
    }
    assert billing_service._scheduled_to_cancel(portal_cancellation) is True


def test_scheduled_to_cancel_ignores_a_null_cancel_at() -> None:
    assert billing_service._scheduled_to_cancel({"cancel_at": None, "canceled_at": None}) is False
