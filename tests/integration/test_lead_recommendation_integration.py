"""Live-database tests for the M7 Slice 4 customer-facing surface:
`GET /leads/{lead_id}/recommendation`, `POST /leads/{lead_id}/explanation`,
and `POST`/`GET /leads/{lead_id}/feedback`.

Uses `make_lead` (a bare, undecided lead — no worker cycle run) rather than
driving a lead through the full simulated pipeline like
`test_receipt_integration.py` does: everything this file asserts —
tenancy, auth, the feedback upsert, the "pending" recommendation shape, the
deterministic explanation fallback for a lead with no evidence — holds
identically for a pending lead, and the deterministic priority/next-action
*rules themselves* are already exhaustively covered without a database at
all in `tests/unit/test_recommendations.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.tenancy import LEGACY_ORGANIZATION_ID as ORG

pytestmark = pytest.mark.integration

MakeLead = Callable[..., tuple[UUID, int]]


# --------------------------------------------------------- recommendation --


def test_recommendation_for_a_pending_lead_is_review(
    api_client: TestClient, make_lead: MakeLead
) -> None:
    lead_id, _ = make_lead()
    response = api_client.get(f"/leads/{lead_id}/recommendation")
    assert response.status_code == 200
    body = response.json()
    assert body["priority"] == "review"
    assert body["next_action"] == "research_more"
    assert body["explanation_status"] == "not_requested"
    assert body["score"] is None
    assert body["research_status"] == "not_performed"


def test_recommendation_for_unknown_lead_is_404(api_client: TestClient) -> None:
    response = api_client.get(f"/leads/{UUID(int=0)}/recommendation")
    assert response.status_code == 404


def test_recommendation_for_a_foreign_organizations_lead_is_404(
    api_client_org_b: TestClient, make_lead: MakeLead
) -> None:
    lead_id, _ = make_lead(organization_id=ORG)
    response = api_client_org_b.get(f"/leads/{lead_id}/recommendation")
    assert response.status_code == 404


# ------------------------------------------------------------- explanation --


def test_explanation_for_a_lead_with_no_evidence_is_deterministic(
    api_client: TestClient, make_lead: MakeLead
) -> None:
    """A lead with no resolved company/person has no evidence pool, so the
    explanation degrades to the always-available fallback rather than
    attempting a model call — see `arie.intelligence.explanation.
    explain_from_pool`'s empty-pool branch."""
    lead_id, _ = make_lead()
    response = api_client.post(f"/leads/{lead_id}/explanation")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "deterministic"


# ---------------------------------------------------------------- feedback --


def test_submit_and_read_back_feedback(api_client: TestClient, make_lead: MakeLead) -> None:
    lead_id, _ = make_lead()
    response = api_client.post(
        f"/leads/{lead_id}/feedback",
        json={"sentiment": "positive", "reason": "good_match", "note": "great fit"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sentiment"] == "positive"
    assert body["reason"] == "good_match"
    assert body["recommendation_priority"] == "review"  # pending lead, per above

    read_back = api_client.get(f"/leads/{lead_id}/feedback")
    assert read_back.status_code == 200
    assert read_back.json()["feedback_id"] == body["feedback_id"]


def test_changing_feedback_replaces_rather_than_duplicates(
    api_client: TestClient, make_lead: MakeLead
) -> None:
    lead_id, _ = make_lead()
    first = api_client.post(f"/leads/{lead_id}/feedback", json={"sentiment": "positive"})
    second = api_client.post(
        f"/leads/{lead_id}/feedback",
        json={"sentiment": "negative", "reason": "wrong_industry"},
    )
    assert first.json()["feedback_id"] == second.json()["feedback_id"]
    assert second.json()["sentiment"] == "negative"
    assert second.json()["reason"] == "wrong_industry"

    read_back = api_client.get(f"/leads/{lead_id}/feedback")
    assert read_back.json()["sentiment"] == "negative"


def test_duplicate_positive_click_is_idempotent(
    api_client: TestClient, make_lead: MakeLead
) -> None:
    lead_id, _ = make_lead()
    first = api_client.post(f"/leads/{lead_id}/feedback", json={"sentiment": "positive"})
    second = api_client.post(f"/leads/{lead_id}/feedback", json={"sentiment": "positive"})
    assert first.json()["feedback_id"] == second.json()["feedback_id"]


def test_feedback_requires_a_valid_reason(api_client: TestClient, make_lead: MakeLead) -> None:
    lead_id, _ = make_lead()
    response = api_client.post(
        f"/leads/{lead_id}/feedback", json={"sentiment": "negative", "reason": "not_a_real_reason"}
    )
    assert response.status_code == 422


def test_feedback_on_foreign_organizations_lead_is_404(
    api_client_org_b: TestClient, make_lead: MakeLead
) -> None:
    lead_id, _ = make_lead(organization_id=ORG)
    response = api_client_org_b.post(f"/leads/{lead_id}/feedback", json={"sentiment": "positive"})
    assert response.status_code == 404


def test_feedback_is_denied_for_an_api_key(app_state: AppState, make_lead: MakeLead) -> None:
    lead_id, _ = make_lead()
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
        response = client.post(f"/leads/{lead_id}/feedback", json={"sentiment": "positive"})
    assert response.status_code == 403


def test_feedback_never_changes_the_lead_or_its_score(
    api_client: TestClient, make_lead: MakeLead
) -> None:
    lead_id, version = make_lead()
    api_client.post(
        f"/leads/{lead_id}/feedback", json={"sentiment": "negative", "reason": "bad_match"}
    )
    lead = api_client.get(f"/leads/{lead_id}").json()
    assert lead["version"] == version
    assert lead["status"] == "NEW"
