"""Productization M3 — organization ICP/scoring profiles.

`api_client` authenticates as an owner of `LEGACY_ORGANIZATION_ID`, which
migration 0019 bootstraps a version-1 "Reference ICP" profile for — every
test that reads without creating anything exercises that real bootstrapped
row, never a fixture invented for this suite.

Tests that *create* a new profile version use `other_org`/`api_client_org_b`
exclusively, never `LEGACY_ORGANIZATION_ID` — activating a new version there
would permanently change what every other integration test file's leads
score against for the rest of this database's life (profiles are immutable
and versions are never un-activated), which would be exactly the kind of
cross-test pollution `tests/integration/conftest.py`'s `RUN_ID`-tagging
discipline exists to prevent.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, authorize_app, source_for
from tests.integration.test_rls_membership_recursion import TwoOrgFixture, _as
from tests.integration.test_rls_membership_recursion import rls_test_roles as rls_test_roles
from tests.integration.test_rls_membership_recursion import two_orgs as two_orgs

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.evalgen.schema import EvalLead
from arie.icp_profiles import REFERENCE_CONFIG
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobHandler, run_worker_cycle
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

_MAX_CYCLES = 6
# Escalates to AWAITING_HUMAN under the reference thresholds (65/55) — the
# same seed-42 corpus identity `test_receipt_integration.py` uses for the
# same reason. Used here only under a disposable organization's own custom
# profile, never under LEGACY_ORGANIZATION_ID.
ESCALATING_EMAIL = "nadia.haddad@cobalt500.com"
ESCALATING_DOMAIN = "cobalt500.com"


def _create_profile(
    client: TestClient, *, name: str = "test profile", config: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = client.post(
        "/organization/icp",
        json={"name": name, "config": config if config is not None else REFERENCE_CONFIG},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# ------------------------------------------------------------------ reads --


def test_legacy_organization_has_a_bootstrapped_v1_reference_profile(
    api_client: TestClient,
) -> None:
    response = api_client.get("/organization/icp")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["name"] == "Reference ICP"
    assert body["status"] == "active"
    assert body["config"] == REFERENCE_CONFIG


def test_an_organization_with_no_profile_at_all_gets_404(
    api_client_org_b: TestClient,
) -> None:
    """`other_org` is created fresh, after migration 0019 already ran, so it
    was never bootstrapped — the honest "no active profile yet" state."""
    response = api_client_org_b.get("/organization/icp")
    assert response.status_code == 404


def test_versions_listing_is_newest_first(api_client_org_b: TestClient) -> None:
    first = _create_profile(api_client_org_b, name="v1")
    second = _create_profile(api_client_org_b, name="v2")

    response = api_client_org_b.get("/organization/icp/versions")
    assert response.status_code == 200
    versions = [item["version"] for item in response.json()]
    assert versions[:2] == [second["version"], first["version"]]


def test_get_by_version_returns_a_specific_historical_version(
    api_client_org_b: TestClient,
) -> None:
    created = _create_profile(api_client_org_b, name="pin me")

    response = api_client_org_b.get(f"/organization/icp/{created['version']}")

    assert response.status_code == 200
    assert response.json()["profile_id"] == created["profile_id"]


def test_get_by_unknown_version_is_404(api_client_org_b: TestClient) -> None:
    response = api_client_org_b.get("/organization/icp/999999")
    assert response.status_code == 404


# ------------------------------------------------------- versioning/activation --


def test_creating_a_new_version_retires_the_previous_active_one(
    api_client_org_b: TestClient,
) -> None:
    first = _create_profile(api_client_org_b, name="first")
    second = _create_profile(api_client_org_b, name="second")

    assert second["version"] == first["version"] + 1
    assert second["status"] == "active"

    retired = api_client_org_b.get(f"/organization/icp/{first['version']}").json()
    assert retired["status"] == "retired"
    assert retired["retired_at"] is not None

    active = api_client_org_b.get("/organization/icp").json()
    assert active["profile_id"] == second["profile_id"]


def test_a_retired_versions_config_is_never_mutated_by_a_later_version(
    api_client_org_b: TestClient,
) -> None:
    """Historical immutability/reproducibility — the whole point of
    versioning. A later, very different config must not be visible through
    an earlier version's own row."""
    original_config = copy.deepcopy(REFERENCE_CONFIG)
    first = _create_profile(api_client_org_b, name="original", config=original_config)

    changed_config = copy.deepcopy(REFERENCE_CONFIG)
    changed_config["qualify_threshold"] = 10.0
    changed_config["reject_threshold"] = 5.0
    _create_profile(api_client_org_b, name="very different", config=changed_config)

    unchanged = api_client_org_b.get(f"/organization/icp/{first['version']}").json()
    assert unchanged["config"]["qualify_threshold"] == 65.0
    assert unchanged["config"] == original_config


def test_invalid_config_is_rejected_and_creates_no_row(api_client_org_b: TestClient) -> None:
    bad_config = copy.deepcopy(REFERENCE_CONFIG)
    bad_config["trigger_event_weight"] = 999.0  # breaks the sum-to-100 invariant

    before = api_client_org_b.get("/organization/icp/versions").json()
    response = api_client_org_b.post(
        "/organization/icp", json={"name": "bad", "config": bad_config}
    )
    after = api_client_org_b.get("/organization/icp/versions").json()

    assert response.status_code == 422
    assert len(after) == len(before)  # no new row was created


def test_reject_threshold_not_strictly_less_than_qualify_is_rejected(
    api_client_org_b: TestClient,
) -> None:
    bad_config = copy.deepcopy(REFERENCE_CONFIG)
    bad_config["qualify_threshold"] = 50.0
    bad_config["reject_threshold"] = 50.0

    response = api_client_org_b.post(
        "/organization/icp", json={"name": "bad thresholds", "config": bad_config}
    )
    assert response.status_code == 422


# --------------------------------------------------------------- authorization --


def test_reading_config_requires_no_particular_role(app_state: AppState) -> None:
    """Any active member — including analyst_reviewer, not just owner/admin —
    may read the active profile. Only *writing* is owner/admin-gated."""
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
        response = client.get("/organization/icp")

    assert response.status_code == 200


def test_creating_a_profile_requires_owner_or_admin(app_state: AppState) -> None:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=uuid.uuid4(),
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="analyst_reviewer",
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/organization/icp", json={"name": "should fail", "config": REFERENCE_CONFIG}
        )

    assert response.status_code == 403


def test_an_api_key_cannot_read_or_write_icp_configuration(
    api_client: TestClient, app_state: AppState, cleanup_api_keys: list[UUID]
) -> None:
    """Unlike the tests above, this must go through the *real*, non-overridden
    `get_auth_context` (`arie.apikeys.looks_like_api_key`'s prefix check) —
    the same `raw_client` shape `test_api_keys_integration.py` uses — since
    `api_client`'s own dependency override would otherwise make every request
    succeed regardless of the header."""
    created = api_client.post(
        "/api-keys", json={"label": "icp-probe", "scopes": ["leads:write", "leads:read"]}
    )
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw_client:
        read = raw_client.get("/organization/icp", headers=headers)
        write = raw_client.post(
            "/organization/icp", json={"name": "x", "config": REFERENCE_CONFIG}, headers=headers
        )

    assert read.status_code == 403
    assert write.status_code == 403


# -------------------------------------------------------------- tenant isolation --


def test_a_different_organizations_active_profile_is_invisible(
    api_client: TestClient, api_client_org_b: TestClient
) -> None:
    _create_profile(api_client_org_b, name="org b's own profile")

    # Organization A (LEGACY_ORGANIZATION_ID) still sees its own bootstrapped
    # profile, never org B's.
    org_a_active = api_client.get("/organization/icp").json()
    assert org_a_active["organization_id"] == str(LEGACY_ORGANIZATION_ID)
    assert org_a_active["name"] == "Reference ICP"


def test_cannot_fetch_another_organizations_profile_by_version_number(
    api_client: TestClient, api_client_org_b: TestClient
) -> None:
    """Organization A's version 1 exists; asking for "version 1" while
    authenticated as organization B must not leak it — versions are
    per-organization, not a global sequence."""
    response = api_client_org_b.get("/organization/icp/1")
    assert response.status_code == 404


# ------------------------------------------------------------------------ RLS --


def test_rls_own_organization_can_select_its_profile(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_icp_profiles "
            "(organization_id, version, name, config, scorer_version, status) "
            "VALUES (%s, 1, 'RLS test', %s, 'icp-1.0.0', 'active') RETURNING profile_id",
            (two_orgs.org_a, Jsonb(REFERENCE_CONFIG)),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    profile_id = row[0]

    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "SELECT profile_id FROM organization_icp_profiles WHERE organization_id = %s",
            (two_orgs.org_a,),
        )
        rows = cur.fetchall()
    assert rows == [(profile_id,)]


def test_rls_foreign_organizations_profile_is_invisible(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_icp_profiles "
            "(organization_id, version, name, config, scorer_version, status) "
            "VALUES (%s, 1, 'RLS test B', %s, 'icp-1.0.0', 'active')",
            (two_orgs.org_b, Jsonb(REFERENCE_CONFIG)),
        )
    db_conn.commit()

    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)  # only a member of org_a
        cur.execute(
            "SELECT profile_id FROM organization_icp_profiles WHERE organization_id = %s",
            (two_orgs.org_b,),
        )
        rows = cur.fetchall()
    assert rows == []


def test_rls_anon_sees_no_profiles_at_all(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, None)
        cur.execute("SELECT profile_id FROM organization_icp_profiles")
        rows = cur.fetchall()
    assert rows == []


def test_rls_non_admin_member_cannot_insert_a_profile(
    migrated_database_direct: str, two_orgs: TwoOrgFixture, db_conn: psycopg.Connection
) -> None:
    """`organization_icp_profiles_admin_insert`'s own policy — a member with
    no owner/admin role (the fixture only ever creates owners, so this
    downgrades one to prove the role check, not just the org check)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_members SET role = 'analyst_reviewer' "
            "WHERE organization_id = %s AND user_id = %s",
            (two_orgs.org_a, two_orgs.user_a),
        )
    db_conn.commit()

    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "INSERT INTO organization_icp_profiles "
            "(organization_id, version, name, config, scorer_version, status) "
            "VALUES (%s, 99, 'should fail', %s, 'icp-1.0.0', 'active')",
            (two_orgs.org_a, Jsonb(REFERENCE_CONFIG)),
        )

    # Restore the fixture's own invariant for any other test sharing db_conn.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_members SET role = 'owner' "
            "WHERE organization_id = %s AND user_id = %s",
            (two_orgs.org_a, two_orgs.user_a),
        )
    db_conn.commit()


# --------------------------------------------------------- scoring integration --


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def icp_scoring_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=6, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def icp_handlers(
    runtime: SimulatedEnrichmentRuntime, icp_scoring_pool: ConnectionPool
) -> dict[str, JobHandler]:
    return build_handlers(icp_scoring_pool, runtime=runtime, provider_mode="simulated")


def _drive_to_completion(
    job_queue: PostgresJobQueue,
    pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    job_id: str,
) -> str:
    for _ in range(_MAX_CYCLES):
        run_worker_cycle(
            job_queue,
            pool,
            handlers,
            worker_id=f"icp-it-{uuid.uuid4().hex[:8]}",
            batch_size=3,
            job_types=["compute_score"],
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        assert row is not None
        if row[0] not in ("pending", "processing"):
            return str(row[0])
    return "pending"


def test_an_organizations_custom_thresholds_actually_change_the_decision(
    api_client_org_b: TestClient,
    other_org: UUID,
    job_queue: PostgresJobQueue,
    icp_scoring_pool: ConnectionPool,
    icp_handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    leads: list[EvalLead],
) -> None:
    """`nadia.haddad@cobalt500.com` escalates under the reference thresholds
    (see `test_receipt_integration.py`). Give `other_org` a profile whose
    qualify_threshold is far below her real score, and prove: (1) the
    decision actually flips to auto_route, and (2) the receipt names the
    exact profile/version that produced it — not just that *some* config
    changed the number.
    """
    aggressive_config = copy.deepcopy(REFERENCE_CONFIG)
    aggressive_config["qualify_threshold"] = 1.0
    aggressive_config["reject_threshold"] = 0.5
    profile = _create_profile(
        api_client_org_b, name="aggressive auto-route", config=aggressive_config
    )

    cleanup_ingest.domains.append(ESCALATING_DOMAIN)
    cleanup_ingest.emails.append(ESCALATING_EMAIL)
    corpus_lead = next(lead for lead in leads if lead.person.email == ESCALATING_EMAIL)
    ingest_response = api_client_org_b.post(
        "/leads",
        json={
            "source": source_for("icp-profile"),
            "email": ESCALATING_EMAIL,
            "external_ref": f"icp-{uuid.uuid4().hex[:12]}",
            "company_domain": ESCALATING_DOMAIN,
            "company_name": corpus_lead.company.legal_name,
            "full_name": corpus_lead.person.full_name,
        },
    )
    assert ingest_response.status_code == 201
    lead_body = ingest_response.json()
    cleanup_ingest.lead_ids.append(uuid.UUID(lead_body["lead_id"]))

    final_status = _drive_to_completion(
        job_queue, icp_scoring_pool, icp_handlers, db_conn, lead_body["job_id"]
    )
    assert final_status == "done"

    receipt = api_client_org_b.get(f"/leads/{lead_body['lead_id']}/receipt").json()
    assert receipt["decision"]["recommended_action"] == "auto_route"
    assert receipt["score"]["threshold_qualify"] == 1.0
    assert receipt["score"]["threshold_reject"] == 0.5
    assert receipt["versions"]["icp_profile_id"] == profile["profile_id"]
    assert receipt["versions"]["icp_profile_version"] == profile["version"]


def test_a_legacy_organization_lead_records_its_bootstrapped_profile(
    api_client: TestClient,
    job_queue: PostgresJobQueue,
    icp_scoring_pool: ConnectionPool,
    icp_handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    leads: list[EvalLead],
) -> None:
    """A lead scored under `LEGACY_ORGANIZATION_ID` (whose active profile is
    the byte-identical, migration-bootstrapped "Reference ICP" v1) still
    gets real, non-null `icp_profile_id`/`icp_profile_version` provenance —
    proving the integration point fires even when the config happens to
    equal the pre-M3 defaults, not only when it visibly differs."""
    autonomous_email = "nadia.delacroix@lumen500.com"
    autonomous_domain = "lumen500.com"
    corpus_lead = next(lead for lead in leads if lead.person.email == autonomous_email)

    cleanup_ingest.domains.append(autonomous_domain)
    cleanup_ingest.emails.append(autonomous_email)
    ingest_response = api_client.post(
        "/leads",
        json={
            "source": source_for("icp-profile-legacy"),
            "email": autonomous_email,
            "external_ref": f"icp-legacy-{uuid.uuid4().hex[:12]}",
            "company_domain": autonomous_domain,
            "company_name": corpus_lead.company.legal_name,
            "full_name": corpus_lead.person.full_name,
        },
    )
    assert ingest_response.status_code == 201
    lead_body = ingest_response.json()
    cleanup_ingest.lead_ids.append(uuid.UUID(lead_body["lead_id"]))

    final_status = _drive_to_completion(
        job_queue, icp_scoring_pool, icp_handlers, db_conn, lead_body["job_id"]
    )
    assert final_status == "done"

    active_profile = api_client.get("/organization/icp").json()
    receipt = api_client.get(f"/leads/{lead_body['lead_id']}/receipt").json()
    assert receipt["versions"]["icp_profile_id"] == active_profile["profile_id"]
    assert receipt["versions"]["icp_profile_version"] == 1
    assert receipt["score"]["threshold_qualify"] == 65.0
    assert receipt["score"]["threshold_reject"] == 55.0
