"""Live-database tests for GET /leads/{lead_id}/receipt (post-M1 Decision Receipt, P1).

Drives real corpus leads through ingestion -> queue -> compute_score exactly like
test_pipeline_integration.py, then asserts what the receipt endpoint reports
against what was actually persisted -- never against values recomputed with
today's policy code, which is the historical-truth trap `arie.api.receipt`'s
module docstring exists to avoid.

`nadia.delacroix@lumen500.com` (autonomous, AUTO_ROUTED) and
`nadia.haddad@cobalt500.com` (escalates to AWAITING_HUMAN) are the same
seed-42 corpus identities documented in docs/architecture.md and README.md's
own n8n walkthrough -- real, stable people in the frozen dataset, not
fixtures invented for this suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup

from arie.core.types import LeadStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobHandler, run_worker_cycle
from arie.providers.catalog import ALL_PROVIDERS

pytestmark = pytest.mark.integration

_MAX_CYCLES = 6

AUTONOMOUS_EMAIL = "nadia.delacroix@lumen500.com"
AUTONOMOUS_DOMAIN = "lumen500.com"
ESCALATING_EMAIL = "nadia.haddad@cobalt500.com"
ESCALATING_DOMAIN = "cobalt500.com"


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def receipt_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=6, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def handlers(
    runtime: SimulatedEnrichmentRuntime, receipt_pool: ConnectionPool
) -> dict[str, JobHandler]:
    return build_handlers(receipt_pool, runtime=runtime, provider_mode="simulated")


def _corpus_lead(leads: list[EvalLead], email: str) -> EvalLead:
    for lead in leads:
        if lead.person.email == email:
            return lead
    raise AssertionError(f"{email} not found in the seed-42 corpus")


def _ingest(
    api_client: TestClient, cleanup: IngestCleanup, lead: EvalLead, *, email: str, domain: str
) -> dict[str, Any]:
    cleanup.domains.append(domain)
    cleanup.emails.append(email)
    response = api_client.post(
        "/leads",
        json={
            "source": "receipt-it",
            "email": email,
            "external_ref": f"receipt-{uuid.uuid4().hex[:12]}",
            "company_domain": domain,
            "company_name": lead.company.legal_name,
            "full_name": lead.person.full_name,
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))
    return body


def _drive_to_completion(
    job_queue: PostgresJobQueue,
    receipt_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    job_id: str,
) -> str:
    for _ in range(_MAX_CYCLES):
        run_worker_cycle(
            job_queue,
            receipt_pool,
            handlers,
            worker_id=f"receipt-it-{uuid.uuid4().hex[:8]}",
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


def _register_cleanup(
    db_conn: psycopg.Connection,
    cleanup: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    lead_body: dict[str, Any],
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT idempotency_key FROM provider_calls WHERE lead_id = %s",
            (lead_body["lead_id"],),
        )
        cleanup.provider_call_keys.extend(row[0] for row in cur.fetchall())
    cleanup_evidence.append(uuid.UUID(lead_body["company_id"]))
    cleanup_evidence.append(uuid.UUID(lead_body["person_id"]))


# --------------------------------------------------------------- unknown lead --


def test_unknown_lead_is_404(api_client: TestClient) -> None:
    response = api_client.get(f"/leads/{uuid.uuid4()}/receipt")
    assert response.status_code == 404


# ----------------------------------------------------------------- pending --


def test_a_freshly_ingested_lead_has_a_pending_receipt(
    api_client: TestClient, cleanup_ingest: IngestCleanup
) -> None:
    """Before any worker has touched it, the receipt must say plainly that no
    decision exists yet -- never fabricate one."""
    domain = f"pending-{uuid.uuid4().hex[:10]}.test"
    email = f"nobody@{domain}"
    cleanup_ingest.domains.append(domain)
    cleanup_ingest.emails.append(email)
    ingest = api_client.post(
        "/leads",
        json={
            "source": "receipt-it",
            "email": email,
            "external_ref": f"receipt-{uuid.uuid4().hex[:10]}",
            "company_domain": domain,
        },
    )
    assert ingest.status_code == 201
    lead_id = ingest.json()["lead_id"]
    cleanup_ingest.lead_ids.append(uuid.UUID(lead_id))

    response = api_client.get(f"/leads/{lead_id}/receipt")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["lead_status"] == LeadStatus.NEW
    assert body["decision"] is None
    assert body["score"] is None
    assert body["stopping"] is None
    assert body["versions"] is None
    assert body["created_at"] is None
    assert body["human_review"] is None
    assert Decimal(str(body["cost"]["total_cost_usd"])) == Decimal(0)


def test_a_dead_lettered_lead_reports_processing_failed_not_pending(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """A lead that broke permanently before ever reaching a decision must be
    told apart from one still legitimately mid-pipeline -- both look
    identical as "no decision_receipts row" otherwise."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (source, status) VALUES (%s, %s) RETURNING lead_id",
            ("receipt-it", str(LeadStatus.DEAD_LETTER)),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    lead_id = row[0]
    cleanup_ingest.lead_ids.append(lead_id)

    response = api_client.get(f"/leads/{lead_id}/receipt")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing_failed"
    assert body["lead_status"] == LeadStatus.DEAD_LETTER
    assert body["decision"] is None


# -------------------------------------------------------------- autonomous --


def test_autonomous_lead_receipt_matches_persisted_state(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    receipt_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    lead = _corpus_lead(leads, AUTONOMOUS_EMAIL)
    body = _ingest(
        api_client, cleanup_ingest, lead, email=AUTONOMOUS_EMAIL, domain=AUTONOMOUS_DOMAIN
    )

    job_status = _drive_to_completion(job_queue, receipt_pool, handlers, db_conn, body["job_id"])
    assert job_status == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT provider, cache_hit FROM provider_calls WHERE lead_id = %s",
            (body["lead_id"],),
        )
        db_calls = cur.fetchall()
        cur.execute(
            "SELECT total_score, decision_confidence FROM scores WHERE lead_id = %s",
            (body["lead_id"],),
        )
        db_score = cur.fetchone()
        cur.execute("SELECT count(*) FROM decision_receipts WHERE lead_id = %s", (body["lead_id"],))
        receipt_row_count = cur.fetchone()

    assert receipt_row_count is not None and receipt_row_count[0] == 1, (
        "compute_score must write exactly one decision_receipts row per lead"
    )

    response = api_client.get(f"/leads/{body['lead_id']}/receipt")
    assert response.status_code == 200
    receipt = response.json()

    assert receipt["status"] == "decided"
    assert receipt["lead_status"] == LeadStatus.AUTO_ROUTED
    assert receipt["decision"]["recommended_action"] == "auto_route"
    assert receipt["decision"]["autonomous"] is True
    assert receipt["decision"]["final_status"] == LeadStatus.AUTO_ROUTED
    assert receipt["decision"]["human_override"] is False, "never escalated -- nothing to override"
    assert receipt["human_review"] is None

    assert db_score is not None
    assert receipt["score"]["value"] == pytest.approx(float(db_score[0]))
    assert receipt["score"]["confidence"] == pytest.approx(float(db_score[1]), abs=1e-6)
    assert (
        receipt["score"]["bounds"]["lower"]
        <= receipt["score"]["value"]
        <= receipt["score"]["bounds"]["upper"]
    )

    assert receipt["stopping"]["reason_code"] in (
        "decision_settled",
        "confidence_reached",
        "all_providers_called",
    )
    assert receipt["stopping"]["explanation"]

    called_providers = {c["provider"] for c in receipt["providers"]["called"]}
    assert called_providers == {row[0] for row in db_calls}
    assert len(receipt["providers"]["called"]) == len(db_calls), (
        "one receipt entry per provider_calls row, no more, no fewer"
    )
    assert set(receipt["providers"]["not_called"]) == set(ALL_PROVIDERS) - called_providers
    assert set(receipt["providers"]["not_called"]).isdisjoint(called_providers)

    assert receipt["evidence"]["provider_calls"] == sum(1 for row in db_calls if not row[1])
    assert receipt["evidence"]["cache_hits"] == sum(1 for row in db_calls if row[1])

    assert receipt["versions"]["policy"] == "calibrated_bounds"
    assert receipt["versions"]["scorer"] == "icp-1.0.0"
    assert receipt["versions"]["confidence_calibration"] in ("isotonic", "platt")

    # Repeated GET is stable -- byte-identical, not just similar.
    second = api_client.get(f"/leads/{body['lead_id']}/receipt")
    assert second.status_code == 200
    assert second.json() == receipt


def test_a_second_lead_at_a_known_company_shows_cache_hits(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    receipt_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    """A cache hit must be reported, not silently dropped -- handoff item #5."""
    lead = _corpus_lead(leads, AUTONOMOUS_EMAIL)
    first = _ingest(
        api_client, cleanup_ingest, lead, email=AUTONOMOUS_EMAIL, domain=AUTONOMOUS_DOMAIN
    )
    assert (
        _drive_to_completion(job_queue, receipt_pool, handlers, db_conn, first["job_id"]) == "done"
    )
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, first)

    # Same corpus identity, a second lead row (fresh external_ref) -- resolves
    # to the same person/company entities, whose evidence the first run just
    # bought and persisted durably.
    second_response = api_client.post(
        "/leads",
        json={
            "source": "receipt-it",
            "email": AUTONOMOUS_EMAIL,
            "external_ref": f"receipt-{uuid.uuid4().hex[:12]}",
            "company_domain": AUTONOMOUS_DOMAIN,
            "company_name": lead.company.legal_name,
            "full_name": lead.person.full_name,
        },
    )
    assert second_response.status_code == 201
    second = second_response.json()
    cleanup_ingest.lead_ids.append(uuid.UUID(second["lead_id"]))

    assert (
        _drive_to_completion(job_queue, receipt_pool, handlers, db_conn, second["job_id"]) == "done"
    )
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, second)

    receipt = api_client.get(f"/leads/{second['lead_id']}/receipt").json()
    assert receipt["evidence"]["cache_hits"] > 0
    assert any(call["cache_hit"] for call in receipt["providers"]["called"])


# --------------------------------------------------------- human-escalated --


def test_escalated_lead_receipt_preserves_recommendation_through_override(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    receipt_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    """The M1 handoff's central worked example: recommendation=reject,
    autonomous=false, a human approves anyway. The receipt must show the
    original recommendation and the overridden outcome side by side, never
    rewrite history as "ARIE decided AUTO_ROUTED"."""
    lead = _corpus_lead(leads, ESCALATING_EMAIL)
    body = _ingest(
        api_client, cleanup_ingest, lead, email=ESCALATING_EMAIL, domain=ESCALATING_DOMAIN
    )

    job_status = _drive_to_completion(job_queue, receipt_pool, handlers, db_conn, body["job_id"])
    assert job_status == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    before = api_client.get(f"/leads/{body['lead_id']}/receipt")
    assert before.status_code == 200
    before_body = before.json()

    assert before_body["status"] == "decided"
    assert before_body["lead_status"] == LeadStatus.AWAITING_HUMAN
    assert before_body["decision"]["recommended_action"] == "reject"
    assert before_body["decision"]["autonomous"] is False
    assert before_body["decision"]["final_status"] == LeadStatus.AWAITING_HUMAN
    assert before_body["decision"]["human_override"] is False, "not yet reviewed"
    assert before_body["human_review"] is not None
    assert before_body["human_review"]["required"] is True
    assert before_body["human_review"]["original_decision"] == "reject"
    assert before_body["human_review"]["final_decision"] is None
    assert before_body["human_review"]["action"] is None
    assert before_body["human_review"]["responded_at"] is None

    with db_conn.cursor() as cur:
        cur.execute("SELECT review_id FROM human_reviews WHERE lead_id = %s", (body["lead_id"],))
        review_row = cur.fetchone()
    assert review_row is not None
    review_id = review_row[0]

    review_get = api_client.get(f"/reviews/{review_id}")
    assert review_get.status_code == 200
    lead_version = review_get.json()["lead_version"]

    decision_response = api_client.post(
        f"/reviews/{review_id}/decision",
        json={
            "action": "approve",
            "reviewer": "receipt-test-reviewer",
            "expected_lead_version": lead_version,
        },
    )
    assert decision_response.status_code == 200
    assert decision_response.json()["final_decision"] == "auto_route"

    after = api_client.get(f"/leads/{body['lead_id']}/receipt")
    assert after.status_code == 200
    after_body = after.json()

    # The frozen recommendation and machine-decision snapshot must survive
    # the override untouched.
    assert after_body["decision"]["recommended_action"] == "reject"
    assert after_body["decision"]["autonomous"] is False
    assert after_body["created_at"] == before_body["created_at"]
    assert after_body["score"] == before_body["score"]
    assert after_body["stopping"] == before_body["stopping"]

    # The live outcome reflects what actually happened.
    assert after_body["lead_status"] == LeadStatus.AUTO_ROUTED
    assert after_body["decision"]["final_status"] == LeadStatus.AUTO_ROUTED
    assert after_body["decision"]["human_override"] is True
    assert after_body["human_review"]["final_decision"] == "auto_route"
    assert after_body["human_review"]["action"] == "approve"
    assert after_body["human_review"]["reviewer"] == "receipt-test-reviewer"
    assert after_body["human_review"]["responded_at"] is not None
