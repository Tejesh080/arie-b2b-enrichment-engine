"""Application layer for billing (Productization M6 Parts 3/4/25/26) — the
only module that combines `arie.billing.stripe_gateway` (Stripe I/O),
`arie.billing.repository` (durable state), `arie.billing.plans` (entitlement
sync), and `arie.audit`/`arie.email` into one coherent flow. `arie.api.main`'s
billing routes call only this module, never `stripe_gateway`/`repository`
directly — the same "one service layer, not scattered logic" shape Part 2/5
ask for.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from arie.audit import SYSTEM_ACTOR_ID, record_event
from arie.billing import stripe_gateway
from arie.billing.models import BillingWebhookEventRecord
from arie.billing.plans import sync_organization_limits
from arie.billing.repository import (
    get_billing,
    hash_payload,
    mark_webhook_event_processed,
    record_webhook_event,
    resolve_organization_id_by_customer,
    update_billing_from_subscription,
    update_billing_stripe_customer,
)
from arie.config import FRONTEND, STRIPE
from arie.email import get_notifier
from arie.observability.tracing import get_tracer, record_error, set_attributes, traced
from arie.organizations import get_organization

__all__ = [
    "HANDLED_EVENT_TYPES",
    "PURCHASABLE_PLANS",
    "NoStripeCustomerError",
    "PurchasableUnknownPlanError",
    "WebhookProcessResult",
    "open_billing_portal",
    "process_webhook_event",
    "start_checkout",
]

_LOGGER = logging.getLogger("arie.billing")
_TRACER = get_tracer("arie.billing")

PURCHASABLE_PLANS = ("starter", "growth", "pro")
"""`internal` is deliberately excluded — it is grandfathered, never sold
through Checkout (Part 6/7). Public so `arie.api.schemas` can validate a
`StartCheckoutRequest.plan` against the same set this module enforces,
rather than restating it."""


class PurchasableUnknownPlanError(ValueError):
    """`plan` is not one of :data:`PURCHASABLE_PLANS`."""


class NoStripeCustomerError(Exception):
    """This organization has never completed a Checkout session, so it has
    no Stripe Customer to open a Portal session for."""


def start_checkout(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    actor_email: str,
    plan: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create (or reuse) this organization's Stripe Customer and start a
    Checkout Session for `plan`. Returns the Checkout URL the frontend
    redirects the browser to. Raises :class:`PurchasableUnknownPlanError` for
    a `plan` outside `starter`/`growth`/`pro`, and
    :class:`~arie.billing.stripe_gateway.StripeNotConfiguredError` /
    :class:`~arie.billing.stripe_gateway.UnknownPlanError` if Stripe or that
    plan's price id isn't configured server-side yet.
    """
    if plan not in PURCHASABLE_PLANS:
        raise PurchasableUnknownPlanError(f"unknown purchasable plan: {plan!r}")

    with traced(
        _TRACER,
        "billing.checkout.start",
        attributes={"arie.organization_id": organization_id, "arie.plan": plan},
    ):
        organization = get_organization(conn, organization_id=organization_id)
        assert organization is not None
        billing = get_billing(conn, organization_id=organization_id)

        customer_id = stripe_gateway.get_or_create_customer(
            existing_customer_id=billing.stripe_customer_id,
            email=actor_email,
            organization_id=str(organization_id),
            organization_name=organization.name,
        )
        if customer_id != billing.stripe_customer_id:
            update_billing_stripe_customer(
                conn, organization_id=organization_id, stripe_customer_id=customer_id
            )

        session = stripe_gateway.create_checkout_session(
            customer_id=customer_id,
            plan=plan,
            organization_id=str(organization_id),
            success_url=success_url,
            cancel_url=cancel_url,
        )
        record_event(
            conn,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            event_type="billing.checkout_started",
            payload={"plan": plan, "checkout_session_id": session.session_id},
        )
        conn.commit()
        return session.url


def open_billing_portal(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    return_url: str | None = None,
) -> str:
    """Create a Stripe Customer Portal session. Raises
    :class:`NoStripeCustomerError` if this organization has never checked
    out. Does **not** touch any entitlement (Part 25) — only a subsequent
    webhook from an action the customer takes inside the Portal does.
    """
    with traced(
        _TRACER, "billing.portal.open", attributes={"arie.organization_id": organization_id}
    ):
        billing = get_billing(conn, organization_id=organization_id)
        if billing.stripe_customer_id is None:
            raise NoStripeCustomerError(
                f"organization {organization_id} has no Stripe customer yet"
            )

        session = stripe_gateway.create_portal_session(
            customer_id=billing.stripe_customer_id,
            return_url=return_url or f"{FRONTEND.base_url}/settings/billing",
        )
        record_event(
            conn,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            event_type="billing.portal_opened",
            payload={},
        )
        conn.commit()
        return session.url


HANDLED_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
        "invoice.payment_failed",
    }
)


@dataclass(frozen=True)
class WebhookProcessResult:
    status_code: int
    detail: str


def _plan_for_price_id(price_id: str | None) -> str | None:
    if price_id is None:
        return None
    for plan in PURCHASABLE_PLANS:
        if STRIPE.price_id_for_plan(plan) == price_id:
            return plan
    return None


def _first_item_price_id(subscription: dict[str, Any]) -> str | None:
    items = subscription.get("items", {})
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list) or not data:
        return None
    price = data[0].get("price") if isinstance(data[0], dict) else None
    return price.get("id") if isinstance(price, dict) else None


def _scheduled_to_cancel(subscription: Mapping[str, Any]) -> bool:
    """Whether this subscription is set to end rather than renew.

    Not simply `cancel_at_period_end`. On Stripe API versions from
    `2026-08-26` on, cancelling through the Customer Portal leaves
    `cancel_at_period_end` **false** and instead stamps `cancel_at` with the
    moment service ends — observed directly against a portal cancellation,
    not inferred from the changelog. Reading only the boolean therefore
    records "renewing normally" for a subscription Stripe has already
    scheduled to end, and the console's own "cancels on <date>" notice
    (`BillingPanel`, gated on this flag) silently never renders — a customer
    is told nothing about a cancellation they themselves requested.

    Either signal means the same thing to every consumer of this column, so
    both are folded into it here rather than at each call site.
    """
    if bool(subscription.get("cancel_at_period_end", False)):
        return True
    return subscription.get("cancel_at") is not None


def _apply_subscription(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    subscription: dict[str, Any],
    stripe_created_at: datetime,
) -> str | None:
    """Map a Stripe `Subscription` object onto `organization_billing` and
    resync entitlements. Returns a sanitized failure reason if the price id
    on the subscription doesn't map to a known plan (a configuration
    problem, not a transient one — retrying via Stripe's own redelivery
    wouldn't fix it), else `None`.
    """
    plan = _plan_for_price_id(_first_item_price_id(subscription))
    if plan is None:
        return "subscription price id does not map to a configured plan"

    period_start, period_end = stripe_gateway.subscription_period_bounds(subscription)
    canceled_at_raw = subscription.get("canceled_at")
    canceled_at = (
        datetime.fromtimestamp(canceled_at_raw, tz=UTC)
        if isinstance(canceled_at_raw, int)
        else None
    )

    applied = update_billing_from_subscription(
        conn,
        organization_id=organization_id,
        stripe_subscription_id=subscription["id"],
        plan=plan,
        status=subscription.get("status", "active"),
        current_period_start=period_start,
        current_period_end=period_end,
        cancel_at_period_end=_scheduled_to_cancel(subscription),
        canceled_at=canceled_at,
        stripe_created_at=stripe_created_at,
    )
    if applied:
        sync_organization_limits(conn, organization_id=organization_id)
    return None


def _resolve_organization(conn: psycopg.Connection, event: dict[str, Any]) -> UUID | None:
    """The organization this event belongs to, resolved only through a
    durable `stripe_customer_id` link — never through event `metadata` alone
    (Part 4). `checkout.session.completed` additionally has
    `client_reference_id` (the organization id `start_checkout` set), used
    only as a fallback if the customer link isn't resolvable yet — extremely
    unlikely since the customer is always created before Checkout, but never
    the sole source of truth on its own.
    """
    obj = event.get("data", {}).get("object", {})
    customer_id = obj.get("customer")
    if isinstance(customer_id, str):
        org_id = resolve_organization_id_by_customer(conn, stripe_customer_id=customer_id)
        if org_id is not None:
            return org_id
    client_reference_id = obj.get("client_reference_id")
    if isinstance(client_reference_id, str):
        try:
            return UUID(client_reference_id)
        except ValueError:
            return None
    return None


def process_webhook_event(
    pool: ConnectionPool, *, raw_body: bytes, signature_header: str
) -> WebhookProcessResult:
    """Verify, deduplicate, and apply one Stripe webhook delivery. Always
    returns a result rather than raising — the API route maps `status_code`
    straight onto the HTTP response, and a 5xx here is exactly what tells
    Stripe to retry (transient DB failure), while a 200 (even for a payload
    this handler ultimately couldn't act on, e.g. an unmapped price)
    deliberately stops Stripe from retrying a delivery that would only ever
    fail the same way.

    A thin tracing wrapper around :func:`_process_webhook_event_impl` (Part
    27) — kept separate so the implementation's own early-return control
    flow (several legitimate 200s before an organization or event type is
    even known) never has to be re-indented under a `with` block just to
    carry a span.
    """
    with traced(_TRACER, "billing.webhook.process") as span:
        result = _process_webhook_event_impl(
            pool, raw_body=raw_body, signature_header=signature_header
        )
        set_attributes(span, {"arie.webhook.status_code": result.status_code})
        if result.status_code >= 500:
            record_error(span, result.detail)
        return result


def _process_webhook_event_impl(
    pool: ConnectionPool, *, raw_body: bytes, signature_header: str
) -> WebhookProcessResult:
    try:
        event = stripe_gateway.construct_event(raw_body=raw_body, signature_header=signature_header)
    except stripe_gateway.StripeNotConfiguredError:
        # No signing secret means there is no way to tell Stripe's traffic
        # from anyone else's, so nothing here can be trusted — the same
        # outcome as a bad signature, reached for a different reason. It is
        # reported as 503 rather than 400 because the fault is this
        # deployment's, not the caller's: Stripe then retries with backoff,
        # and a delivery that arrives after the secret is configured
        # succeeds instead of having been permanently discarded.
        #
        # Uncaught, this was a 500 — safe (still nothing trusted) but wrong
        # twice over: it pages an operator for a configuration state rather
        # than a fault, and it buries the one line that says what to fix.
        _LOGGER.warning("stripe webhook received but STRIPE_WEBHOOK_SECRET is not configured")
        return WebhookProcessResult(503, "stripe webhook signing secret is not configured")
    except stripe_gateway.StripeWebhookSignatureError as exc:
        _LOGGER.warning("stripe webhook signature rejected: %s", exc)
        return WebhookProcessResult(400, "invalid signature")

    event_dict: dict[str, Any] = event.to_dict()
    event_type = str(event_dict.get("type", ""))
    event_id = str(event_dict.get("id", ""))
    stripe_created_at = datetime.fromtimestamp(int(event_dict.get("created", 0)), tz=UTC)

    if event_type not in HANDLED_EVENT_TYPES:
        with pool.connection() as conn:
            record_webhook_event(
                conn,
                event_id=event_id,
                event_type=event_type,
                stripe_created_at=stripe_created_at,
                payload_hash=hash_payload(raw_body),
                organization_id=None,
            )
            mark_webhook_event_processed(conn, event_id=event_id, processing_status="ignored")
            conn.commit()
        return WebhookProcessResult(200, "event type not handled")

    with pool.connection() as conn:
        organization_id = _resolve_organization(conn, event_dict)
        inserted: BillingWebhookEventRecord | None = record_webhook_event(
            conn,
            event_id=event_id,
            event_type=event_type,
            stripe_created_at=stripe_created_at,
            payload_hash=hash_payload(raw_body),
            organization_id=organization_id,
        )
        if inserted is None:
            conn.commit()
            return WebhookProcessResult(200, "duplicate event")

        if organization_id is None:
            mark_webhook_event_processed(
                conn,
                event_id=event_id,
                processing_status="failed",
                sanitized_error="could not resolve organization for this event",
            )
            conn.commit()
            _LOGGER.error(
                "stripe webhook %s (%s) could not be resolved to an organization",
                event_id,
                event_type,
            )
            return WebhookProcessResult(200, "organization not resolvable")

        try:
            error: str | None = None
            obj = event_dict.get("data", {}).get("object", {})

            if event_type == "checkout.session.completed":
                record_event(
                    conn,
                    organization_id=organization_id,
                    actor_user_id=SYSTEM_ACTOR_ID,
                    event_type="billing.checkout_completed",
                    payload={"checkout_session_id": obj.get("id", "")},
                )
            elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
                error = _apply_subscription(
                    conn,
                    organization_id=organization_id,
                    subscription=obj,
                    stripe_created_at=stripe_created_at,
                )
                if error is None:
                    audit_type = (
                        "billing.subscription_activated"
                        if event_type == "customer.subscription.created"
                        else "billing.subscription_changed"
                    )
                    record_event(
                        conn,
                        organization_id=organization_id,
                        actor_user_id=SYSTEM_ACTOR_ID,
                        event_type=audit_type,
                        payload={
                            "status": obj.get("status", ""),
                            "cancel_at_period_end": _scheduled_to_cancel(obj),
                        },
                    )
            elif event_type == "customer.subscription.deleted":
                billing = get_billing(conn, organization_id=organization_id)
                update_billing_from_subscription(
                    conn,
                    organization_id=organization_id,
                    stripe_subscription_id=obj.get("id", ""),
                    plan=billing.plan,
                    status="canceled",
                    current_period_start=billing.current_period_start,
                    current_period_end=billing.current_period_end,
                    cancel_at_period_end=False,
                    canceled_at=datetime.now(UTC),
                    stripe_created_at=stripe_created_at,
                )
                sync_organization_limits(conn, organization_id=organization_id)
                record_event(
                    conn,
                    organization_id=organization_id,
                    actor_user_id=SYSTEM_ACTOR_ID,
                    event_type="billing.subscription_canceled",
                    payload={},
                )
            elif event_type == "invoice.payment_failed":
                record_event(
                    conn,
                    organization_id=organization_id,
                    actor_user_id=SYSTEM_ACTOR_ID,
                    event_type="billing.payment_failed",
                    payload={},
                )
                _notify_payment_failed(conn, organization_id=organization_id)
            # invoice.paid: durably recorded by record_webhook_event above; the
            # authoritative status transition to `active` arrives via
            # customer.subscription.updated, so nothing further to apply here.

            mark_webhook_event_processed(
                conn,
                event_id=event_id,
                processing_status="failed" if error else "processed",
                organization_id=organization_id,
                sanitized_error=error,
            )
            conn.commit()
            return WebhookProcessResult(200, error or "processed")
        except Exception:
            conn.rollback()
            _LOGGER.exception("error processing stripe webhook %s (%s)", event_id, event_type)
            with pool.connection() as conn2:
                mark_webhook_event_processed(
                    conn2,
                    event_id=event_id,
                    processing_status="failed",
                    organization_id=organization_id,
                    sanitized_error="internal error during processing",
                )
                conn2.commit()
            return WebhookProcessResult(500, "internal error processing webhook")


def _notify_payment_failed(conn: psycopg.Connection, *, organization_id: UUID) -> None:
    """Best-effort email to every owner/admin — never raises into the
    webhook transaction; a notification failure must not fail entitlement
    processing. Local import (`arie.members`) avoids a module-level import
    cycle (`arie.members` -> `arie.audit`/`arie.auth`; neither imports
    billing, but keeping this import local matches the same defensive style
    `arie.billing.service._resolve_organization` already uses elsewhere in
    this module for optional lookups)."""
    from arie.members import list_members
    from arie.supabase_admin import get_user_email

    try:
        organization = get_organization(conn, organization_id=organization_id)
        assert organization is not None
        notifier = get_notifier()
        portal_url = f"{FRONTEND.base_url}/settings/billing"
        for member in list_members(conn, organization_id=organization_id):
            if member.role not in ("owner", "admin"):
                continue
            email = get_user_email(member.user_id)
            if email is None:
                continue
            notifier.send_payment_problem(
                to_email=email,
                organization_name=organization.name,
                reason="a subscription invoice failed to pay",
                portal_url=portal_url,
            )
    except Exception:
        _LOGGER.exception(
            "failed to send payment-failure notification for organization %s", organization_id
        )
