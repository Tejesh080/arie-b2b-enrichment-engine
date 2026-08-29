"""Live-database tests for the ingestion API (M1 Step 9).

These exercise the actual HTTP surface against the actual database: identity
resolution, the lead row, its first event, and the queued job all have to appear
together or not at all. The rollback tests at the bottom are the ones that
matter most — every other guarantee here rests on that one transaction really
being one transaction.

Every test uses a freshly generated domain and email so it can never collide
with another test, another run, or real data in the database it points at.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import IngestCleanup, authorize_app, source_for

from arie.api.main import AppState, create_app
from arie.config import POLICY
from arie.core.types import LeadStatus
from arie.jobs.queue import EnqueuedJob, PostgresJobQueue
from arie.providers.catalog import total_catalog_cost
from arie.statemachine.transitions import job_type_for

pytestmark = pytest.mark.integration


def _identity(cleanup: IngestCleanup) -> tuple[str, str]:
    """A domain and matching work email, unique to this test, registered for cleanup."""
    domain = f"acme-{uuid.uuid4().hex[:12]}.test"
    email = f"ada@{domain}"
    cleanup.domains.append(domain)
    cleanup.emails.append(email)
    return domain, email


def _payload(email: str, domain: str | None = None, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": source_for("webhook"),
        "email": email,
        "external_ref": f"crm-{uuid.uuid4().hex[:10]}",
        "company_name": "Acme Incorporated",
        "full_name": "Ada Lovelace",
        "title": "VP Engineering",
    }
    if domain is not None:
        payload["company_domain"] = domain
    payload.update(overrides)
    return payload


def _count(conn: psycopg.Connection, sql: str, params: tuple[Any, ...]) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_post_lead_creates_identity_lead_event_and_job(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    domain, email = _identity(cleanup_ingest)

    response = api_client.post("/leads", json=_payload(email, domain))

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["job_created"] is True
    assert body["status"] == LeadStatus.NEW

    lead_id = uuid.UUID(body["lead_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT person_id, company_id, source, status FROM leads WHERE lead_id = %s",
            (lead_id,),
        )
        lead = cur.fetchone()
    assert lead is not None
    assert str(lead[0]) == body["person_id"]
    assert str(lead[1]) == body["company_id"]
    assert lead[2] == source_for("webhook")
    assert lead[3] == LeadStatus.NEW

    # Identity resolution actually ran in the request path — the whole point of
    # step 9 being where it is first called.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_domain FROM companies WHERE company_id = %s", (body["company_id"],)
        )
        company = cur.fetchone()
        cur.execute(
            "SELECT canonical_email, company_id FROM persons WHERE person_id = %s",
            (body["person_id"],),
        )
        person = cur.fetchone()
    assert company is not None and company[0] == domain
    assert person is not None and person[0] == email
    assert str(person[1]) == body["company_id"]

    # The append-only log records the ingestion itself, not just later transitions.
    assert (
        _count(
            db_conn,
            "SELECT count(*) FROM lead_events WHERE lead_id = %s AND event_type = 'lead:ingested'",
            (lead_id,),
        )
        == 1
    )

    # And the work is queued, of the type the state graph says advances a NEW lead.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT job_type, status, idempotency_key FROM jobs WHERE lead_id = %s", (lead_id,)
        )
        jobs = cur.fetchall()
    assert len(jobs) == 1
    assert jobs[0][0] == job_type_for(LeadStatus.NEW)
    assert jobs[0][1] == "pending"
    assert jobs[0][2] == f"lead:{lead_id}:{job_type_for(LeadStatus.NEW)}"


def test_redelivering_the_same_record_creates_no_second_lead_or_job(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """The idempotency guarantee, on the key upstream systems actually retry with.

    A webhook redelivery is normal, not exceptional — it must be a no-op that
    reports what already exists, not a duplicate lead and a duplicate unit of
    paid work.
    """
    domain, email = _identity(cleanup_ingest)
    payload = _payload(email, domain)

    first = api_client.post("/leads", json=payload)
    second = api_client.post("/leads", json=payload)

    assert first.status_code == 201
    assert first.json()["created"] is True
    # 200, not 409: redelivery is accepted and idempotent, not a conflict.
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["job_created"] is False

    lead_id = uuid.UUID(first.json()["lead_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    assert second.json()["lead_id"] == first.json()["lead_id"]
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["company_id"] == first.json()["company_id"]
    assert second.json()["person_id"] == first.json()["person_id"]

    assert (
        _count(
            db_conn,
            "SELECT count(*) FROM leads WHERE source = %s AND external_ref = %s",
            (source_for("webhook"), payload["external_ref"]),
        )
        == 1
    )
    assert _count(db_conn, "SELECT count(*) FROM jobs WHERE lead_id = %s", (lead_id,)) == 1
    # The ingestion event fires once, for the delivery that created the lead.
    assert (
        _count(
            db_conn,
            "SELECT count(*) FROM lead_events WHERE lead_id = %s AND event_type = 'lead:ingested'",
            (lead_id,),
        )
        == 1
    )


def test_redelivering_after_the_job_dead_letters_requeues_it(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """The audit's literal reproduction: before this fix, a webhook redelivered
    after the lead's job permanently failed matched the dead row forever —
    `job_created: false`, HTTP 200, and no worker would ever claim the lead
    again, indistinguishable from ordinary successful idempotent redelivery
    until someone went looking at `jobs.status` directly."""
    domain, email = _identity(cleanup_ingest)
    payload = _payload(email, domain)

    first = api_client.post("/leads", json=payload)
    assert first.status_code == 201
    lead_id = uuid.UUID(first.json()["lead_id"])
    job_id = uuid.UUID(first.json()["job_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    # Simulate the job exhausting its attempt budget, exactly as
    # arie.jobs.queue.PostgresJobQueue.fail would leave it.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'dead_letter', attempt_count = 4, "
            "last_error = 'simulated permanent failure' WHERE job_id = %s",
            (job_id,),
        )
    db_conn.commit()

    # Upstream redelivers the exact same webhook — the only signal it has
    # that this lead needs processing.
    second = api_client.post("/leads", json=payload)

    assert second.status_code == 200
    assert second.json()["created"] is False, "same lead, not a duplicate"
    assert second.json()["job_id"] == str(job_id), "same job row, requeued in place"
    assert second.json()["job_created"] is False, "not a *new* job — the dead one came back"
    assert second.json()["job_requeued"] is True

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    assert row is not None
    assert row == ("pending", 0), "claimable again, with a fresh attempt budget"


def test_lead_without_external_ref_is_not_deduplicated(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """NULL never conflicts under the partial unique index — so every delivery
    is a new lead, and the response says so rather than pretending otherwise."""
    domain, email = _identity(cleanup_ingest)
    payload = _payload(email, domain)
    del payload["external_ref"]

    first = api_client.post("/leads", json=payload)
    second = api_client.post("/leads", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["lead_id"] != second.json()["lead_id"]
    assert second.json()["created"] is True

    for response in (first, second):
        cleanup_ingest.lead_ids.append(uuid.UUID(response.json()["lead_id"]))

    # Same person and company though — identity resolution is idempotent even
    # where lead creation isn't.
    assert first.json()["company_id"] == second.json()["company_id"]
    assert first.json()["person_id"] == second.json()["person_id"]


def test_second_contact_at_a_known_company_reuses_the_company(
    api_client: TestClient, cleanup_ingest: IngestCleanup
) -> None:
    """The invariant company-level evidence sharing depends on.

    Two different people at one company must resolve to one company_id, or the
    evidence store caches per-contact and the saving it exists to produce
    never materialises.
    """
    domain, email = _identity(cleanup_ingest)
    colleague = f"grace@{domain}"
    cleanup_ingest.emails.append(colleague)

    first = api_client.post("/leads", json=_payload(email, domain))
    second = api_client.post("/leads", json=_payload(colleague, domain))

    for response in (first, second):
        cleanup_ingest.lead_ids.append(uuid.UUID(response.json()["lead_id"]))

    assert first.json()["company_id"] == second.json()["company_id"]
    assert first.json()["person_id"] != second.json()["person_id"]


def test_freemail_sender_resolves_by_company_name_not_by_email_domain(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Otherwise every gmail.com sender would merge into one enormous company."""
    email = f"ada.{uuid.uuid4().hex[:10]}@gmail.com"
    company_name = f"Solo Consulting {uuid.uuid4().hex[:8]}"
    cleanup_ingest.emails.append(email)
    cleanup_ingest.company_names.append(company_name.lower())

    response = api_client.post("/leads", json=_payload(email, None, company_name=company_name))

    assert response.status_code == 201
    cleanup_ingest.lead_ids.append(uuid.UUID(response.json()["lead_id"]))

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_domain, normalized_name FROM companies WHERE company_id = %s",
            (response.json()["company_id"],),
        )
        company = cur.fetchone()
    assert company is not None
    assert company[0] is None, "a free-mail domain must never become a company domain"
    assert company[1] == company_name.lower()


def test_budget_cap_comes_from_policy_config_not_the_stale_column_default(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Pins the defect migration 0005 corrects.

    `leads.budget_usd_cap` defaulted to $0.50, below `deep_research`'s $0.600 —
    the only source of the disqualifying flag. A lead capped there can never
    buy the one provider that detects a blocker, whatever the policy decides.
    Ingestion writes the configured cap explicitly so the column default is
    never what decides this.
    """
    domain, email = _identity(cleanup_ingest)

    response = api_client.post("/leads", json=_payload(email, domain))
    lead_id = uuid.UUID(response.json()["lead_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    with db_conn.cursor() as cur:
        cur.execute("SELECT budget_usd_cap FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == Decimal(str(POLICY.lead_budget_usd_cap))
    assert row[0] > Decimal(str(total_catalog_cost())), (
        "the cap must sit above the cost of calling every provider once, or it "
        "stops being a backstop and becomes a binding constraint"
    )


def test_explicit_budget_cap_is_honoured(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    domain, email = _identity(cleanup_ingest)

    response = api_client.post("/leads", json=_payload(email, domain, budget_usd_cap="2.75"))
    lead_id = uuid.UUID(response.json()["lead_id"])
    cleanup_ingest.lead_ids.append(lead_id)

    with db_conn.cursor() as cur:
        cur.execute("SELECT budget_usd_cap FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == Decimal("2.75")


def test_invalid_email_is_rejected_before_anything_is_written(
    api_client: TestClient, db_conn: psycopg.Connection
) -> None:
    external_ref = f"crm-{uuid.uuid4().hex[:10]}"

    response = api_client.post(
        "/leads",
        json={
            "source": source_for("webhook"),
            "email": "not-an-email",
            "external_ref": external_ref,
        },
    )

    assert response.status_code == 422
    assert (
        _count(db_conn, "SELECT count(*) FROM leads WHERE external_ref = %s", (external_ref,)) == 0
    )


def test_get_lead_returns_status_and_a_zero_cost_rollup(
    api_client: TestClient, cleanup_ingest: IngestCleanup
) -> None:
    """A freshly ingested lead has spent nothing — and `v_lead_cost` must say
    zero rather than returning no row, or the API would 404 a lead that exists."""
    domain, email = _identity(cleanup_ingest)
    created = api_client.post("/leads", json=_payload(email, domain))
    lead_id = created.json()["lead_id"]
    cleanup_ingest.lead_ids.append(uuid.UUID(lead_id))

    response = api_client.get(f"/leads/{lead_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["lead_id"] == lead_id
    assert body["status"] == LeadStatus.NEW
    assert body["version"] == 1
    assert body["source"] == source_for("webhook")
    assert Decimal(str(body["cost"]["total_cost_usd"])) == Decimal(0)
    assert body["cost"]["provider_calls"] == 0
    assert body["cost"]["cache_hits"] == 0


def test_get_unknown_lead_is_404(api_client: TestClient) -> None:
    response = api_client.get(f"/leads/{uuid.uuid4()}")
    assert response.status_code == 404


def test_healthz_reports_the_database_reachable_and_schema_ready(api_client: TestClient) -> None:
    # `api_client` builds on `migrated_database`, so every migration has
    # already been applied — the "ok" case, not just "database reachable".
    response = api_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": True, "schema_ready": True}


def test_healthz_reports_degraded_when_schema_is_incomplete(
    api_client: TestClient, db_conn: psycopg.Connection
) -> None:
    """A reachable database with a migration missing is a different failure
    than an unreachable one — see the handler's own docstring — and must
    report as such rather than as "ok"."""
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM schema_migrations WHERE filename = %s", ("0002_metrics_views.sql",)
        )
    db_conn.commit()

    try:
        response = api_client.get("/healthz")
        assert response.status_code == 503
        assert response.json() == {"status": "degraded", "database": True, "schema_ready": False}
    finally:
        from scripts.migrate import checksum_of, migration_files

        real = next(f for f in migration_files() if f.name == "0002_metrics_views.sql")
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                ("0002_metrics_views.sql", checksum_of(real.read_text(encoding="utf-8"))),
            )
        db_conn.commit()


def test_healthz_never_reports_ok_when_migration_discovery_fails(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit-fixed bug: a `pending_migrations` that can't see the
    migrations directory used to return `[]` (nothing pending), which made
    this endpoint report `ok` on a database whose actual schema state is
    unknown. It must report `degraded`, the same as a real pending migration
    — see `healthz`'s own docstring for why "I can't tell" and "ok" must
    never be the same response."""
    import arie.api.main as main_module
    from arie.migrations import MigrationsDirectoryError

    def _raise(*args: object, **kwargs: object) -> list[str]:
        raise MigrationsDirectoryError("simulated: migrations directory not found")

    monkeypatch.setattr(main_module, "pending_migrations", _raise)

    response = api_client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": True, "schema_ready": False}


# --------------------------------------------------------------- rollback --


class _ExplodingQueue(PostgresJobQueue):
    """Fails at the last step of ingestion, after identity and the lead insert.

    Chosen deliberately: enqueueing is the final write, so a failure there is
    the case where the most has already happened inside the transaction and
    the most would survive if the transaction were not real.
    """

    def enqueue_in(self, *args: Any, **kwargs: Any) -> EnqueuedJob:
        raise RuntimeError("enqueue exploded")


def test_failure_during_ingestion_rolls_back_everything_including_identity(
    app_state: AppState, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    domain, email = _identity(cleanup_ingest)
    external_ref = f"crm-{uuid.uuid4().hex[:10]}"
    broken = replace(app_state, queue=_ExplodingQueue(app_state.pool))
    broken_app = create_app(state=broken)
    authorize_app(broken_app)

    with TestClient(broken_app, raise_server_exceptions=False) as client:
        response = client.post("/leads", json=_payload(email, domain, external_ref=external_ref))

    assert response.status_code == 500

    # Nothing survives: not the lead, and not the company and person that were
    # already resolved and written before the failure.
    assert (
        _count(db_conn, "SELECT count(*) FROM leads WHERE external_ref = %s", (external_ref,)) == 0
    )
    assert (
        _count(db_conn, "SELECT count(*) FROM companies WHERE canonical_domain = %s", (domain,))
        == 0
    )
    assert _count(db_conn, "SELECT count(*) FROM persons WHERE canonical_email = %s", (email,)) == 0


def test_a_rolled_back_request_can_simply_be_retried(
    app_state: AppState,
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
) -> None:
    """Rollback leaves no wreckage to clean up before retrying.

    The failure mode this rules out is a half-written first attempt that makes
    the retry fail differently — a partially resolved identity colliding with
    a unique constraint, say. The retry has to be an ordinary first attempt.
    """
    domain, email = _identity(cleanup_ingest)
    external_ref = f"crm-{uuid.uuid4().hex[:10]}"
    payload = _payload(email, domain, external_ref=external_ref)
    broken = replace(app_state, queue=_ExplodingQueue(app_state.pool))
    broken_app = create_app(state=broken)
    authorize_app(broken_app)

    with TestClient(broken_app, raise_server_exceptions=False) as client:
        assert client.post("/leads", json=payload).status_code == 500

    retry = api_client.post("/leads", json=payload)

    assert retry.status_code == 201
    assert retry.json()["created"] is True
    cleanup_ingest.lead_ids.append(uuid.UUID(retry.json()["lead_id"]))
    assert (
        _count(db_conn, "SELECT count(*) FROM jobs WHERE lead_id = %s", (retry.json()["lead_id"],))
        == 1
    )
