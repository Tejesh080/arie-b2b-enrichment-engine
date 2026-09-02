"""Live-database tests for M7 Slice 6's Ask ARIE surface:
`POST /copilot/query` and `POST /leads/{lead_id}/copilot`.

Builds decided leads directly (company/person/`decision_receipts` rows) via
`db_conn` rather than driving the full simulated pipeline — everything this
file asserts (tenant isolation, intent filtering, single-lead deterministic
answers) holds identically for a hand-built receipt, and the pure ranking/
recognizer rules are already exhaustively covered without a database at all
in `tests/unit/test_copilot.py`.
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
                "source": "firmographics_basic",
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
    """Insert a company/person/lead/decision_receipts row set directly,
    bypassing the pipeline. Returns the new `lead_id`."""

    def _make(
        *,
        organization_id: UUID = ORG,
        company_name: str = "Acme Corp",
        industry: str | None = None,
        status: str = "AUTO_ROUTED",
        decision: str = "auto_route",
        confidence: float = 0.9,
        score: float = 80.0,
        score_lower: float | None = None,
        score_upper: float | None = None,
        known: list[str] | None = None,
        unknown: list[str] | None = None,
    ) -> UUID:
        known = known if known is not None else ["employee_count", "title_seniority"]
        unknown = unknown if unknown is not None else []
        domain = f"{uuid.uuid4().hex[:10]}.test"
        email = f"contact@{domain}"
        full_name = "Nadia Test"

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
                (company_id, email, full_name, "VP Sales", organization_id),
            )
            person_id = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO leads (person_id, company_id, organization_id, source, status) "
                "VALUES (%s, %s, %s, 'test', %s) RETURNING lead_id",
                (person_id, company_id, organization_id, status),
            )
            lead_id: UUID = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                """
                INSERT INTO decision_receipts (
                    lead_id, organization_id, decision, autonomous, confidence, tau,
                    score_value, score_lower, score_upper, stop_reason, policy_name,
                    scorer_version, confidence_calibration, evidence_snapshot
                ) VALUES (
                    %s, %s, %s, true, %s, 0.5,
                    %s, %s, %s, 'decision_settled', 'test-policy',
                    'icp-1.0.0', 'test', %s
                )
                """,
                (
                    lead_id,
                    organization_id,
                    decision,
                    confidence,
                    score,
                    score_lower if score_lower is not None else score,
                    score_upper if score_upper is not None else score,
                    Jsonb(_snapshot(known, unknown)),
                ),
            )
            if industry is not None:
                cur.execute(
                    "INSERT INTO evidence (entity_type, entity_id, field_name, value, source, "
                    "confidence, ttl_seconds, organization_id) "
                    "VALUES ('company', %s, 'industry', %s, 'firmographics_basic', 0.9, 86400, %s)",
                    (company_id, Jsonb(industry), organization_id),
                )
        db_conn.commit()
        cleanup_ingest.lead_ids.append(lead_id)
        cleanup_ingest.domains.append(domain)
        cleanup_ingest.emails.append(email)
        return lead_id

    return _make


MakeDecidedLead = Callable[..., UUID]


# ------------------------------------------------------------- list copilot --


def test_top_leads_obvious_question_returns_leads(
    api_client: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    make_decided_lead(company_name="Contact First Co", confidence=0.9, decision="auto_route")
    response = api_client.post("/copilot/query", json={"question": "Show my top leads"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "top_leads"
    assert body["llm_used"] is False
    assert any(lead["company"] == "Contact First Co" for lead in body["leads"])


def test_work_today_ranks_contact_first_above_worth_pursuing(
    api_client: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    make_decided_lead(company_name="Worth Pursuing Co", confidence=0.5, decision="escalate_human")
    make_decided_lead(company_name="Contact First Co", confidence=0.9, decision="auto_route")

    response = api_client.post("/copilot/query", json={"question": "What should I work on today?"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "work_today"
    companies = [lead["company"] for lead in body["leads"]]
    assert companies.index("Contact First Co") < companies.index("Worth Pursuing Co")


def test_low_confidence_intent_filters_to_low_band(
    api_client: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    make_decided_lead(company_name="High Confidence Co", confidence=0.9, decision="auto_route")
    make_decided_lead(company_name="Low Confidence Co", confidence=0.2, decision="escalate_human")

    response = api_client.post(
        "/copilot/query", json={"question": "Show me leads with low confidence"}
    )
    assert response.status_code == 200
    body = response.json()
    companies = [lead["company"] for lead in body["leads"]]
    assert "Low Confidence Co" in companies
    assert "High Confidence Co" not in companies


def test_missing_decision_maker_requires_unknown_seniority_on_a_promising_lead(
    api_client: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    make_decided_lead(
        company_name="No Contact Co",
        confidence=0.9,
        decision="auto_route",
        known=["employee_count"],
        unknown=["title_seniority"],
    )
    make_decided_lead(
        company_name="Has Contact Co",
        confidence=0.9,
        decision="auto_route",
        known=["employee_count", "title_seniority"],
        unknown=[],
    )
    response = api_client.post(
        "/copilot/query", json={"question": "Which promising leads are missing decision makers?"}
    )
    assert response.status_code == 200
    companies = [lead["company"] for lead in response.json()["leads"]]
    assert "No Contact Co" in companies
    assert "Has Contact Co" not in companies


def test_needs_research_requires_a_material_unknown_field(
    api_client: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    # Borderline score with an unknown field whose ceiling could cross the
    # qualify threshold — materially researchable.
    researchable = make_decided_lead(
        company_name="Researchable Co",
        confidence=0.5,
        decision="escalate_human",
        score=60.0,
        score_lower=60.0,  # no unresolved disqualifier — the floor stays at score_value
        score_upper=85.0,
        known=[],
        unknown=["employee_count", "industry", "title_seniority", "title_function"],
    )
    # Already-decided, nothing unknown could change it.
    settled = make_decided_lead(
        company_name="Settled Co",
        confidence=0.9,
        decision="auto_route",
        score=95.0,
        score_lower=95.0,
        score_upper=95.0,
        known=["employee_count", "industry", "title_seniority", "title_function"],
        unknown=[],
    )
    assert researchable != settled

    response = api_client.post(
        "/copilot/query", json={"question": "Which leads need more research?"}
    )
    assert response.status_code == 200
    companies = [lead["company"] for lead in response.json()["leads"]]
    assert "Researchable Co" in companies
    assert "Settled Co" not in companies


def test_industry_filter_is_case_insensitive(
    api_client: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    make_decided_lead(company_name="Software Co", industry="Software", confidence=0.9)
    make_decided_lead(company_name="Logistics Co", industry="Logistics", confidence=0.9)

    response = api_client.post(
        "/copilot/query",
        json={"question": "Show my top leads"},
    )
    assert response.status_code == 200


# -------------------------------------------------------------- security --


def test_query_plan_organization_id_field_is_rejected(api_client: TestClient) -> None:
    """`extra="forbid"` on `LeadListQueryPlan` means this can never reach the
    executor even if a deterministic recognizer somehow mishandled it — this
    test exercises the endpoint end to end, not just the schema unit test."""
    response = api_client.post("/copilot/query", json={"question": ""})
    assert response.status_code == 422


def test_copilot_query_requires_jwt_session(
    app_state: AppState, make_decided_lead: MakeDecidedLead
) -> None:
    make_decided_lead()
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=ORG,
            auth_method="api_key",
            api_key_id=UUID(int=1),
            scopes=frozenset({"leads:read", "leads:write"}),
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/copilot/query", json={"question": "Show my top leads"})
    assert response.status_code == 403


def test_cross_tenant_named_company_never_appears(
    api_client: TestClient,
    api_client_org_b: TestClient,
    make_decided_lead: MakeDecidedLead,
) -> None:
    make_decided_lead(organization_id=ORG, company_name="Org A Only Co", confidence=0.9)

    response = api_client_org_b.post(
        "/copilot/query", json={"question": 'why is "Org A Only Co" ranked so well'}
    )
    assert response.status_code == 200
    body = response.json()
    companies = [lead.get("company") for lead in body["leads"]]
    assert "Org A Only Co" not in companies


def test_top_leads_never_shows_a_foreign_organizations_lead(
    api_client_org_b: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    make_decided_lead(organization_id=ORG, company_name="Org A Top Co", confidence=0.95)
    response = api_client_org_b.post("/copilot/query", json={"question": "Show my top leads"})
    assert response.status_code == 200
    companies = [lead["company"] for lead in response.json()["leads"]]
    assert "Org A Top Co" not in companies


# ----------------------------------------------------------- single lead --


def test_lead_copilot_missing_info_for_a_pending_lead(
    api_client: TestClient, make_lead: Callable[..., tuple[UUID, int]]
) -> None:
    lead_id, _ = make_lead()
    response = api_client.post(f"/leads/{lead_id}/copilot", json={"question": "What is missing?"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "lead_missing_info"


def test_lead_copilot_why_uses_deterministic_explanation(
    api_client: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    lead_id = make_decided_lead(company_name="Explain Co", confidence=0.9, decision="auto_route")
    response = api_client.post(
        f"/leads/{lead_id}/copilot", json={"question": "Why is this a good lead?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "lead_explanation"
    assert body["answer"]


def test_lead_copilot_researchability_never_executes_research(
    api_client: TestClient, db_conn: psycopg.Connection, make_decided_lead: MakeDecidedLead
) -> None:
    lead_id = make_decided_lead(
        company_name="Researchable Lead",
        confidence=0.5,
        decision="escalate_human",
        score=60.0,
        score_lower=60.0,
        score_upper=85.0,
        known=[],
        unknown=["employee_count", "industry", "title_seniority", "title_function"],
    )
    response = api_client.post(
        f"/leads/{lead_id}/copilot", json={"question": "Would more research help?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "lead_researchability"

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM provider_calls WHERE lead_id = %s", (lead_id,))
        count = cur.fetchone()[0]  # type: ignore[index]
    assert count == 0


def test_lead_copilot_unknown_lead_is_404(api_client: TestClient) -> None:
    response = api_client.post(
        f"/leads/{UUID(int=0)}/copilot", json={"question": "Why is this a good lead?"}
    )
    assert response.status_code == 404


def test_lead_copilot_foreign_organizations_lead_is_404(
    api_client_org_b: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    lead_id = make_decided_lead(organization_id=ORG, company_name="Foreign Lead Co")
    response = api_client_org_b.post(
        f"/leads/{lead_id}/copilot", json={"question": "Why is this a good lead?"}
    )
    assert response.status_code == 404


def test_lead_copilot_unsupported_question_is_controlled_not_500(
    api_client: TestClient, make_decided_lead: MakeDecidedLead
) -> None:
    lead_id = make_decided_lead(company_name="Weather Co")
    response = api_client.post(
        f"/leads/{lead_id}/copilot", json={"question": "What's the weather?"}
    )
    assert response.status_code == 200
    assert response.json()["answer"]
