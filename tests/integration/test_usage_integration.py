"""Productization M3, Part 7 — GET /usage.

Builds leads/provider_calls/model_calls directly against the database with
explicit timestamps, rather than driving the real worker — this endpoint is
pure aggregation over already-persisted rows, and controlling the exact
`created_at`/`requested_at` values a test needs is otherwise impossible
through the ingestion API (which always uses `now()`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from arie.api.main import AppState, create_app
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _insert_lead(
    db_conn: psycopg.Connection,
    *,
    organization_id: UUID,
    status: str,
    created_at: datetime,
) -> UUID:
    lead_id = uuid.uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (lead_id, source, organization_id, status, created_at) "
            "VALUES (%s, 'usage-it', %s, %s, %s)",
            (lead_id, organization_id, status, created_at),
        )
    db_conn.commit()
    return lead_id


def _insert_provider_call(
    db_conn: psycopg.Connection,
    *,
    organization_id: UUID,
    lead_id: UUID,
    cost_usd: float,
    cache_hit: bool,
    requested_at: datetime,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO provider_calls (call_id, lead_id, provider, entity_type, entity_id, "
            "idempotency_key, requested_at, cost_usd, status, cache_hit, organization_id) "
            "VALUES (%s, %s, 'usage-it-provider', 'company', %s, %s, %s, %s, 'success', %s, %s)",
            (
                uuid.uuid4(),
                lead_id,
                uuid.uuid4(),
                f"usage-it:{uuid.uuid4().hex}",
                requested_at,
                cost_usd,
                cache_hit,
                organization_id,
            ),
        )
    db_conn.commit()


def _insert_model_call(
    db_conn: psycopg.Connection,
    *,
    organization_id: UUID,
    lead_id: UUID,
    cost_usd: float,
    created_at: datetime,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_calls (call_id, lead_id, model, tier, purpose, cost_usd, "
            "created_at, organization_id) "
            "VALUES (%s, %s, 'usage-it-model', 'cheap', 'usage-it', %s, %s, %s)",
            (uuid.uuid4(), lead_id, cost_usd, created_at, organization_id),
        )
    db_conn.commit()


@pytest.fixture
def usage_cleanup(db_conn: psycopg.Connection) -> Iterator[list[UUID]]:
    lead_ids: list[UUID] = []
    yield lead_ids
    if lead_ids:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM model_calls WHERE lead_id = ANY(%s)", (lead_ids,))
            cur.execute("DELETE FROM provider_calls WHERE lead_id = ANY(%s)", (lead_ids,))
            cur.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (lead_ids,))
        db_conn.commit()


def _usage(client: TestClient, *, from_at: datetime, to_at: datetime) -> dict[str, Any]:
    response = client.get("/usage", params={"from": from_at.isoformat(), "to": to_at.isoformat()})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def test_counts_and_costs_are_aggregated_within_the_range(
    api_client: TestClient, db_conn: psycopg.Connection, usage_cleanup: list[UUID]
) -> None:
    in_range = NOW + timedelta(hours=1)
    qualified_lead = _insert_lead(
        db_conn, organization_id=LEGACY_ORGANIZATION_ID, status="AUTO_ROUTED", created_at=in_range
    )
    rejected_lead = _insert_lead(
        db_conn, organization_id=LEGACY_ORGANIZATION_ID, status="SYNCED", created_at=in_range
    )
    review_lead = _insert_lead(
        db_conn,
        organization_id=LEGACY_ORGANIZATION_ID,
        status="AWAITING_HUMAN",
        created_at=in_range,
    )
    failed_lead = _insert_lead(
        db_conn, organization_id=LEGACY_ORGANIZATION_ID, status="DEAD_LETTER", created_at=in_range
    )
    pending_lead = _insert_lead(
        db_conn, organization_id=LEGACY_ORGANIZATION_ID, status="NEW", created_at=in_range
    )
    usage_cleanup.extend([qualified_lead, rejected_lead, review_lead, failed_lead, pending_lead])

    _insert_provider_call(
        db_conn,
        organization_id=LEGACY_ORGANIZATION_ID,
        lead_id=qualified_lead,
        cost_usd=0.02,
        cache_hit=False,
        requested_at=in_range,
    )
    _insert_provider_call(
        db_conn,
        organization_id=LEGACY_ORGANIZATION_ID,
        lead_id=qualified_lead,
        cost_usd=0.0,
        cache_hit=True,
        requested_at=in_range,
    )
    _insert_model_call(
        db_conn,
        organization_id=LEGACY_ORGANIZATION_ID,
        lead_id=qualified_lead,
        cost_usd=0.01,
        created_at=in_range,
    )

    body = _usage(api_client, from_at=NOW, to_at=NOW + timedelta(days=1))

    assert body["leads_processed"] == 5
    assert body["qualified_count"] == 1
    assert body["rejected_count"] == 1
    assert body["review_count"] == 1
    assert body["failed_count"] == 1
    assert body["pending_count"] == 1
    assert body["provider_calls"] == 1  # cache hit excluded
    assert body["cache_hits"] == 1
    assert abs(body["provider_cost_usd"] - 0.02) < 1e-9
    assert abs(body["model_cost_usd"] - 0.01) < 1e-9
    assert abs(body["total_cost_usd"] - 0.03) < 1e-9


def test_leads_outside_the_range_are_excluded(
    api_client: TestClient, db_conn: psycopg.Connection, usage_cleanup: list[UUID]
) -> None:
    before_range = _insert_lead(
        db_conn,
        organization_id=LEGACY_ORGANIZATION_ID,
        status="AUTO_ROUTED",
        created_at=NOW - timedelta(days=10),
    )
    after_range = _insert_lead(
        db_conn,
        organization_id=LEGACY_ORGANIZATION_ID,
        status="AUTO_ROUTED",
        created_at=NOW + timedelta(days=10),
    )
    usage_cleanup.extend([before_range, after_range])

    body = _usage(api_client, from_at=NOW, to_at=NOW + timedelta(days=1))

    assert body["leads_processed"] == 0


def test_a_different_organizations_leads_are_not_counted(
    api_client: TestClient,
    api_client_org_b: TestClient,
    db_conn: psycopg.Connection,
    other_org: UUID,
    usage_cleanup: list[UUID],
) -> None:
    in_range = NOW + timedelta(hours=1)
    org_b_lead = _insert_lead(
        db_conn, organization_id=other_org, status="AUTO_ROUTED", created_at=in_range
    )
    usage_cleanup.append(org_b_lead)

    org_a_body = _usage(api_client, from_at=NOW, to_at=NOW + timedelta(days=1))
    org_b_body = _usage(api_client_org_b, from_at=NOW, to_at=NOW + timedelta(days=1))

    assert org_a_body["leads_processed"] == 0
    assert org_b_body["leads_processed"] == 1


def test_from_must_be_strictly_before_to(api_client: TestClient) -> None:
    response = api_client.get("/usage", params={"from": NOW.isoformat(), "to": NOW.isoformat()})
    assert response.status_code == 422


def test_defaults_to_the_trailing_30_days_with_no_params(api_client: TestClient) -> None:
    response = api_client.get("/usage")
    assert response.status_code == 200
    body = response.json()
    from_at = datetime.fromisoformat(body["from_at"])
    to_at = datetime.fromisoformat(body["to_at"])
    assert (to_at - from_at) == timedelta(days=30)


def test_an_api_key_cannot_read_usage(
    api_client: TestClient, app_state: AppState, cleanup_api_keys: list[UUID]
) -> None:
    created = api_client.post("/api-keys", json={"label": "usage-probe", "scopes": ["leads:read"]})
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw:
        response = raw.get("/usage", headers=headers)

    assert response.status_code == 403
