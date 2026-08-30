"""Organization membership management (Productization M4 Part 2) — the parts
of `arie.members` and `UpdateMemberRoleRequest` that don't need a live
database: self-action and role validation both happen before any query is
built.

API-level behavior (role change, removal, last-owner protection, RLS, tenant
isolation) is covered by tests/integration/test_members_integration.py
instead.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from arie.api.schemas import UpdateMemberRoleRequest
from arie.members import CannotActOnSelfError, InvalidMemberRoleError, update_member_role

_UNUSED_CONN = cast(psycopg.Connection, None)
_UNUSED_ORG_ID = cast(UUID, "not-used")


def test_update_member_role_rejects_an_unknown_role_before_touching_the_connection() -> None:
    same_user = uuid4()
    with pytest.raises(InvalidMemberRoleError, match="unknown role"):
        update_member_role(
            _UNUSED_CONN,
            organization_id=_UNUSED_ORG_ID,
            target_user_id=uuid4(),
            new_role="superadmin",
            actor_user_id=same_user,
        )


def test_update_member_role_rejects_changing_your_own_role_before_touching_the_connection() -> None:
    same_user = uuid4()
    with pytest.raises(CannotActOnSelfError):
        update_member_role(
            _UNUSED_CONN,
            organization_id=_UNUSED_ORG_ID,
            target_user_id=same_user,
            new_role="admin",
            actor_user_id=same_user,
        )


# ------------------------------------------------------------- request schema --


def test_update_member_role_request_rejects_an_unknown_role() -> None:
    with pytest.raises(ValidationError, match="unknown role"):
        UpdateMemberRoleRequest(role="superadmin")


def test_update_member_role_request_accepts_every_real_role() -> None:
    for role in ("owner", "admin", "analyst_reviewer"):
        assert UpdateMemberRoleRequest(role=role).role == role
