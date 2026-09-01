"""The one authoritative entitlement service (Productization M6 Part 5/6).
Every plan-gated decision in this codebase resolves through
:func:`resolve_organization_entitlements` — never a scattered `if plan ==
...` / `if stripe_status == ...` at the call site, matching this repo's own
brief for this milestone.

**Plan vs. subscription status, resolved in one place.** `plan` (Productization
M6 Part 6) says which entitlement tier an organization is *entitled to
receive*; `organization_billing.status` (Stripe's own vocabulary, mirrored by
`migrations/0030_organization_billing.sql`) says whether that entitlement is
*currently active*. Only `internal` (grandfathered, no Stripe relationship at
all) and a `starter`/`growth`/`pro` plan with `status` in
:data:`arie.billing.models.SUBSCRIBED_STATUSES` (`active`/`trialing`) grant
that plan's own limits. Every other combination — `none` (never checked out),
`past_due`, `canceled`, `unpaid`, `incomplete`, `incomplete_expired`, `paused`
— falls back to :data:`UNSUBSCRIBED`, a small, safe, non-purchasable floor.
This is "prefer safe failure over free indefinite service" (Part 8), applied
uniformly rather than re-decided per status.

**Not billing enforcement itself.** This module only *computes* the ceiling.
Lead/CSV-row/spend enforcement stays exactly where Productization M4 built it
(`arie.limits.enforce_lead_quota`/`enforce_csv_row_quota`, reading
`organizations.max_leads_per_month` etc.) — :func:`sync_organization_limits`
keeps those columns equal to the resolved entitlement's numbers every time
billing state changes, rather than duplicating the enforcement query path.
Member-count and live-provider-feature entitlements have no equivalent
existing column, so they're enforced directly against a freshly resolved
:class:`EffectiveEntitlements` — see :func:`enforce_member_quota` and
:func:`is_live_provider_feature_allowed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import psycopg

from arie.billing.repository import get_billing
from arie.limits import OrganizationLimits, set_limits

__all__ = [
    "PLAN_DEFINITIONS",
    "UNSUBSCRIBED",
    "EffectiveEntitlements",
    "MemberQuotaExceededError",
    "enforce_member_quota",
    "is_live_provider_feature_allowed",
    "resolve_organization_entitlements",
    "sync_organization_limits",
]


@dataclass(frozen=True)
class EffectiveEntitlements:
    plan: str
    """The plan this organization is actually entitled to right now — may
    differ from `organization_billing.plan` when the subscription isn't
    currently active; see the module docstring. `"unsubscribed"` is a
    synthetic value, never stored on a row."""

    max_leads_per_month: int
    max_csv_rows_per_upload: int
    max_modeled_spend_usd_per_month: float
    max_members: int
    live_provider_feature_allowed: bool
    """Whether this organization may configure a BYOK provider credential or
    set `execution_mode` away from `simulated` at all. **Not** the same
    control as `organization.execution_mode`/`PROVIDER_MODE`/
    `LIVE_AUTONOMY_ENABLED` — see `arie.api.main`'s provider/execution-mode
    routes and Part 20's own "plan entitlement != execution authorization"
    rule. Stripe can make a live feature *eligible*; it can never itself
    place a live provider call."""


UNSUBSCRIBED = EffectiveEntitlements(
    plan="unsubscribed",
    max_leads_per_month=25,
    max_csv_rows_per_upload=10,
    max_modeled_spend_usd_per_month=1.0,
    max_members=1,
    live_provider_feature_allowed=False,
)
"""The safe floor for "provisioned, but no active paid subscription" —
covers `status='none'` (brand-new self-service org, pre-Checkout),
`past_due`, `canceled`, `unpaid`, `incomplete`, `incomplete_expired`, and
`paused`. Generous enough to explore onboarding/simulated mode; nowhere near
enough to run a real workload — the intended nudge is "subscribe to do more,"
never "get stuck," so an owner is never blocked from the billing/settings
pages themselves (see `arie.api.main`'s route list — nothing about entitlement
resolution gates reading or managing an organization's own billing)."""

PLAN_DEFINITIONS: dict[str, EffectiveEntitlements] = {
    "internal": EffectiveEntitlements(
        plan="internal",
        max_leads_per_month=5000,
        max_csv_rows_per_upload=200,
        max_modeled_spend_usd_per_month=50.0,
        max_members=25,
        live_provider_feature_allowed=True,
    ),
    "starter": EffectiveEntitlements(
        plan="starter",
        max_leads_per_month=500,
        max_csv_rows_per_upload=50,
        max_modeled_spend_usd_per_month=10.0,
        max_members=3,
        live_provider_feature_allowed=True,
    ),
    "growth": EffectiveEntitlements(
        plan="growth",
        max_leads_per_month=5000,
        max_csv_rows_per_upload=200,
        max_modeled_spend_usd_per_month=50.0,
        max_members=10,
        live_provider_feature_allowed=True,
    ),
    "pro": EffectiveEntitlements(
        plan="pro",
        max_leads_per_month=25000,
        max_csv_rows_per_upload=200,
        max_modeled_spend_usd_per_month=250.0,
        max_members=50,
        live_provider_feature_allowed=True,
    ),
}
"""Sensible, easily-adjustable numbers — not real marketing prices (Part 6
explicitly asks not to spend time on those). `internal` matches the exact
ceilings Productization M4 originally hard-coded as every organization's
default (`migrations/0026_organization_limits.sql`), so the Legacy
Organization's effective behavior is unchanged by this migration. `growth`
mirrors the same numbers deliberately — it is meant to describe "current
production scale," which *is* the pre-M6 default."""


def resolve_organization_entitlements(
    conn: psycopg.Connection, *, organization_id: UUID
) -> EffectiveEntitlements:
    """The one function every plan-gated decision calls. Never raises for a
    missing billing row in practice — `arie.billing.repository.get_billing`
    asserts one exists, the same "an authenticated caller's own organization
    always exists" invariant `arie.limits.get_limits` already relies on,
    because every organization gets a row at provisioning time
    (`arie.provisioning.create_customer_organization`) or via this
    milestone's own backfill migration.
    """
    billing = get_billing(conn, organization_id=organization_id)
    if billing.plan == "internal":
        return PLAN_DEFINITIONS["internal"]
    if billing.is_subscribed:
        return PLAN_DEFINITIONS[billing.plan]
    return UNSUBSCRIBED


def sync_organization_limits(conn: psycopg.Connection, *, organization_id: UUID) -> None:
    """Write the resolved entitlement's lead/CSV/spend ceilings onto
    `organizations` — the columns `arie.limits.enforce_lead_quota`/
    `enforce_csv_row_quota` already read, unchanged by this milestone. Call
    this every time `organization_billing` changes (checkout completes, a
    subscription updates or cancels, a webhook resolves) so those columns
    stay the single enforcement source of truth rather than a second,
    independently-computed ceiling. Commits (via `update_organization`) —
    call as the last step of a caller's own transaction, same as every other
    commit-on-write helper in this codebase.
    """
    entitlements = resolve_organization_entitlements(conn, organization_id=organization_id)
    set_limits(
        conn,
        organization_id=organization_id,
        limits=OrganizationLimits(
            max_leads_per_month=entitlements.max_leads_per_month,
            max_csv_rows_per_upload=entitlements.max_csv_rows_per_upload,
            max_modeled_spend_usd_per_month=entitlements.max_modeled_spend_usd_per_month,
        ),
    )


class MemberQuotaExceededError(Exception):
    """Refused: this organization's plan does not permit another active
    member. Carries a human-readable message only, same shape as
    `arie.limits.LimitExceededError` — callers map this to a 402/403 at the
    API layer, never a 5xx."""


_LOCK_ORGANIZATION = "SELECT pg_advisory_xact_lock(hashtext(%(organization_id)s::text))"
_COUNT_ACTIVE_MEMBERS = """
    SELECT count(*) AS n FROM organization_members
    WHERE organization_id = %(organization_id)s AND status = 'active'
"""


def enforce_member_quota(conn: psycopg.Connection, *, organization_id: UUID) -> None:
    """Raise :class:`MemberQuotaExceededError` if this organization is
    already at (or would be at) its plan's `max_members`. Call before
    creating an invitation and again inside `accept_invitation`'s own
    transaction — an invitation created when the org was under quota may
    still be accepted after the org grew to fill it, so both checks matter
    and neither alone is race-safe. The advisory lock reuses
    `arie.members`'s own per-organization serialization key (same
    `hashtext(organization_id)` formula) so a concurrent invite-accept and a
    concurrent member-removal can't interleave around this count either.

    **A plan downgrade never removes existing members** (Part 9) — this only
    blocks *new* additions once an organization is at or over its (possibly
    now-lower) ceiling; it is never called anywhere that would delete a row.
    """
    entitlements = resolve_organization_entitlements(conn, organization_id=organization_id)
    with conn.cursor() as cur:
        cur.execute(_LOCK_ORGANIZATION, {"organization_id": organization_id})
        cur.execute(_COUNT_ACTIVE_MEMBERS, {"organization_id": organization_id})
        row = cur.fetchone()
    assert row is not None
    active_count = row[0]
    if active_count >= entitlements.max_members:
        raise MemberQuotaExceededError(
            f"this organization's {entitlements.plan} plan allows "
            f"{entitlements.max_members} member(s); {active_count} are already active"
        )


def is_live_provider_feature_allowed(conn: psycopg.Connection, *, organization_id: UUID) -> bool:
    """Whether this organization's plan entitles it to configure a live BYOK
    provider or set `execution_mode` away from `simulated` at all — checked
    *in addition to*, never instead of, every M5 execution-authorization
    guard (`organization.execution_mode`, worker `PROVIDER_MODE`,
    `LIVE_AUTONOMY_ENABLED`). See Part 20."""
    return resolve_organization_entitlements(
        conn, organization_id=organization_id
    ).live_provider_feature_allowed
