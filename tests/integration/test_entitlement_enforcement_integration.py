"""Productization M6 Part 8/9/20/21 — plan entitlements actually enforced
server-side, against a real database: member quota (invite + accept),
downgrade preserving existing members, and the provider/execution-mode
"live feature" boundary.
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

pytestmark = pytest.mark.integration


@pytest.fixture
def org(db_conn: psycopg.Connection) -> Iterator[uuid.UUID]:
    org_id = uuid.uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (org_id, "Entitlement Test Org", f"ent-test-{org_id.hex[:10]}"),
        )
    db_conn.commit()
    try:
        yield org_id
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM billing_webhook_events WHERE organization_id = %s", (org_id,))
            cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
        db_conn.commit()


def _set_plan(db_conn: psycopg.Connection, org_id: uuid.UUID, *, plan: str, status: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_billing SET plan = %s, status = %s WHERE organization_id = %s",
            (plan, status, org_id),
        )
    db_conn.commit()
    from arie.billing.plans import sync_organization_limits

    sync_organization_limits(db_conn, organization_id=org_id)


def _owner_client(app_state: AppState, *, org_id: uuid.UUID, user_id: uuid.UUID) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app, AuthContext(organization_id=org_id, auth_method="jwt", user_id=user_id, role="owner")
    )
    return TestClient(app, raise_server_exceptions=False)


def _seed_owner_membership(
    db_conn: psycopg.Connection, org_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_members (organization_id, user_id, role, status) "
            "VALUES (%s, %s, 'owner', 'active')",
            (org_id, user_id),
        )
    db_conn.commit()


# ------------------------------------------------------------------ member quota --


def test_invitation_is_blocked_when_the_unsubscribed_floor_is_already_at_capacity(
    app_state: AppState, db_conn: psycopg.Connection, org: uuid.UUID
) -> None:
    owner_id = uuid.uuid4()
    _seed_owner_membership(db_conn, org, owner_id)  # UNSUBSCRIBED.max_members == 1, already met
    client = _owner_client(app_state, org_id=org, user_id=owner_id)

    response = client.post(
        "/organization/invitations", json={"email": "second@example.com", "role": "admin"}
    )
    assert response.status_code == 402


def test_invitation_succeeds_under_a_plan_with_headroom(
    app_state: AppState, db_conn: psycopg.Connection, org: uuid.UUID
) -> None:
    owner_id = uuid.uuid4()
    _seed_owner_membership(db_conn, org, owner_id)
    _set_plan(db_conn, org, plan="internal", status="active")  # max_members == 25
    client = _owner_client(app_state, org_id=org, user_id=owner_id)

    response = client.post(
        "/organization/invitations", json={"email": "second@example.com", "role": "admin"}
    )
    assert response.status_code == 201


def test_accept_is_reblocked_if_the_organization_was_downgraded_after_the_invite_was_sent(
    app_state: AppState, db_conn: psycopg.Connection, org: uuid.UUID
) -> None:
    owner_id = uuid.uuid4()
    _seed_owner_membership(db_conn, org, owner_id)
    _set_plan(db_conn, org, plan="internal", status="active")  # headroom when the invite is sent
    owner_client = _owner_client(app_state, org_id=org, user_id=owner_id)

    invited = owner_client.post(
        "/organization/invitations", json={"email": "later@example.com", "role": "admin"}
    )
    assert invited.status_code == 201
    token = invited.json()["raw_token"]

    _set_plan(db_conn, org, plan="starter", status="none")  # back to UNSUBSCRIBED (max_members=1)

    accept_app = create_app(state=app_state)
    accept_app.dependency_overrides[get_verified_identity] = lambda: VerifiedIdentity(
        user_id=uuid.uuid4(), email="later@example.com"
    )
    with TestClient(accept_app, raise_server_exceptions=False) as accept_client:
        response = accept_client.post("/invitations/accept", json={"token": token})
    assert response.status_code == 402

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM organization_members WHERE organization_id = %s AND status = 'active'",
            (org,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 1  # the rejected accept never created a second member


def test_downgrade_never_removes_existing_active_members(
    app_state: AppState, db_conn: psycopg.Connection, org: uuid.UUID
) -> None:
    owner_id = uuid.uuid4()
    _seed_owner_membership(db_conn, org, owner_id)
    _set_plan(db_conn, org, plan="internal", status="active")
    with db_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO organization_members (organization_id, user_id, role, status) "
            "VALUES (%s, %s, 'admin', 'active')",
            [(org, uuid.uuid4()) for _ in range(3)],
        )
    db_conn.commit()

    _set_plan(db_conn, org, plan="starter", status="none")  # UNSUBSCRIBED.max_members == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM organization_members WHERE organization_id = %s AND status = 'active'",
            (org,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 4  # all 4 preserved despite being over the new ceiling

    client = _owner_client(app_state, org_id=org, user_id=owner_id)
    blocked = client.post(
        "/organization/invitations", json={"email": "one.more@example.com", "role": "admin"}
    )
    assert blocked.status_code == 402  # but no *new* member can be added while over quota

    still_accessible = client.get("/organization")
    assert still_accessible.status_code == 200  # owner never locked out of their own org


# ----------------------------------------------------- live-provider boundary --


def test_unsubscribed_organization_cannot_configure_a_live_provider(
    app_state: AppState, db_conn: psycopg.Connection, org: uuid.UUID
) -> None:
    owner_id = uuid.uuid4()
    _seed_owner_membership(db_conn, org, owner_id)  # plan=starter/status=none -> UNSUBSCRIBED
    client = _owner_client(app_state, org_id=org, user_id=owner_id)

    response = client.put(
        "/organization/providers/abstract_company_enrichment", json={"credential": "sk_test"}
    )
    assert response.status_code == 402


def test_unsubscribed_organization_cannot_enable_live_execution_mode(
    app_state: AppState, db_conn: psycopg.Connection, org: uuid.UUID
) -> None:
    owner_id = uuid.uuid4()
    _seed_owner_membership(db_conn, org, owner_id)
    client = _owner_client(app_state, org_id=org, user_id=owner_id)

    response = client.patch("/organization/execution-mode", json={"execution_mode": "live_shadow"})
    assert response.status_code == 402

    unchanged = client.get("/organization").json()
    assert unchanged["execution_mode"] == "simulated"


def test_subscribed_organization_can_configure_a_live_provider(
    app_state: AppState, db_conn: psycopg.Connection, org: uuid.UUID
) -> None:
    owner_id = uuid.uuid4()
    _seed_owner_membership(db_conn, org, owner_id)
    _set_plan(db_conn, org, plan="growth", status="active")
    client = _owner_client(app_state, org_id=org, user_id=owner_id)

    response = client.patch("/organization/execution-mode", json={"execution_mode": "live_shadow"})
    assert response.status_code == 200
    assert response.json()["execution_mode"] == "live_shadow"
