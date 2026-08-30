"""Organization invitations (Productization M4 Part 2) — the parts of
`arie.invitations` and its request schemas that don't need a live database:
role validation happens before any query is built, so a dummy `None`
connection proves it never gets touched.

API-level behavior (create/list/revoke/accept, role gating, RLS, tenant
isolation, expiry, replay, email mismatch) is covered by
tests/integration/test_invitations_integration.py instead.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

import psycopg
import pytest
from pydantic import ValidationError

from arie.api.schemas import AcceptInvitationRequest, CreateInvitationRequest
from arie.invitations import InvalidInvitationRoleError, create_invitation

_UNUSED_CONN = cast(psycopg.Connection, None)
_UNUSED_ORG_ID = cast(UUID, "not-used")
_UNUSED_USER_ID = cast(UUID, "not-used")


def test_create_invitation_rejects_an_unknown_role_before_touching_the_connection() -> None:
    with pytest.raises(InvalidInvitationRoleError, match="unknown role"):
        create_invitation(
            _UNUSED_CONN,
            organization_id=_UNUSED_ORG_ID,
            invited_by_user_id=_UNUSED_USER_ID,
            email="person@example.com",
            role="superadmin",
        )


# ------------------------------------------------------------- request schemas --


def test_create_invitation_request_rejects_an_unknown_role() -> None:
    with pytest.raises(ValidationError, match="unknown role"):
        CreateInvitationRequest(email="person@example.com", role="superadmin")


def test_create_invitation_request_accepts_every_real_role() -> None:
    for role in ("owner", "admin", "analyst_reviewer"):
        request = CreateInvitationRequest(email="person@example.com", role=role)
        assert request.role == role


def test_create_invitation_request_rejects_an_unnormalizable_email() -> None:
    with pytest.raises(ValidationError):
        CreateInvitationRequest(email="not-an-email-at-all", role="admin")


def test_accept_invitation_request_requires_a_non_empty_token() -> None:
    with pytest.raises(ValidationError):
        AcceptInvitationRequest(token="")


def test_accept_invitation_request_accepts_a_token() -> None:
    request = AcceptInvitationRequest(token="some-opaque-token")
    assert request.token == "some-opaque-token"
