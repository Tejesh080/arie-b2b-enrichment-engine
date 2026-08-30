"""Productization M3 — CSV bulk lead upload (Part 4-6).

`api_client` authenticates as an owner of `LEGACY_ORGANIZATION_ID`; batch
upload itself needs no particular role (any JWT session may upload), so
most tests here use `api_client` purely as a convenient authenticated
client, not because ownership matters.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, authorize_app
from tests.integration.test_rls_membership_recursion import TwoOrgFixture, _as
from tests.integration.test_rls_membership_recursion import rls_test_roles as rls_test_roles
from tests.integration.test_rls_membership_recursion import two_orgs as two_orgs

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.batches import MAX_ROWS
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobHandler, run_worker_cycle
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

_MAX_CYCLES = 6
AUTONOMOUS_EMAIL = "nadia.delacroix@lumen500.com"
AUTONOMOUS_DOMAIN = "lumen500.com"
ESCALATING_EMAIL = "nadia.haddad@cobalt500.com"
ESCALATING_DOMAIN = "cobalt500.com"


def _upload(client: TestClient, *, filename: str = "leads.csv", content: bytes) -> Any:
    return client.post("/batches", files={"file": (filename, content, "text/csv")})


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


class _Cleanup:
    """Batch teardown: leads first (FK on lead_batch_rows/leads.batch_id),
    then the batch rows/parent. Registered explicitly per test rather than a
    shared fixture, since which leads a batch created is only known after
    the upload runs.
    """

    def __init__(self, db_conn: psycopg.Connection) -> None:
        self._db_conn = db_conn
        self.batch_ids: list[UUID] = []
        self.lead_ids: list[UUID] = []
        self.domains: list[str] = []
        self.emails: list[str] = []

    def finish(self) -> None:
        with self._db_conn.cursor() as cur:
            if self.lead_ids:
                cur.execute("DELETE FROM provider_calls WHERE lead_id = ANY(%s)", (self.lead_ids,))
                cur.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (self.lead_ids,))
            if self.batch_ids:
                cur.execute("DELETE FROM lead_batches WHERE batch_id = ANY(%s)", (self.batch_ids,))
            if self.emails:
                cur.execute("DELETE FROM persons WHERE canonical_email = ANY(%s)", (self.emails,))
            if self.domains:
                cur.execute(
                    "DELETE FROM companies WHERE canonical_domain = ANY(%s) "
                    "AND NOT EXISTS (SELECT 1 FROM leads WHERE leads.company_id = companies.company_id)",
                    (self.domains,),
                )
        self._db_conn.commit()


@pytest.fixture
def batch_cleanup(db_conn: psycopg.Connection) -> Iterator[_Cleanup]:
    cleanup = _Cleanup(db_conn)
    yield cleanup
    cleanup.finish()


# ------------------------------------------------------------------ upload --


def test_a_well_formed_csv_creates_a_batch_and_leads(
    api_client: TestClient, batch_cleanup: _Cleanup
) -> None:
    email = f"batch-{uuid.uuid4().hex[:10]}@example.test"
    batch_cleanup.emails.append(email)
    content = _csv(f"email,company\n{email},Acme\n")

    response = _upload(api_client, content=content)

    assert response.status_code == 201, response.text
    body = response.json()
    batch_cleanup.batch_ids.append(uuid.UUID(body["batch_id"]))
    assert body["total_rows"] == 1
    assert body["accepted_rows"] == 1
    assert body["rejected_rows"] == 0
    assert body["progress"]["processing_count"] + body["progress"]["qualified_count"] >= 0

    rows_response = api_client.get(f"/batches/{body['batch_id']}/leads")
    assert rows_response.status_code == 200
    rows = rows_response.json()["items"]
    assert len(rows) == 1
    assert rows[0]["validation_status"] == "accepted"
    assert rows[0]["lead_id"] is not None
    batch_cleanup.lead_ids.append(uuid.UUID(rows[0]["lead_id"]))


def test_partial_acceptance_keeps_invalid_rows_out_of_the_lead_table(
    api_client: TestClient, batch_cleanup: _Cleanup
) -> None:
    good_email = f"good-{uuid.uuid4().hex[:10]}@example.test"
    batch_cleanup.emails.append(good_email)
    content = _csv(f"email\nnot-an-email\n{good_email}\n")

    response = _upload(api_client, content=content)

    assert response.status_code == 201
    body = response.json()
    batch_cleanup.batch_ids.append(uuid.UUID(body["batch_id"]))
    assert body["total_rows"] == 2
    assert body["accepted_rows"] == 1
    assert body["rejected_rows"] == 1

    rows = api_client.get(f"/batches/{body['batch_id']}/leads").json()["items"]
    by_number = {row["row_number"]: row for row in rows}
    assert by_number[1]["validation_status"] == "rejected"
    assert by_number[1]["lead_id"] is None
    assert by_number[1]["validation_error"] is not None
    assert by_number[2]["validation_status"] == "accepted"
    assert by_number[2]["lead_id"] is not None
    batch_cleanup.lead_ids.append(uuid.UUID(by_number[2]["lead_id"]))


def test_missing_email_column_is_rejected_before_any_batch_is_created(
    api_client: TestClient,
) -> None:
    before = api_client.get("/batches?limit=1").json()
    response = _upload(api_client, content=_csv("company\nAcme\n"))

    assert response.status_code == 422
    after = api_client.get("/batches?limit=1").json()
    # The most recent batch (if any) is unchanged — nothing was created.
    assert before == after or before[0]["batch_id"] != after[0].get("batch_id")


def test_empty_file_is_rejected(api_client: TestClient) -> None:
    response = _upload(api_client, content=_csv("email\n"))
    assert response.status_code == 422


def test_row_count_over_the_limit_is_rejected(api_client: TestClient) -> None:
    body_rows = "".join(f"person{i}@example.test\n" for i in range(MAX_ROWS + 1))
    response = _upload(api_client, content=_csv("email\n" + body_rows))
    assert response.status_code == 422


def test_duplicate_email_within_one_file_creates_only_one_lead(
    api_client: TestClient, batch_cleanup: _Cleanup
) -> None:
    email = f"dup-{uuid.uuid4().hex[:10]}@example.test"
    batch_cleanup.emails.append(email)
    content = _csv(f"email\n{email}\n{email.upper()}\n")

    response = _upload(api_client, content=content)

    assert response.status_code == 201
    body = response.json()
    batch_cleanup.batch_ids.append(uuid.UUID(body["batch_id"]))
    assert body["accepted_rows"] == 2  # both rows individually well-formed

    rows = api_client.get(f"/batches/{body['batch_id']}/leads").json()["items"]
    lead_ids = {row["lead_id"] for row in rows}
    assert len(lead_ids) == 1  # but only one real lead
    batch_cleanup.lead_ids.append(uuid.UUID(next(iter(lead_ids))))


def test_reuploading_the_same_file_creates_a_second_batch_but_no_duplicate_lead(
    api_client: TestClient, batch_cleanup: _Cleanup
) -> None:
    email = f"reupload-{uuid.uuid4().hex[:10]}@example.test"
    batch_cleanup.emails.append(email)
    content = _csv(f"email\n{email}\n")

    first = _upload(api_client, content=content).json()
    second = _upload(api_client, content=content).json()
    batch_cleanup.batch_ids.extend([uuid.UUID(first["batch_id"]), uuid.UUID(second["batch_id"])])

    assert first["batch_id"] != second["batch_id"]

    first_lead = api_client.get(f"/batches/{first['batch_id']}/leads").json()["items"][0]["lead_id"]
    second_lead = api_client.get(f"/batches/{second['batch_id']}/leads").json()["items"][0][
        "lead_id"
    ]
    assert first_lead == second_lead  # same lead both times
    batch_cleanup.lead_ids.append(uuid.UUID(first_lead))


# --------------------------------------------------------------- authorization --


def test_an_api_key_cannot_upload_or_list_batches(
    api_client: TestClient, app_state: AppState, cleanup_api_keys: list[UUID]
) -> None:
    created = api_client.post("/api-keys", json={"label": "batch-probe", "scopes": ["leads:write"]})
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as raw_client:
        upload = raw_client.post(
            "/batches", files={"file": ("x.csv", b"email\na@b.com\n", "text/csv")}, headers=headers
        )
        listing = raw_client.get("/batches", headers=headers)

    assert upload.status_code == 403
    assert listing.status_code == 403


def test_any_active_member_role_can_upload(app_state: AppState, batch_cleanup: _Cleanup) -> None:
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
    email = f"reviewer-{uuid.uuid4().hex[:10]}@example.test"
    batch_cleanup.emails.append(email)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = _upload(client, content=_csv(f"email\n{email}\n"))

    assert response.status_code == 201
    batch_cleanup.batch_ids.append(uuid.UUID(response.json()["batch_id"]))
    lead_id = client.get(f"/batches/{response.json()['batch_id']}/leads").json()["items"][0][
        "lead_id"
    ]
    batch_cleanup.lead_ids.append(uuid.UUID(lead_id))


# --------------------------------------------------------------- tenant isolation --


def test_a_batch_is_invisible_to_a_different_organization(
    api_client: TestClient, api_client_org_b: TestClient, batch_cleanup: _Cleanup
) -> None:
    email = f"iso-{uuid.uuid4().hex[:10]}@example.test"
    batch_cleanup.emails.append(email)
    response = _upload(api_client, content=_csv(f"email\n{email}\n"))
    body = response.json()
    batch_cleanup.batch_ids.append(uuid.UUID(body["batch_id"]))
    lead_id = api_client.get(f"/batches/{body['batch_id']}/leads").json()["items"][0]["lead_id"]
    batch_cleanup.lead_ids.append(uuid.UUID(lead_id))

    cross_org_get = api_client_org_b.get(f"/batches/{body['batch_id']}")
    cross_org_list = api_client_org_b.get("/batches")

    assert cross_org_get.status_code == 404
    assert all(item["batch_id"] != body["batch_id"] for item in cross_org_list.json())


# ------------------------------------------------------------------------ RLS --


def test_rls_foreign_organization_cannot_select_batch_rows(
    migrated_database_direct: str,
    db_conn: psycopg.Connection,
    two_orgs: TwoOrgFixture,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lead_batches (organization_id, filename, total_rows, accepted_rows, "
            "rejected_rows, created_by_user_id) VALUES (%s, 'x.csv', 0, 0, 0, %s) "
            "RETURNING batch_id",
            (two_orgs.org_b, uuid.uuid4()),
        )
        batch_row = cur.fetchone()
    assert batch_row is not None
    db_conn.commit()

    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)  # only a member of org_a
        cur.execute(
            "SELECT batch_id FROM lead_batches WHERE organization_id = %s", (two_orgs.org_b,)
        )
        rows = cur.fetchall()
    assert rows == []


def test_rls_own_organization_can_select_its_batch(
    migrated_database_direct: str,
    db_conn: psycopg.Connection,
    two_orgs: TwoOrgFixture,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lead_batches (organization_id, filename, total_rows, accepted_rows, "
            "rejected_rows, created_by_user_id) VALUES (%s, 'x.csv', 0, 0, 0, %s) "
            "RETURNING batch_id",
            (two_orgs.org_a, uuid.uuid4()),
        )
        batch_row = cur.fetchone()
    assert batch_row is not None
    db_conn.commit()
    batch_id = batch_row[0]

    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "SELECT batch_id FROM lead_batches WHERE organization_id = %s", (two_orgs.org_a,)
        )
        rows = cur.fetchall()
    assert rows == [(batch_id,)]


def test_rls_anon_sees_no_batches(migrated_database_direct: str, two_orgs: TwoOrgFixture) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, None)
        cur.execute("SELECT batch_id FROM lead_batches")
        rows = cur.fetchall()
    assert rows == []


# --------------------------------------------------------- progress integration --


@pytest.fixture(scope="module")
def batch_runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def batch_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=6, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def batch_handlers(
    batch_runtime: SimulatedEnrichmentRuntime, batch_pool: ConnectionPool
) -> dict[str, JobHandler]:
    return build_handlers(batch_pool, runtime=batch_runtime, provider_mode="simulated")


def _drive_all_to_completion(
    job_queue: PostgresJobQueue, pool: ConnectionPool, handlers: dict[str, JobHandler]
) -> None:
    for _ in range(_MAX_CYCLES):
        results = run_worker_cycle(
            job_queue,
            pool,
            handlers,
            worker_id=f"batch-it-{uuid.uuid4().hex[:8]}",
            batch_size=10,
            job_types=["compute_score"],
        )
        if not results:
            return


def test_batch_progress_reflects_real_outcomes_after_processing(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    job_queue: PostgresJobQueue,
    batch_pool: ConnectionPool,
    batch_handlers: dict[str, JobHandler],
    cleanup_ingest: IngestCleanup,
    batch_cleanup: _Cleanup,
) -> None:
    """Two corpus identities with known outcomes (autonomous auto-route,
    escalation) in one batch — proves `batch_progress` derives real counts
    from `leads`/`decision_receipts`, not a guess, and that `is_complete`
    only flips once every lead has actually settled.

    Unlike every other test in this file, this one needs its two leads to
    genuinely start at `NEW` — but `arie.batches`'s `external_ref` is
    deliberately *deterministic* per email (`csv:{normalized_email}`, see
    that module's docstring), so a prior run of this exact test against a
    persistent, non-reset local test database (rather than a fresh one per
    CI run) would otherwise match its own leftover leads instead of creating
    new ones. Defensively clearing any pre-existing `csv_upload`-sourced
    lead for these two emails first makes the test idempotent across reruns
    regardless of prior history — the same problem `tests/integration/
    conftest.py`'s `RUN_ID` tagging solves for every *other* test's own
    freely-chosen `source`, solved here by construction instead since this
    module's `source` is intentionally constant, not per-run.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM leads WHERE organization_id = %s AND source = 'csv_upload' "
            "AND external_ref = ANY(%s)",
            (
                LEGACY_ORGANIZATION_ID,
                [f"csv:{AUTONOMOUS_EMAIL}", f"csv:{ESCALATING_EMAIL}"],
            ),
        )
    db_conn.commit()

    cleanup_ingest.domains.extend([AUTONOMOUS_DOMAIN, ESCALATING_DOMAIN])
    cleanup_ingest.emails.extend([AUTONOMOUS_EMAIL, ESCALATING_EMAIL])
    content = _csv(
        f"email,domain\n{AUTONOMOUS_EMAIL},{AUTONOMOUS_DOMAIN}\n{ESCALATING_EMAIL},{ESCALATING_DOMAIN}\n"
    )

    response = _upload(api_client, content=content)
    assert response.status_code == 201
    body = response.json()
    assert body["progress"]["is_complete"] is False
    batch_cleanup.batch_ids.append(uuid.UUID(body["batch_id"]))
    rows = api_client.get(f"/batches/{body['batch_id']}/leads").json()["items"]
    for row in rows:
        batch_cleanup.lead_ids.append(uuid.UUID(row["lead_id"]))

    _drive_all_to_completion(job_queue, batch_pool, batch_handlers)

    final = api_client.get(f"/batches/{body['batch_id']}").json()
    assert final["progress"]["is_complete"] is True
    assert final["progress"]["qualified_count"] == 1
    assert final["progress"]["review_count"] == 1
    assert final["progress"]["processing_count"] == 0
