"""Organization onboarding checklist (Productization M4 Part 8). No new
table, no workflow engine — every step below is derived, at read time, from
tables that are already the source of truth (`organization_icp_profiles`,
`organization_provider_configs`, `leads`), matching
`migrations/0023_organization_settings.sql`'s own reasoning for why
`organizations.onboarding_completed_at` is the *only* onboarding-related
column that exists: storing a second, independently-updated copy of any of
these facts would just be a second place for it to go stale.

`onboarding_completed_at` itself is the one piece of real state this module
touches — `get_onboarding_status` opportunistically stamps it, once, the
first time every step is found true, so a caller that only wants "is this
organization done with setup" has an O(1) fact to read later instead of
re-deriving the whole checklist. It is never unset once stamped, even if
(say) every provider is later removed — onboarding is a one-time milestone,
not a live health check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

__all__ = ["OnboardingStatus", "get_onboarding_status"]


@dataclass(frozen=True)
class OnboardingStatus:
    account_created: bool
    """Always `true` for any organization this function can be called
    about — reaching this code at all means an authenticated member of a
    real organization made the request. Included anyway (rather than
    omitted as "obviously true") so the frontend's checklist can render a
    uniform list of steps without special-casing the first one."""

    organization_configured: bool
    """`true` once the organization has a non-empty `name` — true from the
    moment the organization row was created (`name` is `NOT NULL`), so in
    practice identical to `account_created` today. Kept as its own field
    because Part 1's brief describes organization settings as a distinct
    setup step, and a future settings field required for "configured" to
    mean something more can be added to this one check without changing
    the checklist's shape."""

    icp_configured: bool
    """`true` iff the organization has ever created an ICP profile version
    (`organization_icp_profiles` has a row) — including the bootstrap
    "Reference ICP" every pre-M3 organization received, so an existing
    organization reads as already past this step."""

    provider_configured: bool
    """`true` iff the organization has configured at least one BYOK
    provider credential (`organization_provider_configs` has a row),
    regardless of `enabled` — this step is "did you set one up," not "is
    one currently active." Explicitly optional (see `completed`)."""

    first_upload_completed: bool
    """`true` iff the organization has ever uploaded a CSV batch
    (`lead_batches` has a row)."""

    first_batch_processed: bool
    """`true` iff at least one of the organization's leads has left its
    initial `NEW` status — the worker has processed at least one job for
    this organization to completion (or partial progress; any movement off
    `NEW` proves the pipeline ran, not just that a lead was ingested)."""

    completed: bool
    """`true` iff every step above except `provider_configured` is done —
    provider configuration is explicitly optional (an organization can run
    on simulated/demo mode indefinitely without BYOK credentials; see the
    module docstring and the M4 brief's own "must still be able to use
    simulated mode... if appropriate")."""

    completed_at: datetime | None
    """`organizations.onboarding_completed_at` — when `completed` first
    became `true`, or `None` if it hasn't yet. Stamped by this function
    itself; see the module docstring."""


_SELECT_ORG = "SELECT name, onboarding_completed_at FROM organizations WHERE organization_id = %(organization_id)s"
_HAS_ICP_PROFILE = (
    "SELECT 1 FROM organization_icp_profiles WHERE organization_id = %(organization_id)s LIMIT 1"
)
_HAS_PROVIDER_CONFIG = "SELECT 1 FROM organization_provider_configs WHERE organization_id = %(organization_id)s LIMIT 1"
_HAS_BATCH = "SELECT 1 FROM lead_batches WHERE organization_id = %(organization_id)s LIMIT 1"
_HAS_PROCESSED_LEAD = (
    "SELECT 1 FROM leads WHERE organization_id = %(organization_id)s AND status != 'NEW' LIMIT 1"
)
_STAMP_COMPLETED = """
    UPDATE organizations SET onboarding_completed_at = now()
    WHERE organization_id = %(organization_id)s AND onboarding_completed_at IS NULL
    RETURNING onboarding_completed_at
"""


def get_onboarding_status(conn: psycopg.Connection, *, organization_id: UUID) -> OnboardingStatus:
    """Derive the checklist and commit only if this call is the one that
    first completes it (the `onboarding_completed_at` stamp) — every other
    call is a plain read, no write, no commit.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_ORG, {"organization_id": organization_id})
        org_row = cur.fetchone()
        assert org_row is not None  # an authenticated caller's own organization always exists
        name, completed_at = org_row

        cur.execute(_HAS_ICP_PROFILE, {"organization_id": organization_id})
        icp_configured = cur.fetchone() is not None

        cur.execute(_HAS_PROVIDER_CONFIG, {"organization_id": organization_id})
        provider_configured = cur.fetchone() is not None

        cur.execute(_HAS_BATCH, {"organization_id": organization_id})
        first_upload_completed = cur.fetchone() is not None

        cur.execute(_HAS_PROCESSED_LEAD, {"organization_id": organization_id})
        first_batch_processed = cur.fetchone() is not None

    organization_configured = bool(name)
    completed = icp_configured and first_upload_completed and first_batch_processed

    if completed and completed_at is None:
        with conn.cursor() as cur:
            cur.execute(_STAMP_COMPLETED, {"organization_id": organization_id})
            row = cur.fetchone()
        if row is not None:
            completed_at = row[0]
        conn.commit()

    return OnboardingStatus(
        account_created=True,
        organization_configured=organization_configured,
        icp_configured=icp_configured,
        provider_configured=provider_configured,
        first_upload_completed=first_upload_completed,
        first_batch_processed=first_batch_processed,
        completed=completed,
        completed_at=completed_at,
    )
