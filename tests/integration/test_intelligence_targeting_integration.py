"""The targeting endpoints and the confirm path, against a real database.

Two things only a database can prove: that confirming really does create an
immutable new version and retire the old one through the existing M3
machinery, and that generating drafts changes nothing while nobody confirms.
Everything deterministic is already covered in
``tests/unit/test_intelligence_targeting.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app
from tests.unit.test_intelligence_targeting import SUPPLEMENT_DRAFT

from arie.api.main import AppState, create_app, get_llm_service
from arie.auth import AuthContext
from arie.icp_profiles import get_active_profile, list_profiles
from arie.intelligence.targeting import GENERATION_SOURCE_AI
from arie.llm.fake_provider import FakeLLMProvider
from arie.llm.provider import LLMProvider
from arie.llm.service import LLMService
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

DRAFT_URL = "/intelligence/targeting/draft"
CONFIRM_URL = "/intelligence/targeting/confirm"
VOCAB_URL = "/intelligence/targeting/vocabularies"

QUESTIONS = {
    "what_you_sell": "We wholesale sports supplements to gyms and retailers.",
    "who_you_want": (
        "Multi-location gyms, supplement stores and distributors. Owners, "
        "founders and purchasing managers are best. Solo personal trainers "
        "are usually too small."
    ),
    "objective": "best_prospects",
}


@pytest.fixture
def restore_active_profile(db_conn: psycopg.Connection) -> Iterator[None]:
    """Undo whatever profile versions a test created for the legacy organization.

    `organization_icp_profiles` rows are permanent audit history in production
    and there is deliberately no delete path in the application — so teardown
    reaches around it with raw SQL, deleting only the versions this test made
    and re-activating whichever version was active before. Without this, every
    run leaves the shared organization on a different active profile and the
    next run's scoring assertions drift.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT profile_id FROM organization_icp_profiles "
            "WHERE organization_id = %s AND status = 'active'",
            (LEGACY_ORGANIZATION_ID,),
        )
        previously_active = cur.fetchone()
        cur.execute(
            "SELECT COALESCE(MAX(version), 0) FROM organization_icp_profiles "
            "WHERE organization_id = %s",
            (LEGACY_ORGANIZATION_ID,),
        )
        high_water_row = cur.fetchone()
    assert high_water_row is not None
    high_water = high_water_row[0]

    yield

    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM organization_icp_profiles WHERE organization_id = %s AND version > %s",
            (LEGACY_ORGANIZATION_ID, high_water),
        )
        if previously_active is not None:
            cur.execute(
                "UPDATE organization_icp_profiles SET status = 'active', retired_at = NULL "
                "WHERE profile_id = %s",
                (previously_active[0],),
            )
    db_conn.commit()


def _app_with_provider(
    app_state: AppState, provider: LLMProvider, auth: AuthContext | None = None
) -> tuple[FastAPI, AuthContext]:
    """An app whose targeting routes reach `provider` instead of a real one.

    Overrides `get_llm_service` rather than setting `LLM_PROVIDER=fake` in the
    environment: the substitution stays visible in the test, scoped to this app
    instance, and cannot leak into another test that expected real
    configuration. The budget check still runs against the real database — only
    the model is fake.
    """
    app = create_app(state=app_state)
    context = authorize_app(app, auth)
    app.dependency_overrides[get_llm_service] = lambda: LLMService(
        app_state.pool, provider=provider
    )
    return app, context


@pytest.fixture
def fake_provider() -> FakeLLMProvider:
    return FakeLLMProvider(responses=[json.dumps(SUPPLEMENT_DRAFT)] * 4)


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _confirm_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": f"AI targeting {uuid.uuid4().hex[:6]}",
        "objective": "best_prospects",
        "profile": SUPPLEMENT_DRAFT,
        "llm_provider": "fake",
        "llm_model": "fake-llm",
    }
    body.update(overrides)
    return body


# ------------------------------------------------------------ vocabularies --


def test_vocabularies_are_readable_by_any_member(api_client: TestClient) -> None:
    response = api_client.get(VOCAB_URL)
    assert response.status_code == 200
    body = response.json()
    assert "retail" in body["industries"]
    assert body["scoring_dimensions"] == [
        "employee_count",
        "industry",
        "title_seniority",
        "title_function",
        "buying_intent",
        "recent_trigger_event",
    ]


def test_vocabularies_require_authentication(app_state: AppState) -> None:
    app = create_app(state=app_state)
    with _client(app) as client:
        assert client.get(VOCAB_URL).status_code in (401, 403)


# ------------------------------------------------------------------ draft --


def test_drafting_returns_a_preview_and_changes_no_active_profile(
    app_state: AppState, db_conn: psycopg.Connection, fake_provider: FakeLLMProvider
) -> None:
    before = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    app, _ = _app_with_provider(app_state, fake_provider)
    with _client(app) as client:
        response = client.post(DRAFT_URL, json=QUESTIONS)

    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["offering_summary"]
    assert sum(row["points"] for row in body["allocation"]) == 100.0
    assert body["scoring_config"]["qualify_threshold"] == 65.0
    assert body["llm_provider"] == "fake"

    after = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert (after.profile_id if after else None) == (before.profile_id if before else None)


def test_drafting_rejects_an_empty_business_description(api_client: TestClient) -> None:
    response = api_client.post(
        DRAFT_URL, json={**QUESTIONS, "what_you_sell": "", "who_you_want": ""}
    )
    assert response.status_code == 422


def test_drafting_rejects_an_unknown_objective(api_client: TestClient) -> None:
    response = api_client.post(DRAFT_URL, json={**QUESTIONS, "objective": "make_me_rich"})
    assert response.status_code == 422


def test_an_unavailable_model_never_produces_a_five_hundred(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    """Whatever goes wrong upstream, the customer gets a stated reason."""
    app, _ = _app_with_provider(app_state, FakeLLMProvider(responses=["not json", "still not"]))
    with _client(app) as client:
        response = client.post(DRAFT_URL, json=QUESTIONS)
    assert response.status_code == 502  # the budget said yes; the model did not deliver
    detail = response.json().get("detail", "")
    assert "temporarily unavailable" in str(detail)
    assert "sk-" not in str(detail)
    assert "targeting interpreter" not in str(detail)  # no prompt leak


# ---------------------------------------------------------------- confirm --


def test_confirming_creates_a_new_immutable_active_version(
    api_client: TestClient, db_conn: psycopg.Connection, restore_active_profile: None
) -> None:
    before = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert before is not None

    response = api_client.post(CONFIRM_URL, json=_confirm_body())
    assert response.status_code == 201
    created = response.json()

    assert created["version"] == before.version + 1
    assert created["status"] == "active"
    assert created["organization_id"] == str(LEGACY_ORGANIZATION_ID)

    active = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert active is not None and str(active.profile_id) == created["profile_id"]

    # The previous version is retired, not altered: its config is byte-identical.
    versions = {
        p.version: p for p in list_profiles(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    }
    retired = versions[before.version]
    assert retired.status == "retired"
    assert retired.config == before.config


def test_a_confirmed_profile_records_how_it_was_generated(
    api_client: TestClient, restore_active_profile: None
) -> None:
    response = api_client.post(CONFIRM_URL, json=_confirm_body())
    assert response.status_code == 201
    generation = response.json()["config"]["generation"]
    assert generation["source"] == GENERATION_SOURCE_AI
    assert generation["confirmed"] is True
    assert generation["llm_provider"] == "fake"
    assert generation["objective"] == "best_prospects"
    assert "api_key" not in json.dumps(response.json()["config"])


def test_the_confirmed_config_is_recomputed_not_taken_from_the_client(
    api_client: TestClient, restore_active_profile: None
) -> None:
    """A client that smuggles a scoring config gets it ignored — the request
    model has no field for one, so it is dropped before the handler sees it."""
    response = api_client.post(
        CONFIRM_URL,
        json={
            **_confirm_body(),
            "scoring_config": {"qualify_threshold": 0.0, "industry_points": {"retail": 100.0}},
            "config": {"qualify_threshold": 1.0},
        },
    )
    assert response.status_code == 201
    config = response.json()["config"]
    assert config["qualify_threshold"] == 65.0
    assert max(config["industry_points"].values()) < 100.0


def test_confirming_the_same_draft_twice_creates_two_versions_not_a_conflict(
    api_client: TestClient, db_conn: psycopg.Connection, restore_active_profile: None
) -> None:
    """Existing M3 semantics, unchanged: every confirm is a new version.

    Recorded as a test rather than assumed, because it is the behaviour a
    client retry produces and a reader should be able to see that it was a
    decision. The previous version is retired atomically either way, so a
    double-submit costs an extra history row and changes nothing else.
    """
    first = api_client.post(CONFIRM_URL, json=_confirm_body(name="Targeting A"))
    second = api_client.post(CONFIRM_URL, json=_confirm_body(name="Targeting A"))
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()["version"] == first.json()["version"] + 1

    active = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert active is not None and str(active.profile_id) == second.json()["profile_id"]


def test_an_invalid_edited_draft_is_rejected_before_anything_is_written(
    api_client: TestClient, db_conn: psycopg.Connection
) -> None:
    before = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    response = api_client.post(
        CONFIRM_URL,
        json=_confirm_body(
            profile={**SUPPLEMENT_DRAFT, "preferred_seniorities": ["chief_gym_officer"]}
        ),
    )
    assert response.status_code == 422
    after = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert (after.profile_id if after else None) == (before.profile_id if before else None)


# --------------------------------------------------------------- tenancy --


def test_another_organizations_confirm_does_not_touch_this_ones_profile(
    api_client_org_b: TestClient, db_conn: psycopg.Connection, other_org: uuid.UUID
) -> None:
    before = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    response = api_client_org_b.post(CONFIRM_URL, json=_confirm_body())
    assert response.status_code == 201
    assert response.json()["organization_id"] == str(other_org)

    after = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert (after.profile_id if after else None) == (before.profile_id if before else None)


def test_a_body_supplied_organization_id_is_ignored(
    api_client_org_b: TestClient, other_org: uuid.UUID
) -> None:
    response = api_client_org_b.post(
        CONFIRM_URL, json={**_confirm_body(), "organization_id": str(LEGACY_ORGANIZATION_ID)}
    )
    assert response.status_code == 201
    assert response.json()["organization_id"] == str(other_org)


@pytest.mark.parametrize("url", [DRAFT_URL, CONFIRM_URL])
def test_a_non_admin_member_cannot_change_targeting(app_state: AppState, url: str) -> None:
    """Same rule as `POST /organization/icp`: config writes are owner/admin only."""
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=LEGACY_ORGANIZATION_ID,
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="member",
        ),
    )
    with _client(app) as client:
        body = QUESTIONS if url == DRAFT_URL else _confirm_body()
        assert client.post(url, json=body).status_code == 403


@pytest.mark.parametrize("url", [DRAFT_URL, CONFIRM_URL])
def test_an_api_key_cannot_change_targeting(app_state: AppState, url: str) -> None:
    """A machine caller has no business describing an ideal customer in prose."""
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=LEGACY_ORGANIZATION_ID,
            auth_method="api_key",
            user_id=None,
            role="admin",
        ),
    )
    with _client(app) as client:
        body = QUESTIONS if url == DRAFT_URL else _confirm_body()
        assert client.post(url, json=body).status_code == 403


@pytest.mark.parametrize("url", [DRAFT_URL, CONFIRM_URL, VOCAB_URL])
def test_anonymous_callers_are_denied(app_state: AppState, url: str) -> None:
    app = create_app(state=app_state)
    with _client(app) as client:
        response = client.get(url) if url == VOCAB_URL else client.post(url, json=QUESTIONS)
        assert response.status_code in (401, 403)
