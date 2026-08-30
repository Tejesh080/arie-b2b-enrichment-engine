"""Productization M4 Part 1 — organization settings.

`api_client` authenticates as an owner of `LEGACY_ORGANIZATION_ID` — every
read test exercises that real row, never a fixture invented for this suite.
Tests that *write* use `other_org`/`api_client_org_b` where a mutation would
otherwise leak into every other integration test file that reads
`LEGACY_ORGANIZATION_ID`'s organization row.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app
from tests.integration.test_rls_membership_recursion import TwoOrgFixture, _as
from tests.integration.test_rls_membership_recursion import rls_test_roles as rls_test_roles
from tests.integration.test_rls_membership_recursion import two_orgs as two_orgs

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------------ reads --


def test_get_organization_returns_the_authenticated_organization(
    api_client: TestClient,
) -> None:
    response = api_client.get("/organization")
    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == str(LEGACY_ORGANIZATION_ID)
    assert body["timezone"] == "UTC"  # migration 0023's own column default
    assert "name" in body and "slug" in body


def test_reading_settings_requires_no_particular_role(app_state: AppState) -> None:
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
        response = client.get("/organization")
    assert response.status_code == 200


# ------------------------------------------------------------------------ writes --


def test_owner_can_update_settings(api_client_org_b: TestClient, other_org: uuid.UUID) -> None:
    response = api_client_org_b.patch(
        "/organization",
        json={"name": "Org B Renamed", "timezone": "Australia/Adelaide"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Org B Renamed"
    assert body["timezone"] == "Australia/Adelaide"

    reread = api_client_org_b.get("/organization").json()
    assert reread["name"] == "Org B Renamed"
    assert reread["timezone"] == "Australia/Adelaide"


def test_partial_update_leaves_other_fields_untouched(
    api_client_org_b: TestClient,
) -> None:
    before = api_client_org_b.get("/organization").json()

    response = api_client_org_b.patch("/organization", json={"timezone": "America/New_York"})

    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "America/New_York"
    assert body["name"] == before["name"]
    assert body["slug"] == before["slug"]


def test_company_domain_can_be_set_then_explicitly_cleared(
    api_client_org_b: TestClient,
) -> None:
    set_response = api_client_org_b.patch(
        "/organization", json={"company_domain": "https://Acme.example.com/"}
    )
    assert set_response.status_code == 200
    assert set_response.json()["company_domain"] == "https://Acme.example.com/"

    clear_response = api_client_org_b.patch("/organization", json={"company_domain": None})
    assert clear_response.status_code == 200
    assert clear_response.json()["company_domain"] is None


def test_empty_patch_body_is_rejected(api_client_org_b: TestClient) -> None:
    response = api_client_org_b.patch("/organization", json={})
    assert response.status_code == 422


def test_unknown_timezone_is_rejected(api_client_org_b: TestClient) -> None:
    response = api_client_org_b.patch("/organization", json={"timezone": "Mars/Olympus_Mons"})
    assert response.status_code == 422


def test_blank_name_is_rejected(api_client_org_b: TestClient) -> None:
    response = api_client_org_b.patch("/organization", json={"name": "   "})
    assert response.status_code == 422


def test_updating_settings_requires_owner_or_admin(app_state: AppState) -> None:
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
        response = client.patch("/organization", json={"name": "should fail"})
    assert response.status_code == 403


def test_an_api_key_cannot_read_or_write_organization_settings(
    api_client: TestClient, app_state: AppState, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = api_client.post(
        "/api-keys", json={"label": "org-settings-probe", "scopes": ["leads:write"]}
    )
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw_client:
        read = raw_client.get("/organization", headers=headers)
        write = raw_client.patch("/organization", json={"name": "x"}, headers=headers)

    assert read.status_code == 403
    assert write.status_code == 403


# --------------------------------------------------------------- tenant isolation --


def test_a_different_organizations_settings_are_invisible(
    api_client: TestClient, api_client_org_b: TestClient
) -> None:
    api_client_org_b.patch("/organization", json={"name": "Org B, private name"})

    org_a = api_client.get("/organization").json()
    assert org_a["organization_id"] == str(LEGACY_ORGANIZATION_ID)
    assert org_a["name"] != "Org B, private name"


def test_updating_settings_cannot_reach_another_organization(
    api_client_org_b: TestClient,
) -> None:
    """`organization_id` is never taken from the request — there is no field
    on `UpdateOrganizationRequest` for it at all — so the only organization
    a PATCH can ever affect is the caller's own, regardless of intent."""
    before = api_client_org_b.get("/organization").json()

    response = api_client_org_b.patch("/organization", json={"name": "still just org b"})

    assert response.status_code == 200
    assert response.json()["organization_id"] == before["organization_id"]


# ------------------------------------------------------------------------------ RLS --


def _row_updates_from_service_role(db_conn: psycopg.Connection, organization_id: uuid.UUID) -> Any:
    with db_conn.cursor() as cur:
        cur.execute("SELECT name FROM organizations WHERE organization_id = %s", (organization_id,))
        row = cur.fetchone()
    return row


def test_rls_owner_can_update_own_organization(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "UPDATE organizations SET name = 'RLS updated' WHERE organization_id = %s "
            "RETURNING name",
            (two_orgs.org_a,),
        )
        row = cur.fetchone()
    assert row == ("RLS updated",)


def test_rls_non_admin_member_cannot_update_organization(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
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
                "UPDATE organizations SET name = 'should not stick' WHERE organization_id = %s",
                (two_orgs.org_a,),
            )
            # RLS silently matches zero rows here rather than raising — the
            # UPDATE policy's USING clause filters rows out of the update's
            # own view, it doesn't reject the statement the way the ICP
            # profile's INSERT-only policy does.
            assert cur.rowcount == 0
    finally:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE organization_members SET role = 'owner' "
                "WHERE organization_id = %s AND user_id = %s",
                (two_orgs.org_a, two_orgs.user_a),
            )
        db_conn.commit()


def test_rls_cannot_update_a_foreign_organization(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)  # only a member of org_a
        cur.execute(
            "UPDATE organizations SET name = 'cross-org write' WHERE organization_id = %s",
            (two_orgs.org_b,),
        )
        assert cur.rowcount == 0

    unaffected = _row_updates_from_service_role(db_conn, two_orgs.org_b)
    assert unaffected is not None
    assert unaffected[0] != "cross-org write"


def test_rls_anon_cannot_update_any_organization(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, None)
        cur.execute(
            "UPDATE organizations SET name = 'anon write' WHERE organization_id = %s",
            (two_orgs.org_a,),
        )
        assert cur.rowcount == 0
