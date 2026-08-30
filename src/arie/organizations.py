"""Organization settings (Productization M4 Part 1): the editable, non-tenancy
fields on `organizations` itself — display name, timezone, an optional
company domain, and the coarse onboarding-complete marker
(`migrations/0023_organization_settings.sql`). `slug`/`status`/`created_at`
already existed before this milestone and stay read-only here: `slug`
because nothing in this milestone's brief asks for it to be editable and a
changing slug would be a footgun for anything that already links to it,
`status`/`created_at` because they are not settings a member edits.

Deliberately does not own onboarding *step* tracking — see
`migrations/0023_organization_settings.sql`'s own docstring for why each
step is derived from other tables (`organization_icp_profiles`,
`lead_batches`, ...) rather than stored again here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import available_timezones

import psycopg
from psycopg.rows import dict_row

__all__ = [
    "UPDATABLE_FIELDS",
    "InvalidOrganizationSettingsError",
    "OrganizationRecord",
    "get_organization",
    "update_organization",
    "validate_timezone",
]

_VALID_TIMEZONES = available_timezones()
"""Computed once from the interpreter's own tzdata — every IANA zone name it
knows about. Validating against this (rather than a hardcoded list this repo
would have to maintain) is what lets a new zone name become acceptable
without a code change; see the migration's own docstring for why `timezone`
itself is a bare `TEXT` column with no DB-level `CHECK` mirroring this set."""

UPDATABLE_FIELDS: tuple[str, ...] = ("name", "timezone", "company_domain")
"""The only columns `update_organization` will ever write — a whitelist, not
merely documentation: an update dict containing any other key raises before a
query is built, and every key in this tuple is safe to interpolate directly
into a `SET` clause precisely because it can only ever come from here."""


class InvalidOrganizationSettingsError(ValueError):
    """`update_organization`'s payload failed validation, or named a field
    outside :data:`UPDATABLE_FIELDS`."""


@dataclass(frozen=True)
class OrganizationRecord:
    organization_id: UUID
    name: str
    slug: str
    status: str
    timezone: str
    company_domain: str | None
    onboarding_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _row_to_record(row: Mapping[str, Any]) -> OrganizationRecord:
    return OrganizationRecord(
        organization_id=row["organization_id"],
        name=row["name"],
        slug=row["slug"],
        status=row["status"],
        timezone=row["timezone"],
        company_domain=row["company_domain"],
        onboarding_completed_at=row["onboarding_completed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_SELECT = """
    SELECT organization_id, name, slug, status, timezone, company_domain,
           onboarding_completed_at, created_at, updated_at
    FROM organizations
    WHERE organization_id = %(organization_id)s
"""


def validate_timezone(timezone: str) -> None:
    if timezone not in _VALID_TIMEZONES:
        raise InvalidOrganizationSettingsError(f"unknown timezone: {timezone!r}")


def get_organization(
    conn: psycopg.Connection, *, organization_id: UUID
) -> OrganizationRecord | None:
    """`None` only if `organization_id` doesn't exist at all — every real
    organization has a settings row by construction, this table *is* that
    row, there is nothing separate to be missing."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT, {"organization_id": organization_id})
        row = cur.fetchone()
    return _row_to_record(row) if row is not None else None


def update_organization(
    conn: psycopg.Connection, *, organization_id: UUID, updates: Mapping[str, Any]
) -> OrganizationRecord | None:
    """Apply a partial update and commit. `updates` is exactly the set of
    fields the caller wants changed — build it with the request model's own
    `exclude_unset` (see `arie.api.schemas.UpdateOrganizationRequest`) so a
    field the client never mentioned is left untouched, while one sent as
    explicit `null` (`company_domain` only — the sole nullable field here)
    genuinely clears it. An empty `updates` is a no-op that still returns the
    current row, matching a client that PATCHed with nothing to say.

    Raises :class:`InvalidOrganizationSettingsError` for an unknown field, an
    empty `name`, or an unrecognised `timezone`. Returns `None` only if
    `organization_id` doesn't exist — the IDOR-safe shape every other
    ownership check in this codebase uses (indistinguishable from "wrong
    organization").
    """
    unknown = set(updates) - set(UPDATABLE_FIELDS)
    if unknown:
        raise InvalidOrganizationSettingsError(
            f"cannot update field(s): {', '.join(sorted(unknown))}"
        )

    if not updates:
        return get_organization(conn, organization_id=organization_id)

    if "name" in updates:
        name = updates["name"]
        if not isinstance(name, str) or not name.strip():
            raise InvalidOrganizationSettingsError("name must be a non-empty string")
    if "timezone" in updates:
        validate_timezone(updates["timezone"])

    set_clause = ", ".join(f"{field} = %({field})s" for field in updates)
    query = f"""
        UPDATE organizations
        SET {set_clause}, updated_at = now()
        WHERE organization_id = %(organization_id)s
        RETURNING organization_id, name, slug, status, timezone, company_domain,
                  onboarding_completed_at, created_at, updated_at
    """  # `field` names above come only from UPDATABLE_FIELDS, checked above

    params: dict[str, Any] = dict(updates)
    params["organization_id"] = organization_id
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    if row is None:
        return None
    conn.commit()
    return _row_to_record(row)
