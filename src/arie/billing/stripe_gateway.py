"""The only module in this codebase that imports the Stripe SDK
(Productization M6 Part 3). Every other module — `arie.billing.service`, and
every API route — reaches Stripe only through the functions here, matching
the brief's own "do not make Stripe calls directly from arbitrary API
routes" instruction.

**Server-side only, always.** `STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` are
read once from `arie.config.STRIPE` (never a `NEXT_PUBLIC_`/`VITE_` variable,
never echoed in a response body) — the frontend is a separate repository and
reaches Stripe only via the URLs this module hands back
(`checkout.url`/`portal.url`), never a Stripe secret key of any kind.

**Price ids are never client-supplied.** `create_checkout_session` takes an
internal `plan` string (`"starter"`/`"growth"`/`"pro"`) and resolves the
actual Stripe Price id from `arie.config.StripeConfig.price_id_for_plan` —
server-configured environment, not request input — so a forged
`price_id` in a browser request can never buy a different plan's price.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import stripe

from arie.config import STRIPE

__all__ = [
    "CheckoutSession",
    "PortalSession",
    "StripeNotConfiguredError",
    "StripeWebhookSignatureError",
    "UnknownPlanError",
    "construct_event",
    "create_checkout_session",
    "create_portal_session",
    "get_or_create_customer",
    "subscription_period_bounds",
]


class StripeNotConfiguredError(Exception):
    """`STRIPE_SECRET_KEY` is not set. Raised rather than letting the Stripe
    SDK fail with its own, less actionable error the first time a call is
    attempted."""


class StripeWebhookSignatureError(Exception):
    """The `Stripe-Signature` header didn't verify against
    `STRIPE_WEBHOOK_SECRET` — wraps `stripe.SignatureVerificationError` so
    `arie.billing.service` never has to import the Stripe SDK's own
    exception hierarchy."""


class UnknownPlanError(Exception):
    """`plan` is not one of `arie.billing.models.PLANS`, or has no
    configured Stripe Price id (`STRIPE_PRICE_STARTER` etc. unset)."""


def _require_configured() -> None:
    if not STRIPE.configured:
        raise StripeNotConfiguredError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = STRIPE.secret_key


def get_or_create_customer(
    *, existing_customer_id: str | None, email: str, organization_id: str, organization_name: str
) -> str:
    """Reuse `existing_customer_id` if one is already on file
    (`organization_billing.stripe_customer_id`); otherwise create exactly
    one Stripe Customer for this organization, tagged with
    `organization_id` in `metadata` so a support engineer looking at the
    Stripe dashboard can trace a customer back to its ARIE organization.
    `metadata` is never trusted as an *authorization* signal anywhere in this
    codebase (see Part 4) — it is a display convenience only; the durable
    link a webhook actually resolves through is
    `organization_billing.stripe_customer_id` itself.
    """
    _require_configured()
    if existing_customer_id:
        return existing_customer_id
    customer = stripe.Customer.create(
        email=email,
        name=organization_name,
        metadata={"arie_organization_id": organization_id},
    )
    return customer.id


@dataclass(frozen=True)
class CheckoutSession:
    session_id: str
    url: str


def create_checkout_session(
    *,
    customer_id: str,
    plan: str,
    organization_id: str,
    success_url: str,
    cancel_url: str,
) -> CheckoutSession:
    """One Stripe Checkout Session in `subscription` mode. `client_reference_id`
    and `subscription_data.metadata` both carry `organization_id` — the
    session-level field for reconciling the Session itself, the subscription-
    level one so it rides onto the `Subscription` object every later webhook
    (`customer.subscription.updated`, etc.) carries too, without a second
    Stripe API call to look it up. Neither is trusted for authorization on
    its own — see `arie.billing.service.process_webhook_event`'s own
    docstring for why every webhook resolves the organization through
    `stripe_customer_id`, not metadata.
    """
    _require_configured()
    price_id = STRIPE.price_id_for_plan(plan)
    if price_id is None:
        raise UnknownPlanError(f"no configured Stripe price for plan {plan!r}")
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=organization_id,
        line_items=[{"price": price_id, "quantity": 1}],
        subscription_data={
            "metadata": {"arie_organization_id": organization_id, "arie_plan": plan}
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )
    assert session.url is not None
    return CheckoutSession(session_id=session.id, url=session.url)


@dataclass(frozen=True)
class PortalSession:
    url: str


def create_portal_session(*, customer_id: str, return_url: str) -> PortalSession:
    """A Stripe Customer Portal session — self-service plan change, payment
    method update, invoice history, and cancellation, entirely on Stripe's
    own hosted page. Creating this session never itself changes any
    entitlement; only a subsequent webhook does (Part 25 — "customer portal
    creation does not mutate entitlements")."""
    _require_configured()
    session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return PortalSession(url=session.url)


def construct_event(*, raw_body: bytes, signature_header: str) -> stripe.Event:
    """Verify `raw_body` against `STRIPE_WEBHOOK_SECRET` using the exact
    header Stripe sent, and return the parsed event only if it verifies.
    Raises :class:`StripeWebhookSignatureError` for a missing/invalid
    signature or an unconfigured webhook secret — a webhook endpoint must
    reject those outright (Part 4), never fall back to trusting the payload
    unverified.
    """
    _require_configured()
    if not STRIPE.webhook_configured:
        raise StripeWebhookSignatureError("STRIPE_WEBHOOK_SECRET is not configured")
    try:
        event: stripe.Event = stripe.Webhook.construct_event(
            raw_body, signature_header, STRIPE.webhook_secret
        )
    except (stripe.SignatureVerificationError, ValueError) as exc:
        raise StripeWebhookSignatureError(str(exc)) from exc
    return event


def subscription_period_bounds(
    subscription: Mapping[str, object],
) -> tuple[datetime | None, datetime | None]:
    """`(current_period_start, current_period_end)` from a Stripe
    `Subscription` object's first subscription item — Stripe moved these two
    fields from the subscription's top level onto each `items.data[]` entry
    in its 2025 API versions; reading them off the first item is the current
    documented shape and degrades to `(None, None)` for a subscription with
    no items rather than raising, since a missing period is recoverable
    (the next webhook carries a fresher one) and must never fail the whole
    event.
    """
    items = subscription.get("items")
    if not isinstance(items, Mapping):
        return None, None
    data = items.get("data")
    if not isinstance(data, list) or not data:
        return None, None
    first = data[0]
    if not isinstance(first, Mapping):
        return None, None
    start = first.get("current_period_start")
    end = first.get("current_period_end")
    return (
        datetime.fromtimestamp(start, tz=UTC) if isinstance(start, int) else None,
        datetime.fromtimestamp(end, tz=UTC) if isinstance(end, int) else None,
    )
