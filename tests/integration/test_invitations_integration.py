"""Productization M4 Part 2 — organization invitations.

`api_client` authenticates as an owner of `LEGACY_ORGANIZATION_ID`;
`api_client_org_b`/`other_org` give every test that creates/accepts an
invitation its own disposable organization, so accepted memberships and
audit rows from one test never leak into another.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app
from tests.integration.test_rls_membership_recursion import TwoOrgFixture, _as
from tests.integration.test_rls_membership_recursion import rls_test_roles as rls_test_roles
from tests.integration.test_rls_membership_recursion import two_orgs as two_orgs

from arie.api.main import AppState, create_app, get_verified_identity
from arie.auth import AuthContext, VerifiedIdentity
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration


def _create_invitation(
    client: TestClient, *, email: str, role: str = "analyst_reviewer"
) -> dict[str, Any]:
    response = client.post("/organization/invitations", json={"email": email, "role": role})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _accepting_client(app_state: AppState, *, user_id: uuid.UUID, email: str) -> TestClient:
    """A fresh app whose `get_verified_identity` dependency is overridden to
    a fixed identity — the same "override the auth dependency instead of
    signing a real Supabase JWT" approach `api_client`'s own `get_auth_context`
    override already uses, applied to the accept-only identity dependency."""
    app = create_app(state=app_state)
    app.dependency_overrides[get_verified_identity] = lambda: VerifiedIdentity(
        user_id=user_id, email=email
    )
    return TestClient(app, raise_server_exceptions=False)


def _audit_events(db_conn: psycopg.Connection, organization_id: uuid.UUID) -> list[tuple[str, Any]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM organization_audit_events "
            "WHERE organization_id = %s ORDER BY event_id",
            (organization_id,),
        )
        return cur.fetchall()


# ------------------------------------------------------------------------ create --


def test_owner_can_create_an_invitation(api_client_org_b: TestClient) -> None:
    body = _create_invitation(api_client_org_b, email="new.hire@example.com", role="admin")
    assert body["email_normalized"] == "new.hire@example.com"
    assert body["role"] == "admin"
    assert body["status"] == "pending"
    assert body["raw_token"]  # shown exactly once
    assert "token_hash" not in body


def test_member_invite_is_denied(app_state: AppState) -> None:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=uuid.uuid4(),
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="analyst_reviewer",
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/organization/invitations",
            json={"email": "x@example.com", "role": "admin"},
        )
    assert response.status_code == 403


def test_an_api_key_cannot_invite(
    api_client: TestClient, app_state: AppState, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = api_client.post(
        "/api-keys", json={"label": "invite-probe", "scopes": ["leads:write"]}
    )
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw_client:
        response = raw_client.post(
            "/organization/invitations",
            json={"email": "x@example.com", "role": "admin"},
            headers=headers,
        )
    assert response.status_code == 403


def test_duplicate_pending_invitation_is_blocked(api_client_org_b: TestClient) -> None:
    _create_invitation(api_client_org_b, email="dup@example.com")

    response = api_client_org_b.post(
        "/organization/invitations", json={"email": "dup@example.com", "role": "admin"}
    )
    assert response.status_code == 409


def test_email_is_normalized_for_duplicate_detection(api_client_org_b: TestClient) -> None:
    _create_invitation(api_client_org_b, email="Person+tag@Example.com")

    response = api_client_org_b.post(
        "/organization/invitations", json={"email": "person@example.com", "role": "admin"}
    )
    assert response.status_code == 409


def test_unknown_role_is_rejected(api_client_org_b: TestClient) -> None:
    response = api_client_org_b.post(
        "/organization/invitations", json={"email": "x@example.com", "role": "superadmin"}
    )
    assert response.status_code == 422


# -------------------------------------------------------------------------- list --


def test_listing_invitations_requires_owner_or_admin(app_state: AppState) -> None:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=LEGACY_ORGANIZATION_ID,
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="analyst_reviewer",
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/organization/invitations")
    assert response.status_code == 403


def test_a_different_organizations_invitations_are_invisible(
    api_client: TestClient, api_client_org_b: TestClient
) -> None:
    _create_invitation(api_client_org_b, email="org-b-only@example.com")

    org_a_invitations = api_client.get("/organization/invitations").json()
    assert all(inv["email_normalized"] != "org-b-only@example.com" for inv in org_a_invitations)


# ------------------------------------------------------------------------ revoke --


def test_owner_can_revoke_a_pending_invitation(api_client_org_b: TestClient) -> None:
    created = _create_invitation(api_client_org_b, email="revoke-me@example.com")

    response = api_client_org_b.delete(f"/organization/invitations/{created['invitation_id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


def test_revoking_an_unknown_invitation_is_404(api_client_org_b: TestClient) -> None:
    response = api_client_org_b.delete(f"/organization/invitations/{uuid.uuid4()}")
    assert response.status_code == 404


def test_cannot_revoke_another_organizations_invitation(
    api_client: TestClient, api_client_org_b: TestClient
) -> None:
    created = _create_invitation(api_client_org_b, email="cross-org-revoke@example.com")

    response = api_client.delete(f"/organization/invitations/{created['invitation_id']}")

    assert response.status_code == 404
    # Untouched — org B can still see it as pending.
    still_pending = api_client_org_b.get("/organization/invitations").json()
    match = next(i for i in still_pending if i["invitation_id"] == created["invitation_id"])
    assert match["status"] == "pending"


# ------------------------------------------------------------------------ accept --


def test_accepting_creates_an_active_membership(
    api_client_org_b: TestClient, other_org: uuid.UUID, app_state: AppState
) -> None:
    created = _create_invitation(api_client_org_b, email="accept.me@example.com", role="admin")
    new_user_id = uuid.uuid4()

    accepting = _accepting_client(app_state, user_id=new_user_id, email="accept.me@example.com")
    response = accepting.post("/invitations/accept", json={"token": created["raw_token"]})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"

    # The new member can now authenticate against org B for real.
    member_app = create_app(state=app_state)
    authorize_app(
        member_app,
        AuthContext(
            organization_id=other_org, auth_method="jwt", user_id=new_user_id, role="admin"
        ),
    )
    with TestClient(member_app, raise_server_exceptions=False) as member_client:
        settings = member_client.get("/organization")
    assert settings.status_code == 200


def test_accepting_with_the_wrong_email_is_denied(
    api_client_org_b: TestClient, app_state: AppState
) -> None:
    created = _create_invitation(api_client_org_b, email="intended@example.com")

    accepting = _accepting_client(app_state, user_id=uuid.uuid4(), email="someone.else@example.com")
    response = accepting.post("/invitations/accept", json={"token": created["raw_token"]})

    assert response.status_code == 403


def test_replaying_an_accepted_token_is_denied(
    api_client_org_b: TestClient, app_state: AppState
) -> None:
    created = _create_invitation(api_client_org_b, email="once.only@example.com")
    accepting = _accepting_client(app_state, user_id=uuid.uuid4(), email="once.only@example.com")

    first = accepting.post("/invitations/accept", json={"token": created["raw_token"]})
    assert first.status_code == 200

    second = accepting.post("/invitations/accept", json={"token": created["raw_token"]})
    assert second.status_code == 404


def test_an_unknown_token_is_404(app_state: AppState) -> None:
    accepting = _accepting_client(app_state, user_id=uuid.uuid4(), email="nobody@example.com")
    response = accepting.post("/invitations/accept", json={"token": "totally-made-up-token"})
    assert response.status_code == 404


def test_an_expired_invitation_is_denied(
    api_client_org_b: TestClient, app_state: AppState, db_conn: psycopg.Connection
) -> None:
    created = _create_invitation(api_client_org_b, email="too.late@example.com")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_invitations SET expires_at = %s WHERE invitation_id = %s",
            (datetime.now(UTC) - timedelta(seconds=1), created["invitation_id"]),
        )
    db_conn.commit()

    accepting = _accepting_client(app_state, user_id=uuid.uuid4(), email="too.late@example.com")
    response = accepting.post("/invitations/accept", json={"token": created["raw_token"]})

    assert response.status_code == 410


def test_an_api_key_cannot_accept_an_invitation(
    api_client: TestClient,
    api_client_org_b: TestClient,
    app_state: AppState,
    cleanup_api_keys: list[uuid.UUID],
) -> None:
    created = _create_invitation(api_client_org_b, email="via-api-key@example.com")
    key = api_client.post("/api-keys", json={"label": "accept-probe", "scopes": []})
    assert key.status_code == 201
    cleanup_api_keys.append(uuid.UUID(key.json()["key_id"]))
    headers = {"Authorization": f"Bearer {key.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw_client:
        response = raw_client.post(
            "/invitations/accept", json={"token": created["raw_token"]}, headers=headers
        )
    assert response.status_code == 403


# -------------------------------------------------------------------------- audit --


def test_invite_and_revoke_are_audited(
    api_client_org_b: TestClient, other_org: uuid.UUID, db_conn: psycopg.Connection
) -> None:
    created = _create_invitation(api_client_org_b, email="audited@example.com")
    api_client_org_b.delete(f"/organization/invitations/{created['invitation_id']}")

    events = _audit_events(db_conn, other_org)
    event_types = [e[0] for e in events]
    assert "member.invited" in event_types
    assert "invitation.revoked" in event_types
    invited_payload = next(e[1] for e in events if e[0] == "member.invited")
    assert invited_payload["email"] == "audited@example.com"
    # Never the raw token.
    assert "raw_token" not in invited_payload
    assert "token" not in invited_payload


def test_accept_is_audited(
    api_client_org_b: TestClient,
    other_org: uuid.UUID,
    app_state: AppState,
    db_conn: psycopg.Connection,
) -> None:
    created = _create_invitation(api_client_org_b, email="accept.audit@example.com")
    accepting = _accepting_client(app_state, user_id=uuid.uuid4(), email="accept.audit@example.com")
    accepting.post("/invitations/accept", json={"token": created["raw_token"]})

    events = _audit_events(db_conn, other_org)
    assert any(e[0] == "invitation.accepted" for e in events)


# ---------------------------------------------------------------------------- RLS --


def test_rls_admin_can_select_own_orgs_invitations(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_invitations "
            "(organization_id, email_normalized, role, token_hash, invited_by_user_id, expires_at) "
            "VALUES (%s, 'rls@example.com', 'admin', 'x', %s, now() + interval '7 days') "
            "RETURNING invitation_id",
            (two_orgs.org_a, two_orgs.user_a),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    invitation_id = row[0]

    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)  # owner of org_a per the fixture
        cur.execute(
            "SELECT invitation_id FROM organization_invitations WHERE organization_id = %s",
            (two_orgs.org_a,),
        )
        rows = cur.fetchall()
    assert rows == [(invitation_id,)]


def test_rls_foreign_orgs_invitations_are_invisible(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_invitations "
            "(organization_id, email_normalized, role, token_hash, invited_by_user_id, expires_at) "
            "VALUES (%s, 'rls-b@example.com', 'admin', 'y', %s, now() + interval '7 days')",
            (two_orgs.org_b, two_orgs.user_b),
        )
    db_conn.commit()

    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "SELECT invitation_id FROM organization_invitations WHERE organization_id = %s",
            (two_orgs.org_b,),
        )
        rows = cur.fetchall()
    assert rows == []


def test_rls_non_admin_member_cannot_select_invitations(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_invitations "
            "(organization_id, email_normalized, role, token_hash, invited_by_user_id, expires_at) "
            "VALUES (%s, 'rls-nonadmin@example.com', 'admin', 'z', %s, now() + interval '7 days')",
            (two_orgs.org_a, two_orgs.user_a),
        )
        cur.execute(
            "UPDATE organization_members SET role = 'analyst_reviewer' "
            "WHERE organization_id = %s AND user_id = %s",
            (two_orgs.org_a, two_orgs.user_a),
        )
    db_conn.commit()

    try:
        with (
            psycopg.connect(migrated_database_direct) as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            _as(cur, two_orgs.user_a)
            cur.execute(
                "SELECT invitation_id FROM organization_invitations WHERE organization_id = %s",
                (two_orgs.org_a,),
            )
            rows = cur.fetchall()
        assert rows == []
    finally:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE organization_members SET role = 'owner' "
                "WHERE organization_id = %s AND user_id = %s",
                (two_orgs.org_a, two_orgs.user_a),
            )
        db_conn.commit()


def test_rls_anon_sees_no_invitations(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, None)
        cur.execute("SELECT invitation_id FROM organization_invitations")
        rows = cur.fetchall()
    assert rows == []


def test_rls_audit_events_admin_only(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_audit_events (organization_id, actor_user_id, event_type) "
            "VALUES (%s, %s, 'test.event')",
            (two_orgs.org_a, two_orgs.user_a),
        )
    db_conn.commit()

    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "SELECT event_type FROM organization_audit_events WHERE organization_id = %s",
            (two_orgs.org_a,),
        )
        own = cur.fetchall()
        cur.execute(
            "SELECT event_type FROM organization_audit_events WHERE organization_id = %s",
            (two_orgs.org_b,),
        )
        foreign = cur.fetchall()
    assert own == [("test.event",)]
    assert foreign == []
