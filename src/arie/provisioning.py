"""Self-service organization provisioning (Productization M6 Parts 10/11).
The one function that turns an already-authenticated, already email-verified
Supabase user (`arie.auth.resolve_verified_identity` — the same identity
`arie.invitations.accept_invitation` uses, for the same reason: this action
precedes any organization membership, so it cannot be gated by one) into a
brand-new organization they own.

**Atomic, not scattered inserts.** `organizations` + the owner
`organization_members` row + the `organization_billing` row (Productization
M6) all land in one transaction the caller commits once — a crash between
any two of them would otherwise strand an unusable organization (a org with
no owner, or no billing row `arie.billing.plans.resolve_organization
_entitlements` could assert against).

**Never lets a caller choose an existing organization.** The only
client-supplied input is a display name; `organization_id` is always
database-generated (`organizations.organization_id DEFAULT gen_random_uuid()`)
and `slug` is derived from the name server-side with a randomized retry on
collision — there is no code path here that accepts a caller-supplied id or
slug, so "attach myself to organization X by guessing its slug" is
structurally unreachable, not merely rejected.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from uuid import UUID

import psycopg

from arie.audit import record_event
from arie.billing.plans import sync_organization_limits

__all__ = [
    "InvalidOrganizationNameError",
    "ProvisioningResult",
    "SlugGenerationExhaustedError",
    "create_customer_organization",
]

_SLUGIFY_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_ATTEMPTS = 8


class InvalidOrganizationNameError(ValueError):
    """`organization_name` is empty or whitespace-only."""


class SlugGenerationExhaustedError(Exception):
    """Every randomized slug candidate collided — vanishingly unlikely
    (`_MAX_SLUG_ATTEMPTS` random 6-hex-char suffixes), reported rather than
    looping forever."""


@dataclass(frozen=True)
class ProvisioningResult:
    organization_id: UUID
    slug: str


def _slugify(name: str) -> str:
    base = _SLUGIFY_RE.sub("-", name.strip().lower()).strip("-")
    return base or "organization"


_INSERT_ORG = (
    "INSERT INTO organizations (name, slug) VALUES (%(name)s, %(slug)s) RETURNING organization_id"
)
_INSERT_OWNER_MEMBERSHIP = """
    INSERT INTO organization_members (organization_id, user_id, role, status)
    VALUES (%(organization_id)s, %(user_id)s, 'owner', 'active')
"""
# No explicit `organization_billing` insert here: `migrations/0033
# _organization_billing_bootstrap_trigger.sql`'s AFTER INSERT trigger on
# `organizations` creates the `plan='starter'`/`status='none'` row
# automatically the moment `_INSERT_ORG` above commits — the same safe,
# unsubscribed-floor starting state this function used to insert by hand.
# `arie.billing.plans.resolve_organization_entitlements` reads that straight
# into `UNSUBSCRIBED`; `sync_organization_limits` below makes that the
# organization's actually-enforced ceiling immediately, rather than
# `organizations`' own generous column defaults (sized for the M4-era Legacy
# Organization, not a not-yet-paying signup).


def create_customer_organization(
    conn: psycopg.Connection, *, owner_user_id: UUID, organization_name: str
) -> ProvisioningResult:
    """Create a new organization, its owner membership, and its billing row,
    atomically. Commits. Raises :class:`InvalidOrganizationNameError` for an
    empty name, :class:`SlugGenerationExhaustedError` on the (effectively
    unreachable) exhaustion of retry attempts.
    """
    name = organization_name.strip()
    if not name:
        raise InvalidOrganizationNameError("organization name must not be empty")
    base_slug = _slugify(name)

    organization_id: UUID | None = None
    final_slug = base_slug
    with conn.cursor() as cur:
        for attempt in range(_MAX_SLUG_ATTEMPTS):
            candidate_slug = base_slug if attempt == 0 else f"{base_slug}-{secrets.token_hex(3)}"
            cur.execute("SAVEPOINT provisioning_slug_attempt")
            try:
                cur.execute(_INSERT_ORG, {"name": name, "slug": candidate_slug})
                row = cur.fetchone()
            except psycopg.errors.UniqueViolation:
                cur.execute("ROLLBACK TO SAVEPOINT provisioning_slug_attempt")
                continue
            cur.execute("RELEASE SAVEPOINT provisioning_slug_attempt")
            assert row is not None
            organization_id = row[0]
            final_slug = candidate_slug
            break

        if organization_id is None:
            raise SlugGenerationExhaustedError(
                f"could not generate a unique slug for {name!r} after {_MAX_SLUG_ATTEMPTS} attempts"
            )

        cur.execute(
            _INSERT_OWNER_MEMBERSHIP, {"organization_id": organization_id, "user_id": owner_user_id}
        )

    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=owner_user_id,
        event_type="organization.created",
        payload={"name": name, "slug": final_slug},
    )
    # Last step, deliberately: `sync_organization_limits` (via
    # `arie.limits.set_limits`) is the one call in this function that
    # commits — everything above rides in on that same commit, so the
    # organization/membership/billing/audit rows land together or not at
    # all, matching this function's own atomicity claim.
    sync_organization_limits(conn, organization_id=organization_id)
    return ProvisioningResult(organization_id=organization_id, slug=final_slug)
