"""Slice 3's database-shaped behaviour: mapped uploads, proposals, acceptance.

What only a real database can prove — that a confirmed mapping produces real
leads through the unmodified batch pipeline, that a proposal is durable and
tenant-scoped, and that accepting one creates a new immutable profile version
while rejecting one changes nothing at all.
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
from arie.intelligence.proposals import ProposalStatus
from arie.llm.fake_provider import FakeLLMProvider
from arie.llm.provider import LLMProvider
from arie.llm.service import LLMService
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

PREVIEW_URL = "/batches/mapping-preview"
ANALYZE_URL = "/intelligence/outcomes/analyze"
PROPOSALS_URL = "/intelligence/proposals"
CONFIRM_URL = "/intelligence/targeting/confirm"

MESSY_CSV = (
    b"Business,Contact,Role,Team Size,Web,Email Address\n"
    b"Acme Gyms,Sarah Chen,Owner,45,acmegyms.com,sarah@acmegyms.example\n"
    b"Beta Supps,Li Wei,Purchasing Manager,120,betasupps.com,li@betasupps.example\n"
)

OBVIOUS_CSV = (
    b"Company Name,Work Email,Job Title\n"
    b"Acme Gyms,sarah@acmegyms.example\n"
    b""
    b"Beta Supps,li@betasupps.example,Manager\n"
)


def _outcomes_csv(
    mid_positive: int, mid_total: int, small_positive: int, small_total: int
) -> bytes:
    rows = [f"Mid{i},{'won' if i < mid_positive else 'lost'},120\n" for i in range(mid_total)] + [
        f"Small{i},{'won' if i < small_positive else 'lost'},20\n" for i in range(small_total)
    ]
    return ("company,outcome,employee_count\n" + "".join(rows)).encode()


@pytest.fixture
def restore_active_profile(db_conn: psycopg.Connection) -> Iterator[None]:
    """Undo any profile version a test created, and reinstate the previous one.

    `organization_icp_profiles` is permanent audit history with no application
    delete path, so teardown reaches around it with raw SQL — the same approach
    `test_intelligence_targeting_integration.py` takes and for the same reason.
    Proposals referencing those versions are removed first: the foreign key is
    ON DELETE CASCADE, but deleting explicitly keeps the intent visible.
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
        row = cur.fetchone()
    assert row is not None
    high_water = row[0]

    yield

    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM profile_revision_proposals WHERE organization_id = %s",
            (LEGACY_ORGANIZATION_ID,),
        )
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


@pytest.fixture
def cleanup_proposals(db_conn: psycopg.Connection) -> Iterator[list[uuid.UUID]]:
    ids: list[uuid.UUID] = []
    yield ids
    if ids:
        with db_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM profile_revision_proposals WHERE proposal_id = ANY(%s)", (ids,)
            )
        db_conn.commit()


def _app(
    app_state: AppState, provider: LLMProvider | None = None, auth: AuthContext | None = None
) -> FastAPI:
    app = create_app(state=app_state)
    authorize_app(app, auth)
    if provider is not None:
        app.dependency_overrides[get_llm_service] = lambda: LLMService(
            app_state.pool, provider=provider
        )
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _confirm_targeting(client: TestClient) -> dict[str, Any]:
    """Give this organization a profile with a stored draft to propose against."""
    response = client.post(
        CONFIRM_URL,
        json={
            "name": f"AI targeting {uuid.uuid4().hex[:6]}",
            "objective": "best_prospects",
            "profile": {
                **SUPPLEMENT_DRAFT,
                # Leave a band unpromoted so a proposal has something to say.
                "employee_band_preferences": {
                    "employees_1_10": "avoid",
                    "employees_11_50": "acceptable",
                    "employees_51_200": "acceptable",
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ------------------------------------------------------------ mapping --


def test_an_obvious_file_previews_free_and_needs_no_confirmation(
    api_client: TestClient,
) -> None:
    response = api_client.post(
        PREVIEW_URL, files={"file": ("leads.csv", b"Company Name,Work Email\nAcme,a@b.example\n")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mapping_method"] == "deterministic"
    assert body["requires_confirmation"] is False
    assert body["usable"] is True
    assert body["field_map"] == {"company_name": "Company Name", "email": "Work Email"}
    assert body["llm_cost_usd"] == "0"


def test_a_messy_file_previews_with_labels_a_customer_can_read(
    api_client: TestClient,
) -> None:
    response = api_client.post(PREVIEW_URL, files={"file": ("leads.csv", MESSY_CSV)})
    assert response.status_code == 200
    body = response.json()
    columns = {c["source_column"]: c for c in body["columns"]}
    assert columns["Business"]["label"] == "Company"
    assert columns["Web"]["label"] == "Company website"
    assert columns["Team Size"]["canonical_field"] is None
    assert body["ignored_columns"] == ["Team Size"]
    # The correction dropdown gets human labels, never identifiers alone.
    assert {f["name"] for f in body["available_fields"]} == {
        "email",
        "full_name",
        "first_name",
        "last_name",
        "company_name",
        "company_domain",
        "title",
    }


def test_a_confirmed_mapping_ingests_through_the_ordinary_pipeline(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: Any
) -> None:
    """The whole point of Part A: one dictionary, then the unmodified pipeline."""
    for email in ("sarah@acmegyms.example", "li@betasupps.example"):
        cleanup_ingest.emails.append(email)
    for domain in ("acmegyms.example", "betasupps.example"):
        cleanup_ingest.domains.append(domain)

    mapping = {
        "company_name": "Business",
        "full_name": "Contact",
        "title": "Role",
        "company_domain": "Web",
        "email": "Email Address",
    }
    response = api_client.post(
        "/batches",
        files={"file": ("leads.csv", MESSY_CSV)},
        data={"mapping": json.dumps(mapping)},
    )
    assert response.status_code == 201, response.text
    batch = response.json()
    cleanup_ingest.lead_ids.extend(_lead_ids_for_batch(db_conn, batch["batch_id"]))

    assert batch["accepted_rows"] == 2
    assert batch["rejected_rows"] == 0

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT c.name FROM leads l JOIN companies c ON c.company_id = l.company_id "
            "WHERE l.batch_id = %s ORDER BY c.name",
            (batch["batch_id"],),
        )
        names = [r[0] for r in cur.fetchall()]
    assert names == ["Acme Gyms", "Beta Supps"]


def _lead_ids_for_batch(conn: psycopg.Connection, batch_id: str) -> list[uuid.UUID]:
    with conn.cursor() as cur:
        cur.execute("SELECT lead_id FROM leads WHERE batch_id = %s", (batch_id,))
        return [row[0] for row in cur.fetchall()]


def test_the_same_file_without_a_mapping_loses_the_company_column(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: Any
) -> None:
    """Why the mapping step exists, demonstrated rather than asserted."""
    cleanup_ingest.emails.extend(["sarah@acmegyms.example", "li@betasupps.example"])
    response = api_client.post("/batches", files={"file": ("leads.csv", MESSY_CSV)})
    assert response.status_code == 201
    batch = response.json()
    cleanup_ingest.lead_ids.extend(_lead_ids_for_batch(db_conn, batch["batch_id"]))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM leads WHERE batch_id = %s AND company_id IS NOT NULL",
            (batch["batch_id"],),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 0


@pytest.mark.parametrize(
    "mapping",
    [
        "not json",
        json.dumps(["a", "b"]),
        json.dumps({"employee_count": "Team Size", "email": "Email Address"}),
        json.dumps({"email": "Nope"}),
        json.dumps({"company_name": "Business"}),  # no email
    ],
)
def test_a_broken_mapping_is_refused_rather_than_quietly_ignored(
    api_client: TestClient, mapping: str
) -> None:
    response = api_client.post(
        "/batches", files={"file": ("leads.csv", MESSY_CSV)}, data={"mapping": mapping}
    )
    assert response.status_code == 422


def test_mapping_preview_requires_authentication(app_state: AppState) -> None:
    app = create_app(state=app_state)
    with _client(app) as client:
        assert client.post(
            PREVIEW_URL, files={"file": ("a.csv", b"Email\na@b.example\n")}
        ).status_code in (401, 403)


# --------------------------------------------------------- outcomes --


def test_analysing_outcomes_returns_statistics_and_stores_one_proposal(
    app_state: AppState,
    db_conn: psycopg.Connection,
    restore_active_profile: None,
) -> None:
    provider = FakeLLMProvider(
        responses=[
            json.dumps(
                {
                    "summary": "Mid-sized companies did better in this data.",
                    "observations": ["51-200 people converted more often."],
                    "caveats": ["26 examples is not many."],
                    "suggested_changes": [],
                }
            )
        ]
    )
    app = _app(app_state, provider)
    with _client(app) as client:
        _confirm_targeting(client)
        response = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(16, 26, 5, 24))}
        )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["labelled_rows"] == 50
    assert body["positive_count"] == 21
    assert body["baseline_rate"] == pytest.approx(0.42)
    mid = next(g for g in body["groups"] if g["group_key"] == "employees_51_200")
    assert mid["sample_size"] == 26
    assert mid["signal"] == "moderate"
    assert "in this dataset" in mid["sentence"].lower()
    assert body["proposal_id"] is not None
    assert body["interpretation"] == "Mid-sized companies did better in this data."

    # Analysing changed no scoring: the active profile is untouched.
    active = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert active is not None
    assert active.config["employee_count_bands"]


def test_a_thin_dataset_returns_statistics_and_no_proposal(
    app_state: AppState, restore_active_profile: None
) -> None:
    app = _app(app_state, FakeLLMProvider(responses=["never called"]))
    with _client(app) as client:
        _confirm_targeting(client)
        response = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(2, 3, 1, 3))}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["proposal_id"] is None
    assert body["warnings"]


def test_a_file_with_no_outcome_column_is_refused(api_client: TestClient) -> None:
    response = api_client.post(
        ANALYZE_URL, files={"file": ("history.csv", b"company,revenue\nAcme,100\n")}
    )
    assert response.status_code == 422
    assert "outcome" in response.json()["detail"]


def test_a_model_outage_still_returns_the_statistics(
    app_state: AppState, restore_active_profile: None
) -> None:
    app = _app(app_state, FakeLLMProvider(responses=["not json", "still not json"]))
    with _client(app) as client:
        _confirm_targeting(client)
        response = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(16, 26, 5, 24))}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"] is None
    assert body["labelled_rows"] == 50  # the numbers were never the model's
    assert body["proposal_id"] is not None  # and the proposal stands without prose


# -------------------------------------------------------- proposals --


def test_a_proposal_is_durable_readable_and_still_only_a_suggestion(
    app_state: AppState, db_conn: psycopg.Connection, restore_active_profile: None
) -> None:
    app = _app(app_state, FakeLLMProvider(responses=["{}", "{}"]))
    with _client(app) as client:
        confirmed = _confirm_targeting(client)
        analysis = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(16, 26, 5, 24))}
        ).json()
        proposal_id = analysis["proposal_id"]
        assert proposal_id

        listed = client.get(PROPOSALS_URL).json()
        assert any(p["proposal_id"] == proposal_id for p in listed)

        detail = client.get(f"{PROPOSALS_URL}/{proposal_id}").json()

    assert detail["status"] == ProposalStatus.PROPOSED
    assert detail["profile_version"] == confirmed["version"]
    assert detail["evidence_strength"] == "moderate"
    assert detail["sample_size"] == 26
    assert detail["changes"]
    assert detail["resolved_at"] is None
    assert detail["resulting_profile_id"] is None
    assert detail["supporting_statistics"]["labelled_rows"] == 50

    # Still the same active version: a proposal changes nothing.
    active = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert active is not None and str(active.profile_id) == confirmed["profile_id"]


def test_rejecting_a_proposal_changes_no_targeting(
    app_state: AppState, db_conn: psycopg.Connection, restore_active_profile: None
) -> None:
    app = _app(app_state, FakeLLMProvider(responses=["{}", "{}"]))
    with _client(app) as client:
        confirmed = _confirm_targeting(client)
        proposal_id = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(16, 26, 5, 24))}
        ).json()["proposal_id"]

        rejected = client.post(f"{PROPOSALS_URL}/{proposal_id}/reject")
        assert rejected.status_code == 200
        assert rejected.json()["status"] == ProposalStatus.REJECTED
        assert rejected.json()["resulting_profile_id"] is None

        # A second rejection finds nothing open.
        assert client.post(f"{PROPOSALS_URL}/{proposal_id}/reject").status_code == 404

    active = get_active_profile(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert active is not None and str(active.profile_id) == confirmed["profile_id"]


def test_accepting_a_proposal_creates_a_new_immutable_version(
    app_state: AppState, db_conn: psycopg.Connection, restore_active_profile: None
) -> None:
    app = _app(app_state, FakeLLMProvider(responses=["{}", "{}"]))
    with _client(app) as client:
        confirmed = _confirm_targeting(client)
        proposal_id = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(16, 26, 5, 24))}
        ).json()["proposal_id"]

        accepted = client.post(
            f"{PROPOSALS_URL}/{proposal_id}/accept", json={"name": "Updated from past results"}
        )
        assert accepted.status_code == 201, accepted.text
        created = accepted.json()

        resolved = client.get(f"{PROPOSALS_URL}/{proposal_id}").json()

    assert created["version"] == confirmed["version"] + 1
    assert created["status"] == "active"
    assert resolved["status"] == ProposalStatus.ACCEPTED
    assert resolved["resulting_profile_id"] == created["profile_id"]

    # The change actually landed: 51-200 is now the top size band.
    bands = {
        int(b["min_employees"]): b["points"] for b in created["config"]["employee_count_bands"]
    }
    assert bands[51] == max(bands.values())

    # The previous version is retired and byte-identical to what it was.
    versions = {
        p.version: p for p in list_profiles(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    }
    previous = versions[confirmed["version"]]
    assert previous.status == "retired"
    assert previous.config == confirmed["config"]


def test_accepting_twice_is_refused_rather_than_creating_two_versions(
    app_state: AppState, restore_active_profile: None
) -> None:
    app = _app(app_state, FakeLLMProvider(responses=["{}", "{}"]))
    with _client(app) as client:
        _confirm_targeting(client)
        proposal_id = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(16, 26, 5, 24))}
        ).json()["proposal_id"]

        first = client.post(f"{PROPOSALS_URL}/{proposal_id}/accept", json={"name": "A"})
        second = client.post(f"{PROPOSALS_URL}/{proposal_id}/accept", json={"name": "A"})
    assert first.status_code == 201
    assert second.status_code == 409
    assert "already been dealt with" in second.json()["detail"]


def test_a_proposal_made_stale_by_a_newer_profile_is_refused(
    app_state: AppState, restore_active_profile: None
) -> None:
    """A week-old suggestion must not be folded into targeting that has moved on."""
    app = _app(app_state, FakeLLMProvider(responses=["{}", "{}"]))
    with _client(app) as client:
        _confirm_targeting(client)
        proposal_id = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(16, 26, 5, 24))}
        ).json()["proposal_id"]

        _confirm_targeting(client)  # the customer changes their targeting

        response = client.post(f"{PROPOSALS_URL}/{proposal_id}/accept", json={"name": "A"})
    assert response.status_code == 409
    assert "no longer applies" in response.json()["detail"]


# ---------------------------------------------------------- tenancy --


def test_another_organization_cannot_see_or_resolve_this_ones_proposals(
    app_state: AppState,
    other_org: uuid.UUID,
    restore_active_profile: None,
) -> None:
    app = _app(app_state, FakeLLMProvider(responses=["{}", "{}"]))
    with _client(app) as client:
        _confirm_targeting(client)
        proposal_id = client.post(
            ANALYZE_URL, files={"file": ("history.csv", _outcomes_csv(16, 26, 5, 24))}
        ).json()["proposal_id"]

    other = _app(
        app_state,
        auth=AuthContext(
            organization_id=other_org, auth_method="jwt", user_id=uuid.uuid4(), role="owner"
        ),
    )
    with _client(other) as client:
        assert client.get(PROPOSALS_URL).json() == []
        assert client.get(f"{PROPOSALS_URL}/{proposal_id}").status_code == 404
        assert client.post(f"{PROPOSALS_URL}/{proposal_id}/reject").status_code == 404
        assert (
            client.post(f"{PROPOSALS_URL}/{proposal_id}/accept", json={"name": "A"}).status_code
            == 409
        )


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("post", ANALYZE_URL),
        ("post", f"{PROPOSALS_URL}/00000000-0000-0000-0000-000000000001/reject"),
        ("post", f"{PROPOSALS_URL}/00000000-0000-0000-0000-000000000001/accept"),
    ],
)
def test_a_non_admin_member_cannot_change_or_analyse_targeting(
    app_state: AppState, method: str, url: str
) -> None:
    app = _app(
        app_state,
        auth=AuthContext(
            organization_id=LEGACY_ORGANIZATION_ID,
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="member",
        ),
    )
    with _client(app) as client:
        response = client.post(
            url,
            files={"file": ("h.csv", b"company,outcome\nA,won\n")} if url == ANALYZE_URL else None,
            json=None if url == ANALYZE_URL else {"name": "A"},
        )
    assert response.status_code == 403


def test_a_member_may_read_proposals_without_being_able_to_act_on_them(
    app_state: AppState,
) -> None:
    app = _app(
        app_state,
        auth=AuthContext(
            organization_id=LEGACY_ORGANIZATION_ID,
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="member",
        ),
    )
    with _client(app) as client:
        assert client.get(PROPOSALS_URL).status_code == 200


def test_an_api_key_cannot_reach_the_proposal_endpoints(app_state: AppState) -> None:
    app = _app(
        app_state,
        auth=AuthContext(
            organization_id=LEGACY_ORGANIZATION_ID,
            auth_method="api_key",
            user_id=None,
            role="admin",
        ),
    )
    with _client(app) as client:
        assert (
            client.post(
                ANALYZE_URL, files={"file": ("h.csv", b"company,outcome\nA,won\n")}
            ).status_code
            == 403
        )


def test_proposals_are_row_level_secured(migrated_database: str) -> None:
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(migrated_database, min_size=1, max_size=2, open=True)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT relrowsecurity FROM pg_class WHERE relname = 'profile_revision_proposals'"
            )
            row = cur.fetchone()
        assert row == (True,)
    finally:
        pool.close()
