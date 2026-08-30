"""Productization M4 Part 2 — organization membership management.

Built on `two_orgs` (`tests/integration/test_rls_membership_recursion.py`)
rather than `api_client`/`api_client_org_b`: those two fixtures authenticate
with an `AuthContext` override that never inserts a real `organization_members`
row, which is fine for endpoints that only ever *read* `auth.organization_id`
but not for these — `list_members`/`update_member_role`/`remove_member` query
`organization_members` directly, so the "owner" in every test here needs to
be a real row. `two_orgs` creates exactly that: one real owner member per
organization, ready to extend with additional members per test.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app
from tests.integration.test_rls_membership_recursion import TwoOrgFixture
from tests.integration.test_rls_membership_recursion import rls_test_roles as rls_test_roles
from tests.integration.test_rls_membership_recursion import two_orgs as two_orgs

from arie.api.main import AppState, create_app
from arie.auth import AuthContext

pytestmark = pytest.mark.integration


def _client_as(
    app_state: AppState, *, organization_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(organization_id=organization_id, auth_method="jwt", user_id=user_id, role=role),
    )
    return TestClient(app, raise_server_exceptions=False)


def _add_member(
    db_conn: psycopg.Connection, *, organization_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_members (organization_id, user_id, role, status) "
            "VALUES (%s, %s, %s, 'active')",
            (organization_id, user_id, role),
        )
    db_conn.commit()


def _audit_events(db_conn: psycopg.Connection, organization_id: uuid.UUID) -> list[tuple[str, Any]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM organization_audit_events "
            "WHERE organization_id = %s ORDER BY event_id",
            (organization_id,),
        )
        return cur.fetchall()


# -------------------------------------------------------------------------- list --


def test_owner_can_list_members(app_state: AppState, two_orgs: TwoOrgFixture) -> None:
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )
    response = client.get("/organization/members")
    assert response.status_code == 200
    user_ids = [m["user_id"] for m in response.json()]
    assert str(two_orgs.user_a) in user_ids


def test_listing_members_requires_owner_or_admin(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    reviewer_id = uuid.uuid4()
    _add_member(
        db_conn, organization_id=two_orgs.org_a, user_id=reviewer_id, role="analyst_reviewer"
    )
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=reviewer_id, role="analyst_reviewer"
    )
    response = client.get("/organization/members")
    assert response.status_code == 403


# ------------------------------------------------------------------ role change --


def test_owner_can_change_another_members_role(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    target_id = uuid.uuid4()
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=target_id, role="analyst_reviewer")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )

    response = client.patch(f"/organization/members/{target_id}", json={"role": "admin"})

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_cannot_change_your_own_role(app_state: AppState, two_orgs: TwoOrgFixture) -> None:
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )
    response = client.patch(
        f"/organization/members/{two_orgs.user_a}", json={"role": "analyst_reviewer"}
    )
    assert response.status_code == 403


def test_cannot_demote_the_last_owner(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    second_admin_id = uuid.uuid4()
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=second_admin_id, role="admin")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=second_admin_id, role="admin"
    )

    # two_orgs.user_a is org_a's only owner.
    response = client.patch(f"/organization/members/{two_orgs.user_a}", json={"role": "admin"})

    assert response.status_code == 409


def test_demoting_one_of_two_owners_succeeds(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    second_owner_id = uuid.uuid4()
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=second_owner_id, role="owner")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=second_owner_id, role="owner"
    )

    response = client.patch(f"/organization/members/{two_orgs.user_a}", json={"role": "admin"})

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_unknown_role_is_rejected(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    target_id = uuid.uuid4()
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=target_id, role="admin")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )

    response = client.patch(f"/organization/members/{target_id}", json={"role": "superadmin"})
    assert response.status_code == 422


def test_updating_an_unknown_member_is_404(app_state: AppState, two_orgs: TwoOrgFixture) -> None:
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )
    response = client.patch(f"/organization/members/{uuid.uuid4()}", json={"role": "admin"})
    assert response.status_code == 404


def test_cannot_change_a_foreign_organizations_members_role(
    app_state: AppState, two_orgs: TwoOrgFixture
) -> None:
    """org_a's owner attempting to reach org_b's owner by user id — must
    404, not succeed and not leak whether that user_id exists elsewhere."""
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )
    response = client.patch(f"/organization/members/{two_orgs.user_b}", json={"role": "admin"})
    assert response.status_code == 404


def test_role_change_is_audited(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    target_id = uuid.uuid4()
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=target_id, role="analyst_reviewer")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )
    client.patch(f"/organization/members/{target_id}", json={"role": "admin"})

    events = _audit_events(db_conn, two_orgs.org_a)
    assert any(e[0] == "member.role_changed" for e in events)


# ---------------------------------------------------------------------- removal --


def test_owner_can_remove_another_member(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    target_id = uuid.uuid4()
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=target_id, role="analyst_reviewer")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )

    response = client.delete(f"/organization/members/{target_id}")

    assert response.status_code == 200
    remaining = client.get("/organization/members").json()
    assert all(m["user_id"] != str(target_id) for m in remaining)


def test_cannot_remove_yourself(app_state: AppState, two_orgs: TwoOrgFixture) -> None:
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )
    response = client.delete(f"/organization/members/{two_orgs.user_a}")
    assert response.status_code == 403


def test_cannot_remove_the_last_owner(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    second_admin_id = uuid.uuid4()
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=second_admin_id, role="admin")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=second_admin_id, role="admin"
    )

    response = client.delete(f"/organization/members/{two_orgs.user_a}")

    assert response.status_code == 409


def test_removal_requires_owner_or_admin(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    reviewer_id = uuid.uuid4()
    target_id = uuid.uuid4()
    _add_member(
        db_conn, organization_id=two_orgs.org_a, user_id=reviewer_id, role="analyst_reviewer"
    )
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=target_id, role="analyst_reviewer")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=reviewer_id, role="analyst_reviewer"
    )

    response = client.delete(f"/organization/members/{target_id}")
    assert response.status_code == 403


def test_an_api_key_cannot_manage_membership(
    api_client: TestClient, app_state: AppState, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = api_client.post(
        "/api-keys", json={"label": "membership-probe", "scopes": ["leads:write"]}
    )
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw_client:
        listed = raw_client.get("/organization/members", headers=headers)
        removed = raw_client.delete(f"/organization/members/{uuid.uuid4()}", headers=headers)

    assert listed.status_code == 403
    assert removed.status_code == 403


def test_removal_is_audited(
    app_state: AppState, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    target_id = uuid.uuid4()
    _add_member(db_conn, organization_id=two_orgs.org_a, user_id=target_id, role="analyst_reviewer")
    client = _client_as(
        app_state, organization_id=two_orgs.org_a, user_id=two_orgs.user_a, role="owner"
    )
    client.delete(f"/organization/members/{target_id}")

    events = _audit_events(db_conn, two_orgs.org_a)
    assert any(e[0] == "member.removed" for e in events)
