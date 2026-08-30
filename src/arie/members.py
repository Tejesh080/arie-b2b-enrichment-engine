"""Organization membership management (Productization M4 Part 2). Owns the
read/update/remove side of `organization_members`
(`migrations/0012_organizations_and_members.sql`) — *creating* an active
membership row belongs to `arie.invitations.accept_invitation` instead:
in this milestone a membership only ever comes from accepting an invitation,
never a direct "add member" call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.audit import record_event
from arie.auth import ROLES

__all__ = [
    "CannotActOnSelfError",
    "InvalidMemberRoleError",
    "LastOwnerError",
    "MemberRecord",
    "list_members",
    "remove_member",
    "update_member_role",
]


class InvalidMemberRoleError(ValueError):
    """`new_role` is not one of `arie.auth.ROLES` — the same guard
    `organization_members`' own CHECK constraint enforces, checked here
    first so an unsupported role never reaches a query."""


class LastOwnerError(Exception):
    """Refused: this action would leave the organization with zero active
    owners. Applies to both demoting the sole remaining owner to another
    role and removing them outright — either way the organization would be
    left with no one able to perform owner-only actions, permanently
    (nothing in this milestone models an ownership-transfer workflow)."""


class CannotActOnSelfError(Exception):
    """A caller can never change their own role or remove themselves through
    these endpoints. Structural, not merely a checked permission — mirrors
    `AuthContext.is_org_admin`'s "an API key can never manage API keys"
    rule: self-service role escalation (or self-removal, which an owner
    could otherwise use to strand an organization) is prevented outright
    rather than by a role check that a self-granted role could pass."""


@dataclass(frozen=True)
class MemberRecord:
    organization_id: UUID
    user_id: UUID
    role: str
    status: str
    created_at: datetime
    updated_at: datetime


def _row_to_record(row: Mapping[str, Any]) -> MemberRecord:
    return MemberRecord(
        organization_id=row["organization_id"],
        user_id=row["user_id"],
        role=row["role"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_MEMBER_COLUMNS = "organization_id, user_id, role, status, created_at, updated_at"

_SELECT_ACTIVE_FOR_ORG = f"""
    SELECT {_MEMBER_COLUMNS} FROM organization_members
    WHERE organization_id = %(organization_id)s AND status = 'active'
    ORDER BY created_at
"""

_LOCK_ORGANIZATION = "SELECT pg_advisory_xact_lock(hashtext(%(organization_id)s::text))"
"""Serializes concurrent role-change/removal calls for the same
organization — mirrors `arie.icp_profiles`'s own lock and for the same
reason: "at least one active owner remains" is a check-then-act that two
concurrent requests could otherwise both pass before either commits, each
believing it left an owner behind."""

_COUNT_ACTIVE_OWNERS = """
    SELECT count(*) AS n FROM organization_members
    WHERE organization_id = %(organization_id)s AND role = 'owner' AND status = 'active'
"""

_SELECT_ONE_ACTIVE = f"""
    SELECT {_MEMBER_COLUMNS} FROM organization_members
    WHERE organization_id = %(organization_id)s AND user_id = %(user_id)s AND status = 'active'
"""

_UPDATE_ROLE = f"""
    UPDATE organization_members SET role = %(role)s, updated_at = now()
    WHERE organization_id = %(organization_id)s AND user_id = %(user_id)s AND status = 'active'
    RETURNING {_MEMBER_COLUMNS}
"""

_REMOVE = f"""
    UPDATE organization_members SET status = 'removed', updated_at = now()
    WHERE organization_id = %(organization_id)s AND user_id = %(user_id)s AND status = 'active'
    RETURNING {_MEMBER_COLUMNS}
"""


def list_members(conn: psycopg.Connection, *, organization_id: UUID) -> list[MemberRecord]:
    """Every currently-active member, oldest first (roughly join order —
    there is no separate "joined_at" distinct from `created_at` today)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_ACTIVE_FOR_ORG, {"organization_id": organization_id})
        rows = cur.fetchall()
    return [_row_to_record(row) for row in rows]


def update_member_role(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    target_user_id: UUID,
    new_role: str,
    actor_user_id: UUID,
) -> MemberRecord | None:
    """Change `target_user_id`'s role and commit. `None` if no *active*
    member with that id exists for this organization — the IDOR-safe shape
    every other ownership check in this codebase uses.

    Raises :class:`InvalidMemberRoleError`, :class:`CannotActOnSelfError`
    (`target_user_id == actor_user_id`), or :class:`LastOwnerError`
    (demoting the organization's sole remaining owner).
    """
    if new_role not in ROLES:
        raise InvalidMemberRoleError(f"unknown role: {new_role!r}")
    if target_user_id == actor_user_id:
        raise CannotActOnSelfError()

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_LOCK_ORGANIZATION, {"organization_id": organization_id})

        cur.execute(
            _SELECT_ONE_ACTIVE, {"organization_id": organization_id, "user_id": target_user_id}
        )
        current = cur.fetchone()
        if current is None:
            return None
        previous_role = current["role"]

        if previous_role == "owner" and new_role != "owner":
            cur.execute(_COUNT_ACTIVE_OWNERS, {"organization_id": organization_id})
            count_row = cur.fetchone()
            assert count_row is not None
            if count_row["n"] <= 1:
                raise LastOwnerError()

        cur.execute(
            _UPDATE_ROLE,
            {"organization_id": organization_id, "user_id": target_user_id, "role": new_role},
        )
        row = cur.fetchone()
    assert row is not None
    record = _row_to_record(row)

    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type="member.role_changed",
        payload={
            "user_id": str(target_user_id),
            "previous_role": previous_role,
            "new_role": new_role,
        },
    )
    conn.commit()
    return record


def remove_member(
    conn: psycopg.Connection, *, organization_id: UUID, target_user_id: UUID, actor_user_id: UUID
) -> MemberRecord | None:
    """Remove `target_user_id` (soft: `status = 'removed'`) and commit.
    `None` if no *active* member with that id exists for this organization.

    Raises :class:`CannotActOnSelfError` (`target_user_id == actor_user_id`)
    or :class:`LastOwnerError` (removing the organization's sole remaining
    owner) — this milestone has no ownership-transfer workflow, so the only
    safe move is refusing to strand the organization with none.
    """
    if target_user_id == actor_user_id:
        raise CannotActOnSelfError()

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_LOCK_ORGANIZATION, {"organization_id": organization_id})

        cur.execute(
            _SELECT_ONE_ACTIVE, {"organization_id": organization_id, "user_id": target_user_id}
        )
        current = cur.fetchone()
        if current is None:
            return None
        previous_role = current["role"]

        if previous_role == "owner":
            cur.execute(_COUNT_ACTIVE_OWNERS, {"organization_id": organization_id})
            count_row = cur.fetchone()
            assert count_row is not None
            if count_row["n"] <= 1:
                raise LastOwnerError()

        cur.execute(_REMOVE, {"organization_id": organization_id, "user_id": target_user_id})
        row = cur.fetchone()
    assert row is not None
    record = _row_to_record(row)

    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type="member.removed",
        payload={"user_id": str(target_user_id), "previous_role": previous_role},
    )
    conn.commit()
    return record
