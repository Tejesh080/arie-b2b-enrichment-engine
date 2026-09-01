"""Productization M6 Parts 10-12/36/39 — self-service organization
provisioning (`POST /organizations`) against a real database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app

from arie.api.main import AppState, create_app, get_verified_identity
from arie.auth import AuthContext, VerifiedIdentity
from arie.billing.repository import get_billing
from arie.config import TurnstileConfig
from arie.provisioning import InvalidOrganizationNameError, create_customer_organization

pytestmark = pytest.mark.integration


def _signed_up_client(app_state: AppState, *, user_id: uuid.UUID, email: str) -> TestClient:
    app = create_app(state=app_state)
    app.dependency_overrides[get_verified_identity] = lambda: VerifiedIdentity(
        user_id=user_id, email=email
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def cleanup_provisioned_orgs(db_conn: psycopg.Connection) -> Iterator[list[uuid.UUID]]:
    org_ids: list[uuid.UUID] = []
    yield org_ids
    if org_ids:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM organizations WHERE organization_id = ANY(%s)", (org_ids,))
        db_conn.commit()


# --------------------------------------------------------------------- domain --


def test_create_customer_organization_is_atomic(
    db_conn: psycopg.Connection, cleanup_provisioned_orgs: list[uuid.UUID]
) -> None:
    owner_id = uuid.uuid4()
    result = create_customer_organization(
        db_conn, owner_user_id=owner_id, organization_name="Acme Corp"
    )
    cleanup_provisioned_orgs.append(result.organization_id)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT role, status FROM organization_members "
            "WHERE organization_id = %s AND user_id = %s",
            (result.organization_id, owner_id),
        )
        membership = cur.fetchone()
        cur.execute(
            "SELECT execution_mode FROM organizations WHERE organization_id = %s",
            (result.organization_id,),
        )
        org_row = cur.fetchone()
        cur.execute(
            "SELECT 1 FROM organization_provider_configs WHERE organization_id = %s",
            (result.organization_id,),
        )
        provider_row = cur.fetchone()

    assert membership == ("owner", "active")
    assert org_row is not None and org_row[0] == "simulated"
    assert provider_row is None  # no BYOK provider configured for a brand-new org

    billing = get_billing(db_conn, organization_id=result.organization_id)
    assert billing.plan == "starter"
    assert billing.status == "none"


def test_create_customer_organization_rejects_a_blank_name(db_conn: psycopg.Connection) -> None:
    with pytest.raises(InvalidOrganizationNameError):
        create_customer_organization(db_conn, owner_user_id=uuid.uuid4(), organization_name="   ")


def test_colliding_names_get_distinct_slugs(
    db_conn: psycopg.Connection, cleanup_provisioned_orgs: list[uuid.UUID]
) -> None:
    first = create_customer_organization(
        db_conn, owner_user_id=uuid.uuid4(), organization_name="Collision Co"
    )
    second = create_customer_organization(
        db_conn, owner_user_id=uuid.uuid4(), organization_name="Collision Co"
    )
    cleanup_provisioned_orgs.extend([first.organization_id, second.organization_id])
    assert first.slug != second.slug
    assert first.slug == "collision-co"
    assert second.slug.startswith("collision-co-")


# ------------------------------------------------------------------------ API --


def test_unauthenticated_provisioning_is_blocked(app_state: AppState) -> None:
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    response = client.post("/organizations", json={"name": "No Auth Org"})
    assert response.status_code == 401


def test_api_key_cannot_provision_an_organization(
    app_state: AppState, api_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = api_client.post(
        "/api-keys", json={"label": "provisioning-probe", "scopes": ["leads:write"]}
    )
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw_client:
        response = raw_client.post("/organizations", json={"name": "Key Org"}, headers=headers)
    assert response.status_code == 403


def test_verified_user_can_provision_an_organization_and_becomes_owner(
    app_state: AppState, cleanup_provisioned_orgs: list[uuid.UUID]
) -> None:
    user_id = uuid.uuid4()
    client = _signed_up_client(app_state, user_id=user_id, email="founder@example.com")

    response = client.post("/organizations", json={"name": "Founder's Org"})
    assert response.status_code == 201, response.text
    org_id = uuid.UUID(response.json()["organization_id"])
    cleanup_provisioned_orgs.append(org_id)

    # The creator immediately has an authenticated owner session over the
    # organization they just made — no separate step required.
    owner_app = create_app(state=app_state)
    authorize_app(
        owner_app,
        AuthContext(organization_id=org_id, auth_method="jwt", user_id=user_id, role="owner"),
    )
    with TestClient(owner_app, raise_server_exceptions=False) as owner_client:
        read = owner_client.get("/organization")
    assert read.status_code == 200
    assert read.json()["name"] == "Founder's Org"


def test_cannot_provision_with_an_empty_name(app_state: AppState) -> None:
    client = _signed_up_client(app_state, user_id=uuid.uuid4(), email="founder@example.com")
    response = client.post("/organizations", json={"name": "   "})
    assert response.status_code == 422


def test_a_new_organization_gets_exactly_one_membership_row(
    app_state: AppState, db_conn: psycopg.Connection, cleanup_provisioned_orgs: list[uuid.UUID]
) -> None:
    """There is no field on `CreateOrganizationRequest` for `organization_id`
    or `slug` — the only way anyone reaches a specific organization's data
    is through membership (`arie.auth.resolve_auth_context` raises
    `NotAMemberError` for anyone else, proven generically by
    tests/integration/test_tenancy_isolation_integration.py). What
    provisioning itself owns, and what this test proves directly, is that
    it creates *exactly* the creator's own owner membership — never a
    second row a stranger's guessed or reused user id could already match.
    """
    owner_client = _signed_up_client(app_state, user_id=uuid.uuid4(), email="owner@example.com")
    created = owner_client.post("/organizations", json={"name": "Private Org"})
    assert created.status_code == 201
    org_id = uuid.UUID(created.json()["organization_id"])
    cleanup_provisioned_orgs.append(org_id)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, role FROM organization_members WHERE organization_id = %s", (org_id,)
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "owner"


def test_turnstile_blocks_provisioning_when_configured_and_token_is_missing(
    app_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "arie.turnstile.TURNSTILE", TurnstileConfig(secret_key="sk_test", site_key="pk_test")
    )
    client = _signed_up_client(app_state, user_id=uuid.uuid4(), email="blocked@example.com")
    response = client.post("/organizations", json={"name": "Blocked Org"})
    assert response.status_code == 403


def test_turnstile_is_bypassed_when_unconfigured(
    app_state: AppState, cleanup_provisioned_orgs: list[uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("arie.turnstile.TURNSTILE", TurnstileConfig(secret_key="", site_key=""))
    client = _signed_up_client(app_state, user_id=uuid.uuid4(), email="unblocked@example.com")
    response = client.post("/organizations", json={"name": "Unblocked Org"})
    assert response.status_code == 201
    cleanup_provisioned_orgs.append(uuid.UUID(response.json()["organization_id"]))
