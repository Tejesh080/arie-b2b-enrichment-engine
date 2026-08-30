"""Productization M4 Part 9 — organization usage limits. Enforcement is
tested at the real API endpoints (`POST /leads`, `POST /batches`), not just
`arie.limits` directly — the M4 brief's explicit requirement is that limits
are checked server-side, not merely reported.
"""

from __future__ import annotations

import io
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import IngestCleanup, authorize_app, source_for

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration


def _insert_org(
    db_conn: psycopg.Connection, organization_id: uuid.UUID, **overrides: object
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (organization_id, "Limits Test Org", f"limits-test-{organization_id.hex[:10]}"),
        )
        # `column` comes only from this test file's own literal kwarg names
        # at each call site — never external input.
        for column, value in overrides.items():
            cur.execute(
                f"UPDATE organizations SET {column} = %s WHERE organization_id = %s",
                (value, organization_id),
            )
    db_conn.commit()


def _client_as(
    app_state: AppState, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=organization_id, auth_method="jwt", user_id=user_id, role="owner"
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


# -------------------------------------------------------------------------- read --


def test_limits_endpoint_reports_sensible_defaults(api_client: TestClient) -> None:
    response = api_client.get("/organization/limits")
    assert response.status_code == 200
    body = response.json()
    assert body["leads_limit"] == 5000
    assert body["max_csv_rows_per_upload"] == 200
    assert body["modeled_spend_limit_usd"] == 50.0
    assert body["leads_used"] <= body["leads_limit"]
    assert body["leads_remaining"] == body["leads_limit"] - body["leads_used"]


def test_limits_endpoint_requires_a_jwt_session(app_state: AppState) -> None:
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
        response = client.get("/organization/limits")
    assert response.status_code == 200


# --------------------------------------------------------------- lead quota --


def test_post_leads_is_rejected_once_the_monthly_quota_is_reached(
    app_state: AppState, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, max_leads_per_month=0)
    client = _client_as(app_state, organization_id=org_id, user_id=uuid.uuid4())

    response = client.post(
        "/leads",
        json={
            "source": source_for("limits-lead"),
            "email": f"quota-{uuid.uuid4().hex[:8]}@example.com",
        },
    )

    assert response.status_code == 429


def test_post_leads_succeeds_within_quota(
    app_state: AppState, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)  # default quota, plenty of room
    client = _client_as(app_state, organization_id=org_id, user_id=uuid.uuid4())

    response = client.post(
        "/leads",
        json={
            "source": source_for("limits-lead-ok"),
            "email": f"withinquota-{uuid.uuid4().hex[:8]}@example.com",
        },
    )

    assert response.status_code == 201
    cleanup_ingest.lead_ids.append(uuid.UUID(response.json()["lead_id"]))


# ---------------------------------------------------------------- csv quota --


def _csv_bytes(row_count: int) -> bytes:
    lines = ["email"] + [f"row{i}-{uuid.uuid4().hex[:6]}@example.com" for i in range(row_count)]
    return "\n".join(lines).encode("utf-8")


def test_batch_upload_is_rejected_when_it_exceeds_the_orgs_row_limit(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, max_csv_rows_per_upload=2)
    client = _client_as(app_state, organization_id=org_id, user_id=uuid.uuid4())

    response = client.post(
        "/batches",
        files={"file": ("leads.csv", io.BytesIO(_csv_bytes(5)), "text/csv")},
    )

    assert response.status_code == 429


def test_batch_upload_succeeds_within_the_orgs_row_limit(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, max_csv_rows_per_upload=10)
    client = _client_as(app_state, organization_id=org_id, user_id=uuid.uuid4())

    response = client.post(
        "/batches",
        files={"file": ("leads.csv", io.BytesIO(_csv_bytes(3)), "text/csv")},
    )

    assert response.status_code == 201


def test_batch_upload_is_rejected_once_the_monthly_lead_quota_is_reached(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, max_leads_per_month=0)
    client = _client_as(app_state, organization_id=org_id, user_id=uuid.uuid4())

    response = client.post(
        "/batches",
        files={"file": ("leads.csv", io.BytesIO(_csv_bytes(2)), "text/csv")},
    )

    assert response.status_code == 429


# ------------------------------------------------------------ modeled vs billed --


def test_modeled_spend_field_names_never_claim_to_be_billed(api_client: TestClient) -> None:
    body = api_client.get("/organization/limits").json()
    for key in body:
        assert "billed" not in key.lower()
        assert "invoice" not in key.lower()
