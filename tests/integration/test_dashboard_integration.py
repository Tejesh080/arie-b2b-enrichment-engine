"""Live-database tests for M7 Slice 7's `GET /dashboard` — Parts H/Q/V.

Reuses the same decided-lead helper `test_copilot_integration.py` already
established (the dashboard's `top_leads`/`priority_counts` go through the
identical bounded pool `arie.copilot_service._fetch_lead_pool` builds).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from tests.integration.conftest import IngestCleanup, authorize_app

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.tenancy import LEGACY_ORGANIZATION_ID as ORG

pytestmark = pytest.mark.integration


def _snapshot(known: list[str], unknown: list[str]) -> dict[str, Any]:
    return {
        "known": [
            {
                "field": f,
                "source": "test",
                "confidence": 0.9,
                "candidate_count": 1,
                "contested": False,
            }
            for f in known
        ],
        "unknown": unknown,
        "execution_mode": "simulated",
    }


@pytest.fixture
def make_decided_lead(
    db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> Callable[..., UUID]:
    def _make(
        *,
        organization_id: UUID = ORG,
        company_name: str = "Dashboard Test Co",
        status: str = "AUTO_ROUTED",
        decision: str = "auto_route",
        confidence: float = 0.9,
        score: float = 80.0,
    ) -> UUID:
        domain = f"{uuid.uuid4().hex[:10]}.test"
        email = f"contact@{domain}"
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO companies (canonical_domain, name, normalized_name) "
                "VALUES (%s, %s, %s) RETURNING company_id",
                (domain, company_name, company_name.lower()),
            )
            company_id = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO persons (company_id, canonical_email, full_name, title, organization_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING person_id",
                (company_id, email, "Test Contact", "VP Sales", organization_id),
            )
            person_id = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO leads (person_id, company_id, organization_id, source, status) "
                "VALUES (%s, %s, %s, 'test', %s) RETURNING lead_id",
                (person_id, company_id, organization_id, status),
            )
            lead_id = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                """
                INSERT INTO decision_receipts (
                    lead_id, organization_id, decision, autonomous, confidence, tau,
                    score_value, score_lower, score_upper, stop_reason, policy_name,
                    scorer_version, confidence_calibration, evidence_snapshot
                ) VALUES (%s, %s, %s, true, %s, 0.5, %s, %s, %s, 'decision_settled', 'test-policy',
                    'icp-1.0.0', 'test', %s)
                """,
                (
                    lead_id,
                    organization_id,
                    decision,
                    confidence,
                    score,
                    score,
                    score,
                    Jsonb(_snapshot(["employee_count"], [])),
                ),
            )
        db_conn.commit()
        cleanup_ingest.lead_ids.append(lead_id)
        cleanup_ingest.domains.append(domain)
        cleanup_ingest.emails.append(email)
        return lead_id  # type: ignore[no-any-return]

    return _make


def test_dashboard_priority_counts_and_top_leads(
    api_client: TestClient, make_decided_lead: Callable[..., UUID]
) -> None:
    make_decided_lead(company_name="Contact First Co", confidence=0.9, decision="auto_route")
    make_decided_lead(company_name="Worth Pursuing Co", confidence=0.5, decision="escalate_human")

    response = api_client.get("/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["priority_counts"]["contact_first"] >= 1
    assert body["priority_counts"]["worth_pursuing"] >= 1
    companies = [lead["company"] for lead in body["top_leads"]]
    # Contact First outranks Worth Pursuing in the WORK_TODAY ranking this
    # reuses — see arie.copilot.rank_work_today, already unit-tested.
    if "Contact First Co" in companies and "Worth Pursuing Co" in companies:
        assert companies.index("Contact First Co") < companies.index("Worth Pursuing Co")
    assert len(body["top_leads"]) <= 5


def test_dashboard_has_feedback_and_proposal_and_batch_fields(api_client: TestClient) -> None:
    response = api_client.get("/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert "feedback" in body
    assert "total" in body["feedback"]
    assert "open_proposals" in body
    assert "latest_batch" in body


def test_dashboard_never_leaks_a_foreign_organizations_lead(
    api_client_org_b: TestClient, make_decided_lead: Callable[..., UUID]
) -> None:
    make_decided_lead(organization_id=ORG, company_name="Org A Dashboard Co", confidence=0.95)
    response = api_client_org_b.get("/dashboard")
    assert response.status_code == 200
    companies = [lead["company"] for lead in response.json()["top_leads"]]
    assert "Org A Dashboard Co" not in companies


def test_dashboard_denies_an_api_key(app_state: AppState) -> None:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=ORG,
            auth_method="api_key",
            api_key_id=UUID(int=1),
            scopes=frozenset({"leads:read"}),
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/dashboard")
    assert response.status_code == 403
