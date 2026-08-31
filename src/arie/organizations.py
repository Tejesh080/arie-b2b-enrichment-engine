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

**`execution_mode` (Productization M5 Part 14)** is deliberately not in
:data:`UPDATABLE_FIELDS` and has its own dedicated
:func:`set_execution_mode`, not the generic `update_organization` whitelist
path. It gates real provider spend and real evidence acquisition — a
materially different class of consequence than a display name or timezone —
so it gets its own function, with its own audit event, the same way
`arie.provider_configs.set_provider_enabled` is a dedicated function rather
than a field on some generic "provider settings" updater.
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

from arie.audit import record_event

__all__ = [
    "EXECUTION_MODES",
    "LIVE_HUMAN_ONLY",
    "LIVE_SHADOW",
    "SIMULATED",
    "UPDATABLE_FIELDS",
    "InvalidExecutionModeError",
    "InvalidOrganizationSettingsError",
    "OrganizationRecord",
    "get_execution_mode",
    "get_organization",
    "set_execution_mode",
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
into a `SET` clause precisely because it can only ever come from here.
`execution_mode` is intentionally excluded — see :func:`set_execution_mode`."""

SIMULATED = "simulated"
LIVE_SHADOW = "live_shadow"
LIVE_HUMAN_ONLY = "live_human_only"

EXECUTION_MODES: tuple[str, ...] = (SIMULATED, LIVE_SHADOW, LIVE_HUMAN_ONLY)
"""Mirrors `migrations/0027_organization_execution_mode.sql`'s CHECK
constraint exactly — the database is the ultimate enforcement, this tuple is
what lets an invalid value be rejected before a query is even built, with a
message better than a raw constraint-violation error."""


class InvalidOrganizationSettingsError(ValueError):
    """`update_organization`'s payload failed validation, or named a field
    outside :data:`UPDATABLE_FIELDS`."""


class InvalidExecutionModeError(ValueError):
    """`set_execution_mode`'s `execution_mode` argument is outside
    :data:`EXECUTION_MODES`."""


@dataclass(frozen=True)
class OrganizationRecord:
    organization_id: UUID
    name: str
    slug: str
    status: str
    timezone: str
    company_domain: str | None
    execution_mode: str
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
        execution_mode=row["execution_mode"],
        onboarding_completed_at=row["onboarding_completed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_SELECT = """
    SELECT organization_id, name, slug, status, timezone, company_domain,
           execution_mode, onboarding_completed_at, created_at, updated_at
    FROM organizations
    WHERE organization_id = %(organization_id)s
"""

_SELECT_EXECUTION_MODE = """
    SELECT execution_mode FROM organizations WHERE organization_id = %(organization_id)s
"""

_UPDATE_EXECUTION_MODE = """
    UPDATE organizations
    SET execution_mode = %(execution_mode)s, updated_at = now()
    WHERE organization_id = %(organization_id)s
    RETURNING organization_id, name, slug, status, timezone, company_domain,
              execution_mode, onboarding_completed_at, created_at, updated_at
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
                  execution_mode, onboarding_completed_at, created_at, updated_at
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


def get_execution_mode(conn: psycopg.Connection, *, organization_id: UUID) -> str:
    """`organization_id`'s current execution mode, or :data:`SIMULATED` if the
    organization doesn't exist at all.

    Defensive rather than `None`-returning on a missing organization: this is
    read by `arie.jobs.handlers`' live acquisition path on every job, where
    the safe reading of "I cannot determine this organization's mode" is
    "treat it as simulated" (no real provider call), never "treat it as
    live" — the same fail-safe direction every other guard in this
    milestone takes. In practice a job's own `organization_id` always
    resolves to a real row (the lead was ingested under it), so this branch
    is defensive, not expected.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_EXECUTION_MODE, {"organization_id": organization_id})
        row = cur.fetchone()
    return row["execution_mode"] if row is not None else SIMULATED


def set_execution_mode(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    execution_mode: str,
    actor_user_id: UUID,
) -> OrganizationRecord | None:
    """Change `organization_id`'s execution mode and commit. Only the caller
    (`arie.api.main`'s route, via `_require_org_admin`) may call this for a
    non-owner/admin actor — this function itself enforces no role, the same
    division of responsibility `update_organization` uses.

    Raises :class:`InvalidExecutionModeError` for a value outside
    :data:`EXECUTION_MODES`. Returns `None` only if `organization_id`
    doesn't exist — the same IDOR-safe shape `update_organization` returns.
    """
    if execution_mode not in EXECUTION_MODES:
        raise InvalidExecutionModeError(
            f"unknown execution_mode {execution_mode!r} — must be one of {list(EXECUTION_MODES)}"
        )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _UPDATE_EXECUTION_MODE,
            {"organization_id": organization_id, "execution_mode": execution_mode},
        )
        row = cur.fetchone()
    if row is None:
        return None
    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type="organization.execution_mode_changed",
        payload={"execution_mode": execution_mode},
    )
    conn.commit()
    return _row_to_record(row)
