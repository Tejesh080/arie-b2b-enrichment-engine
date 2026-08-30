"""Productization M4 Part 8 — the onboarding checklist. Every step is
derived from real rows in other tables (never a second, independently
maintained copy) — these tests build that state directly rather than
driving the full API surface for each step, since the point under test is
the derivation, not any one endpoint.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.onboarding import get_onboarding_status

pytestmark = pytest.mark.integration


def _insert_org(db_conn: psycopg.Connection, organization_id: uuid.UUID) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (organization_id, "Onboarding Test Org", f"onb-test-{organization_id.hex[:10]}"),
        )
    db_conn.commit()


def test_a_brand_new_organization_has_completed_nothing(db_conn: psycopg.Connection) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)

    status = get_onboarding_status(db_conn, organization_id=org_id)

    assert status.account_created is True
    assert status.organization_configured is True  # name is NOT NULL from creation
    assert status.icp_configured is False
    assert status.provider_configured is False
    assert status.first_upload_completed is False
    assert status.first_batch_processed is False
    assert status.completed is False
    assert status.completed_at is None


def test_icp_step_completes_once_a_profile_exists(db_conn: psycopg.Connection) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_icp_profiles "
            "(organization_id, version, name, config, scorer_version, status) "
            "VALUES (%s, 1, 'x', '{}'::jsonb, 'icp-1.0.0', 'active')",
            (org_id,),
        )
    db_conn.commit()

    status = get_onboarding_status(db_conn, organization_id=org_id)
    assert status.icp_configured is True
    assert status.completed is False  # upload/processing still missing


def test_provider_step_is_not_required_for_completion(db_conn: psycopg.Connection) -> None:
    """Provider configuration is explicitly optional — an organization can
    complete onboarding entirely on simulated mode."""
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    _complete_everything_except_provider(db_conn, org_id)

    status = get_onboarding_status(db_conn, organization_id=org_id)

    assert status.provider_configured is False
    assert status.completed is True


def test_completed_at_is_stamped_exactly_once(db_conn: psycopg.Connection) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    _complete_everything_except_provider(db_conn, org_id)

    first = get_onboarding_status(db_conn, organization_id=org_id)
    assert first.completed_at is not None

    second = get_onboarding_status(db_conn, organization_id=org_id)
    assert second.completed_at == first.completed_at  # never re-stamped


def _complete_everything_except_provider(db_conn: psycopg.Connection, org_id: uuid.UUID) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_icp_profiles "
            "(organization_id, version, name, config, scorer_version, status) "
            "VALUES (%s, 1, 'x', '{}'::jsonb, 'icp-1.0.0', 'active')",
            (org_id,),
        )
        cur.execute(
            "INSERT INTO lead_batches "
            "(batch_id, organization_id, filename, total_rows, accepted_rows, rejected_rows, "
            "created_by_user_id) VALUES (gen_random_uuid(), %s, 'x.csv', 1, 1, 0, %s)",
            (org_id, uuid.uuid4()),
        )
        cur.execute(
            "INSERT INTO leads (lead_id, source, organization_id, status) "
            "VALUES (gen_random_uuid(), 'test', %s, 'SYNCED')",
            (org_id,),
        )
    db_conn.commit()


def test_onboarding_endpoint_requires_a_jwt_session(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=org_id, auth_method="jwt", user_id=uuid.uuid4(), role="analyst_reviewer"
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/organization/onboarding")
    assert response.status_code == 200


def test_an_api_key_cannot_read_onboarding_status(
    api_client: TestClient, app_state: AppState, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = api_client.post(
        "/api-keys", json={"label": "onboarding-probe", "scopes": ["leads:write"]}
    )
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw_client:
        response = raw_client.get("/organization/onboarding", headers=headers)
    assert response.status_code == 403
