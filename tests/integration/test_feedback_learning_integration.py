"""Live-database tests for M7 Slice 7's feedback learning loop:
`GET /intelligence/feedback-insights` and `POST /intelligence/feedback/analyze`.

Builds companies/leads/evidence directly (the same `db_conn`-level pattern
`test_copilot_integration.py` already established) and attaches feedback via
`arie.feedback.submit_feedback` directly rather than through
`POST /leads/{id}/feedback` — this file is about the *analysis*, not
feedback submission itself (already covered by
`test_lead_recommendation_integration.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from tests.integration.conftest import IngestCleanup, authorize_app
from tests.unit.test_intelligence_targeting import SUPPLEMENT_DRAFT

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.feedback import FeedbackReason, FeedbackSentiment, submit_feedback
from arie.recommendations import CustomerPriority, NextAction
from arie.tenancy import LEGACY_ORGANIZATION_ID as ORG

pytestmark = pytest.mark.integration

INSIGHTS_URL = "/intelligence/feedback-insights"
ANALYZE_URL = "/intelligence/feedback/analyze"
CONFIRM_URL = "/intelligence/targeting/confirm"
PROPOSALS_URL = "/intelligence/proposals"


@pytest.fixture
def restore_active_profile(db_conn: psycopg.Connection) -> Iterator[None]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id FROM organization_icp_profiles "
            "WHERE organization_id = %s AND status = 'active'",
            (ORG,),
        )
        previously_active = cur.fetchone()
        cur.execute(
            "SELECT COALESCE(MAX(version), 0) FROM organization_icp_profiles WHERE organization_id = %s",
            (ORG,),
        )
        row = cur.fetchone()
    assert row is not None
    high_water = row[0]

    yield

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM profile_revision_proposals WHERE organization_id = %s", (ORG,))
        cur.execute(
            "DELETE FROM organization_icp_profiles WHERE organization_id = %s AND version > %s",
            (ORG, high_water),
        )
        if previously_active is not None:
            cur.execute(
                "UPDATE organization_icp_profiles SET status = 'active', retired_at = NULL "
                "WHERE profile_id = %s",
                (previously_active[0],),
            )
    db_conn.commit()


def _confirm_targeting(
    client: TestClient, *, micro_preference: str = "preferred"
) -> dict[str, Any]:
    response = client.post(
        CONFIRM_URL,
        json={
            "name": f"Feedback test targeting {uuid.uuid4().hex[:6]}",
            "objective": "best_prospects",
            "profile": {
                **SUPPLEMENT_DRAFT,
                "employee_band_preferences": {
                    "employees_1_10": micro_preference,
                    "employees_11_50": "acceptable",
                    "employees_51_200": "acceptable",
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


@pytest.fixture
def make_feedback_lead(db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup) -> Iterator[Any]:
    def _make(*, organization_id: uuid.UUID = ORG, employee_count: int | None = None) -> uuid.UUID:
        domain = f"{uuid.uuid4().hex[:10]}.test"
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO companies (canonical_domain, name, normalized_name) "
                "VALUES (%s, %s, %s) RETURNING company_id",
                (domain, "Feedback Test Co", "feedback test co"),
            )
            company_id = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO leads (company_id, organization_id, source) VALUES (%s, %s, 'test') "
                "RETURNING lead_id",
                (company_id, organization_id),
            )
            lead_id = cur.fetchone()[0]  # type: ignore[index]
            if employee_count is not None:
                cur.execute(
                    "INSERT INTO evidence (entity_type, entity_id, field_name, value, source, "
                    "confidence, ttl_seconds, organization_id) "
                    "VALUES ('company', %s, 'employee_count', %s, 'test', 0.9, 86400, %s)",
                    (company_id, Jsonb(employee_count), organization_id),
                )
        db_conn.commit()
        cleanup_ingest.lead_ids.append(lead_id)
        cleanup_ingest.domains.append(domain)
        return lead_id  # type: ignore[no-any-return]

    yield _make


def _give_feedback(
    db_conn: psycopg.Connection,
    lead_id: uuid.UUID,
    *,
    organization_id: uuid.UUID = ORG,
    sentiment: FeedbackSentiment,
    reason: FeedbackReason | None,
    profile_version: int,
) -> None:
    submit_feedback(
        db_conn,
        organization_id=organization_id,
        lead_id=lead_id,
        user_id=uuid.uuid4(),
        sentiment=sentiment,
        reason=reason,
        note=None,
        priority=CustomerPriority.REVIEW,
        next_action=NextAction.RESEARCH_MORE,
        profile_version=profile_version,
        score_snapshot=None,
    )


# --------------------------------------------------------------- thresholds --


def test_insufficient_feedback_shows_no_proposal(api_client: TestClient) -> None:
    response = api_client.get(INSIGHTS_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["support"] in ("insufficient_data", "summary_only", "eligible")
    # Whatever this org's running total is, analyzing must never fabricate a
    # proposal below the eligible tier.
    if body["support"] != "eligible":
        assert body["proposal"] is None


def test_analyze_below_proposal_threshold_creates_nothing(
    api_client: TestClient, db_conn: psycopg.Connection, make_feedback_lead: Any
) -> None:
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM lead_recommendation_feedback WHERE organization_id = %s", (ORG,))
    db_conn.commit()

    # 7 falls inside [MIN_FEEDBACK_FOR_SUMMARY, MIN_FEEDBACK_FOR_PROPOSAL) —
    # enough for a summary, not enough to be `eligible` for a proposal.
    for _ in range(7):
        lead_id = make_feedback_lead(employee_count=5)
        _give_feedback(
            db_conn,
            lead_id,
            sentiment=FeedbackSentiment.NEGATIVE,
            reason=FeedbackReason.COMPANY_TOO_SMALL,
            profile_version=1,
        )

    response = api_client.post(ANALYZE_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["support"] == "summary_only"
    assert body["proposal"] is None


# ------------------------------------------------------------- full loop --


def test_dominant_reason_produces_a_reusable_proposal_then_a_new_one_after_acceptance(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    make_feedback_lead: Any,
    restore_active_profile: None,
) -> None:
    profile = _confirm_targeting(api_client, micro_preference="preferred")
    version = profile["version"]

    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM lead_recommendation_feedback WHERE organization_id = %s", (ORG,))
    db_conn.commit()

    # 12 negative, dominated by company_too_small on micro companies; 15
    # positive elsewhere — enough support to clear both the total-feedback
    # gate and `arie.intelligence.outcomes.MODERATE_MIN_SAMPLE`.
    for _ in range(12):
        lead_id = make_feedback_lead(employee_count=5)
        _give_feedback(
            db_conn,
            lead_id,
            sentiment=FeedbackSentiment.NEGATIVE,
            reason=FeedbackReason.COMPANY_TOO_SMALL,
            profile_version=version,
        )
    for _ in range(15):
        lead_id = make_feedback_lead(employee_count=100)
        _give_feedback(
            db_conn,
            lead_id,
            sentiment=FeedbackSentiment.POSITIVE,
            reason=None,
            profile_version=version,
        )

    insights = api_client.get(INSIGHTS_URL).json()
    assert insights["support"] == "eligible"
    assert insights["total"] == 27
    assert insights["negative_reason_counts"]["company_too_small"] == 12
    assert insights["proposal"] is None  # GET never creates one

    first = api_client.post(ANALYZE_URL).json()
    assert first["proposal"] is not None
    assert first["proposal"]["source"] == "user_feedback"
    assert first["proposal"]["profile_version"] == version
    proposal_id = first["proposal"]["proposal_id"]
    change = next(c for c in first["proposal"]["changes"] if c["target"] == "employees_1_10")
    assert change["from_value"] == "preferred"
    assert change["to_value"] == "acceptable"

    # Calling analyze again must reuse the same open proposal (Part B1) —
    # never a second row for the same signal.
    second = api_client.post(ANALYZE_URL).json()
    assert second["proposal"]["proposal_id"] == proposal_id

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM profile_revision_proposals "
            "WHERE organization_id = %s AND source = 'user_feedback'",
            (ORG,),
        )
        count = cur.fetchone()[0]  # type: ignore[index]
    assert count == 1

    # Accept it -> a new immutable profile version, old one untouched.
    accepted = api_client.post(
        f"{PROPOSALS_URL}/{proposal_id}/accept", json={"name": "Applied from feedback"}
    )
    assert accepted.status_code == 201, accepted.text
    new_version = accepted.json()["version"]
    assert new_version == version + 1

    # More feedback under the *new* version can produce a fresh, distinct
    # proposal — different profile_version than the first.
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM lead_recommendation_feedback WHERE organization_id = %s", (ORG,))
    db_conn.commit()
    for _ in range(12):
        lead_id = make_feedback_lead(employee_count=5)
        _give_feedback(
            db_conn,
            lead_id,
            sentiment=FeedbackSentiment.NEGATIVE,
            reason=FeedbackReason.COMPANY_TOO_SMALL,
            profile_version=new_version,
        )
    for _ in range(15):
        lead_id = make_feedback_lead(employee_count=100)
        _give_feedback(
            db_conn,
            lead_id,
            sentiment=FeedbackSentiment.POSITIVE,
            reason=None,
            profile_version=new_version,
        )
    third = api_client.post(ANALYZE_URL).json()
    # The new active band preference is "acceptable" (from acceptance above),
    # so a further demotion candidate is a *different* change than the first
    # proposal's — either a fresh proposal or none, but never the same row.
    if third["proposal"] is not None:
        assert third["proposal"]["proposal_id"] != proposal_id
        assert third["proposal"]["profile_version"] == new_version


def test_foreign_organization_sees_no_feedback(
    api_client_org_b: TestClient, db_conn: psycopg.Connection, make_feedback_lead: Any
) -> None:
    lead_id = make_feedback_lead(organization_id=ORG, employee_count=5)
    _give_feedback(
        db_conn,
        lead_id,
        organization_id=ORG,
        sentiment=FeedbackSentiment.NEGATIVE,
        reason=FeedbackReason.COMPANY_TOO_SMALL,
        profile_version=1,
    )
    response = api_client_org_b.get(INSIGHTS_URL)
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_analyze_requires_org_admin(app_state: AppState, make_feedback_lead: Any) -> None:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=ORG, auth_method="jwt", user_id=uuid.uuid4(), role="analyst_reviewer"
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(ANALYZE_URL)
    assert response.status_code == 403


def test_llm_unavailable_still_produces_a_deterministic_proposal(
    app_state: AppState,
    db_conn: psycopg.Connection,
    make_feedback_lead: Any,
    restore_active_profile: None,
) -> None:
    from arie.api.main import get_llm_service
    from arie.llm.fake_provider import AlwaysFailingLLMProvider
    from arie.llm.service import LLMService

    app = create_app(state=app_state)
    auth = authorize_app(app)
    app.dependency_overrides[get_llm_service] = lambda: LLMService(
        app_state.pool, provider=AlwaysFailingLLMProvider()
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        profile = _confirm_targeting(client, micro_preference="preferred")
        version = profile["version"]

        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM lead_recommendation_feedback WHERE organization_id = %s",
                (auth.organization_id,),
            )
        db_conn.commit()
        for _ in range(12):
            lead_id = make_feedback_lead(organization_id=auth.organization_id, employee_count=5)
            _give_feedback(
                db_conn,
                lead_id,
                organization_id=auth.organization_id,
                sentiment=FeedbackSentiment.NEGATIVE,
                reason=FeedbackReason.COMPANY_TOO_SMALL,
                profile_version=version,
            )
        for _ in range(15):
            lead_id = make_feedback_lead(organization_id=auth.organization_id, employee_count=100)
            _give_feedback(
                db_conn,
                lead_id,
                organization_id=auth.organization_id,
                sentiment=FeedbackSentiment.POSITIVE,
                reason=None,
                profile_version=version,
            )

        result = client.post(ANALYZE_URL)
        assert result.status_code == 200
        proposal = result.json()["proposal"]
        assert proposal is not None
        assert (
            "Based on" in proposal["summary"]
            or "recommendations you gave feedback on" in proposal["summary"]
        )
