"""Shadow mode against a live database (post-M1 P5).

The deterministic demonstration item 22 of the P5 brief asks for: the same
underlying corpus lead, once ingested normally (ARIE's ordinary authoritative
transition) and once with `mode="shadow"` (ARIE computes the identical
recommendation but never takes an authoritative action) -- and proof that
normal-mode behaviour is unchanged by shadow mode existing at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, source_for

from arie.core.types import LeadStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import (
    SimulatedEnrichmentRuntime,
    build_handlers,
    build_runtime,
    decision_route,
)
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobHandler, run_worker_cycle
from arie.policy.base import EvidenceCache, PolicyOutcome, RunContext
from arie.providers.simulated import CallLedger

pytestmark = pytest.mark.integration

_MAX_CYCLES = 6
_TODAY = "date_trunc('day', now())"


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def shadow_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=6, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def handlers(
    runtime: SimulatedEnrichmentRuntime, shadow_pool: ConnectionPool
) -> dict[str, JobHandler]:
    return build_handlers(shadow_pool, runtime=runtime, provider_mode="simulated")


def _expected_outcome(runtime: SimulatedEnrichmentRuntime, lead: EvalLead) -> PolicyOutcome:
    ctx = RunContext(registry=runtime.registry, ledger=CallLedger(), cache=EvidenceCache())
    return runtime.policy.run(lead, ctx)


def _corpus_lead_with_route(
    runtime: SimulatedEnrichmentRuntime, leads: list[EvalLead], wanted_route: str
) -> tuple[EvalLead, PolicyOutcome]:
    for lead in leads:
        if lead.split != "test":
            continue
        outcome = _expected_outcome(runtime, lead)
        if decision_route(outcome.decision, outcome.autonomous) == wanted_route:
            return lead, outcome
    raise AssertionError(f"no test-split corpus lead routes to {wanted_route!r}")


def _ingest(
    api_client: TestClient,
    cleanup: IngestCleanup,
    lead: EvalLead,
    *,
    mode: str,
) -> dict[str, Any]:
    cleanup.domains.append(lead.company.canonical_domain)
    cleanup.emails.append(lead.person.email)
    response = api_client.post(
        "/leads",
        json={
            "source": source_for("shadow"),
            "email": lead.person.email,
            "external_ref": f"shadow-{mode}-{uuid.uuid4().hex[:12]}",
            "company_domain": lead.company.canonical_domain,
            "company_name": lead.company.legal_name,
            "full_name": lead.person.full_name,
            "mode": mode,
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    assert body["is_shadow"] == (mode == "shadow")
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))
    return body


def _drive_to_completion(
    job_queue: PostgresJobQueue,
    shadow_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    job_id: str,
) -> str:
    for _ in range(_MAX_CYCLES):
        run_worker_cycle(
            job_queue,
            shadow_pool,
            handlers,
            worker_id=f"shadow-it-{uuid.uuid4().hex[:8]}",
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
    body: dict[str, Any],
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT idempotency_key FROM provider_calls WHERE lead_id = %s", (body["lead_id"],)
        )
        cleanup.provider_call_keys.extend(row[0] for row in cur.fetchall())
    cleanup_evidence.append(uuid.UUID(body["company_id"]))
    cleanup_evidence.append(uuid.UUID(body["person_id"]))


def test_the_same_lead_type_normal_vs_shadow(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    shadow_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    """Item 22's deterministic demonstration, in one test: an escalating
    corpus lead reaches AWAITING_HUMAN with a real pending review in normal
    mode, and SHADOW_EVALUATED with no review at all -- for the exact same
    recommendation -- in shadow mode."""
    corpus_lead, expected = _corpus_lead_with_route(runtime, leads, "escalate_human")

    # -- normal mode: unchanged, authoritative -------------------------------
    normal_body = _ingest(api_client, cleanup_ingest, corpus_lead, mode="normal")
    assert (
        _drive_to_completion(job_queue, shadow_pool, handlers, db_conn, normal_body["job_id"])
        == "done"
    )
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, normal_body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (normal_body["lead_id"],))
        normal_status = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM human_reviews WHERE lead_id = %s", (normal_body["lead_id"],)
        )
        normal_review_count = cur.fetchone()
    assert normal_status is not None and normal_status[0] == LeadStatus.AWAITING_HUMAN
    assert normal_review_count is not None and normal_review_count[0] == 1

    normal_receipt = api_client.get(f"/leads/{normal_body['lead_id']}/receipt").json()
    assert normal_receipt["shadow"] is False
    assert normal_receipt["decision"]["recommended_action"] == str(expected.decision)
    assert normal_receipt["human_review"] is not None
    assert normal_receipt["human_review"]["required"] is True

    # -- shadow mode: same recommendation, no authoritative action -----------
    shadow_body = _ingest(api_client, cleanup_ingest, corpus_lead, mode="shadow")
    job_status = _drive_to_completion(
        job_queue, shadow_pool, handlers, db_conn, shadow_body["job_id"]
    )
    assert job_status == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, shadow_body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (shadow_body["lead_id"],))
        shadow_status = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM human_reviews WHERE lead_id = %s", (shadow_body["lead_id"],)
        )
        shadow_review_count = cur.fetchone()
        cur.execute(
            "SELECT event_type FROM lead_events WHERE lead_id = %s ORDER BY event_id",
            (shadow_body["lead_id"],),
        )
        shadow_events = [row[0] for row in cur.fetchall()]

    assert shadow_status is not None and shadow_status[0] == LeadStatus.SHADOW_EVALUATED
    assert shadow_review_count is not None and shadow_review_count[0] == 0, (
        "shadow mode must never open a real human review -- no fake human action"
    )
    assert shadow_events[-1] == "policy:shadow_evaluated"
    assert "lead:escalated" not in shadow_events

    shadow_receipt = api_client.get(f"/leads/{shadow_body['lead_id']}/receipt").json()
    assert shadow_receipt["shadow"] is True
    assert shadow_receipt["lead_status"] == LeadStatus.SHADOW_EVALUATED
    # The frozen recommendation is identical to the normal-mode sibling's --
    # same policy, same corpus lead, only the branch differs.
    assert shadow_receipt["decision"]["recommended_action"] == str(expected.decision)
    assert shadow_receipt["decision"]["autonomous"] == expected.autonomous
    assert shadow_receipt["score"] == normal_receipt["score"]
    assert shadow_receipt["human_review"] is None, "shadow evaluation never opens a real review"
    assert float(shadow_receipt["cost"]["total_cost_usd"]) >= 0.0


def test_an_autonomous_lead_in_shadow_mode_does_not_auto_route(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    shadow_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    """Suppression applies regardless of which decision the policy reached --
    an autonomous auto_route recommendation is just as suppressed as an
    escalation would be."""
    corpus_lead, expected = _corpus_lead_with_route(runtime, leads, "auto_route")
    body = _ingest(api_client, cleanup_ingest, corpus_lead, mode="shadow")

    job_status = _drive_to_completion(job_queue, shadow_pool, handlers, db_conn, body["job_id"])
    assert job_status == "done"
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (body["lead_id"],))
        status_row = cur.fetchone()
    assert status_row is not None and status_row[0] == LeadStatus.SHADOW_EVALUATED

    receipt = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    assert receipt["decision"]["recommended_action"] == str(expected.decision)
    assert receipt["decision"]["final_status"] == LeadStatus.SHADOW_EVALUATED
    assert receipt["decision"]["human_override"] is False


def _pipeline_today(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT leads_processed FROM v_pipeline_metrics WHERE day = {_TODAY}")
        row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def test_shadow_leads_are_excluded_from_pipeline_metrics(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    runtime: SimulatedEnrichmentRuntime,
    handlers: dict[str, JobHandler],
    job_queue: PostgresJobQueue,
    shadow_pool: ConnectionPool,
    leads: list[EvalLead],
) -> None:
    """`v_pipeline_metrics.leads_processed` must not move for a shadow lead,
    but must still be able to move at all -- paired with a normal-mode control
    so 'unchanged' can't pass vacuously (this suite's own established idiom,
    see tests/integration/test_cost_ledger_integration.py)."""
    corpus_lead, _ = _corpus_lead_with_route(runtime, leads, "auto_route")

    before_shadow = _pipeline_today(db_conn)
    shadow_body = _ingest(api_client, cleanup_ingest, corpus_lead, mode="shadow")
    assert (
        _drive_to_completion(job_queue, shadow_pool, handlers, db_conn, shadow_body["job_id"])
        == "done"
    )
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, shadow_body)
    after_shadow = _pipeline_today(db_conn)
    assert after_shadow == before_shadow, "a shadow lead must not inflate leads_processed"

    another_corpus_lead, _ = _corpus_lead_with_route(runtime, leads, "escalate_human")
    normal_body = _ingest(api_client, cleanup_ingest, another_corpus_lead, mode="normal")
    assert (
        _drive_to_completion(job_queue, shadow_pool, handlers, db_conn, normal_body["job_id"])
        == "done"
    )
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, normal_body)
    after_normal = _pipeline_today(db_conn)
    assert after_normal == after_shadow + 1, "the metric must still be able to move at all"
